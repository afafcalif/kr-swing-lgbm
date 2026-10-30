"""
Codex 넓은 리뷰 반영 1차(11J, 2026-10-26) 재현 시험. 가짜 서버·가짜 시계, 실제 주문·조회·logs/exec 없음.

  python -m unittest backtest_swing/live/test_broad1.py -v
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
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import Base, bal_row, jl  # noqa: E402
from test_orders import PLAN, FakeClock, FakeKiwoom, acct_view, hold, sends  # noqa: E402
import common as C  # noqa: E402
import notify as N  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402

POS = {"990006": {"qty": 28, "trade_id": "990006-2026-10-20-1", "entry_date": "2026-10-20", "stop_price": 30000.0, "kind": "본래"}}


class StickyStop(Base):
    """1: 손절 상태 고정 (미만, 반등해도 팔릴 때까지 매 분, 상한 없음, 재시작 유지, 계좌 매도 가능 수량)"""

    def setUp(self):
        super().setUp()
        P.save(self.R.POSITIONS, {"held": json.loads(json.dumps(POS)), "next_open_sells": {}, "trade_seq": 1})
        self.fk.balance_rows = [bal_row("990006", 28, avg=33000, sellable=20)]   # 매도 가능 20주 (8주는 묶임)

    def test_1_strict_less_than(self):
        res = self.started("10:00:00", live=False)
        self.quotes["990006"] = 30000.0                                            # 손절가와 같음 -> 백테스트(저가 < 손절가)처럼 손절 아님
        res.stop_minute()
        self.assertEqual(sends(self.fk, "kt10001"), [])
        self.assertEqual(P.load(self.R.POSITIONS)[0]["sell_duty"], {})

    def test_1_triggered_sticks_after_rebound_and_restart(self):
        res = self.started("10:00:00", live=False)
        self.fk.script["kt10001"] = ["reject", "reject", "accept"]
        self.quotes["990006"] = 29990.0
        res.stop_minute()
        p = jl(self.R.POSITIONS)["sell_duty"]["990006"]                          # 11J 보완 3: 종목 코드 단위 의무
        self.assertEqual((p["kind"], p["since"], p["at"], p["price"], p["stop"]), ("stop", "2026-10-26", "10:00:00", 29990.0, 30000.0))
        s = sends(self.fk, "kt10001")
        self.assertEqual((len(s), s[0][2]["ord_qty"]), (1, "20"))                 # 계좌 매도 가능 수량
        self.quotes["990006"] = 31500.0                                            # 반등
        self.clk.at("10:01:00")
        res.stop_minute()
        self.assertEqual(len(sends(self.fk, "kt10001")), 2)                       # 그래도 다시
        res2 = self.started("10:05:00", live=False)                              # 재시작 (새 상주 프로그램)
        res2.stop_minute()
        self.assertEqual(len(sends(self.fk, "kt10001")), 3)                       # 상태 유지 -> 다시 보냄 (이번엔 접수)
        self.clk.at("10:06:00")
        res2.stop_minute()
        self.assertEqual(len(sends(self.fk, "kt10001")), 3)                       # 접수된 뒤에는 안 보냄
        self.assertTrue(any("[손절 진행 중]" in a for a in self.alerts))

    def test_1_no_cap_until_close_with_growing_gap(self):
        clk = FakeClock("2026-10-26 09:00:00")
        fk = FakeKiwoom(clk)
        fk.script["kt10001"] = ["reject"] * 500
        om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep)
        held, tr = {"990006": {"qty": 28, "sellable": 28, "cur_price": 29000.0}}, {}
        times = []
        while clk().strftime("%H:%M") < "15:30":
            n = len(sends(fk, "kt10001"))
            om.stop_check(held, {"990006": 30000.0}, triggered=tr)
            if len(sends(fk, "kt10001")) > n:
                times.append(clk().strftime("%H:%M"))
            clk.t += pd.Timedelta(minutes=1)
        self.assertEqual(times[:5], ["09:00", "09:01", "09:02", "09:03", "09:04"])  # 5회까지 매 분
        gaps = [(pd.Timestamp(f"2026-10-26 {b}") - pd.Timestamp(f"2026-10-26 {a}")).seconds // 60 for a, b in zip(times[4:], times[5:])]
        self.assertEqual(gaps[:4], [2, 4, 8, 10])                                  # 그 뒤 간격을 늘림 (최대 10분)
        self.assertGreaterEqual(times[-1], "15:20")                               # 장 끝까지 (5회 상한 없음)
        self.assertLess(times[-1], "15:29")


class LateTrend(Base):
    """2: 추세 매도 대상 고정, 08:30 ~ 08:59 에 못 팔면 09:00 이후 장중 시장가 (시가 매도 아님)"""

    def setUp(self):
        super().setUp()
        held = json.loads(json.dumps(POS))
        held["990006"].update(trend_sell_next_open=True, judged_on="2026-10-23")
        P.save(self.R.POSITIONS, {"held": held, "next_open_sells": {}, "trade_seq": 1})
        self.fk.balance_rows = [bal_row("990006", 28, avg=33000)]
        self.quotes["990006"] = 35000.0                                            # 손절 아님

    def late_sells(self):
        return [i for i in self.res.om.records if i["stage"] == "trend_late"]

    def test_2_rejected_at_open_sold_after_0900(self):
        self.res = res = self.started("08:30:00", live=False)
        self.fk.script["kt10001"] = ["reject", "reject"]
        res.trend_sell()
        self.assertEqual([i["status"] for i in res.om.records], [O.REJECTED, O.REJECTED])   # 08:30 거부 2회 (시가 매도 상한)
        self.assertIn("990006", jl(self.R.POSITIONS)["next_open_sells"])         # 목록에 남음
        self.clk.at("09:00:00")
        res.stop_minute()
        late = self.late_sells()
        self.assertEqual(len(late), 1)
        self.assertEqual((late[0]["status"], late[0]["trde_tp"], late[0]["not_open"]), (O.ACCEPTED, "3", True))
        self.assertIn("시가 매도 아님", late[0]["reason"])
        self.clk.at("09:01:00")
        res.stop_minute()
        self.assertEqual(len(self.late_sells()), 1)                              # 접수 뒤에는 다시 안 보냄

    def test_2_late_start_sells_first_chance(self):
        self.res = res = self.started("10:00:00", live=False)                    # 08:30 ~ 08:59 를 놓침 (늦은 시작·재시작)
        res.stop_minute()
        self.assertEqual(len(self.late_sells()), 1)
        self.assertEqual(sends(self.fk, "kt10001")[0][2]["ord_qty"], "28")

    def test_2_target_kept_even_if_later_judged_false(self):
        self.res = res = self.started("15:41:00", live=False)
        pos = P.load(self.R.POSITIONS)[0]
        res.mark_trend(pos, "990006", "2026-10-26")
        pos["held"]["990006"]["trend_sell_next_open"] = False                    # 뒤에 판정이 바뀌어도
        P.save(self.R.POSITIONS, pos)
        self.assertEqual(list(P.load(self.R.POSITIONS)[0]["sell_duty"]), ["990006"])   # 팔릴 때까지(계좌 보유 0) 의무

    def test_2_ac_positions_puts_trend_target_in_list(self):
        self.res = res = self.started("15:41:00", live=False)
        self.cv["990006"] = {"open": 1.0, "high": 1.0, "low": 1.0, "close": 29000.0, "volume": 1.0, "amount": 1.0, "last_ts": "x"}
        self.judged["990006"] = {"close": 29000.0, "stop": 28000.0, "trend": True, "basis": "x"}
        res.day.set("settlement", {"account": {"990006": 28}, "avg_price": {"990006": 33000}, "d2": 1.0, "diff": {}})
        res.ac_positions()
        self.assertIn("990006", jl(self.R.POSITIONS)["next_open_sells"])


class ClosingAuction(unittest.TestCase):
    """3: 15:20 이전 감지한 손절은 조회가 늦어도 나가고, 15:20 ~ 15:29 동시호가에도 시장가로"""

    def om(self, t):
        self.clk = FakeClock(t)
        self.fk = FakeKiwoom(self.clk)
        return O.OrderManager(O.Broker(self.fk, allow_orders=True), clock=self.clk, sleep=self.clk.sleep)

    def test_3_detect_in_1520_minute_with_slow_quotes(self):
        om = self.om("2026-10-26 15:20:40")                                       # 15:20 분의 감시가 조회 지연으로 15:20:40
        r = om.stop_check({"990006": {"qty": 28, "sellable": 28, "cur_price": 29000.0}}, {"990006": 30000.0})
        self.assertEqual(len(sends(self.fk, "kt10001")), 1)
        self.assertEqual((r[0]["trde_tp"], r[0]["closing_auction"]), ("3", True))  # 마감 동시호가 시장가 (종가 체결)

    def test_3_triggered_before_1520_retried_in_auction_until_1529(self):
        om = self.om("2026-10-26 15:19:30")
        self.fk.script["kt10001"] = ["reject", "accept"]
        held, tr = {"990006": {"qty": 28, "sellable": 28, "cur_price": 29000.0}}, {}
        hold(self.fk, "990006", 28)
        om.stop_check(held, {"990006": 30000.0}, triggered=tr)
        self.clk.at("15:25:00")                                                   # 동시호가 중, 시세 감지는 안 함
        om.stop_check(acct_view(self.fk, held), {"990006": 30000.0}, triggered=tr, detect=False)
        self.assertEqual([i["status"] for i in om.records], [O.REJECTED, O.ACCEPTED])
        om2 = self.om("2026-10-26 15:29:00")
        om2.stop_check(held, {"990006": 30000.0}, triggered={"990006": {"since": "x", "price": 1.0, "stop": 2.0}})
        self.assertEqual(sends(self.fk, "kt10001"), [])                           # 15:29 부터는 안 보냄
        om3 = self.om("2026-10-26 15:21:00")
        om3.stop_check(held, {"990006": 30000.0})
        self.assertEqual(sends(self.fk, "kt10001"), [])                           # 15:21 뒤 새 감지 없음


class TickWindow(Base):
    def test_3_tick_detect_until_1520_send_until_1529(self):
        res = self.started("15:20:58", live=False)
        calls = []
        res.stop_minute = lambda detect=True: calls.append((self.clk().strftime("%H:%M:%S"), detect))
        for hms in ("15:20:58", "15:21:00", "15:28:59", "15:29:00"):
            self.clk.at(hms)
            res.tick(self.clk())
        self.assertEqual(calls, [("15:20:58", True), ("15:21:00", False), ("15:28:59", False)])


class Delays(unittest.TestCase):
    """4: 알림은 큐 (본체 대기 0), 429 재시도는 한 겹, 손절 감시 한 번의 대기 상한"""

    def test_4_notify_async_no_wait_in_order(self):
        got = []
        t0 = time.time()
        for i in range(3):
            N.send_async(f"m{i}", sender=lambda text: (time.sleep(1.0), got.append(text)))
        self.assertLess(time.time() - t0, 1.0)                                    # 바로 돌아옴 (보내기를 기다리면 3초 이상)
        self.assertTrue(N.flush(15))
        self.assertEqual(got, ["m0", "m1", "m2"])

    def test_4_caller_single_retry_layer_and_deadline(self):
        kws, codes = [], [429, 429, 200]

        class Client:
            def request(self, **kw):
                kws.append(kw)
                st = codes.pop(0) if codes else 429
                return SimpleNamespace(status_code=st, json=lambda: {"return_code": 5 if st == 429 else 0}, headers={})
        c = C.Caller.__new__(C.Caller)
        c.c, c.n, c.errors, c.hourly, c.fail_streak, c.stopped = Client(), 0, [], C.Counter(), 0, None
        saved = C.time.sleep
        C.time.sleep = lambda s: None
        try:
            j, _, _ = c.post("ka10095", "/x", {})
            self.assertTrue(all(k["retry_429"] is False for k in kws))           # 클라이언트 안 재시도 끔 = 한 겹
            self.assertEqual(len(kws), 3)
            kws.clear()
            with self.assertRaises(ValueError):
                c.post("ka10095", "/x", {}, deadline=time.time() + 1.0)           # 첫 백오프 2초가 상한을 넘음 -> 기다리지 않음
            self.assertEqual(len(kws), 1)
            self.assertIn("max_wait", kws[0])
        finally:
            C.time.sleep = saved

    def test_4_balance_quick_no_client_429_retry(self):
        kws = []
        client = SimpleNamespace(request=lambda **kw: kws.append(kw) or None)    # 429 -> None
        with self.assertRaises(O.NoResponse):
            O.Broker(client).balance(quick=True)
        self.assertIs(kws[0]["retry_429"], False)


class LimitCap(unittest.TestCase):
    def test_5_limit_is_min_of_1p05_and_upper(self):
        top = pd.DataFrame({"code": ["000010", "000020"], "score": [1.0, 0.9], "raw_close": [10000.0, 10000.0]})
        plan = O.p4_plan(top, "raw_close", 30_000_000, 30_000_000, set(), "본래", upper={"000010": 10300.0})
        self.assertEqual((plan[0]["limit_price"], plan[0]["limit_capped_at_upper"], plan[0]["limit_1p05"]), (10300, True, 10500))
        self.assertEqual(plan[1]["limit_price"], 10500)                            # 상한가를 모르면 1.05 그대로
        self.assertNotIn("limit_capped_at_upper", plan[1])


class ProdGuard(Base):
    def test_6_prod_no_check_mode_buy(self):
        res = self.started("15:22:00", live=False)
        res.day.set("live", {"plan": [dict(PLAN[0], kind="점검용")], "kind": "점검용"})
        self.R.CFG["env"] = "prod"
        res.buy()
        self.assertEqual(sends(self.fk), [])
        self.assertIn("실전 계좌", res.day.s["buy_decision"]["reason"])
        self.R.CFG["env"] = "mock"
        self.assertFalse(any("실전 계좌" in w for w in res.buy_gate()))           # 모의에서는 점검용 매수 함
        res.day.set("live", {"plan": [dict(PLAN[0])], "kind": "본래"})
        self.R.CFG["env"] = "prod"
        self.assertFalse(any("실전 계좌" in w for w in res.buy_gate()))           # 실전에서도 필터 통과일(본래)은 함


class ModelCheck(unittest.TestCase):
    """7: meta 의 월 == 요청 월, 시드 정확히 1 ~ 5"""

    def setUp(self):
        import lightgbm as lgb
        import model as M
        self.M, self.tmp = M, tempfile.mkdtemp()
        # 11J 보완 8: 외부 데이터셋 없이 (git clone 직후에도) — 시험 안에서 41개 특징 스키마를 만든다
        self.saved_ds = M.DATASET
        M.DATASET = os.path.join(self.tmp, "schema.parquet")
        names = sorted(M.m.s5.NON_FEATURES) + ["is_kospi"] + [f"f{i:02d}" for i in range(41)]
        pd.DataFrame({c: pd.Series([], dtype="float64") for c in names}).to_parquet(M.DATASET, index=False)
        feats = M.expected_features()
        assert len(feats) == 41
        rng = np.random.default_rng(0)
        X, y = rng.normal(size=(60, len(feats))).astype(np.float32), rng.normal(size=60)
        Xf = X[:3]
        files, preds = {}, {}
        for s in (1, 2, 3, 4, 5, 6):
            b = lgb.train({"objective": "regression", "verbose": -1, "seed": s, "num_leaves": 4}, lgb.Dataset(X, y), num_boost_round=3)
            p = os.path.join(self.tmp, f"seed{s}.txt")
            b.save_model(p)
            files[f"seed{s}.txt"] = M.sha256(p)
            preds[f"seed{s}"] = [float(v) for v in lgb.Booster(model_file=p).predict(Xf)]
        self.meta = {"month": "2026-10", "seeds": [1, 2, 3, 4, 5], "features": feats, "files": files,
                     "fixed_sample": {"X": Xf.tolist(), "pred": preds}, "retrain_day": "2026-10-01", "train_end": "2026-07-01"}

    def tearDown(self):
        self.M.DATASET = self.saved_ds
        shutil.rmtree(self.tmp, ignore_errors=True)

    def chk(self, **upd):
        with open(os.path.join(self.tmp, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({**self.meta, **upd}, f)
        return self.M.load("2026-10", model_dir=self.tmp)[3]

    def test_7_month_and_seeds(self):
        self.assertTrue(self.chk()["ok"])
        c = self.chk(month="2026-09")
        self.assertEqual((c["ok"], c["month_ok"]), (False, False))
        c = self.chk(seeds=[1, 2, 3, 4, 6])
        self.assertEqual((c["ok"], c["seeds_ok"]), (False, False))
        c = self.chk(seeds=[1, 2, 3, 4])
        self.assertEqual((c["ok"], c["seeds_ok"]), (False, False))


class ModelAlert(Base):
    def test_7_model_fail_blocks_buys_and_alerts(self):
        res = self.started("07:30:00", live=False)
        for name in ("finalize_recovery", "check_exclusions", "official_vs_1530"):
            setattr(res, name, lambda *a: None)
        pool = type("FakePool", (), {"close": lambda self: None})
        res.pool, res.prep_reason = None, "하루 시작"
        res.prep_result = {"pool": pool(), "boosters": None, "cols": None, "index": {}, "ledgers": {}, "frozen": {"ok": True}, "amt20": {}, "excluded": {},
                           "prep": {"model": {"check": {"ok": False, "month_ok": False, "meta_month": "2026-09"}}, "ledger_version": 0,
                                    "data": {"daily_up_to_date": True, "prev_trading_day": "2026-10-23", "data_version": "v"}}}
        res.apply_prep()
        self.assertFalse(res.ctx["buy_allowed"])
        self.assertTrue(any("[모델 확인 실패]" in a and "2026-09" in a for a in self.alerts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
