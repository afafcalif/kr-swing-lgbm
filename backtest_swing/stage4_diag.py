"""
4단계 판정 (e) 실패 원인 진단 (보고만, 모델·규칙·특징은 그대로)

  python backtest_swing/stage4_diag.py

1. 정답 섞기 구현 확인: 같은 날 안에서만 섞였는지, 섞은 라벨과 진짜 라벨의 날짜별 순위상관
2. 특징 하나씩의 IC (라벨 y 와 날짜별 순위상관) 상위 10
3. 보유 기간 효과: 본 모델 점수와 청산 규칙 없는 5·10·20거래일 뒤 수익률(같은 날 평균 대비)의 IC, 보유 기간 구간별 상위 3 대 무작위
4. 꼬리 영향: 상위 3 순기대값 원값·상하위 1% 자름·수익 상위 10건 뺌, 연도별

평가 대상 준비는 stage4_model.evaluate 와 똑같이 한다 (실제 규칙 재계산, 전날 코스피 120일선 필터, 개발 구간 끝 자르기,
정지에 묶인 거래 -100%). 무작위 200회도 같은 난수 순서(시드 42)로 다시 뽑는다.
결과: backtest_swing/results/stage4/diag.md
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage3_backtest as sb  # noqa: E402
import stage3_data as sd  # noqa: E402
import stage4_features as s4  # noqa: E402
import stage4_model as sm  # noqa: E402

OUT = os.path.join(sm.OUT, "diag.md")
HORIZONS = (5, 10, 20)
HOLD_BINS = [(0, 3), (4, 7), (8, 14), (15, 30), (31, 10**6)]


def daily_ic(d: pd.DataFrame, a: str, b: str) -> pd.Series:
    """날짜별 순위상관 (종목 6개 이상인 날만)"""
    r = d[["date", a, b]].dropna()
    r = r.assign(ra=r.groupby("date")[a].rank(), rb=r.groupby("date")[b].rank())
    n = r.groupby("date").size()
    c = r.groupby("date")[["ra", "rb"]].corr().unstack().iloc[:, 1]
    return c[n.reindex(c.index) > 5]


def ic_line(s: pd.Series, rng) -> str:
    lo, hi = sm.month_block_ci(s.rename("ic").reset_index().assign(one=1.0), "ic", "one", rng)
    return f"{s.mean():+.4f} [{lo:+.4f}, {hi:+.4f}]"


def prepare(return_sim=False):
    """stage4_model.evaluate 와 같은 순서로 평가 대상 ds, sim, 무작위 200회 선택을 만든다"""
    ds = pd.read_parquet(sm.DATASET)
    pr = pd.read_parquet(sm.PRED)
    full = ds.copy()
    ds = ds.merge(pr.drop(columns="date"), on="g")
    ds = ds[(ds.date >= sm.FIRST_PRED) & (ds.date <= sm.LAST_PRED)].reset_index(drop=True)
    g = s4.SimArrays(np.load(os.path.join(sm.OUT, "sim.npz")))
    T = ds.g.to_numpy()
    stop = g.c[T] - s4.STOP_ATR * g.atr20[T]
    sim = sb.simulate(g, T, stop, np.full(len(T), np.nan), "full", want_legs=True)
    ds["net_real"], ds["hold_real"], ds["reason_real"] = sim["net"], sim["k"], sim["reason"]
    k = pd.read_parquet(sd.KOSPI).sort_values("timestamp").set_index("timestamp").close
    below = (k < k.rolling(120).mean())
    prev_below = below.shift(1).reindex(ds.date.unique()).fillna(False)
    ds["filter_ok"] = ~ds.date.map(prev_below).astype(bool)
    ds["halt_stuck"] = (ds.reason_real == sb.R_END).to_numpy() & (g.vol[g.dev_last[T]] == 0)
    crossing = ds[ds.filter_ok & (ds.reason_real == sb.R_END) & ~ds.halt_stuck]
    last_entry = crossing.date.min()
    drop = (ds.date >= last_entry).to_numpy()
    ds = ds[~drop].reset_index(drop=True)
    sim = {kk: (v[~drop] if isinstance(v, np.ndarray) and len(v) == len(drop) else v) for kk, v in sim.items()}
    ds["net_real"] = np.where(ds.halt_stuck, -1.0, ds.net_real)
    rng = np.random.default_rng(42)
    ds["exit_cal"] = g.cal[sim["j1"]]
    ds["cal"] = g.cal[ds.g.to_numpy()]
    fi = np.flatnonzero(ds.filter_ok.to_numpy())
    day_rows = [fi[ds.date.to_numpy()[fi] == d] for d in np.sort(ds.date[ds.filter_ok].unique())]
    rand_picks = [sm.pick_nodup(ds, [rng.permutation(r) for r in day_rows], sm.TOP_N) for _ in range(sm.N_RAND_REPS)]
    ds["score"] = ds[[c for c in pr.columns if c.startswith("main_")]].mean(axis=1)
    ds["score_shuffle"] = ds[[c for c in pr.columns if c.startswith("shuffle_")]].mean(axis=1)
    order = [r[np.argsort(-ds.score.to_numpy()[r], kind="stable")] for r in day_rows]
    picks = sm.pick_nodup(ds, order, sm.TOP_N)
    if return_sim:
        # 포트폴리오용: evaluate 와 같이 정지에 묶인 거래의 청산 금액을 0 으로 (비관적 평가)
        sim["p1"] = np.where(ds.halt_stuck.to_numpy(), 0.0, sim["p1"])
        return full, ds, g, rand_picks, picks, rng, last_entry, sim
    return full, ds, g, rand_picks, picks, rng, last_entry


def main():
    full, ds, g, rand_picks, picks, rng, last_entry = prepare()
    rand_exp = np.array([ds.loc[p, "net_real"].mean() * 100 for p in rand_picks])
    L = [f"# 4단계 판정 (e) 실패 원인 진단 (보고만)\n",
         f"평가 대상: 예측 {sm.FIRST_PRED.date()} ~ 마지막 진입일 {last_entry.date()} 전날, {len(ds):,}행 "
         f"(필터 통과 {int(ds.filter_ok.sum()):,}행). 무작위 200회 평균 {rand_exp.mean():+.3f}% (요약표와 같아야 함), "
         f"상위 3 {len(picks)}건 {ds.loc[picks, 'net_real'].mean() * 100:+.3f}%.\n"]

    # 1. 정답 섞기 구현 확인 (학습 때와 같은 함수·시드로 전체 dataset 에서 다시 만든다)
    L.append("## 1. 정답 섞기 구현\n")
    grp = full.groupby("date").indices
    for seed in sm.SEEDS_AUX:
        ys = sm.shuffled_labels(full, 1000 + seed)
        y = full.y_clip.to_numpy()
        same_multiset = all(np.array_equal(np.sort(ys[i]), np.sort(y[i])) for i in grp.values())
        f = full.assign(ys=ys)
        c = daily_ic(f, "ys", "y_clip")
        L.append(f"- 시드 {1000 + seed}: 날짜마다 라벨 값 묶음이 그대로(같은 날 안에서만 섞임) **{same_multiset}**, "
                 f"자리가 안 바뀐 행 {np.mean(ys == y) * 100:.2f}%, 섞은 라벨과 진짜 라벨의 날짜별 순위상관 평균 {c.mean():+.4f} "
                 f"(날짜별 분포 5~95%: {c.quantile(.05):+.3f} ~ {c.quantile(.95):+.3f}, {len(c)}일)")
    day_mean = full.groupby("date").y_clip.mean()
    L.append(f"- 학습 라벨 y_clip 의 날짜별 평균: 평균 {day_mean.mean():+.4f}, 날짜 간 표준편차 {day_mean.std():.4f} "
             f"(같은 날 안 표준편차 평균 {full.groupby('date').y_clip.std().mean():.4f}). y 는 같은 날 평균을 뺐지만 "
             f"±30% 자르기 때문에 y_clip 의 날짜별 평균은 0 이 아니고, 섞어도 이 날짜별 평균은 그대로 남는다.")
    ic_sh = daily_ic(ds, "score_shuffle", "y")
    L.append(f"- 정답 섞기 모델 IC (재계산): {ic_line(ic_sh, rng)}")
    rc = daily_ic(ds, "score_shuffle", "score")
    L.append(f"- 정답 섞기 점수와 본 모델 점수의 날짜별 순위상관 평균: {rc.mean():+.4f}")
    sh_corr = pd.Series({c: daily_ic(ds, "score_shuffle", c).mean() for c in sm.feature_sets(full)["main"]})
    top = sh_corr.reindex(sh_corr.abs().sort_values(ascending=False).index)[:5]
    L.append("- 정답 섞기 점수와 날짜별 순위상관이 큰 특징: " + ", ".join(f"{k} {v:+.3f}" for k, v in top.items()))
    L.append("")

    # 2. 특징 하나씩의 IC
    L.append("## 2. 특징 하나씩의 IC (라벨 y, 날짜별 순위상관, 평가 대상 전체)\n")
    feats = sm.feature_sets(full)["main"]
    d2 = ds.assign(dist_ma20_atr=ds.dist_ma20 / ds.atr_pct)
    fic = {c: daily_ic(d2, c, "y") for c in feats + ["dist_ma20_atr"]}
    fm = pd.Series({c: s.mean() for c, s in fic.items()})
    order = fm.drop("dist_ma20_atr").abs().sort_values(ascending=False).index[:10]
    L.append("| 순위 | 특징 | IC [95%] |\n|---|---|---|")
    for i, c in enumerate(order, 1):
        L.append(f"| {i} | {c} | {ic_line(fic[c], rng)} |")
    L.append(f"\n- 참고 (모델 특징 아님, 계산해 본 값): 20일선 거리 / ATR% = {ic_line(fic['dist_ma20_atr'], rng)}")
    for c in ("dist_ma20", "atr_pct"):
        L.append(f"- {c}: IC {ic_line(fic[c], rng)} (63개 중 |IC| 순위 {list(fm.drop('dist_ma20_atr').abs().sort_values(ascending=False).index).index(c) + 1})")
    L.append(f"- 본 모델 점수 IC: {ic_line(daily_ic(ds, 'score', 'y'), rng)}")
    L.append("")

    # 3. 보유 기간 효과
    L.append("## 3. 보유 기간 효과\n")
    T = ds.g.to_numpy()
    for h in HORIZONS:
        j = np.minimum(T + h, len(g.c) - 1)
        ok = (g.sid[j] == g.sid[T]) & (T + h < len(g.c))
        fwd = np.where(ok, g.c[j] / g.c[T] - 1, np.nan)
        ds[f"fwd{h}"] = fwd
        ds[f"xfwd{h}"] = ds[f"fwd{h}"] - ds.groupby("date")[f"fwd{h}"].transform("mean")
    assert (g.cal_dates[g.cal[np.minimum(T + 20, len(g.c) - 1)]] <= sm.LAST_PRED).all()
    L.append("| 대상 | IC [95%] |\n|---|---|")
    L.append(f"| 라벨 y (청산 규칙 적용) | {ic_line(daily_ic(ds, 'score', 'y'), rng)} |")
    for h in HORIZONS:
        L.append(f"| {h}거래일 뒤 수익률 - 같은 날 평균 (청산 규칙 없음) | {ic_line(daily_ic(ds, 'score', f'xfwd{h}'), rng)} |")
    L.append(f"| 실제 규칙 보유 기간 (거래일) | {ic_line(daily_ic(ds, 'score', 'hold_real'), rng)} |")
    yr = ds.assign(r=ds.groupby("date").y.rank(pct=True))
    L.append(f"| (참고) 라벨 y 와 보유 기간 | {ic_line(daily_ic(ds, 'y', 'hold_real'), rng)} |")
    t3 = ds.loc[picks]
    rp = ds.loc[np.concatenate(rand_picks)]
    L.append("\n상위 3 이 고른 종목의 고정 기간 수익률 (청산 규칙·비용 없음, %):\n\n| 기간 | 상위 3 | 무작위 200회 | 차이 | 상위 3 (같은 날 평균 대비) |\n|---|---|---|---|---|")
    for h in HORIZONS:
        L.append(f"| {h}일 | {t3[f'fwd{h}'].mean() * 100:+.3f} | {rp[f'fwd{h}'].mean() * 100:+.3f} | "
                 f"{(t3[f'fwd{h}'].mean() - rp[f'fwd{h}'].mean()) * 100:+.3f} | {t3[f'xfwd{h}'].mean() * 100:+.3f} |")
    L.append("\n실제 규칙 보유 기간 구간별 (구간은 결과로 나눈 것이라 원인 판단용 참고):\n\n"
             "| 보유 기간 | 상위 3 건수 (비율) | 상위 3 순수익 | 무작위 건수 비율 | 무작위 순수익 | 차이 |\n|---|---|---|---|---|---|")
    for lo, hi in HOLD_BINS:
        a = t3[(t3.hold_real >= lo) & (t3.hold_real <= hi)]
        b = rp[(rp.hold_real >= lo) & (rp.hold_real <= hi)]
        name = f"{lo}~{hi}일" if hi < 10**6 else f"{lo}일 이상"
        L.append(f"| {name} | {len(a)} ({len(a) / len(t3) * 100:.1f}%) | {a.net_real.mean() * 100:+.3f}% | "
                 f"{len(b) / len(rp) * 100:.1f}% | {b.net_real.mean() * 100:+.3f}% | {(a.net_real.mean() - b.net_real.mean()) * 100:+.3f}%p |")
    L.append(f"\n보유 기간 평균: 상위 3 {t3.hold_real.mean():.1f}일, 무작위 {rp.hold_real.mean():.1f}일 | "
             f"손절 청산 비율: 상위 3 {(t3.reason_real == sb.R_STOP).mean() * 100:.1f}%, 무작위 {(rp.reason_real == sb.R_STOP).mean() * 100:.1f}%")
    L.append("")

    # 4. 꼬리 영향
    L.append("## 4. 꼬리 영향 (비관적 평가, %)\n")
    L.append("| 구분 | 상위 3 원값 | 상하위 1% 자름 | 수익 상위 10건 뺌 | 무작위 원값 | 무작위 1% 자름 | 무작위 10건 뺌 |\n|---|---|---|---|---|---|---|")

    def row(name, t, rps):
        tr, ex = sm.tail_stats(t.net_real.to_numpy())
        rs = np.array([sm.tail_stats(x.net_real.to_numpy()) for x in rps])
        rm = np.mean([x.net_real.mean() for x in rps]) * 100
        L.append(f"| {name} ({len(t)}건) | {t.net_real.mean() * 100:+.3f} | {tr:+.3f} | {ex:+.3f} | {rm:+.3f} | "
                 f"{np.nanmean(rs[:, 0]):+.3f} | {np.nanmean(rs[:, 1]):+.3f} |")

    rps_all = [ds.loc[p] for p in rand_picks]
    row("전체", t3, rps_all)
    for y in sorted(t3.date.dt.year.unique()):
        row(str(y), t3[t3.date.dt.year == y], [x[x.date.dt.year == y] for x in rps_all])
    top10 = t3.nlargest(10, "net_real")[["code", "date", "net_real", "hold_real"]]
    L.append("\n상위 3 의 수익 상위 10건: " + ", ".join(f"{r.code} {r.date.date()} {r.net_real * 100:+.0f}% ({r.hold_real}일)"
                                               for r in top10.itertuples()))
    L.append(f"\n상위 10건 수익 합 / 상위 3 전체 수익 합: {top10.net_real.sum() / t3.net_real.sum() * 100:.0f}%")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
