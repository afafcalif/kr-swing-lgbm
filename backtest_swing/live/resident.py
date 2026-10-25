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
import engine as E  # noqa: E402
import model as M  # noqa: E402
import notify as N  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import quotes as Q  # noqa: E402
import state as S  # noqa: E402

T = CFG["times"]
NAMES = ("strategy", "full")
LEDGER = LEDGER_PATH
POSITIONS = os.path.join(CFG["state_dir"], "positions.json")
ALERTS = os.path.join(LOG_DIR, "alerts.log")
BLOCK = os.path.join(CFG["state_dir"], "block_new_buys.json")
APPROVED = os.path.join(CFG["state_dir"], "excluded_approved.json")  # Codex 27: 사전 승인 제외 목록 (수정주가 계수 변경·공식 시세 없음)
RECOVERY_LOOKBACK = 5  # 2차 확인 리뷰 3: 아침 복구는 최근 5거래일의 UNKNOWN 의도·미확정 보유를 항상 찾는다
CAND_AMT = 4_000_000_000  # Codex 27: 대상 후보 = 전날까지 20일 평균 거래대금 40억 원 이상 (exec_prep_probe 3번과 같은 기준)


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
    rlog(f"알림: {msg}")
    N.send(msg, log_path=ALERTS)


def save_positions(pos):
    """positions.json 저장 (직전 파일은 .bak, 저장 시각·버전 기록)"""
    P.save(POSITIONS, pos)


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
        m = self.now().strftime("%Y-%m-%d %H:%M")
        if m == self.last_hb:
            return
        self.last_hb = m
        with open(CFG["heartbeat_file"], "a", encoding="utf-8") as f:
            f.write(f"{self.now():%Y-%m-%d %H:%M:%S} pid={os.getpid()} step={step}\n")

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
        return O.Broker(c, allow_orders=allow)

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
            if p and p.get("ambiguous"):  # 3차 확인 리뷰: 미확정은 수량·평균단가만 계좌로 (손절가·표시 그대로)
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
        rlog(f"하루 시작(핵심): {d.date.date()} ({reason})", "INFO")
        self.broker = self.new_broker()
        self.om = self.new_om(d)
        self.om.store.listener = self.on_intent
        if self.om.store.corrupt:
            d.err(f"의도 파일 손상 -> 오늘 모든 주문 막음: {self.om.store.corrupt}")
            alert(f"[주문 차단] {self.om.store.corrupt} — 오늘 모든 주문을 막음")
        if self.watch_day != d.date:
            self.watch = O.AccountWatch(notify=alert, n_alert=3)
            prev = d.s.get("account_watch", {})
            self.watch.fail_count, self.watch.longest_min = prev.get("fail_count", 0), prev.get("longest_streak_min", 0.0)
            self.watch.log = prev.get("log", [])
            self.mismatch_seen = set()
            self.watch_day = d.date
        pos0 = self.positions()  # 손상 확인 (복구는 tick 에서)
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
            pool, b, cols, index, ledgers, pinfo = R.prepare(day)
            out.update(pool=pool, boosters=b, cols=cols, index=index, ledgers=ledgers, prep=pinfo, frozen=E.frozen_check())
            amt = pool.amt20(day)
            ex = daily.excluded_codes()
            for c in ex.get("fetch_error", []):
                amt[c] = daily.amt20_files(c, day)
            out["amt20"], out["excluded"] = amt, ex
        except Exception as e:  # noqa: BLE001
            out["error"] = repr(e)[:300]
            out["trace"] = traceback.format_exc()[-800:]
        self.prep_result = out

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
            alert(f"[준비 실패] 60초 뒤 다시, 신규 매수는 준비될 때까지 안 함: {out['error']}"[:300])
            self.next_prep_try = self.now() + pd.Timedelta(seconds=60)
            return
        if self.pool is not None and hms >= T.get("no_reload_after", "15:18:00"):
            out["pool"].close()  # B6: 15:18 뒤에는 메모리 다시 적재 안 함 (이전 적재 유지)
            self.prep_ok = True
            d.err(f"준비({self.prep_reason})가 15:18 뒤에 끝남 -> 다시 적재 안 함, 그날 신규 매수 안 함")
            self.block_buys("late_reload", "15:18 뒤 다시 적재 -> 그날 신규 매수 안 함 (B6)", "[다시 적재 늦음] 15:18 뒤 끝남 -> 오늘 신규 매수 안 함")
            return
        if self.pool is not None:
            self.pool.close()
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
        if not (model_ok and up):
            d.err(f"모델 로드 확인 {model_ok}, 전날 일봉 반영 {up} (달력 직전 거래일 {pinfo['data'].get('expected_prev_trading_day')}, "
                  f"데이터 마지막 날 {pinfo['data'].get('data_last_date')}) -> 그날 신규 매수 안 함 (6장, Codex 23)")
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
        for name, fn in (("prev_recovery", self.finalize_recovery), ("exclusions", self.check_exclusions), ("official_vs_1530", self.official_vs_1530)):
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

    def finalize_recovery(self):
        """Codex 5·확인 리뷰 N1: 전날 복구 마무리 (준비 뒤, 공식 일봉이 들어온 다음). 임시 손절가 -> 진입일 종가 - 1.7 x ATR20,
        마지막 날 추세 판정을 못 한 종목 판정, 장부가 갱신 안 된 날들을 차례로 다시 계산해 저장 (실패면 그날 신규 매수 안 함).
        끝나기 전(finalized False)에는 신규 매수 안 함 (buy_gate)"""
        d = self.day
        rec = d.s.get("prev_recovery")
        if not rec or rec.get("finalized"):
            return {"note": "복구할 것 없음"}
        prev = rec["prev"]
        pos = self.positions()
        left = []
        fix = {c: p["entry_date"] for c, p in pos["held"].items() if p.get("stop_temp") and not p.get("entry_temp") and p.get("entry_date")
               and not p.get("ambiguous")}  # 미확정은 후보 중 높은 손절가 유지 (3차 확인 리뷰)
        for c, ed in fix.items():
            j = self.judge([c], ed).get(c)
            if j is None:
                left.append(c)
                continue
            pos["held"][c].update(stop_price=j["stop"], stop_temp=False, stop_basis=f"진입일 {ed} 공식 일봉 종가 - 1.7 x ATR20 ({j['basis']}, 다음 날 복구)")
        need = [c for c, p in pos["held"].items() if p.get("judged_on") != prev and p.get("entry_date") and prev > p["entry_date"]]
        jp = self.judge(need, prev) if need else {}
        for c in need:
            if c not in jp:
                left.append(c)
                continue
            pos["held"][c].update(trend_sell_next_open=jp[c]["trend"], judged_on=prev, judged_by="공식 일봉 (다음 날 복구)")
        save_positions(pos)
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
                rows = R.snap_rows(snap, full)
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

    def official_vs_1530(self):
        """Codex 21: 전날 15:30 1분봉으로 한 판단(종가·손절가·추세)을 공식 일봉으로 다시 계산해 차이만 기록 (판단은 바꾸지 않음)"""
        d = self.day
        prev = str(S.prev_trading_day(d.date, self.holidays).date())
        pos = self.positions()
        codes = [c for c, p in pos["held"].items() if p.get("judged_on") == prev and p.get("judged_by", "15:30 1분봉") == "15:30 1분봉"]
        if not codes:
            return {"n": 0}
        j = self.judge(codes, prev)
        diffs = []
        for c in codes:
            p, o = pos["held"][c], j.get(c)
            if o is None:
                diffs.append({"code": c, "note": "공식 일봉 없음"})
                continue
            row = {"code": c}
            if p.get("close_1530") is not None and abs(float(p["close_1530"]) - o["close"]) > 0.5:
                row.update(close_1530=p["close_1530"], close_official=o["close"])
            if bool(p.get("trend_sell_next_open")) != (o["trend"] and prev > p.get("entry_date", prev)):
                row.update(trend_1530=bool(p.get("trend_sell_next_open")), trend_official=o["trend"])
            if p.get("entry_date") == prev and p.get("stop_price") and abs(float(p["stop_price"]) - o["stop"]) > 0.5:
                row.update(stop_1530=p["stop_price"], stop_official=o["stop"])
            if len(row) > 1:
                diffs.append(row)
        d.set("official_vs_1530", {"date": prev, "checked": len(codes), "diffs": diffs})
        return {"n": len(codes), "diffs": len(diffs)}

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
        if self.now() < last + pd.Timedelta(minutes=10):
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

    def trend_sell(self):
        """08:30 ~ 08:59 추세 매도 + 장 전 계좌 대조 (계좌 조회 실패·보유 불일치면 그날 신규 매수 안 함).
        대상 = 전날 판정(trend_sell_next_open) + 다음 날 시가 매도 목록. 목록은 접수·체결 확인까지 유지 (Codex 12)"""
        d = self.day
        pos = self.positions()
        acct_ok = True
        try:
            held = self.broker.account()["holdings"]
            diff = self.holding_diff(held)
            d.set("premarket_account_check", {"time": self.now().strftime("%H:%M:%S"), "ok": not diff, "diff": diff})
            if diff:
                self.block_buys("premarket_account_mismatch", f"장 전 계좌 대조 불일치 {diff} -> 그날 신규 매수 안 함 (6장)",
                                f"[보유 불일치] 장 전 계좌 대조: {diff} -> 오늘 신규 매수 안 함")
        except Exception as e:  # noqa: BLE001
            acct_ok = False
            self.block_buys("premarket_account_fail", f"장 전 계좌 조회 실패 -> 그날 신규 매수 안 함 (6장): {e!r}",
                            f"[장 전 계좌 조회 실패] 오늘 신규 매수 안 함, 추세 매도는 상태 파일 수량으로: {e!r}")
            held = {c: {"qty": int(p.get("qty", 0)), "sellable": int(p.get("qty", 0)), "cur_price": np.nan} for c, p in pos["held"].items()}
        nos = pos["next_open_sells"]
        for c, p in pos["held"].items():
            if p.get("trend_sell_next_open") and c not in nos:
                nos[c] = {"reason": f"추세 매도 ({p.get('judged_on')} 종가 < 20일선)", "since": str(d.date.date()), "attempts": 0, "last_status": None}
        targets = list(nos)
        missing = [c for c in targets if c not in held or held[c].get("qty", 0) <= 0] if acct_ok else []
        if missing:
            key = "trend_missing:" + ",".join(sorted(missing))
            if key not in self.mismatch_seen:
                self.mismatch_seen.add(key)
                d.err(f"추세 매도 대상이 계좌에 없음 {missing} -> 목록에서 빼고 진행")
                alert(f"[보유 불일치] 추세 매도 대상이 계좌에 없음: {missing}")
            for c in missing:
                nos.pop(c, None)
        recs = self.om.trend_sells([c for c in targets if c not in missing], held, kind_of=lambda c: pos["held"].get(c, {}).get("kind", "본래"))
        self._note_sells(nos, recs)
        save_positions(pos)
        return {"targets": targets, "orders": len([r for r in recs if r.get("intent_id")]), "skipped": [r for r in recs if r.get("skipped")]}

    def _note_sells(self, nos, recs):
        for r in recs:
            c = r.get("code")
            if c in nos:
                nos[c].update(attempts=int(nos[c].get("attempts") or 0) + 1, last_status=r.get("status") or r.get("skipped"),
                              last_try=self.now().strftime("%Y-%m-%d %H:%M:%S"), last_intent=r.get("intent_id"))

    def trend_new_targets(self):
        """확인 리뷰 N3: 오늘 아직 추세 매도 의도가 하나도 없는 대상 (복구로 생긴 trend_sell_next_open, 목록에 새로 들어온 종목)"""
        pos = self.positions()
        targets = list(pos["next_open_sells"]) + [c for c, p in pos["held"].items() if p.get("trend_sell_next_open")]
        sent = {r["code"] for r in self.om.records if r.get("stage") == "trend_sell" and r.get("date") == self.om.date()}
        return [c for c in dict.fromkeys(targets) if c not in sent]

    def trend_send(self, codes, why):
        """08:30 ~ 08:59 안에서 추세 매도를 (다시) 보냄: NOT_SENT(다시 보내도 됨)로 확정된 것 + 새 대상 (계좌 조회 기준 수량,
        같은 날 같은 종목 중복은 의도 기록으로 막음)"""
        held = self.broker.account()["holdings"]
        pos = self.positions()
        nos = pos["next_open_sells"]
        for c in codes:
            p = pos["held"].get(c, {})
            if c not in nos and p.get("trend_sell_next_open"):
                nos[c] = {"reason": f"추세 매도 ({p.get('judged_on')} 종가 < 20일선, {p.get('judged_by', '')})", "since": str(self.day.date.date()),
                          "attempts": 0, "last_status": None}
        recs = self.om.trend_sells([c for c in codes if c in held], held, kind_of=lambda c: pos["held"].get(c, {}).get("kind", "본래"))
        self._note_sells(nos, recs)
        save_positions(pos)
        self.day.err(f"추세 매도 ({why}) {codes}: {[r.get('status') or r.get('skipped') for r in recs]}")
        return recs

    def stop_minute(self):
        """1분 손절 감시: 보유·손절가 = positions.json, 현재가 = 시세 조회(ka10095, 실전 토큰).
        계좌 조회(kt00018)는 보유 수량·현금 대조용, 실패해도 손절 판단은 계속 (연속 3분 실패 알림, 회복 알림)."""
        d = self.day
        caller = self.ctx.setdefault("quote_caller", Caller("prod", 5)) or Caller("prod", 5)

        def quote(codes):
            _, snap, _ = Q.snapshot(caller, codes)
            return dict(zip(snap.code, snap.close))
        pos = self.positions()
        r = O.stop_cycle(self.om, self.watch, pos, quote, self.broker.balance, self.now(), self.mismatch_seen)
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
        bad = R.snap_anomalies(snap, raw)     # 확인 리뷰 27: 응답에는 있는데 데이터 이상으로 계산 입력에서 빼는 종목
        rows = R.snap_rows(snap, raw)
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
        fok = res["filter"]["filter_ok"]
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
        os.makedirs(os.path.join(LOG_DIR, "scores"), exist_ok=True)
        res["eligible"][["code", "score"]].to_parquet(os.path.join(LOG_DIR, "scores", f"{d.date.date()}.parquet"), index=False)
        plan = O.p4_plan(res["ledgers"][name]["top3"], "raw_close", equity, orderable, set(held), kind)
        self.ctx.update(res=res, plan=plan, snap_file=snap_file, idx_vals=idx_vals, rows=rows)
        live = {"filter": res["filter"], "plan_ledger": name, "kind": kind, "plan": plan, "equity_1519": equity, "cash": orderable,
                "equity_basis": {"deposit_field": "d2_entra (D+2 추정예수금)", "d2": d2, "holdings_value_at": "15:19 현재가",
                                 "margin_field": "ord_alow_amt (주문가능금액)", "orderable": orderable},
                "ledgers": {n: [{"code": r.code, "score": float(r.score), "price_1519": float(r.raw_close)} for r in res["ledgers"][n]["top3"].itertuples()]
                            for n in NAMES}, "snap_file": snap_file, "index_values": idx_vals, "ledger_before": self.ctx["ledgers"],
                "snapshot_check": vinfo, "data_version": self.ctx.get("data_version"), "candidate_failures": cf,
                "factor_default_n": res.get("factor_default_n"), "vscale_default_n": res.get("vscale_default_n")}
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

    def compare_measure(self, bars, targets):
        """15:20:05 스냅샷 vs 15:19 봉 종가·15:19 까지 누적 거래량 (bars: code -> 그날 15:30 이하 1분봉), exp_cntr_pric 도 비교"""
        d = self.day
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
        except Exception as e:  # noqa: BLE001  (Codex 2: 조회 실패·필드 없음·NaN 이면 매수 안 함)
            self.block_buys("buy_deposit_fail", f"매수 전 주문가능금액 조회 실패 -> 그날 신규 매수 안 함: {e!r}", f"[매수 전 예수금 조회 실패] 오늘 매수 안 함: {e!r}")
            self.record_buy_decision(False, f"주문가능금액 조회 실패: {e!r}"[:200])
            return {"sent": 0, "reason": "주문가능금액 조회 실패"}
        recs = self.om.place_buys(d.s["live"]["plan"], d.s["live"]["kind"], orderable=orderable, pre_qty=pre_qty)
        sent = sum(1 for r in recs if r.get("intent_id") and r.get("status") != O.NOT_SENT)
        self.record_buy_decision(True, "", {"sent": sent, "orderable": orderable})
        return {"sent": sent, "orderable": orderable, "results": [{k: r.get(k) for k in ("code", "status", "skipped", "intent_id", "cause")} for r in recs]}

    def close_check(self):
        d = self.day
        codes = [r["code"] for r in self.om.records if r["side"] in ("buy", "sell")]
        caller = Caller("prod", 5)
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
        nos = pos["next_open_sells"]
        today = str(d.date.date())
        for x in self.om.records:  # Codex 12: 다 체결된 매도는 목록에서 빼고, 남은 매도는 이유와 함께 넣는다
            if x["side"] == "sell" and x.get("date") == today and x.get("fill_status") == "체결":
                nos.pop(x["code"], None)
        for c in self.om.next_open_sells:
            last = [x for x in self.om.records if x["side"] == "sell" and x["code"] == c]
            e = nos.setdefault(c, {"since": today, "attempts": 0})
            e.update(reason=f"{today} 매도 미체결 ({(last[-1].get('fill_status') if last else '')}) -> 다음 날 시가", last_status=last[-1]["status"] if last else None,
                     last_intent=last[-1]["intent_id"] if last else None)
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
            exp, n = P.expected_cash(p2, fills, d.date.year)
            tol = P.cash_tolerance(n)
            cash = {"prev_day": pday, "prev_d2": p2, "expected": round(exp, 1), "actual_d2": a["d2"], "cash_diff": round(a["d2"] - exp, 1),
                    "tolerance": tol, "n_fills": n, "ok": bool(abs(a["d2"] - exp) <= tol),
                    "basis": "전일 D+2 추정예수금 + 당일 체결 금액 ± 수수료 0.015%·매도세"}
        d.set("cash_check", cash)
        if cash.get("ok") is False:
            alert(f"[현금 차이] D+2 추정예수금 {a['d2']:,.0f} vs 기대 {cash['expected']:,.0f} (차이 {cash['cash_diff']:,.0f}, 허용 {cash['tolerance']:,.0f})")
        rec = {"time": self.now().strftime("%H:%M:%S"), "account": acct, "avg_price": {c: h["avg_price"] for c, h in held.items() if h["qty"] > 0},
               "deposit": a["entr"], "orderable": a["orderable"], "d2": a["d2"], "expected": {c: q for c, q in expect.items() if q}, "diff": diff}
        d.set("settlement", rec)
        if diff:
            alert(f"[정산 불일치] 15:40 계좌 vs 기록: {diff} — 계좌 기준으로 보유 갱신, 다음 날 신규 매수는 장 전 대조 결과로")
        return {"diff_n": len(diff), "cash_ok": cash.get("ok")}

    def _res(self):
        """그날 스냅샷 계산 결과 (재시작했으면 저장한 스냅샷과 그날 장부 상태로 다시 계산)"""
        d = self.day
        res = self.ctx.get("res")
        live = d.s.get("live", {})
        if res is None and live.get("snap_file"):
            if not self.prep_ok or self.pool is None:
                raise S.StepFailed("준비 안 됨 -> 스냅샷 다시 계산 못 함 (준비 끝나면 다시)")
            import run_day as R
            full = pd.read_parquet(live["snap_file"])
            snap = pd.DataFrame({c[2:]: full[c] for c in full.columns if c.startswith("p_")})
            rows = R.snap_rows(snap, full)
            res = E.run_day(self.pool, d.date, rows, self.ctx["index"], live["index_values"], self.ctx["boosters"], self.ctx["cols"],
                            live["ledger_before"])
            self.ctx.update(res=res, rows=rows)
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
        pending = set(unexplained) | (touched & (set(acct) | set(old)) if not fills_ok else set())
        caller = Caller("prod", 5)
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
        for c, q in acct.items():
            o = old.get(c) or {}
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
            else:
                problems.append(f"{c} 15:30 1분봉 없음 -> 추세 판정 못 함")
        for c in [c for c in pos["held"] if c not in acct]:
            pos["held"].pop(c)
        for c in [c for c in pos["next_open_sells"] if c not in acct]:
            pos["next_open_sells"].pop(c)
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

    def ac_summary(self):
        """15:30 1분봉 기준 상위 3 (8장 12 (2)) + 15:20 측정 + 동결 함수 대조 (8장 12 (1), run_day.py rerun) + summary.csv + 요약 알림"""
        d = self.day
        res = self._res()
        live = d.s.get("live", {})
        st = d.s.get("settlement") or {}
        lp = os.path.join(LOG_DIR, f"{d.date.date()}.json")
        if res is not None and os.path.exists(lp):
            import run_day as R
            log = jload(lp, {})
            caller = Caller("prod", 5)
            crow, bars = {}, {}
            for code in list(self.ctx.get("rows") or {}):
                try:
                    bars[code] = Q.bars_today(caller, code, d.date)
                    cv = Q.close_values(bars[code])
                except ValueError:
                    cv = None
                if cv:
                    crow[code] = ({k: cv[k] for k in ("open", "high", "low", "close", "volume", "amount")}, None, None)
            if crow:
                ic = Q.index_close_today(caller, d.date)
                cres = E.run_day(self.pool, d.date, crow, self.ctx["index"], {"kospi": ic["kospi"], "kosdaq": ic["kosdaq"]},
                                 self.ctx["boosters"], self.ctx["cols"], live["ledger_before"])
                log["close_1530"] = {"eligible_n": len(cres["eligible"]), "ledgers": {
                    n: {"top3": [{"code": r.code, "score": float(r.score), "close_1530": float(r.raw_close)} for r in cres["ledgers"][n]["top3"].itertuples()],
                        "overlap_with_1519": len({x["code"] for x in live["ledgers"][n]} & set(cres["ledgers"][n]["top3"].code))} for n in NAMES}}
                targets = set(res["eligible"].code) | set(cres["eligible"].code)
                mres = self.compare_measure(bars, targets)
                if mres is not None and d.s.get("measure_snapshot"):
                    import measure as MS
                    try:
                        mres["top3_compare"] = MS.top3_compare(self.pool, d.date, self.ctx["index"], self.ctx["boosters"], self.ctx["cols"],
                                                               live["ledger_before"], MS.snap_rows(d.s["measure_snapshot"]["file"]),
                                                               d.s["measure_snapshot"]["index_values"], MS.rows_upto(bars), MS.index_upto(caller, d.date))
                    except Exception as e:  # noqa: BLE001
                        mres["top3_compare"] = {"error": repr(e)[:300]}
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
            jdump(lp, log)
            r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_day.py"), "rerun", str(d.date.date())],
                               capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            log = jload(lp, {})
            if r.returncode != 0:
                log.setdefault("errors", []).append("rerun 실패: " + r.stderr[-500:])
                jdump(lp, log)
            for n in NAMES:
                if log.get("rerun", {}).get(n, {}).get("same_3of3") is False:
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
            jdump(lp, log)
            R.summary(log)
        n_ord = len([r for r in self.om.records if r.get("ord_no") and r["side"] in ("buy", "sell")])
        n_fill = len([r for r in self.om.records if r.get("filled_qty")])
        line = (f"[{d.date.date()} 요약] 필터 {'통과' if live.get('filter', {}).get('filter_ok') else '미통과'}, "
                f"{live.get('kind', '-')} 상위 3 {[p['code'] for p in live.get('plan', [])]}, 주문 {n_ord} 체결 {n_fill}, "
                f"보유 {len(st.get('account') or {})}종목, D+2 예수금 {st.get('d2') or 0:,.0f}, 정산 불일치 {len(st.get('diff') or {})}, "
                f"현금 차이 {(d.s.get('cash_check') or {}).get('cash_diff', '-')}, 신규 매수 {'가능' if (d.s.get('buy_decision') or {}).get('new_buy_ok') else '안 함'}, "
                f"오류 {len(d.s['errors'])}")
        alert(line)
        return {"summary": line}

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
            if not (p.get("info_pending") or p.get("provisional_new")) or p.get("ambiguous"):
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
            buy = next((i for i in groups[(a["day"], c)] if i["side"] == "buy"), {})
            t = self.mark_ambiguous(pos, c, a["day"], held, a["why"], base=self.start_trade(a["day"], c, pos), kind=buy.get("kind"))
            a["held_qty"], a["stop_price"] = int(held.get(c, {}).get("qty", 0)), (t or {}).get("stop_price")
        rebuilt = self.rebuild_pending(pos, held, days, stores)
        ambiguous.update({c: {"day": r["day"], "why": r["how"], "held_qty": int(held.get(c, {}).get("qty", 0)),
                              "stop_price": pos["held"].get(c, {}).get("stop_price")} for c, r in rebuilt.items() if r["how"].startswith("미확정")})
        intents = [dict(i, date=i.get("date") or x) for x in days for i in stores[x].items]
        temp, unknown = self.fill_missing(pos, held, days[-1], intents, f"{days[0]} ~ {days[-1]} 복구")
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
                                "ambiguous": ambiguous, "finalized": False})
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
        hms = t.strftime("%H:%M:%S")
        if not (T["day_start"] <= hms < T["day_end"]):
            if self.pool is not None and hms >= T["day_end"]:
                self.pool.close()
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
            if self.pool is not None:
                self.pool.close()
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
            if not d.done("trend_sell"):
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
        if T["stop_from"] <= hms <= T["stop_until"]:
            step = "stop"
            minute = t.strftime("%Y-%m-%d %H:%M")
            if self.last_stop_minute != minute:  # Codex 11
                self.last_stop_minute = minute
                try:
                    self.stop_minute()
                except Exception as e:  # noqa: BLE001
                    d.err(f"stop 오류: {e!r}")
                    alert(f"[오류] stop: {e!r}"[:300])
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
        if T["buy_from"] <= hms <= T["buy_until"] and not d.done("buy"):
            step = "buy"
            S.run_step(d, "buy", self.buy, self.om, alert)
        if hms > T["no_new_order_after"] and not d.done("buy") and not self.om.unresolved("buy"):
            if not d.s.get("buy_decision"):
                self.record_buy_decision(False, "; ".join(["매수 시각 지남 (재시작·실패 등) (6장)"] + self.buy_gate()))
            d.mark("buy", {**d.s["steps"].get("buy", {}), "sent_late": 0, "reason": "매수 시각 지남 (재시작·실패 등) -> 그날 새 매수 안 함 (6장)"})
        if hms >= T["close_check"] and not d.done("close_check"):
            step = "close_check"
            S.run_step(d, "close_check", self.close_check, notify=alert)
        # 장 마감 후 하위 단계: 정산(15:40 잔고·예수금, 15:35 확인 결과와 무관하게 시작, Codex 13) -> 보유·손절 -> 장부 -> 요약.
        # 각각 SUCCEEDED 면 다시 안 함. 장부(준비 필요)가 실패해도 보유·손절은 계속 (Codex 4)
        if hms >= T.get("settle", "15:40:00"):
            plan = (("settle", self.settle, ()), ("ac_positions", self.ac_positions, ("settle",)), ("ac_ledger", self.ac_ledger, ()),
                    ("ac_summary", self.ac_summary, ("settle",)))
            for name, fn, need in plan:
                if not d.done(name) and all(d.done(n) for n in need):
                    step = name
                    # 확인 리뷰 N2: 거래 정보 확정은 장 마감 후 60초마다 계속 (day_end 까지)
                    S.run_step(d, name, fn, notify=alert, **({"max_attempts": 60} if name == "ac_positions" else {}))
            if all(d.done(n) for n in ("settle", "ac_ledger", "ac_positions", "ac_summary")) and not d.done("after_close"):
                left = self.om.unresolved() if self.om is not None else []
                if d.done("close_check") and not left:  # 2차 확인 리뷰 3
                    d.mark("after_close", {"substeps": "settle, ac_positions, ac_ledger, ac_summary", "close_check": d.status("close_check")})
                elif not d.s.get("after_close_held"):
                    d.set("after_close_held", {"time": self.now().strftime("%H:%M:%S"), "close_check": d.status("close_check"),
                                               "unknown_orders": [i["intent_id"] for i in left],
                                               "note": "15:35 확인 미완료 또는 UNKNOWN 주문 -> after_close 안 함, 다음 날 아침 복구에서 대조"})
        return step

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
        alert(f"[상주 프로그램 시작] pid {os.getpid()}, env {CFG['env']}, 주문 {'켬' if CFG['orders_enabled'] else '끔'}, 코드 {git_state()}"
              + (", 시간대 오류로 매매 안 함" if self.tz_block else ""))
        while not os.path.exists(stop_flag()):
            step = "idle"
            try:
                step = self.tick(self.now())
            except Exception as e:  # noqa: BLE001
                msg = f"{step} 오류: {e!r}"[:300]
                if self.day is not None:
                    self.day.err(msg)
                alert(f"[오류] {msg}")
            self.heartbeat(step)
            time.sleep(1.0)
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
        try:
            if open(CFG["pid_file"]).read().strip() == str(os.getpid()):
                os.remove(CFG["pid_file"])
        except Exception:  # noqa: BLE001
            pass



if __name__ == "__main__":
    Resident().run()
