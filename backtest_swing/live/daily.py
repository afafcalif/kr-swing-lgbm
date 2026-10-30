"""
일봉: 전날까지 추가분 수집(시세 조회 TR 만) + 장 전 메모리 적재 (EXEC_SPEC 1-1, 2-4, 8장 6)

  python backtest_swing/live/daily.py update   # 백테스트 일봉 파일 마지막 날 다음 ~ 어제: ka10081 수정주가·원주가 + 한국거래소 공식 시세 보정 + ka20006 지수

백테스트 일봉 파일(data/daily_ext·daily_merged·daily_raw, 지수 파일)은 고치지 않는다. 추가분은 data/live_append/ 에 따로 두고
load_stock 과 같은 계산에 이어 붙인다.
- 추가분 행: 수정주가 = ka10081 수정주가(upd_stkpc_tp 1), 원주가 = ka10081 원주가(0), 2026-09-14 이후라 고가·저가·종가는
  krx_official.apply_regular_session 으로 공식 정규장 값, 거래대금은 백만원 -> 원, factor = 수정주가 종가 / 원주가 종가.
- 원주가 파일에 수정주가 파일 마지막 날 뒤의 행이 있으면(예: 10-19, factor 빈칸) 추가분으로 바꾼다.
- 수정주가 파일과 겹치는 최근 5거래일의 시가(장 마감 후 가격이 섞이지 않는 값)가 다르면 그 사이 수정 계수가 바뀐 것.
  11R B: 키움 시가 / 파일 시가 가 5일 내내 같은 비율 K 이고 그사이 KRX 기준가비(기준가 / 전날 종가, 여러 번이면 곱)와 0.2% 안이면
  "rebased" 에 K 를 기록하고 메모리 적재(Store) 때 기본 파일 행을 K 로 다시 맞춘다 (파일은 그대로). 공식 시세를 기다리는 날이 있으면
  "rebase_wait" (그날은 제외, reapply_official 때 확인). 비율이 일정하지 않거나 KRX 와 안 맞으면 예전처럼 "adj_changed" (그 종목 제외, 이유 기록).
- 11P: 수집 실패·빈 응답 종목은 끝에 느린 간격으로 두 번 더 받고(원인 원문 기록), 그래도 실패하면 이전 버전 행 + 공식 시세로 채운다
  (기준가 = 전날 종가 등 확실할 때만, 공식 시세를 기다리면 reapply_official 때). 못 채우면 fetch_error 그대로.
"""
import json
import os
import sys
import threading
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as _C  # noqa: E402
from common import APPEND_DIR, Caller, data_file, num, pinned_versions, publish_data, sd, s4, version_dir  # noqa: E402

KIWOOM_AMOUNT_UNIT = 1_000_000
CHECK_DAYS = 5
REBASE_TOL = 0.002  # 11R B: 키움 비율 K 와 KRX 기준가비의 허용 차이 (0.2%), 5일 비율이 일정한지도 이 값(또는 1원)으로 본다


def _cur_dir():
    """지금 데이터 버전 폴더. common.data_dir 를 부를 때마다 찾는다 (features_eod 등이 잠시 버전을 고정하는 동안 이 모듈을 처음 불러도 그 고정이 남지 않게)"""
    return _C.data_dir(APPEND_DIR)


def file_last_dates(code):
    p = os.path.join(sd.EXT_DIR, f"{code}.parquet")
    path = p if os.path.exists(p) else os.path.join(sd.MERGED_DIR, f"{code}.parquet")
    adj = pd.read_parquet(path)
    adj["timestamp"] = pd.to_datetime(adj.timestamp)
    return adj


RETRY_PASSES = 2    # 11P: 일봉 갱신 끝에 실패·빈 응답 종목만 다시 받는 횟수
RETRY_WAIT = 30.0   # 다시 받기 전 쉬는 초
RETRY_GAP = 1.0     # 다시 받을 때 종목 사이 간격 (초, 느리게)
CARRY = ("carry_adj.parquet", "carry_raw.parquet")  # 11P: 수집 실패 종목의 이전 버전 추가분 행 (공식 값 채움의 바탕)
CARRY_META = "carry_meta.json"  # 11R 보완 1: 그 종목들의 이전 버전 rebase 기록 {code: rebased 항목 (k·근거)} (carry 행과 함께 보관)


def _daily(caller, code, upd, today):
    """ka10081 한 번. 11P: return_code 가 0 이 아니거나 행이 없으면 ValueError (원인 원문: return_code·return_msg·목록 행 수)"""
    j, _, _ = caller.post("ka10081", "/api/dostk/chart", {"stk_cd": code, "base_dt": today, "upd_stkpc_tp": upd})
    rc, msg, items = j.get("return_code"), str(j.get("return_msg", ""))[:80], j.get("stk_dt_pole_chart_qry") or []
    if rc not in (None, 0, "0"):
        raise ValueError(f"upd{upd} return_code {rc} {msg}")
    rows = [{"timestamp": pd.Timestamp(it["dt"]), "open": num(it.get("open_pric")), "high": num(it.get("high_pric")),
             "low": num(it.get("low_pric")), "close": num(it.get("cur_prc")), "volume": num(it.get("trde_qty")),
             "amount": num(it.get("trde_prica")) * KIWOOM_AMOUNT_UNIT}
            for it in items if it.get("dt") and it["dt"] < today]
    if not rows:
        raise ValueError(f"empty: upd{upd} return_code {rc} {msg!r} 목록 {len(items)}행")
    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


def carry_meta(u=None):
    """11R 보완 1: carry 행 종목의 rebase 기록 = 지난 carry 기록(carry_meta.json, 이틀 넘게 실패) + 지금 버전 update.json rebased (지금 버전 기록 우선).
    11S: 지금 버전에 행이 있는 종목(장중 재수집으로 돌아온 종목 포함)은 지금 버전 rebased 만 본다 (지난 carry 기록이 남아 있어도)"""
    try:
        with open(os.path.join(APPEND_DIR, CARRY_META), encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}
    dd = os.path.dirname(data_file("update.json", APPEND_DIR))
    if u is None:
        u = _update_json(dd)
    try:
        have = set(pd.read_parquet(os.path.join(dd, "adj.parquet"), columns=["code"]).code)
    except (OSError, ValueError, KeyError):
        have = set()
    old = {c: v for c, v in {**old, **(u.get("carry_rebased") or {})}.items() if c not in have}
    return {**old, **(u.get("rebased") or {})}


def _carry_source():
    """11P: 지금 버전 추가분 행 + 지난 carry 파일 (지금 버전에 없는 종목만, 이틀 넘게 실패한 종목). 반환 (adj, raw) 또는 (None, None)"""
    pa, pr = data_file("adj.parquet", APPEND_DIR), data_file("raw.parquet", APPEND_DIR)
    a = pd.read_parquet(pa) if os.path.exists(pa) and os.path.exists(pr) else None
    r = pd.read_parquet(pr) if a is not None else None
    ca, cr = (os.path.join(APPEND_DIR, n) for n in CARRY)
    if os.path.exists(ca) and os.path.exists(cr):
        oa, orw = pd.read_parquet(ca), pd.read_parquet(cr)
        if a is None:
            a, r = oa, orw
        else:
            a = pd.concat([a, oa[~oa.code.isin(set(a.code))]], ignore_index=True)
            r = pd.concat([r, orw[~orw.code.isin(set(r.code))]], ignore_index=True)
    if a is not None:
        a["timestamp"], r["timestamp"] = pd.to_datetime(a.timestamp), pd.to_datetime(r.timestamp)
    return a, r


def official_with_chg(start, end):
    """공식 시세 start ~ end (krx_official.load_range 캐시). 전일대비(chg)가 없는 날(11P 전 캐시)은 그날만 다시 받아 캐시를 바꾼다"""
    from kiwoom import krx_official
    off = krx_official.load_range(start, end)
    if not len(off):
        return off
    if "chg" not in off:
        off["chg"] = float("nan")
    redo = [d for d, g in off.groupby("date") if g.chg.isna().all()]
    if redo:
        parts = [off[~off.date.isin(redo)]]
        for d in redo:
            new = krx_official.fetch_day(f"{pd.Timestamp(d):%Y%m%d}")
            if len(new):
                new.to_parquet(os.path.join(krx_official.CACHE_DIR, f"{pd.Timestamp(d):%Y%m%d}.parquet"), index=False)
                parts.append(new)
            else:
                parts.append(off[off.date == d])
        off = pd.concat(parts, ignore_index=True)
    return off


