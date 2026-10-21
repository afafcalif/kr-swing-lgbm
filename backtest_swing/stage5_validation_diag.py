"""
5단계 검증 결과 읽기 전용 진단 (결과·판정·코드·명세는 그대로, 해석 없음)

  python backtest_swing/stage5_validation_diag.py   -> results/stage5_validation/diag/diag.md (+ csv)

A. 대상 종목 수: 2025-01 ~ 2026-10 월별 하루 평균 대상 수, 대상 조건별 걸러진 수 (2026-04·06·07), 원본 파일별 하루 행 수
B. 포트폴리오·대조군 기간 맞추기: P1~P3 와 코스피 월별 수익률, 2026-01-02 ~ 2026-08-14 누적·무작위 200회 분포
C. 신호 단위 상위 3 중 P3 가 산 것·못 산 것 (못 산 이유는 port_run 코드 기준), 무작위 포트폴리오 200회 평균, 수익 상위 10건

포트폴리오는 stage5_model.port_run 원문 사본에 기록 줄만 넣어 계산하고, 누적 수익이 원본(결과 파일)과 같은지 확인한다.
"""
import inspect
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
import stage4_model as sm  # noqa: E402
import stage5_model as m  # noqa: E402
import stage5_validation as v  # noqa: E402

OUT = os.path.join(v.VAL_DIR, "diag")
SUB_END = pd.Timestamp("2026-08-14")
A_MONTHS = pd.period_range("2025-01", "2026-10", freq="M")
COND_MONTHS = ["2026-04", "2026-06", "2026-07"]
P3 = list(m.PORTS)[0]


# ---------------------------------------------------------------- port_run 사본 (기록 줄만 추가)

def make_port_x():
    src = inspect.getsource(m.port_run)
    reps = [
        ("def port_run(g, d, sim, frac, max_pos, risk_frac, capital=CAPITAL):",
         "def port_run_x(g, d, sim, frac, max_pos, risk_frac, capital=CAPITAL, _rec=None):"),
        ("        if cand is not None and len(pos) < max_pos:\n",
         "        if cand is not None and len(pos) >= max_pos:\n"
         "            for _t in cand:\n"
         "                _rec['why'][_t] = 'full'\n"
         "        if cand is not None and len(pos) < max_pos:\n"),
        ("                if len(pos) >= max_pos:\n                    break\n",
         "                if len(pos) >= max_pos:\n"
         "                    for _t in cand[int(np.flatnonzero(cand == t)[0]):]:\n"
         "                        _rec['why'][_t] = 'full'\n"
         "                    break\n"),
        ("                if code_arr[t] in held:\n                    continue\n",
         "                if code_arr[t] in held:\n"
         "                    _rec['why'][t] = 'held'\n"
         "                    continue\n"),
        ("                if sh <= 0:\n                    continue\n",
         "                if sh <= 0:\n"
         "                    _rec['why'][t] = ('cash' if cash < cost_d[t] else 'cap20' if equity * frac < cost_d[t] else 'risk_size')\n"
         "                    continue\n"),
        ("                n += 1\n", "                n += 1\n                _rec['why'][t] = 'bought'\n"),
        ("    e = pd.Series(eq, index=days)\n", "    e = pd.Series(eq, index=days)\n    _rec['e'] = e\n"),
    ]
    for a, b in reps:
        assert src.count(a) == 1, a
        src = src.replace(a, b)
    ns = dict(vars(m))
    exec(src, ns)
    return ns["port_run_x"]


def run_x(port_x, g, d, sim, name):
    rec = {"why": {}}
    r = port_x(g, d, sim, **m.PORTS[name], _rec=rec)
    return r, rec


def top3_bought(ds, picks, why):
    w = pd.Series({t: why.get(t, "not_considered") for t in picks})
    R = ds.loc[picks, "R_real"]
    return w, R


# ---------------------------------------------------------------- 작업 프로세스 (무작위 포트폴리오, 같은 난수)

_W = {}


