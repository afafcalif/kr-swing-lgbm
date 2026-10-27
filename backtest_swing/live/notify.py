"""텔레그램 알림 (.env 의 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). 값이 없으면 로그에만 남긴다. 토큰은 로그에 쓰지 않는다."""
import contextlib
import os
import queue
import threading
import time

import pandas as pd


def _env():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"))
    except Exception:  # noqa: BLE001
        pass
    return os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")


_Q, _T = queue.Queue(), None
_LOCK = threading.Lock()
_FLOCK = threading.Lock()  # 미전송 파일 읽기·쓰기 (알림 파일 스레드만 씀). 프로세스 사이는 _os_lock
LOCK_WAIT = 10.0           # 프로세스 사이 잠금 대기 상한 (초). 못 잡으면 쓰지 않음 (11J 보완 3 N3)
SYNC_WAIT = 0.5            # 알림 파일 스레드가 한 번에 잠금을 기다리는 상한 (초). 못 잡으면 다음 차례에 다시
_OPS = {}                  # 미전송 파일 경로 -> {"add": {키: 기록}, "drop": {키}} : 아직 파일에 못 쓴 변경 (메모리, N3)
_WAKE = threading.Event()
_FT = None
_SEQ = [0]
_RUN = f"{pd.Timestamp.now():%Y%m%d%H%M%S}-{os.getpid()}"  # 알림 번호는 실행마다 1 부터라 파일 키 = 실행 표시 + 번호 (11J 보완 2 N6)
_PENDING = {}  # 번호 -> {n, time, text, path} : 큐에 넣었는데 아직 보내지 못한 것 (보내기 실패 포함)


def _log(log_path, line):
    if log_path:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _deliver(text, sender=None):
    """텔레그램 보내기 (최대 3번). 반환: True 보냄, False 못 보냄, None 텔레그램 설정 없음"""
    if sender is not None:
        return bool(sender(text))
    token, chat = _env()
    if not token or not chat:
        return None
    import requests
    for i in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": text}, timeout=10)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2 * (i + 1))
    return False


@contextlib.contextmanager
def _os_lock(path, wait=LOCK_WAIT):
    """11J 보완 3 R4: 미전송 파일의 프로세스 사이 잠금 (별도 path.lock 파일, Windows msvcrt / 그 밖 fcntl).
    종료 중인 이전 실행과 새로 시작한 실행이 같은 파일을 동시에 고쳐도 한쪽 기록이 사라지지 않게. 반환(yield): 잠금을 잡았는지"""
    if not path:
        yield False
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    f = open(path + ".lock", "a+b")
    got, end = False, time.time() + wait
    try:
        while not got:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
            except OSError:
                if time.time() >= end:
                    break
                time.sleep(0.01)
        yield got
    finally:
        if got:
            try:
                if os.name == "nt":
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


def _read_pending(path):
    """미전송 파일 -> {키: {time, text}}. 예전 형식(목록)은 키를 새로 붙여 읽는다. 못 읽으면 빈 dict"""
    import json
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            j = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(j, list):
        return {f"old-{i + 1}": {"time": x.get("time"), "text": x.get("text")} for i, x in enumerate(j) if isinstance(x, dict)}
    return {str(k): v for k, v in j.items() if isinstance(v, dict)} if isinstance(j, dict) else {}


def _pending_edit(path, add=None, drop=(), wait=LOCK_WAIT):
    """미전송 파일을 알림 번호(키) 단위로 고친다 (11J 보완 2 N6): add = {키: {time, text}} 넣기, drop = 지울 키. 비면 파일을 지운다.
    11J 보완 3 R4·N3: 프로세스 사이 잠금 안에서 파일을 다시 읽어 합친 뒤 쓴다. 잠금을 못 잡으면 쓰지 않는다. 반환: 썼으면 True"""
    if not path:
        return True
    from common import atomic_json
    with _FLOCK, _os_lock(path, wait) as got:
        if not got:
            return False
        items = _read_pending(path)
        items.update(add or {})
        for k in drop:
            items.pop(k, None)
        if items:
            atomic_json(path, items)
        elif os.path.exists(path):
            os.remove(path)
    return True


