"""
5단계: LightGBM 매월 재학습 (R 라벨) + 개발 구간 평가·판정 (명세: backtest_swing/STAGE5_SPEC.md, 세부는 13장)

  python backtest_swing/stage5_model.py train      # 본 모델(시드 5) + 7b 정답 섞기(순위 라벨, 섞기 시드 10개) -> 예측, 중요도
  python backtest_swing/stage5_model.py evaluate   # 판정 (a)~(f), 대조군, 포트폴리오 P1~P3 와 각 무작위 200회 -> summary.md

데이터셋은 2025-12-31 까지만 있다 (검증 구간 2026 은 이 스크립트로 실행하지 않는다).
"""
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage3_backtest as sb  # noqa: E402
import stage3_data as sd  # noqa: E402
import stage4_diag as dg  # noqa: E402  (daily_ic)
import stage4_features as s4  # noqa: E402
import stage4_model as sm  # noqa: E402  (months, fit, PARAMS, pick_nodup, month_block_ci, tail_stats, benchmarks)
import stage5_features as s5  # noqa: E402

OUT = s5.OUT
DATASET = os.path.join(OUT, "dataset.parquet")
SIM = os.path.join(OUT, "sim.npz")
PRED = os.path.join(OUT, "predictions.parquet")
SUMMARY = os.path.join(OUT, "summary.md")
SEEDS_MAIN = [1, 2, 3, 4, 5]
SHUF_SEEDS, SHUF_MODEL_SEED = list(range(1001, 1011)), 1
T975_DF9 = 2.262157  # t 분포 0.975 분위, 자유도 9
REASON5 = {**sb.REASON, sb.R_MAXHOLD: "60일 상한"}
CAPITAL = 30_000_000
PORT_REPS, PORT_SEED = 200, 7
PORTS = {"P3 (본안: 손절 1회 = 총자산 1%, 종목당 최대 20%, 최대 6종목)": dict(frac=0.20, max_pos=6, risk_frac=0.01),
         "P1 (20% x 3)": dict(frac=0.20, max_pos=3, risk_frac=None),
         "P2 (20% x 5)": dict(frac=0.20, max_pos=5, risk_frac=None)}


def feature_cols(ds):
    return [c for c in ds.columns if c not in s5.NON_FEATURES and c != "is_kospi"]


def rank_label(ds):
    """학습 라벨 y_clip 을 날짜마다 순위 백분위로: (순위(동점 평균) - 0.5) / 그날 종목 수 - 0.5"""
    grp = ds.groupby("date").y_clip
    return ((grp.rank(method="average") - 0.5) / grp.transform("size") - 0.5).to_numpy()


# ---------------------------------------------------------------- train

def train(months_limit=None):
    ds = pd.read_parquet(DATASET)
    cols = feature_cols(ds)
    assert len(cols) == 41 and "is_kospi" not in cols, cols
    ms = sm.months(ds)[:months_limit] if months_limit else sm.months(ds)
    rl = rank_label(ds)
    shuf = {s: sm.shuffled_labels(ds.assign(y_clip=rl), s) for s in SHUF_SEEDS}
    preds = pd.DataFrame({"g": ds.g, "date": ds.date})
    out_main = np.full((len(ds), len(SEEDS_MAIN)), np.nan, np.float32)
    out_shuf = np.full((len(ds), len(SHUF_SEEDS)), np.nan, np.float32)
    gains, shaps = [], []
    y = ds.y_clip.to_numpy()
    t0 = time.time()
    print(f"features {len(cols)}, months {len(ms)}, fits {len(ms) * (len(SEEDS_MAIN) + len(SHUF_SEEDS))}", flush=True)
    for m, first_day, cutoff in ms:
        tr_idx = np.flatnonzero((ds.date <= cutoff).to_numpy())
        te_idx = np.flatnonzero(((ds.date >= m.start_time) & (ds.date <= m.end_time)).to_numpy())
        Xtr = ds.iloc[tr_idx][cols].to_numpy(np.float32)
        Xte = ds.iloc[te_idx][cols].to_numpy(np.float32)
        for si, seed in enumerate(SEEDS_MAIN):
            bst = sm.fit(Xtr, y[tr_idx], seed)
            out_main[te_idx, si] = bst.predict(Xte)
            gains.append(pd.Series(bst.feature_importance("gain"), index=cols))
            shaps.append(pd.Series(np.abs(bst.predict(Xte, pred_contrib=True)[:, :-1]).mean(axis=0), index=cols))
        for si, seed in enumerate(SHUF_SEEDS):
            out_shuf[te_idx, si] = sm.fit(Xtr, shuf[seed][tr_idx], SHUF_MODEL_SEED).predict(Xte)
        print(f"  {m}: trained on {len(tr_idx):,} rows, predicted {len(te_idx):,} ({time.time() - t0:.0f}s)", flush=True)
    for si, seed in enumerate(SEEDS_MAIN):
        preds[f"main_s{seed}"] = out_main[:, si]
    for si, seed in enumerate(SHUF_SEEDS):
        preds[f"shuf_{seed}"] = out_shuf[:, si]
    preds.to_parquet(PRED, index=False)
    pd.DataFrame(gains).mean().sort_values(ascending=False).to_csv(os.path.join(OUT, "importance_gain.csv"), header=["gain"])
    pd.DataFrame(shaps).mean().sort_values(ascending=False).to_csv(os.path.join(OUT, "importance_shap.csv"), header=["shap"])
    print(f"train done ({time.time() - t0:.0f}s)")