def _init(folder, first, last, d, sim):
    v.use(folder, first, last)
    _W["g"] = s4.SimArrays(np.load(m.SIM))
    _W["d"], _W["sim"] = d, sim
    _W["px"] = make_port_x()
    fi = np.flatnonzero(d.filter_ok.to_numpy())
    _W["day_rows"] = [fi[d.date.to_numpy()[fi] == x] for x in np.sort(d.date[d.filter_ok].unique())]


def _rand(args):
    name, rep = args
    k = list(m.PORTS).index(name)
    rng = np.random.default_rng([m.PORT_SEED, k, rep])
    d = _W["d"].assign(score=rng.random(len(_W["d"])))
    r, rec = run_x(_W["px"], _W["g"], d, _W["sim"], name)
    e = rec["e"]
    sub = float(e[e.index <= SUB_END].iloc[-1] / m.CAPITAL - 1) * 100 if (e.index <= SUB_END).any() and e.index.max() > SUB_END else np.nan
    out = {"name": name, "rep": rep, "total": r["total"], "sub": sub}
    if name == P3:
        sc = d.score.to_numpy()
        picks = sm.pick_nodup(d, [rr[np.argsort(-sc[rr], kind="stable")] for rr in _W["day_rows"]], sm.TOP_N)
        w = pd.Series({t: rec["why"].get(t, "not_considered") for t in picks})
        R = d.loc[picks, "R_real"]
        bought_all = [t for t, x in rec["why"].items() if x == "bought"]
        out.update({"n_top3": len(picks), "n_bought": int((w == "bought").sum()),
                    "R_bought": float(R[w == "bought"].mean()), "R_not": float(R[w != "bought"].mean()),
                    "why": w[w != "bought"].value_counts().to_dict(),
                    "bought_outside_top3": len(set(bought_all) - set(picks)),
                    "R_bought_outside": float(d.loc[list(set(bought_all) - set(picks)), "R_real"].mean()) if set(bought_all) - set(picks) else np.nan})
    return out


# ---------------------------------------------------------------- A

