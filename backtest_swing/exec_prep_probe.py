"""
실매매 준비 시험 (EXEC_SPEC.md 8장 5·7, 3장 측정 가능성). 시세 조회 TR(ka10080, ka10095)만 쓴다. 주문·계좌 조회 없음.

  python backtest_swing/exec_prep_probe.py   -> results/exec_prep/probe.md (+ minute_{code}.parquet)

1. ka10080: 거래대금 상위 1종목(한국거래소 공식 시세 2026-10-19 기준) 1분봉을 연속조회로 어디까지 과거로 받아지는지, 요청 수·시간 (상한 600회)
2. 거래소: 같은 종목을 접미사 없는 코드 / _NX / _AL 로 조회해 2026-10-20 15:19·15:30 1분봉과 지금 시세(ka10095)를 비교
3. 3장 측정 가능성: 최근 20거래일 대상이 될 수 있는 종목(전날 기준 20일 평균 거래대금 40억 이상) 수, 그 종목들 15:19 1분봉을 다 받는 데 필요한 요청 수·예상 시간 (추정만)
시세는 실전 토큰(Settings.from_env("prod"))으로 1초 5회 이하로 받는다.
"""
import math
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

from kiwoom.client import KiwoomRestClient, RateLimiter  # noqa: E402
from kiwoom.config import Settings  # noqa: E402
import stage3_data as sd  # noqa: E402

OUT = "backtest_swing/results/exec_prep"
MAX_REQ = 600
RPS = 5  # 실전 조회 TR 1초 5회 이하
CAND_AMT = 4_000_000_000  # 3번: 전날 20일 평균 거래대금 40억 이상 (넉넉히)
DAY = "20261020"


def num(x):
    s = str(x).replace(",", "").strip()
    return abs(float(s)) if s not in ("", "-") else float("nan")


def minute_page(client, code, cont="N", key=""):
    r = client.request(tr_code="ka10080", method="POST", path="/api/dostk/chart",
                       json={"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1"}, cont_yn=cont, next_key=key)
    j = r.json()
    rows = [{"ts": it.get("cntr_tm"), "open": num(it.get("open_pric")), "high": num(it.get("high_pric")),
             "low": num(it.get("low_pric")), "close": num(it.get("cur_prc")), "volume": num(it.get("trde_qty"))}
            for it in j.get("stk_min_pole_chart_qry", []) if it.get("cntr_tm")]
    return rows, r.headers.get("cont-yn"), r.headers.get("next-key", ""), j.get("return_code"), j.get("return_msg")


def history(client, code, stop_before=None, max_req=MAX_REQ):
    """연속조회. stop_before(YYYYMMDD) 보다 이른 분봉이 나오면 멈춤. (분봉, 요청 수, 걸린 초, 끝난 이유)"""
    rows, cont, key, n, t0 = [], "N", "", 0, time.time()
    why = "max_req"
    while n < max_req:
        page, c, k, rc, msg = minute_page(client, code, cont, key)
        n += 1
        rows += page
        if rc not in (0, "0", None):
            why = f"return_code {rc} {msg}"
            break
        if not page:
            why = "empty page"
            break
        if stop_before and min(p["ts"] for p in page)[:8] < stop_before:
            why = f"reached {stop_before}"
            break
        if c != "Y" or not k:
            why = f"cont-yn={c}"
            break
        cont, key = "Y", k
        if n % 50 == 0:
            print(f"  {code}: {n} requests, earliest {min(p['ts'] for p in rows)} ({time.time() - t0:.0f}s)", flush=True)
    df = pd.DataFrame(rows).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df, n, time.time() - t0, why


