"""
3단계 개발 구간 백테스트 (명세: backtest_swing/STAGE3_SPEC.md)

  python backtest_swing/stage3_backtest.py build     # 1) 종목별 선·신호 계산 -> results/stage3/cache.npz
  python backtest_swing/stage3_backtest.py run       # 2) 36개 설정, 대조군, 포트폴리오, 선택, 보고

검증 구간(2026)은 이 스크립트로 실행하지 않는다 (DEV_END 이후 데이터는 거래에 쓰지 않음).
"""
import math
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage3_data as sd  # noqa: E402

OUT = "backtest_swing/results/stage3"
CACHE = os.path.join(OUT, "cache.npz")
# 2차 실행 (2026-10-20 정정: 10거래일 상한 청산 삭제). 1차 실행 결과는 results/stage3 에 그대로 둔다.
OUT_RUN = "backtest_swing/results/stage3_v2"
DEV_START, DEV_END = pd.Timestamp("2021-01-04"), pd.Timestamp("2025-12-31")
ROW_START = pd.Timestamp("2020-11-02")

COMMISSION, SLIPPAGE = 0.00015, 0.001
SELL_TAX = {2020: 0.0025, 2021: 0.0023, 2022: 0.0023, 2023: 0.0020, 2024: 0.0018, 2025: 0.0015, 2026: 0.0020}
CASH_RATE = {2021: 0.006, 2022: 0.020, 2023: 0.035, 2024: 0.034, 2025: 0.026, 2026: 0.037}
START_CAPITAL, POSITION_FRAC, MAX_POSITIONS = 5_000_000, 0.20, 3
# 보유 기간 상한 없음: 손절·익절·추세 청산 또는 개발 구간 끝까지 보유 (K_MAX 는 루프 상한일 뿐, 그 전에 모두 끝난다)
MAX_HOLD, K_MAX, LIMIT = 10 ** 9, 1300, 0.295
BUFFERS = {"3%": ("pct", 0.03), "1.0ATR": ("atr", 1.0), "1.7ATR": ("atr", 1.7)}
ENTRIES, TP_MODES, FILTERS = ("pullback", "breakout"), ("full", "half", "ladder"), (False, True)
N_REPS = 200
R_STOP, R_TREND, R_TP, R_MAXHOLD, R_END, R_BE, R_LADFAIL = 1, 2, 3, 4, 5, 6, 7
REASON = {R_STOP: "손절", R_TREND: "추세", R_TP: "익절", R_MAXHOLD: "10일", R_END: "구간끝", R_BE: "본전", R_LADFAIL: "돌파실패"}
TP_LABEL = {"full": "전량", "half": "절반", "ladder": "계단"}

CONFIGS = [{"entry": e, "filter": f, "tp": t, "buffer": b} for e in ENTRIES for f in FILTERS for t in TP_MODES for b in BUFFERS]


def cfg_label(c):
    return f"{'눌림' if c['entry'] == 'pullback' else '돌파'}|{'필터O' if c['filter'] else '필터X'}|{TP_LABEL[c['tp']]}|{c['buffer']}"


# ====================================================================== build