def part_a():
    L = ["## A. 대상 종목 수\n"]
    ds = pd.read_parquet(os.path.join(v.VAL_DIR, "dataset.parquet"), columns=["date", "code"])
    per_day = ds.groupby("date").size()
    mon = per_day.groupby(per_day.index.to_period("M")).agg(["mean", "min", "max", "size"]).reindex(A_MONTHS)
    # 대상 조건 (stage4_features.eligible_mask 와 같은 조건을 하나씩) — 모든 종목을 다시 읽어 계산
    codes = sd.universe_codes()
    lo, hi = pd.Timestamp("2025-01-01"), pd.Timestamp("2026-10-31")
    names = ["상장 60거래일 이후", "20일 평균 거래대금 >= 50억", "원주가 종가 >= 1,000원", "거래량 > 0", "환산 계수 있음",
             "ATR20 있음", "상한가 아님", "종가 >= 20일선"]
    rows = []
    for code in codes:
        df = sd.load_stock(code)
        conds = [df.index >= sd.MIN_LISTED_BARS, df.amt20 >= sd.MIN_AMOUNT_20, df.raw_close >= sd.MIN_RAW_CLOSE,
                 df.volume > 0, df.factor.notna(), df.atr20.notna(), df.raw_close < df.raw_prev_close * (1 + sb.LIMIT),
                 df.close >= df.ma20]
        el = s4.eligible_mask(df)
        sel = ((df.timestamp >= lo) & (df.timestamp <= hi)).to_numpy()
        if not sel.any():
            continue
        f = pd.DataFrame({n: np.asarray(c)[sel] for n, c in zip(names, conds)})
        f["date"] = df.timestamp.to_numpy()[sel]
        f["eligible"] = el.to_numpy()[sel]
        rows.append(f)
    c = pd.concat(rows, ignore_index=True)
    c = c[c.date <= v.VAL_END]
    # 조건을 하나씩 다 만족한 것 = eligible_mask 와 같아야 함
    allc = np.logical_and.reduce([c[n].to_numpy() for n in names])
    same_as_mask = bool((allc == c.eligible.to_numpy()).all())
    elig_day = c[c.eligible].groupby("date").size()
    pd_in = per_day[(per_day.index >= lo) & (per_day.index <= v.VAL_END)]
    match_ds = bool(elig_day.reindex(pd_in.index).fillna(0).astype(int).equals(pd_in.astype(int)))
    cm = c.date.dt.to_period("M")
    rows_day = c.groupby("date").size()
    mon["행 있는 종목 (load_stock)"] = rows_day.groupby(rows_day.index.to_period("M")).mean().reindex(A_MONTHS)
    L.append(f"대상 = 데이터셋 행 (stage4_features.eligible_mask, 시장 필터와 무관). 조건을 하나씩 다시 계산한 결과가 eligible_mask 와 같음: "
             f"{same_as_mask}, 날짜별 대상 수가 데이터셋과 같음: {match_ds}\n")
    L.append("| 월 | 거래일 | 하루 평균 대상 | 최소 | 최대 | 하루 평균 행 있는 종목 (load_stock) |\n|---|---|---|---|---|---|")
    for p, r in mon.iterrows():
        L.append(f"| {p} | {int(r['size']) if pd.notna(r['size']) else 0} | {r['mean']:.1f} | {r['min']:.0f} | {r['max']:.0f} | {r['행 있는 종목 (load_stock)']:.1f} |")
    # 조건별: 하루 평균 (행 있는 종목 중) 그 조건에 걸린 수, 차례대로 걸렀을 때 남는 수
    L.append("\n대상 조건별 (하루 평균 종목 수, 행 있는 종목 기준). 단독 = 그 조건 하나만 못 맞춘 수가 아니라 그 조건을 못 맞춘 전체 수, "
             "차례대로 = 위 조건부터 순서대로 걸렀을 때 그 단계에서 빠진 수\n")
    L.append("| 조건 | " + " | ".join(f"{mo} 단독 | {mo} 차례대로" for mo in COND_MONTHS) + " |\n|---|" + "---|---|" * len(COND_MONTHS))
    stats = {}
    for mo in COND_MONTHS:
        s = c[cm == pd.Period(mo, "M")]
        nd = s.date.nunique()
        alone = {n: (~s[n]).sum() / nd for n in names}
        keep = np.ones(len(s), bool)
        seq = {}
        for n in names:
            seq[n] = (keep & ~s[n].to_numpy()).sum() / nd
            keep &= s[n].to_numpy()
        stats[mo] = (alone, seq, len(s) / nd, keep.sum() / nd, nd)
    for n in names:
        L.append(f"| {n} | " + " | ".join(f"{stats[mo][0][n]:.1f} | {stats[mo][1][n]:.1f}" for mo in COND_MONTHS) + " |")
    L.append("| 행 있는 종목 | " + " | ".join(f"{stats[mo][2]:.1f} | " for mo in COND_MONTHS) + " |")
    L.append("| 남은 대상 | " + " | ".join(f" | {stats[mo][3]:.1f}" for mo in COND_MONTHS) + " |")
    L.append("| 거래일 | " + " | ".join(f"{stats[mo][4]} | " for mo in COND_MONTHS) + " |")
    # 원본 파일별 하루 행 수
    L.append("\n원본 파일에서 하루 행이 있는 종목 수 (월별 하루 평균, 2026-09-14 이후 보정된 현재 파일):\n")
    cal = pd.DatetimeIndex(pd.read_parquet(sd.KOSPI, columns=["timestamp"]).timestamp)
    cal = cal[(cal >= lo) & (cal <= hi)]
    src = {}
    for name, folder in (("daily_ext", sd.EXT_DIR), ("daily_merged", sd.MERGED_DIR), ("daily_raw", sd.RAW_DIR)):
        cnt = pd.Series(0, index=cal)
        for f in os.listdir(folder):
            t = pd.to_datetime(pd.read_parquet(os.path.join(folder, f), columns=["timestamp"]).timestamp)
            t = t[(t >= lo) & (t <= hi)]
            cnt = cnt.add(pd.Series(1, index=pd.DatetimeIndex(t)).reindex(cal).fillna(0), fill_value=0)
        src[name] = cnt.groupby(cnt.index.to_period("M")).mean()
    uni = set(codes)
    L.append(f"(대상 계산에 쓰는 종목 {len(uni)}개: data/universe_20261019.csv 의 KOSPI·KOSDAQ ∩ daily_raw - 제외 3종목. "
             "파일 수: daily_ext 는 일부 종목만, daily_merged 는 ETF 등 포함)\n")
    L.append("| 월 | daily_ext | daily_merged | daily_raw |\n|---|---|---|---|")
    for p in src["daily_raw"].index:
        L.append(f"| {p} | {src['daily_ext'].get(p, np.nan):.1f} | {src['daily_merged'].get(p, np.nan):.1f} | {src['daily_raw'].get(p, np.nan):.1f} |")
    mon.to_csv(os.path.join(OUT, "A_targets_by_month.csv"), encoding="utf-8-sig")
    return L


