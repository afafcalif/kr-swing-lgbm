"""
안전 시험 (2026-10-24 결정 (코드 리뷰) A9). 가짜 클라이언트·가짜 시계, 실제 주문·조회 없음.

  python -m unittest backtest_swing/live/test_safety.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import json
import os
import shutil
import sys
import tempfile
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_orders import PLAN, Crash, FakeClock, FakeKiwoom, acct_view, hold, sends  # noqa: E402
import common as C  # noqa: E402
import orders as O  # noqa: E402
import state as S  # noqa: E402


def make(path, clk, fk, notes):
    return O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep, notify=notes.append, store=O.IntentStore(path, clock=clk))


class Restart(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "intents", "2026-10-26.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _crash_then_restart(self, t, first, again, tr):
        clk, notes = FakeClock(t), []
        self.fk = fk = FakeKiwoom(clk)
        hold(fk, "990006", 28)
        fk.script[tr] = ["accept_then_crash"]
        om = make(self.path, clk, fk, notes)
        with self.assertRaises(Crash):
            first(om)
        saved = json.load(open(self.path, encoding="utf-8"))
        self.assertEqual(saved[0]["status"], O.SENDING)          # 보내기 직전에 디스크에 기록됨
        om2 = make(self.path, clk, fk, notes)                    # 재시작
        self.assertEqual(om2.records[0]["status"], O.UNKNOWN)
        again(om2)
        self.assertEqual(len(sends(fk, tr)), 1)                  # 다시 보내지 않음
        om2.reconcile()                                          # 재시작 직후 예정된 조회
        self.assertEqual(om2.records[0]["status"], O.ACCEPTED)
        self.assertEqual(om2.records[0]["ord_no"], "0000001")
        again(om2)
        self.assertEqual(len(sends(fk, tr)), 1)
        return om2

    def test_buy_crash_restart_no_resend(self):
        self._crash_then_restart("2026-10-26 15:21:05", lambda om: om.place_buys(PLAN, "본래"), lambda om: om.place_buys(PLAN, "본래"), "kt10000")

    def test_trend_sell_crash_restart_no_resend(self):
        held = {"990006": {"qty": 28, "sellable": 28, "cur_price": 31000}}
        self._crash_then_restart("2026-10-26 08:31:00", lambda om: om.trend_sells(["990006"], held),   # 다시: 접수된 주문이 매도 가능 수량을 묶음
                                 lambda om: om.trend_sells(["990006"], acct_view(self.fk, held)), "kt10001")

    def test_stop_crash_restart_no_resend(self):
        held = {"990006": {"qty": 28, "sellable": 28, "cur_price": 29000}}
        self._crash_then_restart("2026-10-26 10:00:00", lambda om: om.stop_check(held, {"990006": 30000}),
                                 lambda om: om.stop_check(acct_view(self.fk, held), {"990006": 30000}), "kt10001")

    def test_unknown_then_late_appearance(self):
        clk, notes = FakeClock("2026-10-26 15:21:05"), []
        fk = FakeKiwoom(clk)
        fk.script["kt10000"] = ["timeout_late:20"]   # 접수됐지만 20초 뒤에야 미체결 조회에 보임
        om = make(self.path, clk, fk, notes)
        r = om.place_buys(PLAN, "본래")[0]
        self.assertEqual(r["status"], O.UNKNOWN)
        clk.t += pd.Timedelta(seconds=5)
        om.reconcile()
        self.assertEqual(r["status"], O.UNKNOWN)      # 5초: 아직 안 보임, NOT_SENT 로 확정하지 않음
        om.place_buys(PLAN, "본래")
        self.assertEqual(len(sends(fk)), 1)
        clk.t += pd.Timedelta(seconds=25)
        om.reconcile()
        self.assertEqual(r["status"], O.ACCEPTED)     # 30초: 보임
        self.assertEqual(r["matched_by"][:3], "미체결")

    def test_unclassified_exception_and_bad_json_no_resend(self):
        for act in ("error", "badjson"):
            clk, notes = FakeClock("2026-10-26 15:21:05"), []
            fk = FakeKiwoom(clk)
            fk.script["kt10000"] = [act]
            om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep, notify=notes.append)
            r = om.place_buys(PLAN, "본래")[0]
            self.assertEqual(r["status"], O.UNKNOWN, act)
            om.place_buys(PLAN, "본래")
            self.assertEqual(len(sends(fk)), 1, act)
            self.assertTrue(any("응답 없음" in n for n in notes), act)

    def test_stop_rejected_retry_no_cap_backoff(self):
        clk, notes = FakeClock("2026-10-26 10:00:00"), []
        fk = FakeKiwoom(clk)
        fk.script["kt10001"] = ["reject"] * 6
        om = make(self.path, clk, fk, notes)
        held = {"990006": {"qty": 28, "sellable": 28, "cur_price": 29000}}
        tr = {}
        om.stop_check(held, {"990006": 30000}, triggered=tr)
        self.assertEqual(len(sends(fk, "kt10001")), 1)        # 같은 분에 바로 다시 보내지 않음
        for _ in range(6):
            clk.t += pd.Timedelta(minutes=1)
            om.stop_check(held, {"990006": 30000}, triggered=tr)
        self.assertEqual(len(sends(fk, "kt10001")), 6)        # 11J: 5회 상한 없음 — 거부 5회 뒤 2분 간격 (6분 동안 6번)
        self.assertEqual(sum("주문 거부" in n for n in notes), 6)  # 매번 알림
        # 거부 뒤 접수되면 그 뒤로는 안 보냄
        clk2, notes2 = FakeClock("2026-10-26 10:00:00"), []
        fk2 = FakeKiwoom(clk2)
        fk2.script["kt10001"] = ["reject", "accept"]
        hold(fk2, "990006", 28)
        om2 = O.OrderManager(O.Broker(fk2, allow_orders=True), clock=clk2, sleep=clk2.sleep, notify=notes2.append)
        tr2 = {}
        for _ in range(4):
            om2.stop_check(acct_view(fk2, held), {"990006": 30000}, triggered=tr2)
            clk2.t += pd.Timedelta(minutes=1)
        self.assertEqual(len(sends(fk2, "kt10001")), 2)

    def test_order_step_exception_then_restart(self):
        clk, notes = FakeClock("2026-10-26 15:21:05"), []
        fk = FakeKiwoom(clk)
        dpath = os.path.join(self.dir, "2026-10-26.json")
        d = S.Day(dpath, clk(), clock=clk)
        om = make(self.path, clk, fk, notes)

        def buy_then_fail():
            om.place_buys(PLAN, "본래")
            raise RuntimeError("주문 뒤 기록 중 오류")
        S.run_step(d, "buy", buy_then_fail, om, notes.append)
        self.assertEqual(d.status("buy"), S.UNKNOWN)
        # 재시작
        d2 = S.Day(dpath, clk(), clock=clk)
        d2.recover_after_restart()
        om2 = make(self.path, clk, fk, notes)
        clk.t += pd.Timedelta(seconds=2)
        S.run_step(d2, "buy", lambda: {"results": om2.place_buys(PLAN, "본래")}, om2, notes.append)
        self.assertEqual(d2.status("buy"), S.SUCCEEDED)
        self.assertEqual(len(sends(fk)), 1)
        # 주문 단계가 끝나기 전에 꺼진 경우 (RUNNING 으로 남음) -> UNKNOWN, 의도 확정 못 하면 다시 실행 안 함
        clk3, notes3 = FakeClock("2026-10-26 08:31:00"), []
        fk3 = FakeKiwoom(clk3)
        fk3.script["kt10001"] = ["timeout"]
        p3 = os.path.join(self.dir, "i3.json")
        d3 = S.Day(os.path.join(self.dir, "d3.json"), clk3(), clock=clk3)
        d3.begin("trend_sell")
        om3 = make(p3, clk3, fk3, notes3)
        om3.trend_sells(["990006"], {"990006": {"qty": 5, "sellable": 5, "cur_price": 1}})
        d4 = S.Day(os.path.join(self.dir, "d3.json"), clk3(), clock=clk3)
        d4.recover_after_restart()
        self.assertEqual(d4.status("trend_sell"), S.UNKNOWN)
        om4 = make(p3, clk3, fk3, notes3)
        r = S.run_step(d4, "trend_sell", lambda: om4.trend_sells(["990006"], {"990006": {"qty": 5, "sellable": 5, "cur_price": 1}}), om4)
        self.assertTrue(str(r).startswith("waiting"))
        self.assertEqual(len(sends(fk3, "kt10001")), 1)

    def test_non_order_step_failed_retries_with_backoff(self):
        clk = FakeClock("2026-10-26 15:36:00")
        d = S.Day(os.path.join(self.dir, "d.json"), clk(), clock=clk)
        calls = []

        def f():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("일시 오류")
            return {"ok": True}
        S.run_step(d, "after_close", f)
        self.assertEqual(d.status("after_close"), S.FAILED)
        S.run_step(d, "after_close", f)
        self.assertEqual(len(calls), 1)               # 60초 전에는 다시 안 함
        clk.t += pd.Timedelta(seconds=61)
        S.run_step(d, "after_close", f)
        self.assertEqual(d.status("after_close"), S.SUCCEEDED)
        self.assertEqual(len(calls), 2)


class Calendar(unittest.TestCase):
    def test_trading_days(self):
        hol = S.holidays()
        self.assertFalse(S.is_trading_day("2026-12-25", hol))   # 금요일 성탄절
        self.assertFalse(S.is_trading_day("2026-10-09", hol))   # 한글날
        self.assertFalse(S.is_trading_day("2026-10-24", hol))   # 토요일
        self.assertTrue(S.is_trading_day("2026-10-26", hol))
        self.assertTrue(S.is_trading_day("2026-10-23", hol))

    def test_snapshot_validation(self):
        good = pd.DataFrame({"volume": [100, 50, 0, 10], "dt": ["20261026"] * 4})
        self.assertTrue(S.validate_snapshot(good, "2026-10-26")[0])
        self.assertFalse(S.validate_snapshot(good.assign(volume=0), "2026-10-26")[0])        # 거래량 없음
        self.assertFalse(S.validate_snapshot(good.assign(dt="20261023"), "2026-10-26")[0])   # 시세 날짜가 오늘 아님

    def test_resident_does_nothing_on_weekday_holiday(self):
        import resident as R
        tmp = tempfile.mkdtemp()
        saved, saved_mod = dict(R.CFG), (R.alert, R.LOG_DIR)
        try:
            R.LOG_DIR = os.path.join(tmp, "logs")                  # 상주 로그도 임시 폴더로
            R.CFG["state_dir"] = tmp
            R.CFG["heartbeat_file"] = os.path.join(tmp, "hb.log")
            alerts = []
            R.alert = alerts.append
            res = R.Resident()
            called = []
            res.start_day = lambda *a, **k: called.append("start_day")
            res.trend_sell = lambda: called.append("trend_sell")
            res.snapshot_and_plan = lambda: called.append("snapshot")
            res.buy = lambda: called.append("buy")
            res.after_close = lambda: called.append("after_close")
            res.write_ledger = lambda *a: called.append("ledger")
            for hms in ("07:00:00", "08:31:00", "10:00:00", "15:18:40", "15:22:00", "15:36:00"):
                step = res.tick(pd.Timestamp(f"2026-12-25 {hms}"))
                self.assertEqual(step, "holiday")
            self.assertEqual(called, [])
            d = json.load(open(os.path.join(tmp, "2026-12-25.json"), encoding="utf-8"))
            self.assertEqual(list(d["steps"]), ["holiday"])
            self.assertEqual(sum("휴장일" in a for a in alerts), 1)
        finally:
            R.CFG.clear()
            R.CFG.update(saved)
            R.alert, R.LOG_DIR = saved_mod
            shutil.rmtree(tmp, ignore_errors=True)


class Ledger(unittest.TestCase):
    def test_version_conflict_refused(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "ledger.json")
        C.atomic_json(p, {"as_of": "2026-10-23", "strategy": [], "full": []})
        _, v = C.ledger_read(p)
        self.assertEqual(v, 0)                                                                                    # 버전 없던 장부 = 0
        v2 = C.ledger_write({"as_of": "2026-10-26", "strategy": [], "full": [{"code": "A", "entry": "2026-10-26"}]}, v, p)
        self.assertEqual(v2, 1)
        with self.assertRaises(C.LedgerConflict):                                                                 # 같은 버전을 읽은 다른 작성자
            C.ledger_write({"as_of": "2026-10-26", "strategy": [], "full": []}, v, p)
        d, v3 = C.ledger_read(p)
        self.assertEqual((d["full"], v3), ([{"code": "A", "entry": "2026-10-26"}], 1))
        shutil.rmtree(tmp, ignore_errors=True)

    def test_run_day_shadow_ledger_read_write_same_path(self):
        tmp = tempfile.mkdtemp()
        saved = (C.LEDGER_PATH, C.LEDGER_SHADOW_PATH)
        try:
            C.LEDGER_PATH, C.LEDGER_SHADOW_PATH = os.path.join(tmp, "ledger.json"), os.path.join(tmp, "ledger_shadow.json")
            C.atomic_json(C.LEDGER_PATH, {"as_of": "2026-10-23", "strategy": [], "full": [{"code": "X", "entry": "2026-10-20"}], "version": 4})
            self.assertEqual(C.ledger_target(running=False), C.LEDGER_PATH)
            p = C.ledger_target(running=True)                                          # 상주 프로그램 실행 중 -> 그림자 장부 (없으면 본 장부 복사)
            self.assertEqual(p, C.LEDGER_SHADOW_PATH)
            d, v = C.ledger_read(p)                                                     # 읽은 경로 = 쓸 경로
            self.assertEqual((d["full"], v), ([{"code": "X", "entry": "2026-10-20"}], 4))
            C.ledger_write({"as_of": "2026-10-26", "strategy": [], "full": []}, v, p)
            self.assertEqual(C.ledger_read(C.LEDGER_PATH)[1], 4)                      # 본 장부는 그대로
            self.assertEqual(C.ledger_read(p)[1], 5)
        finally:
            C.LEDGER_PATH, C.LEDGER_SHADOW_PATH = saved
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
