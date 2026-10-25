"""
보유 장부 positions.json (EXEC_SPEC 2장, 11E Codex 5·6·7·8·12·20·25 반영, 2026-10-24).

형식:
  held: {종목: {qty, avg_price, trade_id, entry_date, entry_price, stop_price, stop_temp, stop_basis, kind,
                trend_sell_next_open, judged_on, close_1530}}
  next_open_sells: {종목: {reason, since, attempts, last_status, last_try, last_intent}}   (Codex 12: 접수·체결 확인까지 유지)
  trade_seq, _version, _saved_at

- 저장할 때 직전 파일을 positions.json.bak 으로 남긴다 (Codex 8). 파일이 없으면 빈 보유. 읽기 실패(손상)면 손상 파일을 보관하고 백업으로 복구,
  백업도 못 읽으면 빈 보유 + 복구 실패. 복구했으면 그날 신규 매수 중단 (손절은 계좌 + 백업 손절가로 계속).
- 같은 종목을 청산한 뒤 다시 사면 새 거래 (trade_id 새로, 진입일·손절가·분류 새로, Codex 7).
- 손절가 = 진입일 종가 - 1.7 x ATR20 (원주가). 일봉 대상에서 빠진 종목(수정주가 계수 변경 등)은 원주가 일봉으로 ATR20 (Codex 6).
  진입가 정보가 없으면 계좌 평균단가 - 1.7 x ATR20 을 임시 손절가로 쓰고 stop_temp = True (Codex 5).
"""
import json
import os
import shutil

import numpy as np
import pandas as pd

from common import atomic_json, data_file, now_kst, s4, sb, sd

ATR_MULT = s4.STOP_ATR  # 1.7


def empty():
    return {"held": {}, "next_open_sells": {}, "trade_seq": 0}


def normalize(pos):
    pos = dict(pos or {})
    pos.setdefault("held", {})
    nos = pos.get("next_open_sells") or {}
    if isinstance(nos, list):  # 예전 형식 (종목 목록)
        nos = {c: {"reason": "예전 형식에서 옮김", "since": None, "attempts": 0, "last_status": None} for c in nos}
    pos["next_open_sells"] = nos
    pos.setdefault("trade_seq", 0)
    return pos


def load(path):
    """반환: (보유 장부, 정보 dict(status: ok / missing / recovered_backup / corrupt_unrecovered, ...))"""
    if not os.path.exists(path):
        return empty(), {"status": "missing"}
    try:
        with open(path, encoding="utf-8") as f:
            return normalize(json.load(f)), {"status": "ok"}
    except Exception as e:  # noqa: BLE001
        kept = f"{path}.corrupt-{now_kst():%Y%m%d%H%M%S}"
        os.replace(path, kept)
        bak = path + ".bak"
        try:
            with open(bak, encoding="utf-8") as f:
                pos = normalize(json.load(f))
            atomic_json(path, pos)
            return pos, {"status": "recovered_backup", "error": repr(e)[:200], "kept": kept}
        except Exception as e2:  # noqa: BLE001
            return empty(), {"status": "corrupt_unrecovered", "error": repr(e)[:200], "backup_error": repr(e2)[:200], "kept": kept}


def save(path, pos):
    """직전 파일을 .bak 으로 두고 저장 (저장 시각·버전 기록)"""
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak")
    pos["_version"] = int(pos.get("_version", 0)) + 1
    pos["_saved_at"] = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    atomic_json(path, pos)


def new_trade(pos, code, entry_date, **kw):
    """새 거래 (Codex 7): trade_id 새로"""
    pos["trade_seq"] = int(pos.get("trade_seq", 0)) + 1
    t = {"trade_id": f"{code}-{entry_date}-{pos['trade_seq']}", "entry_date": str(entry_date), "trend_sell_next_open": False, **kw}
    pos["held"][code] = t
    return t


# ---------------------------------------------------------------- 원주가 일봉 (손절가 계산용)

def raw_daily(code):
    """원주가 일봉 = data/daily_raw + 추가분(raw.parquet). 날짜 중복은 추가분 우선"""
    parts = []
    p = os.path.join(sd.RAW_DIR, f"{code}.parquet")
    if os.path.exists(p):
        parts.append(pd.read_parquet(p)[["timestamp", "open", "high", "low", "close"]])
    ap = data_file("raw.parquet")
    if os.path.exists(ap):
        a = pd.read_parquet(ap)
        parts.append(a[a.code == code][["timestamp", "open", "high", "low", "close"]])
    if not parts:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close"])
    df = pd.concat(parts, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df.timestamp)
    return df.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)


