"""
키움 REST 유량 기준 대응 (11L, 2026-10-28) 재현 시험. 가짜 서버가 TR 별 최소 간격을 어기면 HTTP 429 를 돌려준다 (가짜 시계).
실제 주문·조회·logs/exec·data 없음.

  python -m unittest backtest_swing/live/test_broad10.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import json
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_orders import PLAN, FakeClock, FakeKiwoom, Resp, sends  # noqa: E402
from test_codex import bal_row  # noqa: E402
import common as C  # noqa: E402
import orders as O  # noqa: E402
import settings  # noqa: E402

LIMITS = json.load(open(settings.EXAMPLE, encoding="utf-8"))["rate_limits"]
PLAN3 = [dict(PLAN[0], code=c) for c in ("990001", "990002", "990006")]


class Gate(unittest.TestCase):
    def setUp(self):
        self.clk = FakeClock("2026-10-28 15:21:01")
        self.fk = FakeKiwoom(self.clk)
        self.notes = []
        self.addCleanup(C.RateGate.configure, {}, __import__("time").time, __import__("time").sleep)

    def gate(self, on=True):
        C.RateGate.configure(LIMITS if on else {}, clock=lambda: self.clk().timestamp(), sleep=self.clk.sleep)

    def om(self, env="mock"):
        return O.OrderManager(O.Broker(self.fk, allow_orders=True, env=env), clock=self.clk, sleep=self.clk.sleep, notify=self.notes.append)

    def gaps(self, tr):
        ts = [pd.Timestamp(f"2026-10-28 {t}") for t, x, _ in self.fk.calls if x == tr]
        return [round((b - a).total_seconds(), 3) for a, b in zip(ts, ts[1:])]

    # ---------------------------------------------------------------- 10-28 실측 재현과 대응

    def test_1028_three_buys_without_gate_429_retried_not_counted(self):
        """10-28 15:21: 매수 3건을 1초 미만 간격으로 -> 429. 이제는 유량 초과 = 보내지 않음, 거부 횟수에 안 셈, 간격 두고 다시 -> 3건 모두 접수"""
        self.gate(False)
        self.fk.min_gap = {"kt10000": 1.0}
        om = self.om()
        om.place_buys(PLAN3, "본래")
        self.assertEqual(len(self.fk.r429), 2)                                     # 2·3 번째가 한 번씩 429
        acc = [i for i in om.records if i["status"] == O.ACCEPTED]
        self.assertEqual(sorted(i["code"] for i in acc), sorted(p["code"] for p in PLAN3))
        self.assertTrue(all(om.store.rejected_count(om.date(), p["code"], "buy") == 0 for p in PLAN3))
        rate = [i for i in om.records if i["status"] == O.NOT_SENT]
        self.assertTrue(rate and all(i["not_sent_kind"] == "rate" and i["return_code"] == -429 for i in rate))

    def test_mock_gate_spaces_same_tr_no_429(self):
        self.gate()
        self.fk.min_gap = {"kt10000": 1.0, "kt10001": 1.0, "kt00018": 1.0}
        om = self.om()
        om.place_buys(PLAN3, "본래")
        self.assertEqual((self.fk.r429, len(sends(self.fk))), ([], 3))
        self.assertTrue(all(g >= 1.2 for g in self.gaps("kt10000")), self.gaps("kt10000"))

    def test_same_minute_stop_sells_and_balance_pages(self):
        """같은 분 손절 여러 종목 + 잔고 연속조회 쪽: 공용 제한기로 429 없음"""
        self.gate()
        self.clk.t = pd.Timestamp("2026-10-28 10:00:00")
        self.fk.min_gap = {"kt10001": 1.0, "kt00018": 1.0}
        self.fk.page_size = 1
        codes = ["990001", "990002", "990006"]
        self.fk.balance_rows = [bal_row(c, 10) for c in codes]
        om = self.om()
        bal = O.Broker(self.fk, env="mock").balance()                           # 3쪽
        self.assertEqual(bal["_pages"], 3)
        duty = {c: O.new_duty("stop", "2026-10-28 09:59:00", "손절", price=1.0, stop=2.0) for c in codes}
        om.send_duties(duty, {c: {"file_qty": 10, "acct": {"qty": 10, "sellable": 10}} for c in codes})
        self.assertEqual(self.fk.r429, [])
        self.assertEqual(len(sends(self.fk, "kt10001")), 3)
        self.assertTrue(all(g >= 1.2 for g in self.gaps("kt10001") + self.gaps("kt00018")))

    def test_0830_trend_sells_many_codes(self):
        self.gate()
        self.clk.t = pd.Timestamp("2026-10-28 08:30:00")
        self.fk.min_gap = {"kt10001": 1.0}
        codes = [f"00{i}000" for i in range(1, 6)]
        self.fk.balance_rows = [bal_row(c, 10) for c in codes]
        om = self.om()
        om.trend_sells(codes, {c: {"file_qty": 10, "acct": {"qty": 10, "sellable": 10}} for c in codes})
        self.assertEqual((self.fk.r429, len(sends(self.fk, "kt10001"))), ([], 5))

    def test_balance_429_and_rate_rc_are_query_failure_not_zero(self):
        """kt00018 유량 오류(HTTP 429·return_code 5 + 빈 목록) = 조회 실패. 보유 0 으로 읽어 매도 의무를 끝내거나 수동 확인을 걸지 않음"""
        self.gate(False)
        self.clk.t = pd.Timestamp("2026-10-28 10:00:00")
        self.fk.balance_rows = [bal_row("990006", 28)]
        self.fk.min_gap = {"kt00018": 1.0}
        b = O.Broker(self.fk, env="mock")
        b.balance()
        with self.assertRaises(O.NoResponse) as e:
            b.balance()                                                            # 1초 안 = 429
        self.assertIn("유량", str(e.exception))
        self.fk.min_gap = {}

        class RateEmpty(FakeKiwoom):
            def request(self, tr_code, *a, **k):
                if tr_code == "kt00018":
                    return Resp({"return_code": 5, "return_msg": "[1700:허용된 요청 개수를 초과하였습니다. API ID=kt00018]",
                                 "acnt_evlt_remn_indv_tot": [], "prsm_dpst_aset_amt": "0"})
                return super().request(tr_code, *a, **k)
        fk2 = RateEmpty(self.clk)
        om = O.OrderManager(O.Broker(fk2, allow_orders=True), clock=self.clk, sleep=self.clk.sleep)
        duty = O.new_duty("trend", "2026-10-27 15:41:00", "추세")
        pos = {"held": {"990006": {"qty": 28, "stop_price": 30000.0}}, "sell_duty": {"990006": dict(duty)}, "next_open_sells": {}}
        watch = O.AccountWatch()
        for t in ("10:00:00", "10:01:30"):                                         # 1분 넘게 두 번 -> 성공이었다면 의무가 끝났을 것
            self.clk.at(t)
            r = O.stop_cycle(om, watch, pos, lambda cs, deadline=None: {}, lambda: om.b.balance(quick=True), self.clk(), set())
            self.assertFalse(r["account_ok"])
            self.assertEqual((r["qty_changed"], r["duty_ended"], r["duty_zero"]), ({}, [], []))
        self.assertIn("990006", pos["sell_duty"])
        self.assertNotIn("zero_at", pos["sell_duty"]["990006"])

    def test_buy_429_retries_only_before_1528(self):
        self.gate(False)
        self.clk.t = pd.Timestamp("2026-10-28 15:27:58")

        class Always429(FakeKiwoom):
            def request(self, tr_code, *a, **k):
                if tr_code == "kt10000":
                    self.calls.append((self.clock().strftime("%H:%M:%S.%f")[:-3], tr_code, {}))
                    return None
                return super().request(tr_code, *a, **k)
        self.fk = Always429(self.clk)
        om = self.om()
        om.place_buys(PLAN, "본래")
        self.assertLess(max(t for t, _, _ in self.fk.calls), "15:28:00.000")       # 매수 마감(15:28 전) 넘어 다시 보내지 않음 (11L 보완 2)
        self.assertEqual(len(self.fk.calls), 2)                                   # 15:27:58·59
        self.assertTrue(any("[주문 유량 초과]" in n for n in self.notes))
        self.assertEqual(om.resendable("buy"), [PLAN[0]["code"]])                 # 거부 횟수 0 (다시 보내기 대상, 15:28 전이면)

    def test_rate_rc5_order_is_not_sent(self):
        r = O.classify(lambda: Resp({"return_code": 5, "return_msg": "[1700:허용된 요청 개수를 초과하였습니다. API ID=kt10000]"}))
        self.assertEqual((r["outcome"], r["not_sent_kind"]), (O.NOT_SENT, "rate"))

    def test_prod_limits_orders_and_queries(self):
        self.gate()
        self.fk.min_gap = {"kt10000": 0.2}
        om = self.om(env="prod")
        plan = [dict(PLAN[0], code=f"00{i}000") for i in range(1, 6)]
        om.place_buys(plan, "본래")
        self.assertEqual(self.fk.r429, [])
        self.assertTrue(all(g >= 0.25 for g in self.gaps("kt10000")))             # 주문 초당 4회 이하
        b = O.Broker(self.fk, env="prod")
        for _ in range(3):
            b.balance()
            b.deposit()
        self.assertTrue(all(g >= 0.5 for g in self.gaps("kt00018")))              # 잔고 초당 2회 이하
        ts = [pd.Timestamp(f"2026-10-28 {t}") for t, x, _ in self.fk.calls if x in ("kt00018", "kt00001")]
        self.assertTrue(all((b_ - a).total_seconds() >= 0.25 for a, b_ in zip(ts, ts[1:])))   # 조회 TR 끼리 초당 4회 이하

    def test_two_callers_share_one_prod_query_gate(self):
        """시세 조회 Caller 를 여러 개 만들어도(스냅샷·일봉 반영·손절 감시) 같은 토큰의 조회 TR 을 합쳐서 제한"""
        self.gate()
        times = []

        def caller():
            c = C.Caller.__new__(C.Caller)
            c.env, c.n, c.errors, c.hourly, c.fail_streak, c.stopped = "prod", 0, [], __import__("collections").Counter(), 0, None
            c.c = SimpleNamespace(request=lambda **kw: times.append(self.clk().timestamp()) or SimpleNamespace(
                json=lambda: {"return_code": 0, "atn_stk_infr": []}, headers={}))
            return c
        a, b = caller(), caller()
        for _ in range(4):
            a.post("ka10095", "/x", {})
            b.post("ka10081", "/x", {})
        self.assertTrue(all(round(y - x, 3) >= 0.25 for x, y in zip(times, times[1:])))
        a.post("ka10095", "/x", {})
        with self.assertRaises(ValueError):                                       # 손절 감시의 대기 상한 안에 자리가 없으면 기다리지 않음
            C.RateGate.last[("prod", "query")] = self.clk().timestamp() + 30
            b.post("ka10095", "/x", {}, deadline=__import__("time").time() + 1)


class Config(unittest.TestCase):
    def test_old_config_gets_rate_limits(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        ex = json.load(open(settings.EXAMPLE, encoding="utf-8"))
        ex.pop("rate_limits")
        ex["rate_limits"] = {"mock": {}}                                           # 일부만 있는 예전 값
        p = os.path.join(tmp, "config.json")
        json.dump(ex, open(p, "w", encoding="utf-8"))
        cfg = settings.load(path=p)
        self.assertEqual((cfg["rate_limits"]["mock"]["same_tr"], cfg["rate_limits"]["prod"]["kt00018"]), (1.2, 0.5))
        self.assertIn("rate_limits.mock.same_tr", cfg["_filled"])


if __name__ == "__main__":
    unittest.main()
