"""
보유 장부 positions.json (EXEC_SPEC 2장, 11E Codex 5·6·7·8·12·20·25 반영, 2026-10-24).

형식:
  held: {종목: {qty, avg_price, trade_id, entry_date, entry_price, stop_price, stop_temp, stop_basis, kind,
                trend_sell_next_open, judged_on, close_1530}}
  sell_duty: {종목: {kind(stop 손절 진행 중 / trend 추세 매도), since(발생일), at(발생 시각), reason, price?, stop?}}
             (11J 보완 3: 매도 의무는 종목 코드 단위. 끝 = 성공한 계좌 조회에서 그 종목 보유 0, orders.end_duties)
  next_open_sells: {종목: {reason, since, attempts, last_status, last_try, last_intent}}   (다음 날 시가 매도 목록 = 15:35·15:40 에 계좌 보유가 남은 의무 종목)
  trade_seq, duty_format, _version, _saved_at

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


DUTY_FORMAT = 2  # 11J 보완 3: 매도 의무 종목 코드 단위 (sell_duty). 없으면 예전 형식 -> normalize 가 바꾼다


def empty():
    return {"held": {}, "sell_duty": {}, "next_open_sells": {}, "trade_seq": 0, "duty_format": DUTY_FORMAT}


def normalize(pos):
    pos = dict(pos or {})
    pos.setdefault("held", {})
    nos = pos.get("next_open_sells") or {}
    if isinstance(nos, list):  # 예전 형식 (종목 목록)
        nos = {c: {"reason": "예전 형식에서 옮김", "since": None, "attempts": 0, "last_status": None} for c in nos}
    pos["next_open_sells"] = nos
    pos.setdefault("sell_duty", {})
    pos.setdefault("trade_seq", 0)
    if pos.get("duty_format") != DUTY_FORMAT:
        convert_duties(pos)
    return pos


def convert_duties(pos):
    """11J 보완 3: 예전 형식(거래 단위 515f554·trade_id 없음)의 매도 의무를 종목 코드 단위(sell_duty)로 바꾼다.
    held[c].stop_triggered·추세 판정(trend_sell_next_open) = 지금 보유 거래에 붙은 표시 -> 손절·추세 의무.
    다음 날 시가 목록 항목 (R2): trade_id 가 지금 보유 거래와 같으면 의무, 다르면(이전 거래 것) 버림. trade_id 가 없으면(d4d6c64 형식)
    항목 날짜(since)가 있고 지금 보유 거래의 진입일보다 뒤일 때만 의무 (같은 날·앞이면 이전 거래 것일 수 있음). 판단할 수 없으면
    의무로 만들지 않고 목록에서 빼서 pos["duty_converted"]["_review"] 에 둔다 (상주 프로그램이 시작 때 알림으로 사람에게 확인 요청).
    바꾼 내용은 pos["duty_converted"] (시작 때 계좌와 맞춰 보고 알림)"""
    duty, nos, conv, review = pos["sell_duty"], pos["next_open_sells"], {}, {}
    for c, p in pos["held"].items():
        st = p.pop("stop_triggered", None)
        if st and c not in duty:
            duty[c] = {"kind": "stop", "since": str(st.get("since", ""))[:10] or None, "at": str(st.get("since", ""))[11:19] or None,
                       "reason": f"현재가 {float(st.get('price', 0)):.0f} < 손절가 {float(st.get('stop', 0)):.0f} (예전 형식)",
                       "price": st.get("price"), "stop": st.get("stop")}
            conv[c] = "손절 진행 중 -> 손절 의무"
    for c, e in list(nos.items()):
        e = dict(e) if isinstance(e, dict) else {}
        p = pos["held"].get(c) or {}
        tid, cur, ed, since = e.pop("trade_id", None), p.get("trade_id"), p.get("entry_date"), str(e.get("since") or "")[:10]
        if tid and cur and tid != cur:
            nos.pop(c)
            conv[c] = f"다음 날 시가 목록 항목이 이전 거래 {tid} 것 -> 버림"
            continue
        if not ((tid and tid == cur) or (not tid and since and ed and since > str(ed))):
            nos.pop(c)
            review[c] = {"held": {k: p.get(k) for k in ("qty", "trade_id", "entry_date")} if p else None, "entry": {**e, **({"trade_id": tid} if tid else {})}}
            conv[c] = "; ".join(x for x in (conv.get(c), "옛 목록 항목이 지금 보유 거래 것인지 판단 못 함 -> 목록에서 뺌 (사람 확인)") if x)
            continue
        nos[c] = e
        if c not in duty:
            duty[c] = {"kind": "trend", "since": e.get("since"), "at": None, "reason": e.get("reason") or "다음 날 시가 매도 목록 (예전 형식)"}
            conv.setdefault(c, "다음 날 시가 목록 -> 추세 의무")
    for c, p in pos["held"].items():
        if p.get("trend_sell_next_open") and c not in duty:
            duty[c] = {"kind": "trend", "since": p.get("judged_on"), "at": None, "reason": f"추세 매도 ({p.get('judged_on')} 종가 < 20일선, 예전 형식)"}
            nos.setdefault(c, {"reason": duty[c]["reason"], "since": p.get("judged_on"), "attempts": 0, "last_status": None})
            conv.setdefault(c, "추세 판정 -> 추세 의무")
    pos["duty_format"] = DUTY_FORMAT
    if conv:
        pos["duty_converted"] = {**pos.get("duty_converted", {}), **conv, **({"_review": review} if review else {})}
    return conv


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


# ---------------------------------------------------------------- 체결로 설명 안 되는 보유 수량 변화 (11K 보완 3)

def mark_qty_review(pos, code, acct_qty, day, at, where, price=None):
    """체결로 설명되지 않는 보유 수량 변화 (액면분할·병합·무상증자·사람 거래 등, 자동 조정 안 함) -> 수동 확인 필요 (qty_review).
    손절가·진입가는 그대로. 수량만 계좌 값으로 (같은 변화를 다시 감지하지 않게, 옛 수량은 표시에). 반환 표시"""
    p = pos["held"][code]
    old = int(p.get("qty", 0))
    rec = {"date": str(day), "at": at, "where": where, "file_qty": old, "account_qty": int(acct_qty),
           "ratio": f"{int(acct_qty)}/{old} = {int(acct_qty) / old:.3f}" if old else "-", "stop": p.get("stop_price"), "price": price,
           "clear": "사람이 공시·체결 내역으로 확인한 뒤 이 표시(qty_review)를 지우거나 손절가를 고친다 (손절가가 바뀌면 끝난 것으로 봄)"}
    p["qty_review"] = rec
    p["qty"] = int(acct_qty)
    return rec


def in_review(p):
    """수동 확인 중인가: 표시가 있고 손절가가 표시 때 그대로 (사람이 손절가를 고치면 끝). 이 동안 그 종목 자동 손절·추세 매도·신규 매수 안 함"""
    r = (p or {}).get("qty_review")
    if not r:
        return False
    s0, s1 = r.get("stop"), (p or {}).get("stop_price")
    return s0 is None or s1 is None or abs(float(s1) - float(s0)) < 1e-9


def review_codes(pos):
    return {c for c, p in (pos or {}).get("held", {}).items() if in_review(p)}


def mark_synced(pos, day, at, by):
    """보유 장부 수량을 계좌로 맞춘 때 (11K 보완 3: 이 날 이후의 의도만 수량 변화를 설명한다)"""
    pos["qty_synced"] = {"date": str(day), "time": at, "by": by}


# ---------------------------------------------------------------- 현금 대조 (Codex 20)

def expected_cash(prev_d2, records, year, commission=None):
    """전일 D+2 추정예수금 + 당일 체결 금액 ± 비용 (수수료 commission, 없으면 백테스트 0.015%, 매도세 그해 세율). 반환 (기대값, 체결 건수).
    11K 11: 수수료는 환경별 (config commission: 모의 0.35%, 실전 0.015%)"""
    tax = sb.SELL_TAX.get(int(year), 0.002)
    fee = sb.COMMISSION if commission is None else float(commission)
    cash, n = float(prev_d2), 0
    for r in records:
        q, px = int(r.get("filled_qty") or 0), r.get("fill_price")
        if q <= 0 or not px:
            continue
        n += 1
        amt = float(px) * q
        if r.get("side") == "buy":
            cash -= amt * (1 + fee)
        elif r.get("side") == "sell":
            cash += amt * (1 - fee - tax)
    return cash, n


def fill_amount(records, kind=None, side=None):
    """체결 금액 합 (kind·side 로 거름)"""
    return float(sum(float(r["fill_price"]) * int(r["filled_qty"]) for r in records if int(r.get("filled_qty") or 0) > 0 and r.get("fill_price")
                     and (kind is None or r.get("kind") == kind) and (side is None or r.get("side") == side)))


def cash_tolerance(n_fills):
    """비용 반올림 수준: 체결 1건마다 수수료·세금 10원 단위 절사 2번 + 1원"""
    return 20.0 * n_fills + 1.0
