"""
4단계 참고 지표 (개발 구간, 판정에 안 씀). 모델·규칙·판정은 그대로, 2026 데이터 안 씀.

  python backtest_swing/stage4_reference.py

1. 무작위 3종목 포트폴리오 200회 (같은 청산·필터·비용·비중): 누적 수익 분포에서 본 모델(+254%)의 위치
2. 시작 3,000만 원 포트폴리오: 20% x 3, 20% x 5 (본 모델)
3. 단순 규칙 대조: 같은 날 대상 중 250일 고점에 가까운 절반 안에서 atr_pct 낮은 순 3종목
   (개발 구간 진단에서 나온 규칙이라 판정에 쓰지 않고, 모델이 이 규칙보다 나은지 보는 용도)

포트폴리오는 stage4_model.portfolio_by_score 그대로 (시작 자금·비중·최대 종목 수만 바꿔 부름).
결과: results/stage4/reference.md
"""
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage3_backtest as sb  # noqa: E402
import stage4_diag as dg  # noqa: E402
import stage4_model as sm  # noqa: E402

OUT = os.path.join(sm.OUT, "reference.md")
N_PORT_REPS = 200
PORT_SEED = 7


def port(g, d, sim, capital=5_000_000, frac=0.20, max_pos=3):
    saved = sb.START_CAPITAL, sb.POSITION_FRAC, sb.MAX_POSITIONS
    sm.sb.START_CAPITAL, sm.sb.POSITION_FRAC, sm.sb.MAX_POSITIONS = capital, frac, max_pos
    try:
        return sm.portfolio_by_score(g, d, sim)
    finally:
        sm.sb.START_CAPITAL, sm.sb.POSITION_FRAC, sm.sb.MAX_POSITIONS = saved


def rule_score(ds):
    """시장 필터 통과 대상 안에서: 250일 고점 거리 상위 절반이면 10 - atr_pct, 아니면 -atr_pct (절반 밖은 항상 아래)"""
    s = pd.Series(-np.inf, index=ds.index)
    f = ds[ds.filter_ok]
    near = f.groupby("date").dist_high250.rank(pct=True, ascending=True) > 0.5
    s.loc[f.index] = np.where(near, 10.0, 0.0) - f.atr_pct.to_numpy()
    return s


def signal_stats(ds, score, day_rows, rand_exp, rand_picks):
    order = [r[np.argsort(-score[r], kind="stable")] for r in day_rows]
    picks = sm.pick_nodup(ds, order, sm.TOP_N)
    t3 = ds.loc[picks]
    rp = pd.concat([ds.loc[p, ["date", "net_real"]] for p in rand_picks])
    yearly = {int(y): round((t3[t3.date.dt.year == y].net_real.mean() - rp[rp.date.dt.year == y].net_real.mean()) * 100, 3)
              for y in sorted(t3.date.dt.year.unique())}
    trim, ex10 = sm.tail_stats(t3.net_real.to_numpy())
    return picks, {"signals": len(t3), "exp": t3.net_real.mean() * 100, "diff": t3.net_real.mean() * 100 - rand_exp.mean(),
                   "pct_of_random": (rand_exp < t3.net_real.mean() * 100).mean() * 100, "yearly_diff": yearly,
                   "trim1": trim, "ex10": ex10, "hold": t3.hold_real.mean()}


def fmt_port(p):
    return (f"누적 {p['total']:+.2f}%, CAGR {p['cagr']:+.2f}%, MDD {p['mdd']:.2f}%, 월평균 매매 {p['trades_per_month']:.2f}, "
            f"연도별 {{{', '.join(f'{k}: {v:+.1f}' for k, v in p['yearly'].items())}}}")