def inherit_k(code, r, meta):
    """11R 보완 1: carry 행(r: 그 종목 원주가 추가분 행, 기본 파일 마지막 날 뒤)의 기준 = 기본 파일 행 x K 인지 확인.
    carry 첫 행의 키움 factor 와 기본 파일 마지막 행 factor 로 K_carry = 첫 행 factor x (파일 마지막 날 뒤 ~ 첫 행 날 KRX 기준가비) / 파일 factor
    (키움 factor = 그 뒤 이벤트 비율 곱). 기대 K = 이전 버전 rebase 기록의 k (기록 없으면 1). 0.2%(REBASE_TOL) 안이면 승계.
    반환 (K, None) 또는 (None, 못 채우는 이유)"""
    want = float(meta["k"]) if meta else 1.0
    try:
        fa = file_last_dates(code)
        last = fa.timestamp.max()
        rf = pd.read_parquet(os.path.join(sd.RAW_DIR, f"{code}.parquet"))
        row = rf[pd.to_datetime(rf.timestamp) == last]
    except Exception as e:  # noqa: BLE001
        return None, f"기본 파일 못 읽음 ({e!r})"[:160]
    f0 = float(row.factor.iat[0]) if len(row) and pd.notna(row.factor.iat[0]) else float("nan")
    x = r[r.timestamp > last].sort_values("timestamp")
    if not len(x) or not (f0 > 0) or not (float(x.factor.iat[0]) > 0):
        return None, f"이전 계수 {want:.6f} 승계 확인 못 함 (기본 파일 factor {f0}, carry 첫 행 없음·factor 없음)"
    fc, c1 = float(x.factor.iat[0]), x.timestamp.iat[0]
    k_c = fc / f0
    if abs(k_c / want - 1) > REBASE_TOL:  # 첫 행 날 자체가 이벤트면 KRX 기준가비를 곱해 다시
        off = official_with_chg(last + pd.Timedelta(days=1), c1)
        kk, ev, why = krx_base_ratio(off[off.code == code] if len(off) else None, last, float(row.close.iat[0]), c1)
        if why:
            return None, f"이전 계수 {want:.6f} ≠ carry 첫 행 계수 {k_c:.6f}, KRX 기준가비 확인 못 함 ({why})"
        k_c *= kk
    if abs(k_c / want - 1) > REBASE_TOL:
        return None, (f"이전 계수 {want:.6f} ≠ carry 행 기준 {k_c:.6f} (승계 확인 못 함)" if meta
                      else f"이전 계수 기록 없음, carry 행 기준 {k_c:.6f} ≠ 기본 파일 (승계 확인 못 함)")
    return want, None


K_RANGE = (0.01, 100.0)  # 11S C3: 그날 이벤트 계수(기준가 / 전날 종가)가 이 밖이면 비정상 (공식 값으로 채우지 않음)


def fill_official(codes, carry_a, carry_r, up_to, days=(), meta=None):
    """11P: 일봉 수집에 끝내 실패한 종목의 추가분을 이전 버전 행(carry) + 공식 시세로 채운다. 확실할 때만:
    - 이전 행이 있고 마지막 행의 factor 가 1 이며 수정주가 행 = 원주가 행 (그 뒤로 수정 계수 바뀔 일 없었음)
    - 이전 행 마지막 날 L 뒤 ~ up_to 의 거래일(공식 시세 날짜 + days(지수 일봉 날짜)) 모두 공식 시세에 그 종목 행이 있고
      거래량 > 0·시가 > 0 (거래정지일은 키움 일봉 모양을 모르므로 안 채움)
    - 그날 기준가(= 종가 - 전일대비)가 있음
    11S C3: 기준가 / 전날 종가 = r 가 0.5%(engine.BASE_EVENT) 넘게 1 과 다르면 그날 이벤트(권리락·분할·병합 등) -> 그 앞 이력을 r 로 다시 맞춰 채움
    (11R 과 같은 방식: 이전 행·이벤트 앞 채운 행의 수정주가 가격 x r 원 반올림, 수정 거래량 / r 반올림, 원주가 factor x r, 기본 파일 행은
    update.json rebased 의 k = 승계한 이전 계수 x r 로 Store 가). 0.5% 미만 차이는 이벤트로 보지 않음 (스팩류 기준가 미세 조정, 키움도 계수 안 바꿈).
    못 채움 (그 종목만 제외): 기준가 없음·0 이하, 그날 거래 없음, 이벤트인데 전날 거래 없음(거래정지 뒤 재개·재상장 경매), 계수 비정상(K_RANGE 밖).
    채운 행: 원주가 = 공식 시가·고가·저가·종가·거래량, 거래대금 = 공식 거래대금을 백만원 반올림(키움 단위), factor = 그 뒤 이벤트 곱 (없으면 1),
    수정주가 = 원주가 x factor (원 반올림, factor 1 이면 원주가 그대로).
    11R 보완 1: carry 행이 다시 맞춘 기준(기본 파일 x K)이면 그 K 를 승계해야 기본 파일 행과 이어진다 -> meta {code: 이전 rebase 기록} 의 k 를
    carry 행 자체의 factor 로 확인(inherit_k)해 맞으면 승계, 확인 못 하면 그 종목은 안 채움 (이유 기록).
    반환 (adj 행, raw 행, {code: 채운 정보 (계수가 1 이 아니면 rebased = update.json rebased 항목, k = 승계 계수 x 이벤트 곱)}, {code: 못 채운 이유})"""
    from engine import BASE_EVENT
    up_to = pd.Timestamp(up_to)
    filled, unfilled, pa, pr = {}, {}, [], []
    if carry_a is None or not len(codes):
        return pd.DataFrame(), pd.DataFrame(), filled, {c: "이전 추가분 행 없음" for c in codes}
    by_a, by_r = dict(tuple(carry_a.groupby("code"))), dict(tuple(carry_r.groupby("code")))
    lasts = {c: by_a[c].timestamp.max() for c in codes if c in by_a and c in by_r}
    off = official_with_chg(min(lasts.values()) + pd.Timedelta(days=1), up_to) if lasts and min(lasts.values()) < up_to else pd.DataFrame()
    off_dates = set(pd.to_datetime(off.date)) if len(off) else set()
    off_by = dict(tuple(off.groupby("code"))) if len(off) else {}
    cols = ["timestamp", "open", "high", "low", "close", "volume", "amount"]
    for c in codes:
        if c not in lasts:
            unfilled[c] = "이전 추가분 행 없음"
            continue
        a = by_a[c].drop(columns=["code"]).sort_values("timestamp")
        r = by_r[c].drop(columns=["code"]).sort_values("timestamp")
        a, r = a[a.timestamp <= up_to], r[r.timestamp <= up_to]
        la, lr = a.iloc[-1], r.iloc[-1]
        if la.timestamp != lr.timestamp or not np.isclose(float(lr.get("factor", np.nan)), 1.0, rtol=0, atol=1e-9)                 or any(float(la[k]) != float(lr[k]) for k in cols[1:]):
            unfilled[c] = f"이전 마지막 행 {la.timestamp.date()} factor 1 아님 또는 수정주가 ≠ 원주가"
            continue
        m = (meta or {}).get(c)
        k_in, why_k = inherit_k(c, r, m)
        if why_k:
            unfilled[c] = why_k
            continue
        need = sorted(d for d in off_dates | {pd.Timestamp(x) for x in days} if la.timestamp < d <= up_to)
        o = off_by.get(c)
        o = o.set_index(pd.to_datetime(o.date)) if o is not None else None
        rows, prev, prev_vol, why, events = [], float(lr.close), float(lr.volume), None, []
        for d in need:
            if o is None or d not in o.index:
                why = f"{d.date()} 공식 시세에 종목 없음"
                break
            x = o.loc[d]
            if not (x.volume > 0 and x.open > 0):
                why = f"{d.date()} 거래 없음 (거래량 {x.volume}, 시가 {x.open})"
                break
            base = float(x.close) - float(x.chg) if np.isfinite(x.chg) else float("nan")
            if not (base > 0 and prev > 0):
                why = f"{d.date()} 기준가 {f'{base} 비정상' if np.isfinite(base) else '모름 (전일대비 없음)'} (전날 종가 {prev})"
                break
            k = base / prev
            if abs(k - 1) >= BASE_EVENT:  # 그날 이벤트 (권리락·분할·병합 등)
                if not prev_vol > 0:
                    why = f"{d.date()} 이벤트(기준가 {base} / 전날 종가 {prev} = {k:.6f})인데 전날 거래 없음 (거래정지 뒤 재개·재상장 경매)"
                    break
                if not K_RANGE[0] <= k <= K_RANGE[1]:
                    why = f"{d.date()} 계수 비정상 (기준가 {base} / 전날 종가 {prev} = {k:.6f})"
                    break
                events.append([str(d.date()), round(k, 6), k])
            rows.append({"timestamp": d, "open": float(x.open), "high": float(x.high), "low": float(x.low), "close": float(x.close),
                         "volume": float(x.volume), "amount": float(np.floor(float(x.amount) / KIWOOM_AMOUNT_UNIT + 0.5) * KIWOOM_AMOUNT_UNIT)})
            prev, prev_vol = float(x.close), float(x.volume)
        if why:
            unfilled[c] = why
            continue
        new = pd.DataFrame(rows, columns=cols)
        # 각 행의 factor = 그 행 뒤 이벤트 계수 곱 (마지막 이벤트 뒤 행 = 1). 이전 행(carry)은 모든 이벤트 곱
        fac = np.array([float(np.prod([e[2] for e in events if pd.Timestamp(e[0]) > t])) for t in new.timestamp]) if len(new) else np.array([])
        ktot = float(np.prod([e[2] for e in events])) if events else 1.0
        na, nr = new.copy(), new.assign(factor=fac if len(new) else 1.0)
        a_, r_ = a[cols].copy(), r[cols + ["factor"]].copy()
        if events:  # 11R rebase_rows 와 같은 반올림 (수정주가 원 단위, 수정 거래량 / 계수)
            for k_ in ("open", "high", "low", "close"):
                a_[k_] = np.floor(a_[k_].astype(float) * ktot + 0.5)
                na[k_] = np.where(fac != 1, np.floor(na[k_].astype(float) * fac + 0.5), na[k_])
            a_["volume"] = np.floor(a_.volume.astype(float) / ktot + 0.5)
            na["volume"] = np.where(fac != 1, np.floor(na.volume.astype(float) / fac + 0.5), na.volume)
            r_["factor"] = r_.factor * ktot
        pa.append(pd.concat([a_, na], ignore_index=True).assign(code=c))
        pr.append(pd.concat([r_, nr], ignore_index=True).assign(code=c))
        filled[c] = {"carry_last": str(la.timestamp.date()), "dates": [str(d.date()) for d in need], "last_close": prev}
        ktot_all = k_in * ktot
        if events or (m and abs(k_in - 1) > 1e-12):
            filled[c].update(events=[e[:2] for e in events], k=ktot, k_inherited=k_in if m else None, k_total=ktot_all)
        if abs(ktot_all - 1) > 1e-12:  # Store 가 기본 파일 행을 승계 계수 x 그날 이벤트 곱으로 다시 맞춤
            filled[c]["rebased"] = {**(m or {}), "k": ktot_all, "events": list((m or {}).get("events") or []) + [e[:2] for e in events],
                                    "inherited": ("공식 값 채움 (carry 행 factor 로 확인)" if m else None),
                                    **({"fill_events": [e[:2] for e in events], "k_before_fill": k_in} if events else {})}
    cat = (lambda ps: pd.concat(ps, ignore_index=True) if ps else pd.DataFrame())
    return cat(pa), cat(pr), filled, unfilled


