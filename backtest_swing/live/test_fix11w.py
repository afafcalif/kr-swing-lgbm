"""
11W 시험 (이번 주 수정 묶음).
A 텔레그램 조회 명령 (조회 전용): 내 대화방만, 상태 바꾸는 명령 없음, 키움 새 호출 없음, 15:17 ~ 15:22 답 미룸, 토큰 로그 안 남김, 실패 격리.
B 기록·알림 정리: 1 총자산 변화·실현손익 / 2 같은 오류 모든 발생 해결 / 3 공식 시세 기다리는 동안 INFO / 4 공식 시세 첫 시도 08:05·KRX 대조 08:20 /
  5 같은 실패 종목만 남은 재수집 INFO / 6 [공식 시세 반영] 날짜 = 반영된 일봉 / 7 감시 작업 밀린 알림 원자적 저장.
가짜 서버·가짜 시계·임시 폴더, 실제 키움·KRX·텔레그램 호출·data·logs 없음.

  python -m unittest backtest_swing/live/test_fix11w.py -v
"""
import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import testguard; testguard.install()  # noqa: E401,E402,E702  (첫 줄 유지)
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"
import html
import json
import sys
import threading
import time
import unittest

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_codex import Base, bal_row  # noqa: E402
import positions as P  # noqa: E402
import quotes as Q  # noqa: E402
import telecmd as TC  # noqa: E402

TOKEN, CHAT = "1234" + "56789:" + "AA" + "x" * 30, "555"  # 가짜 (파일에 토큰 모양 글자를 남기지 않게 이어 붙임)
POS = {"990010": {"qty": 28, "trade_id": "990010-2026-10-20-1", "entry_date": "2026-10-20", "stop_price": 30000.0, "kind": "본래",
                  "avg_price": 31000.0, "entry_price": 31000.0},
       "005930": {"qty": 10, "trade_id": "005930-2026-10-21-2", "entry_date": "2026-10-21", "stop_price": 50000.0, "kind": "점검용",
                  "avg_price": 52000.0}}


class FakeTg:
    """가짜 텔레그램: getUpdates 는 넣어 둔 목록을 한 번씩, sendMessage 는 기록. message.date 는 진짜 텔레그램처럼 epoch"""

    def __init__(self, clk):
        self.clk, self.updates, self.sent, self.uid = clk, [], [], 0

    def epoch(self):
        return self.clk().tz_localize("Asia/Seoul").timestamp()                 # 가짜 한국 시계의 진짜 epoch

    def msg(self, text, chat=CHAT, age_s=0):
        self.uid += 1
        self.updates.append({"update_id": self.uid, "message": {"chat": {"id": int(chat)}, "text": text,
                                                                 "date": int(self.epoch() - age_s)}})

    def get(self, method, params, timeout):
        out, self.updates = self.updates, []
        return {"ok": True, "result": [u for u in out if params.get("offset") is None or u["update_id"] >= params["offset"]]}

    def post(self, method, data):
        t = data["text"]
        if data.get("parse_mode") == "HTML":                                   # 11W M: 고정폭(<pre>)으로 보냄 -> 보이는 글로 기록
            t = html.unescape(t[len("<pre>"):-len("</pre>")])
        self.sent.append((data["chat_id"], t))
        return {"ok": True}


class BotBase(Base):
    def setUp(self):
        super().setUp()
        self.logs = []
        self.tg = FakeTg(self.clk)

    def bot(self, view):
        return TC.Bot(TOKEN, CHAT, view, lambda m, lv=None: self.logs.append((lv, m)), clock=self.clk, epoch=self.tg.epoch,
                      http_get=self.tg.get, http_post=self.tg.post, sleep=self.clk.sleep)


class Commands(BotBase):
    def started_with_holdings(self):
        P.save(self.R.POSITIONS, {"held": json.loads(json.dumps(POS)), "next_open_sells": {}, "trade_seq": 2})
        self.fk.balance_rows = [bal_row("990010", 28, avg=31000, sellable=28), bal_row("005930", 10, avg=52000, sellable=10)]
        self.prev_state(steps=("after_close",), settlement={"d2": 30_000_000.0, "equity": 31_388_000.0})
        res = self.started("10:00:00", live=False)
        self.quotes.update({"990010": 33000.0, "005930": 51000.0})
        res.stop_minute()                                                       # 매 분 손절 감시가 받은 시세·계좌를 남김
        return res

    def test_only_my_chat_and_help(self):
        res = self.started_with_holdings()
        b = self.bot(res.query_view)
        self.tg.msg("/holdings", chat="999")                                    # 다른 대화방: 무시·로그만
        self.tg.msg("안녕")                                                      # 그 밖의 글 -> /help
        self.tg.msg("/nope@MyBot")
        b.poll_once()
        self.assertEqual([c for c, _ in self.tg.sent], [CHAT, CHAT])
        self.assertTrue(all(t == TC.HELP for _, t in self.tg.sent))
        self.assertTrue(any("다른 대화방" in m and "999" in m for _, m in self.logs))

    def test_holdings_today_status_pnl_from_saved_values_no_kiwoom(self):
        res = self.started_with_holdings()
        self.filled_sell(res.om, code="005930", qty=10, price=51000, stage="stop")  # 오늘 손절 체결
        n_calls, n_snap = len(self.fk.calls), [0]
        real = Q.snapshot
        Q.snapshot = lambda *a, **k: (n_snap.__setitem__(0, n_snap[0] + 1), real(*a, **k))[1]
        self.addCleanup(setattr, Q, "snapshot", real)
        self.clk.at("10:00:30")
        v0 = jl_version(self.R.POSITIONS)
        b = self.bot(res.query_view)
        for c in ("/holdings", "/today", "/status", "/pnl", "/help"):
            self.tg.msg(c)
        b.poll_once()
        self.assertEqual((len(self.fk.calls), n_snap[0]), (n_calls, 0))         # 키움 새 호출 없음 (시세·계좌·주문 모두)
        h, td, st, pn, hp = [t for _, t in self.tg.sent]
        self.assertIn("990010", h)
        self.assertIn("28주 · 평균 31,000 → 현재 33,000 (+6.5%)\n  손절가 30,000까지 -9.1%\n  진입 10-20 · 전략", h)   # 11W M 형식
        held_part, sold_part = h.split("[오늘 매도]")
        self.assertNotIn("005930", held_part)                                   # 11W M: 오늘 판 종목은 보유에서 뺌 (예전: 장부 10주로 남음)
        self.assertIn("005930", sold_part)
        self.assertIn("10주 · 손절 · 점검용", sold_part)
        self.assertIn("손절 전략\n", td)
        self.assertIn(" 005930 10주 시장가\n", td)
        self.assertIn("체결 10주 @51,000", td)
        self.assertIn("하트비트", st)
        self.assertIn("데이터 버전 v1", st)
        self.assertIn("수동 확인 없음", st)
        # 실현손익 = 10 x (51,000 - 52,000) - 매도 수수료·세금 - 매수 수수료 (모의 0.35%, 10원 절사)
        rate, tax = self.R.commission_rate(), self.R.sb_tax(2026)
        exp = 510000 - 520000 - P.kiwoom_fee(510000, rate) - P.kiwoom_tax(510000, tax) - P.kiwoom_fee(520000, rate)
        line = lambda head: [x for x in pn.split("\n") if x.strip().startswith(head)][0].split()  # noqa: E731
        self.assertEqual(line("오늘 실현")[2], f"{exp:+,.0f}")
        # 총자산 = 전 거래일 D+2 + 오늘 체결(매도) - 비용 + 보유 990010 28 x 33,000 (11W M: 오늘 판 005930 은 주식에서 빠지고 대금이 현금 쪽)
        cash = P.expected_cash(30_000_000.0, [r for r in res.om.records if r.get("filled_qty")], 2026, commission=rate)[0]
        eq = cash + 28 * 33000
        self.assertEqual(line("총자산")[-1], f"{eq:,.0f}")
        self.assertEqual(line("오늘")[-1], f"{eq - 31_388_000:+,.0f}")
        self.assertEqual(line("현금")[1:], [f"{cash:,.0f}"])                    # 체결 확인 끝 -> 추정 아님
        self.assertEqual(hp, TC.HELP)
        self.assertEqual(jl_version(self.R.POSITIONS), v0)                       # 장부 저장 없음 (조회만)

    def test_hold_window_1517_1522(self):
        res = self.started_with_holdings()
        self.clk.at("15:18:10")
        b = self.bot(res.query_view)
        self.tg.msg("/status")
        b.poll_once()                                                           # 가짜 sleep 이 시계를 밀어 15:22 까지 기다림
        self.assertEqual(len(self.tg.sent), 1)
        self.assertGreaterEqual(self.clk().strftime("%H:%M:%S"), "15:22:00")
        self.assertIn("15:18:10 요청", self.tg.sent[0][1])
        self.assertIn("[상태] 10-26 15:22", self.tg.sent[0][1])

    def test_old_message_ignored(self):
        res = self.started_with_holdings()
        b = self.bot(res.query_view)
        self.tg.msg("/pnl", age_s=3600)
        b.poll_once()
        self.assertEqual(self.tg.sent, [])

    def test_view_is_copy_and_state_unchanged(self):
        res = self.started_with_holdings()
        before = json.dumps(res.day.s, sort_keys=True, default=str)
        v = res.query_view()
        v["day"]["errors"].append("x")
        v["view"]["quotes"].clear()
        self.assertEqual(json.dumps(res.day.s, sort_keys=True, default=str), before)
        self.assertIn("990010", res.view["quotes"])


class MessageAgeEpoch(unittest.TestCase):
    """메시지 나이는 epoch 끼리 (서브 PC 실측: 방금 보낸 /holdings·/status 도 '540분 지난 메시지 답 안 함').
    운영과 같은 경로: 실제 한국 시계 + 실제 time.time(), date = 진짜 epoch (시스템 시간대 Asia/Seoul: tzset 있으면 맞추고, Windows 는 PC 설정)"""

    def setUp(self):
        self.logs, self.sent = [], []
        if hasattr(time, "tzset"):
            saved = os.environ.get("TZ")
            os.environ["TZ"] = "Asia/Seoul"
            time.tzset()

            def back():
                os.environ.pop("TZ", None) if saved is None else os.environ.__setitem__("TZ", saved)
                time.tzset()
            self.addCleanup(back)

    def ask(self, text, age_s):
        ups = [{"update_id": 1, "message": {"chat": {"id": int(CHAT)}, "text": text, "date": int(time.time() - age_s)}}]
        view = {"now": "2026-11-09 10:00:00", "today": "2026-11-09", "positions": {"held": {}}}
        b = TC.Bot(TOKEN, CHAT, lambda: view, lambda m, lv=None: self.logs.append(m), hold=lambda: ("00:00:00", "00:00:00"),
                   clock=lambda: pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None),
                   http_get=lambda *a: {"ok": True, "result": ups}, http_post=lambda m, d: self.sent.append(d["text"]) or {"ok": True})
        b.poll_once()

    def test_just_sent_is_answered(self):
        self.ask("/holdings", 0)
        self.ask("/status", 0)
        self.assertEqual([t.replace("<pre>", "").split()[0] for t in self.sent], ["[보유]", "[상태]"])      # 예전: 둘 다 답 없음
        self.assertFalse(any("지난 메시지" in m for m in self.logs))

    def test_nine_minutes_is_answered(self):
        self.ask("/status", 540)
        self.assertEqual(len(self.sent), 1)

    def test_eleven_minutes_not_answered_and_log_says_11(self):
        self.ask("/status", 660)
        self.assertEqual(self.sent, [])
        self.assertTrue(any("11분 지난 메시지 답 안 함" in m for m in self.logs), self.logs)  # 예전: 551분


