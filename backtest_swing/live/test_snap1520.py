"""
11T 시험: 주문 계획 스냅샷 15:18:30 -> 15:20:01 (2026-10-26 ~ 10-30 판정 통과, 11-02 부터). 가짜 서버·가짜 시계, 실제 주문·조회 없음.
- 새 시각에서 계획 -> 15:21 매수, 그 분의 손절 감시 뒤에 스냅샷, 지수 = ka20005 15:19 봉
- 계획이 15:21 에 안 됐으면(조회 실패 60초 뒤 다시·앞 작업 지연) 끝난 직후 매수, 15:26 뒤 새 시도 없음, 15:27 까지 없으면 매수 없음
- 11T 보완 (<커밋> Codex 확인 [P1]): 조회·계산은 별도 스레드 (한 번에 하나). 작업 프로세스 응답 정지 중에도 매 분 손절 감시·15:28 매도 전송,
  작업 프로세스 대기 상한 plan_call_timeout (묶음 전체), 장애 묶음 정리, 계산 중 다시 적재 채택 안 함, 버린 계산의 임시 파일 정리
- 거래시간 변경일(11-19 수능일) 함께 이동, 스냅샷에 묶인 경계(데이터 확인·채택 경계·재수집 마감)의 상대값, config.json 예전 키 무시
- 15:20:05 측정 스냅샷 없음 (10-30 으로 끝)
(재수집 시작·공개 마감은 test_intraday, 다시 적재 채택 경계·버전 고정은 test_swap 이 새 시각으로 확인)

  python -m unittest backtest_swing/live/test_snap1520.py -v
"""
import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import testguard; testguard.install()  # noqa: E401,E402,E702  (첫 줄 유지)
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음
import glob
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_codex  # noqa: E402
from test_codex import Base, bal_row  # noqa: E402
from test_orders import sends, wait_for  # noqa: E402
import engine as E  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import quotes as Q  # noqa: E402
import settings  # noqa: E402
import state as S  # noqa: E402

CODES = ["990010", "005930"]
TOP = pd.DataFrame({"code": ["990010"], "score": [0.9], "raw_close": [33000.0]})
OUT = {"filter": {"filter_ok": True, "prev_kospi_close": 2700.0, "prev_kospi_ma120": 2500.0}, "eligible": TOP[["code", "score"]],
       "market": {}, "errors": [], "ledgers": {n: {"top3": TOP, "busy": [], "skipped_busy": [], "ledger_status": []} for n in ("strategy", "full")}}
HELD = {"qty": 28, "trade_id": "005930-2026-10-20-1", "entry_date": "2026-10-20", "stop_price": 60000.0, "stop_basis": "진입일", "kind": "본래",
        "judged_on": "2026-10-23", "judged_by": "15:30 1분봉", "close_1530": 61000.0, "trend_sell_next_open": False}


