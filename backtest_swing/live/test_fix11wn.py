import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import testguard; testguard.install()  # noqa: E401,E402,E702  (첫 줄 유지)
"""
11W M 보완 (서브 PC <커밋> 실측 11-10 22:16, 22:15 update 재시작 직후): 장 마감 뒤 재시작한 상주의 조회 명령이 그날 15:40 정산·체결 대신
전 거래일 정산 현금·현재가를 섞어 씀. 조회 전용 (주문·장부·정산 판단 데이터는 그대로). 가짜 서버·가짜 시계, 실제 호출 없음.

  python -m unittest backtest_swing/live/test_fix11wn.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import Base, bal_row  # noqa: E402
import common as C  # noqa: E402
import positions as P  # noqa: E402
import state as S  # noqa: E402
import telecmd as TC  # noqa: E402


def held(qty, avg, stop, entry="2026-11-02", kind="점검용", tid=None):
    return {"qty": qty, "avg_price": float(avg), "entry_price": float(avg), "stop_price": float(stop), "entry_date": entry, "kind": kind,
            "trade_id": tid or f"x-{entry}-{qty}"}


SETTLE = {"time": "15:40:05", "account": {"990005": 24, "990004": 20, "990009": 15}, "avg_price": {"990005": 41_200.0, "990004": 9_000.0, "990009": 12_000.0},
          "d2": 16_000_000.0, "equity": 17_000_000.0, "cur_price": {"990005": 40_950.0, "990004": 9_100.0, "990009": 11_900.0},
          "realized": {"pnl": -12_345.0, "n": 1, "unknown": [], "by_code": {"990006": -12_345.0}}}


class AfterCloseRestart(Base):
    """11-10: 15:21 매수 2종목·장중 매도 1종목, 15:40 정산 끝 -> 16:00 그대로 조회 vs 22:16 재시작 뒤 조회"""

    def setUp(self):
        super().setUp()
        self.clk.t = pd.Timestamp("2026-11-10 15:20:00")
        d9 = S.Day(os.path.join(self.tmp, "2026-11-09.json"), pd.Timestamp("2026-11-09"), clock=self.clk)
        d9.set("settlement", {"time": "15:40:00", "d2": 16_543_210.0, "equity": 17_500_000.0, "cur_price": {"990005": 39_000.0, "990006": 29_000.0}})
        P.save(self.R.POSITIONS, {"held": {"990005": held(24, 41_200, 38_237), "990006": held(9, 30_000, 27_000)}, "trade_seq": 2})
        self.fk.balance_rows = [bal_row("990005", 24, avg=41_200), bal_row("990006", 9, avg=30_000)]

    def day_done(self):
        """그날 상주가 끝까지 돈 것: 오늘 체결(확인 끝) + 15:40 정산 기록 + 15:40 보유 갱신된 장부"""
        res = self.started("15:20:00", live=False)
        self.filled_sell(res.om, code="990006", qty=9, price=29_000, stage="trend_sell")
        self.filled_buy(res.om, code="990004", qty=20, price=9_000)
        self.filled_buy(res.om, code="990009", qty=15, price=12_000)
        res.day.set("settlement", dict(SETTLE))
        res.day.set("positions_baseline", {"held": res.positions()["held"], "trade_seq": 2, "time": "15:40:30"})  # 15:40 보유 갱신 시작 때 장부 (ac_positions)
        P.save(self.R.POSITIONS, {"held": {"990005": held(24, 41_200, 38_237), "990004": held(20, 9_000, 8_500, entry="2026-11-10"),
                                           "990009": held(15, 12_000, 11_000, entry="2026-11-10")}, "trade_seq": 4})
        self.clk.at("16:00:00")
        return res

    @staticmethod
    def body(text):
        """시각이 들어가는 머리 줄·마지막 근거 줄의 '답 HH:MM' 을 뺀 본문"""
        ls = text.split("\n")
        return "\n".join(ls[1:-1])

    def test_restart_after_close_uses_that_days_settlement(self):
        res = self.day_done()
        v1 = res.query_view()
        h1, p1 = TC.holdings_text(v1), TC.pnl_text(v1)
        self.clk.at("22:16:00")                                                  # 22:15 update 로 재시작 (하루 끝 17:30 아님, 평소 16:30 뒤)
        res2 = self.resident()
        self.assertEqual(res2.tick(self.clk()), "idle")                          # 장 마감 뒤: 그날 상태를 만들지 않음 (self.day None)
        self.assertIsNone(res2.day)
        v2 = res2.query_view()
        h2, p2 = TC.holdings_text(v2), TC.pnl_text(v2)
        self.assertEqual(self.body(h2), self.body(h1))                           # 재시작 여부와 무관하게 같은 값
        self.assertEqual(self.body(p2), self.body(p1))
        cash = [ln for ln in p2.split("\n") if ln.strip().startswith("현금")][0].split()
        self.assertEqual(cash[1:], ["16,000,000"])                               # 오늘 정산 D+2 (예전: 전 거래일 16,543,210 + 오늘 체결 0건)
        self.assertIn("현금 오늘 정산 D+2 (15:40)", p2)
        self.assertIn("보유 15:40 정산 계좌 15:40", h2)
        self.assertIn("20주 · 평균 9,000 → 현재 9,100 (+1.1%)", h2)             # 오늘 산 종목 = 오늘 정산 현재가 (예전: 일봉 11-09 종가·모름)
        self.assertIn("24주 · 평균 41,200 → 현재 40,950", h2)                   # 예전: 11-09 15:40 정산 39,000
        self.assertNotIn("11-09", h2)
        tot = 16_000_000 + 24 * 40_950 + 20 * 9_100 + 15 * 11_900
        self.assertEqual([ln for ln in p2.split("\n") if ln.startswith("총자산")][0].split()[-1], f"{tot:,.0f}")
        self.assertEqual([ln for ln in p2.split("\n") if ln.startswith("오늘 실현")][0].split()[2], "-12,345")   # 그날 정산 실현
        self.assertIn("2026-10-26 부터 1거래", p2)                               # 그날 매도 체결도 누적에 (예전: 주문 기록 없음 -> 0거래)

    def test_status_after_close(self):
        self.day_done()
        self.clk.at("22:16:00")
        res2 = self.resident()
        res2.tick(self.clk())
        st = TC.status_text(res2.query_view())
        self.assertIn("장 마감 후 (11-10 정산 끝)", st)                          # 예전: 하루 시작 전
        self.assertNotIn("하루 시작 전", st)
        self.assertIn(f"코드  {C.git_state()}", st)                              # 지금 돌고 있는 상주 코드 (그날 하루 시작 기록 아님)

    def test_status_same_process_after_close(self):
        res = self.day_done()
        st = TC.status_text(res.query_view())
        self.assertIn("장 마감 후 (11-10 정산 끝)", st)

    def test_next_morning_before_start_uses_latest_settlement(self):
        """다음 날 07:00 전 재시작: 가장 최근에 끝난 정산 = 11-10 (현금·현재가 모두 11-10)"""
        self.day_done()
        self.clk.t = pd.Timestamp("2026-11-11 06:30:00")
        res2 = self.resident()
        v = res2.query_view()
        p, h = TC.pnl_text(v), TC.holdings_text(v)
        self.assertIn("현금 전 거래일(11-10) 정산 D+2 16,000,000", p)
        self.assertIn("현재 9,100 (+1.1%)", h)
        self.assertIn("11-10 15:40 정산", h)


class NoteViewIsolated(Base):
    def test_note_view_error_does_not_skip_qty_review(self):
        self.clk.t = pd.Timestamp("2026-11-10 10:00:00")
        P.save(self.R.POSITIONS, {"held": {"990010": held(28, 31_000, 30_000)}, "trade_seq": 1})
        self.fk.balance_rows = [bal_row("990010", 56, avg=31_000)]                # 계좌 > 장부 -> 수동 확인 표시
        res = self.started("10:00:00", live=False)
        self.quotes["990010"] = 33_000.0

        def boom(r):
            raise RuntimeError("조회용 값 저장 실패")
        res.note_view = boom
        r = res.stop_minute()
        self.assertEqual(r["qty_changed"], {"990010": {"file": 28, "account": 56}})
        self.assertIn("qty_review", res.positions()["held"]["990010"])           # 예전: 예외로 표시·저장을 건너뜀
        logs = ""
        for fn in os.listdir(os.path.join(self.R.LOG_DIR, "resident")):          # 상주 로그 파일 이름은 실제 날짜
            with open(os.path.join(self.R.LOG_DIR, "resident", fn), encoding="utf-8") as f:
                logs += f.read()
        self.assertIn("조회용 값 저장 실패", logs)


if __name__ == "__main__":
    unittest.main()