class Isolation(BotBase):
    def test_failure_logged_without_token_and_thread_keeps_going(self):
        n = [0]

        def bad_get(method, params, timeout):
            n[0] += 1
            if n[0] >= 3:
                b.stop.set()
            raise ConnectionError(f"HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded with url: /bot{TOKEN}/getUpdates?offset=1")
        b = TC.Bot(TOKEN, CHAT, lambda: {}, lambda m, lv=None: self.logs.append((lv, m)), clock=self.clk, http_get=bad_get, http_post=self.tg.post)
        b.stop.wait = lambda s=None: b.stop.is_set()                             # 쉬기 없이
        b.loop()                                                                 # 예외가 밖으로 나오지 않음
        self.assertEqual(n[0], 3)
        text = " ".join(m for _, m in self.logs)
        self.assertIn("수신 실패", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(TOKEN.split(":")[1], text)

    def test_view_error_answered_and_scrubbed(self):
        def view():
            raise RuntimeError(f"bad {TOKEN}")
        b = self.bot(view)
        self.tg.msg("/pnl")
        b.poll_once()
        self.assertIn("[조회 오류]", self.tg.sent[0][1])
        self.assertNotIn(TOKEN, self.tg.sent[0][1] + " ".join(m for _, m in self.logs))

    def test_start_bot_needs_env_and_runs_in_own_thread(self):
        res = self.resident()
        saved = self.R.N._env
        self.addCleanup(setattr, self.R.N, "_env", saved)
        self.R.N._env = lambda: ("", "")
        self.assertIsNone(res.start_bot())
        self.R.N._env = lambda: (TOKEN, CHAT)
        started = []
        real = TC.Bot.start
        TC.Bot.start = lambda self_: started.append(threading.current_thread().name)
        self.addCleanup(setattr, TC.Bot, "start", real)
        b = res.start_bot()
        self.assertEqual((b.chat, len(started)), (CHAT, 1))
        self.assertEqual(b.hold(), ("15:17:00", "15:22:00"))


class Realized(unittest.TestCase):
    def test_realized_pnl_entry_and_fees(self):
        recs = [{"side": "sell", "code": "A", "filled_qty": 10, "fill_price": 1100}, {"side": "buy", "code": "B", "filled_qty": 5, "fill_price": 10},
                {"side": "sell", "code": "C", "filled_qty": 3, "fill_price": 500}]
        r = P.realized_pnl(recs, {"A": {"entry_price": 1000, "avg_price": 999}}, {}, 0.0035, 2026)
        tax = P.sb.SELL_TAX.get(2026, 0.002)
        self.assertEqual(r["pnl"], round(11000 - 10000 - P.kiwoom_fee(11000, 0.0035) - P.kiwoom_tax(11000, tax) - P.kiwoom_fee(10000, 0.0035), 1))
        self.assertEqual((r["n"], r["unknown"]), (1, ["C"]))
        r2 = P.realized_pnl(recs[:1], {}, {"A": {"avg_price": 1000}}, 0.0035, 2026)   # 장부에 없으면 그날 정산 기준 장부
        self.assertEqual(r2["pnl"], r["pnl"])


# ---------------------------------------------------------------- B

def prep_out(up=True, miss=()):
    class Pool:
        def close(self):
            pass
    return {"pool": Pool(), "boosters": [], "cols": [], "index": {}, "ledgers": {"strategy": [], "full": []}, "frozen": {"ok": True},
            "amt20": {"000001": 5e9}, "excluded": {"fetch_error": []},
            "prep": {"model": {"check": {"ok": True}}, "ledger_as_of": "2026-10-23", "ledger_version": 3,
                     "data": {"daily_up_to_date": up, "data_version": "v2", "prev_trading_day": "2026-10-23", "official_missing_dates": list(miss),
                              "expected_prev_trading_day": "2026-10-23", "data_last_date": "2026-10-23" if up else "2026-10-22"}},
            "data": {"daily_update": "ok", "official_missing_dates": list(miss)}}


class LogMixin:
    def rlines(self):
        import glob
        out = []
        for f in glob.glob(os.path.join(self.R.LOG_DIR, "resident", "*.log")):
            with open(f, encoding="utf-8") as fh:
                out += fh.read().splitlines()
        return out


class RecordsB(LogMixin, Base):
    def test_b1_summary_total_change_and_realized(self):
        P.save(self.R.POSITIONS, {"held": json.loads(json.dumps(POS)), "next_open_sells": {}, "trade_seq": 2})
        self.fk.balance_rows = [bal_row("005930", 10, avg=52000, sellable=10)]       # 990010 다 팔림
        self.prev_state(steps=("after_close",), settlement={"d2": 30_000_000.0, "equity": 31_388_000.0})
        res = self.started("15:40:10", live=False)
        self.filled_sell(res.om, code="990010", qty=28, price=33000, stage="trend_sell")
        res.settle()
        st = res.day.s["settlement"]
        rate, tax = self.R.commission_rate(), self.R.sb_tax(2026)
        exp = 28 * 33000 - 28 * 31000 - P.kiwoom_fee(28 * 33000, rate) - P.kiwoom_tax(28 * 33000, tax) - P.kiwoom_fee(28 * 31000, rate)
        self.assertEqual(st["realized"]["pnl"], round(exp, 1))
        t = self.R.pnl_text(st["pnl"], st["realized"])
        self.assertIn("총자산 변화 ", t)                                          # 예전: "손익" (실제로는 총자산 하루 변화)
        self.assertIn(f"실현손익 {exp:+,.0f}", t)
        self.assertNotIn("손익 ", t.replace("실현손익 ", ""))

    def test_b2_all_occurrences_resolved(self):
        res = self.started("07:20:00")
        res.prep_ok, res.ctx = False, {}
        res.prep_result, res.prep_reason = {"error": "ReadTimeout('data-dbg.krx.co.kr read timeout=30')"}, "하루 시작"
        res.apply_prep()                                                         # 첫 준비 실패 (키 있음)
        res.prep_result = prep_out()
        res.apply_prep()
        res.prep_ok = False
        res.prep_result, res.prep_reason = {"error": "ReadTimeout('data-dbg.krx.co.kr read timeout=31')"}, "공식 시세 반영 뒤 다시 적재"
        res.apply_prep()                                                         # 다시 적재 실패 (예전: 키 없음)
        for n in (30, 31):
            res.apply_official({"time": "08:05:00", "error": f"ReadTimeout('data-dbg.krx.co.kr read timeout={n}')"})  # 예전: 키 없음
        self.assertGreaterEqual(res.day.open_error_count(), 3)
        res.prep_result = prep_out()
        res.apply_prep()
        self.assertEqual(res.day.open_error_count(), 0, res.day.s["errors"])     # 예전: 해결은 키 있는 첫 준비 실패 하나
        self.assertEqual(len(res.day.s["errors_resolved"]), len(res.day.s["errors"]))

    def test_b3_waiting_official_is_info_until_deadline(self):
        res = self.started("07:20:00")
        res.prep_ok, res.ctx = False, {}
        res.prep_result = prep_out(up=False, miss=["2026-10-23"])
        res.apply_prep()
        self.assertEqual(res.day.s["errors"], [])                                 # 예전: "전날 일봉 반영 False" ERROR
        self.assertTrue(any(" INFO " in x and "공식 시세 아직 없음" in x for x in self.rlines()))
        self.clk.at(self.R.T["official_retry_until"])
        res.retry_official()
        self.assertTrue(any("전날 공식 시세 못 받음" in e for e in res.day.s["errors"]))   # 마감이면 오류
        res.prep_result = prep_out(up=False)                                     # 공식 시세 없음이 아닌 미반영은 예전처럼 오류
        res.apply_prep()
        self.assertTrue(any("전날 일봉 반영 False" in e for e in res.day.s["errors"]))

    def test_b4_official_first_try_0805_and_krx_compare_0820(self):
        res = self.started("07:20:00")
        res._official_job = lambda day, started: None
        with res.day.lock:
            res.day.s["steps"]["data"] = {"status": "SUCCEEDED", "official_missing_dates": ["2026-10-23"], "attempt": "07:20:00"}
        for hms in ("07:31:00", "07:50:00", "08:02:00", "08:04:59"):
            self.clk.at(hms)
            res.retry_official()
            self.assertIsNone(res.redo_thread, hms)                               # 예전: 07:31 부터 10분마다
        self.clk.at("08:05:00")
        res.retry_official()
        self.assertIsNotNone(res.redo_thread)
        n = []
        saved = (self.R.base_probe_compare, Q.snapshot, self.R.sd.universe_codes)
        self.addCleanup(lambda: (setattr(self.R, "base_probe_compare", saved[0]), setattr(Q, "snapshot", saved[1]),
                                 setattr(self.R.sd, "universe_codes", saved[2])))
        self.R.sd.universe_codes = lambda *a, **k: ["990010"]                      # 저장소 data 를 읽지 않게
        self.R.base_probe_compare = lambda pdir, day: n.append(day) or {}
        Q.snapshot = lambda caller, codes, **k: (pd.DataFrame({"stk_cd": ["990010"], "base_pric": [1.0]}), None, {"returned": 1, "codes": 1, "seconds": 0})
        res._base_probe(res.day.date, "08:00:00")
        self.assertEqual(n, [])                                                  # 예전: 08:00 회차도 KRX 대조
        res._base_probe(res.day.date, "08:20:00")
        self.assertEqual(len(n), 1)

    def test_b5_same_failures_info_new_failure_warn(self):
        res = self.started("08:31:00")
        r = {"codes": ["000010", "000020"], "got": ["000020"], "failed": {"000010": "empty"}, "published": None, "reason": "받은 종목 없음"}
        res.apply_refetch("08:30:00", dict(r))
        self.assertIn(" INFO ", [x for x in self.rlines() if "장중 재수집 08:30" in x][-1])   # 07:00 실패와 같은 종목만 (예전 WARN)
        self.clk.at("10:01:00")
        res.apply_refetch("10:00:00", {"codes": ["000010"], "got": [], "failed": {"000010": "empty"}, "published": None, "reason": "-"})
        self.assertIn(" INFO ", [x for x in self.rlines() if "장중 재수집 10:00" in x][-1])
        self.clk.at("12:01:00")
        res.apply_refetch("12:00:00", {"codes": ["000010", "000030"], "got": [], "failed": {"000010": "empty", "000030": "timeout"},
                                       "published": None, "reason": "-"})
        self.assertIn(" WARN ", [x for x in self.rlines() if "장중 재수집 12:00" in x][-1])   # 새 실패 종목 -> WARN

    def test_b6_official_alert_uses_bar_date(self):
        res = self.started("08:12:00")
        res.prep_result, res.prep_reason = prep_out(), "공식 시세 반영 뒤 다시 적재 (보관 행 3종목 보정)"
        res.apply_prep()
        a = [x for x in self.alerts if x.startswith("[공식 시세 반영]")]
        self.assertTrue(a and a[-1].startswith("[공식 시세 반영] 2026-10-23 일봉"), a)   # 예전: 오늘 2026-10-26


class WatchdogAtomic(unittest.TestCase):
    def test_b7_pending_save_keeps_old_list_when_write_fails(self):
        import shutil
        import tempfile
        import watchdog as W
        tmp = tempfile.mkdtemp()
        saved = dict(W.CFG)
        self.addCleanup(lambda: (W.CFG.clear(), W.CFG.update(saved), shutil.rmtree(tmp, ignore_errors=True)))
        W.CFG["state_dir"] = tmp
        W.save_pending(["a", "b"])
        real = json.dump

        def broken(obj, f, *a, **k):
            f.write("[" + chr(34) + "x")
            raise OSError("디스크 오류 (쓰는 도중)")
        json.dump = broken
        try:
            with self.assertRaises(OSError):
                W.save_pending(["b"])
        finally:
            json.dump = real
        with open(W.st("pending"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), ["a", "b"])                            # 예전: 반쯤 쓴 파일 -> 목록 유실


# ---------------------------------------------------------------- C

from test_broad7 import Catch, FakeProc  # noqa: E402
from test_orders import sends  # noqa: E402
import common as C  # noqa: E402
import orders as O  # noqa: E402


NEW_UNI = "data/universe_20261102.csv"


def new_list(t):
    """첫 거래일 월별 갱신이 새 종목 목록을 냄 (포인터 = NEW_UNI). 부모 sd.UNIVERSE 는 시험 뒤 되돌림"""
    saved = C.universe_for, C.sd.UNIVERSE
    t.addCleanup(lambda: (setattr(C, "universe_for", saved[0]), setattr(C.sd, "UNIVERSE", saved[1])))
    C.universe_for = lambda day, pointer=None: NEW_UNI if str(pd.Timestamp(day).date()) >= "2026-11-02" else saved[1]
    return saved[1]


class UniverseC1(Catch):
    def test_c1_universe_retry_until_ok(self):
        calls, slept = [], []

        def fn(p, last):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("KRX 시간 초과")
            return {"ok": len(calls) == 3, "error": None if len(calls) == 3 else "응답 0건"}
        u = self.MO.update_universe_retry({"retrain_day": "2026-11-02"}, "2026-10-30", tries=3, gap=60, sleep=slept.append, fn=fn)
        self.assertTrue(u["ok"])
        self.assertEqual((len(calls), slept, [a["ok"] for a in u["attempts"]]), (3, [60, 60], [False, False, True]))

    def test_c1_morning_success_reapplies_universe_to_parent(self):
        """G1 로 대체: 첫 거래일 성공 -> 새 목록을 바로 쓰지 않고 새 목록 준비를 기다림 (성공한 묶음과 함께 교체, UniverseAtomicG1)"""
        self.clk.t = pd.Timestamp("2026-11-02 07:00:00")
        res = self.arm(self.started("07:00:00"))
        new_list(self)
        res.tick(self.clk())
        self.clk.at("08:40:00")
        self.finish()
        res.tick(self.later())
        self.assertEqual(res.uni_pending, NEW_UNI)                                  # 예전: 다음 날 하루 시작 때만
        self.assertEqual(res.day.s["universe_pending"]["path"], NEW_UNI)
        self.assertNotEqual(self.R.sd.UNIVERSE, NEW_UNI)                            # 준비 성공 전까지 부모는 옛 목록
        self.assertTrue(res.prep_ok)

    def test_c1_installed_but_universe_failed_retries_universe_only(self):
        self.clk.t = pd.Timestamp("2026-10-30 16:31:00")
        res = self.arm(self.resident())
        cmds = []
        res.spawn_monthly = lambda month, log, cmd="run": cmds.append(cmd) or self.procs.append(FakeProc()) or self.procs[-1]
        res.tick(self.clk())
        self.finish(ok=False)
        r = jl_path(os.path.join(self.MO.OUT_DIR, "2026-11.json"))
        r["steps"].update(installed={"check_ok": True}, universe={"ok": False, "error": "KRX 응답 0건"})
        C.atomic_json(os.path.join(self.MO.OUT_DIR, "2026-11.json"), r)
        self.models.add("2026-11")                                                  # 모델은 설치됨
        res.tick(self.later())
        res.tick(self.later())
        self.assertEqual(cmds, ["run", "universe"])                                 # 예전: 모델이 있으면 끝 (종목 목록 그대로)


POS2 = {"990010": {"qty": 28, "trade_id": "990010-2026-10-20-1", "entry_date": "2026-10-20", "stop_price": 30000.0, "kind": "본래", "avg_price": 31000.0}}


class QtyReviewC2(Base):
    def setup_prev_partial_stop(self, review=False):
        pos = {"held": json.loads(json.dumps(POS2)), "next_open_sells": {}, "trade_seq": 1, "qty_synced": {"date": "2026-10-22", "time": "15:41:00", "by": "15:40"}}
        if review:
            pos["held"]["990010"]["qty"] = 18
            pos["held"]["990010"]["qty_review"] = {"date": "2026-10-26", "at": "09:00:00", "where": "장중 손절 감시", "file_qty": 28, "account_qty": 18,
                                                   "stop": 30000.0, "synced": "2026-10-22"}
        P.save(self.R.POSITIONS, pos)
        ip = os.path.join(self.tmp, "intents", "2026-10-23.json")                   # 전날 일부 손절 (10주) — 15:40 갱신 중 종료로 장부는 28주 그대로
        os.makedirs(os.path.dirname(ip), exist_ok=True)
        C.atomic_json(ip, [{"intent_id": "2026-10-23-stop-990010-1", "date": "2026-10-23", "stage": "stop", "side": "sell", "code": "990010", "qty": 10,
                            "status": O.ACCEPTED, "filled_qty": 10, "fill_price": 29500}])
        self.fk.balance_rows = [bal_row("990010", 18, avg=31000, sellable=18)]

    def test_c2_explained_by_intents_since_synced_no_review(self):
        """H 로 바꿈: 계좌가 장부보다 적으면 장중 수동 확인 없음·장부 그대로 (맞춤은 확정 시점), 매도는 있는 만큼"""
        self.setup_prev_partial_stop()
        res = self.started("10:00:00", live=False)
        self.quotes["990010"] = 31000.0
        res.stop_minute()
        p = jl_path(self.R.POSITIONS)["held"]["990010"]
        self.assertNotIn("qty_review", p)                                           # 예전: 수동 확인 -> 손절 멈춤
        self.assertEqual(p["qty"], 28)                                              # 11W H: 장중에는 장부를 맞추지 않음 (F: 18 로 맞춤)
        self.assertFalse(any("[수동 확인 필요]" in a for a in self.alerts))
        self.quotes["990010"] = 29000.0
        self.clk.at("10:01:00")
        res.stop_minute()
        s = sends(self.fk, "kt10001")
        self.assertEqual((len(s), s[0][2]["ord_qty"]), (1, "18"))                  # 잔량 손절

    def test_c2_existing_review_stays_until_person(self):
        """H 로 바꿈: 자동 해제(C2·F qty_after) 없앰 — 걸린 수동 확인은 사람이 지우거나 손절가를 고칠 때까지"""
        self.setup_prev_partial_stop(review=True)
        res = self.started("10:00:00", live=False)
        self.quotes["990010"] = 29000.0
        res.stop_minute()
        self.assertIn("qty_review", jl_path(self.R.POSITIONS)["held"]["990010"])
        self.assertFalse(any(a.startswith("[수동 확인 자동 해제]") for a in self.alerts))
        self.assertEqual(sends(self.fk, "kt10001"), [])

    def test_c2_unexplained_still_review(self):
        """H 로 바꿈: 설명할 의도 없는 감소 -> 장중은 수동 확인 없음 (매도는 있는 만큼), 확정 시점(아침 복구)에서 수동 확인"""
        self.setup_prev_partial_stop()
        os.remove(os.path.join(self.tmp, "intents", "2026-10-23.json"))             # 설명할 의도 없음 (병합 등)
        res = self.started("10:00:00", live=False)
        self.quotes["990010"] = 31000.0
        res.stop_minute()
        self.assertNotIn("qty_review", jl_path(self.R.POSITIONS)["held"]["990010"])
        self.prev_state(steps=("settle",))
        res = self.started("07:20:00", live=False)
        res.day.s["steps"].pop("prev_settle")
        res.prev_settle()
        self.assertIn("qty_review", jl_path(self.R.POSITIONS)["held"]["990010"])


class QtyNumericD(Base):
    """D: '설명됨' = qty_synced 뒤 의도 수량으로 숫자로 설명 (감소 <= 매도 의도 합, 증가 <= 매수 의도 합), 아니면 수동 확인"""

    def run_minute(self, acct_qty):
        QtyReviewC2.setup_prev_partial_stop(self)                                    # 어제 매도 의도 10주, 장부 28주
        self.fk.balance_rows = [bal_row("990010", acct_qty, avg=31000, sellable=acct_qty)]
        res = self.started("10:00:00", live=False)
        self.quotes["990010"] = 31000.0
        res.stop_minute()
        return jl_path(self.R.POSITIONS)["held"]["990010"]

    def test_d_split_after_partial_sell_is_review(self):
        p = self.run_minute(56)                                                     # 분할 (수량 두 배)
        self.assertIn("qty_review", p)                                              # <커밋>: 의도가 있어 자동 맞춤
        self.assertTrue(any("[수동 확인 필요]" in a for a in self.alerts))

    def test_d_decrease_equal_to_sell_intents_auto(self):
        p = self.run_minute(18)
        self.assertNotIn("qty_review", p)
        self.assertEqual(p["qty"], 28)                                              # 11W H: 장중 맞춤 없음 (아침 복구가 18 로, test_d_morning_recovery_partial_sell_auto)

    def test_d_decrease_over_sell_intents_is_review(self):
        p = self.run_minute(13)                                                     # 15주 감소 > 매도 체결 10주
        self.assertNotIn("qty_review", p)                                           # 11W H: 장중은 계좌가 적으면 수동 확인 없음 (매도는 있는 만큼)
        self.prev_state(steps=("settle",))
        res = self.started("07:20:00", live=False)
        res.day.s["steps"].pop("prev_settle")
        res.prev_settle()                                                           # 확정 시점: 28 - 10 != 13 -> 수동 확인
        self.assertIn("qty_review", jl_path(self.R.POSITIONS)["held"]["990010"])

    def test_d_existing_review_not_cleared_for_split(self):
        QtyReviewC2.setup_prev_partial_stop(self, review=True)
        pos = jl_path(self.R.POSITIONS)
        pos["held"]["990010"]["qty"] = 56
        pos["held"]["990010"]["qty_review"]["account_qty"] = 56
        P.save(self.R.POSITIONS, pos)
        self.fk.balance_rows = [bal_row("990010", 56, avg=31000, sellable=56)]
        res = self.started("10:00:00", live=False)
        res.stop_minute()
        self.assertIn("qty_review", jl_path(self.R.POSITIONS)["held"]["990010"])

    def test_d_morning_recovery_split_is_review(self):
        QtyReviewC2.setup_prev_partial_stop(self)
        self.fk.balance_rows = [bal_row("990010", 56, avg=15500, sellable=56)]
        self.prev_state(steps=("settle",))                                          # 10-23 정산 안 끝남 (15:40 갱신 중 종료) -> 아침 복구 대상
        res = self.started("07:20:00", live=False)
        res.day.s["steps"].pop("prev_settle")
        res.prev_settle()
        p = jl_path(self.R.POSITIONS)["held"]["990010"]
        self.assertIn("qty_review", p)                                              # <커밋>: 매도 의도가 있어 fill_missing 이 56주로 확정
        self.assertEqual(p["stop_price"], 30000.0)
        self.assertTrue(any("[수동 확인 필요]" in a and "아침 복구" in a for a in self.alerts))

    def test_d_morning_recovery_partial_sell_auto(self):
        QtyReviewC2.setup_prev_partial_stop(self)
        self.prev_state(steps=("settle",))
        res = self.started("07:20:00", live=False)
        res.day.s["steps"].pop("prev_settle")
        res.prev_settle()
        p = jl_path(self.R.POSITIONS)["held"]["990010"]
        self.assertNotIn("qty_review", p)
        self.assertEqual(p["qty"], 18)


class AcPositionsC3(Base):
    def test_c3_retry_keeps_manual_edits(self):
        held = json.loads(json.dumps(POS2))
        held["990010"]["qty_review"] = {"date": "2026-10-26", "file_qty": 20, "account_qty": 28, "stop": 30000.0}
        held["005930"] = {"qty": 10, "trade_id": "005930-2026-10-21-2", "entry_date": "2026-10-21", "stop_price": 50000.0, "kind": "본래"}
        P.save(self.R.POSITIONS, {"held": held, "next_open_sells": {}, "trade_seq": 2})
        res = self.c3res = self.started("15:40:10", live=False)
        res.day.set("settlement", {"account": {"990010": 28, "005930": 10}, "avg_price": {"990010": 31000.0, "005930": 52000.0}})
        with self.assertRaises(self.R.S.StepFailed):                               # 005930 15:30 1분봉 없음 -> 다시
            res.ac_positions()
        pos = jl_path(self.R.POSITIONS)                                             # 사람이 표시를 지우고 손절가를 고침
        pos["held"]["990010"].pop("qty_review")
        pos["held"]["990010"]["stop_price"] = 29000.0
        P.save(self.R.POSITIONS, pos)
        self.clk.at("15:41:10")
        with self.assertRaises(self.R.S.StepFailed):
            res.ac_positions()
        p = jl_path(self.R.POSITIONS)["held"]["990010"]
        self.assertNotIn("qty_review", p)                                           # 예전: 기준 장부로 덮어 되돌아감
        self.assertEqual(p["stop_price"], 29000.0)
        self.assertTrue(res.day.s.get("ac_positions_manual"))


class LateSummaryC4(Base):
    def prepared(self):
        res = self.started("16:31:00", live=False)
        d = res.day
        snap = os.path.join(self.tmp, "snap.parquet")
        pd.DataFrame({"p_code": ["990010"]}).to_parquet(snap, index=False)
        d.set("live", {"snap_file": snap, "plan": [], "kind": "본래", "filter": {"filter_ok": True}})
        d.set("buy_decision", {"time": "15:21:00", "new_buy_ok": True, "reason": "", "buy_block": []})
        d.set("settlement", {"account": {}, "d2": 30_000_000.0})
        d.mark("settle")
        C.atomic_json(os.path.join(self.R.LOG_DIR, "2026-10-26.json"), {"date": "2026-10-26", "mode": "resident (mock, 주문 켬)", "quotes_env": "prod",
                                                                       "git_commit": "x", "errors": [], "model": {"month": "2026-10", "check": {"ok": True}},
                                                                       "data": {}, "data_version": "v1"})
        return res

    def test_c4_restart_keeps_buy_decision(self):
        res = self.prepared()                                                       # 장후 재시작: 메모리(ctx·계산 결과) 없음
        res.ctx = {}
        res.finish_late()
        log = jl_path(os.path.join(self.R.LOG_DIR, "2026-10-26.json"))
        self.assertTrue(res.day.done("ac_summary"))
        self.assertTrue(log["buy_decision"]["new_buy_ok"], log["buy_decision"])   # 예전: False '스냅샷 없음' 으로 뒤집음

    def test_c4_finished_calc_late_summary_not_incomplete(self):
        res = self.prepared()
        job = res.make_close_job()
        job["result"] = {"finished": "15:50:00", "seconds": 600.0, "date": job["date"], "gen": job["gen"], "rerun_out": {}}
        res.close_job = job
        res.finish_late()
        self.assertTrue(res.day.done("ac_summary"))
        self.assertFalse(res.day.s.get("rerun_incomplete"))                        # 예전: '대조 미완료 (16:30 지남)'
        self.assertFalse(any("[대조 미완료]" in a for a in self.alerts))


class SweepC5(unittest.TestCase):
    def test_c5_running_pid_folder_kept(self):
        import shutil
        import subprocess
        import tempfile
        import time as _t
        import engine as E
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True).stdout.strip()
        old = _t.time() - 7 * 3600
        for name in (str(os.getpid()), dead):
            os.makedirs(os.path.join(root, name))
            os.utime(os.path.join(root, name), (old, old))
        self.assertEqual(E.sweep_frozen_tmp(root), 1)
        self.assertTrue(os.path.isdir(os.path.join(root, str(os.getpid()))))     # 예전: 실행 중 프로세스 폴더도 지움
        self.assertFalse(os.path.isdir(os.path.join(root, dead)))


