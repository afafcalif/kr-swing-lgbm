"""
일봉: 전날까지 추가분 수집(시세 조회 TR 만) + 장 전 메모리 적재 (EXEC_SPEC 1-1, 2-4, 8장 6)

  python backtest_swing/live/daily.py update   # 백테스트 일봉 파일 마지막 날 다음 ~ 어제: ka10081 수정주가·원주가 + 한국거래소 공식 시세 보정 + ka20006 지수

백테스트 일봉 파일(data/daily_ext·daily_merged·daily_raw, 지수 파일)은 고치지 않는다. 추가분은 data/live_append/ 에 따로 두고
load_stock 과 같은 계산에 이어 붙인다.
- 추가분 행: 수정주가 = ka10081 수정주가(upd_stkpc_tp 1), 원주가 = ka10081 원주가(0), 2026-09-14 이후라 고가·저가·종가는
  krx_official.apply_regular_session 으로 공식 정규장 값, 거래대금은 백만원 -> 원, factor = 수정주가 종가 / 원주가 종가.
- 원주가 파일에 수정주가 파일 마지막 날 뒤의 행이 있으면(예: 10-19, factor 빈칸) 추가분으로 바꾼다.
- 수정주가 파일과 겹치는 최근 5거래일의 시가(장 마감 후 가격이 섞이지 않는 값)가 다르면 그 사이 수정 계수가 바뀐 것으로 보고
  "adj_changed" 로 기록하고 그 종목은 쓰지 않는다 (백테스트 파일을 다시 만들기 전까지).
"""
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import APPEND_DIR, Caller, data_file, num, publish_data, sd, s4  # noqa: E402

KIWOOM_AMOUNT_UNIT = 1_000_000
CHECK_DAYS = 5


def file_last_dates(code):
    p = os.path.join(sd.EXT_DIR, f"{code}.parquet")
    path = p if os.path.exists(p) else os.path.join(sd.MERGED_DIR, f"{code}.parquet")
    adj = pd.read_parquet(path)
    adj["timestamp"] = pd.to_datetime(adj.timestamp)
    return adj


def _daily(caller, code, upd, today):
    j, _, _ = caller.post("ka10081", "/api/dostk/chart", {"stk_cd": code, "base_dt": today, "upd_stkpc_tp": upd})
    rows = [{"timestamp": pd.Timestamp(it["dt"]), "open": num(it.get("open_pric")), "high": num(it.get("high_pric")),
             "low": num(it.get("low_pric")), "close": num(it.get("cur_prc")), "volume": num(it.get("trde_qty")),
             "amount": num(it.get("trde_prica")) * KIWOOM_AMOUNT_UNIT}
            for it in j.get("stk_dt_pole_chart_qry", []) or [] if it.get("dt") and it["dt"] < today]
    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True) if rows else pd.DataFrame()


