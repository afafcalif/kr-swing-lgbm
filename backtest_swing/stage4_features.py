"""
4단계: 특징·라벨 생성 + 누출 점검 (명세: backtest_swing/STAGE4_SPEC.md)

  python backtest_swing/stage4_features.py build      # results/stage4/dataset.parquet, sim.npz
  python backtest_swing/stage4_features.py leakcheck  # 무작위 종목·날짜에서 'S일까지 데이터만으로' 다시 계산해 비교

대상(신호일 S): KOSPI·KOSDAQ 개별 종목, 20일 평균 거래대금 >= 50억, 원주가 종가 >= 1,000원, 상장 후 60거래일 이후,
               거래량 > 0, S 종가가 원주가 기준 상한가(+29.5% 이상) 아님, S 종가 >= 20일선. 정배열 조건 없음.
신호일 범위: 2020-01-02 ~ 2025-12-31 (2026년은 만들지 않는다. 검증 때 따로 만든다)
라벨: S 종가 진입, 손절 = 진입가 - 1.7 x ATR20(S), S+1 종가부터 20일선 이탈 시 다음 날 시가 매도, 익절 없음,
      60거래일째 종가(라벨 끝내기용 상한), 개발 구간 끝(2025-12-30)에서 정리. 비용 포함 순수익률.
      y = 순수익률 - 같은 날 대상 종목 전체 순수익률 평균. 학습용 y_clip 은 +-30% 에서 자른다.
특징: S일 장 마감까지 알 수 있는 값. 수급만 S-1 까지.
"""
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage3_backtest as sb  # noqa: E402  (simulate, 비용·세율 상수 재사용)
import stage3_data as sd  # noqa: E402
from levels import round_tier  # noqa: E402

OUT = "backtest_swing/results/stage4"
FLOW_DIR = "data/flow"
KOSDAQ = "data/kosdaq_index_long.parquet"
SIG_START, DEV_END = pd.Timestamp("2020-01-02"), pd.Timestamp("2025-12-31")
SIM_ROW_START = pd.Timestamp("2019-12-02")
STOP_ATR, LABEL_CAP, CLIP = 1.7, 60, 0.30
FLOW_COLS = ("frgn", "orgn", "ind")

SR_FEATS = ["sup_dist_pct", "sup_dist_atr", "sup_score", "sup_tier", "res_dist_pct", "res_dist_atr", "res_score",
            "res_tier", "round_dist_pct", "round_tier"]
FLOW_FEATS = [f"{c}_{w}d" for c in FLOW_COLS for w in (1, 5, 20)] + ["frgn_streak", "orgn_streak"]


# ---------------------------------------------------------------- per-stock features (S 까지만 사용)

def stock_features(df: pd.DataFrame, flow: pd.DataFrame | None) -> pd.DataFrame:
    c, h, l, o, v = df.close, df.high, df.low, df.open, df.volume
    f = pd.DataFrame(index=df.index)
    for n in (1, 5, 20, 60, 120, 250):
        f[f"ret_{n}"] = c / c.shift(n) - 1
    ma = {n: c.rolling(n).mean() for n in (5, 20, 60, 120)}
    for n, m in ma.items():
        f[f"dist_ma{n}"] = c / m - 1
    f["slope_ma20"] = ma[20] / ma[20].shift(5) - 1
    f["slope_ma60"] = ma[60] / ma[60].shift(5) - 1
    f["aligned"] = ((ma[5] > ma[20]) & (ma[20] > ma[60])).astype(float)
    for n in (20, 60, 250):
        f[f"dist_high{n}"] = c / h.rolling(n).max() - 1
        f[f"dist_low{n}"] = c / l.rolling(n).min() - 1
    f["days_since_high250"] = h.rolling(250).apply(lambda x: len(x) - 1 - int(np.argmax(x)), raw=True)
    atr = df.atr20
    f["atr_pct"] = atr / c
    f["vol_std20"] = c.pct_change().rolling(20).std()
    f["range_atr"] = (h - l) / atr
    vavg = v.shift(1).rolling(20).mean()
    f["log_amt20"] = np.log(df.amt20.clip(lower=1))
    f["vol_ratio"] = v / vavg
    f["spike_days20"] = ((v >= 2 * vavg) & (vavg > 0)).astype(float).rolling(20).sum()
    rng = (h - l).replace(0, np.nan)
    f["close_pos"] = (c - l) / rng
    f["body_ratio"] = (c - o).abs() / rng
    f["upper_wick"] = (h - np.maximum(o, c)) / rng
    limit_up = (df.raw_close >= df.raw_prev_close * (1 + sb.LIMIT)).astype(float)
    f["limit_up20"] = limit_up.rolling(20).sum()
    # 수급: S-1 까지 (당일 투자자별 확정치는 장 마감 후)
    if flow is not None and len(flow):
        fl = df[["timestamp"]].merge(flow, on="timestamp", how="left")
        amt = df.amt20.to_numpy()
        for col in FLOW_COLS:
            x = fl[col] * 1e6
            for w in (1, 5, 20):
                f[f"{col}_{w}d"] = (x.rolling(w).sum() / amt).shift(1)
        for col in ("frgn", "orgn"):
            pos = (fl[col] > 0).astype(int)
            streak = pos.groupby((pos == 0).cumsum()).cumsum().where(fl[col].notna())
            f[f"{col}_streak"] = streak.shift(1)
    else:
        for name in FLOW_FEATS:
            f[name] = np.nan
    return f


