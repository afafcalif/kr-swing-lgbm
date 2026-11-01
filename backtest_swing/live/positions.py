"""
보유 장부 positions.json (EXEC_SPEC 2장, 11E Codex 5·6·7·8·12·20·25 반영, 2026-10-24).

형식:
  held: {종목: {qty, avg_price, trade_id, entry_date, entry_price, stop_price, stop_temp, stop_basis, kind,
                trend_sell_next_open, judged_on, close_1530, base_seen, base_gap?, base_review?}}
         (11U: base_seen = 마지막으로 그날 기준가를 확인한 거래일. 새 거래는 진입일, 없으면(예전 장부) 공백 없음으로 보고 첫 확인 때 기록)
  sell_duty: {종목: {kind(stop 손절 진행 중 / trend 추세 매도), since(발생일), at(발생 시각), reason, price?, stop?}}
             (11J 보완 3: 매도 의무는 종목 코드 단위. 끝 = 성공한 계좌 조회에서 그 종목 보유 0, orders.end_duties)
  next_open_sells: {종목: {reason, since, attempts, last_status, last_try, last_intent}}   (다음 날 시가 매도 목록 = 15:35·15:40 에 계좌 보유가 남은 의무 종목)
  closed_today: {종목: {date, at, kind}}   (11N: 오늘 의무가 계좌 보유 0 두 번으로 끝난 판 종목. 15:40 보유 갱신 전까지 새 손절 감지·매수 계획의
                의무 판단·추세 매도에서 뺌. 날짜가 다르면 무효, 계좌에 다시 보유가 보이면 지움, 15:40 보유 갱신 뒤 지움. orders.closed_codes)
  trade_seq, duty_format, _version, _saved_at

- 저장할 때 직전 파일을 positions.json.bak 으로 남긴다 (Codex 8). 파일이 없으면 빈 보유. 읽기 실패(손상)면 손상 파일을 보관하고 백업으로 복구,
  백업도 못 읽으면 빈 보유 + 복구 실패. 복구했으면 그날 신규 매수 중단 (손절은 계좌 + 백업 손절가로 계속).
- 같은 종목을 청산한 뒤 다시 사면 새 거래 (trade_id 새로, 진입일·손절가·분류 새로, Codex 7).
- 손절가 = 진입일 종가 - 1.7 x ATR20 (원주가). 일봉 대상에서 빠진 종목(수정주가 계수 변경 등)은 원주가 일봉으로 ATR20 (Codex 6).
  진입가 정보가 없으면 계좌 평균단가 - 1.7 x ATR20 을 임시 손절가로 쓰고 stop_temp = True (Codex 5).
"""
import json
import math
import os
import shutil

import numpy as np
import pandas as pd

from common import atomic_json, data_file, now_kst, s4, sb, sd

ATR_MULT = s4.STOP_ATR  # 1.7


DUTY_FORMAT = 2  # 11J 보완 3: 매도 의무 종목 코드 단위 (sell_duty). 없으면 예전 형식 -> normalize 가 바꾼다


def empty():
    return {"held": {}, "sell_duty": {}, "next_open_sells": {}, "trade_seq": 0, "duty_format": DUTY_FORMAT}


def normalize(pos):
    pos = dict(pos or {})
    pos.setdefault("held", {})
    nos = pos.get("next_open_sells") or {}
    if isinstance(nos, list):  # 예전 형식 (종목 목록)
        nos = {c: {"reason": "예전 형식에서 옮김", "since": None, "attempts": 0, "last_status": None} for c in nos}
    pos["next_open_sells"] = nos
    pos.setdefault("sell_duty", {})
    pos.setdefault("trade_seq", 0)
    if pos.get("duty_format") != DUTY_FORMAT:
        convert_duties(pos)
    return pos


