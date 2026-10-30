"""
Codex 확인(48efca9..b444c85) 반영(11J 보완, 2026-10-26) 재현 시험. 가짜 서버·가짜 시계, 실제 주문·조회·logs/exec 없음.

  python -m unittest backtest_swing/live/test_broad2.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import Base, Crash, bal_row, jl  # noqa: E402
from test_orders import FakeClock, FakeKiwoom, sends, wait_for  # noqa: E402
import notify as N  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402

POS = {"990006": {"qty": 28, "trade_id": "990006-2026-10-20-1", "entry_date": "2026-10-20", "stop_price": 30000.0, "kind": "본래"}}
TRIG = O.new_duty("stop", "2026-10-26 09:30:00", "현재가 29900 < 손절가 30000", price=29900.0, stop=30000.0)   # 11J 보완 3: 종목 코드 단위 의무


def setup(t="2026-10-26 10:00:00"):
    clk = FakeClock(t)
    fk = FakeKiwoom(clk)
    notes = []
    om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep, notify=notes.append)
    return clk, fk, om, notes


def positions(duty=None, **extra):
    held = json.loads(json.dumps(POS))
    held["990006"].update(extra)
    return {"held": held, "sell_duty": {"990006": dict(duty)} if duty else {}, "next_open_sells": {}, "trade_seq": 1, "duty_format": P.DUTY_FORMAT}


class Order(unittest.TestCase):
    """1·5 (11J 보완 2 로 바뀜): ① 계좌 조회 -> ② 손절 진행 중·추세 매도 (같은 분 계좌 매도 가능 수량) -> ③ 새 감지. 계좌 조회 실패면 보유 파일 수량"""

    def test_1_account_then_duty_then_detect(self):
        clk, fk, om, _ = setup()
        log = []
        real = om.b.sell_market
        om.b.sell_market = lambda c, q: (log.append(("send", c, q)), real(c, q))[1]
        fk.balance_rows = [bal_row("990006", 28, sellable=28), bal_row("005930", 10, sellable=10)]
        pos = positions(TRIG)
        pos["held"]["005930"] = {"qty": 10, "entry_date": "2026-10-20", "stop_price": 50000.0}

        def bal():
            log.append(("account",))
            return fk.request("kt00018", "POST", "/x").json()

        def quote(codes, deadline=None):
            log.append(("quote", round(deadline - time.time(), 1)))
            return {"990006": 31000.0, "005930": 49000.0}                          # 990006 반등, 005930 새로 손절
        r = O.stop_cycle(om, O.AccountWatch(), pos, quote, bal, clk(), set(), budget=10)
        self.assertEqual([x[0] for x in log], ["account", "send", "quote", "send"])
        self.assertEqual((log[1][1], log[1][2]), ("990006", 28))                 # 같은 분 계좌 매도 가능 수량
        self.assertLessEqual(log[2][1], 10.0)                                     # 10초 상한은 시세 조회에
        self.assertEqual(r["triggered_new"].keys(), {"005930"})

    def test_1_5_account_and_quotes_fail_still_sells_triggered_and_trend(self):
        clk, fk, om, _ = setup()
        pos = positions(TRIG)
        pos["held"]["000660"] = {"qty": 7, "entry_date": "2026-10-20", "stop_price": 1.0, "trend_sell_next_open": True}
        pos["sell_duty"]["000660"] = O.new_duty("trend", "2026-10-23 15:40:00", "추세 매도")

        def fail(*a, **k):
            raise O.NoResponse("시간 상한")
        r = O.stop_cycle(om, O.AccountWatch(), pos, fail, fail, clk(), set(), budget=10)
        s = {c[2]["stk_cd"]: c[2]["ord_qty"] for c in sends(fk, "kt10001")}
        self.assertEqual(s, {"990006": "28", "000660": "7"})                     # 계좌·시세 조회가 안 돼도 보유 파일 수량으로
        self.assertFalse(r["account_ok"])
        late = [i for i in om.records if i["stage"] == "trend_late"][0]
        self.assertEqual((late["qty_basis"][:5], late["not_open"]), ("보유 파일", True))

    def test_1_uses_same_minute_account_qty(self):
        clk, fk, om, _ = setup()
        fk.balance_rows = [bal_row("990006", 28, sellable=20)]
        O.stop_cycle(om, O.AccountWatch(), positions(TRIG), lambda cs: {}, lambda: fk.request("kt00018", "POST", "/x").json(),
                     clk(), set())
        self.assertEqual(sends(fk, "kt10001")[0][2]["ord_qty"], "20")            # 같은 분 계좌의 매도 가능 수량


class PreSave(Base):
    """2: 손절 진행 중 표시는 주문 전에 먼저 저장 -> 거부 직후 강제 종료 -> 반등 -> 재시작에도 매도 다시"""

    def test_2_crash_after_reject_then_rebound_restart(self):
        P.save(self.R.POSITIONS, positions())
        self.fk.balance_rows = [bal_row("990006", 28)]
        res = self.started("10:00:00", live=False)
        self.quotes["990006"] = 29900.0
        self.fk.script["kt10001"] = ["reject", "accept"]

        def crash_on_reject(msg):
            if "주문 거부" in msg:
                raise Crash()                                                     # 거부 직후 강제 종료
        res.om.notify = crash_on_reject
        with self.assertRaises(Crash):
            res.stop_minute()
        self.assertEqual(len(sends(self.fk, "kt10001")), 1)
        self.assertEqual(jl(self.R.POSITIONS)["sell_duty"]["990006"]["kind"], "stop")   # 전송 전에 저장돼 있음
        self.quotes["990006"] = 31500.0                                            # 반등
        res2 = self.started("10:05:00", live=False)                              # 재시작
        res2.stop_minute()
        self.assertEqual(len(sends(self.fk, "kt10001")), 2)                       # 그래도 다시 보냄
        self.assertEqual(res2.om.records[-1]["status"], O.ACCEPTED)


class DetectWindow(unittest.TestCase):
    """3: 15:20 감지 여부는 감시가 시작된 시각으로 (조회 뒤 지금 시각이 아님)"""

    def test_3_started_1520_quote_finished_1521(self):
        clk, fk, om, _ = setup("2026-10-26 15:20:50")
        start = clk()
        fk.balance_rows = [bal_row("990006", 28)]

        def slow_quote(codes, deadline=None):
            clk.at("15:21:05")                                                    # 조회가 늦게 끝남
            return {"990006": 29900.0}
        r = O.stop_cycle(om, O.AccountWatch(), positions(), slow_quote, lambda: fk.request("kt00018", "POST", "/x").json(), start, set(), budget=10)
        self.assertEqual(r["triggered_new"]["990006"]["since"], "2026-10-26 15:20:50")
        self.assertEqual(len(sends(fk, "kt10001")), 1)                            # 15:21:05 에 동시호가 시장가로 나감
        self.assertTrue(om.records[0]["closing_auction"])
        clk2, fk2, om2, _ = setup("2026-10-26 15:21:00")
        r2 = O.stop_cycle(om2, O.AccountWatch(), positions(), lambda cs, deadline=None: {"990006": 29900.0},
                          lambda: fk2.request("kt00018", "POST", "/x").json(), clk2(), set(), budget=10)
        self.assertEqual(r2["triggered_new"], {})                                  # 15:21 에 시작한 감시는 감지 안 함


class Remainder(Base):
    """4: 손절·추세 매도 주문이 다 체결돼도 계좌에 같은 종목이 남으면 남은 수량으로 계속, 15:35 에 남으면 다음 날 시가 매도 목록"""

    def test_4_full_fill_with_holdings_left_sells_rest(self):
        clk, fk, om, _ = setup()
        fk.balance_rows = [bal_row("990006", 28, sellable=20)]
        pos = positions()
        bal = lambda: fk.request("kt00018", "POST", "/x").json()                   # noqa: E731
        O.stop_cycle(om, O.AccountWatch(), pos, lambda cs, deadline=None: {"990006": 29900.0}, bal, clk(), set(), budget=10)
        first = om.records[0]
        self.assertEqual((first["qty"], first["status"]), (20, O.ACCEPTED))
        clk.at("10:01:00")
        O.stop_cycle(om, O.AccountWatch(), pos, lambda cs, deadline=None: {}, bal, clk(), set(), budget=10)
        self.assertEqual(len(sends(fk, "kt10001")), 1)                            # 아직 체결 전 -> 다시 안 보냄
        fk.fill(first["ord_no"], 20, 29900, "10:01:10")                           # 20주 다 체결, 계좌에 8주 남음
        fk.balance_rows[0]["trde_able_qty"] = "8"
        clk.at("10:02:00")
        r = O.stop_cycle(om, O.AccountWatch(), pos, lambda cs, deadline=None: {}, bal, clk(), set(), budget=10)
        s = sends(fk, "kt10001")
        self.assertEqual((len(s), s[-1][2]["ord_qty"]), (2, "8"))                 # 남은 8주로 계속 (계좌 매도 가능 수량)
        self.assertEqual(om.records[-1]["qty_basis"], "계좌 매도 가능 수량")
        self.assertEqual(r["duties"], ["990006"])
        clk.at("10:03:00")
        O.stop_cycle(om, O.AccountWatch(), pos, lambda cs, deadline=None: {}, bal, clk(), set(), budget=10)
        self.assertEqual(len(sends(fk, "kt10001")), 2)                            # 남은 주문 접수 뒤에는 다시 안 보냄

    def test_4_1535_left_holdings_go_to_next_open(self):
        P.save(self.R.POSITIONS, positions())
        self.fk.balance_rows = [bal_row("990006", 28, sellable=20)]
        res = self.started("10:00:00", live=False)
        self.quotes["990006"] = 29900.0
        res.stop_minute()
        it = res.om.records[0]
        self.fk.fill(it["ord_no"], 20, 29900, "10:00:10")                          # 다 체결, 8주는 매도 가능 0 으로 남음
        self.clk.at("15:35:01")
        res.close_check()
        self.assertEqual(res.om.records[0]["fill_status"], "체결")
        nos = jl(self.R.POSITIONS)["next_open_sells"]
        self.assertIn("990006", nos)
        self.assertIn("8주 남음", nos["990006"]["reason"])


class AlertsLog(unittest.TestCase):
    """6: 큐에 넣는 순간 alerts.log 에 먼저, 전송 결과는 따로. 못 보낸 것은 다음 시작 때 "지난 미전송 알림" 한 통"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log, self.pend = os.path.join(self.tmp, "alerts.log"), os.path.join(self.tmp, "pending.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_6_logged_on_enqueue_result_separate_resend_next_start(self):
        gate = threading.Event()
        n = N.send_async("[손절 진행 중] 990006", log_path=self.log, sender=lambda t: gate.wait(5) and False, pending_path=self.pend)
        with open(self.log, encoding="utf-8") as f:
            self.assertIn(f"[#{n}] [손절 진행 중] 990006", f.read())              # 보내기 전에 이미 기록
        gate.set()
        N.flush(5, pending_path=self.pend)
        with open(self.log, encoding="utf-8") as f:
            self.assertIn(f"-> [#{n}] 텔레그램 보내지 못함", f.read())              # 전송 결과는 따로 한 줄
        self.assertEqual([x["text"] for x in jl(self.pend).values()], ["[손절 진행 중] 990006"])
        got = []
        run, N._RUN = N._RUN, "next-start"                                        # 다음 시작 (새 실행 = 지난 실행의 번호를 다시 보냄)
        try:
            self.assertEqual(N.resend_pending(self.pend, log_path=self.log, sender=lambda t: got.append(t) or True), 1)
        finally:
            N._RUN = run
        N.flush(5)
        self.assertTrue(got[0].startswith("[지난 미전송 알림] 1건"))
        self.assertIn("990006", got[0])
        self.assertFalse(os.path.exists(self.pend))                              # 보낸 번호를 지워 비면 파일도 지움

    def test_6_unsent_at_exit_saved(self):
        gate = threading.Event()
        N.send_async("앞 알림", sender=lambda t: gate.wait(5), pending_path=self.pend + "x")    # 보내는 중에 멈춤
        N.send_async("종료 직전 알림", log_path=self.log, sender=lambda t: True, pending_path=self.pend)
        self.assertFalse(N.flush(0.2, pending_path=self.pend))                    # 종료: 앞 알림이 막혀 있어 다 못 보냄 (반환값은 시간과 무관)
        self.assertTrue(wait_for(lambda: [x["text"] for x in jl(self.pend).values()] == ["종료 직전 알림"]))   # 못 보낸 것은 파일로 (파일 스레드, 최대 5초)
        gate.set()
        N.flush(5)


