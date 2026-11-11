import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import testguard; testguard.install()  # noqa: E401,E402,E702  (첫 줄 유지)
"""
11W L: 2026 수능일(11-19) 거래시간 확정 (KRX 보도자료 2026-11-05) 과 그날 모든 연결 시각이 1시간 밀리는지. 가짜 서버·가짜 시계, 실제 주문·조회 없음.

  python -m unittest backtest_swing/live/test_csat.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import json
import threading
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_broad6 import Base6  # noqa: E402
from test_orders import hold, sends  # noqa: E402
import state as S  # noqa: E402

DAY = pd.Timestamp("2026-11-19")
HERE = os.path.dirname(os.path.abspath(__file__))

# 평소 -> 수능일 (12:00 앞 = 개장 지연 60분, 12:00 뒤 = 폐장 지연 60분, 07:00 하루 시작만 그대로)
TABLE = {
    "day_start": ("07:00:00", "07:00:00"),
    "trend_sell_from": ("08:30:00", "09:30:00"),       # 개장 30분 전 (장 시작 동시호가 호가 접수 시작과 같이 밀림)
    "trend_sell_until": ("08:59:00", "09:59:00"),
    "stop_from": ("09:00:00", "10:00:00"),
    "stop_until": ("15:20:00", "16:20:00"),
    "stop_send_until": ("15:29:00", "16:29:00"),
    "data_check": ("15:18:31", "16:18:31"),
    "plan_snapshot": ("15:20:01", "16:20:01"),
    "snapshot": ("15:20:01", "16:20:01"),
    "buy_from": ("15:21:00", "16:21:00"),
    "plan_start_until": ("15:26:00", "16:26:00"),
    "plan_deadline": ("15:27:00", "16:27:00"),
    "no_new_order_after": ("15:28:00", "16:28:00"),
    "close_calc": ("15:32:00", "16:32:00"),
    "close_check": ("15:35:00", "16:35:00"),
    "after_close": ("15:36:00", "16:36:00"),
    "settle": ("15:40:00", "16:40:00"),
    "close_calc_until": ("16:20:00", "17:20:00"),
    "day_end": ("16:30:00", "17:30:00"),
}
# 코드 안 상수 (resident.at 으로 그날 시각표에)
CONST = {
    "TREND_WAIT_UNTIL": ("08:50:00", "09:50:00"),
    "OFFICIAL_FAST_FROM": ("08:05:00", "09:05:00"),
    "BASE_PROBE_KRX": ("08:20:00", "09:20:00"),
}


class CsatConfirmed(unittest.TestCase):
    def test_entry_confirmed_from_krx_press_release(self):
        with open(os.path.join(HERE, "krx_special_hours.json"), encoding="utf-8") as f:
            e = json.load(f)["2026-11-19"]
        self.assertEqual((e["open"], e["close"], e["confirmed"]), ("10:00:00", "16:30:00", True))
        for s in ("KRX 보도자료 2026-11-05", "10:00~16:30", "16:20~16:30", "16:40~17:00", "17:00~20:00", "09:30~09:40"):
            self.assertIn(s, e["note"])
        h = S.day_hours(DAY)
        self.assertEqual((h["open_shift"], h["close_shift"], h["special"], h["confirmed"]), (60, 60, True, True))
        self.assertTrue(S.is_trading_day(DAY))
        self.assertEqual(S.close_hhmm(DAY), "1630")                               # 1분봉 종가 기준 봉 (평소 1530)


class CsatTimes(Base6):
    def setUp(self):
        super().setUp()
        self.clk.t = pd.Timestamp("2026-11-19 07:00:00")

    def test_every_time_shifts_one_hour(self):
        R = self.R
        for k, (base, _) in TABLE.items():
            self.assertEqual(R.BASE_T[k], base, k)                                  # 평소 시각표가 표와 같음 (config.example)
        R.apply_hours(DAY)
        self.assertEqual({k: R.T[k] for k in TABLE}, {k: v for k, (_, v) in TABLE.items()})
        for k, v in R.BASE_T.items():                                             # 표에 없는 시각 키도 모두 60분 (하루 시작만 그대로)
            if isinstance(v, str) and len(v) == 8 and v[2] == ":" and k != "day_start":
                want = (pd.Timestamp(f"2000-01-01 {v}") + pd.Timedelta(minutes=60)).strftime("%H:%M:%S")
                self.assertEqual(R.T[k], want, k)
        for name, (base, want) in CONST.items():
            self.assertEqual((getattr(R, name), R.at(getattr(R, name))), (base, want), name)
        self.assertEqual([R.at(x) for x in R.REFETCH], ["09:30:00", "11:00:00", "13:00:00", "15:00:00", "16:00:00"])    # 장중 재수집
        self.assertEqual([R.at(x) for x in R.BASE_PROBE], ["08:05:00", "08:30:00", "09:00:00", "09:20:00"])  # 아침 기준가 측정
        self.assertEqual([(R.at(a), R.at(b)) for _, a, b in R.TOKEN_CHECKS], [("09:20:00", "16:40:00"), ("16:10:00", "16:45:00")])
        import telecmd
        self.assertEqual((R.at(telecmd.HOLD[0]), R.at(telecmd.HOLD[1])), ("16:17:00", "16:22:00"))   # 조회 명령 답 미루는 시간
        self.assertEqual(R.index_bar_hhmm(), "1619")                              # 스냅샷 지수 봉 (평소 1519)

    def test_other_modules_follow_the_day(self):
        import backup as B
        import update as U
        cal = B.Calendar(times=self.R.BASE_T)
        self.assertEqual((cal.day_end(DAY), cal.day_end("2026-11-18")), ("17:30:00", "16:30:00"))  # 백업: 하루 끝 뒤에만
        self.assertTrue(cal.in_session(pd.Timestamp("2026-11-19 17:00:00")))
        self.assertFalse(U.allowed(pd.Timestamp("2026-11-19 08:30:00")) and U.allowed(pd.Timestamp("2026-11-19 16:30:00")))
        self.assertTrue(U.allowed(pd.Timestamp("2026-11-19 08:59:00")))            # 갱신 금지 09:00 ~ 16:45 (평소 08:00 ~ 15:45)
        self.assertFalse(U.allowed(pd.Timestamp("2026-11-19 09:00:00")))
        self.assertFalse(U.allowed(pd.Timestamp("2026-11-19 16:44:00")))
        self.assertTrue(U.allowed(pd.Timestamp("2026-11-19 16:45:00")))

    def test_resident_order_windows_on_csat_day(self):
        hold(self.fk, "990010", 28)
        res = self.started("09:29:00", live=False)
        t = res.om.t
        self.assertEqual({k: t[k] for k in ("trend_sell_from", "trend_sell_until", "stop_from", "stop_until", "stop_send_until", "buy_from",
                                             "no_new_order_after", "close_check")},
                         {k: TABLE[k][1] for k in ("trend_sell_from", "trend_sell_until", "stop_from", "stop_until", "stop_send_until", "buy_from",
                                                   "no_new_order_after", "close_check")})
        held = {"990010": {"file_qty": 28, "acct": {"qty": 28, "sellable": 28}}}
        for hms in ("08:30:00", "09:29:59"):                                     # 평소 추세 매도 시각에는 안 보냄 (그날 호가 접수 전)
            self.clk.at(hms)
            self.assertEqual(res.om.trend_sells(["990010"], held), [])
        self.assertEqual(sends(self.fk, "kt10001"), [])
        self.clk.at("09:30:00")                                                   # 개장 30분 전 = 09:30
        res.om.trend_sells(["990010"], held)
        self.assertEqual(len(sends(self.fk, "kt10001")), 1)
        self.assertEqual(res.om.detect_stops({"990010": {"cur_price": 1.0}}, {"990010": 2.0}, at="09:59:59"), {})   # 손절 감지 10:00 부터
        self.assertIn("990010", res.om.detect_stops({"990010": {"cur_price": 1.0}}, {"990010": 2.0}, at="10:00:00"))
        self.assertIn("990010", res.om.detect_stops({"990010": {"cur_price": 1.0}}, {"990010": 2.0}, at="16:20:59"))
        self.assertEqual(res.om.detect_stops({"990010": {"cur_price": 1.0}}, {"990010": 2.0}, at="16:21:00"), {})
        self.assertTrue(any(a.startswith("[거래시간 변경일] 2026-11-19") and "스냅샷 16:20:01" in a and "정산 16:40:00" in a and "하루 끝 17:30:00" in a
                            and "확인 필요" not in a for a in self.alerts))

    def test_base_probe_follows_the_day(self):
        """아침 기준가 측정도 개장 지연만큼 (예전: 07:05·07:30·08:00·08:20 그대로)"""
        got = []
        res = self.started("07:05:00", live=False)
        res._base_probe = lambda day, slot, at_hms=None: got.append((slot, at_hms))

        def tick(hms):
            self.clk.at(hms)
            res.base_probe_tick(self.clk())
            if res.probe_thread is not None:
                res.probe_thread.join(5)
        tick("07:05:00")                                                          # 평소 시각: 수능일에는 아직
        tick("07:30:00")
        self.assertEqual(got, [])
        tick("08:05:00")
        tick("08:30:00")
        tick("09:00:00")
        tick("09:20:00")
        tick("09:40:00")                                                          # 10분 넘게 늦은 회차는 없음
        self.assertEqual(got, [("07:05:00", "08:05:00"), ("07:30:00", "08:30:00"), ("08:00:00", "09:00:00"), ("08:20:00", "09:20:00")])
        self.assertEqual(res.day.s["base_probe"]["08:20:00"], {"started": "09:20:00", "at": "09:20:00"})

    def test_base_probe_normal_day_unchanged(self):
        got = []
        self.clk.t = pd.Timestamp("2026-11-18 07:05:00")
        res = self.started("07:05:00", live=False)
        res._base_probe = lambda day, slot, at_hms=None: got.append((slot, at_hms))
        res.base_probe_tick(self.clk())
        res.probe_thread.join(5)
        self.assertEqual(got, [("07:05:00", "07:05:00")])
        self.assertEqual(res.day.s["base_probe"]["07:05:00"], {"started": "07:05:00"})


if __name__ == "__main__":
    unittest.main()
