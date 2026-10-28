"""
실행 코드 공통 (EXEC_SPEC.md). 동결된 전략 코드(stage3~5_*.py)는 고치지 않고 import 해서 쓴다.

경로: logs/exec/ (대조 로그·스냅샷), models_live/stage5/ (그 달 모델), data/live_append/ (전날까지 일봉 추가분)
"""
import hashlib
import math
import os
import subprocess
import sys
import threading
import time
from collections import Counter

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BT = os.path.join(ROOT, "backtest_swing")
for p in (ROOT, BT):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(ROOT)

import stage3_backtest as sb  # noqa: E402,F401
import stage3_data as sd  # noqa: E402,F401
import stage4_features as s4  # noqa: E402,F401
import stage4_model as sm  # noqa: E402,F401
import stage5_features as s5  # noqa: E402,F401
import stage5_model as m  # noqa: E402,F401

LOG_DIR = "logs/exec"
SNAP_DIR = os.path.join(LOG_DIR, "snapshots")
MODEL_DIR = "models_live/stage5"
APPEND_DIR = "data/live_append"
VAL_DIR = "backtest_swing/results/stage5_validation"
CAPITAL = 30_000_000
SLOTS = 33
LIMIT_MULT = 1.05

# 11K 1: 대상 종목 목록은 매월 재학습 때 KRX 종목기본정보로 갱신 (8장 2). 포인터 = [{"from": 적용 시작일, "path": csv}] (monthly.py 가 씀).
# 동결 코드의 stage3_data.UNIVERSE 상수만 그날 적용할 파일로 바꾼다 (파일 형식 같음: code, name, market, ...). 포인터가 없으면 그대로
UNIVERSE_POINTER = "data/universe_current.json"
UNIVERSE_DEFAULT = sd.UNIVERSE


def universe_for(day, pointer=None):
    """day 에 쓸 대상 종목 목록 파일 (포인터에서 from <= day 인 마지막 항목, 없으면 기본 data/universe_20261019.csv)"""
    import json
    try:
        with open(pointer or UNIVERSE_POINTER, encoding="utf-8") as f:
            items = json.load(f).get("items", [])
    except FileNotFoundError:
        return UNIVERSE_DEFAULT
    ok = [i for i in items if str(i["from"]) <= str(pd.Timestamp(day).date()) and os.path.exists(i["path"])]
    return max(ok, key=lambda i: str(i["from"]))["path"] if ok else UNIVERSE_DEFAULT


def apply_universe(day, pointer=None):
    sd.UNIVERSE = universe_for(day, pointer)
    return sd.UNIVERSE


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:  # noqa: BLE001
        return ""


def git_state() -> str:
    """시작 알림용: '커밋 7자리' + 커밋 안 된 변경 여부 (실행 코드 backtest_swing/live, kiwoom). git 이 없으면 'git 없음'"""
    h = git_commit()
    if not h:
        return "git 없음"
    try:
        out = subprocess.check_output(["git", "status", "--porcelain", "--", "backtest_swing/live", "kiwoom", "backtest_swing/EXEC_SPEC.md"],
                                      cwd=ROOT, text=True)
        dirty = any(line.strip() and not line.endswith(".pyc") and "__pycache__" not in line for line in out.splitlines())
    except Exception:  # noqa: BLE001
        dirty = None
    return f"{h[:7]}" + (" (커밋 안 된 변경 있음)" if dirty else "" if dirty is False else " (변경 확인 실패)")


DATA_FILES = ("adj.parquet", "raw.parquet", "index.parquet", "update.json")


def data_dir(append_dir=None):
    """일봉 추가분의 현재 버전 폴더 (확인 리뷰 15): {append_dir}/CURRENT.json 포인터를 따라 versions/{버전}.
    포인터가 없으면 예전 방식(append_dir 바로 아래). 읽는 쪽은 모두 이것(data_file)으로 경로를 얻는다."""
    import json
    ad = append_dir or APPEND_DIR
    try:
        with open(os.path.join(ad, "CURRENT.json"), encoding="utf-8") as f:
            return os.path.join(ad, "versions", json.load(f)["version"])
    except FileNotFoundError:
        return ad


def data_file(name, append_dir=None):
    return os.path.join(data_dir(append_dir), name)


