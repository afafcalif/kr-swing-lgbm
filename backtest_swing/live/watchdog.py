"""
감시 작업 (EXEC_SPEC 8장 10, 2026-10-22 결정). Windows 작업 스케줄러가 로그온 시와 5분마다 실행 (register_tasks.ps1, 등록은 사용자가).

  python backtest_swing/live/watchdog.py

- 상주 프로그램이 살아 있는지 OS 수준 단일 실행 잠금(logs/exec/resident.lock)으로 확인하고, 아니면 다시 켠다 (2026-10-24 F-02) (텔레그램 "재시작" 은 상주 프로그램이 켜지면서 보냄).
- 하트비트(heartbeat_file 마지막 줄)가 heartbeat_stale_minutes(5분) 이상 끊기면 텔레그램 알림 (끊긴 동안 한 번만, 다시 이어지면 해제).
- 하트비트는 상주 프로그램의 별도 스레드가 찍으므로(11I), 줄 끝의 loop=초(주 반복이 마지막으로 돈 뒤 지난 시간)가 같은 5분을 넘으면
  "[주 반복 멈춤]" 알림을 따로 보낸다 (멈춘 동안 한 번만).
- 11V: 알림은 보내기에 성공했을 때만 "알림 완료"(표시 파일)로 쓴다. 실패면 표시 없이 다음 회차(5분 뒤)에 다시. 한 번뿐인 알림(다시 켬)은
  실패하면 watchdog_pending.json 에 남겨 다음 회차에 먼저 다시 보낸다.
- 11V 보완: 상주 프로그램이 없으면 다시 켜기를 먼저 하고 알림은 그 뒤. 밀린 알림(watchdog_pending.json)은 회차당 30초·5건, 한 건마다 바로 저장.
- 11V: 잠금은 살아 있는데 하트비트 줄이 없거나 읽을 수 없으면, 상주 프로그램 시작(resident.info.json started) 또는 처음 본 때부터
  heartbeat_stale_minutes(5분) 유예 뒤 "[하트비트 없음]" 알림 (없는 동안 한 번만, 다시 보이면 해제).
- 감시 기록: log_dir/watchdog.log
"""
import json
import os
import re
import subprocess
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402
import notify as N  # noqa: E402

CFG = settings.load()
LOG = os.path.join(CFG["log_dir"], "watchdog.log")
FLAG = os.path.join(CFG["state_dir"], "heartbeat_alerted.flag")
LOOP_FLAG = os.path.join(CFG["state_dir"], "loop_stall_alerted.flag")
RESIDENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resident.py")
PENDING_BUDGET, PENDING_MAX = 30, 5  # 11V 보완: 밀린 알림 다시 보내기 회차당 시간(초, 새 건 시작 전 확인)·건수 상한 (작업 제한 2분)
ST_FILES = {"nohb_flag": "heartbeat_missing_alerted.flag", "nohb_since": "heartbeat_missing_since.txt", "pending": "watchdog_pending.json"}


def st(name):
    """11V: 상태 폴더 파일 (부를 때의 CFG 기준): 하트비트 없음 알림 완료 표시·처음 본 때, 보내지 못한 한 번뿐인 알림"""
    return os.path.join(CFG["state_dir"], ST_FILES[name])


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


