"""
11Q 시험: A 그날 행 계수를 15:18 스냅샷 기준가로, E 당일 실패는 그 종목만 제외(5% 이상이면 차단)·다음 날 '넣었으면 상위 3' 확인,
F 아침 기준가 측정(조회만, 별도 스레드). 가짜 서버·가짜 시계, 실제 키움 호출·data·logs 없음.

  python -m unittest backtest_swing/live/test_basefactor.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"
import sys
import threading
import time
import unittest
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_codex  # noqa: E402
from test_codex import Base  # noqa: E402
from test_orders import sends  # noqa: E402
import common as C  # noqa: E402
import engine as E  # noqa: E402
import quotes as Q  # noqa: E402

PRICE_KEYS = {"close_adj", "factor", "factor_default", "vscale_default", "base_k", "base_stale", "code"}


class Conn:
    """작업 프로세스 _serve 의 연결 흉내 (계산 한 번)"""

    def __init__(self, msgs):
        self.msgs, self.sent = list(msgs), []

    def recv(self):
        if not self.msgs:
            raise EOFError
        return self.msgs.pop(0)

    def send(self, x):
        self.sent.append(x)


def worker(adj, raw, day, row, code="368600"):
    """engine._serve 를 그대로 (메모리 사본 load_stock): 반환 (그 종목 계산 결과 dict, 오류 목록)"""
    store = SimpleNamespace(adj={code: adj}, raw={code: raw}, excluded=[])
    cur = {}
    conn = Conn([("calc", (pd.Timestamp(day), {code: row}, {}))])
    E._serve(conn, [code], store, cur, E.make_load_mem(lambda c: cur[c]))
    done = [x for x in conn.sent if x[0] == "done"][0]
    return (done[1][0] if done[1] else None), done[3]


def history(last_day, n=320, last_close=956.0, halt=4, seed=7):
    """원주가 일봉 (이벤트 전 기준, factor 1): 마지막 halt 일은 거래정지(거래량 0, 가격 = 마지막 종가) — 368600 10-15 ~ 10-21 과 같은 모양"""
    rng = np.random.default_rng(seed)
    ts = pd.bdate_range(end=last_day, periods=n)
    c = last_close * np.exp(np.cumsum(rng.normal(0, 0.02, n))[::-1] * -1)
    c = np.round(c / c[-1 - halt] * last_close)
    o = np.round(c * (1 + rng.normal(0, 0.005, n)))
    h = np.maximum(o, c) + np.round(c * 0.01)
    lo = np.minimum(o, c) - np.round(c * 0.01)
    v = np.round(rng.uniform(5e4, 2e5, n))
    for i in range(n - halt, n):
        o[i] = h[i] = lo[i] = c[i] = last_close
        v[i] = 0.0
    raw = pd.DataFrame({"timestamp": ts, "open": o, "high": h, "low": lo, "close": c, "volume": v, "amount": np.round(c * v), "factor": 1.0})
    return raw.drop(columns=["factor"]), raw


def rebase(adj, raw, k):
    """키움이 그날 이벤트(기준가 / 전날 종가 = k)를 반영한 새 기준 이력: 수정주가 x k, 거래량 / k, factor x k (반올림 없는 정확한 값)"""
    a = adj.copy()
    for c in ("open", "high", "low", "close"):
        a[c] = a[c] * k
    a["volume"] = a.volume / k
    return a, raw.assign(factor=raw.factor * k)


def feats(rec):
    return {k: v for k, v in rec.items() if k not in PRICE_KEYS}


def same(a, b, rtol=1e-6):
    bad = []
    for k in a:
        x, y = a[k], b[k]
        if isinstance(x, (bool, np.bool_)) or isinstance(y, (bool, np.bool_)):
            ok = bool(x) == bool(y)
        else:
            ok = (np.isnan(x) and np.isnan(y)) or np.isclose(x, y, rtol=rtol, atol=1e-9)
        if not ok:
            bad.append((k, x, y))
    return bad


# 368600 (2026-10-22, 5:1 병합 뒤 거래 재개): 10-15 ~ 10-21 거래정지 종가 956, 10-22 기준가 4,780 (= 4140 + 640, KRX 전일대비), 그날 시세
SV_368600 = {"open": 4790.0, "high": 5120.0, "low": 3950.0, "close": 4140.0, "volume": 219042.0, "amount": 950465597.0}


class BaseFactor(unittest.TestCase):
    def check_event(self, k, last_close, sv, base, day="2026-10-22", prev="2026-10-21"):
        adj, raw = history(prev, last_close=last_close)
        row = ({**sv, "base": base, "prev_day": prev}, None, None)
        old, e1 = worker(adj, raw, day, row)                                      # 저장 이력이 이벤트 전 기준 (아침 키움이 아직 안 바꿈)
        na, nr = rebase(adj, raw, k)
        new, e2 = worker(na, nr, day, row)                                        # 저장 이력이 이미 새 기준 (키움이 바꿈)
        truth, e3 = worker(na, nr, day, ({k_: v for k_, v in sv.items()}, 1.0, 1.0))   # 백테스트 기준: 키움 새 이력 + 그날 factor 1
        self.assertEqual(e1 + e2 + e3, [])
        self.assertEqual(same(feats(old), feats(truth)), [])                      # 특징값·대상 여부 = 키움 새 이력 기준과 같음
        self.assertEqual(same(feats(new), feats(truth)), [])
        self.assertAlmostEqual(old.get("base_k"), k, places=9)
        self.assertAlmostEqual(old["factor"], 1 / k, places=9)                    # 이벤트 전 기준이면 오늘 factor = 1/k
        self.assertAlmostEqual(new["factor"], 1.0, places=9)                      # 새 기준이면 1
        return adj, raw, row, truth

    def test_368600_reverse_split_1022(self):
        adj, raw, row, truth = self.check_event(5.0, 956.0, SV_368600, 4780.0)
        plain, _ = worker(adj, raw, "2026-10-22", ({k: v for k, v in SV_368600.items()}, None, None))   # 기준가를 안 쓰면 (11Q 전)
        self.assertNotEqual(same(feats(plain), feats(truth)), [])                # 오늘 행이 5배 뛴 것으로 계산됨

    def test_split_2_for_1(self):
        sv = {"open": 25100.0, "high": 25600.0, "low": 24800.0, "close": 25300.0, "volume": 3e5, "amount": 7.6e9}
        self.check_event(0.5, 50000.0, sv, 25000.0)

    def test_rights_issue(self):
        # 115180 (2026-10-19 권리락): 기준가 / 전날 종가 = 0.953488
        k = 0.9534883720930233
        sv = {"open": 20500.0, "high": 20900.0, "low": 19950.0, "close": 20450.0, "volume": 1.3e5, "amount": 2.6e9}
        self.check_event(k, 21500.0, sv, 21500.0 * k)

    def test_normal_day_identical(self):
        adj, raw = history("2026-10-23", halt=0, last_close=30000.0)
        sv = {"open": 30100.0, "high": 30500.0, "low": 29900.0, "close": 30300.0, "volume": 1e5, "amount": 3e9}
        base0, _ = worker(adj, raw, "2026-10-26", (dict(sv), None, None))        # 11Q 전과 같은 입력 (기준가 없음)
        for b in (30000.0, 30000.0 * 1.0014, 30000.0 * 0.996):                   # 기준가 = 전날 종가, 스팩류 0.14%, 0.4% (0.5% 미만)
            r, _ = worker(adj, raw, "2026-10-26", ({**sv, "base": b, "prev_day": "2026-10-23"}, None, None))
            self.assertEqual(r, base0)                                             # 결과 완전히 같음
        r, _ = worker(adj, raw, "2026-10-26", ({**sv, "base": 33000.0, "prev_day": "2026-10-22"}, None, None))   # 이력 마지막 행 != 직전 거래일
        self.assertEqual({k: v for k, v in r.items() if k != "base_stale"}, {k: v for k, v in base0.items() if k != "base_stale"})
        self.assertEqual(r.get("base_stale"), "2026-10-23")

    def test_default_factor_vscale_when_prev_halted(self):
        adj, raw = history("2026-10-21")
        na, nr = rebase(adj, raw, 5.0)
        self.assertEqual(E.default_factor(adj, raw, pd.Timestamp("2026-10-22"), base=4780.0, prev_day="2026-10-21"), (0.2, 5.0))
        f, vs = E.default_factor(na, nr, pd.Timestamp("2026-10-22"), base=4780.0, prev_day="2026-10-21")
        self.assertAlmostEqual(f, 1.0)
        self.assertAlmostEqual(vs, 1.0)                                          # 전날 거래량 0 이어도 새 기준이면 1

    def test_snap_rows_base(self):
        import run_day as R
        snap = pd.DataFrame({"code": ["000001", "000002", "000003"], "open": [100.0] * 3, "high": [101.0] * 3, "low": [99.0] * 3,
                             "close": [100.0] * 3, "volume": [10.0] * 3, "amount": [1e3] * 3})
        raw = pd.DataFrame({"stk_cd": ["000001", "000002_AL", "000003"], "upl_pric": ["+130"] * 3, "lst_pric": ["-70"] * 3,
                            "base_pric": ["100", "", "200"]})
        bad = R.snap_anomalies(snap, raw)
        self.assertEqual(sorted(bad), ["000002", "000003"])                      # 기준가 없음·상한 밖
        rows = R.snap_rows(snap, raw, pd.Timestamp("2026-10-26"))
        self.assertEqual(list(rows), ["000001"])
        self.assertEqual((rows["000001"][0]["base"], rows["000001"][0]["prev_day"]), (100.0, "2026-10-23"))
        old = R.snap_rows(snap, raw.drop(columns=["base_pric"]), pd.Timestamp("2026-10-26"))   # 기준가 열 없는 예전 기록 = 예전과 같음
        self.assertEqual(sorted(old), ["000001", "000002", "000003"])
        self.assertNotIn("base", old["000001"][0])


class Exclude(Base):
    """E: 15:18 당일 실패는 그 종목만 빠지고 매수, 5% 이상이면 시장 전체 문제로 차단"""
    CODES = ["990006"] + [f"9{i:05d}" for i in range(39)]                      # 40종목

    def plan_buy(self, missing):
        res = test_codex.Codex.snapshot_ready(self)
        got = [c for c in self.CODES if c not in missing]
        snap = pd.DataFrame({"code": got, "open": 33000.0, "high": 33500.0, "low": 32500.0, "close": 33000.0, "volume": 1e5, "amount": 3.3e9,
                             "dt": "20261026"})
        raw = pd.DataFrame({"stk_cd": got, "upl_pric": "+42900", "lst_pric": "-23100", "base_pric": "33000"})
        Q.snapshot = lambda caller, cs, **k: (raw, snap, {"returned": len(got), "codes": 40, "seconds": 1.0})
        C.sd.universe_codes = lambda: list(self.CODES)
        res.ctx.update(amt20={c: (9e9 if c == "900001" else 1e9) for c in self.CODES})
        top = pd.DataFrame({"code": ["990006"], "score": [0.9], "raw_close": [33000.0]})
        out = {"filter": {"filter_ok": True, "prev_kospi_close": 2700.0, "prev_kospi_ma120": 2500.0}, "eligible": pd.DataFrame({"code": ["990006"], "score": [0.9]}),
               "market": {}, "errors": [], "ledgers": {n: {"top3": top, "busy": [], "skipped_busy": [], "ledger_status": []} for n in ("strategy", "full")}}
        saved = E.run_day
        try:
            E.run_day = lambda *a, **k: out
            res.snapshot_and_plan()
        finally:
            E.run_day = saved
        self.clk.at("15:22:00")
        res.buy()
        return res, sends(self.fk)

    def test_one_candidate_missing_buys_others(self):
        res, sent = self.plan_buy(["900001"])                                    # 후보(90억) 하나 스냅샷에 없음 (1/40)
        self.assertEqual([s[2]["stk_cd"] for s in sent], ["990006"])
        self.assertFalse(res.day.s.get("buy_block"))
        self.assertEqual(list(res.day.s["excluded_candidates"]["스냅샷·계산 실패"]), ["900001"])

    def test_5pct_failure_blocks(self):
        res, sent = self.plan_buy(["900001", "900002"])                          # 2/40 = 5%
        self.assertEqual(sent, [])
        self.assertIn("market_fail:스냅샷·계산 실패", res.day.s["buy_block"])
        self.assertTrue(any(a.startswith("[스냅샷·계산 실패 5.0%]") for a in self.alerts))

    def test_next_morning_would_have_been_top3(self):
        """다음 날 아침: 전날 빠진 후보(스냅샷 없음 -> 전날 공식 일봉으로 대신)를 넣어 다시 계산 -> 상위 3 이었나 기록"""
        import daily
        prev = pd.Timestamp("2026-10-23")
        snapf = os.path.join(self.tmp, "snap.parquet")
        pd.DataFrame({"stk_cd": ["990006"], "p_code": ["990006"], "p_open": [1.0], "p_high": [1.0], "p_low": [1.0], "p_close": [1.0],
                      "p_volume": [1.0], "p_amount": [1.0]}).to_parquet(snapf)
        C.atomic_json(os.path.join(self.tmp, "2026-10-23.json"), {
            "excluded_candidates": {"스냅샷·계산 실패": {"900001": {"kind": "snapshot_missing", "amt20": 9e9}},
                                    "일봉 수집 실패": {"900002": {"kind": "fetch_error", "amt20": 9e9}}},
            "live": {"snap_file": snapf, "index_values": {"kospi": 1.0}, "ledger_before": {"strategy": [], "full": []},
                     "ledgers": {"strategy": [{"code": "990006"}], "full": [{"code": "990006"}]}}})
        res = self.started("07:20:00", live=False)
        pool = SimpleNamespace(amt20=lambda day: {"990006": 1e9, "900001": 9e9})   # 900002 는 오늘도 적재 안 됨
        a = pd.DataFrame({"timestamp": [prev], "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5], "volume": [50.0], "amount": [525.0]})
        st = SimpleNamespace(adj={"900001": a}, raw={"900001": a.assign(volume=100.0, factor=1.0)})
        seen = {}

        def run_day(pool_, day, rows, *a_, **k):
            seen.update(day=day, rows=rows)
            e = pd.DataFrame({"code": ["900001", "990006"], "score": [0.95, 0.9]})
            return {"eligible": e, "ledgers": {n: {"top3": e} for n in ("strategy", "full")}}
        saved = (daily.Store, E.run_day)
        try:
            daily.Store, E.run_day = (lambda codes, use_append=True: st), run_day
            r = res.prev_excluded_check(pd.Timestamp("2026-10-26"), pool, [], [], {})
        finally:
            daily.Store, E.run_day = saved
        self.assertEqual(seen["day"], prev)
        self.assertEqual(seen["rows"]["900001"], ({"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 100.0, "amount": 525.0}, 1.0, 0.5))
        c = r["candidates"]
        self.assertEqual((c["900001"]["rank"], c["900001"]["in_top3"]), (1, {"strategy": True, "full": True}))
        self.assertIn("공식 일봉", c["900001"]["row"])
        self.assertEqual(c["900002"]["row"], "오늘도 적재 안 됨 (계산 불가)")
        self.assertEqual(r["top3_live"], {"strategy": ["990006"], "full": ["990006"]})


class Probe(Base):
    """F: 아침 기준가 측정은 별도 스레드 (주 반복·손절 감시를 막지 않음), 늦으면·09:00 뒤에는 안 함, 다음 날 KRX 기준가와 대조"""

    def test_probe_runs_in_thread_and_does_not_block(self):
        gate, calls = threading.Event(), []
        real = Q.snapshot

        def snap(caller, codes, **k):
            if threading.current_thread().name == "base_probe":
                calls.append(len(codes))
                gate.wait(10)                                                     # 측정 조회가 오래 걸림
                raw = pd.DataFrame({"stk_cd": ["990006"], "base_pric": ["33000"], "cur_prc": ["33000"], "pred_pre": ["0"]})
                return raw, None, {"returned": 1, "codes": 1, "seconds": 9.0}
            return real(caller, codes, **k)
        Q.snapshot = snap
        C.sd.universe_codes = lambda: ["990006"]
        res = self.started("07:05:00", live=False)
        t0 = time.time()
        res.tick(self.clk())
        self.assertLess(time.time() - t0, 2.0)                                   # 주 반복은 기다리지 않음
        self.assertIn("07:05:00", res.day.s["base_probe"])
        self.clk.at("07:31:00")
        res.tick(self.clk())                                                      # 앞 측정이 아직 돌면 다음 시각은 시작 안 함 (한 번에 하나)
        self.assertEqual(list(res.day.s["base_probe"]), ["07:05:00"])
        self.clk.at("09:01:00")
        t0 = time.time()
        res.tick(self.clk())                                                      # 측정이 막혀 있어도 손절 감시는 돈다
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(res.last_stop_minute, "2026-10-26 09:01")
        gate.set()
        res.probe_thread.join(5)
        self.assertEqual(calls, [1])
        self.assertTrue(os.path.exists(os.path.join(self.R.LOG_DIR, "base_probe", "2026-10-26_0705.parquet")))
        res.tick(self.clk())                                                      # 09:00 뒤에는 남은 시각(07:30 ~ 08:20)도 안 함
        self.assertEqual(list(res.day.s["base_probe"]), ["07:05:00"])

    def test_compare_with_krx_next_day(self):
        import daily
        pdir = os.path.join(self.tmp, "probe")
        os.makedirs(pdir)
        pd.DataFrame({"stk_cd": ["368600", "005930", "000001"], "base_pric": ["956", "60000", ""]}).to_parquet(os.path.join(pdir, "2026-10-22_0705.parquet"))
        pd.DataFrame({"stk_cd": ["368600", "005930", "000001"], "base_pric": ["4780", "60000", "100"]}).to_parquet(os.path.join(pdir, "2026-10-22_0820.parquet"))
        off = pd.DataFrame({"date": pd.to_datetime(["2026-10-21"] * 3 + ["2026-10-22"] * 3), "code": ["368600", "005930", "000001"] * 2,
                            "close": [956.0, 60000.0, 100.0, 4140.0, 61000.0, 101.0], "chg": [0.0, 0.0, 0.0, -640.0, 1000.0, 1.0]})
        saved = daily.official_with_chg
        try:
            daily.official_with_chg = lambda a, b: off
            r = self.R.base_probe_compare(pdir, pd.Timestamp("2026-10-23"))
        finally:
            daily.official_with_chg = saved
        x = r["2026-10-22"]
        self.assertEqual(x["krx_events"], ["368600"])
        self.assertEqual((x["slots"]["0705"]["same_as_krx"], x["slots"]["0705"]["events_still_prev_close"]), (1, ["368600"]))   # 07:05 은 아직 전날 종가
        self.assertEqual((x["slots"]["0820"]["same_as_krx"], x["slots"]["0820"]["events_same_as_krx"]), (3, ["368600"]))
        self.assertTrue(os.path.exists(os.path.join(pdir, "2026-10-22_compare.json")))
        self.assertEqual(self.R.base_probe_compare(pdir, pd.Timestamp("2026-10-23")), {})                     # 한 번만


if __name__ == "__main__":
    unittest.main()
