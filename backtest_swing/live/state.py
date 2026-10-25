"""
하루 상태·단계 실행·거래일 (EXEC_SPEC 7장, 2026-10-24 결정 (코드 리뷰) R2-02·R2-09~12).

- 단계 상태: PENDING / RUNNING / SUCCEEDED / FAILED / UNKNOWN. done() 은 SUCCEEDED 일 때만 참.
  주문 단계(trend_sell, buy)가 예외로 끝나면 UNKNOWN -> 주문 의도 대조(orders.OrderManager.reconcile) 뒤에만 다시 실행.
  주문 없는 단계는 FAILED 면 간격을 두고 다시 실행. 재시작 때 RUNNING 으로 남은 단계 = 도중에 꺼진 것 -> 주문 단계 UNKNOWN, 나머지 FAILED.
- 상태 저장은 모두 같은 잠금(RLock) 안에서 원자적 쓰기.
- 거래일: 주말 + krx_holidays.json 휴장일이 아니면 거래일. 스냅샷은 거래량 있는 종목 비율·시세 날짜로도 확인.
"""
import copy
import json
import os
import threading
import traceback

import pandas as pd

from common import atomic_json, now_kst

PENDING, RUNNING, SUCCEEDED, FAILED, UNKNOWN = "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN"
ORDER_STEPS = {"trend_sell": "trend_sell", "buy": "buy"}  # 단계 -> 주문 의도 stage
HOLIDAYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "krx_holidays.json")


class StepFailed(Exception):
    def __init__(self, msg, info=None):
        super().__init__(msg)
        self.info = info or {}


# ---------------------------------------------------------------- 거래일

def holidays(path=HOLIDAYS_FILE):
    d = json.load(open(path, encoding="utf-8"))
    return {pd.Timestamp(x) for k, v in d.items() if not k.startswith("_") for x in v}


def is_trading_day(day, hol=None):
    day = pd.Timestamp(day).normalize()
    hol = holidays() if hol is None else hol
    return day.weekday() < 5 and day not in hol


def prev_trading_day(day, hol=None):
    """휴장일 달력으로 계산한 직전 거래일 (Codex 23)"""
    hol = holidays() if hol is None else hol
    d = pd.Timestamp(day).normalize() - pd.Timedelta(days=1)
    for _ in range(30):
        if is_trading_day(d, hol):
            return d
        d -= pd.Timedelta(days=1)
    raise ValueError(f"{day} 앞 30일 안에 거래일 없음")


def validate_snapshot(snap, day, min_traded=0.5, min_today=0.9):
    """스냅샷 확인: 거래량 > 0 종목 비율 >= min_traded, 시세 날짜(dt) = 오늘 비율 >= min_today. 반환 (ok, info)"""
    n = len(snap)
    if n == 0:
        return False, {"n": 0, "reason": "응답 0종목"}
    if "dt" not in snap:  # 2026-10-24 F-05: 시세 날짜 칸이 없으면 실패
        return False, {"n": n, "reason": "시세 날짜(dt) 칸 없음"}
    traded = float((snap.volume > 0).mean())
    dts = pd.to_datetime(snap.dt.astype(str).str[:8], format="%Y%m%d", errors="coerce")
    if dts.isna().all():
        return False, {"n": n, "reason": "시세 날짜(dt) 파싱 실패"}
    today = float((dts == pd.Timestamp(day).normalize()).mean())  # 파싱 실패 행은 오늘이 아닌 것으로
    ok = traded >= min_traded and today >= min_today
    info = {"n": n, "traded_ratio": round(traded, 4), "today_dt_ratio": round(today, 4)}
    if not ok:
        info["reason"] = f"거래량 있는 종목 {traded:.1%} (기준 {min_traded:.0%}) / 시세 날짜 오늘 {today:.1%} (기준 {min_today:.0%})"
    return ok, info


# ---------------------------------------------------------------- 하루 상태

