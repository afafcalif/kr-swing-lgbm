"""
상주 프로그램 켜기·끄기·상태 (start_resident.bat / stop_resident.bat 가 부른다, 2026-10-24).

  python backtest_swing/live/control.py start    # 정지 요청 파일을 지우고 감시 작업 경유로 창 없이 켬
  python backtest_swing/live/control.py stop     # 감시 작업이 다시 켜지 않게 정지 요청 -> 상주 프로그램 정상 종료 기다림 -> 시간을 넘으면 강제 종료 -> 잠금 파일 정리
  python backtest_swing/live/control.py status

- 정지 요청 파일: log_dir/resident.stop. 있으면 상주 프로그램은 단계 사이에서 정상 종료하고, 감시 작업은 다시 켜지 않는다. start 가 지운다.
- 켜기: 작업 스케줄러에 감시 작업(kr-swing-watchdog)이 등록돼 있으면 그 작업을 실행, 없으면 watchdog.py 를 직접 한 번 실행.
- 상주 프로그램은 창 없이(CREATE_NO_WINDOW) 따로 떨어진 프로세스라, 이 스크립트 창을 닫아도 영향 없음.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402

CFG = settings.load()
from common import RESIDENT_INFO, RESIDENT_LOCK, resident_running  # noqa: E402

TASK = "kr-swing-watchdog"
NO_WINDOW = 0x08000000
STOP_TIMEOUT = 180  # 정상 종료를 기다리는 시간 (초). 단계 하나가 끝날 때까지 기다리므로 넉넉히
START_WAIT = 90     # 켜진 것(그 pid 의 새 하트비트)을 기다리는 시간 (초). 하트비트는 1분에 한 줄이라 60초보다 길게


def stop_flag():
    return os.path.join(CFG["log_dir"], "resident.stop")


def make_stop_flag(by="stop_resident"):
    """정지 요청 파일을 만든다 (상주 프로그램은 다음 반복에서 끝나고, 감시 작업은 다시 켜지 않는다)"""
    os.makedirs(CFG["log_dir"], exist_ok=True)
    with open(stop_flag(), "w", encoding="utf-8") as f:
        json.dump({"since": time.strftime("%Y-%m-%d %H:%M:%S"), "by": by}, f, ensure_ascii=False)


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW if os.name == "nt" else 0)


def task_exists():
    return os.name == "nt" and _run(["schtasks", "/Query", "/TN", TASK]).returncode == 0


def info_pid():
    try:
        with open(RESIDENT_INFO, encoding="utf-8") as f:
            return json.load(f).get("pid")
    except Exception:  # noqa: BLE001
        return None


def last_heartbeat():
    try:
        with open(CFG["heartbeat_file"], "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 300))
            return f.read().decode("utf-8", "ignore").strip().splitlines()[-1]
    except Exception:  # noqa: BLE001
        return None


def python_procs(script):
    """명령줄에 script 가 들어간 python 프로세스 pid 목록 (Windows)"""
    if os.name != "nt":
        return []
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{script}*' }} | ForEach-Object {{ $_.ProcessId }}")
    r = _run(["powershell", "-NoProfile", "-Command", ps])
    return [int(x) for x in r.stdout.split() if x.strip().isdigit() and int(x) != os.getpid()]


def say(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:  # cp949 콘솔에서 못 쓰는 글자는 바꿔서
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(line.encode(enc, "replace").decode(enc), flush=True)


def status():
    run = resident_running()
    say(f"상주 프로그램 {'실행 중' if run else '꺼짐'} (pid {info_pid() if run else '-'}), 마지막 하트비트: {last_heartbeat()}")
    say(f"정지 요청 파일 {'있음' if os.path.exists(stop_flag()) else '없음'}, 작업 스케줄러 감시 작업 {'등록됨' if task_exists() else '없음'}")
    return 0 if run else 1


def start(wait=START_WAIT, stop_wait=STOP_TIMEOUT):
    """켜기. 0 을 돌려주면 실제로 켜져 있다 (잠금을 잡은 상주 프로그램 + 그 pid 의 새 하트비트).
    2차 확인 리뷰 8: 정지 요청 파일이 있는데 상주 프로그램이 아직 끝나는 중이면 종료를 기다린 뒤(최대 stop_wait 초) 파일을 지우고 켠다.
    그래도 안 끝나면 "종료 중, 잠시 뒤 다시" 로 1.
    3차 확인 리뷰 R4: 정지 요청 파일이 없어도 잠금·pid 가 있으면(이미 실행 중) 그 pid 의 새 하트비트(부른 뒤에 쓴 줄)를 확인해야 0."""
    hb0 = last_heartbeat()
    if os.path.exists(stop_flag()) and resident_running():
        say(f"정지 요청 처리 중 (pid {info_pid()}) - 끝나기를 기다림 (최대 {stop_wait}초)")
        t0 = time.time()
        while resident_running() and time.time() - t0 < stop_wait:
            time.sleep(1)
        if resident_running():
            say("종료 중 - 잠시 뒤 다시 실행하세요 (켜지 않음)")
            return 1
        cleanup()
    elif resident_running():
        say(f"이미 실행 중 (pid {info_pid()}) - 그 pid 의 새 하트비트 확인 (최대 {wait}초)")
        return _confirm(hb0, wait)
    if os.path.exists(stop_flag()):
        os.remove(stop_flag())
        say("정지 요청 파일 지움")
    if task_exists():
        r = _run(["schtasks", "/Run", "/TN", TASK])
        say(f"작업 스케줄러 감시 작업 실행: {r.stdout.strip() or r.stderr.strip()}")
    else:
        import watchdog
        watchdog.main()
        say("감시 작업(watchdog.py)을 직접 한 번 실행")
    return _confirm(hb0, wait)


def _confirm(hb0, wait):
    """잠금을 잡은 상주 프로그램의 pid 로 hb0 뒤에 새로 쓴 하트비트가 보이면 0, wait 초 안에 없으면 1"""
    t0 = time.time()
    while time.time() - t0 < wait:
        pid = info_pid() if resident_running() else None
        hb = last_heartbeat()
        if pid and hb and hb != hb0 and f" pid={pid} " in f"{hb} " and resident_running():
            say(f"켜짐: pid {pid}, 하트비트 {hb}")
            return 0
        time.sleep(1)
    say(f"{wait}초 안에 켜졌는지 확인 못 함 (잠금 {resident_running()}, pid {info_pid()}, 하트비트 {last_heartbeat()}) - "
        f"logs/exec/resident.out.log, logs/exec/watchdog.log 확인")
    return 1


def cleanup():
    """잠금·정보 파일 정리 (상주 프로그램이 꺼진 것을 확인한 뒤에만)"""
    if resident_running():
        return []
    gone = []
    for p in (RESIDENT_INFO, CFG["pid_file"], RESIDENT_LOCK):
        try:
            os.remove(p)
            gone.append(os.path.basename(p))
        except FileNotFoundError:
            pass
        except OSError as e:
            say(f"{p} 지우기 실패: {e!r}")
    return gone


def stop(timeout=STOP_TIMEOUT):
    # 1) 감시 작업: 정지 요청 파일로 다시 켜지 않게 하고, 지금 돌고 있는 감시 작업이 끝날 때까지 (20초, 넘으면 강제 종료)
    make_stop_flag("stop_resident")
    say("정지 요청 파일 만듦 (감시 작업이 다시 켜지 않음)")
    t0 = time.time()
    while python_procs("watchdog.py") and time.time() - t0 < 20:
        time.sleep(1)
    for pid in python_procs("watchdog.py"):
        _run(["taskkill", "/PID", str(pid), "/T", "/F"])
        say(f"감시 작업 강제 종료 pid {pid}")
    # 2) 상주 프로그램: 정상 종료 기다림 (단계 사이에서 끝남), 시간을 넘으면 강제 종료
    if not resident_running():
        say(f"상주 프로그램은 이미 꺼져 있음, 정리한 파일 {cleanup()}")
        return 0
    pid = info_pid()
    say(f"상주 프로그램(pid {pid}) 정상 종료 기다림 (최대 {timeout}초)")
    t0 = time.time()
    while resident_running() and time.time() - t0 < timeout:
        time.sleep(1)
    how = "정상 종료"
    if resident_running():
        how = "강제 종료"
        for p in [pid] + [x for x in python_procs("resident.py") if x != pid]:
            if p:
                r = _run(["taskkill", "/PID", str(p), "/T", "/F"])
                say(f"강제 종료 pid {p}: {(r.stdout or r.stderr).strip()}")
        t1 = time.time()
        while resident_running() and time.time() - t1 < 15:
            time.sleep(1)
    if resident_running():
        say("꺼지지 않음 - 작업 관리자에서 확인 필요")
        return 1
    say(f"꺼짐 ({how}, {time.time() - t0:.0f}초), 정리한 파일 {cleanup()}")
    return 0


if __name__ == "__main__":
    settings.refuse_test_env("켜기·끄기 (control.py)")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    sys.exit({"start": start, "stop": stop, "status": status}[cmd]())
