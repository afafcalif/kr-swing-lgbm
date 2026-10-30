"""
그림자 모드 하루 실행 (EXEC_SPEC 1-3, 3A 그림자 모드, 3B, 5장, 8장 12). 주문(kt10000·kt10001)·계좌 조회 없음, 시세 조회 TR 만 (실전 토큰).

  python backtest_swing/live/run_day.py shadow     # 장 전 준비 -> 15:18:30 스냅샷 -> 점수·두 장부 상위 3·주문 계획 기록 -> 15:32 이후 15:30 1분봉 기준 상위 3 -> 다시 돌리기 대조
  python backtest_swing/live/run_day.py rerun YYYY-MM-DD   # 저장한 스냅샷 + 실행 엔진과 같은 장부 상태로 동결 함수를 그대로 호출해 다시 계산, 실시간 상위 3 과 비교

결과: logs/exec/{날짜}.json, logs/exec/summary.csv, logs/exec/snapshots/{날짜}.parquet, logs/exec/ledger.json (가상 신호 장부 두 개)
장부 (EXEC_SPEC 1-3, 2026-10-22 결정):
  - 전략 장부 (strategy): 시장 필터 통과일의 상위 3 만. 실제 매수와 전진 검증 주 판정용.
  - 전체 장부 (full): 필터와 무관하게 매일 상위 3. 보조 판정·실행 점검 모드용.
주문 계획(보내지 않음): 필터 통과일이면 전략 장부 상위 3, 아니면 전체 장부 상위 3 ("점검용", 3B). 장부상 3,000만 원. 주문·계좌 항목은 비워 둔다.
"""
import json
import os
import subprocess
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (APPEND_DIR, CAPITAL, data_file, LOG_DIR, SNAP_DIR, Caller, LedgerConflict, SingleInstanceLock, git_commit, ledger_read,  # noqa: E402
                    ledger_target, ledger_write, resident_running, sd)
import engine as E  # noqa: E402
import model as M  # noqa: E402
import quotes as Q  # noqa: E402

SNAP_AT = "15:18:30"
CLOSE_AFTER = "15:32:00"
LEDGER = os.path.join(LOG_DIR, "ledger.json")
SUMMARY = os.path.join(LOG_DIR, "summary.csv")
NAMES = ("strategy", "full")
SUFFIX = ""  # 상주 프로그램이 돌고 있을 때 run_day 의 그날 로그·스냅샷 이름 뒤에 붙임 ("_shadow")


def now():
    return pd.Timestamp.now()


def wait_until(hms):
    while now().strftime("%H:%M:%S") < hms:
        time.sleep(0.5)


def jdump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)


def top_list(top, price_col):
    return [{"code": r.code, "score": float(r.score), price_col: float(getattr(r, "raw_close"))} for r in top.itertuples()]


