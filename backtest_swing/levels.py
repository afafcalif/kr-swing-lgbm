"""
지지·저항 존 탐지기 (1·2단계, 3단계에서도 이 파일을 쓴다)

라운드 숫자 (A·B·C 공통)
  단위 집합 {1, 2, 2.5, 5} x 10^n. 가격 K 를 나누어떨어지게 하는 가장 큰 단위 u 로 등급을 매긴다.
    u/K >= 8%  -> 3,  4% <= u/K < 8% -> 2,  1.6% <= u/K < 4% -> 1,  그 미만 -> 라운드 아님
  라운드 판단은 사용자가 본 차트 가격 기준이다. parquet(현재 기준 수정주가) 가격을 price_ratio 로 나눠
  차트 가격으로 바꾼 뒤 라운드를 따지고, 다시 price_ratio 를 곱해 parquet 가격으로 옮긴다.

A. 스윙 군집형 (LevelDetector, volume_evidence="vp")
  존 = 과거 스윙 고·저점이 +-cluster_pct 안에 min_touches 번 이상 모인 가격대. 점수 = 겹친 근거 수.
    스윙 군집 +1 / 라운드(등급 >= 2: major, >= 1: any) +1 / 20·60·120일선 +1씩 / 매물대 +1
C. A 의 거래량 변형 (LevelDetector, volume_evidence="spike")
  매물대 대신 "거래량 터진 봉"(거래량 >= 직전 20일 평균 x 2)의 몸통이 존과 겹치면 +1
B. 라운드 우선형 (RoundFirstDetector)
  라운드 후보 K 마다 +-band_pct 띠 안의 근거로 점수: 등급(1~3) + 스윙점(2개 +1, 4개 +2)
  + 머문 날(같은 폭 평균 대비 1.5배 +1, 2.5배 +2) + 거래량 터진 봉 몸통(+1). min_score 이상만 남기고,
  띠가 겹치는 후보끼리는 점수 > 등급 > 더 둥근 숫자 순으로 하나만 남긴다. 존은 선 K 하나 (lower = upper = K).
다중 기간 (multi_period_zones)
  lookback 125 / 250 / 500 으로 각각 존을 만들고 겹치는 존을 합친다. 경계는 점수가 가장 높은 기간의 존,
  다른 기간에도 나오면 기간 하나당 +1.

미래 정보 차단: 스윙점은 오른쪽 N일이 지나야 확정되므로 날짜 D 의 존에는 D - N 일까지 확정된 것만 쓴다.
이평선·매물대·머문 날·거래량 봉도 D 까지만 쓴다.

실행 (F:\\stock 에서):
    .venv-feat/Scripts/python.exe backtest_swing/levels.py 005930 2025-06-30
    .venv-feat/Scripts/python.exe backtest_swing/levels.py 005930 2025-06-30 --family B --band 0.02 --min-score 5
"""
import argparse
import os
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

MERGED_DIR = "data/daily_merged"
UNITS = sorted({int(m * 10 ** k) for k in range(1, 9) for m in (1, 2, 2.5, 5)})
TIER_BOUNDS = ((0.08, 3), (0.04, 2), (0.016, 1))
PERIODS = (125, 250, 500)


# ---------------------------------------------------------------- round numbers

def largest_unit(k: int) -> int:
    for u in reversed(UNITS):
        if u <= k and k % u == 0:
            return u
    return 1


def round_tier(k: int) -> int:
    r = largest_unit(k) / k
    for bound, tier in TIER_BOUNDS:
        if r >= bound:
            return tier
    return 0


def round_candidates(lo: float, hi: float, min_tier: int = 1) -> list[tuple[int, int]]:
    """[lo, hi] (차트 가격) 안의 라운드 숫자와 등급. 등급 1 이상은 u >= 1.6% x K 인 단위의 배수뿐이다."""
    if hi <= 0 or hi < lo:
        return []
    out = set()
    for u in UNITS:
        if u < 0.016 * lo:
            continue
        if u > hi:
            break
        for k in range(int(np.ceil(lo / u)) * u, int(hi) + 1, u):
            t = round_tier(k)
            if t >= min_tier:
                out.add((k, t))
    return sorted(out)


