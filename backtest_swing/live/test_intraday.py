"""
11S 시험 (3단계 중 3번): C 일봉 수집 실패 회복 순서 (07:00 다시 받기 -> 장중 키움 재수집 -> 공식 값 채움(이벤트 날 계수) -> 그 종목만 제외),
데이터 버전 고정 (15:18 에 쓴 버전을 15:32 동결 대조·features_eod 가 찾음), D 다음 날 대조 (우리가 만든 이력 vs 다음 날 키움 수정 일봉 + 아침 기준가 측정 KRX 대조).
가짜 키움 일봉 서버·가짜 공식 시세·가짜 주문 서버·가짜 시계, 실제 키움 호출·data·logs 없음.

  python -m unittest backtest_swing/live/test_intraday.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"
import json
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

import pandas as pd

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_fetchfill as FF  # noqa: E402
from test_codex import Base, jl  # noqa: E402
from test_orders import sends  # noqa: E402
import common as C  # noqa: E402
import engine as E  # noqa: E402
import quotes as Q  # noqa: E402

D = FF.D
D27, D28, D29 = D[5], D[6], D[7]
AMT = {**{c: 9e9 for c in FF.CODES}, **{f"9{i:05d}": 1e9 for i in range(33)}}   # 적재 시도 40종목 -> 2종목 5% (차단), 1종목 2.5%


class Chart(FF.FakeChart):
    """FakeChart + 종목별 행 바꾸기(override[code](upd) -> [(날짜, 시가, 고가, 저가, 종가, 거래량)]) + 요청마다 부르는 hook"""

    def __init__(self):
        super().__init__()
        self.override, self.hook = {}, None

    def request(self, tr_code, json=None, **kw):
        if tr_code == "ka10081":
            if self.hook:
                self.hook(json)
            code, upd = json["stk_cd"], json["upd_stkpc_tp"]
            if not (self.script.get((code, upd)) or self.script.get(code)) and code in self.override:
                self.calls.append((tr_code, dict(json)))
                rows = [{"dt": f"{d:%Y%m%d}", "open_pric": str(o), "high_pric": str(h), "low_pric": str(lo), "cur_prc": str(c), "trde_qty": str(v),
                         "trde_prica": "1"} for d, o, h, lo, c, v in self.override[code](upd)]
                return self.resp({"return_code": 0, "stk_dt_pole_chart_qry": rows[::-1]})
        return super().request(tr_code, json, **kw)


def split_rows(p, day, k=0.5):
    """day 에 분할(k) 한 종목의 키움 일봉: 수정주가 = 새 기준(이전 날 x k, 거래량 / k), 원주가 = 이전 날 p, day 부터 p x k"""
    def f(upd):
        out = []
        for d in D:
            before = d < day
            px = p * k if (upd == "1" or not before) else p
            vol = (100 / k) if (upd == "1" and before) else 100
            out.append((d, px, px, px, px, vol))
        return out
    return f


class DailyEnv(FF.Env):
    def setUp(self):
        super().setUp()
        self.chart = Chart()
        self.fk = self.chart
        import daily
        daily.Caller = lambda *a, **k: FF.fake_caller(self.chart)                 # 갱신·재수집이 각자 Caller (연속 실패 따로 셈)

    def set_off(self, code, day, **kv):
        m = (self.off.code == code) & (self.off.date == day)
        for k, v in kv.items():
            self.off.loc[m, k] = v


# ---------------------------------------------------------------- C3 공식 값 채움 (이벤트 날 계수·스팩·정지)

class Fill(DailyEnv):
    def test_event_day_fill_rebases(self):
        """10-29 2:1 분할 날 수집 실패 -> 기준가 1,500 / 전날 종가 3,000 = 0.5 로 이전 이력 다시 맞춰 채움 = 키움 새 이력"""
        self.chart.script["000003"] = ["empty"] * 20
        self.set_off("000003", D29, open=1500.0, high=1500.0, low=1500.0, close=1500.0, chg=0.0)
        self.daily.update()
        u = self.u()
        self.assertNotIn("000003", [c for c, _ in u["fetch_error"]])
        f = u["official_filled"]["000003"]
        self.assertEqual((f["events"], f["k_total"]), ([["2026-10-29", 0.5]], 0.5))
        self.assertAlmostEqual(u["rebased"]["000003"]["k"], 0.5)
        st = self.daily.Store(["000003"])
        a, r = st.adj["000003"].set_index("timestamp"), st.raw["000003"].set_index("timestamp")
        self.assertTrue((a.close == 1500.0).all())                                               # 기본 파일·이전 행·채운 행 모두 새 기준
        self.assertEqual(a.volume.loc[D[:7]].tolist(), [200.0] * 7)                              # 이벤트 앞 거래량 / 0.5
        self.assertEqual(a.volume.at[D29], 100.0)
        self.assertEqual(r.factor.loc[[D28, D29]].tolist(), [0.5, 1.0])
        self.assertEqual(r.close.loc[[D28, D29]].tolist(), [3000.0, 1500.0])                     # 원주가 그대로

    def test_spac_small_base_change_not_event(self):
        """기준가 / 전날 종가 = 1.0014 (스팩류 0.14%) -> 이벤트 아님, 예전처럼 채움 (계수 1)"""
        self.chart.script["000004"] = ["empty"] * 20
        self.set_off("000004", D29, chg=4000.0 - 4000.0 * 1.0014)
        self.daily.update()
        u = self.u()
        self.assertIn("000004", u["official_filled"])
        self.assertNotIn("events", u["official_filled"]["000004"])
        self.assertNotIn("000004", u["rebased"])
        st = self.daily.Store(["000004"])
        self.assertTrue((st.adj["000004"].close == 4000.0).all())

    def test_halt_auction_and_no_trade_excluded(self):
        """이벤트(5:1 병합 재개)인데 전날 거래 없음(거래정지 뒤 경매) -> 그 종목만 제외, 그날 거래 없음도 제외"""
        self.chart.script["000005"] = ["empty"] * 20
        self.chart.script["000002"] = ["empty"] * 20
        for n in ("adj.parquet", "raw.parquet"):                                                 # 000005 10-28 거래정지 (거래량 0)
            p = os.path.join(self.ad, n)
            x = pd.read_parquet(p)
            x.loc[(x.code == "000005") & (pd.to_datetime(x.timestamp) == D28), "volume"] = 0.0
            x.to_parquet(p, index=False)
        self.set_off("000005", D29, open=25000.0, high=25000.0, low=25000.0, close=25000.0, chg=0.0)
        self.set_off("000002", D29, volume=0.0, open=0.0)
        self.daily.update()
        u = self.u()
        self.assertEqual(sorted(c for c, _ in u["fetch_error"]), ["000002", "000005"])
        self.assertIn("전날 거래 없음", u["fill_unfilled"]["000005"])
        self.assertIn("거래 없음", u["fill_unfilled"]["000002"])
        self.assertEqual(self.daily.excluded_codes()["fetch_error"], ["000002", "000005"])   # 그 종목만 제외


# ---------------------------------------------------------------- C2 장중 재수집 (상주)

class Intraday(Base):
    def setUp(self):
        super().setUp()
        self.base_tmp, self.kiwoom = self.tmp, self.fk
        self.addCleanup(shutil.rmtree, self.base_tmp, True)
        self.de = DailyEnv("setUp")
        self.de.setUp()
        self.addCleanup(self.de.doCleanups)
        self.fk, self.chart, self.daily, self.ad = self.kiwoom, self.de.chart, self.de.daily, self.de.ad
        self.chart.script["000006"] = ["empty"] * 50                                          # 끝내 실패 (채울 날 기준가 모름)
        self.de.set_off("000006", D29, chg=float("nan"))
        self.chart.script["000007"] = ["empty"] * 3                                           # 07:00 만 실패 (바탕 행 없음 -> 못 채움)
        self.chart.override["000007"] = split_rows(7000.0, D28)                               # 10:00 에 받으면 10-28 분할 종목
        self.de.set_off("000007", D28, open=3500.0, high=3500.0, low=3500.0, close=3500.0, chg=0.0)
        self.de.set_off("000007", D29, open=3500.0, high=3600.0, low=3500.0, close=3500.0, chg=0.0)
        self.preps = []

    def resident(self):
        res = super().resident()
        res.request_prep = lambda reason: self.preps.append(reason)
        return res

    def morning(self, hms):
        self.daily.update()
        res = self.started(hms, live=False)
        res.day.mark("data", {"daily_update": "ok"})
        res.prep_reason = "하루 시작"
        res.ctx.update(excluded=self.daily.excluded_codes(), amt20=dict(AMT))
        res.check_exclusions()
        return res

    def prep_out(self):
        class Pool:
            def close(self):
                pass
        u = jl(C.data_file("update.json", self.ad))
        return {"pool": Pool(), "boosters": [], "cols": [], "index": {}, "ledgers": {"strategy": [], "full": []}, "frozen": {"ok": True},
                "amt20": dict(AMT), "excluded": self.daily.excluded_codes(), "filled": self.daily.official_filled(),
                "prep": {"model": {"month": "2026-10", "check": {"ok": True}}, "ledger_as_of": "2026-10-23", "ledger_version": 3,
                         "data": {"daily_up_to_date": True, "data_version": u["data_version"], "prev_trading_day": "2026-10-23",
                                  "official_missing_dates": [], "expected_prev_trading_day": "2026-10-23", "data_last_date": "2026-10-23",
                                  "version_dir": os.path.basename(C.data_dir(self.ad))}}}

    def run_slot(self, res, hms):
        self.clk.at(hms)
        res.refetch_tick(self.clk())
        res.refetch_thread.join(10)
        res.refetch_tick(self.clk())                                                          # 결과 적용

    def test_0700_fail_1000_refetch_reload_buys(self):
        res = self.morning("07:30:00")
        self.assertEqual(self.daily.excluded_codes()["fetch_error"], ["000006", "000007"])
        self.assertIn("market_fail:일봉 수집 실패", res.day.s["buy_block"])                      # 2 / 40 = 5%
        v0 = C.data_dir(self.ad)
        self.run_slot(res, "10:00:00")
        r = res.day.s["intraday_refetch"]["10:00:00"]
        self.assertEqual((r["codes"], r["got"], sorted(r["failed"])), (["000006", "000007"], ["000007"], ["000006"]))
        self.assertNotEqual(C.data_dir(self.ad), v0)                                           # 새 데이터 버전
        u = jl(C.data_file("update.json", self.ad))
        self.assertTrue(u["data_version"].endswith("|refetch"))
        self.assertEqual([c for c, _ in u["fetch_error"]], ["000006"])
        self.assertAlmostEqual(u["rebased"]["000007"]["k"], 0.5)                                # 아침 갱신과 같은 처리: 11R 계수 대조·다시 맞춤
        st = self.daily.Store(["000007"])
        a = st.adj["000007"].set_index("timestamp")
        self.assertTrue((a.close == 3500.0).all())                                              # 기본 파일 7,000 x 0.5 + 키움 새 이력
        self.assertEqual(a.high.at[D29], 3600.0)                                                # 공식 정규장 값 덮어쓰기
        self.assertEqual(self.preps, ["장중 재수집 뒤 다시 적재 (10:00: ['000007'])"])
        self.assertTrue(any(x.startswith("[장중 재수집] 10:00") for x in self.alerts))
        res.prep_reason, res.prep_thread, res.prep_result = self.preps[-1], object(), self.prep_out()
        res.apply_prep()                                                                      # 다시 적재
        self.assertNotIn("market_fail:일봉 수집 실패", res.day.s["buy_block"])                   # 1 / 40 -> 풀림
        self.assertTrue(any(x.startswith("[일봉 수집 차단 해제]") for x in self.alerts))
        sent = self.snap_buy(res, "000007")
        self.assertEqual([s[2]["stk_cd"] for s in sent], ["000007"])                           # 정상 매수
        vname = os.path.basename(C.data_dir(self.ad))
        self.assertEqual(C.pinned_versions(self.ad)["2026-10-26"], vname)                      # 15:18 에 쓴 버전 고정
        self.assertEqual(jl(os.path.join(self.R.LOG_DIR, "2026-10-26.json"))["data_dir_version"], vname)

    def snap_buy(self, res, top):
        self.clk.at("15:18:40")
        codes = [c for c in AMT if c != "000006"]
        snap = pd.DataFrame({"code": codes, "open": 3500.0, "high": 3600.0, "low": 3400.0, "close": 3500.0, "volume": 1e5, "amount": 3.5e8,
                             "dt": "20261026"})
        raw = pd.DataFrame({"stk_cd": codes, "upl_pric": "+4550", "lst_pric": "-2450", "base_pric": "3500"})
        Q.snapshot = lambda caller, cs, **k: (raw, snap, {"returned": len(codes), "codes": len(codes), "seconds": 1.0})
        Q.index_now = lambda caller: ({"kospi": 1.0, "kosdaq": 1.0}, None)
        saved_uc = C.sd.universe_codes
        C.sd.universe_codes = lambda: list(codes)
        t3 = pd.DataFrame({"code": [top], "score": [0.9], "raw_close": [3500.0]})
        out = {"filter": {"filter_ok": True, "prev_kospi_close": 2700.0, "prev_kospi_ma120": 2500.0}, "eligible": t3[["code", "score"]],
               "market": {}, "errors": [], "ledgers": {n: {"top3": t3, "busy": [], "skipped_busy": [], "ledger_status": []} for n in ("strategy", "full")}}
        saved = E.run_day
        try:
            E.run_day = lambda *a, **k: out
            res.snapshot_and_plan()
        finally:
            E.run_day, C.sd.universe_codes = saved, saved_uc
        self.clk.at("15:22:00")
        res.buy()
        return sends(self.fk)

    def test_refetch_does_not_block_stop_watch(self):
        res = self.morning("07:30:00")
        res.auto_data_jobs = True
        gate = threading.Event()
        self.chart.hook = lambda j: gate.wait(10) if threading.current_thread().name == "refetch" else None
        self.clk.at("10:00:00")
        t0 = time.time()
        res.tick(self.clk())
        self.assertLess(time.time() - t0, 2.0)                                                  # 주 반복은 기다리지 않음
        self.assertTrue(res.refetch_thread.is_alive())
        self.clk.at("10:01:00")
        t0 = time.time()
        res.tick(self.clk())                                                                  # 재수집이 막혀 있어도 손절 감시는 돈다
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(res.last_stop_minute, "2026-10-26 10:01")
        self.assertEqual(list(res.day.s["intraday_refetch"]), ["10:00:00"])                    # 한 번에 하나
        gate.set()
        res.refetch_thread.join(10)
        res.tick(self.clk())
        self.assertEqual(res.day.s["intraday_refetch"]["10:00:00"]["got"], ["000007"])

    def test_no_start_after_1510_and_no_publish_after_1513(self):
        res = self.morning("07:30:00")
        self.clk.at("15:10:00")
        res.refetch_tick(self.clk())
        self.assertIsNone(res.refetch_thread)                                                  # 15:10 뒤에는 시작 안 함
        self.assertNotIn("intraday_refetch", res.day.s)
        v0 = C.data_dir(self.ad)
        self.chart.hook = lambda j: self.clk.at("15:13:30") if j["stk_cd"] == "000007" else None   # 000007 을 받는 동안 15:13 지남
        self.run_slot(res, "15:09:00")
        r = res.day.s["intraday_refetch"]["15:00:00"]
        self.assertIsNone(r["published"])
        self.assertIn("공개 시각 지남", r["reason"])
        self.assertEqual((C.data_dir(self.ad), self.preps), (v0, []))                           # 새 버전·다시 적재 없음

    def test_late_reload_after_1518_keeps_old_without_block(self):
        res = self.morning("07:30:00")
        res.pool = SimpleNamespace(close=lambda: None)
        res.ctx["prep"] = self.prep_out()["prep"]
        self.clk.at("15:18:30")
        res.prep_reason, res.prep_thread, res.prep_result = "장중 재수집 뒤 다시 적재 (15:00: ['000007'])", object(), self.prep_out()
        old = res.pool
        res.apply_prep()
        self.assertIs(res.pool, old)                                                           # 이전 적재 유지
        self.assertNotIn("late_reload", res.day.s.get("buy_block") or [])                      # 장중 재수집 늦음은 차단 안 함
        res.prep_reason, res.prep_thread, res.prep_result = "공식 시세 반영 뒤 다시 적재", object(), self.prep_out()
        res.apply_prep()
        self.assertIn("late_reload", res.day.s["buy_block"])                                   # 다른 이유는 예전처럼 (B6)


# ---------------------------------------------------------------- 데이터 버전 고정

class Pin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ad = os.path.join(self.tmp, "append")
        os.makedirs(self.ad)

    def publish(self, tag):
        return C.publish_data({"update.json": {"data_version": tag}, "adj.parquet": pd.DataFrame({"code": ["000001"]})}, self.ad)

    def test_pinned_version_survives_and_rerun_features_find_it(self):
        import features_eod as FE
        import model as M
        import run_day as R
        vs = [self.publish("v0"), self.publish("v1|15:18")]
        C.pin_version("2026-10-26", vs[1], self.ad)
        vs += [self.publish(f"v{i}") for i in range(2, 7)]                                     # 다음 날 07:00·재반영·장중 재수집 ...
        have = sorted(os.listdir(os.path.join(self.ad, "versions")))
        self.assertIn(vs[1], have)                                                              # 고정 버전은 남음
        self.assertNotIn(vs[0], have)
        self.assertEqual(len(have), 4)                                                          # 최근 3개 + 고정
        pinned = os.path.join(self.ad, "versions", vs[1])
        self.assertEqual(FE.find_version("v1|15:18", self.ad), pinned)
        self.assertEqual(FE.find_version("x", self.ad, name=vs[1]), pinned)
        # 15:32 동결 대조 (run_day.rerun): 그날 로그의 고정 버전으로 작업 프로세스·지수 모두
        snap = os.path.join(self.tmp, "snap.parquet")
        pd.DataFrame({"p_code": ["000001"], "p_open": [1.0], "p_high": [1.0], "p_low": [1.0], "p_close": [1.0], "p_volume": [1.0],
                      "p_amount": [1.0]}).to_parquet(snap)
        C.atomic_json(os.path.join(self.tmp, "2026-10-26.json"), {
            "date": "2026-10-26", "errors": [], "ledger_before": {}, "data_version": "v1|15:18", "data_dir_version": vs[1],
            "live": {"snapshot": {"file": snap}, "index_values": {}, "ledgers": {n: {"top3": []} for n in ("strategy", "full")}}})
        seen = {}

        class Pool:
            def __init__(self, *a, **k):
                seen["vdir"] = k.get("vdir")

            def close(self):
                pass
        import daily
        saved = (R.LOG_DIR, R.APPEND_DIR, M.load, E.ShardPool, E.run_day, daily.index_series, C.sd.universe_codes)
        try:
            R.LOG_DIR, R.APPEND_DIR = self.tmp, self.ad
            M.load = lambda mo: (None, None, {}, {"ok": True})
            E.ShardPool = Pool
            daily.index_series = lambda: seen.setdefault("index_dir", C.data_dir()) and {}
            C.sd.universe_codes = lambda: ["000001"]
            E.run_day = lambda *a, **k: {"eligible": pd.DataFrame({"code": [], "score": []}), "factor_default_n": 0, "vscale_default_n": 0,
                                         "ledgers": {n: {"top3": pd.DataFrame({"code": [], "score": []})} for n in ("strategy", "full")}}
            out = os.path.join(self.tmp, "rerun.json")
            R.rerun("2026-10-26", "", out)
        finally:
            (R.LOG_DIR, R.APPEND_DIR, M.load, E.ShardPool, E.run_day, daily.index_series, C.sd.universe_codes) = saved
        self.assertEqual((seen["vdir"], seen["index_dir"]), (pinned, pinned))
        self.assertEqual(jl(out)["rerun"]["data_dir"], vs[1])
        self.assertNotEqual(C.data_dir(self.ad), pinned)                                        # 지금 버전은 그대로 (고정은 읽기만)


# ---------------------------------------------------------------- D 다음 날 대조

class NextDay(DailyEnv):
    def test_our_history_vs_next_day_kiwoom(self):
        """10-29: 000003 공식 값 채움, 000002 10-29 분할(15:18 그날 계수 0.5), 000001 은 그날 계수 0.5 로 잘못 봄 -> 다음 갱신의 키움 일봉과 대조"""
        self.chart.script["000003"] = ["empty"] * 3
        self.chart.override["000002"] = split_rows(2000.0, D29)
        self.set_off("000002", D29, open=1000.0, high=1000.0, low=1000.0, close=1000.0, chg=0.0)
        self.daily.update()
        u1 = jl(C.data_file("update.json", self.ad))
        self.assertIn("000003", u1["official_filled"])
        C.pin_version("2026-10-29", os.path.basename(C.data_dir(self.ad)), self.ad)              # 10-29 15:18 에 쓴 버전
        base3 = FF.FakeChart().request

        def kiwoom3(upd):                                                                       # 다음 날 키움: 10-29 거래량 150 (우리 채움 100)
            j = base3("ka10081", json={"stk_cd": "000003", "upd_stkpc_tp": upd}).json()["stk_dt_pole_chart_qry"]
            return [(pd.Timestamp(x["dt"]), x["open_pric"], x["high_pric"], x["low_pric"], x["cur_prc"],
                     "150" if x["dt"] == "20261029" else x["trde_qty"]) for x in j][::-1]
        self.chart.override["000003"] = kiwoom3
        self.daily.update(check={"day": "2026-10-29", "base_events": {"000002": 0.5, "000001": 0.5}})
        nd = jl(C.data_file("update.json", self.ad))["next_day_check"]
        self.assertEqual(nd["version"], C.pinned_versions(self.ad)["2026-10-29"])               # 전날 15:18 고정 버전과 대조
        self.assertEqual(nd["checked"], {"filled": 1, "rebased": 1, "base_events": 2})
        self.assertEqual(sorted((m["code"], m["kind"]) for m in nd["mismatch"]), [("000001", "base_events"), ("000003", "filled")])
        m3 = [m for m in nd["mismatch"] if m["code"] == "000003"][0]
        self.assertEqual(list(m3["raw_kiwoom_ours"]), ["volume"])
        m1 = [m for m in nd["mismatch"] if m["code"] == "000001"][0]
        self.assertAlmostEqual(m1["k_kiwoom"], 1.0)


class NextDayReport(Base):
    def setUp(self):
        super().setUp()
        import daily
        self.daily = daily
        self.ad = os.path.join(self.tmp, "append")
        os.makedirs(self.ad)
        self.addCleanup(setattr, daily, "APPEND_DIR", daily.APPEND_DIR)
        daily.APPEND_DIR = self.ad
        nd = {"version": "v", "day": "2026-10-23", "checked": {"filled": 1, "base_events": 1}, "not_fetched": {},
              "mismatch": [{"code": "000003", "kind": "filled", "raw_kiwoom_ours": {"volume": [[150.0, 100.0]]}}]}
        C.publish_data({"update.json": {"run": "2026-10-26 07:20:00", "next_day_check": nd}}, self.ad)
        C.atomic_json(os.path.join(self.tmp, "2026-10-23.json"), {"live": {"base_events": {"000002": 0.5}}})

    def test_alert_with_probe_compare(self):
        res = self.started("08:25:00", live=False)
        res.day.mark("data")
        self.assertEqual(res.next_day_input(res.day.date), {"day": "2026-10-23", "base_events": {"000002": 0.5}})   # 아침 갱신에 넘김
        res.next_day_report(self.clk())
        self.assertNotIn("next_day_check", res.day.s)                                          # 측정 대조(08:20 측정 때 만들어짐) 기다림
        pdir = os.path.join(self.R.LOG_DIR, "base_probe")
        os.makedirs(pdir)
        C.atomic_json(os.path.join(pdir, "2026-10-23_compare.json"), {"krx_events": ["368600"], "slots": {"0705": {"codes": 3, "same_as_krx": 2, "differs": 1,
                                                                                                                  "events_same_as_krx": [], "events_still_prev_close": ["368600"]}}})
        self.clk.at("08:26:00")
        res.next_day_report(self.clk())
        x = res.day.s["next_day_check"]
        self.assertEqual((x["day"], x["base_probe"]["slots"]["0705"]["differs"]), ("2026-10-23", 1))
        self.assertEqual(x["history"]["mismatch"][0]["code"], "000003")
        al = [a for a in self.alerts if a.startswith("[다음 날 대조 불일치] 2026-10-23")]
        self.assertEqual(len(al), 1)
        self.assertIn("000003", al[0])
        self.clk.at("08:27:00")
        res.next_day_report(self.clk())
        self.assertEqual(len([a for a in self.alerts if a.startswith("[다음 날 대조 불일치]")]), 1)   # 하루 한 번

    def test_no_probe_records_at_0900(self):
        res = self.started("08:59:00", live=False)
        res.day.mark("data")
        res.next_day_report(self.clk())
        self.assertNotIn("next_day_check", res.day.s)
        self.clk.at("09:00:00")
        res.next_day_report(self.clk())
        self.assertIn("없음", res.day.s["next_day_check"]["base_probe"])


if __name__ == "__main__":
    unittest.main()
