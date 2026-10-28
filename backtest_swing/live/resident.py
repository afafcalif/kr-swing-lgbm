"""
상주 프로그램 (EXEC_SPEC 7장·8장 10, 2026-10-22 결정; 2026-10-24 결정 (코드 리뷰 A·F·B, Codex 리뷰 11E) 반영). 등록·실행은 사용자 확인 뒤 서브 PC 에서.

  python backtest_swing/live/resident.py        # 상주 (config: live/config.json, 없으면 config.example.json)

- 하루 시작은 두 부분 (Codex 4):
  (1) 핵심(core): 토큰 재발급·주문 모듈·의도 파일·보유 장부. 실패하면 60초 뒤 다시. 손절·매도·정산은 이것만 있으면 된다.
  (2) 준비(prep): 일봉 반영(daily.update)·모델 로드·일봉 메모리 적재·동결 함수 확인·대상 후보. 별도 스레드, 실패하면 60초 뒤 다시.
      준비 실패·늦은 시작(15:18:30 이후 처음 시작)은 그날 신규 매수만 막는다.
- 하루 일정: 08:30 ~ 08:59 추세 매도(전날 판정 + 다음 날 시가 매도 목록), 09:00 ~ 15:20 1분마다 시세 조회로 손절, 15:17 데이터 버전 확인,
  15:18:30 스냅샷·점수·두 장부 상위 3, 15:21 ~ 15:28 전 매수, 15:35 체결·미체결 확인(랜덤엔드), 15:40 정산(잔고·예수금) -> 보유·손절 -> 장부 -> 요약.
- 거래일만 (주말·krx_holidays.json 휴장일이면 아무것도 안 하고 기록만).
- 단계 상태 PENDING / RUNNING / SUCCEEDED / FAILED / UNKNOWN (state.py). 주문 단계가 예외로 끝나면 UNKNOWN -> 주문 의도 대조 뒤에만 다시 실행.
- 주문 의도 선기록 (orders.IntentStore, state_dir/intents/{날짜}.json). 의도 파일이 손상된 날은 차단 파일이 남아 재시작해도 그날 주문을 계속 막는다 (Codex 1).
- 보유 장부 positions.json (positions.py): 백업·복구, 거래 번호, 다음 날 시가 매도 목록 (Codex 5·6·7·8·12·25).
- 1분마다 하트비트 (heartbeat_file). 상태 파일(state_dir/{날짜}.json)로 재시작하면 그날 하던 데서 이어감.
- 텔레그램: 시작·재시작(코드 커밋 + 커밋 안 된 변경 여부), 스냅샷 실패, 주문 거부·응답 없음·확인 안 됨, 임시 손절가, 현금 차이, 장 마감 후 요약 한 줄.
- 주문 TR 은 config 의 orders_enabled = true 이고 (env = mock 또는 allow_prod_orders = true) 일 때만 보낸다.
- 장부(logs/exec/ledger.json)는 상주 프로그램만 쓴다 (OS 수준 단일 실행 잠금 logs/exec/resident.lock, 저장 때 버전 확인).
- 시간은 모두 한국 시간(Asia/Seoul) 기준, 시스템 시간대가 다르면 매매 안 함.
"""
import atexit
import json
import os
import subprocess
import sys
import threading
import time
import traceback

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402

CFG = settings.load()
from common import (Caller, LEDGER_PATH, LOG_DIR, RESIDENT_INFO, RESIDENT_LOCK, SNAP_DIR, LedgerConflict, SingleInstanceLock,  # noqa: E402
                    atomic_json, git_commit, git_state, ledger_read, ledger_write, now_kst, pid_alive, process_cmdline, sd, s4,
                    system_tz_ok)
import common as C  # noqa: E402
import engine as E  # noqa: E402
import model as M  # noqa: E402
import notify as N  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import quotes as Q  # noqa: E402
import state as S  # noqa: E402

T = CFG["times"]
T.setdefault("close_calc", "15:32:00")           # 장 마감 후 계산 시작·상한도 거래시간 변경일에 함께 밀리게 (11K 2)
T.setdefault("close_calc_until", "16:20:00")
BASE_T = dict(T)                                 # 평소 시각표. 그날 시각표 T 는 apply_hours 가 krx_special_hours.json 으로 다시 만든다
HOURS = S.day_hours("2000-01-03", {})            # 그날 개장·폐장 (평소 09:00·15:30)
NAMES = ("strategy", "full")
LEDGER = LEDGER_PATH
POSITIONS = os.path.join(CFG["state_dir"], "positions.json")
ALERTS = os.path.join(LOG_DIR, "alerts.log")
ALERTS_PENDING = os.path.join(LOG_DIR, "alerts_pending.json")  # 11J 보완 6: 못 보낸 알림 (다음 시작 때 "지난 미전송 알림"으로)
BLOCK = os.path.join(CFG["state_dir"], "block_new_buys.json")
APPROVED = os.path.join(CFG["state_dir"], "excluded_approved.json")  # Codex 27: 사전 승인 제외 목록 (수정주가 계수 변경·공식 시세 없음)
QUOTE_TIMEOUT = 10        # 11I 보완: 시세 조회 HTTP 요청 하나의 시간 제한 (초)
STOP_BUDGET = 10          # 11J: 손절 감시 한 번(시세·계좌 조회·매도 전송)이 기다리는 총 시간 상한 (초). 넘으면 다음 분에 이어서
STOP_HTTP_TIMEOUT = 5     # 11J 보완 3: 손절 감시 안 HTTP 요청 하나의 시간 제한 (계좌 조회·시세 조회·매도 전송·토큰 발급, 초)
# 11J 보완 3 R3: 토큰 갱신 확인 (이름, 시각, 이 시각까지 + 30분 쓸 수 있어야 함). 평소에는 만료 30분 전에 미리 갱신 (kiwoom.auth)
MONTHLY_DEADLINE = "15:00:00"
TREND_WAIT_UNTIL = "08:50:00"  # 11K 보완 2 (3): 08:30 추세 매도가 공식 일봉 재판단을 기다리는 한도 (그날 시각표로 밂)  # 11K 보완 1: 첫 거래일 따라잡기 학습 마감 (그날 시각표로 밂)
TOKEN_CHECKS = (("0820", "08:20:00", "15:40:00"), ("1510", "15:10:00", "15:45:00"))
RECOVERY_LOOKBACK = 5  # 2차 확인 리뷰 3: 아침 복구는 최근 5거래일의 UNKNOWN 의도·미확정 보유를 항상 찾는다
CAND_AMT = 4_000_000_000
COMMISSION_BY_ENV = {"mock": 0.0035, "prod": 0.00015}  # 11K 11: 매매수수료율 기본값 (config 의 commission 이 있으면 그 값). 키움 모의투자 0.35%, 실전 0.015%
PROD_COMMISSION = COMMISSION_BY_ENV["prod"]  # Codex 27: 대상 후보 = 전날까지 20일 평균 거래대금 40억 원 이상 (exec_prep_probe 3번과 같은 기준)


class CloseCalcStop(Exception):
    """장 마감 후 계산 중단 (날짜 바뀜·16:30 정리로 취소, 또는 상한 시각 초과)"""


def now():
    """한국 시간 (모든 시간 비교는 Asia/Seoul 기준)"""
    return now_kst()


