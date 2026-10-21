"""
5단계 검증 구간 (명세: backtest_swing/STAGE5_SPEC.md 10장, 13장 "검증 구간 실행 전 확정", "검증 구간 실행 방식")

  python backtest_swing/stage5_validation.py build     # 2020-01-02 ~ 2026-10-16 특징·라벨·시뮬레이션 -> results/stage5_validation/
  python backtest_swing/stage5_validation.py check     # 3a·3b·3c 대조 (어긋나면 종료 코드 1)
  python backtest_swing/stage5_validation.py xcut      # X·마지막 포함 진입일·필터 통과 거래일 수·신호 후보 수 기록 -> xcut.md
  python backtest_swing/stage5_validation.py run       # 재학습·예측 -> 평가·판정 -> summary.md (한 번만)

기존 파일은 고치지 않는다. 기존 함수를 그대로 불러 쓰고, 날짜·경로는 모듈 상수만 바꿔 넘긴다.
"""
import inspect
import json
import os
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage3_backtest as sb  # noqa: E402
import stage3_data as sd  # noqa: E402
import stage4_diag as dg  # noqa: E402
import stage4_features as s4  # noqa: E402
import stage4_model as sm  # noqa: E402
import stage5_features as s5  # noqa: E402
import stage5_model as m  # noqa: E402

DEV_DIR = "backtest_swing/results/stage5"
VAL_DIR = "backtest_swing/results/stage5_validation"
VAL_START, VAL_END = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-10-16")
DEV_FIRST, DEV_LAST = pd.Timestamp("2022-01-01"), pd.Timestamp("2025-12-31")
DEV_LAST_INCLUDED = pd.Timestamp("2025-09-25")
REL_TOL, RANK_TOL = 1e-6, 0.9999
SMOKE_DIR = os.path.join(os.environ.get("TEMP", "."), "stage5_validation_smoke")


def use(folder, first, last, pred=None):
    """stage5_model 이 읽는 데이터셋·시뮬레이션·예측 경로와 평가 기간을 바꾼다 (모듈 상수만)"""
    m.DATASET = os.path.join(folder, "dataset.parquet")
    m.SIM = os.path.join(folder, "sim.npz")
    m.PRED = pred or os.path.join(folder, "predictions.parquet")
    sm.FIRST_PRED, sm.LAST_PRED = first, last


# ---------------------------------------------------------------- 1. 생성

def build():
    s4.DEV_END = VAL_END  # 신호 끝·시뮬레이션 마감일 (dev_last)
    s5.OUT = VAL_DIR
    os.makedirs(VAL_DIR, exist_ok=True)
    s5.build()


# ---------------------------------------------------------------- 학습 반복문 (stage5_model.train 의 본 모델 부분과 같은 코드)

def fit_predict(ds, months, seeds=m.SEEDS_MAIN):
    cols = m.feature_cols(ds)
    assert len(cols) == 41 and "is_kospi" not in cols, cols
    y = ds.y_clip.to_numpy()
    out = np.full((len(ds), len(seeds)), np.nan, np.float32)
    t0 = time.time()
    for mo, first_day, cutoff in months:
        tr_idx = np.flatnonzero((ds.date <= cutoff).to_numpy())
        te_idx = np.flatnonzero(((ds.date >= mo.start_time) & (ds.date <= mo.end_time)).to_numpy())
        Xtr = ds.iloc[tr_idx][cols].to_numpy(np.float32)
        Xte = ds.iloc[te_idx][cols].to_numpy(np.float32)
        for si, seed in enumerate(seeds):
            out[te_idx, si] = sm.fit(Xtr, y[tr_idx], seed).predict(Xte)
        print(f"  {mo} (retrain {first_day.date()}, train cutoff {cutoff.date()}): trained on {len(tr_idx):,} rows, "
              f"predicted {len(te_idx):,} ({time.time() - t0:.0f}s)", flush=True)
    preds = pd.DataFrame({"g": ds.g, "date": ds.date})
    for si, seed in enumerate(seeds):
        preds[f"main_s{seed}"] = out[:, si]
    return preds


# ---------------------------------------------------------------- 신호 단위 평가 (stage5_model.evaluate 와 같은 순서·난수)