class Base1520(Base):
    def setUp(self):
        super().setUp()
        self.calls, self.idx_fail, self.gate = [], 0, None
        saved = self.saved_run_day = E.run_day

        def run_day(*a, **k):
            self.run_kw.append(k)
            if self.gate is not None:
                self.gate.wait(30)                                             # 계산이 오래 걸림 (풀어 줄 때까지)
            return OUT
        self.run_kw = []
        E.run_day = run_day
        self.addCleanup(setattr, E, "run_day", saved)
        self.addCleanup(lambda: self.gate.set() if self.gate is not None else None)

    def now(self):
        return self.clk().strftime("%H:%M:%S")

    def ready(self, date="2026-10-26"):
        """준비 끝난 상주 프로그램 (묶음 있음), 스냅샷·지수·손절 감시는 기록하는 가짜"""
        self.clk.t = pd.Timestamp(f"{date} 15:10:00")
        res = test_codex.Codex.snapshot_ready(self)
        res.clock = self.clk
        dt = pd.Timestamp(date).strftime("%Y%m%d")
        snap = pd.DataFrame({"code": CODES, "open": [33000.0, 60000.0], "high": [33500.0, 61000.0], "low": [32500.0, 59000.0],
                             "close": [33000.0, 60000.0], "volume": [1e5, 1e5], "amount": [3.3e9, 6e9], "dt": [dt] * 2})
        raw = pd.DataFrame({"stk_cd": CODES, "upl_pric": ["+42900", "+78000"], "lst_pric": ["-23100", "-42000"], "base_pric": ["33000", "60000"]})

        def snapshot(caller, cs, **k):
            if threading.current_thread().name == "plan_calc":                 # 손절 감시(주 스레드) 시세는 기록 안 함
                self.calls.append(("snapshot", self.now()))
            return raw, snap[snap.code.isin(cs)].reset_index(drop=True), {"returned": len(cs), "codes": len(cs), "seconds": 1.0}

        def index_bar(caller, day, hhmm):
            self.calls.append(("index_bar", hhmm, self.now()))
            if self.idx_fail:
                self.idx_fail -= 1
                raise RuntimeError(f"ka20005 kospi {hhmm} 봉 없음")
            return {"kospi": 2700.0, "kosdaq": 850.0}, {"kospi": {"bar": dt + hhmm}}
        Q.snapshot, Q.index_bar = snapshot, index_bar
        res.stop_minute = lambda detect=True: self.calls.append(("stop", self.now(), detect)) or {}
        return res

    def settle(self, res):
        """돌고 있는 계산 스레드가 끝날 때까지 (가짜 계산은 바로 끝남)"""
        job = res.plan_job
        if job is not None:
            job["thread"].join(10)

    def tick(self, res, hms, settle=True):
        self.clk.at(hms)
        res.tick(self.clk())
        if settle:
            self.settle(res)

    def run_ticks(self, res, frm, to):
        """frm ~ to (포함) 1초마다 tick"""
        t = pd.Timestamp(f"{self.clk().date()} {frm}")
        end = pd.Timestamp(f"{self.clk().date()} {to}")
        while t <= end:
            self.tick(res, t.strftime("%H:%M:%S"))
            t += pd.Timedelta(seconds=1)

    def bought(self):
        return [s[2]["stk_cd"] for s in sends(self.fk)]