# ---------------------------------------------------------------- B, C

def model_runs(folder, first, last, pred):
    v.use(folder, first, last, pred)
    ds, g, sim, cut = m.prepare()
    px = make_port_x()
    res = {}
    for name in m.PORTS:
        r0 = m.port_run(g, ds, sim, **m.PORTS[name])
        r, rec = run_x(px, g, ds, sim, name)
        assert abs(r["total"] - r0["total"]) < 1e-9, (name, r["total"], r0["total"])
        res[name] = (r, rec)
    fi = np.flatnonzero(ds.filter_ok.to_numpy())
    day_rows = [fi[ds.date.to_numpy()[fi] == x] for x in np.sort(ds.date[ds.filter_ok].unique())]
    dummy = pd.Series(0.0, index=pd.DatetimeIndex(ds.date.to_numpy()[[r[0] for r in day_rows]]))
    picks, t3, _ = m.signal_eval(ds, ds.score.to_numpy(), day_rows, dummy)
    return ds, g, sim, res, picks, t3


def random_runs(folder, first, last, pred, ds, sim, names):
    dslim = ds[["date", "code", "g", "cal", "filter_ok", "cost_ps", "risk_ps", "exit_cal", "R_real"]]
    simlim = {"p1": sim["p1"]}
    tasks = [(nm, r) for nm in names for r in range(m.PORT_REPS)]
    out = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=min(14, os.cpu_count() or 4), initializer=_init,
                             initargs=(folder, first, last, dslim, simlim)) as ex:
        for i, x in enumerate(ex.map(_rand, tasks, chunksize=4)):
            out.append(x)
            if (i + 1) % 100 == 0:
                print(f"  random {folder[-10:]} {i + 1}/{len(tasks)} ({time.time() - t0:.0f}s)", flush=True)
    return pd.DataFrame(out)