def eligible_mask(df: pd.DataFrame) -> pd.Series:
    return ((df.index >= sd.MIN_LISTED_BARS) & (df.amt20 >= sd.MIN_AMOUNT_20) & (df.raw_close >= sd.MIN_RAW_CLOSE)
            & (df.volume > 0) & df.factor.notna() & df.atr20.notna()
            & (df.raw_close < df.raw_prev_close * (1 + sb.LIMIT))
            # 20일선 아래에서 사면 다음 날 추세 청산으로 하루 만에 팔려 비용만 내는 거래가 된다 (정배열 조건은 없음)
            & (df.close >= df.ma20))


def sr_features(fb: sd.FastB, df: pd.DataFrame, i: int) -> list:
    c, atr = df.close.iat[i], df.atr20.iat[i]
    ratio = float(df.factor.iat[i])
    K, Kr, sc = fb.zones(i, ratio)
    out = [np.nan] * len(SR_FEATS)
    below, above = np.flatnonzero(K < c), np.flatnonzero(K > c)
    if len(below):
        j = below[-1]
        out[0:4] = [c / K[j] - 1, (c - K[j]) / atr, sc[j], round_tier(int(Kr[j]))]
    if len(above):
        j = above[0]
        out[4:8] = [K[j] / c - 1, (K[j] - c) / atr, sc[j], round_tier(int(Kr[j]))]
    rc = df.raw_close.iat[i]
    if len(fb.k_raw):
        p = np.searchsorted(fb.k_raw, rc)
        cand = [x for x in (p - 1, p) if 0 <= x < len(fb.k_raw)]
        j = min(cand, key=lambda x: abs(fb.k_raw[x] - rc))
        out[8:10] = [fb.k_raw[j] / rc - 1, fb.k_tier[j]]
    return out


def load_flow(code: str):
    p = os.path.join(FLOW_DIR, f"{code}.parquet")
    if not os.path.exists(p):
        return None
    try:
        fl = pd.read_parquet(p)
    except Exception:  # noqa: BLE001  (수집 중에 쓰고 있는 파일)
        return None
    fl["timestamp"] = pd.to_datetime(fl.timestamp)
    return fl


# ---------------------------------------------------------------- build

class SimArrays:
    """stage3_backtest.simulate 이 쓰는 전역 배열 (행 = 종목·날짜, 종목별로 이어 붙임)"""

    def __init__(self, z):
        for k in ("o", "h", "l", "c", "ma20", "atr20", "vol", "ro", "rh", "rl", "rc", "rpc", "cal", "sid", "dev_last"):
            setattr(self, k, z[k])
        self.cal_dates = pd.DatetimeIndex(z["cal_dates"])
        self.year = self.cal_dates.year.to_numpy()[self.cal]
        # 2019년 행은 매도에 쓰이지 않지만 배열을 숫자로 유지하려고 당시 세율(0.25%)을 넣는다
        self.tax = np.array([sb.SELL_TAX.get(int(y), 0.0025) for y in self.year], dtype=float)


