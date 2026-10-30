"""
11S 보완 시험 (443a1ae Codex 확인 [P1] 2건): 1 장중 재수집 다시 적재는 이중 적재 (기존 적재를 계속 쓰고, 완료 시각이 15:18:00 전이고 스냅샷 전이면
한 번에 바꿔 끼움, 아니면 새 묶음을 닫고 버림, 바꿔 끼우는 동안 손절 감시 계속), 2 채택한 데이터 버전은 '사용 중'으로 정리에서 안 지움·15:18 고정은
그 이름 그대로·그날 버전이 없으면 동결 대조는 '대조 미완료'(불일치 차단 아님).
가짜 키움 일봉 서버·가짜 주문 서버·가짜 시계, 실제 키움 호출·data·logs 없음.

  python -m unittest backtest_swing/live/test_swap.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"
import shutil
import threading
import time
import unittest

import pandas as pd

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_intraday as TI  # noqa: E402
from test_codex import Base, jl  # noqa: E402
import common as C  # noqa: E402
import engine as E  # noqa: E402
import quotes as Q  # noqa: E402
import state as S  # noqa: E402

AMT = TI.AMT


class Pool:
    """가짜 계산 묶음: shutdown 이 불렸는지, gate 를 주면 닫기가 그만큼 막힘"""

    def __init__(self, name, gate=None):
        self.name, self.gate, self.closed = name, gate, False

    def shutdown(self):
        if self.gate is not None:
            self.gate.wait(10)
        self.closed = True


class Swap(Base):
    def setUp(self):
        super().setUp()
        self.base_tmp, self.kiwoom = self.tmp, self.fk
        self.addCleanup(shutil.rmtree, self.base_tmp, True)
        self.de = TI.DailyEnv("setUp")
        self.de.setUp()
        self.addCleanup(self.de.doCleanups)
        self.fk, self.chart, self.daily, self.ad = self.kiwoom, self.de.chart, self.de.daily, self.de.ad
        self.preps = []

    morning = TI.Intraday.morning
    prep_out = TI.Intraday.prep_out

    def resident(self):
        res = super().resident()
        res.request_prep = lambda reason: self.preps.append(reason)
        return res

    def adopt(self, res, pool, reason="하루 시작", finished=None):
        out = self.prep_out()
        out["pool"] = pool
        if finished:
            out["finished"] = finished
        res.prep_reason, res.prep_thread, res.prep_result = reason, object(), out
        res.apply_prep()

    def settle_dispose(self, res):
        for t in res.disposing:
            t.join(10)

    def refetched(self, res, hms):
        """장중 재수집이 새 버전을 냄 -> 다시 적재 요청, 준비 스레드가 도는 중 (결과 아직 없음)"""
        self.clk.at(hms)
        res.apply_refetch("15:00:00" if hms >= "15:00:00" else "10:00:00", {"published": "v", "got": ["000007"], "codes": ["000007"]})
        self.assertEqual(len(self.preps), 1)
        res.prep_reason, res.prep_thread, res.prep_result = self.preps[-1], object(), None

    def snapshot(self, res, hms):
        """15:18:30 스냅샷 단계 (run_step): 점수 계산에 쓴 묶음을 돌려줌"""
        self.clk.at(hms)
        codes = list(AMT)
        snap = pd.DataFrame({"code": codes, "open": 3500.0, "high": 3600.0, "low": 3400.0, "close": 3500.0, "volume": 1e5, "amount": 3.5e8,
                             "dt": "20261026"})
        raw = pd.DataFrame({"stk_cd": codes, "upl_pric": "+4550", "lst_pric": "-2450", "base_pric": "3500"})
        t3 = pd.DataFrame({"code": ["000001"], "score": [0.9], "raw_close": [3500.0]})
        out = {"filter": {"filter_ok": True, "prev_kospi_close": 2700.0, "prev_kospi_ma120": 2500.0}, "eligible": t3[["code", "score"]],
               "market": {}, "errors": [], "ledgers": {n: {"top3": t3, "busy": [], "skipped_busy": [], "ledger_status": []} for n in ("strategy", "full")}}
        used = []
        saved = (Q.snapshot, Q.index_now, C.sd.universe_codes, E.run_day)
        try:
            Q.snapshot = lambda caller, cs, **k: (raw, snap, {"returned": len(codes), "codes": len(codes), "seconds": 1.0})
            Q.index_now = lambda caller: ({"kospi": 1.0, "kosdaq": 1.0}, None)
            C.sd.universe_codes = lambda: list(codes)
            E.run_day = lambda pool, *a, **k: used.append(pool) or out
            S.run_step(res.day, "snapshot", res.snapshot_and_plan)
        finally:
            Q.snapshot, Q.index_now, C.sd.universe_codes, E.run_day = saved
        return used

    # ------------------------------------------------------------ 1 이중 적재

    def test_reload_from_1512_keeps_old_for_1518_snapshot(self):
        """15:12 시작한 재적재가 15:22 에 끝남: 15:18:30 스냅샷은 기존 적재로 성공, 끝난 새 묶음은 닫고 버림 (차단 없음)"""
        res = self.morning("07:30:00")
        old, new = Pool("old"), Pool("new")
        self.adopt(res, old)
        self.refetched(res, "15:12:00")
        self.assertTrue(res.prep_ok)                                                            # 재적재 중에도 기존 적재 사용 가능
        self.assertEqual(self.snapshot(res, "15:18:30"), [old])
        self.assertTrue(res.day.done("snapshot"))
        self.clk.at("15:22:00")
        self.adopt(res, new, self.preps[-1], finished="15:22:00")
        self.settle_dispose(res)
        self.assertIs(res.pool, old)
        self.assertTrue(new.closed)                                                             # 버린 쪽 묶음 닫힘
        self.assertFalse(old.closed)
        self.assertTrue(res.prep_ok)
        self.assertNotIn("late_reload", res.day.s.get("buy_block") or [])
        self.assertEqual(res.day.s["reload_swap"][-1]["adopted"], False)

    def test_adopt_by_finish_time(self):
        """15:17:59 에 끝나 15:18:01 에 반영 -> 채택 (스냅샷 전). 15:18:01 에 끝남 -> 버림. 스냅샷이 이미 시작했으면 15:18 전에 끝났어도 버림"""
        res = self.morning("07:30:00")
        old, new = Pool("old"), Pool("new")
        self.adopt(res, old)
        self.refetched(res, "15:05:00")
        self.clk.at("15:18:01")
        self.adopt(res, new, self.preps[-1], finished="15:17:59")
        self.settle_dispose(res)
        self.assertIs(res.pool, new)
        self.assertTrue(old.closed)
        self.assertEqual(self.snapshot(res, "15:18:30"), [new])                                 # 바꿔 끼운 적재로 스냅샷

        res2 = self.morning("07:30:00")
        self.preps.clear()
        old2, late = Pool("old2"), Pool("late")
        self.adopt(res2, old2)
        self.refetched(res2, "15:05:00")
        self.clk.at("15:18:02")
        self.adopt(res2, late, self.preps[-1], finished="15:18:01")
        self.settle_dispose(res2)
        self.assertIs(res2.pool, old2)
        self.assertTrue(late.closed)
        self.assertNotIn("late_reload", res2.day.s.get("buy_block") or [])

    def test_no_swap_after_snapshot_started(self):
        res = self.morning("07:30:00")
        old, new = Pool("old"), Pool("new")
        self.adopt(res, old)
        self.refetched(res, "15:05:00")
        self.assertEqual(self.snapshot(res, "15:18:30"), [old])
        self.adopt(res, new, self.preps[-1], finished="15:17:59")                               # (가정) 늦게 반영돼도 스냅샷 뒤면 안 바꿈
        self.settle_dispose(res)
        self.assertIs(res.pool, old)
        self.assertTrue(new.closed)

    def test_stop_watch_runs_during_reload_and_swap(self):
        """다시 적재 중·바꿔 끼우는 tick 모두 손절 감시가 돌고, 기존 묶음 닫기가 막혀도 주 반복은 기다리지 않음"""
        res = self.morning("07:30:00")
        gate = threading.Event()
        old, new = Pool("old", gate), Pool("new")
        self.adopt(res, old)
        self.refetched(res, "10:00:30")
        self.clk.at("10:01:00")
        res.tick(self.clk())
        self.assertEqual(res.last_stop_minute, "2026-10-26 10:01")
        self.assertTrue(res.prep_ok)
        out = self.prep_out()
        out.update(pool=new, finished="10:01:30")
        res.prep_result = out
        self.clk.at("10:02:00")
        t0 = time.time()
        res.tick(self.clk())                                                                  # 이 tick 에서 바꿔 끼움
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(res.last_stop_minute, "2026-10-26 10:02")
        self.assertIs(res.pool, new)
        self.assertFalse(old.closed)                                                            # 닫기는 별도 스레드 (아직 막힘)
        gate.set()
        self.settle_dispose(res)
        self.assertTrue(old.closed)

    # ------------------------------------------------------------ 2 사용 중 버전

    def test_in_use_version_survives_refetch_publishes(self):
        """재적재 실패로 아침 적재 유지 중 재수집 버전 3번 공개 -> 아침 버전 남음, 15:18 고정 = 아침 버전, 다음 채택이 해제"""
        res = self.morning("07:30:00")
        v0 = os.path.basename(C.data_dir(self.ad))
        self.adopt(res, Pool("morning"))
        self.assertEqual(C.in_use_version(self.ad), v0)
        for i in range(4):
            C.publish_data({"update.json": {**jl(C.data_file("update.json", self.ad)), "data_version": f"r{i}|refetch"}}, self.ad)
        have = sorted(os.listdir(os.path.join(self.ad, "versions")))
        self.assertIn(v0, have)
        self.assertEqual(len(have), 4)                                                          # 최근 3개 + 사용 중
        self.assertNotEqual(C.data_dir(self.ad), os.path.join(self.ad, "versions", v0))
        self.snapshot(res, "15:18:30")
        self.assertTrue(res.day.done("snapshot"))
        self.assertEqual(C.pinned_versions(self.ad)["2026-10-26"], v0)                          # 사용 중인 버전 이름 그대로
        self.assertEqual(jl(os.path.join(self.R.LOG_DIR, "2026-10-26.json"))["data_dir_version"], v0)
        cur = os.path.basename(C.data_dir(self.ad))
        res.mark_in_use({"data": {"version_dir": cur}})                                         # 다음 날 아침 채택
        self.assertEqual(C.in_use_version(self.ad), cur)

    def test_released_version_is_cleaned(self):
        ad = os.path.join(self.tmp, "a2")
        os.makedirs(ad)
        vs = [C.publish_data({"update.json": {"data_version": "v0"}}, ad)]
        C.mark_in_use(vs[0], "시험", ad)
        vs += [C.publish_data({"update.json": {"data_version": f"v{i}"}}, ad) for i in range(1, 5)]
        self.assertIn(vs[0], os.listdir(os.path.join(ad, "versions")))
        C.mark_in_use(vs[4], "시험", ad)
        C.publish_data({"update.json": {"data_version": "v5"}}, ad)
        self.assertNotIn(vs[0], os.listdir(os.path.join(ad, "versions")))                      # 해제되면 지난 버전 정리 대상

    def rerun(self, log_extra):
        import model as M
        import run_day as R
        import daily
        snap = os.path.join(self.tmp, "snap.parquet")
        pd.DataFrame({"p_code": ["000001"], "p_open": [1.0], "p_high": [1.0], "p_low": [1.0], "p_close": [1.0], "p_volume": [1.0],
                      "p_amount": [1.0]}).to_parquet(snap)
        ld = os.path.join(self.tmp, "rl")
        os.makedirs(ld, exist_ok=True)
        C.atomic_json(os.path.join(ld, "2026-10-26.json"), {
            "date": "2026-10-26", "errors": [], "ledger_before": {}, **log_extra,
            "live": {"snapshot": {"file": snap}, "index_values": {}, "ledgers": {n: {"top3": [{"code": "000009", "score": 0.5}]} for n in ("strategy", "full")}}})
        seen = []

        class FPool:
            def __init__(self, *a, **k):
                seen.append(k.get("vdir"))

            def close(self):
                pass
        saved = (R.LOG_DIR, R.APPEND_DIR, M.load, E.ShardPool, E.run_day, daily.index_series, C.sd.universe_codes)
        out = os.path.join(self.tmp, "rerun.json")
        try:
            R.LOG_DIR, R.APPEND_DIR = ld, self.ad
            M.load = lambda mo: (None, None, {}, {"ok": True})
            E.ShardPool = FPool
            daily.index_series = lambda: {}
            C.sd.universe_codes = lambda: ["000001"]
            E.run_day = lambda *a, **k: {"eligible": pd.DataFrame({"code": ["000001"], "score": [0.9]}), "factor_default_n": 0, "vscale_default_n": 0,
                                         "ledgers": {n: {"top3": pd.DataFrame({"code": ["000001"], "score": [0.9]})} for n in ("strategy", "full")}}
            R.rerun("2026-10-26", "", out)
        finally:
            (R.LOG_DIR, R.APPEND_DIR, M.load, E.ShardPool, E.run_day, daily.index_series, C.sd.universe_codes) = saved
        return jl(out), seen

    def test_missing_version_rerun_incomplete_not_mismatch(self):
        """그날 버전 폴더가 없으면 지금 버전으로 대조하지 않음 -> '대조 미완료' (상위 3 비교 없음 = 불일치 차단 아님)"""
        r, seen = self.rerun({"data_version": "gone|15:18", "data_dir_version": "20261026071500000000"})
        self.assertEqual(seen, [])                                                              # 계산 안 함
        self.assertIn("폴더 없음", r["rerun"]["incomplete"])
        self.assertEqual(r["errors_add"], [])
        self.assertNotIn("strategy", r["rerun"])

    def test_old_log_without_version_uses_current(self):
        r, seen = self.rerun({})
        self.assertEqual(seen, [None])                                                          # 예전 로그는 예전처럼 지금 버전
        self.assertNotIn("incomplete", r["rerun"])
        self.assertEqual(len(r["errors_add"]), 2)                                               # (가짜 값이라 상위 3 다름)

    def test_resident_records_incomplete_without_block(self):
        import run_day as RD
        res = self.started("15:45:00", live=False)
        res.day.set("settlement", {"account": {}, "d2": 1.0, "diff": {}})
        C.atomic_json(os.path.join(self.R.LOG_DIR, "2026-10-26.json"), {
            "date": "2026-10-26", "mode": "resident", "git_commit": "abcdef0", "errors": [], "model": {"month": "2026-10", "check": {"ok": True}},
            "live": {"ledgers": {}}})
        inc = "그날 데이터 버전 x 폴더 없음 -> 동결 대조 안 함 (지금 버전으로 대신하지 않음)"
        res.close_calc_needed, res.close_ready = (lambda: True), (lambda: True)
        res.close_job = {"result": {"late": True, "rerun_out": {"rerun": {"incomplete": inc, "data_dir": None}, "errors_add": []}}}
        res.ctx["res"] = {}
        res.ac_summary()
        self.assertEqual(res.day.s["rerun_incomplete"]["reason"], inc)
        self.assertTrue(any(a.startswith("[대조 미완료] 2026-10-26") for a in self.alerts))
        self.assertFalse(os.path.exists(self.R.BLOCK))                                         # 불일치 차단 파일 없음
        self.assertFalse(any("8장 12 (1) 불일치" in a for a in self.alerts), self.alerts)
        self.assertEqual(jl(os.path.join(self.R.LOG_DIR, "2026-10-26.json"))["rerun"]["incomplete"], inc)
        self.assertTrue(os.path.exists(RD.SUMMARY))


if __name__ == "__main__":
    unittest.main()
