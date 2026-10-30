"""
한국거래소 정보데이터시스템 Open API 정규장 공식 일별 시세

키움 일봉(ka10081)은 2026-09-14 부터 고가·저가·종가에 장 마감 후(넥스트레이드) 거래 가격이 섞여 나온다
(시가·거래량은 그대로 정규장 값). 그날 이후 날짜는 거래소 공식 시세로 고가·저가·종가를 덮어쓴다.

- 인증키: .env 의 KRX_API_KEY
- 캐시: data/krx_daily/{YYYYMMDD}.parquet (KOSPI + KOSDAQ 전 종목, 원주가)
- 공식 시세는 다음 날부터 제공된다 (당일 날짜는 빈 응답)
"""
from __future__ import annotations

import logging
import os
import time

import pandas as pd
import requests

from .config import get_settings  # noqa: F401  (.env 로드)

logger = logging.getLogger(__name__)

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
MARKET_PATHS = {"KOSPI": "sto/stk_bydd_trd", "KOSDAQ": "sto/ksq_bydd_trd"}
CACHE_DIR = "data/krx_daily"
FIX_START = pd.Timestamp("2026-09-14")  # 키움 일봉에 장 마감 후 가격이 섞이기 시작한 날


def _num(x) -> float:
    s = str(x).replace(",", "").strip()
    return float(s) if s not in ("", "-") else float("nan")


def fetch_day(bas_dd: str) -> pd.DataFrame:
    """하루치 KOSPI + KOSDAQ 전 종목 원주가. 휴장일·미제공일은 빈 DataFrame."""
    key = os.getenv("KRX_API_KEY", "")
    if not key:
        raise RuntimeError("KRX_API_KEY 가 .env 에 없습니다")
    parts = []
    for market, path in MARKET_PATHS.items():
        r = requests.get(f"{BASE_URL}/{path}", headers={"AUTH_KEY": key}, params={"basDd": bas_dd}, timeout=30)
        r.raise_for_status()
        rows = r.json().get("OutBlock_1", [])
        if rows:
            df = pd.DataFrame(rows)
            df["market"] = market
            parts.append(df)
        time.sleep(0.2)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    return pd.DataFrame({
        "date": pd.to_datetime(df["BAS_DD"]), "code": df["ISU_CD"].astype(str), "name": df.get("ISU_NM"),
        "market": df["market"], "open": df["TDD_OPNPRC"].map(_num), "high": df["TDD_HGPRC"].map(_num),
        "low": df["TDD_LWPRC"].map(_num), "close": df["TDD_CLSPRC"].map(_num), "volume": df["ACC_TRDVOL"].map(_num),
        "amount": df["ACC_TRDVAL"].map(_num), "list_shares": df["LIST_SHRS"].map(_num),
        # 전일대비 (종가 - 기준가). 기준가 = 종가 - chg 가 전날 종가와 다르면 그날 권리락·분할 등 (11P 공식 값 채움 확인용)
        "chg": df["CMPPREVDD_PRC"].map(_num) if "CMPPREVDD_PRC" in df else float("nan"),
    })


def load_range(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """start~end 평일의 공식 시세 (캐시에 없는 날만 요청해서 저장)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    parts = []
    for day in pd.bdate_range(start, end):
        path = os.path.join(CACHE_DIR, f"{day:%Y%m%d}.parquet")
        if os.path.exists(path):
            parts.append(pd.read_parquet(path))
            continue
        df = fetch_day(f"{day:%Y%m%d}")
        if not df.empty:
            df.to_parquet(path, index=False)
            parts.append(df)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def apply_regular_session(df: pd.DataFrame, official: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """
    한 종목 일봉(df: timestamp, open, high, low, close, ...)의 FIX_START 이후 고가·저가·종가를 공식 값으로 바꾼다.
    df 가 수정주가여도 맞도록, 환산 비율 = 키움 시가 / 공식 시가 (시가는 오염되지 않음)를 곱한다.
    official: 그 종목의 공식 시세 (date, open, high, low, close).
    공식 시가가 0 인 날(정규장 거래 없음·거래정지)은 키움 값 그대로 둔다.
    반환: (바뀐 df, FIX_START 이후인데 공식 시세가 없는 날 수)
    """
    out = df.copy()
    ts_all = pd.to_datetime(out.timestamp)
    recent = ts_all >= FIX_START
    if not recent.any():
        return out, 0
    o = official.set_index("date")
    ts = ts_all[recent]
    listed = ts.isin(o.index)
    have = listed & (o.reindex(ts).open.to_numpy() > 0)
    idx = ts[have].index
    if len(idx):
        k = o.reindex(ts_all[idx])
        ratio = out.loc[idx, "open"].to_numpy() / k.open.to_numpy()
        for col in ("high", "low", "close"):
            out.loc[idx, col] = (k[col].to_numpy() * ratio).round().astype(out[col].dtype)
    return out, int((~listed).sum())
