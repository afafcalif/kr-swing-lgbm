"""
4단계: LightGBM 매월 재학습 + 개발 구간 평가 (명세: backtest_swing/STAGE4_SPEC.md)

  python backtest_swing/stage4_model.py timing     # 가장 큰 학습 집합으로 1회 학습 시간을 재고 전체 예상 시간 보고
  python backtest_swing/stage4_model.py train      # 본 모델(시드 5) + 정답 섞기·지지저항 제외·수급 제외(시드 2) 예측
  python backtest_swing/stage4_model.py evaluate   # 판정 (a)~(e), 대조군, 포트폴리오, 특징 기여·중요도

검증 구간(2026)은 이 스크립트로 실행하지 않는다 (dataset 자체가 2025-12-31 까지만 있다).
"""
import math
import os
import sys
import time

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage3_backtest as sb  # noqa: E402
import stage3_data as sd  # noqa: E402
import stage4_features as s4  # noqa: E402

OUT = s4.OUT
DATASET = os.path.join(OUT, "dataset.parquet")
PRED = os.path.join(OUT, "predictions.parquet")
FIRST_PRED, LAST_PRED = pd.Timestamp("2022-01-01"), pd.Timestamp("2025-12-31")
LABEL_GAP = s4.LABEL_CAP + 1          # 재학습일 - 61 거래일까지의 신호만 학습 (정답이 재학습일 전에 끝남)
PARAMS = {"objective": "regression", "num_leaves": 31, "learning_rate": 0.03, "min_child_samples": 500,
          "feature_fraction": 0.7, "bagging_fraction": 0.7, "bagging_freq": 1, "lambda_l2": 10.0,
          "verbose": -1, "num_threads": 0}
N_TREES = 500
SEEDS_MAIN, SEEDS_AUX = [1, 2, 3, 4, 5], [1, 2]
NON_FEATURES = {"code", "date", "g", "close", "above_ma20", "net", "hold", "reason", "y", "y_clip"}
TOP_N = 3
N_RAND_REPS = 200
BOOT = 2000


def feature_sets(ds):
    allf = [c for c in ds.columns if c not in NON_FEATURES]
    return {"main": allf, "shuffle": allf,
            "no_sr": [c for c in allf if c not in s4.SR_FEATS],
            "no_flow": [c for c in allf if c not in s4.FLOW_FEATS]}


def months(ds):
    dates = pd.DatetimeIndex(np.sort(ds.date.unique()))
    out = []
    for m in pd.period_range(FIRST_PRED, LAST_PRED, freq="M"):
        in_m = dates[(dates >= m.start_time) & (dates <= m.end_time)]
        if len(in_m) == 0:
            continue
        i = dates.searchsorted(in_m[0])
        cutoff = dates[i - LABEL_GAP] if i - LABEL_GAP >= 0 else None
        out.append((m, in_m[0], cutoff))
    return out


def fit(X, y, seed):
    p = dict(PARAMS, seed=seed, bagging_seed=seed, feature_fraction_seed=seed)
    return lgb.train(p, lgb.Dataset(X, label=y, free_raw_data=True), num_boost_round=N_TREES)


def shuffled_labels(ds, seed):
    rng = np.random.default_rng(seed)
    y = ds.y_clip.to_numpy().copy()
    for idx in ds.groupby("date").indices.values():
        y[idx] = y[rng.permutation(idx)]
    return y


def timing():
    ds = pd.read_parquet(DATASET)
    fs = feature_sets(ds)
    m, first_day, cutoff = months(ds)[-1]
    tr = ds[ds.date <= cutoff]
    t0 = time.time()
    fit(tr[fs["main"]].to_numpy(np.float32), tr.y_clip.to_numpy(), 1)
    sec = time.time() - t0
    ms = months(ds)
    sizes = np.array([(ds.date <= c).sum() for _, _, c in ms])
    per_fit = sec * sizes / sizes[-1]
    n_fits = {"main": 5, "shuffle": 2, "no_sr": 2, "no_flow": 2}
    total = per_fit.sum() * sum(n_fits.values())
    print(f"features {len(fs['main'])}, largest training set {len(tr):,} rows (retrain {first_day.date()}), "
          f"one fit {sec:.0f}s")
    print(f"months {len(ms)}, fits: " + ", ".join(f"{k} {v} seeds" for k, v in n_fits.items())
          + f" -> estimated total {total / 3600:.1f}h (main alone {per_fit.sum() * 5 / 3600:.1f}h)")