def build():
    os.makedirs(OUT, exist_ok=True)
    cal = pd.DatetimeIndex(pd.read_parquet(sd.KOSPI).timestamp).sort_values()
    codes = sd.universe_codes()
    cols = {k: [] for k in ("o", "h", "l", "c", "ma20", "atr20", "vol", "ro", "rh", "rl", "rc", "rpc", "cal", "sid")}
    stock_start, dev_last = [], []
    det = {k: [] for k in ("type", "S", "T", "K", "tp")}
    rnd = {k: [] for k in ("S", "K", "tp")}
    el = {k: [] for k in ("g", "n", "change")}
    lines_ptr, lines_val = [0], []
    off, t0 = 0, time.time()
    for sid, code in enumerate(codes):
        df = sd.load_stock(code)
        fb = sd.FastB(df)
        keep = df.timestamp >= ROW_START
        first = int(np.argmax(keep.to_numpy())) if keep.any() else len(df)
        sub = df.iloc[first:]
        n = len(sub)
        if n == 0:
            stock_start.append(off)
            dev_last.append(off - 1)
            continue
        for k, src in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("ma20", "ma20"), ("atr20", "atr20"),
                       ("vol", "volume"), ("ro", "raw_open"), ("rh", "raw_high"), ("rl", "raw_low"), ("rc", "raw_close"),
                       ("rpc", "raw_prev_close")):
            cols[k].append(sub[src].to_numpy(np.float64))
        cols["cal"].append(cal.searchsorted(sub.timestamp.to_numpy()).astype(np.int32))
        cols["sid"].append(np.full(n, sid, np.int32))
        stock_start.append(off)
        dl = int((sub.timestamp <= DEV_END).sum()) - 1
        dev_last.append(off + dl)

        c, l = df.close.to_numpy(float), df.low.to_numpy(float)
        ts = df.timestamp
        elig = np.flatnonzero(df.eligible.to_numpy() & (ts >= DEV_START).to_numpy() & (ts <= DEV_END).to_numpy())
        prev_key = None
        for i in elig:
            ratio = float(df.factor.iat[i])
            K, Kr, _ = fb.zones(int(i), ratio)
            g = off + (i - first)
            key = (tuple(Kr.astype(np.int64)), round(ratio, 4))
            el["g"].append(g)
            el["n"].append(len(K))
            el["change"].append(prev_key is None or key != prev_key)
            prev_key = key
            lines_val.extend(K.tolist())
            lines_ptr.append(len(lines_val))
            if len(K) == 0 or i < 1:
                continue
            # 무작위 진입 후보: 진입가(S 종가) 아래 가장 가까운 지지선 / 위 가장 가까운 저항선
            below, above = K[K < c[i]], K[K > c[i]]
            if len(below):
                rnd["S"].append(g)
                rnd["K"].append(below.max())
                rnd["tp"].append(above.min() if len(above) else np.nan)
            # 눌림: S-1 종가 > K, S 저가 <= K, S 종가 >= K -> 가장 높은 K, 진입 = S 종가
            pk = K[(c[i - 1] > K) & (l[i] <= K) & (c[i] >= K)]
            if len(pk):
                kk = pk.max()
                ab = K[K > c[i]]
                det["type"].append(0); det["S"].append(g); det["T"].append(g); det["K"].append(kk)
                det["tp"].append(ab.min() if len(ab) else np.nan)
            # 돌파: S-1 종가 <= K < S 종가, S+1 종가 > K -> 가장 높은 K, 진입 = S+1 종가
            if i + 1 < len(df) and ts.iat[i + 1] <= DEV_END:
                bk = K[(c[i - 1] <= K) & (K < c[i]) & (c[i + 1] > K)]
                if len(bk):
                    kk = bk.max()
                    ab = K[K > c[i + 1]]
                    det["type"].append(1); det["S"].append(g); det["T"].append(g + 1); det["K"].append(kk)
                    det["tp"].append(ab.min() if len(ab) else np.nan)
        off += n
        if (sid + 1) % 200 == 0:
            print(f"  build {sid + 1}/{len(codes)} ({time.time() - t0:.0f}s, eligible days {len(el['g']):,})", flush=True)

    arr = {k: np.concatenate(v) for k, v in cols.items()}
    np.savez_compressed(
        CACHE, **arr, stock_start=np.array(stock_start), dev_last=np.array(dev_last), cal_dates=cal.values.astype("datetime64[ns]"),
        codes=np.array(codes), **{f"det_{k}": np.array(v) for k, v in det.items()},
        **{f"rnd_{k}": np.array(v) for k, v in rnd.items()}, **{f"el_{k}": np.array(v) for k, v in el.items()},
        lines_ptr=np.array(lines_ptr), lines_val=np.array(lines_val))
    print(f"build done: rows {off:,}, eligible days {len(el['g']):,}, detector signals {len(det['S']):,} "
          f"(pullback {det['type'].count(0):,}, breakout {det['type'].count(1):,}), random-entry candidates {len(rnd['S']):,} "
          f"({time.time() - t0:.0f}s)")


# ====================================================================== simulation

class G:
    def __init__(self):
        z = np.load(CACHE, allow_pickle=False)
        self.z = z
        for k in ("o", "h", "l", "c", "ma20", "atr20", "vol", "ro", "rh", "rl", "rc", "rpc", "cal", "sid"):
            setattr(self, k, z[k])
        self.dev_last = z["dev_last"][self.sid]
        self.cal_dates = pd.DatetimeIndex(z["cal_dates"])
        self.year = self.cal_dates.year.to_numpy()[self.cal]
        self.tax = np.vectorize(SELL_TAX.get)(self.year)
        k = pd.read_parquet(sd.KOSPI).sort_values("timestamp")
        below = (k.close < k.close.rolling(120).mean()).to_numpy()
        self.kospi_below_prev = np.concatenate([[False], below[:-1]])  # 달력 인덱스 t -> t-1 의 값
        self.kospi_close = k.close.to_numpy()


def stops_for(g: G, T, K, buffer):
    kind, x = BUFFERS[buffer]
    return K * (1 - x) if kind == "pct" else K - x * g.atr20[T]


def entry_ok(g: G, T, stop, tp, use_filter):
    c = g.c[T]
    ok = (g.vol[T] > 0) & (g.rc[T] < g.rpc[T] * (1 + LIMIT)) & (c > stop) & (T <= g.dev_last[T])
    rr = (tp - c) / (c - stop)
    ok &= np.isnan(tp) | (rr >= 2)
    if use_filter:
        ok &= ~g.kospi_below_prev[g.cal[T]]
    return ok


