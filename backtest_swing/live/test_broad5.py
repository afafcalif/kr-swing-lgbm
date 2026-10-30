"""
Codex 확인(27b70e7..577bcb3) 반영 (11J 보완 3 N1 ~ N3, 2026-10-27) 재현 시험.
가짜 서버·가짜 시계·가짜 토큰 서버, 실제 주문·조회·토큰 발급·logs/exec 없음.

  python -m unittest backtest_swing/live/test_broad5.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import os
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from test_codex import Base, bal_row, jl  # noqa: E402
from test_orders import FakeClock, Resp, sends, wait_for  # noqa: E402
import notify as N  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402

LIVE = os.path.dirname(os.path.abspath(__file__))
HELD = {"qty": 28, "trade_id": "990006-2026-10-20-1", "entry_date": "2026-10-20", "stop_price": 30000.0, "stop_temp": True,
        "stop_basis": "임시 (계좌 평균단가 - 1.7 x ATR20)", "kind": "본래", "judged_on": "2026-10-23", "judged_by": "15:30 1분봉"}
FIX = {"prev": "2026-10-23", "stops": {"990006": {"stop": 27000.0, "basis": "진입일 2026-10-20 공식 일봉 종가 - 1.7 x ATR20"}},
       "trend": {}, "left": [], "at": "08:58:00"}


def prep_out():
    pool = type("FakePool", (), {"close": lambda self: None, "shutdown": lambda self: None})
    return {"pool": pool(), "boosters": None, "cols": None, "index": {}, "ledgers": {}, "frozen": {"ok": True}, "amt20": {}, "excluded": {},
            "prep": {"model": {"check": {"ok": True}}, "ledger_version": 0,
                     "data": {"daily_up_to_date": True, "prev_trading_day": "2026-10-23", "data_version": "v"}},
            "recovery": {**FIX, "stops": {c: dict(v) for c, v in FIX["stops"].items()}}, "official_judge": {"990006": {"close": 31000.0, "stop": 29000.0, "trend": False}, "_missing": []}}


class TempStop(Base):
    """N1: 준비가 끝난 09:00 tick 은 공식 손절가 적용(계산 없음)이 새 손절 감지보다 먼저 -> 임시 손절가 기준 오매도 없음.
    임시 손절가로 만든 의무는 공식 손절가로 바로잡혔을 때 아직 안 보냈고 현재가 >= 공식 손절가면 해제"""

    def ready(self, hms, duty=None):
        pos = {"held": {"990006": dict(HELD)}, "sell_duty": {"990006": duty} if duty else {}, "next_open_sells": {}, "trade_seq": 1,
               "duty_format": P.DUTY_FORMAT}
        P.save(self.R.POSITIONS, pos)
        self.fk.balance_rows = [bal_row("990006", 28)]
        self.quotes["990006"] = 29000.0                                            # 임시 30000 아래, 공식 27000 위
        res = self.started(hms, live=False)
        res.day.set("prev_recovery", {"prev": "2026-10-23", "finalized": False})
        res.prep_thread, res.prep_result, res.prep_reason = object(), prep_out(), "하루 시작"
        jr = {"day": res.day.date, "recovery": res.prep_result["recovery"], "official_judge": res.prep_result["official_judge"]}
        res.judge_result = res.prep_result["judge"] = jr                           # 11K 보완 2 (2): 준비 결과와 따로 먼저 공개된 값
        res.judge = lambda *a, **k: (_ for _ in ()).throw(AssertionError("주 스레드에서 계산"))
        return res

    def test_0900_tick_with_prep_result_no_false_stop(self):
        res = self.ready("09:00:00")
        t0 = time.time()
        res.tick(self.clk())
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(sends(self.fk, "kt10001"), [])                           # 임시 손절가로 팔지 않음
        p = jl(self.R.POSITIONS)
        self.assertEqual((p["held"]["990006"]["stop_price"], p["held"]["990006"]["stop_temp"], p["sell_duty"]), (27000.0, False, {}))
        self.assertTrue(res.day.s["prev_recovery"]["finalized"])
        self.assertFalse([e for e in res.day.s.get("errors", []) if "계산" in str(e)])   # judge 를 주 스레드에서 부르지 않음
        self.assertEqual(res.day.s["official_vs_1530"]["checked"], 1)            # 공식 일봉 비교도 준비 스레드 값으로

    def test_temp_duty_released_only_if_not_sent(self):
        duty = O.new_duty("stop", "2026-10-26 09:00:00", "현재가 29000 < 손절가 30000", price=29000.0, stop=30000.0, stop_temp=True)
        res = self.ready("09:05:00", duty=dict(duty))
        res.apply_recovery_fixes(res.prep_result["recovery"])
        self.assertEqual(jl(self.R.POSITIONS)["sell_duty"], {})                  # 보낸 주문 없음 + 현재가 >= 공식 -> 해제
        self.assertTrue(any(a.startswith("[손절 의무 해제] 990006") for a in self.alerts))
        res2 = self.ready("09:06:00", duty=dict(duty))
        it = res2.om.store.add(date="2026-10-26", stage="stop", side="sell", code="990006", qty=28, price=None, trde_tp="3", attempt=1,
                               kind="본래", reason="손절", assumed_kind="stop")
        res2.om.store.update(it, status=O.ACCEPTED, ord_no="S1")
        res2.apply_recovery_fixes(res2.prep_result["recovery"])
        du = jl(self.R.POSITIONS)["sell_duty"]["990006"]                          # 보낸 주문은 그대로 -> 의무 유지
        self.assertEqual((du["stop"], du["stop_temp"]), (27000.0, False))
        self.assertIn("보낸 주문 있음", du["reason"])


def auth_mgr(tmp, expires_in):
    from kiwoom.auth import AuthManager
    st = SimpleNamespace(token_cache_path=__import__("pathlib").Path(tmp) / "token.json", base_url="https://fake.test",
                         auth_token_path="/oauth2/token", auth_token_method="POST", app_key="k", app_secret="s")
    a = AuthManager(st)
    a.cache.save({"access_token": "OLD", "expires_at": time.time() + expires_in})
    return a


class ExpiredToken(Base):
    """N2: 만료된 토큰은 다시 쓰지 않고 시도마다 재발급 (60초 억제 없음). 인증·토큰 거부는 NOT_SENT, 거부 횟수에 안 셈 -> 매 분 다시"""

    def test_expired_token_reissue_every_try(self):
        from kiwoom.client import KiwoomRestClient
        from kiwoom.errors import AuthError
        calls = []

        def post(url, headers=None, json=None, timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise requests.Timeout("token server down")
            return SimpleNamespace(ok=True, json=lambda: {"token": "NEW", "expires_dt": (pd.Timestamp.now() + pd.Timedelta(hours=24)).strftime("%Y%m%d%H%M%S")})
        c = KiwoomRestClient.__new__(KiwoomRestClient)
        c.auth, c.timeout, c.logger = auth_mgr(self.tmp, -60), 15, __import__("logging").getLogger("t")   # 1분 전에 만료
        c.settings = SimpleNamespace(base_url="https://fake.test")
        c.rate_limiter = SimpleNamespace(wait=lambda max_wait=None: True)
        sent = []
        c.session = SimpleNamespace(request=lambda **kw: sent.append(kw) or SimpleNamespace(status_code=200, ok=True))
        with mock.patch("requests.post", post):
            with self.assertRaises(AuthError):
                c.request("kt10000", "POST", "/api/dostk/ordr", json={})              # 첫 재발급 실패 -> 보내지 않음 (만료 토큰 안 씀)
            self.assertEqual(sent, [])
            c.request("kt10000", "POST", "/api/dostk/ordr", json={})                  # 바로 다시: 억제 없이 재발급
        self.assertEqual((len(calls), sent[0]["headers"]["Authorization"]), (2, "Bearer NEW"))
        r = O.classify(lambda: (_ for _ in ()).throw(AuthError("토큰 발급 실패")))
        self.assertEqual((r["outcome"], r["not_sent_kind"]), (O.NOT_SENT, "auth"))
        r = O.classify(lambda: Resp({"return_code": 3, "return_msg": "[8005:Token이 유효하지 않습니다]"}))
        self.assertEqual((r["outcome"], r["not_sent_kind"]), (O.NOT_SENT, "auth"))

    def test_buy_succeeds_1524_after_token_failures(self):
        from kiwoom.errors import AuthError
        res = self.started("15:21:05")
        real, n = self.fk.request, [0]

        def req(tr_code, method, path, json=None, **kw):
            if tr_code == "kt10000":
                n[0] += 1
                if n[0] in (1, 3):
                    raise AuthError("토큰 발급 실패 - 보내지 않음 (만료 토큰, 재발급 시간 초과)")
                if n[0] == 2:
                    return Resp({"return_code": 3, "return_msg": "[8005:Token이 유효하지 않습니다]"})
            return real(tr_code, method, path, json=json, **kw)
        self.fk.request = req
        for hms in ("15:21:05", "15:22:00", "15:23:00", "15:24:00", "15:25:00"):
            self.clk.at(hms)
            res.tick(self.clk())
        buys = [i for i in res.om.records if i["side"] == "buy"]
        self.assertEqual([i["status"] for i in buys], [O.NOT_SENT, O.NOT_SENT, O.NOT_SENT, O.ACCEPTED])
        self.assertEqual([i.get("not_sent_kind") for i in buys[:3]], ["auth"] * 3)
        self.assertEqual(buys[-1]["created"][:5], "15:24")                        # 15:24 매수 성공
        self.assertEqual(res.om.store.rejected_count("2026-10-26", "990006", "buy"), 0)   # 거부 횟수에 안 셈 (최대 2회 상한과 무관)
        self.assertEqual(len(sends(self.fk)), 1)
        self.assertEqual(sum(a.startswith("[주문 보내지 않음] 인증·토큰") for a in self.alerts), 3)


class LiveClock(FakeClock):
    """가짜 날짜·시각 + 실제로 흐른 시간 (잠금 기다림이 시각에 나타나게)"""

    def __init__(self, t):
        super().__init__(t)
        self.w0 = time.time()

    def __call__(self):
        return self.t + pd.Timedelta(seconds=time.time() - self.w0)

    def at(self, hms):
        super().at(hms)
        self.w0 = time.time()


class AlertLockHeld(Base):
    """N3: 다른 프로세스가 알림 잠금을 12초 잡고 있어도 매도·주문 경로는 기다리지 않음 (알림은 메모리 큐에만, 파일은 알림 파일 스레드가
    잠금을 잡았을 때만 씀)"""

    def test_lock_held_12s_duty_sell_before_152855(self):
        self.clk = LiveClock("2026-10-26 15:28:50")
        self.fk.clock = self.clk
        pend = self.R.ALERTS_PENDING
        self.R.alert = lambda m: (self.alerts.append(m), N.send_async(m, sender=lambda t: True, pending_path=pend))
        held = os.path.join(self.tmp, "held")
        code = (f"import os, sys, time; sys.path.insert(0, {LIVE!r}); import notify as N\n"
                f"with N._os_lock({pend!r}) as got:\n"
                f"    open({held!r}, 'w').close()\n"
                f"    time.sleep(12)\n")
        p = subprocess.Popen([sys.executable, "-c", code], cwd=self.tmp)
        self.assertTrue(wait_for(lambda: os.path.exists(held), timeout=60))     # 다른 프로세스가 잠금을 잡을 때까지
        duty = O.new_duty("stop", "2026-10-26 15:10:00", "현재가 29000 < 손절가 30000", price=29000.0, stop=30000.0)
        pos = {"held": {"990006": dict(HELD, stop_temp=False), "005930": dict(HELD, trade_id="005930-2026-10-20-2", stop_temp=False)},
               "sell_duty": {"990006": dict(duty), "005930": dict(duty)}, "next_open_sells": {}, "trade_seq": 2, "duty_format": P.DUTY_FORMAT}
        P.save(self.R.POSITIONS, pos)
        self.fk.balance_rows = [bal_row("990006", 28), bal_row("005930", 28)]
        self.fk.script["kt10001"] = ["reject", "accept"]                          # 첫 매도 거부 -> "[주문 거부]" 알림
        res = self.started("15:28:50", live=False)
        t0 = time.time()
        res.stop_minute(detect=False)
        self.assertLess(time.time() - t0, 3.0)                                    # 잠금을 기다리지 않음
        recs = [i for i in res.om.records if i["side"] == "sell"]
        self.assertEqual([i["status"] for i in recs], [O.REJECTED, O.ACCEPTED])
        self.assertLess(recs[-1]["created"][:8], "15:28:55")                      # 15:28:55 전에 나감
        self.assertTrue(any("[주문 거부]" in a for a in self.alerts))
        self.assertFalse(os.path.exists(pend))                                    # 잠금을 못 잡아 아직 안 씀 (잠금 없이 쓰지 않음)
        self.assertEqual(p.wait(60), 0)
        self.assertTrue(N.flush(15, pending_path=pend))                           # 잠금이 풀린 뒤 알림 파일 스레드가 씀 (보냈으니 지워짐)


if __name__ == "__main__":
    unittest.main(verbosity=2)
