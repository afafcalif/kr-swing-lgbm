"""
주문 모듈 단위 시험 (가짜 클라이언트, 가짜 시계). 실제 주문·조회 없음.

  python -m unittest backtest_swing/live/test_orders.py backtest_swing/live/test_safety.py -v
"""
import os
import sys
import tempfile
import unittest

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import orders as O  # noqa: E402
from kiwoom.errors import HTTPError  # noqa: E402


class Crash(BaseException):
    """주문 전송 직후 프로그램이 꺼진 것 흉내 (except Exception 에 잡히지 않음)"""


class Resp:
    def __init__(self, j, bad_json=False, headers=None):
        self._j, self.bad, self.headers = j, bad_json, headers or {}

    def json(self):
        if self.bad:
            raise ValueError("Expecting value: line 1 column 1")
        return self._j


class FakeClock:
    def __init__(self, t):
        self.t = pd.Timestamp(t)
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += pd.Timedelta(seconds=s)

    def at(self, hms):
        self.t = pd.Timestamp(f"{self.t.date()} {hms}")


class FakeKiwoom:
    """주문·계좌 TR 흉내 (상태 있는 가짜 서버, Codex 18). script[tr] = 차례로 쓸 동작 목록:
    accept, reject, timeout, timeout_accepted, http503, http400(본문 없음), http400_rc(본문에 return_code), error(분류 안 된 예외),
    (취소 kt10003: accept, timeout, timeout_applied, reject, reject_filled(잔량이 그 사이 다 체결돼 거부)),
    badjson(접수됐지만 응답 JSON 이 깨짐), accept_then_crash(접수 후 프로그램 꺼짐), timeout_late:<초>(접수됐지만 <초> 뒤에야 조회에 보임),
    listbody(HTTP 200 인데 본문이 목록).
    조회 TR read_script[tr] = 페이지마다 쓸 동작: ok, rc_error(HTTP 200 + return_code != 0), listbody, missing(필수 필드 없음), nan(금액 빈칸), http502.
    체결(fill)하면 보유 수량·매도 가능 수량·예수금(D+2·주문가능)이 바뀐다. page_size 를 주면 목록 조회를 cont-yn/next-key 로 나눠 준다.
    auto_fill[code] = 가격 이면 그 종목 시장가 주문은 접수 즉시 그 가격에 체결."""

    def __init__(self, clock):
        self.clock, self.script, self.calls, self.orders, self.fill_rows, self.n = clock, {}, [], {}, [], 0
        self.read_fail = 0
        self.read_script = {}
        self.balance_rows = []        # kt00018 보유 목록
        self.orderable = 30_000_000   # kt00001 주문가능금액
        self.d2 = None                # kt00001 D+2 추정예수금 (None 이면 주문가능금액과 같음)
        self.page_size = None
        self.auto_fill = {}
        self.pages = []               # (tr, cont_yn, next_key)
        self.rate_wait = 0.0          # 주문 TR 유량 대기 (초): 가짜 시계를 그만큼 보냄

    def _row(self, code):
        for r in self.balance_rows:
            if r["stk_cd"].lstrip("A") == code:
                return r
        return None

    def _settle_fill(self, code, side, qty, price):
        from common import sb
        amt = float(price) * qty
        if self.d2 is None:
            self.d2 = float(self.orderable)
        r = self._row(code)
        if side == "buy":
            if r is None:
                r = {"stk_cd": f"A{code}", "rmnd_qty": "0", "trde_able_qty": "0", "pur_pric": "0", "cur_prc": str(price)}
                self.balance_rows.append(r)
            q0, a0 = int(r["rmnd_qty"]), float(r["pur_pric"])
            r["pur_pric"] = str(round((q0 * a0 + amt) / (q0 + qty)))
            r["rmnd_qty"], r["trde_able_qty"] = str(q0 + qty), str(int(r["trde_able_qty"]) + qty)
            cost = amt * (1 + sb.COMMISSION)
            self.d2 -= cost
            self.orderable -= cost
        else:
            r["rmnd_qty"], r["trde_able_qty"] = str(int(r["rmnd_qty"]) - qty), str(max(int(r["trde_able_qty"]) - qty, 0))
            if int(r["rmnd_qty"]) <= 0:
                self.balance_rows.remove(r)
            got = amt * (1 - sb.COMMISSION - sb.SELL_TAX.get(int(self.clock().year), 0.002))
            self.d2 += got
            self.orderable += got

    def _new_order(self, body, tr, visible_after=0):
        self.n += 1
        no = f"{self.n:07d}"
        self.orders[no] = {"ord_no": no, "stk_cd": body["stk_cd"], "ord_qty": body["ord_qty"], "io_tp_nm": "+매수" if tr == "kt10000" else "-매도",
                           "ord_pric": body.get("ord_uv", ""), "oso_qty": body["ord_qty"], "canceled": False,
                           "_visible": self.clock() + pd.Timedelta(seconds=visible_after)}
        return no

    def fill(self, no, qty, price, hms):
        o = self.orders[no]
        o["oso_qty"] = str(int(o["oso_qty"]) - qty)
        self.fill_rows.append({"ord_no": no, "stk_cd": o["stk_cd"], "io_tp_nm": o["io_tp_nm"], "ord_qty": o["ord_qty"], "cntr_qty": str(qty),
                               "cntr_pric": str(price), "ord_tm": hms.replace(":", ""), "oso_qty": o["oso_qty"]})
        self._settle_fill(o["stk_cd"], "buy" if o["io_tp_nm"] == "+매수" else "sell", qty, price)

    def _list(self, tr, key, items, next_key, top=None):
        """목록 조회 응답 (page_size 가 있으면 나눠서, 헤더 cont-yn/next-key)"""
        start = int(next_key or 0)
        n = self.page_size or max(len(items), 1)
        chunk, more = items[start:start + n], start + n < len(items)
        j = {"return_code": 0, "return_msg": "조회", key: chunk, **(top or {})}
        return Resp(j, headers={"cont-yn": "Y", "next-key": str(start + n)} if more else {"cont-yn": "N", "next-key": ""})

    def request(self, tr_code, method, path, json=None, cont_yn="N", next_key="", **kw):
        if tr_code in ("kt10000", "kt10001", "kt10003"):  # 키움 클라이언트처럼: 유량 대기(rate_wait초, 상한 max_wait) -> 보내기 바로 앞 확인(before_send)
            from kiwoom.errors import NotSent
            mw, bs = kw.get("max_wait"), kw.get("before_send")
            if self.rate_wait:
                if mw is not None and self.rate_wait > mw:
                    raise NotSent(f"유량 대기 상한 {mw}초 초과 (api-id={tr_code}) - 보내지 않음")
                self.clock.t += pd.Timedelta(seconds=self.rate_wait)
            if bs is not None and not bs():
                raise NotSent(f"전송 직전 확인 실패: 마감 시각 지남 (api-id={tr_code}) - 보내지 않음")
        self.calls.append((self.clock().strftime("%H:%M:%S.%f")[:-3], tr_code, dict(json or {})))
        ract = "ok"
        if tr_code in O.READ_TRS:
            self.pages.append((tr_code, cont_yn, next_key))
            ract = self.read_script.get(tr_code, []).pop(0) if self.read_script.get(tr_code) else "ok"
            if ract == "rc_error":
                return Resp({"return_code": 5, "return_msg": "[모의] 조회 오류"})
            if ract == "listbody":
                return Resp([{"return_code": 0}])
            if ract == "http502":
                raise HTTPError(502, "url", "Bad Gateway")
        act = self.script.get(tr_code, []).pop(0) if self.script.get(tr_code) else "accept"
        if tr_code in ("kt10000", "kt10001"):
            if act == "listbody":
                self._new_order(json, tr_code)
                return Resp([{"return_code": 0, "ord_no": "?"}])
            if act == "reject":
                return Resp({"return_code": 1, "return_msg": "[모의] 주문 거부"})
            if act == "timeout":
                raise requests.Timeout("read timeout")
            if act == "timeout_accepted":
                self._new_order(json, tr_code)
                raise requests.Timeout("read timeout (서버는 접수)")
            if act.startswith("timeout_late"):
                self._new_order(json, tr_code, visible_after=int(act.split(":")[1]))
                raise requests.Timeout("read timeout (서버는 접수, 조회 늦게 반영)")
            if act == "http503":
                raise HTTPError(503, "url", "unavailable")
            if act == "http400":
                raise HTTPError(400, "url", "bad request")
            if act == "http400_rc":
                raise HTTPError(400, "url", '{"return_code": 2, "return_msg": "주문 수량 오류"}')
            if act == "error":
                raise RuntimeError("분류 안 된 오류")
            if act == "badjson":
                self._new_order(json, tr_code)
                return Resp({}, bad_json=True)
            if act == "accept_then_crash":
                self._new_order(json, tr_code)
                raise Crash()
            no = self._new_order(json, tr_code)
            if tr_code == "kt10001" and json.get("trde_tp") == "3" and json["stk_cd"] in self.auto_fill:
                self.fill(no, int(json["ord_qty"]), self.auto_fill[json["stk_cd"]], self.clock().strftime("%H:%M:%S"))
            return Resp({"return_code": 0, "return_msg": "접수", "ord_no": no})
        if tr_code == "kt10003":
            o = self.orders[json["orig_ord_no"]]
            if act == "timeout":            # 취소가 서버에 안 닿음 (응답 없음)
                raise requests.Timeout("cancel timeout")
            if act == "reject":             # 취소 거부 (원주문 잔량 그대로)
                return Resp({"return_code": 1, "return_msg": "[모의] 취소 거부"})
            if act == "reject_filled":      # 취소 거부: 그 사이 잔량이 다 체결됨 (취소 가능 수량 없음)
                self.fill(o["ord_no"], int(o["oso_qty"]), int(o.get("ord_pric") or 0) or 1, self.clock().strftime("%H:%M:%S"))
                return Resp({"return_code": 1, "return_msg": "[모의] 취소 가능 수량 없음"})
            o["canceled"], o["oso_qty"] = True, "0"
            if act == "timeout_applied":    # 취소는 됐지만 응답 없음
                raise requests.Timeout("cancel timeout (서버는 취소)")
            return Resp({"return_code": 0, "return_msg": "취소 접수", "ord_no": f"C{json['orig_ord_no']}"})
        if tr_code in ("ka10075", "ka10076") and self.read_fail > 0:
            self.read_fail -= 1
            raise HTTPError(502, "url", "Bad Gateway")
        if tr_code == "ka10075":
            now = self.clock()
            return self._list(tr_code, "oso", [{k: v for k, v in o.items() if not k.startswith("_")} for o in self.orders.values()
                                               if int(o["oso_qty"]) > 0 and not o["canceled"] and o["_visible"] <= now], next_key)
        if tr_code == "ka10076":
            return self._list(tr_code, "cntr", [dict(x) for x in self.fill_rows], next_key)
        if tr_code == "kt00018":
            top = {} if ract == "missing" else {"prsm_dpst_aset_amt": f"{int(self.d2 if self.d2 is not None else self.orderable):015d}"}
            return self._list(tr_code, "acnt_evlt_remn_indv_tot", [dict(x) for x in self.balance_rows], next_key, top)
        if tr_code == "kt00001":
            d2 = self.d2 if self.d2 is not None else self.orderable
            j = {"return_code": 0, "return_msg": "조회", "ord_alow_amt": f"{int(self.orderable):015d}", "entr": f"{int(d2):015d}",
                 "d2_entra": f"{int(d2):015d}"}
            if ract == "missing":
                j.pop("ord_alow_amt")
            if ract == "nan":
                j["ord_alow_amt"] = ""
            return Resp(j)
        raise AssertionError(tr_code)


