"""
코드 갱신 (서브 PC, update.bat 가 부른다, 2026-10-24).

  python backtest_swing/live/update.py [--remote origin] [--branch main]

순서: 시각 확인(거래일 08:00 ~ 15:45 에는 거부) -> 현재 커밋 기록 -> 상주 프로그램 끄기(stop_resident 와 같음) -> 로컬 변경 확인 ->
git pull --ff-only -> 동결 코드 확인 -> 시험 전체 -> 통과면 켜기, 실패면 이전 커밋으로 되돌리고 켜기.
- 로컬 변경이 있거나 pull 이 실패하면 갱신을 중단하고 상주 프로그램을 다시 켠 뒤 알림.
- 켜기가 실패하면 되돌리기 전에 그때 켜진 상주 프로그램을 끄고 꺼진 것을 확인한다. 끄기에 실패해 중단할 때는 정지 요청 파일을 지우고(감시 작업이 다시 켜게),
  정지 상태로 끝낼 때는 마지막에 정지 요청 파일을 다시 만든다. 기록·알림의 상태 문구는 그때 실제 잠금·정지 요청 파일을 읽어 쓴다 (3차 확인 리뷰 R4 ~ R6).
- 동결 코드(backtest_swing/stage3 ~ 5_*.py)가 태그 forward-freeze-20261021 과 다르거나 동결 함수 해시 확인(engine.frozen_check)이 실패하면
  상주 프로그램을 켜지 않고 알림 (정지 요청 파일이 남아 감시 작업도 켜지 않는다).
- 결과(이전 -> 새 커밋, 시험 개수, 걸린 시간)를 logs/exec/update.log 와 상주 로그(logs/exec/resident/{날짜}.log)에 남기고 텔레그램으로 보낸다.
- 원격 저장소는 --remote (기본: 환경 변수 UPDATE_REMOTE, 없으면 origin). 서브 PC 에서는 push 하지 않는다.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402

CFG = settings.load()
from common import LOG_DIR, ROOT, now_kst  # noqa: E402
import control as K  # noqa: E402
import notify as N  # noqa: E402
import state as S  # noqa: E402

FREEZE_TAG = "forward-freeze-20261021"
FROZEN_GLOBS = ["backtest_swing/stage3_*.py", "backtest_swing/stage4_*.py", "backtest_swing/stage5_*.py"]
BLOCK_FROM, BLOCK_UNTIL = "08:00:00", "15:45:00"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def git(*args, timeout=120):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=NO_WINDOW)


def head():
    return git("rev-parse", "HEAD").stdout.strip()


def say(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:  # cp949 콘솔에서 못 쓰는 글자는 바꿔서
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(line.encode(enc, "replace").decode(enc), flush=True)


def record(msg, level="INFO", notify=True):
    """logs/exec/update.log + 상주 로그 + 텔레그램"""
    t = now_kst()
    line = " ".join(str(msg).split("\n"))
    for path in (os.path.join(LOG_DIR, "update.log"), os.path.join(LOG_DIR, "resident", f"{t:%Y-%m-%d}.log")):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{t:%Y-%m-%d %H:%M:%S} {level:5s} {line}\n")
        except Exception:  # noqa: BLE001
            pass
    say(line)
    if notify:
        N.send(line, log_path=os.path.join(LOG_DIR, "alerts.log"))


def allowed(t=None):
    """거래일 08:00 ~ 15:45 에는 갱신하지 않는다 (휴장일·주말·장 마감 후만)"""
    t = t or now_kst()
    hms = t.strftime("%H:%M:%S")
    return not (S.is_trading_day(t) and BLOCK_FROM <= hms < BLOCK_UNTIL)


def frozen_status():
    """동결 코드 확인: (1) 태그와 stage3 ~ 5 파일 차이 (2) 동결 함수 소스 해시·치환 (engine.frozen_check, 새 코드로 따로 실행)"""
    out = {}
    if git("rev-parse", "-q", "--verify", f"refs/tags/{FREEZE_TAG}").returncode != 0:
        git("fetch", "--tags", "-q")
    d = git("diff", "--name-only", FREEZE_TAG, "HEAD", "--", *FROZEN_GLOBS)
    out["tag_diff"] = d.stdout.split() if d.returncode == 0 else None
    out["tag_error"] = d.stderr.strip()[:200] if d.returncode != 0 else None
    code = "import json, sys; sys.path.insert(0, 'backtest_swing/live'); import engine; print(json.dumps(engine.frozen_check(), default=str))"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=300, creationflags=NO_WINDOW)
    try:
        out["frozen_check"] = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        out["frozen_check"] = {"ok": False, "error": (r.stderr or r.stdout)[-300:]}
    out["ok"] = out["tag_diff"] == [] and bool(out["frozen_check"].get("ok"))
    return out


def run_tests():
    """backtest_swing/live/test_*.py 전체. 반환 dict(ok, ran, failures, errors, seconds, tail)"""
    t0 = time.time()
    live = os.path.join(ROOT, "backtest_swing", "live")
    r = subprocess.run([sys.executable, "-W", "ignore", "-m", "unittest", "discover", "-s", live, "-t", live, "-p", "test_*.py"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800, creationflags=NO_WINDOW,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    txt = r.stdout + r.stderr
    ran = re.search(r"Ran (\d+) tests?", txt)
    fails = re.search(r"FAILED \((.*?)\)", txt)
    bad = [x for x in txt.splitlines() if x.startswith(("ERROR:", "FAIL:"))]
    return {"ok": r.returncode == 0 and bool(re.search(r"^OK", txt, re.M)), "ran": int(ran.group(1)) if ran else 0,
            "failed": fails.group(1) if fails else "", "bad": bad[:5], "seconds": round(time.time() - t0, 1)}


def main(remote, branch):
    """반환 코드: 0 완료, 1 시험 실패(이전 커밋으로 켬), 2 시각 거부, 3 로컬 변경·pull 실패(그대로 켬), 4 동결 코드 바뀜(안 켬),
    5 git 상태 못 읽음(손대지 않음), 6 상주 끄기 실패(pull 안 함, 그대로 둠), 7 긴급(되돌리기·다시 켜기 실패, 정지 상태), 8 예외(마지막 정상 커밋으로 켬)"""
    t0 = time.time()
    if not allowed():
        say(f"지금은 갱신하지 않음: 거래일 {BLOCK_FROM[:5]} ~ {BLOCK_UNTIL[:5]} (장중·장 전 준비·정산). 휴장일·주말·장 마감 후에 다시 실행하세요.")
        return 2
    try:
        old = head()
        dirty = [x for x in git("status", "--porcelain", "--untracked-files=no").stdout.splitlines() if x.strip()]
    except Exception as e:  # noqa: BLE001  (시간 제한 포함)
        old, dirty = "", [repr(e)[:200]]
    if not re.fullmatch(r"[0-9a-f]{40}", old or ""):  # git 이 현재 커밋을 못 읽으면 되돌릴 기준이 없으므로 끄기 전에 중단
        say(f"git 상태를 읽지 못함 - 갱신 안 함, 상주 프로그램도 건드리지 않음: {dirty[:1]}")
        return 5
    say(f"현재 커밋 {old[:7]}, 원격 {remote} {branch}")
    try:
        rc_stop = K.stop()
    except Exception as e:  # noqa: BLE001
        rc_stop, say_err = 1, repr(e)[:200]
        say(f"끄기 오류: {say_err}")
    if rc_stop != 0:  # 2차 확인 리뷰 5: 끄지 못하면 pull 하지 않고 그대로 둔다
        # 3차 확인 리뷰 R5: 정지 요청 파일을 지워, 기존 상주 프로그램이 나중에 꺼져도 감시 작업이 다시 켜게 한다 (잠금이 있는 동안은 감시 작업이 켜지 않음)
        cleared = _clear_stop_flag()
        record(f"[코드 갱신 중단] 상주 프로그램을 끄지 못함 - pull 안 함 ({old[:7]}). {_state()}"
               f"{'' if cleared else ' (정지 요청 파일 지우기 실패)'}. stop_resident.bat 결과와 작업 관리자를 확인하세요", "ERROR")
        return 6
    res = {"rc": 8, "level": "ERROR", "msg": "[코드 갱신 오류] 알 수 없는 오류", "target": old, "restart": True, "phase": "끄기 뒤"}
    try:  # 2차 확인 리뷰 7: 어떤 예외든 finally 에서 가능한 마지막 정상 커밋으로 다시 켠다
        _body(res, old, dirty, remote, branch)
    except Exception as e:  # noqa: BLE001  (TimeoutExpired 포함)
        res.update(rc=8, level="ERROR", target=old, restart=True, msg=f"[코드 갱신 오류] {res['phase']} 중 {type(e).__name__}: {str(e)[:200]}")
    finally:
        _finish(res, old, t0)
    return res["rc"]


def _body(res, old, dirty, remote, branch):
    if dirty:
        res.update(rc=3, level="WARN", msg=f"[코드 갱신 중단] 로컬 변경 {len(dirty)}개 ({', '.join(x[3:] for x in dirty[:5])}) - 갱신 안 함")
        return
    res["phase"] = "git pull"
    p = git("pull", "--ff-only", remote, branch, timeout=600)
    if p.returncode != 0:
        res.update(rc=3, level="WARN", msg=f"[코드 갱신 중단] git pull 실패: {(p.stderr or p.stdout).strip()[-200:]}")
        return
    new = head()
    res["phase"] = "동결 코드 확인"
    fz = frozen_status()
    if not fz["ok"]:
        res.update(rc=4, level="ERROR", restart=False, msg=f"[코드 갱신 멈춤] 동결 코드가 바뀜 ({old[:7]} -> {new[:7]}). 태그와 다른 파일 {fz['tag_diff']} "
                   f"{fz.get('tag_error') or ''}, 해시 확인 {fz['frozen_check']}. 확인 뒤 start_resident 로 켜세요")
        return
    res["phase"] = "시험"
    tr = run_tests()
    if tr["ok"]:
        res.update(rc=0, level="INFO", target=new, msg=f"[코드 갱신 완료] {old[:7]} -> {new[:7]}{' (변경 없음)' if new == old else ''}, "
                   f"시험 {tr['ran']}개 통과 ({tr['seconds']}초), 동결 코드 확인 통과")
    else:
        res.update(rc=1, level="ERROR", target=old, msg=f"[코드 갱신 실패] {old[:7]} -> {new[:7]} 시험 실패 ({tr['ran']}개 중 {tr['failed']}: "
                   f"{'; '.join(tr['bad'])}) - 이전 커밋으로 되돌림")


def _clear_stop_flag():
    """정지 요청 파일 지우기. 반환: 지금 파일이 없으면 True"""
    try:
        os.remove(K.stop_flag())
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return not os.path.exists(K.stop_flag())


def _state():
    """기록·알림에 쓸 실제 상태 (상주 프로그램 잠금, 정지 요청 파일) — 문구와 실제가 같게 (3차 확인 리뷰 R6)"""
    try:
        run = K.resident_running()
    except Exception:  # noqa: BLE001
        run = None
    flag = os.path.exists(K.stop_flag())
    who = {True: f"상주 프로그램 실행 중 (pid {K.info_pid()})", False: "상주 프로그램 꺼짐", None: "상주 프로그램 상태 확인 실패"}[run]
    if flag:
        tail = "정지 요청 파일 있음 (감시 작업이 켜지 않음" + (", 상주 프로그램은 다음 반복에서 끝남)" if run else ")")
    else:
        tail = "정지 요청 파일 없음 (" + ("계속 돌고, 꺼지면 감시 작업이 다시 켬)" if run else "감시 작업이 5분 안에 켬)")
    return f"{who}, {tail}"


def _stop_stray(tried):
    """3차 확인 리뷰 R4: 켜기 실패 뒤 되돌리기 전에, 그때 켜진 상주 프로그램(잠금·pid)을 끄고 꺼진 것을 확인. 반환: 꺼져 있으면 True"""
    try:
        rc = K.stop()
    except Exception as e:  # noqa: BLE001
        rc = 1
        tried.append(f"끄기 오류 {e!r}"[:120])
    try:
        run = K.resident_running()
    except Exception:  # noqa: BLE001
        run = True
    if rc == 0 and not run:
        return True
    tried.append(f"켜기 실패 뒤 떠 있는 상주 프로그램(pid {K.info_pid()})을 끄지 못함 -> 되돌리지 않음")
    return False


def _at(commit):
    """작업 폴더를 commit 으로 맞춘다 (git 시간 제한). 반환: (성공, 설명)"""
    try:
        if head() == commit:
            return True, ""
        r = git("reset", "--hard", commit, timeout=120)
        return (r.returncode == 0 and head() == commit), (r.stderr or r.stdout).strip()[-200:]
    except Exception as e:  # noqa: BLE001
        return False, repr(e)[:200]


def _finish(res, old, t0):
    """끝맺음: 코드를 목표 커밋(실패면 이전 커밋)으로 맞추고 상주 프로그램을 다시 켠다. 켜면 정지 요청 파일은 start 가 지운다.
    되돌리기·켜기가 모두 안 되면 정지 상태를 유지하고 긴급 알림 (2차 확인 리뷰 6·7)"""
    took = f"{time.time() - t0:.0f}초"
    if not res["restart"]:
        _hold_stopped()
        record(f"{res['msg']} - 상주 프로그램 안 켬 ({_state()}), 걸린 시간 {took}"[:700], res["level"])
        return
    tried = []
    stray = False
    for commit in dict.fromkeys([res["target"], old]):
        ok, why = _at(commit)
        if not ok:
            tried.append(f"{commit[:7]} 되돌리기 실패 ({why})")
            continue
        try:
            rc = K.start()
        except Exception as e:  # noqa: BLE001
            rc, why = 1, repr(e)[:200]
        if rc == 0:
            extra = f" (목표 {res['target'][:7]} 실패: {'; '.join(tried)})" if tried else ""
            record(f"{res['msg']} - 상주 프로그램 다시 켬 ({commit[:7]}){extra}, 걸린 시간 {took}"[:700], res["level"] if not tried else "ERROR")
            return
        tried.append(f"{commit[:7]} 에서 켜기 실패 ({why or 'start 실패'})")
        if not _stop_stray(tried):  # R4: 떠 있는 상주 프로그램을 끄지 못하면 그 아래에서 코드를 되돌리지 않는다
            stray = True
            break
    res["rc"] = 7
    _hold_stopped()
    try:
        now_at = head()[:7]
    except Exception:  # noqa: BLE001
        now_at = "?"
    record(f"[긴급] {res['msg']} -> 되돌리기·다시 켜기 실패: {'; '.join(tried)}. {_state()}, "
           f"지금 작업 폴더 커밋 {now_at}. 수동 조치: {'작업 관리자에서 resident.py 종료 확인 -> ' if stray else ''}저장소 폴더에서 git status 확인 -> "
           f"git reset --hard {old[:7]} -> "
           f"backtest_swing\\live\\start_resident.bat (원격 접속, SETUP.md 11절), 걸린 시간 {took}"[:1500], "ERROR")


def _hold_stopped():
    """3차 확인 리뷰 R6: 정지 상태로 끝낼 때는 마지막에 정지 요청 파일을 다시 만든다 (start 시도가 지웠을 수 있음) -> 감시 작업이 켜지 않음"""
    try:
        K.make_stop_flag("update (정지 상태 유지)")
    except Exception:  # noqa: BLE001  (실패는 _state() 문구에 그대로 드러남)
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", default=os.environ.get("UPDATE_REMOTE") or "origin")
    ap.add_argument("--branch", default=os.environ.get("UPDATE_BRANCH") or "main")
    a, _ = ap.parse_known_args()
    sys.exit(main(a.remote, a.branch))
