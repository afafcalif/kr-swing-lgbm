"""
11U·11V 보완 시험 (<커밋> Codex 확인 P0 1·P1 3):
1 [P0] before_detect 의 공백 확인 실패로 base_review 가 새로 걸려도 그 분 기존 의무 전송 대상에서 안 뺌
2 [P1] 15:28 에 20초 전송 상한으로 이월된 매도는 15:29 전송 창이 닫혀 당일 사라짐
3 [P1] 감시 작업이 밀린 알림(건당 최대 약 42초)을 다 보낸 뒤에야 상주 프로그램을 다시 켬 (작업 제한 2분)
4 [P1] 일봉 대상 제외 종목: 판정 창에 이벤트가 있으면 약 20거래일 동안 추세 이탈 판단을 막음
가짜 서버·가짜 시계·임시 폴더, 실제 키움·텔레그램 호출·data·logs 없음.

  python -m unittest backtest_swing/live/test_fix11uv.py -v
"""
import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import testguard; testguard.install()  # noqa: E401,E402,E702  (첫 줄 유지)
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"
import json
import shutil
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_rebase as TR  # noqa: E402
from test_broad2 import setup  # noqa: E402
from test_codex import bal_row  # noqa: E402
from test_orders import FakeClock, FakeKiwoom  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import state as S  # noqa: E402

CODES = [f"0000{i}0" for i in range(1, 7)]   # A ~ F


class Cycle(unittest.TestCase):
    """의무 = 손절 진행 중 (전송 하나 5초: 가짜 벽시계·가짜 시계 함께)"""

    def make(self, t="2026-10-26 10:00:00", codes=CODES[:2], times=None):
        self.clk = FakeClock(t)
        self.fk = FakeKiwoom(self.clk)
        self.om = O.OrderManager(O.Broker(self.fk, allow_orders=True), clock=self.clk, sleep=self.clk.sleep, times=times)
        self.t = [0.0]
        self.fk.balance_rows = [bal_row(c, 10) for c in codes]
        self.pos = {"held": {c: {"qty": 10, "trade_id": f"{c}-2026-10-20-1", "entry_date": "2026-10-20", "stop_price": 90.0, "kind": "본래"}
                             for c in codes},
                    "sell_duty": {c: O.new_duty("stop", "2026-10-26 09:30:00", "현재가 85 < 손절가 90", price=85.0, stop=90.0) for c in codes},
                    "next_open_sells": {}, "trade_seq": 6, "duty_format": P.DUTY_FORMAT}
        self.sent = []
        real = self.om.b.sell_market

        def sell(c, q):
            self.sent.append(c)
            self.t[0] += 5.0
            self.clk.t += pd.Timedelta(seconds=5)
            return real(c, q)
        self.om.b.sell_market = sell

    def cycle(self, **kw):
        bal = lambda: self.fk.request("kt00018", "POST", "/x").json()  # noqa: E731
        return O.stop_cycle(self.om, O.AccountWatch(), self.pos, lambda codes, deadline=None: {c: 85.0 for c in codes}, bal,
                            self.clk(), set(), budget=10, wall=lambda: self.t[0], send_budget=20, **kw)

    # 1 [P0]
    def test_new_base_review_excludes_existing_duty(self):
        self.make()
        a = CODES[0]

        def gate(prices):  # 기준가 확인 공백을 확인 못 함 -> base_review (resident.base_gate 와 같이 장부에 표시하고 돌려줌)
            self.pos["held"][a]["base_review"] = {"date": "2026-10-26", "stop": self.pos["held"][a]["stop_price"]}
            return [a]
        r = self.cycle(before_detect=gate)
        self.assertEqual(r["base_paused"], [a])
        self.assertNotIn(a, self.sent)                                           # 예전: 전송 목록 ['A', ...]
        self.assertEqual(self.sent, [CODES[1]])                                  # 다른 종목 의무는 그대로
        self.assertIn(a, r["review_paused"])

    def test_unknown_base_only_keeps_existing_duty(self):
        """기준가를 몰라 새 감지에서만 뺀 종목(수동 확인 아님)은 기존 의무를 그대로 보냄 (예전과 같음)"""
        self.make()
        r = self.cycle(before_detect=lambda prices: [CODES[0]])
        self.assertEqual(sorted(self.sent), CODES[:2])
        self.assertEqual(r["review_paused"], [])

    # 2 [P1]
    def test_last_minute_sends_all_until_close(self):
        self.make("2026-10-26 15:28:00", CODES)
        r = self.cycle(detect=False)
        self.assertEqual(self.sent, CODES)                                       # 예전: A~D 만, E·F 이월 (15:29 부터 창 닫힘)
        self.assertEqual((r["send_deferred"], r["last_send_minute"]), ([], True))

    def test_last_minute_stops_at_send_close_once_each(self):
        self.make("2026-10-26 15:28:50", CODES)
        r = self.cycle(detect=False)
        self.assertEqual(self.sent, CODES[:2])                                   # 15:28:50·55 -> 15:29:00 마감
        self.assertEqual(r["send_deferred"], CODES[2:])

    def test_normal_minute_budget_unchanged(self):
        self.make("2026-10-26 15:27:00", CODES)
        r = self.cycle(detect=False)
        self.assertEqual(self.sent, CODES[:4])                                   # 0·5·10·15 초, 20 초 -> 다음 분
        self.assertEqual((r["send_deferred"], r.get("last_send_minute", False)), (CODES[4:], False))

    def test_last_minute_moves_with_trading_hours(self):
        """거래시간 변경일 (1시간 늦게, 전송 마감 16:29): 15:28 은 평소 분, 16:28 이 마지막 분"""
        times = S.shift_times({"stop_from": "09:00:00", "stop_until": "15:20:00", "stop_send_until": "15:29:00"},
                                {"open_shift": 60, "close_shift": 60})
        self.assertEqual(times["stop_send_until"], "16:29:00")
        self.make("2026-11-19 15:28:00", CODES, times=times)
        self.assertEqual(self.cycle(detect=False)["send_deferred"], CODES[4:])
        self.make("2026-11-19 16:28:00", CODES, times=times)
        r = self.cycle(detect=False)
        self.assertEqual((self.sent, r["send_deferred"]), (CODES, []))
        self.assertIsNone(self.om.last_send_minute(pd.Timestamp("2026-11-19 16:29:00")))


