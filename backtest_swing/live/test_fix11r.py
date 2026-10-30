"""
11R 보완 시험 (c27861e Codex 확인 [P1] 2건 + 기준가 재확인):
1 rebase 로 돌아온 종목(117670, 10-21 분할 k 0.5)이 다음 갱신에서 수집 실패 -> 공식 값 채움 때 이전 계수 승계 (아니면 기본 파일 13,400 + 새 기준 추가분)
2 이벤트 다음 날 임시 손절가 복구 = 진입일 원주가 손절가 x 그 뒤 이벤트 누적 비율 (예전: 오늘 k 만 -> 28,443 이 42,665 로 풀려 30,000 에 매도)
3 08:30 장 전 기준가가 옛 값(이벤트 없음)이면 09:00 이후 첫 손절 감시 시세로 한 번 더 확인
가짜 키움 일봉 서버·가짜 공식 시세·가짜 시계, 실제 키움 호출·data·logs 없음.

  python -m unittest backtest_swing/live/test_fix11r.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"
import json
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_rebase as TR  # noqa: E402
from test_codex import bal_row  # noqa: E402
from test_orders import sends  # noqa: E402
import common as C  # noqa: E402
import positions as P  # noqa: E402
import quotes as Q  # noqa: E402

D26 = pd.Timestamp("2026-10-26")
BASE_CLOSE_1016 = 13400.0          # 117670 기본 파일 10-16 원주가 종가 (10-21 분할 k 0.5 -> 새 기준 6,700)


class Inherit(TR.Env):
    """10-23 갱신에서 117670 rebased (k 0.5) -> 다음 갱신(10-26 추가)에서 117670 수집 실패 -> 이전 버전 행 + 공식 값으로 채움"""

    def next_day(self, official_1026=True):
        add = self.off[self.off.date == pd.Timestamp("2026-10-23")].assign(date=D26, chg=0.0)
        self.off = pd.concat([self.off, add], ignore_index=True) if official_1026 else self.off
        self.fk.off = pd.concat([self.fk.off, add], ignore_index=True)
        self.fk.days = list(self.fk.days) + [D26]
        if official_1026:
            pd.DataFrame({"x": [1]}).to_parquet(os.path.join(self.cache, "20261026.parquet"))
        self.fk.tweak[("117670", "1")] = lambda df: df.iloc[0:0]                                 # 끝내 빈 응답
        return add

    def check_store(self):
        st = self.daily.Store(["117670"])
        a = st.adj["117670"].set_index("timestamp")
        self.assertEqual(float(a.close[pd.Timestamp("2026-10-16")]), BASE_CLOSE_1016 * 0.5)      # 6,700 (예전: 13,400)
        ka, _ = TR.kiwoom_new("117670", TR.krx())
        m = st.adj["117670"].merge(ka, on="timestamp", suffixes=("", "_kw"))
        self.assertTrue(((m.open - m.open_kw).abs() <= 0.5).all())                               # 키움 새 이력과 이어짐
        self.assertEqual(a.index.max(), D26)
        c = a.close
        r20 = c.iat[-1] / c.iat[-21] - 1
        self.assertGreater(r20, -0.2)                                                             # 예전: 기본 파일 13,400 기준 -43% 로 보임
        return st

    def test_fill_inherits_k_0700(self):
        self.daily.update()
        self.assertAlmostEqual(self.u()["rebased"]["117670"]["k"], 0.5)
        self.next_day()
        self.daily.update()
        u = self.u()
        self.assertNotIn("117670", [c for c, _ in u["fetch_error"]])
        self.assertEqual(u["official_filled"]["117670"]["dates"], ["2026-10-26"])
        self.assertAlmostEqual(u["rebased"]["117670"]["k"], 0.5)                                 # 이전 계수 승계 (예전: rebased 에 없음)
        self.assertIn("inherited", u["rebased"]["117670"])
        self.check_store()

    def test_fill_inherits_k_on_reapply(self):
        """07:00 에 10-26 공식 시세 없음 -> carry 행·rebase 기록 함께 보관 -> 08:0x 재반영 때 승계해 채움"""
        self.daily.update()
        self.next_day(official_1026=False)
        self.daily.update()
        u = self.u()
        self.assertIn("117670", [c for c, _ in u["fetch_error"]])
        self.assertAlmostEqual(u["carry_rebased"]["117670"]["k"], 0.5)
        meta = json.load(open(os.path.join(self.ad, self.daily.CARRY_META), encoding="utf-8"))
        self.assertAlmostEqual(meta["117670"]["k"], 0.5)
        add = self.off[self.off.date == pd.Timestamp("2026-10-23")].assign(date=D26, chg=0.0)
        self.off = pd.concat([self.off, add], ignore_index=True)
        pd.DataFrame({"x": [1]}).to_parquet(os.path.join(self.cache, "20261026.parquet"))
        r = self.daily.reapply_official()
        self.assertEqual(r["filled"], ["117670"])
        self.assertAlmostEqual(self.u()["rebased"]["117670"]["k"], 0.5)
        self.check_store()

    def test_no_record_or_wrong_k_excluded(self):
        """rebase 기록이 없거나(잃어버림) carry 행 기준과 다르면 승계 확인 못 함 -> 그 종목만 제외 (이유 기록)"""
        self.daily.update()
        p = C.data_file("update.json", self.ad)
        u = json.load(open(p, encoding="utf-8"))
        u["rebased"].pop("117670")
        u["rebased"]["009140"]["k"] = 0.4                                                        # 009140 (k 0.5) 기록이 틀림
        json.dump(u, open(p, "w", encoding="utf-8"))
        self.next_day()
        self.fk.tweak[("009140", "1")] = lambda df: df.iloc[0:0]
        self.daily.update()
        u = self.u()
        self.assertEqual(sorted(c for c, _ in u["fetch_error"]), ["009140", "117670"])
        self.assertIn("이전 계수 기록 없음", u["fill_unfilled"]["117670"])
        self.assertIn("승계 확인 못 함", u["fill_unfilled"]["009140"])
        self.assertEqual(self.daily.excluded_codes()["fetch_error"], ["009140", "117670"])
        self.assertNotIn("117670", self.daily.Store(["117670"]).adj)


HELD = {"qty": 28, "trade_id": "990006-2026-10-22-1", "entry_date": "2026-10-22", "kind": "본래", "judged_on": "2026-10-23"}


class Stops(TR.Base):
    """990006: 10-22 진입 (진입일 공식 손절가 42,665), 10-23 무상증자 권리락 (기준가 30,000 / 전날 종가 45,000) -> 손절가 28,443"""
    go, held = TR.Held.go, TR.Held.held

    def setUp(self):
        super().setUp()
        self.saved_rd = P.raw_daily
        self.addCleanup(setattr, P, "raw_daily", self.saved_rd)
        self.asked = []

        def snap(caller, codes, **k):
            self.asked.append(list(codes))
            raw = pd.DataFrame({"stk_cd": list(codes), "base_pric": self.q["base"], "dt": self.q["dt"]})
            return raw, pd.DataFrame({"code": list(codes), "close": self.q["price"], "dt": self.q["dt"], "cntr_tm": "090030"}), {}
        Q.snapshot = snap
        P.raw_daily = lambda code: pd.DataFrame({"timestamp": pd.to_datetime(["2026-10-22", "2026-10-23"]), "open": [44000.0, 30500.0],
                                                 "high": [46000.0, 31000.0], "low": [43000.0, 29500.0], "close": [45000.0, 30000.0]})
        self.q = {"price": 30000.0, "base": "30000", "dt": "20261026"}

    def after_event(self, **kw):
        p = {**HELD, "stop_price": 42665.0, "stop_temp": True, "stop_basis": "임시", **kw}
        P.event_adjust(p, pd.Timestamp("2026-10-23"), 30000.0, 45000.0, "시험", "09:00:00")
        return p

    def test_recovery_keeps_event_adjusted_stop(self):
        p = self.after_event()
        self.assertAlmostEqual(p["stop_price"], 28443.333333, places=3)
        P.save(self.R.POSITIONS, {"held": {"990006": p}, "sell_duty": {}, "next_open_sells": {}, "trade_seq": 1, "duty_format": P.DUTY_FORMAT})
        self.fk.balance_rows = [bal_row("990006", 28)]
        res = self.started("08:10:00", live=False)
        res.day.set("prev_recovery", {"prev": "2026-10-23"})
        fx = {"prev": "2026-10-23", "stops": {"990006": {"stop": 42665.0, "basis": "진입일 2026-10-22 공식 일봉", "basis_date": "2026-10-22"}},
              "trend": {}, "left": [], "at": "08:05:00"}
        res.apply_recovery_fixes(fx)
        h = self.held()
        self.assertAlmostEqual(h["stop_price"], 28443.333333, places=3)                          # 예전: 42,665 로 풀림
        self.assertFalse(h["stop_temp"])
        self.clk.at("09:00:00")
        res.stop_minute()                                                                         # 현재가 30,000 >= 28,443 -> 매도 안 함
        self.assertEqual(sends(self.fk, "kt10001"), [])
        res2 = self.resident()                                                                    # 같은 날 재시작: 다시 적용·조정 없음
        res2.day, res2.started = res.day, True
        res2.om, res2.broker = res.om, res.broker
        res2.apply_recovery_fixes({**fx, "applied": None})
        self.assertAlmostEqual(self.held()["stop_price"], 28443.333333, places=3)

    def test_events_k_multi_day(self):
        p = self.after_event()
        P.event_adjust(p, D26, 15000.0, 30000.0, "시험", "09:00:00")                              # 다음 날 또 이벤트 (k 0.5)
        self.assertAlmostEqual(P.events_k(p, "2026-10-22", D26), 30000 / 45000 * 0.5)
        self.assertAlmostEqual(P.events_k(p, "2026-10-23", D26), 0.5)                              # 전날 기준 공식 손절가 -> 오늘 비율만
        self.assertAlmostEqual(P.today_k(p, D26), 0.5)
        self.assertEqual(P.events_k(p, D26, D26), 1.0)

    def test_premarket_old_base_rechecked_at_0900(self):
        """08:30 응답 dt 는 오늘인데 기준가가 옛 값(전날 종가 45,000 = 이벤트 없음) -> 09:00 첫 손절 감시 시세의 새 기준가(30,000)로 조정 (감지 전)"""
        P.raw_daily = lambda code: pd.DataFrame({"timestamp": pd.to_datetime(["2026-10-22", "2026-10-23"]), "open": [44000.0, 45000.0],
                                                 "high": [46000.0, 46000.0], "low": [43000.0, 44000.0], "close": [44500.0, 45000.0]})
        self.q.update(base="45000", price=45000.0)
        res = self.go("08:30:00", stop_price=40000.0)
        res.trend_sell()
        h = self.held()
        self.assertEqual((h["stop_price"], h["base_check"]["adjusted"], h["base_check"]["final"]), (40000.0, False, False))
        self.q.update(base="30000", price=30500.0)                                               # 09:00 새 기준가 (옛 손절가 40,000 아래)
        self.clk.at("09:00:00")
        res.stop_minute()
        self.assertEqual(sends(self.fk, "kt10001"), [])                                         # 예전: 08:30 확인으로 굳어 손절
        h = self.held()
        self.assertAlmostEqual(h["stop_price"], 40000.0 * 30000 / 45000, places=3)
        self.assertTrue(h["base_check"]["final"])
        res2 = self.resident()                                                                    # 재시작: 다시 조정 안 함
        res2.day, res2.started, res2.om, res2.broker = res.day, True, res.om, res.broker
        self.clk.at("09:01:00")
        res2.stop_minute()
        self.assertAlmostEqual(self.held()["stop_price"], 26666.666667, places=3)
        self.assertEqual(len(self.held()["stop_adjust"]), 1)

    def test_premarket_no_event_and_no_base_later_keeps_watching(self):
        """08:30 이벤트 없음 -> 09:00 시세에 기준가가 없으면 08:30 결과 그대로 (멈추지 않음, 진짜 하락은 손절)"""
        P.raw_daily = lambda code: pd.DataFrame({"timestamp": pd.to_datetime(["2026-10-23"]), "open": [45000.0], "high": [46000.0],
                                                 "low": [44000.0], "close": [45000.0]})
        self.q.update(base="45000", price=45000.0)
        res = self.go("08:30:00", stop_price=40000.0)
        res.trend_sell()
        self.q.update(base="0", price=39000.0)
        self.clk.at("09:00:00")
        r = res.stop_minute()
        self.assertEqual(r["base_paused"], [])
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["990006"])


if __name__ == "__main__":
    unittest.main()
