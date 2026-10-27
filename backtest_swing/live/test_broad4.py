"""
Codex 확인(d4d6c64..27b70e7) 반영 (11J 보완 3 R1 ~ R4, 2026-10-27) 재현 시험.
가짜 서버·가짜 시계·가짜 토큰 서버, 실제 주문·조회·토큰 발급·logs/exec 없음.

  python -m unittest backtest_swing/live/test_broad4.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from test_codex import Base, bal_row, jl  # noqa: E402
from test_orders import FakeClock, FakeKiwoom, Resp, sends, wait_for  # noqa: E402
import common as C  # noqa: E402
import notify as N  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402

LIVE = os.path.dirname(os.path.abspath(__file__))
POS = {"084670": {"qty": 28, "trade_id": "084670-2026-10-20-1", "entry_date": "2026-10-20", "stop_price": 30000.0, "kind": "본래"}}
STOP = O.new_duty("stop", "2026-10-26 09:30:00", "현재가 29900 < 손절가 30000", price=29900.0, stop=30000.0)


def setup(t="2026-10-26 10:00:00"):
    clk = FakeClock(t)
    fk = FakeKiwoom(clk)
    om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep)
    return clk, fk, om


def positions(codes=("084670",), duty=STOP):
    held = {c: {**POS["084670"], "trade_id": f"{c}-2026-10-20-1"} for c in codes}
    return {"held": held, "sell_duty": {c: dict(duty) for c in codes}, "next_open_sells": {}, "trade_seq": 1, "duty_format": P.DUTY_FORMAT}


def cycle(om, fk, pos):
    return O.stop_cycle(om, O.AccountWatch(), pos, lambda cs, deadline=None: {}, lambda: om.b.balance(quick=True), om.clock(), set(), budget=10)


class MissingOnce(unittest.TestCase):
    """R1: 계좌 응답에서 종목이 한 번 빠져도 의무 유지 (기록만). 1분 이상 간격으로 두 번 연속 0 일 때만 끝"""

    def test_holding_missing_once_in_account_response(self):
        clk, fk, om = setup()
        fk.balance_rows = [bal_row("084670", 28, sellable=20)]
        pos = positions()
        cycle(om, fk, pos)                                                        # 20 접수 (8 은 다른 곳에 묶임)
        clk.at("10:01:00")
        row = fk.balance_rows.pop()                                               # 응답에서 한 번 빠짐 (실제 보유는 있음)
        r = cycle(om, fk, pos)
        self.assertEqual((r["duty_ended"], r["duty_zero"]), ([], ["084670"]))
        self.assertEqual(pos["sell_duty"]["084670"]["zero_at"], "2026-10-26 10:01:00")
        fk.balance_rows.append(row)
        fk.balance_rows[0]["trde_able_qty"] = "8"
        clk.at("10:02:00")
        r = cycle(om, fk, pos)                                                    # 다시 보임 -> 기록 지움, 남은 8 계속 매도
        self.assertNotIn("zero_at", pos["sell_duty"]["084670"])
        self.assertEqual([int(c[2]["ord_qty"]) for c in sends(fk, "kt10001")], [20, 8])

    def test_two_zeros_need_one_minute_gap(self):
        pos = positions()
        acct = {}
        self.assertEqual(O.end_duties(pos, acct, "2026-10-26 08:31:00"), ([], ["084670"]))
        self.assertEqual(O.end_duties(pos, acct, "2026-10-26 08:31:30"), ([], []))       # 30초 뒤 = 아직 유지
        self.assertEqual(O.end_duties(pos, acct, "2026-10-26 08:32:00"), (["084670"], []))
        self.assertEqual(pos["sell_duty"], {})


class PartialPages(unittest.TestCase):
    """R1: 계좌 조회가 여러 쪽이면 모든 쪽을 받은 응답만 성공 (한 쪽이라도 실패·다음 쪽 키 없음 = 실패, 보유 0 기록 안 함)"""

    def test_second_page_fails(self):
        clk, fk, om = setup()
        fk.balance_rows = [bal_row("000660", 5), bal_row("084670", 28)]
        fk.page_size = 1
        pos = positions()
        fk.read_script["kt00018"] = ["ok", "http502"]                             # 1쪽(000660)만 받고 2쪽 실패
        r = cycle(om, fk, pos)
        self.assertFalse(r["account_ok"])
        self.assertEqual((r["duty_zero"], r["duty_ended"]), ([], []))             # 084670 이 없는 것처럼 보이지 않음
        self.assertNotIn("zero_at", pos["sell_duty"]["084670"])
        self.assertEqual(sends(fk, "kt10001")[0][2]["ord_qty"], "28")             # 계좌 조회 실패 규칙 (걸린 매도 없음 -> 보유 파일)

    def test_cont_yn_without_next_key_is_failure(self):
        class Client:
            def request(self, **kw):
                return Resp({"return_code": 0, "acnt_evlt_remn_indv_tot": [bal_row("000660", 5)], "prsm_dpst_aset_amt": "0"},
                            headers={"cont-yn": "Y", "next-key": ""})
        with self.assertRaises(O.QueryError):
            O.Broker(Client()).balance(quick=True)


class OldListNewTrade(Base):
    """R2: trade_id 없는 옛 형식(d4d6c64) 목록 항목은 날짜가 지금 보유 거래의 진입일보다 뒤일 때만 의무. 아니면 사람 확인 알림"""

    def test_d4d6c64_list_with_new_holding(self):
        old = {"held": {"084670": {"qty": 28, "trade_id": "084670-2026-10-23-5", "entry_date": "2026-10-23", "stop_price": 1.0},   # 10-23 다시 산 새 거래
                        "000660": {"qty": 5, "trade_id": "000660-2026-10-20-2", "entry_date": "2026-10-20", "stop_price": 1.0},
                        "005930": {"qty": 10, "trade_id": "005930-2026-10-20-3", "entry_date": "2026-10-20", "stop_price": 1.0}},
               "next_open_sells": {"084670": {"reason": "2026-10-23 손절 남음", "since": "2026-10-23", "attempts": 0},   # 이전 거래 것일 수 있음
                                   "000660": {"reason": "추세 매도", "since": "2026-10-23", "attempts": 0},          # 진입일 뒤 = 지금 거래
                                   "005930": {"reason": "예전 형식에서 옮김", "since": None, "attempts": 0}},         # 날짜 없음
               "trade_seq": 5}
        C.atomic_json(self.R.POSITIONS, old)
        self.fk.balance_rows = [bal_row("084670", 28), bal_row("000660", 5), bal_row("005930", 10)]
        res = self.started("08:31:00", live=False)
        pos = jl(self.R.POSITIONS)
        self.assertEqual(list(pos["sell_duty"]), ["000660"])
        self.assertEqual(list(pos["next_open_sells"]), ["000660"])
        need = [a for a in self.alerts if a.startswith("[매도 의무 확인 필요]")]
        self.assertEqual(len(need), 2)
        self.assertTrue(any("084670" in a and "보유 파일 28주" in a and "계좌 28주" in a and "2026-10-23 손절 남음" in a for a in need))
        self.assertTrue(any("005930" in a for a in need))
        self.assertEqual(sorted(res.day.s["duty_review"]["items"]), ["005930", "084670"])
        res.trend_sell()
        self.assertEqual([c[2]["stk_cd"] for c in sends(self.fk, "kt10001")], ["000660"])   # 새 거래 084670 은 팔지 않음


class StopFirst(Base):
    """R3: 손절 감시 시간대에는 손절·의무 매도를 그 분의 다른 작업(UNKNOWN 대조 등)보다 먼저"""

    def test_152850_slow_unknown_reconcile_after_stop(self):
        P.save(self.R.POSITIONS, positions())
        self.fk.balance_rows = [bal_row("084670", 28)]
        res = self.started("15:28:50", live=False)
        it = res.om.store.add(date="2026-10-26", stage="buy", side="buy", code="005930", qty=3, price=60000, trde_tp="0", attempt=1,
                              kind="본래", reason="매수 (P4)", assumed_kind="buy")
        res.om.store.update(it, status=O.UNKNOWN, checks_due=["2026-10-26 15:28:00"])   # 대조 예정 시각 지남
        real, order = self.fk.request, []

        def req(tr_code, method, path, json=None, **kw):
            order.append(tr_code)
            if tr_code == "ka10075":
                self.clk.t += pd.Timedelta(seconds=15)                            # 미체결 조회 15초 지연
            return real(tr_code, method, path, json=json, **kw)
        self.fk.request = req
        res.tick(self.clk())
        s = sends(self.fk, "kt10001")
        self.assertEqual((len(s), s[0][0][:8]), (1, "15:28:50"))                  # 15:29 전에 나감
        self.assertLess(order.index("kt10001"), order.index("ka10075"))           # 대조보다 먼저
        self.assertEqual(res.om.records[-1]["status"], O.ACCEPTED)


def auth_mgr(tmp, expires_in):
    from kiwoom.auth import AuthManager
    st = SimpleNamespace(token_cache_path=__import__("pathlib").Path(tmp) / "token.json", base_url="https://fake.test",
                         auth_token_path="/oauth2/token", auth_token_method="POST", app_key="k", app_secret="s")
    a = AuthManager(st)
    a.cache.save({"access_token": "OLD", "expires_at": time.time() + expires_in})
    return a


class SlowToken(Base):
    """R3: 토큰은 만료 30분 전 미리 갱신, 손절 루프 안 발급은 5초 제한, 실패하면 기존 토큰. 08:20·15:10 갱신 확인"""

    def setUp(self):
        super().setUp()
        self.calls = []

        def post(url, headers=None, json=None, timeout=None):                     # 토큰 서버가 30초 걸림
            self.calls.append(timeout)
            if timeout < 30:
                raise requests.Timeout(f"read timeout ({timeout}초)")
            return SimpleNamespace(ok=True, json=lambda: {"token": "NEW", "expires_dt": (pd.Timestamp.now() + pd.Timedelta(hours=24)).strftime("%Y%m%d%H%M%S")})
        p = mock.patch("requests.post", post)
        p.start()
        self.addCleanup(p.stop)

    def test_token_issue_30s_delay_in_stop_loop_uses_old_token(self):
        from kiwoom.client import KiwoomRestClient
        a = auth_mgr(self.tmp, 600)                                               # 10분 남음 = 미리 갱신 구간
        c = KiwoomRestClient.__new__(KiwoomRestClient)
        c.auth, c.timeout, c.logger = a, 15, __import__("logging").getLogger("t")
        c.settings = SimpleNamespace(base_url="https://fake.test")
        c.rate_limiter = SimpleNamespace(wait=lambda max_wait=None: True)
        sent = []
        c.session = SimpleNamespace(request=lambda **kw: sent.append(kw) or SimpleNamespace(status_code=200, ok=True))
        c.request("kt00018", "POST", "/api/dostk/acnt", json={}, timeout=5)         # 손절 루프 안 (5초)
        self.assertEqual(self.calls, [5])                                          # 발급 5초 제한 -> 실패
        self.assertEqual((sent[0]["headers"]["Authorization"], sent[0]["timeout"]), ("Bearer OLD", 5))   # 기존 토큰으로 시도
        c.request("kt00018", "POST", "/api/dostk/acnt", json={}, timeout=5)
        self.assertEqual(self.calls, [5])                                          # 실패 직후 1분은 다시 발급 안 함 (요청마다 기다리지 않음)
        a._next_try = 0
        self.assertEqual(a.get_token(), "NEW")                                     # 손절 루프 밖: 30초 제한 -> 갱신
        self.assertEqual(self.calls, [5, 30])

    def test_token_check_0820_via_tick(self):
        res = self.started("08:20:00", live=False)
        self.fk.auth = auth_mgr(self.tmp, 3 * 3600)                                # 3시간 남음: 15:40 + 30분까지 못 감 -> 갱신
        res.tick(self.clk())
        rec = res.day.s["token_check_0820"]
        self.assertTrue(rec["order"]["ok"])
        self.assertGreater(rec["order"]["left_min"], 23 * 60)
        self.assertEqual(self.calls, [30])
        self.clk.at("08:21:00")
        res.tick(self.clk())
        self.assertEqual(self.calls, [30])                                         # 하루 한 번
        self.clk.at("15:10:00")
        self.fk.auth.cache.save({"access_token": "X", "expires_at": time.time() + 600})   # 15:45 + 30분 전에 만료
        res.tick(self.clk())
        rec = res.day.s["token_check_1510"]
        self.assertEqual((self.calls[-1], rec["order"]["ok"]), (5, False))        # 손절 감시 시간대 = 5초, 실패 -> 기존 토큰 + 알림
        self.assertTrue(any(a.startswith("[토큰 갱신 확인 실패] 1510") for a in self.alerts))


class OverlapExit(unittest.TestCase):
    """R4: 종료 중인 이전 실행과 새로 시작한 실행이 미전송 파일을 동시에 고쳐도 (프로세스 사이 잠금 + 다시 읽어 합침) 기록이 사라지지 않음"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pend = os.path.join(self.tmp, "pending.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exit_and_restart_overlap(self):
        C.atomic_json(self.pend, {f"old#{i}": {"time": "2026-10-26 15:00:00", "text": f"o{i}"} for i in range(20)})
        go, ready = os.path.join(self.tmp, "go"), os.path.join(self.tmp, "ready")
        code = (f"import os, sys, time; sys.path.insert(0, {LIVE!r}); import notify as N\n"
                f"open({ready!r}, 'w').close()\n"
                f"while not os.path.exists({go!r}): time.sleep(0.01)\n"
                f"for i in range(20):\n"
                f"    N._pending_edit({self.pend!r}, add={{f'exit#{{i}}': {{'time': 't', 'text': 'x'}}}}, drop=[f'old#{{i}}'] if i % 2 == 0 else [])\n")
        p = subprocess.Popen([sys.executable, "-c", code], cwd=self.tmp)          # 종료 중인 이전 실행: 보낸 번호 지우고 못 보낸 번호 넣음
        self.assertTrue(wait_for(lambda: os.path.exists(ready), timeout=60))   # 이전 실행이 준비될 때까지 (고정 대기 아님)
        open(go, "w").close()
        for i in range(20):                                                        # 새 실행: 시작 알림을 넣고 복원한 번호를 지움
            N._pending_edit(self.pend, add={f"new#{i}": {"time": "t", "text": "n"}}, drop=[f"old#{i}"] if i % 4 == 1 else [])
        self.assertEqual(p.wait(60), 0)
        got = set(jl(self.pend))
        want = {f"old#{i}" for i in range(20) if i % 2 and i % 4 != 1} | {f"exit#{i}" for i in range(20)} | {f"new#{i}" for i in range(20)}
        self.assertEqual(got, want)

    def test_lock_excludes_other_process(self):
        code = (f"import sys, time; sys.path.insert(0, {LIVE!r}); import notify as N\n"
                f"t = time.time()\n"
                f"with N._os_lock({self.pend!r}, wait=0.3) as got: print(int(got), round(time.time() - t, 1))\n")
        with N._os_lock(self.pend) as got:
            self.assertTrue(got)
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=self.tmp).stdout.split()
        self.assertEqual(out[0], "0")                                              # 다른 프로세스는 잠금을 못 잡음 (기다림 상한 뒤 포기)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=self.tmp).stdout.split()
        self.assertEqual(out[0], "1")                                              # 풀린 뒤에는 잡음


if __name__ == "__main__":
    unittest.main(verbosity=2)