def train(variants=("main", "shuffle", "no_sr", "no_flow")):
    ds = pd.read_parquet(DATASET)
    fs = feature_sets(ds)
    ms = months(ds)
    preds = pd.DataFrame({"g": ds.g, "date": ds.date})
    imp_gain, t0 = {}, time.time()
    for var in variants:
        seeds = SEEDS_MAIN if var == "main" else SEEDS_AUX
        cols = fs[var]
        out = np.full((len(ds), len(seeds)), np.nan, np.float32)
        for mi, (m, first_day, cutoff) in enumerate(ms):
            tr_idx = np.flatnonzero((ds.date <= cutoff).to_numpy())
            te_idx = np.flatnonzero(((ds.date >= m.start_time) & (ds.date <= m.end_time)).to_numpy())
            Xtr = ds.iloc[tr_idx][cols].to_numpy(np.float32)
            Xte = ds.iloc[te_idx][cols].to_numpy(np.float32)
            for si, seed in enumerate(seeds):
                ytr = shuffled_labels(ds, 1000 + seed)[tr_idx] if var == "shuffle" else ds.y_clip.to_numpy()[tr_idx]
                bst = fit(Xtr, ytr, seed)
                out[te_idx, si] = bst.predict(Xte)
                if var == "main" and m.end_time >= LAST_PRED - pd.Timedelta(days=31):
                    g = pd.Series(bst.feature_importance("gain"), index=cols)
                    imp_gain[seed] = g
                    if seed == seeds[0]:
                        sample = Xtr[np.random.default_rng(0).choice(len(Xtr), min(20000, len(Xtr)), replace=False)]
                        contrib = bst.predict(sample, pred_contrib=True)[:, :-1]
                        pd.Series(np.abs(contrib).mean(axis=0), index=cols).to_csv(os.path.join(OUT, "shap_mean_abs.csv"), header=["shap"])
            print(f"  {var} {m} trained on {len(tr_idx):,} rows ({time.time() - t0:.0f}s)", flush=True)
        for si, seed in enumerate(seeds):
            preds[f"{var}_s{seed}"] = out[:, si]
    if imp_gain:
        pd.DataFrame(imp_gain).mean(axis=1).sort_values(ascending=False).to_csv(os.path.join(OUT, "gain_importance.csv"), header=["gain"])
    preds.to_parquet(PRED, index=False)
    print(f"train done ({time.time() - t0:.0f}s)")


# ---------------------------------------------------------------- evaluation

def month_block_ci(per_day: pd.DataFrame, value: str, weight: str, rng, level=0.95):
    """월 단위 블록 부트스트랩: 월별 (가중합, 가중치)를 다시 뽑아 비율을 계산"""
    per_day = per_day.assign(month=per_day.date.dt.to_period("M"))
    agg = per_day.groupby("month").apply(lambda g: pd.Series({"s": (g[value] * g[weight]).sum(), "w": g[weight].sum()}))
    s, w = agg.s.to_numpy(), agg.w.to_numpy()
    k = len(s)
    idx = rng.integers(0, k, size=(BOOT, k))
    boot = s[idx].sum(1) / w[idx].sum(1)
    a = (1 - level) / 2
    return float(np.quantile(boot, a)), float(np.quantile(boot, 1 - a))



def tail_stats(net):
    """(상·하위 1% 에서 자른 평균, 수익 상위 10건을 뺀 평균), % 단위"""
    if len(net) == 0:
        return (np.nan, np.nan)
    lo, hi = np.quantile(net, [0.01, 0.99])
    trimmed = np.clip(net, lo, hi).mean() * 100
    ex10 = np.sort(net)[:-10].mean() * 100 if len(net) > 10 else np.nan
    return (trimmed, ex10)


def pick_nodup(ds, ordered_days, n):
    """날짜 순으로 후보를 차례대로 보며 하루 n 종목까지 고른다. 같은 종목의 앞 거래가 그날 종가 때까지 안 끝났으면
    (청산일 > 그날) 건너뛴다. 그날 시가·장중에 청산된 종목은 종가에 다시 살 수 있다."""
    code = ds.code.to_numpy()
    cal = ds.cal.to_numpy()
    exit_cal = ds.exit_cal.to_numpy()
    busy, picks = {}, []
    for rows in ordered_days:
        today = cal[rows[0]]
        k = 0
        for i in rows:
            c = code[i]
            if busy.get(c, -1) > today:
                continue
            picks.append(i)
            busy[c] = exit_cal[i]
            k += 1
            if k == n:
                break
    return np.array(picks, int)