def convert_duties(pos):
    """11J 보완 3: 예전 형식(거래 단위 <커밋>·trade_id 없음)의 매도 의무를 종목 코드 단위(sell_duty)로 바꾼다.
    held[c].stop_triggered·추세 판정(trend_sell_next_open) = 지금 보유 거래에 붙은 표시 -> 손절·추세 의무.
    다음 날 시가 목록 항목 (R2): trade_id 가 지금 보유 거래와 같으면 의무, 다르면(이전 거래 것) 버림. trade_id 가 없으면(<커밋> 형식)
    항목 날짜(since)가 있고 지금 보유 거래의 진입일보다 뒤일 때만 의무 (같은 날·앞이면 이전 거래 것일 수 있음). 판단할 수 없으면
    의무로 만들지 않고 목록에서 빼서 pos["duty_converted"]["_review"] 에 둔다 (상주 프로그램이 시작 때 알림으로 사람에게 확인 요청).
    바꾼 내용은 pos["duty_converted"] (시작 때 계좌와 맞춰 보고 알림)"""
    duty, nos, conv, review = pos["sell_duty"], pos["next_open_sells"], {}, {}
    for c, p in pos["held"].items():
        st = p.pop("stop_triggered", None)
        if st and c not in duty:
            duty[c] = {"kind": "stop", "since": str(st.get("since", ""))[:10] or None, "at": str(st.get("since", ""))[11:19] or None,
                       "reason": f"현재가 {float(st.get('price', 0)):.0f} < 손절가 {float(st.get('stop', 0)):.0f} (예전 형식)",
                       "price": st.get("price"), "stop": st.get("stop")}
            conv[c] = "손절 진행 중 -> 손절 의무"
    for c, e in list(nos.items()):
        e = dict(e) if isinstance(e, dict) else {}
        p = pos["held"].get(c) or {}
        tid, cur, ed, since = e.pop("trade_id", None), p.get("trade_id"), p.get("entry_date"), str(e.get("since") or "")[:10]
        if tid and cur and tid != cur:
            nos.pop(c)
            conv[c] = f"다음 날 시가 목록 항목이 이전 거래 {tid} 것 -> 버림"
            continue
        if not ((tid and tid == cur) or (not tid and since and ed and since > str(ed))):
            nos.pop(c)
            review[c] = {"held": {k: p.get(k) for k in ("qty", "trade_id", "entry_date")} if p else None, "entry": {**e, **({"trade_id": tid} if tid else {})}}
            conv[c] = "; ".join(x for x in (conv.get(c), "옛 목록 항목이 지금 보유 거래 것인지 판단 못 함 -> 목록에서 뺌 (사람 확인)") if x)
            continue
        nos[c] = e
        if c not in duty:
            duty[c] = {"kind": "trend", "since": e.get("since"), "at": None, "reason": e.get("reason") or "다음 날 시가 매도 목록 (예전 형식)"}
            conv.setdefault(c, "다음 날 시가 목록 -> 추세 의무")
    for c, p in pos["held"].items():
        if p.get("trend_sell_next_open") and c not in duty:
            duty[c] = {"kind": "trend", "since": p.get("judged_on"), "at": None, "reason": f"추세 매도 ({p.get('judged_on')} 종가 < 20일선, 예전 형식)"}
            nos.setdefault(c, {"reason": duty[c]["reason"], "since": p.get("judged_on"), "attempts": 0, "last_status": None})
            conv.setdefault(c, "추세 판정 -> 추세 의무")
    pos["duty_format"] = DUTY_FORMAT
    if conv:
        pos["duty_converted"] = {**pos.get("duty_converted", {}), **conv, **({"_review": review} if review else {})}
    return conv


def load(path):
    """반환: (보유 장부, 정보 dict(status: ok / missing / recovered_backup / corrupt_unrecovered, ...))"""
    if not os.path.exists(path):
        return empty(), {"status": "missing"}
    try:
        with open(path, encoding="utf-8") as f:
            return normalize(json.load(f)), {"status": "ok"}
    except Exception as e:  # noqa: BLE001
        kept = f"{path}.corrupt-{now_kst():%Y%m%d%H%M%S}"
        os.replace(path, kept)
        bak = path + ".bak"
        try:
            with open(bak, encoding="utf-8") as f:
                pos = normalize(json.load(f))
            atomic_json(path, pos)
            return pos, {"status": "recovered_backup", "error": repr(e)[:200], "kept": kept}
        except Exception as e2:  # noqa: BLE001
            return empty(), {"status": "corrupt_unrecovered", "error": repr(e)[:200], "backup_error": repr(e2)[:200], "kept": kept}


def save(path, pos):
    """직전 파일을 .bak 으로 두고 저장 (저장 시각·버전 기록)"""
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak")
    pos["_version"] = int(pos.get("_version", 0)) + 1
    pos["_saved_at"] = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    atomic_json(path, pos)


def new_trade(pos, code, entry_date, **kw):
    """새 거래 (Codex 7): trade_id 새로"""
    pos["trade_seq"] = int(pos.get("trade_seq", 0)) + 1
    t = {"trade_id": f"{code}-{entry_date}-{pos['trade_seq']}", "entry_date": str(entry_date), "trend_sell_next_open": False,
         "base_seen": str(pd.Timestamp(entry_date).date()), **kw}  # 11U: 진입일 손절가(원주가) 뒤 이벤트를 확인할 기준
    pos["held"][code] = t
    return t


