"""
감시 작업 (EXEC_SPEC 8장 10, 2026-10-22 결정). Windows 작업 스케줄러가 로그온 시와 5분마다 실행 (register_tasks.ps1, 등록은 사용자가).

  python backtest_swing/live/watchdog.py

- 상주 프로그램이 살아 있는지 OS 수준 단일 실행 잠금(logs/exec/resident.lock)으로 확인하고, 아니면 다시 켠다 (2026-10-24 F-02) (텔레그램 "재시작" 은 상주 프로그램이 켜지면서 보냄).
- 하트비트(heartbeat_file 마지막 줄)가 heartbeat_stale_minutes(5분) 이상 끊기면 텔레그램 알림 (끊긴 동안 한 번만, 다시 이어지면 해제).
- 감시 기록: log_dir/watchdog.log
"""
import os
import subprocess
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402
import notify as N  # noqa: E402

CFG = settings.load()
LOG = os.path.join(CFG["log_dir"], "watchdog.log")
FLAG = os.path.join(CFG["state_dir"], "heartbeat_alerted.flag")
RESIDENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resident.py")


def log(msg):
    os.makedirs(CFG["log_dir"], exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{pd.Timestamp.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def alive(pid):
    """Windows: tasklist 로 확인 (os.kill(pid, 0) 은 Windows 에서 CTRL_C 신호가 되므로 쓰지 않음)"""
    if not pid:
        return False
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True)
    return f'"{pid}"' in r.stdout and "python" in r.stdout.lower()


def last_heartbeat():
    try:
        with open(CFG["heartbeat_file"], "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 300))
            line = f.read().decode("utf-8", "ignore").strip().splitlines()[-1]
        return pd.Timestamp(line[:19])
    except Exception:  # noqa: BLE001
        return None


def start():
    """상주 프로그램을 창 없이 따로 떨어진 프로세스로 켠다 (2026-10-24).
    - CREATE_NO_WINDOW: 보이지 않는 콘솔을 만들어 자식(venv 런처가 띄우는 실제 python, git, 계산 작업 프로세스)이 그 콘솔을 물려받는다.
      예전 DETACHED_PROCESS 는 콘솔이 없어 venv 런처의 자식 python 이 새 콘솔 창을 띄웠다 (게임 중 창이 떠서 사용자가 닫음).
    - CREATE_BREAKAWAY_FROM_JOB: 작업 스케줄러 작업이 끝나거나 시간 제한으로 정리될 때 같이 꺼지지 않게. 작업 개체가 허용하지 않으면 그 플래그 없이"""
    flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    out = open(os.path.join(CFG["log_dir"], "resident.out.log"), "a", encoding="utf-8")
    kw = dict(cwd=CFG["root"], stdout=out, stderr=out, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    try:
        p = subprocess.Popen([CFG["python"], RESIDENT], creationflags=flags | 0x01000000, **kw)  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        p = subprocess.Popen([CFG["python"], RESIDENT], creationflags=flags, **kw)
    return p.pid


def main():
    os.makedirs(CFG["log_dir"], exist_ok=True)    # Codex 17: 처음 실행하는 PC 에서도 기록·상태 폴더부터
    os.makedirs(CFG["state_dir"], exist_ok=True)
    try:
        pid = int(open(CFG["pid_file"]).read().strip())
    except Exception:  # noqa: BLE001
        pid = None
    # 2026-10-24 F-02: 살아 있는지는 OS 수준 단일 실행 잠금으로 본다 (잠금을 못 잡으면 상주 프로그램이 돌고 있음)
    from common import resident_running
    ok = resident_running()
    if not ok and os.path.exists(os.path.join(CFG["log_dir"], "resident.stop")):
        log("정지 요청 상태 (resident.stop) -> 다시 켜지 않음 (start_resident 로 켬)")
        return
    hb = last_heartbeat()
    stale_min = (pd.Timestamp.now() - hb).total_seconds() / 60 if hb is not None else None
    if ok and stale_min is not None and stale_min >= CFG["heartbeat_stale_minutes"]:
        if not os.path.exists(FLAG):
            N.send(f"[하트비트 끊김] 마지막 {hb:%H:%M:%S} ({stale_min:.0f}분 전), 프로세스 pid {pid} 는 살아 있음",
                   log_path=os.path.join(CFG["log_dir"], "alerts.log"))
            open(FLAG, "w").write(str(hb))
        log(f"heartbeat stale {stale_min:.1f} min, pid {pid} alive")
    elif os.path.exists(FLAG) and stale_min is not None and stale_min < CFG["heartbeat_stale_minutes"]:
        os.remove(FLAG)
    if not ok:
        new = start()
        N.send(f"[감시] 상주 프로그램이 없어 다시 켬 (이전 pid {pid}, 새 pid {new}, 마지막 하트비트 {hb})",
               log_path=os.path.join(CFG["log_dir"], "alerts.log"))
        log(f"restart: old pid {pid} not alive, new pid {new}")
    else:
        log(f"ok pid {pid}, heartbeat {hb}")


if __name__ == "__main__":
    main()