def _op(path, add=None, drop=()):
    """미전송 파일 변경을 메모리에만 쌓는다 (파일·잠금은 알림 파일 스레드, N3)"""
    if not path:
        return
    with _LOCK:
        o = _OPS.setdefault(path, {"add": {}, "drop": set()})
        o["add"].update(add or {})
        o["drop"].update(drop)
    _WAKE.set()


def _sync(wait=SYNC_WAIT):
    """쌓인 변경을 파일에 쓴다 (알림 파일 스레드만 부름). 잠금을 못 잡은 경로는 그대로 두고 다음 차례에. 반환: 다 썼으면 True"""
    with _LOCK:
        snap = {p: {"add": dict(o["add"]), "drop": set(o["drop"])} for p, o in _OPS.items()}
    done = True
    for p, s in snap.items():
        if not _pending_edit(p, s["add"], s["drop"], wait=wait):
            done = False
            continue
        with _LOCK:
            o = _OPS.get(p)
            if o is None:
                continue
            for k, v in s["add"].items():
                if o["add"].get(k) is v:
                    o["add"].pop(k)
            o["drop"] -= s["drop"]
            if not o["add"] and not o["drop"]:
                _OPS.pop(p)
    return done


def _file_worker():
    """11J 보완 3 N3: 미전송 파일 기록·잠금은 이 스레드만 (매도·주문 경로는 메모리 큐에만 넣고 바로 돌아옴). 텔레그램 보내기와 따로라
    보내기가 오래 걸려도 기록이 늦어지지 않음. 잠금을 못 잡으면(다른 실행이 잡고 있음) 쓰지 않고 잠시 뒤 다시"""
    while True:
        _WAKE.wait(0.2)
        _WAKE.clear()
        try:
            if _OPS:
                _sync()
        except Exception:  # noqa: BLE001
            pass


def _worker():
    while True:
        n, text, log_path, sender, pending_path, keys = _Q.get()
        try:
            ok = _deliver(text, sender)
            _log(log_path, f"{pd.Timestamp.now():%Y-%m-%d %H:%M:%S}   -> [#{n}] 텔레그램 "
                           + {True: "보냄", False: "보내지 못함 (다음 시작 때 다시)", None: "설정 없음 (로그만)"}[ok])
            if ok is not False:
                with _LOCK:
                    _PENDING.pop(n, None)
                _op(pending_path, drop=keys)  # 보낸(또는 설정 없음) 번호만 지운다. 못 보낸 것은 파일에 그대로
        except Exception:  # noqa: BLE001  (알림 실패가 다음 알림을 막지 않게)
            pass
        finally:
            _Q.task_done()


def _enqueue(text, log_path, sender, pending_path, keys=None):
    """큐에 넣기 (메모리만, 파일 잠금 기다림 없음 N3). keys None = 새 알림: 미전송 파일에 이 번호를 기록하게 쌓음 (보내면 지움).
    keys 주면(지난 미전송 다시 보내기) 그 키들을 보내면 지움"""
    global _T, _FT
    with _LOCK:
        _SEQ[0] += 1
        n = _SEQ[0]
        t = pd.Timestamp.now()
        _PENDING[n] = {"n": n, "time": f"{t:%Y-%m-%d %H:%M:%S}", "text": text, "path": pending_path}
    if keys is None:
        keys = [f"{_RUN}#{n}"]
        _op(pending_path, add={keys[0]: {"time": f"{t:%Y-%m-%d %H:%M:%S}", "text": text}})
    _log(log_path, f"{t:%Y-%m-%d %H:%M:%S} [#{n}] {text}")
    if _FT is None or not _FT.is_alive():
        _FT = threading.Thread(target=_file_worker, daemon=True, name="notify-file")
        _FT.start()
    if _T is None or not _T.is_alive():
        _T = threading.Thread(target=_worker, daemon=True, name="notify")
        _T.start()
    _Q.put((n, text, log_path, sender, pending_path, list(keys)))
    return n


