"""
EXEC_SPEC.md 3장 측정 보조: 시장 필터와 무관하게 모든 날 상위 3 비교 (측정용) + 확장 구간 판정에 쓰는 공통 함수

  python backtest_swing/exec_stage3_measure_all.py   -> results/exec_prep/stage3_measure/measure_all_days.md

이미 받은 분봉으로 만든 15:19 행(snap_rows.parquet)을 그대로 쓴다. 새 수집 없음.
"""
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exec_stage3_measure as x  # noqa: E402
from exec_stage3_measure import OUT, END, sb, sd, s4, s5, sm, m, v  # noqa: E402,F401


def build_frames(days, snap, months_range, index_dir=OUT):
    """종가 기준·15:19 기준 평가 표 (필터와 무관하게 모든 날). 반환: close_frame, snap_frame, 저장 예측 대조, 지수 메모, prev_below"""
    ds = pd.read_parquet(os.path.join(v.VAL_DIR, "dataset.parquet"))
    v.use(v.VAL_DIR, months_range[0], months_range[1], os.path.join(v.VAL_DIR, "predictions.parquet"))
    models, cols = x.train_months(ds, sm.months(ds))
    chk = {}
    for name, pred_path in (("검증", os.path.join(v.VAL_DIR, "predictions.parquet")), ("개발", os.path.join(v.DEV_DIR, "predictions.parquet"))):
        pv = pd.read_parquet(pred_path).set_index("g")
        for mo, bs in models.items():
            per = pd.Period(mo, "M")
            sub = ds[(ds.date >= per.start_time) & (ds.date <= per.end_time)]
            old = pv.reindex(sub.g)[[f"main_s{s_}" for s_ in m.SEEDS_MAIN]].mean(axis=1).to_numpy()
            ok = ~np.isnan(old)
            if not ok.any():
                continue
            new = np.mean([b.predict(sub[cols].to_numpy(np.float32)) for b in bs], axis=0)
            chk[f"{mo} vs {name}"] = float(np.max(np.abs(new[ok] - old[ok])))
    g = s4.SimArrays(np.load(os.path.join(v.VAL_DIR, "sim.npz")))
    codes = list(np.load(os.path.join(v.VAL_DIR, "sim.npz"))["codes"])
    pos = {(int(a), int(b)): i for i, (a, b) in enumerate(zip(g.sid, g.cal))}
    code_sid = {c: i for i, c in enumerate(codes)}
    kk = pd.read_parquet(sd.KOSPI).sort_values("timestamp").set_index("timestamp").close
    prev_below = (kk < kk.rolling(120).mean()).shift(1)

    def prep(frame):
        frame = frame[frame.date.isin(days)].copy().reset_index(drop=True)
        frame["g"] = [pos.get((code_sid[c], g.cal_dates.get_loc(d))) for c, d in zip(frame.code, frame.date)]
        frame = frame.dropna(subset=["g"]).reset_index(drop=True)
        frame["g"] = frame.g.astype(int)
        T = frame.g.to_numpy()
        risk = s4.STOP_ATR * g.atr20[T]
        sim = sb.simulate(g, T, g.c[T] - risk, np.full(len(T), np.nan), "full", want_legs=True)
        frame["halt_stuck"] = (sim["reason"] == sb.R_END) & (g.vol[g.dev_last[T]] == 0)
        frame["net_real"] = np.where(frame.halt_stuck, -1.0, sim["net"])
        frame["reason_real"] = sim["reason"]
        frame["R_real"] = frame.net_real * sim["cost"] / risk
        frame["exit_cal"], frame["cal"] = g.cal[sim["j1"]], g.cal[T]
        frame["filter_ok"] = ~frame.date.map(prev_below.reindex(frame.date.unique()).fillna(False)).astype(bool)
        mo = frame.date.dt.to_period("M").astype(str)
        sc = np.full(len(frame), np.nan)
        for key, idx in frame.groupby(mo).indices.items():
            X = frame.iloc[idx][cols].to_numpy(np.float32)
            sc[idx] = np.mean([b.predict(X) for b in models[key]], axis=0)
        frame["score"] = sc
        return frame

    close_frame = prep(ds[ds.date.isin(days)])
    e = snap[snap.snap & snap.eligible].copy()
    feat_stock = [c for c in cols if c not in s5.XS_FEATS and not c.startswith(("kospi_", "kosdaq_"))]
    for c in feat_stock:
        e[c] = e[c].astype(np.float32)
    grp = e.groupby("date")
    e["rank_ret20"] = grp.ret_20.rank(pct=True).astype(np.float32)
    e["rank_ret60"] = grp.ret_60.rank(pct=True).astype(np.float32)
    e["rank_amt20"] = grp.log_amt20.rank(pct=True).astype(np.float32)
    e["breadth_ma20"] = grp.above_ma20.transform("mean").astype(np.float32)
    saved = x.OUT
    x.OUT = index_dir
    mk, mk_note = x.market_1519(days)
    x.OUT = saved
    e = e.merge(mk.drop(columns=["kospi_1519", "kospi_close"]).astype(np.float32), left_on="date", right_index=True, how="left")
    snap_frame = prep(e)
    return close_frame, snap_frame, chk, mk_note, prev_below


