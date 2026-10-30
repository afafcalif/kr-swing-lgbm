"""
11R 시험: B 계수 바뀐 종목 이력 다시 맞추기 (daily), 15:30 판단의 그날 계수 (resident.judge), C 보유 종목 그날 이벤트 손절가 조정 (손절 감시 전).
실제 자료: testdata/rebase_real.json (메인 PC data/ 일봉 파일 2026-10-16 까지 + KRX 공식 시세 10-19 ~ 10-23 전일대비).
가짜 키움 일봉 서버·가짜 공식 시세·가짜 시계, 실제 키움 호출·data·logs 없음.

  python -m unittest backtest_swing/live/test_rebase.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"
import json
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import Base, bal_row, jl  # noqa: E402
from test_fetchfill import fake_caller  # noqa: E402
from test_orders import sends  # noqa: E402
import test_basefactor as TB  # noqa: E402
import common as C  # noqa: E402
import engine as E  # noqa: E402
import positions as P  # noqa: E402
import quotes as Q  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = json.load(open(os.path.join(HERE, "testdata", "rebase_real.json"), encoding="utf-8"))
ADJ_COLS = ["timestamp", "open", "high", "low", "close", "volume", "amount"]
RAW_COLS = ADJ_COLS + ["factor"]
NEW_DAYS = [pd.Timestamp(x) for x in ("2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23")]
# KRX 기준가(종가 - 전일대비) / 전날 종가 로 본 그날 이벤트 (2026-10-19 ~ 10-23, 0.5% 넘음) — 시험 기대값 (KRX 원자료를 직접 본 값)
EVENTS = {"368600": [("2026-10-22", 5.0)], "057540": [("2026-10-20", 5.0)], "093240": [("2026-10-20", 5.0)],
          "009140": [("2026-10-20", 0.5)], "117670": [("2026-10-21", 0.5)], "254120": [("2026-10-21", 5.0)],
          "099520": [("2026-10-21", 1105.0 / 1284.0)]}
REAL_NEW = {"115180": 20500.0 / 21500.0, "023150": 2275.0 / 4545.0}   # 10-19 이벤트 뒤에 받은 기본 파일 (ext = 키움 새 이력, raw = 이벤트 전 factor 1)
CODES = sorted(EVENTS) + sorted(REAL_NEW)


def stock(code):
    s = FIX["stocks"][code]
    a = pd.DataFrame(s["adj"], columns=ADJ_COLS)
    r = pd.DataFrame(s["raw"], columns=RAW_COLS)
    a["timestamp"], r["timestamp"] = pd.to_datetime(a.timestamp), pd.to_datetime(r.timestamp)
    return a, r


def krx():
    o = pd.DataFrame(FIX["krx"], columns=["date", "code", "open", "high", "low", "close", "volume", "amount", "chg"])
    o["date"] = pd.to_datetime(o.date)
    return o.assign(name=o.code, market="KOSPI", list_shares=1e6)


def k_after(code, day):
    """그날 뒤(그날 포함 안 함)의 이벤트 비율 곱 = 키움 새 이력에서 그날 행에 곱해진 값"""
    return float(np.prod([k for d, k in EVENTS.get(code, []) if pd.Timestamp(d) > pd.Timestamp(day)] or [1.0]))


def r0(x):
    return np.floor(np.asarray(x, float) + 0.5)


def kiwoom_new(code, off):
    """가짜 키움 일봉 (아침 갱신 때 받는 새 이력): 기본 파일 날짜는 파일 x 그 뒤 이벤트 곱(원 반올림, 거래량 / 곱), 새 날짜는 공식 원주가 x 그 뒤 이벤트 곱.
    115180·023150 은 기본 파일이 이미 새 기준 (곱 1). 원주가(upd 0) = 파일 원주가 + 공식 원주가"""
    a, r = stock(code)
    kf = k_after(code, a.timestamp.max())
    na = a.copy()
    for c in ("open", "high", "low", "close"):
        na[c] = r0(a[c] * kf)
    na["volume"] = r0(a.volume / kf)
    o = off[off.code == code].sort_values("date")
    new_raw = []
    for x in o.itertuples():
        op = x.open if x.open > 0 else x.close
        new_raw.append({"timestamp": x.date, "open": op, "high": x.high if x.high > 0 else x.close, "low": x.low if x.low > 0 else x.close,
                        "close": x.close, "volume": x.volume, "amount": float(r0(x.amount / 1e6)) * 1e6})
    nr = pd.DataFrame(new_raw)
    nadj = nr.copy()
    for i, d in enumerate(nr.timestamp):
        k = k_after(code, d)
        for c in ("open", "high", "low", "close"):
            nadj.loc[i, c] = float(r0(nr.loc[i, c] * k))
        nadj.loc[i, "volume"] = float(r0(nr.loc[i, "volume"] / k))
    adj = pd.concat([na, nadj], ignore_index=True)
    raw = pd.concat([r[ADJ_COLS], nr], ignore_index=True)
    return adj, raw


class FakeChart:
    """ka10081·ka20006 가짜 서버 (실제 자료로 만든 키움 새 이력). tweak[(code, upd)] = 행 DataFrame 을 바꾸는 함수"""

    def __init__(self, off, days):
        self.off, self.days, self.tweak, self.calls = off, days, {}, []

    def request(self, tr_code, json=None, **kw):
        self.calls.append((tr_code, dict(json or {})))
        if tr_code == "ka20006":
            rows = [{"dt": f"{d:%Y%m%d}", "open_pric": "100000", "high_pric": "100000", "low_pric": "100000", "cur_prc": "100000"} for d in self.days]
            return self.resp({"return_code": 0, "inds_dt_pole_qry": rows[::-1]})
        code, upd = json["stk_cd"], json["upd_stkpc_tp"]
        adj, raw = kiwoom_new(code, self.off)
        df = adj if upd == "1" else raw
        if (code, upd) in self.tweak:
            df = self.tweak[(code, upd)](df.copy())
        rows = [{"dt": f"{x.timestamp:%Y%m%d}", "open_pric": f"{x.open:.0f}", "high_pric": f"{x.high:.0f}", "low_pric": f"{x.low:.0f}",
                 "cur_prc": f"{x.close:.0f}", "trde_qty": f"{x.volume:.0f}", "trde_prica": f"{x.amount / 1e6:.0f}"} for x in df.itertuples()]
        return self.resp({"return_code": 0, "stk_dt_pole_chart_qry": rows[::-1]})

    @staticmethod
    def resp(j):
        return SimpleNamespace(json=lambda: j, headers={})


# ---------------------------------------------------------------- B: 실제 이력 대조 (함수)

class RealHistory(unittest.TestCase):
    def test_rebased_old_history_equals_kiwoom_new(self):
        """115180 (10-19 권리락 k 0.9535)·023150 (10-19 k 0.50055): 이벤트 전 이력(원주가, factor 1) x KRX 기준가비 = 키움이 다시 준 새 이력 (원 단위 1원 안)"""
        import daily
        for code, k in REAL_NEW.items():
            new, raw = stock(code)
            self.assertTrue(np.allclose(raw.factor / k, 1.0, atol=5e-4))                      # 이벤트 전 factor 1 = 원주가가 곧 옛 수정주가
            old = raw[ADJ_COLS].copy()
            ra, rr = daily.rebase_rows(old, raw.assign(factor=1.0), k)
            m = ra.merge(new, on="timestamp", suffixes=("", "_kw"))
            self.assertEqual(len(m), len(new))
            for c in ("open", "high", "low", "close"):
                self.assertLessEqual(float((m[c] - m[c + "_kw"]).abs().max()), 1.0, (code, c))   # 키움 정수 반올림과 1원 안
                self.assertLess(float(((m[c] - m[c + "_kw"]).abs() / m[c + "_kw"]).max()), 0.0017)
            self.assertLess(float(((m.volume - m.volume_kw).abs() / m.volume_kw).max()), 0.001)
            self.assertTrue(np.allclose(rr.factor, raw.factor, rtol=5e-4))                      # factor x K = 키움 새 계수
            # 아침 대조: 키움 시가(새) / 파일 시가(옛) 5일 비율이 일정하고 KRX 기준가비와 0.2% 안
            chk = new.tail(5)[["timestamp", "open"]].merge(old.rename(columns={"open": "open_file"})[["timestamp", "open_file"]], on="timestamp")
            kk, why = daily.kiwoom_ratio(chk)
            self.assertIsNone(why)
            kx, ev, why_k = daily.krx_base_ratio(krx()[lambda o: o.code == code], new.timestamp.max(), float(raw.close.iat[-1]), NEW_DAYS[-1])
            self.assertEqual((len(ev), why_k), (1, None))
            self.assertAlmostEqual(kx, k, places=9)
            self.assertEqual(daily.rebase_decide(kk, kx, ev, why_k), (kx, None))

    def test_kiwoom_ratio_rules(self):
        import daily
        f = pd.DataFrame({"open_file": [1001.0, 999.0, 1003.0, 997.0, 1000.0]})
        ok = f.assign(open=r0(f.open_file * 0.5))                                                # 원 반올림 (0.1% 차이) = 일정
        self.assertAlmostEqual(daily.kiwoom_ratio(ok)[0], 0.5, places=2)
        bad = ok.copy()
        bad.loc[2, "open"] += 5                                                                  # 하루만 1% 다름 = 일정하지 않음
        k, why = daily.kiwoom_ratio(bad)
        self.assertIsNone(k)
        self.assertIn("일정하지 않음", why)
        halt = pd.DataFrame({"open_file": [0.0, 956.0], "open": [10.0, 4780.0]})                # 파일 시가 0 인데 키움은 아님
        self.assertIsNone(daily.kiwoom_ratio(halt)[0])

    def test_krx_ratio_small_changes_and_product(self):
        import daily
        o = pd.DataFrame({"date": pd.to_datetime(["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]),
                          "close": [1000.0, 520.0, 510.0, 2600.0], "chg": [-2.0, 20.0, 10.0, 50.0]})
        # 10-19 기준가 1002 / 1000 (0.2%, 스팩류 = 안 곱함), 10-20 500 / 1000 = 0.5, 10-21 500 / 520 = 0.96 (곱함), 10-22 2550 / 510 = 5
        k, ev, why = daily.krx_base_ratio(o, "2026-10-16", 1000.0, "2026-10-22")
        self.assertIsNone(why)
        self.assertEqual([d for d, _ in ev], ["2026-10-20", "2026-10-21", "2026-10-22"])
        self.assertAlmostEqual(k, 0.5 * (500 / 520) * 5.0, places=9)
        k2, _, why2 = daily.krx_base_ratio(o.assign(chg=[0.0, np.nan, 0.0, 0.0]), "2026-10-16", 1000.0, "2026-10-22")
        self.assertIn("전일대비 없음", why2)
        self.assertIn("KRX 기준가비 확인 못 함", daily.rebase_decide(0.5, k2, [], why2)[1])
        self.assertIn("≠ KRX", daily.rebase_decide(0.5, 0.502, [], None)[1])                  # 0.4% 차이 = 안 맞음
        self.assertEqual(daily.rebase_decide(0.5005, 0.5, [], None), (0.5, None))               # 0.1% = 맞음, KRX 값으로


# ---------------------------------------------------------------- B: 아침 갱신 (가짜 키움 + 실제 파일·KRX)

class Env(unittest.TestCase):
    def setUp(self):
        import daily
        from kiwoom import krx_official
        self.daily, self.krx = daily, krx_official
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        sd, s4 = daily.sd, daily.s4
        self.saved = (daily.APPEND_DIR, daily.Caller, daily.RETRY_WAIT, daily.RETRY_GAP, sd.EXT_DIR, sd.MERGED_DIR, sd.RAW_DIR, sd.KOSPI, s4.KOSDAQ,
                      sd.universe_codes, krx_official.load_range, krx_official.fetch_day, krx_official.CACHE_DIR, C.time)
        self.addCleanup(self.restore)
        ext, rawd, self.ad, self.cache = (os.path.join(self.tmp, n) for n in ("ext", "raw", "append", "cache"))
        for p in (ext, rawd, self.ad, self.cache):
            os.makedirs(p)
        for c in CODES:
            a, r = stock(c)
            a.to_parquet(os.path.join(ext, f"{c}.parquet"), index=False)
            r.to_parquet(os.path.join(rawd, f"{c}.parquet"), index=False)
        file_days = sorted(set(stock("368600")[0].timestamp))
        for n in ("kospi", "kosdaq"):
            pd.DataFrame({"timestamp": file_days, "open": 1000.0, "high": 1000.0, "low": 1000.0, "close": 1000.0}).to_parquet(
                os.path.join(self.tmp, f"{n}.parquet"), index=False)
        daily.APPEND_DIR, krx_official.CACHE_DIR = self.ad, self.cache
        daily.RETRY_WAIT = daily.RETRY_GAP = 0
        sd.EXT_DIR = sd.MERGED_DIR = ext
        sd.RAW_DIR = rawd
        sd.KOSPI, s4.KOSDAQ = os.path.join(self.tmp, "kospi.parquet"), os.path.join(self.tmp, "kosdaq.parquet")
        sd.universe_codes = lambda: list(CODES)
        self.off = krx()
        krx_official.load_range = lambda a, b: self.off[(self.off.date >= pd.Timestamp(a)) & (self.off.date <= pd.Timestamp(b))].reset_index(drop=True)
        krx_official.fetch_day = lambda d: (_ for _ in ()).throw(AssertionError("시험에서 실제 공식 시세 요청"))
        for d in NEW_DAYS:
            pd.DataFrame({"x": [1]}).to_parquet(os.path.join(self.cache, f"{d:%Y%m%d}.parquet"))
        C.time = SimpleNamespace(time=time.time, sleep=lambda s: None)
        self.fk = FakeChart(krx(), file_days[-30:] + NEW_DAYS)
        self.caller = fake_caller(self.fk)
        daily.Caller = lambda *a, **k: self.caller

    def restore(self):
        daily, sd, s4 = self.daily, self.daily.sd, self.daily.s4
        (daily.APPEND_DIR, daily.Caller, daily.RETRY_WAIT, daily.RETRY_GAP, sd.EXT_DIR, sd.MERGED_DIR, sd.RAW_DIR, sd.KOSPI, s4.KOSDAQ,
         sd.universe_codes, self.krx.load_range, self.krx.fetch_day, self.krx.CACHE_DIR, C.time) = self.saved

    def u(self):
        return jl(C.data_file("update.json", self.ad))


class Update(Env):
    def test_rebased_history_equals_kiwoom_new(self):
        """368600 10-22 병합(k 5)·10-20·10-21 이벤트 종목: 아침 대조 비율 = KRX 기준가비 -> rebased, 메모리 적재 이력 = 키움 새 이력, 특징값 같음"""
        self.daily.update()
        u = self.u()
        self.assertEqual(u["adj_changed"], [])                                                  # 예전: 7종목 모두 adj_changed (영구 제외)
        self.assertEqual(sorted(u["rebased"]), sorted(EVENTS))
        for c, ev in EVENTS.items():
            self.assertAlmostEqual(u["rebased"][c]["k"], ev[0][1], places=9)
            self.assertEqual([d for d, _ in u["rebased"][c]["events"]], [ev[0][0]])
        self.assertEqual(self.daily.excluded_codes()["adj_changed"], [])
        st = self.daily.Store(CODES)
        self.assertEqual(st.excluded, [])
        self.assertEqual(sorted(st.rebased), sorted(EVENTS))
        lm = E.make_load_mem(lambda c: frames[c])
        for c in CODES:
            ka, kr = kiwoom_new(c, krx())
            a, r = st.adj[c], st.raw[c]
            m = a.merge(ka, on="timestamp", suffixes=("", "_kw"))
            self.assertEqual(len(m), len(ka), c)
            for col in ("open", "volume"):                                                       # 고가·저가·종가는 공식 정규장 값으로 덮어씀 (같은 값)
                self.assertTrue(np.allclose(m[col], m[col + "_kw"], rtol=0, atol=0.5), (c, col))
            for col in ("high", "low", "close"):
                self.assertTrue(np.allclose(m[col], m[col + "_kw"], rtol=0, atol=1.0), (c, col))
            mr = r.merge(kr, on="timestamp", suffixes=("", "_kw"))
            self.assertTrue(np.allclose(mr.factor, m.close / mr.close, rtol=1e-3), c)            # factor = 새 수정주가 / 원주가
        for c in ("368600", "117670"):                                                           # 300일 이력: 동결 load_stock 사본 특징값 = 키움 새 이력 기준
            ka, kr = kiwoom_new(c, krx())
            frames = {c: (st.adj[c], st.raw[c])}
            mine = lm(c, until=NEW_DAYS[-1])
            kr = kr.merge(ka[["timestamp", "close"]].rename(columns={"close": "ac"}), on="timestamp")
            frames = {c: (ka, kr.assign(factor=kr.ac / kr.close).drop(columns=["ac"]))}
            truth = lm(c, until=NEW_DAYS[-1])
            for col in ("ma20", "ma60", "atr20", "raw_close"):
                x, y = mine[col].to_numpy(float)[-40:], truth[col].to_numpy(float)[-40:]
                self.assertTrue(np.allclose(x, y, rtol=2e-3, equal_nan=True), (c, col))
            self.assertEqual(mine.eligible.tolist(), truth.eligible.tolist())

    def test_not_constant_or_krx_mismatch_excluded(self):
        def bump(df):
            df.loc[df.timestamp == pd.Timestamp("2026-10-14"), "open"] *= 1.03                   # 5일 중 하루만 3% 다름
            return df
        self.fk.tweak[("057540", "1")] = bump
        self.off.loc[(self.off.code == "254120") & (self.off.date == pd.Timestamp("2026-10-21")), "chg"] = -330.0   # KRX 기준가 4350 (4.78배 ≠ 키움 5배)
        self.daily.update()
        u = self.u()
        self.assertEqual(u["adj_changed"], ["057540", "254120"])
        self.assertIn("일정하지 않음", u["adj_changed_why"]["057540"])
        self.assertIn("≠ KRX 기준가비", u["adj_changed_why"]["254120"])
        self.assertNotIn("057540", u["rebased"])
        st = self.daily.Store(CODES)
        self.assertEqual(st.excluded, ["057540", "254120"])                                     # 지금처럼 그 종목만 제외
        self.assertIn("368600", st.adj)

    def test_excluded_stock_returns_next_update(self):
        """이미 빠져 있는 종목 (첫 갱신에서 KRX 와 안 맞음 = adj_changed) -> 다음 갱신에서 맞으면 돌아옴"""
        self.off.loc[(self.off.code == "368600") & (self.off.date == pd.Timestamp("2026-10-22")), "chg"] = np.nan
        self.daily.update()
        self.assertIn("368600", self.daily.excluded_codes()["adj_changed"])
        self.assertIn("전일대비 없음", self.u()["adj_changed_why"]["368600"])
        self.assertNotIn("368600", self.daily.Store(["368600"]).adj)
        self.off = krx()
        self.daily.update()
        self.assertNotIn("368600", self.daily.excluded_codes()["adj_changed"])
        st = self.daily.Store(["368600"])
        self.assertEqual(float(st.adj["368600"].set_index("timestamp").close[pd.Timestamp("2026-10-16")]), 4780.0)   # 956 x 5
        self.assertEqual(float(st.raw["368600"].set_index("timestamp").factor[pd.Timestamp("2026-10-16")]), 5.0)

    def test_official_missing_waits_then_reapply(self):
        """07:00 에 전날(10-23) 공식 시세 없음 -> rebase_wait (그날 제외), 08:0x 재반영 때 KRX 로 확인 -> rebased"""
        full = self.off
        self.off = full[full.date < NEW_DAYS[-1]].reset_index(drop=True)
        os.remove(os.path.join(self.cache, "20261023.parquet"))
        self.daily.update()
        u = self.u()
        self.assertEqual(u["official_missing_dates"], ["2026-10-23"])
        self.assertEqual(sorted(u["rebase_wait"]), sorted(EVENTS))
        self.assertEqual(self.daily.excluded_codes()["rebase_wait"], sorted(EVENTS))
        self.off = full
        pd.DataFrame({"x": [1]}).to_parquet(os.path.join(self.cache, "20261023.parquet"))
        r = self.daily.reapply_official()
        self.assertTrue(r["ok"])
        self.assertEqual(r["rebased"], sorted(EVENTS))
        u = self.u()
        self.assertEqual((u["rebase_wait"], self.daily.excluded_codes()["rebase_wait"]), ({}, []))
        st = self.daily.Store(CODES)
        self.assertEqual(st.excluded, [])
        self.assertEqual(st.adj["368600"].timestamp.max(), NEW_DAYS[-1])

    def test_monthly_sources_use_rebased_history(self):
        """월별 재학습 입력도 같은 경로 (Store): 계수 바뀐 종목이 기본 파일만(10-16 에서 멈춤)이 아니라 다시 맞춘 이력 + 추가분"""
        import monthly as MO
        self.daily.update()
        self.addCleanup(setattr, MO, "APPEND_DIR", MO.APPEND_DIR)
        MO.APPEND_DIR = self.ad                                                                  # 저장소 data/ 를 읽지 않게
        out = os.path.join(self.tmp, "mo")
        last, info = MO.sources(out)
        self.assertEqual(info["base_only"], [])
        a = pd.read_parquet(os.path.join(out, "ext", "368600.parquet"))
        a["timestamp"] = pd.to_datetime(a.timestamp)
        self.assertEqual(a.timestamp.max(), NEW_DAYS[-1])
        self.assertEqual(float(a.set_index("timestamp").close[pd.Timestamp("2026-10-16")]), 4780.0)


# ---------------------------------------------------------------- 15:30 판단의 그날 계수 + C 보유 종목 이벤트

HELD = {"qty": 28, "trade_id": "990006-2026-10-20-1", "entry_date": "2026-10-20", "stop_price": 40000.0, "stop_basis": "진입일", "kind": "본래",
        "judged_on": "2026-10-23", "close_1530": 45000.0}


def pos_with(**kw):
    return {"held": {"990006": {**HELD, **kw}}, "sell_duty": {}, "next_open_sells": {}, "trade_seq": 1, "duty_format": P.DUTY_FORMAT}


class Held(Base):
    """10-26 (전날 10-23 종가 45,000). 무상증자 권리락 기준가 30,000 (k 0.6667, 수량 그대로)"""

    def setUp(self):
        super().setUp()
        self.saved_rd = P.raw_daily
        P.raw_daily = lambda code: pd.DataFrame({"timestamp": pd.to_datetime(["2026-10-22", "2026-10-23"]), "open": [44000.0, 45000.0],
                                                 "high": [46000.0, 46000.0], "low": [43000.0, 44000.0], "close": [44500.0, 45000.0]})
        self.addCleanup(setattr, P, "raw_daily", self.saved_rd)
        self.q = {"price": 30500.0, "base": "30000", "dt": "20261026"}
        self.asked = []

        def snap(caller, codes, **k):
            self.asked.append(list(codes))
            raw = pd.DataFrame({"stk_cd": list(codes), "base_pric": self.q["base"], "dt": self.q["dt"]})
            return raw, pd.DataFrame({"code": list(codes), "close": self.q["price"], "dt": self.q["dt"], "cntr_tm": "090030"}), {}
        Q.snapshot = snap

    def go(self, hms, **kw):
        P.save(self.R.POSITIONS, pos_with(**kw))
        self.fk.balance_rows = [bal_row("990006", 28)]
        return self.started(hms, live=False)

    def held(self):
        return P.load(self.R.POSITIONS)[0]["held"]["990006"]

    def test_ex_rights_no_false_stop_then_real_drop_stops(self):
        res = self.go("09:00:00")
        res.stop_minute()                                                                        # 30,500 < 옛 손절가 40,000 (예전이면 손절)
        self.assertEqual(sends(self.fk, "kt10001"), [])
        h = self.held()
        self.assertAlmostEqual(h["stop_price"], 40000.0 * 30000 / 45000, places=6)              # 26,667
        self.assertEqual((h["stop_adjust"][0]["stop_before"], round(h["stop_adjust"][0]["k"], 4)), (40000.0, 0.6667))
        self.assertEqual(h["qty"], 28)                                                           # 수량 그대로 (수동 확인 아님)
        self.assertTrue(any(a.startswith("[이벤트 손절가 조정] 990006") for a in self.alerts))
        self.q["price"] = 26000.0                                                                # 조정된 손절가 아래로 진짜 하락
        self.clk.at("09:01:00")
        res.stop_minute()
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["990006"])
        self.assertAlmostEqual(self.held()["stop_price"], 26666.666667, places=3)               # 하루 한 번만 조정

    def test_premarket_adjusts_before_0900(self):
        res = self.go("08:30:00")
        res.trend_sell()
        self.assertAlmostEqual(self.held()["stop_price"], 26666.666667, places=3)
        self.assertEqual(self.held()["base_check"]["source"].split(" (")[0], "08:30 장 전 시세")
        self.clk.at("09:00:00")
        res.stop_minute()
        self.assertEqual(sends(self.fk, "kt10001"), [])

    def test_base_unknown_pauses_and_alerts(self):
        self.q.update(base="0", price=20000.0)                                                  # 기준가 없음 + 옛 손절가 아래
        res = self.go("09:00:00")
        for m in range(5):
            self.clk.at(f"09:0{m}:00")
            r = res.stop_minute()
            self.assertEqual(r["base_paused"], ["990006"])
        self.assertEqual(sends(self.fk, "kt10001"), [])                                         # 잘못 파는 것보다 멈춤
        self.assertEqual(self.held()["stop_price"], 40000.0)
        warn = [a for a in self.alerts if a.startswith("[기준가 확인 못 함] 990006")]
        self.assertEqual(len(warn), 1)                                                          # 3분 넘으면 한 번
        self.q.update(base="45000", price=39000.0)                                              # 기준가 확인됨 (평소 날) -> 감시 다시, 진짜 손절
        self.clk.at("09:05:00")
        res.stop_minute()
        self.assertTrue(any(a.startswith("[기준가 확인] 990006") for a in self.alerts))
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["990006"])

    def test_normal_day_stop_unchanged(self):
        self.q.update(base="45000", price=39000.0)
        res = self.go("09:00:00")
        res.stop_minute()
        h = self.held()
        self.assertEqual((h["stop_price"], h["base_check"]["adjusted"], h.get("stop_adjust")), (40000.0, False, None))
        self.assertEqual([b[2]["stk_cd"] for b in sends(self.fk, "kt10001")], ["990006"])
        self.assertFalse(any(a.startswith("[이벤트 손절가 조정]") for a in self.alerts))

    def test_qty_review_not_adjusted(self):
        """분할·병합 수량 변화(qty_review) 종목은 지금처럼 멈춤: 손절가를 바꾸지 않음 (바꾸면 수동 확인이 끝난 것으로 보임)"""
        rv = {"date": "2026-10-26", "stop": 40000.0, "file_qty": 28, "account_qty": 56}
        res = self.go("09:00:00", qty_review=rv)
        r = res.stop_minute()
        h = self.held()
        self.assertEqual((h["stop_price"], h.get("base_check")), (40000.0, None))
        self.assertTrue(P.in_review(h))
        self.assertEqual(sends(self.fk, "kt10001"), [])
        self.assertEqual(r["base_paused"], [])

    def test_official_restop_keeps_today_k(self):
        """오늘 이미 조정한 종목에 전날 기준 공식 손절가가 나중에 들어와도 x k (조정이 풀리지 않음)"""
        p = {**HELD}
        P.event_adjust(p, pd.Timestamp("2026-10-26"), 30000.0, 45000.0, "시험", "09:00:00")
        self.assertAlmostEqual(P.today_k(p, pd.Timestamp("2026-10-26")), 30000 / 45000)
        self.assertEqual(P.today_k(p, pd.Timestamp("2026-10-27")), 1.0)

    def test_judge_1530_uses_day_base(self):
        """15:30 1분봉 판단 (보유 종목 손절가·20일선): 그날 이벤트(368600 형 병합 k 5) 날 이력이 이벤트 전 기준이어도 키움 새 이력 기준과 같은 값"""
        import daily
        adj, raw = TB.history("2026-10-21", last_close=956.0, halt=0)
        sv = {k: v for k, v in TB.SV_368600.items()}
        res = self.go("15:40:00")
        res.holidays = {}
        saved = daily.Store
        try:
            daily.Store = lambda codes, use_append=True: SimpleNamespace(adj={"368600": adj}, raw={"368600": raw})
            judge = type(res).judge                                                              # 시험 공통 Base 의 가짜 judge 말고 실제
            new = judge(res, ["368600"], "2026-10-22", {"368600": sv}, {"368600": 4780.0})["368600"]
            old = judge(res, ["368600"], "2026-10-22", {"368600": sv})["368600"]
        finally:
            daily.Store = saved
        na, nr = TB.rebase(adj, raw, 5.0)
        t = E.make_load_mem(lambda c: E.today_frames(na, nr, pd.Timestamp("2026-10-22"), sv, 1.0, 1.0))("368600", until=pd.Timestamp("2026-10-22"))
        self.assertAlmostEqual(new["close"], 4140.0, places=6)
        self.assertAlmostEqual(new["stop"], float(t.close.iat[-1] - 1.7 * t.atr20.iat[-1]), places=6)   # 원주가 기준, 키움 새 이력과 같음
        self.assertEqual(new["trend"], bool(t.close.iat[-1] < t.ma20.iat[-1]))
        self.assertIn("k=5.0000", new["basis"])
        self.assertNotAlmostEqual(old["stop"], new["stop"], places=0)                           # 기준가 없이 = 오늘 5배로 뛴 것으로 (예전)

    def test_day_bases_snapshot_then_base_check(self):
        res = self.go("15:40:00")
        f = os.path.join(self.tmp, "snap.parquet")
        pd.DataFrame({"stk_cd": ["A990006"], "base_pric": ["30000"]}).to_parquet(f)
        res.day.set("live", {"snap_file": f})
        self.assertEqual(res.day_bases(["990006"]), {"990006": 30000.0})
        res.day.set("live", {})
        p = pos_with()
        P.event_adjust(p["held"]["990006"], res.day.date, 30000.0, 45000.0, "시험", "09:00:00")
        self.assertEqual(res.day_bases(["990006"], p), {"990006": 30000.0})                    # 스냅샷 없으면 오늘 확인한 기준가


class Approved(Base):
    def test_returned_stock_marked_ended(self):
        import daily
        res = self.started("07:30:00")
        res.ctx.update(excluded={"adj_changed": ["368600"], "fetch_error": []}, amt20={"000001": 1e9})
        res.check_exclusions()
        saved = daily.rebased_k
        try:
            daily.rebased_k = lambda u=None: {"368600": 5.0}
            res.ctx.update(excluded={"adj_changed": [], "fetch_error": []})
            res.check_exclusions()
        finally:
            daily.rebased_k = saved
        it = jl(self.R.APPROVED)["items"][0]
        self.assertEqual((it["code"], it["ended"]), ("368600", "2026-10-26"))
        self.assertIn("k=5.000000", it["ended_why"])


if __name__ == "__main__":
    unittest.main()