# ---------------------------------------------------------------- evaluate helpers

def prepare():
    """4단계 evaluate 와 같은 순서: 실제 규칙 재계산, 전날 코스피 120일선 필터, 개발 구간 끝 자르기, 정지 묶임 -100%"""
    ds = pd.read_parquet(DATASET)
    pr = pd.read_parquet(PRED)
    ds = ds.merge(pr.drop(columns="date"), on="g")
    ds = ds[(ds.date >= sm.FIRST_PRED) & (ds.date <= sm.LAST_PRED)].reset_index(drop=True)
    g = s4.SimArrays(np.load(SIM))
    T = ds.g.to_numpy()
    risk = s4.STOP_ATR * g.atr20[T]
    stop = g.c[T] - risk
    sim = sb.simulate(g, T, stop, np.full(len(T), np.nan), "full", want_legs=True)
    ds["net_real"], ds["hold_real"], ds["reason_real"] = sim["net"], sim["k"], sim["reason"]
    k = pd.read_parquet(sd.KOSPI).sort_values("timestamp").set_index("timestamp").close
    prev_below = (k < k.rolling(120).mean()).shift(1).reindex(ds.date.unique()).fillna(False)
    ds["filter_ok"] = ~ds.date.map(prev_below).astype(bool)
    ds["halt_stuck"] = (ds.reason_real == sb.R_END).to_numpy() & (g.vol[g.dev_last[T]] == 0)
    crossing = ds[ds.filter_ok & (ds.reason_real == sb.R_END) & ~ds.halt_stuck]
    last_entry = crossing.date.min() if len(crossing) else None
    cut = {"last_entry": last_entry, "dropped_rows": 0, "dropped_days": 0}
    if last_entry is not None:
        drop = (ds.date >= last_entry).to_numpy()
        cut.update(dropped_rows=int((drop & ds.filter_ok.to_numpy()).sum()), dropped_days=int(ds.loc[drop, "date"].nunique()))
        ds = ds[~drop].reset_index(drop=True)
        sim = {kk: (v[~drop] if isinstance(v, np.ndarray) and len(v) == len(drop) else v) for kk, v in sim.items()}
        risk, stop = risk[~drop], stop[~drop]
    assert not (ds.filter_ok & (ds.reason_real == sb.R_END) & ~ds.halt_stuck).any()
    ds["net_real"] = np.where(ds.halt_stuck, -1.0, ds.net_real)
    sim["p1"] = np.where(ds.halt_stuck.to_numpy(), 0.0, sim["p1"])
    ds["risk_ps"] = risk
    ds["cost_ps"] = sim["cost"]
    ds["R_real"] = ds.net_real * sim["cost"] / risk
    j1 = np.maximum(sim["j1"], 0)
    px = sim["p1"] / ((1 - sb.SLIPPAGE) * (1 - sb.COMMISSION - g.tax[j1]))
    ds["gap_stop"] = (ds.reason_real == sb.R_STOP).to_numpy() & (px < stop * (1 - 1e-9))
    ds["exit_cal"] = g.cal[sim["j1"]]
    ds["cal"] = g.cal[ds.g.to_numpy()]
    ds["score"] = ds[[f"main_s{s}" for s in SEEDS_MAIN]].mean(axis=1)
    return ds, g, sim, cut