class FeeC6(Base):
    def test_c6_fee_rate_error_detected_inside_cash_tolerance(self):
        P.save(self.R.POSITIONS, {"held": {"000010": {"qty": 20000, "entry_date": "2026-10-20", "stop_price": 400.0, "avg_price": 450.0}},
                                  "next_open_sells": {}, "trade_seq": 1})
        self.prev_state(steps=("after_close",), settlement={"d2": 30_000_000.0, "equity": 39_000_000.0})
        res = self.started("15:40:10", live=False)
        amt, tax = 20000 * 500, self.R.sb_tax(2026)
        fee_real = P.kiwoom_fee(amt, 0.0030)                                       # 실제 0.30% (설정은 모의 0.35%)
        it = res.om.store.add(date=res.om.date(), stage="stop", side="sell", code="000010", qty=20000, price=None, trde_tp="3", attempt=1, kind="본래",
                              reason="시험", assumed_kind="stop")
        res.om.store.update(it, status=O.ACCEPTED, ord_no="S1", filled_qty=20000, fill_price=500, fill_status="체결", fee_won=float(fee_real),
                            tax_won=float(P.kiwoom_tax(amt, tax)))
        self.fk.d2 = 30_000_000.0 + amt - fee_real - P.kiwoom_tax(amt, tax)
        res.settle()
        c = res.day.s["cash_check"]
        self.assertTrue(c["ok"], c)                                                 # 현금 허용치(20,013) 안
        self.assertFalse(c["fee_ok"], c)                                            # 예전: 감지 없음
        self.assertEqual(c["fee_diff"], fee_real - P.kiwoom_fee(amt, self.R.commission_rate()))
        self.assertTrue(any(a.startswith("[수수료 차이]") for a in self.alerts))


