"""
11V 시험 (첫 주 넓은 리뷰 <커밋>, 무인 운영):
P1-5 감시 작업: 알림 보내기 실패도 "알림 완료"로 써서 다시 안 보냄, 잠금은 살아 있는데 하트비트 없음·읽기 실패는 알림 없음
P1-6 15:35 취소: NOT_SENT(유량·인증) 두 번이면 더 안 보냄
(a) 전날 공식 시세 확인(KRX 30초 시간 초과)이 주 반복을 막음, 08:00 정각 첫 시도 -> 08:03
(b) 같은 [오류] 알림 반복 -> 처음 1번 + 10분마다 요약, 그치면 1번
(c) OPENBLAS_NUM_THREADS 를 setdefault 대신 강제 1 (상주·engine·features_eod·monthly)
(d) 디스크 여유: 10GB 또는 5% 아래 알림, 2GB 아래 그날 신규 매수만 막음
가짜 서버·가짜 시계·임시 폴더, 실제 키움·KRX·텔레그램 호출·data·logs 없음.

  python -m unittest backtest_swing/live/test_fix11v.py -v
"""
import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import testguard; testguard.install()  # noqa: E401,E402,E702  (첫 줄 유지)
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_codex import Base  # noqa: E402
from test_broad2 import setup  # noqa: E402
import notify as N  # noqa: E402
import orders as O  # noqa: E402


# ---------------------------------------------------------------- P1-5

class Watchdog(unittest.TestCase):
    def setUp(self):
        import common
        import watchdog as W
        self.W, self.common = W, common
        self.tmp = tempfile.mkdtemp()
        saved = (dict(W.CFG), W.LOOP_FLAG, W.FLAG, W.LOG, W.start, W.N.send, common.resident_running)

        def restore():
            (cfg, W.LOOP_FLAG, W.FLAG, W.LOG, W.start, W.N.send, common.resident_running) = saved
            W.CFG.clear()
            W.CFG.update(cfg)
            shutil.rmtree(self.tmp, ignore_errors=True)
        self.addCleanup(restore)
        W.CFG.update(log_dir=self.tmp, state_dir=self.tmp, heartbeat_file=os.path.join(self.tmp, "hb.log"), pid_file=os.path.join(self.tmp, "r.pid"))
        W.LOOP_FLAG, W.FLAG, W.LOG = os.path.join(self.tmp, "loop.flag"), os.path.join(self.tmp, "hb.flag"), os.path.join(self.tmp, "w.log")
        W.start = lambda: 4242
        self.sent, self.ok = [], [False]
        W.N.send = lambda msg, **k: self.sent.append(msg) or self.ok[0]
        self.running = [True]
        common.resident_running = lambda *a, **k: self.running[0]

    def hb(self, line):
        with open(self.W.CFG["heartbeat_file"], "w", encoding="utf-8") as f:
            f.write(line + "\n")

    def info(self, started):
        with open(os.path.join(self.tmp, "resident.info.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": 777, "started": f"{started:%Y-%m-%d %H:%M:%S}"}, f)

    def test_failed_stale_alert_retried(self):
        self.hb(f"{pd.Timestamp.now() - pd.Timedelta(minutes=20):%Y-%m-%d %H:%M:%S} pid=777 step=idle loop=1s")
        self.W.main()
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(os.path.exists(self.W.FLAG))                           # 예전: 실패해도 완료 표시 -> 다시 안 보냄
        self.ok[0] = True
        self.W.main()
        self.W.main()
        self.assertEqual(len(self.sent), 2)                                      # 다음 회차에 다시, 성공 뒤엔 한 번만
        self.assertTrue(os.path.exists(self.W.FLAG))

    def test_failed_loop_stall_alert_retried(self):
        self.hb(f"{pd.Timestamp.now():%Y-%m-%d %H:%M:%S} pid=777 step=close_calc loop=660s")
        self.W.main()
        self.ok[0] = True
        self.W.main()
        self.W.main()
        self.assertEqual([m[:9] for m in self.sent], ["[주 반복 멈춤]"] * 2)

    def test_missing_heartbeat_alert_after_grace(self):
        self.info(pd.Timestamp.now() - pd.Timedelta(minutes=1))                  # 막 켜짐 = 유예 안
        self.ok[0] = True
        self.W.main()
        self.assertEqual(self.sent, [])
        self.info(pd.Timestamp.now() - pd.Timedelta(minutes=10))
        with open(self.W.st("nohb_since"), "w") as f:
            f.write(f"{pd.Timestamp.now() - pd.Timedelta(minutes=10):%Y-%m-%d %H:%M:%S}")
        self.W.main()
        self.W.main()
        self.assertEqual(len(self.sent), 1)                                      # 예전: 알림 없이 "ok ... heartbeat None"
        self.assertIn("[하트비트 없음]", self.sent[0])
        self.hb("깨진 줄")                                                        # 읽을 수 없는 줄도 없음과 같음 (이미 알림 = 다시 안 보냄)
        self.W.main()
        self.assertEqual(len(self.sent), 1)
        self.hb(f"{pd.Timestamp.now():%Y-%m-%d %H:%M:%S} pid=777 step=idle loop=1s")
        self.W.main()
        self.assertFalse(os.path.exists(self.W.st("nohb_flag")) or os.path.exists(self.W.st("nohb_since")))   # 다시 보이면 해제

    def test_restart_alert_kept_when_send_fails(self):
        self.running[0] = False
        self.W.main()
        self.assertEqual(len(json.load(open(self.W.st("pending"), encoding="utf-8"))), 1)
        self.running[0], self.ok[0] = True, True
        self.hb(f"{pd.Timestamp.now():%Y-%m-%d %H:%M:%S} pid=4242 step=idle loop=1s")
        self.W.main()
        self.assertTrue(self.sent[-1].startswith("[지난 미전송] ") and "[감시] 상주 프로그램이 없어 다시 켬" in self.sent[-1])
        self.assertFalse(os.path.exists(self.W.st("pending")))