def day_rows(frame, day_list):
    dates = frame.date.to_numpy()
    rows = [np.flatnonzero(dates == np.datetime64(d)) for d in day_list]
    return [r for r in rows if len(r)]


def top3(frame, day_list):
    """day_list 의 날마다 점수 순 pick_nodup (장부는 day_list 기간 동안 이어짐)"""
    sc = frame.score.to_numpy()
    return sm.pick_nodup(frame, [r[np.argsort(-sc[r], kind="stable")] for r in day_rows(frame, day_list)], sm.TOP_N)


def compare(close_frame, snap_frame, cp, spk, day_list):
    ov, r_sc, r_cs = [], [], []
    for d in day_list:
        a, b = set(cp[cp.date == d].code), set(spk[spk.date == d].code)
        cf, sf = close_frame[close_frame.date == d], snap_frame[snap_frame.date == d]
        cpos = {c: i + 1 for i, c in enumerate(cf.sort_values("score", ascending=False).code)}
        spos = {c: i + 1 for i, c in enumerate(sf.sort_values("score", ascending=False).code)}
        r_sc += [cpos.get(c, 0) for c in b - a]
        r_cs += [spos.get(c, 0) for c in a - b]
        ec, es = set(cf.code), set(sf.code)
        ov.append({"date": d, "filter_ok": bool(cf.filter_ok.iloc[0]) if len(cf) else None, "overlap": len(a & b), "n_close": len(a),
                   "n_snap": len(b), "elig_close": len(ec), "elig_snap": len(es), "new_in_snap": len(es - ec), "dropped_in_snap": len(ec - es)})
    return pd.DataFrame(ov), r_sc, r_cs


def rank_bins(r):
    r = np.array(r)
    bins = {"대상 아님": int((r == 0).sum()), "1~3": int(((r >= 1) & (r <= 3)).sum()), "4~5": int(((r >= 4) & (r <= 5)).sum()),
            "6~10": int(((r >= 6) & (r <= 10)).sum()), "11~20": int(((r >= 11) & (r <= 20)).sum()), "21 이상": int((r >= 21).sum())}
    return ", ".join(f"{k}: {v_}" for k, v_ in bins.items())


def closed_stats(t):
    c = t[t.reason_real != sb.R_END]
    return f"{len(t)} | {len(c)} | {c.R_real.mean():+.3f} | {c.net_real.mean() * 100:+.2f}%"


def overlap_table(ov):
    fo, nf = ov[ov.filter_ok == True], ov[ov.filter_ok != True]  # noqa: E712
    L = ["| | 일수 | 0 | 1 | 2 | 3 | 평균 겹침 |", "|---|---|---|---|---|---|---|"]
    for name, t in (("전체", ov), ("필터 통과일", fo), ("필터 미통과일", nf)):
        L.append(f"| {name} | {len(t)} | " + " | ".join(str(int((t.overlap == k_).sum())) for k_ in range(4)) +
                 f" | {t.overlap.mean() if len(t) else float('nan'):.2f} |")
    return L