# ---------------------------------------------------------------- 원주가 일봉 (손절가 계산용)

def raw_daily(code):
    """원주가 일봉 = data/daily_raw + 추가분(raw.parquet). 날짜 중복은 추가분 우선. 11U: factor(키움 수정주가 종가 / 원주가 종가) 열이 있으면 함께"""
    parts = []
    cols = ["timestamp", "open", "high", "low", "close", "factor"]
    p = os.path.join(sd.RAW_DIR, f"{code}.parquet")
    if os.path.exists(p):
        f = pd.read_parquet(p)
        parts.append(f[[k for k in cols if k in f.columns]])
    ap = data_file("raw.parquet")
    if os.path.exists(ap):
        a = pd.read_parquet(ap)
        parts.append(a[a.code == code][[k for k in cols if k in a.columns]])
    if not parts:
        return pd.DataFrame(columns=cols[:-1])
    df = pd.concat(parts, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df.timestamp)
    return df.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)


def raw_atr20(code, until, today_row=None):
    """until 까지 원주가 일봉 (+ 오늘 행) 의 마지막 종가와 ATR20 (load_stock 과 같은 TR 정의). 반환 (종가, ATR20) 또는 (None, None)"""
    df = raw_daily(code)
    df = df[df.timestamp <= pd.Timestamp(until)]
    if today_row is not None:
        df = pd.concat([df[df.timestamp < pd.Timestamp(until)], pd.DataFrame([{"timestamp": pd.Timestamp(until), **today_row}])], ignore_index=True)
    if len(df) < 21:
        return None, None
    pc = df.close.shift(1)
    tr = np.maximum(df.high - df.low, np.maximum((df.high - pc).abs(), (df.low - pc).abs()))
    atr = tr.rolling(20).mean().iat[-1]
    return float(df.close.iat[-1]), float(atr) if np.isfinite(atr) else None


def stop_from_raw(code, day, today_row=None):
    """원주가 기준 손절가 = 종가 - 1.7 x ATR20 (일봉 대상에서 빠진 종목용, Codex 6)"""
    close, atr = raw_atr20(code, day, today_row)
    if close is None or atr is None:
        return None, "원주가 일봉 부족"
    return close - ATR_MULT * atr, f"원주가 일봉 종가 {close:,.0f} - 1.7 x ATR20 {atr:,.1f}"


def factor_events(df, days=None):
    """11U: 원주가 일봉 df(timestamp·factor) 의 이웃한 행 사이 이벤트 계수 k = 앞 행 factor / 뒤 행 factor (키움 수정주가 계수 변화,
    daily 11S D 와 같은 정의) 가 BASE_EVENT(0.5%) 넘게 1 과 다른 날 {날짜: k}. factor 가 빈 행은 건너뛰고 그 앞뒤 값으로 비교 (계수는 누적이라
    변화가 남음). days(거래일 목록)를 주면 그 날들만, 행·factor 가 모두 있어야 함 -> 하나라도 없으면 (None, 이유)"""
    from engine import BASE_EVENT
    d = df[["timestamp", "factor"]].copy() if "factor" in df.columns else pd.DataFrame({"timestamp": [], "factor": []})
    d["timestamp"] = pd.to_datetime(d.timestamp)
    d["factor"] = pd.to_numeric(d.factor, errors="coerce")
    if days is not None:
        days = [pd.Timestamp(x).normalize() for x in days]
        d = d[d.timestamp.isin(days)]
        have = set(d.timestamp[d.factor > 0])
        miss = [str(x.date()) for x in days if x not in have]
        if miss:
            return None, f"키움 수정 일봉 계수(factor) 없는 날 {miss}"
    d = d[d.factor > 0].sort_values("timestamp")
    out = {}
    f, t = d.factor.to_numpy(dtype=float), list(d.timestamp)
    for i in range(1, len(f)):
        k = f[i - 1] / f[i]
        if abs(k - 1) >= BASE_EVENT:
            out[str(t[i].date())] = float(k)
    return out, None