class DocsC7(Base):
    def test_stop_audit_10bp(self):
        res = self.started("15:40:10", live=False)
        it = self.filled_sell(res.om, code="990010", qty=28, price=29950, stage="stop")
        res.om.store.update(it, stop_price=30000.0, first_seen={"time": "10:00:00", "price": 29990.0})
        a = res.stop_audit()
        self.assertEqual((a[0]["diff_bp"], a[0]["worse_10bp"]), (-16.7, True))

    def test_buy_until_dropped_from_times(self):
        import settings
        t = {"plan_snapshot": "15:20:01", "buy_until": "15:25:00"}
        old = settings.snapshot_times(t)
        self.assertNotIn("buy_until", t)
        self.assertEqual(old.get("buy_until"), "15:25:00")                          # 예전 config.json 에 있으면 시작 때 '예전 시각 키 무시'
        self.assertNotIn("buy_until", self.R.T)


# ---------------------------------------------------------------- E (Codex 확인 <커밋>..<커밋>)

class UniverseAtomicG1(Catch):
    """E1 을 G1 로 대체: 새 종목 목록은 '새 목록 일봉 수집 + 준비' 가 성공한 묶음과 함께 한 번에 교체. 그 전까지 부모 목록·묶음·data 는 옛 것"""

    def setup_old_bundle(self, fail=(), running=False):
        """옛 묶음 A 로 운영 중 (07:00 준비 끝) -> 08:40 첫 거래일 월별 갱신 성공. fail = 실패시킬 준비 차례 (1부터).
        running: 묶음 없이 07:00 준비(옛 목록)가 도는 중에 월별 갱신 성공 (첫 준비는 self.go 까지 멈춤)"""
        self.clk.t = pd.Timestamp("2026-11-02 07:00:00")
        res = self.arm(self.started("07:00:00"))
        self.old = new_list(self)
        self.poolA = prep_out()["pool"]
        res.pool = self.poolA
        res.ctx["prep"] = prep_out()["prep"]
        res.day.mark("data", {"daily_update": "ok", "universe": self.old})
        self.works = []

        self.go = threading.Event()
        if running:
            res.pool, res.prep_ok = None, False

        def work(day, need):
            th = threading.current_thread()
            self.works.append({"need": need, "uni": getattr(th, "uni", None)})
            if running and len(self.works) == 1:
                self.go.wait(5)
            o = prep_out()
            if len(self.works) in fail:
                o = {"error": "RuntimeError('새 목록 적재 실패')", "data": {"daily_update": "ok"}}
            res.publish_prep(o)
        res._prep_work = work
        res.request_prep = lambda reason: self.R.Resident.request_prep(res, reason)
        res.tick(self.clk())                                                        # 월별 모델 따라잡기 시작
        self.clk.at("08:40:00")
        self.finish()
        res.tick(self.later())                                                      # 성공 -> 새 목록 대기
        return res

    def step(self, res, sec=61):
        res.tick(self.later(sec))
        if res.prep_thread is not None:
            res.prep_thread.join(5)
        res.tick(self.later(1))

    def test_g1_new_prepare_fails_keeps_old_list_and_bundle(self):
        res = self.setup_old_bundle(fail=(1,))
        self.step(res)                                                              # 새 목록 준비 실패
        self.assertEqual(self.works[0], {"need": True, "uni": NEW_UNI})            # <커밋>: 옛 준비의 data 성공 뒤 need_data False (새 종목 일봉 없음)
        self.assertEqual(self.R.sd.UNIVERSE, self.old)                              # <커밋>: 새 목록 + 옛 묶음 A -> 15:20 스냅샷이 새 목록 시세를 옛 묶음으로
        self.assertIs(res.pool, self.poolA)
        self.assertTrue(res.prep_ok)
        self.assertEqual(res.day.s["steps"]["data"]["universe"], self.old)        # data 상태도 옛 목록 그대로
        self.assertEqual(res.uni_pending, NEW_UNI)

    def test_g1_new_prepare_success_swaps_list_bundle_data_together(self):
        res = self.setup_old_bundle()
        self.step(res)
        self.assertEqual((self.R.sd.UNIVERSE, res.day.s["universe"], res.day.s["steps"]["data"]["universe"]), (NEW_UNI,) * 3)
        self.assertIsNot(res.pool, self.poolA)
        self.assertTrue(res.prep_ok)
        self.assertIsNone(res.uni_pending)
        self.assertTrue(any(a.startswith("[월별 종목 목록 적용]") for a in self.alerts))

    def test_g1_not_applied_by_data_check_runs_old_list_today(self):
        res = self.setup_old_bundle(fail=range(1, 999))
        self.step(res)
        self.step(res)
        n = len(self.works)
        self.assertEqual(n, 2)                                                      # 60초마다 다시
        res.universe_deadline("15:18:31")
        self.assertTrue(any(a.startswith("[월별 목록 미적용]") for a in self.alerts))
        self.assertIsNone(res.uni_pending)
        self.step(res)
        self.assertEqual(len(self.works), n)                                        # 그날은 더 안 함
        self.assertEqual((self.R.sd.UNIVERSE, res.pool, res.prep_ok), (self.old, self.poolA, True))

    def test_g1_old_prepare_running_is_consistent_then_new_list(self):
        """E1 경우 (준비 중 목록 변경): 옛 준비는 옛 목록 그대로 채택 (부모도 옛 목록 = 일관), 끝난 뒤 새 목록 준비 -> 교체"""
        res = self.setup_old_bundle(running=True)
        self.assertEqual((res.uni_pending, len(self.works)), (NEW_UNI, 1))       # 옛 준비는 아직 도는 중
        self.go.set()
        res.prep_thread.join(5)
        res.tick(self.later())
        self.assertEqual((self.works[0]["uni"], self.R.sd.UNIVERSE, res.prep_ok), (None, self.old, True))
        self.step(res)
        self.assertEqual((self.works[-1], self.R.sd.UNIVERSE), ({"need": True, "uni": NEW_UNI}, NEW_UNI))

    def test_g1_prep_work_collects_both_lists_loads_new(self):
        import daily
        import engine as E
        import run_day as RD
        res = self.started("08:41:00")
        got = {}
        saved = (daily.update, RD.prepare, C.universe_codes_at, C.sd.universe_codes, daily.excluded_codes, daily.official_filled, E.frozen_check,
                 daily.data_file)
        self.addCleanup(lambda: (setattr(daily, "update", saved[0]), setattr(RD, "prepare", saved[1]), setattr(C, "universe_codes_at", saved[2]),
                                 setattr(C.sd, "universe_codes", saved[3]), setattr(daily, "excluded_codes", saved[4]),
                                 setattr(daily, "official_filled", saved[5]), setattr(E, "frozen_check", saved[6]), setattr(daily, "data_file", saved[7])))
        daily.update = lambda only=None, check=None: got.setdefault("only", only)
        class Pool:
            def close(self):
                pass

            def amt20(self, day):
                return {}
        RD.prepare = lambda day, codes=None: got.setdefault("codes", codes) and (Pool(), [], [], {}, {}, {"model": {}, "data": {}})
        C.universe_codes_at = lambda path: ["000002", "000003"]
        C.sd.universe_codes = lambda: ["000001", "000002"]
        daily.excluded_codes, daily.official_filled, E.frozen_check = (lambda: {}), (lambda: {}), (lambda: {"ok": True})
        daily.data_file = lambda name, d=None: os.path.join(self.tmp, name)
        res.judge_work = lambda day, out: None
        res.prep_prev_check = False
        th = threading.Thread(target=res._prep_work, args=(res.day.date, True))
        th.uni = NEW_UNI
        th.start()
        th.join(5)
        self.assertEqual((got["only"], got["codes"]), (["000001", "000002", "000003"], ["000002", "000003"]))
        self.assertNotIn("error", res.prep_result or {}, res.prep_result)


class AcPositionsE2(Base):
    def test_e2_manual_edits_kept_on_every_retry(self):
        AcPositionsC3.test_c3_retry_keeps_manual_edits(self)                        # 재시도 1번째: 보존
        for k in ("15:42:10", "15:43:10"):                                          # 재시도 2·3번째 (사람은 더 안 고침)
            self.clk.at(k)
            with self.assertRaises(self.R.S.StepFailed):
                self.c3res.ac_positions()
            p = jl_path(self.R.POSITIONS)["held"]["990010"]
            self.assertNotIn("qty_review", p, k)                                    # <커밋>: 기준 장부 값으로 되돌아감
            self.assertEqual(p["stop_price"], 29000.0, k)