def publish_data(files, append_dir=None, keep=3):
    """새 버전 폴더에 adj·raw·지수·update.json 을 모두 쓴 뒤, 포인터 파일(CURRENT.json) 하나만 원자적으로 바꾼다 (확인 리뷰 15).
    files: 이름 -> DataFrame(parquet) 또는 dict(json). 주지 않은 파일은 현재 버전에서 복사 (한 버전 안에서 서로 맞게).
    중간에 실패하면 포인터를 안 바꿔 이전 버전 그대로. 지난 버전은 최근 keep 개만 남긴다 (지우기 실패는 무시). 반환: 새 버전 이름"""
    import json
    import shutil
    ad = append_dir or APPEND_DIR
    cur = data_dir(ad)
    v = now_kst().strftime("%Y%m%d%H%M%S%f")
    vd = os.path.join(ad, "versions", v)
    os.makedirs(vd)
    try:
        for n in DATA_FILES:
            dst = os.path.join(vd, n)
            if n in files:
                obj = files[n]
                if n.endswith(".json"):
                    with open(dst, "w", encoding="utf-8") as f:
                        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
                else:
                    obj.to_parquet(dst, index=False)
            elif os.path.exists(os.path.join(cur, n)):
                shutil.copyfile(os.path.join(cur, n), dst)
    except BaseException:
        shutil.rmtree(vd, ignore_errors=True)  # 반쯤 쓴 버전 폴더는 지운다 (포인터는 그대로)
        raise
    atomic_json(os.path.join(ad, "CURRENT.json"), {"version": v, "published": now_kst().strftime("%Y-%m-%d %H:%M:%S")})
    vs = sorted(os.listdir(os.path.join(ad, "versions")))
    for old in vs[:-keep]:
        if old != v:
            shutil.rmtree(os.path.join(ad, "versions", old), ignore_errors=True)
    return v


def atomic_json(path, obj):
    """원자적 쓰기: 임시 파일에 쓰고 fsync 뒤 교체"""
    import json
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    # Windows: 백신 실시간 검사 등이 방금 쓴 파일을 잠깐 열고 있으면 교체가 PermissionError 로 실패한다 -> 짧게 다시 (최대 약 10초)
    for i in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.05 * (i + 1))


def pid_alive(pid) -> bool:
    """Windows: tasklist 로 확인 (os.kill(pid, 0) 은 Windows 에서 CTRL_C 신호라 쓰지 않음)"""
    if not pid:
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"], capture_output=True, text=True)
        return f'"{int(pid)}"' in r.stdout
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 한국 시간 (2026-10-24 결정, B5)

KST = "Asia/Seoul"


def now_kst():
    """한국 시간 (시간대 정보 없는 Timestamp). 모든 시간 비교는 이 값으로"""
    return pd.Timestamp.now(tz=KST).tz_localize(None)


def system_tz_ok():
    """시스템 시간대가 한국 시간(UTC+9)인지"""
    import datetime as _dt
    return _dt.datetime.now().astimezone().utcoffset() == _dt.timedelta(hours=9)


# ---------------------------------------------------------------- 단일 실행 잠금 (2026-10-24 결정, F-02)

RESIDENT_LOCK = os.path.join(LOG_DIR, "resident.lock")        # OS 수준 배타 잠금 대상 파일
RESIDENT_INFO = os.path.join(LOG_DIR, "resident.info.json")   # 잠금을 잡은 프로세스 정보 (pid, 시작 시각)


class SingleInstanceLock:
    """OS 수준 배타 잠금 (Windows msvcrt.locking, 그 밖은 fcntl.flock). 프로세스가 죽으면 OS 가 잠금을 푼다"""

    def __init__(self, path=None):
        self.path, self.fh = path or RESIDENT_LOCK, None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def release(self):
        if self.fh is None:
            return
        try:
            self.fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
            self.fh = None