def simulate(g: G, T, stop, tp, tp_mode, entry_mode="close", max_hold=MAX_HOLD, kmax=K_MAX, want_legs=False,
             ladder=None, buffer=None):
    """벡터화 거래 시뮬레이션. T: 진입 기준 행(신호 확인일 종가 진입). 반환 dict.
    계단식(tp_mode="ladder"): ladder = (N, L) 진입가 위 저항선 오름차순(NaN 채움), buffer = 손절 여유폭 이름."""
    N = len(T)
    base = T if entry_mode == "close" else T + 1
    fill = (g.c[T] if entry_mode == "close" else g.o[np.minimum(base, len(g.o) - 1)]) * (1 + SLIPPAGE)
    cost = fill * (1 + COMMISSION)
    last = g.dev_last[T]
    alive = np.ones(N, bool)
    if entry_mode != "close":
        alive &= base <= last
    valid = alive.copy()
    half = np.zeros(N, bool)
    pending = np.zeros(N, bool)
    rem = np.ones(N)
    proceeds, gross = np.zeros(N), np.zeros(N)
    stop_cur = stop.astype(float).copy()
    exit_k = np.zeros(N, np.int32)
    reason = np.zeros(N, np.int8)
    legs = {"j1": np.full(N, -1), "f1": np.zeros(N), "p1": np.zeros(N), "j2": np.full(N, -1), "f2": np.zeros(N), "p2": np.zeros(N)}
    nmax = len(g.c) - 1
    steps = np.zeros(N, np.int32)
    if tp_mode == "ladder":
        L = ladder.shape[1]
        tgt_i = np.zeros(N, np.int32)
        tgt = ladder[:, 0].copy() if L else np.full(N, np.nan)
        bkind, bx = BUFFERS[buffer]

    def sell(mask, j, px, frac, why, k):
        nonlocal proceeds, gross
        if not mask.any():
            return
        idx = np.flatnonzero(mask)
        jj = j[idx]
        net = px[idx] * (1 - SLIPPAGE) * (1 - COMMISSION - g.tax[jj])
        f = frac[idx]
        proceeds[idx] += f * net
        gross[idx] += f * px[idx]
        first_leg = legs["j1"][idx] < 0
        a, b = idx[first_leg], idx[~first_leg]
        legs["j1"][a], legs["f1"][a], legs["p1"][a] = jj[first_leg], f[first_leg], net[first_leg]
        legs["j2"][b], legs["f2"][b], legs["p2"][b] = jj[~first_leg], f[~first_leg], net[~first_leg]
        rem[idx] -= f
        done = idx[rem[idx] <= 1e-9]
        alive[done] = False
        reason[done] = why if np.isscalar(why) else why[done]
        exit_k[done] = k

    start_k = 1 if entry_mode == "close" else 0
    for k in range(start_k, kmax + 1):
        j = base + k
        end_now = alive & (j > last)
        if end_now.any():
            jl = np.minimum(last, nmax)
            sell(end_now, jl, g.c[jl], rem.copy(), R_END, k - 1)
        if not alive.any():
            break
        jj = np.minimum(j, nmax)
        o, h, l, cl, ma = g.o[jj], g.h[jj], g.l[jj], g.c[jj], g.ma20[jj]
        locked = (g.rh[jj] == g.rl[jj]) & (g.ro[jj] == g.rh[jj]) & (g.ro[jj] <= g.rpc[jj] * (1 - LIMIT))
        act = alive & (g.vol[jj] > 0) & ~locked
        m1 = act & pending
        if m1.any():
            why = np.where(o < stop_cur, R_STOP, R_TREND).astype(np.int8)
            sell(m1, jj, o, rem.copy(), why, k)
        m2 = act & ~m1 & (l < stop_cur) & alive
        if m2.any():
            why = np.where(half, R_BE, R_STOP).astype(np.int8)
            sell(m2, jj, np.minimum(o, stop_cur), rem.copy(), why, k)
        m3 = act & ~m1 & ~m2 & alive & ~half & np.isfinite(tp) & (h >= tp) & (tp_mode != "ladder")
        if m3.any():
            px = np.maximum(o, tp)
            if tp_mode == "full":
                sell(m3, jj, px, rem.copy(), R_TP, k)
            else:
                sell(m3, jj, px, np.where(m3, 0.5, 0.0), R_TP, k)
                half |= m3
                stop_cur = np.where(m3, fill, stop_cur)
        if tp_mode == "ladder":
            touch = act & alive & np.isfinite(tgt) & (h >= tgt)
            fail = touch & (cl <= tgt)
            if fail.any():
                sell(fail, jj, cl, rem.copy(), R_LADFAIL, k)
            brk = np.flatnonzero(touch & ~fail)
            if len(brk):
                rows = ladder[brk]
                ar = np.arange(len(brk))
                with np.errstate(invalid="ignore"):
                    cnt = (rows < cl[brk, None]).sum(axis=1)
                newbase = rows[ar, cnt - 1]
                raised = newbase * (1 - bx) if bkind == "pct" else newbase - bx * g.atr20[jj[brk]]
                stop_cur[brk] = np.maximum(stop_cur[brk], raised)
                steps[brk] += cnt - tgt_i[brk]
                tgt_i[brk] = cnt
                tgt[brk] = np.where(cnt < L, rows[ar, np.minimum(cnt, L - 1)], np.nan)
        if k == 0:
            continue
        still = act & alive
        m4 = still & (k >= max_hold)
        if m4.any():
            sell(m4, jj, cl, rem.copy(), R_MAXHOLD, k)
        pending |= still & ~m4 & (cl < ma)
    if alive.any():
        jl = np.minimum(np.minimum(base + kmax, last), nmax)
        sell(alive.copy(), jl, g.c[jl], rem.copy(), R_END, kmax)
    out = {"net": proceeds / cost - 1, "gross": gross / (fill / (1 + SLIPPAGE)) - 1, "k": exit_k, "reason": reason,
           "cost": cost, "half": half, "valid": valid, "steps": steps if tp_mode == "ladder" else None}
    if want_legs:
        out.update(legs)
    return out