class Timeline(Base1520):
    def test_plan_at_152001_after_stop_then_buy_at_1521(self):
        res = self.ready()
        res.day.s["steps"].pop("data_check")
        for hms in ("15:17:00", "15:18:30"):                                   # 예전 데이터 확인·스냅샷 시각: 아무것도 안 함
            self.tick(res, hms)
            self.assertFalse(res.day.done("data_check") or res.day.done("snapshot"), hms)
        self.tick(res, "15:18:31")                                             # 데이터 확인 = 스냅샷 - 90초
        self.assertEqual(res.day.s["steps"]["data_check"]["time"], "15:18:31")
        self.calls.clear()
        self.tick(res, "15:20:00")                                             # 15:20 손절 감시 (감지 있음)
        self.assertEqual(self.calls, [("stop", "15:20:00", True)])
        self.assertEqual(res.day.status("snapshot"), S.PENDING)
        self.tick(res, "15:20:01")                                             # 계산 스레드 시작 (결과는 다음 tick 에 반영)
        self.assertEqual(self.calls[1:], [("snapshot", "15:20:01"), ("index_bar", "1519", "15:20:01")])   # 종목 시세 뒤 지수 15:19 봉
        self.assertEqual(res.day.status("snapshot"), S.RUNNING)
        self.assertEqual(self.run_kw[-1], {"call_timeout": 60.0})                # 작업 프로세스 대기 상한 60초 (config plan_call_timeout)
        self.tick(res, "15:20:16")                                             # 서브 PC 실측 약 15초 뒤 완성
        self.assertTrue(res.day.done("snapshot"))
        log = test_codex.jl(os.path.join(self.R.LOG_DIR, "2026-10-26.json"))
        self.assertEqual((log["live"]["snapshot"]["at"], log["live"]["snapshot"]["start"], log["live"]["index_values"]),
                         ("15:20:01", "15:20:01", {"kospi": 2700.0, "kosdaq": 850.0}))
        self.assertEqual(log["live"]["timing"]["plan_ready"], "15:20:16")
        self.assertTrue(os.path.exists(os.path.join(self.R.SNAP_DIR, "2026-10-26.parquet")))
        self.assertEqual(glob.glob(os.path.join(self.R.SNAP_DIR, "*.tmp")), [])
        self.assertEqual(self.bought(), [])
        self.tick(res, "15:20:30")
        self.assertEqual(self.bought(), [])                                    # 매수는 15:21 부터
        self.tick(res, "15:21:00")
        self.assertEqual(self.bought(), ["990010"])
        self.assertNotIn("plan_wait", res.day.s)
        for hms in ("15:20:05", "15:22:00", "15:27:00"):
            self.tick(res, hms)
        self.assertNotIn("measure_snapshot", res.day.s["steps"])               # 15:20:05 측정 스냅샷 없음
        self.assertNotIn("plan_given_up", res.day.s)
        self.assertEqual([c for c in self.calls if c[0] == "snapshot"], [("snapshot", "15:20:01")])

    def test_stop_runs_before_snapshot_in_same_tick(self):
        res = self.ready()
        self.tick(res, "15:20:01")                                             # 그 분의 첫 tick 이 15:20:01 이어도 손절 감시가 먼저
        self.assertEqual([c[0] for c in self.calls], ["stop", "snapshot", "index_bar"])
        self.assertTrue(self.calls[0][2])                                      # 15:20 은 감지하는 분

    def test_plan_late_retry_buys_right_after(self):
        """15:20:01 지수 봉 없음 -> 60초 뒤 다시. 15:21:00 에는 계획이 없어 매수 기다림, 다시 시도한 계획이 나온 tick 에 매수"""
        res = self.ready()
        self.idx_fail = 1
        self.tick(res, "15:20:01")
        self.tick(res, "15:20:02")                                             # 실패 반영
        self.assertEqual(res.day.status("snapshot"), S.FAILED)
        self.assertTrue(any(a.startswith("[15:20 스냅샷 실패]") for a in self.alerts))
        self.tick(res, "15:21:00")
        self.assertEqual(self.bought(), [])
        self.assertFalse(res.day.done("buy"))
        self.assertEqual(res.day.s["plan_wait"]["since"], "15:21:00")
        self.assertNotIn("buy_decision", res.day.s)                           # 매수 판단(안 함)을 미리 기록하지 않음
        self.tick(res, "15:21:01")
        self.assertEqual(res.day.s["steps"]["snapshot"]["attempts"], 1)        # 60초 뒤 (실패 반영 15:20:02 기준)
        self.tick(res, "15:21:02")                                             # 다시 시도 시작
        self.assertEqual(res.day.s["steps"]["snapshot"]["attempts"], 2)
        self.tick(res, "15:21:03")                                             # 계획 반영 -> 같은 tick 매수
        self.assertTrue(res.day.done("snapshot"))
        self.assertEqual(self.bought(), ["990010"])
        self.assertEqual(res.day.s["steps"]["buy"]["time"], "15:21:03")

    def test_snapshot_not_started_before_1521_still_plans_and_buys(self):
        """앞 작업이 늦어 15:21 전에 스냅샷을 시작하지 못함 -> 15:21:05 에 시작, 다음 tick 계획·매수 (예전: 15:21 뒤에는 스냅샷 안 함 -> 매수 없음)"""
        res = self.ready()
        self.tick(res, "15:20:00")
        self.tick(res, "15:21:05")
        self.assertEqual(self.calls[1:], [("stop", "15:21:05", False), ("snapshot", "15:21:05"), ("index_bar", "1519", "15:21:05")])
        self.assertEqual(self.bought(), [])
        self.tick(res, "15:21:06")
        self.assertEqual(self.bought(), ["990010"])

    def test_plan_never_ready_no_buy(self):
        """5번 모두 실패 -> 그때 매수 안 함 판단, 15:26 뒤 새 시도 없음"""
        res = self.ready()
        self.idx_fail = 99
        self.run_ticks(res, "15:20:01", "15:28:05")
        starts = [c[2] for c in self.calls if c[0] == "index_bar"]
        self.assertEqual(len(starts), 5)
        self.assertLess(starts[-1], "15:26:00")
        self.assertEqual(self.bought(), [])
        self.assertTrue(res.day.done("buy"))
        self.assertFalse(res.day.s["buy_decision"]["new_buy_ok"])
        self.assertIn("스냅샷 없음", res.day.s["buy_decision"]["reason"])

    def test_no_new_attempt_after_1526_and_alert(self):
        """마지막 실패가 15:25:30 -> 다시 시도 시각(15:26:30)이 새 시도 마감 뒤 -> 15:26 에 포기 + 알림, 매수 없음"""
        res = self.ready()
        self.idx_fail = 99
        self.tick(res, "15:20:01")
        self.tick(res, "15:20:02")
        res.day.s["steps"]["snapshot"]["time"] = "15:25:30"                    # (가정) 앞 시도들이 늦게 끝남
        self.run_ticks(res, "15:25:58", "15:26:01")
        self.assertEqual(len([c for c in self.calls if c[0] == "index_bar"]), 1)
        self.assertIn("plan_given_up", res.day.s)
        self.assertTrue(any(a.startswith("[주문 계획 없음]") for a in self.alerts))
        self.assertTrue(res.day.done("buy"))
        self.assertEqual(self.bought(), [])
        self.assertIn("주문 계획 없음", res.day.s["buy_decision"]["reason"])

    def test_no_wait_when_plan_cannot_run(self):
        """준비 안 됨(묶음 없음)이면 기다리지 않고 15:21 에 매수 판단 (안 함)"""
        res = self.ready()
        res.pool = None
        self.tick(res, "15:20:01")
        self.assertEqual(res.day.status("snapshot"), S.FAILED)
        self.assertIsNone(res.plan_job)
        self.tick(res, "15:21:00")
        self.assertTrue(res.day.done("buy"))
        self.assertIn("스냅샷 없음", res.day.s["buy_decision"]["reason"])
        self.assertEqual([c for c in self.calls if c[0] == "snapshot"], [])

    def test_late_start_after_new_snapshot_time(self):
        """처음 시작이 15:20:01 뒤면 늦은 시작 (예전 15:18:30), 그 전이면 아님"""
        res = self.ready()
        res.pool = None
        res.start_core = lambda d, reason=None: None
        for hms, late in (("15:19:59", False), ("15:20:01", True)):
            with res.day.lock:
                res.day.s["steps"].clear()
                res.day.save()
            self.clk.at(hms)
            res.day, res.started = None, False
            res.tick(self.clk())
            self.assertEqual(res.day.done("late_start"), late, hms)
        self.assertIn("15:20:01", res.day.s["steps"]["late_start"]["note"])