def process_cmdline(pid):
    """Windows: 그 pid 의 명령줄 (모르면 '')"""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def resident_running(lock_path=None, info_path=None, alive=pid_alive, cmdline=process_cmdline) -> bool:
    """상주 프로그램이 돌고 있는가: OS 잠금을 못 잡으면 True. 잠금은 잡혔더라도 정보 파일의 pid 가 살아 있고 그 명령줄이 resident.py 면 True"""
    import json
    lk = SingleInstanceLock(lock_path)
    if not lk.acquire():
        return True
    lk.release()
    ip = info_path or RESIDENT_INFO
    try:
        pid = json.load(open(ip, encoding="utf-8")).get("pid")
    except Exception:  # noqa: BLE001
        return False
    return bool(pid and pid != os.getpid() and alive(pid) and "resident.py" in (cmdline(pid) or ""))


# ---------------------------------------------------------------- 장부 단일 작성자 (2026-10-24 결정, R2-18·F-06·F-07)

LEDGER_PATH = os.path.join(LOG_DIR, "ledger.json")
LEDGER_SHADOW_PATH = os.path.join(LOG_DIR, "ledger_shadow.json")


class LedgerConflict(Exception):
    pass


def ledger_read(path=LEDGER_PATH):
    """반환: (장부, 버전). 버전이 없던 예전 장부는 0"""
    import json
    d = json.load(open(path, encoding="utf-8"))
    return d, int(d.get("version", 0))


def ledger_write(data, expected_version, path=LEDGER_PATH):
    """저장 직전 파일의 버전이 읽었을 때(expected_version)와 다르면 저장 거부. 저장할 때 버전 + 1, 저장 시각 기록"""
    import json
    cur = int(json.load(open(path, encoding="utf-8")).get("version", 0)) if os.path.exists(path) else 0
    if cur != int(expected_version):
        raise LedgerConflict(f"{path}: 읽을 때 버전 {expected_version}, 지금 {cur} -> 저장 거부")
    atomic_json(path, {**data, "version": cur + 1, "saved_at": now_kst().strftime("%Y-%m-%d %H:%M:%S")})
    return cur + 1


def ledger_target(running=None):
    """run_day.py 가 장부를 읽고 쓸 곳 (같은 경로): 상주 프로그램이 돌고 있으면 ledger_shadow.json (없으면 본 장부를 복사해 시작)"""
    import shutil
    if running is None:
        running = resident_running()
    if not running:
        return LEDGER_PATH
    if not os.path.exists(LEDGER_SHADOW_PATH):
        shutil.copyfile(LEDGER_PATH, LEDGER_SHADOW_PATH)
    return LEDGER_SHADOW_PATH


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def num(x) -> float:
    """키움 숫자 문자열 (부호·쉼표 포함) -> 절대값 float. 빈 값은 NaN"""
    s = str(x).replace(",", "").strip()
    return abs(float(s)) if s not in ("", "-", "+") else float("nan")


def tick_size(p: float) -> int:
    """KRX 호가 단위 (2023-01-25 이후 코스피·코스닥 공통)"""
    for lim, t in ((2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50), (200_000, 100), (500_000, 500)):
        if p < lim:
            return t
    return 1_000


def ceil_tick(p: float) -> int:
    """호가 단위로 올림 (구간 경계값은 위 구간의 단위로도 맞는 가격이다)"""
    t = tick_size(p)
    return int(math.ceil(round(p / t, 9)) * t)


def floor_tick(p: float) -> int:
    t = tick_size(p)
    return int(math.floor(round(p / t, 9)) * t)


ORDER_TRS_ALL = {"kt10000", "kt10001", "kt10003"}