def rule_score(ds):
    """단순 규칙: 시장 필터 통과 대상 안에서 dist_high250 순위가 위쪽 절반이면 10 - atr_pct, 아니면 -atr_pct"""
    s = pd.Series(-np.inf, index=ds.index)
    f = ds[ds.filter_ok]
    near = f.groupby("date").dist_high250.rank(pct=True) > 0.5
    s.loc[f.index] = np.where(near, 10.0, 0.0) - f.atr_pct.to_numpy()
    return s.to_numpy()


def signal_eval(ds, score, day_rows, rand_mean_day):
    order = [r[np.argsort(-score[r], kind="stable")] for r in day_rows]
    picks = sm.pick_nodup(ds, order, sm.TOP_N)
    t3 = ds.loc[picks]
    by = t3.groupby("date").R_real.agg(["mean", "size"]).join(rand_mean_day.rename("rand"))
    by["d"] = by["mean"] - by.rand
    yearly = by.groupby(by.index.year).apply(lambda x: (x.d * x["size"]).sum() / x["size"].sum()).round(3).to_dict()
    return picks, t3, {int(k): v for k, v in yearly.items()}


def loss_stats(t):
    st = t.reason_real == sb.R_STOP
    gp = t[t.gap_stop]
    return {"n": len(t), "stop_n": int(st.sum()), "gap_n": len(gp),
            "gap_of_stop": len(gp) / max(int(st.sum()), 1) * 100, "gap_of_all": len(gp) / max(len(t), 1) * 100,
            "gap_R_median": gp.R_real.median() if len(gp) else np.nan,
            "gap_R_p05": gp.R_real.quantile(.05) if len(gp) else np.nan, "gap_R_min": gp.R_real.min() if len(gp) else np.nan,
            "lt2_n": int((t.R_real < -2).sum()), "lt2_pct": (t.R_real < -2).mean() * 100}


# ---------------------------------------------------------------- portfolio

def port_run(g, d, sim, frac, max_pos, risk_frac, capital=CAPITAL):
    """자리가 비면 그날 점수 순으로 채우고 보유 종목은 건너뛴다. risk_frac 이 있으면 수량 = 내림(총자산 x risk_frac / 1주당 위험),
    종목당 최대 총자산 x frac, 현금이 모자라면 살 수 있는 만큼."""
    d = d[d.filter_ok].sort_values(["date", "score"], ascending=[True, False])
    p1, cost, risk = sim["p1"], d.cost_ps, d.risk_ps
    code_arr, g_arr, exit_cal = d.code.to_dict(), d.g.to_dict(), d.exit_cal.to_dict()
    cost_d, risk_d, p1_d = cost.to_dict(), risk.to_dict(), {t: p1[t] for t in d.index}
    cand_by_day = {k: v.index.to_numpy() for k, v in d.groupby(d.date)}
    days = g.cal_dates[(g.cal_dates >= sm.FIRST_PRED) & (g.cal_dates <= sm.LAST_PRED)]
    cash, pos, eq, n, prev = float(capital), {}, [], 0, None
    losses = []
    for day in days:
        if prev is not None:
            cash *= (1 + sb.CASH_RATE[day.year]) ** ((day - prev).days / 365)
        ci = g.cal_dates.get_loc(day)
        for t in list(pos):
            if exit_cal[t] == ci:
                sh, _, _, eq0 = pos[t]
                cash += sh * p1_d[t]
                losses.append(sh * (cost_d[t] - p1_d[t]) / eq0)
                del pos[t]
        mv = sum(sh * sm._close_on(g, T0, ci) for sh, _, T0, _ in pos.values())
        cand = cand_by_day.get(day)
        if cand is not None and len(pos) < max_pos:
            held = {v[1] for v in pos.values()}
            equity = cash + mv
            for t in cand:
                if len(pos) >= max_pos:
                    break
                if code_arr[t] in held:
                    continue
                cap_sh = min(equity * frac, cash) / cost_d[t]
                sh = math.floor(min(cap_sh, equity * risk_frac / risk_d[t]) if risk_frac else cap_sh)
                if sh <= 0:
                    continue
                cash -= sh * cost_d[t]
                pos[t] = (sh, code_arr[t], int(g_arr[t]), equity)
                held.add(code_arr[t])
                mv += sh * g.c[int(g_arr[t])]
                n += 1
        eq.append(cash + mv)
        prev = day
    e = pd.Series(eq, index=days)
    yearly, base = {}, capital
    for y, s in e.groupby(e.index.year):
        yearly[int(y)] = (s.iloc[-1] / base - 1) * 100
        base = s.iloc[-1]
    yrs = (e.index[-1] - e.index[0]).days / 365
    losses = np.array(losses)
    return {"total": (e.iloc[-1] / capital - 1) * 100, "cagr": ((e.iloc[-1] / capital) ** (1 / yrs) - 1) * 100,
            "mdd": ((e / e.cummax()) - 1).min() * 100, "trades": n, "trades_per_month": n / 48, "yearly": yearly,
            "loss_gt1": int((losses > 0.01).sum()), "loss_gt2": int((losses > 0.02).sum()), "closed": len(losses)}


