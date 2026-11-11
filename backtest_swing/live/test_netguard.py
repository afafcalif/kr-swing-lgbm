"""
11W 시험 보호 (testguard.py) 시험: 토큰·키가 환경에 있어도 시험에서 텔레그램·키움·KRX 실제 호출이 안 됨, 시도하면 그 시험이 실패.
막힌 호출만 일부러 시도한다 (실제로 나가지 않음). 토큰 값은 모두 가짜.

  python -m unittest backtest_swing/live/test_netguard.py -v
"""
import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import testguard; testguard.install()  # noqa: E401,E402,E702  (첫 줄 유지)
import glob
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["LIVE_CONFIG"] = "example"
import requests  # noqa: E402
from test_codex import Base  # noqa: E402
import monthly as MO  # noqa: E402
import notify as N  # noqa: E402
import telecmd as TC  # noqa: E402

TOKEN = "1234" + "56789:" + "AA" + "x" * 33                                       # 가짜 (파일에 토큰 모양 글자를 남기지 않게 이어 붙임)
FIRST = "import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import testguard; testguard.install()"
LEAK = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "555", "KRX_API_KEY": "fake-krx-key-0123456789", "APP_KEY": "fake-app-key-0123456789",
        "APP_SECRET": "fake-app-secret-0123456789", "ACCOUNT_NO": "00000000"}


