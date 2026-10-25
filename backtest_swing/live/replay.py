"""
시험 A (재생): 이미 받은 1분봉으로 2026-07-20 ~ 2026-10-16 각 날짜의 15:19 스냅샷을 만들어 실행 엔진(오늘 행 -> 특징·대상 -> 점수
-> 가상 장부 pick_nodup -> 상위 3)에 넣고, extend_judge 계산(exec_stage3_measure_all.build_frames, 장부는 2025-10-01 부터 전체 기간)의
15:19 상위 3·특징값과 비교한다. 주문·계좌 조회 없음, API 호출 없음.

  python backtest_swing/live/replay.py ref    # extend_judge 와 같은 계산으로 기준 표 저장 (results/live/replay_ref_*.parquet)
  python backtest_swing/live/replay.py run    # 재생 비교 -> results/live/replay_test.md, replay_daily.csv

재생 스냅샷 = ka10095 응답을 파싱한 뒤의 값과 같은 열 (원주가 시가·고가·저가·현재가·누적 거래량·거래대금[원]).
값은 extend_judge 의 15:19 행과 같게 만든다: 시가 = 첫 봉, 고가·저가 = 15:19 까지, 현재가 = 15:19 봉 종가, 거래량 = 15:19 까지 합,
거래대금 = min(15:19 까지 (거래량 x 종가) 합, 그날 일봉 거래대금). factor·vscale 은 그날 일봉 파일 값 (exec_stage3_measure._worker 와 같음).
대상 종목: extend_judge 와 같은 후보 (candidates_ext.json ∪ candidates.json 의 그날 후보).
"""
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import sd  # noqa: E402
import engine as E  # noqa: E402
import model as M  # noqa: E402

import exec_stage3_measure as x  # noqa: E402
import stage5_validation as v  # noqa: E402

OUT = "backtest_swing/results/live"
START, END = pd.Timestamp("2026-07-20"), pd.Timestamp("2026-10-16")
FULL_START = pd.Timestamp("2025-10-01")
REL_TOL = 1e-6


def calendar(a, b):
    cal = pd.DatetimeIndex(pd.read_parquet(sd.KOSPI, columns=["timestamp"]).timestamp).sort_values()
    return cal[(cal >= a) & (cal <= b)]


def ref():
    import exec_stage3_measure_all as xa
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    days = calendar(FULL_START, END)
    snap = pd.read_parquet(os.path.join(x.OUT, "snap_rows_ext.parquet"))
    close_frame, snap_frame, chk, mk_note, _ = xa.build_frames(days, snap, (FULL_START, pd.Timestamp("2026-10-31")))
    dl = list(days)
    spk = snap_frame.loc[xa.top3(snap_frame, dl)]
    # 전략 장부: 시장 필터 통과일만 (2026 검증·P4 와 같은 방식, 장부는 통과일만으로 이어짐)
    fdays = [d for d in dl if bool(snap_frame[snap_frame.date == d].filter_ok.any())]
    spk_s = snap_frame.loc[xa.top3(snap_frame, fdays)]
    cols = [c for c in snap_frame.columns if c not in ("g",)]
    snap_frame[snap_frame.date >= START][cols].to_parquet(os.path.join(OUT, "replay_ref_frame.parquet"), index=False)
    spk[["code", "date", "score"]].to_parquet(os.path.join(OUT, "replay_ref_picks.parquet"), index=False)
    spk_s[["code", "date", "score"]].to_parquet(os.path.join(OUT, "replay_ref_picks_strategy.parquet"), index=False)
    snap_frame.groupby("date").filter_ok.first().rename("filter_ok").reset_index().to_parquet(os.path.join(OUT, "replay_ref_filter.parquet"), index=False)
    print(f"strategy picks {len(spk_s)} on {len(fdays)} filter days (last {fdays[-1].date() if fdays else None})", flush=True)
    print(f"ref saved: frame rows {int((snap_frame.date >= START).sum())}, picks {len(spk)}, chk {chk}, index note {len(mk_note)} "
          f"({time.time() - t0:.0f}s)", flush=True)