def signal_stats(sim, years, ok_mask=None):
    net = sim["net"] if ok_mask is None else sim["net"][ok_mask]
    if len(net) == 0:
        return {"n": 0, "exp": np.nan}
    r = sim["reason"] if ok_mask is None else sim["reason"][ok_mask]
    k = sim["k"] if ok_mask is None else sim["k"][ok_mask]
    gross = sim["gross"] if ok_mask is None else sim["gross"][ok_mask]
    return {"n": len(net), "exp": net.mean() * 100, "gross_exp": gross.mean() * 100, "win": (net > 0).mean() * 100,
            "trend_3d_pct": ((r == R_TREND) & (k <= 3)).mean() * 100, "hold_med": float(np.median(k)),
            "hold_p90": float(np.quantile(k, 0.9)),
            **({} if sim.get("steps") is None else _steps(sim["steps"] if ok_mask is None else sim["steps"][ok_mask])),
            **{f"exit_{REASON[x]}": (r == x).mean() * 100 for x in REASON}}


def _steps(st):
    return {"steps_mean": st.mean(), "steps_ge1": (st >= 1).mean() * 100, "steps_ge2": (st >= 2).mean() * 100,
            "steps_ge3": (st >= 3).mean() * 100, "steps_max": int(st.max())}


# ====================================================================== portfolio

def portfolio(g: G, T, sim, rng, n_reps):
    """동시 신호 무작위 순서 n_reps 회. 반환: 회차별 지표 DataFrame"""
    cal_T = g.cal[T]
    dev_days = np.flatnonzero((g.cal_dates >= DEV_START) & (g.cal_dates <= DEV_END))
    d0, d1 = dev_days[0], dev_days[-1]
    by_day = {}
    for t, cd in enumerate(cal_T):
        by_day.setdefault(int(cd), []).append(t)
    j1, f1, p1, j2, f2, p2 = (sim[x] for x in ("j1", "f1", "p1", "j2", "f2", "p2"))
    leg_cal1, leg_cal2 = g.cal[j1], np.where(j2 >= 0, g.cal[np.maximum(j2, 0)], -1)
    exit_cal = np.where(j2 >= 0, leg_cal2, leg_cal1)
    cost = sim["cost"]
    end_row = np.maximum(j1, j2)
    days_dt = g.cal_dates
    rates = np.array([CASH_RATE.get(d.year, 0.0) for d in days_dt])
    gap = np.diff(days_dt.values).astype("timedelta64[D]").astype(int)
    growth = np.concatenate([[1.0], (1 + rates[1:]) ** (gap / 365)])
    # 보유 중 평가용 종가: 거래별 (달력, 종가) 경로
    paths = {}
    out = []
    for rep in range(n_reps):
        cash, pos, eq_hist, n_trades = float(START_CAPITAL), {}, [], 0
        for d in range(d0, d1 + 1):
            if d > d0:
                cash *= growth[d]
            for t in list(pos):
                sh = pos[t]
                if leg_cal1[t] == d:
                    cash += sh * f1[t] * p1[t]
                if j2[t] >= 0 and leg_cal2[t] == d:
                    cash += sh * f2[t] * p2[t]
                if exit_cal[t] == d:
                    del pos[t]
            mv = 0.0
            for t, sh in pos.items():
                mv += sh * _mark(g, T[t], d, paths, f1[t], leg_cal1[t], end_row[t])
            cand = by_day.get(d)
            if cand:
                slots = MAX_POSITIONS - len(pos)
                if slots > 0:
                    equity = cash + mv
                    for t in rng.permutation(cand)[:]:
                        if slots == 0:
                            break
                        if t in pos:
                            continue
                        shares = math.floor(min(equity * POSITION_FRAC, cash) / cost[t])
                        if shares <= 0:
                            continue
                        cash -= shares * cost[t]
                        pos[t] = shares
                        mv += shares * g.c[T[t]]
                        n_trades += 1
                        slots -= 1
                        if exit_cal[t] == d:
                            pass
            eq_hist.append(cash + mv)
        eq = pd.Series(eq_hist, index=days_dt[d0:d1 + 1])
        yearly, prev = {}, START_CAPITAL
        for y, s in eq.groupby(eq.index.year):
            yearly[y] = (s.iloc[-1] / prev - 1) * 100
            prev = s.iloc[-1]
        years = (eq.index[-1] - eq.index[0]).days / 365
        out.append({"total": (eq.iloc[-1] / START_CAPITAL - 1) * 100,
                    "cagr": ((eq.iloc[-1] / START_CAPITAL) ** (1 / years) - 1) * 100,
                    "mdd": ((eq / eq.cummax()) - 1).min() * 100, "trades_per_month": n_trades / 60,
                    **{f"y{y}": v for y, v in yearly.items()}})
    return pd.DataFrame(out)


