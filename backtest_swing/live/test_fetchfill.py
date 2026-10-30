"""
11P 일봉 수집 실패 다시 받기·공식 값 채움 재현 시험 (2026-10-30 서브 PC 실측: 07:00 일봉 갱신에서 15종목 "empty"·1종목 시간 초과,
대상 후보 6종목 때문에 신규 매수 안 함, 08:04 공식 시세 재반영 뒤에도 차단 유지).
가짜 키움 일봉 서버(종목별 빈 응답·return_code 오류·유량 오류)·가짜 공식 시세. 실제 조회·data·logs 없음.

  python -m unittest backtest_swing/live/test_fetchfill.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"
import shutil
import sys
import tempfile
import time
import unittest
from collections import Counter
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import Base, jl  # noqa: E402
import common as C  # noqa: E402

D = [pd.Timestamp(x) for x in ("2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29")]
FILE_END = pd.Timestamp("2026-10-26")    # 백테스트 일봉 파일 마지막 날
CARRY_DAYS = D[5:7]                      # 지난 버전 추가분 (10-27, 10-28)
CODES = ["000001", "000002", "000003", "000004", "000005", "000006", "000007"]
RATE1700 = "[1700:허용된 요청 개수를 초과하였습니다. API ID=ka10081]"


def price(code):
    return 1000.0 * int(code)


class FakeChart:
    """ka10081·ka20006 가짜 서버. script[(code, upd)] = 결과 목록 (차례로 씀, 다 쓰면 정상 행):
    rows / empty(return_code 0, 빈 목록) / rc_err(return_code 1) / rate1700(return_code 1 + 1700 메시지) / rate5 / 429"""

    def __init__(self):
        self.script, self.calls = {}, []

    def request(self, tr_code, json=None, **kw):
        self.calls.append((tr_code, dict(json or {})))
        if tr_code == "ka20006":
            rows = [{"dt": f"{d:%Y%m%d}", "open_pric": "100000", "high_pric": "100000", "low_pric": "100000", "cur_prc": "100000"} for d in D]
            return self.resp({"return_code": 0, "inds_dt_pole_qry": rows[::-1]})
        code, upd = json["stk_cd"], json["upd_stkpc_tp"]
        q = self.script.get((code, upd)) or self.script.get(code) or []
        how = q.pop(0) if q else "rows"
        if how == "429":
            return None
        if how == "empty":
            return self.resp({"return_code": 0, "return_msg": "정상적으로 처리되었습니다", "stk_dt_pole_chart_qry": []})
        if how == "rc_err":
            return self.resp({"return_code": 1, "return_msg": "일시적인 오류입니다"})
        if how == "rate1700":
            return self.resp({"return_code": 1, "return_msg": RATE1700})
        if how == "rate5":
            return self.resp({"return_code": 5, "return_msg": RATE1700})
        p = price(code)
        rows = [{"dt": f"{d:%Y%m%d}", "open_pric": str(p), "high_pric": str(p), "low_pric": str(p), "cur_prc": str(p), "trde_qty": "100",
                 "trde_prica": "1"} for d in D]
        return self.resp({"return_code": 0, "stk_dt_pole_chart_qry": rows[::-1]})

    @staticmethod
    def resp(j):
        return SimpleNamespace(json=lambda: j, headers={})


def fake_caller(client):
    c = C.Caller.__new__(C.Caller)
    c.env, c.n, c.errors, c.hourly, c.fail_streak, c.stopped, c.rps = "prod", 0, [], Counter(), 0, None, 5
    c.c = client
    return c


def official(dates, codes=CODES, chg=None):
    """공식 시세 (원주가). chg = {code: 전일대비} (없으면 0 = 기준가가 전날 종가와 같음)"""
    rows = []
    for d in dates:
        for c in codes:
            p = price(c)
            rows.append({"date": d, "code": c, "name": c, "market": "KOSPI", "open": p, "high": p, "low": p, "close": p, "volume": 100.0,
                         "amount": 1_234_567.0, "list_shares": 1e6, "chg": (chg or {}).get(c, 0.0)})
    return pd.DataFrame(rows)


class Env(unittest.TestCase):
    """daily.update 를 임시 폴더·가짜 서버로 (백테스트 일봉 파일·지수 파일·추가분 버전·공식 시세 캐시 모두 임시)"""

    def setUp(self):
        import daily
        from kiwoom import krx_official
        self.daily, self.krx = daily, krx_official
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        sd, s4 = daily.sd, daily.s4
        self.saved = (daily.APPEND_DIR, daily.Caller, getattr(daily, "RETRY_WAIT", None), getattr(daily, "RETRY_GAP", None), sd.EXT_DIR, sd.MERGED_DIR, sd.RAW_DIR, sd.KOSPI, s4.KOSDAQ,
                      sd.universe_codes, krx_official.load_range, krx_official.fetch_day, krx_official.CACHE_DIR, C.time)
        self.addCleanup(self.restore)
        ext, rawd, self.ad, cache = (os.path.join(self.tmp, n) for n in ("ext", "raw", "append", "cache"))
        for p in (ext, rawd, self.ad, cache):
            os.makedirs(p)
        for c in CODES:
            p = price(c)
            f = pd.DataFrame({"timestamp": D[:5], "open": p, "high": p, "low": p, "close": p, "volume": 100.0, "amount": 1e6})
            f.to_parquet(os.path.join(ext, f"{c}.parquet"), index=False)
            f.assign(factor=1.0).to_parquet(os.path.join(rawd, f"{c}.parquet"), index=False)
        for n in ("kospi", "kosdaq"):
            pd.DataFrame({"timestamp": D[:5], "open": 1000.0, "high": 1000.0, "low": 1000.0, "close": 1000.0}).to_parquet(
                os.path.join(self.tmp, f"{n}.parquet"), index=False)
        # 지난 버전 추가분 (10-27·10-28, 000007 은 없음 = 바탕 행 없음)
        prev = [c for c in CODES if c != "000007"]
        rows = pd.DataFrame([{"code": c, "timestamp": d, "open": price(c), "high": price(c), "low": price(c), "close": price(c), "volume": 100.0,
                              "amount": 1e6} for c in prev for d in CARRY_DAYS])
        rows.to_parquet(os.path.join(self.ad, "adj.parquet"), index=False)
        rows.assign(factor=1.0).to_parquet(os.path.join(self.ad, "raw.parquet"), index=False)
        pd.DataFrame({"name": "kospi", "timestamp": CARRY_DAYS, "close": [1.0, 1.0]}).to_parquet(os.path.join(self.ad, "index.parquet"), index=False)
        daily.APPEND_DIR, krx_official.CACHE_DIR = self.ad, cache
        daily.RETRY_WAIT = daily.RETRY_GAP = 0
        sd.EXT_DIR = sd.MERGED_DIR = ext
        sd.RAW_DIR = rawd
        sd.KOSPI, s4.KOSDAQ = os.path.join(self.tmp, "kospi.parquet"), os.path.join(self.tmp, "kosdaq.parquet")
        sd.universe_codes = lambda: list(CODES)
        self.off = official(D[5:])
        krx_official.load_range = lambda a, b: self.off[(self.off.date >= pd.Timestamp(a)) & (self.off.date <= pd.Timestamp(b))].reset_index(drop=True)
        krx_official.fetch_day = lambda d: (_ for _ in ()).throw(AssertionError("시험에서 실제 공식 시세 요청"))
        for d in D[5:]:
            pd.DataFrame({"x": [1]}).to_parquet(os.path.join(cache, f"{d:%Y%m%d}.parquet"))
        self.slept = []
        C.time = SimpleNamespace(time=time.time, sleep=self.slept.append)       # Caller 유량 백오프는 기다리지 않고 기록만
        self.fk = FakeChart()
        self.caller = fake_caller(self.fk)
        daily.Caller = lambda *a, **k: self.caller

    def restore(self):
        daily, sd, s4 = self.daily, self.daily.sd, self.daily.s4
        (daily.APPEND_DIR, daily.Caller, daily.RETRY_WAIT, daily.RETRY_GAP, sd.EXT_DIR, sd.MERGED_DIR, sd.RAW_DIR, sd.KOSPI, s4.KOSDAQ,
         sd.universe_codes, self.krx.load_range, self.krx.fetch_day, self.krx.CACHE_DIR, C.time) = self.saved

    def u(self):
        return jl(C.data_file("update.json", self.ad))

    def adj(self):
        a = pd.read_parquet(C.data_file("adj.parquet", self.ad))
        a["timestamp"] = pd.to_datetime(a.timestamp)
        return a

    def script(self):
        s = self.fk.script
        s[("000002", "1")] = ["rc_err"]                       # 첫 수집 return_code 오류 -> 다시 받기에서 정상
        s["000003"] = ["empty"] * 20                          # 끝내 빈 응답 -> 바탕 행 + 공식 값으로 채움
        s["000004"] = ["empty"] * 20                          # 끝내 빈 응답 + 그날 기준가 모름(전일대비 없음) -> 못 채움 (11S C3: 이벤트 날은 채움)
        s[("000005", "1")] = ["rate1700"]                     # return_code 1 + 1700 메시지 = 유량 초과 -> 백오프 뒤 정상
        s[("000006", "0")] = ["429", "rate5"]                 # HTTP 429·return_code 5 -> 백오프 뒤 정상
        s["000007"] = ["empty"] * 20                          # 끝내 빈 응답 + 바탕 행 없음 -> 못 채움
        self.off.loc[(self.off.code == "000004") & (self.off.date == D[7]), "chg"] = float("nan")


class Update(Env):
    def test_retry_fill_and_causes(self):
        self.script()
        self.daily.update()
        u = self.u()
        fe = dict(map(tuple, u["fetch_error"]))
        self.assertEqual(sorted(fe), ["000004", "000007"])                                         # 000002·000003·000005·000006 은 갖춰짐
        self.assertIn("empty: upd1 return_code 0", fe["000004"])                                 # 원인 원문 (return_code·return_msg·행 수)
        self.assertIn("정상적으로 처리되었습니다", fe["000004"])
        r2 = [x for x in u["fetch_retry"] if x["code"] == "000002"]
        self.assertEqual((r2[0]["pass"], r2[0]["result"]), (1, "ok"))
        self.assertIn("return_code 1 일시적인 오류입니다", r2[0]["before"])
        self.assertEqual(len([x for x in u["fetch_retry"] if x["code"] == "000004"]), 2)          # 두 번 더 받음
        self.assertTrue(any("1700" in e[2] for e in u["errors"]) and any(e[2] == "HTTP 429" for e in u["errors"]))
        self.assertEqual(len(self.slept), 3)                                                         # 유량 오류 3번 = 백오프 3번
        self.assertEqual(sorted(u["official_filled"]), ["000003"])
        self.assertEqual(u["official_filled"]["000003"]["dates"], ["2026-10-29"])
        self.assertIn("기준가 모름 (전일대비 없음)", u["fill_unfilled"]["000004"])
        self.assertIn("이전 추가분 행 없음", u["fill_unfilled"]["000007"])
        a = self.adj()
        x = a[a.code == "000003"].sort_values("timestamp")
        self.assertEqual(x.timestamp.tolist(), D[5:])                                                # 바탕 10-27·10-28 + 공식 10-29
        self.assertEqual((x.close.iat[-1], x.volume.iat[-1], x.amount.iat[-1]), (3000.0, 100.0, 1_000_000.0))   # 거래대금 백만원 반올림
        self.assertEqual(sorted(a.code.unique()), ["000001", "000002", "000003", "000005", "000006"])
        self.assertEqual(self.daily.excluded_codes()["fetch_error"], ["000004", "000007"])
        carry = pd.read_parquet(os.path.join(self.ad, "carry_adj.parquet"))
        self.assertEqual(sorted(carry.code.unique()), ["000004"])                                   # 남은 실패 종목의 바탕 행 보관

    def test_official_missing_then_reapply_fills(self):
        """07:00 에 전날(10-29) 공식 시세 없음 -> 보관 행만, 08:04 재반영 때 바탕 행 + 공식 값으로 채움 (못 채운 종목은 그대로 실패)"""
        self.script()
        full = self.off
        self.off = full[full.date < D[7]].reset_index(drop=True)
        os.remove(os.path.join(self.krx.CACHE_DIR, "20261029.parquet"))
        self.daily.update()
        u = self.u()
        self.assertEqual(u["official_missing_dates"], ["2026-10-29"])
        self.assertEqual(sorted(c for c, _ in u["fetch_error"]), ["000003", "000004", "000007"])  # 07:00 에는 못 채움 (기다리는 날)
        self.off = full
        pd.DataFrame({"x": [1]}).to_parquet(os.path.join(self.krx.CACHE_DIR, "20261029.parquet"))
        r = self.daily.reapply_official()
        self.assertTrue(r["ok"])
        self.assertEqual(r["filled"], ["000003"])
        self.assertEqual(sorted(r["unfilled"]), ["000004", "000007"])
        u = self.u()
        self.assertEqual(sorted(c for c, _ in u["fetch_error"]), ["000004", "000007"])
        self.assertEqual(u["official_filled"]["000003"]["last_close"], 3000.0)
        a = self.adj()
        self.assertEqual(a[a.code == "000003"].timestamp.sort_values().tolist(), D[5:])
        self.assertEqual(a[a.code == "000001"].timestamp.sort_values().tolist(), D[5:])            # 보관 행 보정도 그대로
        self.assertEqual(self.daily.excluded_codes()["fetch_error"], ["000004", "000007"])
        st = self.daily.Store(["000003"])
        self.assertEqual(st.adj["000003"].timestamp.max(), D[7])                                    # 메모리 적재에 들어감

    def test_abnormal_factor_or_dirty_carry_not_filled(self):
        """계수 비정상(기준가 / 전날 종가 = 200)이면 못 채움 (11S C3: 보통 이벤트 날은 채움 -> test_intraday), 바탕 행 factor 가 1 이 아니어도 못 채움"""
        self.fk.script["000003"] = ["empty"] * 20
        self.fk.script["000002"] = ["empty"] * 20
        self.off.loc[(self.off.code == "000003") & (self.off.date == D[7]), "chg"] = 3000.0 - 600000.0  # 기준가 600,000 / 3,000
        raw = pd.read_parquet(os.path.join(self.ad, "raw.parquet"))
        raw.loc[raw.code == "000002", "factor"] = 0.5
        raw.to_parquet(os.path.join(self.ad, "raw.parquet"), index=False)
        self.daily.update()
        u = self.u()
        self.assertEqual(sorted(c for c, _ in u["fetch_error"]), ["000002", "000003"])
        self.assertIn("계수 비정상", u["fill_unfilled"]["000003"])
        self.assertIn("factor 1 아님", u["fill_unfilled"]["000002"])
        self.assertEqual(u["official_filled"], {})


class CallerRate(unittest.TestCase):
    def test_1700_message_with_other_return_code_is_rate(self):
        slept, saved = [], C.time
        C.time = SimpleNamespace(time=time.time, sleep=slept.append)
        try:
            fk = FakeChart()
            fk.script[("000001", "1")] = ["rate1700"]
            c = fake_caller(fk)
            j, _, _ = c.post("ka10081", "/x", {"stk_cd": "000001", "base_dt": "20261030", "upd_stkpc_tp": "1"})
        finally:
            C.time = saved
        self.assertTrue(j["stk_dt_pole_chart_qry"])                                                # 다시 받아 정상 행
        self.assertEqual((slept, len(c.errors)), ([2], 1))
        self.assertFalse(C.rate_json({"return_code": 0, "return_msg": "1700 건"}))
        self.assertTrue(C.rate_json({"return_code": "5", "return_msg": ""}))


AMT40 = {f"9{i:05d}": 1e9 for i in range(37)}   # 11Q E: 적재 시도 40종목 (아래 3종목 + 37) -> 1종목 = 2.5%, 2종목 = 5%


class Release(Base):
    """11Q E: 일봉 수집 실패는 그 종목만 빠짐, 5% 이상이면 시장 전체 문제로 차단, 다시 적재에서 5% 아래면 풀림 (11P 후보 해제를 대체)"""

    def amt(self):
        return {**AMT40, "004170": 9e9, "141000": 9e9, "000009": 1e9}

    def test_block_released_when_failures_drop_below_5pct(self):
        res = self.started("07:30:00")
        res.prep_reason = "준비"
        res.ctx.update(excluded={"fetch_error": ["004170", "141000", "000009"]}, amt20=self.amt())       # 3 / 40 = 7.5%
        r = res.check_exclusions()
        self.assertTrue(r["blocked"])
        self.assertIn("market_fail:일봉 수집 실패", res.day.s["buy_block"])
        res.prep_reason = "공식 시세 반영 뒤 다시 적재 (보관 행 2739종목 보정)"                  # 08:04 재반영 뒤 다시 적재: 2종목 채워짐
        res.ctx["buy_allowed"] = True                                                         # apply_prep 가 다시 계산한 값
        res.ctx.update(excluded={"fetch_error": ["141000"]}, filled={"004170": {"dates": ["2026-10-23"], "last_close": 248000.0},
                                                                     "000009": {"dates": ["2026-10-23"], "last_close": 1.0}})
        r = res.check_exclusions()
        self.assertFalse(r["blocked"])
        self.assertNotIn("market_fail:일봉 수집 실패", res.day.s["buy_block"])
        self.assertFalse(any("차단" in w for w in res.buy_gate()))                              # 후보 141000 은 빠진 채 매수
        self.assertTrue(any("[일봉 수집 차단 해제]" in a and "004170" in a for a in self.alerts))
        self.assertEqual(res.day.s["buy_block_released"][0]["filled"], ["000009", "004170"])
        self.assertEqual(list(res.day.s["excluded_candidates"]["일봉 수집 실패"]), ["141000"])  # 다음 날 아침 확인 대상

    def test_block_kept_at_5pct(self):
        res = self.started("07:30:00")
        res.prep_reason = "준비"
        res.ctx.update(excluded={"fetch_error": ["004170", "141000", "000009"]}, amt20=self.amt())
        res.check_exclusions()
        res.ctx.update(excluded={"fetch_error": ["141000", "000009"]}, filled={"004170": {"last_close": 1.0}})   # 2 / 40 = 5%
        r = res.check_exclusions()
        self.assertTrue(r["blocked"])
        self.assertIn("market_fail:일봉 수집 실패", res.day.s["buy_block"])
        self.assertFalse(any("해제" in a for a in self.alerts))

    def test_other_blocks_not_released(self):
        res = self.started("07:30:00")
        res.prep_reason = "준비"
        res.block_buys("holding_mismatch", "x", "x")
        res.ctx.update(excluded={"fetch_error": []}, amt20={})
        res.check_exclusions()
        self.assertEqual(res.day.s["buy_block"], ["holding_mismatch"])
        self.assertFalse(any("해제" in a for a in self.alerts))

    def snap_buy(self, base):
        """15:18 스냅샷·계획 -> 15:22 매수. 990006 = 공식 값으로 채운 종목 (채운 종가 10,000), 전날 평균 39억(후보 밖), 당일 300억 급증으로 상위 1"""
        import test_codex
        from test_orders import sends
        E = test_codex.E
        res = test_codex.Codex.snapshot_ready(self)
        raw = pd.DataFrame({"stk_cd": ["990006", "005930"], "upl_pric": ["+13000", "+78000"], "base_pric": [base, "60000"]})
        snap = pd.DataFrame({"code": ["990006", "005930"], "open": [9800.0, 60000.0], "high": [10500.0, 61000.0], "low": [9700.0, 59000.0],
                             "close": [10000.0, 60000.0], "volume": [3e6, 100.0], "amount": [3e10, 1e6], "dt": ["20261026"] * 2})
        test_codex.Q.snapshot = lambda caller, cs, **k: (raw, snap, {"returned": 2, "codes": 2, "seconds": 1.0})
        res.ctx.update(amt20={"990006": 3.9e9, "005930": 9e9}, filled={"990006": {"last_close": 10000.0, "dates": ["2026-10-23"]}})
        top = pd.DataFrame({"code": ["990006"], "score": [0.9], "raw_close": [10000.0]})
        out = {"filter": {"filter_ok": True, "prev_kospi_close": 2700.0, "prev_kospi_ma120": 2500.0}, "eligible": pd.DataFrame({"code": ["990006"], "score": [0.9]}),
               "market": {}, "errors": [],
               "ledgers": {n: {"top3": top, "busy": [], "skipped_busy": [], "ledger_status": []} for n in ("strategy", "full")}}
        saved = E.run_day
        try:
            E.run_day = lambda *a, **k: out
            res.snapshot_and_plan()
        finally:
            E.run_day = saved
        self.clk.at("15:22:00")
        res.buy()
        return res, sends(self.fk)

    def test_filled_stock_base_mismatch_no_day_block(self):
        """11Q A: 공식 값으로 채운 종목도 기준가로 그날 계수 (기준가 9,800 ≠ 채운 종가 10,000 -> 이벤트 계수) -> 11P 보완 filled_base 하루 차단 없음"""
        res, sent = self.snap_buy("9800")
        self.assertNotIn("filled_base", res.day.s.get("buy_block") or [])
        self.assertEqual([s[2]["stk_cd"] for s in sent], ["990006"])

    def test_filled_base_match_buys(self):
        res, sent = self.snap_buy("10000")
        self.assertFalse(res.day.s.get("buy_block"))
        self.assertEqual([s[2]["stk_cd"] for s in sent], ["990006"])            # 정상 매수


if __name__ == "__main__":
    unittest.main()