def update(only=None):
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
    pend_a, pend_r = [], []
    for p_ in ("pending_adj.parquet", "pending_raw.parquet", "pending_index.parquet"):
        if os.path.exists(os.path.join(APPEND_DIR, p_)):
            os.remove(os.path.join(APPEND_DIR, p_))
    if missing_dates:
        first_missing = min(missing_dates)
        ix[pd.to_datetime(ix.timestamp) >= first_missing].to_parquet(os.path.join(APPEND_DIR, "pending_index.parquet"), index=False)
        ix = ix[pd.to_datetime(ix.timestamp) < first_missing]
        print(f"official missing {[str(x.date()) for x in missing_dates]} -> rows from {first_missing.date()} not appended", flush=True)
    print(f"official: {official.date.nunique() if len(official) else 0} days {sorted(official.date.dt.strftime('%Y-%m-%d').unique()) if len(official) else []}", flush=True)
    adj_parts, raw_parts, flags = [], [], {"adj_changed": [], "no_official": [], "fetch_error": [], "no_new_rows": [], "missing_row": []}
    for i, code in enumerate(codes):
        try:
            fa = file_last_dates(code)
            last = fa.timestamp.max()
            a = _daily(caller, code, "1", today)
            r = _daily(caller, code, "0", today)
        except ValueError as e:
            flags["fetch_error"].append([code, str(e)[:100]])
            continue
        if a.empty or r.empty:
            flags["fetch_error"].append([code, "empty"])
            continue
        chk = a[a.timestamp <= last].tail(CHECK_DAYS).merge(fa[["timestamp", "open"]], on="timestamp", suffixes=("", "_file"))
        if len(chk) and not np.allclose(chk.open, chk.open_file, rtol=1e-9, atol=0):
            flags["adj_changed"].append(code)
            continue
        na, nr = a[a.timestamp > last].copy(), r[r.timestamp > last].copy()
        if missing_dates:
            # 공식 시세 기다리는 날의 키움 행은 따로 보관 (공식 시세가 나오면 reapply_official 이 보정해 붙임)
            pend_a.append(na[na.timestamp >= min(missing_dates)].assign(code=code))
            pend_r.append(nr[nr.timestamp >= min(missing_dates)].assign(code=code))
            na, nr = na[na.timestamp < min(missing_dates)], nr[nr.timestamp < min(missing_dates)]
        off = off_by.get(code)
        lack = lacking_rows(off, na.timestamp, last, min(missing_dates) if missing_dates else None)
        if lack:  # 11K 5: 공식 시세에는 거래가 있는 날인데 우리 일봉(키움)에 그 행이 없음 -> 그 종목은 붙이지 않고 그날 계산에서 제외
            flags["missing_row"].append([code, lack])
            continue
        if na.empty:
            flags["no_new_rows"].append(code)
            continue
        if off is not None:
            na, _ = krx_official.apply_regular_session(na, off)
            nr, _ = krx_official.apply_regular_session(nr, off)
        else:
            flags["no_official"].append(code)
        nr = nr.merge(na[["timestamp", "close"]].rename(columns={"close": "adj_close"}), on="timestamp", how="left")
        nr["factor"] = nr.adj_close / nr.close
        adj_parts.append(na.assign(code=code))
        raw_parts.append(nr.drop(columns=["adj_close"]).assign(code=code))
        if (i + 1) % 250 == 0:
            print(f"  {i + 1}/{len(codes)} stocks, {caller.n} requests, {time.time() - t0:.0f}s, errors {len(caller.errors)}", flush=True)
    cols_a = ["timestamp", "open", "high", "low", "close", "volume", "amount", "code"]
    adj = pd.concat(adj_parts, ignore_index=True) if adj_parts else pd.DataFrame(columns=cols_a)
    raw = pd.concat(raw_parts, ignore_index=True) if raw_parts else pd.DataFrame(columns=cols_a[:-1] + ["factor", "code"])
    adj["timestamp"], raw["timestamp"] = pd.to_datetime(adj.timestamp), pd.to_datetime(raw.timestamp)
    if pend_a:
        pd.concat(pend_a, ignore_index=True).to_parquet(os.path.join(APPEND_DIR, "pending_adj.parquet"), index=False)
        pd.concat(pend_r, ignore_index=True).to_parquet(os.path.join(APPEND_DIR, "pending_raw.parquet"), index=False)
    rec = {"run": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), "codes": len(codes), "requests": caller.n,
           "seconds": round(time.time() - t0, 1), "errors": caller.errors, "stopped": caller.stopped, "hourly": dict(caller.hourly),
           "index": ix_note, "adj_rows": len(adj), "dates": sorted(adj.timestamp.dt.strftime("%Y-%m-%d").unique().tolist()),
           "factor_not_1": int((raw.factor.sub(1).abs() > 1e-9).sum()),
           "official_received": received, "official_missing_dates": [str(x.date()) for x in missing_dates],
           "latest_trading_day": latest_trading_day,
           # 데이터 버전 (2026-10-24 결정 B6): 반영 시각 | 마지막 반영 날짜 | 공식 시세 없는 날
           "data_version": f"{pd.Timestamp.now():%Y%m%d%H%M%S}|{(sorted(adj.timestamp.dt.strftime('%Y-%m-%d').unique().tolist()) or ['-'])[-1]}"
                           f"|missing={','.join(str(x.date()) for x in missing_dates) or '-'}",
           **{k: v for k, v in flags.items()}}
    # 확인 리뷰 15: 지수·adj·raw·update.json 을 새 버전 폴더에 다 쓴 뒤 포인터만 교체
    v = publish_data({"index.parquet": ix, "adj.parquet": adj, "raw.parquet": raw, "update.json": rec}, APPEND_DIR)
    print(f"data version {v}", flush=True)
    print(json.dumps({k: (v if not isinstance(v, list) or len(v) < 20 else f"{len(v)} items") for k, v in rec.items()},
                     ensure_ascii=False), flush=True)


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
    기다리던 날이 모두 나왔을 때만 붙인다. 반환: 결과 dict"""
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
            "data_dir_version": v}


# ---------------------------------------------------------------- 메모리 적재

def excluded_codes():
    """그날 대상에서 빼는 종목 (2026-10-22 결정): 수정주가 계수가 바뀐 종목(기본 일봉 파일을 다시 만들 때까지),
    추가분에 공식 시세가 없는 종목, 일봉 응답이 비어 추가분이 없는 종목"""
    p = data_file("update.json", APPEND_DIR)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        u = json.load(f)
    return {"adj_changed": sorted(u.get("adj_changed", [])), "no_official": sorted(u.get("no_official", [])),
            "fetch_error": sorted(c for c, _ in u.get("fetch_error", [])),
            "missing_row": sorted(c for c, _ in u.get("missing_row", []))}  # 11K 5: 공식 시세에 있는 날의 일봉 행 없음


class Store:
    """종목별 수정주가·원주가 원본 행 (백테스트 파일 + 추가분). load_stock 이 파일에서 읽는 것과 같은 열."""

    def __init__(self, codes, use_append=True):
        self.adj, self.raw = {}, {}
        add_a = add_r = None
        bad = set()
        dd = os.path.dirname(data_file("adj.parquet", APPEND_DIR))  # 한 버전 폴더에서 adj·raw 를 함께 읽는다
        if use_append and os.path.exists(os.path.join(dd, "adj.parquet")):
            add_a = dict(tuple(pd.read_parquet(os.path.join(dd, "adj.parquet")).groupby("code")))
            add_r = dict(tuple(pd.read_parquet(os.path.join(dd, "raw.parquet")).groupby("code")))
            bad = set().union(*excluded_codes().values())
        self.excluded = sorted(bad & set(codes))
        for code in codes:
            if code in bad:
                continue
            adj = file_last_dates(code)
            raw = pd.read_parquet(os.path.join(sd.RAW_DIR, f"{code}.parquet"))
            raw["timestamp"] = pd.to_datetime(raw.timestamp)
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