def main():
    os.makedirs(OUT, exist_ok=True)
    client = KiwoomRestClient(Settings.from_env("prod"))
    client.rate_limiter = RateLimiter(limits_per_sec=RPS)  # 이 시험에서만 1초 5회 (KiwoomRestClient 기본은 1초 1회)
    L = ["# 실매매 준비 시험 (시세 조회만, 실전 토큰, 1초 5회 이하)\n", f"실행 {pd.Timestamp.now():%Y-%m-%d %H:%M}\n"]

    # 1. ka10080 보관 기간
    k = pd.read_parquet("data/krx_daily/20261019.parquet")
    top = k.sort_values("amount", ascending=False).iloc[0]
    code = str(top.code)
    df, n, sec, why = history(client, code)
    df.to_parquet(os.path.join(OUT, f"minute_{code}.parquet"), index=False)
    df["date"] = df.ts.str[:8]
    df["hm"] = df.ts.str[8:12]
    per_day = df.groupby("date").size()
    reg = df[(df.hm >= "0900") & (df.hm <= "1530")].groupby("date").size()
    full_days = per_day.index[1:-1] if len(per_day) > 2 else per_day.index
    L.append(f"## 1. ka10080 1분봉 연속조회 ({code} {top['name']}, 2026-10-19 공식 거래대금 1위)\n")
    L.append(f"- 요청 {n}회, {sec:.0f}초 (요청당 {sec / max(n, 1):.2f}초), 끝난 이유: {why}")
    L.append(f"- 받은 분봉 {len(df):,}개, 가장 이른 {df.ts.min()}, 가장 늦은 {df.ts.max()}, 거래일 {per_day.size}일")
    L.append(f"- 하루 분봉 수 (처음·마지막 날 제외): 중앙값 {per_day.reindex(full_days).median():.0f}, 최소 {per_day.reindex(full_days).min():.0f}, "
             f"최대 {per_day.reindex(full_days).max():.0f} | 그중 09:00 ~ 15:30: 중앙값 {reg.reindex(full_days).median():.0f}")
    L.append(f"- 분봉 시각 범위: {df.hm.min()} ~ {df.hm.max()} | 1회 응답 분봉 수 약 {len(df) / max(n, 1):.0f}")
    bars_per_day = float(per_day.reindex(full_days).median())

    # 2. 거래소 비교
    L.append(f"\n## 2. 거래소 비교 ({code}, {DAY} 1분봉 + 지금 시세)\n")
    L.append("| 코드 | 15:19 봉 시가/고가/저가/종가/거래량 | 15:30 봉 종가/거래량 | 그날 마지막 봉 시각 | 요청 수 |\n|---|---|---|---|---|")
    comp = {}
    for suffix in ("", "_NX", "_AL"):
        c = code + suffix
        d, nn, _, w = history(client, c, stop_before=DAY, max_req=10)
        d = d[d.ts.str[:8] == DAY]
        b19 = d[d.ts.str[8:12] == "1519"]
        b30 = d[d.ts.str[8:12] == "1530"]
        comp[suffix or "plain"] = b19
        f19 = (" / ".join(f"{b19[x].iloc[0]:,.0f}" for x in ("open", "high", "low", "close", "volume")) if len(b19) else "없음")
        f30 = (f"{b30.close.iloc[0]:,.0f} / {b30.volume.iloc[0]:,.0f}" if len(b30) else "없음")
        L.append(f"| {c} | {f19} | {f30} | {d.ts.max()[8:12] if len(d) else '-'} | {nn} |")
    snap = []
    for suffix in ("", "_NX", "_AL"):
        r = client.request(tr_code="ka10095", method="POST", path="/api/dostk/stkinfo", json={"stk_cd": code + suffix}).json()
        it = (r.get("atn_stk_infr") or [{}])[0]
        snap.append(f"{code + suffix}: 현재가 {num(it.get('cur_prc')):,.0f}, 거래량 {num(it.get('trde_qty')):,.0f} (return_code {r.get('return_code')})")
    L.append(f"\nka10095 지금 시세 ({pd.Timestamp.now():%H:%M:%S}, 세 요청 연속):\n")
    L += [f"- {s}" for s in snap]
    try:
        off = pd.read_parquet("data/krx_daily/20261020.parquet")
        L.append(f"\n한국거래소 공식 {DAY} 종가: {off[off.code == code].close.iloc[0]:,.0f}")
    except Exception:  # noqa: BLE001
        L.append(f"\n한국거래소 공식 {DAY} 종가: 로컬 파일 없음 (data/krx_daily/{DAY}.parquet)")

    # 3. 3장 측정 가능성 (추정만)
    cal = pd.DatetimeIndex(pd.read_parquet(sd.KOSPI, columns=["timestamp"]).timestamp).sort_values()
    days = cal[-20:]
    cnt = pd.Series(0, index=days)
    union = set()
    for c in sd.universe_codes():
        s = sd.load_stock(c)
        prev_amt = s.set_index("timestamp").amt20.shift(1)
        ok = prev_amt.reindex(days) >= CAND_AMT
        cnt += ok.fillna(False).astype(int)
        if ok.any():
            union.add(c)
    pages = math.ceil((len(days) + 2) * bars_per_day / 900)
    L.append(f"\n## 3. 3장 측정 가능성 (추정, 실제 대량 수집 안 함)\n")
    L.append(f"- 최근 20거래일 ({days[0].date()} ~ {days[-1].date()}, 로컬 일봉 기준): 대상이 될 수 있는 종목(전날 20일 평균 거래대금 >= 40억) "
             f"하루 평균 {cnt.mean():.0f}종목 (최소 {cnt.min()}, 최대 {cnt.max()}), 20일 합집합 {len(union)}종목")
    L.append(f"- 15:19 분봉은 연속조회로 지금부터 거슬러 받아야 한다. 하루 분봉 {bars_per_day:.0f}개 (1번, 거래대금 1위 종목 기준), 1회 900개 → "
             f"20거래일 + 이후 날짜를 덮는 데 종목당 약 {pages}회")
    est = len(union) * pages
    L.append(f"- 필요한 요청: 약 {len(union)} x {pages} = {est:,}회. 1초 5회 기준 약 {est / RPS / 60:.0f}분, 1번에서 잰 요청당 "
             f"{sec / max(n, 1):.2f}초 기준 약 {est * sec / max(n, 1) / 60:.0f}분 (거래가 적은 종목은 하루 분봉이 적어 요청이 줄 수 있음)")
    cnt.to_csv(os.path.join(OUT, "candidates_last20.csv"), header=["candidates"])
    with open(os.path.join(OUT, "probe.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