def signal_core(with_ci=True):
    t0 = time.time()
    ds, g, sim, cut = m.prepare()
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
    score = ds.score.to_numpy()
    assert not np.isnan(score).any(), "평가 대상에 예측이 없는 행이 있다"
    picks, t3, yearly = m.signal_eval(ds, score, day_rows, rand_mean_day)
    ic = dg.daily_ic(ds, "score", "R")
    ic_ci = sm.month_block_ci(ic.rename("ic").reset_index().assign(one=1.0), "ic", "one", rng) if with_ci else (np.nan, np.nan)
    rs = m.rule_score(ds)
    r_picks, r_t3, r_yearly = m.signal_eval(ds, rs, day_rows, rand_mean_day)
    # 월별: 상위 3 평균 R, 그 달 무작위 평균 (200회 각각의 그 달 평균을 다시 평균)
    mon = t3.groupby(t3.date.dt.to_period("M")).R_real.agg(["mean", "size"])
    rmon = pd.DataFrame([ds.loc[p].groupby(ds.loc[p].date.dt.to_period("M")).R_real.mean() for p in rand_picks]).mean()
    monthly = mon.join(rmon.rename("random"))
    return {"ds": ds, "g": g, "sim": sim, "cut": cut, "rand_picks": rand_picks, "rand_R": rand_R, "rand_pct": rand_pct,
            "r_mean": rand_R.mean(), "r05": np.quantile(rand_R, .05), "r90": np.quantile(rand_R, .90),
            "r95": np.quantile(rand_R, .95), "picks": picks, "t3": t3, "exp_R": t3.R_real.mean(), "yearly": yearly,
            "ic": ic, "ic_ci": ic_ci, "rs": rs, "r_picks": r_picks, "r_t3": r_t3, "r_yearly": r_yearly, "monthly": monthly,
            "rand_pool": ds.loc[np.concatenate(rand_picks)]}


# ---------------------------------------------------------------- 포트폴리오 (작업 프로세스에 경로·날짜를 다시 지정)

def _vinit(sim_path, first, last, d, sim):
    m.SIM = sim_path
    sm.FIRST_PRED, sm.LAST_PRED = first, last
    m._port_init(d, sim)


def _vrandom(args):
    return m._port_random(args)


def port_run_exposure(g, d, sim, **kw):
    """port_run 사본: 보유 평가액 기록만 추가 (누적 수익이 원본과 같은지 호출하는 쪽에서 확인)"""
    src = inspect.getsource(m.port_run)
    a, b = "        eq.append(cash + mv)\n", "    e = pd.Series(eq, index=days)\n"
    c = "    cash, pos, eq, n, prev = float(capital), {}, [], 0, None\n"
    assert src.count(a) == 1 and src.count(b) == 1 and src.count(c) == 1
    src = (src.replace("def port_run(", "def port_run_x(").replace(c, c + "    _mv = []\n")
           .replace(a, a + "        _mv.append(mv)\n").replace(b, b + "    _out['x'] = pd.Series(_mv, index=days) / e\n"))
    ns = dict(vars(m))
    ns["_out"] = {}
    exec(src, ns)
    r = ns["port_run_x"](g, d, sim, **kw)
    return r, ns["_out"]["x"]


def portfolios(core, port_reps, sim_path, first, last):
    g, ds, sim = core["g"], core["ds"], core["sim"]
    ports, expo = {}, {}
    for name, kw in m.PORTS.items():
        ports[name] = m.port_run(g, ds, sim, **kw)
        r2, x = port_run_exposure(g, ds, sim, **kw)
        assert abs(r2["total"] - ports[name]["total"]) < 1e-9, (name, r2["total"], ports[name]["total"])
        expo[name] = x
    rule_ports = {name: m.port_run(g, ds.assign(score=core["rs"]), sim, **kw) for name, kw in m.PORTS.items()}
    dslim = ds[["date", "code", "g", "filter_ok", "cost_ps", "risk_ps", "exit_cal"]]
    simlim = {"p1": sim["p1"]}
    # 작업 프로세스와 같은 무작위 1회를 주 프로세스에서 계산 (경로·날짜가 같게 지정됐는지 확인)
    m._port_init(dslim, simlim)
    check_name = list(m.PORTS)[0]
    local0 = m._port_random((check_name, 0))[1]
    rnd = {name: [] for name in m.PORTS}
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=min(14, os.cpu_count() or 4), initializer=_vinit,
                             initargs=(sim_path, first, last, dslim, simlim)) as ex:
        tasks = [(nm, r) for nm in m.PORTS for r in range(port_reps)]
        for (nm, r), (name, tot) in zip(tasks, ex.map(_vrandom, tasks, chunksize=4)):
            rnd[name].append(tot)
            if nm == check_name and r == 0:
                assert abs(tot - local0) < 1e-9, ("worker/main random portfolio mismatch", tot, local0)
            if len(rnd[name]) % 50 == 0:
                print(f"  random portfolio {name[:2]} {len(rnd[name])}/{port_reps} ({time.time() - t0:.0f}s)", flush=True)
    return ports, expo, rule_ports, rnd