def snap_anomalies(snap, raw=None, day=None):
    """확인 리뷰 27: 응답에는 있는데 계산 입력에서 빼는 데이터 이상. 반환 {code: 이유}.
    거래량 0(거래정지 포함)은 백테스트에서도 대상이 아니므로 이상이 아니다. 이상 = 거래량 > 0 인데 시가·고가·저가·현재가 중 0 이나 결측,
    또는 가격이 그날 상·하한가(응답의 upl_pric·lst_pric) 밖. raw(응답 원문, stk_cd·upl_pric·lst_pric)가 없으면 범위 확인은 안 함.
    11K 4: day 를 주면 오늘 것이 아닌 시세도 이상 = 거래량 > 0 인데 시세 날짜(dt) != day 이거나 체결 시각(cntr_tm) 없음·000000"""
    if day is not None and "dt" in snap and "cntr_tm" in snap:
        ds = pd.Timestamp(day).strftime("%Y%m%d")
        old = {}
        for r in snap.itertuples():
            if not (r.volume > 0):
                continue
            dt, tm = str(r.dt or "")[:8], str(r.cntr_tm or "")
            if dt != ds:
                old[r.code] = f"시세 날짜 {dt or '없음'} != {ds} (오늘 것 아님)"
            elif tm.strip("0") == "" or tm in ("nan", "None"):
                old[r.code] = f"거래량 > 0 인데 체결 시각 {tm or '없음'} (오늘 것인지 모름)"
        rest = snap[~snap.code.isin(list(old))]
        return {**old, **snap_anomalies(rest, raw)}
    from orders import code_of, num
    lim = {}
    if raw is not None and "stk_cd" in raw and "upl_pric" in raw and "lst_pric" in raw:
        lim = {code_of(c): (num(u), num(lo)) for c, u, lo in zip(raw.stk_cd, raw.upl_pric, raw.lst_pric)}
    out, bases = {}, base_prices(raw)
    for r in snap.itertuples():
        if not (r.volume > 0):
            continue
        px = [r.open, r.high, r.low, r.close]
        if any(not (pd.notna(v) and v > 0) for v in px):
            out[r.code] = "거래량 > 0 인데 시가·고가·저가·현재가 0 또는 결측"
            continue
        up, lo = lim.get(r.code, (float("nan"), float("nan")))
        if up > 0 and lo > 0 and (max(px) > up or min(px) < lo):
            out[r.code] = f"가격이 상·하한 범위 밖 (하한 {lo:.0f}, 상한 {up:.0f}, 가격 {min(px):.0f} ~ {max(px):.0f})"
            continue
        if bases is not None:  # 11Q A: 응답에 기준가 열이 있는데 이 종목 기준가가 없거나 0 이하·상하한 밖 = 그날 계수를 정할 수 없음
            b = bases.get(r.code, float("nan"))
            if not (np.isfinite(b) and b > 0) or (up > 0 and lo > 0 and not (lo < b < up)):
                out[r.code] = f"기준가 없음·비정상 ({b}, 하한 {lo}, 상한 {up})"
    return out


def base_prices(raw):
    """11Q A: 스냅샷 응답 원문의 기준가 {code: float}. 응답에 base_pric 열이 없으면(예전 기록·시험) None = 기준가를 쓰지 않음 (예전과 같음)"""
    if raw is None or "stk_cd" not in raw or "base_pric" not in raw:
        return None
    from orders import code_of, num
    return {code_of(c): num(b) for c, b in zip(raw.stk_cd, raw.base_pric)}


def snap_rows(snap, raw=None, day=None):
    """계산 입력: 거래량 > 0 이고 데이터 이상(snap_anomalies, day 를 주면 오늘 것이 아닌 시세 포함)이 아닌 종목.
    11Q A: 응답에 기준가가 있으면 행에 base(그날 기준가)·prev_day(직전 거래일)를 같이 넘겨 작업 프로세스가 그날 이벤트 계수를 정한다 (engine.default_factor)"""
    bad = snap_anomalies(snap, raw, day)
    bases = base_prices(raw)
    extra = {}
    if bases is not None and day is not None:
        import state as S
        extra["prev_day"] = str(S.prev_trading_day(pd.Timestamp(day)).date())
    return {r.code: ({"open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume, "amount": r.amount,
                      **({"base": bases.get(r.code), **extra} if bases is not None else {})}, None, None)
            for r in snap.itertuples() if r.volume > 0 and r.close > 0 and r.open > 0 and r.code not in bad}