def market_features(cal: pd.DatetimeIndex) -> pd.DataFrame:
    k = pd.read_parquet(sd.KOSPI).set_index("timestamp").close
    q = pd.read_parquet(KOSDAQ).set_index("timestamp").close
    m = pd.DataFrame(index=k.index)
    for name, s in (("kospi", k), ("kosdaq", q.reindex(k.index))):
        for n in (5, 20, 60):
            m[f"{name}_ret{n}"] = s / s.shift(n) - 1
    m["kospi_ma120_pos"] = k / k.rolling(120).mean() - 1
    return m


def build():
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    cal = pd.DatetimeIndex(pd.read_parquet(sd.KOSPI).timestamp).sort_values()
    codes = sd.universe_codes()
    uni = pd.read_csv(sd.UNIVERSE, dtype={"code": str})
    market = dict(zip(uni.code, uni.market))
    cols = {k: [] for k in ("o", "h", "l", "c", "ma20", "atr20", "vol", "ro", "rh", "rl", "rc", "rpc", "cal", "sid")}
    dev_last, parts, off, n_flow = [], [], 0, 0
    for sid, code in enumerate(codes):
        df = sd.load_stock(code)
        flow = load_flow(code)
        n_flow += flow is not None
        feats = stock_features(df, flow)
        keep = (df.timestamp >= SIM_ROW_START).to_numpy()
        first = int(np.argmax(keep)) if keep.any() else len(df)
        sub = df.iloc[first:]
        for k, src in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("ma20", "ma20"), ("atr20", "atr20"),
                       ("vol", "volume"), ("ro", "raw_open"), ("rh", "raw_high"), ("rl", "raw_low"), ("rc", "raw_close"),
                       ("rpc", "raw_prev_close")):
            cols[k].append(sub[src].to_numpy(np.float64))
        cols["cal"].append(cal.searchsorted(sub.timestamp.to_numpy()).astype(np.int32))
        cols["sid"].append(np.full(len(sub), sid, np.int32))
        nd = int((sub.timestamp <= DEV_END).sum())
        dev_last.append(off + nd - 1 if nd > 0 else -1)

        el = eligible_mask(df) & (df.timestamp >= SIG_START) & (df.timestamp <= DEV_END)
        idx = np.flatnonzero(el.to_numpy())
        if len(idx):
            fb = sd.FastB(df)
            sr = np.array([sr_features(fb, df, int(i)) for i in idx], dtype=float)
            part = feats.iloc[idx].astype(np.float32).copy()
            for j, name in enumerate(SR_FEATS):
                part[name] = sr[:, j].astype(np.float32)
            part.insert(0, "date", df.timestamp.iloc[idx].values)
            part.insert(0, "code", code)
            part["is_kospi"] = np.float32(market.get(code) == "KOSPI")
            part["g"] = (off + (idx - first)).astype(np.int64)
            part["close"] = df.close.iloc[idx].to_numpy(np.float32)
            part["above_ma20"] = (df.close.iloc[idx] > df.ma20.iloc[idx]).to_numpy(np.float32)
            parts.append(part)
        off += len(sub)
        if (sid + 1) % 200 == 0:
            print(f"  build {sid + 1}/{len(codes)} ({time.time() - t0:.0f}s, rows {sum(len(p) for p in parts):,}, flow files {n_flow})", flush=True)

    arr = {k: np.concatenate(v) for k, v in cols.items()}
    dl = np.array(dev_last)
    arr["dev_last"] = dl[arr["sid"]]
    np.savez_compressed(os.path.join(OUT, "sim.npz"), **arr, cal_dates=cal.values.astype("datetime64[ns]"), codes=np.array(codes))
    ds = pd.concat(parts, ignore_index=True)
    del parts

    # 상대 강도·시장 폭 (같은 날 대상 종목 안에서)
    grp = ds.groupby("date")
    ds["rank_ret20"] = grp.ret_20.rank(pct=True).astype(np.float32)
    ds["rank_ret60"] = grp.ret_60.rank(pct=True).astype(np.float32)
    ds["rank_amt20"] = grp.log_amt20.rank(pct=True).astype(np.float32)
    ds["breadth_ma20"] = grp.above_ma20.transform("mean").astype(np.float32)
    mk = market_features(cal)
    ds = ds.merge(mk.astype(np.float32), left_on="date", right_index=True, how="left")

    # 라벨: 실제 매매 규칙 그대로
    g = SimArrays(np.load(os.path.join(OUT, "sim.npz")))
    T = ds.g.to_numpy()
    stop = g.c[T] - STOP_ATR * g.atr20[T]
    sim = sb.simulate(g, T, stop, np.full(len(T), np.nan), "full", max_hold=LABEL_CAP, kmax=LABEL_CAP + 1)
    ds["net"] = sim["net"].astype(np.float32)
    ds["hold"] = sim["k"]
    ds["reason"] = sim["reason"]
    ds["y"] = (ds.net - ds.groupby("date").net.transform("mean")).astype(np.float32)
    ds["y_clip"] = ds.y.clip(-CLIP, CLIP)
    ds.to_parquet(os.path.join(OUT, "dataset.parquet"), index=False)
    feat_cols = [c for c in ds.columns if c not in ("code", "date", "g", "close", "above_ma20", "net", "hold", "reason", "y", "y_clip")]
    print(f"build done: {len(ds):,} rows, {ds.code.nunique()} stocks, {ds.date.nunique()} days, {len(feat_cols)} features "
          f"({time.time() - t0:.0f}s)")
    r = pd.Series(sim["reason"]).map(sb.REASON).value_counts(normalize=True) * 100
    print("label exits %:", r.round(1).to_dict())
    print(f"label cap (60d) hit: {(ds.reason == sb.R_MAXHOLD).mean() * 100:.1f}% | trend exit within 3 days: "
          f"{((ds.reason == sb.R_TREND) & (ds.hold <= 3)).mean() * 100:.1f}% | net mean {ds.net.mean() * 100:+.3f}%")
    print("rows per year:", ds.date.dt.year.value_counts().sort_index().to_dict())
    print("missing share of flow features:", round(ds.frgn_1d.isna().mean() * 100, 1), "%")