# ---------------------------------------------------------------- 보고서

def write_summary(path, core, verdict, ports, expo, rule_ports, rnd, bench, header, first, last):
    ds, t3, rp = core["ds"], core["t3"], core["rand_pool"]
    f3 = lambda x: f"{x:+.3f}"
    L = header + ["## 판정\n"]
    for k, ok in verdict.items():
        L.append(f"- {ok} {k}")
    L.append("\n## 신호 단위 (상위 3)\n")
    L.append("| | 신호 | 평균 R | % 순수익률 | IC [95%, 참고] |\n|---|---|---|---|---|")
    L.append(f"| 본 모델 | {len(t3)} | {f3(core['exp_R'])} | {t3.net_real.mean() * 100:+.3f}% | "
             f"{core['ic'].mean():+.4f} [{core['ic_ci'][0]:+.4f}, {core['ic_ci'][1]:+.4f}] |")
    L.append(f"| 단순 규칙 (참고) | {len(core['r_t3'])} | {f3(core['r_t3'].R_real.mean())} | {core['r_t3'].net_real.mean() * 100:+.3f}% | - |")
    L.append(f"\n무작위 200회 (상위 3 평균 R): 평균 {f3(core['r_mean'])}, 5% 지점 {f3(core['r05'])}, 90% 지점 {f3(core['r90'])}, "
             f"95% 지점 {f3(core['r95'])} | % 순수익률 평균 {core['rand_pct'].mean():+.3f}%")
    L.append(f"\n본 모델 - 무작위 평균: {core['exp_R'] - core['r_mean']:+.3f}R | 본 모델 - 단순 규칙: "
             f"{core['exp_R'] - core['r_t3'].R_real.mean():+.3f}R, 고른 신호가 겹치는 비율 "
             f"{len(set(core['picks']) & set(core['r_picks'])) / max(len(core['picks']), 1) * 100:.1f}%")
    L.append(f"\n연도별 차이 (R, 날짜별 차이를 신호 수로 가중): 본 모델 {core['yearly']}, 단순 규칙 {core['r_yearly']}\n")
    L.append("월별 (R):\n\n| 월 | 상위 3 신호 | 상위 3 평균 R | 무작위 평균 R |\n|---|---|---|---|")
    for mo, r in core["monthly"].iterrows():
        L.append(f"| {mo} | {int(r['size'])} | {r['mean']:+.3f} | {r['random']:+.3f} |")
    fo = ds[ds.filter_ok]
    dec = (fo.groupby("date").score.rank(pct=True) * 10).clip(upper=9.999).astype(int)
    L.append(f"\n점수 10분위별 평균 R (시장 필터 통과 대상, 0 = 낮음): {fo.R_real.groupby(dec).mean().round(3).to_dict()}\n")
    tr, ex = sm.tail_stats(t3.R_real.to_numpy())
    rt = np.array([sm.tail_stats(ds.loc[p, "R_real"].to_numpy()) for p in core["rand_picks"]])
    L.append(f"꼬리 (R): 본 모델 상하위 1% 자름 {tr / 100:+.3f}, 수익 상위 10건 뺌 {ex / 100:+.3f} | "
             f"무작위 200회 평균: 1% 자름 {np.nanmean(rt[:, 0]) / 100:+.3f}, 상위 10건 뺌 {np.nanmean(rt[:, 1]) / 100:+.3f}\n")
    top10 = t3.nlargest(10, "R_real")
    L.append("수익 상위 10건 (본 모델 상위 3): " + ", ".join(
        f"{r.code} {r.date.date()} {r.R_real:+.1f}R ({r.net_real * 100:+.0f}%, {r.hold_real}일)" for r in top10.itertuples()))
    tot = t3.R_real.sum()
    L.append(f"상위 10건 R 합 / 전체 R 합: {top10.R_real.sum() / tot * 100:.0f}%\n" if tot > 0 else "상위 10건 R 합 / 전체 R 합: 전체 R 합이 0 이하\n")
    L.append("## 청산·보유·손실 (실제 규칙)\n")
    for name, t in (("본 모델 상위 3", t3), ("무작위 (200회 합침)", rp), ("단순 규칙 상위 3", core["r_t3"])):
        rr = t.reason_real.map(m.REASON5).value_counts(normalize=True).mul(100).round(1).to_dict()
        ls = m.loss_stats(t)
        L.append(f"- **{name}** ({len(t)}건): 청산 사유 % {rr}, 보유 평균 {t.hold_real.mean():.1f}일 / 중앙값 {t.hold_real.median():.0f}일")
        L.append(f"  - 손절가보다 낮게 체결된 손절: 손절 {ls['stop_n']}건 중 {ls['gap_n']}건 ({ls['gap_of_stop']:.1f}%, 전체 거래의 "
                 f"{ls['gap_of_all']:.1f}%), 그 거래들의 R 중앙값 {ls['gap_R_median']:+.2f}, 5% 지점 {ls['gap_R_p05']:+.2f}, 최악 {ls['gap_R_min']:+.2f}")
        L.append(f"  - R < -2: {ls['lt2_n']}건 ({ls['lt2_pct']:.2f}%), 정지 묶임 {int(t.halt_stuck.sum())}건")
    days = core["g"].cal_dates[(core["g"].cal_dates >= first) & (core["g"].cal_dates <= last)]
    months_len = (days[-1] - days[0]).days / 30.4375
    L.append(f"\n## 포트폴리오 (참고, 시작 3,000만 원, {days[0].date()} ~ {days[-1].date()})\n")
    L.append("| | 본 모델 누적 | MDD | 거래 수 | 월평균 매매 | 무작위 200회 중앙값 [5~95%] | 모델 백분위 | 단순 규칙 누적 |\n|---|---|---|---|---|---|---|---|")
    for name, p in ports.items():
        r = np.array(rnd[name])
        L.append(f"| {name} | {p['total']:+.1f}% | {p['mdd']:.1f}% | {p['trades']} | {p['trades'] / months_len:.2f} | "
                 f"{np.median(r):+.1f}% [{np.quantile(r, .05):+.1f}, {np.quantile(r, .95):+.1f}] | {(r < p['total']).mean() * 100:.1f} | "
                 f"{rule_ports[name]['total']:+.1f}% |")
    L.append("\n일별 투자 비중 (보유 평가액 / 총자산, port_run 사본, 누적 수익이 원본과 같음을 확인):\n")
    L.append("| | 평균 | 중앙값 | 최대 | 100% 넘은 날 | 0% 인 날 | 거래일 |\n|---|---|---|---|---|---|---|")
    for name, x in expo.items():
        L.append(f"| {name[:2]} | {x.mean() * 100:.1f}% | {x.median() * 100:.1f}% | {x.max() * 100:.1f}% | {int((x > 1 + 1e-12).sum())} | "
                 f"{int((x == 0).sum())} | {len(x)} |")
    p3 = ports[list(ports)[0]]
    L.append(f"\nP3 진입 시 총자산 대비 실제 손실: 1% 초과 {p3['loss_gt1']}건, 2% 초과 {p3['loss_gt2']}건 (청산 {p3['closed']}건)\n")
    L.append(f"대조군 ({days[0].date()} ~ {days[-1].date()}): " + ", ".join(f"{k} {x:+.2f}%" for k, x in bench.items()))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return L