def _synth(args):
    code, days = args
    p = os.path.join(x.MIN_DIR, f"{code}.parquet")
    if not os.path.exists(p):
        return []
    mdf = pd.read_parquet(p)
    pa = os.path.join(sd.EXT_DIR, f"{code}.parquet")
    adj = pd.read_parquet(pa if os.path.exists(pa) else os.path.join(sd.MERGED_DIR, f"{code}.parquet"))
    adj["timestamp"] = pd.to_datetime(adj.timestamp)
    raw = pd.read_parquet(os.path.join(sd.RAW_DIR, f"{code}.parquet"))
    raw["timestamp"] = pd.to_datetime(raw.timestamp)
    out = []
    for day in days:
        D = pd.Timestamp(day)
        sv = x.snap_values(mdf, day)
        ia, ir = adj.index[adj.timestamp == D], raw.index[raw.timestamp == D]
        if sv is None or len(ia) != 1 or len(ir) != 1:
            continue
        f = float(raw.loc[ir[0], "factor"])
        rv = float(raw.loc[ir[0], "volume"])
        vs = float(adj.loc[ia[0], "volume"]) / rv if rv > 0 else 1.0
        out.append((D, code, {"open": sv["open"], "high": sv["high"], "low": sv["low"], "close": sv["close"], "volume": sv["volume"],
                              "amount": min(sv["amount"], float(adj.loc[ia[0], "amount"]))}, f, vs))
    return out


def index_1519(days):
    k = pd.read_parquet(sd.KOSPI).set_index("timestamp").close
    q = pd.read_parquet(E.s4.KOSDAQ).set_index("timestamp").close
    out, note = {}, []
    for name, s in (("kospi", k), ("kosdaq", q)):
        im = pd.read_parquet(os.path.join(x.OUT, f"index_{name}.parquet"))
        for D in days:
            d = im[(im.ts.str[:8] == D.strftime("%Y%m%d")) & (im.ts.str[8:12] <= x.SNAP)]
            val = float(d.close.iloc[-1]) if len(d) else np.nan
            if np.isnan(val):
                note.append(f"{name} {D.date()}")
                val = float(s[D])
            out.setdefault(D, {})[name] = val
    return out, note


