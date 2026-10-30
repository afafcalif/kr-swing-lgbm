"""
11Q 보완 (0c379e3 Codex 확인 [P2] 2건): 당일 실패율(5% 이상 = market_fail 차단)은 고유 종목 수로, 한 종목은 한 단계에서만 센다.
1 같은 종목이 missing_row 에 두 번(08시 공식 재반영에서도 누락) 남아도 한 종목 / 2 일봉 단계에서 센 종목의 스냅샷 데이터 이상은 스냅샷 단계에서 안 셈.
가짜 서버·가짜 시계, 실제 키움 호출·data·logs 없음.

  python -m unittest backtest_swing/live/test_failshare.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"
import json
import shutil
import sys
import tempfile
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_codex  # noqa: E402
from test_codex import Base  # noqa: E402
from test_orders import sends  # noqa: E402
import common as C  # noqa: E402
import engine as E  # noqa: E402
import quotes as Q  # noqa: E402

CODES40 = [f"9{i:05d}" for i in range(40)]
CODES100 = ["990006"] + [f"9{i:05d}" for i in range(99)]


class DailyStage(Base):
    """1 일봉 단계: missing_row 의 같은 종목 두 항목 = 한 종목"""

    def excluded(self, missing_row):
        import daily
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "update.json"), "w", encoding="utf-8") as f:
            json.dump({"missing_row": missing_row}, f)
        saved = daily.APPEND_DIR
        daily.APPEND_DIR = tmp
        try:
            return daily.excluded_codes()
        finally:
            daily.APPEND_DIR = saved

    def test_duplicate_missing_row_counts_once(self):
        res = self.started("07:30:00")
        # 07:00 갱신에서 10-29 행 누락 + 08시 재반영에서도 누락 -> update.json missing_row 에 같은 종목 두 항목 (daily.reapply_official)
        ex = self.excluded([["900001", ["2026-10-28"]], ["900001", ["2026-10-29"]]])
        self.assertEqual(ex["missing_row"], ["900001", "900001"])
        res.ctx.update(excluded=ex, amt20={c: 1e9 for c in CODES40})
        r = res.check_exclusions()
        self.assertEqual((r["failures"], r["total"]), (1, 40))                                      # 예전: 2 / 40 = 5% -> 차단
        self.assertFalse(r["blocked"])
        self.assertFalse(res.day.s.get("buy_block"))

    def test_two_distinct_codes_still_block(self):
        res = self.started("07:30:00")
        ex = self.excluded([["900001", ["2026-10-29"]], ["900002", ["2026-10-29"]], ["900002", ["2026-10-28"]]])
        res.ctx.update(excluded=ex, amt20={c: 1e9 for c in CODES40})
        r = res.check_exclusions()
        self.assertEqual((r["failures"], r["total"]), (2, 40))                                      # 진짜 2 / 40 = 5% -> 차단
        self.assertTrue(r["blocked"])
        self.assertIn("market_fail:일봉 수집 실패", res.day.s["buy_block"])


class SnapshotStage(Base):
    """2 스냅샷 단계: 데이터 이상은 적재된 종목만 (일봉 단계에서 센 종목은 빼고)"""

    def plan_buy(self, daily_fail, anomaly):
        """100종목. daily_fail = 일봉 수집 실패(fetch_error, 거래대금은 파일 기준으로 amt20 에 있음), anomaly = 스냅샷 기준가 0 (데이터 이상)"""
        res = test_codex.Codex.snapshot_ready(self)
        snap = pd.DataFrame({"code": CODES100, "open": 33000.0, "high": 33500.0, "low": 32500.0, "close": 33000.0, "volume": 1e5, "amount": 3.3e9,
                             "dt": "20261026"})
        raw = pd.DataFrame({"stk_cd": CODES100, "upl_pric": "+42900", "lst_pric": "-23100",
                            "base_pric": ["0" if c in anomaly else "33000" for c in CODES100]})
        Q.snapshot = lambda caller, cs, **k: (raw, snap, {"returned": len(CODES100), "codes": len(CODES100), "seconds": 1.0})
        C.sd.universe_codes = lambda: list(CODES100)
        res.ctx.update(amt20={c: 1e9 for c in CODES100}, excluded={"fetch_error": list(daily_fail), "missing_row": []})
        top = pd.DataFrame({"code": ["990006"], "score": [0.9], "raw_close": [33000.0]})
        out = {"filter": {"filter_ok": True, "prev_kospi_close": 2700.0, "prev_kospi_ma120": 2500.0}, "eligible": pd.DataFrame({"code": ["990006"], "score": [0.9]}),
               "market": {}, "errors": [], "ledgers": {n: {"top3": top, "busy": [], "skipped_busy": [], "ledger_status": []} for n in ("strategy", "full")}}
        saved = E.run_day
        try:
            E.run_day = lambda *a, **k: out
            res.snapshot_and_plan()
        finally:
            E.run_day = saved
        self.clk.at("15:22:00")
        res.buy()
        return res, sends(self.fk)

    def snap_fail(self, res):
        return [x for x in res.day.s["same_day_failures"] if x["what"] == "스냅샷·계산 실패"][-1]

    def test_daily_failed_anomaly_not_counted_again(self):
        # 일봉 실패 3종목(기준가 이상도 있음) + 정상 적재 종목 스냅샷 이상 2종목 -> 스냅샷 단계 2 / 100 (예전: 5 / 100 = 5% -> 차단)
        fails = ["900001", "900002", "900003"]
        res, sent = self.plan_buy(fails, fails + ["900010", "900011"])
        f = self.snap_fail(res)
        self.assertEqual(sorted(x["code"] for x in f["all"]), ["900010", "900011"])
        self.assertEqual((f["total"], f["share"]), (100, 0.02))
        self.assertNotIn("market_fail:스냅샷·계산 실패", res.day.s.get("buy_block") or [])
        self.assertEqual([s[2]["stk_cd"] for s in sent], ["990006"])

    def test_loaded_anomalies_5pct_still_block(self):
        res, sent = self.plan_buy(["900001"], ["900001"] + [f"9000{i}" for i in range(10, 15)])   # 적재된 종목 이상 5 / 100 = 5%
        f = self.snap_fail(res)
        self.assertEqual(len(f["all"]), 5)
        self.assertIn("market_fail:스냅샷·계산 실패", res.day.s["buy_block"])
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
