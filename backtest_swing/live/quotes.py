"""
시세 조회 (EXEC_SPEC 1-3, 1-5). 시세 조회 TR 만: ka10095 (관심종목정보, 100종목씩), ka20001 (업종현재가), ka10080 (1분봉).
종목코드는 접미사 없는 코드 = KRX (8장 5).
"""
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import num  # noqa: E402

BATCH = 100
AMOUNT_UNIT = 1_000_000  # ka10095 trde_prica: 백만원 (시험 B 에서 거래량 x 현재가와 대조해 기록)
INDEX = (("kospi", "0", "001"), ("kosdaq", "1", "101"))


def snapshot(caller, codes):
    """ka10095 100종목씩. 반환: (응답 전체 DataFrame, 파싱 DataFrame, 기록)"""
    t0 = time.time()
    items, req = [], []
    for bi in range(0, len(codes), BATCH):
        batch = codes[bi:bi + BATCH]
        ts = pd.Timestamp.now()
        j, _, _ = caller.post("ka10095", "/api/dostk/stkinfo", {"stk_cd": "|".join(batch)})
        te = pd.Timestamp.now()
        got = j.get("atn_stk_infr") or []
        req.append({"batch": bi // BATCH, "n_req": len(batch), "n_resp": len(got), "sent": ts.strftime("%H:%M:%S.%f")[:-3],
                    "recv": te.strftime("%H:%M:%S.%f")[:-3], "return_code": j.get("return_code")})
        for it in got:
            items.append({**{k: str(v) for k, v in it.items()}, "_batch": bi // BATCH, "_recv": te.strftime("%H:%M:%S.%f")[:-3]})
    raw = pd.DataFrame(items)
    p = pd.DataFrame({"code": raw.stk_cd.str.replace("_AL", "").str.replace("_NX", ""),
                      "open": raw.open_pric.map(num), "high": raw.high_pric.map(num), "low": raw.low_pric.map(num),
                      "close": raw.cur_prc.map(num), "volume": raw.trde_qty.map(num),
                      "amount": raw.trde_prica.map(num) * AMOUNT_UNIT, "cntr_tm": raw.cntr_tm, "dt": raw.dt}) if len(raw) else pd.DataFrame()
    missing = sorted(set(codes) - set(p.code)) if len(p) else list(codes)
    rec = {"requests": len(req), "seconds": round(time.time() - t0, 2), "codes": len(codes), "returned": len(p), "missing": missing,
           "batches": req}
    return raw, p, rec


def index_now(caller):
    out, raw = {}, {}
    for name, mrkt, inds in INDEX:
        j, _, _ = caller.post("ka20001", "/api/dostk/sect", {"mrkt_tp": mrkt, "inds_cd": inds})
        out[name] = num(j.get("cur_prc"))
        raw[name] = {k: v for k, v in j.items() if not isinstance(v, list)}
        raw[name]["_recv"] = pd.Timestamp.now().strftime("%H:%M:%S.%f")[:-3]
    return out, raw


def bars_today(caller, code, day):
    """ka10080 1분봉 첫 페이지 (최근 900봉) 에서 그날 15:30 까지"""
    j, _, _ = caller.post("ka10080", "/api/dostk/chart", {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "0"})
    d = day.strftime("%Y%m%d")
    rows = [{"ts": it["cntr_tm"], "open": num(it.get("open_pric")), "high": num(it.get("high_pric")), "low": num(it.get("low_pric")),
             "close": num(it.get("cur_prc")), "volume": num(it.get("trde_qty"))}
            for it in j.get("stk_min_pole_chart_qry", []) or [] if str(it.get("cntr_tm", ""))[:8] == d]
    df = pd.DataFrame(rows)
    if len(df):
        df = df.drop_duplicates("ts")
        df = df[df.ts.str[8:12] <= "1530"].sort_values("ts").reset_index(drop=True)
    return df


def close_values(bars):
    """15:30 까지 1분봉 -> 그날 행 (시가 첫 봉, 고가·저가, 종가 = 15:30 이하 마지막 봉, 거래량 합, 거래대금 = (거래량 x 종가) 합)"""
    if bars is None or bars.empty:
        return None
    return {"open": float(bars.open.iloc[0]), "high": float(bars.high.max()), "low": float(bars.low.min()), "close": float(bars.close.iloc[-1]),
            "volume": float(bars.volume.sum()), "amount": float((bars.volume * bars.close).sum()), "last_ts": bars.ts.iloc[-1]}


def index_close_today(caller, day):
    """ka20005 업종 1분봉 첫 페이지에서 그날 15:30 이하 마지막 봉 종가 (값은 100배)"""
    out = {}
    for name, _, inds in INDEX:
        j, _, _ = caller.post("ka20005", "/api/dostk/chart", {"inds_cd": inds, "tic_scope": "1"})
        d = day.strftime("%Y%m%d")
        b = sorted((it["cntr_tm"], num(it.get("cur_prc")) / 100.0) for it in j.get("inds_min_pole_qry", []) or []
                   if str(it.get("cntr_tm", ""))[:8] == d and str(it["cntr_tm"])[8:12] <= "1530")
        out[name] = b[-1][1] if b else np.nan
        out[name + "_ts"] = b[-1][0] if b else None
    return out
