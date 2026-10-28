"""
6efb549 Codex 확인 리뷰 반영 (11L 보완 2, 2026-10-28) 재현 시험: 공용 제한기 동시 대기 직렬화, 매수 전송 마감 하나(15:28 전).
실제 키움 클라이언트 경로·실제 스레드(동시 대기) / 가짜 서버·가짜 시계(마감 경계). 실제 주문·조회·logs/exec·data 없음.

  python -m unittest backtest_swing/live/test_broad12.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import collections
import json
import sys
import threading
import time
import unittest
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from test_orders import PLAN, FakeClock, FakeKiwoom, sends  # noqa: E402
from test_codex import Base  # noqa: E402
from test_broad11 import real_client  # noqa: E402
import common as C  # noqa: E402
import orders as O  # noqa: E402
import settings  # noqa: E402

LIMITS = json.load(open(settings.EXAMPLE, encoding="utf-8"))["rate_limits"]


class ConcurrentGate(unittest.TestCase):
    """1: 같은 제한 키를 두 스레드가 동시에 기다릴 때, 먼저 잠든 쪽이 늦게 깨도(잠 초과) 다음 쪽은 그 실제 통과 시각부터 간격을 잰다"""

    def tearDown(self):
        C.RateGate.configure({}, time.time, time.sleep)

    def caller(self, sent):
        import kiwoom.client as KC
        cl = KC.KiwoomRestClient(settings=SimpleNamespace(base_url="https://fake.test", env="prod"), auth=SimpleNamespace(get_token=lambda **k: "t"))
        cl.session = SimpleNamespace(request=lambda **kw: sent.append(time.time()) or SimpleNamespace(
            status_code=200, ok=True, text="", headers={}, json=lambda: {"return_code": 0, "atn_stk_infr": []}))
        cl.send_gate = C.RateGate.client_gate("prod", cl.rate_limiter)
        c = C.Caller.__new__(C.Caller)
        c.c, c.env, c.n, c.errors, c.hourly, c.fail_streak, c.stopped = cl, "prod", 0, [], collections.Counter(), 0, None
        return c

    def test_two_real_callers_two_threads_gap_at_least_limit(self):
        def oversleep(s):                                                          # 스레드 A 는 늘 0.2초 늦게 깸 (OS 스케줄 지연 흉내)
            time.sleep(s + (0.2 if threading.current_thread().name == "A" else 0.0))
        C.RateGate.configure({"prod": {"query": 0.25}}, clock=time.time, sleep=oversleep)
        sent = []
        a, b = self.caller(sent), self.caller(sent)

        def run(c, tr):
            for _ in range(5):
                c.post(tr, "/x", {})
        ts = [threading.Thread(target=run, args=(a, "ka10095"), name="A"), threading.Thread(target=run, args=(b, "ka10081"), name="B")]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        s = sorted(sent)
        gaps = [round(y - x, 3) for x, y in zip(s, s[1:])]
        self.assertEqual(len(s), 10)
        self.assertGreaterEqual(min(gaps), 0.245, gaps)                             # 6efb549: 0.05 안팎이 나옴


class Deadline1528(Base):
    """2: 매수 전송 마감 하나 = 15:28 전 (첫 전송·일반 거부 다시·429 다시·분 다시 보내기·15:21 계좌 재조회 모두)"""

    def times(self):
        return [t for t, x, _ in self.fk.calls if x == "kt10000"]

    def test_1524_429_exhausted_then_minute_resend_accepted_before_1528(self):
        n = {"k": 0}
        orig = self.fk.request

        def req(tr_code, *a, **k):
            if tr_code == "kt10000" and n["k"] < 1 + O.RATE_RETRIES:              # 15:24 에 429 4번 (다시 보내기 다 씀)
                n["k"] += 1
                self.fk.calls.append((self.clk().strftime("%H:%M:%S.%f")[:-3], tr_code, {}))
                return None
            return orig(tr_code, *a, **k)
        self.fk.request = req
        res = self.started("15:24:00")
        res.tick(self.clk())
        self.assertEqual([i["status"] for i in res.om.records], [O.NOT_SENT] * 4)
        self.clk.at("15:25:00")
        res.tick(self.clk())                                                       # 다음 분 다시 보내기 (15:25 분기 없음)
        acc = [i for i in res.om.records if i["status"] == O.ACCEPTED]
        self.assertEqual(len(acc), 1)
        self.assertTrue(all(t < "15:28:00" for t in self.times()), self.times())
        self.assertEqual(res.om.store.rejected_count("2026-10-26", PLAN[0]["code"], "buy"), 0)

    def test_late_429_minute_resend_stops_at_1528(self):
        orig = self.fk.request

        def req(tr_code, *a, **k):
            if tr_code == "kt10000":
                self.fk.calls.append((self.clk().strftime("%H:%M:%S.%f")[:-3], tr_code, {}))
                return None                                                        # 계속 429
            return orig(tr_code, *a, **k)
        self.fk.request = req
        res = self.started("15:26:30")
        for hms in ("15:26:30", "15:27:00", "15:27:40", "15:28:00", "15:29:00"):
            self.clk.at(hms)
            res.tick(self.clk())
        self.assertTrue(self.times() and all(t < "15:28:00" for t in self.times()), self.times())
        self.assertFalse([i for i in res.om.records if i["status"] == O.ACCEPTED])

    def test_first_buy_at_1526_is_sent(self):
        """늦게 시작한 매수 단계도 15:28 전이면 보냄 (6efb549: 15:25 뒤 첫 전송 안 함)"""
        res = self.started("15:26:00")
        res.tick(self.clk())
        self.assertEqual(len(sends(self.fk)), 1)
        self.assertEqual(res.om.records[0]["status"], O.ACCEPTED)


class TokenDelayFirstSend(unittest.TestCase):
    def test_token_delay_crosses_1528_not_sent(self):
        """첫 전송이 15:27:59.5 에 시작했지만 토큰 준비가 1초 -> 전송 직전 확인 15:28:00.5 -> 보내지 않음"""
        clk = FakeClock("2026-10-28 15:27:59.500")
        fk = FakeKiwoom(clk)
        om = O.OrderManager(O.Broker(real_client(fk, clk, token_delay=1.0), allow_orders=True, env="mock"), clock=clk, sleep=clk.sleep)
        om.place_buys(PLAN, "본래")
        self.assertEqual(sends(fk), [])
        self.assertEqual((om.records[0]["status"], om.records[0]["not_sent_kind"]), (O.NOT_SENT, "deadline"))

    def test_gate_wait_crosses_1528_second_buy_not_sent(self):
        clk = FakeClock("2026-10-28 15:27:59")
        fk = FakeKiwoom(clk)
        C.RateGate.configure(LIMITS, clock=lambda: clk().timestamp(), sleep=clk.sleep)
        self.addCleanup(C.RateGate.configure, {}, time.time, time.sleep)
        om = O.OrderManager(O.Broker(real_client(fk, clk), allow_orders=True, env="mock"), clock=clk, sleep=clk.sleep)
        om.place_buys([dict(PLAN[0], code=c) for c in ("990001", "990002")], "본래")
        self.assertEqual([t for t, x, _ in fk.calls if x == "kt10000"], ["15:27:59.000"])   # 두 번째는 1.2초 간격 뒤 = 15:28 지남 -> 안 보냄
        self.assertEqual([i["status"] for i in om.records], [O.ACCEPTED, O.NOT_SENT])


if __name__ == "__main__":
    unittest.main()