def main():
    full, ds, g, rand_picks, picks_main, rng, last_entry, sim = dg.prepare(return_sim=True)
    rand_exp = np.array([ds.loc[p, "net_real"].mean() * 100 for p in rand_picks])
    fi = np.flatnonzero(ds.filter_ok.to_numpy())
    day_rows = [fi[ds.date.to_numpy()[fi] == d] for d in np.sort(ds.date[ds.filter_ok].unique())]
    L = ["# 4단계 참고 지표 (개발 구간, 판정에 안 씀)\n",
         f"평가 대상·청산·비용·필터는 판정 평가와 같음 (마지막 진입일 {last_entry.date()}, 정지 묶임 -100%). "
         "포트폴리오는 기존 함수 그대로, 시작 자금·비중·최대 종목 수만 바꿈.\n"]

    # 기준 확인: 본 모델 500만 원 20% x 3 = 요약표 +254.10%
    base = port(g, ds, sim)
    L.append(f"기준 확인 (본 모델, 500만 원, 20% x 3): {fmt_port(base)} (요약표 +254.10% 와 같아야 함)\n")

    # 1. 무작위 포트폴리오 200회
    t0 = time.time()
    prng = np.random.default_rng(PORT_SEED)
    totals, mdds = [], []
    for rep in range(N_PORT_REPS):
        p = port(g, ds.assign(score=prng.random(len(ds))), sim)
        totals.append(p["total"])
        mdds.append(p["mdd"])
        if rep == 0:
            print(f"one random portfolio {time.time() - t0:.1f}s", flush=True)
    totals, mdds = np.array(totals), np.array(mdds)
    L.append("## 1. 무작위 3종목 포트폴리오 200회 (500만 원, 20% x 3, 같은 청산·필터·비용)\n")
    L.append(f"- 누적 수익: 평균 {totals.mean():+.1f}%, 중앙값 {np.median(totals):+.1f}%, 5~95% {np.quantile(totals, .05):+.1f} ~ "
             f"{np.quantile(totals, .95):+.1f}%, 최대 {totals.max():+.1f}%")
    L.append(f"- 본 모델 {base['total']:+.1f}% 의 위치: 무작위 200회 중 {int((totals >= base['total']).sum())}회가 같거나 높음 "
             f"(백분위 {(totals < base['total']).mean() * 100:.1f})")
    L.append(f"- MDD: 무작위 중앙값 {np.median(mdds):.1f}% (5~95% {np.quantile(mdds, .05):.1f} ~ {np.quantile(mdds, .95):.1f}), 본 모델 {base['mdd']:.1f}%")
    L.append(f"- 무작위 순서 난수 시드 {PORT_SEED}\n")

    # 2. 3,000만 원
    L.append("## 2. 시작 3,000만 원 포트폴리오 (본 모델)\n")
    p3 = port(g, ds, sim, 30_000_000, 0.20, 3)
    p5 = port(g, ds, sim, 30_000_000, 0.20, 5)
    L.append(f"- 20% x 3: {fmt_port(p3)}")
    L.append(f"- 20% x 5: {fmt_port(p5)}\n")

    # 3. 단순 규칙
    rs = rule_score(ds)
    ms = ds.score.to_numpy()
    picks_m, sm_main = signal_stats(ds, ms, day_rows, rand_exp, rand_picks)
    picks_r, sm_rule = signal_stats(ds, rs.to_numpy(), day_rows, rand_exp, rand_picks)
    ic_m = dg.daily_ic(ds, "score", "y")
    ic_r = dg.daily_ic(ds.assign(rule=rs.replace(-np.inf, np.nan)), "rule", "y")
    overlap = len(set(picks_m) & set(picks_r)) / len(picks_m) * 100
    L.append("## 3. 단순 규칙 대조 (250일 고점에 가까운 절반 안에서 atr_pct 낮은 순 3종목)\n")
    L.append("개발 구간 진단에서 나온 규칙이라 판정에 쓰지 않는다.\n")
    L.append("| | 신호 | 상위 3 순기대값 | 무작위 대비 | 무작위 200회 중 백분위 | 연도별 무작위 대비 | 1% 자름 | 상위 10건 뺌 | 보유 평균 | IC (시장 필터 통과 대상) |\n|---|---|---|---|---|---|---|---|---|---|")
    for name, s, ic in (("본 모델", sm_main, ic_m), ("단순 규칙", sm_rule, ic_r)):
        L.append(f"| {name} | {s['signals']} | {s['exp']:+.3f}% | {s['diff']:+.3f}%p | {s['pct_of_random']:.1f} | {s['yearly_diff']} | "
                 f"{s['trim1']:+.3f}% | {s['ex10']:+.3f}% | {s['hold']:.1f}일 | {ic.mean():+.4f} |")
    L.append(f"\n- 모델 - 단순 규칙 (상위 3 순기대값): {sm_main['exp'] - sm_rule['exp']:+.3f}%p, 두 방식이 고른 신호가 겹치는 비율 {overlap:.1f}%")
    L.append("- IC: 본 모델은 평가 대상 전체 기준(요약표와 같음), 단순 규칙은 시장 필터 통과 대상만 점수가 있음")
    pr5 = port(g, ds.assign(score=rs), sim)
    pr3 = port(g, ds.assign(score=rs), sim, 30_000_000, 0.20, 3)
    pr5b = port(g, ds.assign(score=rs), sim, 30_000_000, 0.20, 5)
    L.append(f"\n단순 규칙 포트폴리오:\n\n- 500만 원 20% x 3: {fmt_port(pr5)}\n- 3,000만 원 20% x 3: {fmt_port(pr3)}\n- 3,000만 원 20% x 5: {fmt_port(pr5b)}")
    L.append(f"- 같은 조건 본 모델: 500만 원 20% x 3 {base['total']:+.1f}%, 3,000만 원 20% x 3 {p3['total']:+.1f}%, 20% x 5 {p5['total']:+.1f}%")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