# ---------------------------------------------------------------- 11R B: 계수 바뀐 종목 이력 다시 맞추기

def kiwoom_ratio(chk):
    """키움 시가 / 파일 시가 (chk: open·open_file, 겹치는 최근 5일). 5일 내내 같은 비율인가: K = 비율 중앙값, 날마다
    |키움 시가 - 파일 시가 x K| <= max(1원, 0.2%) (키움 새 이력은 원 단위 반올림). 파일 시가 0 인 날은 키움도 0 이어야 함.
    반환 (K 또는 None, 이유 또는 None)"""
    fo, ko = chk.open_file.to_numpy(float), chk.open.to_numpy(float)
    pos = fo > 0
    if not pos.any() or (ko[~pos] != 0).any():
        return None, f"파일 시가 0 인 날 키움 시가 {ko[~pos].tolist()}" if pos.any() else "파일 시가가 모두 0"
    k = float(np.median(ko[pos] / fo[pos]))
    dev = np.abs(ko[pos] - fo[pos] * k)
    if not (k > 0) or (dev > np.maximum(1.0, REBASE_TOL * fo[pos] * k)).any():
        return None, f"5일 비율이 일정하지 않음 {np.round(ko[pos] / fo[pos], 6).tolist()}"
    return k, None


def krx_base_ratio(off, after, prev_close, up_to):
    """KRX 공식 시세(그 종목: date·close·chg)로 after 뒤 ~ up_to 의 기준가비 곱. 기준가 = 종가 - 전일대비, 비율 = 기준가 / 전날 종가
    (첫날의 전날 종가 = 파일 마지막 날 원주가 종가 prev_close). 0.5%(engine.BASE_EVENT) 넘게 다른 날만 곱함 (그 아래는 키움도 계수 안 바꿈).
    반환 (K, [[날짜, 비율]], 이유 또는 None)"""
    from engine import BASE_EVENT
    if off is None or not len(off):
        return 1.0, [], "그사이 공식 시세에 종목 없음"
    o = off.assign(date=pd.to_datetime(off.date)).sort_values("date")
    o = o[(o.date > pd.Timestamp(after)) & (o.date <= pd.Timestamp(up_to))]
    if not len(o):
        return 1.0, [], "그사이 공식 시세에 종목 없음"
    k, events, prev = 1.0, [], float(prev_close)
    for x in o.itertuples():
        chg = float(x.chg) if "chg" in o and pd.notna(x.chg) else float("nan")
        if not np.isfinite(chg) or not prev > 0:
            return k, events, f"{x.date.date()} 전일대비 없음"
        base = float(x.close) - chg
        if float(x.close) > 0 and base > 0:  # 종가 0·기준가 0 이하(시세 이상)는 그날 정보 없음
            r = base / prev
            if abs(r - 1) >= BASE_EVENT:
                k *= r
                events.append([str(x.date.date()), round(r, 6)])
            prev = float(x.close)
    return k, events, None


def rebase_decide(k_kw, k_krx, events, why_krx):
    """키움 비율 K 와 KRX 기준가비 곱 비교 -> (다시 맞출 K 또는 None, 이유). 다시 맞출 때는 KRX 값 (키움 계수 변화 = 기준가 / 전날 종가)"""
    if why_krx:
        return None, f"키움 비율 {k_kw:.6f}, KRX 기준가비 확인 못 함: {why_krx}"
    if abs(k_kw / k_krx - 1) > REBASE_TOL:
        return None, f"키움 비율 {k_kw:.6f} ≠ KRX 기준가비 {k_krx:.6f} {events} (0.2% 넘음, 오늘 이벤트면 다음 갱신 때)"
    return k_krx, None


def rebase_rows(adj, raw, k):
    """기본 파일 행(수정주가 파일 마지막 날까지)을 새 기준으로: 수정주가 가격 x K 반올림, 수정 거래량 / K 반올림, 원주가 factor x K
    (거래대금·원주가 가격·원주가 거래량은 그대로). 키움이 계수를 바꿔 다시 준 이력과 같은 기준 (2026-02 백업 대조: 정수 88.5% 같음, 모두 0.17% 안)"""
    a, r = adj.copy(), raw.copy()
    last = a.timestamp.max()
    for c in ("open", "high", "low", "close"):
        a[c] = np.floor(a[c].astype(float) * k + 0.5)
    a["volume"] = np.floor(a.volume.astype(float) / k + 0.5)
    m = r.timestamp <= last
    r.loc[m, "factor"] = r.loc[m, "factor"] * k
    return a, r