# ---------------------------------------------------------------- shared series

def _prepare(df: pd.DataFrame, swing_n: int, spike_mult: float):
    df = df.sort_values("timestamp").reset_index(drop=True)
    h, l, c, o, v = (df[x].to_numpy(float) for x in ("high", "low", "close", "open", "volume"))
    win = 2 * swing_n + 1
    rmax = pd.Series(h).rolling(win, center=True).max().to_numpy()
    rmin = pd.Series(l).rolling(win, center=True).min().to_numpy()
    vol_avg = pd.Series(v).shift(1).rolling(20).mean().to_numpy()
    return {
        "df": df, "dates": pd.DatetimeIndex(df["timestamp"]), "h": h, "l": l, "c": c, "o": o, "v": v,
        "swing_hi": (h == rmax) & ~np.isnan(rmax), "swing_lo": (l == rmin) & ~np.isnan(rmin),
        "spike": (v >= spike_mult * vol_avg) & (vol_avg > 0),
        "body_lo": np.minimum(o, c), "body_hi": np.maximum(o, c),
    }


def _index_of(dates: pd.DatetimeIndex, date) -> int:
    i = dates.searchsorted(pd.Timestamp(date), side="right") - 1
    if i < 0:
        raise ValueError(f"{date} 이전 데이터가 없습니다")
    return int(i)


# ---------------------------------------------------------------- A / C

@dataclass(frozen=True)
class LevelParams:
    swing_n: int = 5
    cluster_pct: float = 0.015
    min_touches: int = 2
    swing_lookback: int = 250
    ma_periods: tuple = (20, 60, 120)
    vp_days: int = 120
    vp_bins: int = 40
    vp_top_frac: float = 0.2
    round_mode: str = "major"        # "major": 등급 2 이상 / "any": 등급 1 이상
    volume_evidence: str = "vp"      # "vp": 매물대 (A) / "spike": 거래량 터진 봉 (C)
    spike_mult: float = 2.0


class LevelDetector:
    def __init__(self, df: pd.DataFrame, params: LevelParams = LevelParams(), price_ratio: float = 1.0):
        self.p, self.ratio = params, price_ratio
        s = _prepare(df, params.swing_n, params.spike_mult)
        self.__dict__.update(s)
        self.ma = {k: self.df["close"].rolling(k).mean().to_numpy() for k in params.ma_periods}

    def index_of(self, date) -> int:
        return _index_of(self.dates, date)

    @property
    def close(self):
        return self.c

    def zones(self, date, lookback: int | None = None) -> pd.DataFrame:
        p, i = self.p, self.index_of(date)
        lookback = lookback or p.swing_lookback
        start = max(0, i - lookback)
        idx = np.arange(start, max(start, i - p.swing_n + 1))
        pts = sorted([(self.h[j], "고점", j) for j in idx if self.swing_hi[j]] +
                     [(self.l[j], "저점", j) for j in idx if self.swing_lo[j]], key=lambda x: x[0])
        clusters, cur = [], []
        for pt in pts:
            if cur and pt[0] > cur[0][0] * (1 + 2 * p.cluster_pct):
                clusters.append(cur)
                cur = []
            cur.append(pt)
        if cur:
            clusters.append(cur)

        vp_top = self._volume_profile_top(i) if p.volume_evidence == "vp" else []
        if p.volume_evidence == "spike":
            sp = np.flatnonzero(self.spike[start:i + 1]) + start
            spikes = list(zip(self.body_lo[sp], self.body_hi[sp]))
        min_tier = 2 if p.round_mode == "major" else 1
        close, rows = self.c[i], []
        for cl in clusters:
            if len(cl) < p.min_touches:
                continue
            lo, hi = cl[0][0], cl[-1][0]
            ev = [f"스윙 {len(cl)}회"]
            rc = round_candidates(lo / self.ratio, hi / self.ratio, min_tier)
            if rc:
                k, t = max(rc, key=lambda x: x[1])
                ev.append(f"라운드 {k:,} (등급 {t})")
            for k, ma in self.ma.items():
                if not np.isnan(ma[i]) and lo <= ma[i] <= hi:
                    ev.append(f"MA{k}")
            if p.volume_evidence == "vp" and any(lo <= b and a <= hi for a, b in vp_top):
                ev.append("매물대")
            if p.volume_evidence == "spike" and any(lo <= b and a <= hi for a, b in spikes):
                ev.append("거래량봉")
            rows.append({"lower": lo, "upper": hi, "center": (lo + hi) / 2, "score": len(ev),
                         "role": "지지" if hi < close else "저항" if lo > close else "현재가 위치",
                         "evidence": ", ".join(ev)})
        return pd.DataFrame(rows, columns=["lower", "upper", "center", "score", "role", "evidence"])

    def _volume_profile_top(self, i: int):
        p = self.p
        s = max(0, i - p.vp_days + 1)
        lo, hi = self.l[s:i + 1].min(), self.h[s:i + 1].max()
        if not np.isfinite(lo) or hi <= lo:
            return []
        edges = np.linspace(lo, hi, p.vp_bins + 1)
        typical = (self.h[s:i + 1] + self.l[s:i + 1] + self.c[s:i + 1]) / 3
        hist, _ = np.histogram(typical, bins=edges, weights=self.v[s:i + 1])
        k = max(1, int(round(p.vp_bins * p.vp_top_frac)))
        return [(edges[b], edges[b + 1]) for b in sorted(np.argsort(hist)[::-1][:k]) if hist[b] > 0]


