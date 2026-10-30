"""
11J 보완 3 (2026-10-27) 재현 시험: 매도 의무를 종목 코드 단위로, 증권사 잔고가 정답 (보완 2 의 거래 단위 남은 수량 계산을 걷어냄).
미전송 알림 번호 단위 (보완 2 N6·N7) 는 그대로. 가짜 서버(매도 접수 = 매도 가능 수량 묶음, 넘는 매도는 거부)·가짜 시계, 실제 주문·조회·logs/exec 없음.

  python -m unittest backtest_swing/live/test_broad3.py -v
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
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_codex  # noqa: E402
from test_codex import Base, bal_row, jl  # noqa: E402
from test_orders import FakeClock, FakeKiwoom, sends, wait_for  # noqa: E402
import common as C  # noqa: E402
import engine as E  # noqa: E402
import notify as N  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import quotes as Q  # noqa: E402

TID = "990006-2026-10-20-1"
POS = {"990006": {"qty": 28, "trade_id": TID, "entry_date": "2026-10-20", "stop_price": 30000.0, "kind": "본래"}}
STOP = O.new_duty("stop", "2026-10-26 09:30:00", "현재가 29900 < 손절가 30000", price=29900.0, stop=30000.0)
TREND = O.new_duty("trend", "2026-10-23 15:40:00", "추세 매도 (2026-10-23 종가 < 20일선, 15:30 1분봉)")


def setup(t="2026-10-26 10:00:00"):
    clk = FakeClock(t)
    fk = FakeKiwoom(clk)
    notes = []
    om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep, notify=notes.append)
    return clk, fk, om, notes


def positions(duty=None, listed=False, **extra):
    held = json.loads(json.dumps(POS))
    held["990006"].update(extra)
    nos = {"990006": {"reason": duty["reason"], "since": duty["since"], "attempts": 0, "last_status": None}} if listed else {}
    return {"held": held, "sell_duty": {"990006": dict(duty)} if duty else {}, "next_open_sells": nos, "trade_seq": 1,
            "duty_format": P.DUTY_FORMAT}


def qtys(fk):
    return [int(c[2]["ord_qty"]) for c in sends(fk, "kt10001")]


def bal(fk):
    return lambda: fk.request("kt00018", "POST", "/x").json()


def fail():
    raise O.NoResponse("계좌 조회 실패")


def cycle(om, fk, pos, quotes=None, balance=None, **kw):
    return O.stop_cycle(om, O.AccountWatch(), pos, lambda cs, deadline=None: dict(quotes or {}), balance or bal(fk), om.clock(), set(),
                        budget=10, **kw)


class AccountFail(unittest.TestCase):
    """규칙 2·3: 계좌 조회가 되면 매도 가능 수량, 실패하면 오늘 걸린 매도가 없을 때만 보유 파일 수량. 의무는 계좌 보유 0 일 때만 끝"""

    def test_consecutive_account_fail_with_residual(self):
        clk, fk, om, _ = setup()
        fk.balance_rows = [bal_row("990006", 28, sellable=20)]                   # 8주는 다른 곳에 묶임
        pos = positions(STOP)
        cycle(om, fk, pos)
        fk.fill(om.records[0]["ord_no"], 20, 29900, "10:00:05")                   # 20주 체결, 8주 남음
        fk.balance_rows[0]["trde_able_qty"] = "8"                                # 묶임이 풀림
        for hms in ("10:01:00", "10:02:00", "10:03:00"):                          # 계좌 조회 연속 실패
            clk.at(hms)
            r = cycle(om, fk, pos, balance=fail)
            self.assertIn("걸린 매도", r["orders"][0]["skipped"])                 # 오늘 접수된 매도가 있으면 보유 파일 수량으로 안 보냄
        self.assertEqual(qtys(fk), [20])
        self.assertEqual(list(pos["sell_duty"]), ["990006"])                     # 조회 실패로는 의무가 끝나지 않음
        clk.at("10:04:00")
        cycle(om, fk, pos)                                                        # 회복 -> 계좌 매도 가능 8
        self.assertEqual(qtys(fk), [20, 8])
        clk.at("10:05:00")
        cycle(om, fk, pos)
        self.assertEqual(qtys(fk), [20, 8])                                       # 매도 가능 0 (걸린 8) -> 안 보냄
        fk.fill(om.records[-1]["ord_no"], 8, 29900, "10:05:10")
        clk.at("10:06:00")
        r = cycle(om, fk, pos)
        self.assertEqual((r["duty_ended"], r["duty_zero"]), ([], ["990006"]))     # R1: 첫 보유 0 은 기록만
        clk.at("10:07:00")
        r = cycle(om, fk, pos)
        self.assertEqual((r["duty_ended"], pos["sell_duty"]), (["990006"], {}))   # 1분 뒤 다시 0 -> 의무 끝

    def test_account_fail_no_open_sell_sends_file_qty_excess_rejected(self):
        clk, fk, om, _ = setup()
        fk.balance_rows = [bal_row("990006", 28, sellable=20)]
        pos = positions(STOP)
        cycle(om, fk, pos, balance=fail)                                          # 오늘 걸린 매도 없음 -> 보유 파일 28
        clk.at("10:01:00")
        cycle(om, fk, pos)                                                        # 다음 분 계좌 매도 가능 20
        self.assertEqual(qtys(fk), [28, 20])
        self.assertEqual([i["status"] for i in om.records], [O.REJECTED, O.ACCEPTED])
        self.assertIn("매도 가능 수량 부족", om.records[0]["return_msg"])         # 초과분은 증권사 거부로 끝남
        self.assertEqual(om.records[0]["qty_basis"], "보유 파일 수량 (계좌 조회 실패)")


class Excess(unittest.TestCase):
    """규칙 9: 가짜 서버는 매도 가능 수량을 넘는 주문을 줄이지 않고 거부, 걸린 매도는 매도 가능 수량에서 뺀다"""

    def test_over_sellable_rejected_not_clamped(self):
        clk, fk, om, _ = setup()
        fk.balance_rows = [bal_row("990006", 28, sellable=20)]
        b = O.Broker(fk, allow_orders=True)
        self.assertEqual(b.sell_market("990006", 28)["outcome"], O.REJECTED)
        self.assertEqual(fk.sellable("990006"), 20)                               # 거부된 주문은 묶지 않음
        self.assertEqual(b.sell_market("990006", 20)["outcome"], O.ACCEPTED)
        self.assertEqual(fk.sellable("990006"), 0)                                # 걸린 매도 20 을 뺌
        self.assertEqual(b.sell_market("005930", 1)["outcome"], O.REJECTED)      # 보유 없는 종목


class LastMinute(Base):
    """규칙 6·7: 15:28:50 계좌 조회가 5초 시간 제한으로 실패해도 걸린 매도가 없으면 15:29 전에 보유 파일 수량으로 보냄"""

    def test_152850_account_fail_still_sends(self):
        P.save(self.R.POSITIONS, positions(STOP))
        self.fk.balance_rows = [bal_row("990006", 28)]
        res = self.started("15:28:50", live=False)
        real, seen = self.fk.request, []

        def req(tr_code, method, path, json=None, **kw):
            seen.append((tr_code, kw.get("timeout")))
            if tr_code == "kt00018":
                self.clk.t += pd.Timedelta(seconds=5)
                raise requests.Timeout("read timeout (5초)")
            return real(tr_code, method, path, json=json, **kw)
        self.fk.request = req
        r = res.stop_minute(detect=False)
        self.assertFalse(r["account_ok"])
        self.assertEqual(qtys(self.fk), [28])
        it = res.om.records[0]
        self.assertEqual((it["status"], it["created"][:8], it["qty_basis"], it["closing_auction"]),
                         (O.ACCEPTED, "15:28:55", "보유 파일 수량 (계좌 조회 실패)", True))
        self.assertEqual(seen, [("kt00018", 5), ("kt10001", 5)])                  # 손절 루프 안 HTTP 요청은 5초 제한, 체결 조회 없음


class LateClose(Base):
    """규칙 5: 15:35·15:40 다음 날 시가 목록 = 계좌 보유가 남은 의무 종목. 15:40 보유 갱신 뒤 늦게 끝난 15:35 확인도 체결 수량을 다시 빼지 않음"""

    def test_late_close_check_after_1540_keeps_list(self):
        P.save(self.R.POSITIONS, positions(STOP))
        self.fk.balance_rows = [bal_row("990006", 28, sellable=20)]
        res = self.started("10:00:00", live=False)
        res.stop_minute()
        self.fk.fill(res.om.records[0]["ord_no"], 20, 29900, "10:00:10")         # 20주 체결, 8주 남음 (매도 가능 0)
        self.clk.at("15:45:00")
        res.day.set("settlement", {"account": {"990006": 8}, "avg_price": {"990006": 33000}})
        self.cv["990006"] = {"close": 29500, "open": 29800}
        self.judged["990006"] = {"close": 29500.0, "stop": 28000.0, "trend": False, "basis": "수정주가 일봉"}
        res.ac_positions()                                                        # 15:40 보유 갱신 (보유 파일 8주)
        pos = jl(self.R.POSITIONS)
        self.assertEqual((pos["held"]["990006"]["qty"], list(pos["sell_duty"])), (8, ["990006"]))
        self.assertIn("계좌 8주 남음", pos["next_open_sells"]["990006"]["reason"])
        self.clk.at("15:46:00")
        res.close_check()                                                         # 늦게 끝난 15:35 확인 (예전: 8 - 체결 20 -> 의무 끝)
        pos = jl(self.R.POSITIONS)
        self.assertIn("990006", pos["next_open_sells"])
        self.assertEqual(pos["sell_duty"]["990006"]["kind"], "stop")
        self.assertIn("계좌 8주 남음", pos["next_open_sells"]["990006"]["reason"])

    def test_1535_account_fail_keeps_list(self):
        P.save(self.R.POSITIONS, positions(STOP, listed=True))
        self.fk.balance_rows = [bal_row("990006", 28)]
        res = self.started("15:35:01", live=False)
        self.fk.read_script["kt00018"] = ["http502"]
        res.close_check()
        pos = jl(self.R.POSITIONS)
        self.assertEqual((list(pos["sell_duty"]), list(pos["next_open_sells"])), (["990006"], ["990006"]))   # 조회 실패면 지우지 않음
        self.assertIn("계좌 조회 실패", pos["next_open_sells"]["990006"]["reason"])


class SameMinute(unittest.TestCase):
    """추세 매도 의무와 새 손절이 같은 분: 한 종목은 한 번만 (의무가 있으면 새 손절 감지에서 뺌), 다른 종목의 새 손절은 표시를 먼저 저장하고 보냄"""

    def test_trend_duty_and_new_stop_same_minute(self):
        clk, fk, om, _ = setup()
        fk.balance_rows = [bal_row("990006", 28), bal_row("005930", 10)]
        pos = positions(TREND)
        pos["held"]["005930"] = {"qty": 10, "trade_id": "005930-2026-10-20-2", "entry_date": "2026-10-20", "stop_price": 50000.0}
        log = []
        real = om.b.sell_market
        om.b.sell_market = lambda c, q: (log.append(("send", c, q)), real(c, q))[1]
        r = cycle(om, fk, pos, quotes={"990006": 29000.0, "005930": 49000.0},   # 990006 도 손절가 아래
                  on_trigger=lambda new: log.append(("save", sorted(new))))
        self.assertEqual(log, [("send", "990006", 28), ("save", ["005930"]), ("send", "005930", 10)])
        self.assertEqual([(i["code"], i["stage"]) for i in om.records], [("990006", "trend_late"), ("005930", "stop")])
        self.assertEqual(list(r["triggered_new"]), ["005930"])
        self.assertEqual({c: d["kind"] for c, d in pos["sell_duty"].items()}, {"990006": "trend", "005930": "stop"})
        clk.at("10:01:00")
        cycle(om, fk, pos, quotes={"990006": 29000.0, "005930": 49000.0})
        self.assertEqual(len(sends(fk, "kt10001")), 2)                            # 둘 다 걸림 -> 다음 분 안 보냄


class OldFormat(Base):
    """규칙 8: 예전 형식(515f554 거래 단위·trade_id 없음) 의무·목록은 시작 때 종목 코드 단위로 바꾸고 알림, 계좌에 없으면 버림.
    이전 거래의 목록 항목(trade_id 다름 = 같은 날 다시 산 새 거래)은 버려 새 거래를 팔지 않음"""

    def test_old_format_converted_and_rebuy_not_sold(self):
        old = {"held": {"990006": {**POS["990006"], "stop_triggered": {"since": "2026-10-23 10:00:00", "price": 29900.0, "stop": 30000.0, "trade_id": TID}},
                        "000660": {"qty": 5, "trade_id": "000660-2026-10-20-2", "entry_date": "2026-10-20", "stop_price": 1.0},
                        "035720": {"qty": 3, "trade_id": "035720-2026-10-23-4", "entry_date": "2026-10-23", "stop_price": 1.0}},
               "next_open_sells": {"000660": {"trade_id": "000660-2026-10-20-2", "reason": "추세 매도", "since": "2026-10-23"},
                                   "035720": {"trade_id": "035720-2026-10-20-3", "reason": "이전 거래 손절 남음", "since": "2026-10-23"},
                                   "051910": {"reason": "예전 형식"}},
               "trade_seq": 4}
        C.atomic_json(self.R.POSITIONS, old)
        self.fk.balance_rows = [bal_row("990006", 28), bal_row("000660", 5), bal_row("035720", 3)]
        res = self.started("08:31:00", live=False)
        pos = jl(self.R.POSITIONS)
        self.assertEqual({c: d["kind"] for c, d in pos["sell_duty"].items()}, {"990006": "stop", "000660": "trend"})
        self.assertEqual(sorted(pos["next_open_sells"]), ["000660"])             # 이전 거래 항목은 버림, 보유 없는 옛 항목은 의무 아님
        self.assertNotIn("stop_triggered", pos["held"]["990006"])
        self.assertNotIn("duty_converted", pos)
        self.assertNotIn("trade_id", pos["next_open_sells"]["000660"])
        self.assertTrue(any("[매도 의무 형식 변환]" in a for a in self.alerts))
        self.assertTrue(any(a.startswith("[매도 의무 확인 필요] 051910") and "보유 파일 없음" in a for a in self.alerts))   # R2: 사람 확인
        res.trend_sell()                                                          # 08:30 ~ 08:59: 목록만 (새 거래 035720 은 안 팖)
        self.assertEqual(qtys(self.fk), [5])
        self.clk.at("09:00:00")
        self.quotes.update({"990006": 31000.0, "000660": 99999.0, "035720": 99999.0})
        res.stop_minute()                                                         # 손절 의무 990006 (반등해도), 000660 은 걸림
        self.assertEqual(qtys(self.fk), [5, 28])
        self.assertEqual([i["stage"] for i in res.om.records], ["trend_sell", "stop"])


class RebuyAfterStop(Base):
    """규칙 4 (보완 2 N1): 의무가 남은 종목은 매수 계획에서 보유 중 -> 같은 날 다시 사지 않음. 의무가 끝난 뒤에만 다시 살 수 있고 새 거래는 의무와 무관"""

    def plan(self, res):
        top = pd.DataFrame({"code": ["990006"], "score": [0.9], "raw_close": [33000.0]})
        out = {"filter": {"filter_ok": True}, "eligible": pd.DataFrame({"code": ["990006"], "score": [0.9]}), "market": {}, "errors": [],
               "ledgers": {n: {"top3": top, "busy": [], "skipped_busy": [], "ledger_status": []} for n in ("strategy", "full")}}
        saved = E.run_day
        try:
            E.run_day = lambda *a, **k: out
            res.snapshot_and_plan()
        finally:
            E.run_day = saved
        return res.day.s["live"]["plan"][0]

    def test_stop_then_rebuy_same_day(self):
        P.save(self.R.POSITIONS, positions())
        self.fk.balance_rows = [bal_row("990006", 28)]
        res = self.started("10:00:00", live=False)
        self.quotes["990006"] = 29900.0
        res.stop_minute()
        self.fk.fill(res.om.records[0]["ord_no"], 28, 29900, "10:00:10")         # 다 체결 -> 계좌 0
        self.clk.at("10:01:00")
        self.fk.read_script["kt00018"] = ["http502"]                             # 그 뒤 계좌 조회 실패 -> 의무는 그대로
        res.stop_minute()
        self.assertEqual(list(jl(self.R.POSITIONS)["sell_duty"]), ["990006"])
        res = test_codex.Codex.snapshot_ready(self)                              # 15:18:40 주문 계획 (계좌에는 없음)
        real = Q.snapshot
        raw = pd.DataFrame({"stk_cd": ["990006", "005930"], "upl_pric": ["+42900", "+78000"], "base_pric": ["33000", "60000"]})
        Q.snapshot = lambda caller, cs, **k: (raw,) + tuple(real(caller, cs)[1:])
        p = self.plan(res)
        self.assertEqual((p["qty"], p["skip"]), (0, "보유 중 (매도 의무 남음)"))  # 의무가 남아 다시 사지 않음
        self.clk.at("15:19:00")
        self.assertEqual(res.stop_minute()["duty_zero"], ["990006"])              # 성공한 계좌 조회에서 보유 0 (한 번)
        self.clk.at("15:20:00")
        self.assertEqual(res.stop_minute()["duty_ended"], ["990006"])             # 1분 뒤 다시 0 -> 의무 끝 (R1)
        self.clk.at("15:20:30")
        p = self.plan(res)
        self.assertEqual(p["skip"], "")                                           # 의무가 끝난 뒤에는 살 수 있음
        no = self.fk._new_order({"stk_cd": "990006", "ord_qty": "28", "ord_uv": "34650"}, "kt10000")
        b = res.om.store.add(date="2026-10-26", stage="buy", side="buy", code="990006", qty=28, price=34650, trde_tp="0", attempt=1,
                             kind="본래", reason="매수 (P4)", assumed_kind="buy")
        res.om.store.update(b, status=O.ACCEPTED, ord_no=no)
        self.fk.fill(no, 28, 33000, "15:30:00")
        self.clk.at("15:35:01")
        res.close_check()
        self.assertEqual(jl(self.R.POSITIONS)["next_open_sells"], {})            # 새 거래는 다음 날 시가 매도 아님
        self.clk.at("15:45:00")
        res.day.set("settlement", {"account": {"990006": 28}, "avg_price": {"990006": 33000}})
        self.cv["990006"] = {"close": 33500, "open": 33000}
        self.judged["990006"] = {"close": 33500.0, "stop": 31000.0, "trend": False, "basis": "수정주가 일봉"}
        res.ac_positions()
        pos = jl(self.R.POSITIONS)
        self.assertEqual((pos["held"]["990006"]["trade_id"], pos["sell_duty"], pos["next_open_sells"]), ("990006-2026-10-26-2", {}, {}))


class Residual(Base):
    """보완 2 N2 ~ N4 시나리오를 새 규칙으로: 남은 수량은 계좌 매도 가능 수량으로 팔릴 때까지 (손절·추세 매도 같은 경로)"""

    def test_N2_fill_after_account_query_no_excess(self):
        clk, fk, om, _ = setup()
        fk.balance_rows = [bal_row("990006", 28, sellable=20)]
        pos = positions(STOP)
        cycle(om, fk, pos)
        no = om.records[0]["ord_no"]
        clk.at("10:01:00")
        fk.balance_rows[0]["trde_able_qty"] = "8"                                # 20 은 걸려 있고 8 이 풀림

        def bal_then_fill():
            j = bal(fk)()
            fk.fill(no, 20, 29900, "10:01:01")                                    # 계좌 조회 직후 다 체결
            return j
        cycle(om, fk, pos, balance=bal_then_fill)
        self.assertEqual(qtys(fk), [20, 8])                                       # 28 이 아니라 계좌 매도 가능 8
        self.assertEqual(om.records[-1]["status"], O.ACCEPTED)
        clk.at("10:02:00")
        cycle(om, fk, pos)
        self.assertEqual(qtys(fk), [20, 8])

    def test_N3_trend_sell_residual_sold_intraday(self):
        P.save(self.R.POSITIONS, positions(TREND, listed=True, trend_sell_next_open=True, judged_on="2026-10-23"))
        self.fk.balance_rows = [bal_row("990006", 28, sellable=20)]
        res = self.started("08:31:00", live=False)
        res.trend_sell()
        self.assertEqual(qtys(self.fk), [20])
        self.fk.fill(res.om.records[0]["ord_no"], 20, 31000, "09:00:01")          # 시가에 20 체결, 8 남음
        self.fk.balance_rows[0]["trde_able_qty"] = "8"
        self.clk.at("09:01:00")
        self.quotes["990006"] = 35000.0
        res.stop_minute()
        self.assertEqual(qtys(self.fk), [20, 8])                                  # 남은 8 장중 시장가
        last = res.om.records[-1]
        self.assertEqual((last["stage"], last["not_open"], last["qty_basis"]), ("trend_late", True, "계좌 매도 가능 수량"))
        self.fk.fill(last["ord_no"], 8, 31000, "09:01:05")
        self.clk.at("09:02:00")
        res.stop_minute()
        self.clk.at("09:03:00")
        r = res.stop_minute()
        self.assertEqual((qtys(self.fk), r["duty_ended"]), ([20, 8], ["990006"]))
        self.clk.at("15:35:01")
        res.close_check()
        pos = jl(self.R.POSITIONS)
        self.assertEqual((pos["sell_duty"], pos["next_open_sells"]), ({}, {}))  # 다 팔림 = 의무 끝

    def test_N4_residual_rejected_then_retried(self):
        clk, fk, om, _ = setup()
        fk.balance_rows = [bal_row("990006", 28, sellable=20)]
        pos = positions(STOP)
        cycle(om, fk, pos)
        fk.fill(om.records[0]["ord_no"], 20, 29900, "10:00:05")
        fk.balance_rows[0]["trde_able_qty"] = "8"
        fk.script["kt10001"] = ["reject", "accept"]
        for hms in ("10:01:00", "10:02:00", "10:03:00"):
            clk.at(hms)
            cycle(om, fk, pos)
        self.assertEqual(qtys(fk), [20, 8, 8])                                    # 남은 8 거부 -> 다음 분 다시 (접수 뒤 멈춤)
        self.assertEqual([i["status"] for i in om.records], [O.ACCEPTED, O.REJECTED, O.ACCEPTED])


class AlertPending(unittest.TestCase):
    """N6·N7 (보완 2, 그대로): 미전송 알림 파일은 알림 번호 단위. 보내기에 성공한 번호만 지움, 복원은 파일을 지우지 않고 보낸 번호만 지움"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pend = os.path.join(self.tmp, "pending.json")
        self.run = N._RUN

    def tearDown(self):
        N._RUN = self.run
        N.flush(5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def texts(self):
        return sorted(x["text"] for x in jl(self.pend).values()) if os.path.exists(self.pend) else []

    def test_N6_partial_failure_then_restart(self):
        gate = threading.Event()
        N.send_async("a1", sender=lambda t: gate.wait(5) and False, pending_path=self.pend)   # 실패
        N.send_async("a2", sender=lambda t: True, pending_path=self.pend)                    # a1 실패 기록 때 아직 큐에 있음 -> 나중에 성공
        N.send_async("a3", sender=lambda t: False, pending_path=self.pend)                   # 실패
        self.assertTrue(wait_for(lambda: self.texts() == ["a1", "a2", "a3"]))     # 보완 3 N3: 알림 파일 스레드가 넣은 번호로 씀 (보내기가 막혀 있어도, 최대 5초)
        gate.set()
        self.assertFalse(N.flush(5, pending_path=self.pend))
        self.assertEqual(self.texts(), ["a1", "a3"])                               # 보낸 a2 만 지움
        N._RUN = "next-start"                                                      # 재시작
        got = []
        self.assertEqual(N.resend_pending(self.pend, sender=lambda t: got.append(t) or True), 2)
        N.flush(5)
        self.assertEqual(len(got), 1)
        self.assertIn("a1", got[0])
        self.assertIn("a3", got[0])
        self.assertFalse(os.path.exists(self.pend))

    def test_N7_exit_during_restore_keeps_file(self):
        C.atomic_json(self.pend, {f"old#{i}": {"time": f"2026-10-26 10:{i:02d}:00", "text": f"m{i}"} for i in range(12)})
        N._RUN = "start-1"
        gate = threading.Event()
        self.assertEqual(N.resend_pending(self.pend, sender=lambda t: gate.wait(5) and False), 12)   # 보내기 전에 끝나는 실행
        self.assertEqual(len(self.texts()), 12)                                    # 읽기만 함 (지우지 않음)
        self.assertFalse(N.flush(0.2, pending_path=self.pend))                     # 보내는 중 종료
        self.assertEqual(len(self.texts()), 12)
        gate.set()
        N.flush(5)
        N._RUN = "start-2"
        sent = []
        self.assertEqual(N.resend_pending(self.pend, sender=lambda t: bool(sent.append(t)) or len(sent) == 1), 12)   # 첫 통만 성공
        N.flush(5)
        self.assertEqual(len(sent), 2)                                             # 10건 + 2건 두 통
        self.assertEqual(self.texts(), ["m10", "m11"])                             # 보낸 통의 번호만 지움
        N._RUN = "start-3"
        self.assertEqual(N.resend_pending(self.pend, sender=lambda t: True), 2)
        N.flush(5)
        self.assertFalse(os.path.exists(self.pend))


if __name__ == "__main__":
    unittest.main(verbosity=2)