def window_events(w):
    """11U 보완 4: 판정 창 w(timestamp·factor) 의 키움 계수 변화 {날짜: k} (이웃한 계수 행 사이 k = 앞 행 factor / 뒤 행 factor 가
    0.5% 넘게 1 과 다름, factor_events 와 같은 정의)와 계수 없는 행을 사이에 둔 구간 [(앞 날짜, 뒤 날짜, 구간 전체 변화율 = 양끝 factor 비)]
    (어느 날 이벤트인지 모름, 11V 보완: 변화가 없어도 구간으로 남김 -> 그 구간에 쓰는 계수 곱이 1 이어야 함)"""
    from engine import BASE_EVENT
    f = pd.to_numeric(w.factor, errors="coerce").to_numpy(dtype=float) if "factor" in w.columns else np.full(len(w), np.nan)
    ts = list(pd.to_datetime(w.timestamp))
    idx = [i for i in range(len(f)) if f[i] > 0]
    ev, gaps = {}, []
    for a, b in zip(idx, idx[1:]):
        k = float(f[a] / f[b])
        if b - a > 1:
            gaps.append((str(ts[a].date()), str(ts[b].date()), k))
        elif abs(k - 1) >= BASE_EVENT:
            ev[str(ts[b].date())] = k
    return ev, gaps


def raw_judge(code, day, today_row=None, base=None, p=None):
    """원주가 일봉으로 day 기준 판단 (일봉 대상에서 빠진 종목, Codex 6): (종가, 손절가, 종가 < 20일선) 또는 None (day 행 없음·일봉 부족).
    11U 보완 4: 판정 창(ATR20·20일선 계산 21행, 오늘 포함) 안 이벤트는 계수로 이벤트 전 원주가 행(시가·고가·저가·종가)을 x k 해서 맞춘 뒤 판단.
    계수 = 보유 장부 p 의 stop_adjust (손절가를 조정한 그날 이벤트·공백 보정, stop_ks) -> 없으면 키움 수정 일봉 계수 변화 (window_events)
    -> 오늘 행을 붙인 판단(today_row)은 그날 기준가(base, 없으면 p 의 오늘 base_check) / 전날 원주가 종가 가 0.5% 넘게 다르면 그 비율.
    맞출 계수가 없으면 판단 보류 -> ("hold", 이유): 오늘 기준가 모름(그날 이벤트 확인 못 함), 계수 없는 행 구간(window_events)의 전체 변화율을
    그 구간에 쓰는 계수 곱으로 설명 못 함 (11V 보완: 0.5% 기준, 기록 한 건만 있어도 해결로 보던 것), 0.5% 안의 남은 비율을 구간 맨 앞·맨 뒤
    가능한 날에 넣은 판단과 추세 판단이 다름 (11V 보완 2·3: 모든 구간을 함께 넣은 MA20 최소·최대 두 경우, gap_scenarios). 이벤트가 없으면 예전과 같음"""
    from engine import BASE_EVENT
    df = raw_daily(code)
    day = pd.Timestamp(day)
    df = df[df.timestamp <= day]
    if today_row is not None:
        df = pd.concat([df[df.timestamp < day], pd.DataFrame([{"timestamp": day, **today_row}])], ignore_index=True)
    if len(df) < 21 or df.timestamp.iat[-1] != day:
        return None
    w = df.iloc[-21:].reset_index(drop=True)
    first, today = str(pd.Timestamp(w.timestamp.iat[0]).date()), str(day.date())
    ks = {d: k for d, k in stop_ks(p).items() if first < d <= today}
    ev, gaps = window_events(w)
    for d, k in ev.items():
        ks.setdefault(d, k)
    if today_row is not None and today not in ks:
        pc = float(w.close.iat[-2])
        if base is None:
            base = (base_checked(p, day) or {}).get("base")
        if base is None or not (float(base) > 0):
            return "hold", "오늘 기준가 모름 (그날 이벤트 확인 못 함)"
        if pc > 0 and abs(float(base) / pc - 1) >= BASE_EVENT:
            ks[today] = float(base) / pc
    tries = []
    for a, b, k in gaps:  # 11V 보완: 계수 없는 행 구간은 쓰려는 계수 곱이 양끝 factor 비와 맞아야 (0.5% 기준)
        got = float(np.prod([x for d, x in ks.items() if a < d <= b]))
        r = k / got  # 남은 비율 (설명 안 된 변화)
        if abs(r - 1) >= BASE_EVENT:
            return "hold", f"판정 창 안 계수 없는 행 구간 ({a} ~ {b}) 변화 {k:.4f} 를 계수 곱 {got:.4f} 로 설명 못 함"
        if abs(r - 1) > 1e-9:  # 11V 보완 2: 0.5% 안이어도 남은 비율이 있으면 그 구간 맨 앞·맨 뒤 가능한 날 (어디든 넣을 수 있음)
            first_d = str(pd.Timestamp(w.timestamp[w.timestamp > pd.Timestamp(a)].iat[0]).date())
            tries.append((r, first_d, b))
    out = raw_calc(w, ks)
    if tries:  # 11V 보완 3: 모든 공백의 r 을 함께 넣은 MA20 최소·최대 두 경우에도 추세 판단이 같아야
        for alt_ks in gap_scenarios(w, ks, tries):
            alt = raw_calc(w, alt_ks)
            if out is None or alt is None or alt[2] != out[2]:
                return "hold", (f"판정 창 안 계수 없는 행 구간의 설명 안 된 변화 {[(d0, d1, round(r, 4)) for r, d0, d1 in tries]} 를 "
                                f"함께 넣으면 추세 판단이 바뀜")
    return out