class Send(unittest.TestCase):
    def test_send_sender_error_is_false(self):
        self.assertFalse(N.send("x", sender=lambda t: (_ for _ in ()).throw(RuntimeError("down"))))
        self.assertFalse(N.send("x", sender=lambda t: None))
        self.assertTrue(N.send("x", sender=lambda t: True))


# ---------------------------------------------------------------- P1-6

class Cancel(unittest.TestCase):
    def setUp(self):
        self.clk, self.fk, self.om, _ = setup("2026-10-26 15:35:10")
        self.res = []
        self.om.b.cancel = lambda no, code, q: (self.res.pop(0) if self.res else {"outcome": O.ACCEPTED, "ord_no": "9"})
        self.r = {"ord_no": "1001", "code": "990010", "kind": "본래"}

    def tries(self):
        return [i["status"] for i in self.om.records if i["stage"] == "cancel"]

    def test_not_sent_not_counted_retried_after_gap(self):
        ns = {"outcome": O.NOT_SENT, "cause": "HTTP 429 유량 초과", "not_sent_kind": "rate"}
        self.res = [dict(ns), dict(ns)]
        self.om._cancel(self.r, 5, [])
        self.clk.t += pd.Timedelta(seconds=10)
        self.om._cancel(self.r, 5, [])                                           # 30초 안 = 다시 안 보냄
        self.assertEqual(self.tries(), [O.NOT_SENT])
        self.clk.t += pd.Timedelta(seconds=25)
        self.om._cancel(self.r, 5, [])
        self.clk.t += pd.Timedelta(seconds=31)
        self.om._cancel(self.r, 5, [])                                           # 예전: NOT_SENT 두 번이면 더 안 보냄
        self.assertEqual(self.tries(), [O.NOT_SENT, O.NOT_SENT, O.ACCEPTED])

    def test_rejected_counted_twice(self):
        rj = {"outcome": O.REJECTED, "return_code": 1, "msg": "거부"}
        self.res = [dict(rj), dict(rj), dict(rj)]
        for _ in range(3):
            self.om._cancel(self.r, 5, [])
            self.clk.t += pd.Timedelta(seconds=61)
        self.assertEqual(self.tries(), [O.REJECTED, O.REJECTED])


# ---------------------------------------------------------------- (a)·(d)·(b) 상주 프로그램

