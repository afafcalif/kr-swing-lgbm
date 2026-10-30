"""
Codex 확인(aba7392..5f192a3) 반영 (11K 보완 3: 분할 자동 조정 제거, LIVE_CONFIG 시작 거부, 일봉 누락 = 공식 거래 종목 기준) 재현 시험.
가짜 서버·가짜 시계, 실제 주문·조회·logs/exec·data 없음.

  python -m unittest backtest_swing/live/test_broad9.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import subprocess
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import bal_row, jl  # noqa: E402
from test_orders import sends  # noqa: E402
from test_broad6 import HELD, Base6, pos_with  # noqa: E402
import common as C  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402

LIVE = os.path.dirname(os.path.abspath(__file__))
OTHER = {"qty": 10, "trade_id": "005930-2026-10-20-2", "entry_date": "2026-10-20", "stop_price": 200000.0, "kind": "본래",
         "judged_on": "2026-10-23", "judged_by": "공식 일봉 (다음 날 재판단)"}


def intent(code, side, status, day="2026-10-23", **kw):
    return {"intent_id": f"{day}-{side}-{code}-{kw.pop('n', 1)}", "date": day, "stage": "buy" if side == "buy" else "stop", "side": side, "code": code,
            "qty": 28, "price": 33350 if side == "buy" else None, "status": status, **kw}


class QtyReview(Base6):
    def review(self):
        return jl(self.R.POSITIONS)["held"]["990006"].get("qty_review")

    def test_normal_buy_then_1540_interrupted_not_review(self):
        """10-23 매수 28 체결, 15:40 보유 갱신이 기준 장부만 저장하고 꺼짐 (positions.json 은 28, 맞춘 날 10-22) -> 아침 계좌 56 은 매수로 설명"""
        d = self.prev_state(steps=("snapshot", "settle"), intents=[intent("990006", "buy", O.ACCEPTED, filled_qty=28, fill_price=33000, pre_qty=28)],
                            settlement={"d2": 30_000_000.0, "account": {"990006": 56}})
        d.set("positions_baseline", {"held": {"990006": dict(HELD)}, "trade_seq": 1, "time": "15:40:10"})
        pos = pos_with()
        pos["qty_synced"] = {"date": "2026-10-22", "time": "15:40:30", "by": "15:40 보유 갱신"}
        P.save(self.R.POSITIONS, pos)
        self.fk.balance_rows = [bal_row("990006", 56)]
        res = self.started("07:10:00", live=False)
        res.prev_settle()
        p = jl(self.R.POSITIONS)
        self.assertEqual((p["held"]["990006"]["qty"], self.review(), res.day.s["prev_recovery"]["qty_review"]), (56, None, {}))
        self.assertFalse(any("[수동 확인 필요]" in a for a in self.alerts))
        self.assertEqual(p["qty_synced"]["date"], "2026-10-26")

    def test_synced_book_then_2_to_1_is_review_not_adjusted(self):
        pos = pos_with(dict(HELD, qty=56))
        pos["qty_synced"] = {"date": "2026-10-23", "time": "15:40:30", "by": "15:40 보유 갱신"}
        self.prev_state(steps=("snapshot", "settle"), intents=[intent("990006", "buy", O.ACCEPTED, filled_qty=28, fill_price=33000, pre_qty=28)])
        P.save(self.R.POSITIONS, pos)
        self.fk.balance_rows = [bal_row("990006", 112)]
        res = self.started("07:10:00", live=False)
        res.prev_settle()
        r = self.review()
        h = jl(self.R.POSITIONS)["held"]["990006"]
        self.assertEqual((r["file_qty"], r["account_qty"], r["where"], h["stop_price"]), (56, 112, "아침 복구", 30000.0))   # 장부 반영된 매수는 설명 아님

    def test_intraday_2_to_1_review_pauses_only_that_code(self):
        duty = {"990006": O.new_duty("trend", "2026-10-23 15:41:00", "추세")}
        pos = pos_with(dict(HELD, entry_price=33000.0), duty, {"990006": {"since": "2026-10-23", "attempts": 0}})
        pos["held"]["005930"] = dict(OTHER)
        P.save(self.R.POSITIONS, pos)
        self.fk.balance_rows = [bal_row("990006", 56, avg=16500), bal_row("005930", 10)]
        res = self.started("10:00:00", live=False)
        self.quotes.update({"990006": 16000.0, "005930": 199000.0})
        r = res.stop_minute()
        self.assertEqual(r["qty_changed"], {"990006": {"file": 28, "account": 56}})
        rv = self.review()
        self.assertEqual((rv["file_qty"], rv["account_qty"], rv["ratio"], rv["stop"], rv["price"]), (28, 56, "56/28 = 2.000", 30000.0, 16000.0))
        h = jl(self.R.POSITIONS)["held"]["990006"]
        self.assertEqual((h["stop_price"], h["entry_price"]), (30000.0, 33000.0))                    # 자동 조정 없음
        a = next(x for x in self.alerts if x.startswith("[수동 확인 필요] 990006"))
        for part in ("장부 28주", "계좌 56주", "56/28 = 2.000", "손절가 30000", "현재가 16000"):
            self.assertIn(part, a)
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["005930"])           # 다른 종목 손절은 그대로, 990006 추세 의무·손절 멈춤
        self.clk.at("10:01:00")
        self.quotes["990006"] = 10000.0
        res.stop_minute()
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["005930"])
        self.assertFalse(res.day.s.get("buy_block"))                                               # 전체 매수 차단 아님
        self.assertFalse(any("990006" in w for w in res.buy_gate()))
        self.assertEqual(P.review_codes(jl(self.R.POSITIONS)), {"990006"})                          # 같은 종목 매수는 주문 계획에서 뺌 (snapshot_and_plan)
        pos = jl(self.R.POSITIONS)
        pos["held"]["990006"]["stop_price"] = 15000.0                                              # 사람이 손절가를 고침 -> 끝
        P.save(self.R.POSITIONS, pos)
        self.clk.at("10:02:00")
        res.stop_minute()
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["005930", "990006"])  # 감시·의무 매도 다시

    def test_premarket_trend_sell_skips_review_code(self):
        duty = {"990006": O.new_duty("trend", "2026-10-23 15:41:00", "추세")}
        P.save(self.R.POSITIONS, pos_with(dict(HELD, judged_by="공식 일봉 (다음 날 재판단)"), duty, {"990006": {"since": "2026-10-23", "attempts": 0}}))
        self.fk.balance_rows = [bal_row("990006", 140)]
        res = self.started("08:30:00", live=False)
        res.trend_sell()
        self.assertEqual(sends(self.fk, "kt10001"), [])
        self.assertEqual(self.review()["where"], "08:30 장 전 계좌 대조")
        self.assertTrue(res.day.s["premarket_account_check"]["ok"])

    def test_past_review_with_two_unknown_keeps_stop(self):
        rv = {"date": "2026-10-22", "file_qty": 14, "account_qty": 28, "stop": 30000.0, "ratio": "28/14 = 2.000"}
        P.save(self.R.POSITIONS, pos_with(dict(HELD, qty_review=rv)))
        self.prev_state(steps=("snapshot",), intents=[intent("990006", "buy", O.UNKNOWN, n=1, pre_qty=28), intent("990006", "sell", O.UNKNOWN, n=2, pre_qty=28)])
        self.fk.balance_rows = [bal_row("990006", 28)]
        res = self.started("07:10:00", live=False)
        res.prev_settle()
        h = jl(self.R.POSITIONS)["held"]["990006"]
        self.assertEqual((h["stop_price"], h.get("ambiguous"), h.get("info_pending"), h["qty_review"]["stop"]), (30000.0, None, None, 30000.0))
        self.assertIn("수동 확인 중", res.day.s["prev_recovery"]["ambiguous"]["990006"]["note"])
        self.assertEqual({i["status"] for i in jl(os.path.join(self.tmp, "intents", "2026-10-23.json"))}, {O.UNKNOWN})   # 의도는 그대로 UNKNOWN

    def test_official_rejudge_and_recovery_do_not_touch_review_stop(self):
        rv = {"date": "2026-10-23", "file_qty": 28, "account_qty": 56, "stop": 30000.0, "ratio": "56/28 = 2.000"}
        P.save(self.R.POSITIONS, pos_with(dict(HELD, qty=56, entry_date="2026-10-23", qty_review=rv, stop_temp=True)))
        res = self.started("08:20:00", live=False)
        self.judged["990006"] = {"close": 16000.0, "stop": 14000.0, "trend": True, "basis": "수정주가 일봉"}
        res.official_vs_1530()
        res.day.set("prev_recovery", {"prev": "2026-10-23", "finalized": False})
        res.apply_recovery_fixes({"prev": "2026-10-23", "stops": {"990006": {"stop": 13000.0, "basis": "x"}}, "trend": {"990006": True}, "left": [], "at": "08:00"})
        p = jl(self.R.POSITIONS)
        self.assertEqual((p["held"]["990006"]["stop_price"], p["sell_duty"]), (30000.0, {}))
        self.assertIn("수동 확인 중", res.day.s["official_vs_1530"]["diffs"][0]["note"])
        self.assertEqual(res.rejudge_pending({"x": dict(HELD, qty_review=rv)}, "2026-10-23"), [])     # 08:30 추세 매도가 기다리지 않음

    def test_1540_unexplained_change_is_review_not_pending(self):
        P.save(self.R.POSITIONS, pos_with())
        self.fk.balance_rows = [bal_row("990006", 56)]
        res = self.started("15:40:00", live=False)
        res.day.set("settlement", {"account": {"990006": 56}, "avg_price": {"990006": 16500}})
        self.cv["990006"] = {"close": 16000.0}
        self.judged["990006"] = {"close": 16000.0, "stop": 14000.0, "trend": True, "basis": "수정주가 일봉"}
        out = res.ac_positions()
        h = jl(self.R.POSITIONS)["held"]["990006"]
        self.assertEqual((h["qty"], h["stop_price"], h.get("info_pending"), h["qty_review"]["where"]), (56, 30000.0, None, "15:40 보유 갱신"))
        self.assertNotIn("990006", out["next_open_sells"])                                          # 추세 판단도 안 함
        self.assertEqual(jl(self.R.POSITIONS)["qty_synced"]["date"], "2026-10-26")


class LiveConfigGuard(unittest.TestCase):
    def test_entry_points_refuse_with_live_config(self):
        env = {**os.environ, "LIVE_CONFIG": "example", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "PYTHONIOENCODING": "utf-8"}   # 알림은 보내지 않게
        for script, args in (("resident.py", []), ("watchdog.py", []), ("control.py", ["status"]), ("update.py", [])):
            r = subprocess.run([sys.executable, os.path.join(LIVE, script), *args], env=env, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120)
            self.assertEqual(r.returncode, 9, (script, r.stdout[-300:], r.stderr[-300:]))
            self.assertIn("[시작 거부]", r.stderr)


class MissingWholeCode(Base6):
    def test_code_absent_from_append_but_traded_officially_is_excluded(self):
        import daily
        from kiwoom import krx_official
        ad, cache = os.path.join(self.tmp, "append"), os.path.join(self.tmp, "cache")
        os.makedirs(ad)
        os.makedirs(cache)
        d1, d2 = pd.Timestamp("2026-10-22"), pd.Timestamp("2026-10-23")

        def bars(code, close, ts, extra):
            return pd.DataFrame({"code": code, "timestamp": ts, "open": close, "high": close, "low": close, "close": close, "volume": 10.0, **extra})
        bars("000001", 100.0, [d1], {"amount": 1000.0}).to_parquet(os.path.join(ad, "adj.parquet"), index=False)
        bars("000001", 100.0, [d1], {"factor": 1.0}).to_parquet(os.path.join(ad, "raw.parquet"), index=False)
        pd.DataFrame({"name": "kospi", "timestamp": [d1], "close": [1.0]}).to_parquet(os.path.join(ad, "index.parquet"), index=False)
        bars("000001", 105.0, [d2], {"amount": 1050.0}).to_parquet(os.path.join(ad, "pending_adj.parquet"), index=False)
        bars("000001", 105.0, [d2], {}).to_parquet(os.path.join(ad, "pending_raw.parquet"), index=False)
        pd.DataFrame({"name": "kospi", "timestamp": [d2], "close": [2.0]}).to_parquet(os.path.join(ad, "pending_index.parquet"), index=False)
        C.atomic_json(os.path.join(ad, "update.json"), {"official_missing_dates": ["2026-10-23"], "official_received": {}, "dates": ["2026-10-22"]})
        pd.DataFrame({"x": [1]}).to_parquet(os.path.join(cache, "20261023.parquet"))
        off = pd.DataFrame({"date": [d2] * 3, "code": ["000001", "000002", "000003"], "volume": [10, 5, 0]})
        saved = (daily.APPEND_DIR, krx_official.load_range, krx_official.apply_regular_session, krx_official.CACHE_DIR, daily.sd.universe_codes)
        try:
            daily.APPEND_DIR, krx_official.CACHE_DIR = ad, cache
            krx_official.load_range = lambda a, b: off
            krx_official.apply_regular_session = lambda df, o: (df, None)
            daily.sd.universe_codes = lambda: ["000001", "000002", "000003"]           # 000002 = 추가분·보관 행에 처음부터 없음
            r = daily.reapply_official()
            u = jl(daily.data_file("update.json", ad))
        finally:
            (daily.APPEND_DIR, krx_official.load_range, krx_official.apply_regular_session, krx_official.CACHE_DIR, daily.sd.universe_codes) = saved
        self.assertTrue(r["ok"])
        self.assertEqual(u["missing_row"], [["000002", ["2026-10-23"]]])                # 000003 은 거래량 0 이라 안 셈
        daily.APPEND_DIR = ad
        try:
            self.assertIn("000002", daily.excluded_codes()["missing_row"])
        finally:
            daily.APPEND_DIR = saved[0]


if __name__ == "__main__":
    unittest.main()