def prepare(day, ledger_path=None):
    """장 전: 모델 로드 확인, 일봉 메모리 적재, 장부, 지수"""
    from daily import excluded_codes, index_series
    t0 = time.time()
    mo = day.strftime("%Y-%m")
    b, cols, meta, chk = M.load(mo)
    t_model = time.time() - t0
    index = index_series()
    import common as C
    vdir = C.data_dir(APPEND_DIR)  # 11S: 한 버전 폴더를 정해 update.json·작업 프로세스 적재 모두 그 폴더에서 (장중 재수집이 새 버전을 내도 섞이지 않게)
    up_path = os.path.join(vdir, "update.json")  # 확인 리뷰 15: 현재 버전 폴더
    upd = json.load(open(up_path, encoding="utf-8")) if os.path.exists(up_path) else {}
    prev_day = index["kospi"].index[index["kospi"].index < day].max()
    # 공식 시세가 없어 마지막 날을 빼면 지수도 짧아지므로, 실제 마지막 거래일은 update.json 값 (없으면 지수 기준)
    if upd.get("latest_trading_day") and pd.Timestamp(upd["latest_trading_day"]) < day:
        prev_day = max(prev_day, pd.Timestamp(upd["latest_trading_day"]))
    elif upd.get("official_missing_dates"):
        prev_day = max(prev_day, max(pd.Timestamp(x) for x in upd["official_missing_dates"]))
    # Codex 23: 휴장일 달력으로 계산한 직전 거래일과 데이터 버전의 마지막 날짜를 비교 (지수 기준 prev_day 는 기록만)
    import state as S
    exp_prev = S.prev_trading_day(day)
    last_date = (upd.get("dates") or [None])[-1]
    t1 = time.time()
    pool = E.ShardPool(sd.universe_codes(), use_append=True, vdir=vdir)
    try:
        lj, lver = ledger_read(ledger_path or LEDGER)  # 읽은 장부 경로·버전 = 쓸 때 경로·확인 버전 (2026-10-24 F-06·F-07)
    except BaseException:
        pool.close()  # 11O: 만든 묶음을 돌려주지 못하면 여기서 닫는다 (작업 프로세스가 남지 않게)
        raise
    ledgers = {n: lj[n] for n in NAMES}
    info = {"model": {"month": mo, "check": chk, "retrain_day": meta["retrain_day"], "train_end": meta["train_end"]},
            "data": {"prev_trading_day": str(prev_day.date()), "append_dates": upd.get("dates"), "append_run": upd.get("run"),
                     "stocks_loaded": pool.loaded, "excluded_today": excluded_codes(), "excluded_n": len(pool.excluded),
                     "expected_prev_trading_day": str(exp_prev.date()), "data_last_date": last_date,
                     "daily_up_to_date": bool(last_date == str(exp_prev.date()) == str(prev_day.date())),
                     "official_prev_day": (upd.get("official_received") or {}).get(str(prev_day.date())) or "못 받음",
                     "official_missing_dates": upd.get("official_missing_dates", []), "data_version": upd.get("data_version"),
                     "version_dir": os.path.basename(vdir) if os.path.basename(os.path.dirname(vdir)) == "versions" else None},
            "ledger_as_of": lj.get("as_of"), "ledger_version": lver, "ledger_path": ledger_path or LEDGER,
            "prep_seconds": {"model_load_check": round(t_model, 2), "daily_to_memory": round(time.time() - t1, 1)},
            "pool": {"workers": getattr(pool, "n", None), "openblas_threads": os.environ.get("OPENBLAS_NUM_THREADS")}}
    return pool, b, cols, index, ledgers, info