def gap_scenarios(w, ks, tries):
    """11V 보완 3: 계수 없는 행 구간마다 남은 비율 r 을 맨 앞(d0)·맨 뒤(d1) 가능한 날 중 어디에 넣을지, 모든 구간에 함께 넣은 MA20 최소·최대 경우.
    한 구간의 앞·뒤 선택은 이벤트 전 행 몇 개에 r 을 더 곱하느냐뿐이라 다른 구간 선택과 상관없이 같은 방향 -> 구간마다 따로 실제 MA20 을
    비교해 정함 (조합 2^n 안 셈). tries = [(r, d0, d1)]. 반환 (최소 ks, 최대 ks)"""
    lo, hi = dict(ks), dict(ks)
    for r, d0, d1 in tries:
        m0, m1 = raw_ma20(w, {**ks, d0: ks.get(d0, 1.0) * r}), raw_ma20(w, {**ks, d1: ks.get(d1, 1.0) * r})
        dl, dh = (d0, d1) if m0 <= m1 else (d1, d0)
        lo[dl], hi[dh] = lo.get(dl, 1.0) * r, hi.get(dh, 1.0) * r
    return lo, hi


def raw_adjust(w, ks):
    """원주가 판정 창 w 에 이벤트 계수 ks {날짜: k} (그날 전 행 x k, 곱이라 순서 무관) 를 넣은 사본"""
    w = w.copy()
    for d, k in ks.items():
        m = w.timestamp < pd.Timestamp(d)
        w.loc[m, ["open", "high", "low", "close"]] = w.loc[m, ["open", "high", "low", "close"]].astype(float) * k
    return w


def raw_ma20(w, ks):
    return float(raw_adjust(w, ks).close.rolling(20).mean().iat[-1])


def raw_calc(w, ks):
    """raw_adjust 뒤 (종가, 손절가, 종가 < 20일선) 또는 None"""
    w = raw_adjust(w, ks)
    pc = w.close.shift(1)
    tr = np.maximum(w.high - w.low, np.maximum((w.high - pc).abs(), (w.low - pc).abs()))
    atr, ma20, close = tr.rolling(20).mean().iat[-1], w.close.rolling(20).mean().iat[-1], float(w.close.iat[-1])
    if not (np.isfinite(atr) and np.isfinite(ma20)):
        return None
    return close, close - ATR_MULT * float(atr), bool(close < ma20)


def temp_stop(code, avg_price, day):
    """임시 손절가 (Codex 5): 진입가 정보가 없으면 계좌 평균단가 - 1.7 x ATR20 (원주가). ATR 이 없으면 평균단가 x 0.9"""
    _, atr = raw_atr20(code, day)
    if atr is not None and avg_price:
        return float(avg_price) - ATR_MULT * atr, f"임시: 계좌 평균단가 {avg_price:,.0f} - 1.7 x ATR20 {atr:,.1f}"
    return float(avg_price or 0) * 0.9, "임시: 원주가 일봉 부족 -> 계좌 평균단가 x 0.9"


# ---------------------------------------------------------------- 체결로 설명 안 되는 보유 수량 변화 (11K 보완 3)