def validation_verdict(core):
    n = len(core["t3"])
    if n < 30:
        return {f"판정 불가 (신호 {n}건 < 30)": "-"}
    e, ic = core["exp_R"], core["ic"].mean()
    return {f"(a) 상위 3 평균 R > 0: {e:+.3f}": "O" if e > 0 else "X",
            f"(b) 무작위 평균 {core['r_mean']:+.3f} 보다 높고 200회 90% 지점 {core['r90']:+.3f} 초과: {e:+.3f}":
                "O" if (e > core["r_mean"] and e > core["r90"]) else "X",
            f"(c) IC 평균 > 0: {ic:+.4f}": "O" if ic > 0 else "X"}


# ---------------------------------------------------------------- 2. 대조 3a·3b·3c

def rel_err(a, b):
    """상대 오차. NaN 은 양쪽 모두 NaN 일 때만 일치, 무한대는 양쪽이 같은 부호의 무한대일 때만 일치 (차이 0)"""
    a, b = np.asarray(a, float), np.asarray(b, float)
    both_nan = np.isnan(a) & np.isnan(b)
    same_inf = np.isinf(a) & np.isinf(b) & (a == b)
    bad = (np.isnan(a) != np.isnan(b)) | ((np.isinf(a) | np.isinf(b)) & ~same_inf)
    with np.errstate(invalid="ignore", divide="ignore"):
        d = np.where(b != 0, np.abs(a - b) / np.where(b != 0, np.abs(b), 1), np.abs(a - b))
    d = np.where(both_nan | same_inf, 0.0, d)
    return np.where(bad, np.inf, d)