def shadow():
    import state as S
    day = now().normalize()
    log = {"date": str(day.date()), "mode": "shadow (주문 없음)", "quotes_env": "prod", "git_commit": git_commit(), "errors": []}
    os.makedirs(SNAP_DIR, exist_ok=True)
    global SUFFIX, SUMMARY
    # Codex 19: 그림자 실행 산출물은 상주 프로그램 실행 여부와 무관하게 항상 _shadow 경로 (그날 로그·스냅샷·점수·summary·장부)
    SUFFIX, SUMMARY = "_shadow", os.path.join(LOG_DIR, "summary_shadow.csv")
    path = os.path.join(LOG_DIR, f"{day.date()}{SUFFIX}.json")
    if not S.is_trading_day(day):
        print(f"{day.date()} 휴장일 (주말·krx_holidays.json) -> 아무것도 하지 않음", flush=True)
        return
    ledger_path = ledger_target(running=True)  # 항상 ledger_shadow.json (없으면 본 장부를 복사해 시작), 읽기·쓰기 같은 경로
    pool, b, cols, index, ledgers, info = prepare(day, ledger_path)
    log["ledger_written_to"] = ledger_path
    log.update(info)
    log["ledger_before"] = ledgers
    if not info["model"]["check"]["ok"]:
        log["errors"].append("모델 로드 확인 실패 -> 신규 매수 안 함 (그림자 계산은 계속)")
    if not info["data"]["daily_up_to_date"]:
        log["errors"].append("전날 일봉 미반영 -> 신규 매수 안 함 (그림자 계산은 계속)")
    jdump(path, log)
    print(f"prepared {now():%H:%M:%S}: {json.dumps(info, ensure_ascii=False, default=str)[:600]}", flush=True)
    caller = Caller("prod", 5)
    wait_until(SNAP_AT)
    t0 = time.time()
    t_start = now()
    codes = sd.universe_codes()
    raw, snap, srec = Q.snapshot(caller, codes)
    idx_vals, idx_raw = Q.index_now(caller)
    t_snap = time.time() - t0
    ok, vinfo = S.validate_snapshot(snap, day)
    if not ok:
        log["errors"].append(f"스냅샷 확인 실패 -> 그날 계산 안 함: {vinfo}")
        jdump(path, log)
        pool.close()
        print(f"snapshot check failed: {vinfo}", flush=True)
        return
    rows = snap_rows(snap, raw, day)
    res = E.run_day(pool, day, rows, index, idx_vals, b, cols, ledgers)
    fok = res["filter"]["filter_ok"]
    order_ledger = "strategy" if fok else "full"
    plan = E.order_plan(res["ledgers"][order_ledger]["top3"], price_col="raw_close")
    t_done = now()
    snap_file = os.path.join(SNAP_DIR, f"{day.date()}{SUFFIX}.parquet")
    os.makedirs(os.path.join(LOG_DIR, "scores"), exist_ok=True)
    scores_file = os.path.join(LOG_DIR, "scores", f"{day.date()}{SUFFIX}.parquet")
    res["eligible"][["code", "score"]].to_parquet(scores_file, index=False)
    pd.concat([raw.reset_index(drop=True), snap.add_prefix("p_").reset_index(drop=True)], axis=1).to_parquet(snap_file, index=False)
    log["live"] = {"scores_file": scores_file, "factor_default_n": res.get("factor_default_n"), "vscale_default_n": res.get("vscale_default_n"),
                   "snapshot": {**{k: v for k, v in srec.items() if k != "batches"}, "missing_n": len(srec["missing"]), "batches": srec["batches"],
                                "start": t_start.strftime("%H:%M:%S.%f")[:-3], "file": snap_file, "rows_used": len(rows)},
                   "index_values": idx_vals, "index_raw": idx_raw, "filter": res["filter"], "market": res["market"],
                   "eligible_n": len(res["eligible"]), "compute_errors": res["errors"],
                   "ledgers": {n: {"top3": top_list(res["ledgers"][n]["top3"], "price_1519"), "busy": res["ledgers"][n]["busy"],
                                   "skipped_busy": res["ledgers"][n]["skipped_busy"],
                                   "open_trades": sum(s["open"] for s in res["ledgers"][n]["ledger_status"])} for n in NAMES},
                   "plan_ledger": order_ledger, "plan_kind": "본래 규칙" if fok else "점검용 (필터 미통과, 3B)",
                   "plan": plan, "plan_capital": CAPITAL, "plan_note": "보내지 않음 (장부상 3,000만 원, 보유 없음)",
                   "timing": {"snapshot_s": round(t_snap, 2), "compute_s": res["seconds_total"], "start_to_plan_s": round((t_done - t_start).total_seconds(), 2),
                              "plan_ready": t_done.strftime("%H:%M:%S.%f")[:-3]}}
    log["ledger_status"] = {n: res["ledgers"][n]["ledger_status"] for n in NAMES}
    log["filter"] = res["filter"]
    jdump(path, log)
    print(f"live top3 { {n: [t['code'] for t in log['live']['ledgers'][n]['top3']] for n in NAMES} } filter_ok {fok} plan ready {log['live']['timing']}", flush=True)

    # 장 마감 후: 15:30 1분봉 기준 (같은 장부 상태)
    wait_until(CLOSE_AFTER)
    t1 = time.time()
    crow, miss = {}, []
    for code in [c for c in codes if c in rows]:
        try:
            cv = Q.close_values(Q.bars_today(caller, code, day))
        except ValueError:
            cv = None
        if cv is None:
            miss.append(code)
            continue
        crow[code] = ({k: cv[k] for k in ("open", "high", "low", "close", "volume", "amount")}, None, None)
    idx_close = Q.index_close_today(caller, day)
    cres = E.run_day(pool, day, crow, index, {"kospi": idx_close["kospi"], "kosdaq": idx_close["kosdaq"]}, b, cols, ledgers)
    pool.close()
    log["close_1530"] = {"eligible_n": len(cres["eligible"]), "bars_requests": len(crow) + len(miss), "bars_missing": miss,
                         "bars_seconds": round(time.time() - t1, 1), "index_close": idx_close, "ledgers": {}}
    for n in NAMES:
        l3 = [t["code"] for t in log["live"]["ledgers"][n]["top3"]]
        c3 = list(cres["ledgers"][n]["top3"].code)
        log["close_1530"]["ledgers"][n] = {"top3": top_list(cres["ledgers"][n]["top3"], "close_1530"), "overlap_with_1519": len(set(l3) & set(c3)),
                                           "skipped_busy": cres["ledgers"][n]["skipped_busy"]}
    log["rate"] = {"requests": caller.n, "hourly": dict(caller.hourly), "errors": caller.errors, "stopped": caller.stopped}
    for k in ("orders", "fills", "stops", "skipped_signals", "holdings_cash_check", "virtual_vs_actual", "next_day_official_top3"):
        log[k] = None
    jdump(path, log)
    # 다음 날 장부: 오늘까지 안 끝난 거래 + 오늘 실시간 상위 3 (전략 장부는 필터 통과일만)
    new = {}
    for n in NAMES:
        add = [t["code"] for t in log["live"]["ledgers"][n]["top3"]] if (n == "full" or fok) else []
        new[n] = [{"code": s["code"], "entry": s["entry"]} for s in res["ledgers"][n]["ledger_status"] if s["open"]] + \
                 [{"code": c, "entry": str(day.date())} for c in add]
    # 장부 저장: 읽은 경로에 같은 버전 확인으로. 본 장부는 상주 프로그램 단일 실행 잠금을 잡은 채로만 (2026-10-24 F-06·F-07)
    lk = SingleInstanceLock() if ledger_path == LEDGER else None
    try:
        if lk is not None and not lk.acquire():
            raise LedgerConflict("상주 프로그램이 실행 중 (단일 실행 잠금) -> 본 장부 저장 안 함")
        ledger_write({"as_of": str(day.date()), **new}, info["ledger_version"], ledger_path)
    except LedgerConflict as e:
        log["errors"].append(f"장부 저장 거부: {e}")
        jdump(path, log)
    finally:
        if lk is not None:
            lk.release()
    # 저장한 스냅샷으로 다시 계산 (따로 실행)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "rerun", str(day.date())] + ([SUFFIX] if SUFFIX else []), capture_output=True,
                       text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    log = json.load(open(path, encoding="utf-8"))
    if r.returncode != 0:
        log["errors"].append("rerun 실패: " + r.stderr[-500:])
        jdump(path, log)
    summary(log)
    print(json.dumps({n: {"live": [t["code"] for t in log["live"]["ledgers"][n]["top3"]], "rerun": log.get("rerun", {}).get(n, {}).get("top3"),
                          "close": [t["code"] for t in log["close_1530"]["ledgers"][n]["top3"]]} for n in NAMES}, ensure_ascii=False), flush=True)