def mark_qty_review(pos, code, acct_qty, day, at, where, price=None):
    """체결로 설명되지 않는 보유 수량 변화 (액면분할·병합·무상증자·사람 거래 등, 자동 조정 안 함) -> 수동 확인 필요 (qty_review).
    손절가·진입가는 그대로. 수량만 계좌 값으로 (같은 변화를 다시 감지하지 않게, 옛 수량은 표시에). 반환 표시"""
    p = pos["held"][code]
    old = int(p.get("qty", 0))
    rec = {"date": str(day), "at": at, "where": where, "file_qty": old, "account_qty": int(acct_qty),
           "ratio": f"{int(acct_qty)}/{old} = {int(acct_qty) / old:.3f}" if old else "-", "stop": p.get("stop_price"), "price": price,
           "clear": "사람이 공시·체결 내역으로 확인한 뒤 이 표시(qty_review)를 지우거나 손절가를 고친다 (손절가가 바뀌면 끝난 것으로 봄)"}
    p["qty_review"] = rec
    p["qty"] = int(acct_qty)
    return rec


REVIEW_KEYS = ("qty_review", "base_review")  # 11U: base_review = 기준가 확인 공백 기간의 이벤트를 확인 못 함 (같은 사람 해제 방식)


def in_review(p):
    """수동 확인 중인가: 표시(qty_review 또는 11U base_review)가 있고 손절가가 표시 때 그대로 (사람이 손절가를 고치거나 표시를 지우면 끝).
    이 동안 그 종목 자동 손절·추세 매도·신규 매수 안 함"""
    for key in REVIEW_KEYS:
        r = (p or {}).get(key)
        if not r:
            continue
        s0, s1 = r.get("stop"), (p or {}).get("stop_price")
        if s0 is None or s1 is None or abs(float(s1) - float(s0)) < 1e-9:
            return True
    return False


def review_info(p):
    """알림용: 걸린 수동 확인 표시 {key: 표시}"""
    return {k: (p or {}).get(k) for k in REVIEW_KEYS if (p or {}).get(k)}


def review_codes(pos):
    return {c for c, p in (pos or {}).get("held", {}).items() if in_review(p)}


# ---------------------------------------------------------------- 보유 종목 그날 이벤트 (11R C)

def prev_raw_close(code, prev_day, p=None):
    """그날 기준가와 비교할 전날 원주가 종가: 공식 일봉(원주가 파일 + 추가분)의 전날 행, 없으면(공식 시세 재반영 전 등) 전날 15:30 1분봉 종가
    (judged_on = 전날). 키움 보관 행(pending)은 장 마감 후 가격이 섞일 수 있어 쓰지 않음. 반환 (종가, 출처) 또는 (None, 이유)"""
    df = raw_daily(code)
    x = df[df.timestamp == pd.Timestamp(prev_day)]
    if len(x) and float(x.close.iat[-1]) > 0:
        return float(x.close.iat[-1]), "공식 일봉"
    if p and p.get("judged_on") == str(pd.Timestamp(prev_day).date()) and p.get("close_1530"):
        return float(p["close_1530"]), "전날 15:30 1분봉 종가"
    return None, f"전날({pd.Timestamp(prev_day).date()}) 원주가 종가 모름"


def base_checked(p, day):
    """오늘 기준가 확인을 했는가 (했으면 그 기록)"""
    r = (p or {}).get("base_check")
    return r if r and r.get("date") == str(pd.Timestamp(day).date()) else None


def base_final(p, day):
    """11R 보완 3: 오늘 기준가 확인이 끝났는가. 08:30 장 전 확인에서 이벤트가 없었으면(기준가가 아직 옛 값일 수 있음) 잠정 (final False) ->
    09:00 이후 첫 손절 감시 시세로 한 번 더. final 이 없는 예전 기록은 끝난 것으로"""
    r = base_checked(p, day)
    return r if r and (r.get("final", True) or r.get("adjusted")) else None


def today_k(p, day):
    """오늘 이벤트로 손절가를 조정했으면 그 k, 아니면 1"""
    return events_k(p, pd.Timestamp(day) - pd.Timedelta(days=1), day)


def stop_ks(p):
    """보유 장부에 기록된 이벤트 손절가 조정 비율 {날짜: k} (stop_adjust, 날마다 하나: 기준가 / 전날 종가, 없으면 k = 11U 공백 보정)"""
    ks = {}
    for x in (p or {}).get("stop_adjust") or []:
        ks.setdefault(str(x.get("date")), float(x["base"]) / float(x["prev_close"]) if x.get("base") and x.get("prev_close") else float(x["k"]))
    return ks


def events_k(p, since, until):
    """11R 보완 2: since(그 날 제외) 뒤 ~ until(그 날 포함)에 보유 장부에 기록된 이벤트 손절가 조정 비율(stop_adjust k)의 곱 (날마다 하나).
    since 날 원주가 기준으로 다시 계산한 손절가(진입일 공식 일봉 손절가·전날 공식 손절가)를 오늘 기준으로 바꿀 때 곱한다"""
    a, b = str(pd.Timestamp(since).date()), str(pd.Timestamp(until).date())
    out = 1.0
    for d, k in stop_ks(p).items():
        if a < d <= b:
            out *= k
    return out