class Threaded(Base1520):
    """11T 보완: 계산이 오래 걸리거나 작업 프로세스가 멈춰도 주 반복(손절 감시·매도 전송)은 매 분 돈다"""

    def test_slow_plan_before_1527_buys(self):
        res = self.ready()
        self.gate = threading.Event()
        self.tick(res, "15:20:01", settle=False)
        for m in range(21, 27):                                                # 15:21 ~ 15:26 계산 중: 매 분 손절 감시, 매수 기다림, 두 번째 시도 없음
            t0 = time.time()
            self.tick(res, f"15:{m}:00", settle=False)
            self.assertLess(time.time() - t0, 2.0)
        self.tick(res, "15:26:30", settle=False)
        self.assertEqual([c[1] for c in self.calls if c[0] == "stop"], ["15:20:01"] + [f"15:{m}:00" for m in range(21, 27)])
        self.assertEqual(len([c for c in self.calls if c[0] == "snapshot"]), 1)  # 한 번에 하나
        self.assertEqual(self.bought(), [])
        self.assertEqual(res.day.status("snapshot"), S.RUNNING)
        self.gate.set()
        self.settle(res)
        self.tick(res, "15:26:31")                                             # 15:27 전에 완성 -> 바로 매수
        self.assertEqual(self.bought(), ["990010"])
        self.assertNotIn("plan_given_up", res.day.s)

    def test_plan_after_1527_discarded_no_buy(self):
        res = self.ready()
        self.gate = threading.Event()
        self.tick(res, "15:20:01", settle=False)
        self.tick(res, "15:26:59", settle=False)
        self.assertFalse(res.day.done("buy"))
        self.tick(res, "15:27:00", settle=False)                               # 마감: 포기 + 알림
        self.assertIn("plan_given_up", res.day.s)
        self.assertTrue(any(a.startswith("[주문 계획 없음]") for a in self.alerts))
        self.assertEqual(res.day.status("snapshot"), S.FAILED)
        self.assertTrue(res.day.done("buy"))
        self.assertIn("주문 계획 없음", res.day.s["buy_decision"]["reason"])
        job = res.plan_stale[0]
        self.gate.set()                                                        # 늦게 끝난 계산
        job["thread"].join(10)
        self.assertEqual(len(glob.glob(os.path.join(self.R.SNAP_DIR, "*.tmp"))), 1)
        self.tick(res, "15:27:30")
        self.assertEqual(res.plan_stale, [])                                   # 버린 계산 정리
        self.assertEqual(glob.glob(os.path.join(self.R.SNAP_DIR, "*.tmp")), [])   # 임시 스냅샷 파일 지움
        self.assertFalse(os.path.exists(os.path.join(self.R.SNAP_DIR, "2026-10-26.parquet")))
        self.assertFalse(res.day.done("snapshot"))
        self.assertNotIn("live", res.day.s)
        self.assertEqual(self.bought(), [])

    def test_worker_hang_stop_sell_at_1528_goes_out(self):
        """작업 프로세스 응답 정지 주입 (실제 E.run_day + 응답 없는 묶음): 계산 스레드가 기다리는 동안 15:28 매도 의무 전송이 나감.
        풀면(응답 없음 시간 초과) 그 묶음은 정리하고 그날 매수 막음, 준비 다시"""
        E.run_day = self.saved_run_day
        hang, seen = threading.Event(), []

        class HungPool:
            closed = False

            def compute(self, day, rows, ledger_from, timeout=None):
                seen.append(timeout)
                hang.wait(30)
                raise E.PoolError(f"작업 프로세스 0 응답 없음 ({timeout}초)")

            def shutdown(self):
                HungPool.closed = True
        self.addCleanup(hang.set)
        res = self.ready()
        pool = res.pool = HungPool()
        real = self.R.Resident.stop_minute.__get__(res)
        res.stop_minute = lambda detect=True: (self.calls.append(("stop", self.now(), detect)), real(detect=detect))[1]
        self.fk.balance_rows = [bal_row("005930", 28)]
        P.save(self.R.POSITIONS, {"held": {"005930": dict(HELD)}, "sell_duty": {}, "next_open_sells": {}, "trade_seq": 1, "duty_format": P.DUTY_FORMAT})
        self.tick(res, "15:20:01", settle=False)
        self.assertTrue(wait_for(lambda: seen == [60.0]))                     # 계산 스레드가 작업 프로세스 응답을 기다림 (상한 60초)
        for m in range(21, 28):
            t0 = time.time()
            self.tick(res, f"15:{m}:00", settle=False)
            self.assertLess(time.time() - t0, 3.0)                              # 주 반복은 기다리지 않음
        pos = P.load(self.R.POSITIONS)[0]                                       # 15:27:50 매도 의무가 남아 있음 (예: 앞선 전송 실패)
        pos["sell_duty"]["005930"] = O.new_duty("stop", "2026-10-26 15:20:00", "현재가 59000 < 손절가 60000", price=59000.0, stop=60000.0)
        P.save(self.R.POSITIONS, pos)
        self.assertEqual(sends(self.fk, "kt10001"), [])
        self.tick(res, "15:28:00", settle=False)
        self.assertEqual([s[2]["stk_cd"] for s in sends(self.fk, "kt10001")], ["005930"])   # 15:28 손절 의무 매도 나감 (계산은 아직 멈춤)
        self.assertEqual([c[1] for c in self.calls if c[0] == "stop"], ["15:20:01"] + [f"15:{m}:00" for m in range(21, 29)])
        self.assertIn("plan_given_up", res.day.s)                              # 15:27 포기
        self.assertEqual(self.bought(), [])
        hang.set()                                                             # 응답 없음 시간 초과
        res.plan_stale[0]["thread"].join(10)
        self.tick(res, "15:28:30", settle=False)
        self.assertTrue(wait_for(lambda: HungPool.closed))                     # 멈춘 묶음 정리 (응답이 늦게 와 파이프가 어긋날 수 있음)
        self.assertIsNot(res.pool, pool)
        self.assertFalse(res.prep_ok)                                          # 15:32 계산용 준비 다시
        self.assertIn("pool_error", res.day.s["buy_block"])

    def test_no_reload_swap_while_plan_running(self):
        """계산 스레드가 도는 동안 끝난 다시 적재는 채택 경계(15:19:31) 전에 끝났어도 바꿔 끼우지 않음 (11S 보완 1, 스냅샷 단계 시작 뒤)"""
        res = self.ready()
        self.gate = threading.Event()
        self.tick(res, "15:20:01", settle=False)
        old = res.pool
        closed = []
        new = type("NewPool", (), {"close": lambda s: closed.append(1)})()
        out = test_codex.ResidentPath.prep_out(self)
        out.update(pool=new, finished="15:19:00")
        res.prep_reason, res.prep_thread, res.prep_result = "장중 재수집 뒤 다시 적재 (15:00: ['000007'])", object(), out
        res.apply_prep()
        self.assertIs(res.pool, old)
        self.assertTrue(wait_for(lambda: closed == [1]))
        self.assertIs(res.plan_job["pool"], old)                               # 계산은 시작 때 잡은 묶음으로
        self.gate.set()
        self.settle(res)
        self.tick(res, "15:20:16")
        self.assertTrue(res.day.done("snapshot"))
        self.assertEqual(self.run_kw[0]["call_timeout"], 60.0)

    def test_day_change_discards_running_plan(self):
        res = self.ready()
        self.gate = threading.Event()
        self.tick(res, "15:20:01", settle=False)
        job = res.plan_job
        res.retire_plan_job("날짜 바뀜 (시험)")
        self.assertIsNone(res.plan_job)
        self.gate.set()
        job["thread"].join(10)
        res.reap_plan()
        self.assertEqual(glob.glob(os.path.join(self.R.SNAP_DIR, "*.tmp")), [])
        self.assertNotIn("live", res.day.s)


