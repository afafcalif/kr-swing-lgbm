"""
5단계: 특징·R 라벨 생성 + 7a 특징 잘라내기 점검 (명세: backtest_swing/STAGE5_SPEC.md)

  python backtest_swing/stage5_features.py build     # results/stage5/dataset.parquet, sim.npz
  python backtest_swing/stage5_features.py check7a   # 200쌍 잘라내기 점검 -> results/stage5/check7a.md (실패 시 종료 코드 1)
  python backtest_swing/stage5_features.py check_stored  # 같은 200쌍: float64 재계산 vs 저장 float32 상대 오차 -> check7a_stored.md

4단계(stage4_features)의 특징 함수·대상 조건·시장 특징·시뮬레이션을 그대로 쓰고, 지지·저항과 수급 묶음만 만들지 않는다.
R = 순수익률 x 1주당 매수 비용 / (1.7 x ATR20(S)). 학습 라벨 y_clip = clip(R, -3, +10) - 같은 날 평균. 평가는 자르지 않은 R.
신호일 2020-01-02 ~ 2025-12-31 (2026 은 만들지 않는다).
"""
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage3_backtest as sb  # noqa: E402
import stage3_data as sd  # noqa: E402
import stage4_features as s4  # noqa: E402

OUT = "backtest_swing/results/stage5"
R_LO, R_HI = -3.0, 10.0
XS_FEATS = ["rank_ret20", "rank_ret60", "rank_amt20", "breadth_ma20"]
NON_FEATURES = {"code", "date", "g", "close", "above_ma20", "net", "R", "risk", "hold", "reason", "y", "y_clip"}
N_CHECK, CHECK_SEED, TOL = 200, 0, 1e-9


def stock_features(df: pd.DataFrame) -> pd.DataFrame:
    """4단계 종목별 특징에서 수급 묶음을 뺀 것 (지지·저항은 원래 별도 함수라 부르지 않는다)"""
    return s4.stock_features(df, None).drop(columns=s4.FLOW_FEATS)