def check3a():
    old = pd.read_parquet(os.path.join(DEV_DIR, "dataset.parquet"))
    new = pd.read_parquet(os.path.join(VAL_DIR, "dataset.parquet"))
    cols = m.feature_cols(old)
    o = old[old.date <= DEV_LAST_INCLUDED].reset_index(drop=True)
    n = new[new.date <= DEV_LAST_INCLUDED].reset_index(drop=True)
    so, sn = o.groupby("date").code.apply(frozenset), n.groupby("date").code.apply(frozenset)
    set_bad = int((so.reindex(sn.index.union(so.index)) != sn.reindex(sn.index.union(so.index))).sum())
    mg = o.merge(n, on=["code", "date"], suffixes=("_o", "_n"), how="outer", indicator=True)
    only = int((mg._merge != "both").sum())
    mg = mg[mg._merge == "both"]
    g_same = bool((mg.g_o == mg.g_n).all())
    feat = {c: float(np.max(rel_err(mg[f"{c}_n"], mg[f"{c}_o"]))) for c in cols}
    feat_bad = {c: v for c, v in feat.items() if not v <= REL_TOL}
    k = pd.read_parquet(sd.KOSPI).sort_values("timestamp").set_index("timestamp").close
    prev_below = (k < k.rolling(120).mean()).shift(1)
    dates = sorted(set(o.date) | set(n.date))
    filt_days_o = int((~prev_below.reindex(sorted(set(o.date))).fillna(False).astype(bool)).sum())
    filt_days_n = int((~prev_below.reindex(sorted(set(n.date))).fillna(False).astype(bool)).sum())
    lab = mg[mg.reason_o != sb.R_END]
    lab_R = float(np.max(rel_err(lab.R_n, lab.R_o))) if len(lab) else 0.0
    lab_same = bool((lab.reason_o == lab.reason_n).all() and (lab.hold_o == lab.hold_n).all())
    zo, zn = np.load(os.path.join(DEV_DIR, "sim.npz")), np.load(os.path.join(VAL_DIR, "sim.npz"))
    sim_bad = [kk for kk in zo.files if kk != "dev_last" and not (zo[kk].shape == zn[kk].shape and
               np.array_equal(zo[kk], zn[kk], equal_nan=zo[kk].dtype.kind == "f"))]
    ok = (set_bad == 0 and only == 0 and g_same and not feat_bad and filt_days_o == filt_days_n and lab_R <= REL_TOL
          and lab_same and not sim_bad)
    L = [f"### 3a. 데이터셋 대조 (진입일 <= {DEV_LAST_INCLUDED.date()}): {'일치' if ok else '어긋남'}\n",
         f"- 행: 기존 {len(o):,}, 새 {len(n):,}, 한쪽에만 있는 행 {only}, 날짜별 대상 집합이 다른 날 {set_bad} / {len(dates)}, "
         f"시뮬레이션 행 번호(g) 같음 {g_same}",
         f"- 특징 41개 최대 상대 오차: {max(feat.values()):.1e} (기준 {REL_TOL:.0e}), 기준 넘은 특징 {feat_bad if feat_bad else '없음'}",
         f"- 시장 필터 통과 거래일: 기존 {filt_days_o}, 새 {filt_days_n} (같은 지수·같은 계산)",
         f"- 라벨 R (기존에서 개발 구간 마감 전에 청산된 {len(lab):,}행): 최대 상대 오차 {lab_R:.1e}, 청산 사유·보유 일수 같음 {lab_same}",
         f"- 시뮬레이션 배열 (dev_last 제외) 다른 것: {sim_bad if sim_bad else '없음'}"]
    return ok, L