def raw_atr20(code, until, today_row=None):
    """until 까지 원주가 일봉 (+ 오늘 행) 의 마지막 종가와 ATR20 (load_stock 과 같은 TR 정의). 반환 (종가, ATR20) 또는 (None, None)"""
    df = raw_daily(code)
    df = df[df.timestamp <= pd.Timestamp(until)]
    if today_row is not None:
        df = pd.concat([df[df.timestamp < pd.Timestamp(until)], pd.DataFrame([{"timestamp": pd.Timestamp(until), **today_row}])], ignore_index=True)
    if len(df) < 21:
        return None, None
    pc = df.close.shift(1)
    tr = np.maximum(df.high - df.low, np.maximum((df.high - pc).abs(), (df.low - pc).abs()))
    atr = tr.rolling(20).mean().iat[-1]
    return float(df.close.iat[-1]), float(atr) if np.isfinite(atr) else None


def stop_from_raw(code, day, today_row=None):
    """원주가 기준 손절가 = 종가 - 1.7 x ATR20 (일봉 대상에서 빠진 종목용, Codex 6)"""
    close, atr = raw_atr20(code, day, today_row)
    if close is None or atr is None:
        return None, "원주가 일봉 부족"
    return close - ATR_MULT * atr, f"원주가 일봉 종가 {close:,.0f} - 1.7 x ATR20 {atr:,.1f}"


def raw_judge(code, day, today_row=None):
    """원주가 일봉으로 day 기준 판단 (일봉 대상에서 빠진 종목, Codex 6): (종가, 손절가, 종가 < 20일선) 또는 None (day 행 없음·일봉 부족)"""
    df = raw_daily(code)
    df = df[df.timestamp <= pd.Timestamp(day)]
    if today_row is not None:
        df = pd.concat([df[df.timestamp < pd.Timestamp(day)], pd.DataFrame([{"timestamp": pd.Timestamp(day), **today_row}])], ignore_index=True)
    if len(df) < 21 or df.timestamp.iat[-1] != pd.Timestamp(day):
        return None
    pc = df.close.shift(1)
    tr = np.maximum(df.high - df.low, np.maximum((df.high - pc).abs(), (df.low - pc).abs()))
    atr, ma20, close = tr.rolling(20).mean().iat[-1], df.close.rolling(20).mean().iat[-1], float(df.close.iat[-1])
    if not (np.isfinite(atr) and np.isfinite(ma20)):
        return None
    return close, close - ATR_MULT * float(atr), bool(close < ma20)


def temp_stop(code, avg_price, day):
    """임시 손절가 (Codex 5): 진입가 정보가 없으면 계좌 평균단가 - 1.7 x ATR20 (원주가). ATR 이 없으면 평균단가 x 0.9"""
    _, atr = raw_atr20(code, day)
    if atr is not None and avg_price:
        return float(avg_price) - ATR_MULT * atr, f"임시: 계좌 평균단가 {avg_price:,.0f} - 1.7 x ATR20 {atr:,.1f}"
    return float(avg_price or 0) * 0.9, "임시: 원주가 일봉 부족 -> 계좌 평균단가 x 0.9"


# ---------------------------------------------------------------- 현금 대조 (Codex 20)

def expected_cash(prev_d2, records, year):
    """전일 D+2 추정예수금 + 당일 체결 금액 ± 비용 (수수료 0.015%, 매도세 그해 세율). 반환 (기대값, 체결 건수)"""
    tax = sb.SELL_TAX.get(int(year), 0.002)
    cash, n = float(prev_d2), 0
    for r in records:
        q, px = int(r.get("filled_qty") or 0), r.get("fill_price")
        if q <= 0 or not px:
            continue
        n += 1
        amt = float(px) * q
        if r.get("side") == "buy":
            cash -= amt * (1 + sb.COMMISSION)
        elif r.get("side") == "sell":
            cash += amt * (1 - sb.COMMISSION - tax)
    return cash, n


def cash_tolerance(n_fills):
    """비용 반올림 수준: 체결 1건마다 수수료·세금 10원 단위 절사 2번 + 1원"""
    return 20.0 * n_fills + 1.0
