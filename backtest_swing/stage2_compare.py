"""
2단계 본 비교: A(스윙 군집) / B(라운드 우선) / C(A 의 거래량 변형) x 단일·다중 기간, 274개 설정

매칭 (수정 3): 존마다 대표 가격 하나 (A·C 존 중심, B 는 K) 와 사용자 레벨의 거리로 판정, 허용 2%.
  존 가장자리 기준 매칭은 참고용. 허용 1% / 3% 는 민감도.
지표 (25개 차트 합산)
  찾은 비율 R = 존과 매칭된 사용자 레벨 / 사용자 레벨
  남는 존 비율 E = 어떤 사용자 레벨과도 매칭 안 된 존 / 범위 안 존 (빈 차트의 존은 전부 남는 존)
  F1 = 2 R (1-E) / (R + 1 - E)
  범위 = 그날 parquet 종가 +-30% 안에 대표 가격이 있는 존
선택: F1 1등과 0.03 이내 설정 중 단순한 쪽 -> 단일 기간 > 다중 기간, B > C > A,
  같은 계열은 기본값에 가까운 쪽 (A·C: N=5, +-1.5%, 매물대 120일, 큰 단위 / B: 띠 2%, 기준 5점)
과적합 확인: 기준일 순 앞 12개 / 뒤 13개 차트, 앞 절반 1등의 뒤 절반 순위.

실행: .venv-feat/Scripts/python.exe backtest_swing/stage2_compare.py
"""
import itertools
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from levels import LevelDetector, LevelParams, RoundFirstDetector, RoundParams, load, multi_period_zones  # noqa: E402

LEVELS = "backtest_swing/my_levels_prepared.csv"
CHARTS = "backtest_swing/my_levels_charts.csv"
OUT_DIR = "backtest_swing/results/stage2"
RANGE = 0.3
TOLS = (0.01, 0.02, 0.03)
MAIN_TOL = 0.02
TIE_F1 = 0.03
FAMILY_ORDER = {"B": 0, "C": 1, "A": 2}


def configs():
    out = []
    for multi in (False, True):
        for n, pct, vp, rm in itertools.product((3, 5, 7, 10), (0.01, 0.015, 0.02, 0.025), (60, 120, 250), ("major", "any")):
            out.append({"family": "A", "multi": multi, "swing_n": n, "cluster_pct": pct, "vp_days": vp, "round_mode": rm})
        for n, pct, rm in itertools.product((3, 5, 7, 10), (0.01, 0.015, 0.02, 0.025), ("major", "any")):
            out.append({"family": "C", "multi": multi, "swing_n": n, "cluster_pct": pct, "round_mode": rm})
        for band, t in itertools.product((0.01, 0.02, 0.03), (4, 5, 6)):
            out.append({"family": "B", "multi": multi, "band_pct": band, "min_score": t})
    return out


def label(c: dict) -> str:
    per = "다중" if c["multi"] else "단일"
    if c["family"] == "B":
        return f"B {per} 띠{c['band_pct']:.0%} 기준{c['min_score']}점"
    vp = f" 매물대{c['vp_days']}" if c["family"] == "A" else ""
    return f"{c['family']} {per} N{c['swing_n']} ±{c['cluster_pct']:.1%}{vp} {c['round_mode']}"


def simplicity_key(c: dict) -> tuple:
    if c["family"] == "B":
        dist = (c["band_pct"] != 0.02) + (c["min_score"] != 5)
    else:
        dist = (c["swing_n"] != 5) + (c["cluster_pct"] != 0.015) + (c["round_mode"] != "major")
        if c["family"] == "A":
            dist += c["vp_days"] != 120
    return (int(c["multi"]), FAMILY_ORDER[c["family"]], dist)


def zones_for(c: dict, df: pd.DataFrame, date, ratio: float) -> pd.DataFrame:
    if c["family"] == "B":
        det = RoundFirstDetector(df, RoundParams(band_pct=c["band_pct"], min_score=c["min_score"]), ratio)
    else:
        p = LevelParams(swing_n=c["swing_n"], cluster_pct=c["cluster_pct"], vp_days=c.get("vp_days", 120),
                        round_mode=c["round_mode"], volume_evidence="vp" if c["family"] == "A" else "spike")
        det = LevelDetector(df, p, ratio)
    z = multi_period_zones(det, date) if c["multi"] else det.zones(date)
    if c["family"] != "B" and len(z):
        z = z[z.score >= 2]
    return z