class AcPositionsG2(Base):
    def test_g2_new_trade_manual_stop_kept_on_retry(self):
        P.save(self.R.POSITIONS, {"held": json.loads(json.dumps(POS2)), "next_open_sells": {}, "trade_seq": 1})
        res = self.started("15:40:10", live=False)
        self.filled_buy(res.om, code="000660", qty=10, price=31000)                 # 오늘 신규 매수
        res.day.set("settlement", {"account": {"990010": 28, "000660": 10}, "avg_price": {"990010": 31000.0, "000660": 31000.0}})
        self.cv["000660"] = {"close": 31000}
        self.judged["000660"] = {"close": 31000.0, "stop": 29000.0, "trend": False, "basis": "수정주가 일봉"}
        with self.assertRaises(self.R.S.StepFailed):                               # 990010 15:30 1분봉 없음 -> 첫 갱신 실패
            res.ac_positions()
        self.assertEqual(jl_path(self.R.POSITIONS)["held"]["000660"]["stop_price"], 29000.0)
        pos = jl_path(self.R.POSITIONS)                                             # 사람이 새 거래 손절가를 고침
        pos["held"]["000660"]["stop_price"] = 27000.0
        P.save(self.R.POSITIONS, pos)
        self.cv["990010"] = {"close": 31000}
        self.judged["990010"] = {"close": 31000.0, "stop": 30000.0, "trend": False, "basis": "수정주가 일봉"}
        self.clk.at("15:41:10")
        res.ac_positions()                                                          # 재시도 성공
        self.assertEqual(jl_path(self.R.POSITIONS)["held"]["000660"]["stop_price"], 27000.0)   # <커밋>: new_trade 가 다시 만들어 29000


# ---------------------------------------------------------------- F·H (11W H: 장중에는 맞추지 않음, 확정 시점(07:00·15:40)에 정확히 같을 때만)

def put_intents(tmp, day, items):
    ip = os.path.join(tmp, "intents", f"{day}.json")
    os.makedirs(os.path.dirname(ip), exist_ok=True)
    C.atomic_json(ip, items)


def sell_it(n, qty, filled, created="10:00:00.000", day="2026-10-23", **kw):
    return {"intent_id": f"{day}-stop-990010-{n}", "date": day, "stage": "stop", "side": "sell", "code": "990010", "qty": qty, "status": O.ACCEPTED,
            "filled_qty": filled, "created": created, "ord_no": f"S{n}", **kw}


class QtyHelpers(Base):
    """F·H 시험 도우미 (시험 없음)"""

    def held(self, qty=28, synced=None):
        pos = {"held": json.loads(json.dumps(POS2)), "next_open_sells": {}, "trade_seq": 1,
               "qty_synced": synced or {"date": "2026-10-22", "time": "15:41:00", "by": "15:40"}}
        pos["held"]["990010"]["qty"] = qty
        P.save(self.R.POSITIONS, pos)

    def acct(self, q, sellable=None):
        self.fk.balance_rows = [bal_row("990010", q, avg=31000, sellable=q if sellable is None else sellable)]

    def morning(self, day="2026-10-26", prev="2026-10-23"):
        """다음 거래일 07:20 아침 복구 (prev 날 정산 안 끝남)"""
        self.R.S.Day(os.path.join(self.tmp, f"{prev}.json"), pd.Timestamp(prev), clock=self.clk).mark("settle")
        self.clk.t = pd.Timestamp(f"{day} 07:20:00")
        res = self.started("07:20:00", live=False)
        res.day.s["steps"].pop("prev_settle")
        res.prev_settle()
        return res, jl_path(self.R.POSITIONS)["held"].get("990010")

    def at_1540(self, res, acct_q):
        self.clk.at("15:40:10")
        res.day.set("settlement", {"account": {"990010": acct_q} if acct_q else {}, "avg_price": {"990010": 31000.0}})
        self.cv["990010"] = {"close": 31000}
        self.judged["990010"] = {"close": 31000.0, "stop": 30000.0, "trend": False, "basis": "수정주가 일봉"}
        try:
            res.ac_positions()
        except self.R.S.StepFailed:
            pass
        return jl_path(self.R.POSITIONS)["held"].get("990010")


class QtyUnifiedF(QtyHelpers):
    def test_f1_28_18_8_manual_sell_is_review(self):
        """H 로 바꿈: 28 -> 18 (어제 손절 10주) -> 사람이 10주 더 팖 8 -> 장중은 장부 그대로·수동 확인 없음, 15:40 에 28 - 10 != 8 -> 수동 확인"""
        QtyReviewC2.setup_prev_partial_stop(self)                                    # 어제 손절 10주 체결, 장부 28
        res = self.started("10:00:00", live=False)
        self.quotes["990010"] = 31000.0
        res.stop_minute()
        self.assertEqual(jl_path(self.R.POSITIONS)["held"]["990010"]["qty"], 28)     # <커밋>: 18 로 맞춤 (장중 자동 맞춤)
        self.acct(8)
        self.clk.at("10:01:00")
        res.stop_minute()
        self.assertNotIn("qty_review", jl_path(self.R.POSITIONS)["held"]["990010"])
        p = self.at_1540(res, 8)
        self.assertEqual((p["qty_review"]["file_qty"], p["qty_review"]["account_qty"], p["qty_review"]["where"]), (28, 8, "15:40 보유 갱신"))

    def test_f2_rejected_buy_today_no_stop_sell_and_review(self):
        self.held(28)
        self.acct(56)
        res = self.started("10:00:00", live=False)
        it = res.om.store.add(date=res.om.date(), stage="buy", side="buy", code="990010", qty=28, price=33000, trde_tp="0", attempt=1, kind="본래",
                              reason="매수", assumed_kind="buy")
        res.om.store.update(it, status=O.REJECTED)
        self.quotes["990010"] = 29000.0                                             # 손절 조건 (손절가 30,000)
        r = res.stop_minute()
        self.assertEqual(sends(self.fk, "kt10001"), [])                             # <커밋>: 오늘 의도가 있어 건너뜀 -> 56주 손절 매도
        self.assertEqual(r["qty_changed"], {"990010": {"file": 28, "account": 56}})
        self.assertIn("qty_review", jl_path(self.R.POSITIONS)["held"]["990010"])

    def test_f3_sync_day_stop_counted_next_recovery(self):
        self.held(28, {"date": "2026-10-23", "time": "07:20:00", "by": "아침 복구 (계좌 수량)"})   # 10-23 아침 맞춤
        put_intents(self.tmp, "2026-10-23", [sell_it(1, 10, 10, unfilled_qty=0, fill_status="체결")])  # 그날 10:00 손절 10주 체결, 15:40 전 종료
        self.acct(18)
        self.prev_state(steps=("settle",))
        res = self.started("07:20:00", live=False)
        res.day.s["steps"].pop("prev_settle")
        res.prev_settle()
        p = jl_path(self.R.POSITIONS)["held"]["990010"]
        self.assertNotIn("qty_review", p)                                           # <커밋>: 맞춘 날(10-23) 의도는 빠짐 -> 수동 확인
        self.assertEqual(p["qty"], 18)

    def partial_resend(self, acct_q):
        """H 로 바꿈 (확정 시점): 어제 28주 손절 10주 체결, 잔량 18주 다시 보냄 (0주 체결) -> 오늘 아침 복구"""
        self.held(28)
        put_intents(self.tmp, "2026-10-23", [sell_it(1, 28, 10, unfilled_qty=18),
                                             sell_it(2, 18, 0, created="10:01:00.000", unfilled_qty=18)])
        self.acct(acct_q)
        return self.morning()[1]

    def test_f5_partial_fill_resend_not_double_counted(self):
        p = self.partial_resend(9)                                                  # 실제 매도 10주 -> 18주, 그 뒤 1:2 병합 -> 9주
        self.assertIn("qty_review", p)                                              # <커밋>: 요청 수량 합 46주로 19주 감소 설명 -> 맞춤

    def test_f5b_partial_fill_resend_explained_by_fills(self):
        p = self.partial_resend(18)
        self.assertNotIn("qty_review", p)
        self.assertEqual(p["qty"], 18)

    def test_f6_same_day_stop_then_rebuy_new_trade(self):
        self.held(28)
        res = self.started("10:00:00", live=False)
        self.filled_sell(res.om, qty=28)
        self.clk.at("15:21:00")
        self.filled_buy(res.om, qty=20, price=31000)
        self.clk.at("15:40:10")
        res.day.set("settlement", {"account": {"990010": 20}, "avg_price": {"990010": 31000}})
        self.cv["990010"] = {"close": 31000}
        self.judged["990010"] = {"close": 31000.0, "stop": 29000.0, "trend": False, "basis": "수정주가 일봉"}
        res.ac_positions()
        p = jl_path(self.R.POSITIONS)["held"]["990010"]
        self.assertEqual((p["qty"], p["entry_date"], p["stop_price"], p.get("qty_review"), p.get("info_pending")), (20, "2026-10-26", 29000.0, None, None))
        self.assertNotEqual(p["trade_id"], POS2["990010"]["trade_id"])
        self.assertEqual(jl_path(self.R.POSITIONS)["qty_synced"]["date"], "2026-10-26")

    def test_f7_intraday_partial_stop_fill_waits_for_1540(self):
        self.held(28)
        self.acct(28)
        res = self.started("10:00:00", live=False)
        self.quotes["990010"] = 29000.0
        res.stop_minute()                                                           # 손절 28주 전송 (접수, 체결 조회 없음)
        self.assertEqual(len(sends(self.fk, "kt10001")), 1)
        self.fk.balance_rows = [bal_row("990010", 18, avg=31000, sellable=0)]        # 10주 체결, 18주 걸림
        self.clk.at("10:01:00")
        r = res.stop_minute()
        p = jl_path(self.R.POSITIONS)["held"]["990010"]
        self.assertEqual((r["qty_changed"], p["qty"], p.get("qty_review")), ({}, 28, None))   # 장중 맞춤 없음 -> 15:40 이 확정
        self.assertIn("990010", jl_path(self.R.POSITIONS)["sell_duty"])


