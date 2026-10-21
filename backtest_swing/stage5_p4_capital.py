"""
5단계 P4 시작 금액별 참고 계산 (1,000만 · 3,000만 · 5,000만 원). P4 규칙·1/33·코드·명세·기존 결과는 그대로.

  python backtest_swing/stage5_p4_capital.py   -> results/stage5_p4/capital/capital.md

거래별 목표 금액·실제 매수 금액과 코스피 + 120일선 필터 대조군의 일별 경로는 기존 함수가 돌려주지 않으므로,
stage5_p4.port_p4 와 stage4_model.benchmarks 원문 사본에 기록 줄만 넣어 계산하고 결과가 원본과 같은지 확인한다.
"""
import inspect
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage4_model as sm  # noqa: E402
import stage5_model as m  # noqa: E402
import stage5_p4 as p4  # noqa: E402
import stage5_validation as v  # noqa: E402

OUT = "backtest_swing/results/stage5_p4/capital"
CAPITALS = [10_000_000, 30_000_000, 50_000_000]
SUB_END = pd.Timestamp("2026-08-14")


def make_p4_x():
    src = inspect.getsource(p4.port_p4)
    reps = [("def port_p4(g, ds, sim, picks, first, last, capital=CAPITAL, frac=FRAC):",
             "def port_p4_x(g, ds, sim, picks, first, last, capital=CAPITAL, frac=FRAC, _rec=None):"),
            ("                if sh <= 0:\n                    why[t] = \"size0\"\n",
             "                if sh <= 0:\n                    why[t] = \"size0\"\n                    _rec['size0'].append((t, cost[t], amt))\n"),
            ("                cash -= sh * cost[t]\n",
             "                cash -= sh * cost[t]\n                _rec['buy'].append((t, amt, sh * cost[t]))\n")]
    for a, b in reps:
        assert src.count(a) == 1, a
        src = src.replace(a, b)
    ns = dict(vars(p4))
    exec(src, ns)
    return ns["port_p4_x"]


def make_bench_x():
    src = inspect.getsource(sm.benchmarks)
    a = "        prev = day\n"
    assert src.count(a) == 1
    src = src.replace("def benchmarks(g):", "def benchmarks_x(g, _path):").replace(a, "        _path.append((day, eq))\n" + a)
    ns = dict(vars(sm))
    exec(src, ns)
    return ns["benchmarks_x"]


def filter_mdd(g, first, last):
    saved = sm.FIRST_PRED, sm.LAST_PRED
    sm.FIRST_PRED, sm.LAST_PRED = first, last
    path = []
    bx = make_bench_x()(g, path)
    b0 = sm.benchmarks(g)
    sm.FIRST_PRED, sm.LAST_PRED = saved
    key = "코스피 + 120일선 필터 (ETF 비용)"
    assert abs(bx[key] - b0[key]) < 1e-9
    e = pd.Series([x for _, x in path], index=[d for d, _ in path])
    return b0[key], ((e / e.cummax()) - 1).min() * 100