def check3b():
    use(VAL_DIR, DEV_FIRST, DEV_LAST)
    ds = pd.read_parquet(m.DATASET)
    ms = [x for x in sm.months(ds) if str(x[0]) == "2025-12"]
    assert len(ms) == 1
    new = fit_predict(ds, ms)
    old = pd.read_parquet(os.path.join(DEV_DIR, "predictions.parquet"))
    cols = [f"main_s{s}" for s in m.SEEDS_MAIN]
    mo = ms[0][0]
    nn = new[(new.date >= mo.start_time) & (new.date <= mo.end_time)].assign(sn=lambda x: x[cols].mean(axis=1))
    oo = old[(old.date >= mo.start_time) & (old.date <= mo.end_time)].assign(so=lambda x: x[cols].mean(axis=1))
    j = nn[["g", "date", "sn"]].merge(oo[["g", "so"]], on="g", how="outer", indicator=True)
    only = int((j._merge != "both").sum())
    j = j[j._merge == "both"]
    rc = dg.daily_ic(j, "sn", "so")
    ok = only == 0 and len(rc) > 0 and bool((rc >= RANK_TOL).all())
    L = [f"### 3b. 2025-12 예측 다시 만들기 (시드 1~5 평균, 재학습 {ms[0][1].date()}, 학습 끝 {ms[0][2].date()}): {'일치' if ok else '어긋남'}\n",
         f"- 행 {len(j):,} (한쪽에만 있는 행 {only}), 최대 절대 차이 {np.abs(j.sn - j.so).max():.2e}",
         f"- 날짜별 순위상관: 최소 {rc.min():.6f}, 평균 {rc.mean():.6f}, {len(rc)}일, {RANK_TOL} 미만인 날 {int((rc < RANK_TOL).sum())}"]
    return ok, L


EXPECT = {"exp_R": 0.270, "r_mean": 0.015, "r05": -0.065, "r95": 0.094, "ic": 0.1472, "rule": 0.068,
          "yearly": {2022: 0.082, 2023: 0.187, 2024: 0.293, 2025: 0.337}}


