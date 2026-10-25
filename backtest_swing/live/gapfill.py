"""
2026-10-19 ~ 2026-10-21 공백 채우기: 그날 후보 종목의 1분봉(ka10080, 실전 토큰, 1초 5회 이하)을 받아 15:19 스냅샷을 만들고
실행 엔진으로 두 장부(전략·전체)의 상위 3 을 계산해 장부에 넣는다. 시세 조회 TR 만.

  python backtest_swing/live/gapfill.py fetch   # 후보 계산 + 1분봉·업종 분봉 -> results/live/gap_minute/
  python backtest_swing/live/gapfill.py build   # 2025-10-01 ~ 10-16 기준 장부(동결 방식) + 10-19 ~ 10-21 엔진 계산 -> logs/exec/ledger.json

후보 = 그날 종가 기준 20일 평균 거래대금 50억 이상 (extend_judge 와 같은 기준, 일봉 = 백테스트 파일 + 추가분), 제외 종목 빼고.
1분봉은 최신 페이지부터 거슬러 10-19 09:00 이 덮일 때까지 받는다 (한 페이지 900봉은 장 마감 후 봉 때문에 10-19 에 닿지 않음, 최대 4페이지).
15:19 행·지수·factor 는 replay.py 와 같은 방식 (거래대금 = min(15:19 까지 거래량 x 종가 합, 그날 일봉 거래대금)).
"""
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import LOG_DIR, Caller, num, sd  # noqa: E402

OUT = "backtest_swing/results/live"
MIN_DIR = os.path.join(OUT, "gap_minute")
GAP_DAYS = [pd.Timestamp(d) for d in ("2026-10-19", "2026-10-20", "2026-10-21")]
FIRST = "20261019090000"
MAX_PAGES = 4


def candidates():
    from daily import Store, excluded_codes
    import engine as E
    codes = sd.universe_codes()
    st = Store(codes, use_append=True)
    cur = {}
    lm = E.make_load_mem(lambda c: cur[c])
    out = {}
    for code in st.adj:
        cur[code] = (st.adj[code], st.raw[code])
        df = lm(code, until=GAP_DAYS[-1]).set_index("timestamp")
        s = df.amt20.reindex(GAP_DAYS)
        d = [x.strftime("%Y%m%d") for x in s.index[(s >= sd.MIN_AMOUNT_20).to_numpy()]]
        if d:
            out[code] = d
    return out, excluded_codes()


def pull(caller, tr, body, key_list):
    rows, cont, key, pages = [], "N", "", 0
    while pages < MAX_PAGES:
        j, c, k = caller.post(tr, "/api/dostk/chart", body, cont, key)
        pages += 1
        items = j.get(key_list, []) or []
        rows += [{"ts": it.get("cntr_tm"), "open": num(it.get("open_pric")), "high": num(it.get("high_pric")),
                  "low": num(it.get("low_pric")), "close": num(it.get("cur_prc")), "volume": num(it.get("trde_qty"))}
                 for it in items if it.get("cntr_tm")]
        if not items or min(it["cntr_tm"] for it in items if it.get("cntr_tm")) <= FIRST or c != "Y" or not k:
            break
        cont, key = "Y", k
    df = pd.DataFrame(rows)
    if len(df):
        df = df.drop_duplicates("ts")
        df = df[(df.ts.str[:8] >= "20261019") & (df.ts.str[:8] <= "20261021") & (df.ts.str[8:12] <= "1530")].sort_values("ts").reset_index(drop=True)
    return df, pages