def send_async(text, log_path=None, sender=None, pending_path=None):
    """11J: 상주 프로그램용. 큐에 넣는 순간 log_path(alerts.log)에 먼저 쓰고 바로 돌아온다 (본체 대기 0).
    텔레그램 보내기는 별도 스레드가 차례로, 결과는 같은 번호로 따로 한 줄.
    11J 보완 2 (N6): 넣을 때 pending_path 에 그 번호를 기록하게 하고(알림 파일 스레드가 바로 씀, 11J 보완 3 N3), 보내기에 성공하면 그 번호만 지운다"""
    return _enqueue(text, log_path, sender, pending_path)


def flush(timeout=15.0, pending_path=None):
    """큐에 남은 알림을 보내고 미전송 파일 기록이 끝날 때까지 기다림 (종료할 때). 못 보낸 것은 pending_path 에 있다. 반환: 다 보내고 다 썼으면 True"""
    end = time.time() + timeout
    while (_Q.unfinished_tasks or _OPS) and time.time() < end:
        _WAKE.set()
        time.sleep(0.05)
    with _LOCK:
        left = any(x.get("path") == pending_path for x in _PENDING.values()) or pending_path in _OPS
    return not left


def resend_pending(pending_path, log_path=None, sender=None, chunk=10):
    """시작할 때: 지난번에 못 보낸 알림을 "[지난 미전송 알림]" 으로 다시 보낸다 (11J 보완 2 N7). 파일은 지우지 않고 읽기만 하고,
    chunk 건씩 한 통으로 큐에 넣어 그 통이 보내지면 그 통에 든 번호만 지운다 (복원 중 종료 = 파일에 그대로, 다음 시작 때 다시).
    텔레그램 한 통 길이(4096자) 안에 들게 한 줄 300자, 한 통 10건. 반환: 건수"""
    with _FLOCK, _os_lock(pending_path):  # 읽기만 (잠금을 못 잡아도 읽음: 쓰기는 원자적 바꾸기라 반쯤 쓴 파일은 없음)
        got = _read_pending(pending_path)
    items = sorted(((k, v) for k, v in got.items() if not k.startswith(f"{_RUN}#")),  # 이번 실행 알림은 제외 (따로 보내는 중)
                   key=lambda kv: (str(kv[1].get("time")), kv[0]))
    if not items:
        return 0
    parts = [items[i:i + chunk] for i in range(0, len(items), chunk)]
    for j, part in enumerate(parts):
        lines = [f"{x.get('time')} {x.get('text')}"[:300] for _, x in part]
        head = f"[지난 미전송 알림] {len(items)}건" + (f" ({j + 1}/{len(parts)})" if len(parts) > 1 else "")
        _enqueue(head + "\n" + "\n".join(lines), log_path, sender, pending_path, keys=[k for k, _ in part])
    return len(items)


def send(text, log_path=None, sender=None):
    """반환: 보냈으면 True. sender 는 시험용 (기본은 텔레그램 HTTP)"""
    line = f"{pd.Timestamp.now():%Y-%m-%d %H:%M:%S} {text}"
    if log_path:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    token, chat = _env()
    if sender is not None:
        return sender(text)
    if not token or not chat:
        return False
    import requests
    ok = False
    for i in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": text}, timeout=10)
            if r.status_code == 200:
                ok = True
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2 * (i + 1))
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{pd.Timestamp.now():%Y-%m-%d %H:%M:%S}   -> 텔레그램 {'보냄' if ok else '보내지 못함'}\n")
    return ok