# ---------------------------------------------------------------- leak check

def leakcheck(n=300, seed=0):
    """S일까지 데이터만 남기고(수급은 S-1 까지) 다시 계산한 특징이 저장된 값과 같은지 확인."""
    ds = pd.read_parquet(os.path.join(OUT, "dataset.parquet"))
    feats = [c for c in ds.columns if c not in ("code", "date", "g", "close", "above_ma20", "net", "hold", "reason", "y", "y_clip",
                                                "rank_ret20", "rank_ret60", "rank_amt20", "breadth_ma20", "is_kospi")
             and not c.startswith(("kospi_", "kosdaq_"))]
    rng = np.random.default_rng(seed)
    sample = ds.iloc[rng.choice(len(ds), n, replace=False)]
    bad = []
    for r in sample.itertuples():
        cut = sd.load_stock(r.code, until=r.date)  # 원본 파일에서부터 S 이후를 잘라내고 지표를 다시 계산
        i = len(cut) - 1
        assert cut.timestamp.iat[i] == r.date
        fl = load_flow(r.code)
        fl_cut = None if fl is None else fl[fl.timestamp < r.date]  # S-1 까지
        f = stock_features(cut, fl_cut).iloc[-1]
        srv = sr_features(sd.FastB(cut), cut, i)
        f = pd.concat([f, pd.Series(srv, index=SR_FEATS)])
        stored = ds.loc[r.Index, feats]
        for col in feats:
            a, b = float(f[col]), float(stored[col])
            if not ((np.isnan(a) and np.isnan(b)) or np.isclose(a, b, rtol=1e-4, atol=1e-6)):
                bad.append((r.code, r.date.date(), col, a, b))
    # 시장 특징: 그날까지의 지수만으로 다시 계산
    cal = pd.DatetimeIndex(pd.read_parquet(sd.KOSPI).timestamp).sort_values()
    mk_full = market_features(cal)
    for d in rng.choice(ds.date.unique(), 30, replace=False):
        d = pd.Timestamp(d)
        mk_cut = market_features(cal[cal <= d]).loc[d]
        if not np.allclose(mk_cut.to_numpy(float), mk_full.loc[d].to_numpy(float), equal_nan=True):
            bad.append(("MARKET", d.date(), "market", np.nan, np.nan))
    print(f"leak check: {n} stock-days x {len(feats)} features + 30 market days -> mismatches {len(bad)}")
    for b in bad[:20]:
        print("   ", b)
    return bad


if __name__ == "__main__":
    if sys.argv[1] == "leakcheck":
        # 불일치가 하나라도 있으면 종료 코드 1 (뒤 단계인 학습으로 넘어가지 않게)
        sys.exit(1 if leakcheck() else 0)
    {"build": build}[sys.argv[1]]()
