"""
시험 실패 진단 (서브 PC, 운영 중인 상주 프로그램을 건드리지 않음, 11W).

  git fetch origin
  git show origin/main:backtest_swing/live/diag_tests.py > %TEMP%\\diag_tests.py
  .venv-feat\\Scripts\\python.exe %TEMP%\\diag_tests.py --commit <커밋>

- 저장소 폴더(지금 폴더 = 저장소 루트)의 작업 폴더·data·logs 는 고치지 않는다. 임시 폴더에 git worktree(--detach, 그 커밋)를 만들고
  data 의 진단에 필요한 파일만 복사(원본은 읽기만): universe_current.json·universe_*.csv·live_append/.
- 시험 두 번: (A) 복사한 서브 PC data 로 (B) data 없이. 각 결과·전체 출력(traceback 포함)을 --out(기본 저장소 logs/exec)
  diag_tests_{커밋}_{시각}.txt (UTF-8) 에 남기고 요약을 출력. 끝나면 worktree 를 지움 (--keep 이면 남김).
- 키움·텔레그램 호출 없음 (시험은 가짜 객체만). 상주 프로그램·update.bat 을 부르지 않는다.
"""
import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile

DEFAULT_TESTS = [
    "test_fix11w.PrepReapJ6.test_j6_thread_ended_without_result_reaped_and_retried",
    "test_fix11w.UniverseAtomicG1.test_g1_new_prepare_fails_keeps_old_list_and_bundle",
    "test_fix11w.UniverseAtomicG1.test_g1_new_prepare_success_swaps_list_bundle_data_together",
    "test_fix11w.UniverseAtomicG1.test_g1_not_applied_by_data_check_runs_old_list_today",
    "test_fix11w.UniverseAtomicG1.test_g1_old_prepare_running_is_consistent_then_new_list",
    "test_fix11w.UniverseC1.test_c1_morning_success_reapplies_universe_to_parent",
]
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
SECRET_RE = re.compile(r"TOKEN|SECRET|PASSW|KEY|ACCOUNT|TELEGRAM|KRX|KIWOOM|^MOCK_", re.I)  # testguard.SECRET_RE 와 같게 (test_netguard 확인)


def clean_env(repo):
    """시험 프로세스 환경 변수: 토큰·키·계좌 이름과 저장소 .env 에 있는 이름은 빈 값 (옛 커밋 시험 파일에는 testguard 가 없음, worktree 에는 .env 없음)"""
    names = set()
    for p in (os.path.join(repo, ".env"), os.path.join(repo, "kiwoom", ".env")):
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="ignore") as f:
                names |= {x.split("=", 1)[0].replace("export ", "").strip() for x in f if "=" in x and not x.strip().startswith("#")}
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for k in set(env) | names:
        if k in names or SECRET_RE.search(k):
            env[k] = ""
    env["KIWOOM_TOKEN_CACHE_PATH"] = os.path.join(tempfile.gettempdir(), "stock_test_guard_token_cache.json")
    return env


def copy_data(src, dst):
    """진단에 필요한 data 파일만 복사 (원본은 읽기만). 반환 복사한 상대 경로 목록"""
    out = []
    os.makedirs(dst, exist_ok=True)
    if os.path.isdir(src):
        for n in sorted(os.listdir(src)):
            p = os.path.join(src, n)
            if os.path.isfile(p) and (n == "universe_current.json" or (n.startswith("universe_") and n.endswith(".csv"))):
                shutil.copy2(p, os.path.join(dst, n))
                out.append(n)
        la = os.path.join(src, "live_append")
        if os.path.isdir(la):
            shutil.copytree(la, os.path.join(dst, "live_append"))
            out.append("live_append/")
    return out


def run(cmd, cwd, runner=subprocess.run, timeout=900, env=None):
    r = runner(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW,
               env=env or {**os.environ, "PYTHONIOENCODING": "utf-8"})
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def summary(txt):
    ran = re.search(r"Ran (\d+) tests?", txt)
    bad = [x for x in txt.splitlines() if x.startswith(("ERROR:", "FAIL:"))]
    ok = bool(re.search(r"^OK", txt, re.M))
    return f"{'OK' if ok else 'FAILED'} ({ran.group(1) if ran else 0}개)" + ("".join(f"\n  {b}" for b in bad))


def main(argv=None, runner=subprocess.run):
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", default="HEAD")
    ap.add_argument("--repo", default=os.getcwd(), help="저장소 루트 (기본: 지금 폴더)")
    ap.add_argument("--data", default=None, help="복사할 data 폴더 (기본: 저장소 data)")
    ap.add_argument("--out", default=None, help="출력 폴더 (기본: 저장소 logs/exec)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("tests", nargs="*", default=None)
    a = ap.parse_args(argv)
    repo = os.path.abspath(a.repo)
    tests = a.tests or DEFAULT_TESTS
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    wt = os.path.join(tempfile.gettempdir(), f"stock_diag_{a.commit}_{stamp}")
    out_dir = a.out or os.path.join(repo, "logs", "exec")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"diag_tests_{a.commit}_{stamp}.txt")
    log = [f"진단 {stamp}: 커밋 {a.commit}, 저장소 {repo}, 임시 worktree {wt}, 시험 {len(tests)}개"]
    rc, txt = run(["git", "worktree", "add", "--detach", wt, a.commit], repo, runner, timeout=300)
    log.append(f"git worktree add -> {rc}\n{txt.strip()}")
    results = {}
    try:
        if rc == 0:
            live = os.path.join(wt, "backtest_swing", "live")
            copied = copy_data(a.data or os.path.join(repo, "data"), os.path.join(wt, "data"))
            log.append(f"복사한 data (원본은 읽기만): {copied}")
            for tag in ("A 서브 PC data", "B data 없음"):
                if tag.startswith("B"):
                    shutil.rmtree(os.path.join(wt, "data"), ignore_errors=True)
                rc2, txt2 = run([a.python, "-W", "ignore", "-m", "unittest", "-v", *tests], live, runner, env=clean_env(repo))
                results[tag] = summary(txt2)
                log.append(f"===== {tag}: {results[tag]}\n{txt2}")
    finally:
        if not a.keep and rc == 0:
            rc3, txt3 = run(["git", "worktree", "remove", "--force", wt], repo, runner, timeout=300)
            log.append(f"git worktree remove -> {rc3} {txt3.strip()}")
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(log) + "\n")
    for k, v in results.items():
        print(f"{k}: {v}")
    print(f"전체 출력: {out}")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