_W = {}


def _port_init(d, sim):
    _W["g"] = s4.SimArrays(np.load(SIM))
    _W["d"], _W["sim"] = d, sim


def _port_random(args):
    name, rep = args
    k = list(PORTS).index(name)
    rng = np.random.default_rng([PORT_SEED, k, rep])
    d = _W["d"].assign(score=rng.random(len(_W["d"])))
    return name, port_run(_W["g"], d, _W["sim"], **PORTS[name])["total"]


# ---------------------------------------------------------------- evaluate

def evaluate(port_reps=PORT_REPS):
    t0 = time.time()
    ds, g, sim, cut = prepare()
    rng = np.random.default_rng(42)
    fi = np.flatnonzero(ds.filter_ok.to_numpy())
    day_rows = [fi[ds.date.to_numpy()[fi] == d] for d in np.sort(ds.date[ds.filter_ok].unique())]
    day_dates = pd.DatetimeIndex(ds.date.to_numpy()[[r[0] for r in day_rows]])
    rand_day = np.zeros((sm.N_RAND_REPS, len(day_rows)))
    rand_picks = []
    for rep in range(sm.N_RAND_REPS):
        p = sm.pick_nodup(ds, [rng.permutation(r) for r in day_rows], sm.TOP_N)
        rand_picks.append(p)
        rand_day[rep] = ds.loc[p].groupby("date").R_real.mean().reindex(day_dates).to_numpy()
        if (rep + 1) % 50 == 0:
            print(f"  random signals {rep + 1}/{sm.N_RAND_REPS} ({time.time() - t0:.0f}s)", flush=True)
    rand_mean_day = pd.Series(np.nanmean(rand_day, axis=0), index=day_dates)
    rand_R = np.array([ds.loc[p, "R_real"].mean() for p in rand_picks])
    rand_pct = np.array([ds.loc[p, "net_real"].mean() * 100 for p in rand_picks])
    r_mean, r05, r95 = rand_R.mean(), np.quantile(rand_R, .05), np.quantile(rand_R, .95)
    rand_pool = ds.loc[np.concatenate(rand_picks)]
    print(f"prepared, random x{sm.N_RAND_REPS}: mean R {r_mean:+.4f} [{r05:+.4f}, {r95:+.4f}] ({time.time() - t0:.0f}s)", flush=True)

    # 본 모델
    score = ds.score.to_numpy()
    picks, t3, yearly = signal_eval(ds, score, day_rows, rand_mean_day)
    ic = dg.daily_ic(ds, "score", "R")
    ic_lo, ic_hi = sm.month_block_ci(ic.rename("ic").reset_index().assign(one=1.0), "ic", "one", rng)
    exp_R = t3.R_real.mean()

    # 7b 정답 섞기 10개
    shuf = []
    for s in SHUF_SEEDS:
        sc = ds[f"shuf_{s}"].to_numpy()
        p, tt, _ = signal_eval(ds, sc, day_rows, rand_mean_day)
        shuf.append({"seed": s, "ic": dg.daily_ic(ds.assign(sv=sc), "sv", "R").mean(), "top3_R": tt.R_real.mean()})
        print(f"  shuffle model {s} evaluated ({time.time() - t0:.0f}s)", flush=True)
    sh = pd.DataFrame(shuf)
    sh_ic_m, sh_ic_sd = sh.ic.mean(), sh.ic.std(ddof=1)
    sh_lo, sh_hi = sh_ic_m - T975_DF9 * sh_ic_sd / math.sqrt(len(sh)), sh_ic_m + T975_DF9 * sh_ic_sd / math.sqrt(len(sh))
    sh_top = sh.top3_R.mean()

    check7a = open(os.path.join(OUT, "check7a.md"), encoding="utf-8").read()
    verdict = {
        "(a) 상위 3 평균 R > 0": exp_R > 0,
        "(b) 무작위 200회 평균 +0.10R 이상, 200회 분포 95% 지점 초과": exp_R >= r_mean + 0.10 and exp_R > r95,
        "(c) IC 평균 > 0, 95% 범위 > 0": ic.mean() > 0 and ic_lo > 0,
        "(d) 4년 중 3년 이상 차이 플러스": sum(v > 0 for v in yearly.values()) >= 3,
        "(e) 7b 정답 섞기: 10개 IC 의 t 95% 범위가 0 포함, 상위 3 평균 R 의 평균이 무작위 5~95% 안":
            (sh_lo <= 0 <= sh_hi) and (r05 <= sh_top <= r95),
        "(f) 7a 특징 잘라내기 점검 통과": "**결과: 통과**" in check7a,
    }
    print(f"main: top3 R {exp_R:+.4f}, IC {ic.mean():+.4f} [{ic_lo:+.4f}, {ic_hi:+.4f}], yearly {yearly} | "
          f"shuffle IC {sh_ic_m:+.4f} [{sh_lo:+.4f}, {sh_hi:+.4f}], top3 R {sh_top:+.4f} ({time.time() - t0:.0f}s)", flush=True)

    # 참고: 단순 규칙, 분위, 청산, 손실, 꼬리
    rs = rule_score(ds)
    r_picks, r_t3, r_yearly = signal_eval(ds, rs, day_rows, rand_mean_day)
    fo = ds[ds.filter_ok]
    dec = (fo.groupby("date").score.rank(pct=True) * 10).clip(upper=9.999).astype(int)
    decile_R = fo.R_real.groupby(dec).mean().round(3).to_dict()
    bench = sm.benchmarks(g)

    # 포트폴리오 + 무작위 200회
    print(f"reference signals done ({time.time() - t0:.0f}s), model and rule portfolios ...", flush=True)
    ports = {name: port_run(g, ds, sim, **kw) for name, kw in PORTS.items()}
    rule_ports = {name: port_run(g, ds.assign(score=rs), sim, **kw) for name, kw in PORTS.items()}
    print(f"portfolios done ({time.time() - t0:.0f}s), random portfolios x{port_reps} each ...", flush=True)
    dslim = ds[["date", "code", "g", "filter_ok", "cost_ps", "risk_ps", "exit_cal"]]
    simlim = {"p1": sim["p1"]}
    rnd = {name: [] for name in PORTS}
    with ProcessPoolExecutor(max_workers=min(14, os.cpu_count() or 4), initializer=_port_init, initargs=(dslim, simlim)) as ex:
        for name, tot in ex.map(_port_random, [(nm, r) for nm in PORTS for r in range(port_reps)], chunksize=4):
            rnd[name].append(tot)
            if len(rnd[name]) % 50 == 0:
                print(f"  random portfolio {name[:2]} {len(rnd[name])}/{port_reps} ({time.time() - t0:.0f}s)", flush=True)
    print(f"random portfolios done ({time.time() - t0:.0f}s)", flush=True)

    write_report(locals())
    return verdict