def last_line():
    try:
        with open(CFG["heartbeat_file"], "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 300))
            return f.read().decode("utf-8", "ignore").strip().splitlines()[-1]
    except Exception:  # noqa: BLE001
        return None


def last_heartbeat():
    try:
        return pd.Timestamp(last_line()[:19])
    except Exception:  # noqa: BLE001
        return None


def loop_age():
    """마지막 하트비트 줄의 loop=초 (주 반복이 마지막으로 돈 뒤 지난 시간). 예전 형식이면 None"""
    m = re.search(r" loop=(\d+)s", last_line() or "")
    return int(m.group(1)) if m else None


def notify(text):
    """보냈으면 True. 실패는 감시 기록에 남김 (11V: 부른 쪽은 성공했을 때만 완료 표시)"""
    ok = N.send(text, log_path=os.path.join(CFG["log_dir"], "alerts.log")) is True
    if not ok:
        log(f"알림 보내지 못함 -> 다음 회차에 다시: {text[:80]}")
    return ok


def alert_once(flag, text, value):
    """11V: 표시 파일이 없을 때만 보내고, 보내기에 성공했을 때만 표시 파일을 쓴다 (실패면 다음 회차에 다시)"""
    if not os.path.exists(flag) and notify(text):
        with open(flag, "w") as f:
            f.write(str(value))


def save_pending(items):
    if items:
        with open(st("pending"), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
    elif os.path.exists(st("pending")):
        os.remove(st("pending"))


def resend_pending(budget=PENDING_BUDGET, cap=PENDING_MAX):
    """11V: 지난 회차에 보내지 못한 한 번뿐인 알림을 다시 (성공한 것만 지움). 11V 보완: 회차당 budget 초(새 건 시작 전 확인)·cap 건까지,
    한 건 보내면 바로 저장, 실패하면 그 회차는 멈춤 (텔레그램이 안 되는 중, 한 건 최대 약 42초) -> 남은 것은 다음 회차"""
    try:
        with open(st("pending"), encoding="utf-8") as f:
            left = list(json.load(f))
    except Exception:  # noqa: BLE001  (없음·손상 = 남은 것 없음)
        return
    end = time.time() + budget
    for n in range(min(cap, len(left))):
        if n and time.time() >= end:
            break
        if not notify(f"[지난 미전송] {left[0]}"):
            break
        left = left[1:]
        save_pending(left)


def notify_or_keep(text):
    """11V: 한 번뿐인 알림 (다시 켬 등). 실패하면 watchdog_pending.json 에 남겨 다음 회차에"""
    if notify(text):
        return
    try:
        with open(st("pending"), encoding="utf-8") as f:
            items = json.load(f)
    except Exception:  # noqa: BLE001
        items = []
    save_pending((items + [f"{pd.Timestamp.now():%Y-%m-%d %H:%M:%S} {text}"])[-20:])


def started_at():
    """상주 프로그램 시작 시각 (log_dir/resident.info.json started = common.RESIDENT_INFO), 모르면 None"""
    try:
        with open(os.path.join(CFG["log_dir"], "resident.info.json"), encoding="utf-8") as f:
            return pd.Timestamp(json.load(f)["started"])
    except Exception:  # noqa: BLE001
        return None


def no_heartbeat(pid):
    """11V: 잠금은 살아 있는데 하트비트 줄이 없거나 읽을 수 없음. 시작(또는 처음 본 때)부터 유예(heartbeat_stale_minutes) 뒤 알림 한 번"""
    now = pd.Timestamp.now()
    try:
        with open(st("nohb_since")) as f:
            seen = pd.Timestamp(f.read().strip())
    except Exception:  # noqa: BLE001
        seen = now
        with open(st("nohb_since"), "w") as f:
            f.write(f"{now:%Y-%m-%d %H:%M:%S}")
    t0 = started_at()
    since = max(seen, t0) if t0 is not None and t0 <= now else seen  # 다시 켜졌으면 그때부터 유예
    wait = (now - since).total_seconds() / 60
    if wait >= CFG["heartbeat_stale_minutes"]:
        alert_once(st("nohb_flag"), f"[하트비트 없음] 상주 프로그램 잠금은 살아 있는데 하트비트 줄이 없거나 읽을 수 없음 ({since:%H:%M:%S} 부터 {wait:.0f}분), "
                              f"pid {pid}, 파일 {CFG['heartbeat_file']}", since)
    log(f"heartbeat missing {wait:.1f} min (since {since:%H:%M:%S}), pid {pid}")


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
    new = None
    if not ok:  # 11V 보완: 다시 켜기를 알림보다 먼저 (알림 한 건 최대 약 42초, 작업 제한 2분)
        new = start()
        log(f"restart: old pid {pid} not alive, new pid {new}")
    resend_pending()
    hb = last_heartbeat()
    stale_min = (pd.Timestamp.now() - hb).total_seconds() / 60 if hb is not None else None
    if ok and stale_min is not None and stale_min >= CFG["heartbeat_stale_minutes"]:
        alert_once(FLAG, f"[하트비트 끊김] 마지막 {hb:%H:%M:%S} ({stale_min:.0f}분 전), 프로세스 pid {pid} 는 살아 있음", hb)
        log(f"heartbeat stale {stale_min:.1f} min, pid {pid} alive")
    elif os.path.exists(FLAG) and stale_min is not None and stale_min < CFG["heartbeat_stale_minutes"]:
        os.remove(FLAG)
    if ok and hb is None:  # 11V: 하트비트 없음·읽을 수 없음
        no_heartbeat(pid)
    elif hb is not None:
        for f in (st("nohb_flag"), st("nohb_since")):
            if os.path.exists(f):
                os.remove(f)
    age = loop_age()
    if ok and age is not None and age >= CFG["heartbeat_stale_minutes"] * 60:  # 11I: 하트비트 스레드는 도는데 주 반복이 멈춤
        alert_once(LOOP_FLAG, f"[주 반복 멈춤] 하트비트는 이어지지만 주 반복이 {age // 60}분째 안 돎 "
                              f"(단계 {(last_line() or '').split('step=')[-1][:40]}), pid {pid}", age)
        log(f"main loop stalled {age}s, pid {pid}")
    elif os.path.exists(LOOP_FLAG) and age is not None and age < CFG["heartbeat_stale_minutes"] * 60:
        os.remove(LOOP_FLAG)
    if not ok:
        notify_or_keep(f"[감시] 상주 프로그램이 없어 다시 켬 (이전 pid {pid}, 새 pid {new}, 마지막 하트비트 {hb})")
    elif hb is not None:
        log(f"ok pid {pid}, heartbeat {hb}")


if __name__ == "__main__":
    settings.refuse_test_env("감시 작업 (watchdog.py)")
    main()