# ---------------------------------------------------------------- B

@dataclass(frozen=True)
class RoundParams:
    band_pct: float = 0.02
    min_score: int = 5
    swing_n: int = 5
    lookback: int = 250
    price_window: float = 0.3          # 그날 종가 +-30% 안의 라운드 후보만
    swing_steps: tuple = (2, 4)
    dwell_steps: tuple = (1.5, 2.5)
    spike_mult: float = 2.0


class RoundFirstDetector:
    def __init__(self, df: pd.DataFrame, params: RoundParams = RoundParams(), price_ratio: float = 1.0):
        self.p, self.ratio = params, price_ratio
        self.__dict__.update(_prepare(df, params.swing_n, params.spike_mult))

    def index_of(self, date) -> int:
        return _index_of(self.dates, date)

    @property
    def close(self):
        return self.c

    def zones(self, date, lookback: int | None = None) -> pd.DataFrame:
        p, i = self.p, self.index_of(date)
        lookback = lookback or p.lookback
        s = max(0, i - lookback)
        conf = np.arange(s, max(s, i - p.swing_n + 1))
        swing_px = np.concatenate([self.h[conf][self.swing_hi[conf]], self.l[conf][self.swing_lo[conf]]])
        closes = self.c[s:i + 1]
        c_range = closes.max() - closes.min()
        sp = np.flatnonzero(self.spike[s:i + 1]) + s
        sp_lo, sp_hi = self.body_lo[sp], self.body_hi[sp]

        close = self.c[i]
        cands = round_candidates(close * (1 - p.price_window) / self.ratio,
                                 close * (1 + p.price_window) / self.ratio, 1)
        rows = []
        for k_chart, tier in cands:
            k = k_chart * self.ratio
            lo, hi = k * (1 - p.band_pct), k * (1 + p.band_pct)
            n_sw = int(((swing_px >= lo) & (swing_px <= hi)).sum())
            sw = 2 if n_sw >= p.swing_steps[1] else 1 if n_sw >= p.swing_steps[0] else 0
            days = int(((closes >= lo) & (closes <= hi)).sum())
            expected = len(closes) * (hi - lo) / c_range if c_range > 0 else np.inf
            dens = days / expected if expected > 0 else 0.0
            dw = 2 if dens >= p.dwell_steps[1] else 1 if dens >= p.dwell_steps[0] else 0
            vk = int(bool(((sp_lo <= hi) & (sp_hi >= lo)).any()))
            score = tier + sw + dw + vk
            if score >= p.min_score:
                rows.append({"lower": k, "upper": k, "center": k, "score": score, "tier": tier,
                             "unit_ratio": largest_unit(k_chart) / k_chart,
                             "role": "지지" if k < close else "저항" if k > close else "현재가 위치",
                             "evidence": f"라운드 {k_chart:,} (등급 {tier}), 스윙 {n_sw}개, 머문 날 {days}일 "
                                         f"(평균의 {dens:.1f}배), 거래량봉 {'있음' if vk else '없음'}"})
        z = pd.DataFrame(rows, columns=["lower", "upper", "center", "score", "tier", "unit_ratio", "role", "evidence"])
        if z.empty:
            return z.drop(columns="unit_ratio")
        keep = []
        # 띠가 겹치면 점수 > 등급 > 더 둥근 숫자(단위/가격이 큰 쪽) 순으로 남긴다
        for r in z.sort_values(["score", "tier", "unit_ratio"], ascending=False).itertuples():
            if all(abs(r.center - z.at[j, "center"]) > p.band_pct * (r.center + z.at[j, "center"]) for j in keep):
                keep.append(r.Index)
        return z.loc[sorted(keep)].drop(columns="unit_ratio").reset_index(drop=True)


