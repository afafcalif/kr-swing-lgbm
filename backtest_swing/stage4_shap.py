"""
4단계 특징 중요도 진단 (명세 7번 보고 누락분, 판정·모델은 그대로)

  python backtest_swing/stage4_shap.py

월별 모델을 stage4_model.train 과 같은 학습 집합·시드로 다시 학습해서 (저장된 모델이 없음), 그 달 예측분으로 계산한다.
다시 학습한 모델의 예측이 저장된 predictions.parquet 와 같은지 매달 확인한다.

1. 본 모델(main, 시드 5): 특징별 gain 합계, SHAP 평균 절댓값 상위 20 (월별·시드별 모델 값의 평균)
2. 정답 섞기(shuffle, 시드 2): 같은 방식 상위 10
3. 1번 상위 20 특징과 atr_pct 의 같은 날 순위상관 평균
4. 점수 상위·하위 10분위에서 평균 SHAP(부호 포함) 절댓값이 큰 특징 상위 5
   (분위·평가 대상은 stage4_model.evaluate 와 같음: 시장 필터 통과, 개발 구간 끝 자르기)

결과: backtest_swing/results/stage4/diag_shap.md
"""
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage4_diag as dg  # noqa: E402
import stage4_model as sm  # noqa: E402

OUT = os.path.join(sm.OUT, "diag_shap.md")
TOL = 1e-6  # 저장된 예측은 float32 라 같은 모델이어도 1e-8 정도 차이가 난다 (점수 크기는 0.01 수준)


def run_variant(ds, pr, var, seeds, cols):
    """월별·시드별: gain, 그 달 SHAP 평균 절댓값, 행별 SHAP(시드 평균), 저장된 예측과의 최대 차이"""
    ms = sm.months(ds)
    gains, shap_abs = [], []
    row_shap = np.zeros((len(ds), len(cols)), np.float32)
    max_diff, t0 = 0.0, time.time()
    y_by_seed = {s: (sm.shuffled_labels(ds, 1000 + s) if var == "shuffle" else ds.y_clip.to_numpy()) for s in seeds}
    for m, first_day, cutoff in ms:
        tr_idx = np.flatnonzero((ds.date <= cutoff).to_numpy())
        te_idx = np.flatnonzero(((ds.date >= m.start_time) & (ds.date <= m.end_time)).to_numpy())
        Xtr = ds.iloc[tr_idx][cols].to_numpy(np.float32)
        Xte = ds.iloc[te_idx][cols].to_numpy(np.float32)
        for seed in seeds:
            bst = sm.fit(Xtr, y_by_seed[seed][tr_idx], seed)
            p = bst.predict(Xte)
            saved = pr[f"{var}_s{seed}"].to_numpy()[te_idx]
            d = np.abs(p - saved)
            if np.nanmax(d) > TOL:
                # 다른 모델의 SHAP 을 계산하게 되므로 그 달에서 멈춘다
                msg = (f"STOP: {var} 시드 {seed} {m} - 다시 만든 예측이 저장된 예측과 다름: 최대 차이 {np.nanmax(d):.3e}, "
                       f"허용 {TOL:.0e} 초과 행 {int((d > TOL).sum()):,}/{len(d):,}, 학습 행 {len(tr_idx):,}, "
                       f"예측 순위상관 {pd.Series(p).rank().corr(pd.Series(saved).rank()):.6f}")
                print(msg, flush=True)
                with open(OUT, "w", encoding="utf-8") as fh:
                    fh.write(msg + "\n")
                sys.exit(1)
            max_diff = max(max_diff, float(np.nanmax(d)))
            contrib = bst.predict(Xte, pred_contrib=True)[:, :-1]
            gains.append(pd.Series(bst.feature_importance("gain"), index=cols))
            shap_abs.append(pd.Series(np.abs(contrib).mean(axis=0), index=cols))
            row_shap[te_idx] += contrib.astype(np.float32) / len(seeds)
        print(f"  {var} {m}: {len(tr_idx):,} train rows, {len(te_idx):,} predicted ({time.time() - t0:.0f}s), "
              f"max |pred - saved| so far {max_diff:.2e}", flush=True)
    return pd.DataFrame(gains).mean(), pd.DataFrame(shap_abs).mean(), row_shap, max_diff


def table(gain, shap, n):
    t = pd.DataFrame({"gain": gain, "gain_share": gain / gain.sum() * 100, "shap": shap,
                      "shap_share": shap / shap.sum() * 100})
    t["gain_rank"] = t.gain.rank(ascending=False).astype(int)
    return t.sort_values("shap", ascending=False).head(n)