def build():
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    cal = pd.DatetimeIndex(pd.read_parquet(sd.KOSPI).timestamp).sort_values()
    codes = sd.universe_codes()
    uni = pd.read_csv(sd.UNIVERSE, dtype={"code": str})
    market = dict(zip(uni.code, uni.market))
    cols = {k: [] for k in ("o", "h", "l", "c", "ma20", "atr20", "vol", "ro", "rh", "rl", "rc", "rpc", "cal", "sid")}
    dev_last, parts, off = [], [], 0
    for sid, code in enumerate(codes):
        df = sd.load_stock(code)
        feats = stock_features(df)
        keep = (df.timestamp >= s4.SIM_ROW_START).to_numpy()
        first = int(np.argmax(keep)) if keep.any() else len(df)
        sub = df.iloc[first:]
        for k, src in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("ma20", "ma20"), ("atr20", "atr20"),
                       ("vol", "volume"), ("ro", "raw_open"), ("rh", "raw_high"), ("rl", "raw_low"), ("rc", "raw_close"),
                       ("rpc", "raw_prev_close")):
            cols[k].append(sub[src].to_numpy(np.float64))
        cols["cal"].append(cal.searchsorted(sub.timestamp.to_numpy()).astype(np.int32))
        cols["sid"].append(np.full(len(sub), sid, np.int32))
        nd = int((sub.timestamp <= s4.DEV_END).sum())
        dev_last.append(off + nd - 1 if nd > 0 else -1)
        el = s4.eligible_mask(df) & (df.timestamp >= s4.SIG_START) & (df.timestamp <= s4.DEV_END)
        idx = np.flatnonzero(el.to_numpy())
        if len(idx):
            part = feats.iloc[idx].astype(np.float32).copy()
            part.insert(0, "date", df.timestamp.iloc[idx].values)
            part.insert(0, "code", code)
            part["is_kospi"] = np.float32(market.get(code) == "KOSPI")
            part["g"] = (off + (idx - first)).astype(np.int64)
            part["close"] = df.close.iloc[idx].to_numpy(np.float32)
            part["above_ma20"] = (df.close.iloc[idx] > df.ma20.iloc[idx]).to_numpy(np.float32)
            parts.append(part)
        off += len(sub)
        if (sid + 1) % 400 == 0:
            print(f"  build {sid + 1}/{len(codes)} ({time.time() - t0:.0f}s, rows {sum(len(p) for p in parts):,})", flush=True)

    arr = {k: np.concatenate(v) for k, v in cols.items()}
    dl = np.array(dev_last)
    arr["dev_last"] = dl[arr["sid"]]
    np.savez_compressed(os.path.join(OUT, "sim.npz"), **arr, cal_dates=cal.values.astype("datetime64[ns]"), codes=np.array(codes))
    ds = pd.concat(parts, ignore_index=True)
    del parts
    grp = ds.groupby("date")
    ds["rank_ret20"] = grp.ret_20.rank(pct=True).astype(np.float32)
    ds["rank_ret60"] = grp.ret_60.rank(pct=True).astype(np.float32)
    ds["rank_amt20"] = grp.log_amt20.rank(pct=True).astype(np.float32)
    ds["breadth_ma20"] = grp.above_ma20.transform("mean").astype(np.float32)
    ds = ds.merge(s4.market_features(cal).astype(np.float32), left_on="date", right_index=True, how="left")

    # 라벨: 4단계와 같은 실제 매매 규칙 (60거래일 라벨 상한), R 배수로 바꾼다
    g = s4.SimArrays(np.load(os.path.join(OUT, "sim.npz")))
    T = ds.g.to_numpy()
    risk = s4.STOP_ATR * g.atr20[T]
    sim = sb.simulate(g, T, g.c[T] - risk, np.full(len(T), np.nan), "full", max_hold=s4.LABEL_CAP, kmax=s4.LABEL_CAP + 1)
    ds["net"] = sim["net"]
    ds["risk"] = risk
    ds["R"] = sim["net"] * sim["cost"] / risk
    ds["hold"] = sim["k"]
    ds["reason"] = sim["reason"]
    ds["y"] = ds.R - ds.groupby("date").R.transform("mean")
    rc = ds.R.clip(R_LO, R_HI)
    ds["y_clip"] = rc - rc.groupby(ds.date).transform("mean")
    ds.to_parquet(os.path.join(OUT, "dataset.parquet"), index=False)
    feat_cols = [c for c in ds.columns if c not in NON_FEATURES]
    print(f"build done: {len(ds):,} rows, {ds.code.nunique()} stocks, {ds.date.nunique()} days, {len(feat_cols)} features "
          f"({time.time() - t0:.0f}s)")
    print("features:", feat_cols)
    r = pd.Series(sim["reason"]).map(sb.REASON).value_counts(normalize=True) * 100
    print("label exits %:", r.round(1).to_dict(), f"| label cap (60d) hit {(ds.reason == sb.R_MAXHOLD).mean() * 100:.1f}%")
    q = ds.R.quantile([.01, .05, .25, .5, .75, .95, .99]).round(3).to_dict()
    print(f"R: mean {ds.R.mean():+.3f}, quantiles {q}, clipped low {(ds.R < R_LO).mean() * 100:.2f}% / high {(ds.R > R_HI).mean() * 100:.2f}%")
    print(f"R < -1.05 (lost more than about 1R, gap / limit-down): {(ds.R < -1.05).mean() * 100:.1f}% | "
          f"stop exits with R < -1.05: {((ds.reason == sb.R_STOP) & (ds.R < -1.05)).sum() / max((ds.reason == sb.R_STOP).sum(), 1) * 100:.1f}%")
    print(f"y_clip day-mean max |.| {ds.groupby('date').y_clip.mean().abs().max():.2e}")
    print("rows per year:", ds.date.dt.year.value_counts().sort_index().to_dict())


# ---------------------------------------------------------------- 7a 특징 잘라내기 점검

def _diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """원소별 차이. 둘 다 빈칸이면 0, 한쪽만 빈칸이면 inf"""
    a, b = np.asarray(a, float), np.asarray(b, float)
    na, nb = np.isnan(a), np.isnan(b)
    d = np.abs(np.where(na | nb, 0.0, a - b))
    return np.where(na != nb, np.inf, d)


def _same32(stored, full64) -> np.ndarray:
    """저장된 float32 값 == 전체 데이터 float64 계산값을 float32 로 바꾼 값 (빈칸 동일 포함)"""
    s = np.asarray(stored, np.float32)
    f = np.asarray(full64, np.float64).astype(np.float32)
    return (s == f) | (np.isnan(s) & np.isnan(f))