def event_adjust(p, day, base, prev_close, source, at, final=True):
    """11R C: 보유 종목의 그날 기준가 확인. k = 기준가 / 전날 원주가 종가 가 0.5%(engine.BASE_EVENT) 넘게 다르면 그날 이벤트(권리락·무상증자·
    분할 등) -> 저장 손절가(원주가) x k (백테스트는 수정주가라 이벤트로 기계적으로 떨어진 가격에 손절하지 않음). 조정 전·후 손절가를 stop_adjust 에 남김.
    하루 한 번 (base_check). 11R 보완 3: final False (08:30 장 전 확인) 이고 이벤트가 없으면 잠정 -> 손절 감시 첫 시세로 한 번 더. 반환 기록 dict"""
    from engine import BASE_EVENT
    k = float(base) / float(prev_close)
    rec = {"date": str(pd.Timestamp(day).date()), "at": at, "source": source, "base": float(base), "prev_close": float(prev_close),
           "k": round(k, 6), "adjusted": False, "final": bool(final)}
    if abs(k - 1) >= BASE_EVENT and p.get("stop_price"):
        before = float(p["stop_price"])
        after = before * k
        rec.update(adjusted=True, stop_before=before, stop_after=after)
        p.setdefault("stop_adjust", []).append({k_: rec[k_] for k_ in ("date", "at", "source", "base", "prev_close", "k", "stop_before", "stop_after")})
        p["stop_price"] = after
        p["stop_basis"] = f"{p.get('stop_basis', '')} -> {rec['date']} 이벤트 k={k:.4f} (기준가 {base:,.0f} / 전날 종가 {prev_close:,.0f}) 반영 {before:,.0f} -> {after:,.0f}"[-300:]
    p["base_check"] = rec
    return rec


def base_gap_days(p, prev, hol=None):
    """11U: 마지막 기준가 확인 거래일(base_seen) 뒤 ~ 직전 거래일(prev) 의 거래일 = 기준가를 확인하지 못한 날 (상주 프로그램이 안 돈 날 등).
    base_seen 이 없으면(11U 전 장부) 공백 없음. 반환 (base_seen, [거래일 ...]) — 둘째가 비면 공백 없음"""
    from state import is_trading_day
    last = (p or {}).get("base_seen")
    if not last:
        return None, []
    last, prev = pd.Timestamp(last).normalize(), pd.Timestamp(prev).normalize()
    days, d = [], last + pd.Timedelta(days=1)
    while d <= prev:
        if is_trading_day(d, hol):
            days.append(d)
        d += pd.Timedelta(days=1)
    return last, days


def raw_factors(code):
    """11U: 기준가 공백 확인용 원주가 일봉 (timestamp·factor). 일봉 대상이면 daily.Store (11R 다시 맞춤 반영), 아니면 원주가 파일 + 추가분"""
    from daily import Store
    st = Store([code], use_append=True)
    return st.raw[code] if code in st.raw else raw_daily(code)


def gap_adjust(code, p, day, last, days, at):
    """11U: 기준가 확인 공백 기간(days, 그 앞 확인일 last)의 이벤트를 키움 수정 일봉 계수(factor_events)로 확인해 손절가 x 각 날 k.
    last·공백 날 모두 행·factor 가 있어야 확인됨. 확인되면 이벤트 날마다 stop_adjust 에 남김 (events_k 가 그 k 를 씀), 근거는 base_gap.
    반환 (확인됨 True/False, 기록)"""
    rec = {"date": str(pd.Timestamp(day).date()), "at": at, "from": str(pd.Timestamp(last).date()), "days": [str(x.date()) for x in days]}
    try:
        ev, why = factor_events(raw_factors(code), [last, *days])
    except Exception as e:  # noqa: BLE001  (일봉 파일 못 읽음 등 = 확인 못 함)
        ev, why = None, f"일봉 읽기 실패 {e!r}"[:200]
    if ev is None:
        rec.update(ok=False, why=why)
        p["base_gap"] = rec
        return False, rec
    rec.update(ok=True, source="키움 수정 일봉 계수 (앞 거래일 factor / 그날 factor)", events=ev)
    for dd, k in sorted(ev.items()):
        if p.get("stop_price"):
            before = float(p["stop_price"])
            p["stop_price"] = before * k
            p.setdefault("stop_adjust", []).append({"date": dd, "at": f"{rec['date']} {at}", "source": f"공백 확인: {rec['source']}", "k": round(k, 6),
                                                    "stop_before": before, "stop_after": p["stop_price"]})
            p["stop_basis"] = f"{p.get('stop_basis', '')} -> {dd} 이벤트 k={k:.4f} (공백 확인, 키움 계수) 반영 {before:,.0f} -> {p['stop_price']:,.0f}"[-300:]
    p["base_gap"] = rec
    return True, rec