def evaluate():
    ds = pd.read_parquet(DATASET)
    pr = pd.read_parquet(PRED)
    ds = ds.merge(pr.drop(columns="date"), on="g")
    ds = ds[(ds.date >= FIRST_PRED) & (ds.date <= LAST_PRED)].reset_index(drop=True)
    g = s4.SimArrays(np.load(os.path.join(OUT, "sim.npz")))
    # 실제 규칙(60일 상한 없음)으로 다시 계산
    T = ds.g.to_numpy()
    stop = g.c[T] - s4.STOP_ATR * g.atr20[T]
    sim = sb.simulate(g, T, stop, np.full(len(T), np.nan), "full", want_legs=True)
    ds["net_real"] = sim["net"]
    ds["hold_real"] = sim["k"]
    ds["reason_real"] = sim["reason"]
    ds["gross_real"] = sim["gross"]
    k = pd.read_parquet(sd.KOSPI).sort_values("timestamp").set_index("timestamp").close
    below = (k < k.rolling(120).mean())
    prev_below = below.shift(1).reindex(ds.date.unique()).fillna(False)
    ds["filter_ok"] = ~ds.date.map(prev_below).astype(bool)
    # 개발 구간 끝: 2025-12-30 종가까지 청산되지 않은(2026년으로 넘어가는) 거래 중 진입일이 가장 이른 날을 마지막 진입일로,
    # 그날부터의 신호는 평가에서 뺀다. 남은 거래는 모두 2025년 안에 청산되므로 2026년 가격은 쓰이지 않는다.
    # 거래정지로 묶인 거래: 개발 구간 마지막 날까지 열려 있고 그날 거래량이 0 (팔 수 없음). 마지막 진입일 계산에서는 빼고 평가에는 남긴다.
    ds["halt_stuck"] = (ds.reason_real == sb.R_END).to_numpy() & (g.vol[g.dev_last[T]] == 0)
    crossing = ds[ds.filter_ok & (ds.reason_real == sb.R_END) & ~ds.halt_stuck]
    last_entry = crossing.date.min() if len(crossing) else None
    cut_info = {"last_entry": last_entry, "dropped_rows": 0, "dropped_days": 0}
    if last_entry is not None:
        drop = (ds.date >= last_entry).to_numpy()
        cut_info.update(dropped_rows=int((drop & ds.filter_ok.to_numpy()).sum()), dropped_days=int(ds.loc[drop, "date"].nunique()))
        ds = ds[~drop].reset_index(drop=True)
        sim = {k: (v[~drop] if isinstance(v, np.ndarray) and len(v) == len(drop) else v) for k, v in sim.items()}
    assert last_entry is None or not (ds.filter_ok & (ds.reason_real == sb.R_END) & ~ds.halt_stuck).any()
    # 판정은 비관적 평가 (정지에 묶인 거래 = -100%), 낙관적 평가(정지 직전 가격)는 나란히 보고만 한다
    ds["net_opt"] = ds.net_real
    ds["net_real"] = np.where(ds.halt_stuck, -1.0, ds.net_real)
    p1_opt = sim["p1"].copy()
    sim["p1"] = np.where(ds.halt_stuck.to_numpy(), 0.0, sim["p1"])
    print(f"dev-end cut: last entry {last_entry.date() if last_entry is not None else None}, "
          f"dropped {cut_info['dropped_rows']} filtered signal rows over {cut_info['dropped_days']} days", flush=True)
    rng = np.random.default_rng(42)
    ds["exit_cal"] = g.cal[sim["j1"]]
    ds["cal"] = g.cal[ds.g.to_numpy()]
    f_all = ds[ds.filter_ok]
    # 같은 날 대상 안에서의 3분위 (생존자 편향 진단용): 거래대금, 직전 60일 수익률
    ds["tert_amt"] = (ds.groupby("date").log_amt20.rank(pct=True) * 3).clip(upper=2.999).astype(int)
    ds["tert_ret60"] = (ds.groupby("date").ret_60.rank(pct=True) * 3).clip(upper=2.999).astype(int)

    # 대조 1: 같은 날 무작위 3종목, 200회. 같은 종목 거래가 아직 안 끝났으면 건너뛰고 다음 후보 (모델과 같은 방식)
    fi = np.flatnonzero(ds.filter_ok.to_numpy())
    day_rows = [fi[ds.date.to_numpy()[fi] == d] for d in np.sort(ds.date[ds.filter_ok].unique())]
    rand_day = np.zeros((N_RAND_REPS, len(day_rows)))
    rand_n = np.zeros((N_RAND_REPS, len(day_rows)))
    rand_picks = []
    for rep in range(N_RAND_REPS):
        picks = pick_nodup(ds, [rng.permutation(r) for r in day_rows], TOP_N)
        rand_picks.append(picks)
        pdf = ds.loc[picks, ["date", "net_real"]]
        by = pdf.groupby("date").net_real.agg(["mean", "size"])
        rand_day[rep] = by["mean"].reindex(ds.date.to_numpy()[[r[0] for r in day_rows]]).to_numpy()
        rand_n[rep] = by["size"].reindex(ds.date.to_numpy()[[r[0] for r in day_rows]]).fillna(0).to_numpy()
    day_dates = pd.DatetimeIndex(ds.date.to_numpy()[[r[0] for r in day_rows]])
    rand_mean_day = pd.Series(np.nanmean(rand_day, axis=0), index=day_dates)
    rand_pool = ds.loc[np.concatenate(rand_picks)]
    rand_exp_reps = np.array([ds.loc[p, "net_real"].mean() * 100 for p in rand_picks])
    rand_tail = np.array([tail_stats(ds.loc[p, "net_real"].to_numpy()) for p in rand_picks])
    rand_info = {"exp_opt": float(np.mean([ds.loc[p, "net_opt"].mean() * 100 for p in rand_picks])),
                 "halt_per_rep": float(np.mean([ds.loc[p, "halt_stuck"].sum() for p in rand_picks])),
                 "trim1": float(rand_tail[:, 0].mean()), "ex_top10": float(rand_tail[:, 1].mean())}
    print(f"random (no duplicate entries) x{N_RAND_REPS}: mean {rand_exp_reps.mean():+.3f}% "
          f"[{np.quantile(rand_exp_reps, .05):+.3f}, {np.quantile(rand_exp_reps, .95):+.3f}]", flush=True)

    variants = {"main": [c for c in pr.columns if c.startswith("main_")]}
    variants["main2"] = variants["main"][:2]
    for v in ("shuffle", "no_sr", "no_flow"):
        variants[v] = [c for c in pr.columns if c.startswith(v + "_")]
    results, picks_by_variant = {}, {}
    for v, cols in variants.items():
        if not cols:
            continue
        score = ds[cols].mean(axis=1).to_numpy()
        ordered = [r[np.argsort(-score[r], kind="stable")] for r in day_rows]
        picks = pick_nodup(ds, ordered, TOP_N)
        picks10 = pick_nodup(ds, ordered, 10)
        picks_by_variant[v] = picks
        t3 = ds.loc[picks]
        top = t3.groupby("date").net_real.agg(["mean", "size"]).rename(columns={"mean": "top", "size": "n"})
        pdays = top.join(rand_mean_day.rename("rand")).reset_index().rename(columns={"index": "date"})
        pdays["diff"] = pdays.top - pdays.rand
        exp = t3.net_real.mean() * 100
        rnd = (pdays.rand * pdays.n).sum() / pdays.n.sum() * 100
        diff = exp - rnd
        lo, hi = month_block_ci(pdays.assign(diff=pdays["diff"] * 100), "diff", "n", rng)
        d = ds.assign(score=score)
        ic = d.groupby("date").apply(lambda x: x.score.rank().corr(x.y.rank()) if len(x) > 5 else np.nan).rename("ic").reset_index()
        ic["one"] = 1.0
        ic_lo, ic_hi = month_block_ci(ic.dropna(), "ic", "one", rng)
        yearly = pdays.groupby(pdays.date.dt.year).apply(lambda x: ((x.top - x.rand) * x.n).sum() / x.n.sum() * 100)
        dec = d[d.filter_ok].assign(decile=lambda x: (x.groupby("date").score.rank(pct=True) * 10).clip(upper=9.999).astype(int))
        gross_mean, net_mean = t3.gross_real.mean() * 100, exp
        turnover = {"hold_mean": float(t3.hold_real.mean()), "hold_median": float(t3.hold_real.median()),
                    "trades_per_year": float(len(t3) / t3.date.dt.year.nunique()),
                    "cost_per_trade_pct": gross_mean - net_mean, "gross_per_trade_pct": gross_mean,
                    "cost_share_of_gross": (gross_mean - net_mean) / gross_mean if gross_mean > 0 else np.nan,
                    "exit_within_3d_pct": float((t3.hold_real <= 3).mean() * 100)}
        tert = {}
        for col, name in (("tert_amt", "거래대금"), ("tert_ret60", "직전60일수익률")):
            mt = t3.groupby(col).net_real.agg(["mean", "size"])
            rt = rand_pool.groupby(col).net_real.mean()
            tert[name] = {int(k): {"model": mt.loc[k, "mean"] * 100, "n": int(mt.loc[k, "size"]),
                                   "random": rt.get(k, np.nan) * 100, "diff": (mt.loc[k, "mean"] - rt.get(k, np.nan)) * 100}
                          for k in mt.index}
        results[v] = {
            "signals": len(t3), "days": len(pdays), "exp_top3": exp, "exp_random": rnd, "diff": diff,
            "diff_ci": (lo, hi), "ic_mean": ic.ic.mean(), "ic_ci": (ic_lo, ic_hi),
            "yearly_diff": yearly.round(3).to_dict(), "exp_top10": ds.loc[picks10, "net_real"].mean() * 100,
            "exp_random_aligned": f_all[f_all.aligned == 1].groupby("date").net_real.mean().mean() * 100,
            "decile_net": dec.groupby("decile").net_real.mean().mul(100).round(3).to_dict(),
            "turnover": turnover, "tertiles": tert,
            "exp_top3_opt": t3.net_opt.mean() * 100, "halt_n": int(t3.halt_stuck.sum()),
            "trim1": tail_stats(t3.net_real.to_numpy())[0], "ex_top10": tail_stats(t3.net_real.to_numpy())[1],
        }
        print(f"[{v}] top3 {exp:+.3f}% vs random {rnd:+.3f}% -> diff {diff:+.3f}%p [{lo:+.3f}, {hi:+.3f}] | IC {ic.ic.mean():+.4f} "
              f"[{ic_lo:+.4f}, {ic_hi:+.4f}] | yearly {yearly.round(2).to_dict()}", flush=True)
    results["_random"] = {"exp_reps_mean": rand_exp_reps.mean(), "exp_reps_p05": np.quantile(rand_exp_reps, .05),
                          "exp_reps_p95": np.quantile(rand_exp_reps, .95),
                          "turnover_hold_mean": float(rand_pool.hold_real.mean()),
                          "exit_within_3d_pct": float((rand_pool.hold_real <= 3).mean() * 100), **rand_info}

    m = results["main"]
    rr_mean, rr_p05, rr_p95 = rand_exp_reps.mean(), np.quantile(rand_exp_reps, .05), np.quantile(rand_exp_reps, .95)
    verdict = {
        "(a) 순기대값 > 0": m["exp_top3"] > 0,
        "(b) 무작위 200회 평균 +0.5%p 이상, 200회 분포 95% 지점 초과": m["exp_top3"] >= rr_mean + 0.5 and m["exp_top3"] > rr_p95,
        "(c) IC 평균 > 0, 95% 범위 > 0": m["ic_mean"] > 0 and m["ic_ci"][0] > 0,
        "(d) 4년 중 3년 이상 차이 플러스": sum(v > 0 for v in m["yearly_diff"].values()) >= 3,
        "(e) 정답 섞기: IC 95% 범위가 0 포함, 상위 3 이 무작위 200회 5~95% 안": ("shuffle" in results
            and results["shuffle"]["ic_ci"][0] <= 0 <= results["shuffle"]["ic_ci"][1]
            and rr_p05 <= results["shuffle"]["exp_top3"] <= rr_p95),
    }
    dmain = ds.assign(score=ds[variants["main"]].mean(axis=1))
    port = portfolio_by_score(g, dmain, sim)
    port["optimistic"] = portfolio_by_score(g, dmain, dict(sim, p1=p1_opt))
    bench = benchmarks(g)
    write_report(results, verdict, port, bench, ds, cut_info)


