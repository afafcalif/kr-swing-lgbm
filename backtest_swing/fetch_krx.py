"""
한국거래소 정보데이터시스템 Open API: 정규장 공식 일별 시세 (키움 일봉에는 2025-03-04 넥스트레이드 출범 이후
장 마감 후 거래 가격이 시가·고가·저가·종가에 섞여 있어서, 그 이후 구간은 이 데이터로 바로잡는다)

  python backtest_swing/fetch_krx.py probe                  # 요청 1번: 주소·응답 형식 확인
  python backtest_swing/fetch_krx.py verify                 # 정답을 아는 값으로 대조 (요청 약 12번)
  python backtest_swing/fetch_krx.py fetch 20250304 20261016   # 기간 전체 (거래일 x 시장 2개 + 지수 2개)

인증키: .env 의 KRX_API_KEY (출력하지 않는다)
결과: data/krx_daily/{YYYYMMDD}.parquet  (KOSPI + KOSDAQ 전 종목, 원주가)
      data/krx_index.parquet           (코스피·코스닥 지수 일별)
"""
import os
import sys
import time

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
KEY = os.getenv("KRX_API_KEY", "")
BASE = "https://data-dbg.krx.co.kr/svc/apis"
MARKETS = {"KOSPI": "sto/stk_bydd_trd", "KOSDAQ": "sto/ksq_bydd_trd"}
INDEX = {"KOSPI": "idx/kospi_dd_trd", "KOSDAQ": "idx/kosdaq_dd_trd"}
OUT_DIR = os.path.join(ROOT, "data", "krx_daily")
INDEX_OUT = os.path.join(ROOT, "data", "krx_index.parquet")
PAUSE = 0.2


def call(path: str, bas_dd: str) -> list[dict]:
    r = requests.get(f"{BASE}/{path}", headers={"AUTH_KEY": KEY}, params={"basDd": bas_dd}, timeout=30)
    r.raise_for_status()
    return r.json().get("OutBlock_1", [])


def num(x):
    s = str(x).replace(",", "").strip()
    return float(s) if s not in ("", "-") else float("nan")


def stocks_on(bas_dd: str) -> pd.DataFrame:
    parts = []
    for mkt, path in MARKETS.items():
        rows = call(path, bas_dd)
        time.sleep(PAUSE)
        if rows:
            df = pd.DataFrame(rows)
            df["market"] = mkt
            parts.append(df)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    out = pd.DataFrame({
        "date": pd.to_datetime(df["BAS_DD"]), "code": df["ISU_CD"].astype(str), "name": df.get("ISU_NM"),
        "market": df["market"], "open": df["TDD_OPNPRC"].map(num), "high": df["TDD_HGPRC"].map(num),
        "low": df["TDD_LWPRC"].map(num), "close": df["TDD_CLSPRC"].map(num), "volume": df["ACC_TRDVOL"].map(num),
        "amount": df["ACC_TRDVAL"].map(num), "list_shares": df.get("LIST_SHRS", pd.Series(dtype=str)).map(num),
    })
    return out


def probe():
    print("key loaded:", bool(KEY))
    rows = call(MARKETS["KOSPI"], "20261020")
    print("rows", len(rows), "| fields", list(rows[0].keys()) if rows else None)
    print([r for r in rows if str(r.get("ISU_CD", "")).endswith("006400")][:1])


def verify():
    # 2026-10-20 값(삼성SDI 556,000, 대덕전자 132,300)은 API 가 다음 날부터 제공해서 여기서 뺀다
    known = {("006400", "20261019"): 592000, ("353200", "20261019"): 133800, ("373220", "20250331"): 330000, ("035420", "20250630"): 263500,
             ("033780", "20250630"): 127400, ("267260", "20250930"): 581000}
    days = sorted({d for _, d in known})
    got = {d: stocks_on(d) for d in days}
    ok = 0
    for (code, d), v in known.items():
        x = got[d][got[d].code == code]
        c = float(x.close.iloc[0]) if len(x) else float("nan")
        ok += c == v
        print(f"  {code} {d}: KRX close {c:,.0f} vs known {v:,} -> {'MATCH' if c == v else 'DIFF'}")
    print(f"matched {ok}/{len(known)}")
    # 넥스트레이드 이전 날짜는 키움 일봉과 같아야 한다
    pre = stocks_on("20250228")
    same = []
    for code in ("006400", "353200", "373220", "035420", "033780", "267260", "005930", "000660"):
        k = pd.read_parquet(os.path.join(ROOT, "data", "daily_raw", f"{code}.parquet"))
        k = k[k.timestamp == "2025-02-28"]
        x = pre[pre.code == code]
        if len(k) and len(x):
            row = tuple(float(x[c].iloc[0]) == float(k[c].iloc[0]) for c in ("open", "high", "low", "close", "volume"))
            same.append(all(row))
            print(f"  2025-02-28 {code}: KRX vs Kiwoom open/high/low/close/volume equal -> {row}")
    print(f"pre-NXT day identical: {sum(same)}/{len(same)}")


def fetch(start: str, end: str):
    os.makedirs(OUT_DIR, exist_ok=True)
    cal = pd.read_parquet(os.path.join(ROOT, "data", "kospi_index_long.parquet")).timestamp
    days = [d.strftime("%Y%m%d") for d in cal if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    t0, n = time.time(), 0
    idx_rows = []
    for i, d in enumerate(days):
        path = os.path.join(OUT_DIR, f"{d}.parquet")
        if not os.path.exists(path):
            df = stocks_on(d)
            if df.empty:
                print(f"[empty] {d}", flush=True)
            else:
                df.to_parquet(path, index=False)
                n += 1
        for mkt, p in INDEX.items():
            for r in call(p, d):
                if r.get("IDX_NM") in ("코스피", "코스닥"):
                    idx_rows.append({"date": pd.to_datetime(d), "index": mkt, "close": num(r.get("CLSPRC_IDX")),
                                     "open": num(r.get("OPNPRC_IDX")), "high": num(r.get("HGPRC_IDX")), "low": num(r.get("LWPRC_IDX"))})
            time.sleep(PAUSE)
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(days)} days ({time.time() - t0:.0f}s)", flush=True)
    pd.DataFrame(idx_rows).to_parquet(INDEX_OUT, index=False)
    print(f"done: {n} new day files, {len(days)} days, index rows {len(idx_rows)} ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    cmd = sys.argv[1]
    {"probe": probe, "verify": verify}.get(cmd, lambda: fetch(sys.argv[2], sys.argv[3]))()
