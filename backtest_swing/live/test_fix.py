"""
수정 확인 리뷰(F-01 ~ F-07)·B 항목 시험 (2026-10-24). 가짜 클라이언트·가짜 시계, 실제 주문·조회 없음.

  python -m unittest backtest_swing/live/test_fix.py -v
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
from test_orders import PLAN, FakeClock, FakeKiwoom, acct_view, sends  # noqa: E402
import common as C  # noqa: E402
import engine as E  # noqa: E402
import orders as O  # noqa: E402
import state as S  # noqa: E402


def bal_row(code, qty, sellable=None):
    return {"stk_cd": f"A{code}", "rmnd_qty": str(qty), "trde_able_qty": str(qty if sellable is None else sellable), "pur_pric": "10000", "cur_prc": "9000"}


class Orders(unittest.TestCase):
    def test_f01_stop_skips_code_not_in_account(self):
        clk, notes = FakeClock("2026-10-26 10:00:00"), []
        fk = FakeKiwoom(clk)
        om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep, notify=notes.append)
        watch = O.AccountWatch(notify=notes.append)
        pos = {"held": {"084670": {"qty": 28, "stop_price": 30000}}}
        r = O.stop_cycle(om, watch, pos, lambda cs: {c: 29000 for c in cs}, lambda: {"acnt_evlt_remn_indv_tot": []}, clk(), set())
        self.assertEqual(len(sends(fk, "kt10001")), 0)               # 계좌 조회 성공, 계좌에 없음 -> 팔지 않음
        self.assertEqual(r["not_in_account"], ["084670"])
        self.assertEqual(sum("보유 불일치" in n for n in notes), 1)
        def fail():
            raise O.NoResponse("502")
        clk.t += pd.Timedelta(minutes=1)
        O.stop_cycle(om, watch, pos, lambda cs: {c: 29000 for c in cs}, fail, clk(), set())
        self.assertEqual(len(sends(fk, "kt10001")), 1)               # 계좌 조회 실패 때만 상태 파일 수량으로 손절

    def test_f03_corrupt_intent_file_blocks_all_orders(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "intents", "2026-10-26.json")
        os.makedirs(os.path.dirname(p))
        open(p, "w").write('[{"intent_id": "x", "status": "ACCEPT')    # 깨진 파일
        clk, notes = FakeClock("2026-10-26 15:21:05"), []
        fk = FakeKiwoom(clk)
        store = O.IntentStore(p, clock=clk)
        self.assertTrue(store.corrupt)
        self.assertTrue(any(f.startswith("2026-10-26.json.corrupt-") for f in os.listdir(os.path.dirname(p))))   # 손상 파일 보관
        om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep, notify=notes.append, store=store)
        r = om.place_buys(PLAN, "본래")
        om.stop_check({"084670": {"qty": 1, "sellable": 1, "cur_price": 1}}, {"084670": 2})
        self.assertEqual(len(sends(fk)) + len(sends(fk, "kt10001")), 0)
        self.assertIn("손상", r[0]["skipped"])
        self.assertEqual(sum("주문 차단" in n for n in notes), 1)
        # 파일이 없으면 빈 목록으로 시작 (차단 아님)
        s2 = O.IntentStore(os.path.join(tmp, "intents", "2026-10-27.json"), clock=clk)
        self.assertIsNone(s2.corrupt)
        shutil.rmtree(tmp, ignore_errors=True)

    def test_sell_unknown_resend_rule(self):
        # 흔적 없음 + 잔고 그대로 -> 2분 뒤 NOT_SENT, 다음 분 손절 다시 보냄
        clk, notes = FakeClock("2026-10-26 10:00:00"), []
        fk = FakeKiwoom(clk)
        fk.balance_rows = [bal_row("084670", 28)]
        fk.script["kt10001"] = ["timeout", "accept"]
        om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep, notify=notes.append)
        held = {"084670": {"qty": 28, "sellable": 28, "cur_price": 29000}}
        r = om.stop_check(held, {"084670": 30000})[0]
        self.assertEqual(r["status"], O.UNKNOWN)
        for sec in (5, 30, 120):
            clk.t = pd.Timestamp("2026-10-26 10:00:00") + pd.Timedelta(seconds=sec)
            om.reconcile()
        self.assertEqual(r["status"], O.NOT_SENT)
        self.assertTrue(r["resendable"])
        clk.t = pd.Timestamp("2026-10-26 10:03:00")
        om.stop_check(acct_view(fk, held), {"084670": 30000})
        self.assertEqual(len(sends(fk, "kt10001")), 2)
        # 흔적 있음 (잔고가 줄고 남은 10주는 걸린 매도로 매도 가능 0) -> 다시 보내지 않음 (11J 보완 3: 계좌 매도 가능 수량이 정답)
        clk2, notes2 = FakeClock("2026-10-26 10:00:00"), []
        fk2 = FakeKiwoom(clk2)
        fk2.script["kt10001"] = ["timeout"]
        om2 = O.OrderManager(O.Broker(fk2, allow_orders=True), clock=clk2, sleep=clk2.sleep, notify=notes2.append)
        r2 = om2.stop_check(held, {"084670": 30000})[0]
        fk2.balance_rows = [bal_row("084670", 10, sellable=0)]
        for sec in (5, 30, 120):
            clk2.t = pd.Timestamp("2026-10-26 10:00:00") + pd.Timedelta(seconds=sec)
            om2.reconcile()
        self.assertEqual(r2["status"], O.UNKNOWN)
        clk2.t = pd.Timestamp("2026-10-26 10:03:00")
        om2.stop_check(acct_view(fk2, held), {"084670": 30000})
        self.assertEqual(len(sends(fk2, "kt10001")), 1)
        # 흔적 있음 (미체결 조회에 보임) -> ACCEPTED, 다시 보내지 않음
        clk3 = FakeClock("2026-10-26 10:00:00")
        fk3 = FakeKiwoom(clk3)
        fk3.balance_rows = [bal_row("084670", 28)]
        fk3.script["kt10001"] = ["timeout_late:100"]
        om3 = O.OrderManager(O.Broker(fk3, allow_orders=True), clock=clk3, sleep=clk3.sleep)
        r3 = om3.stop_check(held, {"084670": 30000})[0]
        for sec in (5, 30, 120):
            clk3.t = pd.Timestamp("2026-10-26 10:00:00") + pd.Timedelta(seconds=sec)
            om3.reconcile()
        self.assertEqual(r3["status"], O.ACCEPTED)
        om3.stop_check(acct_view(fk3, held), {"084670": 30000})
        self.assertEqual(len(sends(fk3, "kt10001")), 1)

    def test_buy_unknown_not_resent_before_1535(self):
        clk = FakeClock("2026-10-26 15:21:05")
        fk = FakeKiwoom(clk)
        fk.script["kt10000"] = ["timeout"]
        om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep)
        om.place_buys(PLAN, "본래")
        for sec in (5, 30, 120, 180):
            clk.t = pd.Timestamp("2026-10-26 15:21:05") + pd.Timedelta(seconds=sec)
            om.reconcile()
            om.place_buys(PLAN, "본래")
        self.assertEqual(om.records[0]["status"], O.UNKNOWN)
        self.assertEqual(len(sends(fk)), 1)

    def test_b4_margin_short_skip_recorded(self):
        clk = FakeClock("2026-10-26 15:21:05")
        fk = FakeKiwoom(clk)
        om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep)
        plan = [{"code": "A1", "qty": 28, "limit_price": 33350, "skip": ""}, {"code": "B2", "qty": 10, "limit_price": 50000, "skip": ""}]
        out = om.place_buys(plan, "본래", orderable=1_000_000)       # A1 933,800 은 됨, B2 500,000 은 남은 66,200 보다 큼
        self.assertEqual(len(sends(fk)), 1)
        self.assertEqual(out[1]["skipped"], "증거금 부족(백테스트는 매수)")
        rec = [i for i in om.records if i["code"] == "B2"][0]
        self.assertEqual(rec["status"], O.NOT_SENT)
        self.assertIn("증거금 부족(백테스트는 매수)", rec["cause"])

    def test_b1_settlement_rerun_no_duplicate_and_cancel_unknown(self):
        # 취소 UNKNOWN: 서버는 취소함 -> 원주문이 미체결에 없음 -> ACCEPTED, 다시 정산해도 취소 안 보냄
        clk, notes = FakeClock("2026-10-26 15:21:05"), []
        fk = FakeKiwoom(clk)
        fk.script["kt10003"] = ["timeout_applied"]
        om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep, notify=notes.append)
        om.place_buys(PLAN, "본래")
        clk.at("15:35:00")
        r = om.close_check()
        cancels = [i for i in om.records if i["stage"] == "cancel"]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0]["status"], O.ACCEPTED)
        self.assertEqual(r["cancel_unknown"], 0)
        om.close_check()
        om.close_check()
        self.assertEqual(len([c for c in fk.calls if c[1] == "kt10003"]), 1)
        self.assertEqual(len([i for i in om.records if i["stage"] == "cancel"]), 1)
        # 취소 UNKNOWN: 서버에 안 닿음 -> 원주문이 아직 미체결 -> UNKNOWN 유지·알림, 정산 결과에 남음
        clk2, notes2 = FakeClock("2026-10-26 15:21:05"), []
        fk2 = FakeKiwoom(clk2)
        fk2.script["kt10003"] = ["timeout"]
        om2 = O.OrderManager(O.Broker(fk2, allow_orders=True), clock=clk2, sleep=clk2.sleep, notify=notes2.append)
        om2.place_buys(PLAN, "본래")
        clk2.at("15:35:00")
        r2 = om2.close_check()
        self.assertEqual(r2["cancel_unknown"], 1)
        self.assertTrue(any("취소 확인 안 됨" in n for n in notes2))
        om2.close_check()
        self.assertEqual(len([c for c in fk2.calls if c[1] == "kt10003"]), 1)   # UNKNOWN 취소는 다시 안 보냄
        # 하위 단계 다시 실행해도 중복 없음
        d = S.Day(os.path.join(tempfile.mkdtemp(), "d.json"), clk(), clock=clk)
        n = []
        for _ in range(3):
            S.run_step(d, "ac_ledger", lambda: n.append(1) or {"ok": True})
        self.assertEqual(len(n), 1)

    def test_f04_missing_fields_not_succeeded(self):
        clk = FakeClock("2026-10-26 15:21:05")
        fk = FakeKiwoom(clk)
        om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep)
        r = om.place_buys(PLAN, "본래")[0]
        fk.fill(r["ord_no"], 28, 33000, "15:30:10")
        fk.fill_rows[0].pop("cntr_pric")                                # 필드 없음
        clk.at("15:35:00")
        d = S.Day(os.path.join(tempfile.mkdtemp(), "d.json"), clk(), clock=clk)
        S.run_step(d, "close_check", om.close_check)
        self.assertEqual(d.status("close_check"), S.FAILED)
        self.assertIn("필드 없음", d.s["steps"]["close_check"]["error"])


class StateAndSnapshot(unittest.TestCase):
    def test_f05_snapshot_without_dt_fails(self):
        self.assertFalse(S.validate_snapshot(pd.DataFrame({"volume": [1, 2, 3]}), "2026-10-26")[0])
        self.assertFalse(S.validate_snapshot(pd.DataFrame({"volume": [1, 2], "dt": ["abc", ""]}), "2026-10-26")[0])

    def test_order_step_stepfailed_is_unknown(self):
        clk = FakeClock("2026-10-26 15:21:05")
        d = S.Day(os.path.join(tempfile.mkdtemp(), "d.json"), clk(), clock=clk)

        def f():
            raise S.StepFailed("계획 확인 실패")
        S.run_step(d, "buy", f, O.OrderManager(O.Broker(FakeKiwoom(clk), allow_orders=True), clock=clk))
        self.assertEqual(d.status("buy"), S.UNKNOWN)
        S.run_step(d, "snapshot", f)
        self.assertEqual(d.status("snapshot"), S.FAILED)

    def test_b7_frozen_hash_mismatch(self):
        self.assertTrue(E.frozen_check()["ok"])
        bad = E.frozen_check({"load_stock": "0" * 64, "market_features": E.FROZEN_SOURCE_SHA256["market_features"]})
        self.assertFalse(bad["ok"])
        self.assertFalse(bad["hash_ok"])


class Lock(unittest.TestCase):
    def test_f02_second_instance_refused(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "resident.lock")
        a, b = C.SingleInstanceLock(p), C.SingleInstanceLock(p)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())                                   # 두 번째 실행 거부
        self.assertTrue(C.resident_running(p, os.path.join(tmp, "info.json")))
        a.release()
        self.assertTrue(b.acquire())                                    # 앞 실행이 끝나면 잡힘
        b.release()
        self.assertFalse(C.resident_running(p, os.path.join(tmp, "info.json")))
        shutil.rmtree(tmp, ignore_errors=True)


class ResidentRules(unittest.TestCase):
    """상주 프로그램 규칙 (start_day 없이, 가짜 브로커)"""

    def setUp(self):
        import resident as R
        self.R = R
        self.tmp = tempfile.mkdtemp()
        self.saved = (dict(R.CFG), R.POSITIONS, R.BLOCK, R.alert, R.RESIDENT_LOCK, R.RESIDENT_INFO, R.LOG_DIR)
        R.LOG_DIR = os.path.join(self.tmp, "logs")                 # 상주 로그도 임시 폴더로
        R.CFG["state_dir"] = self.tmp
        R.CFG["heartbeat_file"] = os.path.join(self.tmp, "hb.log")
        R.POSITIONS, R.BLOCK = os.path.join(self.tmp, "positions.json"), os.path.join(self.tmp, "block.json")
        R.RESIDENT_LOCK, R.RESIDENT_INFO = os.path.join(self.tmp, "resident.lock"), os.path.join(self.tmp, "resident.info.json")
        self.alerts = []
        R.alert = self.alerts.append
        self.clk = FakeClock("2026-10-26 15:22:00")
        self.fk = FakeKiwoom(self.clk)
        res = R.Resident()
        res.day = S.Day(os.path.join(self.tmp, "2026-10-26.json"), self.clk(), clock=self.clk)
        res.broker = O.Broker(self.fk, allow_orders=True)
        res.om = O.OrderManager(res.broker, clock=self.clk, sleep=self.clk.sleep, notify=self.alerts.append)
        res.watch, res.mismatch_seen = O.AccountWatch(notify=self.alerts.append), set()
        res.ctx = {"buy_allowed": True, "frozen": {"ok": True}, "prep": {"data": {"daily_up_to_date": True, "official_missing_dates": []}},
                   "data_version": "v1"}
        res.day.set("live", {"plan": [{"code": "084670", "qty": 28, "limit_price": 33350, "skip": ""}], "kind": "본래"})
        res.day.mark("prev_settle")
        res.day.mark("data_check")
        self.res = res

    def tearDown(self):
        R = self.R
        cfg, R.POSITIONS, R.BLOCK, R.alert, R.RESIDENT_LOCK, R.RESIDENT_INFO, R.LOG_DIR = self.saved
        R.CFG.clear()
        R.CFG.update(cfg)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_buy_ok_when_all_checks_pass(self):
        out = self.res.buy()
        self.assertEqual(out["sent"], 1)
        self.assertEqual(len(sends(self.fk)), 1)

    def test_b2_prev_day_unsettled_blocks_buy(self):
        self.res.day.s["steps"].pop("prev_settle")
        out = self.res.buy()
        self.assertIn("전날 미완료", out["reason"])
        self.assertEqual(len(sends(self.fk)), 0)

    def test_b2_prev_settle_resolves_unknown_by_balance(self):
        R = self.R
        prev = os.path.join(self.tmp, "intents", "2026-10-23.json")
        os.makedirs(os.path.dirname(prev))
        C.atomic_json(prev, [{"intent_id": "p1", "date": "2026-10-23", "stage": "buy", "side": "buy", "code": "084670", "qty": 28, "status": "UNKNOWN"}])
        S.Day(os.path.join(self.tmp, "2026-10-23.json"), pd.Timestamp("2026-10-23"), clock=self.clk).mark("ac_summary")  # after_close 없음
        self.fk.balance_rows = [bal_row("084670", 28)]
        r = self.res.prev_settle()
        self.assertEqual(r["settled"], 1)
        st = json.load(open(prev, encoding="utf-8"))
        self.assertEqual(st[0]["status"], O.ACCEPTED)                   # 계좌에 생겼으니 체결된 것으로
        self.assertEqual(json.load(open(R.POSITIONS, encoding="utf-8"))["held"]["084670"]["qty"], 28)
        self.assertTrue(S.Day(os.path.join(self.tmp, "2026-10-23.json"), pd.Timestamp("2026-10-23")).done("after_close"))

    def test_b3_holding_mismatch_blocks_buy(self):
        C.atomic_json(self.R.POSITIONS, {"held": {"084670": {"qty": 10, "stop_price": 1}}, "next_open_sells": []})
        self.fk.balance_rows = [bal_row("084670", 10), bal_row("005930", 3)]   # 11K 보완 3: 보유 종목 수량 변화는 그 종목만 수동 확인, 장부에 없는 종목은 보유 불일치
        self.clk.at("10:00:00")
        self.res.ctx["quote_caller"] = None
        self.res.stop_minute = self.res.stop_minute  # 시세 대신 고정 값
        import quotes as Q
        saved = Q.snapshot
        Q.snapshot = lambda caller, codes: (None, pd.DataFrame({"code": codes, "close": [99999.0] * len(codes)}), {})
        try:
            self.res.stop_minute()
        finally:
            Q.snapshot = saved
        self.assertFalse(self.res.ctx["buy_allowed"])
        self.assertIn("holding_mismatch", self.res.day.s["buy_block"])
        self.clk.at("15:22:00")
        self.assertIn("신규 매수 안 함", self.res.buy()["reason"])
        self.assertEqual(len(sends(self.fk)), 0)

    def test_b6_data_version_not_reflected_blocks_buy(self):
        self.res.day.s["steps"].pop("data_check")
        self.res.ctx["prep"]["data"] = {"daily_up_to_date": False, "official_missing_dates": ["2026-10-23"]}
        self.res.data_check()
        self.res.day.mark("data_check")
        self.assertFalse(self.res.ctx["buy_allowed"])
        self.assertIn("신규 매수 안 함", self.res.buy()["reason"])
        self.assertEqual(len(sends(self.fk)), 0)

    def test_b7_frozen_check_failed_blocks_buy(self):
        self.res.ctx["frozen"] = {"ok": False}
        self.assertIn("동결 함수", self.res.buy()["reason"])
        self.assertEqual(len(sends(self.fk)), 0)

    def test_b5_timezone_mismatch_no_trading(self):
        self.res.check_tz(tz_ok=False)
        called = []
        self.res.start_day = lambda *a, **k: called.append("start")
        self.assertEqual(self.res.tick(pd.Timestamp("2026-10-26 09:30:00")), "tz_block")
        self.assertEqual(called, [])
        self.assertTrue(any("시간대 오류" in a for a in self.alerts))

    def test_f01_trend_sell_skips_code_not_in_account(self):
        C.atomic_json(self.R.POSITIONS, {"held": {"084670": {"qty": 10, "trend_sell_next_open": True}}, "next_open_sells": []})
        self.fk.balance_rows = []
        self.clk.at("08:31:00")
        self.res.trend_sell()
        self.assertEqual(len(sends(self.fk, "kt10001")), 0)
        self.assertTrue(any("추세 매도 대상이 계좌에 없음" in a for a in self.alerts))

    def test_f02_resident_second_instance_refused(self):
        first = self.R.Resident()
        ok1, _ = first.acquire_single_instance()
        second = self.R.Resident()
        ok2, why = second.acquire_single_instance()
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        first.release_single_instance()
        self.assertFalse(os.path.exists(self.R.RESIDENT_INFO))         # 자기 pid 라서 지움
        ok3, _ = second.acquire_single_instance()
        self.assertTrue(ok3)
        second.release_single_instance()


if __name__ == "__main__":
    unittest.main(verbosity=2)