class PoolTimeout(unittest.TestCase):
    def test_compute_timeout_is_total_not_per_worker(self):
        """ShardPool.compute(timeout): 묶음 전체 상한 (작업 프로세스 3개가 모두 응답 없음이어도 약 timeout 초, 3배 아님)"""
        class Conn:
            def send(self, m):
                pass

            def poll(self, t):
                time.sleep(min(t, 0.05))
                return False

        class Proc:
            pid, exitcode = 1, None

            def is_alive(self):
                return True
        sp = E.ShardPool.__new__(E.ShardPool)
        sp.lock, sp.shards, sp.call_timeout = threading.RLock(), [["a"], ["b"], ["c"]], 180
        sp.conns, sp.procs = [Conn(), Conn(), Conn()], [Proc(), Proc(), Proc()]
        t0 = time.time()
        with self.assertRaises(E.PoolError) as e:
            sp.compute(pd.Timestamp("2026-10-26"), {}, {}, timeout=0.6)
        self.assertLess(time.time() - t0, 1.5)
        self.assertIn("응답 없음", str(e.exception))


class SpecialDay(Base1520):
    def test_csat_day_moves_snapshot_and_index_bar(self):
        res = self.ready("2026-11-19")                                         # 폐장 16:30 (1시간 늦음)
        T = self.R.T
        self.assertEqual((T["snapshot"], T["data_check"], T["no_reload_after"], T["refetch_start_until"], T["refetch_publish_by"],
                          T["official_retry_until"], T["monthly_deadline"], T["plan_start_until"], T["plan_deadline"]),
                         ("16:20:01", "16:18:31", "16:19:31", "16:11:31", "16:14:31", "16:01:31", "16:01:31", "16:26:00", "16:27:00"))
        self.assertEqual(self.R.index_bar_hhmm(), "1619")
        for hms in ("15:20:01", "16:18:30", "16:20:00"):
            self.tick(res, hms)
            self.assertEqual(res.day.status("snapshot"), S.PENDING, hms)
        self.tick(res, "16:20:01")
        self.assertEqual([c for c in self.calls if c[0] != "stop"], [("snapshot", "16:20:01"), ("index_bar", "1619", "16:20:01")])
        self.tick(res, "16:20:16")
        self.assertTrue(res.day.done("snapshot"))
        self.tick(res, "16:20:59")
        self.assertEqual(self.bought(), [])
        self.tick(res, "16:21:00")
        self.assertEqual(self.bought(), ["990010"])
        self.R.apply_hours(pd.Timestamp("2026-11-20"))
        self.assertEqual((self.R.T["snapshot"], self.R.index_bar_hhmm()), ("15:20:01", "1519"))


