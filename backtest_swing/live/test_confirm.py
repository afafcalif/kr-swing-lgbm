"""
Codex 확인 리뷰(11F, 2026-10-24) 시험: N1 ~ N3, 5, 7, 15, 16, 26, 27 보강. 상태 있는 가짜 서버·가짜 시계, 실제 주문·조회·logs/exec 없음.
외부 데이터 파일(data/ 아래)에 기대지 않는다.

  python -m unittest backtest_swing/live/test_confirm.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_codex import Base, Codex, Crash, bal_row, jl  # noqa: E402
from test_orders import PLAN, sends  # noqa: E402
import common as C  # noqa: E402
import engine as E  # noqa: E402
import orders as O  # noqa: E402
import positions as P  # noqa: E402
import quotes as Q  # noqa: E402
import state as S  # noqa: E402


def intents_file(tmp, day, items):
    p = os.path.join(tmp, "intents", f"{day}.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    C.atomic_json(p, items)


def day_state(tmp, day, clk, steps=(), **sets):
    d = S.Day(os.path.join(tmp, f"{day}.json"), pd.Timestamp(day), clock=clk)
    for s in steps:
        d.mark(s)
    for k, v in sets.items():
        d.set(k, v)
    return d


class Confirm(Base):
    # ---------------------------------------------------------------- N1
    def test_N1_recovery_spans_days_since_last_settled_with_missed_day(self):
        day_state(self.tmp, "2026-10-20", self.clk, steps=("after_close",))
        day_state(self.tmp, "2026-10-21", self.clk, steps=("settle",))                      # 정산 미완료
        intents_file(self.tmp, "2026-10-21", [{"intent_id": "a1", "date": "2026-10-21", "stage": "buy", "side": "buy", "code": "084670", "qty": 28,
                                              "status": "UNKNOWN", "kind": "본래"}])
        # 10-22 는 실행 안 한 날 (상태·의도 파일 없음)
        day_state(self.tmp, "2026-10-23", self.clk, steps=("ac_summary",))
        self.fk.balance_rows = [bal_row("084670", 28, avg=10000)]
        res = self.started("07:10:00")
        res.day.s["steps"].pop("prev_settle")
        r = res.prev_settle()
        self.assertEqual((r["incomplete_days"], r["missed_days"], r["settled"]), (["2026-10-21", "2026-10-22", "2026-10-23"], ["2026-10-22"], 1))
        self.assertEqual(r["days"], ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"])   # 최근 5거래일도 봄
        self.assertEqual(jl(os.path.join(self.tmp, "intents", "2026-10-21.json"))[0]["status"], O.ACCEPTED)   # 앞날 UNKNOWN 을 계좌로 확정
        p = jl(self.R.POSITIONS)["held"]["084670"]
        self.assertEqual((p["entry_date"], p["stop_temp"]), ("2026-10-21", True))                                 # 의도 날짜가 진입일
        for x in ("2026-10-21", "2026-10-23"):
            self.assertTrue(S.Day(os.path.join(self.tmp, f"{x}.json"), x).done("after_close"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "2026-10-22.json")))                               # 실행 안 한 날은 기록만
        self.assertEqual(res.day.s["prev_recovery"]["missed_days"], ["2026-10-22"])
        self.assertTrue(any("실행 안 한 날" in a for a in self.alerts))
        self.assertIn("복구할 것 없음", res.prev_settle()["note"])                                                # 다시 돌려도 중복 없음

    # ---------------------------------------------------------------- 5
    def test_5_unfinished_recovery_blocks_buys_stop_continues(self):
        day_state(self.tmp, "2026-10-23", self.clk, steps=("ac_summary",))
        intents_file(self.tmp, "2026-10-23", [{"intent_id": "p1", "date": "2026-10-23", "stage": "buy", "side": "buy", "code": "084670", "qty": 28,
                                              "status": "ACCEPTED", "kind": "본래"}])
        self.fk.balance_rows = [bal_row("084670", 28, avg=10000)]
        res = self.started("09:00:00")
        res.day.s["steps"].pop("prev_settle")
        res.prev_settle()
        res.day.mark("prev_settle")
        gate = "; ".join(res.buy_gate())
        self.assertIn("전날 복구 미완료", gate)
        self.assertIn("임시 거래 정보", gate)
        self.clk.at("10:00:00")
        self.quotes["084670"] = 9100.0
        res.stop_minute()                                                    # 임시 손절가(9150)로 손절은 계속
        self.assertEqual(len(sends(self.fk, "kt10001")), 1)
        self.clk.at("15:22:00")
        self.assertEqual(res.buy()["sent"], 0)
        self.assertEqual(len(sends(self.fk)), 0)
        self.judged["084670"] = {"close": 10500.0, "stop": 9800.0, "trend": False, "basis": "수정주가 일봉"}
        res.finalize_recovery()
        gate = "; ".join(res.buy_gate())
        self.assertNotIn("전날 복구 미완료", gate)
        self.assertNotIn("임시 거래 정보", gate)

    # ---------------------------------------------------------------- N2·7
    def test_N2_fills_query_fails_at_1540_then_reconfirmed_at_1600(self):
        res = self.started("15:22:00")
        res.buy()
        no = res.om.records[0]["ord_no"]
        self.fk.fill(no, 28, 33000, "15:30:00")
        self.fk.read_script["ka10076"] = ["http502"] * 6                    # 15:35 확인·15:40 거래 정보 확정 때 체결 조회 실패
        self.clk.at("15:36:00")
        S.run_step(res.day, "close_check", res.close_check)
        self.assertEqual(res.day.status("close_check"), S.FAILED)
        self.clk.at("15:40:10")
        res.settle()
        self.assertEqual(res.day.s["settlement"]["account"], {"084670": 28})  # 계좌 수량은 확정
        self.cv["084670"] = {"close": 33500}
        self.judged["084670"] = {"close": 33500.0, "stop": 31000.0, "trend": True, "basis": "수정주가 일봉"}
        self.clk.at("15:40:30")
        S.run_step(res.day, "ac_positions", res.ac_positions, max_attempts=60)
        self.assertEqual(res.day.status("ac_positions"), S.FAILED)
        p = jl(self.R.POSITIONS)["held"]["084670"]
        self.assertTrue(p["provisional_new"] and p["info_pending"] and p["stop_temp"])
        self.assertEqual(p["qty"], 28)
        gate = "; ".join(res.buy_gate())
        self.assertIn("거래 정보 미확정", gate)
        self.assertTrue(any("거래 정보 미확정" in a for a in self.alerts))
        self.clk.at("16:00:00")                                                # 체결 조회 성공 -> 다시 확정
        S.run_step(res.day, "ac_positions", res.ac_positions, max_attempts=60)
        self.assertEqual(res.day.status("ac_positions"), S.SUCCEEDED)
        p = jl(self.R.POSITIONS)["held"]["084670"]
        self.assertEqual((p["trade_id"], p["stop_price"], p["stop_temp"], p["kind"]), ("084670-2026-10-26-1", 31000.0, False, "본래"))
        self.assertFalse(p.get("provisional_new") or p.get("info_pending"))
        self.assertTrue(res.day.s["positions_unconfirmed"]["resolved_at"])
        self.assertNotIn("거래 정보 미확정", "; ".join(res.buy_gate()))

    def test_N2_unconfirmed_next_morning_recovery_uses_intents(self):
        P.save(self.R.POSITIONS, {"held": {"084670": {"qty": 28, "trade_id": "084670-2026-10-23-1", "entry_date": "2026-10-23", "kind": "미확인",
                                                      "stop_price": 9150.0, "stop_temp": True, "provisional_new": True, "info_pending": True}},
                                  "next_open_sells": {}, "trade_seq": 1})
        day_state(self.tmp, "2026-10-23", self.clk, steps=("settle", "ac_summary"))
        intents_file(self.tmp, "2026-10-23", [{"intent_id": "b1", "date": "2026-10-23", "stage": "buy", "side": "buy", "code": "084670", "qty": 28,
                                              "status": "ACCEPTED", "ord_no": "B1", "kind": "점검용"}])
        self.fk.balance_rows = [bal_row("084670", 28, avg=33000)]
        res = self.started("07:10:00")
        res.day.s["steps"].pop("prev_settle")
        res.prev_settle()
        p = jl(self.R.POSITIONS)["held"]["084670"]
        self.assertEqual((p["trade_id"], p["kind"], p["entry_date"]), ("084670-2026-10-23-1", "점검용", "2026-10-23"))
        self.assertFalse(p.get("provisional_new") or p.get("info_pending") or p.get("entry_temp"))
        self.assertTrue(p["stop_temp"])                                       # 손절가는 준비 뒤 공식 일봉으로 (finalize)

    # ---------------------------------------------------------------- 18: 보유 있는 계좌, 하위 단계 저장 중 종료
    def test_kill_while_saving_positions_with_holdings_then_restart(self):
        P.save(self.R.POSITIONS, {"held": {"084670": {"qty": 28, "trade_id": "084670-2026-10-20-1", "entry_date": "2026-10-20", "kind": "본래",
                                                      "stop_price": 30000.0}}, "next_open_sells": {}, "trade_seq": 1})
        intents_file(self.tmp, "2026-10-26", [{"intent_id": "b1", "date": "2026-10-26", "stage": "buy", "side": "buy", "code": "005930", "qty": 10,
                                              "price": 60000, "status": "ACCEPTED", "ord_no": "B1", "filled_qty": 10, "fill_price": 60000.0,
                                              "fill_status": "체결", "kind": "본래", "assumed_kind": "buy", "trde_tp": "0", "pre_qty": 0}])
        self.fk.balance_rows = [bal_row("084670", 28, avg=33000), bal_row("005930", 10, avg=60000)]
        self.cv.update({"084670": {"open": 33500, "close": 34000}, "005930": {"open": 60500, "close": 61000}})
        self.judged.update({"084670": {"close": 34000.0, "stop": 32000.0, "trend": True, "basis": "수정주가 일봉"},
                            "005930": {"close": 61000.0, "stop": 57000.0, "trend": False, "basis": "수정주가 일봉"}})
        saved_save = P.save

        def save_then_die(path, pos):                                         # 거래 정보 저장 도중 강제 종료
            if "005930" in pos.get("held", {}):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(pos)[:40])                             # 반쯤 쓴 파일
                raise Crash()
            return saved_save(path, pos)
        res1 = self.resident()
        self.clk.at("15:41:00")
        P.save = save_then_die
        try:
            with self.assertRaises(Crash):
                res1.tick(self.clk())
        finally:
            P.save = saved_save
        res2 = self.resident()                                                 # 재시작: 보유 장부 손상 -> 백업 복구, 하위 단계 이어서
        self.clk.at("15:41:30")
        res2.tick(self.clk())
        self.assertEqual(res2.day.status("ac_positions"), S.FAILED)
        self.clk.at("15:42:05")
        res2.tick(self.clk())
        for s in ("settle", "ac_positions", "ac_ledger", "ac_summary", "after_close"):
            self.assertTrue(res2.day.done(s), s)
        held = jl(self.R.POSITIONS)["held"]
        self.assertEqual((held["084670"]["trade_id"], held["084670"]["stop_price"], held["084670"]["trend_sell_next_open"]),
                         ("084670-2026-10-20-1", 30000.0, True))
        self.assertEqual((held["005930"]["trade_id"], held["005930"]["stop_price"], held["005930"]["trend_sell_next_open"]),
                         ("005930-2026-10-26-2", 57000.0, False))                # 기준 장부에서 다시 계산 (거래 번호 중복 없음)
        self.assertEqual(res2.day.s["steps"]["settle"]["attempts"], 1)

    # ---------------------------------------------------------------- 26
    def test_26_order_tr_no_client_429_retry_and_no_send_after_1528(self):
        import kiwoom.client as KC

        class Sess:
            calls = []

            def request(self, **kw):
                Sess.calls.append(kw["headers"]["api-id"])
                return SimpleNamespace(status_code=429, ok=False, text="", headers={})
        c = KC.KiwoomRestClient(settings=SimpleNamespace(base_url="http://mock"), auth=SimpleNamespace(get_token=lambda: "t"))
        c.session, c.rate_limiter = Sess(), SimpleNamespace(wait=lambda max_wait=None: True)
        slept, saved = [], KC.time.sleep
        KC.time.sleep = slept.append
        try:
            self.assertIsNone(c.request("kt10000", "POST", "/api/dostk/ordr", json={}, retry_429=False))
            self.assertEqual((Sess.calls, slept), (["kt10000"], []))
            c.request("ka10075", "POST", "/api/dostk/acnt", json={})          # 조회 TR 은 지금처럼 클라이언트 재시도
            self.assertEqual(slept, [2, 4, 8, 16])
            Sess.calls, slept[:] = [], []
            self.clk.t = pd.Timestamp("2026-10-26 15:27:59.500")
            om = O.OrderManager(O.Broker(c, allow_orders=True), clock=self.clk, sleep=self.clk.sleep, times={"buy_until": "15:27:59"})
            om.place_buys(PLAN, "본래")
        finally:
            KC.time.sleep = saved
        self.assertEqual((Sess.calls, slept), (["kt10000"], []))               # 클라이언트 대기 없음, 15:28 넘은 재시도 없음
        self.assertEqual([(i["status"], i["return_code"]) for i in om.records], [(O.REJECTED, -429)])
        self.assertEqual(self.clk.slept, [1.0])

    # ---------------------------------------------------------------- N3
    def test_N3_trend_sell_after_0831_recovery(self):
        P.save(self.R.POSITIONS, {"held": {"084670": {"qty": 10, "entry_date": "2026-10-20", "stop_price": 1.0}}, "next_open_sells": {}})
        self.fk.balance_rows = [bal_row("084670", 10)]
        res = self.started("08:30:05")
        res.tick(self.clk())
        self.assertTrue(res.day.done("trend_sell"))
        self.assertEqual(len(sends(self.fk, "kt10001")), 0)
        pos = jl(self.R.POSITIONS)                                            # 08:31 전날 복구 마무리가 추세 판정을 넣음
        pos = P.normalize(pos)
        pos["held"]["084670"].update(trend_sell_next_open=True, judged_on="2026-10-23", judged_by="공식 일봉 (다음 날 복구)")
        res.mark_trend(pos, "084670", "2026-10-23", "공식 일봉 (다음 날 복구)")    # 복구 마무리와 같게 (11J 보완 3: 매도 의무 + 목록)
        P.save(self.R.POSITIONS, pos)
        for hms in ("08:31:00", "08:31:30", "08:32:00", "08:33:00"):
            self.clk.at(hms)
            res.tick(self.clk())
        self.assertEqual(len(sends(self.fk, "kt10001")), 1)                   # 한 번만 (의도 기록으로 중복 막음)
        self.assertIn("084670", jl(self.R.POSITIONS)["next_open_sells"])

    # ---------------------------------------------------------------- 16
    def test_16_summary_row_without_snapshot(self):
        import run_day as RD
        saved = RD.SUMMARY
        RD.SUMMARY = os.path.join(self.tmp, "summary.csv")
        try:
            res = self.started("15:45:00", live=False)
            res.day.set("settlement", {"account": {}, "d2": 1.0, "diff": {}})
            res.ac_summary()
            row = pd.read_csv(RD.SUMMARY, dtype=str).iloc[0]
        finally:
            RD.SUMMARY = saved
        self.assertEqual((row.date, row.new_buy_ok), ("2026-10-26", "False"))
        self.assertIn("스냅샷 없음", row.buy_skip_reason)

    # ---------------------------------------------------------------- 27
    def test_27_anomalies_counted_but_not_volume_zero(self):
        import run_day as RD
        snap = pd.DataFrame({"code": ["000001", "000002", "000003", "000004"], "open": [0.0, 100.0, 100.0, 100.0], "high": [0.0, 101.0, 140.0, 101.0],
                             "low": [0.0, 0.0, 99.0, 99.0], "close": [0.0, 100.0, 130.0, 100.0], "volume": [0.0, 10.0, 10.0, 10.0],
                             "amount": [0.0, 1e3, 1e3, 1e3]})
        raw = pd.DataFrame({"stk_cd": ["000001", "000002", "000003", "000004"], "upl_pric": ["+130"] * 4, "lst_pric": ["-70"] * 4})
        bad = RD.snap_anomalies(snap, raw)
        self.assertEqual(sorted(bad), ["000002", "000003"])                   # 거래량 0(거래정지)은 이상 아님
        self.assertEqual(list(RD.snap_rows(snap, raw)), ["000004"])
        res = Codex.snapshot_ready(self)
        res.ctx["amt20"] = {"000001": 5e9, "000002": 5e9, "000003": 1e9, "000004": 5e9}
        Q.snapshot = lambda caller, cs: (raw, snap.assign(dt="20261026"), {"returned": 4, "codes": 4, "seconds": 1.0})
        C.sd.universe_codes = lambda: list(snap.code)
        top = pd.DataFrame({"code": ["000004"], "score": [0.9], "raw_close": [100.0]})
        out = {"filter": {"filter_ok": True}, "eligible": pd.DataFrame({"code": ["000004"], "score": [0.9]}), "market": {}, "errors": [],
               "ledgers": {n: {"top3": top, "busy": [], "skipped_busy": [], "ledger_status": []} for n in ("strategy", "full")}}
        seen = []
        saved = E.run_day
        try:
            E.run_day = lambda pool, day, rows, *a, **k: seen.append(sorted(rows)) or out
            S.run_step(res.day, "snapshot", res.snapshot_and_plan)
        finally:
            E.run_day = saved
        self.assertEqual(seen, [["000004"]])
        f = res.day.s["same_day_failures"][-1]
        self.assertEqual(sorted(x["code"] for x in f["all"] if x["kind"] == "data_anomaly"), ["000002", "000003"])
        self.assertEqual([x["code"] for x in f["candidates"]], ["000002"])     # 후보(40억 이상)만 차단 사유
        self.assertIn("candidate_fail:스냅샷·계산 실패", res.day.s["buy_block"])


class Ops(Base):
    """상주 로그·정상 종료·켜기/끄기 (2026-10-24 운영)"""

    def log_lines(self):
        d = os.path.join(self.R.LOG_DIR, "resident")
        out = []
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                out += fh.read().splitlines()
        return out

    def test_resident_log_has_readable_lines_without_repeats(self):
        res = self.started("15:22:00")
        res.buy()
        self.fk.fill(res.om.records[0]["ord_no"], 28, 33000, "15:30:00")
        self.clk.at("15:36:00")
        S.run_step(res.day, "close_check", res.close_check)
        for i in range(3):                                                       # 같은 실패 3번 -> 한 줄
            self.clk.t += pd.Timedelta(seconds=61)
            S.run_step(res.day, "ac_positions", res.ac_positions, max_attempts=60)
        lines = self.log_lines()
        text = "\n".join(lines)
        for want in ("하루 시작(핵심)", "주문 전송: buy buy 084670 28주", "주문 ACCEPTED: buy buy 084670", "체결: buy buy 084670 28주 @33350 체결 28주 @33000",
                     "단계 시작: close_check", "단계 끝: close_check 성공"):
            self.assertIn(want, text)
        self.assertTrue(all(len(x.split(" ", 3)) == 4 and x.split(" ")[2] in ("INFO", "WARN", "ERROR") for x in lines))
        self.assertEqual(sum("단계 ac_positions FAILED" in x for x in lines), 1)
        self.assertEqual(sum("단계 시작: ac_positions" in x for x in lines), 1)
        self.assertTrue(any(" WARN " in x and "ac_positions FAILED" in x for x in lines))

    def _run_env(self):
        R = self.R
        saved = (R.RESIDENT_LOCK, R.RESIDENT_INFO, R.time.sleep)
        R.RESIDENT_LOCK, R.RESIDENT_INFO = os.path.join(self.tmp, "resident.lock"), os.path.join(self.tmp, "resident.info.json")
        R.CFG["pid_file"] = os.path.join(self.tmp, "resident.pid")
        self.addCleanup(lambda: (setattr(R, "RESIDENT_LOCK", saved[0]), setattr(R, "RESIDENT_INFO", saved[1]), setattr(R.time, "sleep", saved[2])))

    def test_stop_flag_graceful_exit_and_refuse_start(self):
        R = self._run_env() or self.R
        res = self.resident()
        res.tick = lambda t: "idle"
        R.time.sleep = lambda s: open(R.stop_flag(), "w").close()                # 첫 반복 뒤 정지 요청
        res.run()                                                                 # 반환 = 정상 종료
        self.assertTrue(any("정상 종료" in a for a in self.alerts))
        self.assertFalse(os.path.exists(R.CFG["pid_file"]))
        lk = C.SingleInstanceLock(R.RESIDENT_LOCK)
        self.assertTrue(lk.acquire())                                             # 잠금 풀림
        lk.release()
        with self.assertRaises(SystemExit) as cm:                                 # 정지 요청 파일이 있으면 시작 안 함
            self.resident().run()
        self.assertEqual(cm.exception.code, 3)

    def test_watchdog_does_not_restart_while_stopped(self):
        import common
        import watchdog as W
        saved = (dict(W.CFG), common.resident_running, W.N.send, W.LOG, W.FLAG, W.start)
        started = []
        try:
            W.CFG["log_dir"], W.CFG["state_dir"] = os.path.join(self.tmp, "logs"), os.path.join(self.tmp, "state")
            W.CFG["heartbeat_file"], W.CFG["pid_file"] = os.path.join(self.tmp, "hb.log"), os.path.join(self.tmp, "x.pid")
            W.LOG, W.FLAG = os.path.join(self.tmp, "logs", "watchdog.log"), os.path.join(self.tmp, "flag")
            common.resident_running, W.N.send, W.start = (lambda *a, **k: False), (lambda *a, **k: None), (lambda: started.append(1) or 1)
            os.makedirs(W.CFG["log_dir"], exist_ok=True)
            open(os.path.join(W.CFG["log_dir"], "resident.stop"), "w").close()
            W.main()
            self.assertEqual(started, [])
            os.remove(os.path.join(W.CFG["log_dir"], "resident.stop"))
            W.main()
            self.assertEqual(started, [1])
        finally:
            W.CFG.clear()
            W.CFG.update(saved[0])
            common.resident_running, W.N.send, W.LOG, W.FLAG, W.start = saved[1:]

    def test_control_stop_then_start(self):
        import control as K
        import watchdog as W
        saved = (dict(K.CFG), K.resident_running, K.python_procs, K.task_exists, K.RESIDENT_INFO, K.RESIDENT_LOCK, W.main, K.time.sleep)
        state = {"running": True, "calls": 0}

        def running():
            state["calls"] += 1
            return state["running"]
        try:
            K.CFG["log_dir"], K.CFG["pid_file"], K.CFG["heartbeat_file"] = self.tmp, os.path.join(self.tmp, "r.pid"), os.path.join(self.tmp, "hb.log")
            K.RESIDENT_INFO, K.RESIDENT_LOCK = os.path.join(self.tmp, "info.json"), os.path.join(self.tmp, "r.lock")
            for p in (K.RESIDENT_INFO, K.CFG["pid_file"], K.RESIDENT_LOCK):
                C.atomic_json(p, {"pid": 4242})
            K.resident_running, K.python_procs, K.task_exists = running, (lambda s: []), (lambda: False)
            K.time.sleep = lambda s: state.update(running=False)                  # 정상 종료 요청 뒤 꺼짐
            self.assertEqual(K.stop(timeout=30), 0)
            self.assertTrue(os.path.exists(K.stop_flag()))                        # 정지 상태 유지 (감시 작업이 안 켬)
            self.assertFalse(any(os.path.exists(p) for p in (K.RESIDENT_INFO, K.CFG["pid_file"], K.RESIDENT_LOCK)))   # 잠금 파일 정리
            W.main = lambda: state.update(running=True) or C.atomic_json(K.RESIDENT_INFO, {"pid": 777}) or \
                open(K.CFG["heartbeat_file"], "w").write("2026-10-24 16:00:00 pid=777 step=idle\n")
            self.assertEqual(K.start(wait=5), 0)
            self.assertFalse(os.path.exists(K.stop_flag()))                       # 켜기는 정지 요청 파일을 지우고 감시 작업 경유
        finally:
            K.CFG.clear()
            K.CFG.update(saved[0])
            K.resident_running, K.python_procs, K.task_exists, K.RESIDENT_INFO, K.RESIDENT_LOCK, W.main, K.time.sleep = saved[1:]


class Update(unittest.TestCase):
    """update.py 흐름 (가짜 git·켜기/끄기·시험, 실제 git·주문 없음)"""

    def setUp(self):
        import update as U
        self.U = U
        self.saved = (U.git, U.head, U.K.stop, U.K.start, U.run_tests, U.frozen_status, U.record, U.allowed, U.now_kst, U.K.resident_running,
                      dict(U.K.CFG))
        self.tmp = tempfile.mkdtemp()
        U.K.CFG["log_dir"] = self.tmp                                            # 정지 요청 파일도 임시 폴더로 (저장소 logs/ 를 건드리지 않음)
        U.K.resident_running = lambda: False
        self.calls, self.records = [], []
        self.state = {"head": "a" * 40, "dirty": "", "pull_rc": 0, "pulled": "b" * 40}

        def git(*args, timeout=300):
            self.calls.append(args)
            if args[0] == "status":
                return SimpleNamespace(returncode=0, stdout=self.state["dirty"], stderr="")
            if args[0] == "pull":
                if self.state["pull_rc"] == 0:
                    self.state["head"] = self.state["pulled"]
                return SimpleNamespace(returncode=self.state["pull_rc"], stdout="", stderr="fatal: Not possible to fast-forward")
            if args[0] == "reset":
                self.state["head"] = args[2]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        U.git, U.head = git, (lambda: self.state["head"])
        U.K.stop, U.K.start = (lambda *a, **k: self.calls.append(("STOP",)) or 0), (lambda *a, **k: self.calls.append(("START",)) or 0)
        U.record = lambda msg, level="INFO", notify=True: self.records.append((level, msg))
        U.allowed = lambda t=None: True
        U.frozen_status = lambda: {"ok": True, "tag_diff": [], "frozen_check": {"ok": True}}
        U.run_tests = lambda: {"ok": True, "ran": 132, "failed": "", "bad": [], "seconds": 12.0}

    def tearDown(self):
        U = self.U
        (U.git, U.head, U.K.stop, U.K.start, U.run_tests, U.frozen_status, U.record, U.allowed, U.now_kst, U.K.resident_running, cfg) = self.saved
        U.K.CFG.clear()
        U.K.CFG.update(cfg)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ops(self):
        return [c[0] for c in self.calls if c[0] in ("STOP", "START", "pull", "reset")]

    def test_refused_during_trading_hours(self):
        U = self.U
        U.allowed = self.saved[7]
        self.assertFalse(U.allowed(pd.Timestamp("2026-10-26 08:00:00")))    # 월요일 장 전 준비
        self.assertFalse(U.allowed(pd.Timestamp("2026-10-26 15:44:59")))
        self.assertTrue(U.allowed(pd.Timestamp("2026-10-26 15:45:00")))     # 장 마감 정산 후
        self.assertTrue(U.allowed(pd.Timestamp("2026-10-26 07:59:00")))
        self.assertTrue(U.allowed(pd.Timestamp("2026-10-24 10:00:00")))     # 토요일
        U.allowed = lambda t=None: False
        self.assertEqual(U.main("origin", "main"), 2)
        self.assertEqual(self.ops(), [])                                     # 아무것도 안 함

    def test_pass_pulls_tests_and_starts(self):
        self.assertEqual(self.U.main("origin", "main"), 0)
        self.assertEqual(self.ops(), ["STOP", "pull", "START"])
        self.assertIn("aaaaaaa -> bbbbbbb", self.records[-1][1])
        self.assertIn("시험 132개 통과", self.records[-1][1])

    def test_local_changes_abort_and_restart(self):
        self.state["dirty"] = " M backtest_swing/live/resident.py\n"
        self.assertEqual(self.U.main("origin", "main"), 3)
        self.assertEqual(self.ops(), ["STOP", "START"])                      # pull 안 함
        self.assertEqual(self.records[-1][0], "WARN")

    def test_unreadable_git_aborts_before_stopping(self):
        self.state["head"] = ""
        self.assertEqual(self.U.main("origin", "main"), 5)
        self.assertEqual(self.ops(), [])                                     # 끄지도 pull 하지도 않음

    def test_pull_failure_restarts_old(self):
        self.state["pull_rc"] = 1
        self.assertEqual(self.U.main("origin", "main"), 3)
        self.assertEqual(self.ops(), ["STOP", "pull", "START"])
        self.assertEqual(self.state["head"], "a" * 40)

    def test_test_failure_rolls_back_then_starts(self):
        self.U.run_tests = lambda: {"ok": False, "ran": 133, "failed": "failures=1", "bad": ["FAIL: test_x"], "seconds": 12.0}
        self.assertEqual(self.U.main("origin", "main"), 1)
        self.assertEqual(self.ops(), ["STOP", "pull", "reset", "START"])
        self.assertEqual(self.state["head"], "a" * 40)                      # 이전 커밋으로
        self.assertEqual(self.records[-1][0], "ERROR")
        self.assertIn("되돌림", self.records[-1][1])

    def test_frozen_change_does_not_start(self):
        self.U.frozen_status = lambda: {"ok": False, "tag_diff": ["backtest_swing/stage4_features.py"], "frozen_check": {"ok": False}}
        self.assertEqual(self.U.main("origin", "main"), 4)
        self.assertEqual(self.ops(), ["STOP", "pull"])                       # 켜지 않음
        self.assertIn("동결 코드", self.records[-1][1])


class Migration(unittest.TestCase):
    """서브 PC 이전 도구 (모델 재현 판정, 묶음 금지 파일·비밀 검사)"""

    def test_model_repro_verdicts(self):
        import model_repro as MR
        d = pd.to_datetime(["2026-10-16"] * 4)
        ref = pd.DataFrame({"row": range(4), "code": list("abcd"), "date": d, "seed1": [0.4, 0.3, 0.2, 0.1], "mean": [0.4, 0.3, 0.2, 0.1]})
        self.assertEqual(MR.compare(ref, ref.copy())["verdict"], "동일 (차이 0)")
        tiny = ref.assign(seed1=ref.seed1 + 1e-12, mean=ref["mean"] + 1e-12)
        self.assertTrue(MR.compare(ref, tiny)["verdict"].startswith("부동소수"))
        swap = ref.assign(seed1=[0.1, 0.3, 0.2, 0.4], mean=[0.1, 0.3, 0.2, 0.4])
        r = MR.compare(ref, swap)
        self.assertEqual((r["verdict"], r["top3_all_same"]), ("다름", False))

    def test_bundle_forbidden_names_and_digit_boundary(self):
        import zipfile
        import make_bundle as MB
        for n in (".env", "kiwoom/.env", ".env.local", ".kiwoom_token_cache.json", "x/id_ed25519"):
            self.assertTrue(MB.FORBIDDEN.search(n), n)
        for n in ("data/daily_raw/005930.parquet", "models_live/stage5/2026-10/seed1.txt", "logs/exec/ledger.json"):
            self.assertFalse(MB.FORBIDDEN.search(n), n)
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        z = os.path.join(tmp, "b.zip")
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("models_live/stage5/2026-10/seed1.txt", "threshold=0.1234567890123 leaf_value=5")
            f.writestr("logs/exec/state/x.json", '{"chat": "1234567890"}')
        saved = MB.env_values
        MB.env_values = lambda: ["1234567890"]
        try:
            hits = MB.scan_zip(z)["hits"]
        finally:
            MB.env_values = saved
        self.assertEqual(hits, ["logs/exec/state/x.json: .env 값 일치"])         # 긴 실수 안의 숫자는 일치 아님, 따로 있는 값은 잡음


if __name__ == "__main__":
    unittest.main(verbosity=2)