def _xs_worker(args):
    """한 종목: 표본 날짜마다 S 까지 잘라 대상 여부와 상대 강도 입력값을 다시 계산 (전체 데이터 값과 함께)"""
    code, dates = args
    full = sd.load_stock(code)
    ts = pd.DatetimeIndex(full.timestamp)
    on = [d for d in dates if d in ts]
    if not on:
        return []
    ff = stock_features(full)
    el_full = s4.eligible_mask(full) & (full.timestamp >= s4.SIG_START) & (full.timestamp <= s4.DEV_END)
    out = []
    for d in on:
        i = ts.get_loc(d)
        cut = sd.load_stock(code, until=d)
        assert cut.timestamp.iat[-1] == d
        fc = stock_features(cut).iloc[-1]
        el_cut = bool((s4.eligible_mask(cut) & (cut.timestamp >= s4.SIG_START) & (cut.timestamp <= s4.DEV_END)).iat[-1])
        out.append((d, code, bool(el_full.iat[i]), el_cut,
                    ff.ret_20.iat[i], ff.ret_60.iat[i], ff.log_amt20.iat[i], float(full.close.iat[i] > full.ma20.iat[i]),
                    fc.ret_20, fc.ret_60, fc.log_amt20, float(cut.close.iat[-1] > cut.ma20.iat[-1])))
    return out