def mark_base_review(p, day, at, rec):
    """11U: 공백 기간 이벤트를 확인 못 함 -> 그 종목 자동 손절·추세 매도·신규 매수 멈춤 (in_review, 사람이 손절가를 고치거나 표시를 지우면 끝)"""
    p["base_review"] = {"date": str(pd.Timestamp(day).date()), "at": at, "from": rec.get("from"), "days": rec.get("days"), "why": rec.get("why"),
                        "stop": p.get("stop_price"),
                        "clear": "사람이 공백 기간의 권리락·분할 등을 공시로 확인한 뒤 손절가를 고치거나 이 표시(base_review)를 지운다"}
    return p["base_review"]


def mark_synced(pos, day, at, by):
    """보유 장부 수량을 계좌로 맞춘 때 (11K 보완 3: 이 날 이후의 의도만 수량 변화를 설명한다)"""
    pos["qty_synced"] = {"date": str(day), "time": at, "by": by}


# ---------------------------------------------------------------- 현금 대조 (Codex 20)

def kiwoom_fee(amount, rate):
    """11O: 키움 매매수수료 = 체결 금액 x 수수료율, 10원 미만 절사 (주문 하나의 체결 금액 합 기준)"""
    return math.floor(round(float(amount) * float(rate), 6) / 10) * 10


def kiwoom_tax(amount, rate):
    """11O: 매도 세금(증권거래세·농특세) = 체결 금액 x 세율, 원 미만 절사"""
    return math.floor(round(float(amount) * float(rate), 6))


def expected_cash(prev_d2, records, year, commission=None):
    """전일 D+2 추정예수금 + 당일 체결 금액 ± 비용 (수수료 commission, 없으면 백테스트 0.015%, 매도세 그해 세율). 반환 (기대값, 체결 건수).
    11K 11: 수수료는 환경별 (config commission: 모의 0.35%, 실전 0.015%).
    11O: 키움 절사 규칙 (주문마다 수수료 10원 미만 절사, 세금 원 미만 절사). 2026-10-29 모의 실측(매도 1·매수 3)이 이 규칙 + 실제 체결 금액으로 원 단위까지 맞음"""
    tax = sb.SELL_TAX.get(int(year), 0.002)
    fee = sb.COMMISSION if commission is None else float(commission)
    cash, n = float(prev_d2), 0
    for r in records:
        q, px = int(r.get("filled_qty") or 0), r.get("fill_price")
        if q <= 0 or not px:
            continue
        n += 1
        amt = float(px) * q
        if r.get("side") == "buy":
            cash -= amt + kiwoom_fee(amt, fee)
        elif r.get("side") == "sell":
            cash += amt - kiwoom_fee(amt, fee) - kiwoom_tax(amt, tax)
    return cash, n


def fill_amount(records, kind=None, side=None):
    """체결 금액 합 (kind·side 로 거름)"""
    return float(sum(float(r["fill_price"]) * int(r["filled_qty"]) for r in records if int(r.get("filled_qty") or 0) > 0 and r.get("fill_price")
                     and (kind is None or r.get("kind") == kind) and (side is None or r.get("side") == side)))


def cash_tolerance(records):
    """11O: 현금 대조 허용치 (원) = 1 + 체결 주문마다 [체결 수량 x 1원 (ka10076 cntr_pric 는 주문의 평균 체결가를 원 단위로 끊은 값이라
    여러 가격에 나눠 체결되면 1주당 1원 미만 차이, 2026-10-29 990011 156주 기록 2841 = 실제 평균 약 2841.51) + 10원 (수수료 10원 절사 경계)
    + 2원 (세금 원 절사 경계, 거래세·농특세 따로)]. 예전 20원 x 건수 + 1 은 체결가 끊김을 넣지 않아 수량이 많은 매도에서 넘침"""
    tol = 1.0
    for r in records:
        q = int(r.get("filled_qty") or 0)
        if q > 0 and r.get("fill_price"):
            tol += q + 12
    return tol
