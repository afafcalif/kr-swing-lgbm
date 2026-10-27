"""
Codex 확인(8cb95f6..d8cb441) 반영 (11K 보완 1 ~ 5, 2026-10-27) 재현 시험. 가짜 서버·가짜 시계·가짜 학습 프로세스, 실제 주문·조회·학습·logs/exec·data 없음.

  python -m unittest backtest_swing/live/test_broad7.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import os
import sys
import time
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import PLAN, bal_row, jl  # noqa: E402
from test_orders import sends  # noqa: E402
from test_broad6 import HELD, Base6, pos_with  # noqa: E402
import common as C  # noqa: E402
import engine as E  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import quotes as Q  # noqa: E402


class FakeProc:
    n = 0

    def __init__(self):
        FakeProc.n += 1
        self.pid, self.done, self.killed = 90000 + FakeProc.n, False, False

    def poll(self):
        return 0 if self.done else None

    def kill(self):
        self.killed = self.done = True


class Catch(Base6):
    """보완 1 공통: 가짜 학습 프로세스, 설치된 모델 = self.models, 결과 파일 폴더 = 임시"""

    def setUp(self):
        super().setUp()
        import monthly as MO
        self.MO, self.models, self.procs, self.killed = MO, set(), [], []
        saved = MO.OUT_DIR
        MO.OUT_DIR = os.path.join(self.tmp, "monthly")
        self.addCleanup(setattr, MO, "OUT_DIR", saved)

    def arm(self, res):
        res.auto_monthly = True
        res.spawn_monthly = lambda month, log: self.procs.append(FakeProc()) or self.procs[-1]
        res.model_ready = lambda month: month in self.models and month not in res.model_bad
        res.kill_monthly = lambda pid: self.killed.append(pid)
        return res

    def finish(self, month="2026-11", ok=True, started="2026-01-01 00:00:00"):
        r = {"month": month, "ok": ok, "started": started, "finished": "x", "seconds": 300.0, "plan": self.MO.plan(month), "data_last": "2026-10-29",
             "steps": {"train": {"train_rows": 457258}, "check": {"ok": ok}, "universe": {"new_n": 2763, "removed": 11, "added": 2}},
             **({} if ok else {"error": "학습 실패"})}
        r["started"] = max(started, jl(os.path.join(self.tmp, f"monthly_{month}.json"))["started_full"])
        os.makedirs(self.MO.OUT_DIR, exist_ok=True)
        C.atomic_json(os.path.join(self.MO.OUT_DIR, f"{month}.json"), r)
        if ok:
            self.models.add(month)
        self.procs[-1].done = True

    def later(self, sec=31):
        self.clk.t += pd.Timedelta(seconds=sec)
        return self.clk()


class MonthlyCatchup(Catch):
    def test_restart_1700_last_day_trains_without_day_state(self):
        self.clk.t = pd.Timestamp("2026-10-30 17:00:00")
        res = self.arm(self.resident())                                              # 재시작 직후: 하루 상태 없음
        self.assertEqual(res.tick(self.clk()), "idle")
        self.assertIsNone(res.day)
        self.assertEqual(len(self.procs), 1)
        st = jl(os.path.join(self.tmp, "monthly_2026-11.json"))
        self.assertEqual((st["kind"], st["attempts_evening"]), ("evening", 1))
        res.tick(self.later())
        self.assertEqual(len(self.procs), 1)                                         # 도는 동안 기다림
        res2 = self.arm(self.resident())                                             # 또 재시작: 살아 있는 pid 를 기다림
        saved = self.R.pid_alive
        self.R.pid_alive = lambda pid: True
        try:
            res2.tick(self.later())
        finally:
            self.R.pid_alive = saved
        self.assertEqual(len(self.procs), 1)
        self.finish()
        res.tick(self.later())
        self.assertTrue(any(a.startswith("[월별 모델 설치] 2026-11") for a in self.alerts))
        self.assertTrue(jl(os.path.join(self.tmp, "monthly_2026-11.json"))["ok"])
        self.clk.t = pd.Timestamp("2026-10-31 11:00:00")                             # 토요일: 이미 있으면 안 돌림
        res.tick(self.later())
        self.assertEqual(len(self.procs), 1)

    def test_evening_two_failures_then_morning_retry(self):
        self.clk.t = pd.Timestamp("2026-10-30 16:31:00")
        res = self.arm(self.resident())
        res.tick(self.clk())
        self.finish(ok=False)
        res.tick(self.later())                                                       # 실패 알림 -> 다음 확인에서 다시
        res.tick(self.later())
        self.assertEqual(len(self.procs), 2)
        self.finish(ok=False)
        res.tick(self.later())
        res.tick(self.later())
        self.assertEqual(len(self.procs), 2)
        self.assertTrue(any("evening 2번 시도 모두 실패" in a for a in self.alerts))
        self.clk.t = pd.Timestamp("2026-11-01 12:00:00")                             # 일요일: 저녁 시간창이지만 두 번 다 씀
        res.tick(self.later())
        self.assertEqual(len(self.procs), 2)

    def test_1102_0700_missing_model_trains_then_buys(self):
        self.clk.t = pd.Timestamp("2026-11-02 07:00:00")
        res = self.arm(self.started("07:00:00"))
        res.tick(self.clk())
        self.assertEqual(len(self.procs), 1)
        self.assertEqual(jl(os.path.join(self.tmp, "monthly_2026-11.json"))["kind"], "morning")
        self.assertTrue(any(a.startswith("[월별 모델 따라잡기] 2026-11") for a in self.alerts))
        self.clk.at("08:40:00")
        self.finish()
        res.tick(self.later())
        self.assertFalse(res.prep_ok)                                                # 새 모델로 다시 적재 요청
        self.assertIn("reload", res.day.s["monthly_model"])
        res.prep_ok = True                                                           # (준비 스레드가 새 모델로 적재했다고 침)
        self.clk.at("15:00:30")
        res.tick(self.clk())
        self.assertNotIn("monthly_model_missing", res.day.s.get("buy_block") or [])
        self.clk.at("15:22:00")
        res.buy()
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk)], [PLAN[0]["code"]])

    def test_training_past_0900_stop_monitor_runs(self):
        P.save(self.R.POSITIONS, pos_with())
        self.fk.balance_rows = [bal_row("084670", 28)]
        self.clk.t = pd.Timestamp("2026-11-02 07:00:00")
        res = self.arm(self.started("07:00:00", live=False))
        res.tick(self.clk())
        self.assertEqual(len(self.procs), 1)
        self.quotes["084670"] = 29000.0                                              # 손절가 30000 아래
        self.clk.at("09:00:00")
        t0 = time.time()
        res.tick(self.clk())
        self.assertLess(time.time() - t0, 2.0)
        self.assertFalse(self.procs[0].done)                                         # 학습은 계속 (끄지 않음)
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["084670"])

    def test_1500_deadline_kills_and_blocks_buys(self):
        self.clk.t = pd.Timestamp("2026-11-02 07:00:00")
        res = self.arm(self.started("07:00:00"))
        res.tick(self.clk())
        self.clk.at("15:00:05")
        res.tick(self.clk())
        self.assertEqual(self.killed, [self.procs[0].pid])
        self.assertIn("monthly_model_missing", res.day.s["buy_block"])
        self.assertTrue(any(a.startswith("[월별 모델 없음] 2026-11") and "학습 중단" in a for a in self.alerts))
        self.clk.at("15:22:00")
        res.buy()
        self.assertEqual(sends(self.fk), [])

    def test_check_failed_model_is_retrained_in_morning(self):
        self.models.add("2026-11")
        self.clk.t = pd.Timestamp("2026-11-02 07:05:00")
        res = self.arm(self.started("07:05:00"))
        res.tick(self.clk())
        self.assertEqual(self.procs, [])                                             # 있으면 안 돌림
        res.model_bad.add("2026-11")                                                 # 하루 시작 검사 실패 (apply_prep)
        res.tick(self.later())
        self.assertEqual(len(self.procs), 1)


# ---------------------------------------------------------------- 2 시장 필터 입력

def index_series(n=300, last=None, zero=False):
    days = pd.bdate_range("2025-08-01", periods=n)
    k = pd.Series(np.linspace(2500, 2700, n), index=days)
    q = pd.Series(np.linspace(800, 850, n), index=days)
    if last is not None:
        k.iloc[-1] = last
    if zero:
        k.iloc[-5] = 0.0
    return {"kospi": k, "kosdaq": q}, days[-1] + pd.Timedelta(days=3)


class FilterInput(Base6):
    def test_filter_invalid_rules(self):
        self.assertIsNone(E.filter_invalid(2700.0, 2600.0))
        for c, m in ((np.nan, 2600.0), (2700.0, np.nan), (0.0, 2600.0), (2700.0, 0.0), (None, 1.0), (27.0, 2600.0)):
            self.assertIsNotNone(E.filter_invalid(c, m), (c, m))
        self.assertIsNotNone(E.filter_invalid(2700.0, 2600.0, n_ok=119))

    def test_market_row_nan_and_zero_not_pass(self):
        ix, day = index_series()
        _, f = E.market_row(ix, day, {"kospi": 2700.0, "kosdaq": 850.0})
        self.assertEqual((f["filter_ok"], f["filter_valid"]), (True, True))
        ix, day = index_series(last=np.nan)
        _, f = E.market_row(ix, day, {"kospi": 2700.0, "kosdaq": 850.0})
        self.assertEqual((f["filter_ok"], f["filter_valid"]), (False, False))         # 예전: not (NaN < NaN) = True
        ix, day = index_series(zero=True)
        _, f = E.market_row(ix, day, {"kospi": 2700.0, "kosdaq": 850.0})
        self.assertFalse(f["filter_ok"])
        self.assertFalse(self.R.filter_valid({"filter_ok": True, "prev_kospi_close": float("nan"), "prev_kospi_ma120": float("nan")}))  # 예전 기록

    def test_snapshot_with_nan_filter_blocks_buys_and_no_check_mode_end(self):
        R = self.R
        res = self.started("15:18:40", live=False)
        codes = ["084670"]
        raw = pd.DataFrame({"stk_cd": codes, "upl_pric": ["+42900"], "base_pric": ["33000"], "lst_pric": ["23100"]})
        snap = pd.DataFrame({"code": codes, "open": [33000.0], "high": [33500.0], "low": [32800.0], "close": [33100.0], "volume": [1e5],
                             "amount": [3.3e9], "dt": ["20261026"], "cntr_tm": ["151830"]})
        Q.snapshot = lambda caller, cs, **k: (raw, snap, {"returned": 1, "codes": 1, "requests": 1, "seconds": 0.1, "missing": []})
        Q.index_now = lambda caller: ({"kospi": 2700.0, "kosdaq": 850.0}, {})
        top = pd.DataFrame({"code": codes, "score": [0.5], "raw_close": [33100.0]})
        fake = {"filter": {"filter_ok": True, "prev_kospi_close": float("nan"), "prev_kospi_ma120": float("nan")},  # NaN 인데 통과로 온 경우
                "eligible": pd.DataFrame({"code": codes, "score": [0.5]}), "market": {}, "errors": [],
                "ledgers": {n: {"top3": top, "busy": [], "skipped_busy": [], "ledger_status": []} for n in ("strategy", "full")}}
        saved = (E.run_day, R.sd.universe_codes)
        E.run_day = lambda *a, **k: fake
        R.sd.universe_codes = lambda: codes
        try:
            res.pool = object()
            res.ctx.update(index={}, boosters=None, cols=None, ledgers={"strategy": [], "full": []}, prep={})
            res.snapshot_and_plan()
        finally:
            E.run_day, R.sd.universe_codes = saved
        live = res.day.s["live"]
        self.assertEqual((live["filter"]["filter_ok"], live["kind"]), (False, "점검용"))
        self.assertIn("filter_invalid", res.day.s["buy_block"])
        self.assertTrue(any(a.startswith("[시장 필터 입력 비정상]") for a in self.alerts))
        self.assertFalse(os.path.exists(R.check_mode_path()))                       # 점검 모드 종료 안 함
        self.clk.at("15:22:00")
        res.buy()
        self.assertEqual(sends(self.fk), [])


# ---------------------------------------------------------------- 3 공식 일봉 재판단 (모델과 분리)

class RejudgeWithoutModel(Base6):
    def trend_pos(self):
        duty = {"084670": O.new_duty("trend", "2026-10-23 15:41:00", "추세 매도 (2026-10-23 종가 < 20일선, 15:30 1분봉)")}
        P.save(self.R.POSITIONS, pos_with(dict(HELD, trend_sell_next_open=True), duty, {"084670": {"since": "2026-10-23", "attempts": 0}}))
        self.fk.balance_rows = [bal_row("084670", 28)]

    def test_prep_work_judges_before_model_load(self):
        import run_day as RD
        self.trend_pos()
        res = self.started("08:10:00", live=False)
        self.judged["084670"] = {"close": 34000.0, "stop": 30000.0, "trend": False, "basis": "수정주가 일봉"}
        saved = RD.prepare
        RD.prepare = lambda day: (_ for _ in ()).throw(FileNotFoundError("models_live/stage5/2026-10/meta.json"))
        try:
            res._prep_work(res.day.date, False)
        finally:
            RD.prepare = saved
        out = res.prep_result
        self.assertIn("FileNotFoundError", out["error"])
        self.assertFalse(out["official_judge"]["084670"]["trend"])                   # 모델이 없어도 공식 판단은 계산됨

    def test_model_load_failure_still_releases_trend_duty_before_0830(self):
        self.trend_pos()
        res = self.started("08:20:00", live=False)
        res.prep_thread, res.prep_reason = object(), "하루 시작"
        res.prep_result = {"error": "FileNotFoundError('meta.json')", "recovery": None,
                           "official_judge": {"084670": {"close": 34000.0, "stop": 30000.0, "trend": False, "basis": "수정주가 일봉"}, "_missing": []}}
        res.judge_result = res.prep_result["judge"] = {"day": res.day.date, "recovery": None, "official_judge": res.prep_result["official_judge"]}
        res.tick(self.clk())
        p = jl(self.R.POSITIONS)
        self.assertEqual((p["sell_duty"], p["next_open_sells"]), ({}, {}))           # 공식 거짓 -> 추세 의무 해제
        self.clk.at("08:30:00")
        res.tick(self.clk())
        self.assertEqual(sends(self.fk, "kt10001"), [])

    def test_0830_waits_for_official_then_falls_back_at_0850(self):
        self.trend_pos()
        res = self.started("08:30:00", live=False)
        self.assertEqual(res.tick(self.clk()), "trend_wait")
        self.assertEqual(sends(self.fk, "kt10001"), [])
        self.clk.at("08:45:00")
        res.tick(self.clk())
        self.assertEqual(sends(self.fk, "kt10001"), [])
        self.clk.at("08:50:00")
        res.tick(self.clk())
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["084670"])   # 전날 판단으로 보냄
        self.assertTrue(res.day.s["trend_sell_without_official"]["codes"])
        self.assertTrue(any(a.startswith("[추세 매도 공식 판단 없음]") for a in self.alerts))

    def test_0830_sends_after_official_applied(self):
        self.trend_pos()
        res = self.started("08:30:00", live=False)
        res.tick(self.clk())
        self.judged["084670"] = {"close": 31000.0, "stop": 30000.0, "trend": True, "basis": "수정주가 일봉"}
        res.official_vs_1530()                                                       # 08:40 공식 판단 (참, 같음)
        self.clk.at("08:40:00")
        res.tick(self.clk())
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["084670"])
        self.assertNotIn("trend_sell_without_official", res.day.s)


# 4 아침 복구 분할 감지 시험은 11K 보완 3 (자동 조정 제거) 으로 test_broad9.QtyReview 에


# ---------------------------------------------------------------- 5 점검 모드 종료일 시작 금액

class CheckModeStart(Base6):
    def sell(self, res, created, px):
        it = res.om.store.add(date="2026-10-26", stage="stop", side="sell", code="084670", qty=10, price=None, trde_tp="3", attempt=1,
                              kind="점검용", reason="손절", assumed_kind="stop")
        res.om.store.update(it, status=O.ACCEPTED, filled_qty=10, fill_price=px, created=created)

    def test_sell_before_end_not_subtracted_again(self):
        P.save(self.R.POSITIONS, pos_with(dict(HELD, kind="점검용", qty=10)))
        res = self.started("10:00:00", live=False)
        self.sell(res, "10:00:00.000", 1200.0)                                       # 종료 전 점검용 매도 (대금은 15:19 D+2 에 이미 들어 있음)
        P.save(self.R.POSITIONS, {"held": {}, "sell_duty": {}, "next_open_sells": {}, "trade_seq": 1, "duty_format": P.DUTY_FORMAT})
        self.clk.at("15:19:00")
        res.end_check_mode(30_000_000.0, {}, {})                                    # 점검용 보유 없음 = 평가 0
        perf = res.strategy_perf(30_000_000.0, {})
        self.assertEqual((perf["check_sold_total"], perf["strategy_equity"]), (0.0, 30_000_000.0))   # 예전: 매도 대금을 또 뺌
        self.sell(res, "15:25:00.000", 1100.0)                                       # 종료 뒤 매도는 분리
        perf = res.strategy_perf(30_011_000.0, {})
        self.assertAlmostEqual(perf["check_sold_total"], 11000 * (1 - 0.0035 - self.R.sb_tax(2026)), places=0)


if __name__ == "__main__":
    unittest.main()