class Resident(Base):
    def test_a_official_probe_does_not_block_loop(self):
        from kiwoom import krx_official
        import daily
        res = self.started("08:05:30", live=False)
        res.day.mark("data", {"official_missing_dates": ["2026-10-23"], "attempt": "08:03:00"})
        gate, calls = threading.Event(), []
        saved = (krx_official.fetch_day, daily.has_pending, daily.reapply_official)
        self.addCleanup(lambda: (gate.set(), setattr(krx_official, "fetch_day", saved[0]), setattr(daily, "has_pending", saved[1]),
                                 setattr(daily, "reapply_official", saved[2])))
        krx_official.fetch_day = lambda d: calls.append(d) or gate.wait(10) and pd.DataFrame({"x": [1]})
        daily.has_pending = lambda: True
        daily.reapply_official = lambda: {"ok": True, "stocks": 2700}
        asked = []
        res.request_prep = asked.append
        t0 = time.time()
        th = threading.Thread(target=res.retry_official, daemon=True)
        th.start()
        th.join(2)
        self.assertFalse(th.is_alive())                                          # 예전: KRX 응답(시간 초과 30초)까지 주 반복이 섬
        self.assertLess(time.time() - t0, 2.5)
        res.retry_official()                                                     # 확인 중 = 기다리지 않고 돌아옴, 새로 시작 안 함
        self.assertEqual(len(calls), 1)
        gate.set()
        res.redo_thread.join(5)
        res.retry_official()                                                     # 다음 tick 에 반영
        d = res.day.s
        self.assertEqual((d["official_retries"][-1]["available"], d["official_reapply"][-1]["stocks"]), (True, 2700))
        self.assertEqual(d["steps"]["data"]["official_missing_dates"], [])
        self.assertEqual(asked, ["공식 시세 반영 뒤 다시 적재 (보관 행 2700종목 보정)"])

    def test_a_fast_retry_from_0803(self):
        from kiwoom import krx_official
        saved = krx_official.fetch_day
        self.addCleanup(setattr, krx_official, "fetch_day", saved)
        calls = []
        krx_official.fetch_day = lambda d: calls.append(d) or pd.DataFrame()
        res = self.started("08:01:30", live=False)
        res.day.mark("data", {"official_missing_dates": ["2026-10-23"], "attempt": "07:59:00"})
        res.retry_official()
        self.assertEqual(calls, [])                                              # 예전: 08:00 부터 2분 간격 -> 08:01:30 시도
        self.clk.at("08:09:00")
        res.retry_official()
        res.redo_thread.join(5)
        self.assertEqual(len(calls), 1)

    def disk(self, free_gb, total_gb=500):
        return lambda path: SimpleNamespace(total=total_gb * 1e9, used=(total_gb - free_gb) * 1e9, free=free_gb * 1e9)

    def test_d_disk_warn_and_block(self):
        res = self.started("07:00:30", live=False)
        res.disk_usage = self.disk(200)
        self.assertEqual(res.disk_check("하루 시작")["level"], 0)
        self.assertEqual(self.alerts, [])
        res.disk_usage = self.disk(8)
        res.disk_check("장후")
        res.disk_check("장후")
        self.assertEqual(len([a for a in self.alerts if a.startswith("[디스크 여유 적음]")]), 1)   # 그날 한 번
        self.assertNotIn("disk_low", res.day.s.get("buy_block") or [])
        res.disk_usage = self.disk(1.5)
        res.disk_check("하루 시작")
        self.assertIn("disk_low", res.day.s["buy_block"])                       # 신규 매수만 막음 (손절·매도 경로는 buy_block 을 안 봄)
        self.assertTrue(any(a.startswith("[디스크 부족]") for a in self.alerts))
        res.disk_usage = self.disk(30, total_gb=1000)                            # 30GB 여도 3% = 알림 (이미 더 나쁜 단계 알림 뒤라 안 보냄)
        self.assertEqual(res.disk_check("장후")["level"], 1)

    def test_d_day_start_checks_when_running(self):
        res = self.resident()
        res.auto_disk, res.disk_usage = True, self.disk(1.0)
        self.clk.at("07:00:30")
        res.tick(self.clk())
        self.assertEqual(res.day.s["disk"][0]["where"], "하루 시작")
        self.assertIn("disk_low", res.day.s["buy_block"])

    def test_b_error_alerts_grouped(self):
        R = self.R
        alert = self.saved[7]                                                    # 진짜 alert (Base 가 바꿔 둔 것 말고)
        got = []
        saved = (N.send_async, R.REPEAT, R.now_kst)
        self.addCleanup(lambda: (setattr(N, "send_async", saved[0]), setattr(R, "REPEAT", saved[1]), setattr(R, "now_kst", saved[2])))
        N.send_async = lambda msg, **k: got.append(msg)
        R.REPEAT = N.Repeat(10)
        t = [pd.Timestamp("2026-10-26 08:00:00")]
        R.now_kst = lambda: t[0]
        for i in range(20):                                                      # 30초마다 같은 오류 (숫자만 다름)
            alert(f"[오류] official_retry 오류: ReadTimeout(read timeout=30, {i})")
            alert(f"[손절 진행 중] 00{i:04d} 현재가 80 < 손절가 90")             # 종목별 알림은 안 묶음
            t[0] += pd.Timedelta(seconds=30)
        self.assertEqual(len([m for m in got if m.startswith("[오류]")]), 1)    # 예전: 20번
        self.assertEqual(len([m for m in got if m.startswith("[손절 진행 중]")]), 20)
        R.alert = alert
        R.alert_repeats()                                                        # 10분 지남 -> 요약 1번
        self.assertTrue(got[-1].startswith("[오류 반복] 같은 오류 19번 더") and "계속 중" in got[-1])
        t[0] += pd.Timedelta(minutes=10)
        R.alert_repeats()
        self.assertTrue(got[-1].startswith("[오류 그침]"))
        t[0] += pd.Timedelta(minutes=1)
        alert("[오류] official_retry 오류: ReadTimeout(read timeout=30, 99)")      # 그친 뒤 다시 나오면 처음처럼 바로
        self.assertTrue(got[-1].startswith("[오류] official_retry"))


# ---------------------------------------------------------------- (c)

class Openblas(unittest.TestCase):
    def test_forced_before_numpy(self):
        for fn in ("resident.py", "engine.py", "features_eod.py", "monthly.py"):
            src = open(os.path.join(HERE, fn), encoding="utf-8").read()
            m = re.search(r'^os\.environ\["OPENBLAS_NUM_THREADS"\] = "1"', src, re.M)
            self.assertIsNotNone(m, fn)                                          # 예전: setdefault (밖에서 다른 값이면 그대로) 또는 없음
            self.assertLess(m.start(), src.index("\nimport numpy"), fn)


if __name__ == "__main__":
    unittest.main()