def c_section(label, ds, res, picks, t3, rnd):
    r, rec = res[P3]
    w, R = top3_bought(ds, picks, rec["why"])
    bought_all = [t for t, x in rec["why"].items() if x == "bought"]
    outside = sorted(set(bought_all) - set(picks))
    names = {"bought": "산 신호", "full": "자리 다 참 (최대 6종목)", "held": "같은 종목 보유 중", "cash": "현금 부족 (현금 < 1주 매수 비용)",
             "cap20": "1주 매수 비용 > 총자산 20%", "risk_size": "위험 기준 수량 0주", "not_considered": "후보로 안 봄"}
    L = [f"### {label}\n",
         f"P3 누적 {r['total']:+.1f}% (결과 파일과 같음 확인). 상위 3 신호 {len(picks)}건.\n",
         "| 구분 | 건수 | 평균 R |\n|---|---|---|"]
    L.append(f"| P3 가 산 상위 3 신호 | {int((w == 'bought').sum())} | {R[w == 'bought'].mean():+.3f} |")
    L.append(f"| P3 가 못 산 상위 3 신호 | {int((w != 'bought').sum())} | {R[w != 'bought'].mean():+.3f} |")
    for k, n in w[w != "bought"].value_counts().items():
        L.append(f"| - 못 산 이유: {names.get(k, k)} | {n} | {R[w == k].mean():+.3f} |")
    L.append(f"| P3 가 산 상위 3 밖의 신호 (상위 3 종목을 이미 보유 등으로 다음 순위를 삼) | {len(outside)} | "
             f"{ds.loc[outside, 'R_real'].mean() if outside else float('nan'):+.3f} |")
    rr = rnd[rnd.name == P3]
    why = pd.DataFrame(list(rr.why)).fillna(0)
    L.append(f"\n무작위 포트폴리오 P3 200회 (각 회의 무작위 점수로 같은 방식의 상위 3 을 만들어 비교, 200회 평균): 상위 3 {rr.n_top3.mean():.1f}건 중 "
             f"산 것 {rr.n_bought.mean():.1f}건 (평균 R {rr.R_bought.mean():+.3f}), 못 산 것 {(rr.n_top3 - rr.n_bought).mean():.1f}건 "
             f"(평균 R {rr.R_not.mean():+.3f}), 상위 3 밖에서 산 것 {rr.bought_outside_top3.mean():.1f}건 (평균 R {rr.R_bought_outside.mean():+.3f})")
    L.append("못 산 이유 (200회 평균 건수): " + ", ".join(f"{names.get(k, k)} {why[k].mean():.1f}" for k in why.columns))
    L.append(f"무작위 P3 누적 (사본으로 다시 계산): 중앙값 {rr.total.median():+.1f}% [5~95% {rr.total.quantile(.05):+.1f}, {rr.total.quantile(.95):+.1f}]\n")
    top10 = t3.nlargest(10, "R_real")
    L.append("수익 상위 10건 (본 모델 상위 3) 과 P3 매수 여부:\n\n| 종목 | 진입일 | R | P3 |\n|---|---|---|---|")
    for t, x in top10.iterrows():
        L.append(f"| {x.code} | {x.date.date()} | {x.R_real:+.1f} | {names.get(rec['why'].get(t, 'not_considered'))} |")
    return L