class UpperLimit(unittest.TestCase):
    """7: 상한가가 없거나 비정상이면 기준가(전일 종가) x 1.30 호가 내림, 기준가도 없으면 매수 안 함 (데이터 이상)"""

    def test_7_fallback_and_none(self):
        import resident as R
        raw = pd.DataFrame({"stk_cd": ["000010", "000020", "000030", "000040", "000050"],
                            "upl_pric": ["+6890", "", "+100", "", "+9000"],
                            "base_pric": ["5300", "1172", "15500", "", "7000"]})
        up, info = R.upper_limits(raw, {"000010", "000020", "000030", "000040", "000050"}, {"000050": 9500.0})
        self.assertEqual(up["000010"], 6890.0)                                    # 정상
        self.assertEqual(up["000020"], 1523.0)                                    # 없음 -> 1172 x 1.3 = 1523.6 -> 1원 단위 내림
        self.assertEqual(up["000030"], 20150.0)                                   # 비정상(기준가의 1.25 ~ 1.35 밖) -> 20150 (50원 단위)
        self.assertIsNone(up["000040"])                                           # 상한가·기준가 모두 없음
        self.assertEqual(up["000050"], 9100.0)                                    # 15:19 가격보다 낮은 상한가 = 비정상 -> 7000 x 1.3
        self.assertEqual(info["none"], ["000040"])
        top = pd.DataFrame({"code": ["000040", "000010"], "score": [1.0, 0.9], "raw_close": [5000.0, 5300.0]})
        plan = O.p4_plan(top, "raw_close", 30_000_000, 30_000_000, set(), "본래", upper=up)
        self.assertEqual((plan[0]["qty"], plan[0]["skip"]), (0, "데이터 이상: 상한가·전일 종가 없음"))
        self.assertEqual(plan[1]["limit_price"], 5570)                            # 5300 x 1.05 = 5565 -> 5570 (< 상한가 6890)


if __name__ == "__main__":
    unittest.main(verbosity=2)
