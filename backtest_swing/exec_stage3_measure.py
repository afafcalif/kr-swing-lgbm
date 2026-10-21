"""
EXEC_SPEC.md 3장: 15:19 잠정 값과 종가 기준의 차이 재기 (시세 조회 TR 만, 실전 토큰, 1초 5회 이하)

  python backtest_swing/exec_stage3_measure.py collect   # 15:40 이후 시작: 후보 합집합 1분봉(ka10080) + 코스피·코스닥 업종 분봉(ka20005)
  python backtest_swing/exec_stage3_measure.py measure   # 15:19 값으로 그날 행을 바꿔 같은 함수로 특징·대상·점수 -> 상위 3 비교

기간: 2026-10-16 까지 최근 60거래일. 후보: 그날 종가 기준 20일 평균 거래대금 50억 이상.
결과: backtest_swing/results/exec_prep/stage3_measure/
"""
import inspect
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

import stage3_backtest as sb  # noqa: E402
import stage3_data as sd  # noqa: E402
import stage4_features as s4  # noqa: E402
import stage4_model as sm  # noqa: E402
import stage5_features as s5  # noqa: E402
import stage5_model as m  # noqa: E402
import stage5_validation as v  # noqa: E402

OUT = "backtest_swing/results/exec_prep/stage3_measure"
MIN_DIR = os.path.join(OUT, "minute")
END = pd.Timestamp("2026-10-16")
N_DAYS = 60
RPS = 5
START_AFTER = "15:40"
SNAP = "1519"


def window():
    cal = pd.DatetimeIndex(pd.read_parquet(sd.KOSPI, columns=["timestamp"]).timestamp).sort_values()
    return cal[cal <= END][-N_DAYS:]


def candidates(days):
    """날짜별 후보: 그날 종가 기준 amt20 >= 50억 (load_stock 의 amt20 = 거래대금 20일 평균, 그날 포함)"""
    out = {}
    for code in sd.universe_codes():
        df = sd.load_stock(code)
        s = df.set_index("timestamp").amt20.reindex(days)
        d = s.index[(s >= sd.MIN_AMOUNT_20).to_numpy()]
        if len(d):
            out[code] = [x.strftime("%Y%m%d") for x in d]
    return out


# ---------------------------------------------------------------- collect

class Caller:
    """1초 5회 이하. 유량 초과(HTTP 429 / return_code 5)면 백오프, 연속 5번이면 중단."""

    def __init__(self):
        from kiwoom.client import KiwoomRestClient, RateLimiter
        from kiwoom.config import Settings
        self.c = KiwoomRestClient(Settings.from_env("prod"))
        self.c.rate_limiter = RateLimiter(limits_per_sec=RPS)
        self.n, self.errors, self.hourly, self.fail_streak, self.stopped = 0, [], Counter(), 0, None

    def post(self, tr, path, body, cont="N", key=""):
        for backoff in (0, 2, 4, 8, 16, 32):
            if backoff:
                time.sleep(backoff)
            self.n += 1
            self.hourly[pd.Timestamp.now().strftime("%Y-%m-%d %H")] += 1
            try:
                r = self.c.request(tr_code=tr, method="POST", path=path, json=body, cont_yn=cont, next_key=key)
            except Exception as e:  # noqa: BLE001
                msg = str(e)[:200]
                if "429" not in msg and "1700" not in msg:
                    self.errors.append((pd.Timestamp.now().strftime("%H:%M:%S"), tr, body.get("stk_cd") or body.get("inds_cd"), "other: " + msg))
                    raise ValueError(msg)  # 유량 초과가 아닌 오류: 그 종목만 건너뜀
                r, err = None, msg
            else:
                err = None
            if r is not None:
                j = r.json()
                if j.get("return_code") in (5, "5"):
                    err = f"return_code 5 {j.get('return_msg')}"
                else:
                    self.fail_streak = 0
                    return j, r.headers.get("cont-yn"), r.headers.get("next-key", "")
            elif err is None:
                err = "HTTP 429 (client 재시도 3회 후)"
            self.errors.append((pd.Timestamp.now().strftime("%H:%M:%S"), tr, body.get("stk_cd") or body.get("inds_cd"), err))
            self.fail_streak += 1
            if self.fail_streak >= 5:
                self.stopped = err
                raise RuntimeError(f"rate limit repeated: {err}")
        raise RuntimeError("unreachable")


