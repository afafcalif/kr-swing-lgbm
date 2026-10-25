"""
Codex 리뷰(11E, 2026-10-24) 1 ~ 27 재현 시험. 상태 있는 가짜 서버(test_orders.FakeKiwoom: 체결하면 보유·매도 가능 수량·예수금이 바뀜,
페이지 나눔, HTTP 200 + return_code != 0, 목록 본문)와 가짜 시계. 실제 주문·조회·파일(logs/exec) 없음.

  python -m unittest backtest_swing/live/test_codex.py -v
"""
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_orders import PLAN, FakeClock, FakeKiwoom, sends  # noqa: E402
import common as C  # noqa: E402
import engine as E  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import quotes as Q  # noqa: E402
import state as S  # noqa: E402


class Crash(BaseException):
    """강제 종료 흉내 (except Exception 에 잡히지 않음)"""


def bal_row(code, qty, avg=10000, sellable=None):
    return {"stk_cd": f"A{code}", "rmnd_qty": str(qty), "trde_able_qty": str(qty if sellable is None else sellable), "pur_pric": str(avg),
            "cur_prc": str(avg)}


def jl(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class Base(unittest.TestCase):
    def setUp(self):
        import resident as R
        self.R = R
        self.tmp = tempfile.mkdtemp()
        self.saved = (dict(R.CFG), R.POSITIONS, R.BLOCK, R.APPROVED, R.LEDGER, R.LOG_DIR, R.SNAP_DIR, R.alert, R.Caller,
                      P.raw_atr20, Q.snapshot, Q.bars_today, Q.close_values, Q.index_now)
        R.CFG["state_dir"] = self.tmp
        R.CFG["heartbeat_file"] = os.path.join(self.tmp, "hb.log")
        R.POSITIONS, R.BLOCK = os.path.join(self.tmp, "positions.json"), os.path.join(self.tmp, "block.json")
        R.APPROVED, R.LEDGER = os.path.join(self.tmp, "approved.json"), os.path.join(self.tmp, "ledger.json")
        R.LOG_DIR, R.SNAP_DIR = os.path.join(self.tmp, "logs"), os.path.join(self.tmp, "snaps")
        os.makedirs(R.LOG_DIR)
        C.atomic_json(R.LEDGER, {"as_of": "2026-10-23", "strategy": [], "full": [], "version": 3})
        self.alerts = []
        R.alert = self.alerts.append
        R.Caller = lambda *a, **k: None
        P.raw_atr20 = lambda code, until, today_row=None: (10000.0, 500.0)   # 임시 손절가 = 평균단가 - 850
        self.quotes, self.cv, self.judged = {}, {}, {}
        Q.snapshot = lambda caller, codes: (None, pd.DataFrame({"code": list(codes), "close": [self.quotes.get(c, 99999.0) for c in codes]}), {})
        Q.bars_today = lambda caller, code, day: code
        Q.close_values = lambda b: self.cv.get(b)
        self.clk = FakeClock("2026-10-26 15:22:00")
        self.fk = FakeKiwoom(self.clk)
        import run_day as RD                                        # summary.csv 도 임시 폴더로 (common 이 작업 폴더를 저장소 루트로 바꾸므로)
        self._saved_summary = RD.SUMMARY
        RD.SUMMARY = os.path.join(self.tmp, "logs", "summary.csv")

    def tearDown(self):
        import run_day as RD
        RD.SUMMARY = self._saved_summary
        R = self.R
        (cfg, R.POSITIONS, R.BLOCK, R.APPROVED, R.LEDGER, R.LOG_DIR, R.SNAP_DIR, R.alert, R.Caller,
         P.raw_atr20, Q.snapshot, Q.bars_today, Q.close_values, Q.index_now) = self.saved
        R.CFG.clear()
        R.CFG.update(cfg)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def resident(self):
        R = self.R
        res = R.Resident()
        res.clock, res.sleep = self.clk, self.clk.sleep
        res.new_broker = lambda: O.Broker(self.fk, allow_orders=True)
        res.request_prep = lambda reason: None
        res.judge = lambda codes, day, cvs=None: {c: dict(self.judged[c]) for c in codes if c in self.judged and (cvs is None or c in cvs)}
        return res

    def started(self, hms, live=True):
        """핵심 시작 끝난 상주 프로그램 (준비·전날 정산·데이터 확인 끝난 것으로)"""
        self.clk.at(hms)
        res = self.resident()
        d = self.R.new_day(self.clk(), clock=self.clk)
        res.start_core(d)
        res.started, res.prep_ok = True, True
        res.ctx = {"buy_allowed": True, "frozen": {"ok": True}, "prep": {"data": {"daily_up_to_date": True, "official_missing_dates": []}},
                   "data_version": "v1"}
        if live:
            d.set("live", {"plan": [dict(PLAN[0])], "kind": "본래"})
        d.mark("prev_settle")
        d.mark("data_check")
        return res

    def prev_state(self, steps=("ac_summary",), intents=None, settlement=None):
        d = S.Day(os.path.join(self.tmp, "2026-10-23.json"), pd.Timestamp("2026-10-23"), clock=self.clk)
        for s in steps:
            d.mark(s)
        if settlement:
            d.set("settlement", settlement)
        if intents is not None:
            p = os.path.join(self.tmp, "intents", "2026-10-23.json")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            C.atomic_json(p, intents)
        return d

    def filled_buy(self, om, code="084670", qty=28, price=33000):
        it = om.store.add(date=om.date(), stage="buy", side="buy", code=code, qty=qty, price=price, trde_tp="0", attempt=1, kind="본래",
                          reason="매수 (P4)", assumed_kind="buy")
        om.store.update(it, status=O.ACCEPTED, ord_no="B1", filled_qty=qty, fill_price=price, fill_status="체결")
        return it

    def filled_sell(self, om, code="084670", qty=28, price=29000, stage="stop"):
        it = om.store.add(date=om.date(), stage=stage, side="sell", code=code, qty=qty, price=None, trde_tp="3", attempt=1, kind="본래",
                          reason="손절", assumed_kind="stop")
        om.store.update(it, status=O.ACCEPTED, ord_no="S1", filled_qty=qty, fill_price=price, fill_status="체결")
        return it


class Codex(Base):
    # ---------------------------------------------------------------- 1
    def test_01_corrupt_intent_file_blocks_after_second_restart(self):
        p = os.path.join(self.tmp, "intents", "2026-10-26.json")
        os.makedirs(os.path.dirname(p))
        with open(p, "w", encoding="utf-8") as f:
            f.write('[{"intent_id": "x", "status": "ACCEPT')
        for hms in ("15:21:10", "15:21:40", "15:22:10"):     # 처음 + 재시작 2번
            res = self.started(hms)
            self.assertTrue(res.om.store.corrupt)
            self.assertTrue(any("의도 파일 손상" in w for w in res.buy_gate()))
            res.buy()
            res.om.stop_check({"084670": {"qty": 1, "sellable": 1, "cur_price": 1}}, {"084670": 2})
        self.assertFalse(os.path.exists(p))                    # 손상 파일은 보관으로 옮겨짐
        self.assertTrue(os.path.exists(p + ".block"))          # 차단 파일이 남아 계속 막음
        self.assertEqual(len(sends(self.fk)) + len(sends(self.fk, "kt10001")), 0)

    # ---------------------------------------------------------------- 2
    def test_02_query_validation(self):
        b = O.Broker(self.fk)
        for act in ("rc_error", "listbody"):
            self.fk.read_script["kt00001"] = [act]
            with self.assertRaises(O.QueryError):
                b.deposit()
        self.fk.read_script["kt00018"] = ["missing"]
        with self.assertRaises(O.QueryError):
            b.balance()
        with self.assertRaises(O.FieldError):
            O.check_fields_bal({})
        self.fk.read_script["kt00001"] = ["nan"]                 # 주문가능금액 NaN(빈칸) -> 조회 실패
        with self.assertRaises(O.QueryError):
            b.account()
        res = self.started("15:22:00")
        self.fk.read_script["kt00001"] = ["missing"]             # 주문가능금액 없음 -> 매수 안 함
        out = res.buy()
        self.assertEqual(out["sent"], 0)
        self.assertIn("주문가능금액 조회 실패", out["reason"])
        self.assertEqual(len(sends(self.fk)), 0)
        self.assertFalse(res.day.s["buy_decision"]["new_buy_ok"])

    def test_02_query_failure_is_not_empty_account(self):
        self.clk.at("10:00:00")
        om = O.OrderManager(O.Broker(self.fk, allow_orders=True), clock=self.clk, sleep=self.clk.sleep)
        self.fk.read_script["kt00018"] = ["rc_error"]
        pos = {"held": {"084670": {"qty": 28, "stop_price": 30000}}}
        r = O.stop_cycle(om, O.AccountWatch(), pos, lambda cs: {c: 29000 for c in cs}, om.b.balance, self.clk(), set())
        self.assertFalse(r["account_ok"])                       # 빈 계좌가 아니라 조회 실패
        self.assertEqual(r["not_in_account"], [])
        self.assertEqual(len(sends(self.fk, "kt10001")), 1)     # 상태 파일 수량으로 손절

    # ---------------------------------------------------------------- 3
    def test_03_pagination_all_pages_or_fail(self):
        self.fk.page_size = 2
        self.fk.balance_rows = [bal_row(f"00000{i}", i + 1) for i in range(5)]
        a = O.Broker(self.fk).account()
        self.assertEqual(len(a["holdings"]), 5)
        bal = [x for x in self.fk.pages if x[0] == "kt00018"]
        self.assertEqual([x[1] for x in bal], ["N", "Y", "Y"])
        self.assertEqual([x[2] for x in bal], ["", "2", "4"])
        self.fk.read_script["kt00018"] = ["ok", "http502"]      # 2페이지 실패 -> 전체 실패
        with self.assertRaises(O.NoResponse):
            O.Broker(self.fk).balance()
        self.fk.fill_rows = [{"ord_no": str(i), "cntr_qty": "1"} for i in range(3)]
        self.assertEqual(len(O.Broker(self.fk).fills()), 3)

    # ---------------------------------------------------------------- 4
    def test_04_late_start_blocks_only_buys(self):
        res = self.resident()
        self.clk.at("15:25:00")
        res.tick(self.clk())
        d = res.day
        self.assertTrue(d.done("late_start"))
        self.assertTrue(any("늦은 시작" in w for w in res.buy_gate()))
        self.assertFalse(d.s["buy_decision"]["new_buy_ok"])
        self.clk.at("15:41:00")
        res.tick(self.clk())
        for s in ("close_check", "settle", "ac_positions", "ac_ledger", "ac_summary", "after_close"):
            self.assertTrue(d.done(s), s)                        # 정산은 계속

    def test_04_prep_failure_keeps_stop_running(self):
        res = self.resident()
        res.request_prep = lambda reason: self.R.Resident.request_prep(res, reason)
        res._prep_work = lambda day, need: setattr(res, "prep_result", {"error": "RuntimeError('적재 실패')"})
        calls = []
        res.stop_minute = lambda: calls.append(self.clk().strftime("%H:%M"))
        self.clk.at("10:00:00")
        res.tick(self.clk())
        res.prep_thread.join()
        self.clk.at("10:00:01")
        res.tick(self.clk())
        self.assertFalse(res.prep_ok)
        self.assertTrue(any("준비" in w for w in res.buy_gate()))
        self.assertEqual(calls, ["10:00"])
        self.clk.at("10:01:00")
        res.tick(self.clk())
        self.assertEqual(calls, ["10:00", "10:01"])

    # ---------------------------------------------------------------- 5
    def test_05_prev_day_recovery_temp_stop_then_real_stop(self):
        self.prev_state(intents=[{"intent_id": "p1", "date": "2026-10-23", "stage": "buy", "side": "buy", "code": "084670", "qty": 28,
                                  "status": "ACCEPTED", "kind": "본래"}])
        self.fk.balance_rows = [bal_row("084670", 28, avg=10000)]
        res = self.started("09:00:00")
        res.day.s["steps"].pop("prev_settle")
        r = res.prev_settle()
        p = jl(self.R.POSITIONS)["held"]["084670"]
        self.assertEqual((p["entry_date"], p["stop_temp"], p["stop_price"]), ("2026-10-23", True, 9150.0))
        self.assertTrue(p["trade_id"].startswith("084670-2026-10-23-"))
        self.assertTrue(any("임시 손절가" in a for a in self.alerts))
        self.assertEqual(r["temp_stops"], ["084670"])
        self.clk.at("10:00:00")
        self.quotes["084670"] = 9100.0
        res.stop_minute()                                          # 임시 손절가로 실제 손절
        s = sends(self.fk, "kt10001")
        self.assertEqual((len(s), s[0][2]["ord_qty"]), (1, "28"))
        # 준비 뒤 공식 일봉으로 마무리: 진입일 종가 - 1.7 x ATR20
        self.judged["084670"] = {"close": 10500.0, "stop": 9800.0, "trend": False, "basis": "수정주가 일봉"}
        res.finalize_recovery()
        p = jl(self.R.POSITIONS)["held"]["084670"]
        self.assertEqual((p["stop_price"], p["stop_temp"]), (9800.0, False))
        self.assertTrue(res.day.s["prev_recovery"]["finalized"])

    def test_05_mark_info_cannot_override_status(self):
        d = S.Day(os.path.join(self.tmp, "x.json"), "2026-10-26", clock=self.clk)
        d.mark("a", {"status": "FAILED", "time": "00:00:00", "k": 1})
        d.finish("b", S.FAILED, {"status": "SUCCEEDED"})
        self.assertEqual((d.status("a"), d.s["steps"]["a"]["k"], d.status("b")), (S.SUCCEEDED, 1, S.FAILED))

    # ---------------------------------------------------------------- 6
    def test_06_positions_need_stop_and_entry_or_fail(self):
        res = self.started("15:45:00", live=False)
        self.filled_buy(res.om)
        res.day.set("settlement", {"account": {"084670": 28}, "avg_price": {"084670": 33000}})
        S.run_step(res.day, "ac_positions", res.ac_positions)
        self.assertEqual(res.day.status("ac_positions"), S.FAILED)  # 15:30 1분봉 없음 -> 손절가 계산 못 함 -> 실패
        self.cv["084670"] = {"open": 33000, "high": 34000, "low": 32000, "close": 33500, "volume": 1, "amount": 1}
        self.judged["084670"] = {"close": 33500.0, "stop": 31000.0, "trend": True, "basis": "수정주가 일봉"}
        self.clk.t += pd.Timedelta(seconds=61)
        S.run_step(res.day, "ac_positions", res.ac_positions)
        self.assertEqual(res.day.status("ac_positions"), S.SUCCEEDED)
        p = jl(self.R.POSITIONS)["held"]["084670"]
        self.assertEqual((p["stop_price"], p["stop_temp"], p["entry_date"]), (31000.0, False, "2026-10-26"))

    def test_06_excluded_code_stop_from_raw_daily(self):
        import daily
        saved = (daily.Store, P.raw_daily)

        class NoStore:
            def __init__(self, codes, use_append=True):
                self.adj, self.raw = {}, {}
        ts = pd.bdate_range("2026-09-01", "2026-10-26")
        close = np.linspace(10000, 12000, len(ts))
        raw = pd.DataFrame({"timestamp": ts, "open": close, "high": close + 200, "low": close - 200, "close": close})
        try:
            daily.Store, P.raw_daily = NoStore, (lambda code: raw)
            res = self.R.Resident()
            j = res.judge(["999999"], "2026-10-26", cvs={"999999": {"open": 12000, "high": 12300, "low": 11900, "close": 12100}})["999999"]
        finally:
            daily.Store, P.raw_daily = saved
        self.assertIn("원주가 일봉", j["basis"])
        self.assertAlmostEqual(j["close"], 12100.0)
        self.assertLess(j["stop"], 12100.0)

    # ---------------------------------------------------------------- 7
    def test_07_rebuy_after_liquidation_is_new_trade(self):
        P.save(self.R.POSITIONS, {"held": {"084670": {"qty": 28, "trade_id": "084670-2026-10-20-1", "entry_date": "2026-10-20",
                                                      "stop_price": 30000.0, "kind": "본래"}}, "next_open_sells": {}, "trade_seq": 1})
        res = self.started("15:45:00", live=False)
        self.filled_sell(res.om)
        self.filled_buy(res.om)
        res.day.set("settlement", {"account": {"084670": 28}, "avg_price": {"084670": 33000}})
        self.cv["084670"] = {"close": 33500}
        self.judged["084670"] = {"close": 33500.0, "stop": 31000.0, "trend": True, "basis": "수정주가 일봉"}
        res.ac_positions()
        p = jl(self.R.POSITIONS)["held"]["084670"]
        self.assertEqual(p["trade_id"], "084670-2026-10-26-2")
        self.assertEqual((p["entry_date"], p["stop_price"], p["trend_sell_next_open"]), ("2026-10-26", 31000.0, False))

    # ---------------------------------------------------------------- 8
    def test_08_positions_backup_recovery_blocks_buys_until_recovered(self):
        P.save(self.R.POSITIONS, {"held": {"084670": {"qty": 28, "entry_date": "2026-10-20", "stop_price": 30000.0}}, "next_open_sells": {}})
        P.save(self.R.POSITIONS, {"held": {"084670": {"qty": 28, "entry_date": "2026-10-20", "stop_price": 30000.0}}, "next_open_sells": {}})
        with open(self.R.POSITIONS, "w", encoding="utf-8") as f:
            f.write('{"held": {"0846')
        self.fk.balance_rows = [bal_row("084670", 28)]
        res = self.started("10:00:00")
        self.assertTrue(res.recovery_pending())
        self.assertTrue(any("보유 장부 복구 전" in w for w in res.buy_gate()))
        self.quotes["084670"] = 29000.0
        res.stop_minute()                                            # 손절은 계좌 + 백업 손절가로 계속
        self.assertEqual(len(sends(self.fk, "kt10001")), 1)
        res.recover_positions()
        self.assertFalse(res.recovery_pending())
        self.assertFalse(any("보유 장부 복구 전" in w for w in res.buy_gate()))
        p = jl(self.R.POSITIONS)["held"]["084670"]                   # 백업의 진입일·손절가 유지
        self.assertEqual((p["entry_date"], p["stop_price"], p.get("stop_temp")), ("2026-10-20", 30000.0, None))

    def test_08_missing_is_empty_unrecoverable_rebuilt_from_account(self):
        self.assertEqual(P.load(self.R.POSITIONS)[1]["status"], "missing")
        with open(self.R.POSITIONS, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.fk.balance_rows = [bal_row("084670", 28, avg=10000)]
        res = self.started("10:00:00")
        self.assertEqual(res.day.s["positions_recovery"]["status"], "corrupt_unrecovered")
        res.recover_positions()
        p = jl(self.R.POSITIONS)["held"]["084670"]
        self.assertEqual((p["qty"], p["stop_price"], p["stop_temp"]), (28, 9150.0, True))

    # ---------------------------------------------------------------- 9
    def test_09_non_dict_response_and_exception_after_send_are_unknown(self):
        self.clk.at("15:22:00")
        om = O.OrderManager(O.Broker(self.fk, allow_orders=True), clock=self.clk, sleep=self.clk.sleep)
        self.fk.script["kt10000"] = ["listbody"]
        r = om.place_buys(PLAN, "본래")[0]
        self.assertEqual(r["status"], O.UNKNOWN)
        om2 = O.OrderManager(O.Broker(self.fk, allow_orders=True), clock=self.clk, sleep=self.clk.sleep)

        def boom(*a, **k):
            raise KeyError("ord_no")
        om2._apply = boom
        r = om2.place_buys([{"code": "005930", "qty": 1, "limit_price": 100, "skip": ""}], "본래")[0]
        self.assertEqual(r["status"], O.UNKNOWN)
        self.assertEqual(len(r["checks_due"]), 3)

    # ---------------------------------------------------------------- 10
    def test_10_sell_unknown_query_failure_is_check_failed(self):
        self.clk.at("10:00:00")
        self.fk.balance_rows = [bal_row("084670", 28)]
        om = O.OrderManager(O.Broker(self.fk, allow_orders=True), clock=self.clk, sleep=self.clk.sleep)
        self.fk.script["kt10001"] = ["timeout"]
        it = om.stop_check({"084670": {"qty": 28, "sellable": 28, "cur_price": 29000}}, {"084670": 30000})[0]
        self.assertEqual(it["status"], O.UNKNOWN)
        self.clk.t += pd.Timedelta(seconds=6)
        self.fk.read_script["ka10075"] = ["http502"]
        om.reconcile()
        self.assertEqual(it["last_check"], "확인 실패")
        self.clk.t += pd.Timedelta(minutes=2)
        self.fk.read_script["kt00018"] = ["rc_error"]                  # 잔고 조회 실패 -> 확인 실패, 1분 뒤 다시
        om.reconcile()
        self.assertEqual((it["status"], it["last_check"]), (O.UNKNOWN, "확인 실패"))
        self.assertEqual(it["checks_due"], [str(self.clk() + pd.Timedelta(seconds=60))])
        self.clk.t += pd.Timedelta(seconds=61)
        om.reconcile()
        self.assertEqual(it["status"], O.NOT_SENT)                       # 잔고 그대로 -> 다시 보낼 수 있음
        self.assertTrue(it["resendable"])

    # ---------------------------------------------------------------- 11
    def test_11_stop_runs_every_minute_regardless_of_heartbeat(self):
        res = self.started("10:00:00")
        calls = []
        res.stop_minute = lambda: calls.append(self.clk().strftime("%H:%M:%S"))
        res.tick(self.clk())
        res.heartbeat("stop")
        self.clk.at("10:00:30")
        res.tick(self.clk())
        self.clk.at("10:01:00")
        res.last_hb = "2026-10-26 10:01"                                 # 하트비트가 먼저 이번 분을 썼어도
        res.tick(self.clk())
        self.assertEqual(calls, ["10:00:00", "10:01:00"])

    # ---------------------------------------------------------------- 12
    def test_12_next_open_sells_kept_until_filled(self):
        P.save(self.R.POSITIONS, {"held": {"084670": {"qty": 10, "entry_date": "2026-10-20", "stop_price": 1.0, "trend_sell_next_open": True,
                                                      "judged_on": "2026-10-23"}}, "next_open_sells": []})
        self.fk.balance_rows = [bal_row("084670", 10)]
        self.fk.script["kt10001"] = ["reject", "reject"]
        res = self.started("08:31:00")
        res.trend_sell()
        nos = jl(self.R.POSITIONS)["next_open_sells"]
        self.assertEqual((nos["084670"]["last_status"], nos["084670"]["attempts"]), (O.REJECTED, 1))
        # 다음 거래일: 목록에 남아 있어 다시 매도, 체결되면 15:35 확인 때 목록에서 빠짐
        self.clk.t = pd.Timestamp("2026-10-27 08:31:00")
        self.fk.auto_fill["084670"] = 30000
        res2 = self.started("08:31:00")
        res2.trend_sell()
        self.assertIn("084670", jl(self.R.POSITIONS)["next_open_sells"])
        self.clk.at("15:36:00")
        res2.close_check()
        self.assertNotIn("084670", jl(self.R.POSITIONS)["next_open_sells"])

    # ---------------------------------------------------------------- 13
    def test_13_close_check_incomplete_fails_but_settle_runs(self):
        res = self.started("15:22:00")
        res.buy()
        self.fk.script["kt10003"] = ["timeout", "timeout", "timeout"]     # 취소가 서버에 안 닿음 -> 잔량 남음
        self.clk.at("15:36:00")
        S.run_step(res.day, "close_check", res.close_check)
        self.assertEqual(res.day.status("close_check"), S.FAILED)
        self.assertTrue(res.day.s["steps"]["close_check"]["reasons"])
        self.clk.at("15:40:30")
        res.tick(self.clk())
        self.assertTrue(res.day.done("settle"))                           # 15:35 확인과 무관하게 15:40 정산

    # ---------------------------------------------------------------- 14
    def test_14_stop_fill_is_not_a_mismatch_and_does_not_block_buys(self):
        P.save(self.R.POSITIONS, {"held": {"084670": {"qty": 28, "entry_date": "2026-10-20", "stop_price": 30000.0}}, "next_open_sells": {}})
        self.fk.balance_rows = [bal_row("084670", 28)]
        self.fk.auto_fill["084670"] = 29500
        res = self.started("10:00:00")
        self.quotes["084670"] = 29500.0
        res.stop_minute()
        self.assertEqual(self.fk.balance_rows, [])                          # 가짜 서버 계좌에서 빠짐
        self.clk.at("10:01:00")
        r = res.stop_minute()
        self.assertEqual((r["mismatch"], r["not_in_account"]), ({}, []))
        self.assertNotIn("holding_mismatch", res.day.s.get("buy_block") or [])
        self.assertEqual(res.holding_diff(O.Broker(self.fk).account()["holdings"]), {})

    # ---------------------------------------------------------------- 15
    def test_15_reapply_official_new_version_then_pointer(self):
        import daily
        from kiwoom import krx_official
        saved = (daily.APPEND_DIR, krx_official.load_range, krx_official.apply_regular_session, krx_official.CACHE_DIR, pd.DataFrame.to_parquet)
        ad = os.path.join(self.tmp, "append")
        cache = os.path.join(self.tmp, "cache")
        os.makedirs(ad)
        os.makedirs(cache)
        d1, d2 = pd.Timestamp("2026-10-22"), pd.Timestamp("2026-10-23")

        def bars(close, ts, extra):
            return pd.DataFrame({"code": "000001", "timestamp": ts, "open": close, "high": close, "low": close, "close": close, "volume": 10.0, **extra})

        def write_all():
            bars(100.0, [d1, d2], {"amount": 1000.0}).to_parquet(os.path.join(ad, "adj.parquet"), index=False)
            bars(100.0, [d1, d2], {"factor": 1.0}).to_parquet(os.path.join(ad, "raw.parquet"), index=False)
            pd.DataFrame({"name": "kospi", "timestamp": [d1, d2], "close": [1.0, 1.0]}).to_parquet(os.path.join(ad, "index.parquet"), index=False)
            bars(105.0, [d2], {"amount": 1050.0}).to_parquet(os.path.join(ad, "pending_adj.parquet"), index=False)
            bars(105.0, [d2], {}).to_parquet(os.path.join(ad, "pending_raw.parquet"), index=False)
            pd.DataFrame({"name": "kospi", "timestamp": [d2], "close": [2.0]}).to_parquet(os.path.join(ad, "pending_index.parquet"), index=False)
            C.atomic_json(os.path.join(ad, "update.json"), {"official_missing_dates": ["2026-10-23"], "official_received": {}, "dates": ["2026-10-22"]})
        try:
            daily.APPEND_DIR, krx_official.CACHE_DIR = ad, cache
            krx_official.load_range = lambda a, b: pd.DataFrame({"date": [d2], "code": ["000001"]})
            krx_official.apply_regular_session = lambda df, off: (df, None)
            pd.DataFrame({"x": [1]}).to_parquet(os.path.join(cache, "20261023.parquet"))
            write_all()
            orig = pd.DataFrame.to_parquet
            written = []

            def fail_second(self_, path, *a, **k):               # 새 버전 폴더의 두 번째 파일 쓰기에서 실패
                if os.sep + "versions" + os.sep in str(path) or "/versions/" in str(path):
                    written.append(path)
                    if len(written) == 2:
                        raise OSError("디스크 오류")
                return orig(self_, path, *a, **k)
            pd.DataFrame.to_parquet = fail_second
            with self.assertRaises(OSError):
                daily.reapply_official()
            pd.DataFrame.to_parquet = orig
            self.assertFalse(os.path.exists(os.path.join(ad, "CURRENT.json")))                    # 포인터 안 바뀜
            self.assertEqual(C.data_dir(ad), ad)                                                      # 이전 버전(예전 위치) 그대로
            self.assertEqual(pd.read_parquet(C.data_file("adj.parquet", ad)).close.tolist(), [100.0, 100.0])
            self.assertTrue(daily.has_pending())                                                      # 다시 할 수 있음
            r = daily.reapply_official()
            self.assertTrue(r["ok"])
            self.assertEqual(C.data_dir(ad), os.path.join(ad, "versions", r["data_dir_version"]))    # 포인터만 새 버전으로
            adj = pd.read_parquet(C.data_file("adj.parquet", ad))
            raw = pd.read_parquet(C.data_file("raw.parquet", ad))
            self.assertFalse(adj.duplicated(["code", "timestamp"]).any())
            self.assertEqual(adj.sort_values("timestamp").close.tolist(), [100.0, 105.0])          # 같은 날짜는 교체
            self.assertEqual(len(raw), 2)
            self.assertEqual(jl(C.data_file("update.json", ad))["official_missing_dates"], [])     # update.json 도 같은 버전 안
            self.assertEqual(pd.read_parquet(os.path.join(ad, "adj.parquet")).close.tolist(), [100.0, 100.0])   # 예전 파일은 그대로 (안 읽음)
        finally:
            (daily.APPEND_DIR, krx_official.load_range, krx_official.apply_regular_session, krx_official.CACHE_DIR, pd.DataFrame.to_parquet) = saved

    # ---------------------------------------------------------------- 16
    def test_16_summary_uses_buy_decision(self):
        import run_day as RD
        saved = RD.SUMMARY
        RD.SUMMARY = os.path.join(self.tmp, "summary.csv")
        try:
            RD.summary({"date": "2026-10-26", "mode": "resident", "git_commit": "abcdef0", "model": {"month": "2026-10", "check": {"ok": True}},
                        "data": {"daily_up_to_date": True}, "errors": [],
                        "buy_decision": {"new_buy_ok": False, "reason": "차단 ['holding_mismatch']", "buy_block": ["holding_mismatch"]},
                        "cash_check": {"cash_diff": 12.0, "ok": True}})
            row = pd.read_csv(RD.SUMMARY, dtype=str).iloc[0]
        finally:
            RD.SUMMARY = saved
        self.assertEqual((row.new_buy_ok, row.buy_block, row.cash_diff), ("False", "holding_mismatch", "12.0"))
        self.assertIn("holding_mismatch", row.buy_skip_reason)
        res = self.started("15:22:00")
        res.block_buys("holding_mismatch", "x", "y")
        res.buy()
        self.assertEqual(res.day.s["buy_decision"]["new_buy_ok"], False)
        self.assertIn("holding_mismatch", res.day.s["buy_decision"]["buy_block"])

    # ---------------------------------------------------------------- 17
    def test_17_watchdog_creates_folders(self):
        import common
        import watchdog as W
        saved = (dict(W.CFG), common.resident_running, W.N.send, W.LOG, W.FLAG)
        try:
            W.CFG["log_dir"], W.CFG["state_dir"] = os.path.join(self.tmp, "new", "logs"), os.path.join(self.tmp, "new", "state")
            W.LOG, W.FLAG = os.path.join(W.CFG["log_dir"], "watchdog.log"), os.path.join(W.CFG["state_dir"], "flag")
            W.CFG["heartbeat_file"], W.CFG["pid_file"] = os.path.join(self.tmp, "none.log"), os.path.join(self.tmp, "none.pid")
            common.resident_running = lambda *a, **k: True
            W.N.send = lambda *a, **k: None
            W.main()
            self.assertTrue(os.path.isdir(W.CFG["log_dir"]) and os.path.isdir(W.CFG["state_dir"]))
        finally:
            W.CFG.clear()
            W.CFG.update(saved[0])
            common.resident_running, W.N.send, W.LOG, W.FLAG = saved[1], saved[2], saved[3], saved[4]

    # ---------------------------------------------------------------- 18
    def test_18_fake_server_is_stateful(self):
        self.fk.d2 = 1_000_000.0
        b = O.Broker(self.fk, allow_orders=True)
        r = b.buy_limit("084670", 10, 33350)
        self.fk.fill(r["ord_no"], 10, 33000, "15:30:00")
        a = b.account()
        self.assertEqual((a["holdings"]["084670"]["qty"], a["holdings"]["084670"]["sellable"]), (10, 10))
        self.assertLess(a["d2"], 1_000_000 - 330_000)
        self.fk.auto_fill["084670"] = 34000
        b.sell_market("084670", 4)
        a2 = b.account()
        self.assertEqual(a2["holdings"]["084670"]["qty"], 6)
        self.assertGreater(a2["d2"], a["d2"])

    # ---------------------------------------------------------------- 19
    def test_19_shadow_always_writes_shadow_paths(self):
        import run_day as RD
        saved = (RD.SUFFIX, RD.SUMMARY, S.is_trading_day, RD.resident_running if hasattr(RD, "resident_running") else None, RD.SNAP_DIR)
        try:
            RD.SNAP_DIR = os.path.join(self.tmp, "snaps_shadow")          # 2차 확인 리뷰 9: 스냅샷 폴더도 임시 경로 (저장소에 폴더를 만들지 않게)
            S.is_trading_day = lambda *a, **k: False                     # 휴장일이면 경로만 정하고 끝
            if saved[3] is not None:
                RD.resident_running = lambda *a, **k: False
            RD.SUFFIX = ""
            RD.shadow()
            self.assertEqual(RD.SUFFIX, "_shadow")
            self.assertTrue(RD.SUMMARY.endswith("summary_shadow.csv"))
        finally:
            RD.SUFFIX, RD.SUMMARY, S.is_trading_day, RD.SNAP_DIR = saved[0], saved[1], saved[2], saved[4]
            if saved[3] is not None:
                RD.resident_running = saved[3]

    # ---------------------------------------------------------------- 20
    def test_20_cash_check(self):
        self.prev_state(steps=("after_close",), settlement={"d2": 30_000_000.0})
        res = self.started("15:22:00")
        res.buy()
        no = res.om.records[0]["ord_no"]
        self.fk.fill(no, 28, 33000, "15:30:00")
        self.clk.at("15:36:00")
        res.close_check()
        self.clk.at("15:40:10")
        res.settle()
        c = res.day.s["cash_check"]
        self.assertTrue(c["ok"], c)
        self.assertLessEqual(abs(c["cash_diff"]), c["tolerance"])
        self.fk.d2 -= 50_000
        res.settle()
        self.assertFalse(res.day.s["cash_check"]["ok"])
        self.assertTrue(any("현금 차이" in a for a in self.alerts))

    # ---------------------------------------------------------------- 21
    def test_21_official_vs_1530_records_diffs_only(self):
        P.save(self.R.POSITIONS, {"held": {"084670": {"qty": 28, "entry_date": "2026-10-20", "stop_price": 30000.0, "judged_on": "2026-10-23",
                                                      "judged_by": "15:30 1분봉", "close_1530": 33000.0, "trend_sell_next_open": False}},
                                  "next_open_sells": {}})
        res = self.started("07:30:00")
        self.judged["084670"] = {"close": 33100.0, "stop": 30100.0, "trend": True, "basis": "수정주가 일봉"}
        res.official_vs_1530()
        o = res.day.s["official_vs_1530"]
        self.assertEqual((o["date"], o["checked"]), ("2026-10-23", 1))
        self.assertEqual(o["diffs"], [{"code": "084670", "close_1530": 33000.0, "close_official": 33100.0, "trend_1530": False, "trend_official": True}])
        self.assertFalse(jl(self.R.POSITIONS)["held"]["084670"]["trend_sell_next_open"])     # 판단은 바꾸지 않음

    # ---------------------------------------------------------------- 22
    def test_22_dead_worker_is_pool_error_and_blocks_buys_only(self):
        sp = E.ShardPool.__new__(E.ShardPool)
        a, b = multiprocessing.Pipe()
        b.close()

        class Dead:
            pid, exitcode = 1, -9

            def is_alive(self):
                return False
        sp.conns, sp.procs, sp.call_timeout = [a], [Dead()], 5
        with self.assertRaises(E.PoolError):
            sp._recv(0)
        with self.assertRaises(E.PoolError):
            sp._send(0, ("x", None))
        res = self.snapshot_ready()
        saved = E.run_day

        def dead(*a_, **k):
            raise E.PoolError("작업 프로세스 0 죽음")
        try:
            E.run_day = dead
            S.run_step(res.day, "snapshot", res.snapshot_and_plan)
        finally:
            E.run_day = saved
        self.assertEqual(res.day.status("snapshot"), S.FAILED)
        self.assertIn("pool_error", res.day.s["buy_block"])
        self.assertFalse(res.prep_ok)

    def snapshot_ready(self):
        res = self.started("15:18:40", live=False)
        res.pool = object()
        res.ctx.update(index={}, boosters=[], cols=[], ledgers={"strategy": [], "full": []}, amt20={}, excluded={},
                       prep={"model": {"month": "2026-10", "check": {"ok": True}}, "data": {"daily_up_to_date": True}})
        codes = ["084670", "005930"]
        snap = pd.DataFrame({"code": codes, "open": [33000.0, 60000.0], "high": [33500.0, 61000.0], "low": [32500.0, 59000.0],
                             "close": [33000.0, 60000.0], "volume": [100.0, 100.0], "amount": [1e6, 1e6], "dt": ["20261026"] * 2})
        Q.snapshot = lambda caller, cs: (pd.DataFrame({"raw": [1, 2]}), snap, {"returned": 2, "codes": 2, "seconds": 1.0})
        Q.index_now = lambda caller: ({"kospi": 1.0, "kosdaq": 1.0}, None)
        import common
        self._saved_uc = common.sd.universe_codes
        common.sd.universe_codes = lambda: codes
        self.addCleanup(lambda: setattr(common.sd, "universe_codes", self._saved_uc))
        return res

    # ---------------------------------------------------------------- 23
    def test_23_calendar_prev_day_and_data_failed(self):
        self.assertEqual(S.prev_trading_day("2026-10-26", []), pd.Timestamp("2026-10-23"))
        self.assertEqual(S.prev_trading_day("2026-10-26", {pd.Timestamp("2026-10-23")}), pd.Timestamp("2026-10-22"))
        import daily
        import run_day as RD
        ad = os.path.join(self.tmp, "append2")
        os.makedirs(ad)
        C.atomic_json(os.path.join(ad, "update.json"), {"dates": ["2026-10-21", "2026-10-22"], "data_version": "v|2026-10-22|missing=-"})
        idx = pd.Series([1.0, 1.0], index=pd.to_datetime(["2026-10-21", "2026-10-22"]))

        class Pool:
            loaded, excluded = 2, []

            def __init__(self, *a, **k):
                pass
        saved = (RD.APPEND_DIR, daily.excluded_codes, daily.index_series, E.ShardPool, RD.M.load, RD.ledger_read, S.holidays)
        try:
            RD.APPEND_DIR = ad
            daily.excluded_codes, daily.index_series = (lambda: {}), (lambda: {"kospi": idx, "kosdaq": idx})
            E.ShardPool = Pool
            RD.M.load = lambda mo: (None, [], {"retrain_day": "x", "train_end": "y"}, {"ok": True})
            RD.ledger_read = lambda p: ({"as_of": "2026-10-22", "strategy": [], "full": []}, 1)
            S.holidays = lambda *a, **k: []
            uc = C.sd.universe_codes
            C.sd.universe_codes = lambda: ["000001", "000002"]          # 외부 data/universe_*.csv 에 기대지 않음
            try:
                info = RD.prepare(pd.Timestamp("2026-10-26"))[5]
            finally:
                C.sd.universe_codes = uc
        finally:
            (RD.APPEND_DIR, daily.excluded_codes, daily.index_series, E.ShardPool, RD.M.load, RD.ledger_read, S.holidays) = saved
        # 지수·데이터 모두 10-22 에서 끝나도 달력상 직전 거래일(10-23)과 다르면 미반영
        self.assertEqual((info["data"]["expected_prev_trading_day"], info["data"]["daily_up_to_date"]), ("2026-10-23", False))
        res = self.started("07:10:00")
        res.prep_ok = False
        res.ctx = {}
        res.prep_result = {"data": {"daily_update": "fail", "error": "ConnectionError()", "official_missing_dates": []}, "error": "RuntimeError('x')"}
        res.apply_prep()
        self.assertEqual(res.day.status("data"), S.FAILED)
        self.assertTrue(any("일봉 반영 실패" in w for w in res.buy_gate()))

    # ---------------------------------------------------------------- 24
    def test_24_equity_uses_d2_deposit_and_orderable_only_for_margin(self):
        res = self.snapshot_ready()
        self.fk.d2, self.fk.orderable = 20_000_000.0, 5_000_000.0
        self.fk.balance_rows = [bal_row("005930", 10, avg=50000)]
        P.save(self.R.POSITIONS, {"held": {"005930": {"qty": 10, "entry_date": "2026-10-20", "stop_price": 1.0}}, "next_open_sells": {}})
        top = pd.DataFrame({"code": ["084670"], "score": [0.9], "raw_close": [33000.0]})
        out = {"filter": {"filter_ok": True}, "eligible": pd.DataFrame({"code": ["084670"], "score": [0.9]}), "market": {}, "errors": [],
               "ledgers": {n: {"top3": top, "busy": [], "skipped_busy": [], "ledger_status": []} for n in ("strategy", "full")}}
        saved = E.run_day
        try:
            E.run_day = lambda *a, **k: out
            res.snapshot_and_plan()
        finally:
            E.run_day = saved
        live = res.day.s["live"]
        self.assertEqual(live["equity_1519"], 20_000_000.0 + 10 * 60000.0)
        self.assertEqual(live["cash"], 5_000_000.0)
        self.assertEqual(live["equity_basis"]["deposit_field"], "d2_entra (D+2 추정예수금)")
        self.assertEqual(live["plan"][0]["amount_target"], round((20_000_000.0 + 600_000.0) / C.SLOTS))

    # ---------------------------------------------------------------- 25
    def test_25_entry_day_close_drop_no_next_day_sell(self):
        res = self.started("15:45:00", live=False)
        self.filled_buy(res.om)
        res.day.set("settlement", {"account": {"084670": 28}, "avg_price": {"084670": 33000}})
        self.cv["084670"] = {"close": 30000}
        self.judged["084670"] = {"close": 30000.0, "stop": 28000.0, "trend": True, "basis": "수정주가 일봉"}
        res.ac_positions()
        self.assertFalse(jl(self.R.POSITIONS)["held"]["084670"]["trend_sell_next_open"])     # 진입일 당일 판정 안 함
        self.clk.t = pd.Timestamp("2026-10-27 15:45:00")
        res2 = self.started("15:45:00", live=False)
        res2.day.set("settlement", {"account": {"084670": 28}, "avg_price": {"084670": 33000}})
        res2.ac_positions()
        p = jl(self.R.POSITIONS)["held"]["084670"]
        self.assertTrue(p["trend_sell_next_open"])                                              # 다음 날은 판정
        self.assertEqual(p["stop_price"], 28000.0)                                              # 손절가는 진입일 값 유지

    # ---------------------------------------------------------------- 26
    def test_26_no_resend_after_1528(self):
        self.clk.t = pd.Timestamp("2026-10-26 15:27:59.500")
        om = O.OrderManager(O.Broker(self.fk, allow_orders=True), clock=self.clk, sleep=self.clk.sleep, times={"buy_until": "15:27:59"})
        self.fk.script["kt10000"] = ["reject", "accept"]
        om.place_buys(PLAN, "본래")
        self.assertEqual(len(sends(self.fk)), 1)                     # 1초 뒤 15:28:00.5 -> 재시도 안 보냄
        self.clk.t = pd.Timestamp("2026-10-26 15:20:10")
        om2 = O.OrderManager(O.Broker(self.fk, allow_orders=True), clock=self.clk, sleep=self.clk.sleep)
        self.fk.script["kt10001"] = ["reject", "accept"]
        om2.stop_check({"084670": {"qty": 1, "sellable": 1, "cur_price": 1}}, {"084670": 2})
        self.assertEqual(len(sends(self.fk, "kt10001")), 0)          # 손절 시간창(~15:20) 밖

    # ---------------------------------------------------------------- 27
    def test_27_same_day_failure_of_candidate_blocks_buys(self):
        res = self.started("07:30:00")
        res.ctx.update(excluded={"fetch_error": ["000001"], "adj_changed": ["000003"], "no_official": []}, amt20={"000001": 1e9})
        r = res.check_exclusions()
        self.assertEqual(r["candidates"], [])                         # 후보 밖 -> 기록만
        self.assertFalse(res.day.s.get("buy_block"))
        self.assertEqual(jl(self.R.APPROVED)["items"][0]["code"], "000003")
        self.assertEqual(jl(self.R.APPROVED)["items"][0]["since"], "2026-10-26")
        res.ctx.update(excluded={"fetch_error": ["000001", "000002"]}, amt20={"000001": 1e9, "000002": 5e9})
        r = res.check_exclusions()
        self.assertEqual(r["candidates"], ["000002"])
        self.assertIn("candidate_fail:일봉 수집 실패", res.day.s["buy_block"])
        self.assertTrue(any("대상 후보" in w or "차단" in w for w in res.buy_gate()))


class ResidentPath(Base):
    def prep_out(self, up=True):
        class Pool:
            closed = False

            def close(self):
                Pool.closed = True
        return {"pool": Pool(), "boosters": [], "cols": [], "index": {}, "ledgers": {"strategy": [], "full": []}, "frozen": {"ok": True},
                "amt20": {"000001": 5e9}, "excluded": {"fetch_error": []},
                "prep": {"model": {"check": {"ok": True}}, "ledger_as_of": "2026-10-23", "ledger_version": 3,
                         "data": {"daily_up_to_date": up, "data_version": "v2", "prev_trading_day": "2026-10-23", "official_missing_dates": [],
                                  "expected_prev_trading_day": "2026-10-23", "data_last_date": "2026-10-23" if up else "2026-10-22"}},
                "data": {"daily_update": "ok", "official_missing_dates": []}}

    def test_04_prep_success_applies_and_allows_buys(self):
        res = self.started("07:20:00")
        res.prep_ok, res.ctx = False, {}
        res.prep_result = self.prep_out()
        res.apply_prep()
        self.assertTrue(res.prep_ok and res.ctx["buy_allowed"])
        self.assertTrue(res.day.done("day_start") and res.day.done("data"))
        res.day.set("live", {"plan": [dict(PLAN[0])], "kind": "본래"})
        self.assertEqual(res.buy_gate(), [])
        res.prep_result = self.prep_out(up=False)                      # 다시 적재했는데 전날 일봉 미반영 -> 매수 막음 + 정정 기록
        res.apply_prep()
        self.assertFalse(res.ctx["buy_allowed"])
        self.assertEqual(res.day.s["corrections"][0]["new"], False)
        res.prep_ok = False                                             # 다시 적재 실패 -> 이전 적재 유지
        res.prep_result = {"error": "RuntimeError('x')"}
        res.apply_prep()
        self.assertTrue(res.prep_ok)
        self.clk.at("15:18:10")                                         # 15:18 뒤에 끝난 다시 적재 -> 버리고 이전 적재 유지, 매수 막음
        res.prep_ok, old = False, res.pool
        res.prep_result = self.prep_out()
        res.apply_prep()
        self.assertTrue(res.prep_ok and res.pool is old)
        self.assertIn("late_reload", res.day.s["buy_block"])

    def test_05_prev_ledger_recomputed_when_stale(self):
        snap = os.path.join(self.tmp, "snap_1023.parquet")
        pd.DataFrame({"p_code": ["084670"], "p_open": [1.0], "p_high": [1.0], "p_low": [1.0], "p_close": [1.0], "p_volume": [1.0],
                      "p_amount": [1.0]}).to_parquet(snap, index=False)
        pday = self.prev_state(steps=("settle",), intents=[])
        pday.set("live", {"snap_file": snap, "index_values": {"kospi": 1.0, "kosdaq": 1.0}, "ledger_before": {"strategy": [], "full": []},
                          "ledgers": {"strategy": [{"code": "084670"}], "full": [{"code": "084670"}]}})
        C.atomic_json(self.R.LEDGER, {"as_of": "2026-10-22", "strategy": [], "full": [], "version": 3})
        res = self.started("07:20:00")
        res.day.s["steps"].pop("prev_settle")
        res.pool = object()
        res.ctx.update(boosters=[], cols=[], index={}, ledger_version=3)
        self.assertEqual(res.prev_settle()["ledger_stale_days"], ["2026-10-23"])
        out = {"filter": {"filter_ok": True}, "ledgers": {n: {"ledger_status": []} for n in ("strategy", "full")}}
        seen = []
        saved = E.run_day
        try:
            E.run_day = lambda pool, day, rows, *a, **k: seen.append((str(pd.Timestamp(day).date()), list(rows))) or out
            res.finalize_recovery()
        finally:
            E.run_day = saved
        led = jl(self.R.LEDGER)
        self.assertEqual(seen, [("2026-10-23", ["084670"])])
        self.assertEqual((led["as_of"], led["strategy"], led["version"]), ("2026-10-23", [{"code": "084670", "entry": "2026-10-23"}], 4))
        self.assertEqual((res.ctx["ledger_version"], res.ctx["ledgers"]["full"][0]["code"]), (4, "084670"))

    def test_forced_kill_in_settlement_substep_then_restart(self):
        res1 = self.resident()
        orig = res1.ac_positions

        def killed():
            raise Crash()
        res1.ac_positions = killed
        self.clk.at("15:41:00")
        with self.assertRaises(Crash):
            res1.tick(self.clk())
        d = jl(os.path.join(self.tmp, "2026-10-26.json"))
        self.assertEqual((d["steps"]["settle"]["status"], d["steps"]["ac_positions"]["status"]), (S.SUCCEEDED, S.RUNNING))
        del orig
        res2 = self.resident()                                        # 재시작 (실제 tick 경로)
        self.clk.at("15:41:30")
        res2.tick(self.clk())
        self.assertEqual(res2.day.status("ac_positions"), S.FAILED)   # 도중에 꺼진 단계 = FAILED, 간격 뒤 다시
        self.clk.at("15:42:05")
        res2.tick(self.clk())
        for s in ("settle", "ac_positions", "ac_ledger", "ac_summary", "after_close"):
            self.assertTrue(res2.day.done(s), s)
        self.assertEqual(res2.day.s["steps"]["settle"]["attempts"], 1)  # 끝난 정산은 다시 안 함
        self.assertEqual(res2.day.s["steps"]["ac_positions"]["attempts"], 2)
        self.assertEqual(res2.day.s["restarts"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