def _mark(g, T0, d, paths, f1, leg1_cal, end_row):
    """종목 T0 의 달력일 d 종가 (그날 거래가 없으면 직전 종가). 절반 매도 후에는 보유 수량이 줄어든 것을 반영."""
    key = int(T0)
    if key not in paths:
        sid = g.sid[T0]
        j = T0
        p = {}
        while j < len(g.c) and g.sid[j] == sid and j <= end_row:
            p[int(g.cal[j])] = g.c[j]
            j += 1
        paths[key] = p
    p = paths[key]
    dd = d
    while dd not in p and dd > g.cal[T0]:
        dd -= 1
    px = p.get(dd, g.c[T0])
    return px * ((1 - f1) if (leg1_cal >= 0 and leg1_cal <= d and f1 < 1) else 1.0)


# ====================================================================== random lines

def random_lines_rep(g: G, rng, el_g, el_n, el_change, seg_id, n_seg_lines, kmax_lines):
    """한 번의 무작위 선 배치: 다시 뽑는 날(선 목록이 바뀐 날)에 그날 종가 +-30% 균등, 그 사이에는 유지."""
    seg_start = np.flatnonzero(el_change)
    base_close = g.c[el_g[seg_start]]
    L = base_close[:, None] * rng.uniform(0.7, 1.3, size=(len(seg_start), kmax_lines))
    L[np.arange(kmax_lines)[None, :] >= n_seg_lines[:, None]] = np.nan
    return L[seg_id]


def signals_from_lines(g: G, el_g, Lday):
    """선 행렬(날짜 x 선)에서 눌림·돌파 신호, K, 익절 목표 계산"""
    S = el_g
    cp, lS, cS = g.c[S - 1], g.l[S], g.c[S]
    same_next = (S + 1 < len(g.c)) & (g.sid[np.minimum(S + 1, len(g.c) - 1)] == g.sid[S]) & (S + 1 <= g.dev_last[S])
    cN = np.where(same_next, g.c[np.minimum(S + 1, len(g.c) - 1)], np.nan)
    with np.errstate(invalid="ignore"):
        pb = (cp[:, None] > Lday) & (lS[:, None] <= Lday) & (cS[:, None] >= Lday)
        bo = (cp[:, None] <= Lday) & (Lday < cS[:, None]) & (cN[:, None] > Lday)
    out = {}
    for name, m, T, ep in (("pullback", pb, S, cS), ("breakout", bo, S + 1, cN)):
        rows = np.flatnonzero(m.any(axis=1))
        K = np.where(m[rows], Lday[rows], -np.inf).max(axis=1)
        above = np.where(Lday[rows] > ep[rows, None], Lday[rows], np.inf).min(axis=1)
        tp = np.where(np.isfinite(above), above, np.nan)
        lad = np.sort(np.where(Lday[rows] > ep[rows, None], Lday[rows], np.nan), axis=1)
        out[name] = (T[rows], K, tp, lad)
    return out


# ====================================================================== run