def num(x):
    s = str(x).replace(",", "").strip()
    return abs(float(s)) if s not in ("", "-") else float("nan")


def pull(caller, tr, body, list_key, first_day, max_pages=80):
    rows, cont, key, pages = [], "N", "", 0
    while pages < max_pages:
        j, c, k = caller.post(tr, "/api/dostk/chart", body, cont, key)
        pages += 1
        items = j.get(list_key, []) or []
        rows += [{"ts": it.get("cntr_tm"), "open": num(it.get("open_pric")), "high": num(it.get("high_pric")),
                  "low": num(it.get("low_pric")), "close": num(it.get("cur_prc")), "volume": num(it.get("trde_qty"))}
                 for it in items if it.get("cntr_tm")]
        if not items or min(it["cntr_tm"] for it in items if it.get("cntr_tm"))[:8] < first_day or c != "Y" or not k:
            break
        cont, key = "Y", k
    df = pd.DataFrame(rows)
    if len(df):
        df = df.drop_duplicates("ts")
        df = df[(df.ts.str[:8] >= first_day) & (df.ts.str[8:12] <= "1530")].sort_values("ts").reset_index(drop=True)
    return df, pages


def collect():
    now = pd.Timestamp.now()
    while now.strftime("%H:%M") < START_AFTER:
        time.sleep(20)
        now = pd.Timestamp.now()
    os.makedirs(MIN_DIR, exist_ok=True)
    days = window()
    first = days[0].strftime("%Y%m%d")
    t0 = time.time()
    cand = candidates(days)
    json.dump(cand, open(os.path.join(OUT, "candidates.json"), "w"))
    print(f"candidates: {len(cand)} stocks ({time.time() - t0:.0f}s)", flush=True)
    caller = Caller()
    t1 = time.time()
    done = pages_total = 0
    try:
        for inds, name in (("001", "kospi"), ("101", "kosdaq")):
            p = os.path.join(OUT, f"index_{name}.parquet")
            if not os.path.exists(p):
                df, pg = pull(caller, "ka20005", {"inds_cd": inds, "tic_scope": "1"}, "inds_min_pole_qry", first, max_pages=400)
                for c in ("open", "high", "low", "close"):
                    df[c] = df[c] / 100.0  # 문서: 지수 값은 소수점 제거 후 100배
                df.to_parquet(p, index=False)
                pages_total += pg
        for i, code in enumerate(sorted(cand)):
            p = os.path.join(MIN_DIR, f"{code}.parquet")
            if os.path.exists(p):
                done += 1
                continue
            try:
                df, pg = pull(caller, "ka10080", {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "0"}, "stk_min_pole_chart_qry", first)
            except ValueError:
                continue
            df.to_parquet(p, index=False)
            pages_total += pg
            done += 1
            if (i + 1) % 25 == 0:
                print(f"  {i + 1}/{len(cand)} stocks, {caller.n} requests, {time.time() - t1:.0f}s, errors {len(caller.errors)}", flush=True)
    except RuntimeError as e:
        print("STOPPED:", e, flush=True)
    rec = {"stocks_total": len(cand), "stocks_done": done, "requests": caller.n, "seconds": time.time() - t1,
           "errors": caller.errors, "stopped": caller.stopped, "hourly": dict(caller.hourly),
           "started": now.strftime("%Y-%m-%d %H:%M:%S"), "window": [str(days[0].date()), str(days[-1].date())]}
    json.dump(rec, open(os.path.join(OUT, "collect.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v_ for k, v_ in rec.items() if k != "errors"}, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------- measure

def make_load_x():
    """stage3_data.load_stock 원문 사본: 원본 파일을 읽고 자른 직후 그날 행을 바꾸는 줄만 추가"""
    src = inspect.getsource(sd.load_stock)
    a = "    raw = raw.rename(columns={c: f\"raw_{c}\" for c in (\"open\", \"high\", \"low\", \"close\")})\n"
    assert src.count(a) == 1
    src = src.replace("def load_stock(code: str, until=None) -> pd.DataFrame:",
                      "def load_stock_x(code: str, until=None, override=None) -> pd.DataFrame:")
    src = src.replace(a, "    if override is not None:\n        adj, raw = override(adj, raw)\n" + a)
    ns = dict(vars(sd))
    exec(src, ns)
    return ns["load_stock_x"]


def snap_values(mdf, day):
    """그날 15:19 까지: 시가(첫 봉), 고가·저가, 종가(15:19 이하 마지막 봉), 거래량 합, 거래대금 근사(거래량 x 종가 합). 원주가."""
    d = mdf[mdf.ts.str[:8] == day]
    upto = d[d.ts.str[8:12] <= SNAP]
    full = d[d.ts.str[8:12] <= "1530"]
    if upto.empty:
        return None
    return {"open": upto.open.iloc[0], "high": upto.high.max(), "low": upto.low.min(), "close": upto.close.iloc[-1],
            "volume": upto.volume.sum(), "amount": float((upto.volume * upto.close).sum()),
            "full_volume": full.volume.sum(), "full_amount": float((full.volume * full.close).sum())}


def _worker(args):
    code, days = args
    load_x = make_load_x()
    p = os.path.join(MIN_DIR, f"{code}.parquet")
    if not os.path.exists(p):
        return []
    mdf = pd.read_parquet(p)
    raw_vol = pd.read_parquet(os.path.join(sd.RAW_DIR, f"{code}.parquet"), columns=["timestamp", "volume"])
    raw_vol = raw_vol.set_index(pd.to_datetime(raw_vol.timestamp)).volume
    out = []
    for day in days:
        sv = snap_values(mdf, day)
        D = pd.Timestamp(day)
        if sv is None:
            out.append({"code": code, "date": D, "snap": False})
            continue

        def override(adj, raw, sv=sv, D=D):
            adj, raw = adj.copy(), raw.copy()
            ia = adj.index[adj.timestamp == D]
            ir = raw.index[pd.to_datetime(raw.timestamp) == D]
            if len(ia) != 1 or len(ir) != 1:
                return adj, raw
            f = float(raw.loc[ir[0], "factor"])
            rv = float(raw.loc[ir[0], "volume"])
            vscale = float(adj.loc[ia[0], "volume"]) / rv if rv > 0 else 1.0
            day_amt = float(adj.loc[ia[0], "amount"])
            for c in ("open", "high", "low", "close"):
                raw.loc[ir[0], c] = sv[c]
                adj.loc[ia[0], c] = sv[c] * f
            raw.loc[ir[0], "volume"] = sv["volume"]
            adj.loc[ia[0], "volume"] = sv["volume"] * vscale
            adj.loc[ia[0], "amount"] = min(sv["amount"], day_amt)  # 15:19 누적 <= 하루 전체 (근사가 넘지 않게)
            return adj, raw

        cut = load_x(code, until=D, override=override)
        if cut.timestamp.iat[-1] != D:
            continue
        f = s5.stock_features(cut).iloc[-1]
        el = bool((s4.eligible_mask(cut) & (cut.timestamp >= s4.SIG_START)).iat[-1])
        full = sd.load_stock(code, until=D)
        out.append({"code": code, "date": D, "snap": True, "eligible": el, "above_ma20": float(cut.close.iat[-1] > cut.ma20.iat[-1]),
                    "close_1519_raw": sv["close"], "close_raw": float(full.raw_close.iat[-1]),
                    "vol_1519": sv["volume"], "vol_full_min": sv["full_volume"], "vol_day_raw": float(raw_vol.get(D, np.nan)),
                    "amt_full_min": sv["full_amount"], "amt_day": float(full.amount.iat[-1]),
                    **{c: float(f[c]) for c in f.index}})
    return out


def market_1519(days):
    k = pd.read_parquet(sd.KOSPI).set_index("timestamp").close
    q = pd.read_parquet(s4.KOSDAQ).set_index("timestamp").close.reindex(k.index)
    rows, note = {}, []
    for name, s in (("kospi", k), ("kosdaq", q)):
        p = os.path.join(OUT, f"index_{name}.parquet")
        im = pd.read_parquet(p) if os.path.exists(p) else pd.DataFrame(columns=["ts", "close"])
        for D in days:
            day = D.strftime("%Y%m%d")
            d = im[(im.ts.str[:8] == day) & (im.ts.str[8:12] <= SNAP)] if len(im) else im
            val = d.close.iloc[-1] if len(d) else np.nan
            if np.isnan(val):
                note.append(f"{name} {day}: 15:19 업종 분봉 없음 -> 종가")
                val = s[D]
            s2 = s.copy()
            s2[D] = val
            i = s2.index.get_loc(D)
            r = rows.setdefault(D, {})
            for n in (5, 20, 60):
                r[f"{name}_ret{n}"] = s2.iloc[i] / s2.iloc[i - n] - 1
            if name == "kospi":
                r["kospi_ma120_pos"] = s2.iloc[i] / s2.iloc[i - 119:i + 1].mean() - 1
                r["kospi_1519"], r["kospi_close"] = val, s[D]
    return pd.DataFrame.from_dict(rows, orient="index"), note


def train_months(ds, months):
    cols = m.feature_cols(ds)
    y = ds.y_clip.to_numpy()
    models = {}
    for mo, first_day, cutoff in months:
        tr_idx = np.flatnonzero((ds.date <= cutoff).to_numpy())
        Xtr = ds.iloc[tr_idx][cols].to_numpy(np.float32)
        models[str(mo)] = [sm.fit(Xtr, y[tr_idx], seed) for seed in m.SEEDS_MAIN]
        print(f"  model {mo} (retrain {first_day.date()}, cutoff {cutoff.date()}): {len(tr_idx):,} rows", flush=True)
    return models, cols


def measure():
    t0 = time.time()
    days = window()
    cand = json.load(open(os.path.join(OUT, "candidates.json")))
    col = json.load(open(os.path.join(OUT, "collect.json"), encoding="utf-8"))
    tasks = [(c, d) for c, d in cand.items()]
    with ProcessPoolExecutor(max_workers=min(14, os.cpu_count() or 4)) as ex:
        rows = [r for part in ex.map(_worker, tasks, chunksize=4) for r in part]
    snap = pd.DataFrame(rows)
    print(f"15:19 rows {len(snap):,} ({time.time() - t0:.0f}s)", flush=True)
    snap.to_parquet(os.path.join(OUT, "snap_rows.parquet"), index=False)

    # 종가 기준: 검증 데이터셋 (같은 파일·같은 코드로 만든 것)
    ds = pd.read_parquet(os.path.join(v.VAL_DIR, "dataset.parquet"))
    v.use(v.VAL_DIR, pd.Timestamp("2026-07-01"), pd.Timestamp("2026-10-31"), os.path.join(v.VAL_DIR, "predictions.parquet"))
    months = sm.months(ds)
    models, cols = train_months(ds, months)
    # 7·8월 모델이 검증 실행 예측과 같은지
    pv = pd.read_parquet(os.path.join(v.VAL_DIR, "predictions.parquet"))
    chk = {}
    for mo in ("2026-07", "2026-08"):
        per = pd.Period(mo, "M")
        idx = np.flatnonzero(((ds.date >= per.start_time) & (ds.date <= per.end_time)).to_numpy())
        p_new = np.mean([b.predict(ds.iloc[idx][cols].to_numpy(np.float32)) for b in models[mo]], axis=0)
        p_old = pv.iloc[idx][[f"main_s{s}" for s in m.SEEDS_MAIN]].mean(axis=1).to_numpy()
        chk[mo] = float(np.nanmax(np.abs(p_new - p_old)))

    def score(frame):
        mo = frame.date.dt.to_period("M").astype(str)
        out = np.full(len(frame), np.nan)
        for key, idx in frame.groupby(mo).indices.items():
            X = frame.iloc[idx][cols].to_numpy(np.float32)
            out[idx] = np.mean([b.predict(X) for b in models[key]], axis=0)
        return out

    g = s4.SimArrays(np.load(os.path.join(v.VAL_DIR, "sim.npz")))
    codes = list(np.load(os.path.join(v.VAL_DIR, "sim.npz"))["codes"])
    pos = {(int(s_), int(c_)): i for i, (s_, c_) in enumerate(zip(g.sid, g.cal))}
    code_sid = {c: i for i, c in enumerate(codes)}
    kk = pd.read_parquet(sd.KOSPI).sort_values("timestamp").set_index("timestamp").close
    prev_below = (kk < kk.rolling(120).mean()).shift(1)

    def evaluate(frame):
        frame = frame[frame.date.isin(days)].copy().reset_index(drop=True)
        frame["g"] = [pos.get((code_sid[c], g.cal_dates.get_loc(d))) for c, d in zip(frame.code, frame.date)]
        frame = frame.dropna(subset=["g"]).reset_index(drop=True)
        frame["g"] = frame.g.astype(int)
        T = frame.g.to_numpy()
        risk = s4.STOP_ATR * g.atr20[T]
        sim = sb.simulate(g, T, g.c[T] - risk, np.full(len(T), np.nan), "full", want_legs=True)
        frame["net_real"], frame["reason_real"] = sim["net"], sim["reason"]
        frame["R_real"] = sim["net"] * sim["cost"] / risk
        frame["exit_cal"], frame["cal"] = g.cal[sim["j1"]], g.cal[T]
        frame["filter_ok"] = ~frame.date.map(prev_below.reindex(frame.date.unique()).fillna(False)).astype(bool)
        frame["score"] = score(frame)
        fi = np.flatnonzero(frame.filter_ok.to_numpy())
        day_rows = [fi[frame.date.to_numpy()[fi] == d] for d in np.sort(frame.date[frame.filter_ok].unique())]
        sc = frame.score.to_numpy()
        picks = sm.pick_nodup(frame, [r[np.argsort(-sc[r], kind="stable")] for r in day_rows], sm.TOP_N)
        return frame, picks

    close_frame, close_picks = evaluate(ds[ds.date.isin(days)])
    # 15:19 기준 대상 행 + 같은 날 순위·시장 폭 (대상 종목만으로, build 와 같게 float32 로 바꾼 뒤 순위)
    e = snap[snap.snap & snap.eligible].copy()
    feat_stock = [c for c in cols if c not in s5.XS_FEATS and not c.startswith(("kospi_", "kosdaq_"))]
    for c in feat_stock:
        e[c] = e[c].astype(np.float32)
    grp = e.groupby("date")
    e["rank_ret20"] = grp.ret_20.rank(pct=True).astype(np.float32)
    e["rank_ret60"] = grp.ret_60.rank(pct=True).astype(np.float32)
    e["rank_amt20"] = grp.log_amt20.rank(pct=True).astype(np.float32)
    e["breadth_ma20"] = grp.above_ma20.transform("mean").astype(np.float32)
    mk, mk_note = market_1519(days)
    e = e.merge(mk.drop(columns=["kospi_1519", "kospi_close"]).astype(np.float32), left_on="date", right_index=True, how="left")
    snap_frame, snap_picks = evaluate(e)

    # 비교
    cp = close_frame.loc[close_picks]
    spk = snap_frame.loc[snap_picks]
    fdays = sorted(set(close_frame.date[close_frame.filter_ok]))
    ov = []
    for d in fdays:
        a = set(cp[cp.date == d].code)
        b = set(spk[spk.date == d].code)
        ec = set(close_frame[close_frame.date == d].code)
        es = set(snap_frame[snap_frame.date == d].code)
        ov.append({"date": d, "overlap": len(a & b), "n_close": len(a), "n_snap": len(b), "elig_close": len(ec), "elig_snap": len(es),
                   "new_in_snap": len(es - ec), "dropped_in_snap": len(ec - es)})
    ov = pd.DataFrame(ov)
    ov.to_csv(os.path.join(OUT, "daily_overlap.csv"), index=False, encoding="utf-8-sig")

    def closed(t):
        t = t[t.reason_real != sb.R_END]
        return len(t), t.R_real.mean(), t.net_real.mean() * 100

    nc, rc, pc = closed(cp)
    ns, rs, ps = closed(spk)
    s_ok = snap[snap.snap]
    chg = (s_ok.close_raw / s_ok.close_1519_raw - 1) * 100
    chg_e = (spk.close_raw / spk.close_1519_raw - 1) * 100  # 15:19 행에는 종가·15:19 가격 열이 이미 있다
    q = lambda s_: ", ".join(f"{k_}: {s_.quantile(k_):+.2f}%" for k_ in (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99))
    vol_ok = (s_ok.vol_full_min == s_ok.vol_day_raw).mean() * 100
    amt_ratio = (s_ok.amt_full_min / s_ok.amt_day)
    L = ["# EXEC_SPEC 3장: 15:19 잠정 값 vs 종가 기준 (해석 없음)\n",
         f"기간 {days[0].date()} ~ {days[-1].date()} ({len(days)}거래일), 시장 필터 통과일 {len(fdays)}일. 후보: 그날 종가 기준 20일 평균 거래대금 50억 이상 "
         f"{len(cand)}종목 (합집합). 실전 토큰, 시세 조회 TR 만 (ka10080 접미사 없는 코드·원주가, ka20005).\n",
         "## 수집\n",
         f"- 시작 {col['started']}, 요청 {col['requests']:,}회, {col['seconds'] / 60:.1f}분, 받은 종목 {col['stocks_done']} / {col['stocks_total']}, "
         f"유량 오류 {len(col['errors'])}건, 중단 {col['stopped'] or '없음'}",
         f"- 시간당 호출 수: {col['hourly']}",
         f"- 분봉 점검 (후보 종목·날짜 {len(s_ok):,}개): 09:00 ~ 15:30 분봉 거래량 합 = 일봉 원주가 거래량인 비율 {vol_ok:.1f}%, "
         f"분봉 (거래량 x 종가) 합 / 일봉 거래대금: 중앙값 {amt_ratio.median():.4f}, 5~95% {amt_ratio.quantile(.05):.4f} ~ {amt_ratio.quantile(.95):.4f}",
         f"- 15:19 분봉이 없는 후보 종목·날짜 (그날 15:19 까지 체결 없음): {int((~snap.snap).sum())}",
         f"- 지수: " + ("업종 분봉(ka20005) 15:19 값 사용" if not mk_note else f"업종 분봉 없는 날은 종가로 대신 ({len(mk_note)}건: {mk_note[:5]})"),
         f"- 모델: 그 달 모델 (7·8·9·10월, 검증과 같은 학습 집합·시드 1~5). 7·8월 모델의 예측과 검증 실행 저장 예측의 최대 차이: {chk}\n",
         "## 상위 3 겹침 (시장 필터 통과일)\n",
         "| 겹친 종목 수 | 0 | 1 | 2 | 3 |\n|---|---|---|---|---|",
         "| 일수 | " + " | ".join(str(int((ov.overlap == k_).sum())) for k_ in range(4)) + " |",
         f"\n평균 겹침 {ov.overlap.mean():.2f} / 3, 상위 3 이 3개가 아닌 날: 종가 {int((ov.n_close < 3).sum())}일, 15:19 {int((ov.n_snap < 3).sum())}일\n",
         "## 대상 집합\n",
         f"- 하루 평균 대상 수: 15:19 {ov.elig_snap.mean():.1f}, 종가 {ov.elig_close.mean():.1f}",
         f"- 15:19 에만 대상 (새로 들어옴): 하루 평균 {ov.new_in_snap.mean():.1f} (최대 {ov.new_in_snap.max()}), 종가에만 대상 (빠짐): 하루 평균 "
         f"{ov.dropped_in_snap.mean():.1f} (최대 {ov.dropped_in_snap.max()})\n",
         "## 상위 3 성과 (실제 규칙, 종가 진입, 2026-10-16 까지 청산된 거래만)\n",
         "| | 상위 3 신호 | 청산된 거래 | 평균 R | 평균 % 순수익률 |\n|---|---|---|---|---|",
         f"| 종가 기준 | {len(cp)} | {nc} | {rc:+.3f} | {pc:+.2f}% |",
         f"| 15:19 기준 | {len(spk)} | {ns} | {rs:+.3f} | {ps:+.2f}% |",
         "\n## 15:19 가격 대비 종가 변화 (원주가)\n",
         f"- 후보 종목·날짜 전체 ({len(chg):,}개): 평균 {chg.mean():+.2f}%, 분위 {q(chg)}",
         f"- 15:19 상위 3 ({len(chg_e)}건): 평균 {chg_e.mean():+.2f}%, 분위 {q(chg_e)}, +5% 초과 {int((chg_e > 5).sum())}건",
         f"\n({time.time() - t0:.0f}s)"]
    with open(os.path.join(OUT, "measure.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    {"collect": collect, "measure": measure}[sys.argv[1]]()
