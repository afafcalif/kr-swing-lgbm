"""
3단계 공통 데이터: 종목별 수정주가 + 원주가 + 지표 + 대상 여부, 그리고 빠른 B 탐지기(다중 기간)

수정주가: data/daily_ext (2019-01~) 가 있으면 그것, 없으면 data/daily_merged
원주가:   data/daily_raw (factor = 수정주가 / 원주가)
대상 여부 (신호일): 정배열(5>20>60), 20일 평균 거래대금 >= 50억, 원주가 종가 >= 1,000원, 상장 후 60거래일 이후,
                  거래량 > 0, 환산 계수 있음

fast_zones() 는 levels.RoundFirstDetector + multi_period_zones (B, 띠 2%, 기준 5점, 125/250/500일) 와
같은 결과를 내도록 벡터화한 것이다. verify_fast_zones() 로 일치를 확인한다.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from levels import PERIODS, largest_unit, round_candidates, round_tier  # noqa: E402

EXT_DIR, MERGED_DIR, RAW_DIR = "data/daily_ext", "data/daily_merged", "data/daily_raw"
UNIVERSE = "data/universe_20261019.csv"
KOSPI = "data/kospi_index_long.parquet"
EXCLUDE = {"096250", "101970", "036220"}

MIN_AMOUNT_20 = 5_000_000_000
MIN_RAW_CLOSE = 1_000
MIN_LISTED_BARS = 60
BAND, MIN_SCORE, SWING_N, SPIKE_MULT, WINDOW = 0.02, 5, 5, 2.0, 0.3


def universe_codes() -> list[str]:
    uni = pd.read_csv(UNIVERSE, dtype={"code": str})
    codes = set(uni[uni.market.isin(["KOSPI", "KOSDAQ"])].code)
    have = {f[:-8] for f in os.listdir(RAW_DIR) if f.endswith(".parquet")}
    return sorted((codes & have) - EXCLUDE)


def load_stock(code: str, until=None) -> pd.DataFrame:
    """until 을 주면 그 날짜까지의 원본 행만 읽어서 지표를 계산한다 (누출 점검용)."""
    p = os.path.join(EXT_DIR, f"{code}.parquet")
    adj = pd.read_parquet(p if os.path.exists(p) else os.path.join(MERGED_DIR, f"{code}.parquet"))
    adj["timestamp"] = pd.to_datetime(adj["timestamp"])
    raw = pd.read_parquet(os.path.join(RAW_DIR, f"{code}.parquet"))
    if until is not None:
        adj = adj[adj.timestamp <= pd.Timestamp(until)]
        raw = raw[pd.to_datetime(raw.timestamp) <= pd.Timestamp(until)]
    raw = raw.rename(columns={c: f"raw_{c}" for c in ("open", "high", "low", "close")})
    df = adj[["timestamp", "open", "high", "low", "close", "volume", "amount"]].merge(
        raw[["timestamp", "raw_open", "raw_high", "raw_low", "raw_close", "factor"]], on="timestamp", how="inner")
    df = df.sort_values("timestamp").reset_index(drop=True)
    c = df["close"]
    df["ma5"], df["ma20"], df["ma60"] = c.rolling(5).mean(), c.rolling(20).mean(), c.rolling(60).mean()
    df["amt20"] = df["amount"].rolling(20).mean()
    pc = c.shift(1)
    tr = np.maximum(df.high - df.low, np.maximum((df.high - pc).abs(), (df.low - pc).abs()))
    df["atr20"] = tr.rolling(20).mean()
    df["raw_prev_close"] = df["raw_close"].shift(1)
    df["eligible"] = ((df.index >= MIN_LISTED_BARS) & df.ma60.notna() & (df.ma5 > df.ma20) & (df.ma20 > df.ma60)
                      & (df.amt20 >= MIN_AMOUNT_20) & (df.raw_close >= MIN_RAW_CLOSE) & (df.volume > 0)
                      & df.factor.notna())
    return df


def kospi_filter() -> pd.Series:
    """날짜 -> 그날 코스피 종가가 120일선 아래인가 (신규 진입 차단 판단은 진입일 '전날' 값을 쓴다)"""
    k = pd.read_parquet(KOSPI).sort_values("timestamp").set_index("timestamp")["close"]
    return (k < k.rolling(120).mean()).rename("kospi_below_ma120")


class FastB:
    """한 종목의 B 다중 기간 존을 날짜별로 빠르게 계산"""

    def __init__(self, df: pd.DataFrame):
        h, l, c, o, v = (df[x].to_numpy(float) for x in ("high", "low", "close", "open", "volume"))
        self.h, self.l, self.c = h, l, c
        win = 2 * SWING_N + 1
        rmax = pd.Series(h).rolling(win, center=True).max().to_numpy()
        rmin = pd.Series(l).rolling(win, center=True).min().to_numpy()
        self.hi_idx = np.flatnonzero((h == rmax) & ~np.isnan(rmax))
        self.lo_idx = np.flatnonzero((l == rmin) & ~np.isnan(rmin))
        vol_avg = pd.Series(v).shift(1).rolling(20).mean().to_numpy()
        self.sp_idx = np.flatnonzero((v >= SPIKE_MULT * vol_avg) & (vol_avg > 0))
        self.body_lo, self.body_hi = np.minimum(o, c), np.maximum(o, c)
        raw = df["raw_close"].to_numpy(float)
        lo = max(1.0, np.nanmin(raw) * (1 - WINDOW) * 0.5)
        hi = np.nanmax(raw) * (1 + WINDOW) * 2
        ks = []
        d = 10 ** int(np.floor(np.log10(lo)))
        while d <= hi:
            ks += round_candidates(max(lo, d), min(hi, d * 10), 1)
            d *= 10
        ks = sorted(set(ks))
        self.k_raw = np.array([k for k, _ in ks], float)
        self.k_tier = np.array([t for _, t in ks], int)
        self.k_ur = np.array([largest_unit(int(k)) / k for k, _ in ks], float)

    def _period(self, i, lookback, kc, tier, ur, ratio):
        s = max(0, i - lookback)
        e = i - SWING_N
        if e >= s:
            a = self.hi_idx[np.searchsorted(self.hi_idx, s):np.searchsorted(self.hi_idx, e, "right")]
            b = self.lo_idx[np.searchsorted(self.lo_idx, s):np.searchsorted(self.lo_idx, e, "right")]
            sw = np.sort(np.concatenate([self.h[a], self.l[b]]))
        else:
            sw = np.array([])
        closes = self.c[s:i + 1]
        cs = np.sort(closes)
        c_range = cs[-1] - cs[0]
        sp = self.sp_idx[np.searchsorted(self.sp_idx, s):np.searchsorted(self.sp_idx, i, "right")]
        k = kc * ratio
        lo, hi = k * (1 - BAND), k * (1 + BAND)
        n_sw = np.searchsorted(sw, hi, "right") - np.searchsorted(sw, lo, "left")
        swp = np.where(n_sw >= 4, 2, np.where(n_sw >= 2, 1, 0))
        days = np.searchsorted(cs, hi, "right") - np.searchsorted(cs, lo, "left")
        if c_range > 0:
            dens = days / (len(closes) * (hi - lo) / c_range)
        else:
            dens = np.zeros(len(k))
        dw = np.where(dens >= 2.5, 2, np.where(dens >= 1.5, 1, 0))
        if len(sp):
            vk = ((self.body_lo[sp][None, :] <= hi[:, None]) & (self.body_hi[sp][None, :] >= lo[:, None])).any(axis=1)
        else:
            vk = np.zeros(len(k), bool)
        score = tier + swp + dw + vk.astype(int)
        keep = np.flatnonzero(score >= MIN_SCORE)
        if len(keep) == 0:
            return keep, score
        order = keep[np.lexsort((-ur[keep], -tier[keep], -score[keep]))]
        kept = []
        for j in order:
            if all(abs(k[j] - k[m]) > BAND * (k[j] + k[m]) for m in kept):
                kept.append(j)
        return np.array(sorted(kept), int), score

    def zones(self, i: int, ratio: float):
        """반환: (K 수정주가 배열, K 원주가 배열, 점수 배열) - 다중 기간 합친 결과, K 오름차순"""
        close = self.c[i]
        lo_r, hi_r = close * (1 - WINDOW) / ratio, close * (1 + WINDOW) / ratio
        a, b = np.searchsorted(self.k_raw, lo_r, "left"), np.searchsorted(self.k_raw, hi_r, "right")
        kc, tier, ur = self.k_raw[a:b], self.k_tier[a:b], self.k_ur[a:b]
        best, nper = {}, {}
        for per in PERIODS:
            kept, score = self._period(i, per, kc, tier, ur, ratio)
            for j in kept:
                key = int(kc[j])
                best[key] = max(best.get(key, -1), int(score[j]))
                nper[key] = nper.get(key, 0) + 1
        keys = sorted(best)
        kr = np.array(keys, float)
        return kr * ratio, kr, np.array([best[x] + nper[x] - 1 for x in keys], int)


def verify_fast_zones(codes: list[str], n_days: int = 40, seed: int = 0) -> int:
    """levels.py 의 B 탐지기와 비교. 불일치 건수를 반환."""
    from levels import RoundFirstDetector, RoundParams, multi_period_zones
    rng = np.random.default_rng(seed)
    bad = 0
    for code in codes:
        df = load_stock(code)
        fb = FastB(df)
        det = RoundFirstDetector(df[["timestamp", "open", "high", "low", "close", "volume"]],
                                 RoundParams(band_pct=BAND, min_score=MIN_SCORE), 1.0)
        days = rng.choice(np.flatnonzero(df.eligible.to_numpy()), size=min(n_days, int(df.eligible.sum())), replace=False)
        for i in days:
            ratio = float(df.factor.iat[i])
            det.ratio = ratio
            ref = multi_period_zones(det, df.timestamp.iat[i])
            ref_k = np.sort(ref.center.to_numpy(float)) if len(ref) else np.array([])
            ref_s = ref.sort_values("center").score.to_numpy(int) if len(ref) else np.array([], int)
            k, _, s = fb.zones(int(i), ratio)
            if len(k) != len(ref_k) or not np.allclose(k, ref_k) or not np.array_equal(s, ref_s):
                bad += 1
    return bad
