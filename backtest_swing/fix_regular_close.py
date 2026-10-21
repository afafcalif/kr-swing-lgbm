"""
키움 일봉의 장 마감 후 가격 섞임을 거래소 공식 정규장 시세로 바로잡는다

  python backtest_swing/fix_regular_close.py audit   # 2025-03-04 ~ 2026-10-19 전 종목·전 거래일 대조 (수정 안 함)
  python backtest_swing/fix_regular_close.py apply   # 대조 후, 오염 구간 고가·저가·종가 교체 (원본은 백업)

대상: data/daily_raw (원주가), data/daily_merged · data/daily_ext (수정주가 = 공식 원주가 x 키움 시가 / 공식 시가)
       data/daily_raw 의 factor 는 고친 값으로 다시 계산 (수정주가 종가 / 원주가 종가)
공식 시세: data/krx_daily (backtest_swing/fetch_krx.py fetch 로 받은 것 + 없는 날은 kiwoom/krx_official 로 요청)
백업: data/backup_aftermarket_20261020/{daily_raw,daily_merged,daily_ext}/ (바뀌는 파일만)
결과: backtest_swing/results/regular_close_audit.csv (월별 불일치), regular_close_fix.csv (종목별 교체 행 수)
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import pandas as pd

from kiwoom import krx_official as ko

START, END = pd.Timestamp("2025-03-04"), pd.Timestamp("2026-10-19")
RAW, MERGED, EXT = "data/daily_raw", "data/daily_merged", "data/daily_ext"
BACKUP = "data/backup_aftermarket_20261020"
OUT = "backtest_swing/results"
COLS = ("open", "high", "low", "close", "volume")


def official() -> dict:
    off = ko.load_range(START, END)
    return dict(tuple(off.groupby("code")))


def audit(by_code: dict) -> pd.DataFrame:
    rows = []
    for f in os.listdir(RAW):
        code = f[:-8]
        if code not in by_code:
            continue
        d = pd.read_parquet(os.path.join(RAW, f))
        x = d[(d.timestamp >= START) & (d.timestamp <= END)].merge(
            by_code[code], left_on="timestamp", right_on="date", suffixes=("", "_k"))
        # 공식 시가 0 = 정규장 체결 없음(시간외 체결만): 공식 시가·고가·저가가 0 이라 비교 대상이 아님
        x = x[(x.volume_k > 0) & (x.open_k > 0)]
        rows.append(pd.DataFrame({"month": x.timestamp.dt.to_period("M").astype(str), "day": x.timestamp,
                                  **{c: x[c] != x[f"{c}_k"] for c in COLS}}))
    a = pd.concat(rows, ignore_index=True)
    m = a.groupby("month").agg(stock_days=("day", "size"), **{f"{c}_diff": (c, "sum") for c in COLS})
    first = a[a.close | a.high | a.low].day.min()
    last_clean = a[(a.day < first)].day.max() if pd.notna(first) else None
    m.to_csv(os.path.join(OUT, "regular_close_audit.csv"), encoding="utf-8-sig")
    print(m.to_string())
    print(f"first day with high/low/close mismatch: {first.date() if pd.notna(first) else None} "
          f"(last day fully matching before it: {last_clean.date() if last_clean is not None else None}); "
          f"open mismatches {int(a.open.sum())}, volume mismatches {int(a.volume.sum())}")
    return a


def apply(by_code: dict):
    a = audit(by_code)
    first = a[a.close | a.high | a.low].day.min()
    assert pd.isna(first) or first >= ko.FIX_START, f"오염이 {first.date()} 부터 있음 - kiwoom/krx_official.FIX_START({ko.FIX_START.date()}) 보다 이르다"
    assert not a.open.any() and not a.volume.any(), "시가·거래량 불일치가 있음 - 시가 기준 환산을 쓸 수 없다"
    stats = []
    for folder in (RAW, MERGED, EXT):
        os.makedirs(os.path.join(BACKUP, os.path.basename(folder)), exist_ok=True)
        for f in os.listdir(folder):
            code = f[:-8]
            if code not in by_code:
                continue
            path = os.path.join(folder, f)
            d = pd.read_parquet(path)
            new, _ = ko.apply_regular_session(d, by_code[code])
            changed = int((new[["high", "low", "close"]] != d[["high", "low", "close"]]).any(axis=1).sum())
            if changed:
                shutil.copy2(path, os.path.join(BACKUP, os.path.basename(folder), f))
                new.to_parquet(path, index=False)
            stats.append({"folder": os.path.basename(folder), "code": code, "rows_changed": changed})
    # 원주가 factor 다시 계산 (수정주가 종가 / 원주가 종가)
    for f in os.listdir(RAW):
        code = f[:-8]
        raw = pd.read_parquet(os.path.join(RAW, f))
        src = os.path.join(MERGED, f)
        if code not in by_code or not os.path.exists(src):
            continue
        adj = pd.read_parquet(src)[["timestamp", "close"]].rename(columns={"close": "adj_close"})
        adj["timestamp"] = pd.to_datetime(adj.timestamp)
        recent = raw.timestamp >= ko.FIX_START
        m = raw.loc[recent, ["timestamp", "close"]].merge(adj, on="timestamp", how="left")
        new_f = (m.adj_close / m.close.where(m.close > 0)).to_numpy()
        if not pd.Series(new_f).equals(raw.loc[recent, "factor"].reset_index(drop=True)):
            raw.loc[recent, "factor"] = new_f
            raw.to_parquet(os.path.join(RAW, f), index=False)
    s = pd.DataFrame(stats)
    s.to_csv(os.path.join(OUT, "regular_close_fix.csv"), index=False, encoding="utf-8-sig")
    print(s.groupby("folder").agg(files=("code", "size"), files_changed=("rows_changed", lambda x: int((x > 0).sum())),
                                  rows_changed=("rows_changed", "sum")).to_string())


if __name__ == "__main__":
    by = official()
    {"audit": audit, "apply": apply}[sys.argv[1]](by)