def portfolio_by_score(g, d, sim):
    """자리가 비면 그날 점수 순으로 채우고, 이미 보유 중인 종목은 건너뛴다 (결정적, 1회)."""
    d = d[d.filter_ok].sort_values(["date", "score"], ascending=[True, False])
    j1, p1 = sim["j1"], sim["p1"]
    cost = sim["cost"]
    code_of = d.code.to_numpy()
    cand_by_day = {k: v.index.to_numpy() for k, v in d.groupby(d.date)}
    days = g.cal_dates[(g.cal_dates >= FIRST_PRED) & (g.cal_dates <= LAST_PRED)]
    cash, pos, eq, n, bought = float(sb.START_CAPITAL), {}, [], 0, []
    prev = None
    exit_cal = g.cal[j1]
    gg = d.g
    for day in days:
        if prev is not None:
            cash *= (1 + sb.CASH_RATE[day.year]) ** ((day - prev).days / 365)
        ci = g.cal_dates.get_loc(day)
        for t in list(pos):
            if exit_cal[t] == ci:
                cash += pos[t][0] * p1[t]
                del pos[t]
        mv = 0.0
        for t, (sh, code, T0) in pos.items():
            mv += sh * _close_on(g, T0, ci)
        cand = cand_by_day.get(day)
        if cand is not None and len(pos) < sb.MAX_POSITIONS:
            held = {v[1] for v in pos.values()}
            equity = cash + mv
            for t in cand:
                if len(pos) >= sb.MAX_POSITIONS:
                    break
                if code_of[d.index.get_loc(t)] in held:
                    continue
                sh = math.floor(min(equity * sb.POSITION_FRAC, cash) / cost[t])
                if sh <= 0:
                    continue
                cash -= sh * cost[t]
                pos[t] = (sh, code_of[d.index.get_loc(t)], int(gg.loc[t]))
                bought.append((t, sh))
                held.add(pos[t][1])
                mv += sh * g.c[int(gg.loc[t])]
                n += 1
        eq.append(cash + mv)
        prev = day
    e = pd.Series(eq, index=days)
    yearly, base = {}, sb.START_CAPITAL
    for y, s in e.groupby(e.index.year):
        yearly[y] = (s.iloc[-1] / base - 1) * 100
        base = s.iloc[-1]
    yrs = (e.index[-1] - e.index[0]).days / 365
    tb = np.array([t for t, _ in bought], int)
    shb = np.array([x for _, x in bought], float)
    entry_px = g.c[d.g.to_numpy()[[d.index.get_loc(t) for t in tb]]] if len(tb) else np.array([])
    j1t = j1[tb] if len(tb) else np.array([], int)
    exit_gross = p1[tb] / ((1 - sb.SLIPPAGE) * (1 - sb.COMMISSION - g.tax[j1t])) if len(tb) else np.array([])
    cost_won = (shb * (cost[tb] - entry_px) + shb * (exit_gross - p1[tb])).sum() if len(tb) else 0.0
    gross_won = (shb * (exit_gross - entry_px)).sum() if len(tb) else 0.0
    holds = sim["k"][tb] if len(tb) else np.array([])
    turnover = {"hold_mean": float(holds.mean()) if len(holds) else np.nan, "hold_median": float(np.median(holds)) if len(holds) else np.nan,
                "trades_per_year": n / 4, "cost_won": float(cost_won), "gross_pnl_won": float(gross_won),
                "cost_share_of_gross": float(cost_won / gross_won) if gross_won > 0 else np.nan,
                "exit_within_3d_pct": float((holds <= 3).mean() * 100) if len(holds) else np.nan,
                "halt_stuck_n": int(d.loc[tb, "halt_stuck"].sum()) if len(tb) else 0}
    return {"turnover": turnover, "total": (e.iloc[-1] / sb.START_CAPITAL - 1) * 100, "cagr": ((e.iloc[-1] / sb.START_CAPITAL) ** (1 / yrs) - 1) * 100,
            "mdd": ((e / e.cummax()) - 1).min() * 100, "trades_per_month": n / 48, "yearly": yearly}


