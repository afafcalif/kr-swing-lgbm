"""
5단계 P4 모의투자 포트폴리오 규칙 — 참고 계산 (명세: STAGE5_SPEC.md "P4 모의투자 포트폴리오 규칙")

  python backtest_swing/stage5_p4.py    -> results/stage5_p4/p4.md (+ csv)

P4: 시장 필터 통과일에 신호 단위 판정과 같은 상위 3(pick_nodup 결과 그대로)을 S일 종가에 전부 매수 시도.
종목당 매수 금액 = 매수 시점 총자산 / 33, 수량 내림. 같은 종목 보유 중이거나 현금이 매수 금액보다 적으면 건너뜀(대체 없음).
청산·비용·현금 금리·정지 묶임 처리는 실제 규칙과 같다 (stage5_model.prepare 의 sim 을 그대로 쓴다).
결과·판정·기존 코드는 고치지 않는다.
"""
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage3_backtest as sb  # noqa: E402
import stage4_features as s4  # noqa: E402
import stage4_model as sm  # noqa: E402
import stage5_model as m  # noqa: E402
import stage5_validation as v  # noqa: E402
import stage5_validation_diag as vd  # noqa: E402

OUT = "backtest_swing/results/stage5_p4"
FRAC = 1 / 33
CAPITAL = 30_000_000
KIND = 3
SUB_END = pd.Timestamp("2026-08-14")
WHY = {"bought": "산 신호", "held": "같은 종목 보유 중", "cash": "현금 부족 (현금 < 매수 금액)", "size0": "수량 0주"}


