"""
15:20:05 측정 스냅샷 판정 보조 (EXEC_SPEC 1-3 "2차 수정 (2026-10-23)"). 시세 조회 TR 만.

(2) 두 장부 각각 "15:20:05 스냅샷으로 계산한 상위 3" = "15:19 1분봉으로 만든 행(3A 방식)으로 계산한 상위 3" 인지.
같은 그날 장부 상태(그날 로그의 ledger_before)를 넣는다.

  python backtest_swing/live/measure.py backfill 2026-10-23   # 그날 1분봉·업종 분봉을 받아 계산 (참고용, 판정에 쓰지 않는 날)
"""
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import LOG_DIR, Caller, num, sd  # noqa: E402
import engine as E  # noqa: E402
import quotes as Q  # noqa: E402

NAMES = ("strategy", "full")


def rows_upto(bars, upto="1519"):
    """1분봉 -> 그날 행 (3A 방식: exec_stage3_measure.snap_values 와 같은 값). 거래대금 = 15:19 까지 (거래량 x 종가) 합"""
    rows = {}
    for code, b in bars.items():
        if b is None or b.empty:
            continue
        u = b[b.ts.str[8:12] <= upto]
        if u.empty:
            continue
        rows[code] = ({"open": float(u.open.iloc[0]), "high": float(u.high.max()), "low": float(u.low.min()), "close": float(u.close.iloc[-1]),
                       "volume": float(u.volume.sum()), "amount": float((u.volume * u.close).sum())}, None, None)
    return rows


def index_upto(caller, day, upto="1519"):
    out = {}
    for name, _, inds in Q.INDEX:
        j, _, _ = caller.post("ka20005", "/api/dostk/chart", {"inds_cd": inds, "tic_scope": "1"})
        d = day.strftime("%Y%m%d")
        b = sorted((it["cntr_tm"], num(it.get("cur_prc")) / 100.0) for it in j.get("inds_min_pole_qry", []) or []
                   if str(it.get("cntr_tm", ""))[:8] == d and str(it["cntr_tm"])[8:12] <= upto)
        out[name] = b[-1][1] if b else np.nan
    return out


def snap_rows(path):
    s = pd.read_parquet(path)
    s = pd.DataFrame({c[2:]: s[c] for c in s.columns if c.startswith("p_")})
    return {r.code: ({"open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume, "amount": r.amount}, None, None)
            for r in s.itertuples() if r.volume > 0 and r.close > 0 and r.open > 0}


def top3_compare(pool, day, index, boosters, cols, ledgers, rows_a, idx_a, rows_b, idx_b):
    """기준 = 15:19 1분봉 행 + 15:19 업종 분봉 지수 (3A 방식).
    (a) 판정용 (2026-10-23 결정): 종목 = 15:20:05 스냅샷, 지수 = 15:19 업종 분봉 종가. 결과의 strategy·full 키가 (a).
    (b) 참고: 종목·지수 모두 15:20:05 스냅샷 (지수 = 그때 ka20001)."""
    base = E.run_day(pool, day, rows_b, index, idx_b, boosters, cols, ledgers)
    ra = E.run_day(pool, day, rows_a, index, idx_b, boosters, cols, ledgers)
    rr = E.run_day(pool, day, rows_a, index, idx_a, boosters, cols, ledgers)
    out = {"index_1520_ka20001": idx_a, "index_1519_bar": idx_b, "eligible_bars_1519": len(base["eligible"]),
           "eligible_a": len(ra["eligible"]), "eligible_b_ref": len(rr["eligible"]), "ref_b": {}}
    for n in NAMES:
        b0 = list(base["ledgers"][n]["top3"].code)
        a, r = list(ra["ledgers"][n]["top3"].code), list(rr["ledgers"][n]["top3"].code)
        out[n] = {"top3_bars_1519": b0, "top3_a_snapshot_1520_index_1519bar": a, "eq_3of3": set(a) == set(b0) and len(a) == len(b0),
                  "same_order": a == b0}
        out["ref_b"][n] = {"top3_b_snapshot_1520_index_ka20001": r, "eq_3of3": set(r) == set(b0) and len(r) == len(b0), "same_order": r == b0}
    return out


def backfill(date):
    """그날 장 마감 뒤: ka10080 첫 페이지(그날 1분봉) 전 종목 + ka20005 를 받아 (2) 를 계산하고 그날 로그·summary.csv 에 남긴다"""
    import model as M
    import run_day as R
    from daily import index_series
    day = pd.Timestamp(date)
    lp = os.path.join(LOG_DIR, f"{day.date()}.json")
    log = json.load(open(lp, encoding="utf-8"))
    st = json.load(open(os.path.join(LOG_DIR, "state", f"{day.date()}.json"), encoding="utf-8"))
    mfile = st["measure_snapshot"]["file"]
    rows_a = snap_rows(mfile)
    t0 = time.time()
    caller = Caller("prod", 5)
    bars = {}
    for code in rows_a:
        try:
            bars[code] = Q.bars_today(caller, code, day)
        except ValueError:
            pass
    idx_b = index_upto(caller, day)
    rows_b = rows_upto(bars)
    b, cols, _, chk = M.load(day.strftime("%Y-%m"))
    pool = E.ShardPool(sd.universe_codes(), n=12, use_append=True)
    try:
        res = top3_compare(pool, day, index_series(), b, cols, log["ledger_before"], rows_a, st["measure_snapshot"]["index_values"], rows_b, idx_b)
    finally:
        pool.close()
    res.update(requests=caller.n, seconds=round(time.time() - t0, 1), rows_1520=len(rows_a), rows_1519bars=len(rows_b),
               note="10-23 은 판정에 쓰지 않음 (참고, EXEC_SPEC 1-3 2차 수정)" if str(day.date()) == "2026-10-23" else "")
    log.setdefault("measure_1520", {})["top3_compare"] = res
    R.jdump(lp, log)
    R.summary(log)
    print(json.dumps(res, ensure_ascii=False, default=str))


if __name__ == "__main__":
    {"backfill": backfill}[sys.argv[1]](*sys.argv[2:])