class QtySimpleH(QtyHelpers):
    """11W H 재현 (Codex 확인 <커밋>..<커밋> 4-A·4-B·6) + 단순 구조 확인"""

    def test_h_4a_late_fill_cumulative_next_day_decrease_synced(self):
        """10-23 15:40: 28주 손절 15주 체결(체결 조회 누계)·잔량 13주 취소 미확정, 계좌 18 -> 맞추지 않음 (미확정). 10-26 아침 계좌 13 = 28 - 15 -> 맞춤"""
        self.held(28)
        self.acct(18)
        self.clk.t = pd.Timestamp("2026-10-23 15:40:10")
        res = self.started("15:40:10", live=False)
        it = res.om.store.add(date=res.om.date(), stage="stop", side="sell", code="990010", qty=28, price=None, trde_tp="3", attempt=1, kind="본래",
                              reason="손절", assumed_kind="stop")
        res.om.store.update(it, status=O.ACCEPTED, ord_no="S1", filled_qty=15, fill_price=29500.0, unfilled_qty=13)
        self.at_1540(res, 18)
        self.acct(13)
        _, p = self.morning()
        self.assertNotIn("qty_review", p)                                           # <커밋>: 맞출 때 누계 15 를 기준으로 -5 -> 수동 확인
        self.assertEqual(p["qty"], 13)

    def test_h_4a_1540_keeps_book_while_unconfirmed(self):
        """4-A 의 15:40: 미확정 의도(잔량 취소 미확정)가 있으면 맞추지 않음 — 장부 28·그 종목 반영한 날 그대로, 미확정 표시"""
        self.held(28)
        self.clk.t = pd.Timestamp("2026-10-23 15:40:10")
        res = self.started("15:40:10", live=False)
        it = res.om.store.add(date=res.om.date(), stage="stop", side="sell", code="990010", qty=28, price=None, trde_tp="3", attempt=1, kind="본래",
                              reason="손절", assumed_kind="stop")
        res.om.store.update(it, status=O.ACCEPTED, ord_no="S1", filled_qty=15, fill_price=29500.0, unfilled_qty=13)
        p = self.at_1540(res, 18)
        self.assertEqual((p["qty"], p.get("synced_through"), p.get("info_pending")), (28, "2026-10-22", True))   # <커밋>: 18 로 맞추고 누계 저장
        self.assertEqual(jl_path(self.R.POSITIONS)["qty_synced"]["through"], "2026-10-23")

    def test_h_4b_no_created_monday_sell_tuesday_synced(self):
        """월요일(10-26) 아침 복구로 맞춤 -> 그날 손절 10주 체결 (예전 의도: 시각 created 없음) -> 15:40 전 종료 -> 화요일 아침 복구 18 로 맞춤"""
        self.held(28, {"date": "2026-10-26", "time": "07:20:00", "by": "아침 복구 (계좌 수량)"})
        it = sell_it(1, 10, 10, day="2026-10-26", unfilled_qty=0, fill_status="체결")
        it.pop("created")
        put_intents(self.tmp, "2026-10-26", [it])
        self.acct(18)
        _, p = self.morning(day="2026-10-27", prev="2026-10-26")
        self.assertNotIn("qty_review", p)                                           # <커밋>: 시각 없는 의도 = 0시 = 맞춘 때 앞 -> 빠짐 -> 수동 확인
        self.assertEqual(p["qty"], 18)

    def two_minutes(self, status):
        """10:00 장부 28·계좌 28 손절 28주 전송 (같은 종목 오늘 매수 의도 status) -> 10:01 계좌 56 (매도 가능 28)"""
        self.held(28)
        self.acct(28)
        res = self.started("10:00:00", live=False)
        it = res.om.store.add(date=res.om.date(), stage="buy", side="buy", code="990010", qty=28, price=33000, trde_tp="0", attempt=1, kind="본래",
                              reason="매수", assumed_kind="buy")
        res.om.store.update(it, status=status)
        self.quotes["990010"] = 29000.0
        res.stop_minute()
        self.acct(56, sellable=28)
        self.clk.at("10:01:00")
        res.stop_minute()
        return res

    def test_h_6_unknown_buy_account_56_sells_28_only(self):
        res = self.two_minutes(O.UNKNOWN)
        self.assertEqual([x[2]["ord_qty"] for x in sends(self.fk, "kt10001")], ["28"])   # <커밋>: 구간 안·오늘 미확정 -> 28주 더 = 56주
        self.assertIn("qty_review", jl_path(self.R.POSITIONS)["held"]["990010"])
        p = self.at_1540(res, 28)
        self.assertIn("qty_review", p)                                              # 15:40 에도 수동 확인

    def test_h_rejected_buy_account_56_sells_28_only_review(self):
        """같은 동작 (<커밋> 도 REJECTED 는 구간 밖 -> 수동 확인)"""
        self.two_minutes(O.REJECTED)
        self.assertEqual([x[2]["ord_qty"] for x in sends(self.fk, "kt10001")], ["28"])
        self.assertIn("qty_review", jl_path(self.R.POSITIONS)["held"]["990010"])

    def test_h_split_intraday_review_no_sell(self):
        """같은 동작: 분할 (계좌 2배, 의도 없음) + 손절 조건 -> 장중 수동 확인·매도 없음"""
        self.held(28)
        self.acct(56)
        res = self.started("10:00:00", live=False)
        self.quotes["990010"] = 15000.0
        r = res.stop_minute()
        self.assertEqual((sends(self.fk, "kt10001"), r["qty_changed"]), ([], {"990010": {"file": 28, "account": 56}}))
        self.assertIn("qty_review", jl_path(self.R.POSITIONS)["held"]["990010"])

    def test_h_sell_qty_capped_at_book(self):
        """매도 수량 = min(계좌 매도 가능, 장부). <커밋>: 계좌 매도 가능 수량 전부"""
        res = self.started("10:00:00", live=False)
        win = (self.R.T["stop_from"], self.R.T["stop_send_until"], False)
        r = res.om.duty_sell("stop", "990010", {"file_qty": 28, "acct": {"qty": 56, "sellable": 56}}, "본래", "손절 (시험", "stop", win, False)
        self.assertEqual((r["qty"], [x[2]["ord_qty"] for x in sends(self.fk, "kt10001")]), (28, ["28"]))
        r = res.om.duty_sell("stop", "005930", {"file_qty": 28, "acct": {"qty": 10, "sellable": 10}}, "본래", "손절 (시험", "stop", win, False)
        self.assertEqual(r["qty"], 10)                                              # 계좌가 적으면 있는 만큼

    def test_h_partial_fill_resend_no_double_sell(self):
        """같은 동작: 손절 28주 -> 10주 체결·18주 걸림 (매도 가능 0, 안 보냄) -> 걸린 주문이 끝나 매도 가능 18 -> 18주 (이중 매도 없음) -> 15:40 28 - 28 = 0"""
        self.held(28)
        self.acct(28)
        res = self.started("10:00:00", live=False)
        self.quotes["990010"] = 29000.0
        res.stop_minute()
        self.acct(18, sellable=0)
        self.clk.at("10:01:00")
        res.stop_minute()
        self.acct(18)
        self.clk.at("10:02:00")
        res.stop_minute()
        self.assertEqual([x[2]["ord_qty"] for x in sends(self.fk, "kt10001")], ["28", "18"])
        a, b = [i for i in res.om.records if i["side"] == "sell"]
        res.om.store.update(a, filled_qty=10, unfilled_qty=18)
        res.om.store.update(b, filled_qty=18, unfilled_qty=0, fill_status="체결")
        c = res.om.store.add(date=res.om.date(), stage="cancel", side="cancel", code="990010", qty=18, price=None, trde_tp="cancel", attempt=1,
                             kind="본래", reason="취소", assumed_kind="cancel", orig_ord_no=a["ord_no"])
        res.om.store.update(c, status=O.ACCEPTED)
        self.fk.balance_rows = []
        self.assertIsNone(self.at_1540(res, 0))                                     # 다 팔림 -> 장부에서 빠짐
        self.assertFalse(res.day.s.get("positions_unconfirmed"))

    def test_h_same_day_rebuy_not_checked_intraday(self):
        """같은 동작: 오늘 판 종목(closed_today)을 다시 사면 당일 신규 거래 = 장중 수량 검사 밖 (재매수 40 > 옛 장부 28 이어도 수동 확인 아님)"""
        pos = {"held": json.loads(json.dumps(POS2)), "next_open_sells": {}, "trade_seq": 1,
               "closed_today": {"990010": {"date": "2026-10-26", "at": "10:02:00", "kind": "stop"}}}
        acct = {"990010": {"qty": 40, "sellable": 40}}
        O.end_duties(pos, acct, pd.Timestamp("2026-10-26 15:22:00"))
        self.assertEqual((O.closed_codes(pos, "2026-10-26"), pos["closed_today"]["990010"]["reheld"]), (set(), "15:22:00"))
        self.assertEqual(O.qty_over(pos, acct, "2026-10-26"), {})

    def test_h_unconfirmed_today_waits_then_review_if_different(self):
        """오늘 미확정(UNKNOWN 매도) -> 15:40 은 맞추지 않음 (장부 28·반영한 날 그대로), 다음 날 아침 확정 체결로 다르면 수동 확인"""
        self.held(28)
        self.clk.t = pd.Timestamp("2026-10-23 15:40:10")
        res = self.started("15:40:10", live=False)
        it = res.om.store.add(date=res.om.date(), stage="stop", side="sell", code="990010", qty=10, price=None, trde_tp="3", attempt=1, kind="본래",
                              reason="손절", assumed_kind="stop")
        res.om.store.update(it, status=O.UNKNOWN)
        p = self.at_1540(res, 18)
        self.assertEqual((p["qty"], p.get("synced_through"), p.get("qty_review")), (28, "2026-10-22", None))
        intents = jl_path(os.path.join(self.tmp, "intents", "2026-10-23.json"))
        intents[0].update(status=O.ACCEPTED, filled_qty=10, unfilled_qty=0)       # 다음 날: 10주 체결 확정, 계좌는 9 (병합 등)
        put_intents(self.tmp, "2026-10-23", intents)
        self.acct(9)
        _, p = self.morning()
        self.assertEqual(p["qty_review"]["account_qty"], 9)

class QtyEstimateI(QtyHelpers):
    """11W I: 지난 날 체결 수량을 끝내 모르는 의도의 계좌 기준 맞춤은 범위 안일 때만 + 항상 [수량 추정 확정]"""

    def unknown_sell_day(self, acct_q):
        """10-23 손절 10주 접수, 체결 수량 모름 (15:35 전 종료: 잔량 확인·체결 조회 없음), 장부 28 -> 10-26 아침 복구 계좌 acct_q"""
        self.held(28)
        put_intents(self.tmp, "2026-10-23", [sell_it(1, 10, 0)])
        self.acct(acct_q)
        res, p = self.morning()
        return res, p

    def test_i1_sell_unknown_decrease_within_request_synced_with_alert(self):
        res, p = self.unknown_sell_day(18)
        self.assertNotIn("qty_review", p)
        self.assertEqual(p["qty"], 18)
        a = [x for x in self.alerts if x.startswith("[수량 추정 확정]")]
        self.assertEqual(len(a), 1)                                                 # <커밋>: 알림 없음
        self.assertTrue("990010" in a[0] and "장부 28주 -> 계좌 18주" in a[0] and "2026-10-23-stop-990010-1" in a[0])
        self.assertEqual(res.day.s["qty_estimated"][0]["open"], ["2026-10-23-stop-990010-1"])

    def test_i2_split_increase_is_review(self):
        _, p = self.unknown_sell_day(56)
        self.assertEqual((p["qty_review"]["file_qty"], p["qty_review"]["account_qty"]), (28, 56))   # <커밋>: 계좌 기준 56
        self.assertFalse(any(x.startswith("[수량 추정 확정]") for x in self.alerts))

    def test_i3_decrease_over_request_is_review(self):
        _, p = self.unknown_sell_day(10)
        self.assertEqual((p["qty_review"]["file_qty"], p["qty_review"]["account_qty"]), (28, 10))   # <커밋>: 계좌 기준 10
        self.assertFalse(any(x.startswith("[수량 추정 확정]") for x in self.alerts))