def fetch():
    os.makedirs(MIN_DIR, exist_ok=True)
    t0 = time.time()
    cand, excl = candidates()
    json.dump({"candidates": cand, "excluded": excl}, open(os.path.join(OUT, "gap_candidates.json"), "w"), ensure_ascii=False)
    t1 = time.time()
    print(f"candidates {len(cand)} stocks ({t1 - t0:.0f}s), per day " +
          str({d.strftime('%Y%m%d'): sum(d.strftime('%Y%m%d') in v for v in cand.values()) for d in GAP_DAYS}), flush=True)
    caller = Caller("prod", 5)
    pages_hist, skipped, short = {}, [], []
    for inds, name in (("001", "kospi"), ("101", "kosdaq")):
        df, pg = pull(caller, "ka20005", {"inds_cd": inds, "tic_scope": "1"}, "inds_min_pole_qry")
        for c in ("open", "high", "low", "close"):
            df[c] = df[c] / 100.0
        df.to_parquet(os.path.join(MIN_DIR, f"index_{name}.parquet"), index=False)
        pages_hist[f"index_{name}"] = pg
    for i, code in enumerate(sorted(cand)):
        try:
            df, pg = pull(caller, "ka10080", {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "0"}, "stk_min_pole_chart_qry")
        except ValueError:
            skipped.append(code)
            continue
        pages_hist[code] = pg
        if len(df) == 0 or df.ts.min() > FIRST:
            short.append(code)
        df.to_parquet(os.path.join(MIN_DIR, f"{code}.parquet"), index=False)
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(cand)}, {caller.n} requests, {time.time() - t1:.0f}s", flush=True)
    pc = pd.Series([v for k, v in pages_hist.items() if not k.startswith("index")])
    rec = {"stocks": len(cand), "requests": caller.n, "seconds": round(time.time() - t1, 1), "pages_per_stock": pc.value_counts().sort_index().to_dict(),
           "index_pages": {k: v for k, v in pages_hist.items() if k.startswith("index")}, "skipped_other_error": skipped,
           "not_reaching_1019_0900": short, "rate_errors": caller.errors, "stopped": caller.stopped, "hourly": dict(caller.hourly),
           "started": pd.Timestamp.fromtimestamp(t1).strftime("%H:%M:%S")}
    json.dump(rec, open(os.path.join(OUT, "gap_fetch.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print(json.dumps({k: (v if not isinstance(v, list) or len(v) < 15 else f"{len(v)} items") for k, v in rec.items()}, ensure_ascii=False, default=str))


def _snap_rows(day):
    """그날 후보의 15:19 행 (replay._synth 와 같은 값 계산, 일봉 = 추가분)"""
    import exec_stage3_measure as x
    g = json.load(open(os.path.join(OUT, "gap_candidates.json"), encoding="utf-8"))
    excl = set().union(*[set(v) for v in g["excluded"].values()])
    from common import data_file
    adj = pd.read_parquet(data_file("adj.parquet"))
    raw = pd.read_parquet(data_file("raw.parquet"))
    a = adj[adj.timestamp == day].set_index("code")
    r = raw[raw.timestamp == day].set_index("code")
    ds = day.strftime("%Y%m%d")
    rows, no_bar = {}, []
    for code, days in g["candidates"].items():
        if ds not in days or code in excl:
            continue
        p = os.path.join(MIN_DIR, f"{code}.parquet")
        sv = x.snap_values(pd.read_parquet(p), ds) if os.path.exists(p) else None
        if sv is None or code not in a.index or code not in r.index:
            no_bar.append(code)
            continue
        rv = float(r.at[code, "volume"])
        vs = float(a.at[code, "volume"]) / rv if rv > 0 else 1.0
        rows[code] = ({"open": sv["open"], "high": sv["high"], "low": sv["low"], "close": sv["close"], "volume": sv["volume"],
                       "amount": min(sv["amount"], float(a.at[code, "amount"]))}, float(r.at[code, "factor"]), vs)
    vals = {}
    for name in ("kospi", "kosdaq"):
        im = pd.read_parquet(os.path.join(MIN_DIR, f"index_{name}.parquet"))
        d = im[(im.ts.str[:8] == ds) & (im.ts.str[8:12] <= "1519")]
        vals[name] = float(d.close.iloc[-1])
    return rows, vals, no_bar, sorted(excl)


def build():
    from daily import index_series
    import engine as E
    import model as M
    t0 = time.time()
    refp = pd.read_parquet(os.path.join(OUT, "replay_ref_picks.parquet"))
    refs = pd.read_parquet(os.path.join(OUT, "replay_ref_picks_strategy.parquet"))
    c1016 = json.load(open(os.path.join(OUT, "check1016.json"), encoding="utf-8"))
    d16 = pd.Timestamp("2026-10-16")
    hist = [{"ledger": "full", "date": r.date, "code": r.code, "score": r.score, "source": "extend_judge 15:19 장부"}
            for r in refp[refp.date < d16].itertuples()]
    hist += [{"ledger": "full", "date": d16, "code": c, "score": np.nan, "source": "실행 엔진 (10-16, 열린 거래 건너뜀)"} for c in c1016["engine_top3"]]
    hist += [{"ledger": "strategy", "date": r.date, "code": r.code, "score": r.score, "source": "extend_judge 15:19 장부 (필터 통과일)"}
             for r in refs.itertuples()]
    ledgers = {name: [{"code": h["code"], "entry": str(pd.Timestamp(h["date"]).date())} for h in hist if h["ledger"] == name]
               for name in ("strategy", "full")}
    b, cols, _, chk = M.load("2026-10")
    assert chk["ok"]
    index = index_series()
    pool = E.ShardPool(sd.universe_codes(), n=12, use_append=True)
    days = []
    try:
        for D in GAP_DAYS:
            rows, vals, no_bar, excl = _snap_rows(D)
            res = E.run_day(pool, D, rows, index, vals, b, cols, ledgers)
            fok = res["filter"]["filter_ok"]
            rec = {"date": str(D.date()), "filter": res["filter"], "rows": len(rows), "eligible": len(res["eligible"]),
                   "candidates_without_1519_bar": no_bar, "excluded": excl, "index_1519": vals, "errors": res["errors"]}
            for name in ("strategy", "full"):
                per = res["ledgers"][name]
                top = list(per["top3"].code)
                add = top if (name == "full" or fok) else []
                rec[name] = {"top3": top, "added": add, "skipped_busy": per["skipped_busy"], "open_before": sum(s["open"] for s in per["ledger_status"])}
                ledgers[name] = [{"code": s["code"], "entry": s["entry"]} for s in per["ledger_status"] if s["open"]] + \
                                [{"code": c, "entry": str(D.date())} for c in add]
                hist += [{"ledger": name, "date": D, "code": c, "score": float(per["top3"].set_index("code").score[c]), "source": "실행 엔진 (10-19 ~ 10-21 공백 채우기)"}
                         for c in add]
            days.append(rec)
            print(json.dumps({k: rec[k] for k in ("date", "rows", "eligible", "strategy", "full")}, ensure_ascii=False), flush=True)
        # 10-21 장 마감까지의 일봉으로 장부 상태 (오늘 행 없이)
        today = pd.Timestamp("2026-10-22")
        lf = {}
        for trs in ledgers.values():
            for tr in trs:
                e = pd.Timestamp(tr["entry"])
                lf[tr["code"]] = min(e, lf.get(tr["code"], e))
        _, arrs, _ = pool.compute(today, {}, lf)
        state = {}
        for name, trs in ledgers.items():
            _, st = E.ledger_status(trs, arrs, today)
            ledgers[name] = [{"code": s["code"], "entry": s["entry"]} for s in st if s["open"]]
            unk = [s for s in st if s["open"] and s["reason"].startswith("일봉 없음")]
            state[name] = {"open": len(ledgers[name]), "open_no_daily_data": len(unk), "open_trades": [s for s in st if s["open"]]}
    finally:
        pool.close()
    jdump = lambda p, o: json.dump(o, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)  # noqa: E731
    jdump(os.path.join(LOG_DIR, "ledger.json"), {"as_of": "2026-10-21", "strategy": ledgers["strategy"], "full": ledgers["full"],
                                                 "note": "전략 장부 = 시장 필터 통과일 상위 3, 전체 장부 = 필터 무관 매일 상위 3. 2025-10-01 ~ 10-16 은 extend_judge 15:19 장부 "
                                                         "(10-16 전체 장부는 실행 엔진 값), 10-19 ~ 10-21 은 실행 엔진 공백 채우기. 열린 거래만 남김"})
    pd.DataFrame(hist).to_parquet(os.path.join(OUT, "ledger_history.parquet"), index=False)
    jdump(os.path.join(OUT, "gap_build.json"), {"days": days, "state_after_1021_close": state, "seconds": round(time.time() - t0, 1)})
    print(json.dumps({k: v["open"] for k, v in state.items()}, ensure_ascii=False), f"({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    {"fetch": fetch, "build": build}[sys.argv[1]]()