def jload(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def jdump(p, o):
    atomic_json(p, o)


WARN_WORDS = ("실패", "거부", "불일치", "차단", "응답 없음", "확인 안 됨", "손상", "미확정", "끊김", "차이", "중단", "안 함", "임시 손절가", "REJECTED", "UNKNOWN")


def level_of(msg):
    if "[오류]" in msg or "오류" in msg[:20]:
        return "ERROR"
    return "WARN" if any(w in msg for w in WARN_WORDS) else "INFO"


def rlog(msg, level=None):
    """사람이 읽는 상주 로그 (logs/exec/resident/{날짜}.log, view_log 창이 실시간으로 보여 줌). 한 줄 = 시각 수준 내용.
    수준: INFO / WARN / ERROR. 기록 실패는 무시 (매매를 막지 않음)"""
    try:
        t = now_kst()
        d = os.path.join(LOG_DIR, "resident")
        os.makedirs(d, exist_ok=True)
        line = " ".join(str(msg).split("\n"))
        with open(os.path.join(d, f"{t:%Y-%m-%d}.log"), "a", encoding="utf-8") as f:
            f.write(f"{t:%Y-%m-%d %H:%M:%S} {(level or level_of(line)):5s} {line}\n")
    except Exception:  # noqa: BLE001
        pass


def stop_flag():
    """정상 종료 요청 파일 (stop_resident 가 만든다): 있으면 상주 프로그램은 다음 반복에서 끝나고, 감시 작업은 다시 켜지 않는다"""
    return os.path.join(LOG_DIR, "resident.stop")


def alert(msg):
    """11J: 텔레그램은 큐에 넣고 별도 스레드가 보낸다 (주 반복 대기 0)"""
    rlog(f"알림: {msg}")
    N.send_async(msg, log_path=ALERTS, pending_path=ALERTS_PENDING)


def save_positions(pos):
    """positions.json 저장 (직전 파일은 .bak, 저장 시각·버전 기록)"""
    P.save(POSITIONS, pos)


def upper_limits(raw, codes, px=None):
    """11J 보완 7: 주문 계획 종목의 그날 상한가. 스냅샷(ka10095) upl_pric 가 없거나 비정상(0·결측, 기준가 x 1.25 ~ 1.35 밖, 15:19 가격보다 낮음)이면
    기준가(base_pric, 전일 종가) x 1.30 을 호가 단위로 내림. 기준가도 없으면 None (그 종목은 매수 안 함, 데이터 이상으로 집계).
    반환 ({code: 상한가 또는 None}, {"fallback": {code: 값}, "none": [code], "bad_upper": {code: 원래 값}})"""
    from common import floor_tick
    px = px or {}
    rows = {}
    if raw is not None and "stk_cd" in raw:
        for _, r in raw.iterrows():
            rows[O.code_of(r["stk_cd"])] = r
    out, info = {}, {"fallback": {}, "none": [], "bad_upper": {}}
    for c in sorted(codes):
        r = rows.get(c)
        up = O.num(r.get("upl_pric")) if r is not None and "upl_pric" in r else float("nan")
        base = O.num(r.get("base_pric")) if r is not None and "base_pric" in r else float("nan")
        p = px.get(c)
        ok = np.isfinite(up) and up > 0 and (not (np.isfinite(base) and base > 0) or 1.25 * base <= up <= 1.35 * base) and not (p and p > up)
        if ok:
            out[c] = float(up)
            continue
        info["bad_upper"][c] = None if not np.isfinite(up) else float(up)
        if np.isfinite(base) and base > 0:
            out[c] = float(floor_tick(base * 1.30))
            info["fallback"][c] = out[c]
        else:
            out[c] = None
            info["none"].append(c)
    return out, info


def fresh_prices(snap, day):
    """11K 4: 시세 조회(ka10095) 응답에서 오늘 체결된 현재가만. 시세 날짜(dt)가 오늘이 아니거나 체결 시각(cntr_tm)이 없음·000000(오늘 체결 없음,
    거래정지 포함)이면 뺀다. 반환 ({code: 현재가}, {code: 뺀 이유}). 날짜·시각 칸이 없는 응답(시험용 가짜)은 확인하지 않음"""
    out, stale = {}, {}
    if snap is None or len(snap) == 0:
        return out, stale
    ds = pd.Timestamp(day).strftime("%Y%m%d")
    for r in snap.itertuples():
        dt, tm = str(getattr(r, "dt", ds) or ""), str(getattr(r, "cntr_tm", "x") or "")
        if dt[:8] != ds:
            stale[r.code] = f"시세 날짜 {dt or '없음'} != 오늘"
        elif tm.strip("0") == "" or tm in ("nan", "None"):
            stale[r.code] = f"오늘 체결 없음 (체결 시각 {tm or '없음'})"
        elif r.close == r.close and r.close > 0:
            out[r.code] = float(r.close)
    return out, stale


def commission_rate():
    """11K 11: 이 환경(env)의 매매수수료율"""
    c = CFG.get("commission") or {}
    return float(c.get(CFG["env"], COMMISSION_BY_ENV.get(CFG["env"], PROD_COMMISSION)))


def hhmmss(x):
    """시각 문자열(15:30:12, 153012, 20261026153012, 15:30:12.123)을 HHMMSS 로. 모르면 000000"""
    dg = "".join(ch for ch in str(x or "") if ch.isdigit())
    if len(dg) >= 14:
        dg = dg[8:14]
    return (dg + "000000")[:6] if dg else "000000"


def sb_tax(year):
    from common import sb
    return sb.SELL_TAX.get(int(year), 0.002)


def pnl_text(p):
    """요약 알림: 손익과 실전 수수료였다면 손익 (11K 11)"""
    if not p or p.get("pnl") is None:
        return ""
    return f"손익 {p['pnl']:+,.0f} (실전 수수료였다면 {p['pnl_prod_fee']:+,.0f}), "


def perf_text(p):
    if not p or p.get("strategy_equity") is None:
        return ""
    return f"전략 {p['return_pct']:+.2f}% ({p['since']} 부터, 점검용 분리), "


def filter_valid(f):
    """11K 보완 2: 시장 필터 기록이 유효한 지수로 계산됐는가 (filter_valid 가 없는 예전 기록은 값을 다시 확인). NaN 은 통과로 보지 않음"""
    if not f:
        return False
    if "filter_valid" in f and not f["filter_valid"]:
        return False
    return E.filter_invalid(f.get("prev_kospi_close"), f.get("prev_kospi_ma120")) is None


def check_mode_path():
    """11K 8: 실행 점검 모드 종료 기록 (state_dir/check_mode.json). 있으면(ended) 필터 미통과일 매수 없음"""
    return os.path.join(CFG["state_dir"], "check_mode.json")


def apply_hours(day):
    """11K 2: 그날 개장·폐장(krx_special_hours.json)에 맞춰 시각표 T 를 제자리에서 바꾼다 (평소 날은 평소 시각표로 되돌림).
    OrderManager 는 만들 때 T 를 복사하므로 그날 주문 모듈을 만들기 전에 부른다 (start_core)"""
    h = S.day_hours(day)
    T.clear()
    T.update(S.shift_times(BASE_T, h))
    HOURS.clear()
    HOURS.update(h)
    return h


def at(hms):
    """코드 안에 적힌 시각(토큰 확인 등)을 그날 시각표로"""
    return S.shift_hms(hms, HOURS)


def new_day(t, clock=None):
    return S.Day(os.path.join(CFG["state_dir"], f"{pd.Timestamp(t).date()}.json"), t, **({"clock": clock} if clock else {}))


class Resident:
    def __init__(self):
        os.makedirs(CFG["state_dir"], exist_ok=True)
        self.clock = now
        self.sleep = time.sleep
        self.pool = None
        self.day = None
        self.om = None
        self.broker = None
        self.last_hb = None
        self.last_recovery_minute = None
        self.last_trend_minute = None
        self.last_buy_minute = None
        self._step_err = {}
        self.last_stop_minute = None       # Codex 11: 손절 감시는 하트비트가 아니라 마지막으로 돈 분으로
        self.ctx = {}
        self.redo_thread = None
        self.redo_result = None
        self.prep_thread, self.prep_result, self.prep_ok, self.next_prep_try, self.prep_reason = None, None, False, None, "하루 시작"
        self.holidays = S.holidays()
        self.started, self.next_start_try, self.start_reason = False, None, "하루 시작"
        self.tz_block = False
        self.instance_lock = None
        self.watch, self.watch_day, self.mismatch_seen = O.AccountWatch(notify=alert, n_alert=3), None, set()
        # 11I (모의 1일차): 하트비트는 별도 스레드가 1분마다 (cur_step = 주 반복이 마지막으로 끝낸 단계, last_tick = 그 시각)
        self.cur_step, self.last_tick, self.hb_lock, self.hb_stop = "start", time.time(), threading.Lock(), threading.Event()
        # 11I: 장 마감 후 무거운 계산(15:30 1분봉 전 종목·15:30 상위 3·15:20 측정·동결 코드 대조)은 별도 스레드 (close_calc)
        # 작업 = {gen 세대, date 작업 날짜, cancel 취소 신호, deadline 상한, thread, result, day·ctx·pool (시작 때 잡은 것)} (11I 보완)
        self.close_job, self.close_gen, self.close_stale, self.close_next_try = None, 0, [], None
        self.disposing = []  # 11I 보완: 버리는 계산 프로세스 묶음을 정리하는 스레드 (주 반복은 기다리지 않음)
        self._qc = None
        self.hours_day = None
        self.monthly_proc = None  # 11K 1: 월별 모델 프로세스 (monthly.py run)
        self.judge_result = None  # 11K 보완 2 (2): 준비 스레드가 먼저 공개하는 공식 일봉 재판단·전날 복구 값 {day, recovery, official_judge}
        self.monthly_next_check = None  # 11K 보완 1: 다음 확인 시각 (모델 파일·프로세스 확인은 30초마다)
        self.model_bad = set()          # 하루 시작 모델 검사에 실패한 월 (따라잡기 대상)
        self.auto_monthly = False       # 실제 상주 실행(run)에서만 켬 (시험이 실제 학습 프로세스를 띄우지 않게)

    def now(self):
        return self.clock()

    # ------------------------------------------------------------ 사람이 읽는 로그

    def on_day(self, kind, step, obj):
        """하루 상태 변화 -> 상주 로그 (단계 시작·끝·기록, 오류). 같은 단계의 같은 실패가 되풀이되면 10번에 한 번만"""
        if kind == "err":
            rlog(f"오류 기록: {obj}"[:500], "ERROR")
            return
        st, att = obj.get("status"), obj.get("attempts", 0)
        if kind == "begin":
            if step in self._step_err and att % 10:  # 같은 실패로 다시 도는 중이면 시작 줄도 10번에 한 번
                return
            rlog(f"단계 시작: {step}" + (f" ({att}번째)" if att > 1 else ""), "INFO")
            return
        info = {k: v for k, v in obj.items() if k not in ("status", "time", "started", "attempts", "legacy")}
        brief = json.dumps(info, ensure_ascii=False, default=str)
        if st == S.SUCCEEDED:
            rlog(f"단계 {'끝' if kind == 'finish' else '기록'}: {step} 성공 {brief[:200] if brief != '{}' else ''}".rstrip(), "INFO")
            self._step_err.pop(step, None)
            return
        err = str(obj.get("error", ""))[:200]
        if self._step_err.get(step) == err and att % 10:
            return
        self._step_err[step] = err
        rlog(f"단계 {step} {st} ({att}번째): {err or brief[:200]}", "WARN")

    def on_intent(self, kind, it, kw):
        """주문 의도 변화 -> 상주 로그 (전송·접수·거부·응답 없음·체결·취소)"""
        what = f"{it.get('stage')} {it.get('side')} {it.get('code')} {it.get('qty')}주" + (f" @{it.get('price')}" if it.get("price") else "")
        if kind == "add":
            rlog(f"주문 전송: {what} (시도 {it.get('attempt')}, {it.get('reason', '')})", "INFO")
            return
        if "status" in kw:
            st = kw["status"]
            more = kw.get("return_msg") or kw.get("cause") or kw.get("matched_by") or ""
            rlog(f"주문 {st}: {what} 주문번호 {it.get('ord_no') or '-'} {more}".rstrip(), "INFO" if st == O.ACCEPTED else "WARN")
        if kw.get("filled_qty"):
            rlog(f"체결: {what} 체결 {kw['filled_qty']}주 @{kw.get('fill_price')} {kw.get('fill_status') or ''}".rstrip(), "INFO")
        elif kw.get("fill_status"):
            rlog(f"체결 확인: {what} {kw['fill_status']}", "WARN" if "미체결" in str(kw["fill_status"]) else "INFO")

    # ------------------------------------------------------------ 공통

    def heartbeat(self, step):
        """1분에 한 줄: 시각 pid 단계 loop=주 반복이 마지막으로 돈 뒤 지난 초 (감시 작업이 주 반복 멈춤을 따로 알 수 있게, 11I)"""
        with self.hb_lock:
            m = self.now().strftime("%Y-%m-%d %H:%M")
            if m == self.last_hb:
                return
            self.last_hb = m
            with open(CFG["heartbeat_file"], "a", encoding="utf-8") as f:
                f.write(f"{self.now():%Y-%m-%d %H:%M:%S} pid={os.getpid()} step={step} loop={time.time() - self.last_tick:.0f}s\n")

    def start_heartbeat(self, every=5.0):
        """11I: 하트비트 스레드. 무거운 단계와 상관없이 1분마다 찍힌다 (몇 초마다 깨어 분이 바뀌었으면 한 줄)"""
        def loop():
            while not self.hb_stop.is_set():
                try:
                    self.heartbeat(self.cur_step)
                except Exception:  # noqa: BLE001  (기록 실패가 매매를 막지 않게)
                    pass
                self.hb_stop.wait(every)
        t = threading.Thread(target=loop, daemon=True, name="heartbeat")
        t.start()
        return t

    def quote_caller(self):
        """시세 조회(실전 토큰, 1초 5회)는 하나를 같이 쓴다: 장 마감 후 계산 스레드와 주 반복이 동시에 불러도 합쳐서 1초 5회 (EXEC_SPEC 1A)"""
        if self._qc is None:
            self._qc = Caller("prod", 5)
            if getattr(self._qc, "c", None) is not None:
                self._qc.c.timeout = QUOTE_TIMEOUT  # 11I 보완: HTTP 요청마다 시간 제한 (1분봉 조회가 멈춰 계산이 끝나지 않는 일 없게)
        return self._qc

    def new_broker(self):
        """토큰 재발급: 그 서버의 토큰 캐시 파일을 지우고 새 클라이언트"""
        from kiwoom.client import KiwoomRestClient, RateLimiter
        from kiwoom.config import Settings
        env = CFG["env"]
        st = Settings.from_env(env)
        cache = getattr(st, "token_cache_path", None)
        if cache and os.path.exists(cache):
            os.remove(cache)
        c = KiwoomRestClient(st)
        c.rate_limiter = RateLimiter(limits_per_sec=1 if env == "mock" else 5)
        allow = bool(CFG["orders_enabled"]) and (env == "mock" or bool(CFG["allow_prod_orders"]))
        return O.Broker(c, allow_orders=allow, env=env)  # 11L: env = 공용 호출 제한기의 토큰 키

    def new_om(self, d):
        store = O.IntentStore(os.path.join(CFG["state_dir"], "intents", f"{d.date.date()}.json"), clock=self.now)
        return O.OrderManager(self.broker, clock=self.clock, sleep=self.sleep, notify=alert, times=T, store=store)

    def block_buys(self, tag, msg, notice):
        d = self.day
        self.ctx["buy_allowed"] = False
        with d.lock:
            if tag not in (d.s.get("buy_block") or []):
                d.s.setdefault("buy_block", []).append(tag)
            d.s["errors"].append(msg[:800])
            d.save()
        alert(notice[:300])

    # ------------------------------------------------------------ 보유 장부 (Codex 8)

    def positions(self):
        """positions.json 읽기. 손상이면 백업으로 복구(positions.load) + 그날 복구 확인 전까지 신규 매수 중단 (손절은 계좌 + 백업 손절가로 계속)"""
        pos, info = P.load(POSITIONS)
        if info["status"] in ("recovered_backup", "corrupt_unrecovered") and self.day is not None:
            rec = {**info, "since": self.now().strftime("%H:%M:%S"), "recovered_at": None}
            self.day.set("positions_recovery", rec)
            self.day.err(f"보유 장부 손상 ({info['status']}) -> 백업·의도·계좌로 복구, 복구 확인 전까지 신규 매수 안 함: {info.get('error')}")
            alert(f"[보유 장부 손상] {info['status']} — 백업·의도·계좌로 복구할 때까지 신규 매수 중단 (손절은 계속)")
        return pos

    def recovery_pending(self):
        r = (self.day.s.get("positions_recovery") if self.day else None) or {}
        return bool(r) and not r.get("recovered_at")

    def fill_missing(self, pos, held, day_str, buy_intents, why, drop=True):
        """계좌 보유(held) 기준으로 보유 장부를 맞춘다 (Codex 5·6, 확인 리뷰 N1·N2): 수량·평균단가는 계좌.
        진입일이 없거나 15:40 거래 정보 미확정으로 남은 잠정 거래(provisional_new)면 (1) 매수 의도(ACCEPTED)가 있으면 가장 최근 의도의 날짜에 거래,
        (2) 없으면 진입일 미확인(entry_temp: 사람이 확인할 때까지 신규 매수 안 함). 손절가가 없으면 임시 손절가. 수량 미확정 표시(info_pending)는 지운다
        (계좌 수량이 기준). buy_intents 는 날짜 오름차순"""
        temp, unknown = [], []
        for c, h in held.items():
            if h["qty"] <= 0:
                continue
            p = pos["held"].get(c)
            if p and (p.get("ambiguous") or P.in_review(p)):  # 3차 확인 리뷰·11K 보완 3: 미확정·수동 확인은 수량·평균단가만 계좌로 (손절가·표시 그대로)
                p["qty"], p["avg_price"] = int(h["qty"]), h.get("avg_price")
                continue
            if not p or not p.get("entry_date") or p.get("provisional_new"):
                b = [i for i in buy_intents if i.get("code") == c and i.get("side") == "buy" and i.get("status") == O.ACCEPTED]
                keep = dict(p or {})
                if b:
                    ed = str(b[-1].get("date") or day_str)
                    if keep.get("provisional_new") and keep.get("entry_date") == ed and keep.get("trade_id"):
                        p = keep
                        p.pop("provisional_new", None)
                        p["kind"] = b[-1].get("kind", p.get("kind", "본래"))
                        pos["held"][c] = p
                    else:
                        p = P.new_trade(pos, c, ed, kind=b[-1].get("kind", "본래"))
                else:
                    p = P.new_trade(pos, c, day_str, kind="미확인", entry_temp=True)
                    unknown.append(c)
                for k in ("stop_price", "stop_temp", "stop_basis"):
                    if keep.get(k) is not None and p is not keep:
                        p[k] = keep[k]
            p.pop("info_pending", None)
            p["qty"], p["avg_price"] = int(h["qty"]), h.get("avg_price")
            if not p.get("stop_price"):
                s, basis = P.temp_stop(c, h.get("avg_price"), day_str)
                p.update(stop_price=float(s), stop_temp=True, stop_basis=basis)
                temp.append(c)
        for c in [c for c in pos["held"] if drop and (c not in held or held[c]["qty"] <= 0)]:
            pos["held"].pop(c)
            pos["next_open_sells"].pop(c, None)
        if temp or unknown:
            alert(f"[임시 손절가] {why}: 임시 손절가 {temp}, 진입일 미확인 {unknown} (계좌 평균단가 - 1.7 x ATR20)"
                  + (" — 진입일 미확인 종목은 positions.json 에 진입일을 확인해 넣을 때까지 신규 매수 안 함" if unknown else ""))
        return temp, unknown

    def recover_positions(self):
        """보유 장부 손상 뒤 복구 (Codex 8): 계좌 + 백업 + 오늘·전날 의도로 맞추고 저장하면 복구 완료 (신규 매수 차단 해제)"""
        d = self.day
        held = self.broker.account()["holdings"]
        pos = self.positions()
        prev = S.prev_trading_day(d.date, self.holidays)
        pstore = O.IntentStore(os.path.join(CFG["state_dir"], "intents", f"{prev.date()}.json"), clock=self.now)
        today_buys = [i for i in self.om.records if i.get("side") == "buy"]
        temp, unknown = self.fill_missing(pos, {c: h for c, h in held.items() if any(i["code"] == c for i in today_buys)},
                                          str(d.date.date()), today_buys, "보유 장부 복구 (오늘 매수)", drop=False)
        t2, u2 = self.fill_missing(pos, held, str(prev.date()), pstore.items, "보유 장부 복구")
        P.mark_synced(pos, d.date.date(), self.now().strftime("%H:%M:%S"), "보유 장부 복구 (계좌 수량)")
        save_positions(pos)
        with d.lock:
            d.s["positions_recovery"].update(recovered_at=self.now().strftime("%H:%M:%S"), held=len(pos["held"]), temp_stops=temp + t2,
                                             unknown=unknown + u2)
            d.save()
        alert(f"[보유 장부 복구 완료] 계좌 기준 {len(pos['held'])}종목, 임시 손절가 {temp + t2}")

    # ------------------------------------------------------------ 하루 시작 (Codex 4: 핵심 / 준비)

    def start_core(self, d, reason="하루 시작"):
        """핵심: 토큰·주문 모듈·의도 파일. 손절·매도·정산에 필요한 것만"""
        self.day = d
        d.listener = self.on_day
        h = apply_hours(d.date)  # 11K 2: 거래시간 변경일이면 그날 시각표 (주문 모듈을 만들기 전에)
        self.hours_day = d.date
        uni = C.apply_universe(d.date)  # 11K 1: 그달 대상 종목 목록 (월별 갱신 포인터, 준비 스레드보다 먼저)
        if uni != C.UNIVERSE_DEFAULT:
            d.set("universe", uni)
        if h["special"]:
            d.set("special_hours", {**h, "times": dict(T)})
            if not d.s.get("special_hours_alerted"):
                d.set("special_hours_alerted", self.now().strftime("%H:%M:%S"))
                alert(f"[거래시간 변경일] {d.date.date()} {h['reason']}: 개장 {h['open']} 폐장 {h['close']} -> 모든 시각을 함께 밂 "
                      f"(손절 감시 {T['stop_from']} ~ {T['stop_until']}, 스냅샷 {T['snapshot']}, 매수 {T['buy_from']} ~ {T['no_new_order_after']} 전, "
                      f"정산 {T['settle']}, 하루 끝 {T['day_end']})" + ("" if h.get("confirmed") else " — KRX 공지로 확인 필요 (krx_special_hours.json)"))
        rlog(f"하루 시작(핵심): {d.date.date()} ({reason})", "INFO")
        self.broker = self.new_broker()
        self.om = self.new_om(d)
        self.om.store.listener = self.on_intent
        if self.om.store.corrupt:
            d.err(f"의도 파일 손상 -> 오늘 매수 막음, 매도 의무는 계좌 매도 가능 수량 안에서 계속 (11K 6): {self.om.store.corrupt}")
            alert(f"[주문 차단] {self.om.store.corrupt} — 오늘 매수 막음, 매도 의무(손절·추세 매도)는 계좌 매도 가능 수량 안에서 계속")
        if self.watch_day != d.date:
            self.watch = O.AccountWatch(notify=alert, n_alert=3)
            prev = d.s.get("account_watch", {})
            self.watch.fail_count, self.watch.longest_min = prev.get("fail_count", 0), prev.get("longest_streak_min", 0.0)
            self.watch.log = prev.get("log", [])
            self.mismatch_seen = set()
            self.watch_day = d.date
        pos0 = self.positions()  # 손상 확인 (복구는 tick 에서)
        if pos0.get("duty_converted"):
            pos0 = self.converted_duties(pos0)
        rv = P.review_codes(pos0)
        if rv and not d.s.get("qty_review_reminded"):  # 11K 보완 3: 수동 확인 중인 종목은 하루 한 번 다시 알림
            d.set("qty_review_reminded", self.now().strftime("%H:%M:%S"))
            alert(f"[수동 확인 남음] {sorted(rv)}: 체결로 설명 안 되는 보유 수량 변화 — 자동 손절·추세 매도·신규 매수 멈춤 중. "
                  f"{ {c: pos0['held'][c]['qty_review'] for c in sorted(rv)} }"[:400])
        # 2차 확인 리뷰 2·3차 확인 리뷰 R2: 그날 시작 보유 수량 (pre_qty 없는 예전 의도의 대조 기준). 그날 처음 한 번, 매수 시간창(15:21) 전에만 쓰고
        # 이미 있으면 덮어쓰지 않는다. 장 마감 후 처음 켜진 날 등은 쓰지 않고 이전 기준(정산 기준 장부, 전 거래일 정산 계좌)을 쓴다 (day_start_qty)
        if not d.s.get("positions_start"):
            hms = self.now().strftime("%H:%M:%S")
            if not d.s.get("core_starts") and hms < T["buy_from"]:
                d.set("positions_start", {"time": hms, "held": {c: int(x.get("qty", 0)) for c, x in pos0.get("held", {}).items() if int(x.get("qty", 0)) > 0}})
            elif not d.s.get("positions_start_skipped"):
                d.set("positions_start_skipped", {"time": hms, "reason": "그날 첫 시작이 아님" if d.s.get("core_starts") else f"매수 시간창({T['buy_from']}) 이후 처음 시작",
                                                  "basis": "정산 기준 장부(positions_baseline), 없으면 전 거래일 정산 계좌"})
        with d.lock:
            d.s.setdefault("core_starts", []).append({"time": self.now().strftime("%H:%M:%S"), "reason": reason, "orders_enabled": self.broker.allow_orders,
                                                      "env": CFG["env"], "code": git_state(), "intents_corrupt": self.om.store.corrupt})
            d.save()

    def token_check(self, tag, until):
        """11J 보완 3 R3: 주문·계좌 클라이언트와 시세 클라이언트의 토큰이 until(그날 시각) + 30분까지 쓸 수 있는지 확인, 아니면 지금 갱신.
        갱신 실패면 기존 토큰을 두고 알림. 결과는 그날 상태 파일 token_check_{tag}"""
        need = (pd.Timestamp(f"{self.day.date.date()} {until}") - self.now()).total_seconds() + 1800
        hms = self.now().strftime("%H:%M:%S")
        tmo = STOP_HTTP_TIMEOUT if T["stop_from"] <= hms < T.get("stop_send_until", "15:29:00") else None  # 손절 감시 시간대면 5초
        out = {}
        for name, c in (("order", getattr(self.broker, "client", None)), ("quote", getattr(self.quote_caller(), "c", None))):
            auth = getattr(c, "auth", None)
            if auth is None:
                continue
            try:
                auth.get_token(ahead=need, timeout=tmo)
                left = auth.seconds_left()
                out[name] = {"ok": left >= need, "left_min": round(left / 60, 1)}
            except Exception as e:  # noqa: BLE001
                out[name] = {"ok": False, "error": repr(e)[:150]}
        self.day.set(f"token_check_{tag}", {"time": self.now().strftime("%H:%M:%S"), "need_min": round(need / 60, 1), **out})
        bad = {k: v for k, v in out.items() if not v["ok"]}
        if bad:
            alert(f"[토큰 갱신 확인 실패] {tag}: {bad} — 기존 토큰으로 계속, 만료 30분 전부터 다시 갱신 시도"[:300])
        return out

    def converted_duties(self, pos):
        """11J 보완 3: 예전 형식(거래 단위·trade_id 없음)에서 종목 코드 단위로 바꾼 매도 의무·목록 (positions.convert_duties) 을 계좌와 맞추고 알림.
        계좌에 없는 종목은 보유 0 으로 기록 (R1: 1분 이상 간격 두 번째 0 에서 끝). 옛 목록 항목 중 지금 보유 거래 것인지 판단 못 한 것(R2)은
        의무로 만들지 않고 종목마다 "[매도 의무 확인 필요]" 알림 (보유 파일·계좌 수량, 옛 항목 내용)"""
        conv = pos.pop("duty_converted")
        review = conv.pop("_review", {})
        try:
            acct = self.broker.account()["holdings"]
            zero = O.end_duties(pos, acct, self.now())[1]
        except Exception as e:  # noqa: BLE001
            acct, zero = None, f"계좌 조회 실패 ({e!r})"[:150]
        save_positions(pos)
        self.day.err(f"매도 의무 형식 변환 (종목 코드 단위): {conv}, 계좌 보유 0 (다음 확인에서 끝): {zero}, 사람 확인 {list(review)}")
        alert(f"[매도 의무 형식 변환] 종목 코드 단위로: {conv} / 계좌 보유 0 (1분 뒤 다시 0 이면 끝): {zero} / 남은 의무 {sorted(pos['sell_duty'])}"[:400])
        if review:
            self.day.set("duty_review", {"time": self.now().strftime("%H:%M:%S"), "items": review})
        for c, x in review.items():
            h, aq = x["held"], ("계좌 조회 실패" if acct is None else f"{int((acct.get(c) or {}).get('qty', 0))}주")
            alert(f"[매도 의무 확인 필요] {c}: 옛 다음 날 시가 목록 항목이 지금 보유 거래 것인지 알 수 없어 의무로 만들지 않음. "
                  f"보유 파일 {('%s주 (거래 %s, 진입일 %s)' % (h['qty'], h['trade_id'], h['entry_date'])) if h else '없음'}, 계좌 {aq}, "
                  f"옛 항목 {json.dumps(x['entry'], ensure_ascii=False)} — 팔아야 하면 사람이 판단"[:400])
        return pos

    def request_prep(self, reason):
        """준비를 (다시) 시작: 스레드에서 일봉 반영·적재. 상태 파일은 스레드가 고치지 않는다"""
        if self.prep_thread is not None:
            return
        d = self.day
        need_data = not d.done("data") and (d.status("data") != S.FAILED or d.can_retry("data"))
        if need_data:
            d.begin("data")
        self.prep_reason = reason
        self.prep_result = None
        self.prep_thread = threading.Thread(target=self._prep_work, args=(d.date, need_data), daemon=True)
        self.prep_thread.start()

    def _prep_work(self, day, need_data):
        """스레드: 일봉 반영(필요하면) -> 모델·일봉 메모리 적재 -> 동결 함수 확인 -> 대상 후보 거래대금. 결과만 prep_result 에"""
        import daily
        import run_day as R
        out = {"started": self.now().strftime("%H:%M:%S")}
        try:
            if need_data:
                try:
                    daily.update()
                    out["data"] = {"daily_update": "ok"}
                except Exception as e:  # noqa: BLE001
                    out["data"] = {"daily_update": "fail", "error": repr(e)[:300]}
                out["data"]["official_missing_dates"] = jload(daily.data_file("update.json", daily.APPEND_DIR), {}).get("official_missing_dates", [])
                out["data"]["attempt"] = self.now().strftime("%H:%M:%S")
            # 11K 보완 3: 공식 일봉 재판단·전날 복구 값은 모델·메모리 적재와 따로, 먼저 (모델이 없거나 적재가 실패해도 08:30 전에 적용되게)
            self.judge_work(day, out)
            pool, b, cols, index, ledgers, pinfo = R.prepare(day)
            out.update(pool=pool, boosters=b, cols=cols, index=index, ledgers=ledgers, prep=pinfo, frozen=E.frozen_check())
            amt = pool.amt20(day)
            ex = daily.excluded_codes()
            for c in ex.get("fetch_error", []) + ex.get("missing_row", []):  # 11K 5: 일봉 행 누락 종목도 파일 기준 거래대금
                amt[c] = daily.amt20_files(c, day)
            out["amt20"], out["excluded"] = amt, ex
        except Exception as e:  # noqa: BLE001
            out["error"] = repr(e)[:300]
            out["trace"] = traceback.format_exc()[-800:]
        self.prep_result = out

    def judge_work(self, day, out):
        """준비 스레드 (11J 보완 3 N1, 11K 보완 3): 전날 복구의 공식 손절가·추세 판정과 공식 일봉 재판단(official_judge)을 계산만 한다.
        모델 로드·일봉 메모리 적재 전에, 따로 (그쪽이 실패해도 결과가 남음). 주 스레드는 tick 맨 앞에서 적용만"""
        jr = {"day": pd.Timestamp(day).normalize(), "at": self.now().strftime("%H:%M:%S")}
        try:
            jr["recovery"] = self.recovery_fixes()
            prev = str(S.prev_trading_day(day, self.holidays).date())
            held = P.load(POSITIONS)[0]["held"]
            codes = self.rejudge_pending(held, prev)
            j = self.judge(codes, prev) if codes else {}
            jr["official_judge"] = {**j, "_missing": [c for c in codes if c not in j]}
        except Exception as e:  # noqa: BLE001
            jr["recovery"] = {"error": repr(e)[:300]}
            out["judge_error"] = repr(e)[:300]
        # 보완 2 (2): 준비 결과(prep_result)와 따로 바로 공개 -> 모델·메모리 적재가 늦거나 실패해도 주 스레드가 다음 tick 에 적용
        out["recovery"], out["official_judge"], out["judge"] = jr.get("recovery"), jr.get("official_judge"), jr
        self.judge_result = jr

    @staticmethod
    def rejudge_pending(held, prev):
        """전날 15:30 1분봉으로 판단했고 아직 공식 일봉으로 재판단하지 않은 종목 (11K 3)"""
        return [c for c, p in held.items() if p.get("judged_on") == prev and p.get("judged_by", "15:30 1분봉") == "15:30 1분봉" and not P.in_review(p)]

    def apply_prep(self):
        """준비 스레드 결과를 상태에 반영 (주 스레드)"""
        d = self.day
        out, self.prep_result, self.prep_thread = self.prep_result or {}, None, None
        hms = self.now().strftime("%H:%M:%S")
        if "data" in out:
            info = out["data"]
            if info["daily_update"] == "fail":  # Codex 23: 일봉 반영 실패면 data 단계 FAILED
                d.finish("data", S.FAILED, info)
                d.err(f"일봉 반영 실패: {info.get('error')}"[:300])
                alert(f"[일봉 반영 실패] {info.get('error')}"[:300])
            else:
                d.mark("data", info)
        if out.get("error"):
            d.err(f"준비 실패 ({self.prep_reason}): {out['error']} | {out.get('trace', '')}"[:800])
            if self.pool is not None and self.ctx.get("prep"):  # 다시 적재 실패: 이전 적재 유지 (판단은 이전 준비 결과 그대로)
                self.prep_ok = True
                alert(f"[다시 적재 실패] 이전 적재 유지: {out['error']}"[:300])
                return
            if out["error"] != getattr(self, "_last_prep_error", None):  # 같은 실패(예: 모델 학습 중 모델 없음)는 알림 한 번
                alert(f"[준비 실패] 60초 뒤 다시, 신규 매수는 준비될 때까지 안 함: {out['error']}"[:300])
            self._last_prep_error = out["error"]
            d.tag_last_error("prep_fail")
            self.next_prep_try = self.now() + pd.Timedelta(seconds=60)
            return
        if self.pool is not None and hms >= T.get("no_reload_after", "15:18:00"):
            self.dispose_pool(out["pool"], "15:18 뒤 다시 적재 안 함")  # B6: 15:18 뒤에는 메모리 다시 적재 안 함 (이전 적재 유지)
            self.prep_ok = True
            d.err(f"준비({self.prep_reason})가 15:18 뒤에 끝남 -> 다시 적재 안 함, 그날 신규 매수 안 함")
            self.block_buys("late_reload", "15:18 뒤 다시 적재 -> 그날 신규 매수 안 함 (B6)", "[다시 적재 늦음] 15:18 뒤 끝남 -> 오늘 신규 매수 안 함")
            return
        self._last_prep_error = None
        self.dispose_pool(self.pool, "다시 적재로 바꿈")
        self.pool = out["pool"]
        pinfo = out["prep"]
        self.ctx.update(boosters=out["boosters"], cols=out["cols"], index=out["index"], ledgers=out["ledgers"], prep=pinfo,
                        ledger_as_of=pinfo.get("ledger_as_of"), ledger_version=pinfo.get("ledger_version", 0),
                        data_version=pinfo["data"].get("data_version"), frozen=out["frozen"], amt20=out["amt20"], excluded=out["excluded"])
        model_ok, up = pinfo["model"]["check"]["ok"], pinfo["data"]["daily_up_to_date"]
        frozen_ok = out["frozen"]["ok"]
        data_failed = d.status("data") == S.FAILED
        self.ctx["buy_allowed"] = model_ok and up and frozen_ok and not data_failed
        self.prep_ok = True
        if not model_ok:  # 11J: 모델 확인 실패(해시·특징·고정 표본 예측·월·시드 1 ~ 5) -> 그날 신규 매수 안 함 + 알림
            self.model_bad.add(d.date.strftime("%Y-%m"))  # 11K 보완 1: 따라잡기 대상 (첫 거래일 07:00 ~ 15:00 이면 다시 학습)
            alert(f"[모델 확인 실패] 오늘 신규 매수 안 함: { {k: v for k, v in pinfo['model']['check'].items() if k != 'files'} }"[:300])
        if not (model_ok and up):
            d.err(f"모델 로드 확인 {model_ok}, 전날 일봉 반영 {up} (달력 직전 거래일 {pinfo['data'].get('expected_prev_trading_day')}, "
                  f"데이터 마지막 날 {pinfo['data'].get('data_last_date')}) -> 그날 신규 매수 안 함 (6장, Codex 23)", key="prep_data")
        else:  # 11I: 앞서 남긴 "전날 일봉 반영 False"·준비 실패 오류가 다시 적재로 풀렸으면 해결로 표시 (요약의 오류 수에서 뺌)
            d.resolve("prep_data", f"{self.prep_reason}: 모델 확인·전날 일봉 반영 True")
            d.resolve("prep_fail", f"{self.prep_reason}: 준비 성공")
        if not frozen_ok:
            d.err(f"동결 함수 해시·치환 확인 실패 -> 그날 신규 매수 안 함 (B7): {out['frozen']}")
            alert(f"[동결 함수 확인 실패] 오늘 신규 매수 안 함: {out['frozen']}"[:300])
        rec = {"reason": self.prep_reason, "model_ok": model_ok, "daily_up_to_date": up, "data_step": d.status("data"),
               "expected_prev_trading_day": pinfo["data"].get("expected_prev_trading_day"), "data_last_date": pinfo["data"].get("data_last_date"),
               "prev_trading_day": pinfo["data"]["prev_trading_day"], "official_prev_day": pinfo["data"].get("official_prev_day"),
               "orders_enabled": self.broker.allow_orders if self.broker else None, "env": CFG["env"], "code": git_state(),
               "data_version": self.ctx["data_version"], "frozen_ok": frozen_ok}
        if d.done("day_start"):
            with d.lock:
                d.s.setdefault("day_start_again", []).append({"time": hms, **rec})
                old = d.s["steps"]["day_start"]
                if old.get("daily_up_to_date") != up:
                    d.s.setdefault("corrections", []).append({"time": hms, "field": "day_start.daily_up_to_date", "old": old.get("daily_up_to_date"),
                                                              "new": up, "reason": self.prep_reason})
                d.save()
        else:
            d.mark("day_start", rec)
        if self.prep_reason.startswith("공식"):
            alert(f"[공식 시세 반영] {d.date.date()} {self.prep_reason}, 전날 일봉 반영 {up}")
        for name, fn in (("prev_recovery", lambda: self.finalize_recovery(out.get("recovery"))), ("exclusions", self.check_exclusions),
                         ("official_vs_1530", lambda: None if (out.get("judge") or out).get("official_applied") else self.official_vs_1530(out.get("official_judge")))):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                d.err(f"{name} 오류: {e!r} | {traceback.format_exc()[-400:]}")
                if name in ("prev_recovery", "exclusions"):
                    self.block_buys(f"{name}_error", f"{name} 확인 오류 -> 그날 신규 매수 안 함: {e!r}", f"[{name} 확인 오류] 오늘 신규 매수 안 함: {e!r}")

    def check_exclusions(self):
        """Codex 27: 제외 종목을 (a) 사전 승인 목록(수정주가 계수 변경·공식 시세 없음, 날짜·사유 기록)과 (b) 당일 수집 실패로 나눈다.
        당일 실패 종목이 전날 대상 후보(20일 평균 거래대금 40억 이상)면 그날 신규 매수 안 함, 후보 밖이면 기록만"""
        d = self.day
        ex, amt = self.ctx.get("excluded") or {}, self.ctx.get("amt20") or {}
        appr = jload(APPROVED, {"items": []})
        have = {(i["code"], i["kind"]) for i in appr["items"]}
        for kind, why in (("adj_changed", "수정주가 계수 변경 (기본 일봉 파일 다시 만들 때까지)"), ("no_official", "추가분에 공식 시세 없음")):
            for c in ex.get(kind, []):
                if (c, kind) not in have:
                    appr["items"].append({"code": c, "kind": kind, "since": str(d.date.date()), "reason": why})
        jdump(APPROVED, appr)
        if ex.get("missing_row"):  # 11K 5: 공식 시세에 거래가 있는 날의 일봉 행이 없는 종목 = 그날 계산에서 제외, 집계만 (매수 차단 안 함)
            d.set("missing_rows", {"n": len(ex["missing_row"]), "codes": ex["missing_row"],
                                   "candidates": [c for c in ex["missing_row"] if (amt.get(c) or 0) >= CAND_AMT]})
            rlog(f"일봉 행 누락 (공식 시세에는 있음) -> 그날 계산에서 제외: {ex['missing_row']}"[:300], "WARN")
        fails = [{"code": c, "kind": "fetch_error", "amt20": amt.get(c)} for c in ex.get("fetch_error", [])]
        return self._candidate_fail(fails, "일봉 수집 실패")

    def _candidate_fail(self, fails, what):
        d = self.day
        cand = [f for f in fails if f.get("amt20") is not None and np.isfinite(f["amt20"]) and f["amt20"] >= CAND_AMT]
        unknown = [f for f in fails if f.get("amt20") is None or not np.isfinite(f["amt20"])]
        with d.lock:
            d.s.setdefault("same_day_failures", []).append({"time": self.now().strftime("%H:%M:%S"), "what": what, "all": fails,
                                                            "candidates": cand, "amt_unknown": unknown})
            d.save()
        if cand or unknown:
            self.block_buys(f"candidate_fail:{what}", f"{what} 종목 중 전날 대상 후보(20일 평균 거래대금 40억 이상 또는 거래대금 모름): "
                            f"{[f['code'] for f in cand + unknown]} -> 그날 신규 매수 안 함 (Codex 27)",
                            f"[대상 후보 {what}] {[f['code'] for f in cand + unknown]} -> 오늘 신규 매수 안 함")
        return {"failures": len(fails), "candidates": [f["code"] for f in cand], "amt_unknown": [f["code"] for f in unknown]}

    # ------------------------------------------------------------ 판단 (손절가·추세)

    def judge(self, codes, day, cvs=None):
        """day 기준 종가·손절가(원주가)·추세(종가 < 20일선). cvs={code: 15:30 1분봉 값} 이면 그 종목만 오늘 행을 붙여서 (동결 load_stock 사본),
        cvs 가 없으면 파일의 day 행(공식 일봉)으로. 일봉 대상에서 빠진 종목은 원주가 일봉 (Codex 6). 반환 {code: {close, stop, trend, basis}}"""
        from daily import Store
        day = pd.Timestamp(day)
        if cvs is not None:
            codes = [c for c in codes if c in cvs]
        st = Store(list(codes), use_append=True)
        frames, out = {}, {}
        lm = E.make_load_mem(lambda c: frames[c])
        for c in codes:
            cv = (cvs or {}).get(c)
            if c not in st.adj:
                row = {k: cv[k] for k in ("open", "high", "low", "close")} if cv else None
                r = P.raw_judge(c, day, row)
                if r is not None:
                    out[c] = {"close": r[0], "stop": r[1], "trend": r[2], "basis": "원주가 일봉 (일봉 대상 제외 종목)"}
                continue
            if cv:
                f, vs = E.default_factor(st.adj[c], st.raw[c], day)
                frames[c] = E.today_frames(st.adj[c], st.raw[c], day, cv, f, vs)
            else:
                a, r = st.adj[c], st.raw[c]
                frames[c] = (a[a.timestamp <= day].copy(), r[r.timestamp <= day].copy())
            df = lm(c, until=day)
            if df.empty or df.timestamp.iat[-1] != day or not np.isfinite(df.atr20.iat[-1]):
                continue
            fac = float(df.factor.iat[-1])
            out[c] = {"close": float(df.close.iat[-1] / fac), "stop": float((df.close.iat[-1] - s4.STOP_ATR * df.atr20.iat[-1]) / fac),
                      "trend": bool(df.close.iat[-1] < df.ma20.iat[-1]), "basis": "수정주가 일봉"}
        return out

    def recovery_fixes(self):
        """11J 보완 3 N1 (준비 스레드에서): 전날 복구 마무리에 쓸 값을 계산만 한다 — 임시 손절가 종목의 진입일 공식 일봉 손절가,
        마지막 날 추세 판정을 못 한 종목의 판정. 보유 장부는 읽기만. 반환 None = 할 것 없음, 아니면 {prev, stops, trend, left, at}"""
        rec = self.day.s.get("prev_recovery")
        if not rec or rec.get("finalized"):
            return None
        prev = rec["prev"]
        pos = P.load(POSITIONS)[0]
        stops, trend, left = {}, {}, []
        fix = {c: p["entry_date"] for c, p in pos["held"].items() if p.get("stop_temp") and not p.get("entry_temp") and p.get("entry_date")
               and not p.get("ambiguous")}  # 미확정은 후보 중 높은 손절가 유지 (3차 확인 리뷰)
        for c, ed in fix.items():
            j = self.judge([c], ed).get(c)
            if j is None:
                left.append(c)
                continue
            stops[c] = {"stop": j["stop"], "basis": f"진입일 {ed} 공식 일봉 종가 - 1.7 x ATR20 ({j['basis']}, 다음 날 복구)"}
        need = [c for c, p in pos["held"].items() if p.get("judged_on") != prev and p.get("entry_date") and prev > p["entry_date"]]
        jp = self.judge(need, prev) if need else {}
        for c in need:
            if c not in jp:
                left.append(c)
                continue
            trend[c] = bool(jp[c]["trend"])
        return {"prev": prev, "stops": stops, "trend": trend, "left": left, "at": self.now().strftime("%H:%M:%S")}

    def apply_recovery_fixes(self, fx):
        """11J 보완 3 N1 (주 스레드, 계산 없음): 준비 스레드가 계산한 공식 손절가·추세 판정을 보유 장부에 적용. 매 분 새 손절 감지보다 먼저 부른다.
        임시 손절가로 만든 손절 의무는 공식 손절가로 바로잡히면: 오늘 그 종목 매도를 아직 안 보냈고(NOT_SENT 만) 현재가 >= 공식 손절가면 의무 해제,
        아니면 의무 유지(손절가만 공식으로). 반환: 해제한 의무"""
        if fx is None or fx.get("applied") or "error" in fx:
            return {}
        rec = self.day.s.get("prev_recovery")
        if not rec or rec.get("finalized"):
            fx["applied"] = "복구할 것 없음"
            return {}
        pos = self.positions()
        fixed = []
        for c, s in fx["stops"].items():
            p = pos["held"].get(c)
            if p and p.get("stop_temp") and not P.in_review(p):  # 11K 보완 3: 수동 확인 중이면 손절가 그대로
                p.update(stop_price=s["stop"], stop_temp=False, stop_basis=s["basis"])
                fixed.append(c)
        for c, tr in fx["trend"].items():
            if c in pos["held"] and not P.in_review(pos["held"][c]):
                pos["held"][c].update(trend_sell_next_open=tr, judged_on=fx["prev"], judged_by="공식 일봉 (다음 날 복구)")
                if tr:
                    self.mark_trend(pos, c, fx["prev"], "공식 일봉 (다음 날 복구)")
        released = self.release_temp_duties(pos, fixed)
        save_positions(pos)
        fx["applied"] = self.now().strftime("%H:%M:%S")
        rlog(f"전날 복구 적용 (계산은 준비 스레드 {fx['at']}): 공식 손절가 {fixed}, 추세 판정 {sorted(fx['trend'])}, 손절 의무 해제 {sorted(released)}")
        return released

    def release_temp_duties(self, pos, codes, old=None):
        """N1: 임시 손절가로 만든 손절 의무 중 공식 손절가로 바로잡힌 것 (codes). 오늘 생긴 의무이고 오늘 그 종목 매도를 보낸 적이 없고
        (NOT_SENT 밖의 의도 없음) 현재가(시세 조회, 5초) >= 공식 손절가면 해제. 보낸 주문이 있거나 시세를 모르면 유지 (손절가만 공식으로).
        11K 3: old = {code: 바뀌기 전 손절가} 면 임시 손절가 대신 그 손절가로 만든 의무 (15:30 1분봉 손절가 -> 공식 손절가)"""
        today, duty = str(self.day.date.date()), pos.get("sell_duty", {})
        cand = [c for c in codes if (duty.get(c) or {}).get("kind") == "stop"
                and (duty[c].get("stop_temp") if old is None else abs(float(duty[c].get("stop") or 0) - old[c]) < 0.5)]
        if not cand:
            return {}
        prices = {}
        try:
            _, snap, _ = Q.snapshot(self.quote_caller(), cand, timeout=STOP_HTTP_TIMEOUT)
            prices = fresh_prices(snap, self.day.date)[0]  # 11K 4: 오늘 체결 시세만
        except Exception as e:  # noqa: BLE001
            self.day.err(f"임시 손절가 의무 확인 시세 조회 실패 -> 의무 유지: {e!r}"[:300])
        out = {}
        for c in cand:
            du, stop = duty[c], float(pos["held"][c]["stop_price"])
            sent = [i["intent_id"] for i in (self.om.store.of(today, c, side="sell") if self.om else [None]) if i is None or i["status"] != O.NOT_SENT]
            px = prices.get(c)
            if du.get("since") == today and not sent and px and px >= stop:
                duty.pop(c)
                pos["next_open_sells"].pop(c, None)
                out[c] = {"price": float(px), "stop": stop, "temp_stop": du.get("stop")}
                alert(f"[손절 의무 해제] {c}: 임시 손절가 {du.get('stop')} 로 만든 의무, 공식 손절가 {stop:.0f} <= 현재가 {px:.0f}, 보낸 주문 없음")
            else:
                du.update(stop=stop, stop_temp=False, reason=f"{du.get('reason')} (공식 손절가 {stop:.0f} 로 바로잡음, "
                          + ("보낸 주문 있음 -> 유지)" if sent else "현재가 모름 -> 유지)" if not px else f"현재가 {px:.0f} < 공식 손절가 -> 유지)"))
        return out

    def finalize_recovery(self, fx=None):
        """Codex 5·확인 리뷰 N1: 전날 복구 마무리 (준비 뒤, 공식 일봉이 들어온 다음). 임시 손절가 -> 진입일 종가 - 1.7 x ATR20,
        마지막 날 추세 판정을 못 한 종목 판정, 장부가 갱신 안 된 날들을 차례로 다시 계산해 저장 (실패면 그날 신규 매수 안 함).
        끝나기 전(finalized False)에는 신규 매수 안 함 (buy_gate). 11J 보완 3 N1: 손절가·판정은 준비 스레드가 계산한 fx 를 (tick 맨 앞에서 이미)
        적용, fx 가 없으면(준비 스레드 계산 실패) 여기서 계산"""
        d = self.day
        rec = d.s.get("prev_recovery")
        if not rec or rec.get("finalized"):
            return {"note": "복구할 것 없음"}
        if fx is None or "error" in fx:
            fx = self.recovery_fixes()
        self.apply_recovery_fixes(fx)
        left, prev = list(fx["left"]), rec["prev"]
        stale = rec.get("ledger_stale_days")
        if stale is None:  # 예전 형식
            stale = [prev] if rec.get("ledger_stale") else []
        led = self.recompute_ledgers(stale) if stale else "확인"
        ok = not left and not str(led).startswith("실패")
        with d.lock:
            d.s["prev_recovery"].update(finalized=ok, left=left, ledger=led, time_finalized=self.now().strftime("%H:%M:%S"))
            d.save()
        if left:
            alert(f"[전날 복구 미완료] 공식 일봉이 없어 임시 손절가·추세 판정 유지, 신규 매수 안 함: {left}")
        return {"left": left, "ledger": led}

    def recompute_ledgers(self, days):
        """장부가 갱신 안 된 날들(Codex 5·확인 리뷰 N1): 오래된 날부터 그날 스냅샷 + 지금 장부(앞 날을 다시 계산한 결과)로 계산해
        as_of = 그날 로 저장. 스냅샷 없는 날은 장부 그대로. 오늘 메모리 장부도 다시 읽음. 실패하면 그날 신규 매수 안 함"""
        import run_day as R
        out = []
        for ds in sorted(days):
            day = pd.Timestamp(ds)
            live = new_day(day, clock=self.clock).s.get("live") or {}
            if not live.get("snap_file"):
                out.append(f"{ds}: 스냅샷 없음 -> 장부 그대로")
                continue
            try:
                full = pd.read_parquet(live["snap_file"])
                snap = pd.DataFrame({c[2:]: full[c] for c in full.columns if c.startswith("p_")})
                rows = R.snap_rows(snap, full, day)
                b, cols = self.ctx["boosters"], self.ctx["cols"]
                if day.strftime("%Y-%m") != self.day.date.strftime("%Y-%m"):
                    b, cols, _, _ = M.load(day.strftime("%Y-%m"))
                lj, _ = ledger_read(LEDGER)
                res = E.run_day(self.pool, day, rows, self.ctx["index"], live["index_values"], b, cols, {n: lj[n] for n in NAMES})
                out.append(f"{ds}: {self.write_ledger(res, live, day=day)}")
            except Exception as e:  # noqa: BLE001
                self.block_buys("prev_ledger_unconfirmed", f"{ds} 장부 다시 계산 실패 -> 그날 신규 매수 안 함 (Codex 5): {e!r}",
                                f"[전날 장부 미확인] {ds} 다시 계산 실패 -> 오늘 신규 매수 안 함: {e!r}")
                return f"실패: {ds} {e!r}"[:200]
        lj, ver = ledger_read(LEDGER)
        self.ctx.update(ledgers={n: lj[n] for n in NAMES}, ledger_version=ver, ledger_as_of=lj.get("as_of"))
        alert(f"[장부 다시 계산] {out}"[:300])
        return "; ".join(out)

    def official_vs_1530(self, pre=None, compute=True):
        """Codex 21 -> 11K 3: 전날 15:30 1분봉으로 한 판단(종가·손절가·추세)을 공식 일봉으로 다시 계산해, 다르면 공식 값을 쓰고 기록
        (예전: 기록만). 공식 시세는 보통 다음 날 08:10 전후 -> 준비 스레드가 계산(pre), 주 스레드는 tick 맨 앞에서 적용만 (08:30 추세 매도·09:00 손절 감지 전).
        - 진입일 손절가(진입일 = 전날)가 다르면 공식 손절가로. 그 손절가로 오늘 생긴 손절 의무는 보낸 주문이 없고 현재가 >= 공식 손절가면 해제.
        - 추세: 공식 참·15:30 거짓 -> 추세 매도 의무·다음 날 시가 목록에 넣음. 공식 거짓·15:30 참 -> 그 판정으로 생긴 의무(발생일 = 전날)이고 오늘 그 종목
          매도를 보낸 적이 없으면(NOT_SENT 밖 없음) 의무·목록에서 뺌, 보냈으면 유지하고 기록.
        - 재판단한 종목은 judged_by = "공식 일봉 (다음 날 재판단)" (다시 안 함). 공식 일봉이 없는 종목은 그대로 (공식 시세 다시 받은 뒤 준비에서 다시).
        compute False (tick 맨 앞): pre 가 대상 종목을 다 덮지 않으면 계산하지 않고 넘김 (준비 결과 적용 때 계산)"""
        d = self.day
        prev = str(S.prev_trading_day(d.date, self.holidays).date())
        pos = self.positions()
        codes = [c for c, p in pos["held"].items() if p.get("judged_on") == prev and p.get("judged_by", "15:30 1분봉") == "15:30 1분봉"]
        if not codes:
            return {"n": 0}
        if pre is not None:
            pre = {**pre, "_missing": list(pre.get("_missing", [])) + [c for c in codes if P.in_review(pos["held"][c]) and c not in pre]}
        if pre is not None and set(codes) <= set(pre) | set(pre.get("_missing", [])):
            j = pre
        elif not compute:
            return {"n": len(codes), "skipped": "준비 결과에 없음 -> 준비 결과 적용 때 계산"}
        else:
            j = self.judge(codes, prev)
        today = str(d.date.date())
        diffs, changed, old_stops = [], [], {}
        for c in codes:
            p, o = pos["held"][c], j.get(c)
            if P.in_review(p):  # 11K 보완 3: 수동 확인 중 = 손절가·추세 판단을 바꾸지 않음
                diffs.append({"code": c, "note": "수동 확인 중 -> 공식 값 적용 안 함"})
                p["judged_by"] = "공식 일봉 (다음 날 재판단, 수동 확인 중 적용 안 함)"
                continue
            if o is None:
                diffs.append({"code": c, "note": "공식 일봉 없음 (판단 그대로, 공식 시세 받은 뒤 다시)"})
                continue
            row = {"code": c}
            if p.get("close_1530") is not None and abs(float(p["close_1530"]) - o["close"]) > 0.5:
                row.update(close_1530=p["close_1530"], close_official=o["close"])
            t15, tof = bool(p.get("trend_sell_next_open")), bool(o["trend"] and prev > p.get("entry_date", prev))
            if t15 != tof:
                row.update(trend_1530=t15, trend_official=tof)
                p["trend_sell_next_open"] = tof
                du = pos.get("sell_duty", {}).get(c)
                if tof:
                    self.mark_trend(pos, c, prev, "공식 일봉 (다음 날 재판단)")
                    row["applied"] = "추세 매도 의무 추가"
                elif du and du.get("kind") == "trend":
                    sent = [i["intent_id"] for i in (self.om.store.of(today, c, side="sell") if self.om else []) if i["status"] != O.NOT_SENT]
                    if du.get("since") == prev and not sent:
                        pos["sell_duty"].pop(c)
                        pos["next_open_sells"].pop(c, None)
                        row["applied"] = "추세 매도 의무 해제"
                    else:
                        row["applied"] = "의무 유지 (" + (f"오늘 매도 보냄 {sent}" if sent else f"의무 발생일 {du.get('since')} != 전날") + ")"
            if p.get("entry_date") == prev and p.get("stop_price") and not p.get("ambiguous") and abs(float(p["stop_price"]) - o["stop"]) > 0.5:
                row.update(stop_1530=p["stop_price"], stop_official=o["stop"], applied_stop="공식 손절가로")
                old_stops[c] = float(p["stop_price"])
                p.update(stop_price=float(o["stop"]), stop_temp=False,
                         stop_basis=f"진입일 {prev} 공식 일봉 종가 - 1.7 x ATR20 ({o.get('basis', '')}, 다음 날 재판단, 15:30 1분봉 값 {old_stops[c]:.0f})")
            p.update(judged_by="공식 일봉 (다음 날 재판단)", close_official=o["close"])
            if len(row) > 1:
                diffs.append(row)
                if "applied" in row or "applied_stop" in row:
                    changed.append(row)
        released = self.release_temp_duties(pos, list(old_stops), old=old_stops) if old_stops else {}
        save_positions(pos)
        d.set("official_vs_1530", {"date": prev, "checked": len(codes), "diffs": diffs, "applied_at": self.now().strftime("%H:%M:%S"),
                                   "released": released, "rule": "다르면 공식 값을 씀 (11K 3)"})
        if changed:
            alert(f"[공식 일봉 재판단] {prev} 15:30 1분봉 판단과 다름 -> 공식 값: {changed}"[:400])
        return {"n": len(codes), "diffs": len(diffs), "changed": len(changed)}

    def retry_official(self):
        """전날 공식 시세가 없어 일봉을 다 넣지 못했으면 official_retry_until(15:00)까지 10분마다 확인 (2026-10-23 결정).
        나오면: 보관한 그날 키움 행이 있으면 바로 보정해 붙이고(몇 초), 없으면 일봉 반영 전체를 따로 돌린다(스레드, 장중 감시·하트비트는 계속).
        스레드는 상태를 직접 고치지 않고 결과(redo_result)만 넘긴다 (2026-10-24, R2-12). 끝나면 일봉을 다시 메모리에 올린다.
        마감까지 없으면 텔레그램 알림 + 그날 신규 매수 쉼."""
        import daily
        d = self.day
        if self.redo_thread is not None:
            if self.redo_thread.is_alive():
                return
            self.redo_thread = None
            res, self.redo_result = self.redo_result or {}, None
            if res.get("error"):
                d.err(f"일봉 다시 반영 실패: {res['error']}")
            if self.now().strftime("%H:%M:%S") >= T.get("no_reload_after", "15:18:00"):
                d.err("일봉 다시 반영이 15:18 뒤에 끝남 -> 메모리 다시 적재 안 함 (B6), 그날 신규 매수 안 함")
                return
            u = jload(daily.data_file("update.json", daily.APPEND_DIR), {})
            with d.lock:
                d.s["steps"]["data"].update(official_missing_dates=u.get("official_missing_dates", []), attempt=self.now().strftime("%H:%M:%S"),
                                            redone_full=self.now().strftime("%H:%M:%S"))
                d.save()
            self.prep_ok = False
            self.request_prep("공식 시세 반영 뒤 다시 적재 (일봉 반영 전체)")
            return
        st = d.s["steps"].get("data", {})
        if not st.get("official_missing_dates"):
            return
        last = pd.Timestamp(f"{d.date.date()} {st.get('attempt', '00:00:00')}")
        hms = self.now().strftime("%H:%M:%S")
        gap = 2 if at("08:00:00") <= hms < T["trend_sell_from"] else 10  # 11K 3: 공식 시세가 나오는 08:10 전후는 2분마다 (08:30 전에 재판단)
        if self.now() < last + pd.Timedelta(minutes=gap):
            return
        if self.now().strftime("%H:%M:%S") >= T.get("official_retry_until", "15:00:00"):
            if not d.done("official_given_up"):
                d.err(f"전날 공식 시세 못 받음 ({st['official_missing_dates']}) -> 전날 일봉 미반영, 신규 매수 안 함 (6장)")
                alert(f"[공식 시세 못 받음] {st['official_missing_dates']} -> 오늘 신규 매수 안 함")
                d.mark("official_given_up")
            return
        from kiwoom import krx_official
        probe = krx_official.fetch_day(pd.Timestamp(st["official_missing_dates"][0]).strftime("%Y%m%d"))  # 공식 시세 2회 요청으로 먼저 확인
        with d.lock:
            d.s.setdefault("official_retries", []).append({"time": self.now().strftime("%H:%M:%S"), "available": not probe.empty})
            d.s["steps"]["data"]["attempt"] = self.now().strftime("%H:%M:%S")
            d.save()
        if probe.empty:
            return
        with d.lock:
            d.s["official_first_seen"] = d.s.get("official_first_seen") or self.now().strftime("%Y-%m-%d %H:%M:%S")
            d.save()
        if daily.has_pending():
            r = daily.reapply_official()
            with d.lock:
                d.s.setdefault("official_reapply", []).append({"time": self.now().strftime("%H:%M:%S"), **r})
                if r.get("ok"):
                    d.s["steps"]["data"]["official_missing_dates"] = []
                d.save()
            if r.get("ok"):
                self.prep_ok = False
                self.request_prep(f"공식 시세 반영 뒤 다시 적재 (보관 행 {r['stocks']}종목 보정)")
            return
        self.redo_thread = threading.Thread(target=self._redo_update, daemon=True)
        self.redo_thread.start()
        with d.lock:
            d.s.setdefault("official_reapply", []).append({"time": self.now().strftime("%H:%M:%S"), "mode": "보관 행 없음 -> 일봉 반영 전체 (스레드)"})
            d.save()

    def _redo_update(self):
        """스레드: 상태(d.s)를 고치지 않고 결과만 남긴다"""
        import daily
        try:
            daily.update()
            self.redo_result = {"ok": True, "time": self.now().strftime("%H:%M:%S")}
        except Exception as e:  # noqa: BLE001
            self.redo_result = {"ok": False, "error": repr(e)[:300]}


    # ------------------------------------------------------------ 장중

    def holding_diff(self, held):
        """상태 파일 vs 계좌 보유. 같은 날 매수·매도 의도·체결로 설명되는 차이는 뺀다 (Codex 14)"""
        pos = self.positions().get("held", {})
        st = {c: int(p.get("qty", 0)) for c, p in pos.items() if p.get("qty", 0) > 0}
        ac = {c: int(h["qty"]) for c, h in held.items() if h["qty"] > 0}
        return O.unexplained_diff(st, ac, self.om.records, self.om.date())

    def mark_qty_review(self, pos, changes, where, prices=None):
        """11K 보완 3: 체결로 설명되지 않는 보유 수량 변화 (orders.qty_changes) -> 그 종목은 수동 확인 필요 (positions.mark_qty_review).
        자동 조정 없음: 손절가·진입가 그대로, 그 종목의 자동 손절·추세 매도·신규 매수 멈춤 (다른 종목은 그대로). 알림: 종목·장부 수량·계좌 수량·비율·손절가·현재가.
        pos 는 저장하지 않음. prices 가 없으면 시세 조회 (5초, 실패면 현재가 모름)"""
        out = {}
        changes = {c: x for c, x in changes.items() if c in pos["held"]}
        if not changes:
            return out
        px = dict(prices or {})
        if any(c not in px for c in changes):
            try:
                _, snap, _ = Q.snapshot(self.quote_caller(), [c for c in changes if c not in px], timeout=STOP_HTTP_TIMEOUT)
                px.update(fresh_prices(snap, self.day.date)[0])
            except Exception:  # noqa: BLE001
                pass
        for c, x in changes.items():
            r = P.mark_qty_review(pos, c, x["account"], self.day.date.date(), self.now().strftime("%H:%M:%S"), where, px.get(c))
            out[c] = r
            with self.day.lock:
                self.day.s.setdefault("qty_review", []).append({"code": c, **r})
                self.day.save()
            alert(f"[수동 확인 필요] {c} 체결로 설명 안 되는 보유 수량 변화 ({where}): 장부 {r['file_qty']}주 -> 계좌 {r['account_qty']}주 (비율 {r['ratio']}), "
                  f"손절가 {r['stop'] if r['stop'] is None else round(r['stop'])}, 현재가 {px.get(c, '모름')} — 분할·병합 등 자동 조정 안 함. "
                  f"이 종목 자동 손절·추세 매도·신규 매수 멈춤 (다른 종목은 그대로). positions.json 에서 qty_review 를 지우거나 손절가를 고칠 때까지"[:500])
        return out

    def trend_wait(self, hms):
        """11K 보완 3·보완 2 (3): 08:30 추세 매도는 공식 일봉 재판단이 끝날 때까지 기다린다 (장 시작 동시호가라 늦게 보내도 같은 시가). 08:50 (TREND_WAIT_UNTIL, 계좌 조회가 늦어도 08:59 전에 보낼 여유)
        까지 안 끝나면 전날(15:30 1분봉) 판단으로 보내고 기록·알림. 반환: 지금 기다리는가"""
        d = self.day
        prev = str(S.prev_trading_day(d.date, self.holidays).date())
        pend = self.rejudge_pending(self.positions().get("held", {}), prev)
        if not pend:
            return False
        last = at(TREND_WAIT_UNTIL)  # 보완 2 (3): 08:50 (계좌 조회가 늦어도 08:59 전에 보낼 여유)
        if hms < last:
            if not d.s.get("trend_wait"):
                d.set("trend_wait", {"since": hms, "codes": pend, "until": last})
                rlog(f"추세 매도: 공식 일봉 재판단 기다림 {pend} (늦어도 {last} 에 보냄)", "WARN")
            return True
        if not d.s.get("trend_sell_without_official"):
            d.set("trend_sell_without_official", {"time": hms, "codes": pend, "note": "공식 일봉 재판단 없이 전날 15:30 1분봉 판단으로 추세 매도 (11K 보완 3)"})
            alert(f"[추세 매도 공식 판단 없음] {hms} 까지 공식 일봉 재판단 안 됨 {pend} -> 전날 15:30 1분봉 판단으로 보냄")
        return False

    def trend_sell(self):
        """08:30 ~ 08:59 추세 매도 + 장 전 계좌 대조 (계좌 조회 실패·보유 불일치면 그날 신규 매수 안 함).
        대상 = 다음 날 시가 매도 목록 (매도 의무 종목, 11J 보완 3). 수량 = 계좌 매도 가능 수량, 계좌 조회 실패면 걸린 매도가 없을 때 보유 파일 수량"""
        d = self.day
        pos = self.positions()
        acct_ok = True
        try:
            held = self.broker.account()["holdings"]
            ch = O.qty_changes(pos["held"], held, self.om.records, self.om.date())  # 11K 보완 3: 장 전 계좌에서 설명 안 되는 수량 변화 먼저
            if ch:
                self.mark_qty_review(pos, ch, "08:30 장 전 계좌 대조")
                save_positions(pos)
            diff = self.holding_diff(held)
            d.set("premarket_account_check", {"time": self.now().strftime("%H:%M:%S"), "ok": not diff, "diff": diff})
            if diff:
                self.block_buys("premarket_account_mismatch", f"장 전 계좌 대조 불일치 {diff} -> 그날 신규 매수 안 함 (6장)",
                                f"[보유 불일치] 장 전 계좌 대조: {diff} -> 오늘 신규 매수 안 함")
        except Exception as e:  # noqa: BLE001
            acct_ok = False
            self.block_buys("premarket_account_fail", f"장 전 계좌 조회 실패 -> 그날 신규 매수 안 함 (6장): {e!r}",
                            f"[장 전 계좌 조회 실패] 오늘 신규 매수 안 함, 추세 매도는 상태 파일 수량으로: {e!r}")
            held = None
        nos = pos["next_open_sells"]
        review = P.review_codes(pos)
        targets = [c for c in nos if c not in review]  # 11K 보완 3: 수동 확인 중인 종목은 자동 추세 매도 안 함
        missing = [c for c in targets if (held.get(c) or {}).get("qty", 0) <= 0] if acct_ok else []
        if acct_ok:
            O.end_duties(pos, held, self.now())  # R1: 보유 0 은 기록, 1분 이상 간격 두 번 연속이면 의무 끝 (목록에서도 뺌)
        if missing:
            key = "trend_missing:" + ",".join(sorted(missing))
            if key not in self.mismatch_seen:
                self.mismatch_seen.add(key)
                d.err(f"추세 매도 대상이 계좌에 없음 {missing} -> 이번에는 안 보냄 (의무는 다음 확인에서 다시 0 이면 끝)")
                alert(f"[보유 불일치] 추세 매도 대상이 계좌에 없음: {missing}")
        go = [c for c in targets if c not in missing]
        recs = self.om.trend_sells(go, O.duty_inputs(pos, go, held), kind_of=lambda c: pos["held"].get(c, {}).get("kind", "본래"))
        self._note_sells(nos, recs)
        save_positions(pos)
        return {"targets": targets, "orders": len([r for r in recs if r.get("intent_id")]), "skipped": [r for r in recs if r.get("skipped")]}

    def mark_trend(self, pos, c, judged_on, by="15:30 1분봉"):
        """11J·11J 보완 3: 추세 매도 대상이 되면 매도 의무(종목 코드 단위)를 걸고 다음 날 시가 매도 목록에 넣는다 — 계좌 보유가 0 이 될 때까지 빠지지 않음.
        뒤에 판정이 바뀌어도(trend_sell_next_open False) 그대로 (백테스트: 신호가 나면 다음 날 시가에 팔고, 못 팔면 그다음 날)"""
        why = f"추세 매도 ({judged_on} 종가 < 20일선, {by})"
        pos.setdefault("sell_duty", {}).setdefault(c, O.new_duty("trend", f"{judged_on} {self.now():%H:%M:%S}", why))
        pos["next_open_sells"].setdefault(c, {"reason": why, "since": judged_on, "attempts": 0, "last_status": None})

    def carry_duties(self, pos, acct, when):
        """11J 보완 3 (15:35·15:40): 다음 날 시가 매도 목록 = 계좌 보유가 남은 매도 의무 종목. acct = 성공한 계좌 조회 (보유 0 은 R1 규칙,
        15:35 와 15:40 두 번 0 이면 끝), None(조회 실패)이면 목록을 지우지 않고 의무 종목만 더한다. 체결 수량은 쓰지 않는다. 반환: 끝난 의무"""
        nos, today = pos["next_open_sells"], str(self.day.date.date())
        ended = O.end_duties(pos, acct, self.now())[0] if acct is not None else []
        for c, du in pos["sell_duty"].items():
            q = "계좌 조회 실패" if acct is None else (f"계좌 {int(acct[c]['qty'])}주 남음" if int((acct.get(c) or {}).get("qty", 0)) > 0
                                                    else "계좌 보유 0 한 번 (다음 확인에서 다시 0 이면 끝)")
            last = [x for x in self.om.records if x["side"] == "sell" and x["code"] == c and x.get("date") == today] if self.om else []
            e = nos.setdefault(c, {"since": today, "attempts": 0})
            e.update(reason=f"{when} {'손절' if du.get('kind') == 'stop' else '추세 매도'} 의무 ({du.get('since')} {du.get('reason')}), {q} -> 다음 날 시가",
                     last_status=(last[-1].get("fill_status") or last[-1]["status"]) if last else e.get("last_status"),
                     last_intent=last[-1]["intent_id"] if last else e.get("last_intent"))
        if ended:
            self.day.err(f"{when} 매도 의무 끝 (계좌 보유 0): {ended}")
        return ended

    def _note_sells(self, nos, recs):
        for r in recs:
            c = r.get("code")
            if c in nos:
                nos[c].update(attempts=int(nos[c].get("attempts") or 0) + 1, last_status=r.get("status") or r.get("skipped"),
                              last_try=self.now().strftime("%Y-%m-%d %H:%M:%S"), last_intent=r.get("intent_id"))

    def trend_new_targets(self):
        """확인 리뷰 N3: 오늘 아직 추세 매도 의도가 하나도 없는 대상 (복구로 생긴 trend_sell_next_open, 목록에 새로 들어온 종목)"""
        pos = self.positions()
        targets = [c for c in pos["next_open_sells"] if c not in P.review_codes(pos)]  # 11K 보완 3: 수동 확인 중이면 안 보냄
        sent = {r["code"] for r in self.om.records if r.get("stage") == "trend_sell" and r.get("date") == self.om.date()}
        return [c for c in dict.fromkeys(targets) if c not in sent]

    def trend_send(self, codes, why):
        """08:30 ~ 08:59 안에서 추세 매도를 (다시) 보냄: NOT_SENT(다시 보내도 됨)로 확정된 것 + 새 대상 (계좌 조회 기준 수량,
        같은 날 같은 종목 중복은 의도 기록으로 막음)"""
        held = self.broker.account()["holdings"]
        pos = self.positions()
        nos = pos["next_open_sells"]
        O.end_duties(pos, held, self.now())
        go = [c for c in codes if c in nos and c not in P.review_codes(pos)]
        recs = self.om.trend_sells(go, O.duty_inputs(pos, go, held), kind_of=lambda c: pos["held"].get(c, {}).get("kind", "본래"))
        self._note_sells(nos, recs)
        save_positions(pos)
        self.day.err(f"추세 매도 ({why}) {codes}: {[r.get('status') or r.get('skipped') for r in recs]}")
        return recs

    def stop_minute(self, detect=True):
        """1분 손절 감시 (11J, 11J 보완 3, orders.stop_cycle): ① 계좌 조회(429 재시도 없음, 보유 0 이면 의무 끝) -> ② 매도 의무(종목 코드 단위:
        손절 진행 중·추세 매도) = 계좌 매도 가능 수량으로 시장가, 계좌 조회 실패면 걸린 매도가 없을 때만 보유 파일 수량 -> ③ 새 손절 감지
        (시세 조회, 감지 창은 이 감시를 시작한 시각, 새 의무는 주문 전에 positions.json 에 먼저 저장). 체결 조회는 하지 않는다.
        HTTP 요청 하나는 STOP_HTTP_TIMEOUT(5초), 시세 조회 기다림은 STOP_BUDGET(10초) 안. detect False(15:20 뒤 동시호가)면 ③ 없이"""
        d = self.day
        caller = self.quote_caller()

        def quote(codes, deadline=None):
            _, snap, _ = Q.snapshot(caller, codes, deadline=deadline, timeout=STOP_HTTP_TIMEOUT)
            px, stale = fresh_prices(snap, d.date)  # 11K 4: 오늘 체결 시세만 손절 판단에 (빠진 종목은 다음 분 감시에서 다시 조회)
            if stale:
                self.note_stale(stale, "손절 감시")
            return px
        pos = self.positions()
        if not detect and not pos.get("sell_duty"):
            return {"skipped": "동시호가: 매도 의무 없음"}

        def save_trigger(new):
            rlog(f"손절 진행 중 표시 (주문 전 저장): {new}", "WARN")
            save_positions(pos)
        duty_state = {c: x.get("zero_at") for c, x in pos.get("sell_duty", {}).items()}
        with self.broker.http_timeout(STOP_HTTP_TIMEOUT):
            r = O.stop_cycle(self.om, self.watch, pos, quote, lambda: self.broker.balance(quick=True), self.now(), self.mismatch_seen,
                             detect=detect, budget=STOP_BUDGET, on_trigger=save_trigger)
        if r.get("qty_changed"):  # 11K 보완 3: 이번 분 매도·새 손절 감지에서 뺀 수량 변화 종목 -> 수동 확인 필요로 저장
            self.mark_qty_review(pos, r["qty_changed"], "장중 손절 감시", r.get("quotes"))
            save_positions(pos)
        sent = [x for x in r["orders"] if x.get("intent_id")]  # 보낸 것만 기록 (걸린 주문 대기 같은 건너뜀은 매 분 쓰지 않음)
        if sent or duty_state != {c: x.get("zero_at") for c, x in pos.get("sell_duty", {}).items()}:  # 의무 끝·보유 0 기록·지움도 저장
            self._note_sells(pos["next_open_sells"], sent)
            save_positions(pos)
        if r["duty_zero"]:
            rlog(f"매도 의무 종목이 계좌 응답에 보유 0 (한 번, 의무 유지 — 1분 이상 뒤 다시 0 이면 끝): {r['duty_zero']}", "WARN")
        if r["duty_ended"]:
            rlog(f"매도 의무 끝 (계좌 보유 0 두 번 연속): {r['duty_ended']}")
        t = self.now()
        if t.minute % 10 == 0 or not r["account_ok"]:
            gaps = {c: (r["quotes"][c] / p["stop_price"] - 1) * 100 for c, p in pos.get("held", {}).items()
                    if p.get("stop_price") and r["quotes"].get(c)}
            near = min(gaps.items(), key=lambda x: x[1]) if gaps else None
            rlog(f"손절 감시: 보유 {len(pos.get('held', {}))}종목, 시세 {len(r['quotes'])}, 계좌 조회 {'정상' if r['account_ok'] else '실패 ' + str(r['account_error'])}"
                 + (f", 손절가에 가장 가까운 종목 {near[0]} (+{near[1]:.1f}%)" if near else ""), "INFO" if r["account_ok"] else "WARN")
        with d.lock:
            if r["mismatch"] or r["not_in_account"]:
                d.s.setdefault("holding_mismatch", []).append({"time": self.now().strftime("%H:%M:%S"), "diff": r["mismatch"],
                                                               "not_in_account": r["not_in_account"]})
            d.s["account_watch"] = self.watch.summary()
            d.save()
        if (r["mismatch"] or r["not_in_account"]) and "holding_mismatch" not in (d.s.get("buy_block") or []):
            self.block_buys("holding_mismatch", f"장중 보유 불일치 {r['mismatch']} / 계좌에 없음 {r['not_in_account']} -> 그날 신규 매수 안 함 (B3)",
                            f"[보유 불일치] 신규 매수 중단: {r['mismatch']} / 계좌에 없음 {r['not_in_account']}")
        return r

    def note_stale(self, stale, where):
        """11K 4: 오늘 것이 아닌 시세를 데이터 이상으로 집계 (그날 상태 stale_quotes: 종목별 횟수·처음·마지막·이유)"""
        d, hms = self.day, self.now().strftime("%H:%M:%S")
        with d.lock:
            sq = d.s.setdefault("stale_quotes", {})
            new = {c: w for c, w in stale.items() if c not in sq}
            for c, why in stale.items():
                e = sq.setdefault(c, {"n": 0, "first": hms, "where": where})
                e.update(n=e["n"] + 1, last=hms, reason=why)
            d.save()
        if new:  # 같은 종목은 처음 한 번만 상주 로그 (거래정지 종목이 매 분 남지 않게)
            rlog(f"오래된 시세 ({where}) -> 계산·판단에서 뺌: {new}"[:300], "WARN")

    def snapshot_and_plan(self):
        d = self.day
        if not self.prep_ok or self.pool is None:
            raise S.StepFailed("준비(모델·일봉 메모리) 안 됨 -> 스냅샷 계산 못 함")
        caller = Caller("prod", 5)
        try:
            raw, snap, srec = Q.snapshot(caller, sd.universe_codes())
            idx_vals, _ = Q.index_now(caller)
            if srec["returned"] < 0.95 * srec["codes"]:
                raise RuntimeError(f"응답 {srec['returned']} / {srec['codes']}")
        except Exception as e:  # noqa: BLE001
            alert(f"[15:18 스냅샷 실패] {e!r}"[:300])
            d.err(f"스냅샷 실패 -> 다시 시도, 끝내 실패하면 신규 매수 안 함: {e!r}"[:300])
            raise S.StepFailed(f"스냅샷 조회 실패: {e!r}"[:200])
        ok, vinfo = S.validate_snapshot(snap, d.date)
        if not ok:
            alert(f"[15:18 스냅샷 실패] {vinfo.get('reason')} (휴장일 또는 시세 이상)")
            raise S.StepFailed(f"스냅샷 확인 실패: {vinfo.get('reason')}", vinfo)
        os.makedirs(SNAP_DIR, exist_ok=True)
        snap_file = os.path.join(SNAP_DIR, f"{d.date.date()}.parquet")
        pd.concat([raw.reset_index(drop=True), snap.add_prefix("p_").reset_index(drop=True)], axis=1).to_parquet(snap_file, index=False)
        import run_day as R
        bad = R.snap_anomalies(snap, raw, d.date)     # 확인 리뷰 27: 응답에는 있는데 데이터 이상으로 계산 입력에서 빼는 종목
        rows = R.snap_rows(snap, raw, d.date)
        try:
            res = E.run_day(self.pool, d.date, rows, self.ctx["index"], idx_vals, self.ctx["boosters"], self.ctx["cols"], self.ctx["ledgers"])
        except E.PoolError as e:  # Codex 22: 계산 프로세스 장애 -> 신규 매수만 막고 주 반복은 계속, 준비는 다시
            self.block_buys("pool_error", f"점수 계산 프로세스 장애 -> 그날 신규 매수 안 함 (Codex 22): {e!r}", f"[계산 프로세스 장애] 오늘 신규 매수 안 함: {e!r}")
            self.prep_ok = False
            self.next_prep_try = self.now()
            raise S.StepFailed(f"점수 계산 실패: {e!r}"[:200])
        # Codex 27·확인 리뷰 27: 당일 실패 = 스냅샷 응답에 없는 종목 + 응답에는 있지만 데이터 이상(거래량 > 0 인데 가격 0·결측, 상·하한 밖)으로
        # 계산 입력에서 뺀 종목 + 계산 오류 종목. 거래량 0(거래정지)은 백테스트에서도 대상이 아니라 실패로 세지 않는다. 전날 대상 후보면 신규 매수 안 함
        amt = self.ctx.get("amt20") or {}
        loaded = set(amt) - set((self.ctx.get("excluded") or {}).get("fetch_error", []))
        got = set(snap.code)
        fails = [{"code": c, "kind": "snapshot_missing", "amt20": amt.get(c)} for c in sorted(loaded - got)]
        fails += [{"code": c, "kind": "data_anomaly", "amt20": amt.get(c), "reason": why} for c, why in sorted(bad.items())]
        fails += [{"code": c, "kind": "engine_error", "amt20": amt.get(c), "error": m} for c, m in res.get("errors", [])]
        cf = self._candidate_fail(fails, "스냅샷·계산 실패") if fails else {"failures": 0}
        fv = filter_valid(res["filter"])
        fok = bool(res["filter"]["filter_ok"]) and fv  # 11K 보완 2: 유효한 지수로 계산한 통과만 통과
        if not fv:
            res["filter"]["filter_ok"] = False
            self.block_buys("filter_invalid", f"시장 필터 입력 비정상 ({res['filter'].get('filter_invalid')}) -> filter_ok False, 그날 신규 매수 없음 (11K 보완 2)",
                            f"[시장 필터 입력 비정상] 전날 코스피 종가 {res['filter'].get('prev_kospi_close')}, 120일선 {res['filter'].get('prev_kospi_ma120')}: "
                            f"{res['filter'].get('filter_invalid')} -> 오늘 신규 매수 없음")
        name = "strategy" if fok else "full"
        kind = "본래" if fok else "점검용"
        try:
            a = self.broker.account()
            held, orderable, d2 = a["holdings"], a["orderable"], a["d2"]
        except Exception as e:  # noqa: BLE001  (15:18 주문 계획 때 계좌 조회 실패면 그날 신규 매수 안 함)
            self.block_buys("plan_account_fail", f"15:18 주문 계획 계좌 조회 실패 -> 그날 신규 매수 안 함 (6장): {e!r}",
                            f"[15:18 계좌 조회 실패] 오늘 신규 매수 안 함: {e!r}")
            held, orderable, d2 = {}, 0.0, 0.0
        diff = self.holding_diff(held) if held or self.positions().get("held") else {}
        if diff and orderable:
            self.block_buys("plan_holding_mismatch", f"15:18 보유 대조 불일치 {diff} -> 그날 신규 매수 안 함 (B3)",
                            f"[보유 불일치] 15:18 주문 계획: {diff} -> 오늘 신규 매수 안 함")
        px = dict(zip(snap.code, snap.close))
        # Codex 24: 총자산 = D+2 추정예수금(d2_entra) + 보유 수량 x 15:19 가격. 주문가능금액(ord_alow_amt)은 증거금 확인(현금 부족 건너뜀)에만
        equity = d2 + sum(h["qty"] * px.get(c, h["cur_price"]) for c, h in held.items())
        if fok and not jload(check_mode_path(), {}).get("ended") and held is not None and d2:
            self.end_check_mode(equity, held, px)  # 11K 8: 첫 필터 통과일 = 점검 모드 영구 종료
        os.makedirs(os.path.join(LOG_DIR, "scores"), exist_ok=True)
        res["eligible"][["code", "score"]].to_parquet(os.path.join(LOG_DIR, "scores", f"{d.date.date()}.parquet"), index=False)
        upper, uinfo = upper_limits(raw, set(res["ledgers"][name]["top3"].code), px)
        # 11J 보완 3: 매도 의무가 남은 종목은 보유 중으로 본다 (계좌에서 0 이 되기 전에는 다시 사지 않음 = 같은 날 손절 후 재매수 막음)
        pos_now = self.positions()
        review = P.review_codes(pos_now)  # 11K 보완 3: 수동 확인 중인 종목은 다시 사지 않음 (보유 중으로 봄)
        duty = set(pos_now.get("sell_duty", {})) | review
        plan = O.p4_plan(res["ledgers"][name]["top3"], "raw_close", equity, orderable, set(held) | duty, kind, upper=upper)  # 11J: 지정가 상한 = 상한가
        for p in plan:
            if p["code"] in review:
                p["skip"] = "수동 확인 필요 (체결로 설명 안 되는 보유 수량 변화)"
            elif p["code"] in duty and p["code"] not in held:
                p["skip"] = "보유 중 (매도 의무 남음)"
        if uinfo["fallback"] or uinfo["none"]:
            alert(f"[상한가 확인] 스냅샷 상한가 없음·비정상 -> 기준가 x 1.30 호가 내림 {uinfo['fallback']}, 기준가도 없음(매수 안 함, 데이터 이상) {uinfo['none']}"[:300])
        self.ctx.update(res=res, plan=plan, snap_file=snap_file, idx_vals=idx_vals, rows=rows)
        live = {"filter": res["filter"], "plan_ledger": name, "kind": kind, "plan": plan, "equity_1519": equity, "cash": orderable,
                "equity_basis": {"deposit_field": "d2_entra (D+2 추정예수금)", "d2": d2, "holdings_value_at": "15:19 현재가",
                                 "margin_field": "ord_alow_amt (주문가능금액)", "orderable": orderable},
                "ledgers": {n: [{"code": r.code, "score": float(r.score), "price_1519": float(r.raw_close)} for r in res["ledgers"][n]["top3"].itertuples()]
                            for n in NAMES}, "snap_file": snap_file, "index_values": idx_vals, "ledger_before": self.ctx["ledgers"],
                "snapshot_check": vinfo, "data_version": self.ctx.get("data_version"), "candidate_failures": cf,
                "factor_default_n": res.get("factor_default_n"), "vscale_default_n": res.get("vscale_default_n"), "upper_limit_check": uinfo}
        d.set("live", live)
        prep = self.ctx["prep"]
        log = {"date": str(d.date.date()), "mode": f"resident ({CFG['env']}, 주문 {'켬' if self.broker.allow_orders else '끔'})", "quotes_env": "prod",
               "git_commit": git_commit(), "errors": [], **prep, "ledger_before": self.ctx["ledgers"], "filter": res["filter"],
               "data_version": self.ctx.get("data_version"), "frozen_check": self.ctx.get("frozen"),
               "live": {"snapshot": {**{k: v for k, v in srec.items() if k != "batches"}, "file": snap_file, "rows_used": len(rows), "check": vinfo},
                        "factor_default_n": res.get("factor_default_n"), "vscale_default_n": res.get("vscale_default_n"),
                        "scores_file": os.path.join(LOG_DIR, "scores", f"{d.date.date()}.parquet"),
                        "index_values": idx_vals, "filter": res["filter"], "market": res["market"], "eligible_n": len(res["eligible"]),
                        "ledgers": {n: {"top3": live["ledgers"][n], "busy": res["ledgers"][n]["busy"],
                                        "skipped_busy": res["ledgers"][n]["skipped_busy"],
                                        "open_trades": sum(s["open"] for s in res["ledgers"][n]["ledger_status"])} for n in NAMES},
                        "plan_ledger": name, "plan_kind": kind, "plan": plan, "equity_basis": live["equity_basis"], "timing": {}}}
        jdump(os.path.join(LOG_DIR, f"{d.date.date()}.json"), log)
        return {"ok": True, "top3": [p["code"] for p in plan], "filter_ok": fok}

    def end_check_mode(self, equity, held, px):
        """11K 8 (3B): 첫 필터 통과일에 실행 점검 모드를 영구 종료. 그날 매수 전 총자산(15:19 가격) = 전략 시작 금액. 이후 필터 미통과일은 매수 없음.
        남은 점검용 보유는 규칙대로 청산(손절·추세 매도 그대로)하되 전략 성과에서 분리: 종료 때 점검용 보유 평가액(15:19 가격)을 기준으로 두고,
        정산 때 전략 총자산 = 계좌 총자산 - (점검용 보유 평가액 + 종료 뒤 점검용 매도 대금 - 종료 때 점검용 평가액) (settle 의 strategy_perf)"""
        pos = self.positions()
        chk = {c: {"qty": int(h["qty"]), "value": float(h["qty"] * px.get(c, h["cur_price"]))} for c, h in held.items()
               if h["qty"] > 0 and (pos["held"].get(c) or {}).get("kind") == "점검용"}
        rec = {"ended": str(self.day.date.date()), "time": self.now().strftime("%H:%M:%S"), "start_equity": float(equity),
               "basis": "그날 매수 전 총자산 = D+2 추정예수금 + 보유 x 15:19 가격", "check_holdings": chk,
               "check_value_at_end": float(sum(x["value"] for x in chk.values())), "check_sold": {},
               "note": "이후 필터 미통과일 매수 없음 (영구). 점검용 보유는 규칙대로 청산, 전략 성과에서 분리 (11K 8)"}
        jdump(check_mode_path(), rec)
        self.day.set("check_mode_end", rec)
        alert(f"[점검 모드 종료] {rec['ended']} 첫 필터 통과일: 전략 시작 금액 {equity:,.0f} (매수 전 총자산), 이후 필터 미통과일 매수 없음. "
              f"남은 점검용 보유 {sorted(chk)} (평가 {rec['check_value_at_end']:,.0f}) 는 규칙대로 청산, 전략 성과에서 분리")
        return rec

    def strategy_perf(self, equity, held_prices):
        """11K 8: 점검 모드 종료 뒤 전략 성과 (점검용 거래 분리). held_prices = {code: (수량, 현재가)}. 종료 전이면 None"""
        cm = jload(check_mode_path(), {})
        if not cm.get("ended"):
            return None
        today, rate = str(self.day.date.date()), commission_rate()
        tax = sb_tax(self.day.date.year)
        if today >= cm["ended"]:
            # 11K 보완 5: 종료일에 종료(15:18 계획) 전에 보낸 점검용 매도 대금은 이미 시작 금액(그때 D+2 추정예수금)에 들어 있으므로 빼지 않는다 (이중 차감 방지)
            # 보완 2 (4): 나누는 기준은 체결 시각 (없으면 주문 시각·생성 시각). 종료 시각 전에 체결된 매도 대금은 시작 금액(D+2)에 이미 들어 있음
            end = hhmmss(cm.get("time"))
            after = (lambda r: True) if today > cm["ended"] else (lambda r: hhmmss(r.get("fill_time") or r.get("order_time") or r.get("created")) >= end)
            sold = sum(float(r["fill_price"]) * int(r["filled_qty"]) for r in self.om.records
                       if r.get("date") == today and r["side"] == "sell" and r.get("kind") == "점검용" and int(r.get("filled_qty") or 0) > 0 and r.get("fill_price")
                       and after(r))
            cm.setdefault("check_sold", {})[today] = round(sold * (1 - rate - tax), 1)
            jdump(check_mode_path(), cm)
        pos = self.positions()
        cval = sum(q * px for c, (q, px) in held_prices.items() if (pos["held"].get(c) or {}).get("kind") == "점검용")
        sold_all = sum(cm.get("check_sold", {}).values())
        strat = equity - (cval + sold_all - cm["check_value_at_end"])
        return {"since": cm["ended"], "start_equity": cm["start_equity"], "strategy_equity": round(strat, 1),
                "return_pct": round((strat / cm["start_equity"] - 1) * 100, 3), "check_value_now": round(cval, 1), "check_sold_total": round(sold_all, 1),
                "check_value_at_end": cm["check_value_at_end"], "account_equity": round(equity, 1)}

    def measure_snapshot(self):
        """측정용 스냅샷 (2026-10-22 결정): 15:20:05 에 15:18:30 과 같은 방식(ka10095 전 종목 + ka20001)으로 한 번 더 받아 저장만 한다.
        장 마감 후 15:19 1분봉 종가·15:19 까지 누적 거래량과 비교 (after_close). 주문 계획에는 쓰지 않는다."""
        d = self.day
        caller = Caller("prod", 5)
        t0 = self.now()
        raw, snap, srec = Q.snapshot(caller, sd.universe_codes())
        idx_vals, _ = Q.index_now(caller)
        f = os.path.join(SNAP_DIR, f"{d.date.date()}_measure.parquet")
        pd.concat([raw.reset_index(drop=True), snap.add_prefix("p_").reset_index(drop=True)], axis=1).to_parquet(f, index=False)
        d.set("measure_snapshot", {"file": f, "start": t0.strftime("%H:%M:%S.%f")[:-3], "seconds": srec["seconds"], "returned": srec["returned"],
                                   "index_values": idx_vals})
        return {"file": f, "start": t0.strftime("%H:%M:%S")}

    def compare_measure(self, bars, targets, d=None):
        """15:20:05 스냅샷 vs 15:19 봉 종가·15:19 까지 누적 거래량 (bars: code -> 그날 15:30 이하 1분봉), exp_cntr_pric 도 비교"""
        d = d or self.day
        m = d.s.get("measure_snapshot")
        if not m:
            return None
        s = pd.read_parquet(m["file"])
        rows = []
        for r in s.itertuples():
            b = bars.get(r.p_code)
            if b is None or b.empty or not (r.p_volume > 0):
                continue
            upto = b[b.ts.str[8:12] <= "1519"]
            if upto.empty:
                continue
            exp = O.num(getattr(r, "exp_cntr_pric", None))
            rows.append({"code": r.p_code, "target": r.p_code in targets, "price": r.p_close, "vol": r.p_volume, "c1519": float(upto.close.iloc[-1]),
                         "v1519": float(upto.volume.sum()), "c1530": float(b.close.iloc[-1]), "exp": exp,
                         "last_bar": upto.ts.iloc[-1][8:12], "cntr_tm": str(getattr(r, "cntr_tm", ""))})
        df = pd.DataFrame(rows)
        if df.empty:
            return {"n": 0}

        def stats(x):
            return {"n": len(x), "price_eq_1519_close_pct": float((x.price == x.c1519).mean() * 100),
                    "vol_eq_1519_cum_pct": float((x.vol == x.v1519).mean() * 100),
                    "both_eq_pct": float(((x.price == x.c1519) & (x.vol == x.v1519)).mean() * 100),
                    "vol_within_0p1_pct": float(((x.vol - x.v1519).abs() / x.v1519 <= 0.001).mean() * 100),
                    "vol_rel_diff_max_pct": float(((x.vol - x.v1519).abs() / x.v1519).max() * 100),
                    "exp_present_pct": float((x.exp > 0).mean() * 100),
                    "exp_eq_1519_close_pct": float((x.exp == x.c1519).mean() * 100), "exp_eq_1530_close_pct": float((x.exp == x.c1530).mean() * 100),
                    "price_eq_1530_close_pct": float((x.price == x.c1530).mean() * 100)}
        out = {"snapshot_start": m["start"], "targets": stats(df[df.target]), "all": stats(df),
               "mismatch_targets_sample": df[df.target & ((df.price != df.c1519) | (df.vol != df.v1519))].head(10).to_dict("records")}
        df.to_csv(os.path.join(LOG_DIR, f"measure_1520_{d.date.date()}.csv"), index=False, encoding="utf-8-sig")
        return out


    def buy_gate(self):
        """신규 매수를 막는 이유 목록 (Codex 16: summary 의 new_buy_ok·buy_skip_reason 도 이 결과)"""
        d = self.day
        why = []
        if not self.ctx.get("prep"):
            why.append("준비(모델·일봉 메모리) 안 됨 (Codex 4)")
        if not self.ctx.get("buy_allowed"):
            why.append("신규 매수 안 함 (장애·불일치·차단)")
        if d.s.get("buy_block"):
            why.append(f"차단 {d.s['buy_block']}")
        if d.done("late_start"):
            why.append(f"늦은 시작 ({d.s['steps']['late_start'].get('started')}) (Codex 4)")
        if d.status("data") == S.FAILED:
            why.append("일봉 반영 실패 (Codex 23)")
        if self.recovery_pending():
            why.append("보유 장부 복구 전 (Codex 8)")
        rec = d.s.get("prev_recovery")
        if rec and not rec.get("finalized"):
            why.append(f"전날 복구 미완료 ({rec.get('days') or rec.get('prev')}, 남은 것 {rec.get('left')}) (확인 리뷰 5)")
        pu = d.s.get("positions_unconfirmed")
        if pu and not pu.get("resolved_at"):
            why.append("거래 정보 미확정 (확인 리뷰 N2)")
        if (rec or {}).get("ambiguous"):
            why.append(f"복구 미확정 {sorted(rec['ambiguous'])} (3차 확인 리뷰 R1·R3)")
        tmp = {c: [k for k in ("entry_temp", "stop_temp", "info_pending", "provisional_new", "ambiguous") if p.get(k)]
               for c, p in self.positions().get("held", {}).items()}
        tmp = {c: v for c, v in tmp.items() if v}
        if tmp:
            why.append(f"임시 거래 정보 보유 {tmp} (확인 리뷰 5·N2)")
        if self.om is not None and getattr(self.om.store, "corrupt", None):
            why.append("의도 파일 손상 (Codex 1)")
        if jload(BLOCK, None):
            why.append(f"신규 매수 중단 파일 {jload(BLOCK, {})}")
        if not d.s.get("live"):
            why.append("스냅샷 없음")
        elif CFG["env"] == "prod" and d.s["live"].get("kind") != "본래":
            why.append("실전 계좌: 필터 미통과일(점검용) 매수 안 함 — 점검용은 모의(env=mock)에서만 (11J)")
        elif d.s["live"].get("kind") != "본래" and jload(check_mode_path(), {}).get("ended"):
            why.append(f"점검 모드 종료 ({jload(check_mode_path(), {}).get('ended')}) 뒤 필터 미통과일: 매수 없음 (11K 8)")
        if not d.done("prev_settle"):
            why.append("전날 미완료 정산 전 (B2)")
        if not d.done("data_check"):
            why.append("15:17 데이터 버전 확인 전 (B6)")
        if not self.ctx.get("frozen", {}).get("ok"):
            why.append("동결 함수 확인 실패 (B7)")
        return why

    def record_buy_decision(self, ok, reason, extra=None):
        d = self.day
        d.set("buy_decision", {"time": self.now().strftime("%H:%M:%S"), "new_buy_ok": bool(ok), "reason": reason,
                               "buy_block": list(d.s.get("buy_block") or []), **(extra or {})})

    def buy(self):
        d = self.day
        why = self.buy_gate()
        if why:
            self.record_buy_decision(False, "; ".join(why))
            return {"sent": 0, "reason": "; ".join(why)}
        if not self.broker.allow_orders:
            self.record_buy_decision(True, "주문 끔: 주문 계획만 기록")
            return {"sent": 0, "reason": "주문 끔 (allow_orders=False): 주문 계획만 기록", "plan": d.s["live"]["plan"]}
        try:  # B4·Codex 24: 주문가능금액과 지정가 x 수량 합 비교, 2차 확인 리뷰 2: 같은 조회의 보유 수량을 의도에 pre_qty 로
            acct = self.broker.account()
            orderable = acct["orderable"]
            pre_qty = {c: int(h["qty"]) for c, h in acct["holdings"].items()}
        except O.FieldError as e:  # Codex 2: 필드 없음·NaN = 응답 형식 이상 -> 그날 매수 안 함 (그대로)
            self.block_buys("buy_deposit_fail", f"매수 전 주문가능금액 조회 실패 -> 그날 신규 매수 안 함: {e!r}", f"[매수 전 예수금 조회 실패] 오늘 매수 안 함: {e!r}")
            self.record_buy_decision(False, f"주문가능금액 조회 실패: {e!r}"[:200])
            return {"sent": 0, "reason": "주문가능금액 조회 실패"}
        except Exception as e:  # noqa: BLE001  (11L 보완 3: 유량 오류·네트워크 = 일시 실패 -> 15:25 까지 매 분 다시, 영구 차단 아님)
            r = dict(d.s.get("buy_account_retry") or {})
            first = not r
            r.update(pending=True, since=r.get("since") or self.now().strftime("%H:%M:%S"), tries=int(r.get("tries", 0)) + 1,
                     minute=self.now().strftime("%H:%M"), last_error=repr(e)[:200])
            d.set("buy_account_retry", r)
            self.record_buy_decision(False, f"매수 전 주문가능금액 조회 실패 (일시) -> {T['no_new_order_after'][:5]} 전까지 매 분 다시: {e!r}"[:200])
            if first:
                alert(f"[매수 전 계좌 조회 실패] {e!r} — {T['no_new_order_after'][:5]} 전까지 매 분 다시 조회해 성공하면 매수"[:300])
            return {"sent": 0, "reason": "주문가능금액 조회 실패 (일시, 다시)", "retry": True}
        if (d.s.get("buy_account_retry") or {}).get("pending"):
            with d.lock:
                d.s["buy_account_retry"].update(pending=False, resolved=self.now().strftime("%H:%M:%S"))
                d.save()
        recs = self.om.place_buys(d.s["live"]["plan"], d.s["live"]["kind"], orderable=orderable, pre_qty=pre_qty)
        sent = sum(1 for r in recs if r.get("intent_id") and r.get("status") != O.NOT_SENT)
        self.record_buy_decision(True, "", {"sent": sent, "orderable": orderable})
        return {"sent": sent, "orderable": orderable, "results": [{k: r.get(k) for k in ("code", "status", "skipped", "intent_id", "cause")} for r in recs]}

    def buy_resend(self, codes):
        """11J 보완 3 N2: 15:21 ~ 15:25 매 분, 보내지 못한(NOT_SENT 다시 보내도 됨: 인증·토큰, 유량 대기) 매수를 다시 보낸다.
        거부 횟수에 안 셈, 같은 날 같은 종목 중복은 의도 기록으로 막음, 주문가능금액은 지금 계좌로"""
        d = self.day
        why = self.buy_gate()
        if why or not self.broker.allow_orders:
            return []
        acct = self.broker.account()
        plan = [p for p in d.s["live"]["plan"] if p["code"] in codes]
        recs = self.om.place_buys(plan, d.s["live"]["kind"], orderable=acct["orderable"], pre_qty={c: int(h["qty"]) for c, h in acct["holdings"].items()})
        d.err(f"매수 다시 보냄 {codes}: {[r.get('status') or r.get('skipped') for r in recs]}")
        return recs

    def close_check(self):
        d = self.day
        codes = [r["code"] for r in self.om.records if r["side"] in ("buy", "sell")]
        caller = self.quote_caller()
        closes, opens = {}, {}
        for c in dict.fromkeys(codes):
            try:
                cv = Q.close_values(Q.bars_today(caller, c, d.date))
            except ValueError:
                cv = None
            if cv:
                closes[c], opens[c] = cv["close"], cv["open"]
        try:
            r = self.om.close_check(close_prices=closes, open_prices=opens)
        except O.FieldError as e:
            alert(f"[정산 필드 확인 실패] {e} — 정산 SUCCEEDED 안 함 (첫 체결 날 실제 필드 확인)"[:300])
            raise
        pos = self.positions()
        try:  # 11J 보완 3: 다음 날 시가 매도 목록 = 계좌 보유가 남은 매도 의무 종목 (조회 실패면 목록을 지우지 않음, 15:40 에 다시)
            acct = self.broker.account()["holdings"]
        except Exception as e:  # noqa: BLE001
            acct = None
            d.err(f"15:35 계좌 조회 실패 -> 다음 날 시가 매도 목록은 지우지 않음 (15:40 에 다시): {e!r}"[:300])
        self.carry_duties(pos, acct, "15:35")
        save_positions(pos)
        d.set("orders", list(self.om.records))
        if not r.get("done"):  # Codex 13: 취소 거부·미확정 원주문·남은 잔량 -> 실패로 두고 다시 확인 (최종 기준은 15:40 계좌)
            raise S.StepFailed(f"15:35 확인 미완료: {r.get('reasons')}", r)
        return r

    def prev_d2(self):
        """전 거래일 15:40 정산의 D+2 추정예수금 (Codex 20 현금 대조 기준)"""
        prev = S.prev_trading_day(self.day.date, self.holidays)
        st = jload(os.path.join(CFG["state_dir"], f"{prev.date()}.json"), {})
        return (st.get("settlement") or {}).get("d2"), str(prev.date())

    def settle(self):
        """15:40 정산 마지막 기준 = 계좌 잔고(kt00018)와 예수금(kt00001). 체결된 매수·매도가 계좌에 반영됐는지 대조 + 현금 대조 (Codex 20)"""
        d = self.day
        last = None
        for _ in range(3):
            try:
                a = self.broker.account()
                break
            except O.FieldError as e:
                alert(f"[정산 필드 확인 실패] 잔고 응답: {e}"[:300])
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2)
        else:
            raise RuntimeError(f"15:40 잔고·예수금 조회 3회 실패: {last!r}")
        held = a["holdings"]
        today = str(d.date.date())
        before = {c: int(p.get("qty", 0)) for c, p in self.positions().get("held", {}).items()}
        expect = dict(before)
        fills = [r for r in self.om.records if r.get("date", today) == today and r["side"] in ("buy", "sell") and r.get("filled_qty")]
        for r in fills:
            expect[r["code"]] = expect.get(r["code"], 0) + (int(r["filled_qty"]) if r["side"] == "buy" else -int(r["filled_qty"]))
        acct = {c: h["qty"] for c, h in held.items() if h["qty"] > 0}
        diff = {c: {"expected": expect.get(c, 0), "account": acct.get(c, 0)} for c in set(expect) | set(acct) if expect.get(c, 0) != acct.get(c, 0)}
        p2, pday = self.prev_d2()
        if p2 is None:
            cash = {"ok": None, "note": f"전 거래일({pday}) D+2 추정예수금 기록 없음 -> 대조 안 함", "actual_d2": a["d2"]}
        else:
            exp, n = P.expected_cash(p2, fills, d.date.year, commission=commission_rate())
            tol = P.cash_tolerance(n)
            cash = {"prev_day": pday, "prev_d2": p2, "expected": round(exp, 1), "actual_d2": a["d2"], "cash_diff": round(a["d2"] - exp, 1),
                    "tolerance": tol, "n_fills": n, "ok": bool(abs(a["d2"] - exp) <= tol), "commission": commission_rate(),
                    "basis": f"전일 D+2 추정예수금 + 당일 체결 금액 ± 수수료 {commission_rate() * 100:.3f}% ({CFG['env']})·매도세 (11K 11)"}
        d.set("cash_check", cash)
        if cash.get("ok") is False:
            alert(f"[현금 차이] D+2 추정예수금 {a['d2']:,.0f} vs 기대 {cash['expected']:,.0f} (차이 {cash['cash_diff']:,.0f}, 허용 {cash['tolerance']:,.0f})")
        rec = {"time": self.now().strftime("%H:%M:%S"), "account": acct, "avg_price": {c: h["avg_price"] for c, h in held.items() if h["qty"] > 0},
               "deposit": a["entr"], "orderable": a["orderable"], "d2": a["d2"], "expected": {c: q for c, q in expect.items() if q}, "diff": diff}
        # 11K 11: 총자산 = D+2 추정예수금 + 보유 x 계좌 현재가, 손익 = 전 거래일 정산 총자산 대비. "실전 수수료였다면" = 손익 + (이 환경 수수료 - 실전 수수료)
        hp = {c: (int(h["qty"]), float(h.get("cur_price") or h.get("avg_price") or 0)) for c, h in held.items() if h["qty"] > 0}
        rec["equity"] = round(float(a["d2"]) + sum(q * px for q, px in hp.values()), 1)
        amt = P.fill_amount(fills)
        rate = commission_rate()
        fee_back = round(amt * (rate - PROD_COMMISSION), 1)
        prev_eq = (jload(os.path.join(CFG["state_dir"], f"{pday}.json"), {}).get("settlement") or {}).get("equity")
        pnl = None if prev_eq is None else round(rec["equity"] - float(prev_eq), 1)
        rec["pnl"] = {"prev_day": pday, "prev_equity": prev_eq, "pnl": pnl, "commission": rate, "fill_amount": round(amt, 1),
                      "fee": round(amt * rate, 1), "fee_prod": round(amt * PROD_COMMISSION, 1), "fee_back": fee_back,
                      "pnl_prod_fee": None if pnl is None else round(pnl + fee_back, 1),
                      "note": "pnl_prod_fee = 실전 수수료(0.015%)였다면 손익 (수수료 차이를 되돌린 값)" + ("" if prev_eq is not None else ", 전 거래일 총자산 기록 없음")}
        try:
            rec["strategy_perf"] = self.strategy_perf(rec["equity"], hp)  # 11K 8
        except Exception as e:  # noqa: BLE001
            rec["strategy_perf"] = {"error": repr(e)[:200]}
        d.set("settlement", rec)
        if diff:
            alert(f"[정산 불일치] 15:40 계좌 vs 기록: {diff} — 계좌 기준으로 보유 갱신, 다음 날 신규 매수는 장 전 대조 결과로")
        return {"diff_n": len(diff), "cash_ok": cash.get("ok")}

    def _res(self, d=None, ctx=None, pool=None):
        """그날 스냅샷 계산 결과 (재시작했으면 저장한 스냅샷과 그날 장부 상태로 다시 계산). 장 마감 후 계산 스레드는 시작 때 잡은 날·ctx·풀을 넘긴다"""
        d, ctx, pool = d or self.day, self.ctx if ctx is None else ctx, self.pool if pool is None else pool
        res = ctx.get("res")
        live = d.s.get("live", {})
        if res is None and live.get("snap_file"):
            if not self.prep_ok or pool is None:
                raise S.StepFailed("준비 안 됨 -> 스냅샷 다시 계산 못 함 (준비 끝나면 다시)")
            import run_day as R
            full = pd.read_parquet(live["snap_file"])
            snap = pd.DataFrame({c[2:]: full[c] for c in full.columns if c.startswith("p_")})
            rows = R.snap_rows(snap, full, d.date)
            res = E.run_day(pool, d.date, rows, ctx["index"], live["index_values"], ctx["boosters"], ctx["cols"], live["ledger_before"])
            ctx.update(res=res, rows=rows)
        return res

    def write_ledger(self, res, live, day=None):
        """장부 갱신 (상주 프로그램만 씀, 단일 실행 잠금 안). 이미 그 날짜로 썼으면 건너뜀. 저장 때 버전 + 1, 읽었을 때 버전과 다르면 거부 (F-07)"""
        day = pd.Timestamp(day if day is not None else self.day.date)
        cur, ver = ledger_read(LEDGER) if os.path.exists(LEDGER) else ({}, 0)
        if (cur.get("as_of") or "") >= str(day.date()):
            return f"이미 {cur.get('as_of')} 날짜로 갱신됨"
        fok = res["filter"]["filter_ok"]
        new = {}
        for n in NAMES:
            add = [x["code"] for x in live["ledgers"][n]] if (n == "full" or fok) else []
            new[n] = [{"code": s["code"], "entry": s["entry"]} for s in res["ledgers"][n]["ledger_status"] if s["open"]] + \
                     [{"code": c, "entry": str(day.date())} for c in add]
        expected = self.ctx.get("ledger_version", ver) if day == self.day.date else ver
        try:
            v = ledger_write({"as_of": str(day.date()), **new}, expected, LEDGER)
        except LedgerConflict as e:
            alert(f"[장부 저장 거부] {e}")
            raise
        self.ctx["ledger_version"] = v
        return f"갱신 (버전 {v})"

    def ac_ledger(self):
        res = self._res()
        return {"ledger": self.write_ledger(res, self.day.s.get("live", {})) if res is not None else "스냅샷 없음 -> 장부 그대로"}

    def ac_positions(self):
        """거래 정보 확정 (Codex 5·6·7·12·25, 확인 리뷰 N2). 15:40 settle 이 계좌 수량을 확정한 뒤, 진입일·손절가·trade_id·분류를 확정한다.
        - 기준: 그날 처음 이 단계를 시작할 때의 보유 장부(positions_baseline). 다시 돌려도 같은 기준에서 다시 계산 (체결이 늦게 들어오면 다시 확정).
        - 체결 조회(ka10076)를 다시 해서 체결 수량을 채우고, 기준 수량 + 매수 체결 - 매도 체결 = 계좌 수량 인지 확인.
          체결 조회 실패 또는 설명 안 되는 종목이 있으면 수량은 계좌로 맞추되 그 종목은 미확정(info_pending, 새 종목은 잠정 거래 provisional_new +
          임시 손절가)으로 저장하고 FAILED (장 마감 후 60초마다 계속, 다음 날 아침 전날 복구에서도). 미확정 동안 신규 매수 안 함, 손절은 임시 손절가로.
        - 같은 종목을 청산한 뒤 다시 샀으면 새 거래. 추세 매도 판정은 판정일 > 진입일 일 때만. 모든 보유 종목에 손절가·진입일이 있어야 SUCCEEDED"""
        import copy
        d = self.day
        today = str(d.date.date())
        st_ = d.s.get("settlement") or {}
        acct = st_.get("account")
        if acct is None:
            raise S.StepFailed("15:40 정산 결과 없음")
        avg = st_.get("avg_price") or {}
        pos = self.positions()
        if not d.s.get("positions_baseline"):
            d.set("positions_baseline", {"held": copy.deepcopy(pos["held"]), "trade_seq": pos.get("trade_seq", 0),
                                         "time": self.now().strftime("%H:%M:%S")})
        base = d.s["positions_baseline"]
        pos["held"], pos["trade_seq"] = copy.deepcopy(base["held"]), base["trade_seq"]
        old = {c: dict(p) for c, p in pos["held"].items()}
        fills_ok, ferr = True, None
        try:
            self.om.refresh_fills()
        except Exception as e:  # noqa: BLE001  (조회 실패·필드 오류 = 거래 정보 미확정)
            fills_ok, ferr = False, repr(e)[:200]
        bought, sold, buy_q = {}, {}, {}
        for r in self.om.records:
            if r.get("date", today) != today or not r.get("filled_qty") or r["side"] not in ("buy", "sell"):
                continue
            if r["side"] == "buy":
                bought[r["code"]] = r
                buy_q[r["code"]] = buy_q.get(r["code"], 0) + int(r["filled_qty"])
            else:
                sold[r["code"]] = sold.get(r["code"], 0) + int(r["filled_qty"])
        touched = {r["code"] for r in self.om.records if r.get("date", today) == today and r["side"] in ("buy", "sell")
                   and r["status"] in (O.ACCEPTED, O.UNKNOWN, O.SENDING)}
        unexplained = {}
        for c in set(old) | set(acct) | set(buy_q) | set(sold):
            b0 = int((old.get(c) or {}).get("qty", 0))
            if b0 + buy_q.get(c, 0) - sold.get(c, 0) != int(acct.get(c, 0)):
                unexplained[c] = {"before": b0, "buy": buy_q.get(c, 0), "sell": sold.get(c, 0), "account": int(acct.get(c, 0))}
        # 11K 보완 3: 오늘 그 종목 의도가 하나도 없는데 수량이 다르면(둘 다 > 0) = 체결로 설명 안 되는 변화 -> 수동 확인 (미확정·임시 손절가로 만들지 않음)
        mine = {r["code"] for r in self.om.records if r.get("date", today) == today and r.get("status") not in (O.NOT_SENT, O.REJECTED)}
        review_new = {c: {"file": u["before"], "account": u["account"]} for c, u in unexplained.items()
                      if u["before"] > 0 and u["account"] > 0 and c not in mine}
        review_all = set(review_new) | {c for c, p in old.items() if P.in_review(p)}
        pending = (set(unexplained) | (touched & (set(acct) | set(old)) if not fills_ok else set())) - review_all
        caller = self.quote_caller()
        cvs = {}
        for c in acct:
            try:
                cv = Q.close_values(Q.bars_today(caller, c, d.date))
            except ValueError:
                cv = None
            if cv:
                cvs[c] = cv
        j = self.judge(list(acct), d.date, cvs)
        problems, temp, new_trades = [], [], []
        if review_new:
            self.mark_qty_review(pos, review_new, "15:40 보유 갱신")
        for c, q in acct.items():
            o = old.get(c) or {}
            if c in review_all:  # 수동 확인 중: 수량·평균단가만 계좌, 손절가·진입가·추세 판단 그대로
                pos["held"][c]["qty"], pos["held"][c]["avg_price"] = int(q), avg.get(c, pos["held"][c].get("avg_price"))
                continue
            if c in pending:
                # 기준에 없던 종목이거나 (체결로 보아) 청산 뒤 다시 산 종목 = 잠정 새 거래, 그 밖은 기존 거래 정보 유지. 수량은 계좌
                if not o or (c in bought and sold.get(c, 0) >= int(o.get("qty", 0))):
                    p = P.new_trade(pos, c, today, kind=(bought.get(c) or {}).get("kind", "미확인"), provisional_new=True)
                else:
                    p = pos["held"][c]
                p["info_pending"], p["pending_since"] = True, today
                p["qty"], p["avg_price"] = int(q), avg.get(c, p.get("avg_price"))
                if not p.get("stop_price") or p.get("provisional_new"):
                    s_, basis = P.temp_stop(c, p.get("avg_price"), today)
                    p.update(stop_price=float(s_), stop_temp=True, stop_basis=basis)
                    temp.append(c)
                jc = j.get(c)
                if jc and not p.get("provisional_new"):
                    p.update(close_1530=jc["close"], judged_on=today, judged_by="15:30 1분봉")
                    p["trend_sell_next_open"] = bool(jc["trend"]) if today > p["entry_date"] else False
                    if p["trend_sell_next_open"]:
                        self.mark_trend(pos, c, today)
                continue
            is_new = c in bought and (not o or (o.get("entry_date") != today and sold.get(c, 0) >= int(o.get("qty", 0))))
            if is_new:  # Codex 7: 새 거래 (거래 번호·진입일·손절가·분류 새로)
                p = P.new_trade(pos, c, today, kind=bought[c].get("kind", "본래"), entry_price=bought[c].get("fill_price"))
                new_trades.append(p["trade_id"])
            else:
                p = pos["held"].setdefault(c, {})
            p["qty"], p["avg_price"] = int(q), avg.get(c, p.get("avg_price"))
            jc = j.get(c)
            if p.get("entry_date") == today and (not p.get("stop_price") or p.get("stop_temp")):
                if jc:
                    p.update(stop_price=jc["stop"], stop_temp=False, stop_basis=f"진입일 15:30 1분봉 종가 - 1.7 x ATR20 ({jc['basis']})")
                else:
                    problems.append(f"{c} 오늘 매수: 15:30 1분봉·일봉 없어 손절가 계산 못 함")
            if not p.get("entry_date"):
                p.update(entry_date=today, entry_temp=True, kind=p.get("kind", "미확인"))
            if not p.get("stop_price") and not (p.get("entry_date") == today and not p.get("entry_temp")):
                s_, basis = P.temp_stop(c, p.get("avg_price"), today)
                p.update(stop_price=float(s_), stop_temp=True, stop_basis=basis)
                temp.append(c)
            if jc:
                p.update(close_1530=jc["close"], judged_on=today, judged_by="15:30 1분봉")
                p["trend_sell_next_open"] = bool(jc["trend"]) if today > p["entry_date"] else False  # Codex 25: 진입일 당일은 판정 안 함
                if p["trend_sell_next_open"]:
                    self.mark_trend(pos, c, today)
            else:
                problems.append(f"{c} 15:30 1분봉 없음 -> 추세 판정 못 함")
        for c in [c for c in pos["held"] if c not in acct]:
            pos["held"].pop(c)
        for c in [c for c in pos["next_open_sells"] if c not in acct]:
            pos["next_open_sells"].pop(c)
        self.carry_duties(pos, {c: {"qty": int(q)} for c, q in acct.items()}, "15:40")  # 11J 보완 3: 계좌 보유 0 = 의무 끝, 남으면 다음 날 시가
        P.mark_synced(pos, today, self.now().strftime("%H:%M:%S"), "15:40 보유 갱신")  # 11K 보완 3: 이 저장 뒤로는 오늘 의도가 수량에 반영됨
        save_positions(pos)
        if pending:
            first = not d.s.get("positions_unconfirmed") or d.s["positions_unconfirmed"].get("resolved_at")
            d.set("positions_unconfirmed", {"time": self.now().strftime("%H:%M:%S"), "fills_ok": fills_ok, "fills_error": ferr,
                                            "unexplained": unexplained, "codes": sorted(pending), "resolved_at": None})
            if first:
                alert(f"[거래 정보 미확정] 체결 조회 {'성공' if fills_ok else '실패'}, 설명 안 되는 수량 {unexplained} — 수량은 계좌 기준, "
                      f"{sorted(pending)} 는 임시 손절가, 신규 매수 안 함, 장 마감 후 계속 다시 확인"[:300])
            raise S.StepFailed(f"거래 정보 미확정: 체결 조회 {'성공' if fills_ok else '실패: ' + str(ferr)}, 설명 안 됨 {unexplained}",
                               {"pending": sorted(pending), "unexplained": unexplained})
        if d.s.get("positions_unconfirmed") and not d.s["positions_unconfirmed"].get("resolved_at"):
            with d.lock:
                d.s["positions_unconfirmed"]["resolved_at"] = self.now().strftime("%H:%M:%S")
                d.save()
            alert(f"[거래 정보 확정] {today} 늦게 들어온 체결로 다시 확정: 새 거래 {new_trades}")
        if temp:
            alert(f"[임시 손절가] 손절가 없는 보유 종목 {temp}: 계좌 평균단가 - 1.7 x ATR20")
        missing = [c for c, p in pos["held"].items() if not p.get("stop_price") or not p.get("entry_date")]
        if problems or missing:  # Codex 6: 손절가·진입일이 빠진 종목이 있으면 실패 -> 다시
            raise S.StepFailed(f"보유·손절 갱신 미완료: {problems} / 손절가·진입일 없음 {missing}", {"problems": problems, "missing": missing})
        return {"held": len(pos["held"]), "new_trades": new_trades, "temp_stops": temp, "next_open_sells": list(pos["next_open_sells"])}

    @property
    def close_thread(self):
        return (self.close_job or {}).get("thread")

    @property
    def close_result(self):
        return (self.close_job or {}).get("result")

    def make_close_job(self):
        """작업 기록: 날짜·세대 번호·취소 신호·상한 시각과, 시작 때의 날(Day)·ctx·풀 (날짜가 바뀌어도 이전 작업이 오늘 것을 쓰지 않게)"""
        d = self.day
        self.close_gen += 1
        return {"gen": self.close_gen, "date": str(d.date.date()), "cancel": threading.Event(), "why": None,
                "deadline": pd.Timestamp(f"{d.date.date()} {T.get('close_calc_until', '16:20:00')}"), "thread": None, "result": None,
                "day": d, "ctx": self.ctx, "pool": self.pool, "out": {}}

    def start_close_calc(self):
        """11I (모의 1일차): 15:32 부터 별도 스레드로 무거운 계산. 주 반복(하트비트·정산·보유 갱신)은 기다리지 않는다.
        계산이 필요 없는 날(스냅샷 없음)은 바로 결과. 실패하면 60초 뒤 다시"""
        if self.close_job is not None:
            return
        if self.close_next_try is not None and self.now() < self.close_next_try:
            return
        job = self.make_close_job()
        self.close_job = job
        if not self.close_calc_needed():
            job["result"] = {"note": "스냅샷 없음 -> 계산 없음", "date": job["date"], "gen": job["gen"]}
            return

        def work():
            try:
                r = self.close_calc(job)
            except CloseCalcStop as e:  # 취소·상한 초과: 그때까지 계산한 것 + 중단 이유
                r = {**job["out"], "aborted": str(e)}
            except Exception as e:  # noqa: BLE001
                r = {"error": f"{type(e).__name__}: {e}"[:300], "trace": traceback.format_exc()[-600:]}
            r.update(date=job["date"], gen=job["gen"])
            job["result"] = r
        rlog(f"장 마감 후 계산 시작 (세대 {job['gen']}, {job['date']}, 상한 {job['deadline']:%H:%M}): 15:30 1분봉 전 종목, 15:30 상위 3, 15:20 측정, 동결 코드 대조", "INFO")
        job["thread"] = threading.Thread(target=work, daemon=True, name=f"close_calc-{job['gen']}")
        job["thread"].start()

    def close_ready(self):
        """지금 날짜·세대의 계산 결과가 있으면 True. 날짜·세대가 다른 결과는 버리고 로그만 남긴다 (11I 보완)"""
        job = self.close_job
        if job is None or job["result"] is None or (job["thread"] is not None and job["thread"].is_alive()):
            return False
        r, today = job["result"], str(self.day.date.date())
        if job["date"] != today or r.get("date") != today or r.get("gen") != job["gen"]:
            rlog(f"장 마감 후 계산 결과 버림: 작업 {r.get('date')} 세대 {r.get('gen')} vs 지금 {today} 세대 {job['gen']}", "WARN")
            self.close_job = None
            return False
        job["thread"] = None  # 끝난 스레드 참조 회수
        return True

    def retire_close_job(self, why):
        """날짜가 바뀌거나 16:30 이 지나면: 지금 작업에 취소 신호를 보내고, 아직 돌면 정리 목록으로 (끝나면 그 풀을 닫고 참조 회수, reap_close)"""
        job, self.close_job, self.close_next_try = self.close_job, None, None
        if job is None:
            return None
        job["why"] = why
        job["cancel"].set()
        if job["thread"] is not None and job["thread"].is_alive():
            self.close_stale.append(job)
            rlog(f"장 마감 후 계산 취소 신호 (세대 {job['gen']}, {job['date']}): {why} — 끝나면 정리, 결과는 쓰지 않음", "WARN")
        elif job["result"] is not None:
            rlog(f"장 마감 후 계산 결과 버림 (세대 {job['gen']}, {job['date']}): {why}", "WARN")
        return job

    def dispose_pool(self, pool, why):
        """11I 보완 (주문 사고 방지): 버리는 계산 프로세스 묶음은 별도 스레드에서 terminate -> 짧은 대기 -> kill (ShardPool.shutdown).
        주 반복 쪽 대기는 0 (장중 손절 감시가 묶음 정리를 기다리는 경로 없음). 반환: 정리 스레드"""
        if pool is None:
            return None

        def run():
            try:
                r = (getattr(pool, "shutdown", None) or pool.close)()
                rlog(f"계산 프로세스 묶음 정리 ({why}): {r if r is not None else '끝'}", "INFO")
            except Exception as e:  # noqa: BLE001
                rlog(f"계산 프로세스 묶음 정리 실패 ({why}): {e!r}", "WARN")
        t = threading.Thread(target=run, daemon=True, name="pool_dispose")
        t.start()
        self.disposing = [x for x in self.disposing if x.is_alive()] + [t]
        return t

    def reap_close(self):
        """취소한 이전 작업 중 끝난 것: 참조 회수 (그 작업의 묶음은 취소할 때 이미 dispose_pool 로 넘김)"""
        for job in list(self.close_stale):
            if job["thread"].is_alive():
                continue
            self.close_stale.remove(job)
            rlog(f"장 마감 후 계산 정리 (세대 {job['gen']}, {job['date']}, {job['why']}): 결과 버림"
                 + (f" ({job['result'].get('aborted') or job['result'].get('error') or '끝남'})" if job.get("result") else ""), "INFO")

    def finish_late(self):
        """11I 보완: 16:30(day_end) 이 지났는데 요약(ac_summary)이 안 끝났으면 — 계산 작업은 취소하고 "대조 미완료"(16:30 지남)로 요약을 마무리,
        after_close 조건을 확인해 마무리한다 (PENDING 으로 남지 않게). 정산(settle)이 안 된 날은 다음 날 아침 복구에 맡긴다"""
        d = self.day
        if d is None or d.done("holiday") or d.done("ac_summary"):
            if self.close_job is not None:
                self.retire_close_job("16:30 지남 (그날 적용 못 함)")
            return
        job = self.close_job
        partial = {k: v for k, v in dict((job or {}).get("out") or {}).items() if k in ("close_1530", "measure_1520", "bars_n", "bars_s", "started")}
        if job is not None and job.get("result") and not job["result"].get("error"):
            partial.update({k: v for k, v in job["result"].items() if k in ("close_1530", "measure_1520", "rerun_out")})
        old = self.retire_close_job("16:30 지남 (그날 적용 못 함)")
        self.dispose_pool(self.pool if old is not None and old["pool"] is self.pool else None, "16:30 지남")
        if old is not None and old["pool"] is self.pool:
            self.pool, self.prep_ok = None, False
        if not d.done("settle"):
            return
        if self.close_calc_needed():
            self.close_gen += 1
            self.close_job = {"gen": self.close_gen, "date": str(d.date.date()), "thread": None, "cancel": threading.Event(), "pool": None,
                              "result": {**partial, "aborted": f"16:30 지남 (장 마감 후 계산 {'끝나지 않음' if old is not None else '시작 못 함'})",
                                         "late": True, "date": str(d.date.date()), "gen": self.close_gen}}
        S.run_step(d, "ac_summary", self.ac_summary, notify=alert, backoff_s=0, max_attempts=10 ** 6)
        if not d.done("ac_summary"):
            self.close_job = None
            return
        self.finish_after_close(d)

    def finish_after_close(self, d):
        """after_close: 하위 단계 넷이 끝났고 15:35 확인 SUCCEEDED + UNKNOWN 주문 없음일 때만 (2차 확인 리뷰 3)"""
        if not all(d.done(n) for n in ("settle", "ac_ledger", "ac_positions", "ac_summary")) or d.done("after_close"):
            return
        left = self.om.unresolved() if self.om is not None else []
        if d.done("close_check") and not left:  # 2차 확인 리뷰 3
            d.mark("after_close", {"substeps": "settle, ac_positions, ac_ledger, ac_summary", "close_check": d.status("close_check")})
        elif not d.s.get("after_close_held"):
            d.set("after_close_held", {"time": self.now().strftime("%H:%M:%S"), "close_check": d.status("close_check"),
                                       "unknown_orders": [i["intent_id"] for i in left],
                                       "note": "15:35 확인 미완료 또는 UNKNOWN 주문 -> after_close 안 함, 다음 날 아침 복구에서 대조"})

    def close_calc_needed(self):
        """장 마감 후 무거운 계산이 필요한 날 = 그날 스냅샷 계산이 있고 그날 로그가 있는 날"""
        d = self.day
        return bool((d.s.get("live") or {}).get("snap_file")) and os.path.exists(os.path.join(LOG_DIR, f"{d.date.date()}.json"))

    def start_rerun(self, day, out):
        """동결 코드 대조 (8장 12 (1), run_day.py rerun): 별도 프로세스로 시작만 (1분봉 받는 동안 함께 돈다). 결과는 out 파일에"""
        err = open(out + ".err", "w", encoding="utf-8")
        p = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_day.py"), "rerun", str(day.date()), "", out],
                             stdout=err, stderr=err, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                             creationflags=0x08000000 if os.name == "nt" else 0)  # CREATE_NO_WINDOW
        return p, err

    def wait_rerun(self, started, out, should_stop=None):
        """대조 프로세스 기다림. should_stop() 이 이유를 돌려주면(취소·상한 초과) 프로세스를 끝내고 중단 기록"""
        p, err = started
        stopped = None
        while True:
            try:
                rc = p.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                stopped = should_stop() if should_stop else None
                if stopped:
                    p.kill()
                    rc = "중단"
                    break
        err.close()
        if stopped:
            return {"error": f"대조 중단: {stopped}", "stopped": stopped}
        r = jload(out, None) if rc == 0 else None
        if r is None:
            tail = ""
            try:
                tail = open(out + ".err", encoding="utf-8", errors="replace").read()[-500:]
            except OSError:
                pass
            return {"error": f"rerun 실패 (종료 {rc}): {tail}"}
        return r

    def close_calc(self, job):
        """스레드에서 (11I): 15:30 1분봉 전 종목(ka10080 종목당 1회, 1초 5회 -> 약 2,650종목 9분) -> 15:30 1분봉 기준 상위 3 (8장 12 (2)) +
        15:20 측정, 그동안 동결 코드 대조(run_day.py rerun, 별도 프로세스)가 함께 돈다. 상태 파일·그날 로그는 고치지 않고 결과만 돌려준다
        (ac_summary 가 날짜·세대를 확인하고 반영). 입력·계산은 예전 ac_summary 와 같다 (같은 결과, test_mock1).
        취소 신호(날짜 바뀜·16:30 정리)나 상한 시각(close_calc_until, 기본 16:20)이면 종목마다·단계마다 확인해 CloseCalcStop"""
        d, ctx, pool = job["day"], job["ctx"], job["pool"]
        t0 = time.time()
        out = job["out"]
        out["started"] = self.now().strftime("%H:%M:%S")

        def stop_reason():
            if job["cancel"].is_set():
                return f"취소 ({job['why']})"
            if self.now() >= job["deadline"]:
                return f"상한 {job['deadline']:%H:%M} 초과"
            return None

        def check():
            why = stop_reason()
            if why:
                raise CloseCalcStop(why)
        check()
        res = self._res(d, ctx, pool)
        check()
        live = d.s.get("live", {})
        rr = os.path.join(LOG_DIR, f"{d.date.date()}_rerun.json")
        started = self.start_rerun(d.date, rr)
        try:
            caller = self.quote_caller()
            crow, bars = {}, {}
            for code in list(ctx.get("rows") or {}):
                check()
                try:
                    bars[code] = Q.bars_today(caller, code, d.date)
                    cv = Q.close_values(bars[code])
                except ValueError:
                    cv = None
                if cv:
                    crow[code] = ({k: cv[k] for k in ("open", "high", "low", "close", "volume", "amount")}, None, None)
            out["bars_s"], out["bars_n"] = round(time.time() - t0, 1), len(bars)
            check()
            if crow:
                ic = Q.index_close_today(caller, d.date)
                check()
                cres = E.run_day(pool, d.date, crow, ctx["index"], {"kospi": ic["kospi"], "kosdaq": ic["kosdaq"]},
                                 ctx["boosters"], ctx["cols"], live["ledger_before"])
                out["close_1530"] = {"eligible_n": len(cres["eligible"]), "ledgers": {
                    n: {"top3": [{"code": r.code, "score": float(r.score), "close_1530": float(r.raw_close)} for r in cres["ledgers"][n]["top3"].itertuples()],
                        "overlap_with_1519": len({x["code"] for x in live["ledgers"][n]} & set(cres["ledgers"][n]["top3"].code))} for n in NAMES}}
                check()
                targets = set(res["eligible"].code) | set(cres["eligible"].code)
                mres = self.compare_measure(bars, targets, d)
                out["measure_1520"] = mres
                check()
                if mres is not None and d.s.get("measure_snapshot"):
                    import measure as MS
                    check()
                    try:
                        mres["top3_compare"] = MS.top3_compare(pool, d.date, ctx["index"], ctx["boosters"], ctx["cols"],
                                                               live["ledger_before"], MS.snap_rows(d.s["measure_snapshot"]["file"]),
                                                               d.s["measure_snapshot"]["index_values"], MS.rows_upto(bars), MS.index_upto(caller, d.date))
                    except Exception as e:  # noqa: BLE001
                        mres["top3_compare"] = {"error": repr(e)[:300]}
                out["measure_1520"] = mres
                check()  # 11I 보완: 마지막 측정 뒤에도 상한·취소 확인
        finally:
            out["rerun_out"] = self.wait_rerun(started, rr, stop_reason)
        if out["rerun_out"].get("stopped"):
            raise CloseCalcStop(out["rerun_out"]["stopped"])
        check()
        out["seconds"] = round(time.time() - t0, 1)
        out["finished"] = self.now().strftime("%H:%M:%S")
        return dict(out)

    def rerun_text(self, rr):
        """요약 알림용 동결 코드 대조 결과: "대조 전략 3/3, 전체 3/3" (같은 3종목·같은 순서면 n/n, 순서만 다르면 "순서 다름")"""
        if not rr or not all(n in rr for n in NAMES):
            return "대조 없음"
        parts = []
        for n, w in zip(NAMES, ("전략", "전체")):
            x = rr[n]
            k = len(x.get("top3") or [])
            parts.append(f"{w} {x.get('overlap')}/{k}" + ("" if x.get("same_3of3") or x.get("overlap") != k else " 순서 다름"))
        return "대조 " + ", ".join(parts)

    def ac_summary(self):
        """15:30 1분봉 기준 상위 3 (8장 12 (2)) + 15:20 측정 + 동결 함수 대조 (8장 12 (1), run_day.py rerun) + summary.csv + 요약 알림.
        11I: 무거운 계산은 close_calc(스레드)가 끝낸 결과(close_result)를 쓰고, 여기서는 반영·기록만 (몇 초)"""
        d = self.day
        if self.close_calc_needed():
            cr = self.close_result if self.close_ready() else None
            if cr is None:
                raise S.StepFailed("장 마감 후 계산(close_calc)이 아직 안 끝남")
            if cr.get("error"):
                self.close_job, self.close_next_try = None, self.now() + pd.Timedelta(seconds=60)  # 60초 뒤 다시 계산
                raise S.StepFailed(f"장 마감 후 계산 실패 (60초 뒤 다시): {cr['error']}", {"trace": cr.get("trace")})
        else:
            cr = None
        # 16:30 마무리(late)는 계산하지 않고 이미 있는 스냅샷 결과만 (묶음은 정리 중)
        res = (self.ctx.get("res") if cr.get("late") else self._res()) if cr is not None else None
        live = d.s.get("live", {})
        inc = None
        if (cr or {}).get("aborted"):  # 11I 보완: 상한 초과·16:30 지남 = 대조 미완료. 기록·알림만, 매수 차단 안 함 (다음 날 매수에 영향 없음)
            inc = {"time": self.now().strftime("%H:%M:%S"), "reason": cr["aborted"], "rerun": "결과 있음" if (cr.get("rerun_out") or {}).get("rerun") else "없음",
                   "note": "대조 미완료: 신규 매수 차단 안 함, 다음 날 매수에 영향 없음 (다음 날 대조는 그날 스냅샷으로 따로 함)"}
            d.set("rerun_incomplete", inc)
            alert(f"[대조 미완료] {d.date.date()} 장 마감 후 계산 중단 ({cr['aborted']}) — 매수 차단 안 함, 다음 날 매수에 영향 없음")
        st = d.s.get("settlement") or {}
        lp = os.path.join(LOG_DIR, f"{d.date.date()}.json")
        rr = None
        if res is not None and os.path.exists(lp):
            import run_day as R
            log = jload(lp, {})
            if cr.get("close_1530") is not None:
                log["close_1530"] = cr["close_1530"]
            mres = cr.get("measure_1520")
            if mres is not None:
                log["measure_1520"] = mres
                d.set("measure_result", mres)
                t_ = mres.get("targets", {})
                tc = mres.get("top3_compare") or {}
                alert(f"[15:20 측정] 대상 후보 {t_.get('n')}종목: 현재가=15:19봉 종가 {t_.get('price_eq_1519_close_pct', float('nan')):.1f}%, "
                      f"거래량 0.1% 이내 {t_.get('vol_within_0p1_pct', float('nan')):.1f}% (기록만), 상위 3 같음 (a, 판정) 전략 "
                      f"{(tc.get('strategy') or {}).get('eq_3of3')} 전체 {(tc.get('full') or {}).get('eq_3of3')} / (b, 참고) 전략 "
                      f"{((tc.get('ref_b') or {}).get('strategy') or {}).get('eq_3of3')} 전체 {((tc.get('ref_b') or {}).get('full') or {}).get('eq_3of3')}")
            log["orders"] = [r for r in self.om.records]
            log["buy_decision"] = d.s.get("buy_decision") or {"new_buy_ok": False, "reason": "; ".join(self.buy_gate()) or "매수 단계 기록 없음"}
            log["cash_check"] = d.s.get("cash_check")                                    # Codex 20
            log["order_counts"] = {"orders": len([r for r in self.om.records if r.get("ord_no") and r["side"] in ("buy", "sell")]),
                                   "fills": len([r for r in self.om.records if r.get("filled_qty")])}
            log["official_vs_1530"] = d.s.get("official_vs_1530")                        # Codex 21
            log["same_day_failures"] = d.s.get("same_day_failures")                      # Codex 27
            log["prev_recovery"] = d.s.get("prev_recovery")
            log["holdings_cash_check"] = {"account_holdings": st.get("account"), "deposit": st.get("deposit"), "orderable": st.get("orderable"),
                                          "d2": st.get("d2"), "diff": st.get("diff")}
            ro = cr.get("rerun_out") or {}
            if inc:
                log["rerun_incomplete"] = inc
            if ro.get("error"):
                log.setdefault("errors", []).append(ro["error"][:600])
            elif ro.get("rerun") is not None:
                log["rerun"] = ro["rerun"]
                log.setdefault("errors", []).extend(ro.get("errors_add") or [])
            log["close_calc_timing"] = {k: cr.get(k) for k in ("started", "finished", "seconds", "bars_s", "bars_n")}
            rr = log.get("rerun")
            for n in NAMES:
                if (rr or {}).get(n, {}).get("same_3of3") is False:
                    alert(f"[8장 12 (1) 불일치] {n} 장부: 원인을 찾을 때까지 신규 매수 중단 ({BLOCK} 를 지우면 다시 매수)")
                    jdump(BLOCK, {"since": str(d.date.date()), "reason": f"8장 12 (1) 불일치 ({n} 장부)"})
            log["account_watch"] = self.watch.summary()
            jdump(lp, log)
            R.summary(log)
        else:  # 확인 리뷰 16: 스냅샷이 없거나 실패한 날도 summary.csv 한 줄 (매수 안 함, 이유 포함)
            import run_day as R
            prep = self.ctx.get("prep") or {}
            log = jload(lp, None) or {"date": str(d.date.date()), "mode": f"resident ({CFG['env']}, 주문 {'켬' if self.broker and self.broker.allow_orders else '끔'})",
                                      "quotes_env": "prod", "git_commit": git_commit(), "errors": [],
                                      "model": prep.get("model") or {"month": None, "check": {"ok": False}}, "data": prep.get("data") or {},
                                      "data_version": self.ctx.get("data_version"), "note": "스냅샷 없음 또는 실패"}
            log.setdefault("model", {"month": None, "check": {"ok": False}})
            log.setdefault("git_commit", git_commit())
            bd = d.s.get("buy_decision") or {}
            why = [x for x in [bd.get("reason")] + self.buy_gate() if x]
            log["buy_decision"] = {**bd, "new_buy_ok": False, "reason": "; ".join(dict.fromkeys(why)) or "스냅샷 없음",
                                   "buy_block": list(d.s.get("buy_block") or [])}
            log["errors"] = list(dict.fromkeys(list(log.get("errors") or []) + list(d.s.get("errors") or [])))
            log["cash_check"] = d.s.get("cash_check")
            log["order_counts"] = {"orders": len([r for r in self.om.records if r.get("ord_no") and r["side"] in ("buy", "sell")]),
                                   "fills": len([r for r in self.om.records if r.get("filled_qty")])}
            log["holdings_cash_check"] = {"account_holdings": st.get("account"), "deposit": st.get("deposit"), "orderable": st.get("orderable"),
                                          "d2": st.get("d2"), "diff": st.get("diff")}
            log["prev_recovery"], log["account_watch"] = d.s.get("prev_recovery"), self.watch.summary()
            if inc:
                log["rerun_incomplete"] = inc
            jdump(lp, log)
            R.summary(log)
        n_ord = len([r for r in self.om.records if r.get("ord_no") and r["side"] in ("buy", "sell")])
        n_fill = len([r for r in self.om.records if r.get("filled_qty")])
        cc = d.s.get("cash_check") or {}
        cash = f"현금 차이 {cc['cash_diff']:,.0f}" if cc.get("cash_diff") is not None else f"현금 차이 - ({cc.get('note') or '정산 기록 없음'})"
        n_err, n_res = d.open_error_count(), len(d.s.get("errors_resolved") or [])
        line = (f"[{d.date.date()} 요약] 필터 {'통과' if live.get('filter', {}).get('filter_ok') else '미통과'}, "
                f"{live.get('kind', '-')} 상위 3 {[p['code'] for p in live.get('plan', [])]}, 주문 {n_ord} 체결 {n_fill}, "
                f"보유 {len(st.get('account') or {})}종목, D+2 예수금 {st.get('d2') or 0:,.0f}, 정산 불일치 {len(st.get('diff') or {})}, "
                f"{cash}, {pnl_text(st.get('pnl'))}{perf_text(st.get('strategy_perf'))}{'대조 미완료' if inc and not rr else (self.rerun_text(rr) if res is not None else '대조 없음 (스냅샷 없음)')}, "
                f"신규 매수 {'가능' if (d.s.get('buy_decision') or {}).get('new_buy_ok') else '안 함'}, "
                f"오류 {n_err}" + (f" (해결 {n_res})" if n_res else ""))
        alert(line)
        self.close_job = None  # 반영 끝: 작업 참조 회수
        return {"summary": line, "close_calc_s": (cr or {}).get("seconds")}

    def pending_days(self):
        """확인 리뷰 N1: 마지막으로 정산(after_close)이 SUCCEEDED 된 날 다음부터 어제까지의 모든 거래일 (오래된 순, 최대 30거래일).
        정산된 날이 하나도 없으면 가장 오래된 상태 파일 날부터. 반환 (마지막 정산일 또는 None, [날짜 문자열])"""
        today = str(self.day.date.date())
        files = sorted(f[:10] for f in os.listdir(CFG["state_dir"]) if len(f) == 15 and f[:4].isdigit() and f.endswith(".json") and f[:10] < today)
        last_ok = None
        for f in reversed(files):
            if S.is_trading_day(f, self.holidays) and new_day(pd.Timestamp(f), clock=self.clock).done("after_close"):
                last_ok = f
                break
        if last_ok is None and not files:
            return None, []
        start = pd.Timestamp(last_ok) + pd.Timedelta(days=1) if last_ok else pd.Timestamp(files[0])
        days = [str(x.date()) for x in pd.date_range(start, self.day.date - pd.Timedelta(days=1)) if S.is_trading_day(x, self.holidays)]
        return last_ok, days[-30:]

    def recent_trading_days(self, n):
        """오늘 앞 n 거래일 (오래된 순)"""
        out, x = [], self.day.date
        for _ in range(60):
            x -= pd.Timedelta(days=1)
            if S.is_trading_day(x, self.holidays):
                out.append(str(x.date()))
                if len(out) >= n:
                    break
        return sorted(out)

    def day_start_qty(self, x):
        """x 날 시작 때 보유 수량: 그날 첫 시작 스냅샷(positions_start, 15:21 전에만 씀), 없으면 정산 기준 장부(positions_baseline),
        없으면 전 거래일 15:40 정산 계좌 (3차 확인 리뷰 R2). 모두 없으면 None"""
        st = jload(os.path.join(CFG["state_dir"], f"{x}.json"), {})
        if st.get("positions_start"):
            return {c: int(q) for c, q in st["positions_start"].get("held", {}).items()}
        if st.get("positions_baseline"):
            return {c: int(v.get("qty", 0)) for c, v in st["positions_baseline"].get("held", {}).items()}
        prev = S.prev_trading_day(pd.Timestamp(x), self.holidays)
        acct = (jload(os.path.join(CFG["state_dir"], f"{prev.date()}.json"), {}).get("settlement") or {}).get("account")
        if acct is not None:
            return {c: int(q) for c, q in acct.items()}
        return None

    def start_trade(self, x, c, pos):
        """x 날 시작 때 c 의 거래 정보: 그날 정산 기준 장부, 없으면 지금 보유 장부의 앞 거래(진입일 < x). 없으면 None"""
        base = (jload(os.path.join(CFG["state_dir"], f"{x}.json"), {}).get("positions_baseline") or {}).get("held", {}).get(c)
        if base and base.get("entry_date"):
            return dict(base)
        p = pos["held"].get(c) or {}
        if p.get("entry_date") and p["entry_date"] < x and not p.get("provisional_new"):
            return {k: v for k, v in p.items() if k not in ("info_pending", "pending_since", "ambiguous")}
        return None

    def mark_ambiguous(self, pos, c, x, held, why, base=None, kind=None):
        """3차 확인 리뷰 R1·R3: 복구가 애매하면 추측해서 확정하지 않는다. 수량은 계좌, 거래 정보는 미확정(info_pending + ambiguous)으로 두고
        손절가는 후보(그날 시작 거래의 손절가 / 그날 새 거래의 임시 손절가) 중 높은 쪽(더 일찍 파는 쪽). 신규 매수 안 함(buy_gate), 알림은 prev_settle.
        사람이 체결 내역으로 확인해 의도 상태와 positions.json 의 ambiguous 를 고칠 때까지 유지. 계좌에 없으면 None"""
        h = held.get(c) or {}
        qty, avg = int(h.get("qty", 0)), h.get("avg_price")
        if qty <= 0:
            return None
        cands = {}
        if base and base.get("stop_price") is not None:
            cands["그날 시작 거래"] = {"trade_id": base.get("trade_id"), "entry_date": base.get("entry_date"), "stop_price": float(base["stop_price"])}
        s_, basis = P.temp_stop(c, avg, x)
        cands["그날 새 거래"] = {"entry_date": x, "stop_price": float(s_), "stop_basis": basis}
        if base and base.get("entry_date"):
            t = dict(base)
            pos["held"][c] = t
        elif pos["held"].get(c) and not pos["held"][c].get("provisional_new"):
            t = pos["held"][c]
        else:
            t = P.new_trade(pos, c, x, kind=kind or "미확인")
        t.pop("provisional_new", None)
        hi = max(cands.items(), key=lambda kv: kv[1]["stop_price"])
        t.update(qty=qty, avg_price=avg, info_pending=True, pending_since=x, stop_price=hi[1]["stop_price"], stop_temp=True,
                 stop_basis=f"미확정: 후보 중 높은 쪽 ({hi[0]}) (3차 확인 리뷰)",
                 ambiguous={"since": x, "why": why, "candidates": cands, "marked": str(self.day.date.date()),
                            "clear": "체결 내역으로 확인한 뒤 의도 상태와 이 항목(ambiguous·info_pending)을 사람이 고친다"})
        return t

    def rebuild_pending(self, pos, held, days, stores):
        """2차 확인 리뷰 1: 정산 때 거래 정보 미확정(info_pending·provisional_new)으로 남은 보유는 표시만 지우지 않고 그날 의도·체결로 다시 만든다.
        - 그날 시작 보유가 없었거나, 시작 보유를 같은 종목 매도로 전량 판 뒤 다시 샀으면 새 거래 (매수 의도 날짜·분류, 잠정 거래의 번호는 그대로 씀)
        - 그 밖에 시작 보유가 있었으면 그날 시작 때의 거래 정보(진입일·손절가·trade_id·분류)로 되돌림
        - 매수 의도도 시작 보유도 없으면 진입일 미확인(entry_temp, 사람이 확인할 때까지 신규 매수 안 함)
        새 거래·미확인은 임시 손절가 (준비 뒤 공식 일봉으로 진입일 종가 - 1.7 x ATR20, finalize_recovery)"""
        out = {}
        for c, p in list(pos["held"].items()):
            if not (p.get("info_pending") or p.get("provisional_new")) or p.get("ambiguous") or P.in_review(p):  # 수동 확인 중도 그대로 (11K 보완 3)
                continue  # 미확정(ambiguous)은 사람이 확인할 때까지 그대로 (3차 확인 리뷰)
            x = p.get("pending_since")
            if x not in stores:
                x = next((dd for dd in reversed(days) if any(i.get("code") == c for i in stores[dd].items)), days[-1])
            items = [i for i in stores[x].items if i.get("code") == c]
            buys = [i for i in items if i.get("side") == "buy" and i.get("status") == O.ACCEPTED]
            sells = [i for i in items if i.get("side") == "sell" and i.get("status") == O.ACCEPTED]
            start = (self.day_start_qty(x) or {}).get(c, 0)
            base = (jload(os.path.join(CFG["state_dir"], f"{x}.json"), {}).get("positions_baseline") or {}).get("held", {}).get(c) or {}
            # 3차 확인 리뷰 R3: 전량 매도 여부는 요청 수량이 아니라 체결 수량으로. 체결 수량을 모르는 매도가 있어 판단이 갈리면 미확정
            sold = sum(int(i.get("filled_qty") or 0) for i in sells)
            most = sum(int(i.get("filled_qty") or 0) if O.fill_known(i) else int(i.get("qty") or 0) for i in sells)
            full_sell = start > 0 and sold >= start
            qty = int(held.get(c, {}).get("qty", p.get("qty", 0)))
            avg = held.get(c, {}).get("avg_price", p.get("avg_price"))
            if buys and start > 0 and not full_sell and most >= start:
                t = self.mark_ambiguous(pos, c, x, held, f"{x} 매도 체결 수량 모름 (체결 확인 {sold}주, 요청 합 {most}주, 시작 {start}주) -> 전량 매도 뒤 새 거래인지 알 수 없음",
                                        base=dict(base) if base.get("entry_date") else self.start_trade(x, c, pos), kind=buys[-1].get("kind"))
                out[c] = {"day": x, "how": "미확정 (매도 체결 수량 모름, 3차 확인 리뷰 R3)", "trade_id": (t or {}).get("trade_id"), "start_qty": start,
                          "sold": sold, "full_sell": None}
                continue
            if buys and (start == 0 or full_sell):
                b = buys[-1]
                if p.get("provisional_new") and p.get("entry_date") == x and p.get("trade_id"):
                    t = p
                else:
                    t = P.new_trade(pos, c, x, entry_price=b.get("fill_price"))
                t.update(kind=b.get("kind", "본래"), entry_temp=False)
                how = "새 거래 (그날 매수" + (", 앞 거래 전량 매도 뒤" if full_sell else "") + ")"
            elif start > 0 and base.get("entry_date"):
                t = dict(base)
                pos["held"][c] = t
                how = "그날 시작 거래 정보로 되돌림"
            elif start > 0:
                t = p
                how = "기존 거래 정보 유지 (그날 시작 거래 정보 없음)"
            else:
                t = P.new_trade(pos, c, x, kind="미확인", entry_temp=True)
                how = "진입일 미확인 (매수 의도·시작 보유 없음)"
            for k in ("info_pending", "provisional_new", "pending_since"):
                t.pop(k, None)
            t["qty"], t["avg_price"] = qty, avg
            if how.startswith(("새 거래", "진입일 미확인")) or not t.get("stop_price"):
                s_, basis = P.temp_stop(c, avg, x)
                t.update(stop_price=float(s_), stop_temp=True, stop_basis=basis)
            out[c] = {"day": x, "how": how, "trade_id": t.get("trade_id"), "start_qty": start, "sold": sold, "full_sell": full_sell}
        if out:
            alert(f"[거래 정보 다시 만듦] {out}"[:300])
        return out

    def prev_settle(self):
        """B2·Codex 5·확인 리뷰 N1·2차 확인 리뷰 1~3: 아침 복구 (끝날 때까지 신규 매수 안 함).
        볼 날 = 마지막으로 정산이 SUCCEEDED 된 날 이후의 모든 거래일 + 최근 5거래일 (완료된 날이라도 UNKNOWN·미확정이 남았으면 대조).
        - UNKNOWN·SENDING 의도: 계좌 수량 - 주문 직전 보유(pre_qty) 로 체결 여부 (매수는 늘었으면, 매도는 줄었으면 ACCEPTED, 아니면 NOT_SENT).
          pre_qty 가 없는 예전 의도는 그날 시작 보유 스냅샷 기준. 이미 갱신된 보유 장부는 기준으로 쓰지 않는다.
        - 거래 정보 미확정 보유: 그날 의도·체결로 다시 만든다 (rebuild_pending).
        - 상태 파일이 없는 날(실행 안 한 날)은 기록. 진입일·손절가를 확인 못 한 종목은 임시 손절가, 준비 뒤 공식 일봉으로 마무리 (finalize_recovery)"""
        d = self.day
        today = str(d.date.date())
        last_ok, inc = self.pending_days()
        days = sorted(set(inc) | set(self.recent_trading_days(RECOVERY_LOOKBACK)))
        if not days:
            return {"last_settled": last_ok, "note": "볼 날 없음"}
        missed = [x for x in inc if not os.path.exists(os.path.join(CFG["state_dir"], f"{x}.json"))]
        stores = {x: O.IntentStore(os.path.join(CFG["state_dir"], "intents", f"{x}.json"), clock=self.now) for x in days}
        unresolved = [(x, i) for x in days for i in stores[x].items if i.get("status") in (O.UNKNOWN, O.SENDING) and i.get("stage") != "cancel"]
        pos = self.positions()
        pend = [c for c, p in pos.get("held", {}).items() if p.get("info_pending") or p.get("provisional_new")]
        corrupt = [x for x in days if stores[x].corrupt]
        if not inc and not unresolved and not pend and not corrupt:
            return {"last_settled": last_ok, "checked_days": days, "note": f"복구할 것 없음 (최근 {RECOVERY_LOOKBACK}거래일 UNKNOWN·미확정 없음)"}
        held = self.broker.account()["holdings"]
        # 11K 보완 4·보완 3: 보유 수량을 계좌로 덮어쓰기(fill_missing·rebuild_pending) 전에, 체결로 설명 안 되는 수량 변화를 찾아 수동 확인으로.
        # 설명 = 장부 수량을 마지막으로 계좌로 맞춘 날(qty_synced, 15:40 보유 갱신이 저장까지 끝낸 날) 뒤의 의도와 오늘 의도(NOT_SENT·REJECTED 밖).
        # 15:40 갱신 도중 꺼진 날(기준 장부만 저장)은 맞춘 날이 아니라 그날 매수 의도가 설명으로 남는다. 기록 없는 예전 장부는 마지막 정산일 뒤
        synced = (pos.get("qty_synced") or {}).get("date") or last_ok
        since = [x for x in days if synced is None or x > synced]
        recs = [dict(i, date=today) for x in since for i in stores[x].items if i.get("status") not in (O.NOT_SENT, O.REJECTED)]
        recs += [i for i in self.om.records if i.get("status") not in (O.NOT_SENT, O.REJECTED)]
        ch = O.qty_changes({c: p for c, p in pos["held"].items() if not p.get("ambiguous")}, held, recs, today)
        reviews = self.mark_qty_review(pos, ch, "아침 복구") if ch else {}
        if reviews:
            save_positions(pos)
        starts = {}
        groups = {}
        for x, it in unresolved:
            groups.setdefault((x, it["code"]), []).append(it)
        ambiguous, n_settled = {}, 0
        for x, it in unresolved:
            c = it["code"]
            g = groups[(x, c)]
            if len(g) >= 2:  # 3차 확인 리뷰 R1: 같은 날 같은 종목 UNKNOWN 이 둘 이상 -> 잔고 하나로 각각 확정하지 않는다 (체결 조회로 확정될 때까지 미확정)
                ambiguous.setdefault(c, {"day": x, "intents": [i["intent_id"] for i in g],
                                         "why": f"{x} 같은 종목 UNKNOWN {len(g)}건 ({', '.join(i['stage'] + ' ' + i['side'] for i in g)}) — 잔고 하나로 각각 확정 못 함"})
                stores[x].update(it, recovery_note=f"{today} 아침 복구: 같은 날 같은 종목 UNKNOWN {len(g)}건 -> 미확정 유지 (3차 확인 리뷰 R1)")
                continue
            n_settled += 1
            now_q = int(held.get(c, {}).get("qty", 0))
            if it.get("pre_qty") is not None:
                base, basis = int(it["pre_qty"]), "주문 직전 보유(pre_qty)"
            else:
                if x not in starts:
                    starts[x] = self.day_start_qty(x)
                if starts[x] is not None:
                    base, basis = int(starts[x].get(c, 0)), f"{x} 시작 보유"
                else:
                    base, basis = int((pos["held"].get(c) or {}).get("qty", 0)), "보유 장부 (그날 시작 스냅샷 없음)"
            moved = now_q > base if it["side"] == "buy" else now_q < base
            stores[x].update(it, status=O.ACCEPTED if moved else O.NOT_SENT, checks_due=[],
                             cause=f"{today} 계좌 잔고로 확정 ({x} 의도, 기준 {basis} {base}, 지금 {now_q})", matched_by=f"{today} 잔고" if moved else None)
        for c, a in ambiguous.items():
            if P.in_review(pos["held"].get(c)):  # 11K 보완 3: 수동 확인 중이면 손절가를 후보 중 높은 쪽으로 바꾸지 않음 (의도는 UNKNOWN 그대로)
                a["held_qty"], a["stop_price"], a["note"] = int(held.get(c, {}).get("qty", 0)), pos["held"][c].get("stop_price"), "수동 확인 중 -> 장부 그대로"
                continue
            buy = next((i for i in groups[(a["day"], c)] if i["side"] == "buy"), {})
            t = self.mark_ambiguous(pos, c, a["day"], held, a["why"], base=self.start_trade(a["day"], c, pos), kind=buy.get("kind"))
            a["held_qty"], a["stop_price"] = int(held.get(c, {}).get("qty", 0)), (t or {}).get("stop_price")
        rebuilt = self.rebuild_pending(pos, held, days, stores)
        ambiguous.update({c: {"day": r["day"], "why": r["how"], "held_qty": int(held.get(c, {}).get("qty", 0)),
                              "stop_price": pos["held"].get(c, {}).get("stop_price")} for c, r in rebuilt.items() if r["how"].startswith("미확정")})
        intents = [dict(i, date=i.get("date") or x) for x in days for i in stores[x].items]
        temp, unknown = self.fill_missing(pos, held, days[-1], intents, f"{days[0]} ~ {days[-1]} 복구")
        P.mark_synced(pos, today, self.now().strftime("%H:%M:%S"), "아침 복구 (계좌 수량)")
        prev = days[-1]
        trend_unjudged = [c for c, p in pos["held"].items() if p.get("judged_on") != prev and p.get("entry_date") and prev > p["entry_date"]]
        save_positions(pos)
        as_of = jload(LEDGER, {}).get("as_of") or ""
        stale = []
        for x in inc:
            if x in missed:
                continue
            xd = new_day(pd.Timestamp(x), clock=self.clock)
            if xd.s.get("live") and not xd.done("ac_ledger") and as_of < x:
                stale.append(x)
        d.set("prev_recovery", {"prev": prev, "days": inc, "checked_days": days, "last_settled": last_ok, "missed_days": missed,
                                "time": self.now().strftime("%H:%M:%S"), "settled": n_settled, "rebuilt": rebuilt, "temp_stops": temp,
                                "unknown_entry": unknown, "trend_unjudged": trend_unjudged, "ledger_stale_days": stale, "intents_corrupt": corrupt,
                                "ambiguous": ambiguous, "qty_review": reviews, "finalized": False})
        for x in inc:
            if x in missed:
                continue
            xd = new_day(pd.Timestamp(x), clock=self.clock)
            xd.mark("after_close", {**xd.s["steps"].get("after_close", {}), "settled_next_day": today,
                                    "note": f"{today} 에 계좌 기준으로 정산 (B2, Codex 5, 확인 리뷰 N1)"})
        if ambiguous:
            what = ", ".join(f"{c}: {a['why']}" for c, a in ambiguous.items())
            alert(f"[복구 미확정] {what} — 추측해서 확정하지 않음: 수량은 계좌, 손절가는 후보 중 높은 쪽, 신규 매수 안 함. "
                  f"체결 내역으로 확인 뒤 의도 상태·positions.json 의 ambiguous 를 고치세요"[:300])
        alert(f"[전날 미완료 정산] 미완료 {inc or '없음'} (실행 안 한 날 {missed}), 최근 {RECOVERY_LOOKBACK}거래일 확인: 미확정 주문 {n_settled}건 계좌로 확정, "
              f"거래 정보 다시 만듦 {list(rebuilt)}, 임시 손절가 {temp}, 추세 판정 필요 {trend_unjudged}, 장부 다시 계산 {stale}"[:300])
        return {"days": days, "incomplete_days": inc, "missed_days": missed, "settled": n_settled, "rebuilt": rebuilt,
                "temp_stops": temp, "ledger_stale_days": stale, "ambiguous": sorted(ambiguous)}

    def data_check(self):
        """B6: 15:17 에 전날 공식 일봉이 반영된 데이터 버전이 메모리에 올라와 있지 않으면 그날 신규 매수 안 함"""
        prep = self.ctx.get("prep", {}).get("data", {})
        ok = bool(self.ctx.get("prep")) and bool(prep.get("daily_up_to_date")) and not prep.get("official_missing_dates")
        if not ok:
            self.block_buys("data_version", f"15:17 데이터 버전 {self.ctx.get('data_version')} 에 전날 공식 일봉 미반영 (또는 준비 안 됨) -> 그날 신규 매수 안 함 (B6)",
                            f"[데이터 버전 미반영] 15:17 기준 전날 공식 일봉 없음 -> 오늘 신규 매수 안 함 ({self.ctx.get('data_version')})")
        return {"ok": ok, "data_version": self.ctx.get("data_version")}

    # ------------------------------------------------------------ 반복

    def tick(self, t):
        """1초마다 한 번. 반환: 하트비트에 남길 단계 이름"""
        if self.tz_block:
            return "tz_block"
        if self.hours_day != t.normalize():  # 11K 2: 날짜가 바뀌면 먼저 그날 시각표 (하루 끝·늦은 시작 판단도 그날 폐장 기준)
            apply_hours(t.normalize())
            self.hours_day = t.normalize()
        try:
            if self.auto_monthly:
                self.monthly_catchup(t)  # 11K 보완 1: 다음 달 모델 따라잡기 (하루 상태 없이도, 별도 프로세스, 주 반복 대기 없음)
        except Exception as e:  # noqa: BLE001
            rlog(f"월별 모델 따라잡기 오류: {e!r}"[:300], "ERROR")
        hms = t.strftime("%H:%M:%S")
        self.reap_close()
        if not (T["day_start"] <= hms < T["day_end"]):
            # 11I 보완: 장외 시간에도 계산 작업을 정리 (16:30 이 지나면 취소 신호, 끝난 스레드 참조 회수) 뒤 풀을 닫는다
            if hms >= T["day_end"]:
                self.finish_late()  # 미적용 작업은 "대조 미완료"로 요약·after_close 마무리
                if self.pool is not None:
                    self.dispose_pool(self.pool, "16:30 지남")  # 별도 스레드 (주 반복 대기 0)
                    self.pool, self.prep_ok = None, False
            return "idle"
        if self.day is None or self.day.date != t.normalize():
            d = new_day(t, clock=self.clock)
            d.listener = self.on_day
            if not S.is_trading_day(t, self.holidays):
                rlog(f"휴장일 {t.date()} (주말 또는 krx_holidays.json) — 일봉 반영·매도·손절·매수 안 함, 하트비트만", "INFO")
                if not d.done("holiday"):
                    d.mark("holiday", {"weekday": int(t.weekday()), "note": "주말 또는 휴장일: 일봉 반영·매도·손절·스냅샷·매수·장부 갱신 안 함"})
                    if t.weekday() < 5:
                        alert(f"[휴장일] {t.date()} — 오늘은 아무것도 하지 않음")
                self.day = d
                return "holiday"
            self.start_reason = "하루 시작"
            if d.s["steps"]:
                d.recover_after_restart()
                with d.lock:
                    d.s["restarts"] += 1
                    d.save()
                alert(f"[재시작] {d.date.date()} 이어서 (단계 {({k: v.get('status') for k, v in d.s['steps'].items()})}), 코드 {git_state()}")
                self.start_reason = f"재시작 {d.s['restarts']}회"
            elif hms >= T["snapshot"]:
                d.mark("late_start", {"started": hms, "note": "15:18:30 이후 처음 시작 -> 그날 신규 매수만 안 함, 손절·매도·정산은 함 (Codex 4)"})
            self.retire_close_job(f"날짜 바뀜 ({t.date()})")  # 11I 보완: 이전 날 작업은 취소, 결과는 쓰지 않음
            self.dispose_pool(self.pool, f"날짜 바뀜 ({t.date()})")    # 별도 스레드 (주 반복 대기 0)
            self.day, self.started, self.next_start_try = d, False, t
            self.pool, self.prep_ok, self.prep_thread, self.prep_result, self.next_prep_try, self.ctx = None, False, None, None, t, {}
            self.last_stop_minute = None
        d = self.day
        if d.done("holiday"):
            return "holiday"
        if not self.started:
            if t < self.next_start_try:
                return "start_wait"
            try:
                self.start_core(d, reason=self.start_reason)
                self.started = True
            except Exception as e:  # noqa: BLE001  (60초 뒤 다시)
                d.err(f"하루 시작(핵심) 실패: {e!r}")
                alert(f"[하루 시작 실패] 60초 뒤 다시: {e!r}"[:300])
                self.next_start_try = t + pd.Timedelta(seconds=60)
                return "start_failed"
        step = "wait"
        # 11J: 새 손절 감지는 stop_until(15:20) 의 분까지, 손절 진행 중·늦은 추세 매도 보내기는 마감 동시호가 안 stop_send_until(15:29) 까지.
        # 11J 보완 3 R3: 그 분의 다른 작업(UNKNOWN 대조, 재적재, 준비 등)보다 먼저 (늦은 대조가 15:29 마감을 넘기지 않게)
        # N1: 단, 준비 스레드가 이미 계산한 공식 손절가·추세 판정(전날 복구)은 새 손절 감지보다 먼저 적용 (적용만, 계산 없음)
        # 11K 보완 2 (2): 준비 스레드가 모델·적재와 따로 먼저 공개한 값(judge_result)을 적용 (준비가 아직 돌거나 실패해도)
        jr = self.judge_result if self.judge_result is not None and self.judge_result.get("day") == d.date else None
        if jr and jr.get("recovery"):
            try:
                self.apply_recovery_fixes(jr["recovery"])
            except Exception as e:  # noqa: BLE001
                d.err(f"전날 복구 적용 오류 (준비 결과 적용 때 다시): {e!r}"[:300])
        if jr and jr.get("official_judge") is not None and not jr.get("official_applied"):
            try:  # 11K 3: 공식 일봉 재판단도 적용만 (08:30 추세 매도·새 손절 감지보다 먼저)
                if not self.official_vs_1530(jr["official_judge"], compute=False).get("skipped"):
                    jr["official_applied"] = self.now().strftime("%H:%M:%S")
            except Exception as e:  # noqa: BLE001
                d.err(f"공식 일봉 재판단 적용 오류 (준비 결과 적용 때 다시): {e!r}"[:300])
        if T["stop_from"] <= hms < T.get("stop_send_until", "15:29:00"):
            step = "stop"
            minute = t.strftime("%Y-%m-%d %H:%M")
            if self.last_stop_minute != minute:  # Codex 11
                self.last_stop_minute = minute
                try:
                    self.stop_minute(detect=hms[:5] <= T["stop_until"][:5])
                except Exception as e:  # noqa: BLE001
                    d.err(f"stop 오류: {e!r}")
                    alert(f"[오류] stop: {e!r}"[:300])
        if self.auto_monthly:
            self.monthly_deadline(t)
        for tag, t0, until in TOKEN_CHECKS:  # 11J 보완 3 R3: 장 시작 전·매수 전 토큰 갱신 확인 (하루 한 번씩, 11K 2: 그날 시각표로)
            if at(t0) <= hms < at(until) and not d.s.get(f"token_check_{tag}"):
                try:
                    self.token_check(tag, at(until))
                except Exception as e:  # noqa: BLE001
                    d.err(f"토큰 갱신 확인 오류: {e!r}")
        # 준비 (스레드): 실패·늦은 시작이어도 아래 손절·매도·정산은 계속 (Codex 4)
        if self.prep_thread is not None and self.prep_result is not None:
            self.apply_prep()
        elif self.prep_thread is None and not self.prep_ok and t >= (self.next_prep_try or t):
            self.request_prep(self.start_reason if not d.done("day_start") else "준비 다시")
        if self.recovery_pending() and self.last_recovery_minute != t.strftime("%H:%M"):
            self.last_recovery_minute = t.strftime("%H:%M")
            try:
                self.recover_positions()
            except Exception as e:  # noqa: BLE001  (1분 뒤 다시)
                d.err(f"보유 장부 복구 실패 (1분 뒤 다시): {e!r}")
        if not d.done("prev_settle"):
            step = "prev_settle"
            S.run_step(d, "prev_settle", self.prev_settle, notify=alert)
        if self.om is not None:
            self.om.reconcile()  # 응답 없음 주문의 예정된 다시 조회 (5초·30초·2분, 조회 실패면 1분 뒤)
        if hms < T.get("no_reload_after", "15:18:00") and self.prep_ok:
            step = "official_retry"
            self.retry_official()
        if T["trend_sell_from"] <= hms <= T["trend_sell_until"]:
            if not d.done("trend_sell") and self.trend_wait(hms):
                step = "trend_wait"
            elif not d.done("trend_sell"):
                step = "trend_sell"
                S.run_step(d, "trend_sell", self.trend_sell, self.om, alert)
            elif self.last_trend_minute != t.strftime("%H:%M"):  # 확인 리뷰 N3: SUCCEEDED 뒤에도 매 분 새 대상·다시 보낼 것 확인
                self.last_trend_minute = t.strftime("%H:%M")
                codes = self.om.resendable("trend_sell") + self.trend_new_targets()
                if codes:
                    step = "trend_more"
                    try:
                        self.trend_send(list(dict.fromkeys(codes)), "매 분 확인: 다시 보낼 것·새 대상")
                    except Exception as e:  # noqa: BLE001  (계좌 조회 실패 등: 다음 분에 다시)
                        d.err(f"추세 매도 매 분 확인 오류 (다음 분에 다시): {e!r}")
        if hms >= T.get("data_check", "15:17:00") and not d.done("data_check"):
            step = "data_check"
            S.run_step(d, "data_check", self.data_check, notify=alert)
        if T["snapshot"] <= hms < T["buy_from"] and not d.done("snapshot") and not d.done("late_start"):
            step = "snapshot"
            S.run_step(d, "snapshot", self.snapshot_and_plan, notify=alert)
        if T.get("measure_snapshot") and str(d.date.date()) <= T.get("measure_until", "2026-10-30") \
                and T["measure_snapshot"] <= hms < T["no_new_order_after"] and d.done("snapshot") and not d.done("measure_snapshot"):
            step = "measure_snapshot"
            S.run_step(d, "measure_snapshot", self.measure_snapshot, notify=alert)
        if T["buy_from"] <= hms < T["no_new_order_after"] and not d.done("buy"):  # 11L 보완 2: 매수 마감 하나 = 15:28 전
            step = "buy"
            S.run_step(d, "buy", self.buy, self.om, alert)
        elif T["buy_from"] <= hms < T["no_new_order_after"] and self.last_buy_minute != t.strftime("%H:%M"):  # N2: 매 분 보내지 못한 매수 다시
            self.last_buy_minute = t.strftime("%H:%M")
            ar = d.s.get("buy_account_retry") or {}
            if ar.get("pending") and ar.get("minute") != t.strftime("%H:%M"):  # 11L 보완 3: 매수 전 계좌 조회 일시 실패 -> 매 분 다시 (성공하면 매수)
                step = "buy_retry"
                try:
                    self.buy()
                except Exception as e:  # noqa: BLE001  (다음 분에 다시)
                    d.err(f"매수 다시 (계좌 조회 실패 뒤) 오류: {e!r}"[:300])
            codes = self.om.resendable("buy")
            if codes:
                step = "buy_more"
                try:
                    self.buy_resend(codes)
                except Exception as e:  # noqa: BLE001  (다음 분에 다시)
                    d.err(f"매수 다시 보내기 오류 (다음 분에 다시): {e!r}"[:300])
        if hms >= T["no_new_order_after"] and (d.s.get("buy_account_retry") or {}).get("pending"):  # 15:28 전까지 계좌 조회가 끝내 실패
            with d.lock:
                d.s["buy_account_retry"].update(pending=False, expired=hms)
                d.save()
            self.block_buys("buy_deposit_fail", f"매수 전 주문가능금액 조회가 {T['no_new_order_after'][:5]} 전까지 실패 -> 그날 신규 매수 안 함 (11L 보완 3)",
                            f"[매수 전 예수금 조회 실패] {T['no_new_order_after'][:5]} 전까지 다시 조회했지만 실패 -> 오늘 매수 안 함")
            self.record_buy_decision(False, f"주문가능금액 조회 실패 ({T['no_new_order_after'][:5]} 전까지 다시 시도)")
        if hms > T["no_new_order_after"] and not d.done("buy") and not self.om.unresolved("buy"):
            if not d.s.get("buy_decision"):
                self.record_buy_decision(False, "; ".join(["매수 시각 지남 (재시작·실패 등) (6장)"] + self.buy_gate()))
            d.mark("buy", {**d.s["steps"].get("buy", {}), "sent_late": 0, "reason": "매수 시각 지남 (재시작·실패 등) -> 그날 새 매수 안 함 (6장)"})
        if hms >= T["close_check"] and not d.done("close_check"):
            step = "close_check"
            S.run_step(d, "close_check", self.close_check, notify=alert)
        # 장 마감 후 하위 단계: 정산(15:40 잔고·예수금, 15:35 확인 결과와 무관하게 시작, Codex 13) -> 보유·손절 -> 장부 -> 요약.
        # 각각 SUCCEEDED 면 다시 안 함. 장부(준비 필요)가 실패해도 보유·손절은 계속 (Codex 4)
        # 11I: 무거운 계산은 15:32 부터 별도 스레드 (주 반복·하트비트·정산을 막지 않음). 요약(ac_summary)은 그 결과가 나온 뒤 반영만
        if hms >= T.get("close_calc", "15:32:00") and not d.done("ac_summary"):
            self.start_close_calc()
        if hms >= T.get("settle", "15:40:00"):
            plan = (("settle", self.settle, ()), ("ac_positions", self.ac_positions, ("settle",)), ("ac_ledger", self.ac_ledger, ()),
                    ("ac_summary", self.ac_summary, ("settle",)))
            for name, fn, need in plan:
                if name == "ac_summary" and not d.done(name) and not self.close_ready():
                    step = "close_calc"
                    continue
                if not d.done(name) and all(d.done(n) for n in need):
                    step = name
                    # 확인 리뷰 N2: 거래 정보 확정은 장 마감 후 60초마다 계속 (day_end 까지)
                    S.run_step(d, name, fn, notify=alert, **({"max_attempts": 60} if name == "ac_positions" else {}))
            self.finish_after_close(d)
        return step

    def spawn_monthly(self, month, log_path):
        """monthly.py run {month} 를 창 없이, 낮은 우선순위로 별도 프로세스로 (상주 프로그램이 꺼져도 계속 돎, 장중에 돌아도 본체·손절 감시가 밀리지 않게)"""
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        py = CFG.get("python") or sys.executable
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name == "nt" else 0
        return subprocess.Popen([py, os.path.join(os.path.dirname(os.path.abspath(__file__)), "monthly.py"), "run", month],
                                stdout=open(log_path, "a", encoding="utf-8"), stderr=subprocess.STDOUT, creationflags=flags)

    def kill_monthly(self, pid):
        """15:00 까지 못 끝난 월별 모델 프로세스 끄기 (자식까지)"""
        if self.monthly_proc is not None and getattr(self.monthly_proc, "pid", None) == pid:
            try:
                self.monthly_proc.kill()
            except Exception:  # noqa: BLE001
                pass
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)

    def model_ready(self, month):
        """그 달 모델 파일이 설치돼 있는가 (meta 의 월·시드 파일, 가벼운 확인). 하루 시작 모델 검사에 실패한 월은 아님 (model_bad)"""
        if month in self.model_bad:
            return False
        meta = jload(os.path.join(M.MODEL_DIR, month, "meta.json"), {})
        return meta.get("month") == month and all(os.path.exists(os.path.join(M.MODEL_DIR, month, f"seed{x}.txt")) for x in meta.get("seeds") or [0])

    def monthly_window(self, t):
        """따라잡기 시간창. 반환 (월, "evening"|"morning") 또는 None.
        evening = 그 달 마지막 거래일 하루 끝 뒤 ~ 다음 거래일 하루 시작 전 (밤·주말·휴일), morning = 그 달 첫 거래일 하루 시작 ~ 15:00"""
        import monthly as MO
        hol, day, hms = self.holidays, t.normalize(), t.strftime("%H:%M:%S")
        if S.is_trading_day(day, hol) and hms >= BASE_T["day_start"]:
            if hms >= T["day_end"] and MO.last_trading_day_of_month(day, hol):
                return MO.next_month(day), "evening"
            first = next(x for x in pd.date_range(day.replace(day=1), day) if S.is_trading_day(x, hol))
            if first == day and hms < at(MONTHLY_DEADLINE):
                return day.strftime("%Y-%m"), "morning"
            return None
        prev = S.prev_trading_day(day, hol)
        return (MO.next_month(prev), "evening") if MO.last_trading_day_of_month(prev, hol) else None

    def monthly_catchup(self, t):
        """11K 보완 1: 다음 달 모델이 없거나 하루 시작 검사에 실패했으면 자동으로 monthly.py 를 별도 프로세스로 돌린다.
        - 그 달 마지막 거래일 하루 끝(16:30) 뒤 언제든 (재시작 직후·하루 상태 없이도), 다음 달 첫 거래일 하루 시작(07:00) ~ 15:00 에도 없으면 그 자리에서 다시.
          시간창마다 최대 2번 (결과 없음·실패면 다시). 상태 = state_dir/monthly_{월}.json (재시작해도 살아 있는 pid 를 기다림).
        - 첫 거래일에 끝나면 준비를 다시 적재해 그날 매수 가능 (15:18 전). 15:00 까지 못 끝나면 프로세스를 끄고 그날 신규 매수 안 함 + 알림.
        - 주 반복은 기다리지 않는다 (모델 파일·프로세스 확인은 30초마다, 학습은 낮은 우선순위 별도 프로세스)"""
        import monthly as MO
        if self.monthly_next_check is not None and t < self.monthly_next_check:
            return
        self.monthly_next_check = t + pd.Timedelta(seconds=30)
        w = self.monthly_window(t)
        if w is None:
            return
        month, kind = w
        sp = os.path.join(CFG["state_dir"], f"monthly_{month}.json")
        st = jload(sp, {})
        hms = t.strftime("%H:%M:%S")
        running = st.get("pid") and not st.get("ended")
        if running:
            alive = self.monthly_proc.poll() is None if self.monthly_proc is not None and self.monthly_proc.pid == st["pid"] else pid_alive(st["pid"])
            if alive:
                return
            self.monthly_proc = None
            st["ended"] = t.strftime("%Y-%m-%d %H:%M:%S")
            r = jload(os.path.join(MO.OUT_DIR, f"{month}.json"), {})
            got = r.get("finished") and str(r.get("started", "")) >= str(st.get("started_full", ""))
            st.update(ok=bool(got and r.get("ok")), result=MO.summary_line(r) if got else f"[월별 모델 실패] {month}: 프로세스가 결과 없이 끝남 (로그 {st.get('log')})")
            atomic_json(sp, st)
            alert(st["result"])
            if st["ok"]:
                self.model_bad.discard(month)
                if self.day is not None and self.day.date.strftime("%Y-%m") == month:  # 첫 거래일: 새 모델로 다시 적재 -> 그날 매수 가능
                    self.prep_ok, self.next_prep_try = False, t
                    self.day.set("monthly_model", {**st, "reload": hms})
                return
        if self.model_ready(month):
            return
        n = int(st.get(f"attempts_{kind}", 0))
        if n >= 2:
            if not st.get(f"gave_up_{kind}"):
                st[f"gave_up_{kind}"] = t.strftime("%Y-%m-%d %H:%M:%S")
                atomic_json(sp, st)
                alert(f"[월별 모델 실패] {month}: {kind} 2번 시도 모두 실패 — "
                      + ("다음 달 첫 거래일 07:00 에 다시 시도" if kind == "evening" else "15:00 까지 없으면 오늘 신규 매수 안 함")
                      + f", 수동: python backtest_swing/live/monthly.py run {month}")
            return
        log = os.path.join(LOG_DIR, "monthly", f"{month}.log")
        self.monthly_proc = self.spawn_monthly(month, log)
        st.update(month=month, kind=kind, pid=self.monthly_proc.pid, started_full=t.strftime("%Y-%m-%d %H:%M:%S"), ended=None, ok=None, log=log,
                  **{f"attempts_{kind}": n + 1})
        atomic_json(sp, st)
        rlog(f"월별 모델 시작: {month} ({kind} {n + 1}번째, pid {st['pid']}, 로그 {log})", "INFO")
        if kind == "morning":
            alert(f"[월별 모델 따라잡기] {month} 모델 없음·검사 실패 -> 지금 학습 시작 ({hms}, 15:00 까지 끝나면 오늘 매수 가능)")

    def monthly_deadline(self, t):
        """11K 보완 1: 첫 거래일 15:00 에 그 달 모델이 아직 없으면 학습 프로세스를 끄고 그날 신규 매수 안 함 + 알림 (하루 한 번)"""
        d = self.day
        if d is None or d.s.get("monthly_deadline") or t.strftime("%H:%M:%S") < at(MONTHLY_DEADLINE):
            return
        month = d.date.strftime("%Y-%m")
        first = next(x for x in pd.date_range(d.date.replace(day=1), d.date) if S.is_trading_day(x, self.holidays))
        if first != d.date or self.model_ready(month):
            return
        sp = os.path.join(CFG["state_dir"], f"monthly_{month}.json")
        st = jload(sp, {})
        if st.get("pid") and not st.get("ended"):
            self.kill_monthly(st["pid"])
            self.monthly_proc = None
            st.update(ended=t.strftime("%Y-%m-%d %H:%M:%S"), ok=False, killed=t.strftime("%H:%M:%S"))
            atomic_json(sp, st)
        d.set("monthly_deadline", {"time": t.strftime("%H:%M:%S"), "month": month, "killed": bool(st.get("killed"))})
        self.block_buys("monthly_model_missing", f"{month} 모델이 15:00 까지 없음 -> 그날 신규 매수 안 함 (11K 보완 1)",
                        f"[월별 모델 없음] {month} 모델이 15:00 까지 준비 안 됨" + (" (학습 중단)" if st.get("killed") else "") + " -> 오늘 신규 매수 안 함")

    def check_tz(self, tz_ok=None):
        """B5: 시스템 시간대가 Asia/Seoul 이 아니면 알림 후 매매 안 함 (tick 이 아무것도 하지 않음)"""
        ok = system_tz_ok() if tz_ok is None else tz_ok
        if not ok:
            self.tz_block = True
            alert(f"[시간대 오류] 시스템 시간대가 Asia/Seoul(UTC+9) 이 아님 -> 매매 안 함 (pid {os.getpid()})")
        return ok

    def acquire_single_instance(self):
        """F-02: OS 수준 배타 잠금. 못 잡으면(다른 상주 프로그램이 돌고 있음) False. 정보 파일의 pid 가 살아 있고 resident.py 면 False"""
        lk = SingleInstanceLock(RESIDENT_LOCK)
        if not lk.acquire():
            return False, "OS 잠금을 다른 프로세스가 잡고 있음"
        info = jload(RESIDENT_INFO, {})
        pid = info.get("pid")
        if pid and pid != os.getpid() and pid_alive(pid) and "resident.py" in (process_cmdline(pid) or ""):
            lk.release()
            return False, f"정보 파일의 pid {pid} 가 살아 있는 resident.py"
        self.instance_lock = lk
        atomic_json(RESIDENT_INFO, {"pid": os.getpid(), "started": now().strftime("%Y-%m-%d %H:%M:%S")})
        return True, ""

    def release_single_instance(self):
        """종료할 때: 정보 파일은 자기 pid 일 때만 지우고 잠금을 푼다"""
        try:
            if jload(RESIDENT_INFO, {}).get("pid") == os.getpid():
                os.remove(RESIDENT_INFO)
        except Exception:  # noqa: BLE001
            pass
        if self.instance_lock is not None:
            self.instance_lock.release()

    def run(self):
        if os.path.exists(stop_flag()):
            rlog(f"정지 요청 파일({stop_flag()})이 있어 시작 안 함 — start_resident 로 켜세요", "WARN")
            sys.exit(3)
        ok, why = self.acquire_single_instance()
        if not ok:
            alert(f"[상주 프로그램 시작 거부] pid {os.getpid()}: 이미 실행 중 ({why})")
            sys.exit(2)
        atexit.register(self.release_single_instance)
        with open(CFG["pid_file"], "w") as f:
            f.write(str(os.getpid()))
        self.check_tz()
        n_old = N.resend_pending(ALERTS_PENDING, log_path=ALERTS)  # 11J 보완 6·N7: 지난번에 못 보낸 알림 (보낸 번호만 파일에서 지움)
        if n_old:
            rlog(f"지난 미전송 알림 {n_old}건 다시 보냄", "WARN")
        alert(f"[상주 프로그램 시작] pid {os.getpid()}, env {CFG['env']}, 주문 {'켬' if CFG['orders_enabled'] else '끔'}, 코드 {git_state()}"
              + (", 시간대 오류로 매매 안 함" if self.tz_block else ""))
        if CFG.get("_filled"):  # 11K 보완 6: config.json 에 없어 config.example.json 기본값으로 채운 키
            rlog(f"설정: {CFG.get('_source')} 에 없는 키를 config.example.json 기본값으로 채움 {CFG['_filled']}", "WARN")
        self.start_heartbeat()
        C.RateGate.configure(CFG.get("rate_limits"))  # 11L: 프로세스 전체 공용 키움 호출 제한기 (TR 별 간격, 실제 상주 실행에서만)
        rlog(f"키움 호출 제한기: {C.RateGate.limits}", "INFO")
        self.auto_monthly = True
        while not os.path.exists(stop_flag()):
            step = "idle"
            t0 = time.time()
            try:
                step = self.tick(self.now())
            except Exception as e:  # noqa: BLE001
                msg = f"{step} 오류: {e!r}"[:300]
                if self.day is not None:
                    self.day.err(msg)
                alert(f"[오류] {msg}")
            took = time.time() - t0
            if took >= 10:  # 11I: 주 반복 한 번이 10초 넘으면 기록 (장중 손절 감시가 늦어지는 단계를 찾기 위해)
                rlog(f"주 반복 느림: {step} {took:.1f}초", "WARN")
            self.cur_step, self.last_tick = step, time.time()
            time.sleep(1.0)
        self.hb_stop.set()
        self.shutdown()

    def shutdown(self):
        """정상 종료 (정지 요청 파일): 단계 사이에서만 끝난다 (단계 실행 중에는 그 단계가 끝난 뒤). 계산 프로세스 정리, 잠금 해제"""
        alert(f"[상주 프로그램 정상 종료] pid {os.getpid()} (정지 요청)")
        if self.pool is not None:
            try:
                self.pool.close()
            except Exception:  # noqa: BLE001
                pass
            self.pool = None
        self.release_single_instance()
        N.flush(15, pending_path=ALERTS_PENDING)
        try:
            if open(CFG["pid_file"]).read().strip() == str(os.getpid()):
                os.remove(CFG["pid_file"])
        except Exception:  # noqa: BLE001
            pass



if __name__ == "__main__":
    settings.refuse_test_env("상주 프로그램 (resident.py)")
    Resident().run()