class QtyStartJ(QtyHelpers):
    """11W J: 대조 기준 = 그날 시작 수량 하나 (장중 경로는 운영 수량만). Codex 확인 <커밋>..<커밋> 1 ~ 6"""

    def trend_pos(self):
        self.held(28)
        pos = jl_path(self.R.POSITIONS)
        pos["next_open_sells"] = {"990010": {"reason": "추세 매도", "since": "2026-10-23", "attempts": 1, "last_status": "NOT_SENT"}}
        pos["sell_duty"] = {"990010": O.new_duty("trend", "2026-10-23 15:40:00", "추세 매도")}
        P.save(self.R.POSITIONS, pos)

    def test_j1_trend_resend_account_over_book_review_not_sent(self):
        """08:30 첫 전송 때 28 = 28 -> 다시 보낼 때(매 분 확인) 계좌 56 -> 수동 확인·안 보냄"""
        self.trend_pos()
        self.acct(56)
        res = self.started("08:31:00", live=False)
        res.trend_send(["990010"], "매 분 확인: 다시 보낼 것·새 대상")
        self.assertEqual(sends(self.fk, "kt10001"), [])                             # <커밋>: 28주 전송
        p = jl_path(self.R.POSITIONS)["held"]["990010"]
        self.assertEqual((p["qty_review"]["file_qty"], p["qty_review"]["account_qty"], p["qty_review"]["where"]), (28, 56, "추세 매도 다시·새 대상"))

    def test_j1_trend_resend_normal_still_sends(self):
        """같은 동작: 계좌 = 장부면 그대로 다시 보냄"""
        self.trend_pos()
        self.acct(28)
        res = self.started("08:31:00", live=False)
        res.trend_send(["990010"], "매 분 확인: 다시 보낼 것·새 대상")
        self.assertEqual([x[2]["ord_qty"] for x in sends(self.fk, "kt10001")], ["28"])

    def review_cleared_day(self):
        """10:00 계좌 56 > 장부 28 -> 수동 확인 (운영 수량 56, 표시 전 28) -> 사람이 qty_review 지움 -> 10:01 다시 안 걸림"""
        self.held(28)
        self.acct(56)
        res = self.started("10:00:00", live=False)
        self.quotes["990010"] = 31000.0
        res.stop_minute()
        pos = jl_path(self.R.POSITIONS)
        self.assertEqual((pos["held"]["990010"]["qty"], pos["held"]["990010"]["review_base"]["qty"]), (56, 28))
        pos["held"]["990010"].pop("qty_review")                                     # 사람이 확인 (분할 확인 등)
        P.save(self.R.POSITIONS, pos)
        self.clk.at("10:01:00")
        res.stop_minute()
        self.assertNotIn("qty_review", jl_path(self.R.POSITIONS)["held"]["990010"])
        return res

    def test_j2_person_cleared_review_1540_accepts_account_next_day_normal(self):
        res = self.review_cleared_day()
        p = self.at_1540(res, 56)
        self.assertNotIn("qty_review", p)                                           # <커밋>: 장부 28 (review_base) != 56 -> 다시 수동 확인
        self.assertEqual((p["qty"], p.get("review_base"), p.get("info_pending")), (56, None, None))
        self.assertTrue(any(a.startswith("[수동 확인 반영]") and "990010" in a and "계좌 56주" in a for a in self.alerts))
        self.assertEqual((res.day.s["qty_review_ack"]["990010"]["qty"], res.day.s["qty_review_ack"]["990010"]["result"]["how"]), (56, "sync"))  # K1
        self.clk.t = pd.Timestamp("2026-10-27 10:00:00")                            # 다음 날: 시작 수량 56, 손절 감시 정상
        res2 = self.started("10:00:00", live=False)
        self.assertEqual(res2.day.s["qty_start"]["held"]["990010"], {"qty": 56, "through": "2026-10-26"})
        self.quotes["990010"] = 29000.0
        res2.stop_minute()
        self.assertEqual([x[2]["ord_qty"] for x in sends(self.fk, "kt10001")], ["56"])
        self.assertNotIn("qty_review", jl_path(self.R.POSITIONS)["held"]["990010"])

    def test_j2_cleared_between_1540_retries_kept(self):
        """첫 15:40 시도 때는 수동 확인 중 (그대로) -> 사람이 지운 뒤 다시 돌면 계좌 그대로 받음 (ac_manual_keep 과 같은 방식)"""
        self.held(28)
        self.acct(56)
        res = self.started("10:00:00", live=False)
        self.quotes["990010"] = 31000.0
        res.stop_minute()
        p = self.at_1540(res, 56)
        self.assertIn("qty_review", p)
        pos = jl_path(self.R.POSITIONS)
        pos["held"]["990010"].pop("qty_review")
        P.save(self.R.POSITIONS, pos)
        self.clk.at("15:41:10")
        p = self.at_1540(res, 56)
        self.assertNotIn("qty_review", p)
        self.assertEqual(p["qty"], 56)
        self.assertTrue(any(a.startswith("[수동 확인 반영]") for a in self.alerts))

    def corrupt_positions(self):
        self.held(28)
        P.save(self.R.POSITIONS, jl_path(self.R.POSITIONS))                         # 백업
        with open(self.R.POSITIONS, "w", encoding="utf-8") as f:
            f.write('{"held": {"0846')

    def test_j3_intraday_recovery_keeps_start_qty(self):
        """10:00 장부 손상 -> 복구 (계좌 28) -> 11:00 손절 10주 체결 -> 15:40 계좌 18 = 시작 28 - 10 -> 맞춤"""
        self.corrupt_positions()
        self.acct(28)
        res = self.started("10:00:00", live=False)
        res.recover_positions()
        self.assertFalse(res.recovery_pending())
        self.clk.at("11:00:00")
        self.filled_sell(res.om, qty=10, price=29500)
        self.acct(18)
        p = self.at_1540(res, 18)
        self.assertNotIn("qty_review", p)                                           # <커밋>: 복구가 반영한 날 = 오늘 -> 11:00 매도 빠짐 -> 28 != 18 수동 확인
        self.assertEqual((p["qty"], p.get("info_pending")), (18, None))
        self.assertEqual(res.day.s["qty_start"]["held"]["990010"]["qty"], 28)    # 장중 복구는 시작 수량을 바꾸지 않음

    def test_j3_intraday_recovery_after_fill_counts_once(self):
        """09:30 손절 10주 체결 -> 10:00 장부 손상·복구 (계좌 18, 운영 수량만) -> 11:00 5주 더 -> 15:40 계좌 13 = 28 - 15 (오늘분 한 번만)"""
        self.corrupt_positions()
        self.acct(28)
        res = self.started("09:30:00", live=False)
        self.filled_sell(res.om, qty=10, price=29500)
        self.acct(18)
        self.clk.at("10:00:00")
        res.recover_positions()
        self.assertEqual(jl_path(self.R.POSITIONS)["held"]["990010"]["qty"], 28)  # 11W K4: 백업 수량 그대로 (J: 계좌 18)
        self.clk.at("11:00:00")
        it = self.filled_sell(res.om, qty=5, price=29000)
        res.om.store.update(it, ord_no="S2")
        self.acct(13)
        p = self.at_1540(res, 13)
        self.assertNotIn("qty_review", p)
        self.assertEqual(p["qty"], 13)

    def test_j3_late_morning_recovery_counts_today_once(self):
        """전날 15:35 전 종료 (손절 10주 체결 확인) -> 아침 복구가 10:30 로 늦어짐, 그 전 10:00 오늘 손절 5주 체결 (계좌 13)
        -> 시작 수량 = 계좌 13 - 오늘 -5 = 18 -> 15:40 18 - 5 = 13 맞춤 (오늘분 이중 집계 없음)"""
        self.held(28)
        put_intents(self.tmp, "2026-10-23", [sell_it(1, 10, 10, unfilled_qty=0, fill_status="체결")])
        self.R.S.Day(os.path.join(self.tmp, "2026-10-23.json"), pd.Timestamp("2026-10-23"), clock=self.clk).mark("settle")
        self.acct(13)
        res = self.started("10:00:00", live=False)
        self.filled_sell(res.om, qty=5, price=29000)
        self.clk.at("10:30:00")
        res.day.s["steps"].pop("prev_settle")
        res.prev_settle()
        p = self.at_1540(res, 13)
        self.assertNotIn("qty_review", p)
        self.assertEqual(p["qty"], 13)
        self.assertEqual(res.day.s["qty_start"]["held"]["990010"], {"qty": 18, "through": "2026-10-23"})

    def test_j4_unknown_sell_not_presettled_when_other_intent(self):
        """10-23: 손절 28주 UNKNOWN (주문 직전 보유 28) + 같은 날 확정 매수 28주 -> 10-26 아침 계좌 28. 예전: 계좌가 pre_qty 보다 안 줄었음 -> NOT_SENT 로
        미리 확정 -> 장부 28 + 28 != 28 수동 확인. 이제: I 범위 계산 (남은 변화 -28, 매도 요청 28 이내) -> 계좌 기준·[수량 추정 확정], 매도는 ACCEPTED"""
        self.held(28)
        sell = {"intent_id": "2026-10-23-stop-990010-1", "date": "2026-10-23", "stage": "stop", "side": "sell", "code": "990010", "qty": 28,
                "status": O.UNKNOWN, "pre_qty": 28, "filled_qty": 0, "created": "10:00:00.000"}
        buy = {"intent_id": "2026-10-23-buy-990010-2", "date": "2026-10-23", "stage": "buy", "side": "buy", "code": "990010", "qty": 28,
               "status": O.ACCEPTED, "filled_qty": 28, "unfilled_qty": 0, "fill_status": "체결", "fill_price": 31000.0, "ord_no": "B1",
               "created": "15:21:00.000", "kind": "본래"}
        put_intents(self.tmp, "2026-10-23", [sell, buy])
        self.acct(28)
        res, p = self.morning()
        self.assertNotIn("qty_review", p)                                           # <커밋>: 수동 확인
        self.assertEqual(p["qty"], 28)
        its = {i["intent_id"]: i for i in jl_path(os.path.join(self.tmp, "intents", "2026-10-23.json"))}
        self.assertEqual((its[sell["intent_id"]]["status"], its[sell["intent_id"]]["est_filled_qty"]), (O.ACCEPTED, 28))
        self.assertEqual(len([a for a in self.alerts if a.startswith("[수량 추정 확정]")]), 1)

    def test_j4_unknown_alone_presettled_as_before(self):
        """같은 동작: 같은 종목 다른 의도가 없으면 예전처럼 잔고로 미리 확정 (줄었으면 ACCEPTED)"""
        self.held(28)
        put_intents(self.tmp, "2026-10-23", [{"intent_id": "2026-10-23-stop-990010-1", "date": "2026-10-23", "stage": "stop", "side": "sell",
                                              "code": "990010", "qty": 28, "status": O.UNKNOWN, "pre_qty": 28, "filled_qty": 0, "created": "10:00:00.000"}])
        self.acct(0)
        self.fk.balance_rows = []
        _, p = self.morning()
        self.assertIsNone(p)
        its = jl_path(os.path.join(self.tmp, "intents", "2026-10-23.json"))
        self.assertEqual((its[0]["status"], its[0].get("est_filled_qty")), (O.ACCEPTED, None))

    def fb88_positions(self, qty=28, by="15:40 보유 갱신"):
        """<커밋> (서브 PC 현재) 형식: qty_synced = 날짜·시각·누가 (through 없음), review_base·synced_through 없음"""
        pos = {"held": json.loads(json.dumps(POS2)), "next_open_sells": {}, "trade_seq": 1,
               "qty_synced": {"date": "2026-10-22", "time": "15:41:02", "by": by}}
        pos["held"]["990010"]["qty"] = qty
        P.save(self.R.POSITIONS, pos)

    def fb88_day(self, day, steps):
        d = self.R.S.Day(os.path.join(self.tmp, f"{day}.json"), pd.Timestamp(day), clock=self.clk)
        for st in steps:
            d.mark(st)
        d.set("positions_start", {"time": "07:00:05", "held": {"990010": 28}})
        d.set("settlement", {"account": {"990010": 28}, "avg_price": {"990010": 31000.0}, "d2": 30_000_000.0})
        return d

    def test_j5_prevcommit_state_normal_day(self):
        """<커밋> 형식 장부·상태 (전 거래일 정산 끝) -> 아침 복구 (복구할 것 없음) -> 장중 손절 10주 -> 15:40 계좌 18 맞춤, 수동 확인 없음"""
        self.fb88_positions()
        self.fb88_day("2026-10-23", ("settle", "ac_ledger", "ac_positions", "ac_summary", "after_close"))
        self.acct(28)
        res, p = self.morning()
        self.assertNotIn("qty_review", p)
        self.clk.at("10:00:00")
        self.quotes["990010"] = 31000.0
        res.stop_minute()
        self.filled_sell(res.om, qty=10, price=29500)
        self.acct(18)
        self.clk.at("10:01:00")
        res.stop_minute()
        self.assertNotIn("qty_review", jl_path(self.R.POSITIONS)["held"]["990010"])
        p = self.at_1540(res, 18)
        self.assertNotIn("qty_review", p)
        self.assertEqual((p["qty"], jl_path(self.R.POSITIONS)["qty_synced"]["through"]), (18, "2026-10-26"))
        self.assertEqual(res.day.s["qty_start"]["held"]["990010"], {"qty": 28, "through": "2026-10-22"})

    def test_j5_prevcommit_state_unsettled_prev_day(self):
        """<커밋> 형식, 전 거래일 15:35 전 종료 (손절 10주 체결 확인, 예전 의도 형식) -> 아침 복구 18 맞춤 -> 장중 8주 더 -> 15:40 계좌 10 맞춤"""
        self.fb88_positions()
        self.fb88_day("2026-10-23", ("settle",))
        it = sell_it(1, 10, 10, unfilled_qty=0, fill_status="체결")
        put_intents(self.tmp, "2026-10-23", [it])
        self.acct(18)
        res, p = self.morning()
        self.assertNotIn("qty_review", p)
        self.assertEqual(p["qty"], 18)
        self.clk.at("10:00:00")
        self.quotes["990010"] = 31000.0
        res.stop_minute()
        self.filled_sell(res.om, qty=8, price=29500)
        self.acct(10)
        self.clk.at("10:01:00")
        res.stop_minute()
        p = self.at_1540(res, 10)
        self.assertNotIn("qty_review", p)
        self.assertEqual(p["qty"], 10)
        self.assertFalse(any("[수동 확인 필요]" in a for a in self.alerts))

    def test_j5_prevcommit_today_state_restart_midday(self):
        """서브 PC 갱신이 장중 재시작이면: 오늘 상태 파일이 <커밋> 가 쓴 것 (qty_start 없음) -> 재시작 때 장부로 시작 수량 -> 15:40 정상"""
        self.fb88_positions()
        self.fb88_day("2026-10-23", ("settle", "ac_ledger", "ac_positions", "ac_summary", "after_close"))
        d = self.fb88_day("2026-10-26", ("prev_settle",))
        d.s.setdefault("core_starts", []).append({"time": "07:00:05", "reason": "하루 시작"})
        d.save()
        self.acct(28)
        res = self.started("11:00:00", live=False)
        self.filled_sell(res.om, qty=10, price=29500)
        p = self.at_1540(res, 18)
        self.assertNotIn("qty_review", p)
        self.assertEqual(p["qty"], 18)
        self.assertEqual(res.day.s["qty_start"]["held"]["990010"]["qty"], 28)