def main():
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    L = ["# 5단계 검증 결과 읽기 전용 진단 (해석 없음)\n",
         "결과·판정·코드·명세는 바꾸지 않았다. 포트폴리오는 stage5_model.port_run 원문 사본에 기록 줄만 넣어 계산했고, "
         "모델 포트폴리오 누적 수익이 원본과 같은지 확인했다. 무작위 포트폴리오는 결과 파일과 같은 난수(시드 7, 종류, 회차)로 다시 돌렸다.\n"]
    L += part_a()
    print(f"A done ({time.time() - t0:.0f}s)", flush=True)

    # 검증 구간
    ds, g, sim, res, picks, t3 = model_runs(v.VAL_DIR, v.VAL_START, v.VAL_END, os.path.join(v.VAL_DIR, "predictions.parquet"))
    rnd = random_runs(v.VAL_DIR, v.VAL_START, v.VAL_END, os.path.join(v.VAL_DIR, "predictions.parquet"), ds, sim, list(m.PORTS))
    print(f"validation runs done ({time.time() - t0:.0f}s)", flush=True)
    k = pd.read_parquet(sd.KOSPI).sort_values("timestamp").set_index("timestamp").close
    days = g.cal_dates[(g.cal_dates >= v.VAL_START) & (g.cal_dates <= v.VAL_END)]
    kd = k.reindex(days)
    L.append("\n## B. 포트폴리오와 대조군의 기간 맞추기 (검증 구간)\n")
    L.append("월별 수익률 = 그 달 마지막 거래일 값 / 전달 마지막 거래일 값 - 1 (2026-01 은 시작값 대비: 포트폴리오 3,000만 원, 코스피 2026-01-02 종가 "
             "— 결과 파일의 코스피 보유 대조군과 같은 기준)\n")
    L.append("| 월 | P3 | P1 | P2 | 코스피 |\n|---|---|---|---|---|")
    ends = {}
    for name in m.PORTS:
        e = res[name][1]["e"]
        ends[name] = e.groupby(e.index.to_period("M")).last()
    ke = kd.groupby(kd.index.to_period("M")).last()
    base = {name: m.CAPITAL for name in m.PORTS}
    kb = kd.iloc[0]
    for p in ke.index:
        vals = []
        for name in m.PORTS:
            vals.append(ends[name][p] / base[name] - 1)
            base[name] = ends[name][p]
        kr = ke[p] / kb - 1
        kb = ke[p]
        L.append(f"| {p} | {vals[0] * 100:+.1f}% | {vals[1] * 100:+.1f}% | {vals[2] * 100:+.1f}% | {kr * 100:+.1f}% |")
    L.append(f"\n2026-01-02 ~ {SUB_END.date()} (마지막 포함 진입일까지, 그날 종가 평가):\n")
    L.append("| | 본 모델 누적 | 무작위 200회 중앙값 [5~95%] |\n|---|---|---|")
    for name in m.PORTS:
        e = res[name][1]["e"]
        sub = (e[e.index <= SUB_END].iloc[-1] / m.CAPITAL - 1) * 100
        rs = rnd[rnd.name == name]["sub"]
        full = rnd[rnd.name == name]["total"]
        L.append(f"| {name[:2]} | {sub:+.1f}% | {rs.median():+.1f}% [{rs.quantile(.05):+.1f}, {rs.quantile(.95):+.1f}] |")
        L.append(f"| {name[:2]} 전체 기간 (확인용, 결과 파일과 같아야 함) | {res[name][0]['total']:+.1f}% | "
                 f"{full.median():+.1f}% [{full.quantile(.05):+.1f}, {full.quantile(.95):+.1f}] |")
    saved = sm.FIRST_PRED, sm.LAST_PRED
    sm.FIRST_PRED, sm.LAST_PRED = v.VAL_START, SUB_END
    bench_sub = sm.benchmarks(g)
    sm.FIRST_PRED, sm.LAST_PRED = saved
    L.append("\n대조군 (2026-01-02 ~ 2026-08-14, stage4_model.benchmarks 를 이 기간으로): " + ", ".join(f"{a} {b:+.2f}%" for a, b in bench_sub.items()))

    L.append("\n## C. 신호 단위 상위 3 과 P3 포트폴리오 매수\n")
    L.append("못 산 이유는 port_run 코드 기준: 그날 자리가 이미 다 찼거나 후보를 보다가 다 참(full), 같은 종목 보유 중(held), "
             "수량 0주(현금 < 1주 비용 / 1주 비용 > 총자산 20% / 위험 기준 수량 0). 상위 3 = 신호 단위 판정에 쓴 pick_nodup 결과.\n")
    L += c_section("검증 구간 (2026-01-02 ~ 2026-10-16)", ds, res, picks, t3, rnd)
    # 개발 구간
    dds, dg_, dsim, dres, dpicks, dt3 = model_runs(v.DEV_DIR, v.DEV_FIRST, v.DEV_LAST, os.path.join(v.DEV_DIR, "predictions.parquet"))
    drnd = random_runs(v.DEV_DIR, v.DEV_FIRST, v.DEV_LAST, os.path.join(v.DEV_DIR, "predictions.parquet"), dds, dsim, [P3])
    print(f"dev runs done ({time.time() - t0:.0f}s)", flush=True)
    L.append("")
    L += c_section("개발 구간 (예측 2022-01 ~ 2025-12, 마지막 포함 진입 2025-09-25)", dds, dres, dpicks, dt3, drnd)
    rnd.drop(columns=["why"], errors="ignore").to_csv(os.path.join(OUT, "random_validation.csv"), index=False, encoding="utf-8-sig")
    drnd.drop(columns=["why"], errors="ignore").to_csv(os.path.join(OUT, "random_dev_P3.csv"), index=False, encoding="utf-8-sig")
    L.append(f"\n({time.time() - t0:.0f}s)")
    with open(os.path.join(OUT, "diag.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