def _update_json(dd):
    p = os.path.join(dd, "update.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def rebased_k(u=None):
    """지금 데이터 버전에서 기본 파일 행을 다시 맞출 종목 {code: K}"""
    if u is None:
        u = _update_json(os.path.dirname(data_file("update.json", APPEND_DIR)))
    return {c: float(v["k"]) for c, v in (u.get("rebased") or {}).items()}


class Collect:
    """한 종목 수집·반영 (아침 일봉 갱신과 11S 장중 재수집이 같이 씀): ka10081 수정주가·원주가 -> 파일과 겹치는 5일 시가 대조
    (11R B 계수 바뀜이면 KRX 기준가비로 다시 맞춤 확인) -> 공식 시세 기다리는 날 행 보관 -> 행 누락 확인(11K 5) -> 공식 정규장 값 덮어쓰기 -> factor.
    결과는 adj_parts·raw_parts·flags·pend_a·pend_r 에 쌓는다. keep 종목은 받은 키움 행을 fetched 에 남김 (11S D 다음 날 대조)"""

    def __init__(self, caller, today, yesterday, off_by, missing_dates, keep=()):
        self.caller, self.today, self.yesterday, self.off_by, self.missing_dates = caller, today, yesterday, off_by, missing_dates
        self.adj_parts, self.raw_parts, self.pend_a, self.pend_r = [], [], [], []
        self.flags = {"adj_changed": [], "no_official": [], "fetch_error": [], "no_new_rows": [], "missing_row": []}
        self.flags.update(adj_changed_why={}, rebased={}, rebase_wait={})  # 11R B
        self.off_chg = {}  # 공식 시세(전일대비 포함) 시작 날짜별, 계수 바뀐 종목이 있을 때만 읽음
        self.keep, self.fetched = set(keep), {}

    def rebase_check(self, code, chk, last):
        """11R B: 계수가 바뀐 종목. 반환 True = 추가분을 붙임 (rebased 또는 rebase_wait), False = adj_changed (이유 기록)"""
        flags = self.flags
        k_kw, why = kiwoom_ratio(chk)
        if why is None:
            rf = pd.read_parquet(os.path.join(sd.RAW_DIR, f"{code}.parquet"))
            pc = rf[pd.to_datetime(rf.timestamp) == last].close
            info = {"k_kiwoom": round(k_kw, 6), "file_last": str(last.date()), "file_close": float(pc.iat[0]) if len(pc) else float("nan")}
            if self.missing_dates:  # 공식 시세 없는 날에 이벤트가 있었을 수 있음 -> 재반영 때 확인 (그때까지 제외)
                flags["rebase_wait"][code] = info
                return True
            start = last + pd.Timedelta(days=1)
            if start not in self.off_chg:
                self.off_chg[start] = dict(tuple(official_with_chg(start, self.yesterday).groupby("code"))) if start <= self.yesterday else {}
            k, ev, why_k = krx_base_ratio(self.off_chg[start].get(code), last, info["file_close"], self.yesterday)
            k2, why = rebase_decide(k_kw, k, ev, why_k)
            if why is None:
                flags["rebased"][code] = {"k": k2, "events": ev, **info}
                return True
        flags["adj_changed"].append(code)
        flags["adj_changed_why"][code] = why
        return False

    def one(self, code):
        """한 종목 수집·반영. 반환: 수집 실패 원인(문자열) 또는 None (반영·다른 사유로 뺌)"""
        try:
            fa = file_last_dates(code)
            last = fa.timestamp.max()
            a = _daily(self.caller, code, "1", self.today)
            r = _daily(self.caller, code, "0", self.today)
        except ValueError as e:
            return f"{pd.Timestamp.now():%H:%M:%S} {e}"[:160]
        if code in self.keep:
            self.fetched[code] = {"a": a, "r": r}
        chk = a[a.timestamp <= last].tail(CHECK_DAYS).merge(fa[["timestamp", "open"]], on="timestamp", suffixes=("", "_file"))
        if len(chk) and not np.allclose(chk.open, chk.open_file, rtol=1e-9, atol=0) and not self.rebase_check(code, chk, last):
            return None
        self._append(code, a, r, last)
        return None

    def _append(self, code, a, r, last):
        from kiwoom import krx_official
        md = self.missing_dates
        na, nr = a[a.timestamp > last].copy(), r[r.timestamp > last].copy()
        if md:
            # 공식 시세 기다리는 날의 키움 행은 따로 보관 (공식 시세가 나오면 reapply_official 이 보정해 붙임)
            self.pend_a.append(na[na.timestamp >= min(md)].assign(code=code))
            self.pend_r.append(nr[nr.timestamp >= min(md)].assign(code=code))
            na, nr = na[na.timestamp < min(md)], nr[nr.timestamp < min(md)]
        off = self.off_by.get(code)
        lack = lacking_rows(off, na.timestamp, last, min(md) if md else None)
        if lack:  # 11K 5: 공식 시세에는 거래가 있는 날인데 우리 일봉(키움)에 그 행이 없음 -> 그 종목은 붙이지 않고 그날 계산에서 제외
            self.flags["missing_row"].append([code, lack])
            return
        if na.empty:
            self.flags["no_new_rows"].append(code)
            return
        if off is not None:
            na, _ = krx_official.apply_regular_session(na, off)
            nr, _ = krx_official.apply_regular_session(nr, off)
        else:
            self.flags["no_official"].append(code)
        nr = nr.merge(na[["timestamp", "close"]].rename(columns={"close": "adj_close"}), on="timestamp", how="left")
        nr["factor"] = nr.adj_close / nr.close
        if code in self.fetched:
            self.fetched[code].update(na=na, nr=nr.drop(columns=["adj_close"]))
        self.adj_parts.append(na.assign(code=code))
        self.raw_parts.append(nr.drop(columns=["adj_close"]).assign(code=code))


def update(only=None, check=None):
    """아침 일봉 갱신. check = {"day": 전날, "base_events": {code: k}} (11S D: 전날 15:18 그날 계수, 상주 프로그램이 넘김)"""
    from kiwoom import krx_official
    os.makedirs(APPEND_DIR, exist_ok=True)
    t0 = time.time()
    today = pd.Timestamp.now().strftime("%Y%m%d")
    yesterday = pd.Timestamp(today) - pd.Timedelta(days=1)
    caller = Caller("prod", 5)
    # 지수 (ka20006, 값은 100배)
    kfile = {"kospi": sd.KOSPI, "kosdaq": s4.KOSDAQ}
    ix_rows, ix_note = [], {}
    for name, inds in (("kospi", "001"), ("kosdaq", "101")):
        j, _, _ = caller.post("ka20006", "/api/dostk/chart", {"inds_cd": inds, "base_dt": today})
        d = pd.DataFrame([{"timestamp": pd.Timestamp(it["dt"]), "open": num(it["open_pric"]) / 100, "high": num(it["high_pric"]) / 100,
                           "low": num(it["low_pric"]) / 100, "close": num(it["cur_prc"]) / 100}
                          for it in j.get("inds_dt_pole_qry", []) if it.get("dt") and it["dt"] < today])
        f = pd.read_parquet(kfile[name])
        f["timestamp"] = pd.to_datetime(f.timestamp)
        last = f.timestamp.max()
        ov = d.merge(f[["timestamp", "close"]], on="timestamp", suffixes=("", "_file"))
        ov = ov[ov.timestamp > last - pd.Timedelta(days=30)]
        ix_note[name] = {"file_last": str(last.date()), "overlap_days": len(ov),
                         "overlap_max_abs_diff": float((ov.close - ov.close_file).abs().max()) if len(ov) else None}
        new = d[d.timestamp > last].assign(name=name)
        ix_rows.append(new)
    ix = pd.concat(ix_rows, ignore_index=True)
    print(f"index: {ix_note}, new rows {ix.groupby('name').timestamp.agg(['min', 'max', 'size']).to_dict('index')}", flush=True)

    codes = only or sd.universe_codes()
    start = min(pd.to_datetime(ix.timestamp)) if len(ix) else yesterday
    official = krx_official.load_range(start, yesterday)
    off_by = dict(tuple(official.groupby("code"))) if len(official) else {}
    # 공식 시세가 아직 없는 날은 키움 일봉(장 마감 후 가격 섞임)을 넣지 않는다 -> 그날까지 미반영으로 남김 (2026-10-22 결정)
    new_dates = sorted(pd.to_datetime(ix.timestamp).unique()) if len(ix) else []
    # 실제 마지막 거래일 (지수 일봉 기준, 아래에서 공식 시세 없는 날을 빼기 전) -> 전날 일봉 반영 여부 판단에 쓴다
    latest_trading_day = str(pd.Timestamp(max(new_dates)).date()) if new_dates else max(ix_note[n]["file_last"] for n in ix_note)
    off_dates = set(pd.to_datetime(official.date).unique()) if len(official) else set()
    missing_dates = [pd.Timestamp(d_) for d_ in new_dates if pd.Timestamp(d_) not in off_dates]
    received = {}
    for d_ in new_dates:
        cp = os.path.join(krx_official.CACHE_DIR, f"{pd.Timestamp(d_):%Y%m%d}.parquet")
        received[str(pd.Timestamp(d_).date())] = pd.Timestamp(os.path.getmtime(cp), unit="s", tz="UTC").tz_convert("Asia/Seoul").strftime("%Y-%m-%d %H:%M:%S") \
            if os.path.exists(cp) else None
    # 11S D: 전날 15:18 에 쓴 고정 버전 (없으면 갱신 전 지금 버전) = 다음 날 대조 대상
    prev_dir = version_dir(pinned_versions(APPEND_DIR).get(latest_trading_day), append_dir=APPEND_DIR) or _cur_dir()
    prev_u = _update_json(prev_dir)
    targets = next_day_targets(prev_u, check, latest_trading_day)
    carry_a, carry_r = _carry_source()  # 11P: 지우기 전에 읽음 (이번에 실패한 종목의 바탕)
    cmeta = carry_meta()  # 11R 보완 1: 그 종목들의 이전 rebase 기록 (carry 행과 함께)
    for p_ in ("pending_adj.parquet", "pending_raw.parquet", "pending_index.parquet") + CARRY + (CARRY_META,):
        if os.path.exists(os.path.join(APPEND_DIR, p_)):
            os.remove(os.path.join(APPEND_DIR, p_))
    if missing_dates:
        first_missing = min(missing_dates)
        ix[pd.to_datetime(ix.timestamp) >= first_missing].to_parquet(os.path.join(APPEND_DIR, "pending_index.parquet"), index=False)
        ix = ix[pd.to_datetime(ix.timestamp) < first_missing]
        print(f"official missing {[str(x.date()) for x in missing_dates]} -> rows from {first_missing.date()} not appended", flush=True)
    print(f"official: {official.date.nunique() if len(official) else 0} days {sorted(official.date.dt.strftime('%Y-%m-%d').unique()) if len(official) else []}", flush=True)
    col = Collect(caller, today, yesterday, off_by, missing_dates, keep=set().union(*[set(v) for v in targets.values()]))
    adj_parts, raw_parts, flags, pend_a, pend_r = col.adj_parts, col.raw_parts, col.flags, col.pend_a, col.pend_r

    fails = {}
    for i, code in enumerate(codes):
        why = col.one(code)
        if why:
            fails[code] = why
        if (i + 1) % 250 == 0:
            print(f"  {i + 1}/{len(codes)} stocks, {caller.n} requests, {time.time() - t0:.0f}s, errors {len(caller.errors)}", flush=True)
    # 11P: 실패·빈 응답 종목만 쉬었다가 느린 간격으로 다시 (RETRY_PASSES 번). 결과·원인 원문 기록
    retries = []
    for n in range(1, RETRY_PASSES + 1):
        if not fails:
            break
        time.sleep(RETRY_WAIT)
        for code in list(fails):
            time.sleep(RETRY_GAP)
            why = col.one(code)
            retries.append({"pass": n, "code": code, "before": fails[code], "result": why or "ok"})
            if why:
                fails[code] = why
            else:
                fails.pop(code)
        print(f"retry pass {n}: still failing {sorted(fails)}", flush=True)
    # 11P: 그래도 실패한 종목은 이전 버전 행 + 공식 시세로 채움 (공식 시세가 다 있을 때만, 없으면 reapply_official 이 나중에)
    # 11S C3: 그날 이벤트면 계수 = 기준가 / 전날 종가 로 다시 맞춰 채움 (기본 파일 행은 rebased 로 Store 가)
    filled, unfilled = {}, {}
    if fails and not missing_dates and len(ix):
        fa_, fr_, filled, unfilled = fill_official(sorted(fails), carry_a, carry_r, pd.Timestamp(latest_trading_day), new_dates, meta=cmeta)
        if len(fa_):
            adj_parts.append(fa_)
            raw_parts.append(fr_)
        for c in filled:
            fails.pop(c)
            if "rebased" in filled[c]:
                flags["rebased"][c] = filled[c]["rebased"]
    flags["fetch_error"] = [[c, w] for c, w in sorted(fails.items())]
    if fails and carry_a is not None:  # 남은 실패 종목의 바탕 행 보관 (공식 시세 재반영·다음 날)
        ca_, cr_ = carry_a[carry_a.code.isin(list(fails))], carry_r[carry_r.code.isin(list(fails))]
        if len(ca_):
            ca_.to_parquet(os.path.join(APPEND_DIR, CARRY[0]), index=False)
            cr_.to_parquet(os.path.join(APPEND_DIR, CARRY[1]), index=False)
    carry_rb = {c: cmeta[c] for c in fails if c in cmeta}  # 11R 보완 1: 남은 실패 종목의 rebase 기록도 carry 행과 함께 (재반영·다음 날 채움 때 승계)
    if carry_rb:
        with open(os.path.join(APPEND_DIR, CARRY_META), "w", encoding="utf-8") as f:
            json.dump(carry_rb, f, ensure_ascii=False, indent=1, default=str)
    cols_a = ["timestamp", "open", "high", "low", "close", "volume", "amount", "code"]
    adj = pd.concat(adj_parts, ignore_index=True) if adj_parts else pd.DataFrame(columns=cols_a)
    raw = pd.concat(raw_parts, ignore_index=True) if raw_parts else pd.DataFrame(columns=cols_a[:-1] + ["factor", "code"])
    adj["timestamp"], raw["timestamp"] = pd.to_datetime(adj.timestamp), pd.to_datetime(raw.timestamp)
    if pend_a:
        pd.concat(pend_a, ignore_index=True).to_parquet(os.path.join(APPEND_DIR, "pending_adj.parquet"), index=False)
        pd.concat(pend_r, ignore_index=True).to_parquet(os.path.join(APPEND_DIR, "pending_raw.parquet"), index=False)
    try:  # 11S D: 기록만 (실패해도 갱신은 그대로)
        ndc = next_day_check(prev_dir, col.fetched, targets, latest_trading_day)
    except Exception as e:  # noqa: BLE001
        ndc = {"error": repr(e)[:300]}
    rec = {"run": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), "codes": len(codes), "requests": caller.n,
           "seconds": round(time.time() - t0, 1), "errors": caller.errors, "stopped": caller.stopped, "hourly": dict(caller.hourly),
           "index": ix_note, "adj_rows": len(adj), "dates": sorted(adj.timestamp.dt.strftime("%Y-%m-%d").unique().tolist()),
           "factor_not_1": int((raw.factor.sub(1).abs() > 1e-9).sum()),
           "official_received": received, "official_missing_dates": [str(x.date()) for x in missing_dates],
           "latest_trading_day": latest_trading_day,
           # 데이터 버전 (2026-10-24 결정 B6): 반영 시각 | 마지막 반영 날짜 | 공식 시세 없는 날
           "data_version": f"{pd.Timestamp.now():%Y%m%d%H%M%S}|{(sorted(adj.timestamp.dt.strftime('%Y-%m-%d').unique().tolist()) or ['-'])[-1]}"
                           f"|missing={','.join(str(x.date()) for x in missing_dates) or '-'}",
           "fetch_retry": retries, "official_filled": filled, "fill_unfilled": unfilled, "carry_rebased": carry_rb, "next_day_check": ndc,
           **{k: v for k, v in flags.items()}}
    # 확인 리뷰 15: 지수·adj·raw·update.json 을 새 버전 폴더에 다 쓴 뒤 포인터만 교체
    with PUBLISH_LOCK:
        v = publish_data({"index.parquet": ix, "adj.parquet": adj, "raw.parquet": raw, "update.json": rec}, APPEND_DIR)
    print(f"data version {v}", flush=True)
    print(json.dumps({k: (v if not isinstance(v, list) or len(v) < 20 else f"{len(v)} items") for k, v in rec.items()},
                     ensure_ascii=False, default=str), flush=True)