def port_p4(g, ds, sim, picks, first, last, capital=CAPITAL, frac=FRAC):
    """picks: 신호 단위 상위 3 (ds 행 번호). 하루 흐름은 port_run 과 같고, 매수 대상만 picks 로 고정."""
    p1, cost = sim["p1"], ds.cost_ps.to_numpy()
    code, gg, exit_cal = ds.code.to_numpy(), ds.g.to_numpy(), ds.exit_cal.to_numpy()
    pk = ds.loc[picks, ["date"]].assign(t=picks)
    by_day = {d: grp.t.to_numpy() for d, grp in pk.groupby("date", sort=False)}  # picks 순서(그날 점수 순) 유지
    days = g.cal_dates[(g.cal_dates >= first) & (g.cal_dates <= last)]
    cash, pos, eq, mvs, npos, prev = float(capital), {}, [], [], [], None
    why = {}
    for day in days:
        if prev is not None:
            cash *= (1 + sb.CASH_RATE[day.year]) ** ((day - prev).days / 365)
        ci = g.cal_dates.get_loc(day)
        for t in list(pos):
            if exit_cal[t] == ci:
                cash += pos[t][0] * p1[t]
                del pos[t]
        mv = sum(sh * sm._close_on(g, T0, ci) for sh, _, T0 in pos.values())
        cand = by_day.get(day)
        if cand is not None:
            held = {x[1] for x in pos.values()}
            equity = cash + mv
            amt = equity * frac
            for t in cand:
                if code[t] in held:
                    why[t] = "held"
                    continue
                if cash < amt:
                    why[t] = "cash"
                    continue
                sh = int(amt // cost[t])
                if sh <= 0:
                    why[t] = "size0"
                    continue
                cash -= sh * cost[t]
                pos[t] = (sh, code[t], int(gg[t]))
                held.add(code[t])
                mv += sh * g.c[int(gg[t])]
                why[t] = "bought"
        eq.append(cash + mv)
        mvs.append(mv)
        npos.append(len(pos))
        prev = day
    e = pd.Series(eq, index=days)
    return {"e": e, "expo": pd.Series(mvs, index=days) / e, "npos": pd.Series(npos, index=days), "why": why,
            "left_open": len(pos)}


def stats(res, cut=None):
    e = res["e"] if cut is None else res["e"][res["e"].index <= cut]
    x = res["expo"].reindex(e.index)
    n = res["npos"].reindex(e.index)
    yrs = (e.index[-1] - e.index[0]).days / 365
    return {"total": (e.iloc[-1] / CAPITAL - 1) * 100, "cagr": ((e.iloc[-1] / CAPITAL) ** (1 / yrs) - 1) * 100,
            "mdd": ((e / e.cummax()) - 1).min() * 100, "expo_mean": x.mean() * 100, "expo_med": x.median() * 100,
            "expo_max": x.max() * 100, "expo_zero": int((x == 0).sum()), "npos_mean": n.mean(), "npos_max": int(n.max()),
            "days": len(e)}


def period_returns(e, freq):
    ends = e.groupby(e.index.to_period(freq)).last()
    base, out = CAPITAL, {}
    for p, val in ends.items():
        out[str(p)] = (val / base - 1) * 100
        base = val
    return out


# ---------------------------------------------------------------- 무작위 (작업 프로세스)

_W = {}


def _init(folder, first, last, d, sim):
    v.use(folder, first, last)
    _W["g"] = s4.SimArrays(np.load(m.SIM))
    _W["d"], _W["sim"], _W["first"], _W["last"] = d, sim, first, last
    fi = np.flatnonzero(d.filter_ok.to_numpy())
    _W["day_rows"] = [fi[d.date.to_numpy()[fi] == x] for x in np.sort(d.date[d.filter_ok].unique())]


def _rand(rep):
    rng = np.random.default_rng([m.PORT_SEED, KIND, rep])
    d = _W["d"]
    sc = rng.random(len(d))
    picks = sm.pick_nodup(d, [r[np.argsort(-sc[r], kind="stable")] for r in _W["day_rows"]], sm.TOP_N)
    res = port_p4(_W["g"], d, _W["sim"], picks, _W["first"], _W["last"])
    e = res["e"]
    sub = (e[e.index <= SUB_END].iloc[-1] / CAPITAL - 1) * 100 if e.index.max() > SUB_END else np.nan
    w = pd.Series([res["why"].get(t, "none") for t in picks])
    return {"rep": rep, "total": (e.iloc[-1] / CAPITAL - 1) * 100, "sub": sub, "n_bought": int((w == "bought").sum()),
            "n_picks": len(picks)}


def run_period(label, folder, first, last, pred, signal_ref, cut=None):
    v.use(folder, first, last, pred)
    ds, g, sim, cutinfo = m.prepare()
    fi = np.flatnonzero(ds.filter_ok.to_numpy())
    day_rows = [fi[ds.date.to_numpy()[fi] == x] for x in np.sort(ds.date[ds.filter_ok].unique())]
    dummy = pd.Series(0.0, index=pd.DatetimeIndex(ds.date.to_numpy()[[r[0] for r in day_rows]]))
    picks, t3, _ = m.signal_eval(ds, ds.score.to_numpy(), day_rows, dummy)
    assert abs(t3.R_real.mean() - signal_ref[0]) < 0.0005, (t3.R_real.mean(), signal_ref)
    res = port_p4(g, ds, sim, picks, first, last)
    # 기존 P3 (port_run 사본, 누적이 원본과 같은지 확인)
    px = vd.make_port_x()
    r3, rec3 = vd.run_x(px, g, ds, sim, vd.P3)
    assert abs(r3["total"] - m.port_run(g, ds, sim, **m.PORTS[vd.P3])["total"]) < 1e-9
    bench = sm.benchmarks(g)
    sub_bench = None
    if cut is not None:
        saved = sm.FIRST_PRED, sm.LAST_PRED
        sm.FIRST_PRED, sm.LAST_PRED = first, cut
        sub_bench = sm.benchmarks(g)
        sm.FIRST_PRED, sm.LAST_PRED = saved
    dslim = ds[["date", "code", "g", "cal", "filter_ok", "cost_ps", "exit_cal"]]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=min(14, os.cpu_count() or 4), initializer=_init,
                             initargs=(folder, first, last, dslim, {"p1": sim["p1"]})) as ex:
        rnd = pd.DataFrame(list(ex.map(_rand, range(m.PORT_REPS), chunksize=4)))
    print(f"  {label}: random x{m.PORT_REPS} ({time.time() - t0:.0f}s)", flush=True)
    rnd.to_csv(os.path.join(OUT, f"random_{label}.csv"), index=False, encoding="utf-8-sig")

    w = pd.Series({t: res["why"].get(t, "none") for t in picks})
    R, net = ds.loc[picks, "R_real"], ds.loc[picks, "net_real"]
    s = stats(res)
    L = [f"## {label} ({res['e'].index[0].date()} ~ {res['e'].index[-1].date()}, 진입 < {cutinfo['last_entry'].date()})\n",
         f"- P4 누적 {s['total']:+.1f}%" + (f", CAGR {s['cagr']:+.1f}%" if label == "개발" else "") + f", MDD {s['mdd']:.1f}%",
         f"- 무작위 200회 누적: 중앙값 {rnd.total.median():+.1f}% [5~95% {rnd.total.quantile(.05):+.1f}, {rnd.total.quantile(.95):+.1f}], "
         f"P4 백분위 {(rnd.total < s['total']).mean() * 100:.1f} (무작위 산 신호 평균 {rnd.n_bought.mean():.1f}건 / 상위 3 {rnd.n_picks.mean():.1f}건)",
         f"- 대조군: " + ", ".join(f"{k} {x:+.2f}%" for k, x in bench.items()) + f", 기존 P3 {r3['total']:+.1f}%",
         f"- 일별 투자 비중: 평균 {s['expo_mean']:.1f}%, 중앙값 {s['expo_med']:.1f}%, 최대 {s['expo_max']:.1f}%, 0% 인 날 {s['expo_zero']} / {s['days']}일",
         f"- 동시 보유 종목 수: 평균 {s['npos_mean']:.1f}, 최대 {s['npos_max']} (구간 끝에 남은 보유 {res['left_open']})"]
    pr = period_returns(res["e"], "Y" if label == "개발" else "M")
    L.append(f"- {'연도별' if label == '개발' else '월별'} 수익률 (%): " + ", ".join(f"{k}: {x:+.1f}" for k, x in pr.items()))
    if label != "개발":
        k = pd.read_parquet(v.sd.KOSPI).sort_values("timestamp").set_index("timestamp").close.reindex(res["e"].index)
        ke = k.groupby(k.index.to_period("M")).last()
        kb, kr = k.iloc[0], {}
        for p, val in ke.items():
            kr[str(p)] = (val / kb - 1) * 100
            kb = val
        L.append("- 코스피 월별 수익률 (%, 2026-01 은 01-02 종가 대비): " + ", ".join(f"{a}: {b:+.1f}" for a, b in kr.items()))
    L.append("\n| 상위 3 신호 | 건수 | 평균 R | 평균 % 순수익률 |\n|---|---|---|---|")
    L.append(f"| 전체 (신호 단위 판정과 같음) | {len(picks)} | {R.mean():+.3f} | {net.mean() * 100:+.2f}% |")
    L.append(f"| P4 가 산 신호 | {int((w == 'bought').sum())} | {R[w == 'bought'].mean():+.3f} | {net[w == 'bought'].mean() * 100:+.2f}% |")
    L.append(f"| P4 가 못 산 신호 | {int((w != 'bought').sum())} | {R[w != 'bought'].mean() if (w != 'bought').any() else float('nan'):+.3f} | "
             f"{net[w != 'bought'].mean() * 100 if (w != 'bought').any() else float('nan'):+.2f}% |")
    for kk, n in w[w != "bought"].value_counts().items():
        L.append(f"| - 못 산 이유: {WHY.get(kk, kk)} | {n} | {R[w == kk].mean():+.3f} | {net[w == kk].mean() * 100:+.2f}% |")
    L.append(f"\n확인: 산 신호 평균 {R[w == 'bought'].mean():+.3f}R / {net[w == 'bought'].mean() * 100:+.2f}% vs 신호 단위 결과 "
             f"{signal_ref[0]:+.3f}R / {signal_ref[1]:+.2f}% (차이 {R[w == 'bought'].mean() - signal_ref[0]:+.3f}R / "
             f"{net[w == 'bought'].mean() * 100 - signal_ref[1]:+.2f}%p)\n")
    if cut is not None:
        sc = stats(res, cut)
        e3 = rec3["e"]
        p3_cut = (e3[e3.index <= cut].iloc[-1] / CAPITAL - 1) * 100
        L.append(f"### {label}, {cut.date()} 종가까지 자른 값\n")
        L.append(f"- P4 누적 {sc['total']:+.1f}%, MDD {sc['mdd']:.1f}%")
        L.append(f"- 무작위 200회: 중앙값 {rnd['sub'].median():+.1f}% [5~95% {rnd['sub'].quantile(.05):+.1f}, {rnd['sub'].quantile(.95):+.1f}], "
                 f"P4 백분위 {(rnd['sub'] < sc['total']).mean() * 100:.1f}")
        L.append("- 대조군: " + ", ".join(f"{k} {x:+.2f}%" for k, x in sub_bench.items()) + f", 기존 P3 {p3_cut:+.1f}%")
        L.append(f"- 일별 투자 비중: 평균 {sc['expo_mean']:.1f}%, 중앙값 {sc['expo_med']:.1f}%, 최대 {sc['expo_max']:.1f}%, 0% 인 날 "
                 f"{sc['expo_zero']} / {sc['days']}일, 동시 보유 평균 {sc['npos_mean']:.1f}, 최대 {sc['npos_max']}\n")
    return L


def main():
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    L = ["# 5단계 P4 포트폴리오 참고 계산 (해석 없음)\n",
         "규칙: 명세 'P4 모의투자 포트폴리오 규칙'. 시작 3,000만 원, 종목당 매수 시점 총자산의 1/33, 신호 단위 상위 3 전부 매수 시도, "
         "같은 종목 보유 중·현금 부족이면 건너뜀(대체 없음). 청산·비용·금리·정지 묶임은 실제 규칙. 무작위 = 같은 규칙에 무작위 점수 상위 3, 200회, "
         "난수 [7, 3, 회차]. 기존 P3 는 port_run 그대로(누적이 결과 파일과 같은지 확인). 참고 계산이며 판정에 쓰지 않는다.\n"]
    L += run_period("개발", v.DEV_DIR, v.DEV_FIRST, v.DEV_LAST, os.path.join(v.DEV_DIR, "predictions.parquet"), (0.270, 1.72))
    L += run_period("검증", v.VAL_DIR, v.VAL_START, v.VAL_END, os.path.join(v.VAL_DIR, "predictions.parquet"), (0.328, 2.74), cut=SUB_END)
    L.append(f"({time.time() - t0:.0f}s)")
    with open(os.path.join(OUT, "p4.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
