"""
Codex 확인(d8cb441..aba7392) 반영 + 서브 PC update 실패 원인 (11K 보완 2, 2026-10-27) 재현 시험. 가짜 서버·가짜 시계, 실제 주문·조회·logs/exec·data 없음.

  python -m unittest backtest_swing/live/test_broad8.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import bal_row, jl  # noqa: E402
from test_orders import sends, wait_for  # noqa: E402
from test_broad6 import HELD, Base6, pos_with  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import settings  # noqa: E402
import state as S  # noqa: E402


def synced_prev(test, held_base, intents=None, start=None):
    """10-23: 15:40 정산·보유 갱신은 돌았고(장부에 계좌 수량 반영) 정산 마무리(after_close)는 못 한 날"""
    d = test.prev_state(steps=("snapshot", "settle"), intents=intents,
                        settlement={"d2": 30_000_000.0, "account": {c: int(h["qty"]) for c, h in held_base.items()}})
    d.set("positions_baseline", {"held": held_base, "trade_seq": 1, "time": "15:40:10"})
    if start is not None:
        d.set("positions_start", {"time": "07:00:00", "held": start})
    return d


# 1 분할 감지(장부 수량 기준) 시험은 11K 보완 3 (자동 조정 제거) 으로 test_broad9.QtyReview 에


# ---------------------------------------------------------------- 2·3 공식 판단 따로 먼저 공개, 기다림 한도 08:50

class JudgeFirst(Base6):
    def trend_pos(self):
        P.save(self.R.POSITIONS, pos_with())                                       # 15:30 1분봉 추세 거짓
        self.fk.balance_rows = [bal_row("084670", 28)]

    def test_judge_published_before_slow_model_load(self):
        import run_day as RD
        self.trend_pos()
        res = self.started("08:25:00", live=False)
        self.judged["084670"] = {"close": 31000.0, "stop": 30000.0, "trend": True, "basis": "수정주가 일봉"}
        gate, saved = threading.Event(), RD.prepare

        def slow(day):
            gate.wait(10)
            raise FileNotFoundError("models_live/stage5/2026-10/meta.json")
        RD.prepare = slow
        try:
            res.prep_thread = threading.Thread(target=res._prep_work, args=(res.day.date, False), daemon=True)
            res.prep_thread.start()
            self.assertTrue(wait_for(lambda: res.judge_result is not None))       # 준비 스레드가 공식 판단을 먼저 공개할 때까지 (최대 5초)
            self.assertIsNone(res.prep_result)                                      # 모델 적재는 아직 (막혀 있음)
            res.judge = lambda *a, **k: (_ for _ in ()).throw(AssertionError("주 스레드에서 계산"))
            res.tick(self.clk())
            p = jl(self.R.POSITIONS)
            self.assertEqual(p["sell_duty"]["084670"]["kind"], "trend")              # 공식 참 -> 08:30 전에 적용
            self.clk.at("08:30:00")
            res.tick(self.clk())
            self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["084670"])   # 기다리지 않고 바로
        finally:
            gate.set()
            res.prep_thread.join(5)
            RD.prepare = saved
        self.assertIn("FileNotFoundError", res.prep_result["error"])

    def test_wait_limit_0850(self):
        duty = {"084670": O.new_duty("trend", "2026-10-23 15:41:00", "추세 매도 (2026-10-23 종가 < 20일선, 15:30 1분봉)")}
        P.save(self.R.POSITIONS, pos_with(dict(HELD, trend_sell_next_open=True), duty, {"084670": {"since": "2026-10-23", "attempts": 0}}))
        self.fk.balance_rows = [bal_row("084670", 28)]
        res = self.started("08:49:30", live=False)
        self.assertEqual(res.tick(self.clk()), "trend_wait")
        self.assertEqual(res.day.s["trend_wait"]["until"], "08:50:00")
        self.clk.at("08:50:00")
        res.tick(self.clk())
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["084670"])


# ---------------------------------------------------------------- 4 점검 모드 종료일: 체결 시각 기준

class CheckModeFillTime(Base6):
    def sell(self, res, created, fill_time, px):
        it = res.om.store.add(date="2026-10-26", stage="stop", side="sell", code="084670", qty=10, price=None, trde_tp="3", attempt=1,
                              kind="점검용", reason="손절", assumed_kind="stop")
        res.om.store.update(it, status=O.ACCEPTED, filled_qty=10, fill_price=px, created=created, fill_time=fill_time)

    def test_split_by_fill_time(self):
        P.save(self.R.POSITIONS, {"held": {}, "sell_duty": {}, "next_open_sells": {}, "trade_seq": 1, "duty_format": P.DUTY_FORMAT})
        res = self.started("15:19:00", live=False)
        res.end_check_mode(30_000_000.0, {}, {})
        self.sell(res, "15:10:00.000", "151005", 1200.0)                           # 종료 전 체결 = 시작 금액에 이미 들어 있음
        self.sell(res, "15:15:00.000", "153012", 1100.0)                           # 종료 전 주문이지만 15:30 체결 (동시호가) = 종료 뒤
        perf = res.strategy_perf(30_000_000.0, {})
        self.assertAlmostEqual(perf["check_sold_total"], 11000 * (1 - 0.0035 - self.R.sb_tax(2026)), places=0)
        self.assertEqual((self.R.hhmmss("15:30:12.123"), self.R.hhmmss("20261026153012"), self.R.hhmmss(None)), ("153012", "153012", "000000"))


# ---------------------------------------------------------------- 6 ~ 8 설정 병합·시각 이동·시험 격리

class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ex = json.load(open(settings.EXAMPLE, encoding="utf-8"))

    def stale(self, drop=("stop_send_until",), top_drop=("commission",)):
        """서브 PC 처럼 예전에 복사한 config.json: times 키 몇 개·새 최상위 키가 없음, 경로는 PC 값"""
        c = json.loads(json.dumps(self.ex))
        for k in drop:
            c["times"].pop(k)
        for k in top_drop:
            c.pop(k, None)
        c["root"], c["orders_enabled"] = "D:/kr-swing-lgbm", True
        p = os.path.join(self.tmp, "config.json")
        json.dump(c, open(p, "w", encoding="utf-8"))
        return p

    def test_deep_merge_fills_missing_keys(self):
        cfg = settings.load(path=self.stale(drop=("stop_send_until", "measure_snapshot")))
        self.assertEqual(sorted(cfg["_filled"]), ["commission", "times.measure_snapshot", "times.stop_send_until"])
        self.assertEqual((cfg["times"]["stop_send_until"], cfg["root"], cfg["orders_enabled"]), ("15:29:00", "D:/kr-swing-lgbm", True))   # PC 값은 그대로
        self.assertEqual(cfg["commission"]["mock"], 0.0035)
        self.assertTrue(cfg["state_dir"].startswith("D:/kr-swing-lgbm"))

    def test_stale_config_csat_shift_all_time_keys(self):
        cfg = settings.load(path=self.stale())
        h = S.day_hours("2026-11-19")
        t = S.shift_times(cfg["times"], h)
        keys = [k for k, v in self.ex["times"].items() if isinstance(v, str) and len(v) == 8 and v[2] == ":"]
        self.assertIn("stop_send_until", keys)
        for k in keys:                                                              # 모든 시각 키가 이동 (07:00 하루 시작만 그대로)
            want = self.ex["times"][k] if k == "day_start" else (pd.Timestamp(f"2000-01-01 {self.ex['times'][k]}") + pd.Timedelta(hours=1)).strftime("%H:%M:%S")
            self.assertEqual(t[k], want, k)
        self.assertEqual(t["stop_send_until"], "16:29:00")                         # 예전: 키가 없어 15:29 그대로

    def test_resident_timetable_shifts_every_key(self):
        import resident as R
        self.assertEqual(R.CFG["_source"], "config.example.json (LIVE_CONFIG=example)")
        R.apply_hours(pd.Timestamp("2026-11-19"))
        try:
            for k, v in R.BASE_T.items():
                if isinstance(v, str) and len(v) == 8 and v[2] == ":" and k != "day_start":
                    self.assertNotEqual(R.T[k], v, k)
            self.assertEqual((R.T["stop_send_until"], R.T["close_calc"], R.T["close_calc_until"]), ("16:29:00", "16:32:00", "17:20:00"))
        finally:
            R.apply_hours(pd.Timestamp("2026-11-20"))

    def test_tests_ignore_local_config(self):
        saved = settings.LOCAL
        settings.LOCAL = self.stale()
        try:
            self.assertEqual(settings.load()["_source"], "config.example.json (LIVE_CONFIG=example)")   # 시험 환경 변수 = 로컬 파일 안 읽음
            os.environ.pop("LIVE_CONFIG")
            cfg = settings.load()
            self.assertEqual((cfg["_source"], cfg["root"]), ("config.json", "D:/kr-swing-lgbm"))   # 실제 실행은 로컬 파일 + 기본값
            self.assertIn("times.stop_send_until", cfg["_filled"])
        finally:
            os.environ["LIVE_CONFIG"] = "example"
            settings.LOCAL = saved


if __name__ == "__main__":
    unittest.main()