# ---------------------------------------------------------------- 11S C2: 장중 키움 재수집

PUBLISH_LOCK = threading.Lock()  # 같은 프로세스 안에서 버전 공개를 한 번에 하나씩 (장중 재수집·공식 시세 재반영·아침 갱신)


def refetch(codes, stop_at=None):
    """11S C2: 07:00 에 실패한 종목(fetch_error·missing_row)만 키움 일봉을 다시 받아, 받은 종목은 아침 갱신과 같은 종목별 처리(Collect: 11R 계수 대조·
    다시 맞춤 -> 행 누락 확인 -> 공식 값 덮어쓰기)를 거쳐 지금 버전에서 그 종목 행·표시를 바꾼 새 버전을 공개한다 (다시 적재는 상주 프로그램이).
    공식 시세를 기다리는 날이 있으면 안 함 (재반영 뒤에). stop_at() 이 참이면 더 받지 않고 공개도 안 함 (다시 적재가 15:18 전에 끝나게).
    호출 제한기는 같은 프로세스의 공용 제한기, 연속 유량 실패는 이 재수집 Caller 가 따로 셈. 반환 dict (published = 새 버전 이름 또는 None)"""
    from kiwoom import krx_official
    t0 = time.time()
    codes = list(codes)
    u0 = _update_json(_cur_dir())
    out = {"codes": codes, "started": pd.Timestamp.now().strftime("%H:%M:%S"), "published": None}
    if u0.get("official_missing_dates"):
        return {**out, "ok": False, "reason": f"공식 시세 기다리는 날 {u0['official_missing_dates']} (재반영 뒤에)"}
    today = pd.Timestamp.now().strftime("%Y%m%d")
    yesterday = pd.Timestamp(today) - pd.Timedelta(days=1)
    f = pd.read_parquet(sd.KOSPI)
    start = pd.to_datetime(f.timestamp).max() + pd.Timedelta(days=1)  # 아침 갱신과 같은 공식 시세 구간 (지수 파일 마지막 날 뒤 ~ 어제)
    official = krx_official.load_range(start, yesterday)
    off_by = dict(tuple(official.groupby("code"))) if len(official) else {}
    caller = Caller("prod", 5)
    col = Collect(caller, today, yesterday, off_by, [])
    fails = {}
    for i, code in enumerate(codes):
        if stop_at is not None and stop_at():
            fails.update({c: "시간 상한 (받지 않음)" for c in codes[i:]})
            break
        try:
            why = col.one(code)
        except RuntimeError as e:  # 유량 초과 연속 (이 재수집만 멈춤)
            fails.update({c: f"{pd.Timestamp.now():%H:%M:%S} 유량 초과 반복 -> 이번 회차 멈춤: {e}"[:160] for c in codes[i:]})
            break
        if why:
            fails[code] = why
    done = [c for c in codes if c not in fails]
    got = sorted({c for p in col.adj_parts for c in p.code.unique()})
    out.update(ok=True, done=done, got=got, failed=fails, requests=caller.n, errors=caller.errors[-10:], seconds=round(time.time() - t0, 1),
               flags={k: v for k, v in col.flags.items() if v and k != "fetch_error"})
    if not done:
        return {**out, "reason": "받은 종목 없음"}
    if stop_at is not None and stop_at():
        return {**out, "reason": "공개 시각 지남 -> 공개 안 함 (다시 적재가 15:18 전에 끝나지 못함)"}
    with PUBLISH_LOCK:
        dd = _cur_dir()
        u = _update_json(dd)
        if u.get("official_missing_dates"):
            return {**out, "reason": "그사이 공식 시세 기다리는 버전이 됨 -> 공개 안 함"}
        ds = set(done)
        adj, raw = pd.read_parquet(os.path.join(dd, "adj.parquet")), pd.read_parquet(os.path.join(dd, "raw.parquet"))
        adj = pd.concat([adj[~adj.code.isin(ds)]] + col.adj_parts, ignore_index=True)
        raw = pd.concat([raw[~raw.code.isin(ds)]] + col.raw_parts, ignore_index=True)
        adj["timestamp"], raw["timestamp"] = pd.to_datetime(adj.timestamp), pd.to_datetime(raw.timestamp)
        for df_ in (adj, raw):
            assert not df_.duplicated(["code", "timestamp"]).any(), "중복 행"
        u["fetch_error"] = [[c, fails.get(c, w)] for c, w in u.get("fetch_error", []) if c not in ds]
        u["missing_row"] = [list(x) for x in u.get("missing_row", []) if x[0] not in ds] + col.flags["missing_row"]
        for k in ("adj_changed", "no_official", "no_new_rows"):
            u[k] = sorted((set(u.get(k, [])) - ds) | set(col.flags[k]))
        for k in ("adj_changed_why", "rebased"):
            u[k] = {**{c: v for c, v in (u.get(k) or {}).items() if c not in ds}, **col.flags[k]}
        u["carry_rebased"] = {c: v for c, v in (u.get("carry_rebased") or {}).items() if c not in ds}
        cmp_ = os.path.join(APPEND_DIR, CARRY_META)  # 11R 보완 1: 받은 종목의 지난 carry 기록은 지움 (다음 채움 때 옛 계수를 승계하지 않게)
        if os.path.exists(cmp_):
            with open(cmp_, encoding="utf-8") as f:
                cm_ = {c: v for c, v in json.load(f).items() if c not in ds}
            with open(cmp_, "w", encoding="utf-8") as f:
                json.dump(cm_, f, ensure_ascii=False, indent=1, default=str)
        u["dates"] = sorted(adj.timestamp.dt.strftime("%Y-%m-%d").unique().tolist())
        u["adj_rows"] = len(adj)
        u["data_version"] = f"{pd.Timestamp.now():%Y%m%d%H%M%S}|{u['dates'][-1] if u['dates'] else '-'}|missing=-|refetch"
        u.setdefault("intraday_refetch", []).append({k: out[k] for k in ("started", "codes", "done", "got", "failed", "requests", "flags")})
        out["published"] = publish_data({"adj.parquet": adj, "raw.parquet": raw, "update.json": u}, APPEND_DIR)
        out["data_version"] = u["data_version"]
    return out


