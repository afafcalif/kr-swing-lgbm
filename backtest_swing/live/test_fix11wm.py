import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import testguard; testguard.install()  # noqa: E401,E402,E702  (첫 줄 유지)
"""
11W M: 텔레그램 조회 명령 보완 (서브 PC 실측 11-08·11-09). 가짜 서버·가짜 시계·가짜 텔레그램, 실제 주문·조회·텔레그램 없음.

  python -m unittest backtest_swing/live/test_fix11wm.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import json
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import Base, bal_row  # noqa: E402
import common as C  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import state as S  # noqa: E402
import telecmd as TC  # noqa: E402

TOKEN, CHAT = "123456:FAKE_token_for_tests_only_xxxxxxxx", "555"


def held(qty, avg, stop, entry="2026-11-02", kind="점검용", tid=None, **kw):
    return {"qty": qty, "avg_price": float(avg), "entry_price": float(avg), "stop_price": float(stop), "entry_date": entry, "kind": kind,
            "trade_id": tid or f"x-{entry}-{qty}", **kw}


class MBase(Base):
    def state(self, day, settlement=None, baseline=None, intents=None, **extra):
        d = S.Day(os.path.join(self.tmp, f"{day}.json"), pd.Timestamp(day), clock=self.clk)
        if settlement:
            d.set("settlement", settlement)
        if baseline is not None:
            d.set("positions_baseline", {"held": baseline, "trade_seq": 1, "time": "15:41:00"})
        for k, v in extra.items():
            d.set(k, v)
        if intents is not None:
            p = os.path.join(self.tmp, "intents", f"{day}.json")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            C.atomic_json(p, intents)
        return d

    def lines(self, text, head):
        """'head' 로 시작하는 줄 (앞 공백 무시)"""
        return [ln for ln in text.split("\n") if ln.strip().startswith(head)]


# ---------------------------------------------------------------- 1 장중 보유 = 매 분 계좌 (11-09 실측: 08:30 추세 매도로 판 990006 이 남음)

class TodaySold(MBase):
    def setUp(self):
        super().setUp()
        self.clk.t = pd.Timestamp("2026-11-09 08:29:00")
        self.state("2026-11-06", settlement={"d2": 18_000_000.0, "equity": 28_412_350.0, "time": "15:40:12",
                                             "cur_price": {"990005": 40_800.0, "990006": 28_500.0}})
        P.save(self.R.POSITIONS, {"held": {"990005": held(24, 41_200, 38_237, kind="점검용"),
                                           "990006": held(9, 30_000, 27_000, kind="점검용", trend_sell_next_open=True)},
                                  "next_open_sells": {"990006": {"since": "2026-11-06", "attempts": 0}}, "trade_seq": 2})
        self.fk.balance_rows = [bal_row("990005", 24, avg=41_200), bal_row("990006", 9, avg=30_000)]
        self.fk.auto_fill["990006"] = 28_000                                    # 시장가 매도는 접수 즉시 체결 (가짜 서버)

    def run_morning(self):
        res = self.started("08:30:00", live=False)
        res.om.trend_sells(["990006"], {"990006": {"file_qty": 9, "acct": {"qty": 9, "sellable": 9}}}, kind_of=lambda c: "점검용")
        self.clk.at("09:43:00")
        self.quotes.update({"990005": 40_550.0, "990006": 28_050.0})
        res.stop_minute()                                                       # 계좌: 990006 없음 (positions.json 은 15:40 까지 그대로 9주)
        return res

    def test_sold_today_out_of_holdings_and_cash_estimated(self):
        res = self.run_morning()
        self.assertEqual(res.positions()["held"]["990006"]["qty"], 9)          # 장부는 그대로 (15:40 에 반영)
        v = res.query_view()
        h, pn = TC.holdings_text(v), TC.pnl_text(v)
        held_part, sold_part = h.split("[오늘 매도]")
        self.assertIn("[보유] 1종목 · 시세 09:43", h)
        self.assertNotIn("990006", held_part)                                   # 예전: 보유에 9주로 남음
        self.assertIn("990005", held_part)
        self.assertIn("24주 · 평균 41,200 → 현재 40,550 (-1.6%)", held_part)
        self.assertIn("손절가 38,237까지 -5.7%", held_part)
        self.assertIn("990006", sold_part)
        self.assertIn("9주 · 추세 매도 · 점검용 · 체결 확인 전", sold_part)
        self.assertIn(f"대금 약 {9 * 28_050:,.0f} (추정, 시세 09:43)", sold_part)
        self.assertIn("계좌 09:43 (매 분 손절 감시)", h)
        rate, tax = self.R.commission_rate(), self.R.sb_tax(2026)
        amt = 9 * 28_050
        cash = 18_000_000.0 + amt - P.kiwoom_fee(amt, rate) - P.kiwoom_tax(amt, tax)
        stock = 24 * 40_550
        self.assertEqual(self.lines(pn, "총자산")[0].split()[-1], f"{cash + stock:,.0f}")   # 990006 은 주식이 아니라 현금(추정) 쪽
        self.assertEqual(self.lines(pn, "현금")[0].split()[1:], [f"{cash:,.0f}", "(추정)"])
        self.assertEqual(self.lines(pn, "주식")[0].split()[-1], f"{stock:,.0f}")
        self.assertEqual(self.lines(pn, "보유 평가손익")[0].split()[-1], f"{24 * (40_550 - 41_200):+,.0f}")
        cost = 9 * 30_000
        rz = amt - cost - P.kiwoom_fee(amt, rate) - P.kiwoom_tax(amt, tax) - P.kiwoom_fee(cost, rate)   # 확인 전: 현재가로 추정
        self.assertEqual(self.lines(pn, "오늘 실현")[0].split()[2:], [f"{rz:+,.0f}", "(추정", "포함)"])
        self.assertIn(f"실현 {rz:+,.0f} (진입 30,000 대비, 비용 포함, 추정)", sold_part)
        self.assertIn("오늘 매도 1종목 대금은 현금 쪽 (체결 확인 전 추정)", pn)
        td = TC.today_text(v)                                                   # 종목 이름은 환경마다 (목록 파일) 달라 빼고 확인
        self.assertIn("08:30:00 추세 매도 점검용\n", td)
        self.assertIn(" 990006 9주 시장가\n  ACCEPTED · 체결 확인 전", td)

    def test_after_fill_check_exact_amount(self):
        res = self.run_morning()
        it = [r for r in res.om.records if r["code"] == "990006"][0]
        res.om.store.update(it, filled_qty=9, fill_price=28_000, fill_status="체결")   # 15:35 체결 확인 뒤
        v = res.query_view()
        h, pn = TC.holdings_text(v), TC.pnl_text(v)
        self.assertIn("9주 · 추세 매도 · 점검용\n", h)
        self.assertIn(f"대금 {9 * 28_000:,.0f} (체결 평균 28,000)", h)
        self.assertNotIn("(추정)", self.lines(pn, "현금")[0])
        rate, tax = self.R.commission_rate(), self.R.sb_tax(2026)
        amt, cost = 9 * 28_000, 9 * 30_000
        rz = amt - cost - P.kiwoom_fee(amt, rate) - P.kiwoom_tax(amt, tax) - P.kiwoom_fee(cost, rate)
        self.assertEqual(self.lines(pn, "오늘 실현")[0].split()[2], f"{rz:+,.0f}")
        self.assertIn(f"실현 {rz:+,.0f} (진입 30,000 대비, 비용 포함)\n", h)

    def test_before_open_order_pending_stays_in_holdings(self):
        """09:00 전 (계좌 조회 전): 추세 매도 주문은 체결 전 -> 보유에 '매도 주문 중'"""
        self.fk.auto_fill.clear()
        res = self.started("08:30:00", live=False)
        res.om.trend_sells(["990006"], {"990006": {"file_qty": 9, "acct": {"qty": 9, "sellable": 9}}}, kind_of=lambda c: "점검용")
        self.clk.at("08:40:00")
        h = TC.holdings_text(res.query_view())
        self.assertIn("[보유] 2종목", h)
        self.assertIn("매도 주문 중 (체결 전)", h)
        self.assertNotIn("[오늘 매도]", h)
        self.assertIn("장부 (저장", h)


# ---------------------------------------------------------------- 2 현재가 대체·모름 (11-08 실측: 현재가 모름인데 평가손익 +0)

class PriceFallback(MBase):
    def holiday(self, settle_px):
        self.clk.t = pd.Timestamp("2026-11-08 10:00:00")                       # 일요일
        self.state("2026-11-06", settlement={"d2": 20_000_000.0, "equity": 29_000_000.0, "time": "15:40:12", "cur_price": settle_px})
        P.save(self.R.POSITIONS, {"held": {"990005": held(24, 41_200, 38_237), "990006": held(9, 30_000, 27_000)}, "trade_seq": 2})
        res = self.resident()
        res.day = self.R.new_day(self.clk(), clock=self.clk)
        res.day.mark("holiday", {"weekday": 6})
        return res

    def test_settlement_price_then_unknown(self):
        res = self.holiday({"990005": 40_800.0})                                # 990006 은 어디에도 현재가 없음
        v = res.query_view()
        h, pn = TC.holdings_text(v), TC.pnl_text(v)
        self.assertIn("24주 · 평균 41,200 → 현재 40,800 (-1.0%)", h)
        self.assertIn("[보유] 2종목 · 11-06 15:40 정산", h)
        self.assertIn("9주 · 평균 30,000 → 현재 모름", h)
        self.assertIn("손절가 27,000까지 모름", h)
        self.assertEqual(self.lines(pn, "보유 평가손익")[0].split()[-1], "모름")    # 예전: +0
        self.assertEqual(self.lines(pn, "총자산")[0].split()[-1], "모름")
        self.assertIn("현재가 모름 ['990006']", pn)

    def test_daily_close_fills_missing(self):
        res = self.holiday({"990005": 40_800.0})
        res.last_close_fn = lambda codes: {c: ("2026-11-06", 27_900.0) for c in codes}
        v = res.query_view()
        h, pn = TC.holdings_text(v), TC.pnl_text(v)
        self.assertIn("현재 27,900 (-7.0%) [일봉 11-06 종가]", h)
        self.assertEqual(self.lines(pn, "보유 평가손익")[0].split()[-1], f"{24 * (40_800 - 41_200) + 9 * (27_900 - 30_000):+,.0f}")
        self.assertEqual(self.lines(pn, "총자산")[0].split()[-1], f"{20_000_000 + 24 * 40_800 + 9 * 27_900:,.0f}")

    def test_yesterday_quote_not_used_as_today(self):
        """상주가 날을 넘겨 켜져 있으면 view 에 지난 날 시세가 남음 -> 오늘 시세로 쓰지 않고 정산 현재가"""
        res = self.holiday({"990005": 40_800.0, "990006": 28_500.0})
        res.view = {"quotes": {"990005": {"px": 1.0, "t": "15:28:00", "d": "2026-11-06"}},
                    "acct": {"t": "15:28:00", "d": "2026-11-06", "holdings": {"990005": {"qty": 99, "avg_price": 1.0, "cur_price": 1.0}}}}
        h = TC.holdings_text(res.query_view())
        self.assertIn("24주 · 평균 41,200 → 현재 40,800", h)
        self.assertIn("장부 (저장", h)


# ---------------------------------------------------------------- 3 /status 휴장일·하루 시작 전 (11-08 실측: env -, 주문 끔)

class StatusConfig(MBase):
    def test_holiday_shows_real_config(self):
        self.R.CFG.update(env="mock", orders_enabled=True, allow_prod_orders=False)
        self.clk.t = pd.Timestamp("2026-11-08 10:00:00")
        res = self.resident()
        res.day = self.R.new_day(self.clk(), clock=self.clk)
        res.day.mark("holiday", {"weekday": 6})
        st = TC.status_text(res.query_view())
        self.assertIn("env   mock · 주문 켬", st)                                 # 예전: env - , 주문 끔
        self.assertIn("휴장일 · 상주 설정", st)
        self.assertIn(f"코드  {C.git_state()}", st)

    def test_prod_without_allow_says_off(self):
        self.R.CFG.update(env="prod", orders_enabled=True, allow_prod_orders=False)
        self.clk.t = pd.Timestamp("2026-11-09 06:50:00")
        res = self.resident()
        st = TC.status_text(res.query_view())
        self.assertIn("env   prod · 주문 끔 (실전 주문 허용 안 함 (allow_prod_orders))", st)
        self.assertIn("하루 시작 전 · 상주 설정", st)

    def test_after_day_start_uses_core_start(self):
        res = self.started("10:00:00", live=False)
        st = TC.status_text(res.query_view())
        self.assertIn("(하루 시작 10:00)", st)
        self.assertIn("주문 켬", st)


# ---------------------------------------------------------------- 4 누적 실현손익·총손익·실전 수수료

class Cumulative(MBase):
    def setUp(self):
        super().setUp()
        self.clk.t = pd.Timestamp("2026-10-29 10:00:00")
        A = held(10, 10_000, 9_000, entry="2026-10-26", kind="점검용", tid="A-1")
        B = held(5, 20_000, 18_000, entry="2026-10-26", kind="점검용", tid="B-1")
        Cc = held(8, 5_000, 4_500, entry="2026-10-27", kind="본래", tid="C-1")
        sell = lambda code, q, px, kind: {"date": "x", "side": "sell", "code": code, "filled_qty": q, "fill_price": px, "kind": kind,  # noqa: E731
                                          "stage": "stop", "status": "ACCEPTED", "fill_status": "체결"}
        buy = lambda code, q, px, kind: {"date": "x", "side": "buy", "code": code, "filled_qty": q, "fill_price": px, "kind": kind,  # noqa: E731
                                         "stage": "buy", "status": "ACCEPTED", "fill_status": "체결"}
        self.state("2026-10-26", intents=[buy("A", 10, 10_000, "점검용"), buy("B", 5, 20_000, "점검용")], baseline={})
        self.state("2026-10-27", intents=[sell("A", 10, 11_000, "점검용"), buy("C", 8, 5_000, "본래")], baseline={"A": A, "B": B})
        self.state("2026-10-28", intents=[sell("B", 5, 19_000, "점검용")], baseline={"B": B, "C": Cc},
                   settlement={"d2": 29_000_000.0, "equity": 29_040_000.0, "time": "15:40:00", "cur_price": {"C": 5_000.0}})
        P.save(self.R.POSITIONS, {"held": {"C": dict(Cc)}, "trade_seq": 3})
        self.fk.balance_rows = [bal_row("C", 8, avg=5_000)]

    def test_cumulative_pnl_wins_losses_prod_fee_and_split(self):
        res = self.started("10:00:00", live=False)
        self.filled_sell(res.om, code="C", qty=8, price=5_500, stage="stop")    # 오늘 C 손절 (확정, 전략 = 본래)
        v = res.query_view()
        pn = TC.pnl_text(v)
        rate, prod, y = self.R.commission_rate(), self.R.PROD_COMMISSION, 2026

        def rz(q, px, entry, fee):
            amt, cost = q * px, q * entry
            return amt - cost - P.kiwoom_fee(amt, fee) - P.kiwoom_tax(amt, self.R.sb_tax(y)) - P.kiwoom_fee(cost, fee)
        a, b, c = rz(10, 11_000, 10_000, rate), rz(5, 19_000, 20_000, rate), rz(8, 5_500, 5_000, rate)
        ap, bp, cp = rz(10, 11_000, 10_000, prod), rz(5, 19_000, 20_000, prod), rz(8, 5_500, 5_000, prod)
        self.assertGreater(a, 0)
        self.assertLess(b, 0)
        self.assertGreater(c, 0)
        cu = TC.cumulative(v, TC.book(v))
        self.assertAlmostEqual(cu["pnl"], round(a + b + c, 1), places=0)
        self.assertAlmostEqual(cu["pnl_prod"], round(ap + bp + cp, 1), places=0)
        self.assertEqual((cu["n"], cu["win"], cu["loss"], cu["days"]), (3, 2, 1, 3))
        fills = 10 * 10_000 + 5 * 20_000 + 10 * 11_000 + 8 * 5_000 + 5 * 19_000 + 8 * 5_500
        self.assertEqual(cu["fill_amount"], fills)
        self.assertIn(f"({cu['win']}승 {cu['loss']}패)", self.lines(pn, "실현(누적)")[0])
        self.assertEqual(self.lines(pn, "실현(누적)")[0].split()[1], f"{a + b + c:+,.0f}")
        # 총자산 = 전 거래일 D+2 + 오늘 체결 (C 매도) - 비용, 보유 없음 -> 총손익 = 총자산 - 30,000,000
        cash = P.expected_cash(29_000_000.0, [r for r in res.om.records if r.get("filled_qty")], 2026, commission=rate)[0]
        tot = cash - 30_000_000
        self.assertEqual(self.lines(pn, "총손익")[0].split()[1:], [f"{tot:+,.0f}", f"({tot / 30_000_000 * 100:+.2f}%)"])
        fee_back = fills * (rate - prod)
        self.assertEqual(self.lines(pn, "실전 수수료면")[0].split()[2], f"{tot + fee_back:+,.0f}")       # 총손익 (요약 방식)
        self.assertEqual(self.lines(pn, "실전 수수료면")[1].split()[2], f"{ap + bp + cp:+,.0f}")        # 실현 (실전 수수료로 다시 계산)
        split = self.lines(pn, "실현(누적)")[1].split()                         # 전략 / 점검용 칸
        self.assertEqual(split[1:], [f"{c:+,.0f}", f"{a + b:+,.0f}"])
        self.assertEqual(self.lines(pn, "구분")[0].split()[1:], ["전략", "점검용"])
        self.assertIn("2026-10-26 부터 3거래", pn)

    def test_all_check_kind_one_line(self):
        P.save(self.R.POSITIONS, {"held": {"C": held(8, 5_000, 4_500, entry="2026-10-27", kind="점검용", tid="C-1")}, "trade_seq": 3})
        res = self.started("10:00:00", live=False)
        pn = TC.pnl_text(res.query_view())
        self.assertIn("구분: 전부 점검용", pn)                                    # 지금은 전부 점검용 (필터가 켜져 전략 거래가 생기면 칸을 나눔)
        self.assertEqual(self.lines(pn, "구분"), ["구분: 전부 점검용"])


# ---------------------------------------------------------------- 5 형식·나눠 보내기

class Format(MBase):
    def test_lines_blank_between_stocks_and_basis_at_bottom(self):
        self.clk.t = pd.Timestamp("2026-11-09 10:00:00")
        self.state("2026-11-06", settlement={"d2": 1_000_000.0, "equity": 2_000_000.0, "time": "15:40:00", "cur_price": {"A": 100.0, "B": 200.0}})
        P.save(self.R.POSITIONS, {"held": {"A": held(1, 100, 90), "B": held(2, 200, 180)}, "trade_seq": 2})
        res = self.resident()
        res.day = self.R.new_day(self.clk(), clock=self.clk)
        v = res.query_view()
        h, pn = TC.holdings_text(v), TC.pnl_text(v)
        ls = h.split("\n")
        self.assertEqual(ls[1], "")                                             # 머리 뒤 빈 줄
        self.assertEqual(ls[2].split()[-1], "A")
        self.assertTrue(ls[3].startswith("  1주 · 평균"))
        self.assertIn("", ls[4:7])                                              # 종목 사이 빈 줄
        self.assertTrue(ls[-1].startswith("[기준: 보유 "))                       # 근거는 맨 아래 한 줄
        pl = pn.split("\n")
        self.assertTrue(pl[-1].startswith("[현금 전 거래일(11-06) 정산 D+2"))
        nums = [ln for ln in pl[1:4]]                                           # 총자산·현금·주식: 숫자 끝이 같은 칸 (고정폭, 한글 2칸)
        self.assertEqual(len({TC.dw(ln) for ln in nums}), 1)

    def test_long_message_split_under_4096_and_mono_fallback(self):
        sent = []

        def post(method, data):
            sent.append(dict(data))
            return {"ok": data.get("parse_mode") != "HTML" or len(sent) > 2}     # 첫 조각의 고정폭 전송은 거부 -> 일반 글로
        b = TC.Bot(TOKEN, CHAT, lambda: None, lambda *a: None, http_post=post, http_get=lambda *a: {"ok": True, "result": []})
        body = "\n\n".join(f"종목{i:03d} {i:06d}\n  10주 · 평균 1,000 → 현재 1,100 (+10.0%)\n  손절가 900까지 -18.2%" for i in range(120))
        self.assertGreater(len(body), 4096)
        self.assertTrue(b.send(body))
        self.assertTrue(all(len(x["text"]) <= 4096 for x in sent))
        self.assertEqual(sent[0].get("parse_mode"), "HTML")
        self.assertNotIn("parse_mode", sent[1])                                 # 거부된 첫 조각을 일반 글로
        parts = [x["text"] for x in sent[1:]]
        got = "\n".join(p.replace("<pre>", "").replace("</pre>", "") for p in parts)
        self.assertEqual(got.replace("\n", ""), body.replace("\n", ""))          # 내용 그대로 (줄 단위로만 나눔)
        self.assertGreaterEqual(len(parts), 2)

    def test_html_escaped(self):
        sent = []
        b = TC.Bot(TOKEN, CHAT, lambda: None, lambda *a: None, http_post=lambda m, d: sent.append(d) or {"ok": True})
        b.send("a < b & c > d")
        self.assertEqual(sent[0]["text"], "<pre>a &lt; b &amp; c &gt; d</pre>")


if __name__ == "__main__":
    unittest.main()
