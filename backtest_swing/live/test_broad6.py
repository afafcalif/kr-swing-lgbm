"""
Codex 넓은 리뷰 2차 (11K, 2026-10-27) 재현 시험. 가짜 서버·가짜 시계, 실제 주문·조회·logs/exec·data 없음.

  python -m unittest backtest_swing/live/test_broad6.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import json
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import Base, bal_row, jl  # noqa: E402
from test_orders import FakeClock, FakeKiwoom, sends  # noqa: E402
from test_broad5 import prep_out  # noqa: E402
import common as C  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import quotes as Q  # noqa: E402
import state as S  # noqa: E402

HELD = {"qty": 28, "trade_id": "084670-2026-10-20-1", "entry_date": "2026-10-20", "stop_price": 30000.0, "stop_basis": "진입일", "kind": "본래",
        "judged_on": "2026-10-23", "judged_by": "15:30 1분봉", "close_1530": 33000.0, "trend_sell_next_open": False}


def pos_with(held=None, duty=None, nos=None):
    return {"held": {"084670": dict(held or HELD)}, "sell_duty": duty or {}, "next_open_sells": nos or {}, "trade_seq": 1, "duty_format": P.DUTY_FORMAT}


class Base6(Base):
    def tearDown(self):
        self.R.apply_hours(pd.Timestamp("2026-10-26"))  # 거래시간 변경일 시험 뒤 평소 시각표로
        super().tearDown()


# ---------------------------------------------------------------- 1 월별 모델

class Monthly(Base6):
    def test_plan_from_holiday_calendar(self):
        import monthly as MO
        self.assertEqual(MO.plan("2026-11"), {"month": "2026-11", "retrain_day": "2026-11-02", "train_end": "2026-07-31", "prev_day": "2026-10-30",
                                             "label_gap": 61})
        self.assertEqual((MO.plan("2026-10")["retrain_day"], MO.plan("2026-10")["train_end"]), ("2026-10-01", "2026-07-02"))  # 10월 모델 meta 와 같음
        self.assertTrue(MO.last_trading_day_of_month("2026-10-30"))
        self.assertFalse(MO.last_trading_day_of_month("2026-10-29"))
        self.assertEqual(MO.next_month("2026-10-30"), "2026-11")

    def test_train_uses_given_plan_without_month_in_dataset(self):
        import model as M
        rng = np.random.default_rng(0)
        cols = [f"f{i}" for i in range(41)]
        days = pd.bdate_range("2026-05-01", "2026-10-30")
        ds = pd.DataFrame({"code": "000001", "date": np.repeat(days, 8)})
        for c in cols:
            ds[c] = rng.normal(size=len(ds)).astype(np.float32)
        ds["y_clip"] = rng.normal(size=len(ds))
        p = os.path.join(self.tmp, "ds.parquet")
        ds.to_parquet(p, index=False)
        saved = (M.m.feature_cols, M.sm.N_TREES)
        M.m.feature_cols = lambda d: cols
        try:
            r = M.train("2026-11", out_dir=os.path.join(self.tmp, "m"), dataset=p, plan=("2026-11-02", "2026-07-31"))
        finally:
            M.m.feature_cols = saved[0]
        meta = jl(os.path.join(self.tmp, "m", "meta.json"))
        self.assertEqual((r["trained"], meta["retrain_day"], meta["train_end"], meta["month"], meta["seeds"]), (True, "2026-11-02", "2026-07-31", "2026-11",
                                                                                                               [1, 2, 3, 4, 5]))
        self.assertEqual(meta["train_rows"], int((ds.date <= "2026-07-31").sum()))
        self.assertIn("달력", meta["plan_source"])

    # 상주 자동 실행 시험은 11K 보완 1 의 따라잡기 규칙으로 test_broad7.MonthlyCatchup 에 (예전 monthly_job 은 없앰)

    def test_universe_update_guard_and_pointer(self):
        import monthly as MO
        raw = os.path.join(self.tmp, "raw")
        os.makedirs(raw)
        old = pd.DataFrame({"code": [f"{i:06d}" for i in range(100)], "name": "x", "market": "KOSPI", "reg_day": "", "state": "", "order_warning": 0})
        for c in old.code:
            open(os.path.join(raw, f"{c}.parquet"), "w").close()
        op = os.path.join(self.tmp, "uni_old.csv")
        old.to_csv(op, index=False)
        saved = (MO.sd.UNIVERSE, MO.sd.RAW_DIR, MO.fetch_universe)
        self.addCleanup(lambda: (setattr(MO.sd, "UNIVERSE", saved[0]), setattr(MO.sd, "RAW_DIR", saved[1]), setattr(MO, "fetch_universe", saved[2])))
        MO.sd.UNIVERSE, MO.sd.RAW_DIR = op, raw
        ptr = os.path.join(self.tmp, "ptr.json")
        p = MO.plan("2026-11")
        MO.fetch_universe = lambda bas: old.iloc[:90].copy()                       # 10% 빠짐 -> 적용 안 함
        r = MO.update_universe(p, "2026-10-30", out_csv=os.path.join(self.tmp, "u.csv"), pointer=ptr)
        self.assertFalse(r["ok"])
        self.assertFalse(os.path.exists(ptr))
        MO.fetch_universe = lambda bas: pd.concat([old.iloc[:98], old.iloc[:1].assign(code="999999")])
        r = MO.update_universe(p, "2026-10-30", out_csv=os.path.join(self.tmp, "u.csv"), pointer=ptr)
        self.assertEqual((r["ok"], r["removed"], r["added"]), (True, 2, 1))
        self.assertEqual(C.universe_for("2026-10-30", ptr), C.UNIVERSE_DEFAULT)     # 학습일 전에는 그대로
        self.assertEqual(C.universe_for("2026-11-02", ptr), os.path.join(self.tmp, "u.csv"))

    def test_dataset_compare_counts_only_real_diffs(self):
        import monthly as MO
        import stage5_model as m
        cols = [f"f{i}" for i in range(3)]
        saved = m.feature_cols
        m.feature_cols = lambda d: cols
        self.addCleanup(setattr, m, "feature_cols", saved)
        a = pd.DataFrame({"code": ["A", "B", "C"], "date": pd.to_datetime(["2026-10-16"] * 3), "f0": [1.0, np.inf, np.nan], "f1": [2.0, 2.0, 2.0],
                          "f2": [3.0, 3.0, 3.0], "R": [0.1, 0.2, 0.3]})
        b = a.copy()
        b.loc[2, "f1"] = 2.5
        pa, pb = os.path.join(self.tmp, "a.parquet"), os.path.join(self.tmp, "b.parquet")
        a.to_parquet(pa)
        b.to_parquet(pb)
        r = MO.compare_datasets(pa, pb)
        self.assertEqual((r["common"], r["rows_feature_diff"], r["label_R_diff_rows"]), (3, 1, 0))   # 무한대=무한대·결측=결측은 같음


# ---------------------------------------------------------------- 2 거래시간 변경일

class SpecialHours(Base6):
    def test_times_shift_on_csat_day(self):
        R = self.R
        h = R.apply_hours(pd.Timestamp("2026-11-19"))
        self.assertEqual((h["open"], h["close"], h["special"], h["confirmed"]), ("10:00:00", "16:30:00", True, False))
        want = {"trend_sell_from": "09:30:00", "trend_sell_until": "09:59:00", "stop_from": "10:00:00", "stop_until": "16:20:00",
                "stop_send_until": "16:29:00", "data_check": "16:17:00", "snapshot": "16:18:30", "buy_from": "16:21:00", "buy_until": "16:25:00",
                "no_new_order_after": "16:28:00", "close_check": "16:35:00", "settle": "16:40:00", "close_calc": "16:32:00",
                "close_calc_until": "17:20:00", "day_end": "17:30:00", "official_retry_until": "16:00:00", "day_start": "07:00:00"}
        self.assertEqual({k: R.T[k] for k in want}, want)
        self.assertEqual((R.at("08:20:00"), R.at("15:10:00")), ("09:20:00", "16:10:00"))
        R.apply_hours(pd.Timestamp("2026-11-20"))
        self.assertEqual(R.T, R.BASE_T)
        R.apply_hours(pd.Timestamp("2027-01-04"))                                   # 새해 첫 거래일: 개장만 1시간 늦음
        self.assertEqual((R.T["trend_sell_from"], R.T["stop_from"], R.T["stop_until"], R.T["snapshot"]), ("09:30:00", "10:00:00", "15:20:00", "15:18:30"))

    def test_resident_day_uses_shifted_windows(self):
        self.clk.t = pd.Timestamp("2026-11-19 09:58:00")
        res = self.started("09:58:00", live=False)
        self.assertEqual((res.om.t["stop_from"], res.om.t["buy_from"], res.om.t["no_new_order_after"]), ("10:00:00", "16:21:00", "16:28:00"))
        self.assertTrue(any(a.startswith("[거래시간 변경일] 2026-11-19") and "KRX 공지로 확인 필요" in a for a in self.alerts))
        self.assertEqual(res.day.s["special_hours"]["times"]["settle"], "16:40:00")
        self.assertEqual(res.om.detect_stops({"084670": {"cur_price": 1.0}}, {"084670": 2.0}, at="09:59:30"), {})   # 10:00 전에는 감지 안 함
        self.assertIn("084670", res.om.detect_stops({"084670": {"cur_price": 1.0}}, {"084670": 2.0}, at="16:20:30"))  # 16:20 분까지 감지
        self.clk.at("16:22:00")
        self.assertTrue(res.om.in_window((res.om.t["buy_from"], res.om.t["no_new_order_after"], False)))
        self.assertTrue(res.tick(self.clk()) not in ("idle",))                       # 16:30 뒤에도 하루가 끝나지 않음 (17:30)
        self.clk.at("17:10:00")
        self.assertNotEqual(res.tick(self.clk()), "idle")
        self.clk.at("17:31:00")
        self.assertEqual(res.tick(self.clk()), "idle")

    def test_close_bar_and_update_block(self):
        import update as U

        class Cl:
            def post(self, tr, path, body, **k):
                rows = [{"cntr_tm": f"20261119{h}00", "open_pric": "1", "high_pric": "1", "low_pric": "1", "cur_prc": "1", "trde_qty": "1"}
                        for h in ("1529", "1530", "1600", "1630", "1631", "1700")]
                return {"stk_min_pole_chart_qry": rows}, "N", ""
        saved = self.saved[11]                                                     # Base 가 가짜로 바꾼 bars_today 말고 원래 함수
        b = saved(Cl(), "084670", pd.Timestamp("2026-11-19"))
        self.assertEqual(b.ts.str[8:12].tolist(), ["1529", "1530", "1600", "1630"])  # 16:30 봉까지 (평소는 15:30)
        self.assertEqual(S.close_hhmm("2026-11-18"), "1530")
        self.assertFalse(U.allowed(pd.Timestamp("2026-11-19 16:00:00")))             # 장중 (평소면 15:45 뒤라 허용)
        self.assertTrue(U.allowed(pd.Timestamp("2026-11-18 16:00:00")))
        self.assertTrue(U.allowed(pd.Timestamp("2026-11-19 16:50:00")))


# ---------------------------------------------------------------- 3 공식 일봉 재판단

class OfficialRejudge(Base6):
    def test_trend_false_releases_unsent_duty(self):
        duty = {"084670": O.new_duty("trend", "2026-10-23 15:41:00", "추세 매도 (2026-10-23 종가 < 20일선, 15:30 1분봉)")}
        P.save(self.R.POSITIONS, pos_with(dict(HELD, trend_sell_next_open=True), duty, {"084670": {"since": "2026-10-23", "attempts": 0}}))
        res = self.started("08:20:00", live=False)
        self.judged["084670"] = {"close": 34000.0, "stop": 30000.0, "trend": False, "basis": "수정주가 일봉"}
        res.official_vs_1530()
        p = jl(self.R.POSITIONS)
        self.assertEqual((p["sell_duty"], p["next_open_sells"], p["held"]["084670"]["trend_sell_next_open"]), ({}, {}, False))
        self.assertEqual(res.day.s["official_vs_1530"]["diffs"][0]["applied"], "추세 매도 의무 해제")
        self.assertTrue(any(a.startswith("[공식 일봉 재판단]") for a in self.alerts))

    def test_trend_false_keeps_duty_if_already_sent(self):
        duty = {"084670": O.new_duty("trend", "2026-10-23 15:41:00", "추세")}
        P.save(self.R.POSITIONS, pos_with(dict(HELD, trend_sell_next_open=True), duty, {"084670": {"since": "2026-10-23", "attempts": 1}}))
        res = self.started("08:40:00", live=False)
        it = res.om.store.add(date="2026-10-26", stage="trend_sell", side="sell", code="084670", qty=28, price=None, trde_tp="3", attempt=1,
                              kind="본래", reason="추세", assumed_kind="open")
        res.om.store.update(it, status=O.ACCEPTED, ord_no="T1")
        self.judged["084670"] = {"close": 34000.0, "stop": 30000.0, "trend": False, "basis": "수정주가 일봉"}
        res.official_vs_1530()
        self.assertIn("084670", jl(self.R.POSITIONS)["sell_duty"])
        self.assertIn("의무 유지", res.day.s["official_vs_1530"]["diffs"][0]["applied"])

    def test_entry_day_stop_replaced_by_official(self):
        P.save(self.R.POSITIONS, pos_with(dict(HELD, entry_date="2026-10-23", trend_sell_next_open=False)))
        res = self.started("08:20:00", live=False)
        self.judged["084670"] = {"close": 33000.0, "stop": 29000.0, "trend": True, "basis": "수정주가 일봉"}
        res.official_vs_1530()
        h = jl(self.R.POSITIONS)["held"]["084670"]
        self.assertEqual((h["stop_price"], h["judged_by"], h["trend_sell_next_open"]), (29000.0, "공식 일봉 (다음 날 재판단)", False))  # 진입일 당일 추세 판정 안 함
        self.assertIn("15:30 1분봉 값 30000", h["stop_basis"])

    def test_tick_applies_prep_result_before_0830_trend_sell(self):
        P.save(self.R.POSITIONS, pos_with())
        self.fk.balance_rows = [bal_row("084670", 28)]
        res = self.started("08:25:00", live=False)
        out = prep_out()
        out["recovery"] = None
        out["official_judge"] = {"084670": {"close": 32000.0, "stop": 30000.0, "trend": True, "basis": "수정주가 일봉"}, "_missing": []}
        res.prep_thread, res.prep_result, res.prep_reason = object(), out, "공식 시세 반영 뒤 다시 적재"
        res.judge = lambda *a, **k: (_ for _ in ()).throw(AssertionError("주 스레드에서 계산"))
        res.tick(self.clk())
        p = jl(self.R.POSITIONS)
        self.assertEqual((p["sell_duty"]["084670"]["kind"], p["held"]["084670"]["judged_by"]), ("trend", "공식 일봉 (다음 날 재판단)"))
        self.assertFalse([e for e in res.day.s.get("errors", []) if "계산" in str(e)])
        self.clk.at("08:30:00")
        res.tick(self.clk())
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["084670"])      # 08:30 추세 매도에 들어감

    def test_official_retry_every_2min_before_0830(self):
        from kiwoom import krx_official
        res = self.started("08:05:30", live=False)
        res.day.mark("data", {"official_missing_dates": ["2026-10-23"], "attempt": "08:03:00"})
        calls = []
        saved = krx_official.fetch_day
        krx_official.fetch_day = lambda d: calls.append(d) or pd.DataFrame()
        self.addCleanup(setattr, krx_official, "fetch_day", saved)
        res.retry_official()
        self.assertEqual(len(calls), 1)                                             # 2분 지나면 다시 (예전 10분)
        res.day.s["steps"]["data"]["attempt"] = "09:03:00"
        self.clk.at("09:05:30")
        res.retry_official()
        self.assertEqual(len(calls), 1)                                             # 08:30 뒤는 10분 그대로


# ---------------------------------------------------------------- 4 오래된 시세

class StaleQuotes(Base6):
    def test_fresh_prices_and_snapshot_anomalies(self):
        import run_day as RD
        snap = pd.DataFrame({"code": ["A", "B", "C", "D"], "open": [1.0] * 4, "high": [1.0] * 4, "low": [1.0] * 4, "close": [1.0, 2.0, 3.0, 4.0],
                             "volume": [10.0, 10.0, 0.0, 10.0], "amount": [1.0] * 4, "dt": ["20261026", "20261023", "20261026", "20261026"],
                             "cntr_tm": ["151801", "151500", "000000", "000000"]})
        px, stale = self.R.fresh_prices(snap, pd.Timestamp("2026-10-26"))
        self.assertEqual((px, sorted(stale)), ({"A": 1.0}, ["B", "C", "D"]))
        bad = RD.snap_anomalies(snap, None, pd.Timestamp("2026-10-26"))
        self.assertEqual(sorted(bad), ["B", "D"])                                   # 거래량 0(C)은 이상 아님
        self.assertEqual(sorted(RD.snap_rows(snap, None, pd.Timestamp("2026-10-26"))), ["A"])
        self.assertEqual(sorted(RD.snap_rows(snap, None)), ["A", "B", "D"])          # 날짜를 안 주면 예전 그대로

    def test_stale_quote_no_stop_and_requeried_next_minute(self):
        P.save(self.R.POSITIONS, pos_with())
        self.fk.balance_rows = [bal_row("084670", 28)]
        res = self.started("10:00:00", live=False)
        rows = {"dt": "20261023", "tm": "151500"}
        asked = []
        Q.snapshot = lambda caller, codes, **k: (asked.append(list(codes)) or None, pd.DataFrame(
            {"code": list(codes), "close": [20000.0] * len(codes), "dt": rows["dt"], "cntr_tm": rows["tm"]}), {})
        res.stop_minute()
        self.assertEqual(sends(self.fk, "kt10001"), [])                              # 어제 시세 20000 < 손절가 30000 이어도 팔지 않음
        self.assertEqual(res.day.s["stale_quotes"]["084670"]["n"], 1)
        rows.update(dt="20261026", tm="100059")
        self.clk.at("10:01:00")
        res.stop_minute()
        self.assertEqual(asked, [["084670"], ["084670"]])                            # 다음 분에 다시 조회
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["084670"])


# ---------------------------------------------------------------- 5 종목 일봉 누락

class MissingRow(unittest.TestCase):
    def test_lacking_rows_and_exclusion(self):
        import daily
        off = pd.DataFrame({"date": pd.to_datetime(["2026-10-21", "2026-10-22", "2026-10-23"]), "volume": [100, 0, 50]})
        have = pd.to_datetime(["2026-10-21"])
        self.assertEqual(daily.lacking_rows(off, have, pd.Timestamp("2026-10-20")), ["2026-10-23"])     # 10-22 는 거래량 0 이라 안 셈
        self.assertEqual(daily.lacking_rows(off, have, pd.Timestamp("2026-10-20"), pd.Timestamp("2026-10-23")), [])  # 기다리는 날은 안 셈
        self.assertEqual(daily.lacking_rows(None, have, pd.Timestamp("2026-10-20")), [])
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "update.json"), "w", encoding="utf-8") as f:
            json.dump({"missing_row": [["005930", ["2026-10-23"]]], "adj_changed": ["000660"]}, f)
        saved = daily.APPEND_DIR
        daily.APPEND_DIR = tmp
        self.addCleanup(setattr, daily, "APPEND_DIR", saved)
        ex = daily.excluded_codes()
        self.assertEqual((ex["missing_row"], ex["adj_changed"]), (["005930"], ["000660"]))
        self.assertIn("005930", set().union(*ex.values()))                         # Store 가 메모리 적재에서 뺌 = 그날 계산 제외


# ---------------------------------------------------------------- 6 의도 파일 손상

class CorruptIntents(unittest.TestCase):
    def test_sells_continue_buys_blocked(self):
        from test_orders import PLAN
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        p = os.path.join(tmp, "intents", "2026-10-26.json")
        os.makedirs(os.path.dirname(p))
        open(p, "w").write('[{"intent_id": "x", "status": "ACCEPT')
        clk, notes = FakeClock("2026-10-26 10:00:00"), []
        fk = FakeKiwoom(clk)
        fk.balance_rows = [bal_row("084670", 28)]
        om = O.OrderManager(O.Broker(fk, allow_orders=True), clock=clk, sleep=clk.sleep, notify=notes.append, store=O.IntentStore(p, clock=clk))
        self.assertTrue(om.store.corrupt)
        duty = {"084670": O.new_duty("stop", "2026-10-26 09:59:00", "현재가 < 손절가", price=1.0, stop=2.0)}
        r = om.send_duties(duty, {"084670": {"file_qty": 28, "acct": None}})        # 계좌 조회 실패: 걸린 매도를 몰라 안 보냄
        self.assertIn("의도 파일 손상", r[0]["skipped"])
        r = om.send_duties(duty, {"084670": {"file_qty": 28, "acct": {"qty": 28, "sellable": 28}}})
        self.assertEqual(r[0]["status"], O.ACCEPTED)                                # 계좌 매도 가능 수량으로 매도
        clk.t = pd.Timestamp("2026-10-26 15:22:00")
        b = om.place_buys(PLAN, "본래")
        self.assertIn("매수 막음", b[0]["skipped"])
        self.assertEqual([x[2]["stk_cd"] for x in sends(fk, "kt10001")], ["084670"])
        self.assertEqual(sends(fk), [])
        self.assertTrue(any("매도 의무" in n and "주문 차단" in n for n in notes))


# 7 액면분할·병합 자동 조정 시험은 11K 보완 3 (자동 조정 제거) 으로 test_broad9.QtyReview 에


# ---------------------------------------------------------------- 8 점검 모드 종료

class CheckModeEnd(Base6):
    def test_end_on_first_filter_day_and_separate_perf(self):
        P.save(self.R.POSITIONS, pos_with(dict(HELD, kind="점검용", qty=10)))
        res = self.started("15:19:00", live=False)
        rec = res.end_check_mode(30_000_000.0, {"084670": {"qty": 10, "cur_price": 1000.0}}, {"084670": 1100.0})
        self.assertEqual((rec["ended"], rec["start_equity"], rec["check_value_at_end"]), ("2026-10-26", 30_000_000.0, 11000.0))
        self.assertTrue(any(a.startswith("[점검 모드 종료] 2026-10-26") for a in self.alerts))
        self.assertTrue(jl(self.R.check_mode_path())["ended"])
        res.day.set("live", {"plan": [], "kind": "점검용"})                         # 다음 필터 미통과일
        self.assertTrue(any("점검 모드 종료" in w for w in res.buy_gate()))
        res.day.set("live", {"plan": [], "kind": "본래"})
        self.assertFalse(any("점검 모드 종료" in w for w in res.buy_gate()))
        it = res.om.store.add(date="2026-10-26", stage="trend_late", side="sell", code="084670", qty=10, price=None, trde_tp="3", attempt=1,
                              kind="점검용", reason="추세", assumed_kind="open")
        res.om.store.update(it, status=O.ACCEPTED, filled_qty=10, fill_price=1200.0)
        P.save(self.R.POSITIONS, {"held": {}, "sell_duty": {}, "next_open_sells": {}, "trade_seq": 1, "duty_format": P.DUTY_FORMAT})
        perf = res.strategy_perf(30_050_000.0, {})
        sold = 12000 * (1 - 0.0035 - self.R.sb_tax(2026))
        self.assertAlmostEqual(perf["strategy_equity"], round(30_050_000.0 - (sold - 11000.0), 1), places=1)   # 점검용 손익은 빼고
        self.assertEqual(perf["since"], "2026-10-26")

    def test_no_end_before_filter_day(self):
        res = self.started("15:19:00", live=False)
        self.assertIsNone(res.strategy_perf(1.0, {}))
        res.day.set("live", {"plan": [], "kind": "점검용"})
        self.assertFalse(any("점검 모드 종료" in w for w in res.buy_gate()))       # 종료 전 모의 점검용 매수는 그대로


# ---------------------------------------------------------------- 11 환경별 수수료

class Commission(Base6):
    def test_mock_fee_cash_check_and_prod_fee_pnl(self):
        self.prev_state(steps=("after_close",), settlement={"d2": 30_000_000.0, "equity": 30_000_000.0})
        self.fk.fee = 0.0035
        res = self.started("15:22:00")
        res.buy()
        self.fk.fill(res.om.records[0]["ord_no"], 28, 33000, "15:30:00")
        self.clk.at("15:36:00")
        res.close_check()
        self.clk.at("15:40:10")
        res.settle()
        c, st = res.day.s["cash_check"], res.day.s["settlement"]
        self.assertTrue(c["ok"], c)
        self.assertEqual(c["commission"], 0.0035)
        amt = 28 * 33000
        p = st["pnl"]
        self.assertAlmostEqual(p["fee"], amt * 0.0035, places=1)
        self.assertAlmostEqual(p["pnl_prod_fee"] - p["pnl"], amt * (0.0035 - 0.00015), places=1)
        self.assertAlmostEqual(p["pnl_prod_fee"], -amt * 0.00015, delta=2.0)       # 가격 그대로면 실전 수수료만큼 손해
        self.R.CFG["env"] = "prod"
        self.assertEqual(self.R.commission_rate(), 0.00015)
        self.R.CFG["commission"] = {"prod": 0.0002}
        self.assertEqual(self.R.commission_rate(), 0.0002)
        self.assertIn("실전 수수료였다면", self.R.pnl_text(p))


if __name__ == "__main__":
    unittest.main()
