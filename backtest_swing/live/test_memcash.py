"""
11O (2026-10-29 서브 PC 관찰) 시험: A 계산 프로세스 묶음 메모리·정리, B 현금 대조 104원.
A: OpenBLAS 1 스레드 환경 변수가 작업 프로세스에 물려지는지, 작업 프로세스 수 설정(pool_workers), 버린 준비(날짜 바뀜·16:30·적재 뒤 실패)의
   묶음이 닫히는지, 동결 대조 임시 폴더가 남지 않는지. B: 키움 절사 규칙 + 체결가 끊김을 넣은 기대 예수금·허용치 (10-29 숫자 그대로).
가짜 서버·가짜 시계·가짜 묶음, 실제 주문·조회·logs/exec·data 없음.

  python -m unittest backtest_swing/live/test_memcash.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import multiprocessing as mp
import shutil
import sys
import tempfile
import threading
import time
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import Base, jl  # noqa: E402,F401
import engine as E  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402


def _child_env(conn):
    """spawn 자식: 물려받은 OPENBLAS_NUM_THREADS 를 돌려줌"""
    conn.send(os.environ.get("OPENBLAS_NUM_THREADS"))
    conn.close()


class FakePool:
    def __init__(self):
        self.closed = []

    def shutdown(self, wait=2.0):
        self.closed.append("shutdown")
        return {"killed": 0, "alive": 0}

    def close(self):
        self.closed.append("close")


def join_disposing(res):
    for t in list(res.disposing):
        t.join(5)


# ---------------------------------------------------------------- A 작업 프로세스 설정·환경

class PoolConfig(unittest.TestCase):
    def test_openblas_one_thread_inherited_by_spawn_child(self):
        self.assertEqual(os.environ.get("OPENBLAS_NUM_THREADS"), "1")                 # engine import 가 정함 (numpy 버퍼 = 스레드 수)
        ctx = mp.get_context("spawn")
        a, b = ctx.Pipe()
        p = ctx.Process(target=_child_env, args=(b,), daemon=True)
        p.start()
        b.close()
        self.assertTrue(a.poll(60))
        self.assertEqual(a.recv(), "1")                                                 # 작업 프로세스(spawn)가 물려받음
        p.join(10)

    def test_pool_workers_setting(self):
        cpu = os.cpu_count()
        self.assertEqual(E.pool_workers({}), min(6, cpu))                              # 없으면 6 (오래된 config.json)
        self.assertEqual(E.pool_workers({"pool_workers": 4}), min(4, cpu))
        self.assertEqual(E.pool_workers({"pool_workers": 999}), cpu)                    # CPU 스레드 수로 자름
        self.assertEqual(E.pool_workers({"pool_workers": "x"}), min(6, cpu))
        self.assertEqual(E.pool_workers({"pool_workers": -3}), 1)
        self.assertEqual(E.pool_workers(), min(6, cpu))                                 # config.example.json 값

    def test_shard_pool_uses_setting(self):
        saved = E.pool_workers
        E.pool_workers = lambda cfg=None: 3
        sp = E.ShardPool.__new__(E.ShardPool)
        try:
            # 프로세스는 띄우지 않고 n 결정·나누기만 확인 (시작 부분을 가짜 Process 로)
            class Ctx:
                def Pipe(self):
                    class C:
                        def close(self):
                            pass
                    return C(), C()

                def Process(self, target, args, daemon):
                    class Pr:
                        pid, exitcode = 0, None

                        def start(self):
                            pass
                    return Pr()
            saved_ctx = E.mp.get_context
            E.mp.get_context = lambda kind: Ctx()
            saved_recv = E.ShardPool._recv
            E.ShardPool._recv = lambda self, i, timeout=None: ("ready", len(self.shards[i]), [])
            try:
                sp.__init__([f"{i:06d}" for i in range(10)], use_append=False)
            finally:
                E.mp.get_context, E.ShardPool._recv = saved_ctx, saved_recv
        finally:
            E.pool_workers = saved
        self.assertEqual((sp.n, [len(s) for s in sp.shards], sp.loaded), (3, [4, 3, 3], 10))

    def test_sweep_frozen_tmp(self):
        root = tempfile.mkdtemp()
        try:
            for name in ("111", "222", "index"):
                os.makedirs(os.path.join(root, name, "ext"))
            old = time.time() - 7 * 3600
            os.utime(os.path.join(root, "111"), (old, old))
            os.utime(os.path.join(root, "index"), (old, old))
            self.assertEqual(E.sweep_frozen_tmp(root), 1)
            self.assertEqual(sorted(os.listdir(root)), ["222", "index"])                 # 새 것·지수 폴더는 그대로
            self.assertEqual(E.sweep_frozen_tmp(os.path.join(root, "없음")), 0)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_frozen_worker_removes_tmp_on_stop(self):
        tmp = tempfile.mkdtemp()
        saved = (os.environ.get("TEMP"), E._serve)
        try:
            os.environ["TEMP"] = tmp

            class Store:
                def __init__(self, codes, use_append=True):
                    self.adj, self.excluded = {}, []
            import daily
            saved_store = daily.Store
            daily.Store = Store
            seen = []
            E._serve = lambda conn, codes, store, cur, load_mem: seen.append(os.path.isdir(os.path.join(tmp, "live_frozen", str(os.getpid()), "ext")))
            saved_dirs = (E.sd.EXT_DIR, E.sd.MERGED_DIR, E.sd.RAW_DIR)
            try:
                E._loop(None, [], False, frozen=True)
            finally:
                daily.Store = saved_store
                E.sd.EXT_DIR, E.sd.MERGED_DIR, E.sd.RAW_DIR = saved_dirs
            self.assertEqual(seen, [True])                                              # 일하는 동안은 있음
            self.assertFalse(os.path.exists(os.path.join(tmp, "live_frozen", str(os.getpid()))))  # 끝나면 지움
        finally:
            if saved[0] is None:
                os.environ.pop("TEMP", None)
            else:
                os.environ["TEMP"] = saved[0]
            E._serve = saved[1]
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- A 묶음 정리 (준비 결과)

class PoolLifecycle(Base):
    def test_prepare_closes_pool_when_ledger_read_fails(self):
        import daily
        import run_day as RD
        made = []

        class Pool(FakePool):
            loaded, excluded, n = 2, [], 2

            def __init__(self, *a, **k):
                super().__init__()
                made.append(self)
        idx = pd.Series([1.0], index=pd.to_datetime(["2026-10-23"]))
        saved = (daily.excluded_codes, daily.index_series, E.ShardPool, RD.M.load, RD.ledger_read, RD.APPEND_DIR)
        try:
            RD.APPEND_DIR = os.path.join(self.tmp, "append_none")
            daily.excluded_codes, daily.index_series = (lambda: {}), (lambda: {"kospi": idx, "kosdaq": idx})
            E.ShardPool = Pool
            RD.M.load = lambda mo: (None, [], {"retrain_day": "x", "train_end": "y"}, {"ok": True})
            RD.ledger_read = lambda p: (_ for _ in ()).throw(OSError("장부 읽기 실패"))
            uc = E.sd.universe_codes
            E.sd.universe_codes = lambda: ["000001", "000002"]
            try:
                with self.assertRaises(OSError):
                    RD.prepare(pd.Timestamp("2026-10-26"))
            finally:
                E.sd.universe_codes = uc
        finally:
            (daily.excluded_codes, daily.index_series, E.ShardPool, RD.M.load, RD.ledger_read, RD.APPEND_DIR) = saved
        self.assertEqual(made[0].closed, ["close"])                                     # 만든 묶음을 닫고 실패

    def test_prep_failure_after_load_disposes_pool(self):
        import run_day as RD
        res = self.started("08:04:00", live=False)
        pool = FakePool()
        pool.amt20 = lambda day: (_ for _ in ()).throw(RuntimeError("거래대금 실패"))
        saved = RD.prepare
        RD.prepare = lambda day: (pool, None, [], {}, {}, {"model": {}, "data": {}})
        try:
            res._prep_work(res.day.date, False)
        finally:
            RD.prepare = saved
        join_disposing(res)
        self.assertIn("거래대금 실패", res.prep_result["error"])
        self.assertNotIn("pool", res.prep_result)
        self.assertEqual(pool.closed, ["shutdown"])                                      # 예전: 참조만 버려 작업 프로세스가 남음

    def test_running_prep_dropped_on_day_change_closes_its_pool(self):
        res = self.started("16:20:00", live=False)
        gate, pool = threading.Event(), FakePool()

        def work(day, need):
            gate.wait(5)
            res.publish_prep({"pool": pool, "started": "16:20:00"})
        res._prep_work = work
        self.R.Resident.request_prep(res, "시험")
        t = res.prep_thread
        res.drop_prep("날짜 바뀜 (시험)")                                                  # 아직 도는 준비를 버림
        self.assertIsNone(res.prep_thread)
        gate.set()
        t.join(5)
        join_disposing(res)
        self.assertIsNone(res.prep_result)                                              # 버린 준비 결과는 공개 안 함
        self.assertEqual(pool.closed, ["shutdown"])                                      # 그 묶음은 닫힘

    def test_pending_prep_result_closed_at_1630_and_day_change(self):
        res = self.started("16:29:00", live=False)
        pool = FakePool()
        res.prep_thread, res.prep_result = object(), {"pool": pool}
        self.clk.at("16:31:00")
        res.tick(self.clk())
        join_disposing(res)
        self.assertEqual((res.prep_thread, res.prep_result, pool.closed), (None, None, ["shutdown"]))
        res.tick(self.clk())                                                            # 더 할 것 없으면 그대로
        join_disposing(res)
        self.assertEqual(pool.closed, ["shutdown"])
        pool2 = FakePool()
        res.prep_thread, res.prep_result = object(), {"pool": pool2}
        res.drop_prep("날짜 바뀜 (시험)")
        join_disposing(res)
        self.assertEqual(pool2.closed, ["shutdown"])

    def test_current_prep_still_published(self):
        res = self.started("08:04:00", live=False)
        res._prep_work = lambda day, need: res.publish_prep({"ok": 1})
        self.R.Resident.request_prep(res, "시험")
        res.prep_thread.join(5)
        self.assertEqual(res.prep_result, {"ok": 1})


# ---------------------------------------------------------------- B 현금 대조 (2026-10-29 숫자)

PREV_D2, ACTUAL_D2 = 10_000_000.0, 8_700_352.0  # 공개 사본: 계좌 금액 대신 같은 차이의 가짜 값
FILLS_1029 = [{"side": "sell", "code": "990011", "filled_qty": 228, "fill_price": 3461.0},    # 시장가 손절: 실제 평균 약 3461.36 (기록은 끊긴 값)
              {"side": "buy", "code": "990003", "filled_qty": 1, "fill_price": 651000.0},
              {"side": "buy", "code": "990008", "filled_qty": 25, "fill_price": 35650.0},
              {"side": "buy", "code": "990005", "filled_qty": 1, "fill_price": 535000.0}]


class Cash(Base):
    def test_kiwoom_rounding(self):
        self.assertEqual(P.kiwoom_fee(651000, 0.0035), 2270)                            # 2278.5 -> 10원 미만 절사
        self.assertEqual(P.kiwoom_fee(891250, 0.0035), 3110)
        self.assertEqual(P.kiwoom_fee(535000, 0.0035), 1870)                            # 1872.5
        self.assertEqual(P.kiwoom_tax(789190, 0.002), 1578)                             # 1578.38 -> 원 미만 절사
        self.assertEqual(P.kiwoom_tax(1000, 0.002), 2)                                  # 부동소수 2.0000000000000004 / 1.9999.. 에 흔들리지 않음

    def test_1029_matches_with_true_fill_amount(self):
        exact = [dict(FILLS_1029[0], fill_price=789190 / 228)] + FILLS_1029[1:]         # 실제 체결 금액 789,190 (3465 x 62 + 3460 x 166)
        exp, n = P.expected_cash(PREV_D2, exact, 2026, commission=0.0035)
        self.assertEqual(n, 4)
        self.assertAlmostEqual(exp, ACTUAL_D2, delta=0.01)                              # 키움 절사 규칙이면 원 단위까지 같음

    def test_1029_recorded_price_within_tolerance(self):
        exp, n = P.expected_cash(PREV_D2, FILLS_1029, 2026, commission=0.0035)
        tol = P.cash_tolerance(FILLS_1029)
        self.assertAlmostEqual(ACTUAL_D2 - exp, 82.0, delta=0.01)                       # 남는 차이 = 체결가 끊김 (789,190 - 789,108)
        self.assertEqual(tol, 1 + (228 + 12) + (1 + 12) + (25 + 12) + (1 + 12))          # 304
        self.assertLessEqual(abs(ACTUAL_D2 - exp), tol)
        old = PREV_D2 - sum(r["fill_price"] * r["filled_qty"] * 1.0035 for r in FILLS_1029[1:]) + 3461 * 228 * (1 - 0.0035 - 0.002)
        self.assertGreater(ACTUAL_D2 - old, 20 * 4 + 1)                                 # 예전 계산·허용치(81)로는 104 -> 실패였음
        self.assertGreater(abs(ACTUAL_D2 - 400 - exp), tol)                              # 수백 원 넘는 차이는 계속 잡음
        self.assertEqual(P.cash_tolerance([]), 1.0)

    def test_fill_costs_from_ka10076(self):
        rows = [{"cntr_qty": "228", "cntr_pric": "3461", "tdy_trde_cmsn": "2760", "tdy_trde_tax": "1578"}]
        self.assertEqual(O.fill_costs(rows), {"fee_won": 2760.0, "tax_won": 1578.0})
        self.assertEqual(O.fill_costs([{"cntr_qty": "1", "cntr_pric": "10"}]), {})       # 필드 없으면 기록 안 함
        self.assertEqual(O.fill_costs([{"tdy_trde_cmsn": "", "tdy_trde_tax": "x"}]), {})

    def test_settle_cash_check_1029(self):
        self.prev_state(steps=("after_close",), settlement={"d2": PREV_D2, "equity": PREV_D2})
        res = self.started("15:40:10", live=False)
        for i, f in enumerate(FILLS_1029):
            it = res.om.store.add(date=res.om.date(), stage="stop" if f["side"] == "sell" else "buy", side=f["side"], code=f["code"],
                                  qty=f["filled_qty"], price=None, trde_tp="3", attempt=1, kind="본래", reason="시험", assumed_kind=f["side"])
            extra = {"fee_won": 2760.0, "tax_won": 1578.0} if f["side"] == "sell" else {}
            res.om.store.update(it, status=O.ACCEPTED, ord_no=f"N{i}", filled_qty=f["filled_qty"], fill_price=f["fill_price"], fill_status="체결", **extra)
        self.fk.d2 = ACTUAL_D2
        res.settle()
        c = res.day.s["cash_check"]
        self.assertTrue(c["ok"], c)
        self.assertEqual((c["cash_diff"], c["tolerance"], c["n_fills"]), (82.0, 304.0, 4))
        self.assertEqual(c["fee_tax_rule"], 2270 + 3110 + 1870 + 2760 + 1578)          # 규칙 계산 비용 합
        self.assertIsNone(c["fee_tax_kiwoom"])                                          # 매수 3건에 체결 조회 비용 필드 없음
        self.assertFalse(any("현금 차이" in a for a in self.alerts))


if __name__ == "__main__":
    unittest.main()