class PrepReapJ6(Catch):
    """11W J6: 준비 스레드의 목록 읽기 실패·결과 없이 끝남 -> 실패 결과로 회수, 60초 뒤 다시"""

    step = UniverseAtomicG1.step

    def setUp(self):
        super().setUp()
        hook = threading.excepthook
        self.addCleanup(setattr, threading, "excepthook", hook)
        threading.excepthook = lambda a: None                                       # 결과 없이 끝나는 스레드의 예외 출력 안 함

    def test_j6_universe_read_error_is_prep_failure(self):
        res = self.started("08:40:00")
        real = C.universe_codes_at
        self.addCleanup(setattr, C, "universe_codes_at", real)
        C.universe_codes_at = lambda path: (_ for _ in ()).throw(OSError("목록 파일 읽기 실패"))
        th = threading.Thread(target=res._prep_work, args=(res.day.date, True), daemon=True)
        th.uni = NEW_UNI
        th.start()
        th.join(10)
        self.assertIsNotNone(res.prep_result)                                       # <커밋>: try 밖 예외 -> 결과 없이 스레드 끝
        self.assertIn("목록 파일 읽기 실패", res.prep_result["error"])

    def test_j6_thread_ended_without_result_reaped_and_retried(self):
        self.clk.t = pd.Timestamp("2026-11-02 08:40:00")
        res = self.arm(self.started("08:40:00"))
        self.old = new_list(self)
        self.poolA = prep_out()["pool"]
        res.pool, res.ctx["prep"] = self.poolA, prep_out()["prep"]
        res.day.mark("data", {"daily_update": "ok", "universe": self.old})
        self.works, mode = [], ["die"]

        def work(day, need):
            self.works.append({"need": need, "uni": getattr(threading.current_thread(), "uni", None)})
            if mode[0] == "die":
                raise RuntimeError("예상 못 한 예외 (결과 없이 끝남)")
            res.publish_prep(prep_out())
        res._prep_work = work
        res.request_prep = lambda reason: self.R.Resident.request_prep(res, reason)
        res.uni_pending = NEW_UNI
        self.step(res, 1)                                                           # 새 목록 준비 -> 결과 없이 끝남 -> 회수 (실패)
        self.assertEqual(self.works, [{"need": True, "uni": NEW_UNI}])
        self.assertIsNone(res.prep_thread)                                          # <커밋>: 스레드가 남아 재시도 멈춤
        self.assertEqual((self.R.sd.UNIVERSE, res.pool, res.uni_pending, res.prep_ok), (self.old, self.poolA, NEW_UNI, True))
        mode[0] = "ok"
        self.step(res)                                                              # 60초 뒤 다시 (성공)
        self.assertEqual(len(self.works), 2)
        self.assertEqual(self.R.sd.UNIVERSE, NEW_UNI)

class QtyAckK(QtyHelpers):
    """11W K: Codex 확인 <커밋>..<커밋> 예외 경로 4건 (사람 확인 = 확인 수량, 15:40 재시도, 기준 없는 장중 복구, 07:00 백업 복구)"""

    def reviewed_cleared(self):
        """10:00 계좌 56 > 장부 28 -> 수동 확인 (확인 대상 56) -> 14:00 사람이 지움 -> 감시가 확인 수량 56 기록"""
        self.held(28)
        self.acct(56)
        res = self.started("10:00:00", live=False)
        self.quotes["990010"] = 31000.0
        res.stop_minute()
        pos = jl_path(self.R.POSITIONS)
        self.ack0 = pos["held"]["990010"].get("review_ack")
        pos["held"]["990010"].pop("qty_review")
        P.save(self.R.POSITIONS, pos)
        self.clk.at("14:00:00")
        res.stop_minute()
        return res

    def test_k1_ack_56_then_account_84_at_1540_is_new_review(self):
        res = self.reviewed_cleared()
        p = self.at_1540(res, 84)                                                   # 확인 뒤 설명 없이 84
        self.assertIn("qty_review", p)                                              # <커밋>: 84 수용 -> 다음 날 84주 매도 가능
        self.assertEqual((p["qty_review"]["account_qty"], p["qty_review"]["where"]), (84, "15:40 보유 갱신"))
        self.assertFalse(any(a.startswith("[수동 확인 반영]") for a in self.alerts))
        self.assertEqual(self.ack0, {"qty": 56, "date": "2026-10-26", "at": "10:00:00"})    # 확인 대상 = 표시 때 계좌
        self.assertEqual(res.day.s["qty_review_ack"]["990010"]["qty"], 56)          # 사람이 지운 뒤 감시가 그날 상태에 기록

    def test_k1_ack_56_then_account_84_next_morning_is_new_review(self):
        """15:40 전에 꺼짐 -> 다음 날 아침 복구에서 계좌 84 -> 새 수동 확인"""
        self.reviewed_cleared()
        self.acct(84)
        _, p = self.morning(day="2026-10-27", prev="2026-10-26")
        self.assertIn("qty_review", p)                                              # <커밋>: review_base + 표시 없음 -> 84 수용
        self.assertEqual(p["qty_review"]["account_qty"], 84)

    def test_k1_ack_then_stop_sell_counted_after_ack(self):
        """확인 56 -> 확인 뒤 손절 20주 체결 -> 15:40 계좌 36 = 56 - 20 -> 받음·[수동 확인 반영]"""
        res = self.reviewed_cleared()
        self.clk.at("14:10:00")
        self.filled_sell(res.om, qty=20, price=29500)
        p = self.at_1540(res, 36)
        self.assertNotIn("qty_review", p)
        self.assertEqual((p["qty"], p.get("review_ack")), (36, None))
        self.assertTrue(any(a.startswith("[수동 확인 반영]") and "56주로 확인" in a and "계좌 36주" in a for a in self.alerts))

    def test_k1_ack_no_change_morning_accepts(self):
        """같은 동작 (사람 확인 뒤 변화 없음): 다음 날 아침 계좌 56 -> 받음"""
        self.reviewed_cleared()
        _, p = self.morning(day="2026-10-27", prev="2026-10-26")
        self.assertNotIn("qty_review", p)
        self.assertEqual((p["qty"], p.get("review_ack")), (56, None))

    def test_k2_review_made_at_1540_ended_by_stop_change_retry_keeps(self):
        """15:40 첫 시도가 만든 수동 확인 (시작 28 != 계좌 56) -> 사람이 손절가를 고쳐 끝냄 -> 다시 돌면 확인 수량 56 그대로 받음 -> 다음 날 손절 정상"""
        self.held(28)
        self.acct(56)
        res = self.started("10:00:00", live=False)
        self.clk.t = pd.Timestamp("2026-10-26 15:40:10")
        p = self.at_1540(res, 56)
        self.assertEqual(p["qty_review"]["account_qty"], 56)
        pos = jl_path(self.R.POSITIONS)
        pos["held"]["990010"]["stop_price"] = 15000.0                               # 사람: 분할 1:2 확인, 손절가 반으로
        P.save(self.R.POSITIONS, pos)
        self.clk.at("15:41:10")
        p = self.at_1540(res, 56)
        self.assertFalse(P.in_review(p))                                            # <커밋>: 기준 장부(표시 전)로 다시 -> 같은 수동 확인
        self.assertEqual((p["qty"], p["stop_price"]), (56, 15000.0))
        self.assertTrue(any(a.startswith("[수동 확인 반영]") for a in self.alerts))
        self.clk.t = pd.Timestamp("2026-10-27 10:00:00")
        res2 = self.started("10:00:00", live=False)
        self.quotes["990010"] = 14000.0
        res2.stop_minute()
        self.assertEqual([x[2]["ord_qty"] for x in sends(self.fk, "kt10001")], ["56"])

    def test_k3_no_basis_intraday_restart_recovery(self):
        """그날 시작 수량 없음 + 장부·백업 모두 손상 + 장중 재시작 (09:30 손절 10주 체결, 계좌 18) -> 시작 수량 = 18 + 10 = 28·[수량 기준 없음] -> 15:40 18 맞춤"""
        put_intents(self.tmp, "2026-10-26", [sell_it(1, 10, 10, created="09:30:00.000", day="2026-10-26", unfilled_qty=0, fill_status="체결")])
        with open(self.R.POSITIONS, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.acct(18)
        res = self.started("10:00:00", live=False)
        self.assertEqual(res.day.s["positions_recovery"]["status"], "corrupt_unrecovered")
        res.recover_positions()
        p = self.at_1540(res, 18)
        self.assertNotIn("qty_review", p)                                           # <커밋>: 복구 장부 18 에서 오늘 매도 10 을 또 빼 8 -> 수동 확인
        self.assertEqual(p["qty"], 18)
        self.assertEqual(res.day.s["qty_start"]["held"]["990010"]["qty"], 28)
        self.assertTrue(any(a.startswith("[수량 기준 없음]") and "990010" in a for a in self.alerts))

    def backup_day(self, acct_q):
        """전 거래일 정산 끝, 장부 28 (추세 매도 대상), 07:00 장부 손상 -> 백업 복구 -> 아침 복구 (계좌 acct_q)"""
        self.held(28)
        pos = jl_path(self.R.POSITIONS)
        pos["next_open_sells"] = {"990010": {"reason": "추세 매도", "since": "2026-10-23", "attempts": 0, "last_status": None}}
        pos["sell_duty"] = {"990010": O.new_duty("trend", "2026-10-23 15:40:00", "추세 매도")}
        P.save(self.R.POSITIONS, pos)
        P.save(self.R.POSITIONS, pos)                                               # 백업
        with open(self.R.POSITIONS, "w", encoding="utf-8") as f:
            f.write('{"held": {"0846')
        self.prev_state(steps=("settle", "ac_ledger", "ac_positions", "ac_summary", "after_close"))
        self.acct(acct_q)
        self.clk.t = pd.Timestamp("2026-10-26 07:00:00")
        res = self.started("07:00:00", live=False)
        res.recover_positions()
        res.day.s["steps"].pop("prev_settle")
        res.prev_settle()
        return res

    def test_k4_premarket_backup_recovery_does_not_absorb_increase(self):
        res = self.backup_day(56)
        p = jl_path(self.R.POSITIONS)["held"]["990010"]
        self.assertEqual((p["qty_review"]["file_qty"], p["qty_review"]["account_qty"], p["qty_review"]["where"]), (28, 56, "아침 복구"))  # <커밋>: 56 흡수
        self.clk.at("08:30:00")
        res.trend_sell()
        self.assertEqual(sends(self.fk, "kt10001"), [])                             # <커밋>: 56주 추세 매도

    def test_k4_premarket_backup_recovery_normal(self):
        """같은 동작: 계좌 = 백업 장부면 그대로 -> 08:30 28주 추세 매도"""
        res = self.backup_day(28)
        p = jl_path(self.R.POSITIONS)["held"]["990010"]
        self.assertNotIn("qty_review", p)
        self.clk.at("08:30:00")
        res.trend_sell()
        self.assertEqual([x[2]["ord_qty"] for x in sends(self.fk, "kt10001")], ["28"])


def jl_path(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jl_version(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["_version"]


if __name__ == "__main__":
    unittest.main()