class Day:
    def __init__(self, path, date, clock=now_kst):
        self.date = pd.Timestamp(date).normalize()
        self.path, self.clock = path, clock
        self.listener = None  # 단계 변화를 사람이 읽을 기록으로 남기는 함수 (상주 프로그램이 붙임): listener(kind, step, entry_or_msg)
        self.lock = threading.RLock()
        try:
            self.s = json.load(open(path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self.s = {"date": str(self.date.date()), "steps": {}, "orders": [], "errors": [], "restarts": 0}
        for k, v in self.s["steps"].items():  # 옛 형식(상태 없음) = 끝난 단계
            if isinstance(v, dict) and "status" not in v:
                v["status"], v["legacy"] = SUCCEEDED, True

    def hms(self):
        return self.clock().strftime("%H:%M:%S")

    def status(self, step):
        return self.s["steps"].get(step, {}).get("status", PENDING)

    def done(self, step):
        return self.status(step) == SUCCEEDED

    def recover_after_restart(self):
        """재시작 때 RUNNING 으로 남은 단계: 주문 단계 UNKNOWN, 나머지 FAILED"""
        with self.lock:
            for k, v in self.s["steps"].items():
                if v.get("status") == RUNNING:
                    v["status"] = UNKNOWN if k in ORDER_STEPS else FAILED
                    v["recovered"] = f"재시작 때 RUNNING ({self.hms()})"
            self.save()

    def begin(self, step):
        with self.lock:
            old = self.s["steps"].get(step, {})
            self.s["steps"][step] = {**old, "status": RUNNING, "started": self.hms(), "attempts": old.get("attempts", 0) + 1}
            self.save()
        self._emit("begin", step, self.s["steps"][step])

    def finish(self, step, status, info=None):
        """단계 끝. info 에 status·time 이 있어도 덮어쓰지 못하게 마지막에 둔다 (Codex 5)"""
        with self.lock:
            extra = {k: v for k, v in (info or {}).items() if k not in ("status", "time")}
            self.s["steps"].setdefault(step, {}).update({**extra, "status": status, "time": self.hms()})
            self.save()
        self._emit("finish", step, self.s["steps"][step])

    def mark(self, step, info=None, status=SUCCEEDED):
        with self.lock:
            extra = {k: v for k, v in (info or {}).items() if k not in ("status", "time")}
            self.s["steps"][step] = {**extra, "time": self.hms(), "status": status}
            self.save()
        self._emit("mark", step, self.s["steps"][step])

    def can_retry(self, step, backoff_s=60, max_attempts=5):
        v = self.s["steps"].get(step, {})
        if v.get("attempts", 0) >= max_attempts:
            return False
        t = v.get("time") or v.get("started")
        if not t:
            return True
        last = pd.Timestamp(f"{self.date.date()} {t}")
        return (self.clock() - last).total_seconds() >= backoff_s

    def err(self, msg):
        with self.lock:
            self.s["errors"].append(str(msg)[:800])
            self.save()
        self._emit("err", None, str(msg))

    def _emit(self, kind, step, obj):
        if self.listener is not None:
            try:
                self.listener(kind, step, obj)
            except Exception:  # noqa: BLE001  (기록 실패가 매매를 막지 않게)
                pass

    def set(self, key, value):
        with self.lock:
            self.s[key] = value
            self.save()

    def save(self):
        """저장 시각·버전을 함께 기록 (2026-10-24 B3)"""
        with self.lock:
            self.s["_version"] = int(self.s.get("_version", 0)) + 1
            self.s["_saved_at"] = self.clock().strftime("%Y-%m-%d %H:%M:%S")
            atomic_json(self.path, copy.deepcopy(self.s))


def run_step(day, name, fn, om=None, notify=lambda m: None, max_attempts=5, backoff_s=60):
    """단계 하나 실행. 반환: 실행 결과 dict, 또는 건너뛴 이유 문자열. 주문 없는 단계가 FAILED 면 backoff_s 간격, max_attempts 회까지 다시."""
    st = day.status(name)
    if st == SUCCEEDED:
        return "done"
    stage = ORDER_STEPS.get(name)
    if stage:
        if st in (UNKNOWN, RUNNING):
            om.reconcile(stage, force=True)
            if om.unresolved(stage):
                return "waiting: 주문 의도 확정 전 (재실행 안 함)"
    elif st == FAILED and not day.can_retry(name, backoff_s, max_attempts):
        return "backoff"
    day.begin(name)
    try:
        info = fn() or {}
    except StepFailed as e:
        status = UNKNOWN if stage else FAILED  # 2026-10-24: 주문 단계는 어떤 예외로 끝나도 UNKNOWN
        day.finish(name, status, {"error": str(e)[:300], **e.info})
        return f"{status.lower()}: {e}"
    except Exception as e:  # noqa: BLE001
        status = UNKNOWN if stage else FAILED
        day.finish(name, status, {"error": repr(e)[:300]})
        day.err(f"{name} 오류 ({status}): {e!r} | {traceback.format_exc()[-500:]}")
        notify(f"[오류] {name} ({status}): {e!r}"[:300])
        return f"{status.lower()}: {e!r}"
    day.finish(name, SUCCEEDED, info if isinstance(info, dict) else {"result": info})
    return info