class RateGate:
    """11L: 프로세스 전체 공용 키움 호출 제한기 (TR 별 최소 간격, 토큰(서버) 단위). 모든 Caller·Broker 호출이 지난다 (스레드 안전).
    limits = {"mock": {"same_tr": 1.2}, "prod": {"order": 0.25, "query": 0.25, "kt00018": 0.5}} (설정 rate_limits, 상주 프로그램이 run 에서 켬).
      mock: 같은 TR 사이 same_tr 초 (키움 모의: TR 1개당 초당 1회, 주문·조회 모두)
      prod: 주문 TR 끼리 order 초, 조회 TR 끼리 query 초 (키움 실전: 주문·조회 각각 초당 5회 -> 4회), kt00018 끼리 따로 (초당 2회)
    예약 방식: 잠금 안에서 다음 빈 시각을 예약하고 잠금 밖에서 그때까지 잠 (긴 대기가 다른 TR 을 막지 않음, 먼저 온 순서). 꺼져 있으면(limits 비어 있음) 대기 0"""

    lock = threading.Lock()
    klocks = {}  # 제한 키 -> 잠금 (같은 키 직렬화)
    last = {}
    limits = {}
    clock, sleep = staticmethod(time.time), staticmethod(time.sleep)
    waited = Counter()

    @classmethod
    def configure(cls, limits, clock=None, sleep=None):
        cls.limits = {k: dict(v) for k, v in (limits or {}).items() if not k.startswith("_") and isinstance(v, dict)}
        if clock is not None:
            cls.clock = staticmethod(clock)
        if sleep is not None:
            cls.sleep = staticmethod(sleep)
        cls.last = {}

    @classmethod
    def rules(cls, env, tr):
        """이 호출이 지켜야 할 (키, 간격) 목록"""
        lim = cls.limits.get(env) or {}
        out = []
        if "same_tr" in lim:
            out.append(((env, tr), float(lim["same_tr"])))
        cat = "order" if tr in ORDER_TRS_ALL else "query"
        if cat in lim:
            out.append(((env, cat), float(lim[cat])))
        if tr in lim:
            out.append(((env, tr, "tr"), float(lim[tr])))
        return out

    @classmethod
    def interval(cls, env, tr):
        return max([iv for _, iv in cls.rules(env, tr)] or [0.0])

    @classmethod
    def wait(cls, env, tr, max_wait=None):
        """다음 빈 시각까지 기다림. 반환: 기다린 초, max_wait 안에 통과할 수 없으면 None (호출한 쪽이 보내지 않음·조회 실패로).
        11L 보완 2 (1): 같은 제한 키는 잠금으로 직렬화 — 키 잠금을 잡은 채로 기다리고, 깬 뒤 마지막 실제 통과 시각을 다시 확인한 다음
        이번 실제 통과 시각을 남기고 푼다 (먼저 잠든 호출이 늦게 깨도 다음 호출이 그 실제 시각부터 간격을 잼). 다른 키는 막지 않음.
        여러 키를 잡을 때는 정렬한 순서로 (교착 없음). 잠금 대기도 max_wait 안 (실제 시간)"""
        rs = cls.rules(env, tr)
        if not rs:
            return 0.0
        keys = sorted({k for k, _ in rs})
        with cls.lock:
            locks = [cls.klocks.setdefault(k, threading.Lock()) for k in keys]
        t_real, got, waited = time.monotonic(), [], 0.0
        try:
            for lk in locks:
                if max_wait is None:
                    lk.acquire()
                else:
                    left = max_wait - (time.monotonic() - t_real)
                    if left < 0 or not lk.acquire(timeout=max(left, 0.0)):
                        return None
                got.append(lk)
            while True:
                now = cls.clock()
                need = max([0.0] + [cls.last.get(k, -1e18) + iv - now for k, iv in rs])
                if need <= 0:
                    break
                if max_wait is not None and waited + need > max_wait:
                    return None
                cls.waited[tr] += 1
                cls.sleep(need)
                waited += need
            with cls.lock:
                for k, _ in rs:
                    cls.last[k] = now  # 이번 실제 통과 시각 (곧바로 전송)
            return waited
        finally:
            for lk in reversed(got):
                lk.release()

    @classmethod
    def client_gate(cls, env, limiter=None):
        """kiwoom 클라이언트의 send_gate (토큰 준비 뒤 HTTP 전송 바로 앞에서 불림). 공용 제한기에 이 TR 규칙이 있으면 그것만,
        없으면(꺼져 있음: 시험·도구) 클라이언트 자기 제한기 (대기 장치는 늘 하나). 반환 True = 보냄, False = 대기 상한 넘음"""
        def gate(tr, max_wait=None):
            if cls.rules(env, tr):
                return cls.wait(env, tr, max_wait=max_wait) is not None
            if limiter is None:
                return True
            return limiter.wait(max_wait=max_wait) is not False if max_wait is not None else (limiter.wait() or True)
        return gate


