"""
Codex 3차 확인 리뷰(11H, 2026-10-24) R1 ~ R7 재현 시험. 상태 있는 가짜 서버·가짜 시계, 실제 주문·조회·git·logs/exec 없음.

  python -m unittest backtest_swing/live/test_third.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import Base, bal_row, jl  # noqa: E402
from test_confirm import day_state, intents_file  # noqa: E402
from test_orders import PLAN, FakeClock, FakeKiwoom  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import state as S  # noqa: E402

BASE_TRADE = {"qty": 28, "trade_id": "990006-2026-10-20-1", "entry_date": "2026-10-20", "stop_price": 30000.0, "kind": "본래"}


class Ambiguous(Base):
    """R1·R3: 복구가 애매하면 추측해서 확정하지 않는다 (미확정, 손절가는 후보 중 높은 쪽, 신규 매수 차단, 알림)"""

    def setup_day(self, intents, positions_flag=True):
        """10-23: 시작 보유 990006 28주 (손절가 30000). 지금 계좌 28주 평균 33000 -> 새 거래 임시 손절가 = 33000 - 850 = 32150"""
        day_state(self.tmp, "2026-10-23", self.clk, steps=("settle", "ac_summary"), positions_start={"held": {"990006": 28}},
                  positions_baseline={"held": {"990006": dict(BASE_TRADE)}, "trade_seq": 1})
        intents_file(self.tmp, "2026-10-23", intents)
        held = dict(BASE_TRADE)
        if positions_flag:
            held.update(info_pending=True, pending_since="2026-10-23")
        P.save(self.R.POSITIONS, {"held": {"990006": held}, "next_open_sells": {}, "trade_seq": 1})
        self.fk.balance_rows = [bal_row("990006", 28, avg=33000)]
        res = self.started("07:10:00")
        res.day.s["steps"].pop("prev_settle")
        return res

    def assert_ambiguous(self, res):
        p = jl(self.R.POSITIONS)["held"]["990006"]
        self.assertEqual(p["qty"], 28)                                            # 수량은 계좌
        self.assertTrue(p["info_pending"] and p["ambiguous"])
        self.assertEqual(p["stop_price"], 32150.0)                                # 후보 30000 / 32150 중 높은 쪽
        self.assertEqual(set(p["ambiguous"]["candidates"]), {"그날 시작 거래", "그날 새 거래"})
        self.assertTrue(any("복구 미확정" in w for w in res.buy_gate()))           # 신규 매수 안 함
        self.assertTrue(any("[복구 미확정]" in a for a in self.alerts))
        return p

    # ---------------------------------------------------------------- R1
    def test_R1_two_unknown_same_code_left_unconfirmed(self):
        res = self.setup_day([
            {"intent_id": "s1", "date": "2026-10-23", "stage": "stop", "side": "sell", "code": "990006", "qty": 28, "trde_tp": "3",
             "status": "UNKNOWN", "pre_qty": 28, "held_qty_before": 28, "kind": "본래"},
            {"intent_id": "b1", "date": "2026-10-23", "stage": "buy", "side": "buy", "code": "990006", "qty": 28, "trde_tp": "0",
             "status": "UNKNOWN", "pre_qty": 28, "kind": "점검용"}], positions_flag=False)
        r = res.prev_settle()
        st = {i["intent_id"]: i for i in jl(os.path.join(self.tmp, "intents", "2026-10-23.json"))}
        self.assertEqual((st["s1"]["status"], st["b1"]["status"]), (O.UNKNOWN, O.UNKNOWN))   # 잔고 28 = 28 로 둘 다 NOT_SENT 라고 하지 않음
        self.assertIn("R1", st["s1"]["recovery_note"])
        self.assertEqual((r["settled"], r["ambiguous"]), (0, ["990006"]))
        self.assert_ambiguous(res)
        # 준비 뒤 마무리가 미확정 손절가를 공식 일봉 손절가로 낮추지 않는다
        self.judged["990006"] = {"stop": 20000.0, "trend": False, "close": 33000.0, "basis": "x"}
        res.finalize_recovery()
        self.assertEqual(jl(self.R.POSITIONS)["held"]["990006"]["stop_price"], 32150.0)
        # 다음 날 아침 복구도 사람이 고치기 전에는 그대로 (표시를 지우지 않음)
        res2 = self.started("07:20:00")
        res2.day.s["steps"].pop("prev_settle")
        res2.prev_settle()
        self.assertTrue(jl(self.R.POSITIONS)["held"]["990006"]["ambiguous"])

    def test_R1_single_unknown_still_settled_by_balance(self):
        res = self.setup_day([{"intent_id": "s1", "date": "2026-10-23", "stage": "stop", "side": "sell", "code": "990006", "qty": 28,
                               "trde_tp": "3", "status": "UNKNOWN", "pre_qty": 28, "kind": "본래"}], positions_flag=False)
        self.fk.balance_rows = []
        r = res.prev_settle()
        self.assertEqual((r["settled"], r["ambiguous"]), (1, []))
        self.assertEqual(jl(os.path.join(self.tmp, "intents", "2026-10-23.json"))[0]["status"], O.ACCEPTED)   # 0 < 28 -> 체결

    def test_R1_intraday_sell_not_resent_when_buy_unknown_same_code(self):
        def run(with_buy):
            clk = FakeClock("2026-10-26 15:19:00")
            fk = FakeKiwoom(clk)
            fk.balance_rows = [bal_row("990006", 28)]
            om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep)
            fk.script["kt10001"] = ["timeout"]
            om.stop_check({"990006": {"qty": 28, "sellable": 28, "cur_price": 100.0}}, {"990006": 200.0})
            if with_buy:
                clk.at("15:21:05")
                fk.script["kt10000"] = ["timeout"]
                om.place_buys(PLAN, "본래")
            clk.at("15:21:30")                                                    # 매도의 5초·30초·2분 확인이 다 지남
            om.reconcile()
            return om.records[0]
        self.assertEqual(run(False)["status"], O.NOT_SENT)                        # 혼자면 예전처럼 (잔고 그대로 -> 다시 보낼 수 있음)
        s = run(True)
        self.assertEqual(s["status"], O.UNKNOWN)                                  # 같은 종목 매수도 UNKNOWN -> 잔고로 확정 안 함
        self.assertFalse(s.get("resendable"))

    # ---------------------------------------------------------------- R3
    def sell_buy(self, sell_qty, **fill):
        return [{"intent_id": "s1", "date": "2026-10-23", "stage": "stop", "side": "sell", "code": "990006", "qty": sell_qty, "trde_tp": "3",
                 "status": "ACCEPTED", "ord_no": "S1", "filled_qty": 0, "pre_qty": 28, "held_qty_before": 28, "kind": "본래", **fill},
                {"intent_id": "b1", "date": "2026-10-23", "stage": "buy", "side": "buy", "code": "990006", "qty": 28, "trde_tp": "0",
                 "status": "ACCEPTED", "ord_no": "B1", "filled_qty": 28, "unfilled_qty": 0, "pre_qty": 0, "kind": "점검용"}]

    def test_R3_sold_10_of_28_keeps_start_trade(self):
        res = self.setup_day(self.sell_buy(28, filled_qty=10, unfilled_qty=18))
        self.fk.balance_rows = [bal_row("990006", 46, avg=33000)]                # 28 - 10 + 28
        r = res.prev_settle()
        p = jl(self.R.POSITIONS)["held"]["990006"]
        self.assertEqual((p["trade_id"], p["entry_date"], p["stop_price"], p["qty"]), ("990006-2026-10-20-1", "2026-10-20", 30000.0, 46))
        self.assertFalse(p.get("info_pending") or p.get("ambiguous"))            # 요청 28 >= 시작 28 이어도 체결 10 이라 전량 매도 아님
        self.assertEqual(r["ambiguous"], [])

    def test_R3_full_fill_known_is_new_trade(self):
        res = self.setup_day(self.sell_buy(28, filled_qty=28, unfilled_qty=0))
        res.prev_settle()
        p = jl(self.R.POSITIONS)["held"]["990006"]
        self.assertEqual((p["entry_date"], p["kind"]), ("2026-10-23", "점검용"))
        self.assertFalse(p.get("ambiguous"))

    def test_R3_fill_unknown_left_unconfirmed(self):
        res = self.setup_day(self.sell_buy(28))                                   # 매도 체결 수량 모름 (잔고로만 확정된 접수)
        r = res.prev_settle()
        self.assertEqual(r["ambiguous"], ["990006"])
        p = self.assert_ambiguous(res)
        self.assertEqual(p["trade_id"], "990006-2026-10-20-1")                   # 거래 정보는 시작 거래 그대로 두고 표시

    def test_R3_partial_sell_request_below_start_not_ambiguous(self):
        res = self.setup_day(self.sell_buy(10))                                   # 요청 10 < 시작 28: 체결 수량을 몰라도 전량 매도일 수 없음
        res.prev_settle()
        self.assertFalse(jl(self.R.POSITIONS)["held"]["990006"].get("ambiguous"))


class StartSnapshot(Base):
    # ---------------------------------------------------------------- R2
    def test_R2_first_start_after_close_does_not_write(self):
        P.save(self.R.POSITIONS, {"held": {"005930": {"qty": 3, "entry_date": "2026-10-20", "stop_price": 1.0}}, "next_open_sells": {}})
        res = self.started("15:45:00")
        self.assertNotIn("positions_start", res.day.s)
        self.assertIn("15:21:00", res.day.s["positions_start_skipped"]["reason"])

    def test_R2_written_once_before_buy_window_not_overwritten(self):
        P.save(self.R.POSITIONS, {"held": {"005930": {"qty": 3, "entry_date": "2026-10-20", "stop_price": 1.0}}, "next_open_sells": {}})
        res = self.started("10:00:00")
        self.assertEqual(res.day.s["positions_start"], {"time": "10:00:00", "held": {"005930": 3}})
        P.save(self.R.POSITIONS, {"held": {"005930": {"qty": 9, "entry_date": "2026-10-20", "stop_price": 1.0}}, "next_open_sells": {}})
        res2 = self.started("11:00:00")                                           # 재시작
        self.assertEqual(res2.day.s["positions_start"]["held"], {"005930": 3})

    def test_R2_not_first_start_does_not_write(self):
        d = self.R.new_day(self.clk(), clock=self.clk)
        d.set("core_starts", [{"time": "09:00:00", "reason": "예전 코드"}])        # 앞선 시작이 시작 보유를 남기지 않음
        res = self.started("10:00:00")
        self.assertNotIn("positions_start", res.day.s)
        self.assertEqual(res.day.s["positions_start_skipped"]["reason"], "그날 첫 시작이 아님")

    def test_R2_day_start_falls_back_to_previous_settlement(self):
        day_state(self.tmp, "2026-10-22", self.clk, settlement={"account": {"990006": 28}})
        day_state(self.tmp, "2026-10-23", self.clk, steps=("settle",))           # 시작 보유·정산 기준 장부 없음
        res = self.started("07:10:00")
        self.assertEqual(res.day_start_qty("2026-10-23"), {"990006": 28})


class CloseCheck(Base):
    # ---------------------------------------------------------------- R7
    def setup_partial(self, cancel_act):
        res = self.started("15:22:00")
        res.buy()
        r = res.om.records[0]
        self.fk.fill(r["ord_no"], 10, 33350, "15:30:01")
        self.clk.at("15:35:01")
        self.fk.script["kt10003"] = [cancel_act]
        return res, r

    def test_R7_cancel_rejected_but_nothing_left_succeeds(self):
        res, r = self.setup_partial("reject_filled")                             # 취소 거부 = 남은 18주가 그 사이 다 체결
        S.run_step(res.day, "close_check", res.close_check, notify=self.alerts.append)
        self.assertTrue(res.day.done("close_check"))                              # 남은 미체결 0, UNKNOWN 0 -> SUCCEEDED
        self.assertEqual((r["filled_qty"], r["unfilled_qty"], r["fill_status"]), (28, 0, "체결"))   # 체결도 다시 조회해 맞춤
        c = [i for i in res.om.records if i["stage"] == "cancel"][0]
        self.assertEqual(c["status"], O.REJECTED)
        self.assertIn("미체결 0", c["resolved_by"])

    def test_R7_cancel_rejected_and_still_left_retries(self):
        res, r = self.setup_partial("reject")                                     # 원주문 18주가 아직 미체결
        S.run_step(res.day, "close_check", res.close_check, notify=self.alerts.append)
        self.assertFalse(res.day.done("close_check"))
        self.assertIn("남은 잔량", str(res.day.s["steps"]["close_check"]))


class UpdateThird(unittest.TestCase):
    """R4 ~ R6: update.py (가짜 git·켜기/끄기·시험, 정지 요청 파일은 임시 폴더) + 감시 작업이 실제로 켜는지"""

    def setUp(self):
        import common
        import control as K
        import update as U
        import watchdog as W
        self.U, self.K, self.W, self.C = U, K, W, common
        self.tmp = tempfile.mkdtemp()
        self.saved = (U.git, U.head, K.stop, K.start, U.run_tests, U.frozen_status, U.record, U.allowed, K.resident_running, K.info_pid,
                      dict(K.CFG), dict(W.CFG), W.LOG, W.FLAG, W.start, W.N.send, common.resident_running)
        K.CFG["log_dir"] = self.tmp
        W.CFG.update(log_dir=self.tmp, state_dir=self.tmp, pid_file=os.path.join(self.tmp, "r.pid"), heartbeat_file=os.path.join(self.tmp, "hb.log"))
        W.LOG, W.FLAG = os.path.join(self.tmp, "watchdog.log"), os.path.join(self.tmp, "hb.flag")
        self.ops, self.records, self.reset_running = [], [], []
        self.st = {"head": "a" * 40, "running": True, "stop_rcs": [], "start": lambda: 0}

        def git(*args, timeout=120):
            self.ops.append(args[0])
            if args[0] == "pull":
                self.st["head"] = "b" * 40
            if args[0] == "reset":
                self.reset_running.append(self.st["running"])
                self.st["head"] = args[2]
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def stop(*a, **k):                                                        # 진짜 stop 처럼 정지 요청 파일부터 만든다
            self.ops.append("STOP")
            K.make_stop_flag("test")
            rc = self.st["stop_rcs"].pop(0) if self.st["stop_rcs"] else 0
            if rc == 0:
                self.st["running"] = False
            return rc

        def start(*a, **k):                                                       # 진짜 start 처럼 정지 요청 파일을 지운다
            self.ops.append("START")
            if os.path.exists(K.stop_flag()):
                os.remove(K.stop_flag())
            return self.st["start"]()
        U.git, U.head, K.stop, K.start = git, (lambda: self.st["head"]), stop, start
        U.record = lambda msg, level="INFO", notify=True: self.records.append((level, msg))
        U.allowed = lambda t=None: True
        U.frozen_status = lambda: {"ok": True, "tag_diff": [], "frozen_check": {"ok": True}}
        U.run_tests = lambda: {"ok": True, "ran": 150, "failed": "", "bad": [], "seconds": 12.0}
        K.resident_running = lambda: self.st["running"]
        K.info_pid = lambda: 777
        self.launched = []
        W.start = lambda: self.launched.append(1) or 999
        W.N.send = lambda *a, **k: None
        common.resident_running = lambda *a, **k: self.st["running"]

    def tearDown(self):
        U, K, W = self.U, self.K, self.W
        (U.git, U.head, K.stop, K.start, U.run_tests, U.frozen_status, U.record, U.allowed, K.resident_running, K.info_pid,
         kcfg, wcfg, W.LOG, W.FLAG, W.start, W.N.send, self.C.resident_running) = self.saved
        K.CFG.clear()
        K.CFG.update(kcfg)
        W.CFG.clear()
        W.CFG.update(wcfg)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def flag(self):
        return os.path.exists(self.K.stop_flag())

    def new_comes_up_without_heartbeat(self):
        """새 커밋(b)에서 켜면 잠금·pid 는 생기는데 하트비트가 없어 start 실패, 이전 커밋(a)에서는 정상"""
        def s():
            self.st["running"] = True
            return 1 if self.st["head"].startswith("b") else 0
        self.st["start"] = s

    # ---------------------------------------------------------------- R4
    def test_R4_start_failure_stops_stray_before_rollback(self):
        self.new_comes_up_without_heartbeat()
        self.assertEqual(self.U.main("origin", "main"), 0)
        i = self.ops.index("reset")
        self.assertEqual(self.ops[i - 2:i + 2], ["START", "STOP", "reset", "START"])   # 켜기 실패 -> 끄기 -> 되돌리기 -> 켜기
        self.assertEqual(self.reset_running, [False])                              # 떠 있는 프로세스가 꺼진 것을 확인한 뒤에 되돌림
        self.assertEqual(self.st["head"], "a" * 40)
        self.assertEqual(self.records[-1][0], "ERROR")

    def test_R4_stray_cannot_be_stopped_no_rollback_urgent(self):
        self.new_comes_up_without_heartbeat()
        self.st["stop_rcs"] = [0, 1]                                              # 처음 끄기는 됨, 켜기 실패 뒤 떠 있는 것은 못 끔
        self.assertEqual(self.U.main("origin", "main"), 7)
        self.assertNotIn("reset", self.ops)                                       # 돌고 있는 프로세스 아래에서 코드를 되돌리지 않음
        self.assertTrue(self.flag())
        lvl, msg = self.records[-1]
        self.assertIn("[긴급]", msg)
        self.assertIn("실행 중 (pid 777)", msg)                                    # 문구 = 실제 상태
        self.assertIn("작업 관리자", msg)

    # ---------------------------------------------------------------- R5
    def test_R5_stop_failure_clears_flag_and_watchdog_restarts_later(self):
        self.st["stop_rcs"] = [1]
        self.assertEqual(self.U.main("origin", "main"), 6)
        self.assertFalse(self.flag())                                             # 정지 요청 파일 지움
        self.assertIn("정지 요청 파일 없음", self.records[-1][1])
        self.W.main()
        self.assertEqual(self.launched, [])                                       # 잠금이 있는 동안은 감시 작업이 켜지 않음 (중복 없음)
        self.st["running"] = False                                                # 기존 상주 프로그램이 나중에 꺼짐
        self.W.main()
        self.assertEqual(self.launched, [1])                                      # 감시 작업이 다시 켬

    # ---------------------------------------------------------------- R6
    def test_R6_urgent_end_recreates_flag_watchdog_stays_off(self):
        def s():
            self.st["running"] = False
            return 1
        self.st["start"] = s                                                      # 어디서도 켜지지 않음 (start 가 정지 요청 파일을 지움)
        self.assertEqual(self.U.main("origin", "main"), 7)
        self.assertTrue(self.flag())                                              # 마지막에 다시 만듦
        lvl, msg = self.records[-1]
        self.assertIn("[긴급]", msg)
        self.assertIn("상주 프로그램 꺼짐, 정지 요청 파일 있음", msg)
        self.W.main()
        self.assertEqual(self.launched, [])                                       # 감시 작업이 켜지 않음
        with open(self.W.LOG, encoding="utf-8") as f:
            self.assertIn("정지 요청 상태", f.read())

    def test_R6_frozen_changed_keeps_flag(self):
        self.U.frozen_status = lambda: {"ok": False, "tag_diff": ["backtest_swing/stage3_data.py"], "frozen_check": {"ok": True}}
        self.assertEqual(self.U.main("origin", "main"), 4)
        self.assertTrue(self.flag())
        self.assertIn("정지 요청 파일 있음", self.records[-1][1])
        self.W.main()
        self.assertEqual(self.launched, [])


class ControlStartRunning(unittest.TestCase):
    """R4: 정지 요청 파일이 없어도 잠금·pid 가 있으면 그 pid 의 새 하트비트를 확인해야 성공"""

    def setUp(self):
        import control as K
        import watchdog as W
        self.K, self.W = K, W
        self.tmp = tempfile.mkdtemp()
        self.saved = (dict(K.CFG), K.resident_running, K.task_exists, K.RESIDENT_INFO, W.main, K.time.sleep, K.time.time)
        K.CFG["log_dir"], K.CFG["heartbeat_file"] = self.tmp, os.path.join(self.tmp, "hb.log")
        K.RESIDENT_INFO = os.path.join(self.tmp, "info.json")
        K.task_exists = lambda: False
        K.resident_running = lambda: True
        self.W.main = lambda: self.fail("이미 실행 중이면 켜지 않음")
        with open(K.RESIDENT_INFO, "w", encoding="utf-8") as f:
            json.dump({"pid": 777}, f)
        self.hb("2026-10-24 17:00:00 pid=777 step=idle")
        self.t, self.new_at = [0.0], None
        K.time.time = lambda: self.t[0]

        def sleep(s):
            self.t[0] += s
            if self.new_at is not None and self.t[0] >= self.new_at[0]:
                self.hb(self.new_at[1])
                self.new_at = None
        K.time.sleep = sleep

    def tearDown(self):
        K, W = self.K, self.W
        K.CFG.clear()
        K.CFG.update(self.saved[0])
        K.resident_running, K.task_exists, K.RESIDENT_INFO, W.main, K.time.sleep, K.time.time = self.saved[1:]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hb(self, line):
        with open(self.K.CFG["heartbeat_file"], "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def test_R4_running_without_new_heartbeat_fails(self):
        self.assertEqual(self.K.start(wait=10), 1)                                 # 잠금·pid 만 있고 하트비트가 멈춤

    def test_R4_running_with_new_heartbeat_succeeds(self):
        self.new_at = (3.0, "2026-10-24 17:01:00 pid=777 step=idle")
        self.assertEqual(self.K.start(wait=10), 0)

    def test_R4_other_pid_heartbeat_does_not_count(self):
        self.new_at = (3.0, "2026-10-24 17:01:00 pid=7770 step=idle")             # 다른 pid (777 이 앞부분만 같음)
        self.assertEqual(self.K.start(wait=10), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