# ---------------------------------------------------------------- 11S D: 다음 날 대조

def next_day_targets(prev_u, check, day):
    """다음 날 대조 대상: 전날 버전의 공식 값 채움 {code: 날짜들}·다시 맞춤 {code: k}, 전날 15:18 그날 계수 {code: k} (check 의 day 가 맞을 때만)"""
    ev = ((check or {}).get("base_events") or {}) if str((check or {}).get("day")) == str(day) else {}
    return {"filled": {c: list(v.get("dates") or []) for c, v in (prev_u.get("official_filled") or {}).items()},
            "rebased": rebased_k(prev_u), "base_events": {c: float(k) for c, k in ev.items()}}


def _off_by(kv, ov):
    """키움 값과 우리 값의 날짜별 차이가 1원 또는 0.2% 넘는 위치"""
    return [int(i) for i in np.flatnonzero(np.abs(kv - ov) > np.maximum(1.0, REBASE_TOL * np.abs(kv)))]


def next_day_check(prev_dir, fetched, targets, day):
    """11S D: 우리가 만든 이력(공식 값 채움·다시 맞춤·그날 계수)을 오늘 07:00 받은 키움 수정 일봉과 대조. 기록만 (매매 영향 없음).
    - 공식 값 채움: 채운 날의 원주가 시가·고가·저가·종가·거래량·거래대금 (키움 행은 공식 정규장 값 덮어쓴 뒤) 같음, 수정주가 시가·고가·저가·종가는
      일정한 비율 K (시가 비율 중앙값, 오늘 새 이벤트면 K ≠ 1) 로 1원 또는 0.2% 안
    - 다시 맞춤: 기본 파일 마지막 5일 시가 x k (원 반올림) x K = 키움 수정주가 시가 (1원 또는 0.2% 안)
    - 그날 계수: 키움 factor(그 앞 거래일) / factor(그날) = k (0.2% 안). k = 15:18 기준가 / 전날 종가
    반환 {version, day, checked {종류: 수}, mismatch [{code, kind, ...}], not_fetched {종류: [code]}}"""
    out = {"version": os.path.basename(prev_dir), "day": str(day), "checked": {}, "mismatch": [], "not_fetched": {}}
    pa = pr = None
    if targets["filled"] and os.path.exists(os.path.join(prev_dir, "adj.parquet")):
        pa, pr = pd.read_parquet(os.path.join(prev_dir, "adj.parquet")), pd.read_parquet(os.path.join(prev_dir, "raw.parquet"))
        pa["timestamp"], pr["timestamp"] = pd.to_datetime(pa.timestamp), pd.to_datetime(pr.timestamp)

    def miss(kind, c):
        out["not_fetched"].setdefault(kind, []).append(c)

    def seen(kind):
        out["checked"][kind] = out["checked"].get(kind, 0) + 1

    for c, ds in targets["filled"].items():
        f = fetched.get(c) or {}
        if "na" not in f or pa is None:
            miss("filled", c)
            continue
        ds = [pd.Timestamp(x) for x in ds]
        oa = pa[(pa.code == c) & pa.timestamp.isin(ds)].sort_values("timestamp")
        orw = pr[(pr.code == c) & pr.timestamp.isin(ds)].sort_values("timestamp")
        ka, kr = (x[x.timestamp.isin(ds)].sort_values("timestamp") for x in (f["na"], f["nr"]))
        seen("filled")
        if list(ka.timestamp) != list(oa.timestamp) or list(kr.timestamp) != list(orw.timestamp):
            out["mismatch"].append({"code": c, "kind": "filled", "why": f"날짜 다름: 키움 {[str(x.date()) for x in ka.timestamp]}, "
                                                                     f"우리 {[str(x.date()) for x in oa.timestamp]}"})
            continue
        raw_bad = {k: [[float(x), float(y)] for x, y in zip(kr[k], orw[k])] for k in ("open", "high", "low", "close", "volume", "amount")
                   if not np.allclose(kr[k].to_numpy(float), orw[k].to_numpy(float), rtol=0, atol=0.5)}
        fo = oa.open.to_numpy(float)
        kk = float(np.median(ka.open.to_numpy(float)[fo > 0] / fo[fo > 0])) if (fo > 0).any() else 1.0
        adj_bad = [[k, str(ka.timestamp.iat[i].date()), float(ka[k].iat[i]), round(float(oa[k].iat[i]) * kk, 2)]
                   for k in ("open", "high", "low", "close") for i in _off_by(ka[k].to_numpy(float), oa[k].to_numpy(float) * kk)]
        if raw_bad or adj_bad:
            out["mismatch"].append({"code": c, "kind": "filled", "raw_kiwoom_ours": raw_bad, "adj_k": round(kk, 6), "adj_kiwoom_ours": adj_bad[:10]})
    for c, k in targets["rebased"].items():
        f = fetched.get(c)
        try:
            fa = file_last_dates(c) if f else None
        except Exception:  # noqa: BLE001
            fa = None
        chk = f["a"].merge(fa[["timestamp", "open"]], on="timestamp", suffixes=("", "_file")).tail(CHECK_DAYS) if fa is not None else []
        if not len(chk):
            miss("rebased", c)
            continue
        seen("rebased")
        ours = np.floor(chk.open_file.to_numpy(float) * k + 0.5)
        kk = float(np.median(chk.open.to_numpy(float)[ours > 0] / ours[ours > 0])) if (ours > 0).any() else 1.0
        bad = _off_by(chk.open.to_numpy(float), ours * kk)
        if bad:
            out["mismatch"].append({"code": c, "kind": "rebased", "k": k, "k_new": round(kk, 6),
                                    "open_kiwoom_ours": [[str(chk.timestamp.iat[i].date()), float(chk.open.iat[i]), float(ours[i] * kk)] for i in bad]})
    for c, k in targets["base_events"].items():
        f = fetched.get(c)
        m = f["a"][["timestamp", "close"]].merge(f["r"][["timestamp", "close"]], on="timestamp", suffixes=("_a", "_r")) if f else pd.DataFrame()
        m = m[(m.timestamp <= pd.Timestamp(day)) & (m.close_r > 0)].sort_values("timestamp") if len(m) else m
        if len(m) < 2 or m.timestamp.iat[-1] != pd.Timestamp(day):
            miss("base_events", c)
            continue
        seen("base_events")
        fac = (m.close_a / m.close_r).to_numpy(float)
        k_kw = float(fac[-2] / fac[-1])
        if abs(k_kw / k - 1) > REBASE_TOL:
            out["mismatch"].append({"code": c, "kind": "base_events", "k_ours": k, "k_kiwoom": round(k_kw, 6),
                                    "factor": [[str(m.timestamp.iat[i].date()), round(float(fac[i]), 6)] for i in (-2, -1)]})
    return out