class EnvRules(unittest.TestCase):
    def test_env_blanks_secret_names_and_env_file_names(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
            f.write("# 주석\nSOME_OTHER_NAME=value\nexport THIRD_NAME=value\n")
        env = testguard.test_env({**LEAK, "PATH": "p", "SOME_OTHER_NAME": "real"}, root=tmp)
        for k in LEAK:
            self.assertEqual(env[k], "", k)
        self.assertEqual((env["SOME_OTHER_NAME"], env["THIRD_NAME"], env["PATH"]), ("", "", "p"))     # .env 에 있는 이름은 모양과 상관없이 비움
        self.assertEqual(env[testguard.FLAG], "1")
        self.assertTrue(env["PYTHONPATH"].startswith(testguard.SITE_DIR))
        self.assertTrue(env[testguard.CACHE_ENV].startswith(tempfile.gettempdir()))                    # 진짜 토큰 캐시 안 읽음·안 지움
        self.assertFalse(testguard.SECRET_RE.search(testguard.LOG_ENV) or testguard.SECRET_RE.search("PYTHONPATH"))

    def test_env_file_not_loaded(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        p = os.path.join(tmp, ".env")
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"TELEGRAM_BOT_TOKEN={TOKEN}\nGUARD_ONLY_NAME=x\n")
        import dotenv
        self.assertFalse(dotenv.load_dotenv(p))
        self.assertNotIn("GUARD_ONLY_NAME", os.environ)
        self.assertEqual(N._env(), ("", ""))                                        # 저장소 .env 에 진짜 값이 있어도

    def test_each_test_starts_blank_even_if_leaked(self):
        seen = {}

        class Inner(unittest.TestCase):
            def test_x(self):
                seen.update({k: os.environ.get(k) for k in LEAK})
        with mock.patch.dict(os.environ, LEAK):
            Inner("test_x").run(unittest.TestResult())
        self.assertEqual(set(seen.values()), {""})

    def test_every_test_file_starts_with_guard(self):
        for p in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
            with open(p, encoding="utf-8") as f:
                src = f.read()
            self.assertIn(FIRST, src, p)
            before = src.split(FIRST)[0]
            self.assertNotIn("\nimport ", "\n" + before.split('"""')[-1], p)         # 그 앞에 다른 import 없음
            self.assertNotIn("\nfrom ", "\n" + before.split('"""')[-1], p)


class Blocked(unittest.TestCase):
    def setUp(self):
        testguard.take()
        self.addCleanup(testguard.take)                                             # 일부러 시도한 것은 이 시험 실패로 치지 않음

    def test_telegram_get_and_post_blocked_token_not_in_record(self):
        with mock.patch.dict(os.environ, LEAK):
            b = TC.Bot(TOKEN, "555", lambda: {}, lambda m, lv=None: None)           # 운영과 같은 기본 경로 (가짜 http 없음)
            with self.assertRaises(ConnectionError):
                b.poll_once(timeout=1)
            with self.assertRaises(ConnectionError):
                b._post("sendMessage", {"chat_id": "555", "text": "x"})
        got = testguard.take()
        self.assertEqual([x.split()[:3] for x in got], [["requests", "api.telegram.org", "getUpdates"], ["requests", "api.telegram.org", "sendMessage"]])
        self.assertNotIn(TOKEN, " ".join(got))
        self.assertNotIn(TOKEN.split(":")[1], " ".join(got))

    def test_notify_with_token_blocked(self):
        with mock.patch.dict(os.environ, LEAK), mock.patch("time.sleep"):
            self.assertFalse(N.send("x"))                                            # 예외를 삼키는 경로: 못 보냄, 기록은 남음
        self.assertEqual(len([x for x in testguard.take() if "api.telegram.org sendMessage" in x]), 3)

    def test_krx_blocked(self):
        with mock.patch.dict(os.environ, LEAK), self.assertRaises(ConnectionError):
            MO.fetch_universe("20261030")
        self.assertIn("data-dbg.krx.co.kr", testguard.take()[0])

    def test_kiwoom_token_and_tr_blocked(self):
        from kiwoom.client import KiwoomRestClient
        from kiwoom.config import Settings
        with mock.patch.dict(os.environ, LEAK):
            c = KiwoomRestClient(Settings.from_env("prod"))
            with self.assertRaises(Exception):
                c.request("ka10095", "POST", "/api/dostk/stkinfo", json={})
        self.assertTrue(any("api.kiwoom.com" in x for x in testguard.take()))

    def test_socket_level_blocked_loopback_allowed(self):
        for f in (lambda: socket.getaddrinfo("api.telegram.org", 443), lambda: socket.gethostbyname("api.kiwoom.com"),
                  lambda: socket.create_connection(("203.0.113.1", 443), timeout=1)):
            with self.assertRaises(testguard.NetworkBlocked):
                f()
        s = socket.socket()
        self.addCleanup(s.close)
        with self.assertRaises(testguard.NetworkBlocked):
            s.connect(("203.0.113.1", 443))
        self.assertEqual(len(testguard.take()), 4)
        srv = socket.socket()
        self.addCleanup(srv.close)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        c = socket.create_connection(srv.getsockname(), timeout=2)                  # 가짜 서버(루프백)는 됨
        c.close()
        self.assertEqual(testguard.take(), [])

    def test_attempt_fails_the_test_even_if_exception_swallowed(self):
        class Inner(unittest.TestCase):
            def test_quiet(self):
                try:
                    requests.get("https://api.telegram.org/botX/getUpdates", timeout=1)
                except Exception:  # noqa: BLE001
                    pass
        r = unittest.TestResult()
        Inner("test_quiet").run(r)
        self.assertEqual(len(r.failures), 1)
        self.assertIn("api.telegram.org getUpdates", r.failures[0][1])
        self.assertFalse(r.wasSuccessful())

    def test_thread_attempt_fails_too(self):
        class Inner(unittest.TestCase):
            def test_thread(self):
                def go():
                    try:
                        requests.get("https://api.kiwoom.com/oauth2/token", timeout=1)
                    except Exception:  # noqa: BLE001
                        pass
                t = threading.Thread(target=go)
                t.start()
                t.join()
        r = unittest.TestResult()
        Inner("test_thread").run(r)
        self.assertEqual(len(r.failures), 1)

    def test_child_process_blocked_and_recorded(self):
        code = ("import socket, notify\n"
                "try:\n    socket.getaddrinfo('api.telegram.org', 443)\n    print('NOT BLOCKED')\n"
                "except ConnectionError:\n    print('BLOCKED')\n"
                "print(repr(notify._env()))\n")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=HERE, timeout=120,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertIn("BLOCKED", r.stdout, r.stderr[-300:])
        self.assertNotIn("NOT BLOCKED", r.stdout)
        self.assertIn("('', '')", r.stdout)                                         # 자식이 저장소 .env 를 읽어도 빈 값
        self.assertEqual([x.split()[:2] for x in testguard.take()], [["dns", "api.telegram.org"]])

    def test_update_style_env_site_first_then_install_blocks_requests(self):
        """update.py 는 PYTHONPATH 를 미리 넘김 -> sitecustomize(소켓만) 가 먼저, 그 뒤 install 도 requests 를 막아야 함 (예전: 소켓만, 기록 모양 다름)"""
        code = ("import sys; sys.path.insert(0, r'%s')\nimport testguard\nprint('site', testguard._state['network'], testguard._state['requests'])\n"
                "testguard.install()\nimport requests\n"
                "try:\n    requests.get('https://api.telegram.org/botX/getUpdates', timeout=1)\nexcept testguard.NetworkBlocked:\n    print('REQUESTS BLOCKED')\n"
                "print(testguard.take()[0].split()[:3])\ntestguard.take()\n") % HERE
        env = {k: v for k, v in testguard.test_env(root=testguard.ROOT).items() if k not in (testguard.LOG_ENV, testguard.OWNER_ENV)}
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                           env={**env, "PYTHONIOENCODING": "utf-8"})
        self.assertIn("site True False", r.stdout, r.stderr[-300:])                 # sitecustomize 가 먼저 돌았음
        self.assertIn("REQUESTS BLOCKED", r.stdout, r.stderr[-300:])
        self.assertIn("['requests', 'api.telegram.org', 'getUpdates']", r.stdout)

    def test_record_names_the_test(self):
        with self.assertRaises(testguard.NetworkBlocked):
            socket.getaddrinfo("api.telegram.org", 443)
        self.assertIn("test=test_netguard.Blocked.test_record_names_the_test", testguard.take()[0])

    def test_exit_code_70_when_attempt_after_last_test(self):
        code = ("import sys; sys.path.insert(0, r'%s')\nimport testguard; testguard.install()\nimport requests\n"
                "try:\n    requests.get('https://api.telegram.org/botX/getUpdates', timeout=1)\nexcept Exception:\n    pass\n") % HERE
        env = {k: v for k, v in os.environ.items() if k not in (testguard.LOG_ENV, testguard.OWNER_ENV)}
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                           env={**env, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(r.returncode, 70, r.stderr[-300:])
        self.assertIn("[시험 보호]", r.stderr)


class Resident(Base):
    def test_run_path_starts_no_bot_even_if_token_leaked(self):
        """21:51 출처: test_confirm 의 res.run() -> start_bot -> .env 의 진짜 토큰으로 실제 getUpdates"""
        res = self.resident()
        self.assertIsNone(res.start_bot())
        self.assertFalse([t.name for t in threading.enumerate() if t.name == "telegram-cmd"])

    def test_test_fix_rules_use_no_real_caller(self):
        import test_fix as TF
        t = TF.ResidentRules("test_f01_trend_sell_skips_code_not_in_account")
        r = unittest.TestResult()
        t.run(r)                                                                     # 예전: 실제 키움 토큰·ka10095 시도 (여기서는 실패로 잡힘)
        self.assertTrue(r.wasSuccessful(), r.failures + r.errors)


class Entry(unittest.TestCase):
    def test_update_run_tests_passes_clean_env(self):
        import update as U
        got = {}

        def runner(cmd, **kw):
            got.update(kw["env"])
            return types.SimpleNamespace(returncode=0, stdout="Ran 1 test in 0.1s\n\nOK\n", stderr="")
        with mock.patch.dict(os.environ, LEAK):                                     # 서브 PC update.py: notify 가 .env 를 읽어 진짜 값이 환경에 있음
            U.run_tests(runner=runner)
        for k in LEAK:
            self.assertEqual(got[k], "", k)
        self.assertEqual(got[testguard.FLAG], "1")
        self.assertEqual(got["PYTHONIOENCODING"], "utf-8")

    def test_diag_tests_passes_clean_env_same_rule(self):
        import diag_tests as D
        self.assertEqual(D.SECRET_RE.pattern, testguard.SECRET_RE.pattern)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
            f.write("ODD_NAME=real\n")
        with mock.patch.dict(os.environ, {**LEAK, "ODD_NAME": "real"}):
            env = D.clean_env(tmp)
        for k in (*LEAK, "ODD_NAME"):
            self.assertEqual(env[k], "", k)
        self.assertTrue(env["KIWOOM_TOKEN_CACHE_PATH"].startswith(tempfile.gettempdir()))


if __name__ == "__main__":
    unittest.main()