def _close_on(g, T0, ci):
    j = T0
    while j + 1 < len(g.c) and g.sid[j + 1] == g.sid[T0] and g.cal[j + 1] <= ci:
        j += 1
    return g.c[j]


def benchmarks(g):
    k = pd.read_parquet(sd.KOSPI).sort_values("timestamp").set_index("timestamp").close
    days = k.index[(k.index >= FIRST_PRED) & (k.index <= LAST_PRED)]
    kp = k.loc[days]
    out = {"코스피 보유": (kp.iloc[-1] / kp.iloc[0] - 1) * 100}
    below_prev = (k < k.rolling(120).mean()).shift(1).fillna(False)
    eq, inside, prev = 1.0, False, None
    for day in days:
        if prev is not None:
            r = k[day] / k[prev] - 1
            eq *= (1 + r) if inside else (1 + sb.CASH_RATE[day.year]) ** ((day - prev).days / 365)
        want = not below_prev[day]
        if want != inside:
            eq *= 1 - (sb.COMMISSION + sb.SLIPPAGE)
            inside = want
        prev = day
    out["코스피 + 120일선 필터 (ETF 비용)"] = (eq - 1) * 100
    dep = 1.0
    for a, b in zip(days[:-1], days[1:]):
        dep *= (1 + sb.CASH_RATE[b.year]) ** ((b - a).days / 365)
    out["예금 (연도별 금리)"] = (dep - 1) * 100
    return out


