"""
Codex 2차 확인 리뷰(11G, 2026-10-24) 1 ~ 10 재현 시험. 상태 있는 가짜 서버·가짜 시계, 실제 주문·조회·git·logs/exec 없음.

  python -m unittest backtest_swing/live/test_second.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import Base, bal_row, jl  # noqa: E402
from test_confirm import day_state, intents_file  # noqa: E402
from test_orders import PLAN, FakeClock, FakeKiwoom, sends  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import state as S  # noqa: E402


class Recovery(Base):
    # ---------------------------------------------------------------- 1
    def setup_overnight(self, sell_qty, sell_fill=None):
        """10-23: 시작 보유 084670 28주(거래 084670-2026-10-20-1) -> 손절 매도 sell_qty 접수, 점검용 매수 28주 접수, 거래 정보 미확정.
        sell_fill: 매도 체결 수량을 알면 그 수량 (15:35 확인으로 미체결 잔량까지 채움), None 이면 체결 수량 모름 (3차 확인 리뷰 R3)"""
        fill = {} if sell_fill is None else {"filled_qty": sell_fill, "unfilled_qty": sell_qty - sell_fill}
        base = {"084670": {"qty": 28, "trade_id": "084670-2026-10-20-1", "entry_date": "2026-10-20", "stop_price": 30000.0, "kind": "본래"}}
        day_state(self.tmp, "2026-10-23", self.clk, steps=("settle", "ac_summary"), positions_start={"held": {"084670": 28}},
                  positions_baseline={"held": base, "trade_seq": 1})
        intents_file(self.tmp, "2026-10-23", [
            {"intent_id": "s1", "date": "2026-10-23", "stage": "stop", "side": "sell", "code": "084670", "qty": sell_qty, "trde_tp": "3",
             "status": "ACCEPTED", "ord_no": "S1", "filled_qty": 0, "pre_qty": 28, "held_qty_before": 28, "kind": "본래", **fill},
            {"intent_id": "b1", "date": "2026-10-23", "stage": "buy", "side": "buy", "code": "084670", "qty": 28, "trde_tp": "0",
             "status": "ACCEPTED", "ord_no": "B1", "filled_qty": 0, "pre_qty": 0, "kind": "점검용"}])
        P.save(self.R.POSITIONS, {"held": {"084670": {**base["084670"], "info_pending": True, "pending_since": "2026-10-23"}},
                                  "next_open_sells": {}, "trade_seq": 1})
        self.fk.balance_rows = [bal_row("084670", 28, avg=33000)]
        res = self.started("07:10:00")
        res.day.s["steps"].pop("prev_settle")
        return res

    def test_1_overnight_rebuy_after_full_sell_is_new_trade(self):
        res = self.setup_overnight(sell_qty=28, sell_fill=28)                    # 3차 R3: 전량 매도는 체결 수량으로 (모르면 미확정, test_third)
        r = res.prev_settle()
        p = jl(self.R.POSITIONS)["held"]["084670"]
        self.assertEqual((p["trade_id"], p["entry_date"], p["kind"], p["stop_temp"]), ("084670-2026-10-23-2", "2026-10-23", "점검용", True))
        self.assertFalse(p.get("info_pending") or p.get("provisional_new") or p.get("pending_since"))
        self.assertIn("새 거래", r["rebuilt"]["084670"]["how"])

    def test_1_partial_sell_keeps_start_of_day_trade(self):
        res = self.setup_overnight(sell_qty=10)
        res.prev_settle()
        p = jl(self.R.POSITIONS)["held"]["084670"]
        self.assertEqual((p["trade_id"], p["entry_date"], p["stop_price"], p["kind"]), ("084670-2026-10-20-1", "2026-10-20", 30000.0, "본래"))
        self.assertFalse(p.get("info_pending"))

    # ---------------------------------------------------------------- 2
    def test_2_unknown_buy_judged_by_pre_qty_not_updated_positions(self):
        day_state(self.tmp, "2026-10-23", self.clk, steps=("settle",), positions_start={"held": {}})
        intents_file(self.tmp, "2026-10-23", [
            {"intent_id": "b1", "date": "2026-10-23", "stage": "buy", "side": "buy", "code": "084670", "qty": 28, "status": "UNKNOWN", "pre_qty": 0,
             "kind": "본래"},
            {"intent_id": "b2", "date": "2026-10-23", "stage": "buy", "side": "buy", "code": "005930", "qty": 5, "status": "UNKNOWN", "kind": "본래"}])
        # 보유 장부는 이미 계좌 수량으로 갱신돼 있음 (예전 방식이면 28 > 28 이 아니라서 NOT_SENT 로 잘못 확정)
        P.save(self.R.POSITIONS, {"held": {"084670": {"qty": 28, "entry_date": "2026-10-23", "stop_price": 1.0},
                                           "005930": {"qty": 5, "entry_date": "2026-10-23", "stop_price": 1.0}}, "next_open_sells": {}})
        self.fk.balance_rows = [bal_row("084670", 28), bal_row("005930", 5)]
        res = self.started("07:10:00")
        res.day.s["steps"].pop("prev_settle")
        res.prev_settle()
        st = {i["intent_id"]: i for i in jl(os.path.join(self.tmp, "intents", "2026-10-23.json"))}
        self.assertEqual(st["b1"]["status"], O.ACCEPTED)
        self.assertIn("pre_qty", st["b1"]["cause"])
        self.assertEqual(st["b2"]["status"], O.ACCEPTED)                       # pre_qty 없음 -> 그날 시작 보유(없음) 기준
        self.assertIn("2026-10-23 시작 보유", st["b2"]["cause"])

    def test_2_buy_records_pre_qty_and_start_snapshot(self):
        self.fk.balance_rows = [bal_row("005930", 3)]
        P.save(self.R.POSITIONS, {"held": {"005930": {"qty": 3, "entry_date": "2026-10-20", "stop_price": 1.0}}, "next_open_sells": {}})
        res = self.started("15:20:00")                                          # 3차 R2: 시작 보유는 15:21 전에만 씀
        self.assertEqual(res.day.s["positions_start"]["held"], {"005930": 3})
        self.clk.at("15:22:00")
        res.buy()
        self.assertEqual(res.om.records[0]["pre_qty"], 0)                        # 084670 은 주문 직전 0주
        self.clk.at("10:00:00")
        om = O.OrderManager(O.Broker(self.fk, allow_orders=True), clock=self.clk, sleep=self.clk.sleep)
        O.stop_cycle(om, O.AccountWatch(), {"held": {"005930": {"qty": 3, "stop_price": 99999.0}}}, lambda cs: {c: 100.0 for c in cs},
                     om.b.balance, self.clk(), set())
        self.assertEqual(om.records[0]["pre_qty"], 3)                            # 손절도 계좌 수량을 pre_qty 로

    # ---------------------------------------------------------------- 3
    def test_3_after_close_needs_close_check_and_no_unknown(self):
        res = self.started("15:22:00")
        self.fk.script["kt10000"] = ["timeout"]
        res.buy()
        self.assertEqual(res.om.records[0]["status"], O.UNKNOWN)
        self.fk.read_script["ka10075"] = ["http502"] * 50
        self.fk.read_script["ka10076"] = ["http502"] * 50
        self.clk.at("15:41:00")
        res.tick(self.clk())
        d = res.day
        for st in ("settle", "ac_positions", "ac_ledger", "ac_summary"):
            self.assertTrue(d.done(st), st)
        self.assertFalse(d.done("close_check"))
        self.assertFalse(d.done("after_close"))                                    # 15:35 확인 미완료 + UNKNOWN 주문
        self.assertEqual(d.s["after_close_held"]["unknown_orders"], [res.om.records[0]["intent_id"]])

    def test_3_recovery_scans_recent_days_even_if_completed(self):
        for x in ("2026-10-21", "2026-10-22", "2026-10-23"):
            day_state(self.tmp, x, self.clk, steps=("after_close",))
        intents_file(self.tmp, "2026-10-21", [{"intent_id": "s1", "date": "2026-10-21", "stage": "stop", "side": "sell", "code": "084670", "qty": 10,
                                              "status": "UNKNOWN", "pre_qty": 10, "kind": "본래"}])
        res = self.started("07:10:00")
        res.day.s["steps"].pop("prev_settle")
        r = res.prev_settle()
        self.assertEqual((r["incomplete_days"], r["settled"]), ([], 1))             # 완료된 날이라도 최근 5거래일 UNKNOWN 은 대조
        self.assertEqual(jl(os.path.join(self.tmp, "intents", "2026-10-21.json"))[0]["status"], O.ACCEPTED)   # 계좌 0 < 10 -> 체결


class SendDeadline(unittest.TestCase):
    # ---------------------------------------------------------------- 4
    def test_4_deadline_passes_during_rate_wait(self):
        clk = FakeClock("2026-10-26 15:27:59.500")
        fk = FakeKiwoom(clk)
        fk.rate_wait = 0.8                                                       # 유량 대기 0.8초 -> 15:28:00.3
        om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep, times={"buy_until": "15:27:59"})
        om.place_buys(PLAN, "본래")
        self.assertEqual(len(sends(fk)), 0)                                      # HTTP 보내기 직전 확인으로 안 보냄
        self.assertEqual(om.records[0]["status"], O.NOT_SENT)
        self.assertIn("마감", om.records[0]["cause"])

    def test_4_rate_wait_cap_not_sent_then_retry_in_window(self):
        clk = FakeClock("2026-10-26 15:22:00")
        fk = FakeKiwoom(clk)
        fk.rate_wait = 3.0                                                       # 상한 2초 초과 -> 기다리지 않음
        om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep)
        om.place_buys(PLAN, "본래")
        self.assertEqual(len(sends(fk)), 0)
        self.assertTrue(all(i["status"] == O.NOT_SENT and "유량" in i["cause"] for i in om.records))
        self.assertEqual(len(om.records), 6)                                     # 1초 간격 다시 5번 (거부 횟수에는 안 셈)
        fk.rate_wait = 0.5
        om2 = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep)
        om2.place_buys(PLAN, "본래")
        self.assertEqual(len(sends(fk)), 1)                                      # 상한 안이면 기다렸다 보냄

    def test_4_real_client_cap_and_before_send(self):
        import kiwoom.client as KC
        from kiwoom.errors import NotSent
        called = []
        c = KC.KiwoomRestClient(settings=SimpleNamespace(base_url="http://mock"), auth=SimpleNamespace(get_token=lambda: "t"))
        c.session = SimpleNamespace(request=lambda **kw: called.append(1))
        c.rate_limiter = KC.RateLimiter(limits_per_sec=1)
        c.rate_limiter.last_call = KC.time.time()                                # 방금 호출 -> 약 1초 기다려야 함
        with self.assertRaises(NotSent):
            c.request("kt10000", "POST", "/x", json={}, retry_429=False, max_wait=0.2)
        c.rate_limiter.last_call = 0.0
        with self.assertRaises(NotSent):
            c.request("kt10000", "POST", "/x", json={}, retry_429=False, max_wait=2.0, before_send=lambda: False)
        self.assertEqual(called, [])                                             # 둘 다 HTTP 를 보내지 않음
        self.assertEqual(O.classify(lambda: (_ for _ in ()).throw(NotSent("유량 대기 상한 2초 초과")))["outcome"], O.NOT_SENT)


class UpdateSafety(unittest.TestCase):
    """update.py 5 ~ 7 (가짜 git·켜기/끄기·시험)"""

    def setUp(self):
        import update as U
        self.U = U
        self.saved = (U.git, U.head, U.K.stop, U.K.start, U.run_tests, U.frozen_status, U.record, U.allowed, U.K.resident_running, dict(U.K.CFG))
        self.tmp = tempfile.mkdtemp()
        U.K.CFG["log_dir"] = self.tmp                                            # 정지 요청 파일도 임시 폴더로 (저장소 logs/ 를 건드리지 않음)
        U.K.resident_running = lambda: False
        self.ops, self.records = [], []
        self.st = {"head": "a" * 40, "pull_rc": 0, "reset_ok": True, "start_rc": 0, "stop_rc": 0}

        def git(*args, timeout=120):
            self.ops.append(args[0])
            if args[0] == "status":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[0] == "pull":
                if self.st["pull_rc"] == 0:
                    self.st["head"] = "b" * 40
                return SimpleNamespace(returncode=self.st["pull_rc"], stdout="", stderr="pull 실패")
            if args[0] == "reset":
                if self.st["reset_ok"]:
                    self.st["head"] = args[2]
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(returncode=128, stdout="", stderr="fatal: Unable to create index.lock")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        U.git, U.head = git, (lambda: self.st["head"])
        U.K.stop = lambda *a, **k: self.ops.append("STOP") or self.st["stop_rc"]
        U.K.start = lambda *a, **k: self.ops.append("START") or self.st["start_rc"]
        U.record = lambda msg, level="INFO", notify=True: self.records.append((level, msg))
        U.allowed = lambda t=None: True
        U.frozen_status = lambda: {"ok": True, "tag_diff": [], "frozen_check": {"ok": True}}
        U.run_tests = lambda: {"ok": True, "ran": 150, "failed": "", "bad": [], "seconds": 12.0}

    def tearDown(self):
        U = self.U
        (U.git, U.head, U.K.stop, U.K.start, U.run_tests, U.frozen_status, U.record, U.allowed, U.K.resident_running, cfg) = self.saved
        U.K.CFG.clear()
        U.K.CFG.update(cfg)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_5_stop_failure_no_pull(self):
        self.st["stop_rc"] = 1
        self.assertEqual(self.U.main("origin", "main"), 6)
        self.assertNotIn("pull", self.ops)
        self.assertNotIn("START", self.ops)                                      # 상주 상태 그대로
        self.assertIn("끄지 못함", self.records[-1][1])

    def test_6_rollback_failure_stays_stopped_urgent(self):
        self.U.run_tests = lambda: {"ok": False, "ran": 150, "failed": "failures=1", "bad": ["FAIL: x"], "seconds": 12.0}
        self.st["reset_ok"] = False
        self.assertEqual(self.U.main("origin", "main"), 7)
        self.assertNotIn("START", self.ops)                                      # 켜지 않음
        lvl, msg = self.records[-1]
        self.assertEqual(lvl, "ERROR")
        self.assertIn("[긴급]", msg)
        self.assertIn("git reset --hard aaaaaaa", msg)                          # 어느 커밋으로, 수동 조치 방법
        self.assertIn("start_resident.bat", msg)

    def test_7_timeout_restarts_on_last_good_commit(self):
        def boom():
            raise subprocess.TimeoutExpired("unittest", 1800)
        self.U.run_tests = boom
        self.assertEqual(self.U.main("origin", "main"), 8)
        self.assertEqual(self.st["head"], "a" * 40)                               # 이전 커밋으로 맞추고
        self.assertEqual(self.ops[-1], "START")                                  # 다시 켬
        self.assertIn("TimeoutExpired", self.records[-1][1])

    def test_7_start_failure_on_new_falls_back_to_old(self):
        starts = []

        def start(*a, **k):
            starts.append(self.st["head"][:1])
            return 1 if self.st["head"].startswith("b") else 0
        self.U.K.start = start
        self.assertEqual(self.U.main("origin", "main"), 0)
        self.assertEqual(starts, ["b", "a"])                                      # 새 커밋에서 못 켜면 이전 커밋으로 켬
        self.assertEqual(self.records[-1][0], "ERROR")

    def test_7_cannot_restart_anywhere_urgent(self):
        self.st["start_rc"] = 1
        self.assertEqual(self.U.main("origin", "main"), 7)
        self.assertIn("[긴급]", self.records[-1][1])


class ControlStart(unittest.TestCase):
    # ---------------------------------------------------------------- 8
    def setUp(self):
        import control as K
        import watchdog as W
        self.K, self.W = K, W
        self.tmp = tempfile.mkdtemp()
        self.saved = (dict(K.CFG), K.resident_running, K.task_exists, K.RESIDENT_INFO, K.RESIDENT_LOCK, W.main, K.time.sleep, K.time.time)
        K.CFG["log_dir"], K.CFG["pid_file"], K.CFG["heartbeat_file"] = self.tmp, os.path.join(self.tmp, "r.pid"), os.path.join(self.tmp, "hb.log")
        K.RESIDENT_INFO, K.RESIDENT_LOCK = os.path.join(self.tmp, "info.json"), os.path.join(self.tmp, "r.lock")
        K.task_exists = lambda: False
        self.t = [0.0]
        K.time.time = lambda: self.t[0]
        K.time.sleep = lambda s: self.t.__setitem__(0, self.t[0] + s)

    def tearDown(self):
        K, W = self.K, self.W
        K.CFG.clear()
        K.CFG.update(self.saved[0])
        K.resident_running, K.task_exists, K.RESIDENT_INFO, K.RESIDENT_LOCK, W.main, K.time.sleep, K.time.time = self.saved[1:]
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def launch(self, pid=777, heartbeat=True):
        def main():
            self.state["running"] = True
            with open(self.K.RESIDENT_INFO, "w", encoding="utf-8") as f:
                json.dump({"pid": pid}, f)
            if heartbeat:
                with open(self.K.CFG["heartbeat_file"], "w", encoding="utf-8") as f:
                    f.write(f"2026-10-24 17:00:00 pid={pid} step=idle\n")
        return main

    def test_8_waits_for_stopping_resident_then_starts(self):
        open(self.K.stop_flag(), "w").close()
        self.state = {"running": True, "stop_at": 5.0}
        self.K.resident_running = lambda: self.state["running"] and (self.t[0] < self.state["stop_at"] or self.state.get("restarted"))
        launch = self.launch()

        def main():
            self.state["restarted"] = True
            launch()
        self.W.main = main
        self.assertEqual(self.K.start(wait=10, stop_wait=30), 0)
        self.assertFalse(os.path.exists(self.K.stop_flag()))
        self.assertTrue(self.state.get("restarted"))                              # 끝난 뒤에 켬

    def test_8_still_stopping_returns_failure(self):
        open(self.K.stop_flag(), "w").close()
        self.state = {"running": True}
        self.K.resident_running = lambda: True
        self.W.main = lambda: self.fail("켜면 안 됨")
        self.assertEqual(self.K.start(wait=10, stop_wait=5), 1)
        self.assertTrue(os.path.exists(self.K.stop_flag()))

    def test_8_success_only_when_really_running(self):
        self.state = {"running": False}
        self.K.resident_running = lambda: self.state["running"]
        self.W.main = self.launch(heartbeat=False)                                # 잠금은 잡았는데 하트비트가 없음
        self.assertEqual(self.K.start(wait=5), 1)


class Encoding(unittest.TestCase):
    # ---------------------------------------------------------------- 10
    def test_10_scripts_safe_under_cp949(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        try:
            out = subprocess.run(["git", "ls-files", "*.bat", "*.cmd", "*.ps1", "requirements*.txt", "backtest_swing/live/*.txt"],
                                 cwd=root, capture_output=True, text=True, timeout=30).stdout.split()
        except Exception:  # noqa: BLE001
            self.skipTest("git 없음")
        bad = []
        for f in out:
            b = open(os.path.join(root, f), "rb").read()
            if all(x < 0x80 for x in b):
                continue
            if f.endswith(".ps1") and b.startswith(b"\xef\xbb\xbf"):              # PowerShell 5.1 은 BOM 있는 UTF-8 을 바르게 읽음
                continue
            bad.append(f)                                                         # .bat·.cmd·.txt 는 ASCII 만
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
