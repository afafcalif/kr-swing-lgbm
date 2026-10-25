"""
시험 B (속도): 15:18:30 스냅샷 시작 -> 주문 계획까지 걸리는 시간. 시세 조회 TR 만.

  python backtest_swing/live/speed.py     # 실전 토큰(1초 5회)·모의 토큰(1초 1회)으로 ka10095 전 종목 + ka20001 지수를 각각 한 번씩 재고,
                                          # 종목 계산 시간은 시험 A 의 하루 계산 시간(replay_daily.csv)을 쓴다 -> results/live/speed.md
ka10095 거래대금(trde_prica) 단위도 여기서 확인한다 (거래량 x 현재가 와 비교).
"""
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Caller, sd  # noqa: E402
import quotes as Q  # noqa: E402

OUT = "backtest_swing/results/live"


def main():
    codes = sd.universe_codes()
    rec = {}
    for env, rps in (("prod", 5), ("mock", 1)):
        c = Caller(env, rps)
        t0 = time.time()
        try:
            raw, snap, srec = Q.snapshot(c, codes)
            t1 = time.time()
            idx, _ = Q.index_now(c)
            rec[env] = {"rps": rps, "snapshot_s": round(t1 - t0, 2), "index_s": round(time.time() - t1, 2), "requests": c.n,
                        "returned": srec["returned"], "missing": len(srec["missing"]), "errors": c.errors, "index": idx,
                        "at": pd.Timestamp.now().strftime("%H:%M:%S")}
            if env == "prod":
                s = snap[(snap.volume > 0) & (snap.close > 0)].copy()
                s["ratio"] = s.amount / (s.volume * s.close)
                rec["amount_unit_check"] = {"n": len(s), "ratio_median": float(s.ratio.median()) if len(s) else None,
                                            "ratio_p05": float(s.ratio.quantile(.05)) if len(s) else None,
                                            "ratio_p95": float(s.ratio.quantile(.95)) if len(s) else None}
        except Exception as e:  # noqa: BLE001
            rec[env] = {"rps": rps, "error": repr(e)[:300], "requests": c.n, "errors": c.errors}
    # 종목 계산: 전 종목 행을 넣은 계산 시간 (2026-10-21 일봉 원주가 값을 오늘 행으로, 일봉은 10-20 까지 메모리 적재)
    import engine as E
    import model as Mo
    from daily import index_series
    day = pd.Timestamp("2026-10-21")
    from common import data_file
    raw_add = pd.read_parquet(data_file("raw.parquet"))
    r21 = raw_add[raw_add.timestamp == day]
    rows = {r.code: ({"open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume, "amount": r.amount}, None, None)
            for r in r21.itertuples() if r.volume > 0}
    b, cols, _, _ = Mo.load("2026-10")
    index = index_series()
    vals = {k: float(s[day]) for k, s in index.items()}
    t0 = time.time()
    pool = E.ShardPool(codes, n=12, use_append=True)
    t_load = time.time() - t0
    secs = []
    try:
        for _ in range(3):
            res = E.run_day(pool, day, rows, index, vals, b, cols, [])
            secs.append(res["seconds_total"])
            t1 = time.time()
            E.order_plan(res["top3"], price_col="raw_close")
            secs[-1] += time.time() - t1
    finally:
        pool.close()
    comp, compm = float(max(secs)), float(np.mean(secs))
    rec["compute"] = {"rows": len(rows), "eligible": len(res["eligible"]), "seconds": secs, "pool_load_s": round(t_load, 1)}
    L = ["# 시험 B: 스냅샷 시작 -> 주문 계획 시간 (해석 없음)\n",
         f"대상 종목 목록 {len(codes)}종목 (ka10095 100종목씩), 지수 ka20001 2회. 종목 계산 + 주문 계획 = 전 종목 {rec['compute']['rows']}행 "
         f"(2026-10-21 일봉 값을 오늘 행으로, 대상 {rec['compute']['eligible']}종목) 3회: {[round(s_, 1) for s_ in secs]}초 "
         f"(작업 프로세스 12개, 일봉은 장 전에 메모리 적재 {rec['compute']['pool_load_s']}초, 장부 비움).\n",
         "| 토큰 | 호출 제한 | 요청 수 | 스냅샷 (초) | 지수 (초) | 종목 계산 최대 (초) | 합계 (초) | 15:18:30 시작 시 끝나는 시각 | 비고 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for env in ("prod", "mock"):
        r = rec.get(env, {})
        if "error" in r:
            L.append(f"| {env} | 1초 {r['rps']}회 | {r['requests']} | - | - | - | - | - | 오류: {r['error']} |")
            continue
        tot = r["snapshot_s"] + r["index_s"] + comp + 1
        end = (pd.Timestamp("2026-10-22 15:18:30") + pd.Timedelta(seconds=tot)).strftime("%H:%M:%S")
        L.append(f"| {env} | 1초 {r['rps']}회 | {r['requests']} | {r['snapshot_s']:.1f} | {r['index_s']:.1f} | {comp:.1f} | {tot:.1f} | {end} | "
                 f"응답 {r['returned']}종목, 누락 {r['missing']}, 유량 오류 {len(r['errors'])}, 잰 시각 {r['at']} |")
    if "amount_unit_check" in rec:
        a = rec["amount_unit_check"]
        L.append(f"\nka10095 거래대금 x 100만 / (거래량 x 현재가): {a['n']}종목, 중앙값 {a['ratio_median']}, 5% {a['ratio_p05']}, 95% {a['ratio_p95']}")
    with open(os.path.join(OUT, "speed.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    json.dump(rec, open(os.path.join(OUT, "speed.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print("\n".join(L))


if __name__ == "__main__":
    main()