def main():
    t0 = time.time()
    days = x.window()
    snap = pd.read_parquet(os.path.join(OUT, "snap_rows.parquet"))
    close_frame, snap_frame, chk, mk_note, prev_below = build_frames(days, snap, (pd.Timestamp("2026-07-01"), pd.Timestamp("2026-10-31")))
    dl = list(days)
    cp, spk = close_frame.loc[top3(close_frame, dl)], snap_frame.loc[top3(snap_frame, dl)]
    ov, r_sc, r_cs = compare(close_frame, snap_frame, cp, spk, dl)
    ov.to_csv(os.path.join(OUT, "daily_overlap_all_days.csv"), index=False, encoding="utf-8-sig")
    fdays = set(ov[ov.filter_ok == True].date)  # noqa: E712
    cal = pd.DatetimeIndex(pd.read_parquet(sd.KOSPI, columns=["timestamp"]).timestamp).sort_values()
    pb = prev_below.reindex(cal).fillna(False).astype(bool)
    last_pass = cal[(~pb).to_numpy() & (cal <= END)].max()
    kk = pd.read_parquet(sd.KOSPI).sort_values("timestamp").set_index("timestamp").close
    ma = kk.rolling(120).mean()
    d15 = pd.Timestamp("2026-10-15")
    L = ["# EXEC_SPEC 3장: 15:19 vs 종가, 시장 필터와 무관하게 60거래일 전체 (측정용, 해석 없음)\n",
         f"기간 {days[0].date()} ~ {days[-1].date()} ({len(days)}거래일). 상위 3 은 시장 필터와 무관하게 매일 계산 (pick_nodup 장부는 60일 동안 이어짐). "
         f"필터 통과 여부는 열(daily_overlap_all_days.csv 의 filter_ok)로만 남긴다. 15:19 행·모델·지수는 measure.md 와 같다 "
         f"(모델 예측과 저장 예측의 최대 차이 {chk}, 지수 {'업종 분봉 15:19' if not mk_note else '일부 종가 대신 ' + str(len(mk_note)) + '건'}).\n",
         "## 상위 3 겹침\n"] + overlap_table(ov) + [
         "\n## 서로 다른 종목의 순위 (그날 대상 안 점수 순위, 0 = 그쪽 대상 아님)\n",
         f"- 15:19 상위 3 에만 있는 종목 ({len(r_sc)}건)의 종가 기준 순위: {rank_bins(r_sc)}",
         f"- 종가 상위 3 에만 있는 종목 ({len(r_cs)}건)의 15:19 기준 순위: {rank_bins(r_cs)}",
         "\n## 대상 집합\n",
         f"- 하루 평균 대상 수: 15:19 {ov.elig_snap.mean():.1f}, 종가 {ov.elig_close.mean():.1f} | 15:19 에만 대상 하루 평균 {ov.new_in_snap.mean():.1f} "
         f"(최대 {ov.new_in_snap.max()}), 종가에만 대상 하루 평균 {ov.dropped_in_snap.mean():.1f} (최대 {ov.dropped_in_snap.max()})",
         "\n## 상위 3 성과 (실제 규칙, 종가 진입, 2026-10-16 까지 청산된 거래만, 정지 묶임 -100%)\n",
         "| | 구간 | 상위 3 신호 | 청산된 거래 | 평균 R | 평균 % 순수익률 |", "|---|---|---|---|---|---|",
         f"| 종가 기준 | 전체 60일 | {closed_stats(cp)} |", f"| 15:19 기준 | 전체 60일 | {closed_stats(spk)} |",
         f"| 종가 기준 | 필터 통과일 | {closed_stats(cp[cp.date.isin(fdays)])} |", f"| 15:19 기준 | 필터 통과일 | {closed_stats(spk[spk.date.isin(fdays)])} |",
         "\n## 시장 필터 상태\n",
         f"- 2026-10-16 진입 판단 (전날 2026-10-15 코스피 종가 {kk[d15]:,.2f}, 120일선 {ma[d15]:,.2f}): {'통과' if not pb[END] else '미통과'}",
         f"- 2026-10-16 코스피 종가 {kk[END]:,.2f}, 120일선 {ma[END]:,.2f} (다음 거래일 진입 판단: {'통과' if kk[END] >= ma[END] else '미통과'})",
         f"- 마지막 필터 통과일 (2026-10-16 까지): {last_pass.date()}",
         f"\n({time.time() - t0:.0f}s)"]
    with open(os.path.join(OUT, "measure_all_days.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