def main():
    ds = pd.read_parquet(sm.DATASET)
    pr = pd.read_parquet(sm.PRED)
    assert (pr.g.to_numpy() == ds.g.to_numpy()).all()
    fs = sm.feature_sets(ds)
    cols = fs["main"]
    res = {}
    for var, seeds in (("main", sm.SEEDS_MAIN), ("shuffle", sm.SEEDS_AUX)):
        res[var] = run_variant(ds, pr, var, seeds, cols)

    L = ["# 4단계 특징 중요도 진단 (명세 7번, 판정·모델 변경 없음)\n",
         "월별 모델을 같은 학습 집합·시드로 다시 학습해 그 달 예측분으로 계산 (예측 2022-01 ~ 2025-12, 48개월). "
         "gain = 모델 하나의 특징별 분할 이득 합계, SHAP = 그 달 예측 행의 |기여도| 평균. 둘 다 월별·시드별 모델 값의 평균.\n",
         f"다시 학습한 모델과 저장된 예측의 최대 차이: main {res['main'][3]:.2e}, shuffle {res['shuffle'][3]:.2e} "
         "(0 이면 판정에 쓴 모델과 같은 모델)\n"]

    # 1, 2
    for var, n in (("main", 20), ("shuffle", 10)):
        gain, shap = res[var][0], res[var][1]
        t = table(gain, shap, n)
        L.append(f"## {'1. 본 모델 (main, 시드 5)' if var == 'main' else '2. 정답 섞기 (shuffle, 시드 2)'}: SHAP 평균 절댓값 상위 {n}\n")
        L.append("| 순위 | 특징 | SHAP 평균 절댓값 | SHAP 비중 | gain 합계 | gain 비중 | gain 순위 |\n|---|---|---|---|---|---|---|")
        for i, (f, r) in enumerate(t.iterrows(), 1):
            L.append(f"| {i} | {f} | {r.shap:.5f} | {r.shap_share:.1f}% | {r.gain:,.1f} | {r.gain_share:.1f}% | {int(r.gain_rank)} |")
        topg = (gain / gain.sum() * 100).sort_values(ascending=False).head(n)
        L.append(f"\ngain 순 상위 {n}: " + ", ".join(f"{k} {v:.1f}%" for k, v in topg.items()))
        L.append(f"\n상위 {n}개 SHAP 비중 합 {t.shap_share.sum():.1f}%, 지지·저항 특징 SHAP 비중 합 "
                 f"{(shap[[c for c in cols if c in dg.s4.SR_FEATS]].sum() / shap.sum() * 100):.1f}%, "
                 f"수급 특징 {(shap[[c for c in cols if c in dg.s4.FLOW_FEATS]].sum() / shap.sum() * 100):.1f}%\n")

    # 평가 대상 (evaluate 와 같음)
    full, ev, g, rand_picks, picks, rng, last_entry = dg.prepare()
    gpos = pd.Series(np.arange(len(ds)), index=ds.g.to_numpy())
    ev_rows = gpos.reindex(ev.g.to_numpy()).to_numpy()
    shap_main = pd.DataFrame(res["main"][2][ev_rows], columns=cols, index=ev.index)

    # 3
    top20 = table(res["main"][0], res["main"][1], 20).index
    L.append("## 3. 본 모델 상위 20 특징과 atr_pct 의 같은 날 순위상관 평균 (평가 대상 전체)\n")
    L.append("| 순위 | 특징 | atr_pct 와 순위상관 | 라벨 y 와 IC |\n|---|---|---|---|")
    for i, f in enumerate(top20, 1):
        rc = dg.daily_ic(ev, f, "atr_pct").mean() if f != "atr_pct" else 1.0
        L.append(f"| {i} | {f} | {rc:+.3f} | {dg.daily_ic(ev, f, 'y').mean():+.3f} |")
    L.append("")

    # 4
    fo = ev[ev.filter_ok]
    dec = (fo.groupby("date").score.rank(pct=True) * 10).clip(upper=9.999).astype(int)
    pct = fo[cols].groupby(fo.date).rank(pct=True)
    L.append("## 4. 점수 상위·하위 10분위의 평균 SHAP (부호 포함, 절댓값 상위 5, 시장 필터 통과 대상)\n")
    L.append("특징 값 분위 = 같은 날 대상 안 순위 백분위의 분위 평균 (0.5 = 중간).\n")
    for d, name in ((9, "상위 10분위"), (0, "하위 10분위")):
        idx = dec.index[dec == d]
        ms_ = shap_main.loc[idx].mean()
        top5 = ms_.reindex(ms_.abs().sort_values(ascending=False).index)[:5]
        L.append(f"**{name}** ({len(idx):,}행, 평균 점수 {fo.loc[idx, 'score'].mean():+.4f}, 실제 규칙 순수익 "
                 f"{fo.loc[idx, 'net_real'].mean() * 100:+.2f}%, SHAP 합 평균 {shap_main.loc[idx].sum(axis=1).mean():+.4f})\n")
        L.append("| 특징 | 평균 SHAP | 특징 값 분위 |\n|---|---|---|")
        for f, v in top5.items():
            L.append(f"| {f} | {v:+.5f} | {pct.loc[idx, f].mean():.2f} |")
        L.append("")
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