class Settings1520(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ex = json.load(open(settings.EXAMPLE, encoding="utf-8"))

    def write(self, times):
        c = json.loads(json.dumps(self.ex))
        c["times"] = times
        p = os.path.join(self.tmp, "config.json")
        json.dump(c, open(p, "w", encoding="utf-8"))
        return settings.load(path=p)

    def test_old_subpc_config_keys_ignored(self):
        """서브 PC config.json = 예전 config.example.json 통째 복사 (snapshot 15:18:30 등) -> 무시하고 15:20:01 기준"""
        old = {k: v for k, v in self.ex["times"].items() if k != "plan_snapshot"}
        old.update(data_check="15:17:00", no_reload_after="15:18:00", snapshot="15:18:30", measure_snapshot="15:20:05",
                   measure_until="2026-10-30", official_retry_until="15:00:00")
        cfg = self.write(old)
        t = cfg["times"]
        self.assertEqual((t["snapshot"], t["data_check"], t["no_reload_after"], t["official_retry_until"], t["refetch_start_until"],
                          t["refetch_publish_by"], t["monthly_deadline"]),
                         ("15:20:01", "15:18:31", "15:19:31", "15:01:31", "15:11:31", "15:14:31", "15:01:31"))
        self.assertNotIn("measure_snapshot", t)
        self.assertNotIn("measure_until", t)
        self.assertIn("times.plan_snapshot", cfg["_filled"])
        self.assertEqual(cfg["_legacy_times"], {"data_check": "15:17:00", "no_reload_after": "15:18:00", "snapshot": "15:18:30",
                                                "measure_snapshot": "15:20:05", "measure_until": "2026-10-30", "official_retry_until": "15:00:00"})

    def test_relative_to_plan_snapshot(self):
        cfg = self.write({**self.ex["times"], "plan_snapshot": "15:18:30"})    # 예전 시각으로 두면 예전 경계 그대로
        t = cfg["times"]
        self.assertEqual((t["snapshot"], t["data_check"], t["no_reload_after"], t["refetch_start_until"], t["refetch_publish_by"],
                          t["official_retry_until"], t["monthly_deadline"]),
                         ("15:18:30", "15:17:00", "15:18:00", "15:10:00", "15:13:00", "15:00:00", "15:00:00"))
        self.assertEqual(cfg["_legacy_times"], {})

    def test_example_defaults(self):
        t = settings.load(path=os.path.join(self.tmp, "none.json"))["times"]
        self.assertEqual((t["plan_snapshot"], t["snapshot"], t["buy_from"], t["no_new_order_after"], t["stop_until"]),
                         ("15:20:01", "15:20:01", "15:21:00", "15:28:00", "15:20:00"))


class IndexBar(unittest.TestCase):
    class Caller:
        def __init__(self, bars):
            self.bars, self.n = bars, []

        def post(self, tr, path, body):
            self.n.append((tr, body["inds_cd"]))
            return {"inds_min_pole_qry": [{"cntr_tm": t, "cur_prc": v} for t, v in self.bars.get(body["inds_cd"], [])]}, None, None

    def test_exact_bar_close(self):
        c = self.Caller({"001": [("20261026152000", "+554800"), ("20261026151900", "+554211"), ("20261026151800", "-553900"),
                                 ("20261023151900", "+540000")],
                         "101": [("20261026151900", "75120"), ("20261026151800", "75100")]})
        v, raw = Q.index_bar(c, pd.Timestamp("2026-10-26"), "1519")
        self.assertEqual(v, {"kospi": 5542.11, "kosdaq": 751.2})              # 15:19 봉 종가 (15:20 봉 아님, 값은 100배)
        self.assertEqual(c.n, [("ka20005", "001"), ("ka20005", "101")])
        self.assertEqual((raw["kospi"]["bar"], raw["kospi"]["last_bar"]), ("202610261519", "20261026152000"))

    def test_missing_bar_raises(self):
        c = self.Caller({"001": [("20261026151800", "+553900"), ("20261023151900", "+540000")], "101": [("20261026151900", "75120")]})
        with self.assertRaises(RuntimeError) as e:
            Q.index_bar(c, pd.Timestamp("2026-10-26"), "1519")                 # 그날 15:19 봉이 아직 없음 (전날 봉은 안 씀)
        self.assertIn("1519 봉 없음", str(e.exception))


if __name__ == "__main__":
    unittest.main()