def lacking_rows(off, have, after, before=None):
    """11K 5: 공식 시세(off: 그 종목, date·volume)에서 거래량 > 0 인 날 중 after 뒤(before 앞)인데 우리 일봉 날짜(have)에 없는 날. 반환 [날짜 문자열]
    (거래량 0 인 날 = 거래정지 등은 키움 일봉에 없을 수 있고 백테스트 일봉도 같은 출처라 세지 않음)"""
    if off is None or len(off) == 0:
        return []
    o = off[pd.to_numeric(off.volume, errors="coerce").fillna(0) > 0] if "volume" in off else off
    dates = pd.to_datetime(o.date)
    dates = dates[(dates > pd.Timestamp(after)) & ((dates < pd.Timestamp(before)) if before is not None else True)]
    h = set(pd.to_datetime(pd.Series(list(have))))
    return sorted(str(d.date()) for d in set(dates) - h)


def amt20_files(code, day):
    """파일(백테스트 수정주가 + 추가분)에서 day 앞 20거래일 평균 거래대금 (원). 일봉 대상에서 빠진 종목의 후보 판정용 (Codex 27)"""
    try:
        a = file_last_dates(code)
    except Exception:  # noqa: BLE001
        return float("nan")
    ap = data_file("adj.parquet", APPEND_DIR)
    if os.path.exists(ap):
        x = pd.read_parquet(ap)
        x = x[x.code == code]
        if len(x):
            a = pd.concat([a[a.timestamp < pd.to_datetime(x.timestamp).min()], x.drop(columns=["code"])], ignore_index=True)
    a["timestamp"] = pd.to_datetime(a.timestamp)
    a = a[a.timestamp < pd.Timestamp(day)].sort_values("timestamp")
    return float(a.amount.tail(20).mean()) if len(a) else float("nan")


def has_pending():
    return all(os.path.exists(os.path.join(APPEND_DIR, p)) for p in ("pending_adj.parquet", "pending_raw.parquet", "pending_index.parquet"))


def reapply_official():
    """공식 시세가 나온 뒤: 보관해 둔 그날 키움 행에 공식 정규장 고가·저가·종가를 덮어 추가분에 붙인다 (요청은 공식 시세 2회뿐, 몇 초).
    기다리던 날이 모두 나왔을 때만 붙인다. 반환: 결과 dict. 11S: 버전 공개는 장중 재수집과 한 번에 하나 (PUBLISH_LOCK)"""
    with PUBLISH_LOCK:
        return _reapply_official()