def run():
    os.makedirs(OUT_RUN, exist_ok=True)
    t0 = time.time()
    g = G()
    z = g.z
    yr_T = lambda T: g.year[T]
    det_type, det_T, det_K, det_tp = z["det_type"], z["det_T"], z["det_K"], z["det_tp"]
    rnd_S, rnd_K, rnd_tp = z["rnd_S"], z["rnd_K"], z["rnd_tp"]
    el_g, el_n, el_change = z["el_g"], z["el_n"], z["el_change"]
    print(f"loaded cache ({time.time() - t0:.0f}s): eligible days {len(el_g):,}, detector signals {len(det_T):,}, random-entry candidates {len(rnd_S):,}")
    # 계단식용: 대상 종목·날짜별 선 목록 (오름차순, NaN 채움). 신호의 선 집합은 신호일(S) 기준으로 고정
    ptr, vals = z["lines_ptr"], z["lines_val"]
    cnts = np.diff(ptr)
    LM = np.full((len(el_g), int(cnts.max())), np.nan)
    LM[np.repeat(np.arange(len(el_g)), cnts), np.arange(len(vals)) - np.repeat(ptr[:-1], cnts)] = vals
    e_det, e_rnd = np.searchsorted(el_g, z["det_S"]), np.searchsorted(el_g, rnd_S)
    assert (el_g[e_det] == z["det_S"]).all() and (el_g[e_rnd] == rnd_S).all()

    def ladder_of(e_idx, entry_price):
        m = LM[e_idx]
        with np.errstate(invalid="ignore"):
            return np.sort(np.where(m > entry_price[:, None], m, np.nan), axis=1)

    rows, sims = [], {}
    for ci, c in enumerate(CONFIGS):
        sel = det_type == (0 if c["entry"] == "pullback" else 1)
        T, K, tp, ed = det_T[sel], det_K[sel], det_tp[sel], e_det[sel]
        stop = stops_for(g, T, K, c["buffer"])
        ok = entry_ok(g, T, stop, tp, c["filter"])
        T, K, tp, stop, ed = T[ok], K[ok], tp[ok], stop[ok], ed[ok]
        lad = ladder_of(ed, g.c[T]) if c["tp"] == "ladder" else None
        sim = simulate(g, T, stop, tp, c["tp"], want_legs=True, ladder=lad, buffer=c["buffer"])
        sims[ci] = (T, K, tp, stop, sim, lad)
        st = signal_stats(sim, yr_T(T))
        yrs = pd.Series(yr_T(T)).value_counts().sort_index()
        # 대조 1: 무작위 진입 (전수, 탐지기 연도 비중으로 가중)
        rstop = stops_for(g, rnd_S, rnd_K, c["buffer"])
        rok = entry_ok(g, rnd_S, rstop, rnd_tp, c["filter"])
        rlad = ladder_of(e_rnd[rok], g.c[rnd_S[rok]]) if c["tp"] == "ladder" else None
        rsim = simulate(g, rnd_S[rok], rstop[rok], rnd_tp[rok], c["tp"], ladder=rlad, buffer=c["buffer"])
        ry = pd.Series(rsim["net"], index=yr_T(rnd_S[rok])).groupby(level=0).mean() * 100
        w = yrs / yrs.sum()
        rnd_exp = float((ry.reindex(w.index) * w).sum())
        rows.append({"cfg": ci, "label": cfg_label(c), **st, "rand_entry_exp": rnd_exp,
                     **{f"n_{y}": int(yrs.get(y, 0)) for y in range(2021, 2026)}})
        print(f"  [{ci + 1:2d}/{len(CONFIGS)}] {cfg_label(c):24s} n={st['n']:6d} exp={st['exp']:+.3f}% rand={rnd_exp:+.3f}% ({time.time() - t0:.0f}s)", flush=True)
    res = pd.DataFrame(rows)

    # 대조 2: 무작위 선 200회
    seg_id = np.cumsum(el_change) - 1
    seg_start = np.flatnonzero(el_change)
    n_seg = el_n[seg_start]
    kmax_lines = int(n_seg.max())
    rng = np.random.default_rng(20261020)
    rl = np.zeros((N_REPS, len(CONFIGS)))
    for rep in range(N_REPS):
        Lday = random_lines_rep(g, rng, el_g, el_n, el_change, seg_id, n_seg, kmax_lines)
        sig = signals_from_lines(g, el_g, Lday)
        for ci, c in enumerate(CONFIGS):
            T, K, tp, lad = sig[c["entry"]]
            stop = stops_for(g, T, K, c["buffer"])
            ok = entry_ok(g, T, stop, tp, c["filter"])
            if ok.sum() == 0:
                rl[rep, ci] = np.nan
                continue
            s = simulate(g, T[ok], stop[ok], tp[ok], c["tp"], ladder=lad[ok] if c["tp"] == "ladder" else None, buffer=c["buffer"])
            rl[rep, ci] = s["net"].mean() * 100
        if (rep + 1) % 20 == 0:
            print(f"  random lines {rep + 1}/{N_REPS} ({time.time() - t0:.0f}s)", flush=True)
    np.save(os.path.join(OUT_RUN, "random_lines_exp.npy"), rl)
    best_per_rep = np.nanmax(rl, axis=1)
    res["rand_lines_mean"] = np.nanmean(rl, axis=0)
    res["rand_lines_p95"] = np.nanquantile(rl, 0.95, axis=0)
    res["pctile_vs_same_cfg"] = [(rl[:, i] < res.exp[i]).mean() * 100 for i in range(len(CONFIGS))]
    best_p95 = float(np.quantile(best_per_rep, 0.95))

    # 포트폴리오 200회 (동시 신호 무작위 순서)
    prng = np.random.default_rng(7)
    port = {}
    for ci, c in enumerate(CONFIGS):
        T, K, tp, stop, sim, _ = sims[ci]
        pf = portfolio(g, T, sim, prng, N_REPS)
        port[ci] = pf
        for col in pf.columns:
            res.loc[ci, f"pf_{col}"] = pf[col].mean()
        for col in ("total", "y2022", "y2024", "mdd", "trades_per_month"):
            res.loc[ci, f"pf_{col}_p05"] = pf[col].quantile(0.05)
            res.loc[ci, f"pf_{col}_p95"] = pf[col].quantile(0.95)
        print(f"  portfolio [{ci + 1:2d}/{len(CONFIGS)}] total={pf.total.mean():+.1f}% 22+24={pf.y2022.mean() + pf.y2024.mean():+.1f}%p ({time.time() - t0:.0f}s)", flush=True)
    res["pf_y22_24"] = res.pf_y2022 + res.pf_y2024

    # 선택
    res["eligible_a"] = res.exp > 0
    res["eligible_b"] = res.exp >= res.rand_entry_exp + 1.0
    res["eligible_c"] = res.pf_trades_per_month >= 2
    res["eligible_d"] = res.n >= 100
    res["eligible"] = res.eligible_a & res.eligible_b & res.eligible_c & res.eligible_d
    res.to_csv(os.path.join(OUT_RUN, "configs.csv"), index=False, encoding="utf-8-sig")
    chosen = None
    elig = res[res.eligible]
    if len(elig):
        top = elig.pf_y22_24.max()
        near = elig[elig.pf_y22_24 >= top - 5].copy()
        bord = {"3%": 0, "1.0ATR": 1, "1.7ATR": 1}
        tord = {"full": 0, "half": 1, "ladder": 2}
        near["key"] = [(int(CONFIGS[i]["filter"]), tord[CONFIGS[i]["tp"]], bord[CONFIGS[i]["buffer"]], -v)
                       for i, v in zip(near.cfg, near.pf_y22_24)]
        chosen = int(near.sort_values("key").iloc[0].cfg)

    deposit = 1.0
    dd = g.cal_dates[(g.cal_dates >= DEV_START) & (g.cal_dates <= DEV_END)]
    for a, b in zip(dd[:-1], dd[1:]):
        deposit *= (1 + CASH_RATE[b.year]) ** ((b - a).days / 365)
    kc = pd.Series(g.kospi_close, index=g.cal_dates)
    kd = kc[(kc.index >= DEV_START) & (kc.index <= DEV_END)]

    refs = {}
    if chosen is not None:
        T, K, tp, stop, _, lad = sims[chosen]
        c = CONFIGS[chosen]
        s_open = simulate(g, T, stop, tp, c["tp"], entry_mode="open_next", ladder=lad, buffer=c["buffer"])
        refs["다음 날 시가 진입"] = signal_stats(s_open, None, s_open["valid"])

    write_report(res, chosen, best_p95, best_per_rep, rl, deposit, kd, refs, port)
    print(f"run done ({time.time() - t0:.0f}s)")