def write_report(results, verdict, port, bench, ds, cut_info):
    p = os.path.join(OUT, "summary.md")
    f2 = lambda x: f"{x:+.3f}"
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 4단계 개발 구간 결과 (예측 2022-01 ~ 2025-12)\n\n")
        f.write("평가는 실제 규칙(라벨용 60일 상한 없음)으로 다시 계산한 순수익률. 시장 필터: 진입일 전날 코스피 종가가 120일선 아래면 진입 안 함.\n\n")
        le = cut_info["last_entry"]
        f.write(f"개발 구간 마지막 진입일: {le.date() if le is not None else '없음'} (2026년으로 넘어가는 거래 중 가장 이른 진입일). "
                f"그날부터 빠진 신호: 시장 필터 통과 대상 {cut_info['dropped_rows']}건, {cut_info['dropped_days']}거래일.\n\n")
        f.write("판정은 비관적 평가 (거래정지로 묶인 거래 = -100%). 월 단위 블록 부트스트랩 구간은 참고로만 보고한다.\n\n")
        f.write("## 판정\n\n" + "\n".join(f"- {'O' if ok else 'X'} {k}" for k, ok in verdict.items()))
        f.write(f"\n\n**{'통과' if all(verdict.values()) else '통과하지 못함 -> 검증 구간 실행하지 않음'}**\n\n")
        f.write("## 신호 단위 (상위 3)\n\n| 모델 | 신호 | 상위3 | 같은 날 무작위 | 차이 [95%] | IC [95%] | 연도별 차이 | 상위10 | 정배열 무작위 |\n|---|---|---|---|---|---|---|---|---|\n")
        for v, r in results.items():
            if v.startswith("_"):
                continue
            f.write(f"| {v} | {r['signals']} | {f2(r['exp_top3'])} | {f2(r['exp_random'])} | {f2(r['diff'])} [{f2(r['diff_ci'][0])}, {f2(r['diff_ci'][1])}] | "
                    f"{r['ic_mean']:+.4f} [{r['ic_ci'][0]:+.4f}, {r['ic_ci'][1]:+.4f}] | {r['yearly_diff']} | {f2(r['exp_top10'])} | {f2(r['exp_random_aligned'])} |\n")
        f.write(f"\n점수 10분위별 순수익률 (본 모델, %): {results['main']['decile_net']}\n\n")
        rr = results["_random"]
        mm = results["main"]
        f.write(f"거래정지로 묶인 거래: 본 모델 상위 3 {mm['halt_n']}건, 무작위 1회당 평균 {rr['halt_per_rep']:.1f}건, 포트폴리오 {port['turnover']['halt_stuck_n']}건\n\n")
        f.write(f"낙관적 평가(정지 직전 가격): 본 모델 상위 3 {mm['exp_top3_opt']:+.3f}%, 무작위 평균 {rr['exp_opt']:+.3f}%, "
                f"포트폴리오 누적 {port['optimistic']['total']:+.2f}%\n\n")
        f.write("꼬리 영향 (판정에 안 씀, 비관적 평가 기준):\n\n| | 순기대값 | 상·하위 1% 자른 평균 | 수익 상위 10건 뺀 평균 |\n|---|---|---|---|\n")
        f.write(f"| 본 모델 상위 3 | {mm['exp_top3']:+.3f}% | {mm['trim1']:+.3f}% | {mm['ex_top10']:+.3f}% |\n")
        f.write(f"| 무작위 (200회 평균) | {rr['exp_reps_mean']:+.3f}% | {rr['trim1']:+.3f}% | {rr['ex_top10']:+.3f}% |\n\n")
        f.write(f"대조 1 (같은 날 무작위 3종목, 중복 진입 없음, 200회): 순기대값 평균 {rr['exp_reps_mean']:+.3f}% "
                f"[5~95% {rr['exp_reps_p05']:+.3f} ~ {rr['exp_reps_p95']:+.3f}], 보유 평균 {rr['turnover_hold_mean']:.1f}일, 3일 안 청산 {rr['exit_within_3d_pct']:.1f}%\n\n")
        f.write("생존자 편향 진단 (본 모델 상위 3 vs 무작위, 같은 날 대상 안 3분위, 0 = 낮음):\n\n| 기준 | 3분위 | 모델 | 건수 | 무작위 | 차이 |\n|---|---|---|---|---|---|\n")
        for name, tb in results["main"]["tertiles"].items():
            for k, x in sorted(tb.items()):
                f.write(f"| {name} | {k} | {x['model']:+.3f}% | {x['n']} | {x['random']:+.3f}% | {x['diff']:+.3f}%p |\n")
        f.write("\n")
        tv = results["main"]["turnover"]
        f.write(f"회전율 (본 모델 상위 3, 신호 단위): 보유 평균 {tv['hold_mean']:.1f}일 / 중앙값 {tv['hold_median']:.0f}일, 1년 평균 {tv['trades_per_year']:.0f}건, "
                f"1회 비용 {tv['cost_per_trade_pct']:.3f}%p (비용 전 수익 {tv['gross_per_trade_pct']:+.3f}%, 비용 비율 {tv['cost_share_of_gross']:.2f}), "
                f"3일 안 청산 {tv['exit_within_3d_pct']:.1f}%\n\n")
        if "main2" in results:
            for v in ("no_sr", "no_flow"):
                if v in results:
                    f.write(f"- 특징 기여 {v}: 차이 {results[v]['diff'] - results['main2']['diff']:+.3f}%p, IC {results[v]['ic_mean'] - results['main2']['ic_mean']:+.4f} (본 모델 처음 2개 시드 대비)\n")
        f.write(f"\n## 포트폴리오 (점수 순, 1회)\n\n누적 {port['total']:+.2f}%, CAGR {port['cagr']:+.2f}%, MDD {port['mdd']:+.2f}%, 월평균 매매 {port['trades_per_month']:.2f}, "
                f"연도별 {({k: round(v, 2) for k, v in port['yearly'].items()})}\n\n")
        pt = port["turnover"]
        f.write(f"회전율 (포트폴리오): 보유 평균 {pt['hold_mean']:.1f}일 / 중앙값 {pt['hold_median']:.0f}일, 1년 평균 {pt['trades_per_year']:.0f}건, "
                f"비용 합계 {pt['cost_won']:,.0f}원 / 비용 전 손익 {pt['gross_pnl_won']:,.0f}원 (비율 {pt['cost_share_of_gross']:.2f}), "
                f"3일 안 청산 {pt['exit_within_3d_pct']:.1f}%. 보유 중 갈아타기 없음 (청산은 손절·추세 청산만)\n\n")
        f.write("## 대조군 (2022-01 ~ 2025-12)\n\n" + "\n".join(f"- {k}: {v:+.2f}%" for k, v in bench.items()) + "\n")
        r = pd.Series(ds.reason_real).map(sb.REASON).value_counts(normalize=True).mul(100).round(1).to_dict()
        f.write(f"\n실제 규칙 청산 사유 (대상 전체): {r}, 3일 안 추세 청산 {((ds.reason_real == sb.R_TREND) & (ds.hold_real <= 3)).mean() * 100:.1f}%, "
                f"보유 중앙값 {ds.hold_real.median():.0f}일 / 90% {ds.hold_real.quantile(.9):.0f}일\n")
    print(open(p, encoding="utf-8").read())


if __name__ == "__main__":
    {"timing": timing, "train": train, "evaluate": evaluate}[sys.argv[1]]()