def rerun(date, suffix="", out=""):
    """8장 12 (1): 장부를 새로 계산하지 않고 실행 엔진과 같은 장부 상태(ledger_before, 그날 기준 열린 거래는 열린 그대로)를 넣는다.
    out 을 주면 그날 로그는 고치지 않고 결과({"rerun", "errors_add"})만 out 파일에 쓴다 (상주 프로그램이 1분봉을 받는 동안 함께 돌릴 때, 11I)"""
    day = pd.Timestamp(date)
    import common as C
    C.apply_universe(day)  # 11K 1: 그날 쓴 대상 종목 목록으로
    path = os.path.join(LOG_DIR, f"{day.date()}{suffix}.json")
    log = json.load(open(path, encoding="utf-8"))
    from daily import index_series
    b, cols, meta, chk = M.load(day.strftime("%Y-%m"))
    full = pd.read_parquet(log["live"]["snapshot"]["file"])
    snap = pd.DataFrame({c[2:]: full[c] for c in full.columns if c.startswith("p_")})
    rows = snap_rows(snap, full, day)
    # 11S: 그날 15:18 에 쓴 데이터 버전 (고정 버전 이름, 없으면 data_version 이 같은 폴더).
    # 11S 보완 2: 그날 버전 기록이 있는데 폴더가 없으면 지금 버전으로 대신하지 않고 '대조 미완료' (상위 3 비교 안 함 = 불일치 차단 아님).
    # 버전 기록이 없는 예전 로그만 지금 버전
    want = log.get("data_dir_version") or log.get("data_version")
    vdir = C.version_dir(log.get("data_dir_version"), log.get("data_version"), APPEND_DIR)
    if want and not vdir:
        rr = {"incomplete": f"그날 데이터 버전 {want} 폴더 없음 -> 동결 대조 안 함 (지금 버전으로 대신하지 않음)", "run": now().strftime("%H:%M:%S"),
              "data_dir": None}
        if out:
            jdump(out, {"rerun": rr, "errors_add": []})
            return
        log["rerun"] = rr
        jdump(path, log)
        return
    saved_dd = C.data_dir
    if vdir:
        C.data_dir = lambda append_dir=None: vdir
    # 동결 load_stock·market_features 를 그대로 부른다 (임시 폴더 파일, 폴더·경로 상수만 바꿈). 실시간은 메모리 사본 (EXEC_SPEC 0장 예외)
    try:
        pool = E.ShardPool(sd.universe_codes(), use_append=True, frozen=True, **({"vdir": vdir} if vdir else {}))
        try:
            res = E.run_day(pool, day, rows, index_series(), log["live"]["index_values"], b, cols, log["ledger_before"], frozen_market=True)
        finally:
            pool.close()
    finally:
        C.data_dir = saved_dd
    # B7: 상위 3 뿐 아니라 대상 전 종목 점수 최대 차이, factor·vscale 기본값 대체 종목 수
    sf = log["live"].get("scores_file") or os.path.join(LOG_DIR, "scores", f"{day.date()}{suffix}.parquet")
    score_all = None
    if os.path.exists(sf):
        a = pd.read_parquet(sf).set_index("code").score
        b_ = res["eligible"].set_index("code").score
        common_ = a.index.intersection(b_.index)
        score_all = {"n_live": int(len(a)), "n_rerun": int(len(b_)), "n_common": int(len(common_)),
                     "only_live": sorted(set(a.index) - set(b_.index))[:20], "only_rerun": sorted(set(b_.index) - set(a.index))[:20],
                     "max_abs_diff": float((a[common_] - b_[common_]).abs().max()) if len(common_) else None}
    log["rerun"] = {"eligible_n": len(res["eligible"]), "model_check": chk["ok"], "run": now().strftime("%H:%M:%S"),
                    "score_all": score_all, "factor_default_n": res.get("factor_default_n"), "vscale_default_n": res.get("vscale_default_n"),
                    "method": "동결 stage3_data.load_stock·stage4_features.market_features 를 그대로 호출 (임시 파일), 장부 상태 = 실행 엔진과 같음",
                    "data_dir": os.path.basename(vdir) if vdir else "지금 버전 (그날 버전 기록 없는 예전 로그)"}
    errs = []
    for n in NAMES:
        top = res["ledgers"][n]["top3"]
        re3 = list(top.code)
        live = log["live"]["ledgers"][n]["top3"]
        lv = {t["code"]: t["score"] for t in live}
        log["rerun"][n] = {"top3": re3, "same_3of3": re3 == [t["code"] for t in live], "overlap": len(set(re3) & set(lv)),
                           "score_max_abs_diff": float(max(abs(float(s) - lv.get(c, np.nan)) for c, s in zip(top.code, top.score))) if re3 else None}
        if not log["rerun"][n]["same_3of3"]:
            errs.append(f"8장 12 (1) 코드 일치 실패 ({n} 장부): 원인을 찾을 때까지 신규 매수 중단")
    if out:
        jdump(out, {"rerun": log["rerun"], "errors_add": errs})
        return
    log["errors"] += errs
    jdump(path, log)