def write_report(res, chosen, best_p95, best_per_rep, rl, deposit, kd, refs, port):
    p = os.path.join(OUT_RUN, "summary.md")
    f2 = lambda x: "" if pd.isna(x) else f"{x:+.2f}"
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 3단계 개발 구간 결과 - 2차 실행 (2021-01-04 ~ 2025-12-31)\n\n")
        f.write("> 1차 실행은 10일 상한이 들어간 명세 오류, 판정은 2차 실행만 사용. 두 실행 중 좋은 쪽을 고르지 않는다. "
                "(1차 결과: results/stage3/summary.md, 2차는 보유 기간 상한 없이 손절·익절·추세 청산만 사용)\n\n")
        f.write(f"예금(연도별 금리) {(deposit - 1) * 100:+.2f}%, 코스피 {(kd.iloc[-1] / kd.iloc[0] - 1) * 100:+.2f}%\n\n")
        f.write("## 신호 단위\n\n| 설정 | 신호 | 순기대값 | 비용 전 | 승률 | 무작위 진입 | 무작위 선 평균 | 무작위 선 95% | 같은 설정 백분위 | 3일내 추세청산 | 보유 중앙값 / 90% | 손절/추세/익절/돌파실패/본전/구간끝 % | 계단 (평균, 1계단 이상, 2계단 이상, 최대) |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in res.itertuples():
            d = r._asdict()
            f.write(f"| {r.label} | {r.n} | {f2(r.exp)} | {f2(r.gross_exp)} | {r.win:.1f} | {f2(r.rand_entry_exp)} | {f2(r.rand_lines_mean)} | "
                    f"{f2(r.rand_lines_p95)} | {r.pctile_vs_same_cfg:.0f} | {r.trend_3d_pct:.1f} | {r.hold_med:.0f} / {r.hold_p90:.0f} | "
                    f"{d['exit_손절']:.0f}/{d['exit_추세']:.0f}/{d['exit_익절']:.0f}/{d['exit_돌파실패']:.0f}/{d['exit_본전']:.0f}/{d['exit_구간끝']:.0f} | "
                    + (f"{d['steps_mean']:.2f}, {d['steps_ge1']:.0f}%, {d['steps_ge2']:.0f}%, {int(d['steps_max'])}" if pd.notna(d.get('steps_mean')) else "") + " |\n")
        f.write(f"\n무작위 선 200회, 매번 {len(CONFIGS)}개 설정 중 최고값의 95% 지점: {best_p95:+.3f}% (평균 {np.mean(best_per_rep):+.3f}%)\n\n")
        f.write("## 포트폴리오 (동시 신호 무작위 순서 200회 평균 [5~95%])\n\n| 설정 | 누적 | CAGR | MDD | 월평균 매매 | 2021 | 2022 | 2023 | 2024 | 2025 | 2022+2024 |\n|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in res.itertuples():
            d = r._asdict()
            f.write(f"| {r.label} | {f2(r.pf_total)} [{f2(r.pf_total_p05)}~{f2(r.pf_total_p95)}] | {f2(r.pf_cagr)} | {f2(r.pf_mdd)} | "
                    f"{r.pf_trades_per_month:.2f} | {f2(d.get('pf_y2021'))} | {f2(r.pf_y2022)} [{f2(r.pf_y2022_p05)}~{f2(r.pf_y2022_p95)}] | "
                    f"{f2(d.get('pf_y2023'))} | {f2(r.pf_y2024)} [{f2(r.pf_y2024_p05)}~{f2(r.pf_y2024_p95)}] | {f2(d.get('pf_y2025'))} | {f2(r.pf_y22_24)} |\n")
        f.write("\n## 자격\n\n| 설정 | (a) 기대값>0 | (b) 무작위 진입+1%p | (c) 월 2회 | (d) 100건 | 자격 |\n|---|---|---|---|---|---|\n")
        for r in res.itertuples():
            f.write(f"| {r.label} | {'O' if r.eligible_a else 'X'} | {'O' if r.eligible_b else 'X'} | {'O' if r.eligible_c else 'X'} | "
                    f"{'O' if r.eligible_d else 'X'} | {'O' if r.eligible else 'X'} |\n")
        if chosen is None:
            f.write("\n**자격을 통과한 설정 없음 -> 검증 구간 실행하지 않음.**\n")
            best_rl = int(np.nanargmax(np.nanmean(rl, axis=0)))
            f.write(f"\n무작위 선 전략 자체의 순기대값 ({len(CONFIGS)}개 설정 평균): {np.nanmean(rl):+.3f}%, 가장 좋은 설정 {res.label[best_rl]} {np.nanmean(rl[:, best_rl]):+.3f}%\n")
        else:
            r = res.loc[chosen]
            passed = r.exp > best_p95
            f.write(f"\n## 선택: {r.label}\n\n순기대값 {r.exp:+.3f}% vs 무작위 선 최고값 95% 지점 {best_p95:+.3f}% -> "
                    f"{'선이 기여함' if passed else '**선이 기여하지 않음 -> 검증 구간 실행하지 않음**'}\n\n")
            if not passed:
                f.write(f"무작위 선 전략 자체의 순기대값 (같은 설정): 평균 {r.rand_lines_mean:+.3f}%\n\n")
            for name, s in refs.items():
                f.write(f"- 참고 {name}: 신호 {s['n']}, 순기대값 {s['exp']:+.3f}%, 승률 {s['win']:.1f}%, 보유 중앙값 {s['hold_med']:.0f}일 / 90% {s['hold_p90']:.0f}일\n")
    print(open(p, encoding="utf-8").read())


if __name__ == "__main__":
    {"build": build, "run": run}[sys.argv[1]]()
