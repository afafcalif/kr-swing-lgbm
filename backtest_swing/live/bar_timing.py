"""
1분봉(ka10080) 시각 표기 확인 (시세 조회 TR 만, 실전 토큰). 오늘 15:18:30 스냅샷(ka10095) 가격·누적 거래량을 그날 1분봉과 비교.

  python backtest_swing/live/bar_timing.py 2026-10-22 [표본 수]   -> results/live/bar_timing.md
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Caller, num  # noqa: E402

OUT = "backtest_swing/results/live"


def main(date, n=600):
    day = pd.Timestamp(date)
    d8 = day.strftime("%Y%m%d")
    snap = pd.read_parquet(f"logs/exec/snapshots/{day.date()}.parquet")
    s = pd.DataFrame({"code": snap.p_code, "price": snap.p_close, "vol": snap.p_volume, "amt": snap.p_amount, "cntr_tm": snap.cntr_tm,
                      "recv": snap._recv})
    s = s[s.vol > 0].sort_values("amt", ascending=False).head(int(n))
    c = Caller("prod", 5)
    rows, bar_sets = [], {}
    for code in s.code:
        j, _, _ = c.post("ka10080", "/api/dostk/chart", {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "0"})
        b = pd.DataFrame([{"t": it["cntr_tm"][8:12], "o": num(it.get("open_pric")), "h": num(it.get("high_pric")), "l": num(it.get("low_pric")),
                           "c": num(it.get("cur_prc")), "v": num(it.get("trde_qty"))}
                          for it in j.get("stk_min_pole_chart_qry", []) or [] if str(it.get("cntr_tm", ""))[:8] == d8])
        if b.empty:
            continue
        b = b.drop_duplicates("t").sort_values("t")
        b = b[b.t <= "1530"]
        bar_sets[code] = set(b.t)
        r = s[s.code == code].iloc[0]
        rec = {"code": code, "price": r.price, "vol": r.vol, "cntr_tm": r.cntr_tm, "recv": r.recv}
        for t in ("1517", "1518", "1519", "1520"):
            x = b[b.t == t]
            rec[f"c{t}"] = float(x.c.iloc[0]) if len(x) else np.nan
            rec[f"in{t}"] = bool(len(x) and x.l.iloc[0] <= r.price <= x.h.iloc[0])
            rec[f"cum{t}"] = float(b[b.t <= t].v.sum())
        rec["has1520_1529"] = bool(((b.t >= "1520") & (b.t <= "1529")).any())
        rec["has1530"] = "1530" in set(b.t)
        rows.append(rec)
    d = pd.DataFrame(rows)
    eq = {t: float((d.price == d[f"c{t}"]).mean() * 100) for t in ("1517", "1518", "1519", "1520")}
    # 누적 거래량: 스냅샷 거래량이 어느 봉까지의 합 사이에 드는가
    vb = {"<= 1516 까지 합": float((d.vol <= d.cum1517 - (d.cum1517 - d.cum1517)).mean() * 0)}
    w17 = ((d.vol > d.cum1517 - 0) & False)
    between = {
        "1516 까지 합 < 스냅샷 <= 1517 까지 합": float(((d.vol <= d.cum1517) & (d.vol > d.cum1517 - (d.cum1518 - d.cum1517) * 0) & (d.vol > d.cum1517 - 1e18) ).mean() * 0),
    }
    lo17, lo18, lo19 = d.cum1517, d.cum1518, d.cum1519
    vol_bin = {"스냅샷 = 1517 까지 합": float((d.vol == lo17).mean() * 100),
               "1517 까지 합 < 스냅샷 < 1518 까지 합": float(((d.vol > lo17) & (d.vol < lo18)).mean() * 100),
               "스냅샷 = 1518 까지 합": float((d.vol == lo18).mean() * 100),
               "1518 까지 합 < 스냅샷 < 1519 까지 합": float(((d.vol > lo18) & (d.vol < lo19)).mean() * 100),
               "스냅샷 >= 1519 까지 합": float((d.vol >= lo19).mean() * 100),
               "스냅샷 < 1517 까지 합": float((d.vol < lo17).mean() * 100)}
    cnt = pd.Series(d.cntr_tm.astype(str).str[:4]).value_counts().head(5).to_dict()
    L = ["# 1분봉 시각 표기 확인 (해석 없음)\n",
         f"{day.date()} 15:18:30 스냅샷(ka10095, 응답 수신 {d.recv.min()} ~ {d.recv.max()}) 거래대금 상위 {len(d)}종목 vs 그날 ka10080 1분봉 (접미사 없는 코드, 15:30 이하).\n",
         "| 1분봉 | 스냅샷 현재가 = 그 봉 종가 (%) | 스냅샷 현재가가 그 봉 저가~고가 안 (%) | 그 봉이 있는 종목 (%) |", "|---|---|---|---|"]
    for t in ("1517", "1518", "1519", "1520"):
        L.append(f"| {t} | {eq[t]:.1f} | {d[f'in{t}'].mean() * 100:.1f} | {d[f'c{t}'].notna().mean() * 100:.1f} |")
    L += ["", f"- 15:20 ~ 15:29 봉이 하나라도 있는 종목: {d.has1520_1529.mean() * 100:.1f}%, 15:30 봉이 있는 종목: {d.has1530.mean() * 100:.1f}%",
          "- 스냅샷 누적 거래량(ka10095 trde_qty) 과 1분봉 거래량 누적 합 비교 (%): " + ", ".join(f"{k} {v:.1f}" for k, v in vol_bin.items()),
          f"- 스냅샷의 체결 시각(cntr_tm 앞 4자리) 많은 순: {cnt}"]
    with open(os.path.join(OUT, "bar_timing.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    d.to_csv(os.path.join(OUT, "bar_timing.csv"), index=False, encoding="utf-8-sig")
    print("\n".join(L))
    print("requests", c.n, "errors", len(c.errors))


if __name__ == "__main__":
    main(*sys.argv[1:])
