"""
11U 시험 (첫 주 넓은 리뷰 <커밋>, 손절·판단 안전):
P0-3 기존 매도 의무 전송이 새 손절 감지를 막음 (계좌 1초 + 의무 A·B 전송 각 5초 -> 시세 조회 남은 시간 0 -> C 감지 못 함)
P0-1 이벤트 날 상주 프로그램이 안 돌았으면 옛 손절가 (권리락 10,000 -> 8,000 날 미실행 -> 다음 날 기준가·전날 종가 모두 8,000 -> 조정 없음)
P0-2 일봉 대상 제외 종목의 원주가 판단이 이벤트를 하락으로 봄 (10,000 -> 8,000 권리락 평탄 이력 -> 추세 매도·낮은 손절가)
가짜 서버·가짜 시계, 실제 키움 호출·data·logs 없음.

  python -m unittest backtest_swing/live/test_fix11u.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_rebase as TR  # noqa: E402
from test_broad2 import setup  # noqa: E402
from test_codex import bal_row  # noqa: E402
from test_orders import sends  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import quotes as Q  # noqa: E402


# ---------------------------------------------------------------- P0-3

class Order(unittest.TestCase):
    """A·B = 손절 진행 중 의무 (전송 각 5초), C = 손절가 90·현재가 80 (새 손절). 계좌 조회 1초. 가짜 벽시계"""

    def setUp(self):
        self.clk, self.fk, self.om, _ = setup()
        self.t = [0.0]
        self.fk.balance_rows = [bal_row(c, 10) for c in ("000010", "000020", "000030")]
        self.pos = {"held": {c: {"qty": 10, "trade_id": f"{c}-2026-10-20-1", "entry_date": "2026-10-20", "stop_price": 90.0, "kind": "본래"}
                             for c in ("000010", "000020", "000030")},
                    "sell_duty": {c: O.new_duty("stop", "2026-10-26 09:30:00", "현재가 85 < 손절가 90", price=85.0, stop=90.0)
                                  for c in ("000010", "000020")},
                    "next_open_sells": {}, "trade_seq": 3, "duty_format": P.DUTY_FORMAT}
        self.log = []
        real = self.om.b.sell_market

        def sell(c, q):
            self.log.append(("send", c))
            self.t[0] += 5.0
            return real(c, q)
        self.om.b.sell_market = sell

    def wall(self):
        return self.t[0]

    def bal(self):
        self.log.append(("account",))
        self.t[0] += 1.0
        return self.fk.request("kt00018", "POST", "/x").json()

    def quote(self, codes, deadline=None):
        self.log.append(("quote", sorted(codes)))
        if deadline is not None and self.wall() >= deadline:
            raise ValueError("시간 상한")
        return {"000010": 85.0, "000020": 85.0, "000030": 80.0}

    def cycle(self, **kw):
        return O.stop_cycle(self.om, O.AccountWatch(), self.pos, self.quote, self.bal, self.clk(), set(), budget=10, wall=self.wall, **kw)

    def test_duties_do_not_block_new_detection(self):
        r = self.cycle()
        self.assertEqual(list(r["triggered_new"]), ["000030"])                   # 예전: 비어 있음 (시세 조회 시간 0)
        self.assertEqual(self.log[0][0], "quote")                                # 시세 조회 먼저
        self.assertEqual([x[1] for x in self.log if x[0] == "send"], ["000030", "000010", "000020"])   # 새 손절 먼저
        self.assertEqual(self.pos["sell_duty"]["000030"]["kind"], "stop")

    def test_send_budget_defers_rest_to_next_minute(self):
        r = self.cycle(send_budget=8)
        self.assertEqual([x[1] for x in self.log if x[0] == "send"], ["000030", "000010"])   # 0 초·5 초에 보냄, 10 초 > 8 초 -> 이월
        self.assertEqual(r["send_deferred"], ["000020"])
        self.assertEqual(self.om.send_left, ["000020"])
        self.log.clear()
        self.clk.at("10:01:00")
        self.fk.balance_rows = [bal_row(c, 10, sellable=0 if c != "000020" else 10) for c in ("000010", "000020", "000030")]   # 걸린 매도 대기
        r = self.cycle(send_budget=8)
        self.assertEqual([x[1] for x in self.log if x[0] == "send"], ["000020"])                 # 이월된 것 먼저 (나머지는 매도 가능 0)
        self.assertEqual(r["send_deferred"], [])


# ---------------------------------------------------------------- P0-1

def raw_days(days, closes):
    return pd.DataFrame({"timestamp": pd.to_datetime(days), "open": closes, "high": closes, "low": closes, "close": closes})


class Gap(TR.Base):
    """990006 손절가 9,000. 10-23 권리락 (10,000 -> 8,000, 수량 그대로) 날 상주 프로그램 안 돎 (마지막 확인 10-22). 오늘 10-26 기준가·전날 종가 8,000"""
    go, held = TR.Held.go, TR.Held.held

    def setUp(self):
        super().setUp()
        saved = (P.raw_daily, getattr(P, "raw_factors", None), Q.snapshot)
        self.addCleanup(lambda: (setattr(P, "raw_daily", saved[0]), setattr(P, "raw_factors", saved[1]), setattr(Q, "snapshot", saved[2])))
        P.raw_daily = lambda code: raw_days(["2026-10-22", "2026-10-23"], [10000.0, 8000.0])
        self.fac = pd.DataFrame({"timestamp": pd.to_datetime(["2026-10-21", "2026-10-22", "2026-10-23"]), "factor": [0.8, 0.8, 1.0]})
        P.raw_factors = lambda code: self.fac
        self.q = {"price": 8100.0, "base": "8000", "dt": "20261026"}

        def snap(caller, codes, **k):
            raw = pd.DataFrame({"stk_cd": list(codes), "base_pric": self.q["base"], "dt": self.q["dt"]})
            return raw, pd.DataFrame({"code": list(codes), "close": self.q["price"], "dt": self.q["dt"], "cntr_tm": "090030"}), {}
        Q.snapshot = snap

    def test_missed_event_day_adjusted_by_kiwoom_factor(self):
        res = self.go("09:00:00", stop_price=9000.0, base_seen="2026-10-22")
        res.stop_minute()
        self.assertEqual(sends(self.fk, "kt10001"), [])                          # 예전: 8,100 < 9,000 손절
        h = self.held()
        self.assertAlmostEqual(h["stop_price"], 7200.0)
        self.assertEqual((h["base_gap"]["days"], h["base_gap"]["events"]), (["2026-10-23"], {"2026-10-23": 0.8}))
        self.assertEqual(h["base_seen"], "2026-10-26")
        self.assertFalse(P.in_review(h))
        self.assertAlmostEqual(P.events_k(h, "2026-10-22", "2026-10-26"), 0.8)  # 복구·재판단의 누적 비율에도 들어감
        self.q["price"] = 7100.0                                                  # 조정된 손절가 아래 진짜 하락
        self.clk.at("09:01:00")
        res.stop_minute()
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["990006"])
        self.assertAlmostEqual(self.held()["stop_price"], 7200.0)                 # 두 번 조정 안 함

    def test_unverifiable_gap_pauses_code(self):
        self.fac = self.fac[self.fac.timestamp != pd.Timestamp("2026-10-23")]     # 공백 날 계수 없음
        res = self.go("09:00:00", stop_price=9000.0, base_seen="2026-10-22")
        r = res.stop_minute()
        self.assertEqual(sends(self.fk, "kt10001"), [])
        self.assertIn("990006", r["base_paused"])
        h = self.held()
        self.assertTrue(P.in_review(h))
        self.assertEqual((h["stop_price"], h["base_review"]["days"]), (9000.0, ["2026-10-23"]))
        self.assertTrue(any(a.startswith("[수동 확인 필요] 990006 기준가 확인 공백") for a in self.alerts))
        self.q["price"] = 7000.0
        self.clk.at("09:01:00")
        res.stop_minute()
        self.assertEqual(sends(self.fk, "kt10001"), [])                          # 사람이 확인할 때까지 자동 손절 안 함
        pos = P.load(self.R.POSITIONS)[0]
        pos["held"]["990006"]["stop_price"] = 7200.0                             # 사람이 손절가를 고치면 해제
        P.save(self.R.POSITIONS, pos)
        self.clk.at("09:02:00")
        res.stop_minute()
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["990006"])

    def no_gap(self, **kw):
        P.raw_factors = lambda code: (_ for _ in ()).throw(AssertionError("공백 없으면 일봉 안 읽음"))
        res = self.go("09:00:00", stop_price=9000.0, **kw)
        res.stop_minute()
        h = self.held()
        self.assertEqual((h["stop_price"], h["base_seen"], h.get("base_gap")), (9000.0, "2026-10-26", None))
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["990006"])   # 8,100 < 9,000 진짜 하락은 그대로 손절

    def test_no_gap_normal_day(self):
        self.no_gap(base_seen="2026-10-23")

    def test_old_ledger_without_base_seen(self):
        self.no_gap()                                                             # 11U 전 장부 = 공백 없음으로 시작, 오늘 기록

    def test_new_trade_records_entry_day(self):
        pos = P.empty()
        self.assertEqual(P.new_trade(pos, "990006", "2026-10-23")["base_seen"], "2026-10-23")


# ---------------------------------------------------------------- P0-2

def flat_event(n=30, event_at=-1, factor=True):
    """10,000 평탄 (고저 ±100) 이력 뒤 권리락 8,000 (고저 ±100). event_at = 이벤트 행 위치"""
    ts = pd.bdate_range(end="2026-10-26", periods=n)
    close = np.full(n, 10000.0)
    close[event_at:] = 8000.0
    df = pd.DataFrame({"timestamp": ts, "open": close, "high": close + 100, "low": close - 100, "close": close})
    if factor:
        df["factor"] = np.where(close == 10000.0, 0.8, 1.0)
    return df


class RawJudge(TR.Base):
    def setUp(self):
        super().setUp()
        import daily
        saved = (daily.Store, P.raw_daily)
        self.addCleanup(lambda: (setattr(daily, "Store", saved[0]), setattr(P, "raw_daily", saved[1])))

        class NoStore:
            def __init__(self, codes, use_append=True):
                self.adj, self.raw = {}, {}
        daily.Store = NoStore
        self.res = self.R.Resident()

    def old_calc(self, df):
        pc = df.close.shift(1)
        tr = np.maximum(df.high - df.low, np.maximum((df.high - pc).abs(), (df.low - pc).abs()))
        atr, ma = tr.rolling(20).mean().iat[-1], df.close.rolling(20).mean().iat[-1]
        return float(df.close.iat[-1]), float(df.close.iat[-1] - 1.7 * atr), bool(df.close.iat[-1] < ma)

    def test_event_in_window_from_factor(self):
        """공식 일봉(파일 행)으로 판단: 판정 창 안 키움 계수 변화 (예전: 추세 매도·손절가 낮아짐).
        11U 보완 4: 보류 대신 계수로 이벤트 전 행을 맞춰 판단 -> 권리락만으로는 추세 매도 아님"""
        df = flat_event(event_at=-3)
        P.raw_daily = lambda code: df
        self.assertTrue(self.old_calc(df)[2])                                     # 예전 판단 = 추세 매도
        adj = df.copy()
        adj.loc[adj.index[:-3], ["open", "high", "low", "close"]] *= 0.8
        want = self.old_calc(adj)
        j = self.res.judge(["999999"], "2026-10-26")["999999"]
        self.assertEqual((j["close"], round(j["stop"], 6), j["trend"]), (want[0], round(want[1], 6), False))
        self.assertFalse(any(a.startswith("[판단 보류]") for a in self.alerts))

    def test_today_event_from_base(self):
        """15:30 1분봉 행 + 그날 기준가 8,000 / 전날 종가 10,000 (계수 열 없는 이력도). 11U 보완 4: 그 비율로 맞춰 판단 -> 추세 매도 아님"""
        df = flat_event(factor=False)
        P.raw_daily = lambda code: df.iloc[:-1]
        cv = {"999999": {"open": 8000.0, "high": 8100.0, "low": 7900.0, "close": 8000.0}}
        self.assertTrue(self.old_calc(df)[2])
        j = self.res.judge(["999999"], "2026-10-26", cvs=cv, bases={"999999": 8000.0})["999999"]
        self.assertFalse(j["trend"])
        self.assertFalse(any(a.startswith("[판단 보류]") for a in self.alerts))

    def test_normal_day_same_as_before(self):
        ts = pd.bdate_range(end="2026-10-26", periods=30)
        close = np.linspace(10000, 9000, 30)
        df = pd.DataFrame({"timestamp": ts, "open": close, "high": close + 150, "low": close - 150, "close": close, "factor": 1.0})
        P.raw_daily = lambda code: df
        want = self.old_calc(df)
        j = self.res.judge(["999999"], "2026-10-26")["999999"]
        self.assertEqual((j["close"], round(j["stop"], 6), j["trend"]), (want[0], round(want[1], 6), want[2]))
        P.raw_daily = lambda code: df.iloc[:-1]
        cv = {"999999": {k: float(df[k].iat[-1]) for k in ("open", "high", "low", "close")}}
        j = self.res.judge(["999999"], "2026-10-26", cvs=cv, bases={"999999": float(close[-2])})["999999"]
        self.assertEqual((j["close"], round(j["stop"], 6), j["trend"]), (want[0], round(want[1], 6), want[2]))
        self.assertFalse(any(a.startswith("[판단 보류]") for a in self.alerts))


if __name__ == "__main__":
    unittest.main()