# ---------------------------------------------------------------- 3 [P1]

class Watchdog(unittest.TestCase):
    def setUp(self):
        import common
        import watchdog as W
        self.W = W
        self.tmp = tempfile.mkdtemp()
        saved = (dict(W.CFG), W.LOOP_FLAG, W.FLAG, W.LOG, W.start, W.N.send, common.resident_running, W.time.time)

        def restore():
            (cfg, W.LOOP_FLAG, W.FLAG, W.LOG, W.start, W.N.send, common.resident_running, W.time.time) = saved
            W.CFG.clear()
            W.CFG.update(cfg)
            shutil.rmtree(self.tmp, ignore_errors=True)
        self.addCleanup(restore)
        W.CFG.update(log_dir=self.tmp, state_dir=self.tmp, heartbeat_file=os.path.join(self.tmp, "hb.log"), pid_file=os.path.join(self.tmp, "r.pid"))
        W.LOOP_FLAG, W.FLAG, W.LOG = os.path.join(self.tmp, "loop.flag"), os.path.join(self.tmp, "hb.flag"), os.path.join(self.tmp, "w.log")
        self.clock, self.log, self.ok = [0.0], [], [False]
        W.time.time = lambda: self.clock[0]

        def start():
            self.log.append(("start", self.clock[0]))
            return 4242
        W.start = start

        def send(msg, **k):  # 한 건 42초 (보내기 실패 때 최대) / 성공은 20초
            self.log.append(("send", msg, self.pending()))
            self.clock[0] += 20.0 if self.ok[0] else 42.0
            return self.ok[0]
        W.N.send = send
        self.running = [False]
        common.resident_running = lambda *a, **k: self.running[0]
        with open(W.st("pending"), "w", encoding="utf-8") as f:
            json.dump(["a", "b", "c"], f)

    def pending(self):
        try:
            return json.load(open(self.W.st("pending"), encoding="utf-8"))
        except FileNotFoundError:
            return None

    def test_restart_before_alerts(self):
        self.W.main()
        self.assertEqual(self.log[0], ("start", 0.0))                           # 예전: 미전송 3건 x 42초 뒤 (작업 제한 2분에 잘림)
        sends = [x for x in self.log if x[0] == "send"]
        self.assertEqual(sends[0][1], "[지난 미전송] a")                          # 실패하면 그 회차 다시 보내기 멈춤
        self.assertTrue(sends[1][1].startswith("[감시] 상주 프로그램이 없어 다시 켬"))
        self.assertEqual(len(sends), 2)
        self.assertEqual(self.pending()[:3], ["a", "b", "c"])                    # 남은 것·다시 켬 알림은 다음 회차

    def test_resend_budget_and_saved_each(self):
        self.running[0], self.ok[0] = True, True
        self.W.resend_pending()
        sends = [x for x in self.log if x[0] == "send"]
        self.assertEqual([x[1] for x in sends], ["[지난 미전송] a", "[지난 미전송] b"])   # 0·20 초 시작, 40 초 >= 30 초 -> 다음 회차
        self.assertEqual(sends[1][2], ["b", "c"])                                # 첫 건 성공 직후 저장
        self.assertEqual(self.pending(), ["c"])
        self.W.resend_pending()
        self.assertIsNone(self.pending())

    def test_resend_count_cap(self):
        self.ok[0] = True
        with open(self.W.st("pending"), "w", encoding="utf-8") as f:
            json.dump(list("abcdefg"), f)
        self.W.resend_pending(budget=1000)
        self.assertEqual(self.pending(), ["f", "g"])                             # 회차당 5건