def run(mode="memory"):
    frozen = mode == "frozen"
    sfx = "_frozen" if frozen else ""
    from daily import index_series
    t0 = time.time()
    days = calendar(START, END)
    ds_days = {d.strftime("%Y%m%d") for d in days}
    cand = {}
    for fn in ("candidates_ext.json", "candidates.json"):
        for k, v_ in json.load(open(os.path.join(x.OUT, fn))).items():
            cand.setdefault(k, set()).update(d for d in v_ if d in ds_days)
    tasks = [(k, sorted(v_)) for k, v_ in cand.items() if v_]
    with ProcessPoolExecutor(max_workers=14) as ex:
        synth = [r for part in ex.map(_synth, tasks, chunksize=4) for r in part]
    rows_by_day = {}
    for D, code, sv, f, vs in synth:
        rows_by_day.setdefault(D, {})[code] = (sv, f, vs)
    idx_vals, idx_note = index_1519(days)
    t_synth = time.time() - t0
    print(f"synth snapshots: {len(synth):,} rows, {len(rows_by_day)} days ({t_synth:.0f}s), index fallback {len(idx_note)}", flush=True)

    ref_frame = pd.read_parquet(os.path.join(OUT, "replay_ref_frame.parquet"))
    ref_picks = pd.read_parquet(os.path.join(OUT, "replay_ref_picks.parquet"))
    ledger = [{"code": r.code, "entry": str(r.date.date())} for r in ref_picks[ref_picks.date < START].itertuples()]
    index = index_series()
    index = {k: s[s.index <= END] for k, s in index.items()}
    pool = E.ShardPool(sd.universe_codes(), n=12, use_append=False, frozen=frozen)
    print(f"pool loaded {pool.loaded} stocks ({pool.load_seconds:.0f}s)", flush=True)
    models = {}
    daily, feat_bad, worst = [], [], {}
    try:
        for D in days:
            mo = D.strftime("%Y-%m")
            if mo not in models:
                b, cols, meta, chk = M.load(mo)
                assert chk["ok"], chk
                models[mo] = (b, cols)
            b, cols = models[mo]
            res = E.run_day(pool, D, rows_by_day.get(D, {}), index, idx_vals[D], b, cols, ledger, frozen_market=frozen)
            live3 = list(res["top3"].code)
            refp = ref_picks[ref_picks.date == D].sort_values("score", ascending=False)
            ref3 = list(refp.code)
            e = res["eligible"].set_index("code")
            rf = ref_frame[ref_frame.date == D].set_index("code")
            el_same = set(e.index) == set(rf.index)
            common = sorted(set(e.index) & set(rf.index))
            dmax = 0.0
            for c in cols + ["score"]:
                err = v.rel_err(e.loc[common, c].to_numpy(np.float64), rf.loc[common, c].to_numpy(np.float64)) if common else np.array([0.0])
                mx = float(np.max(err))
                worst[c] = max(worst.get(c, 0.0), mx)
                dmax = max(dmax, mx if c != "score" else 0.0)
                if mx > REL_TOL:
                    i = int(np.argmax(err))
                    feat_bad.append((str(D.date()), c, common[i], float(e.loc[common[i], c]), float(rf.loc[common[i], c]), mx))
            daily.append({"date": str(D.date()), "overlap": len(set(live3) & set(ref3)), "live_top3": " ".join(live3), "ref_top3": " ".join(ref3),
                          "same_order": live3 == ref3, "eligible_live": len(e), "eligible_ref": len(rf), "eligible_same": el_same,
                          "feat_max_rel": dmax, "score_max_rel": float(np.max(v.rel_err(e.loc[common, "score"].to_numpy(), rf.loc[common, "score"].to_numpy()))) if common else 0.0,
                          "busy_skipped": " ".join(res["skipped_busy"]), "filter_ok": res["filter"]["filter_ok"], "rows": res["n_rows"],
                          "errors": len(res["errors"]), "sec_compute": res["seconds_compute"], "sec_total": res["seconds_total"]})
            ledger = [{"code": s_["code"], "entry": s_["entry"]} for s_ in res["ledger_status"] if s_["open"]] + \
                     [{"code": c, "entry": str(D.date())} for c in live3]
            print(f"  {D.date()} overlap {daily[-1]['overlap']}/3 elig {len(e)}/{len(rf)} feat {dmax:.1e} ({res['seconds_total']:.1f}s)", flush=True)
    finally:
        pool.close()
    dd = pd.DataFrame(daily)
    dd.to_csv(os.path.join(OUT, f"replay_daily{sfx}.csv"), index=False, encoding="utf-8-sig")
    allok = bool((dd.overlap == 3).all() and dd.eligible_same.all() and not feat_bad)
    L = [f"# 시험 A: 15:19 재생 (실행 엔진 vs extend_judge 계산, {'동결 load_stock·market_features 직접 호출 (장 마감 후 대조 경로)' if frozen else '메모리 사본 (실시간 경로)'}, 해석 없음)\n",
         f"기간 {days[0].date()} ~ {days[-1].date()} ({len(days)}거래일). 재생 스냅샷 {len(synth):,}행 (extend_judge 의 15:19 행과 같은 값, 후보 종목). "
         f"지수 15:19 업종 분봉 (없는 날 종가로 대신 {len(idx_note)}건). 장부 시작: extend_judge 장부의 {START.date()} 전 상위 3 {len([r for r in ref_picks.itertuples() if r.date < START])}건. "
         f"모델: models_live/stage5/2026-07 ~ 2026-10 (로드 확인 통과).\n",
         f"**결과: {'모든 날 상위 3 이 3/3 같고 특징값이 기준 안' if allok else '다른 날 또는 기준 넘은 특징 있음'}**\n",
         "| 겹친 종목 수 | 0 | 1 | 2 | 3 |", "|---|---|---|---|---|",
         "| 일수 | " + " | ".join(str(int((dd.overlap == k).sum())) for k in range(4)) + " |\n",
         f"- 순서까지 같은 날: {int(dd.same_order.sum())} / {len(dd)}",
         f"- 대상 집합이 같은 날: {int(dd.eligible_same.sum())} / {len(dd)}",
         f"- 특징 41개 최대 상대 오차 (기준 {REL_TOL:.0e}): {max(v_ for k, v_ in worst.items() if k != 'score'):.1e}, 점수 최대 상대 오차 {worst.get('score', 0):.1e}",
         f"- 기준 넘은 특징: {len(feat_bad)}건" + (f" (처음 10건: {feat_bad[:10]})" if feat_bad else ""),
         f"- 계산 오류 종목: {int(dd.errors.sum())}",
         f"- 하루 계산 시간 (작업 프로세스 12개, 일봉 메모리 적재 뒤): 평균 {dd.sec_total.mean():.1f}초, 최대 {dd.sec_total.max():.1f}초 "
         f"(그중 종목 계산 평균 {dd.sec_compute.mean():.1f}초), 일봉 메모리 적재 {pool.load_seconds:.0f}초\n"]
    bad = dd[(dd.overlap < 3) | ~dd.eligible_same]
    if len(bad):
        L += ["다른 날:\n", "| 날짜 | 겹침 | 실행 엔진 상위 3 | extend_judge 상위 3 | 대상 수 (엔진/기준) | 장부로 건너뜀 |", "|---|---|---|---|---|---|"]
        L += [f"| {r.date} | {r.overlap} | {r.live_top3} | {r.ref_top3} | {r.eligible_live}/{r.eligible_ref} | {r.busy_skipped} |" for r in bad.itertuples()]
    L.append("\n| 특징 | 최대 상대 오차 |\n|---|---|")
    L += [f"| {k} | {v_:.1e} |" for k, v_ in worst.items()]
    L.append(f"\n({time.time() - t0:.0f}s)")
    with open(os.path.join(OUT, f"replay_test{sfx}.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("\n".join(L))


def check1016():
    """8장 12 (1) 보완 규칙으로 2026-10-16 다시 돌리기: 장부를 새로 계산하지 않고 실행 엔진과 같은 장부 상태(그날 기준 열린 거래는 열린 그대로)를
    (a) extend_judge 기준 점수(동결 계산)와 (b) 동결 load_stock·market_features 를 그대로 호출한 계산에 넣어 stage4_model.pick_nodup 으로 고른다."""
    from daily import index_series
    D = pd.Timestamp("2026-10-16")
    day = D.strftime("%Y%m%d")
    cand = {}
    for fn in ("candidates_ext.json", "candidates.json"):
        for k, v_ in json.load(open(os.path.join(x.OUT, fn))).items():
            if day in v_:
                cand[k] = [day]
    with ProcessPoolExecutor(max_workers=14) as ex:
        synth = [r for part in ex.map(_synth, list(cand.items()), chunksize=4) for r in part]
    rows = {code: (sv, f, vs) for _, code, sv, f, vs in synth}
    idx_vals, _ = index_1519([D])
    ref_frame = pd.read_parquet(os.path.join(OUT, "replay_ref_frame.parquet"))
    ref_picks = pd.read_parquet(os.path.join(OUT, "replay_ref_picks.parquet"))
    ledger = [{"code": r.code, "entry": str(r.date.date())} for r in ref_picks[ref_picks.date < D].itertuples()]
    index = {k: s[s.index <= END] for k, s in index_series().items()}
    b, cols, _, _ = M.load("2026-10")
    out = {}
    for frozen in (False, True):
        pool = E.ShardPool(sd.universe_codes(), n=12, use_append=False, frozen=frozen)
        try:
            out[frozen] = E.run_day(pool, D, rows, index, idx_vals[D], b, cols, ledger, frozen_market=frozen)
        finally:
            pool.close()
    eng = out[False]
    status = eng["ledger_status"]
    rf = ref_frame[ref_frame.date == D].sort_values("code").reset_index(drop=True)
    picks, _ = E.pick_top(rf, status, D)
    ref_with_state = list(rf.iloc[picks].sort_values("score", ascending=False).code)
    open_n = sum(s_["open"] for s_ in status)
    res = {"engine_top3": list(eng["top3"].code), "frozen_rerun_top3_same_ledger": list(out[True]["top3"].code),
           "extend_scores_with_engine_ledger_top3": ref_with_state, "extend_judge_original_top3": list(ref_picks[ref_picks.date == D].sort_values("score", ascending=False).code),
           "open_trades_on_1016": open_n, "open_codes": sorted({s_["code"] for s_ in status if s_["open"]})}
    res["engine_vs_frozen_3of3"] = res["engine_top3"] == res["frozen_rerun_top3_same_ledger"]
    res["engine_vs_extend_scores_same_ledger_3of3"] = res["engine_top3"] == res["extend_scores_with_engine_ledger_top3"]
    json.dump(res, open(os.path.join(OUT, "check1016.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1] == "check1016":
        check1016()
        sys.exit(0)
    {"ref": ref, "run": run}[sys.argv[1]](*sys.argv[2:])