def match_chart(z: pd.DataFrame, lv: np.ndarray, close: float, tol: float, mode: str):
    """lv: (k, 2) 사용자 레벨 [low, high]. 반환 (매칭된 레벨 수, 존 수, 남는 존 수)"""
    if len(z):
        z = z[(z.center >= close * (1 - RANGE)) & (z.center <= close * (1 + RANGE))]
    nz = len(z)
    if nz == 0:
        return 0, 0, 0
    if len(lv) == 0:
        return 0, nz, nz
    lo, hi = lv[:, 0][None, :], lv[:, 1][None, :]
    mid = (lo + hi) / 2
    if mode == "rep":
        cz = z.center.to_numpy()[:, None]
        dist = np.where((cz >= lo) & (cz <= hi), 0.0, np.minimum(np.abs(cz - lo), np.abs(cz - hi)) / mid)
        m = dist <= tol
    else:
        zl, zu = z.lower.to_numpy()[:, None], z.upper.to_numpy()[:, None]
        m = (zl <= hi * (1 + tol)) & (zu >= lo * (1 - tol))
    return int(m.any(axis=0).sum()), nz, int((~m.any(axis=1)).sum())


def metrics(rows: pd.DataFrame) -> dict:
    r = rows.matched.sum() / rows.n_levels.sum()
    e = rows.extra.sum() / rows.n_zones.sum() if rows.n_zones.sum() else 0.0
    f1 = 2 * r * (1 - e) / (r + 1 - e) if (r + 1 - e) > 0 else 0.0
    return {"recall": r, "extra_ratio": e, "extra_per_chart": rows.extra.mean(), "zones_per_chart": rows.n_zones.mean(), "f1": f1}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    lv = pd.read_csv(LEVELS, dtype={"code": str}, parse_dates=["date"])
    ch = pd.read_csv(CHARTS, dtype={"code": str}, parse_dates=["date"]).sort_values(["date", "code"]).reset_index(drop=True)
    ch["half"] = np.where(ch.index < 12, "앞 12", "뒤 13")
    data = {c: load(c) for c in ch.code.unique()}
    levels = {(r.code, r.date): lv[(lv.code == r.code) & (lv.date == r.date) & (lv.set == "main")][["low_adj", "high_adj"]].to_numpy()
              for r in ch.itertuples()}
    ver1 = lv[lv.set == "ver1_only"][["low_adj", "high_adj"]].to_numpy()

    cfgs = configs()
    print(f"설정 {len(cfgs)}개 x 차트 {len(ch)}개")
    t0, zone_cache, recs = time.time(), {}, []
    for ci, c in enumerate(cfgs):
        for r in ch.itertuples():
            z = zones_for(c, data[r.code], r.date, r.ratio_used)
            zone_cache[(ci, r.code, r.date)] = z
            for tol in TOLS:
                for mode in ("rep", "edge"):
                    mt, nz, ex = match_chart(z, levels[(r.code, r.date)], r.close_parquet, tol, mode)
                    recs.append({"cfg": ci, "code": r.code, "date": r.date, "half": r.half, "tol": tol, "mode": mode,
                                 "matched": mt, "n_levels": len(levels[(r.code, r.date)]), "n_zones": nz, "extra": ex})
        if (ci + 1) % 50 == 0:
            print(f"  {ci + 1}/{len(cfgs)} ({time.time() - t0:.0f}s)")
    rec = pd.DataFrame(recs)
    rec.to_parquet(os.path.join(OUT_DIR, "chart_level_results.parquet"))

    def table(sub: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for ci, g in sub.groupby("cfg"):
            rows.append({"cfg": ci, "label": label(cfgs[ci]), **metrics(g)})
        return pd.DataFrame(rows).sort_values("f1", ascending=False).reset_index(drop=True)

    main_t = table(rec[(rec.tol == MAIN_TOL) & (rec["mode"] == "rep")])
    edge_t = table(rec[(rec.tol == MAIN_TOL) & (rec["mode"] == "edge")]).set_index("cfg")
    main_t["f1_edge"] = main_t.cfg.map(edge_t.f1)
    main_t["recall_edge"] = main_t.cfg.map(edge_t.recall)
    for tol in (0.01, 0.03):
        t = table(rec[(rec.tol == tol) & (rec["mode"] == "rep")]).reset_index().set_index("cfg")
        main_t[f"f1_tol{int(tol * 100)}"] = main_t.cfg.map(t.f1)
        main_t[f"rank_tol{int(tol * 100)}"] = main_t.cfg.map(t["index"] + 1)
    main_t["rank"] = np.arange(1, len(main_t) + 1)
    main_t.to_csv(os.path.join(OUT_DIR, "all_configs.csv"), index=False, encoding="utf-8-sig")

    top_f1 = main_t.f1.iloc[0]
    near = main_t[main_t.f1 >= top_f1 - TIE_F1].copy()
    near["key"] = near.cfg.map(lambda i: simplicity_key(cfgs[i]))
    chosen = near.sort_values(["key", "f1"], ascending=[True, False]).iloc[0]

    halves = {}
    for h in ("앞 12", "뒤 13"):
        halves[h] = table(rec[(rec.tol == MAIN_TOL) & (rec["mode"] == "rep") & (rec.half == h)])
    first_best = halves["앞 12"].iloc[0]
    second = halves["뒤 13"].reset_index()
    rank_in_second = int(second.loc[second.cfg == first_best.cfg, "index"].iloc[0]) + 1

    fam_best = []
    for fam, multi in itertools.product("ABC", (False, True)):
        idx = [i for i, c in enumerate(cfgs) if c["family"] == fam and c["multi"] == multi]
        fam_best.append(main_t[main_t.cfg.isin(idx)].iloc[0])
    fam_best = pd.DataFrame(fam_best)

    c0 = int(chosen.cfg)
    r660 = ch[(ch.code == "000660")].iloc[0]
    z660 = zone_cache[(c0, r660.code, r660.date)]
    v1 = match_chart(z660, ver1, r660.close_parquet, MAIN_TOL, "rep")
    v2 = match_chart(z660, levels[(r660.code, r660.date)], r660.close_parquet, MAIN_TOL, "rep")

    detail = rec[(rec.cfg == c0) & (rec.tol == MAIN_TOL) & (rec["mode"] == "rep")]
    detail = detail[["code", "date", "half", "n_levels", "matched", "n_zones", "extra"]]
    detail.to_csv(os.path.join(OUT_DIR, "chosen_by_chart.csv"), index=False, encoding="utf-8-sig")
    zl = []
    for r in ch.itertuples():
        z = zone_cache[(c0, r.code, r.date)]
        if len(z):
            z = z[(z.center >= r.close_parquet * (1 - RANGE)) & (z.center <= r.close_parquet * (1 + RANGE))]
            zl.append(z.assign(code=r.code, date=r.date.date(), chart_price=z.center / r.ratio_used))
    pd.concat(zl).to_csv(os.path.join(OUT_DIR, "chosen_zones.csv"), index=False, encoding="utf-8-sig")

    cols = ["rank", "label", "recall", "extra_ratio", "extra_per_chart", "zones_per_chart", "f1", "f1_edge",
            "f1_tol1", "rank_tol1", "f1_tol3", "rank_tol3"]
    with open(os.path.join(OUT_DIR, "summary.txt"), "w", encoding="utf-8") as f:
        def w(s=""):
            print(s)
            f.write(s + "\n")
        w(f"=== 상위 15개 (대표 가격 매칭, 허용 {MAIN_TOL:.0%})")
        w(main_t[cols].head(15).round(3).to_string(index=False))
        w("\n=== 계열·기간별 1등")
        w(fam_best[cols].round(3).to_string(index=False))
        w(f"\n=== 선택: {chosen.label} (F1 {chosen.f1:.3f}, 1등 F1 {top_f1:.3f}, 0.03 이내 후보 {len(near)}개)")
        w(f"=== 과적합 확인: 앞 12개 1등 = {first_best.label} (F1 {first_best.f1:.3f}) -> 뒤 13개에서 {rank_in_second}위 / {len(cfgs)}")
        w(f"    뒤 13개 1등 = {halves['뒤 13'].iloc[0].label} (F1 {halves['뒤 13'].iloc[0].f1:.3f})")
        w(f"=== 000660 2024-09-30 (선택 설정): ver=2 레벨 {len(levels[(r660.code, r660.date)])}개 중 {v2[0]}개 찾음 / "
          f"ver=1 레벨 {len(ver1)}개 중 {v1[0]}개 찾음 (범위 안 존 {v1[1]}개)")
        w(f"\n-> {OUT_DIR}/")


if __name__ == "__main__":
    main()