# ---------------------------------------------------------------- multi period

def multi_period_zones(detector, date, periods=PERIODS) -> pd.DataFrame:
    parts = []
    for per in periods:
        z = detector.zones(date, lookback=per)
        if len(z):
            parts.append(z.assign(period=per))
    if not parts:
        return pd.DataFrame(columns=["lower", "upper", "center", "score", "role", "evidence", "periods"])
    allz = pd.concat(parts, ignore_index=True).sort_values("lower").reset_index(drop=True)
    groups, cur, cur_hi = [], [], -np.inf
    for r in allz.itertuples():
        if cur and r.lower > cur_hi:
            groups.append(cur)
            cur, cur_hi = [], -np.inf
        cur.append(r.Index)
        cur_hi = max(cur_hi, r.upper)
    groups.append(cur)
    rows = []
    for g in groups:
        sub = allz.loc[g]
        best = sub.sort_values(["score", "period"], ascending=[False, True]).iloc[0]
        n_per = sub.period.nunique()
        rows.append({**best.drop("period").to_dict(), "score": best.score + (n_per - 1),
                     "periods": ",".join(str(x) for x in sorted(sub.period.unique()))})
    return pd.DataFrame(rows)


def load(code: str) -> pd.DataFrame:
    return pd.read_parquet(os.path.join(MERGED_DIR, f"{code}.parquet"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("date")
    ap.add_argument("--family", choices=["A", "B", "C"], default="A")
    ap.add_argument("--multi", action="store_true", help="125/250/500일 다중 기간")
    ap.add_argument("--ratio", type=float, default=1.0, help="parquet 가격 / 차트 가격")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--pct", type=float, default=0.015)
    ap.add_argument("--vp-days", type=int, default=120)
    ap.add_argument("--round-mode", choices=["any", "major"], default="major")
    ap.add_argument("--band", type=float, default=0.02)
    ap.add_argument("--min-score", type=int, default=5)
    ap.add_argument("--range", type=float, default=0.3)
    args = ap.parse_args()

    df = load(args.code)
    if args.family == "B":
        det = RoundFirstDetector(df, RoundParams(band_pct=args.band, min_score=args.min_score), args.ratio)
    else:
        p = LevelParams(swing_n=args.n, cluster_pct=args.pct, vp_days=args.vp_days, round_mode=args.round_mode,
                        volume_evidence="vp" if args.family == "A" else "spike")
        det = LevelDetector(df, p, args.ratio)
    i = det.index_of(args.date)
    close = det.close[i]
    z = multi_period_zones(det, args.date) if args.multi else det.zones(args.date)
    z = z[(z.center >= close * (1 - args.range)) & (z.center <= close * (1 + args.range))]
    print(f"{args.code} {det.dates[i].date()} 종가 {close:,.0f}")
    print(z.sort_values("center", ascending=False).round(0).to_string(index=False))


if __name__ == "__main__":
    main()