def summary(log):
    lv, cl, rr = log.get("live", {}), log.get("close_1530", {}), log.get("rerun", {})
    row = {"date": log["date"], "mode": log["mode"], "git_commit": log["git_commit"][:7], "model_month": log["model"]["month"],
           "model_ok": log["model"]["check"]["ok"], "filter_ok": log.get("filter", {}).get("filter_ok"),
           "prev_kospi_close": log.get("filter", {}).get("prev_kospi_close"), "prev_kospi_ma120": log.get("filter", {}).get("prev_kospi_ma120"),
           "excluded_n": log.get("data", {}).get("excluded_n"),
           "official_prev_day": log.get("data", {}).get("official_prev_day"),
           "daily_up_to_date": log.get("data", {}).get("daily_up_to_date"),
           # Codex 16: 실제 매수 단계의 결정과 상태 파일의 차단·오류 기준 (상주 프로그램이 buy_decision 을 남긴다)
           "new_buy_ok": (log.get("buy_decision") or {}).get("new_buy_ok") if log.get("buy_decision") else
                         bool(log["model"]["check"]["ok"] and log.get("data", {}).get("daily_up_to_date")
                              and all(rr.get(n, {}).get("same_3of3", True) is not False for n in NAMES)),
           "buy_skip_reason": (log.get("buy_decision") or {}).get("reason") if log.get("buy_decision") else
                              "; ".join(x for x in log.get("errors", []) if "신규 매수" in x or "매수 안 함" in x),
           "buy_block": ";".join((log.get("buy_decision") or {}).get("buy_block") or []),
           "account_fail_count": (log.get("account_watch") or {}).get("fail_count"),
           "account_fail_longest_min": (log.get("account_watch") or {}).get("longest_streak_min"),
           "measure_price_eq_pct": ((log.get("measure_1520") or {}).get("targets") or {}).get("price_eq_1519_close_pct"),
           "measure_vol_within_0p1_pct": ((log.get("measure_1520") or {}).get("targets") or {}).get("vol_within_0p1_pct"),
           "measure_n": ((log.get("measure_1520") or {}).get("targets") or {}).get("n"),
           "rerun_score_all_max_diff": ((rr.get("score_all") or {}).get("max_abs_diff")),
           "factor_default_n": lv.get("factor_default_n"), "vscale_default_n": lv.get("vscale_default_n"),
           "data_version": log.get("data_version") or log.get("data", {}).get("data_version"),
           "measure_top3_eq_strategy": (((log.get("measure_1520") or {}).get("top3_compare") or {}).get("strategy") or {}).get("eq_3of3"),
           "measure_top3_eq_full": (((log.get("measure_1520") or {}).get("top3_compare") or {}).get("full") or {}).get("eq_3of3"),
           "measure_top3_eq_ref_strategy": ((((log.get("measure_1520") or {}).get("top3_compare") or {}).get("ref_b") or {}).get("strategy") or {}).get("eq_3of3"),
           "measure_top3_eq_ref_full": ((((log.get("measure_1520") or {}).get("top3_compare") or {}).get("ref_b") or {}).get("full") or {}).get("eq_3of3")}
    for n in NAMES:
        row[f"{n}_top3_1519"] = " ".join(t["code"] for t in lv.get("ledgers", {}).get(n, {}).get("top3", []))
        row[f"{n}_top3_rerun"] = " ".join(rr.get(n, {}).get("top3", []) or [])
        row[f"{n}_rerun_same_3of3"] = rr.get(n, {}).get("same_3of3")
        row[f"{n}_top3_close_1530"] = " ".join(t["code"] for t in cl.get("ledgers", {}).get(n, {}).get("top3", []))
        row[f"{n}_overlap_1519_close"] = cl.get("ledgers", {}).get(n, {}).get("overlap_with_1519")
        row[f"{n}_open_trades"] = lv.get("ledgers", {}).get(n, {}).get("open_trades")
    row.update({"plan_ledger": lv.get("plan_ledger"), "eligible_1519": lv.get("eligible_n"), "eligible_close": cl.get("eligible_n"),
                "snapshot_s": lv.get("timing", {}).get("snapshot_s"), "start_to_plan_s": lv.get("timing", {}).get("start_to_plan_s"),
                "plan_ready": lv.get("timing", {}).get("plan_ready"),
                "orders": (log.get("order_counts") or {}).get("orders", ""), "fills": (log.get("order_counts") or {}).get("fills", ""),
                "holdings_diff": len((log.get("holdings_cash_check") or {}).get("diff") or {}) if log.get("holdings_cash_check") else "",
                "cash_diff": (log.get("cash_check") or {}).get("cash_diff", ""),            # Codex 20
                "cash_diff_ok": (log.get("cash_check") or {}).get("ok", ""),
                "errors": len(log.get("errors", []))})
    df = pd.DataFrame([row])
    if os.path.exists(SUMMARY):
        old = pd.read_csv(SUMMARY, dtype=str)
        old = old[old.date != row["date"]]
        df = pd.concat([old, df.astype(str)], ignore_index=True)
    df.to_csv(SUMMARY, index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    {"shadow": shadow, "rerun": rerun}[sys.argv[1]](*sys.argv[2:])