def write_report(v):
    ds, t3, rand_pool = v["ds"], v["t3"], v["rand_pool"]
    f3 = lambda x: f"{x:+.3f}"
    L = ["# 5단계 개발 구간 결과 (예측 2022-01 ~ 2025-12, R 라벨)\n",
         "평가는 실제 규칙(라벨용 60일 상한 없음)으로 다시 계산한 R (= 순수익률 x 매수 비용 / (1.7 x ATR20)). "
         "시장 필터: 진입일 전날 코스피 종가가 120일선 아래면 진입 안 함. 정지에 묶인 거래 = 순수익률 -100%.\n",
         f"개발 구간 마지막 진입일: {v['cut']['last_entry'].date()} (그날부터 빠진 신호: 필터 통과 {v['cut']['dropped_rows']}건, "
         f"{v['cut']['dropped_days']}거래일). 특징 41개 (is_kospi 제외).\n",
         "## 판정\n"]
    for k, ok in v["verdict"].items():
        L.append(f"- {'O' if ok else 'X'} {k}")
    allok = all(v["verdict"].values())
    L.append(f"\n**{'개발 구간 통과 -> 검증 구간 실행 여부를 사용자에게 확인' if allok else '통과하지 못함 -> 검증 구간 실행하지 않음'}**\n")
    L.append("## 신호 단위 (상위 3)\n")
    L.append("| | 신호 | 평균 R | 무작위 200회 평균 R [5~95%] | 차이 | IC [95%] | 연도별 차이 (R) | % 순수익률 |\n|---|---|---|---|---|---|---|---|")
    L.append(f"| 본 모델 | {len(t3)} | {f3(v['exp_R'])} | {f3(v['r_mean'])} [{f3(v['r05'])}, {f3(v['r95'])}] | {f3(v['exp_R'] - v['r_mean'])} | "
             f"{v['ic'].mean():+.4f} [{v['ic_lo']:+.4f}, {v['ic_hi']:+.4f}] | {v['yearly']} | {t3.net_real.mean() * 100:+.3f}% "
             f"(무작위 {v['rand_pct'].mean():+.3f}%) |")
    L.append(f"| 단순 규칙 (참고) | {len(v['r_t3'])} | {f3(v['r_t3'].R_real.mean())} | 같음 | {f3(v['r_t3'].R_real.mean() - v['r_mean'])} | - | "
             f"{v['r_yearly']} | {v['r_t3'].net_real.mean() * 100:+.3f}% |")
    L.append(f"\n본 모델 - 단순 규칙: {v['exp_R'] - v['r_t3'].R_real.mean():+.3f}R, 고른 신호가 겹치는 비율 "
             f"{len(set(v['picks']) & set(v['r_picks'])) / len(v['picks']) * 100:.1f}%\n")
    sh = v["sh"]
    L.append("### 7b 정답 섞기 (순위 라벨, 섞기 시드 10개, 모델 시드 1)\n")
    L.append(f"- IC: 10개 평균 {v['sh_ic_m']:+.4f}, t 95% 범위 [{v['sh_lo']:+.4f}, {v['sh_hi']:+.4f}] (표준편차 {v['sh_ic_sd']:.4f})")
    L.append(f"- 상위 3 평균 R 의 10개 평균: {v['sh_top']:+.3f} (무작위 5~95% {v['r05']:+.3f} ~ {v['r95']:+.3f})")
    L.append("- 모델별: " + ", ".join(f"{int(r.seed)}: IC {r.ic:+.4f} / R {r.top3_R:+.3f}" for r in sh.itertuples()) + "\n")
    L.append(f"점수 10분위별 평균 R (시장 필터 통과 대상, 0 = 낮음): {v['decile_R']}\n")
    L.append("### 청산·보유·손실 (실제 규칙)\n")
    for name, t in (("본 모델 상위 3", t3), ("무작위 (200회 합침)", rand_pool), ("단순 규칙 상위 3", v["r_t3"])):
        rr = t.reason_real.map(REASON5).value_counts(normalize=True).mul(100).round(1).to_dict()
        ls = loss_stats(t)
        L.append(f"- **{name}**: 청산 사유 % {rr}, 보유 평균 {t.hold_real.mean():.1f}일 / 중앙값 {t.hold_real.median():.0f}일, "
                 f"3일 안 청산 {(t.hold_real <= 3).mean() * 100:.1f}%")
        L.append(f"  - 손절가보다 낮게 체결된 손절: 손절 {ls['stop_n']}건 중 {ls['gap_n']}건 ({ls['gap_of_stop']:.1f}%, 전체 거래의 "
                 f"{ls['gap_of_all']:.1f}%), 그 거래들의 R 중앙값 {ls['gap_R_median']:+.2f}, 5% 지점 {ls['gap_R_p05']:+.2f}, 최악 {ls['gap_R_min']:+.2f}")
        L.append(f"  - R < -2: {ls['lt2_n']}건 ({ls['lt2_pct']:.2f}%), 정지 묶임 {int(t.halt_stuck.sum())}건")
    tr, ex = sm.tail_stats(t3.R_real.to_numpy())
    rt = np.array([sm.tail_stats(ds.loc[p, "R_real"].to_numpy()) for p in v["rand_picks"]])
    L.append(f"\n꼬리 (R): 본 모델 상하위 1% 자름 {tr / 100:+.3f}, 수익 상위 10건 뺌 {ex / 100:+.3f} | 무작위 평균 {rt[:, 0].mean() / 100:+.3f}, "
             f"{rt[:, 1].mean() / 100:+.3f}")
    top10 = t3.nlargest(10, "R_real")
    L.append("\n수익 상위 10건 (본 모델 상위 3): " + ", ".join(f"{r.code} {r.date.date()} {r.R_real:+.1f}R ({r.net_real * 100:+.0f}%, {r.hold_real}일)"
                                                     for r in top10.itertuples()))
    L.append(f"상위 10건 R 합 / 전체 R 합: {top10.R_real.sum() / t3.R_real.sum() * 100:.0f}%\n" if t3.R_real.sum() > 0 else "\n")
    L.append("## 포트폴리오 (참고, 시작 3,000만 원)\n")
    L.append("| | 본 모델 누적 | CAGR | MDD | 월평균 매매 | 연도별 | 무작위 200회 중앙값 [5~95%] | 모델 백분위 | 단순 규칙 누적 |\n|---|---|---|---|---|---|---|---|---|")
    for name, p in v["ports"].items():
        r = np.array(v["rnd"][name])
        yr = ", ".join(f"{k}: {x:+.1f}" for k, x in p["yearly"].items())
        L.append(f"| {name} | {p['total']:+.1f}% | {p['cagr']:+.1f}% | {p['mdd']:.1f}% | {p['trades_per_month']:.2f} | {yr} | "
                 f"{np.median(r):+.1f}% [{np.quantile(r, .05):+.1f}, {np.quantile(r, .95):+.1f}] | {(r < p['total']).mean() * 100:.1f} | "
                 f"{v['rule_ports'][name]['total']:+.1f}% |")
    p3 = v["ports"][list(v["ports"])[0]]
    L.append(f"\nP3 진입 시 총자산 대비 실제 손실: 1% 초과 {p3['loss_gt1']}건, 2% 초과 {p3['loss_gt2']}건 (청산 {p3['closed']}건)\n")
    L.append("대조군 (2022-01 ~ 2025-12): " + ", ".join(f"{k} {x:+.2f}%" for k, x in v["bench"].items()) + "\n")
    gain = pd.read_csv(os.path.join(OUT, "importance_gain.csv"), index_col=0).gain
    shap = pd.read_csv(os.path.join(OUT, "importance_shap.csv"), index_col=0).shap
    L.append("## 특징 중요도 (본 모델, 월별·시드별 모델 평균)\n")
    L.append("| 순위 | SHAP 평균 절댓값 | 비중 | gain | 비중 |\n|---|---|---|---|---|")
    for i, ((fs, s_), (fg, g_)) in enumerate(zip(shap.head(20).items(), gain.head(20).items()), 1):
        L.append(f"| {i} | {fs} {s_:.4f} | {s_ / shap.sum() * 100:.1f}% | {fg} {g_:,.0f} | {g_ / gain.sum() * 100:.1f}% |")
    with open(SUMMARY, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    {"train": train, "evaluate": evaluate}[sys.argv[1]]()