def _reapply_official():
    from kiwoom import krx_official
    with open(data_file("update.json", APPEND_DIR), encoding="utf-8") as f:
        u = json.load(f)
    miss = [pd.Timestamp(x) for x in u.get("official_missing_dates", [])]
    if not miss or not has_pending():
        return {"ok": False, "reason": "기다리는 날 없음 또는 보관 행 없음"}
    official = krx_official.load_range(min(miss), max(miss))
    got = set(pd.to_datetime(official.date).unique()) if len(official) else set()
    if not all(d in got for d in miss):
        return {"ok": False, "reason": f"아직 없음 {[str(d.date()) for d in miss if d not in got]}"}
    off_by = dict(tuple(official.groupby("code")))
    pa = pd.read_parquet(os.path.join(APPEND_DIR, "pending_adj.parquet"))
    pr = pd.read_parquet(os.path.join(APPEND_DIR, "pending_raw.parquet"))
    adj_parts, raw_parts, no_off = [], [], []
    # 11K 5: 기다리던 날 공식 시세에는 거래가 있는데 보관 행에 없는 종목 -> 붙이지 않고 그날 계산에서 제외 (missing_row)
    skip = set(u.get("adj_changed", [])) | {c for c, _ in u.get("fetch_error", [])}
    have_by = {c: set(pd.to_datetime(g.timestamp)) for c, g in pa.groupby("code")}
    lack = {}
    # 11K 보완 3 (3): 대상 = 공식 시세에 그날 거래가 있는 종목 중 우리 종목 (대상 종목 목록 + 추가분·보관 행 종목).
    # 추가분·보관 행에 처음부터 없던 종목도 공식 시세에 거래가 있으면 누락으로 센다 (첫 수집에 통째로 빠진 종목)
    try:
        uni = set(sd.universe_codes())
    except (FileNotFoundError, OSError):  # data 폴더가 없는 환경 (시험·clone 직후)
        uni = set()
    tracked = (uni | set(pd.read_parquet(data_file("adj.parquet", APPEND_DIR), columns=["code"]).code) | set(pa.code)) & set(off_by)
    for code in tracked - skip:
        x = lacking_rows(off_by.get(code), have_by.get(code, set()), min(miss) - pd.Timedelta(days=1), max(miss) + pd.Timedelta(days=1))
        if x:
            lack[code] = x
    pa, pr = pa[~pa.code.isin(list(lack))], pr[~pr.code.isin(list(lack))]
    for code, na in pa.groupby("code"):
        nr = pr[pr.code == code].drop(columns=["code"])
        na = na.drop(columns=["code"])
        off = off_by.get(code)
        if off is not None:
            na, _ = krx_official.apply_regular_session(na, off)
            nr, _ = krx_official.apply_regular_session(nr, off)
        else:
            no_off.append(code)
        nr = nr.merge(na[["timestamp", "close"]].rename(columns={"close": "adj_close"}), on="timestamp", how="left")
        nr["factor"] = nr.adj_close / nr.close
        adj_parts.append(na.assign(code=code))
        raw_parts.append(nr.drop(columns=["adj_close"]).assign(code=code))
    # Codex 15: 같은 날짜 구간은 교체 (기존 행 중 그 날짜를 빼고 붙임)
    md = set(miss)
    old_a = pd.read_parquet(data_file("adj.parquet", APPEND_DIR))
    old_r = pd.read_parquet(data_file("raw.parquet", APPEND_DIR))
    old_i = pd.read_parquet(data_file("index.parquet", APPEND_DIR))
    adj = pd.concat([old_a[~pd.to_datetime(old_a.timestamp).isin(md)]] + adj_parts, ignore_index=True)
    raw = pd.concat([old_r[~pd.to_datetime(old_r.timestamp).isin(md)]] + raw_parts, ignore_index=True)
    ix = pd.concat([old_i[~pd.to_datetime(old_i.timestamp).isin(md)], pd.read_parquet(os.path.join(APPEND_DIR, "pending_index.parquet"))],
                   ignore_index=True)
    # 11P: 07:00 수집에 끝내 실패한 종목은 이전 버전 행(carry) + 공식 시세로 채움 (확실할 때만, 못 채우면 fetch_error 그대로 = 차단 유지)
    fe = {c: w for c, w in u.get("fetch_error", [])}
    filled, unfilled = {}, {}
    if fe:
        ca, cr = (os.path.join(APPEND_DIR, n) for n in CARRY)
        has = os.path.exists(ca) and os.path.exists(cr)
        carry_a = pd.read_parquet(ca) if has else None
        carry_r = pd.read_parquet(cr) if has else None
        if has:
            carry_a["timestamp"], carry_r["timestamp"] = pd.to_datetime(carry_a.timestamp), pd.to_datetime(carry_r.timestamp)
        cm = carry_meta(u)  # 11R 보완 1: carry 행 종목의 이전 rebase 기록 (승계 확인)
        fa_, fr_, filled, unfilled = fill_official(sorted(fe), carry_a, carry_r, max(miss), pd.to_datetime(ix.timestamp).unique(), meta=cm)
        if len(fa_):
            adj = pd.concat([adj, fa_], ignore_index=True)
            raw = pd.concat([raw, fr_], ignore_index=True)
        u["fetch_error"] = [[c, w] for c, w in u.get("fetch_error", []) if c not in filled]
        u["official_filled"] = {**(u.get("official_filled") or {}), **filled}
        u["fill_unfilled"] = unfilled
        u["rebased"] = {**(u.get("rebased") or {}), **{c: f["rebased"] for c, f in filled.items() if "rebased" in f}}
        u["carry_rebased"] = {c: v for c, v in (u.get("carry_rebased") or {}).items() if c not in filled}
    # 11R B: 공식 시세를 기다리던 계수 바뀜 종목 -> 이제 KRX 기준가비로 확인 (맞으면 rebased, 아니면 adj_changed)
    rw = u.get("rebase_wait") or {}
    rb_ok, rb_no = {}, {}
    if rw:
        start = min(pd.Timestamp(v["file_last"]) for v in rw.values()) + pd.Timedelta(days=1)
        offc = official_with_chg(start, max(miss))
        by = dict(tuple(offc.groupby("code"))) if len(offc) else {}
        for c, info in rw.items():
            k, ev, why_k = krx_base_ratio(by.get(c), info["file_last"], info["file_close"], max(miss))
            k2, why = rebase_decide(info["k_kiwoom"], k, ev, why_k)
            if why is None:
                rb_ok[c] = {"k": k2, "events": ev, **info}
            else:
                rb_no[c] = why
        u["rebased"] = {**(u.get("rebased") or {}), **rb_ok}
        u["adj_changed"] = sorted(set(u.get("adj_changed", [])) | set(rb_no))
        u["adj_changed_why"] = {**(u.get("adj_changed_why") or {}), **rb_no}
        u["rebase_wait"] = {}
    for df_, keys in ((adj, ["code", "timestamp"]), (raw, ["code", "timestamp"]), (ix, ["name", "timestamp"])):
        assert not df_.duplicated(keys).any(), f"중복 행 {keys}"
    for d in miss:
        cp = os.path.join(krx_official.CACHE_DIR, f"{d:%Y%m%d}.parquet")
        u["official_received"][str(d.date())] = pd.Timestamp(os.path.getmtime(cp), unit="s", tz="UTC").tz_convert("Asia/Seoul").strftime("%Y-%m-%d %H:%M:%S")
    u["dates"] = sorted(pd.to_datetime(adj.timestamp).dt.strftime("%Y-%m-%d").unique().tolist())
    u["official_missing_dates"] = []
    u["no_official"] = sorted(set(u.get("no_official", [])) | set(no_off))
    u["missing_row"] = sorted([list(x) for x in u.get("missing_row", [])] + [[c, d_] for c, d_ in lack.items()])
    u["adj_rows"] = len(adj)
    u["data_version"] = f"{pd.Timestamp.now():%Y%m%d%H%M%S}|{u['dates'][-1] if u['dates'] else '-'}|missing=-|reapplied"
    u.setdefault("reapplied", []).append({"time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), "dates": [str(d.date()) for d in miss],
                                          "stocks": len(adj_parts)})
    # 확인 리뷰 15: adj·raw·지수·update.json 을 새 버전 폴더에 모두 쓴 뒤 포인터 하나만 원자적으로 교체 (중간 실패면 이전 버전 그대로)
    v = publish_data({"adj.parquet": adj, "raw.parquet": raw, "index.parquet": ix, "update.json": u}, APPEND_DIR)
    for p_ in ("pending_adj.parquet", "pending_raw.parquet", "pending_index.parquet"):
        os.remove(os.path.join(APPEND_DIR, p_))
    return {"ok": True, "dates": [str(d.date()) for d in miss], "stocks": len(adj_parts), "no_official": len(no_off), "missing_row": len(lack),
            "data_dir_version": v, "filled": sorted(filled), "unfilled": unfilled, "rebased": sorted(rb_ok), "rebase_failed": rb_no}


def official_filled():
    """11P: 지금 데이터 버전에서 공식 값으로 채운 종목 {code: {carry_last, dates, last_close}}"""
    p = data_file("update.json", APPEND_DIR)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f).get("official_filled") or {}


# ---------------------------------------------------------------- 메모리 적재

def excluded_codes():
    """그날 대상에서 빼는 종목 (2026-10-22 결정): 수정주가 계수가 바뀐 종목 중 다시 맞추지 못한 것(11R B: 비율이 일정하지 않음·KRX 와 다름),
    공식 시세를 기다려 아직 확인 못 한 계수 바뀜 종목(rebase_wait), 추가분에 공식 시세가 없는 종목, 일봉 응답이 비어 추가분이 없는 종목"""
    p = data_file("update.json", APPEND_DIR)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        u = json.load(f)
    return {"adj_changed": sorted(u.get("adj_changed", [])), "no_official": sorted(u.get("no_official", [])),
            "fetch_error": sorted(c for c, _ in u.get("fetch_error", [])),
            "missing_row": sorted(c for c, _ in u.get("missing_row", [])),  # 11K 5: 공식 시세에 있는 날의 일봉 행 없음
            "rebase_wait": sorted(u.get("rebase_wait") or {})}


class Store:
    """종목별 수정주가·원주가 원본 행 (백테스트 파일 + 추가분). load_stock 이 파일에서 읽는 것과 같은 열.
    11R B: 추가분을 쓸 때 계수가 바뀐 종목(update.json rebased)은 기본 파일 행을 메모리에서 K 로 다시 맞춘다 (rebase_rows, 파일은 그대로)"""

    def __init__(self, codes, use_append=True):
        self.adj, self.raw = {}, {}
        add_a = add_r = None
        bad, rb = set(), {}
        dd = os.path.dirname(data_file("adj.parquet", APPEND_DIR))  # 한 버전 폴더에서 adj·raw 를 함께 읽는다
        if use_append and os.path.exists(os.path.join(dd, "adj.parquet")):
            add_a = dict(tuple(pd.read_parquet(os.path.join(dd, "adj.parquet")).groupby("code")))
            add_r = dict(tuple(pd.read_parquet(os.path.join(dd, "raw.parquet")).groupby("code")))
            bad = set().union(*excluded_codes().values())
            rb = rebased_k(_update_json(dd))
        self.excluded = sorted(bad & set(codes))
        self.rebased = {c: k for c, k in rb.items() if c in set(codes) and c not in bad}
        for code in codes:
            if code in bad:
                continue
            adj = file_last_dates(code)
            raw = pd.read_parquet(os.path.join(sd.RAW_DIR, f"{code}.parquet"))
            raw["timestamp"] = pd.to_datetime(raw.timestamp)
            if code in self.rebased:
                adj, raw = rebase_rows(adj, raw, self.rebased[code])
            if add_a is not None and code in add_a:
                last = adj.timestamp.max()
                na = add_a[code].drop(columns=["code"])
                adj = pd.concat([adj, na[na.timestamp > last]], ignore_index=True)
                nr = add_r[code].drop(columns=["code"])
                raw = pd.concat([raw[raw.timestamp <= last], nr[nr.timestamp > last]], ignore_index=True)
            self.adj[code], self.raw[code] = adj, raw


def index_series():
    """코스피·코스닥 종가 (지수 파일 + 추가분)"""
    out = {}
    p = data_file("index.parquet", APPEND_DIR)
    add = pd.read_parquet(p) if os.path.exists(p) else pd.DataFrame(columns=["name", "timestamp", "close"])
    for name, path in (("kospi", sd.KOSPI), ("kosdaq", s4.KOSDAQ)):
        f = pd.read_parquet(path)
        f["timestamp"] = pd.to_datetime(f.timestamp)
        a = add[add.name == name]
        a = a[pd.to_datetime(a.timestamp) > f.timestamp.max()]
        s = pd.concat([f[["timestamp", "close"]], a[["timestamp", "close"]]], ignore_index=True)
        s["timestamp"] = pd.to_datetime(s.timestamp)
        out[name] = s.sort_values("timestamp").set_index("timestamp").close
    return out


if __name__ == "__main__":
    {"update": update}[sys.argv[1]](*([sys.argv[2:]] if len(sys.argv) > 2 else []))
