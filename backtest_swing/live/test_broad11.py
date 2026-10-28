"""
e6b7f4d Codex 확인 리뷰 반영 (11L 보완, 2026-10-28) 재현 시험. 실제 키움 클라이언트(kiwoom/client.py) 경로 + 가짜 서버(TR 별 간격 위반 = HTTP 429) + 가짜 시계.
실제 주문·조회·logs/exec·data 없음.

  python -m unittest backtest_swing/live/test_broad11.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import json
import sys
import time
import unittest
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from test_orders import PLAN, FakeClock, FakeKiwoom, sends  # noqa: E402
from test_codex import Base, bal_row  # noqa: E402
import common as C  # noqa: E402
import orders as O  # noqa: E402
import settings  # noqa: E402

LIMITS = json.load(open(settings.EXAMPLE, encoding="utf-8"))["rate_limits"]
PLAN3 = [dict(PLAN[0], code=c) for c in ("990001", "990002", "084670")]


def real_client(fk, clk, token_delay=0.0):
    """실제 KiwoomRestClient (kiwoom/client.py 의 request: 토큰 -> 대기 -> 전송 직전 확인 -> 전송) 뒤에 가짜 서버.
    가짜 서버의 None(간격 위반) = HTTP 429. token_delay: 토큰 준비에 걸리는 시간 (가짜 시계를 그만큼 보냄)"""
    import kiwoom.client as KC

    class Session:
        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            r = fk.request(tr_code=headers["api-id"], method=method, path=url, json=json, cont_yn=headers["cont-yn"], next_key=headers["next-key"])
            if r is None:
                return SimpleNamespace(status_code=429, ok=False, text="", headers={})
            return SimpleNamespace(status_code=200, ok=True, text="", json=r.json, headers=getattr(r, "headers", {}) or {})

    def token(**k):
        clk.t += pd.Timedelta(seconds=token_delay)
        return "t"
    c = KC.KiwoomRestClient(settings=SimpleNamespace(base_url="https://fake.test", env="mock"), auth=SimpleNamespace(get_token=token))
    c.session = Session()
    c.rate_limiter = FakeLimiter(clk, 1.0)                                         # 클라이언트 자기 제한기 (모의 1초 1회, 가짜 시계)
    return c


class FakeLimiter:
    """kiwoom.client.RateLimiter 와 같은 동작을 가짜 시계로"""

    def __init__(self, clk, interval):
        self.clk, self.interval, self.last = clk, interval, -1e18

    def wait(self, max_wait=None):
        now = self.clk().timestamp()
        w = self.interval - (now - self.last)
        if max_wait is not None and w > max_wait:
            return False
        if w > 0:
            self.clk.sleep(w)
        self.last = self.clk().timestamp()
        return True


class RealClientGap(unittest.TestCase):
    def setUp(self):
        self.clk = FakeClock("2026-10-28 15:21:00")
        self.fk = FakeKiwoom(self.clk)
        self.fk.min_gap = {"kt10000": 1.0, "kt00018": 1.0, "kt00001": 1.0}         # 키움 모의: TR 1개당 초당 1회 (어기면 429)
        self.fk.balance_rows = [bal_row("000660", 5)]
        C.RateGate.configure(LIMITS, clock=lambda: self.clk().timestamp(), sleep=self.clk.sleep)
        self.addCleanup(C.RateGate.configure, {}, time.time, time.sleep)

    def gaps(self, tr):
        ts = [pd.Timestamp(f"2026-10-28 {t}") for t, x, _ in self.fk.calls if x == tr]
        return [round((b - a).total_seconds(), 3) for a, b in zip(ts, ts[1:])]

    def test_account_then_three_buys_actual_send_gap_mock(self):
        """잔고 -> 예수금 -> 매수 3건 (10-28 15:21 순서) 을 실제 클라이언트 경로로: 실제 전송 간격 >= 1.2초, 429 없음.
        토큰 준비가 0.3초 걸려도 (전송을 늦추는 앞 단계) 간격은 실제 전송 시각 기준"""
        c = real_client(self.fk, self.clk, token_delay=0.3)
        b = O.Broker(c, allow_orders=True, env="mock")
        om = O.OrderManager(b, clock=self.clk, sleep=self.clk.sleep)
        a = b.account()
        om.place_buys(PLAN3, "본래", orderable=a["orderable"])
        self.assertEqual(len(sends(self.fk)), 3)
        self.assertTrue(all(g >= 1.2 for g in self.gaps("kt10000")), self.gaps("kt10000"))   # e6b7f4d: [1.0, 1.0] (공용 예약 뒤 클라이언트 제한기가 더 늦춤)
        self.assertEqual(self.fk.r429, [])
        self.assertIsNotNone(c.send_gate)                                          # 공용 제한기가 클라이언트의 전송 바로 앞 대기 (하나)
        self.assertEqual([i["status"] for i in om.records], [O.ACCEPTED] * 3)

    def test_client_limiter_not_used_when_gate_on(self):
        c = real_client(self.fk, self.clk)
        c.rate_limiter = SimpleNamespace(wait=lambda max_wait=None: (_ for _ in ()).throw(AssertionError("두 번째 대기 장치")))
        O.Broker(c, allow_orders=True, env="mock").balance()                     # 공용 제한기에 규칙이 있으면 클라이언트 제한기를 안 거침
        C.RateGate.configure({})
        used = []
        c.rate_limiter = SimpleNamespace(wait=lambda max_wait=None: used.append(1) or self.clk.sleep(1.0) or True)
        O.Broker(c, allow_orders=True, env="mock").balance()                     # 꺼져 있으면(도구·시험) 클라이언트 제한기 하나
        self.assertEqual(used, [1])

    def test_slow_wakeup_measured_from_actual_send(self):
        """예약보다 늦게 깨면(잠이 길어짐) 다음 간격은 실제 깬 시각부터"""
        late = []

        def sleep(s):
            self.clk.sleep(s + 0.5)                                                # 0.5초 늦게 깸
            late.append(s)
        C.RateGate.configure(LIMITS, clock=lambda: self.clk().timestamp(), sleep=sleep)
        c = real_client(self.fk, self.clk)
        b = O.Broker(c, env="mock")
        for _ in range(3):
            b.balance()
        self.assertTrue(all(g >= 1.2 for g in self.gaps("kt00018")), self.gaps("kt00018"))
        self.assertEqual(self.fk.r429, [])


class BuyRetryDeadline(unittest.TestCase):
    def first429(self, start):
        clk = FakeClock(start)

        class First429(FakeKiwoom):
            n = 0

            def request(self, tr_code, *a, **k):
                if tr_code == "kt10000":
                    First429.n += 1
                    if First429.n == 1:
                        self.calls.append((self.clock().strftime("%H:%M:%S.%f")[:-3], tr_code, {}))
                        return None
                    self.rate_wait = 2.0
                return super().request(tr_code, *a, **k)
        fk = First429(clk)
        om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep)
        om.place_buys(PLAN, "본래")
        return clk, fk, om

    def test_1527_429_retry_waits_past_1528_not_sent(self):
        """15:27:58 첫 429 -> 15:27:59 다시 시작 -> 유량 대기 2초 -> 15:28:01: 전송 직전 확인 = 15:28 전 -> 보내지 않음"""
        clk, fk, om = self.first429("2026-10-28 15:27:58")
        self.assertEqual([i["status"] for i in om.records], [O.NOT_SENT, O.NOT_SENT])
        self.assertEqual(om.records[-1]["not_sent_kind"], "deadline")
        self.assertEqual([t for t, x, _ in fk.calls if x == "kt10000"], ["15:27:58.000"])   # 15:28 뒤 전송 없음
        self.assertGreater(clk().strftime("%H:%M:%S"), "15:28:00")

    def test_1524_429_retry_accepted_before_1528(self):
        """15:24:58 첫 429 -> 15:25:01 접수 = 동시호가(15:30 전) 종가 체결, 마감(15:28 전) 안이라 정상 (11L 보완 2: 15:25 분기 없음)"""
        clk, fk, om = self.first429("2026-10-28 15:24:58")
        self.assertEqual([i["status"] for i in om.records], [O.NOT_SENT, O.ACCEPTED])
        self.assertEqual(om.records[-1]["order_time"][:8], "15:25:01")

    def test_first_send_window_unchanged(self):
        """유량 초과가 아닌 첫 전송·거부 1회 다시는 예전 창(15:28 전) 그대로"""
        for start, want in (("2026-10-28 15:24:59.500", [O.REJECTED, O.ACCEPTED]), ("2026-10-28 15:27:59.500", [O.REJECTED])):
            clk = FakeClock(start)
            fk = FakeKiwoom(clk)
            fk.script["kt10000"] = ["reject", "accept"]
            om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep)
            om.place_buys(PLAN, "본래")
            self.assertEqual([i["status"] for i in om.records], want, start)          # 거부 1초 뒤 다시도 15:28 전에만
            self.assertTrue(all(t < "15:28:00" for t, x, _ in fk.calls if x == "kt10000"))


class BuyAccountRetry(Base):
    def test_1521_account_fail_then_1522_success_buys(self):
        res = self.started("15:21:00")
        self.fk.read_script["kt00018"] = ["http502"]                               # 15:21 매수 전 잔고 조회 일시 실패
        res.tick(self.clk())
        self.assertEqual(sends(self.fk), [])
        self.assertTrue(res.day.done("buy"))
        self.assertTrue(res.day.s["buy_account_retry"]["pending"])
        self.assertNotIn("buy_deposit_fail", res.day.s.get("buy_block") or [])     # 영구 차단 아님
        self.assertTrue(any(a.startswith("[매수 전 계좌 조회 실패]") for a in self.alerts))
        self.clk.at("15:21:30")
        res.tick(self.clk())
        self.assertEqual(sends(self.fk), [])                                       # 같은 분에는 다시 안 함
        self.clk.at("15:22:00")
        res.tick(self.clk())
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk)], [PLAN[0]["code"]])   # 다음 분 조회 성공 -> 매수 접수
        self.assertEqual(res.day.s["buy_account_retry"]["pending"], False)
        self.assertTrue(res.day.s["buy_decision"]["new_buy_ok"])

    def test_fail_until_1528_then_blocked(self):
        res = self.started("15:21:00")
        self.fk.read_script["kt00018"] = ["http502"] * 20
        for m in range(21, 28):
            for sec in ("00", "20", "40"):                                         # 같은 분 여러 tick -> 한 번만 다시 조회
                self.clk.at(f"15:{m}:{sec}")
                res.tick(self.clk())
        self.assertEqual(res.day.s["buy_account_retry"]["tries"], 7)              # 15:21 ~ 15:27 분마다 한 번
        self.assertEqual(len([p for p in self.fk.pages if p[0] == "kt00018"]), 7)
        self.clk.at("15:28:00")
        res.tick(self.clk())
        self.assertIn("buy_deposit_fail", res.day.s["buy_block"])
        self.assertFalse(res.day.s["buy_decision"]["new_buy_ok"])
        self.assertEqual(sends(self.fk), [])

    def test_bad_deposit_response_retried_then_buys(self):
        """15:21 예수금 응답 이상(필수 필드 없음·금액 NaN = 조회 실패)은 마감 전 매 분 다시 조회, 정상 응답일 때만 매수 (11L 보완 2 (3))"""
        res = self.started("15:21:00")
        self.fk.read_script["kt00001"] = ["missing"]
        res.tick(self.clk())
        self.assertEqual(sends(self.fk), [])
        self.assertTrue(res.day.s["buy_account_retry"]["pending"])
        self.fk.read_script["kt00001"] = ["nan"]
        self.clk.at("15:22:00")
        res.tick(self.clk())
        self.assertEqual(sends(self.fk), [])                                       # 금액 NaN 으로는 매수 안 함
        self.clk.at("15:23:00")
        res.tick(self.clk())
        self.assertEqual(len(sends(self.fk)), 1)                                   # 정상 응답 -> 매수
        self.assertEqual(res.day.s["buy_account_retry"]["tries"], 2)

    def test_bad_deposit_response_until_1528_blocks(self):
        res = self.started("15:27:00")
        self.fk.read_script["kt00001"] = ["nan"] * 3
        res.tick(self.clk())
        self.clk.at("15:28:00")
        res.tick(self.clk())
        self.assertIn("buy_deposit_fail", res.day.s["buy_block"])
        self.assertEqual(sends(self.fk), [])


if __name__ == "__main__":
    unittest.main()