def setup(t="2026-10-23 15:21:05", path=None):
    clk = FakeClock(t)
    fk = FakeKiwoom(clk)
    notes = []
    store = O.IntentStore(path, clock=clk) if path else None
    om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep, notify=notes.append, store=store)
    return clk, fk, om, notes


PLAN = [{"code": "084670", "qty": 28, "limit_price": 33350, "skip": ""}]


def sends(fk, tr="kt10000"):
    return [c for c in fk.calls if c[1] == tr]


class T(unittest.TestCase):
    def test_reject_then_retry_once_ok(self):
        clk, fk, om, notes = setup()
        fk.script["kt10000"] = ["reject", "accept"]
        r = om.place_buys(PLAN, "점검용")[0]
        self.assertEqual(len(sends(fk)), 2)
        self.assertEqual(clk.slept, [1.0])
        self.assertEqual(r["status"], O.ACCEPTED)
        self.assertEqual(r["attempt"], 2)
        self.assertEqual([i["status"] for i in om.records], [O.REJECTED, O.ACCEPTED])
        self.assertEqual(sends(fk)[0][2]["trde_tp"], "0")
        self.assertEqual(sends(fk)[0][2]["ord_uv"], "33350")

    def test_reject_twice_skip(self):
        clk, fk, om, notes = setup()
        fk.script["kt10000"] = ["reject", "reject"]
        r = om.place_buys(PLAN, "본래")[0]
        self.assertEqual(len(sends(fk)), 2)
        self.assertEqual(r["status"], O.REJECTED)
        self.assertTrue(any("주문 거부" in n for n in notes))
        om.place_buys(PLAN, "본래")
        self.assertEqual(len(sends(fk)), 2)  # 최대 시도 2회

    def test_http400_with_return_code_is_reject_without_is_unknown(self):
        clk, fk, om, notes = setup()
        fk.script["kt10000"] = ["http400_rc", "accept"]
        r = om.place_buys(PLAN, "본래")[0]
        self.assertEqual(len(sends(fk)), 2)
        self.assertEqual(r["status"], O.ACCEPTED)
        clk2, fk2, om2, _ = setup()
        fk2.script["kt10000"] = ["http400"]
        r2 = om2.place_buys(PLAN, "본래")[0]
        self.assertEqual(r2["status"], O.UNKNOWN)
        self.assertEqual(len(sends(fk2)), 1)

    def test_429_exhausted_is_reject_with_cause(self):
        clk = FakeClock("2026-10-23 15:21:05")

        class C:
            def request(self, **kw):
                return None
        om = O.OrderManager(O.Broker(C(), allow_orders=True), clock=clk, sleep=clk.sleep)
        r = om.place_buys(PLAN, "본래")[0]
        self.assertEqual(r["status"], O.REJECTED)
        self.assertEqual(r["cause"], "429")

    def test_no_response_accepted_found_by_query_no_resend(self):
        clk, fk, om, notes = setup()
        fk.script["kt10000"] = ["timeout_accepted"]
        r = om.place_buys(PLAN, "본래")[0]
        self.assertEqual(len(sends(fk)), 1)
        self.assertEqual(r["status"], O.UNKNOWN)
        om.place_buys(PLAN, "본래")
        self.assertEqual(len(sends(fk)), 1)
        clk.t += pd.Timedelta(seconds=5)
        om.reconcile()
        self.assertEqual(r["status"], O.ACCEPTED)
        self.assertEqual(r["ord_no"], "0000001")
        self.assertTrue(any("응답 없음" in n for n in notes))

    def test_no_response_not_accepted_stays_unknown_until_settlement(self):
        clk, fk, om, notes = setup()
        fk.script["kt10000"] = ["timeout"]
        r = om.place_buys(PLAN, "본래")[0]
        for s in (5, 30, 120):
            clk.t = pd.Timestamp("2026-10-23 15:21:05") + pd.Timedelta(seconds=s)
            om.reconcile()
            self.assertEqual(r["status"], O.UNKNOWN)
        self.assertEqual(sum("확인 안 됨" in n for n in notes), 1)
        om.place_buys(PLAN, "본래")
        self.assertEqual(len(sends(fk)), 1)
        clk.at("15:35:00")
        om.close_check()
        self.assertEqual(r["status"], O.NOT_SENT)

    def test_http503_is_unknown(self):
        clk, fk, om, notes = setup()
        fk.script["kt10000"] = ["http503"]
        r = om.place_buys(PLAN, "본래")[0]
        self.assertEqual(len(sends(fk)), 1)
        self.assertEqual(r["status"], O.UNKNOWN)

    def test_time_window(self):
        for t, sent in (("2026-10-23 15:20:59", 0), ("2026-10-23 15:25:30", 0), ("2026-10-23 15:28:00", 0), ("2026-10-23 15:24:59", 1)):
            clk, fk, om, _ = setup(t)
            om.place_buys(PLAN, "본래")
            self.assertEqual(len(sends(fk)), sent, t)

    def test_random_end_fill_after_1530_no_cancel(self):
        clk, fk, om, _ = setup()
        r = om.place_buys(PLAN, "본래")[0]
        clk.at("15:30:00")
        self.assertFalse(om.close_check()["done"])
        self.assertFalse(any(c[1] == "kt10003" for c in fk.calls))
        fk.fill(r["ord_no"], 28, 32100, "15:30:21")
        clk.at("15:35:00")
        self.assertTrue(om.close_check(close_prices={"084670": 32100})["done"])
        self.assertEqual(r["filled_qty"], 28)
        self.assertEqual(r["unfilled_qty"], 0)
        self.assertEqual(r["fill_status"], "체결")
        self.assertEqual(r["fill_time"], "153021")
        self.assertEqual(r["fill_time_field"], "ord_tm")
        self.assertFalse(any(c[1] == "kt10003" for c in fk.calls))
        self.assertAlmostEqual(r["assumed_price"], 32100 * 1.001)
        self.assertAlmostEqual(r["diff_pct"], (1 / 1.001 - 1) * 100)

    def test_partial_fill_cancel_rest_at_1535(self):
        clk, fk, om, _ = setup()
        r = om.place_buys(PLAN, "점검용")[0]
        fk.fill(r["ord_no"], 6, 33000, "15:30:05")
        clk.at("15:35:01")
        om.close_check()
        self.assertEqual(r["filled_qty"], 6)
        self.assertEqual(r["unfilled_qty"], 22)
        self.assertEqual(r["fill_status"], "부분 체결, 나머지 미체결 건너뜀")
        canc = [c for c in fk.calls if c[1] == "kt10003"]
        self.assertEqual(len(canc), 1)
        self.assertEqual(canc[0][2]["orig_ord_no"], r["ord_no"])
        self.assertEqual(canc[0][0][:5], "15:35")
        om.close_check()  # 정산을 다시 돌려도 취소를 또 보내지 않음
        self.assertEqual(len([c for c in fk.calls if c[1] == "kt10003"]), 1)

    def test_unfilled_at_1535_skip_and_cancel(self):
        clk, fk, om, _ = setup()
        r = om.place_buys(PLAN, "본래")[0]
        clk.at("15:35:00")
        om.close_check()
        self.assertEqual(r["filled_qty"], 0)
        self.assertEqual(r["fill_status"], "미체결 건너뜀")
        self.assertEqual(len([c for c in fk.calls if c[1] == "kt10003"]), 1)

    def test_stop_loss_market_once(self):
        clk, fk, om, _ = setup("2026-10-23 10:00:00")
        held = {"084670": {"qty": 28, "avg_price": 32100, "cur_price": 29900, "sellable": 28}}
        om.stop_check(held, {"084670": 30000})
        om.stop_check(held, {"084670": 30000})
        s = sends(fk, "kt10001")
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0][2]["trde_tp"], "3")
        self.assertEqual(s[0][2]["ord_qty"], "28")
        clk2, fk2, om2, _ = setup("2026-10-23 15:21:00")
        om2.stop_check(held, {"084670": 30000})
        self.assertEqual(len(sends(fk2, "kt10001")), 0)

    def test_locked_sell_unfilled_carry_to_next_open(self):
        clk, fk, om, _ = setup("2026-10-23 10:00:00")
        held = {"084670": {"qty": 28, "avg_price": 32100, "cur_price": 22000, "sellable": 28}}
        r = om.stop_check(held, {"084670": 30000})[0]
        clk.at("15:35:00")
        om.close_check()
        self.assertEqual(r["fill_status"], "미체결, 다음 날 시가 재주문")
        self.assertEqual(om.next_open_sells, ["084670"])
        self.assertEqual(len([c for c in fk.calls if c[1] == "kt10003"]), 1)
        clk3, fk3, om3, _ = setup("2026-10-24 08:31:00")
        om3.trend_sells(om.next_open_sells, held)
        self.assertEqual(len(sends(fk3, "kt10001")), 1)
        clk4, fk4, om4, _ = setup("2026-10-24 09:00:30")
        om4.trend_sells(["084670"], held)
        self.assertEqual(len(sends(fk4, "kt10001")), 0)

    def test_p4_plan_skips(self):
        top = pd.DataFrame({"code": ["A1", "B2", "C3"], "score": [0.3, 0.2, 0.1], "raw_close": [31750.0, 20200.0, 47700.0]})
        p = O.p4_plan(top, "raw_close", equity=30_000_000, cash=30_000_000, held={"B2"}, kind="본래")
        self.assertEqual([x["skip"] for x in p], ["", "보유 중", ""])
        self.assertEqual(p[0]["qty"], 28)
        self.assertEqual(p[0]["limit_price"], 33350)
        p2 = O.p4_plan(top, "raw_close", equity=30_000_000, cash=1_000_000, held=set(), kind="점검용")
        self.assertEqual([x["skip"] for x in p2], ["", "현금 부족", "현금 부족"])

    def test_tick_boundaries(self):
        """O-07: 호가 단위 경계 (2,000 / 5,000 / 20,000 / 50,000 / 200,000 / 500,000)"""
        from common import ceil_tick, tick_size
        cases = {1999: 1999, 1999.4: 2000, 2000: 2000, 2001: 2005, 4996: 5000, 5001: 5010, 19995: 20000, 20001: 20050,
                 49960: 50000, 50001: 50100, 199950: 200000, 200001: 200500, 499600: 500000, 500001: 501000}
        for p, want in cases.items():
            self.assertEqual(ceil_tick(p), want, p)
            self.assertEqual(want % tick_size(want), 0, want)

    def test_notify_without_keys_logs_only(self):
        import notify
        got = []
        p = os.path.join(tempfile.mkdtemp(), "alerts.log")
        self.assertTrue(notify.send("시험", log_path=p, sender=lambda t: got.append(t) or True))
        self.assertEqual(got, ["시험"])
        self.assertIn("시험", open(p, encoding="utf-8").read())

    def test_stop_by_quote_while_account_fails_and_alerts_once(self):
        clk, fk, om, notes = setup("2026-10-23 10:00:00")
        watch = O.AccountWatch(notify=notes.append, n_alert=3)
        pos = {"held": {"084670": {"qty": 28, "stop_price": 30000, "kind": "본래"}}}
        price = {"v": 31000}

        def quote(codes):
            return {c: price["v"] for c in codes}

        def bal_fail():
            raise O.NoResponse("HTTP 502")

        seen = set()
        for i in range(4):  # 10:00 ~ 10:03 계좌 조회 실패, 10:02 에 시세가 손절가 아래
            if i == 2:
                price["v"] = 29900
            r = O.stop_cycle(om, watch, pos, quote, bal_fail, clk(), seen)
            clk.t += pd.Timedelta(minutes=1)
            self.assertFalse(r["account_ok"])
        s = sends(fk, "kt10001")
        self.assertEqual(len(s), 1)                      # 계좌 조회 실패 중에도 시세로 손절 1회
        self.assertEqual(s[0][2]["ord_qty"], "28")       # 수량은 상태 파일 기준
        self.assertEqual(s[0][0][:5], "10:02")
        self.assertEqual(sum(n.startswith("[계좌 조회 연속 실패]") for n in notes), 1)   # 3분째 한 번만
        self.assertEqual(watch.fail_count, 4)
        self.assertEqual(watch.longest_min, 4.0)

        def bal_ok():
            return {"acnt_evlt_remn_indv_tot": [{"stk_cd": "A084670", "rmnd_qty": "28", "trde_able_qty": "0", "pur_pric": "32100", "cur_prc": "29900"}]}
        O.stop_cycle(om, watch, pos, quote, bal_ok, clk(), seen)
        O.stop_cycle(om, watch, pos, quote, bal_ok, clk(), seen)
        self.assertEqual(sum("[회복]" in n for n in notes), 1)      # 회복 알림 한 번
        self.assertEqual(sum(n.startswith("[계좌 조회 연속 실패]") for n in notes), 1)

    def test_holding_mismatch_alert_once(self):
        clk, fk, om, notes = setup("2026-10-23 11:00:00")
        watch = O.AccountWatch(notify=notes.append)
        pos = {"held": {"084670": {"qty": 28, "stop_price": 30000}}}
        bal = lambda: {"acnt_evlt_remn_indv_tot": [{"stk_cd": "A084670", "rmnd_qty": "20", "trde_able_qty": "20", "pur_pric": "1", "cur_prc": "31000"}]}  # noqa: E731
        seen = set()
        r1 = O.stop_cycle(om, watch, pos, lambda cs: {c: 31000 for c in cs}, bal, clk(), seen)
        r2 = O.stop_cycle(om, watch, pos, lambda cs: {c: 31000 for c in cs}, bal, clk(), seen)
        self.assertEqual(r1["mismatch"], {"084670": {"state": 28, "account": 20}})
        self.assertEqual(r2["mismatch"], r1["mismatch"])
        self.assertEqual(sum("보유 불일치" in n for n in notes), 1)
        self.assertEqual(len(sends(fk, "kt10001")), 0)

    def test_orders_disabled_not_sent(self):
        clk = FakeClock("2026-10-23 15:22:00")
        fk = FakeKiwoom(clk)
        b = O.Broker(fk, allow_orders=False)
        self.assertEqual(b.buy_limit("084670", 1, 1000)["outcome"], O.NOT_SENT)
        self.assertEqual(b.cancel("1", "084670")["outcome"], O.NOT_SENT)
        self.assertEqual(fk.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