class Caller:
    """시세 조회 TR 만. rps 이하로 호출, 유량 초과(HTTP 429 / return_code 5)면 백오프, 연속 5번이면 중단 (EXEC_SPEC 1A)."""

    QUOTE_TRS = {"ka10095", "ka10080", "ka10081", "ka20001", "ka20005", "ka20006"}

    def __init__(self, env="prod", rps=5):
        from kiwoom.client import KiwoomRestClient, RateLimiter
        from kiwoom.config import Settings
        self.c = KiwoomRestClient(Settings.from_env(env))
        self.c.rate_limiter = RateLimiter(limits_per_sec=rps)
        self.c.send_gate = RateGate.client_gate(env, self.c.rate_limiter)  # 11L 보완: 공용 제한기를 전송 바로 앞에서 (대기 장치 하나)
        self.env, self.rps = env, rps
        self.n, self.errors, self.hourly, self.fail_streak, self.stopped = 0, [], Counter(), 0, None

    def post(self, tr, path, body, cont="N", key="", deadline=None, timeout=None):
        """deadline(time.time() 초): 유량 대기·백오프가 그 시각을 넘으면 기다리지 않고 ValueError (손절 감시 한 번의 대기 상한, 11J).
        429 재시도는 여기 한 겹만 (클라이언트 안 재시도는 끔, 11J). timeout: HTTP 요청 하나의 시간 제한 (손절 감시 5초, 11J 보완 3)"""
        assert tr in self.QUOTE_TRS, f"시세 조회 TR 만 허용: {tr}"
        for backoff in (0, 2, 4, 8, 16, 32):
            if backoff:
                if deadline is not None and time.time() + backoff > deadline:
                    raise ValueError(f"시간 상한: {tr} 유량 초과 백오프 {backoff}초가 상한을 넘음")
                time.sleep(backoff)
            # 11L: 프로세스 전체 공용 제한기 (Caller 를 여러 개 만들어도 같은 토큰의 조회 TR 을 합쳐서 제한). 손절 감시의 대기 상한도 지킴
            mw0 = None if deadline is None else max(0.0, deadline - time.time())
            if getattr(self.c, "send_gate", None) is None and RateGate.wait(getattr(self, "env", "prod"), tr, max_wait=mw0) is None:
                raise ValueError(f"시간 상한: {tr} 공용 유량 제한기 대기가 상한을 넘음")  # send_gate 없는 클라이언트(시험용)만 여기서
            self.n += 1
            self.hourly[pd.Timestamp.now().strftime("%Y-%m-%d %H")] += 1
            try:
                mw = None if deadline is None else max(0.0, deadline - time.time())
                r = self.c.request(tr_code=tr, method="POST", path=path, json=body, cont_yn=cont, next_key=key, retry_429=False,
                                   **({"max_wait": mw} if mw is not None else {}), **({"timeout": timeout} if timeout else {}))
            except Exception as e:  # noqa: BLE001
                msg = str(e)[:200]
                if "429" not in msg and "1700" not in msg:
                    self.errors.append((pd.Timestamp.now().strftime("%H:%M:%S"), tr, "other: " + msg))
                    raise ValueError(msg)
                r, err = None, msg
            else:
                err = None
            if r is not None:
                j = r.json()
                if j.get("return_code") in (5, "5"):
                    err = f"return_code 5 {j.get('return_msg')}"
                else:
                    self.fail_streak = 0
                    return j, r.headers.get("cont-yn"), r.headers.get("next-key", "")
            elif err is None:
                err = "HTTP 429"
            self.errors.append((pd.Timestamp.now().strftime("%H:%M:%S"), tr, err))
            self.fail_streak += 1
            if self.fail_streak >= 5:
                self.stopped = err
                raise RuntimeError(f"rate limit repeated: {err}")
        raise RuntimeError("unreachable")


def calendar() -> pd.DatetimeIndex:
    """코스피 지수 파일 + 추가분의 거래일"""
    cal = set(pd.to_datetime(pd.read_parquet(sd.KOSPI, columns=["timestamp"]).timestamp))
    p = data_file("index.parquet")
    if os.path.exists(p):
        ix = pd.read_parquet(p)
        cal |= set(pd.to_datetime(ix[ix.name == "kospi"].timestamp))
    return pd.DatetimeIndex(sorted(cal))


apply_universe(now_kst())  # 11K 1: 이 프로세스가 오늘 쓸 대상 종목 목록 (상주 프로그램은 하루 시작 때마다 다시, run_day rerun 은 그 날짜로)