def check7a():
    t0 = time.time()
    ds = pd.read_parquet(os.path.join(OUT, "dataset.parquet"))
    feats = [c for c in ds.columns if c not in NON_FEATURES]
    mk_cols = [c for c in feats if c.startswith(("kospi_", "kosdaq_"))]
    per_stock = [c for c in feats if c not in XS_FEATS + mk_cols + ["is_kospi"]]
    rng = np.random.default_rng(CHECK_SEED)
    sample = ds.iloc[np.sort(rng.choice(len(ds), N_CHECK, replace=False))]
    worst = {c: 0.0 for c in feats}
    worst_at = {}
    stored_bad = {}

    def record(col, d, where, s_ok):
        d = float(np.max(d)) if np.size(d) else 0.0
        if d > worst[col]:
            worst[col], worst_at[col] = d, where
        if not np.all(s_ok):
            stored_bad[col] = stored_bad.get(col, 0) + int(np.size(s_ok) - np.sum(s_ok))

    # 종목별 특징 30개
    for r in sample.itertuples():
        full = sd.load_stock(r.code)
        i = int(np.flatnonzero(full.timestamp.to_numpy() == np.datetime64(r.date))[0])
        f_full = stock_features(full).iloc[i]
        cut = sd.load_stock(r.code, until=r.date)
        assert cut.timestamp.iat[-1] == r.date and len(cut) == i + 1
        f_cut = stock_features(cut).iloc[-1]
        for col in per_stock:
            record(col, _diff([f_cut[col]], [f_full[col]]), (r.code, r.date.date(), float(f_cut[col]), float(f_full[col])),
                   _same32([ds.at[r.Index, col]], [f_full[col]]))
    print(f"  per-stock done ({time.time() - t0:.0f}s)", flush=True)

    # 시장 특징 7개: 지수를 S 까지만 남기고 다시 계산
    cal = pd.DatetimeIndex(pd.read_parquet(sd.KOSPI).timestamp).sort_values()
    mk_full = s4.market_features(cal)
    for r in sample.itertuples():
        mk_cut = s4.market_features(cal[cal <= r.date]).loc[r.date]
        for col in mk_cols:
            record(col, _diff([mk_cut[col]], [mk_full.at[r.date, col]]), (r.date.date(), float(mk_cut[col]), float(mk_full.at[r.date, col])),
                   _same32([ds.at[r.Index, col]], [mk_full.at[r.date, col]]))

    # 같은 날 상대 강도·시장 폭 4개: 표본 날짜마다 그날 행이 있는 모든 종목을 S 까지 잘라 다시 계산
    dates = sorted(set(sample.date))
    codes = sd.universe_codes()
    with ProcessPoolExecutor(max_workers=min(14, os.cpu_count() or 4)) as ex:
        rows = [x for part in ex.map(_xs_worker, [(c, dates) for c in codes], chunksize=8) for x in part]
    xs = pd.DataFrame(rows, columns=["date", "code", "el_full", "el_cut", "r20_f", "r60_f", "amt_f", "above_f",
                                     "r20_c", "r60_c", "amt_c", "above_c"])
    print(f"  cross-section inputs: {len(xs):,} stock-days on {len(dates)} dates ({time.time() - t0:.0f}s)", flush=True)
    el_mismatch = xs[xs.el_full != xs.el_cut]
    for d, gx in xs.groupby("date"):
        a = gx[gx.el_full]
        b = gx[gx.el_cut]
        ranks = {}
        for name, (fcol, ccol) in {"rank_ret20": ("r20_f", "r20_c"), "rank_ret60": ("r60_f", "r60_c"),
                                   "rank_amt20": ("amt_f", "amt_c")}.items():
            ranks[name] = (a.set_index("code")[fcol].rank(pct=True), b.set_index("code")[ccol].rank(pct=True),
                           a.set_index("code")[fcol].astype(np.float32).rank(pct=True))
        breadth = (a.above_f.mean(), b.above_c.mean())
        for r in sample[sample.date == d].itertuples():
            for name, (rf, rcut, r32) in ranks.items():
                vf, vc = rf.get(r.code, np.nan), rcut.get(r.code, np.nan)
                record(name, _diff([vc], [vf]), (r.code, d.date(), vc, vf), _same32([ds.at[r.Index, name]], [r32.get(r.code, np.nan)]))
            record("breadth_ma20", _diff([breadth[1]], [breadth[0]]), (d.date(), breadth[1], breadth[0]),
                   _same32([ds.at[r.Index, "breadth_ma20"]], [breadth[0]]))
    # 저장된 데이터셋의 그날 대상 집합 = 전체 데이터로 다시 계산한 대상 집합
    ds_sets = ds[ds.date.isin(dates)].groupby("date").code.apply(set)
    set_bad = [d.date() for d in dates if set(xs[(xs.date == d) & xs.el_full].code) != ds_sets.get(d, set())]

    failed = {c: v for c, v in worst.items() if c != "is_kospi" and not v < TOL}
    ok = not failed and el_mismatch.empty and not set_bad
    L = ["# 5단계 7a 특징 잘라내기 점검\n",
         f"표본: 개발 구간 데이터셋에서 (종목, 신호일) {N_CHECK}쌍 (시드 {CHECK_SEED}), 표본 날짜 {len(dates)}일. "
         f"기준: 잘라낸 값과 전체 값의 최대 차이 < {TOL:.0e} (float64 끼리), 빈칸은 양쪽 모두 빈칸.\n",
         f"- 종목별 특징 {len(per_stock)}개: 원본 파일에서 S 이후 행을 지우고 지표·특징을 다시 계산 (200쌍)",
         f"- 시장 특징 {len(mk_cols)}개: 지수를 S 까지만 남기고 다시 계산 (200쌍)",
         f"- 같은 날 상대 강도·시장 폭 4개: 표본 날짜마다 그날 행이 있는 종목 {xs.groupby('date').size().mean():.0f}개(평균)를 "
         f"모두 S 까지 잘라 대상 여부·입력값을 다시 계산하고 순위를 다시 매김 (종목-날짜 {len(xs):,}개)",
         "- is_kospi: 현재(2026-10-19) 시장 구분 고정값이라 이 점검으로 확인할 수 없음 (한계)\n",
         f"**결과: {'통과' if ok else '실패'}**\n",
         f"- 기준을 넘은 특징: {failed if failed else '없음'}",
         f"- S 까지 잘랐을 때 대상 여부가 바뀐 종목-날짜: {len(el_mismatch)}",
         f"- 저장된 데이터셋의 그날 대상 집합과 다시 계산한 집합이 다른 날: {set_bad if set_bad else '없음'}",
         f"- 저장된 값(float32)이 전체 데이터 계산값의 float32 와 다른 경우: {stored_bad if stored_bad else '없음'}\n",
         "| 특징 | 최대 차이 |\n|---|---|"]
    for c in feats:
        if c == "is_kospi":
            continue
        L.append(f"| {c} | {worst[c]:.1e} |" + (f" {worst_at[c]}" if c in failed else ""))
    if not el_mismatch.empty:
        L.append("\n대상 여부가 바뀐 예: " + el_mismatch.head(10)[["date", "code", "el_full", "el_cut"]].to_string(index=False))
    L.append(f"\n({time.time() - t0:.0f}s)")
    with open(os.path.join(OUT, "check7a.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("\n".join(L))
    return ok


# ---------------------------------------------------------------- 7a 추가: 점검 코드 = 학습 데이터를 만든 코드 확인

REL_TOL = 1e-6


def _xs_full_worker(args):
    """한 종목: 표본 날짜의 전체 데이터 대상 여부와 상대 강도 입력값 (float64)"""
    code, dates = args
    full = sd.load_stock(code)
    ts = pd.DatetimeIndex(full.timestamp)
    on = [d for d in dates if d in ts]
    if not on:
        return []
    ff = stock_features(full)
    el = s4.eligible_mask(full) & (full.timestamp >= s4.SIG_START) & (full.timestamp <= s4.DEV_END)
    return [(d, code, bool(el.iat[i]), ff.ret_20.iat[i], ff.ret_60.iat[i], ff.log_amt20.iat[i],
             float(full.close.iat[i] > full.ma20.iat[i])) for d in on for i in [ts.get_loc(d)]]


def check_stored():
    """같은 200쌍: 전체 데이터로 float64 재계산한 특징 vs 저장된 float32 데이터셋 값, 상대 오차 (기준 1e-6)"""
    t0 = time.time()
    ds = pd.read_parquet(os.path.join(OUT, "dataset.parquet"))
    feats = [c for c in ds.columns if c not in NON_FEATURES]
    mk_cols = [c for c in feats if c.startswith(("kospi_", "kosdaq_"))]
    per_stock = [c for c in feats if c not in XS_FEATS + mk_cols + ["is_kospi"]]
    rng = np.random.default_rng(CHECK_SEED)
    sample = ds.iloc[np.sort(rng.choice(len(ds), N_CHECK, replace=False))]
    errs = {c: [] for c in feats}

    def rel(stored, full):
        s_, f_ = float(stored), float(full)
        if np.isnan(s_) and np.isnan(f_):
            return 0.0
        if np.isnan(s_) != np.isnan(f_):
            return np.inf
        return abs(s_ - f_) / abs(f_) if f_ != 0 else abs(s_ - f_)

    for r in sample.itertuples():
        full = sd.load_stock(r.code)
        i = int(np.flatnonzero(full.timestamp.to_numpy() == np.datetime64(r.date))[0])
        ff = stock_features(full).iloc[i]
        for col in per_stock:
            errs[col].append(rel(ds.at[r.Index, col], ff[col]))
    cal = pd.DatetimeIndex(pd.read_parquet(sd.KOSPI).timestamp).sort_values()
    mk = s4.market_features(cal)
    for r in sample.itertuples():
        for col in mk_cols:
            errs[col].append(rel(ds.at[r.Index, col], mk.at[r.date, col]))
    uni = pd.read_csv(sd.UNIVERSE, dtype={"code": str})
    market = dict(zip(uni.code, uni.market))
    for r in sample.itertuples():
        errs["is_kospi"].append(rel(ds.at[r.Index, "is_kospi"], float(market.get(r.code) == "KOSPI")))
    dates = sorted(set(sample.date))
    with ProcessPoolExecutor(max_workers=min(14, os.cpu_count() or 4)) as ex:
        rows = [x for part in ex.map(_xs_full_worker, [(c, dates) for c in sd.universe_codes()], chunksize=8) for x in part]
    xs = pd.DataFrame(rows, columns=["date", "code", "el", "r20", "r60", "amt", "above"])
    for d, gx in xs[xs.el].groupby("date"):
        gx = gx.set_index("code")
        rk = {"rank_ret20": gx.r20.rank(pct=True), "rank_ret60": gx.r60.rank(pct=True), "rank_amt20": gx.amt.rank(pct=True)}
        br = gx.above.mean()
        for r in sample[sample.date == d].itertuples():
            for name, s_ in rk.items():
                errs[name].append(rel(ds.at[r.Index, name], s_.get(r.code, np.nan)))
            errs["breadth_ma20"].append(rel(ds.at[r.Index, "breadth_ma20"], br))
    bad = {c: int((np.array(v) > REL_TOL).sum()) for c, v in errs.items() if (np.array(v) > REL_TOL).any()}
    L = ["# 5단계 7a 추가: 점검 코드 = 학습 데이터를 만든 코드 확인\n",
         f"같은 {N_CHECK}쌍 (시드 {CHECK_SEED}): 전체 데이터로 float64 재계산한 특징 vs 저장된 float32 데이터셋 값. "
         f"상대 오차 = |저장값 - 재계산값| / |재계산값| (재계산값 0 이면 절대 오차), 기준 {REL_TOL:.0e}. "
         "같은 날 순위 특징은 표본 날짜의 모든 종목을 전체 데이터로 다시 계산해 순위를 다시 매김. is_kospi 는 유니버스 파일 값과 비교.\n",
         f"**결과: {'모든 특징이 기준 안' if not bad else '기준을 넘은 특징 있음'}**"
         + (f" — {bad} (특징: 기준을 넘은 쌍 수)" if bad else "") + "\n",
         "| 특징 | 최대 상대 오차 | 기준 넘은 쌍 |\n|---|---|---|"]
    for c in feats:
        v = np.array(errs[c])
        L.append(f"| {c} | {v.max():.1e} | {int((v > REL_TOL).sum())} / {len(v)} |")
    L.append(f"\n({time.time() - t0:.0f}s)")
    with open(os.path.join(OUT, "check7a_stored.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("\n".join(L))
    return not bad


if __name__ == "__main__":
    if sys.argv[1] in ("check7a", "check_stored"):
        sys.exit(0 if {"check7a": check7a, "check_stored": check_stored}[sys.argv[1]]() else 1)
    {"build": build}[sys.argv[1]]()
