"""
4단계 판정 후 수정 (명세 13장): 정답 섞기 검사 — 같은 날 안에서 섞은 뒤 그날 평균을 다시 빼서 날짜별 평균 0

  python backtest_swing/stage4_shuffle_dm.py train      # 섞기 시드 1001·1002 ↔ 모델 시드 1·2, 월별 재학습 (기존과 같은 방식)
  python backtest_swing/stage4_shuffle_dm.py evaluate   # 통과 기준: IC 95% 범위가 0 포함, 상위 3 이 무작위 200회 5~95% 안

기존 predictions.parquet·summary.md 는 건드리지 않는다.
결과: results/stage4/predictions_shuffle_dm.parquet, results/stage4/shuffle_dm.md
"""
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage4_diag as dg  # noqa: E402
import stage4_model as sm  # noqa: E402

PRED_DM = os.path.join(sm.OUT, "predictions_shuffle_dm.parquet")
OUT = os.path.join(sm.OUT, "shuffle_dm.md")


def shuffled_labels_demeaned(ds, seed):
    """기존 shuffled_labels 로 같은 날 안에서 섞은 뒤, 섞은 라벨에서 그날 평균을 뺀다"""
    y = sm.shuffled_labels(ds, seed)
    return y - pd.Series(y).groupby(ds.date.to_numpy()).transform("mean").to_numpy()


def train():
    ds = pd.read_parquet(sm.DATASET)
    cols = sm.feature_sets(ds)["shuffle"]
    ms = sm.months(ds)
    preds = pd.DataFrame({"g": ds.g, "date": ds.date})
    t0 = time.time()
    ys = {s: shuffled_labels_demeaned(ds, 1000 + s) for s in sm.SEEDS_AUX}
    for s, y in ys.items():
        dm = pd.Series(y).groupby(ds.date.to_numpy()).mean()
        print(f"seed {1000 + s}: day-mean of demeaned shuffled label max |.| {dm.abs().max():.2e}", flush=True)
    out = np.full((len(ds), len(sm.SEEDS_AUX)), np.nan, np.float32)
    for m, first_day, cutoff in ms:
        tr_idx = np.flatnonzero((ds.date <= cutoff).to_numpy())
        te_idx = np.flatnonzero(((ds.date >= m.start_time) & (ds.date <= m.end_time)).to_numpy())
        Xtr = ds.iloc[tr_idx][cols].to_numpy(np.float32)
        Xte = ds.iloc[te_idx][cols].to_numpy(np.float32)
        for si, seed in enumerate(sm.SEEDS_AUX):
            out[te_idx, si] = sm.fit(Xtr, ys[seed][tr_idx], seed).predict(Xte)
        print(f"  shuffle_dm {m} trained on {len(tr_idx):,} rows ({time.time() - t0:.0f}s)", flush=True)
    for si, seed in enumerate(sm.SEEDS_AUX):
        preds[f"shuffle_dm_s{seed}"] = out[:, si]
    preds.to_parquet(PRED_DM, index=False)
    print(f"train done ({time.time() - t0:.0f}s)")


def evaluate():
    full, ds, g, rand_picks, picks_main, rng, last_entry = dg.prepare()
    pr = pd.read_parquet(sm.PRED)
    pdm = pd.read_parquet(PRED_DM)
    ds = ds.merge(pdm.drop(columns="date"), on="g", how="left")
    assert ds[[c for c in pdm.columns if c.startswith("shuffle_dm_")]].notna().all().all()
    rand_exp = np.array([ds.loc[p, "net_real"].mean() * 100 for p in rand_picks])
    r_mean, r05, r95 = rand_exp.mean(), np.quantile(rand_exp, .05), np.quantile(rand_exp, .95)
    fi = np.flatnonzero(ds.filter_ok.to_numpy())
    day_rows = [fi[ds.date.to_numpy()[fi] == d] for d in np.sort(ds.date[ds.filter_ok].unique())]
    res = {}
    for name, cols in (("shuffle_dm", [c for c in pdm.columns if c.startswith("shuffle_dm_")]),
                       ("shuffle (기존, 참고)", [c for c in pr.columns if c.startswith("shuffle_")])):
        score = ds[cols].mean(axis=1).to_numpy()
        order = [r[np.argsort(-score[r], kind="stable")] for r in day_rows]
        picks = sm.pick_nodup(ds, order, sm.TOP_N)
        d = ds.assign(score_v=score)
        ic = dg.daily_ic(d, "score_v", "y")
        lo, hi = sm.month_block_ci(ic.rename("ic").reset_index().assign(one=1.0), "ic", "one", rng)
        exp = ds.loc[picks, "net_real"].mean() * 100
        yearly = ds.loc[picks].groupby(ds.loc[picks].date.dt.year).net_real.mean().mul(100).round(3).to_dict()
        res[name] = {"signals": len(picks), "exp": exp, "ic": ic.mean(), "ic_ci": (lo, hi), "yearly": yearly,
                     "pass_ic": lo <= 0 <= hi, "pass_top3": r05 <= exp <= r95}
    m = res["shuffle_dm"]
    ok = m["pass_ic"] and m["pass_top3"]
    L = ["# 4단계 판정 후 수정: 정답 섞기 검사 (명세 13장)\n",
         f"평가 대상은 기존 평가와 같음 (마지막 진입일 {last_entry.date()}). 무작위 200회: 평균 {r_mean:+.3f}%, "
         f"5~95% {r05:+.3f} ~ {r95:+.3f}%\n",
         "| 모델 | 신호 | 상위 3 순기대값 | 무작위 5~95% 안 | IC [95%] | IC 범위가 0 포함 | 연도별 상위 3 순수익 |\n|---|---|---|---|---|---|---|"]
    for name, r in res.items():
        L.append(f"| {name} | {r['signals']} | {r['exp']:+.3f}% | {'O' if r['pass_top3'] else 'X'} | "
                 f"{r['ic']:+.4f} [{r['ic_ci'][0]:+.4f}, {r['ic_ci'][1]:+.4f}] | {'O' if r['pass_ic'] else 'X'} | {r['yearly']} |")
    L.append(f"\n**판정 (e, 수정): {'통과' if ok else '통과하지 못함'}** "
             "(기준: 수정한 정답 섞기 모델의 IC 95% 범위가 0 포함, 상위 3 이 무작위 200회 5~95% 안)")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    {"train": train, "evaluate": evaluate}[sys.argv[1]]()
