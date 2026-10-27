"""
모의 1일차(10-26) 반영(11I) 시험. 가짜 서버·가짜 시계·가짜 계산, 실제 주문·조회·logs/exec 없음.

  python -m unittest backtest_swing/live/test_mock1.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import Base, jl  # noqa: E402
from test_orders import PLAN, FakeClock, FakeKiwoom  # noqa: E402
import common as C  # noqa: E402
import engine as E  # noqa: E402
import orders as O  # noqa: E402
import quotes as Q  # noqa: E402
import state as S  # noqa: E402

CODES = ["000010", "000020", "000030", "000040"]
TOP = {"strategy": ["000010", "000020", "000030"], "full": ["000020", "000030", "000040"]}


def wait_for(cond, timeout=5.0):
    t0 = time.time()
    while not cond():
        if time.time() - t0 > timeout:
            return False
        time.sleep(0.01)
    return True


def fake_run_day(pool, day, rows, *a, **k):
    """같은 입력이면 같은 결과: 종가 큰 순 점수, 장부별 상위 3"""
    df = pd.DataFrame([{"code": c, "score": float(r[0]["close"]) / 1000, "raw_close": float(r[0]["close"])} for c, r in sorted(rows.items())])
    df = df.sort_values("score", ascending=False).reset_index(drop=True)
    return {"eligible": df, "ledgers": {n: {"top3": df[df.code.isin(TOP[n])].head(3).reset_index(drop=True)} for n in TOP}}


class AfterCloseBase(Base):
    """장 마감 후 계산 시험 준비 (스냅샷 있는 날, 가짜 계산·가짜 대조)"""

    def setUp(self):
        super().setUp()
        self.saved_q = (Q.index_close_today, E.run_day)
        Q.index_close_today = lambda caller, day: {"kospi": 2500.0, "kosdaq": 800.0}
        E.run_day = fake_run_day
        self.cv = {c: {"open": 100.0, "high": 110.0, "low": 90.0, "close": 1000.0 * (i + 1), "volume": 10.0, "amount": 1e4, "last_ts": "x"}
                   for i, c in enumerate(CODES)}

    def tearDown(self):
        Q.index_close_today, E.run_day = self.saved_q
        super().tearDown()

    def day_with_snapshot(self, hms="15:31:00"):
        res = self.started(hms, live=False)
        d = res.day
        live = {"snap_file": os.path.join(self.tmp, "snap.parquet"), "kind": "점검용", "plan": [dict(PLAN[0], kind="점검용")],
                "filter": {"filter_ok": False}, "ledgers": {n: [{"code": c, "score": 1.0} for c in TOP[n]] for n in TOP}, "ledger_before": {}}
        d.set("live", live)
        res.ctx.update(res={"eligible": pd.DataFrame({"code": CODES[:2]})}, rows={c: None for c in CODES}, index={}, boosters=None, cols=None)
        C.atomic_json(os.path.join(self.R.LOG_DIR, f"{d.date.date()}.json"),
                      {"date": str(d.date.date()), "mode": "resident (mock)", "git_commit": "abcdef0", "model": {"month": "2026-10", "check": {"ok": True}},
                       "errors": [], "live": {"ledgers": {n: {"top3": [{"code": c, "score": 1.0} for c in TOP[n]]} for n in TOP}}})
        self.rerun_calls = []
        res.start_rerun = lambda day, out: self.rerun_calls.append(out) or "proc"
        res.wait_rerun = lambda started, out, should_stop=None: {"rerun": {n: {"top3": TOP[n], "same_3of3": True, "overlap": 3} for n in TOP}, "errors_add": []}
        d.mark("ac_ledger")
        return res


class AfterClose(AfterCloseBase):
    """1: 장 마감 후 무거운 계산은 별도 스레드, 주 반복·하트비트를 막지 않음 + 2·3·4 요약 알림"""

    def test_1_close_calc_runs_in_thread_loop_not_blocked(self):
        res = self.day_with_snapshot()
        gate = threading.Event()
        real = res.close_calc
        res.close_calc = lambda job: (gate.wait(10), real(job))[1]                       # 1분봉 9분을 흉내: 풀어 줄 때까지 계산 중
        self.clk.at("15:31:59")
        res.tick(self.clk())
        self.assertIsNone(res.close_thread)                                       # 15:32 전에는 시작 안 함
        self.clk.at("15:32:00")
        t0 = time.time()
        res.tick(self.clk())
        self.assertTrue(res.close_thread.is_alive())
        self.clk.at("15:41:00")
        res.tick(self.clk())                                                      # 계산 중에도 주 반복은 바로 돌아옴
        self.assertLess(time.time() - t0, 2.0)
        d = res.day
        for st in ("settle", "ac_positions"):
            self.assertTrue(d.done(st), st)                                       # 정산·보유 갱신은 기다리지 않음
        self.assertFalse(d.done("ac_summary"))
        self.assertNotIn("ac_summary", d.s["steps"])                              # 실패로 세지도 않음 (기다리는 중)
        gate.set()
        self.assertTrue(wait_for(lambda: not res.close_thread.is_alive()))
        self.clk.at("15:41:05")
        res.tick(self.clk())
        self.assertTrue(d.done("ac_summary"))
        self.assertTrue(d.done("after_close"))
        log = jl(os.path.join(self.R.LOG_DIR, "2026-10-26.json"))
        self.assertEqual([t["code"] for t in log["close_1530"]["ledgers"]["full"]["top3"]], ["000040", "000030", "000020"])
        self.assertEqual(log["rerun"]["strategy"]["same_3of3"], True)
        self.assertEqual(len(self.rerun_calls), 1)

    def test_1_same_input_same_result_thread_or_not(self):
        res = self.day_with_snapshot("15:33:00")
        a = res.close_calc(res.make_close_job())                                  # 같은 스레드에서
        res.start_close_calc()                                                    # 별도 스레드에서
        self.assertTrue(wait_for(res.close_ready))
        b = res.close_result
        drop = ("started", "finished", "seconds", "bars_s", "date", "gen")
        self.assertEqual({k: v for k, v in a.items() if k not in drop}, {k: v for k, v in b.items() if k not in drop})
        # 예전 ac_summary 와 같은 식: 15:30 1분봉 행 -> run_day -> 장부별 상위 3·15:19 와 겹침
        exp = fake_run_day(None, None, {c: ({k: self.cv[c][k] for k in ("open", "high", "low", "close", "volume", "amount")}, None, None) for c in CODES})
        for n in TOP:
            self.assertEqual([t["code"] for t in a["close_1530"]["ledgers"][n]["top3"]], list(exp["ledgers"][n]["top3"].code))
            self.assertEqual(a["close_1530"]["ledgers"][n]["overlap_with_1519"], len(set(TOP[n]) & set(exp["ledgers"][n]["top3"].code)))
        self.assertEqual(a["bars_n"], 4)

    def test_1_close_calc_error_retries_after_60s(self):
        res = self.day_with_snapshot("15:33:00")
        res.close_calc = lambda job: (_ for _ in ()).throw(RuntimeError("1분봉 실패"))
        res.start_close_calc()
        self.assertTrue(wait_for(res.close_ready))
        res.day.set("settlement", {"account": {}, "d2": 1.0, "diff": {}})
        with self.assertRaises(S.StepFailed):
            res.ac_summary()
        self.assertIsNone(res.close_result)
        res.start_close_calc()
        self.assertIsNone(res.close_thread)                                       # 60초 안에는 다시 안 함
        self.clk.at("15:34:01")
        res.start_close_calc()
        self.assertIsNotNone(res.close_thread)

    def test_2_3_4_summary_line(self):
        res = self.day_with_snapshot("15:33:00")
        d = res.day
        d.err("모델 로드 확인 True, 전날 일봉 반영 False -> 그날 신규 매수 안 함", key="prep_data")   # 07:18 오류
        d.resolve("prep_data", "공식 시세 반영 뒤 다시 적재: 전날 일봉 반영 True")                   # 08:09 해결
        d.set("settlement", {"account": {"084670": 28}, "d2": 30_000_000.0, "diff": {}})
        d.set("cash_check", {"ok": None, "note": "전 거래일(2026-10-23) D+2 추정예수금 기록 없음 -> 대조 안 함", "actual_d2": 30_000_000.0})
        res.start_close_calc()
        self.assertTrue(wait_for(res.close_ready))
        line = res.ac_summary()["summary"]
        self.assertIn("대조 전략 3/3, 전체 3/3", line)                              # 2
        self.assertIn("현금 차이 - (전 거래일(2026-10-23) D+2 추정예수금 기록 없음", line)  # 3: 첫날이라 기준 없음
        self.assertIn("오류 0 (해결 1)", line)                                      # 4
        self.assertIn("점검용 상위 3", line)

    def test_2_rerun_text_order_and_missing(self):
        res = self.resident()
        self.assertEqual(res.rerun_text({"strategy": {"top3": ["a", "b", "c"], "overlap": 3, "same_3of3": False},
                                         "full": {"top3": ["a", "b"], "overlap": 1, "same_3of3": False}}), "대조 전략 3/3 순서 다름, 전체 1/2")
        self.assertEqual(res.rerun_text(None), "대조 없음")

    def test_1_wait_rerun_reads_out_file_or_error(self):
        res = self.resident()
        out = os.path.join(self.tmp, "r.json")
        C.atomic_json(out, {"rerun": {"x": 1}, "errors_add": []})
        err = open(out + ".err", "w", encoding="utf-8")
        self.assertEqual(res.wait_rerun((type("P", (), {"wait": lambda self, timeout=None: 0})(), err), out), {"rerun": {"x": 1}, "errors_add": []})
        with open(out + ".err", "w", encoding="utf-8") as f:
            f.write("Traceback: 동결 코드 오류")
        err = open(out + ".err", "a", encoding="utf-8")
        r = res.wait_rerun((type("P", (), {"wait": lambda self, timeout=None: 1})(), err), out)
        self.assertIn("rerun 실패 (종료 1)", r["error"])
        self.assertIn("동결 코드 오류", r["error"])


class Lifecycle(AfterCloseBase):
    """11I 보완: 작업 날짜·세대, 날짜 바뀜 취소, 16:30 뒤 정리, 상한 초과 = 대조 미완료"""

    def blocked(self, res):
        gate = threading.Event()
        real = res.close_calc
        res.close_calc = lambda job: (gate.wait(10), real(job))[1]
        return gate

    def test_carry_over_to_next_day_result_discarded(self):
        res = self.day_with_snapshot("15:33:00")
        res.wait_rerun = lambda started, out, should_stop=None: {"rerun": {n: {"top3": TOP[n], "same_3of3": False, "overlap": 3} for n in TOP},
                                                                "errors_add": []}   # 쓰이면 block_new_buys 를 만들 결과
        gate = self.blocked(res)
        res.start_close_calc()
        old = res.close_job
        self.assertEqual((old["date"], old["gen"]), ("2026-10-26", 1))
        self.clk.t = pd.Timestamp("2026-10-27 07:30:00")                         # 계산이 다음 날까지 남음
        res.tick(self.clk())
        self.assertEqual(str(res.day.date.date()), "2026-10-27")
        self.assertTrue(old["cancel"].is_set())                                   # 이전 작업에 취소 신호
        self.assertIsNone(res.close_job)
        self.assertEqual(res.close_stale, [old])
        gate.set()
        self.assertTrue(wait_for(lambda: not old["thread"].is_alive()))
        self.assertIn("취소 (날짜 바뀜", old["result"]["aborted"])
        self.assertEqual(old["result"]["date"], "2026-10-26")
        self.clk.at("07:31:00")
        res.tick(self.clk())
        self.assertEqual(res.close_stale, [])                                     # 끝난 뒤 참조 회수
        self.assertFalse(os.path.exists(self.R.BLOCK))                            # 전날 결과로 매수 차단 안 만듦
        self.assertFalse(any("8장 12 (1)" in a or "대조 미완료" in a for a in self.alerts))
        st = jl(os.path.join(self.tmp, "2026-10-27.json"))
        self.assertFalse(any(k in st for k in ("measure_result", "rerun_incomplete")))   # 오늘 기록에도 안 씀

    def test_result_with_other_date_or_gen_dropped(self):
        res = self.day_with_snapshot("15:33:00")
        res.start_close_calc()
        self.assertTrue(wait_for(lambda: not res.close_thread.is_alive()))
        res.close_job["result"]["date"] = "2026-10-23"                           # 다른 날 결과
        self.assertFalse(res.close_ready())
        self.assertIsNone(res.close_job)
        res.start_close_calc()
        self.assertTrue(wait_for(lambda: not res.close_thread.is_alive()))
        res.close_job["gen"] += 1                                                 # 다른 세대 결과
        self.assertFalse(res.close_ready())

    def stuck_pool(self, closed, release):
        """자식이 안 꺼지는 묶음: 정리(shutdown)가 release 될 때까지 끝나지 않음"""
        return type("StuckPool", (), {"shutdown": lambda self: (closed.append("start"), release.wait(10), closed.append("end"))[-1]})()

    def test_stuck_pool_does_not_delay_0900_stop(self):
        closed, release = [], threading.Event()
        res = self.day_with_snapshot("15:33:00")
        res.pool = self.stuck_pool(closed, release)
        gate = self.blocked(res)
        res.start_close_calc()
        try:
            self.clk.t = pd.Timestamp("2026-10-27 07:30:00")                     # 날짜 바뀜: 취소한 작업의 묶음 정리
            t0 = time.time()
            res.tick(self.clk())
            self.assertLess(time.time() - t0, 3.0)                                # 주 반복은 정리를 기다리지 않음
            self.assertTrue(wait_for(lambda: closed == ["start"]))                # 정리는 별도 스레드에서 멈춰 있음
            calls = []
            res.stop_minute = lambda **k: calls.append(time.time())
            self.clk.at("09:00:00")
            t0 = time.time()
            res.tick(self.clk())
            self.assertEqual(len(calls), 1)
            self.assertLess(calls[0] - t0, 0.5)                                   # 09:00 손절 호출 지연 없음
            self.assertTrue(res.disposing[-1].is_alive())                         # 그때도 정리 스레드는 아직 멈춤
        finally:
            release.set()
            gate.set()
        self.assertTrue(wait_for(lambda: closed == ["start", "end"]))

    def test_shardpool_shutdown_terminate_wait_kill(self):
        calls = []

        class Proc:
            def __init__(self, stubborn):
                self.stubborn, self.dead = stubborn, False

            def terminate(self):
                calls.append("terminate")
                self.dead = not self.stubborn

            def join(self, t=None):
                calls.append(("join", round(t, 1) if t is not None else None))

            def is_alive(self):
                return not self.dead

            def kill(self):
                calls.append("kill")
                self.dead = True
        pool = E.ShardPool.__new__(E.ShardPool)
        pool.conns = [type("C", (), {"close": lambda self: calls.append("pipe")})()]
        pool.procs = [Proc(True)]                                                 # terminate 로 안 꺼지는 자식
        r = pool.shutdown(wait=0.1)
        self.assertEqual(calls[:3], ["pipe", "terminate", ("join", 0.1)])
        self.assertEqual(calls[3:], ["kill", ("join", 1.0)])
        self.assertEqual(r, {"killed": 1, "alive": 0})

    def test_last_measure_past_1630_finishes_after_close(self):
        closed = []
        res = self.day_with_snapshot("15:31:00")
        res.pool = type("FakePool", (), {"shutdown": lambda self: closed.append(1)})()
        gate = threading.Event()
        res.compare_measure = lambda bars, targets, d=None: (gate.wait(10), {"n": 0})[1]   # 마지막 측정이 끝나지 않음
        self.clk.at("15:32:00")
        res.tick(self.clk())
        job = res.close_job
        self.clk.at("15:41:00")
        res.tick(self.clk())
        d = res.day
        self.assertTrue(d.done("settle") and not d.done("ac_summary"))
        self.clk.at("16:30:00")
        t0 = time.time()
        res.tick(self.clk())
        self.assertLess(time.time() - t0, 3.0)
        self.assertTrue(job["cancel"].is_set())
        self.assertTrue(d.done("ac_summary"))                                     # PENDING 으로 남지 않음
        self.assertTrue(d.done("after_close"))
        self.assertIn("16:30 지남", d.s["rerun_incomplete"]["reason"])
        self.assertIn("다음 날 매수에 영향 없음", d.s["rerun_incomplete"]["note"])
        log = jl(os.path.join(self.R.LOG_DIR, "2026-10-26.json"))
        self.assertIn("rerun_incomplete", log)
        self.assertIn("close_1530", log)                                          # 그때까지 계산한 15:30 상위 3 은 남김
        self.assertTrue(any("[대조 미완료]" in a for a in self.alerts))
        self.assertTrue(wait_for(lambda: closed == [1]))                          # 묶음은 별도 스레드에서 정리
        self.assertIsNone(res.pool)
        gate.set()                                                                # 늦게 끝난 측정
        self.assertTrue(wait_for(lambda: not job["thread"].is_alive()))
        self.assertIn("취소", job["result"]["aborted"])                            # 측정 뒤 확인에서 멈춤
        self.clk.at("16:31:00")
        res.tick(self.clk())
        self.assertEqual((res.close_stale, res.close_job), ([], None))
        self.assertFalse(os.path.exists(self.R.BLOCK))
        st = jl(os.path.join(self.tmp, "2026-10-26.json"))
        self.assertEqual((st["steps"]["after_close"]["status"], st["steps"]["ac_summary"]["status"]), (S.SUCCEEDED, S.SUCCEEDED))

    def test_deadline_checked_after_last_measure(self):
        res = self.day_with_snapshot("16:15:00")
        clk = self.clk

        def measure(bars, targets, d=None):
            clk.at("16:20:01")                                                    # 마지막 측정 중 상한 넘김
            return {"n": 0}
        res.compare_measure = measure
        res.start_close_calc()
        self.assertTrue(wait_for(res.close_ready))
        self.assertIn("상한 16:20 초과", res.close_result["aborted"])
        self.assertEqual(res.close_result["measure_1520"], {"n": 0})               # 측정 결과는 남김

    def test_after_1630_no_job_pool_closed(self):
        closed = []
        res = self.day_with_snapshot("16:00:00")
        res.pool = type("FakePool", (), {"close": lambda self: closed.append(1)})()
        self.clk.at("16:30:00")
        res.tick(self.clk())
        self.assertIsNone(res.pool)
        self.assertTrue(wait_for(lambda: closed == [1]))

    def test_deadline_aborts_records_incomplete_no_block(self):
        res = self.day_with_snapshot("16:19:00")
        clk = self.clk

        def bars(caller, code, day):
            if code == CODES[1]:
                clk.at("16:20:01")                                                # 1분봉 받다가 상한 넘김
            return code
        Q.bars_today = bars
        res.wait_rerun = lambda started, out, should_stop=None: {"error": f"대조 중단: {should_stop()}", "stopped": should_stop()}
        res.start_close_calc()
        self.assertTrue(wait_for(res.close_ready))
        self.assertIn("상한 16:20 초과", res.close_result["aborted"])
        d = res.day
        d.set("settlement", {"account": {}, "d2": 1.0, "diff": {}})
        self.clk.at("16:20:10")
        S.run_step(d, "ac_summary", res.ac_summary)
        self.assertTrue(d.done("ac_summary"))                                     # 대조 미완료여도 요약은 끝냄 (after_close 진행)
        line = [a for a in self.alerts if "요약]" in a][-1]
        self.assertTrue("대조 미완료" in line)
        self.assertTrue(any("[대조 미완료]" in a and "매수 차단 안 함" in a for a in self.alerts))
        self.assertIn("다음 날 매수에 영향 없음", d.s["rerun_incomplete"]["note"])
        self.assertFalse(os.path.exists(self.R.BLOCK))
        self.assertFalse(d.s.get("buy_block"))
        self.assertIn("rerun_incomplete", jl(os.path.join(self.R.LOG_DIR, "2026-10-26.json")))

    def test_wait_rerun_kills_on_stop(self):
        res = self.resident()
        killed = []

        class P:
            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("rerun", timeout)

            def kill(self):
                killed.append(1)
        out = os.path.join(self.tmp, "r.json")
        r = res.wait_rerun((P(), open(out + ".err", "w", encoding="utf-8")), out, lambda: "상한 16:20 초과")
        self.assertEqual((killed, r["stopped"]), ([1], "상한 16:20 초과"))

    def test_quote_http_timeout(self):
        from types import SimpleNamespace
        self.R.Caller = lambda *a, **k: SimpleNamespace(c=SimpleNamespace(timeout=15))
        res = self.resident()
        self.assertEqual(res.quote_caller().c.timeout, self.R.QUOTE_TIMEOUT)


class Heartbeat(Base):
    """1: 하트비트는 별도 스레드가 1분마다, 주 반복이 멈추면 감시 작업이 따로 알림"""

    def test_1_heartbeat_thread_writes_while_main_blocked(self):
        res = self.resident()
        res.cur_step, res.last_tick = "close_calc", time.time()
        self.clk.at("15:40:00")
        th = res.start_heartbeat(every=0.01)
        try:
            hb = self.R.CFG["heartbeat_file"]
            self.assertTrue(wait_for(lambda: os.path.exists(hb) and len(open(hb, encoding="utf-8").read().splitlines()) == 1))
            res.last_tick = time.time() - 660                                     # 주 반복이 11분째 안 돎 (예: 무거운 단계)
            for m in ("15:41:00", "15:42:00"):
                self.clk.at(m)
                self.assertTrue(wait_for(lambda m=m: m in open(hb, encoding="utf-8").read()))
        finally:
            res.hb_stop.set()
            th.join(2)
        lines = open(hb, encoding="utf-8").read().splitlines()
        self.assertEqual([x[11:16] for x in lines], ["15:40", "15:41", "15:42"])  # 1분에 한 줄
        self.assertIn(f" pid={os.getpid()} step=close_calc", lines[-1])
        self.assertRegex(lines[-1], r" loop=6\d\ds$")

    def test_1_watchdog_alerts_stalled_loop_once(self):
        import common
        import watchdog as W
        saved = (dict(W.CFG), W.LOOP_FLAG, W.FLAG, W.LOG, W.start, W.N.send, common.resident_running)
        tmp = tempfile.mkdtemp()
        sent = []
        try:
            W.CFG.update(log_dir=tmp, state_dir=tmp, heartbeat_file=os.path.join(tmp, "hb.log"), pid_file=os.path.join(tmp, "r.pid"))
            W.LOOP_FLAG, W.FLAG, W.LOG = os.path.join(tmp, "loop.flag"), os.path.join(tmp, "hb.flag"), os.path.join(tmp, "w.log")
            W.start = lambda: self.fail("살아 있으면 켜지 않음")
            W.N.send = lambda msg, **k: sent.append(msg)
            common.resident_running = lambda *a, **k: True
            now = pd.Timestamp.now()
            with open(W.CFG["heartbeat_file"], "w", encoding="utf-8") as f:
                f.write(f"{now:%Y-%m-%d %H:%M:%S} pid=777 step=close_calc loop=660s\n")
            W.main()
            W.main()
            self.assertEqual(len(sent), 1)                                        # 멈춘 동안 한 번만
            self.assertIn("[주 반복 멈춤]", sent[0])
            with open(W.CFG["heartbeat_file"], "a", encoding="utf-8") as f:
                f.write(f"{now:%Y-%m-%d %H:%M:%S} pid=777 step=idle loop=1s\n")
            W.main()
            self.assertFalse(os.path.exists(W.LOOP_FLAG))                         # 다시 돌면 해제
        finally:
            (cfg, W.LOOP_FLAG, W.FLAG, W.LOG, W.start, W.N.send, common.resident_running) = saved
            W.CFG.clear()
            W.CFG.update(cfg)
            shutil.rmtree(tmp, ignore_errors=True)


class Errors(Base):
    """4: 해결된 오류는 오류 수에서 빼고 따로 센다"""

    def test_4_prep_data_error_resolved_by_reload(self):
        res = self.started("07:18:00", live=False)
        for name in ("finalize_recovery", "check_exclusions", "official_vs_1530"):
            setattr(res, name, lambda *a: None)
        pool = type("FakePool", (), {"close": lambda self: None})

        def out(up):
            return {"pool": pool(), "boosters": None, "cols": None, "index": {}, "ledgers": {}, "frozen": {"ok": True}, "amt20": {}, "excluded": {},
                    "prep": {"model": {"check": {"ok": True}}, "ledger_version": 0,
                             "data": {"daily_up_to_date": up, "prev_trading_day": "2026-10-23", "data_version": "v"}}}
        res.pool, res.prep_reason, res.prep_result = None, "하루 시작", out(False)
        res.apply_prep()
        d = res.day
        self.assertEqual(d.open_error_count(), 1)
        self.clk.at("08:09:00")
        res.prep_reason, res.prep_result = "공식 시세 반영 뒤 다시 적재 (보관 행 2700종목 보정)", out(True)
        res.apply_prep()
        self.assertEqual((len(d.s["errors"]), d.open_error_count(), len(d.s["errors_resolved"])), (1, 0, 1))  # 기록은 그대로, 해결 표시만
        self.assertEqual(d.s["errors_resolved"][0]["resolved_at"], "08:09:00")
        self.assertEqual(d.resolve("prep_data", "다시"), 0)                         # 두 번 세지 않음


class Label(unittest.TestCase):
    """5: 주문 이름표는 분류를 따른다 (필터 미통과일 = 점검용)"""

    def test_5_buy_label_follows_kind(self):
        for kind, label in (("본래", "매수 (P4)"), ("점검용", "매수 (점검용)")):
            clk = FakeClock("2026-10-26 15:21:05")
            om = O.OrderManager(O.Broker(FakeKiwoom(clk), allow_orders=True), clock=clk, sleep=clk.sleep)
            r = om.place_buys([dict(PLAN[0], kind=kind)], kind)[0]
            self.assertEqual((r["kind"], r["reason"]), (kind, label))


class RerunOut(unittest.TestCase):
    """1: 동결 코드 대조를 결과 파일로 따로 써도(1분봉 받는 동안 함께) 그날 로그에 쓰던 것과 같은 결과"""

    def test_1_rerun_out_file_same_as_in_place(self):
        import daily
        import model as M
        import run_day as R
        tmp = tempfile.mkdtemp()
        saved = (R.LOG_DIR, M.load, R.sd.universe_codes, E.ShardPool, E.run_day, daily.index_series, R.now)
        try:
            snap = os.path.join(tmp, "snap.parquet")
            pd.DataFrame({"p_code": CODES, "p_open": [100.0] * 4, "p_high": [110.0] * 4, "p_low": [90.0] * 4, "p_close": [1000.0, 3000.0, 2000.0, 4000.0],
                          "p_volume": [10.0] * 4, "p_amount": [1e4] * 4}).to_parquet(snap, index=False)
            log = {"date": "2026-10-26", "errors": ["앞 오류"], "ledger_before": {},
                   "live": {"snapshot": {"file": snap}, "index_values": {}, "scores_file": os.path.join(tmp, "none.parquet"),
                            "ledgers": {n: {"top3": [{"code": c, "score": 1.0} for c in (TOP[n] if n == "strategy" else TOP[n][::-1])]} for n in TOP}}}
            R.LOG_DIR = tmp
            M.load = lambda mo: (None, None, {}, {"ok": True})
            R.sd.universe_codes = lambda: CODES
            E.ShardPool = type("FakePool", (), {"__init__": lambda self, *a, **k: None, "close": lambda self: None})
            E.run_day = fake_run_day
            daily.index_series = lambda: {}
            R.now = lambda: pd.Timestamp("2026-10-26 15:33:00")
            C.atomic_json(os.path.join(tmp, "2026-10-26.json"), log)
            R.rerun("2026-10-26")                                                 # 예전: 그날 로그에 씀
            old = jl(os.path.join(tmp, "2026-10-26.json"))
            C.atomic_json(os.path.join(tmp, "2026-10-26.json"), log)
            R.rerun("2026-10-26", "", os.path.join(tmp, "out.json"))              # 새: 결과 파일에만
            new = jl(os.path.join(tmp, "out.json"))
            self.assertEqual(jl(os.path.join(tmp, "2026-10-26.json")), json.loads(json.dumps(log)))   # 그날 로그는 안 고침
            self.assertEqual(new["rerun"], old["rerun"])
            self.assertEqual(log["errors"] + new["errors_add"], old["errors"])
            self.assertEqual(old["rerun"]["full"]["same_3of3"], False)              # 순서 다른 경우도 같은 판정
        finally:
            (R.LOG_DIR, M.load, R.sd.universe_codes, E.ShardPool, E.run_day, daily.index_series, R.now) = saved
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
