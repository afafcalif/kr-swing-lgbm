"""
11M 운영 데이터 백업(backup.py)·장후 특징값 저장(features_eod.py) 시험. 모두 임시 폴더 (저장소의 logs/·data/·models_live/ 를 건드리지 않음),
가짜 시계·가짜 알림, 키움·네트워크 없음.

  python -m unittest backtest_swing/live/test_backup.py -v
"""
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"  # 11K 보완 8: 시험은 로컬 config.json 을 읽지 않음 (config.example.json 만)
import json
import shutil
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backup as B  # noqa: E402
import settings  # noqa: E402

TIMES = json.load(open(settings.EXAMPLE, encoding="utf-8"))["times"]
FAKE_VAL = "Zx9FakeValue_abcdefghijklmnopqrstuvwxyz0123"   # 가짜 .env 값 (시험용)
ACCOUNT = "81234567"
D = "2026-10-28"  # 수요일, 거래일


def put(root, rel, text="x", binary=None):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(binary if binary is not None else text.encode("utf-8"))
    return p


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Clock:
    def __init__(self, t):
        self.t = pd.Timestamp(t)

    def __call__(self):
        return self.t


class Env(unittest.TestCase):
    """서브 PC 모양의 임시 저장소 + 임시 대상 폴더"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "repo")
        self.dest = os.path.join(self.tmp, "share")
        r = self.root
        put(r, ".env", f"APP_KEY=KeyValue_0123456789abcdef\nAPP_SECRET={FAKE_VAL}\nACCOUNT_NO={ACCOUNT}\nKIWOOM_TOKEN_CACHE_PATH=.kiwoom_token.json\n"
                       f"KIWOOM_ENV=mock\nTELEGRAM_CHAT_ID=1234567890\n")
        L = "logs/exec"
        put(r, f"{L}/summary.csv", "date,x\n2026-10-28,1\n")
        put(r, f"{L}/ledger.json", '{"v": "ledger-1028"}')
        put(r, f"{L}/state/positions.json", '{"v": "pos-1028"}')
        put(r, f"{L}/state/positions.json.bak", '{"v": "pos-1027"}')
        put(r, f"{L}/state/{D}.json", '{"steps": {}}')
        put(r, f"{L}/state/intents/{D}.json", '{"intents": []}')
        put(r, f"{L}/{D}.json", '{"date": "2026-10-28"}')
        put(r, f"{L}/{D}_rerun.json", '{"rerun": {}}')
        put(r, f"{L}/2026-10-27.json", '{"date": "2026-10-27"}')
        put(r, f"{L}/resident/{D}.log", "15:21 매수 접수\n")
        put(r, f"{L}/alerts.log", "알림\n")
        put(r, f"{L}/scores/{D}.parquet", binary=b"PAR1fake")
        put(r, f"{L}/features/{D}.parquet", binary=b"PAR1fake")
        # 제외될 것
        put(r, f"{L}/resident.lock", "")
        put(r, f"{L}/resident.pid", "123")
        put(r, f"{L}/config.json", "{}")
        put(r, f"{L}/.env.bak", "APP_KEY=x")
        put(r, f"{L}/kiwoom_token_cache.json", "{}")
        put(r, f"{L}/state/x.json.1234.tmp", "{}")
        # 일봉 추가분 버전 (원본 3개), 모델, 데이터셋, 종목 목록
        for v in ("20261026070001000000", "20261027070001000000", "20261028070001000000"):
            put(r, f"data/live_append/versions/{v}/update.json", json.dumps({"data_version": v}))
            put(r, f"data/live_append/versions/{v}/adj.parquet", binary=b"PAR1" + v.encode())
        put(r, "data/live_append/CURRENT.json", '{"version": "20261028070001000000"}')
        put(r, "models_live/stage5/2026-10/seed1.txt", f"tree\nleaf_value=0.{ACCOUNT}1\n{FAKE_VAL}\n")  # 모델 파일은 검사 안 함
        put(r, "models_live/stage5/2026-11.new/seed1.txt", "학습 중")
        put(r, "models_live/datasets/2026-10/dataset.parquet", binary=b"PAR1ds")
        put(r, "models_live/datasets/2026-10/sim.npz", binary=b"npz")
        put(r, "models_live/datasets/2026-11.new/dataset.parquet", binary=b"PAR1new")
        put(r, "data/universe_20261019.csv", "code,name\n005930,a\n")
        put(r, "data/universe_current.json", '{"items": []}')
        put(r, "data/daily_ext/005930.parquet", binary=b"PAR1")
        put(r, "data/krx_daily/20261027.parquet", binary=b"PAR1")
        self.sent, self.feat_calls = [], []
        self.clock = Clock(f"{D} 18:00:00")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_dest(self, marker=True):
        os.makedirs(self.dest, exist_ok=True)
        if marker:
            put(self.dest, B.MARKER, "")

    def bk(self, dest=None, features=None):
        def feat():
            self.feat_calls.append(self.clock())
            if features:
                return features()
            return {"rc": 0}
        return B.Backup(self.dest if dest is None else dest, root=self.root, times=TIMES, now=self.clock, send=self.sent.append,
                        run_features=feat)

    def mirror_rel(self):
        base = os.path.join(self.dest, "mirror")
        out = set()
        for cur, _, files in os.walk(base):
            out |= {os.path.relpath(os.path.join(cur, f), base).replace("\\", "/") for f in files}
        return out


class Marker(Env):
    def test_no_marker_no_copy(self):
        """대상 폴더가 있어도 표지 파일이 없으면 아무것도 복사하지 않음 (실패로 기록, 그날 사본은 로컬 대기 폴더에)"""
        self.make_dest(marker=False)
        r = self.bk().run()
        self.assertEqual(r["status"], "failed")
        self.assertIn(B.MARKER, r["reason"])
        self.assertEqual(os.listdir(self.dest), [])
        self.assertTrue(os.path.exists(os.path.join(self.root, "logs/exec/backup_pending", D, "ledger.json")))

    def test_no_dest_setting(self):
        r = self.bk(dest="").run()
        self.assertEqual(r["status"], "failed")
        self.assertIn("설정 없음", r["reason"])


class PendingThenMove(Env):
    def test_unreachable_pending_then_moved(self):
        """못 닿은 날: 로컬 logs/exec/backup_pending/{날짜}/ 에 그날 상태 사본 -> 다음 날 장부가 덮어써져도 -> 닿는 날 그 사본을 daily/{날짜}/ 로 옮김"""
        r = self.bk().run()  # 대상 폴더 없음 (메인 PC 꺼짐)
        self.assertEqual(r["status"], "failed")
        pend = os.path.join(self.root, "logs/exec/backup_pending", D)
        self.assertEqual(read(os.path.join(pend, "ledger.json")), '{"v": "ledger-1028"}')
        self.assertTrue(os.path.exists(os.path.join(pend, "state/intents", f"{D}.json")))
        self.assertTrue(os.path.exists(os.path.join(pend, f"{D}_rerun.json")))
        self.assertFalse(os.path.exists(os.path.join(pend, "2026-10-27.json")))  # 다른 날 로그는 그날 사본에 안 들어감
        man = jload(os.path.join(pend, "MANIFEST.json"))
        self.assertEqual(man["day"], D)
        # 다음 날(10-29) 장중에 장부·보유 장부가 덮어써짐, 18:00 에 메인 PC 켜져 있음
        put(self.root, "logs/exec/ledger.json", '{"v": "ledger-1029"}')
        put(self.root, "logs/exec/state/positions.json", '{"v": "pos-1029"}')
        self.clock.t = pd.Timestamp("2026-10-29 18:00:00")
        self.make_dest()
        r = self.bk().run()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["moved"], [D, "2026-10-29"])
        self.assertEqual(read(os.path.join(self.dest, "daily", D, "ledger.json")), '{"v": "ledger-1028"}')  # 그날 상태
        self.assertEqual(read(os.path.join(self.dest, "daily", D, "state/positions.json")), '{"v": "pos-1028"}')
        self.assertEqual(read(os.path.join(self.dest, "daily", "2026-10-29", "ledger.json")), '{"v": "ledger-1029"}')
        self.assertEqual(read(os.path.join(self.dest, "mirror/logs/exec/ledger.json")), '{"v": "ledger-1029"}')  # 미러는 지금 것
        self.assertFalse(os.path.exists(os.path.join(self.root, "logs/exec/backup_pending", D)))  # 옮긴 뒤 로컬 대기 사본 지움
        self.assertTrue(os.path.exists(os.path.join(self.dest, "daily", D, "MANIFEST.json")))
        self.assertTrue(os.path.exists(os.path.join(self.dest, "manifests", "2026-10-29.json")))
        self.assertEqual([x for x in self.sent if "실패" in x], [])

    def test_capture_once_per_day(self):
        """같은 날 다시 실행해도 그날 사본은 처음 것 (덮어쓰지 않음)"""
        self.make_dest()
        self.bk().run()
        put(self.root, "logs/exec/ledger.json", '{"v": "changed"}')
        self.clock.t = pd.Timestamp(f"{D} 21:00:00")
        r = self.bk().run()
        self.assertEqual(r["capture"]["skip"], "이미 만든 날")
        self.assertEqual(read(os.path.join(self.dest, "daily", D, "ledger.json")), '{"v": "ledger-1028"}')

    def test_label_day(self):
        cal = B.Calendar(times=TIMES)
        self.assertEqual(cal.label_day("2026-10-28 18:00"), pd.Timestamp("2026-10-28"))
        self.assertEqual(cal.label_day("2026-10-29 05:00"), pd.Timestamp("2026-10-28"))  # 다음 날 07:00 전 = 아직 그날 상태
        self.assertEqual(cal.label_day("2026-10-31 12:00"), pd.Timestamp("2026-10-30"))  # 토요일 -> 금요일
        self.assertEqual(cal.label_day("2026-11-19 17:00"), pd.Timestamp("2026-11-18"))  # 수능일 하루 끝 17:30 전


class FailAlert(Env):
    def test_three_days_alert(self):
        """실패 1·2일째는 알림 없음, 3일째 한 번 (같은 날 다시 실패해도 한 번), 다시 되면 복구 알림"""
        for i, day in enumerate(("2026-10-28", "2026-10-29", "2026-10-30")):
            self.clock.t = pd.Timestamp(f"{day} 18:00:00")
            r = self.bk().run()
            self.assertEqual(r["fail_days"], i + 1)
            alerts = [x for x in self.sent if "백업 실패" in x]
            self.assertEqual(len(alerts), 0 if i < 2 else 1, (day, alerts))
        self.clock.t = pd.Timestamp("2026-10-30 21:00:00")
        self.bk().run()
        self.assertEqual(len([x for x in self.sent if "백업 실패" in x]), 1)
        self.assertIn("3일 연속", [x for x in self.sent if "백업 실패" in x][0])
        self.clock.t = pd.Timestamp("2026-10-31 18:00:00")
        self.make_dest()
        r = self.bk().run()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["moved"], ["2026-10-28", "2026-10-29", "2026-10-30"])  # 밀린 것 한꺼번에
        self.assertEqual(len([x for x in self.sent if "다시 됨" in x]), 1)
        self.assertEqual(jload(os.path.join(self.root, "logs/exec/backup_state.json"))["fail_dates"], [])

    def test_two_days_then_ok_no_alert(self):
        for day in ("2026-10-28", "2026-10-29"):
            self.clock.t = pd.Timestamp(f"{day} 18:00:00")
            self.bk().run()
        self.make_dest()
        self.clock.t = pd.Timestamp("2026-10-30 18:00:00")
        self.assertEqual(self.bk().run()["status"], "ok")
        self.clock.t = pd.Timestamp("2026-11-02 18:00:00")
        shutil.rmtree(self.dest)
        self.assertEqual(self.bk().run()["fail_days"], 1)  # 성공하면 연속 실패가 다시 1 부터
        self.assertEqual([x for x in self.sent if "백업" in x and "실패" in x], [])


class Exclusions(Env):
    def test_mirror_sets_and_filters(self):
        self.make_dest()
        r = self.bk().run()
        self.assertEqual(r["status"], "ok")
        got = self.mirror_rel()
        for need in ("logs/exec/summary.csv", "logs/exec/ledger.json", "logs/exec/state/positions.json", f"logs/exec/state/intents/{D}.json",
                     f"logs/exec/resident/{D}.log", f"logs/exec/scores/{D}.parquet", f"logs/exec/features/{D}.parquet",
                     "data/live_append/CURRENT.json", "data/live_append/versions/20261028070001000000/adj.parquet",
                     "models_live/stage5/2026-10/seed1.txt", "models_live/datasets/2026-10/dataset.parquet",
                     "data/universe_20261019.csv", "data/universe_current.json"):
            self.assertIn(need, got)
        for p in got:
            name = p.rsplit("/", 1)[-1].lower()
            self.assertFalse(name.startswith(".env") or "token" in name or name == "config.json" or name.endswith((".pid", ".lock", ".tmp", ".kr_tmp")), p)
            self.assertFalse(".new/" in p or p.endswith("sim.npz") or "backup_pending" in p, p)
            self.assertTrue(p.startswith(("logs/exec/", "data/live_append/", "models_live/stage5/", "models_live/datasets/", "data/universe_")), p)
        self.assertNotIn("data/daily_ext/005930.parquet", got)
        self.assertNotIn("data/krx_daily/20261027.parquet", got)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "mirror", ".env")))

    def test_changed_only_and_no_delete(self):
        """두 번째 실행은 바뀐 파일만 복사, 원본에서 지운 파일은 대상에 그대로, 임시 이름 파일이 남지 않음"""
        self.make_dest()
        self.bk().run()
        put(self.root, "logs/exec/alerts.log", "알림\n알림2\n")
        os.remove(os.path.join(self.root, "logs/exec/2026-10-27.json"))
        self.clock.t = pd.Timestamp(f"{D} 19:00:00")
        r = self.bk().run()
        # 바뀐 것: alerts.log, backup.log·backup_state.json·backup_index.json (백업 자신의 기록)
        self.assertLessEqual(r["mirror"]["copied"], 4)
        self.assertEqual(read(os.path.join(self.dest, "mirror/logs/exec/alerts.log")), "알림\n알림2\n")
        self.assertTrue(os.path.exists(os.path.join(self.dest, "mirror/logs/exec/2026-10-27.json")))
        left = [f for cur, _, fs in os.walk(self.dest) for f in fs if f.endswith(".kr_tmp")]
        self.assertEqual(left, [])
        src = os.path.join(self.root, "data/live_append/versions/20261028070001000000/adj.parquet")
        dst = os.path.join(self.dest, "mirror/data/live_append/versions/20261028070001000000/adj.parquet")
        self.assertAlmostEqual(os.path.getmtime(src), os.path.getmtime(dst), delta=2)

    def test_name_filter(self):
        for p in (".env", ".env.local", "kiwoom_token_cache.json", "x/token/y.json", "config.json", "resident.pid", "resident.lock", "a.json.1.tmp"):
            self.assertTrue(B.excluded(p), p)
        for p in ("ledger.json", "state/positions.json", "config.example.json", "summary.csv"):
            self.assertFalse(B.excluded(p), p)


class Secrets(Env):
    def test_log_with_secret_skipped_and_alerted(self):
        put(self.root, "logs/exec/resident.out.log", f"Traceback ... appkey {FAKE_VAL} ...\n")
        put(self.root, f"logs/exec/state/{D}.json", json.dumps({"acct": ACCOUNT}))  # 그날 사본 파일에도 검사
        self.make_dest()
        r = self.bk().run()
        self.assertEqual(r["status"], "ok")
        got = self.mirror_rel()
        self.assertNotIn("logs/exec/resident.out.log", got)
        self.assertNotIn(f"logs/exec/state/{D}.json", got)
        self.assertIn("logs/exec/resident.out.log", r["mirror"]["skipped_secret"])
        self.assertIn(f"state/{D}.json", r["capture"]["skipped_secret"])
        self.assertFalse(os.path.exists(os.path.join(self.dest, "daily", D, "state", f"{D}.json")))
        self.assertTrue(any("resident.out.log" in x and "비밀 값" in x for x in self.sent))
        self.assertFalse(any(FAKE_VAL in x or ACCOUNT in x for x in self.sent))  # 알림에 값을 쓰지 않음
        log = read(os.path.join(self.root, "logs/exec/backup.log"))
        self.assertNotIn(FAKE_VAL, log)
        self.assertIn("models_live/stage5/2026-10/seed1.txt", got)  # 모델 파일은 검사 안 함 (그대로 복사)
        # 같은 내용이면 다시 알리지 않음
        n = len(self.sent)
        self.clock.t = pd.Timestamp(f"{D} 20:00:00")
        self.bk().run()
        self.assertEqual(len([x for x in self.sent[n:] if "resident.out.log" in x]), 0)

    def test_values_from_env(self):
        vals = B.env_secret_values([os.path.join(self.root, ".env")])
        self.assertIn(FAKE_VAL, vals)
        self.assertIn(ACCOUNT, vals)
        self.assertNotIn(".kiwoom_token.json", vals)  # 경로 값은 뺌
        self.assertNotIn("mock", vals)
        self.assertNotIn("1234567890", vals)  # 채팅 ID 는 키·시크릿·계좌·토큰이 아님
        m = B.secret_matchers([ACCOUNT])
        self.assertTrue(any(x.search(f"계좌 {ACCOUNT} 조회".encode()) for x in m))
        self.assertFalse(any(x.search(f"0.{ACCOUNT}1".encode()) for x in m))  # 긴 숫자 안에 우연히 들어간 것은 아님


class Session(Env):
    def test_in_session_exit(self):
        """거래일 07:00 ~ 16:30 에는 아무것도 안 함 (특징값 계산도 안 함)"""
        self.make_dest()
        for t in (f"{D} 07:00:00", f"{D} 12:00:00", f"{D} 16:29:59"):
            self.clock.t = pd.Timestamp(t)
            self.assertEqual(self.bk().run()["status"], "in_session", t)
        self.assertEqual(self.feat_calls, [])
        self.assertEqual(sorted(os.listdir(self.dest)), [B.MARKER])
        self.assertFalse(os.path.exists(os.path.join(self.root, "logs/exec/backup_pending")))
        for t in (f"{D} 06:59:59", f"{D} 16:30:00", "2026-10-31 12:00:00"):  # 장 전·하루 끝 뒤·토요일
            self.clock.t = pd.Timestamp(t)
            self.assertEqual(self.bk().run()["status"], "ok", t)

    def test_special_day(self):
        """수능일(2026-11-19 폐장 16:30)은 하루 끝도 1시간 늦게 (17:30)"""
        cal = B.Calendar(times=TIMES)
        self.assertTrue(cal.in_session("2026-11-19 17:29:00"))
        self.assertFalse(cal.in_session("2026-11-19 17:30:00"))
        self.assertTrue(cal.in_session("2026-11-18 16:29:00"))
        self.assertFalse(cal.in_session("2026-11-18 16:31:00"))
        self.assertFalse(cal.in_session("2026-12-31 12:00:00"))  # 연말 휴장일 (krx_holidays.json)

    def test_features_first_and_failure_ignored(self):
        """특징값 저장을 먼저 부르고, 실패(예외)해도 백업은 계속"""
        self.make_dest()
        order = []

        def boom():
            order.append(os.path.exists(os.path.join(self.dest, "mirror")))
            raise RuntimeError("특징값 실패")
        r = self.bk(features=boom).run()
        self.assertEqual(order, [False])  # 복사 전에 불림
        self.assertEqual(r["status"], "ok")
        self.assertIn("특징값 실패", r["features"]["error"])


class Versions(Env):
    def test_thin(self):
        """대상: 최근 10거래일 + 월말 버전 + CURRENT 만 남김. 원본 버전은 그대로"""
        self.make_dest()
        vd = os.path.join(self.dest, "mirror/data/live_append/versions")
        old = ["20260828070000000000", "20260831070000000000", "20260831150000000000",  # 8월: 31일 15시가 월말
               "20260915070000000000", "20260930070000000000",                          # 9월: 30일
               "20261001070000000000", "20261012070000000000", "20261013070000000000",
               "20261014070000000000", "20261015070000000000", "20261016070000000000", "20261016150000000000"]
        for v in old:
            put(vd, f"{v}/update.json", "{}")
        put(vd, "readme.txt", "not a version")
        r = self.bk().run()
        left = sorted(os.listdir(vd))
        # 10-28 기준 최근 10거래일 = 10-15 ~ 10-28 (10-15·16·19·20·21·22·23·26·27·28)
        expect = {"20260831150000000000", "20260930070000000000", "20261015070000000000", "20261016070000000000", "20261016150000000000",
                  "20261026070001000000", "20261027070001000000", "20261028070001000000", "readme.txt"}
        self.assertEqual(set(left), expect)
        self.assertEqual(set(r["thinned"]), set(old) - expect)
        self.assertEqual(len(os.listdir(os.path.join(self.root, "data/live_append/versions"))), 3)  # 원본 그대로
        man = jload(os.path.join(self.dest, "manifests", f"{D}.json"))
        self.assertFalse(any("20261012" in e["path"] for e in man["files"]))

    def test_keep_from(self):
        cal = B.Calendar(times=TIMES)
        self.assertEqual(cal.keep_from("2026-10-28"), pd.Timestamp("2026-10-15"))
        self.assertEqual(cal.keep_from("2026-11-01"), pd.Timestamp("2026-10-19"))  # 일요일 -> 금요일부터 10거래일


class Verify(Env):
    def test_verify(self):
        self.make_dest()
        self.bk().run()
        b = self.bk()
        r = b.verify()
        self.assertTrue(r["ok"], r)
        self.assertGreater(r["checked"], 20)
        self.assertEqual(b.verify(D)["ok"], True)
        put(self.dest, "mirror/models_live/stage5/2026-10/seed1.txt", "망가짐")
        os.remove(os.path.join(self.dest, "daily", D, "ledger.json"))
        r = b.verify()
        self.assertFalse(r["ok"])
        self.assertIn("mirror/models_live/stage5/2026-10/seed1.txt: 해시 다름", r["bad"])
        self.assertIn(f"daily/{D}/ledger.json: 없음", r["bad"])
        self.assertEqual(B.Backup(os.path.join(self.tmp, "nope"), root=self.root, times=TIMES, now=self.clock).verify()["ok"], False)

    def test_main_verify_exit_code(self):
        self.make_dest()
        self.bk().run()
        old = B.from_config
        try:
            B.from_config = lambda dest=None, cfg=None: B.Backup(dest, root=self.root, times=TIMES, now=self.clock)
            self.assertEqual(B.main(["--verify", "--dest", self.dest]), 0)
            put(self.dest, "mirror/logs/exec/ledger.json", "x")
            self.assertEqual(B.main(["--verify", "--dest", self.dest]), 1)
        finally:
            B.from_config = old


class NoResidentImport(unittest.TestCase):
    def test_backup_imports(self):
        """backup.py 는 상주 모듈을 import 하지 않는다 (settings·notify 만)"""
        import ast
        src = read(B.__file__)
        mods = set()
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        live = {os.path.splitext(f)[0] for f in os.listdir(os.path.dirname(B.__file__)) if f.endswith(".py")}
        self.assertEqual(mods & live, {"settings", "notify"})

    def test_config_key(self):
        ex = jload(settings.EXAMPLE)
        self.assertIn("dest", ex["backup"])
        self.assertFalse(ex["backup"]["dest"])  # 기본값 없음


# ---------------------------------------------------------------- features_eod

class FeaturesEod(unittest.TestCase):
    def setUp(self):
        import features_eod as F
        self.F = F
        self.tmp = tempfile.mkdtemp()
        self.log_dir = os.path.join(self.tmp, "logs", "exec")
        self.app = os.path.join(self.tmp, "live_append")
        os.makedirs(os.path.join(self.log_dir, "scores"))
        self.snap = put(self.tmp, "snap.parquet", binary=b"PAR1")
        self.sent = []
        self.codes = ["000020", "000040", "005930", "035720", "373220"]
        self.scores = np.array([0.11, -0.2, 0.5, 0.3, 0.05])
        pd.DataFrame({"code": self.codes, "score": self.scores}).to_parquet(os.path.join(self.log_dir, "scores", f"{D}.parquet"), index=False)
        self.dv = "20261028070512|2026-10-27"
        for v, dv in (("20261027070000000000", "20261027070000|2026-10-24"), ("20261028070512000000", self.dv)):
            put(self.app, f"versions/{v}/update.json", json.dumps({"data_version": dv}))
        put(self.app, "CURRENT.json", json.dumps({"version": "20261028070512000000"}))
        self.write_log(self.dv)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_log(self, dv, **kw):
        log = {"date": D, "data_version": dv, "ledger_before": {"strategy": [], "full": []},
               "live": {"snapshot": {"file": self.snap}, "index_values": {"kospi": 1.0, "kosdaq": 1.0},
                        "scores_file": os.path.join(self.log_dir, "scores", f"{D}.parquet")}}
        log.update(kw)
        with open(os.path.join(self.log_dir, f"{D}.json"), "w", encoding="utf-8") as f:
            json.dump(log, f)

    def fake(self, delta=0.0, drop=None):
        calls = []

        def compute(day, log, vdir):
            calls.append(vdir)
            e = pd.DataFrame({"code": self.codes, "ret_20": [0.1, 0.2, 0.3, 0.4, 0.5], "atr_pct": np.float32([1, 2, 3, 4, 5]),
                              "score": self.scores + delta})
            if drop:
                e = e[e.code != drop]
            top = e.sort_values("score", ascending=False, kind="stable").head(3)
            return {"eligible": e, "ledgers": {"strategy": {"top3": top}, "full": {"top3": top.head(2)}}, "errors": []}, {"model_check": True}
        return compute, calls

    def run_one(self, compute):
        return self.F.run_one(D, log_dir=self.log_dir, append_dir=self.app, compute_fn=compute, send=self.sent.append)

    def test_zero_diff(self):
        compute, calls = self.fake()
        r = self.run_one(compute)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["score_compare"]["max_abs_diff"], 0.0)
        self.assertEqual(calls, [os.path.join(self.app, "versions", "20261028070512000000")])  # 그날 데이터 버전 폴더
        self.assertEqual(self.sent, [])
        df = pd.read_parquet(os.path.join(self.log_dir, "features", f"{D}.parquet"))
        self.assertEqual(list(df.sort_values("rank").code), ["005930", "035720", "000020", "373220", "000040"])
        self.assertEqual(sorted(df[df.top3_strategy].code), ["000020", "005930", "035720"])
        self.assertIn("ret_20", df)
        self.assertIn("atr_pct", df)
        self.assertEqual(set(df.data_version), {self.dv})
        j = jload(os.path.join(self.log_dir, "features", f"{D}.json"))
        self.assertEqual(j["status"], "ok")
        self.assertIn(f"{D} ok", read(os.path.join(self.log_dir, "features", "features_eod.log")))

    def test_diff_alert(self):
        compute, _ = self.fake(delta=1e-7)
        r = self.run_one(compute)
        self.assertEqual(r["status"], "score_diff")
        self.assertAlmostEqual(r["score_compare"]["max_abs_diff"], 1e-7)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("점수 차이", self.sent[0])

    def test_code_set_diff_alert(self):
        compute, _ = self.fake(drop="000040")
        r = self.run_one(compute)
        self.assertEqual(r["status"], "score_diff")
        self.assertEqual(r["score_compare"]["only_live"], ["000040"])
        self.assertEqual(len(self.sent), 1)

    def test_version_missing(self):
        self.write_log("20261020070000|2026-10-19")
        compute, calls = self.fake()
        r = self.run_one(compute)
        self.assertEqual(r["status"], "version_missing")
        self.assertEqual(calls, [])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.F.pending(self.log_dir, today=D), [])  # 끝난 기록 -> 다시 안 함

    def test_old_version_pinned(self):
        """CURRENT 가 다음 날 버전으로 바뀌어도 그날 버전 폴더로"""
        self.write_log("20261027070000|2026-10-24")
        compute, calls = self.fake()
        self.run_one(compute)
        self.assertEqual(calls, [os.path.join(self.app, "versions", "20261027070000000000")])

    def test_no_snapshot_and_pending(self):
        os.remove(self.snap)
        compute, _ = self.fake()
        self.assertEqual(self.run_one(compute)["status"], "no_snapshot")
        put(self.log_dir, "2026-10-27.json", "{}")
        put(self.log_dir, "2026-10-01.json", "{}")       # 14일 밖
        put(self.log_dir, "2026-10-27_rerun.json", "{}")  # 그날 로그 아님
        self.assertEqual(self.F.pending(self.log_dir, today=D), ["2026-10-27"])

    def test_error_retry_limit(self):
        def boom(day, log, vdir):
            raise RuntimeError("계산 실패")
        for i in range(3):
            self.assertEqual(self.F.pending(self.log_dir, today=D), [D])
            r = self.run_one(boom)
            self.assertEqual(r["status"], "error")
            self.assertEqual(len(self.sent), 1 if i == 2 else 0)
        self.assertEqual(self.F.pending(self.log_dir, today=D), [])

    def test_compute_restores_pins(self):
        """compute 가 고친 data_dir·engine._loop·TEMP·대상 종목 목록은 실패해도 되돌림, 임시 폴더도 지움"""
        import common as C
        import engine as E
        import model as M
        saved = C.data_dir, E._loop, os.environ.get("TEMP"), M.load, C.sd.UNIVERSE
        seen = []

        def load(month):
            seen.append((C.data_dir(), os.environ.get("TEMP")))
            raise RuntimeError("모델 없음")
        try:
            M.load = load
            with self.assertRaises(RuntimeError):
                self.F.compute(pd.Timestamp(D), jload(os.path.join(self.log_dir, f"{D}.json")), self.app)
            self.assertEqual(seen[0][0], self.app)  # 계산 중에는 그날 버전 폴더로 고정
            self.assertIs(C.data_dir, saved[0])
            self.assertIs(E._loop, saved[1])
            self.assertEqual(os.environ.get("TEMP"), saved[2])
            self.assertEqual(C.sd.UNIVERSE, saved[4])
            self.assertFalse(os.path.exists(seen[0][1]))
        finally:
            M.load = saved[3]


if __name__ == "__main__":
    unittest.main()
