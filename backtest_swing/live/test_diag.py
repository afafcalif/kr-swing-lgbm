"""
11W 서브 PC 시험 실패(<커밋>) 뒤 재발 방지 시험: update.py 시험 실패 전체 출력 파일, diag_tests.py (임시 worktree 진단), 시험 격리(월별 종목 목록 포인터).
가짜 실행기·임시 폴더만 (git·시험 실제 실행·저장소 logs/data 없음).

  python -m unittest backtest_swing/live/test_diag.py -v
"""
import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import testguard; testguard.install()  # noqa: E401,E402,E702  (첫 줄 유지)
import os  # noqa: E402
os.environ["LIVE_CONFIG"] = "example"
import contextlib
import io
import shutil
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_codex import Base  # noqa: E402
import common as C  # noqa: E402

TRACE = ("FAIL: test_x (test_fix11w.Y.test_x)\n----\nTraceback (most recent call last):\n  File \"t.py\", line 1\n"
         "AssertionError: 'data/universe_20261102.csv' != '목록'\n\nRan 3 tests in 0.1s\n\nFAILED (failures=1)\n")


def fake_run(rc, out, calls=None):
    def f(cmd, **kw):
        if calls is not None:
            calls.append((cmd, kw.get("cwd")))
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")
    return f


class UpdateLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_failure_writes_full_output_utf8(self):
        import update as U
        r = U.run_tests(out_dir=self.tmp, runner=fake_run(1, TRACE))
        self.assertFalse(r["ok"])
        self.assertTrue(os.path.basename(r["log"]).startswith("update_tests_") and r["log"].endswith(".txt"))
        with open(r["log"], encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("Traceback (most recent call last)", txt)                      # 예전: 요약(FAIL 줄 5개)만 update.log 에
        self.assertIn("'목록'", txt)
        self.assertEqual(r["bad"], ["FAIL: test_x (test_fix11w.Y.test_x)"])

    def test_success_writes_nothing(self):
        import update as U
        r = U.run_tests(out_dir=self.tmp, runner=fake_run(0, "Ran 3 tests in 0.1s\n\nOK\n"))
        self.assertEqual((r["ok"], r["log"], os.listdir(self.tmp)), (True, None, []))


class DiagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        src = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(src, "live_append"))
        os.makedirs(os.path.join(src, "daily_raw"))
        for n in ("universe_current.json", "universe_20261102.csv", "universe_20261019.csv", "stock_data.db"):
            with open(os.path.join(src, n), "w", encoding="utf-8") as f:
                f.write(n)
        with open(os.path.join(src, "live_append", "update.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        self.src = src

    def test_copy_only_needed_files_source_untouched(self):
        import diag_tests as D
        before = sorted(os.listdir(self.src))
        got = D.copy_data(self.src, os.path.join(self.tmp, "wt", "data"))
        self.assertEqual(got, ["universe_20261019.csv", "universe_20261102.csv", "universe_current.json", "live_append/"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.tmp, "wt", "data"))),
                         ["live_append", "universe_20261019.csv", "universe_20261102.csv", "universe_current.json"])
        self.assertEqual(sorted(os.listdir(self.src)), before)

    def test_main_worktree_two_runs_output_file_then_remove(self):
        import diag_tests as D
        calls = []
        out = os.path.join(self.tmp, "out")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = D.main(["--commit", "HEAD", "--repo", self.tmp, "--data", self.src, "--out", out], runner=fake_run(0, TRACE, calls))
        self.assertEqual(rc, 0)
        wt = calls[0][0][4]
        self.addCleanup(shutil.rmtree, wt, ignore_errors=True)
        self.assertEqual(calls[0][0][:4], ["git", "worktree", "add", "--detach"])
        self.assertEqual(calls[0][0][5], "HEAD")
        self.assertEqual([c[1] for c in calls[1:3]], [os.path.join(wt, "backtest_swing", "live")] * 2)   # 시험은 임시 worktree 에서만
        self.assertIn(D.DEFAULT_TESTS[0], calls[1][0])
        self.assertEqual(calls[3][0][:4], ["git", "worktree", "remove", "--force"])
        (f,) = os.listdir(out)
        with open(os.path.join(out, f), encoding="utf-8") as fh:
            txt = fh.read()
        self.assertIn("===== A 서브 PC data: FAILED", txt)
        self.assertIn("===== B data 없음", txt)
        self.assertIn("Traceback", txt)


class PointerIsolation(Base):
    def test_base_uses_temp_universe_pointer(self):
        """시험은 저장소 data/universe_current.json (서브 PC 에는 11월 목록) 을 읽지 않음: 포인터 = 임시 폴더, 그날 목록 = 기본 목록에서 시작"""
        self.assertTrue(C.UNIVERSE_POINTER.startswith(self.tmp))
        self.assertEqual(C.sd.UNIVERSE, C.UNIVERSE_DEFAULT)
        import monthly as MO
        self.assertEqual(MO.UNIVERSE_POINTER, C.UNIVERSE_POINTER)
        self.assertEqual(C.universe_for("2026-11-02"), C.UNIVERSE_DEFAULT)