def check3c():
    use(DEV_DIR, DEV_FIRST, DEV_LAST)
    core = signal_core()
    got = {"exp_R": round(core["exp_R"], 3), "r_mean": round(core["r_mean"], 3), "r05": round(core["r05"], 3),
           "r95": round(core["r95"], 3), "ic": round(core["ic"].mean(), 4), "rule": round(core["r_t3"].R_real.mean(), 3),
           "yearly": {k: round(v, 3) for k, v in core["yearly"].items()}}
    diff = {k: (got[k], EXPECT[k]) for k in EXPECT if got[k] != EXPECT[k]}
    ok = not diff
    L = [f"### 3c. 새 판정·보고 코드를 개발 구간 예측에 적용: {'summary.md 와 일치' if ok else '어긋남'}\n",
         f"- 상위 3 평균 R {got['exp_R']:+.3f} (summary +0.270), 무작위 평균 {got['r_mean']:+.3f} (+0.015), "
         f"5% 지점 {got['r05']:+.3f} (-0.065), 95% 지점 {got['r95']:+.3f} (+0.094), IC {got['ic']:+.4f} (+0.1472), "
         f"단순 규칙 {got['rule']:+.3f} (+0.068)",
         f"- 연도별 차이 {got['yearly']} (summary {EXPECT['yearly']})",
         f"- 어긋난 항목: {diff if diff else '없음'}",
         f"- 개발 구간 무작위 200회 90% 지점: {core['r90']:+.3f}"]
    # 검증용 보고 코드 전체를 개발 구간 데이터로 한 번 끝까지 (무작위 포트폴리오 4회, 출력은 임시 폴더)
    os.makedirs(SMOKE_DIR, exist_ok=True)
    try:
        ports, expo, rule_ports, rnd = portfolios(core, 4, m.SIM, DEV_FIRST, DEV_LAST)
        bench = sm.benchmarks(core["g"])
        write_summary(os.path.join(SMOKE_DIR, "summary_dev_smoke.md"), core, validation_verdict(core), ports, expo,
                      rule_ports, rnd, bench, ["# smoke\n"], DEV_FIRST, DEV_LAST)
        smoke = f"오류 없음 (개발 구간 포트폴리오 누적: " + ", ".join(f"{k[:2]} {v['total']:+.1f}%" for k, v in ports.items()) + \
                ", summary 는 P3 +96.0%, P1 +38.7%, P2 +76.7%)"
        smoke_ok = [round(p["total"], 1) for p in ports.values()] == [96.0, 38.7, 76.7]
    finally:
        shutil.rmtree(SMOKE_DIR, ignore_errors=True)
    L.append(f"- 검증용 보고 코드 전체 시험 (개발 구간 데이터, 무작위 포트폴리오 4회, 임시 폴더는 삭제): {smoke}")
    return ok and smoke_ok, L


def check():
    res = []
    for f in (check3a, check3b, check3c):
        ok, L = f()
        print("\n".join(L), flush=True)
        res.append((ok, L))
        if not ok:
            break
    allok = len(res) == 3 and all(ok for ok, _ in res)
    with open(os.path.join(VAL_DIR, "precheck.md"), "w", encoding="utf-8") as fh:
        fh.write("# 5단계 검증 실행 전 대조 (3a·3b·3c)\n\n" + "\n\n".join("\n".join(L) for _, L in res)
                 + f"\n\n**결과: {'모두 일치 -> 검증 실행' if allok else '어긋남 -> 멈춤'}**\n")
    return allok


# ---------------------------------------------------------------- 3. X 계산·기록 (예측 없이, stage5_model.prepare 앞부분과 같은 계산)