# ---------------------------------------------------------------- 4 [P1]

def series(closes, factor=None):
    ts = pd.bdate_range(end="2026-10-26", periods=len(closes))
    c = np.asarray(closes, dtype=float)
    df = pd.DataFrame({"timestamp": ts, "open": c, "high": c + 100, "low": c - 100, "close": c})
    if factor is not None:
        df["factor"] = factor
    return df


def calc(df):
    pc = df.close.shift(1)
    tr = np.maximum(df.high - df.low, np.maximum((df.high - pc).abs(), (df.low - pc).abs()))
    atr, ma = tr.rolling(20).mean().iat[-1], df.close.rolling(20).mean().iat[-1]
    return float(df.close.iat[-1]), round(float(df.close.iat[-1] - 1.7 * atr), 6), bool(df.close.iat[-1] < ma)


def scaled(df, before, k):
    out = df.copy()
    m = out.timestamp < pd.Timestamp(before)
    out.loc[m, ["open", "high", "low", "close"]] *= k
    return out


class RawJudge(TR.Base):
    """10,000 평탄 이력 -> 권리락 (k 0.8) 뒤 8,000. 이벤트 3행 전"""

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
        self.res.clock = self.clk

    def j(self, df, p=None, **kw):
        P.raw_daily = lambda code: df
        r = P.raw_judge("999999", "2026-10-26", **({"p": p} if p else {}), **kw)
        return r if r is None or r[0] == "hold" else (r[0], round(r[1], 6), r[2])

    def test_real_break_after_event_from_factor(self):
        c = [10000.0] * 27 + [8000.0, 7900.0, 7000.0]
        f = [0.8] * 27 + [1.0] * 3
        df = series(c, f)
        ev = df.timestamp.iat[27]
        want = calc(scaled(df, ev, 0.8))
        self.assertTrue(want[2])                                                 # 맞춘 이력으로도 진짜 추세 이탈
        self.assertEqual(self.j(df), want)                                       # 예전: 보류 (약 20거래일)
        self.assertEqual(self.res.judge(["999999"], "2026-10-26")["999999"]["trend"], True)

    def test_event_only_not_trend_sell(self):
        df = series([10000.0] * 27 + [8000.0] * 3, [0.8] * 27 + [1.0] * 3)
        r = self.j(df)
        self.assertFalse(r[2])                                                   # 권리락만으로는 추세 매도 아님
        self.assertEqual(r, calc(scaled(df, df.timestamp.iat[27], 0.8)))

    def test_ledger_stop_adjust_without_factor(self):
        """factor 열 없음: 보유 장부 stop_adjust (그날 기준가 / 전날 종가) 로 맞춤 -> resident.judge 가 장부를 읽어 넘김"""
        df = series([10000.0] * 27 + [8000.0, 7900.0, 7000.0])
        ev = str(df.timestamp.iat[27].date())
        p = {"qty": 10, "stop_price": 7200.0, "stop_adjust": [{"date": ev, "base": 8000.0, "prev_close": 10000.0, "k": 0.8}]}
        want = calc(scaled(df, ev, 0.8))
        self.assertEqual(self.j(df, p), want)
        pos = P.empty()
        pos["held"]["999999"] = p
        P.save(self.R.POSITIONS, pos)
        j = self.res.judge(["999999"], "2026-10-26")["999999"]
        self.assertEqual((j["close"], round(j["stop"], 6), j["trend"]), want)

    def test_today_event_from_base(self):
        df = series([10000.0] * 29 + [7000.0])
        cv = {k: float(df[k].iat[-1]) for k in ("open", "high", "low", "close")}
        P.raw_daily = lambda code: df.iloc[:-1]
        r = P.raw_judge("999999", "2026-10-26", cv, 8000.0)
        self.assertEqual((r[0], round(r[1], 6), r[2]), calc(scaled(df, df.timestamp.iat[-1], 0.8)))
        self.assertTrue(r[2])
        p = {"base_check": {"date": "2026-10-26", "base": 8000.0, "prev_close": 10000.0}}   # 기준가를 안 넘겨도 오늘 확인 기록으로
        r2 = P.raw_judge("999999", "2026-10-26", cv, None, p)
        self.assertEqual(r2, r)

    def test_no_coefficient_holds_and_alerts_daily(self):
        df = series([10000.0] * 30)
        P.raw_daily = lambda code: df.iloc[:-1]
        cv = {"999999": {k: float(df[k].iat[-1]) for k in ("open", "high", "low", "close")}}
        self.assertNotIn("999999", self.res.judge(["999999"], "2026-10-26", cvs=cv))   # 오늘 기준가 모름 -> 보류
        self.res.judge(["999999"], "2026-10-26", cvs=cv)
        n = sum(a.startswith("[판단 보류] 999999") for a in self.alerts)
        self.assertEqual(n, 1)                                                   # 같은 날은 한 번
        self.clk.t = pd.Timestamp("2026-10-27 15:40:00")                         # 다음 날도 보류 -> 다시 알림 (예전: 판단한 날짜당 한 번)
        self.res.raw_hold("999999", "2026-10-26", "오늘 기준가 모름")
        self.assertEqual(sum(a.startswith("[판단 보류] 999999") for a in self.alerts), 2)

    def test_factor_gap_ambiguous_holds(self):
        f = [0.8] * 25 + [np.nan, np.nan] + [1.0] * 3                             # 계수 변화가 계수 없는 행 사이 -> 날짜 모름
        df = series([10000.0] * 26 + [8000.0] * 4, f)
        self.assertEqual(self.j(df)[0], "hold")

    def gap2(self):
        """11V 보완 (Codex 재현): 10,000 -> 8,000 -> 4,000 (이벤트 0.8·0.5, 누적 0.4), factor 0.4 -> NaN -> NaN -> 1.0 (두 날짜 모름). 실제 하락 없음"""
        df = series([10000.0] * 25 + [8000.0] + [4000.0] * 4, [0.4] * 25 + [np.nan, np.nan] + [1.0] * 3)
        return df, str(df.timestamp.iat[25].date()), str(df.timestamp.iat[26].date())

    def test_gap_partial_ledger_holds(self):
        df, e1, e2 = self.gap2()
        p = {"stop_adjust": [{"date": e2, "base": 4000.0, "prev_close": 8000.0, "k": 0.5}]}   # 첫 이벤트 뒤 진입 -> 0.5 만
        self.assertEqual(self.j(df, p)[0], "hold")                               # 예전: 0.5 만 맞추고 손절가 낮음·추세 매도
        full = {"stop_adjust": p["stop_adjust"] + [{"date": e1, "base": 8000.0, "prev_close": 10000.0, "k": 0.8}]}
        r = self.j(df, full)
        self.assertEqual(r, calc(scaled(scaled(df, e1, 0.8), e2, 0.5)))          # 두 계수가 다 있으면 정상 판단
        self.assertFalse(r[2])

    def test_gap_one_event_ledger_matches(self):
        f = [0.8] * 25 + [np.nan, np.nan] + [1.0] * 3
        df = series([10000.0] * 26 + [8000.0] * 4, f)
        ev = str(df.timestamp.iat[26].date())
        p = {"stop_adjust": [{"date": ev, "base": 8000.0, "prev_close": 10000.0, "k": 0.8}]}
        self.assertEqual(self.j(df, p), calc(scaled(df, ev, 0.8)))              # 기록 곱 = 구간 변화 -> 지금처럼 정상 판단
        bad = {"stop_adjust": [{"date": ev, "base": 9000.0, "prev_close": 10000.0, "k": 0.9}]}
        self.assertEqual(self.j(df, bad)[0], "hold")                             # 기록 곱이 구간 변화와 다름 -> 보류
        flat = series([10000.0] * 30, [1.0] * 25 + [np.nan, np.nan] + [1.0] * 3)
        self.assertEqual(self.j(flat), calc(flat))                               # 변화 없는 공백 구간 = 그대로
        self.assertEqual(self.j(flat, p)[0], "hold")                             # 변화 없는 구간에 계수 0.8 -> 설명 안 됨

    def small(self, last=9951.0):
        """11V 보완 2 (Codex 재현): 이벤트만으로 10,000 -> 9,951 (계수 0.9951, 0.5% 안), 판정 창에 이전 18행·이후 3행,
        factor 중간 공백·양끝 비 0.9951, 장부 기록 없음"""
        c = [10000.0] * 27 + [9951.0, 9951.0, last]
        f = [0.9951] * 25 + [np.nan] * 3 + [1.0] * 2
        return series(c, f)

    def test_small_unexplained_gap_flips_trend_holds(self):
        df = self.small()
        self.assertTrue(calc(df)[2])                                             # 예전: 빈 곱 1 로 통과 -> 추세 매도 (MA20 9,992.65)
        self.assertAlmostEqual(df.close.iloc[-20:].mean(), 9992.65)
        self.assertFalse(calc(scaled(df, df.timestamp.iat[27], 0.9951))[2])      # 정확한 계수면 추세 매도 아님
        self.assertEqual(self.j(df)[0], "hold")
        self.assertNotIn("999999", self.res.judge(["999999"], "2026-10-26"))
        self.assertTrue(any(a.startswith("[판단 보류] 999999") for a in self.alerts))

    def test_small_unexplained_gap_far_from_ma_proceeds(self):
        for last in (10600.0, 9300.0):                                           # MA20 에서 멂 -> 앞·뒤 넣어도 같은 판단
            df = self.small(last)
            self.assertEqual(self.j(df), calc(df))                               # 기존 판단 그대로

    def two_gaps(self, f_mid=0.996, f_first=0.992016):
        """11V 보완 3 (Codex 재현, 21행): 종가 0~19행 10,000, 20행 9,964. factor 0~4행 f_first, 5행 빈칸, 6~14행 f_mid, 15행 빈칸, 16~20행 1"""
        f = [f_first] * 5 + [np.nan] + [f_mid] * 9 + [np.nan] + [1.0] * 5
        return series([10000.0] * 20 + [9964.0], f)

    def test_two_gaps_together_flip_holds(self):
        df = self.two_gaps()
        ts = [str(x.date()) for x in df.timestamp]
        self.assertAlmostEqual(df.close.iloc[-20:].mean(), 9998.2)
        self.assertTrue(calc(df)[2])
        for d in (ts[5], ts[6], ts[15], ts[16]):                                 # 한 공백만 넣으면 모두 추세 매도 (예전: 그래서 통과)
            self.assertTrue(calc(scaled(df, d, 0.996))[2])
        both = scaled(scaled(df, ts[5], 0.996), ts[15], 0.996)
        self.assertAlmostEqual(both.close.iloc[-20:].mean(), 9962.232)
        self.assertFalse(calc(both)[2])
        self.assertEqual(self.j(df)[0], "hold")
        self.assertNotIn("999999", self.res.judge(["999999"], "2026-10-26"))
        self.assertTrue(any(a.startswith("[판단 보류] 999999") for a in self.alerts))

    def test_gap_scenarios_match_all_combinations(self):
        """r<1·r>1 섞인 두 공백 (그리고 같은 방향 두 공백): 최소·최대 경우 = 앞/뒤 전체 조합(2^n)의 MA20 최소·최대"""
        import itertools
        for f_mid, f_first in ((0.996, 0.996 * 1.003), (0.996, 0.992016), (1.003, 1.003 * 1.004)):
            df = self.two_gaps(f_mid, f_first)
            ev, gaps = P.window_events(df)
            self.assertEqual(ev, {})
            tries = []
            for a, b, k in gaps:
                d0 = str(df.timestamp[df.timestamp > pd.Timestamp(a)].iat[0].date())
                tries.append((k, d0, b))
            self.assertEqual(len(tries), 2)
            lo, hi = P.gap_scenarios(df, {}, tries)
            mas = []
            for pick in itertools.product(*[(d0, d1) for _, d0, d1 in tries]):
                x = df
                for (r, _, _), d in zip(tries, pick):
                    x = scaled(x, d, r)
                mas.append(x.close.iloc[-20:].mean())
            self.assertAlmostEqual(P.raw_ma20(df, lo), min(mas), places=6)
            self.assertAlmostEqual(P.raw_ma20(df, hi), max(mas), places=6)

    def test_normal_day_same_as_before(self):
        c = np.linspace(10000, 9000, 30)
        df = series(c, 1.0)
        self.assertEqual(self.j(df), calc(df))
        P.raw_daily = lambda code: df.iloc[:-1]
        cv = {k: float(df[k].iat[-1]) for k in ("open", "high", "low", "close")}
        r = P.raw_judge("999999", "2026-10-26", cv, float(c[-2]))
        self.assertEqual((r[0], round(r[1], 6), r[2]), calc(df))
        nof = df.drop(columns="factor")                                          # factor 열 없는 이력도 같은 값
        self.assertEqual(self.j(nof), calc(nof))


if __name__ == "__main__":
    unittest.main()