def period(label, folder, first, last, pred, ref_R):
    v.use(folder, first, last, pred)
    ds, g, sim, cut = m.prepare()
    fi = np.flatnonzero(ds.filter_ok.to_numpy())
    day_rows = [fi[ds.date.to_numpy()[fi] == x] for x in np.sort(ds.date[ds.filter_ok].unique())]
    dummy = pd.Series(0.0, index=pd.DatetimeIndex(ds.date.to_numpy()[[r[0] for r in day_rows]]))
    picks, t3, _ = m.signal_eval(ds, ds.score.to_numpy(), day_rows, dummy)
    px = make_p4_x()
    raw_close = g.rc[ds.g.to_numpy()]
    rows = []
    for cap in CAPITALS:
        rec = {"buy": [], "size0": []}
        r = px(g, ds, sim, picks, first, last, capital=cap, _rec=rec)
        r0 = p4.port_p4(g, ds, sim, picks, first, last, capital=cap)
        assert abs(r["e"].iloc[-1] - r0["e"].iloc[-1]) < 1e-6 and r["why"] == r0["why"]
        w = pd.Series({t: r["why"].get(t, "none") for t in picks})
        R = ds.loc[picks, "R_real"]
        buy = pd.DataFrame(rec["buy"], columns=["t", "target", "actual"])
        ratio = buy.actual / buy.target
        s0 = pd.DataFrame(rec["size0"], columns=["t", "cost", "target"])
        s0_raw = raw_close[s0.t.to_numpy()] if len(s0) else np.array([])
        e = r["e"]
        row = {"금액": f"{cap // 10_000:,}만 원", "상위 3": len(picks),
               "산 건수": f"{int((w == 'bought').sum())} ({(w == 'bought').mean() * 100:.1f}%)",
               "현금 부족 건너뜀": f"{int((w == 'cash').sum())} ({(w == 'cash').mean() * 100:.1f}%)",
               "수량 0주 건너뜀": f"{int((w == 'size0').sum())} ({(w == 'size0').mean() * 100:.1f}%)",
               "수량 0주 종목 원주가 종가 (최소/중앙값/최대)":
                   f"{s0_raw.min():,.0f} / {np.median(s0_raw):,.0f} / {s0_raw.max():,.0f}" if len(s0_raw) else "-",
               "실제/목표 매수 금액 (중앙값 / 10% 지점 / 70% 미만 거래 비율)":
                   f"{ratio.median() * 100:.1f}% / {ratio.quantile(.10) * 100:.1f}% / {(ratio < 0.7).mean() * 100:.1f}%",
               "산 신호 평균 R (신호 단위와 차이)": f"{R[w == 'bought'].mean():+.3f} ({R[w == 'bought'].mean() - ref_R:+.3f})",
               "누적": f"{(e.iloc[-1] / cap - 1) * 100:+.1f}%", "MDD": f"{((e / e.cummax()) - 1).min() * 100:.1f}%"}
        if label == "검증":
            es = e[e.index <= SUB_END]
            row["08-14 까지 누적 / MDD"] = f"{(es.iloc[-1] / cap - 1) * 100:+.1f}% / {((es / es.cummax()) - 1).min() * 100:.1f}%"
        rows.append(row)
    sig_R = t3.R_real.mean()
    assert abs(sig_R - ref_R) < 0.0005
    L = [f"## {label} (진입 < {cut['last_entry'].date()}, 상위 3 신호 {len(picks)}건, 신호 단위 평균 R {sig_R:+.3f})\n"]
    cols = list(rows[0].keys())
    L.append("| " + " | ".join(cols) + " |\n|" + "---|" * len(cols))
    for row in rows:
        L.append("| " + " | ".join(str(row[c]) for c in cols) + " |")
    tot, mdd = filter_mdd(g, first, last)
    L.append(f"\n코스피 + 120일선 필터 (ETF 비용) 대조군: 누적 {tot:+.2f}%, MDD {mdd:.1f}% ({first.date()} ~ {last.date()})")
    if label == "검증":
        tot_s, mdd_s = filter_mdd(g, first, SUB_END)
        L.append(f"코스피 + 120일선 필터 (ETF 비용) 대조군, {SUB_END.date()} 까지: 누적 {tot_s:+.2f}%, MDD {mdd_s:.1f}%")
    pd.DataFrame(rows).to_csv(os.path.join(OUT, f"capital_{label}.csv"), index=False, encoding="utf-8-sig")
    return L + [""]


def main():
    os.makedirs(OUT, exist_ok=True)
    L = ["# 5단계 P4 시작 금액별 참고 계산 (해석 없음)\n",
         "P4 규칙·1/33 은 그대로, 시작 금액만 1,000만·3,000만·5,000만 원. port_p4 사본에 기록 줄만 넣어 거래별 목표 금액(매수 시점 총자산 / 33)과 "
         "실제 매수 금액(수량 x 1주 매수 비용)을 기록했고, 사본의 결과(최종 자산·신호별 처리)가 원본과 같은지 확인했다. 비율은 상위 3 신호 수 대비. "
         "수량 0주 판정은 수정주가 기준 1주 매수 비용으로 하고, 표의 매수가는 그 신호일의 원주가 종가다. 코스피 + 120일선 필터 MDD 는 "
         "stage4_model.benchmarks 사본에 일별 값 기록만 넣어 계산했고 누적이 원본과 같은지 확인했다.\n"]
    L += period("개발", v.DEV_DIR, v.DEV_FIRST, v.DEV_LAST, os.path.join(v.DEV_DIR, "predictions.parquet"), 0.270)
    L += period("검증", v.VAL_DIR, v.VAL_START, v.VAL_END, os.path.join(v.VAL_DIR, "predictions.parquet"), 0.328)
    with open(os.path.join(OUT, "capital.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
