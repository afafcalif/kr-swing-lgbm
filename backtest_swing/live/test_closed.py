"""
11N (2026-10-29 서브 PC 실측) 재현 시험: 손절 -> 전량 체결 -> 의무 끝(계좌 보유 0 두 번) 뒤 계좌 조회 502 인 분에 보유 파일(15:40 갱신 전)만 보고
판 종목을 다시 손절 감지하던 것. 오늘 판 종목 표시(closed_today)로 새 손절 감지·15:18 매수 계획의 의무 판단·추세 매도에서 뺀다.
가짜 서버·가짜 시계, 실제 주문·조회·logs/exec 없음.

  python -m unittest backtest_swing/live/test_closed.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_codex  # noqa: E402
from test_broad3 import STOP, cycle, fail, positions, qtys, setup  # noqa: E402
from test_codex import Base, bal_row, jl  # noqa: E402
import engine as E  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import quotes as Q  # noqa: E402

TID2 = "005930-2026-10-20-2"


class Closed(Base):
    def setUp(self):
        super().setUp()
        self.logs = []
        saved = self.R.rlog
        self.R.rlog = lambda msg, level=None: self.logs.append(msg)
        self.addCleanup(lambda: setattr(self.R, "rlog", saved))

    def sold(self):
        """10:00 손절 감지 -> 28주 시장가 접수 -> 전량 체결 -> 10:01 보유 0 (한 번) -> 10:02 의무 끝. 005930 은 손절가 위로 계속 보유"""
        pos = positions()
        pos["held"]["005930"] = {"qty": 10, "trade_id": TID2, "entry_date": "2026-10-20", "stop_price": 50000.0, "kind": "본래"}
        P.save(self.R.POSITIONS, pos)
        self.fk.balance_rows = [bal_row("990006", 28), bal_row("005930", 10)]
        res = self.started("10:00:00", live=False)
        self.quotes.update({"990006": 29900.0, "005930": 51000.0})
        res.stop_minute()
        self.fk.fill(res.om.records[0]["ord_no"], 28, 29900, "10:00:10")
        self.clk.at("10:01:00")
        self.assertEqual(res.stop_minute()["duty_zero"], ["990006"])
        self.clk.at("10:02:00")
        self.assertEqual(res.stop_minute()["duty_ended"], ["990006"])
        pos = jl(self.R.POSITIONS)
        self.assertEqual((pos["sell_duty"], pos["closed_today"]["990006"]["date"]), ({}, "2026-10-26"))
        self.assertEqual(pos["held"]["990006"]["qty"], 28)                       # 보유 파일은 15:40 까지 그대로 (기준선)
        return res

    def stop_alerts(self):
        return [a for a in self.alerts if a.startswith("[손절 진행 중] 990006")]

    def test_a_account_502_after_duty_ended_no_redetect(self):
        res = self.sold()
        self.quotes["990006"] = 28000.0                                          # 계속 손절가 아래
        self.clk.at("10:03:00")
        self.fk.read_script["kt00018"] = ["http502"]
        r = res.stop_minute()
        self.assertFalse(r["account_ok"])
        self.assertEqual((r["triggered_new"], r["closed_today"]), ({}, ["990006"]))
        self.assertEqual(jl(self.R.POSITIONS)["sell_duty"], {})                 # 의무 없음
        self.assertEqual(len(self.stop_alerts()), 1)                            # 10:00 처음 한 번만
        self.assertEqual(qtys(self.fk), [28])                                   # 주문 없음
        self.assertNotIn("990006", r["quotes"])                                 # 판 종목은 시세 조회도 안 함
        line = self.logs[-1]
        self.assertIn("보유 1종목 (뺀 종목 ['990006'])", line)                  # 감시 로그: 판 종목 뺌, 부호 형식
        self.assertIn("005930 (+2.0%)", line)

    def test_b_plan_keeps_closed_code_buyable(self):
        res = self.sold()
        self.quotes["990006"] = 28000.0
        self.clk.at("15:17:00")
        self.fk.read_script["kt00018"] = ["http502"]
        res.stop_minute()
        self.assertEqual(jl(self.R.POSITIONS)["sell_duty"], {})
        res = test_codex.Codex.snapshot_ready(self)                              # 15:18:40 주문 계획 (상위 3 에 990006)
        real = Q.snapshot
        raw = pd.DataFrame({"stk_cd": ["990006", "005930"], "upl_pric": ["+42900", "+78000"], "base_pric": ["33000", "60000"]})
        Q.snapshot = lambda caller, cs, **k: (raw,) + tuple(real(caller, cs)[1:])
        top = pd.DataFrame({"code": ["990006"], "score": [0.9], "raw_close": [33000.0]})
        out = {"filter": {"filter_ok": True}, "eligible": pd.DataFrame({"code": ["990006"], "score": [0.9]}), "market": {}, "errors": [],
               "ledgers": {n: {"top3": top, "busy": [], "skipped_busy": [], "ledger_status": []} for n in ("strategy", "full")}}
        saved = E.run_day
        try:
            E.run_day = lambda *a, **k: out
            res.snapshot_and_plan()
        finally:
            E.run_day = saved
        p = res.day.s["live"]["plan"][0]
        self.assertEqual((p["code"], p["skip"]), ("990006", ""))                # 매수 대상으로 남음 (의무 남음 아님)
        self.assertGreater(p["qty"], 0)

    def test_c_held_again_clears_mark(self):
        res = self.sold()
        self.fk.balance_rows.append(bal_row("990006", 5))                        # 같은 날 다시 보유 (재매수 등)
        self.clk.at("10:03:00")
        r = res.stop_minute()
        self.assertEqual(r["closed_today"], [])
        self.assertNotIn("closed_today", jl(self.R.POSITIONS))                   # 표시 해제 -> 원래대로 (감시 대상)
        self.assertIn("990006", r["quotes"])

    def test_d_next_day_mark_invalid(self):
        pos = positions()
        pos["closed_today"] = {"990006": {"date": "2026-10-23", "at": "10:02:00", "kind": "stop"}}
        self.assertEqual(O.closed_codes(pos, "2026-10-23"), {"990006"})
        self.assertEqual(O.closed_codes(pos, "2026-10-26"), set())
        clk, fk, om, notes = setup("2026-10-26 10:00:00")                       # 다음 날: 표시 무효 -> 계좌 조회 실패여도 원래 규칙
        r = cycle(om, fk, pos, quotes={"990006": 29900.0}, balance=fail)
        self.assertEqual(list(r["triggered_new"]), ["990006"])
        fk.balance_rows = [bal_row("990006", 28)]
        O.end_duties(pos, O.holdings(fk.request("kt00018", "POST", "/x").json()), clk())
        self.assertNotIn("closed_today", pos)                                    # 성공한 계좌 조회에서 지난 표시는 지움

    def test_e_1540_update_clears_mark(self):
        res = self.sold()
        self.clk.at("15:45:00")
        res.day.set("settlement", {"account": {"005930": 10}, "avg_price": {"005930": 10000}})
        self.cv["005930"] = {"close": 51000, "open": 51000}
        self.judged["005930"] = {"close": 51000.0, "stop": 50000.0, "trend": False, "basis": "수정주가 일봉"}
        res.om.store.update(res.om.records[0], filled_qty=28, fill_price=29900.0, fill_status="체결")
        res.om.refresh_fills = lambda *a, **k: None
        res.ac_positions()
        pos = jl(self.R.POSITIONS)
        self.assertEqual((sorted(pos["held"]), pos["sell_duty"], pos["next_open_sells"]), (["005930"], {}, {}))
        self.assertNotIn("closed_today", pos)

    def test_f_trend_targets_skip_closed(self):
        pos = positions(STOP, listed=True)
        pos["closed_today"] = {"990006": {"date": "2026-10-26", "at": "08:31:00", "kind": "trend"}}
        P.save(self.R.POSITIONS, pos)
        self.fk.balance_rows = [bal_row("990006", 28)]
        res = self.started("08:31:00", live=False)
        self.assertEqual(res.trend_new_targets(), [])


if __name__ == "__main__":
    unittest.main()