def xcut():
    ds = pd.read_parquet(os.path.join(VAL_DIR, "dataset.parquet"))
    ds = ds[(ds.date >= VAL_START) & (ds.date <= VAL_END)].reset_index(drop=True)
    g = s4.SimArrays(np.load(os.path.join(VAL_DIR, "sim.npz")))
    T = ds.g.to_numpy()
    risk = s4.STOP_ATR * g.atr20[T]
    sim = sb.simulate(g, T, g.c[T] - risk, np.full(len(T), np.nan), "full", want_legs=True)
    ds["reason_real"] = sim["reason"]
    k = pd.read_parquet(sd.KOSPI).sort_values("timestamp").set_index("timestamp").close
    prev_below = (k < k.rolling(120).mean()).shift(1).reindex(ds.date.unique()).fillna(False)
    ds["filter_ok"] = ~ds.date.map(prev_below).astype(bool)
    ds["halt_stuck"] = (ds.reason_real == sb.R_END).to_numpy() & (g.vol[g.dev_last[T]] == 0)
    crossing = ds[ds.filter_ok & (ds.reason_real == sb.R_END) & ~ds.halt_stuck]
    X = crossing.date.min() if len(crossing) else None
    kept = ds[ds.date < X] if X is not None else ds
    cal = g.cal_dates
    last_incl = cal[cal.get_loc(X) - 1] if X is not None else VAL_END
    months = [str(p) for p in pd.period_range(VAL_START, last_incl, freq="M")]
    rec = {"X": str(X.date()) if X is not None else None, "last_included_entry": str(last_incl.date()),
           "last_kept_signal_date": str(kept.date.max().date()), "retrain_months": months,
           "filter_days": int(kept[kept.filter_ok].date.nunique()), "filter_signal_candidates": int(kept.filter_ok.sum()),
           "dropped_filter_rows": int(ds[ds.date >= X].filter_ok.sum()) if X is not None else 0,
           "eval_window_rows": len(ds), "crossing_trades": len(crossing)}
    with open(os.path.join(VAL_DIR, "xcut.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    L = ["# 5단계 검증 구간 진입 범위 (X) — 예측·평가 전 기록\n",
         "계산 (stage5_validation.xcut, stage5_model.py:109-113 과 같은 조건):\n",
         "    crossing = ds[ds.filter_ok & (ds.reason_real == sb.R_END) & ~ds.halt_stuck]",
         "    X = crossing.date.min() if len(crossing) else None",
         "    kept = ds[ds.date < X] if X is not None else ds\n",
         f"- X (처음 제외되는 진입일): {rec['X']}",
         f"- 마지막 포함 진입일 (X 직전 거래일): {rec['last_included_entry']} (포함된 신호의 마지막 날짜 {rec['last_kept_signal_date']})",
         f"- 재학습 월: {', '.join(months)}",
         f"- 포함 구간 시장 필터 통과 거래일 수: {rec['filter_days']}, 필터 통과 신호 후보 수: {rec['filter_signal_candidates']:,}",
         f"- X 이후로 빠지는 필터 통과 신호 후보: {rec['dropped_filter_rows']:,}"]
    with open(os.path.join(VAL_DIR, "xcut.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    return rec


# ---------------------------------------------------------------- 4. 검증 실행 (한 번)

def run():
    assert os.path.exists(os.path.join(VAL_DIR, "precheck.md")) and "모두 일치" in open(os.path.join(VAL_DIR, "precheck.md"), encoding="utf-8").read()
    rec = json.load(open(os.path.join(VAL_DIR, "xcut.json"), encoding="utf-8"))
    pred_path = os.path.join(VAL_DIR, "predictions.parquet")
    assert not os.path.exists(pred_path) and not os.path.exists(os.path.join(VAL_DIR, "summary.md")), "검증은 한 번만 실행한다"
    last_month = pd.Period(rec["retrain_months"][-1], "M")
    use(VAL_DIR, VAL_START, last_month.end_time.normalize(), pred_path)
    ds = pd.read_parquet(m.DATASET)
    ms = sm.months(ds)
    assert [str(x[0]) for x in ms] == rec["retrain_months"], ([str(x[0]) for x in ms], rec["retrain_months"])
    print("retrain:", [(str(a), str(b.date()), str(c.date())) for a, b, c in ms], flush=True)
    fit_predict(ds, ms).to_parquet(pred_path, index=False)
    use(VAL_DIR, VAL_START, VAL_END, pred_path)
    core = signal_core()
    X = core["cut"]["last_entry"]
    assert (str(X.date()) if X is not None else None) == rec["X"], (X, rec["X"])
    verdict = validation_verdict(core)
    ports, expo, rule_ports, rnd = portfolios(core, m.PORT_REPS, m.SIM, VAL_START, VAL_END)
    bench = sm.benchmarks(core["g"])
    header = ["# 5단계 검증 구간 결과 (2026-01-02 ~ 2026-10-16, 한 번)\n",
              "평가는 실제 규칙(라벨용 60일 상한 없음) R. 시장 필터: 진입일 전날 코스피 종가가 120일선 아래면 진입 안 함. "
              "정지에 묶인 거래 = 순수익률 -100%. 특징 41개, 시드 1~5.\n",
              f"X (처음 제외되는 진입일): {rec['X']}, 마지막 포함 진입일: {rec['last_included_entry']}, "
              f"재학습 월: {', '.join(rec['retrain_months'])}, 신호(상위 3): {len(core['t3'])}건, "
              f"포함 구간 필터 통과 거래일 {rec['filter_days']}, 필터 통과 신호 후보 {rec['filter_signal_candidates']:,}\n"]
    L = write_summary(os.path.join(VAL_DIR, "summary.md"), core, verdict, ports, expo, rule_ports, rnd, bench, header,
                      VAL_START, VAL_END)
    print("\n".join(L))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "check":
        sys.exit(0 if check() else 1)
    {"build": build, "xcut": xcut, "run": run}[cmd]()
