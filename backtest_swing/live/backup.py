"""
운영 데이터 백업 (EXEC_SPEC 11M, 2026-10-28). 서브 PC 작업 스케줄러 <백업 작업> 이 매일 18:00 pythonw(창 없음)로 실행한다 (register_tasks.ps1 -Backup).

  pythonw backtest_swing/live/backup.py [--dest \\\\메인PC\\<공유 이름>]      # 백업 (대상: --dest, 없으면 설정 backup.dest)
  python  backtest_swing/live/backup.py --verify [--date YYYY-MM-DD] [--dest ...]  # 대상의 목록 파일과 해시 대조 (기본: 마지막 목록 + 모든 날짜 사본)

- 상주 모듈(common·resident·orders·positions·state ...)을 import 하지 않는다 (settings·notify 만). 원본은 읽기만 한다. 실패해도 매매에 영향 없음.
- 장중(거래일 day_start 07:00 ~ day_end, krx_special_hours.json 거래시간 변경일 반영)에 실행되면 아무것도 안 하고 끝낸다.
- 순서: 장후 특징값 저장(features_eod.py --pending, 별도 프로세스, 실패해도 계속) -> 그날 사본을 로컬 대기 폴더(logs/exec/backup_pending/{날짜}/)에 ->
  대상 확인(대상 폴더 안 표지 파일 KR_SWING_BACKUP) -> 대기 사본을 대상 daily/{날짜}/ 로 옮김 -> 미러 -> 일봉 추가분 버전 솎기 -> 목록 파일 manifests/{날짜}.json.
- 대상에 못 닿으면(설정 없음·메인 PC 꺼짐·네트워크·표지 파일 없음) 복사하지 않고 기록만. 실패한 날이 3일 연속이면 텔레그램 알림 (하루 한 번). 닿는 날 밀린 것을 한꺼번에.
- 미러(mirror/): logs/exec, data/live_append, models_live/stage5, models_live/datasets/*/dataset.parquet, data/universe_*.csv, data/universe_current.json.
  바뀐 파일(크기·수정 시각)만 복사, 대상에서는 지우지 않음 (일봉 추가분 버전만 '최근 10거래일 + 월말 버전 + 현재' 로 솎음, 원본은 그대로). 임시 이름으로 쓰고 이름 바꾸기.
  *.new 폴더(학습 중)·logs/exec/backup_pending 은 건너뜀.
- 그날 사본(daily/{날짜}/): summary.csv, positions.json(+.bak), ledger.json, state/{날짜}.json, state/intents/{날짜}.json, 그날 로그 json ({날짜}.json, {날짜}_*.json).
  날짜 = 끝난 마지막 거래일 (거래일 day_end 뒤면 그날, 아니면 직전 거래일). 하루 한 번만 만든다 (덮어쓰는 파일의 그날 상태).
- 절대 제외: 위 목록 밖 전부 + 이름 필터 .env*, *token*, config.json, *.pid, *.lock (경로의 어느 부분이든). logs 쪽 파일(바이너리 제외)은 복사 전에 로컬 .env 의
  키·시크릿·계좌·토큰 값이 들어 있는지 검사하고, 걸리면 그 파일은 건너뛰고 알림 (값은 쓰지 않음). 모델·일봉 파일은 검사하지 않음.
- 기록: logs/exec/backup.log, 상태 logs/exec/backup_state.json (만든 날짜 사본, 연속 실패 날짜), 해시 캐시 logs/exec/backup_index.json.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import settings  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(HERE))
MARKER = "KR_SWING_BACKUP"
EXCLUDE = (".env*", "*token*", "config.json", "*.pid", "*.lock", "*.tmp", "*.kr_tmp")
BINARY_EXT = (".parquet", ".npz", ".npy", ".pkl", ".pth", ".zip", ".gz", ".txt.gz")
SECRET_NAME = ("KEY", "SECRET", "TOKEN", "ACCOUNT", "ACNT", "PASSWORD", "PASSWD")
NOT_SECRET_SUFFIX = ("_PATH", "_URL", "_DIR", "_FILE", "_LEVEL", "_ENV")
FAIL_ALERT_DAYS = 3
KEEP_TRADING_DAYS = 10
FEATURES_TIMEOUT = 900   # 장후 특징값 저장 상한 (초). 넘으면 끝내고 백업은 계속 (작업 제한 30분 안)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
BELOW_NORMAL = 0x00004000 if os.name == "nt" else 0
VERSION_RE = re.compile(r"^\d{14,20}$")


def _hms_shift(hms, minutes):
    return hms if not minutes else (pd.Timestamp(f"2000-01-01 {hms}") + pd.Timedelta(minutes=minutes)).strftime("%H:%M:%S")


def _minutes(hms):
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 60 + m + s / 60


class Calendar:
    """거래일·하루 끝 (state.py 와 같은 규칙을 파일에서 직접 읽음: 주말 + krx_holidays.json 휴장일, krx_special_hours.json 폐장 지연만큼 하루 끝을 밂)"""

    def __init__(self, holidays_path=None, special_path=None, times=None):
        hp = holidays_path or os.path.join(HERE, "krx_holidays.json")
        sp = special_path or os.path.join(HERE, "krx_special_hours.json")
        h = jload(hp) if os.path.exists(hp) else {}
        self.hol = {pd.Timestamp(x).normalize() for k, v in h.items() if not k.startswith("_") for x in v}
        s = jload(sp) if os.path.exists(sp) else {}
        self.special = {pd.Timestamp(k).normalize(): v for k, v in s.items() if not k.startswith("_")}
        self.times = times or {}

    def trading(self, day):
        day = pd.Timestamp(day).normalize()
        return day.weekday() < 5 and day not in self.hol

    def prev_trading(self, day):
        d = pd.Timestamp(day).normalize() - pd.Timedelta(days=1)
        for _ in range(60):
            if self.trading(d):
                return d
            d -= pd.Timedelta(days=1)
        raise ValueError(f"{day} 앞 60일 안에 거래일 없음")

    def day_end(self, day):
        """그날 하루 끝 (평소 16:30, 폐장 지연일은 그만큼 늦게)"""
        e = self.special.get(pd.Timestamp(day).normalize()) or {}
        shift = round(_minutes(e.get("close", "15:30:00")) - _minutes("15:30:00"))
        return _hms_shift(self.times.get("day_end", "16:30:00"), shift)

    def in_session(self, t):
        """장중 = 거래일 day_start(07:00, 밀지 않음) ~ day_end"""
        t = pd.Timestamp(t)
        hms = t.strftime("%H:%M:%S")
        return self.trading(t) and self.times.get("day_start", "07:00:00") <= hms < self.day_end(t)

    def label_day(self, t):
        """그날 사본의 날짜 = 끝난 마지막 거래일"""
        t = pd.Timestamp(t)
        d = t.normalize()
        if self.trading(d) and t.strftime("%H:%M:%S") >= self.day_end(d):
            return d
        return self.prev_trading(d)

    def keep_from(self, today, n=KEEP_TRADING_DAYS):
        """오늘(거래일이면 포함)부터 거꾸로 n 번째 거래일"""
        d = pd.Timestamp(today).normalize()
        if not self.trading(d):
            d = self.prev_trading(d)
        for _ in range(n - 1):
            d = self.prev_trading(d)
        return d


def excluded(rel):
    """이름 필터: 경로의 어느 부분이든 걸리면 제외"""
    return any(fnmatch.fnmatch(part.lower(), pat) for part in rel.replace("\\", "/").split("/") for pat in EXCLUDE)


def env_secret_values(paths):
    """로컬 .env 의 키·시크릿·계좌·토큰 값 (이름으로 고름, 경로·주소·수준 값은 뺌, 6자 미만은 뺌). 값은 어디에도 쓰지 않는다"""
    vals = []
    for p in paths:
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
        for line in lines:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k, v = k.strip().upper().removeprefix("EXPORT ").strip(), v.strip().strip('"').strip("'")
            if any(n in k for n in SECRET_NAME) and not k.endswith(NOT_SECRET_SUFFIX) and len(v) >= 6:
                vals.append(v)
    return sorted(set(vals))


def secret_matchers(vals):
    # 숫자만인 값(계좌 번호 등)은 앞뒤가 숫자·소수점이 아닐 때만 (make_bundle.scan_zip 과 같은 규칙)
    return [re.compile(rb"(?<![\d.])" + re.escape(v.encode()) + rb"(?!\d)") if v.isdigit() else re.compile(re.escape(v.encode())) for v in vals]


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_atomic(src, dst):
    """임시 이름(dst.kr_tmp)으로 쓰고(내용 해시 계산, 수정 시각 복사) 이름 바꾸기. 반환: (sha256, 크기)"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".kr_tmp"
    h, n = hashlib.sha256(), 0
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            for chunk in iter(lambda: fi.read(1 << 20), b""):
                h.update(chunk)
                fo.write(chunk)
                n += len(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        shutil.copystat(src, tmp)
        for i in range(10):
            try:
                os.replace(tmp, dst)
                break
            except PermissionError:
                if i == 9:
                    raise
                time.sleep(0.1 * (i + 1))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return h.hexdigest(), n


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".kr_tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class InstanceLock:
    """backup.py 하나만 (작업 + 손으로 실행이 겹치지 않게). OS 잠금이라 프로세스가 죽으면 풀린다"""

    def __init__(self, path):
        self.path, self.fh = path, None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
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
        except OSError:
            pass
        self.fh.close()
        self.fh = None


class Unreachable(Exception):
    pass


class Backup:
    def __init__(self, dest, root=ROOT, state_dir=None, times=None, now=None, send=None, run_features=None,
                 env_files=None, holidays_path=None, special_path=None, echo=False):
        self.root = root
        self.dest = dest or ""
        self.log_dir = os.path.join(root, "logs", "exec")
        self.state_dir = state_dir or os.path.join(self.log_dir, "state")
        self.cal = Calendar(holidays_path, special_path, times)
        self.now = now or (lambda: pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None))
        self._send = send
        self.run_features = run_features or self._run_features
        self.env_files = env_files if env_files is not None else [os.path.join(root, ".env"), os.path.join(root, "kiwoom", ".env")]
        self.pending_dir = os.path.join(self.log_dir, "backup_pending")
        self.log_path = os.path.join(self.log_dir, "backup.log")
        self.state_path = os.path.join(self.log_dir, "backup_state.json")
        self.index_path = os.path.join(self.log_dir, "backup_index.json")
        self._matchers = None
        self.echo = echo  # 화면에도 (손으로 실행할 때)

    # ------------------------------------------------------------ 기록·알림

    def log(self, msg, level="INFO"):
        line = " ".join(str(msg).split("\n"))
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{self.now():%Y-%m-%d %H:%M:%S} {level:5s} {line}\n")
        except Exception:  # noqa: BLE001
            pass
        if self.echo and sys.stdout is not None:
            try:
                print(line, flush=True)
            except Exception:  # noqa: BLE001
                pass

    def send(self, text):
        self.log(f"알림: {text}", "WARN")
        try:
            if self._send is not None:
                return self._send(text)
            import notify as N
            return N.send(text, log_path=os.path.join(self.log_dir, "alerts.log"))
        except Exception as e:  # noqa: BLE001
            self.log(f"알림 보내기 실패: {e!r}", "WARN")

    def load_state(self):
        try:
            return jload(self.state_path)
        except Exception:  # noqa: BLE001
            return {}

    def save_state(self, st):
        write_json_atomic(self.state_path, st)

    # ------------------------------------------------------------ 비밀 값 검사

    def matchers(self):
        if self._matchers is None:
            self._matchers = secret_matchers(env_secret_values(self.env_files))
        return self._matchers

    def has_secret(self, path):
        """logs 쪽 파일(바이너리 확장자 제외)에 .env 값이 들어 있는가"""
        if path.lower().endswith(BINARY_EXT) or not self.matchers():
            return False
        with open(path, "rb") as f:
            data = f.read()
        return any(m.search(data) for m in self.matchers())

    # ------------------------------------------------------------ 장후 특징값

    def _run_features(self):
        out = os.path.join(self.log_dir, "features", "features_eod.out.log")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        t0 = time.time()
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"--- {self.now():%Y-%m-%d %H:%M:%S} features_eod --pending\n")
            f.flush()
            p = subprocess.Popen([sys.executable, os.path.join(HERE, "features_eod.py"), "--pending"], cwd=self.root, stdout=f, stderr=f,
                                 env={**os.environ, "PYTHONIOENCODING": "utf-8"}, creationflags=NO_WINDOW | BELOW_NORMAL)
            try:
                rc = p.wait(timeout=FEATURES_TIMEOUT)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(30)
                rc = f"상한 {FEATURES_TIMEOUT}초 초과 -> 끝냄"
        return {"rc": rc, "seconds": round(time.time() - t0, 1)}

    # ------------------------------------------------------------ 그날 사본 (로컬 대기 폴더)

    def daily_sources(self, day):
        """(대상 안 이름, 원본 경로)"""
        d = str(pd.Timestamp(day).date())
        L, S = self.log_dir, self.state_dir
        out = [("summary.csv", os.path.join(L, "summary.csv")), ("ledger.json", os.path.join(L, "ledger.json")),
               ("state/positions.json", os.path.join(S, "positions.json")), ("state/positions.json.bak", os.path.join(S, "positions.json.bak")),
               (f"state/{d}.json", os.path.join(S, f"{d}.json")), (f"state/intents/{d}.json", os.path.join(S, "intents", f"{d}.json"))]
        if os.path.isdir(L):
            for n in sorted(os.listdir(L)):
                if n == f"{d}.json" or (n.startswith(f"{d}_") and n.endswith(".json")):
                    out.append((n, os.path.join(L, n)))
        return out

    def capture(self, day):
        """끝난 마지막 거래일의 사본을 logs/exec/backup_pending/{날짜}/ 에 (하루 한 번). 반환: 기록용 dict"""
        d = str(pd.Timestamp(day).date())
        st = self.load_state()
        if d in st.get("captured", {}):
            return {"day": d, "skip": "이미 만든 날"}
        pd_dir = os.path.join(self.pending_dir, d)
        shutil.rmtree(pd_dir, ignore_errors=True)  # 지난번에 만들다 만 것
        files, missing, secret = [], [], []
        for name, src in self.daily_sources(d):
            if not os.path.isfile(src):
                missing.append(name)
                continue
            if excluded(name):
                continue
            if self.has_secret(src):
                secret.append(name)
                continue
            sha, n = copy_atomic(src, os.path.join(pd_dir, name))
            files.append({"path": name, "size": n, "sha256": sha})
        man = {"day": d, "captured": f"{self.now():%Y-%m-%d %H:%M:%S}", "files": files, "missing": missing, "skipped_secret": secret}
        write_json_atomic(os.path.join(pd_dir, "MANIFEST.json"), man)
        st = self.load_state()
        st.setdefault("captured", {})[d] = man["captured"]
        st["captured"] = dict(sorted(st["captured"].items())[-90:])
        self.save_state(st)
        if secret:
            self.send(f"[백업 비밀 값] 그날 사본 {d}: .env 값이 들어 있는 파일을 넣지 않음 {secret}")
        return {"day": d, "files": len(files), "missing": missing, "skipped_secret": secret}

    def move_pending(self):
        """대기 사본을 대상 daily/{날짜}/ 로: 파일마다 임시 이름 + 해시 확인 -> 목록 파일(MANIFEST.json)을 마지막에 -> 로컬 대기 폴더 지움"""
        moved = []
        if not os.path.isdir(self.pending_dir):
            return moved
        for d in sorted(os.listdir(self.pending_dir)):
            src_dir = os.path.join(self.pending_dir, d)
            mp = os.path.join(src_dir, "MANIFEST.json")
            if not os.path.exists(mp):
                continue  # 만들다 만 것 (다음 capture 가 다시 만들거나, 지난날이면 그대로 둠)
            man = jload(mp)
            out = os.path.join(self.dest, "daily", d)
            for f in man["files"]:
                sha, _ = copy_atomic(os.path.join(src_dir, f["path"]), os.path.join(out, f["path"]))
                if sha != f["sha256"]:
                    raise RuntimeError(f"그날 사본 {d}/{f['path']}: 옮긴 해시가 다름")
            copy_atomic(mp, os.path.join(out, "MANIFEST.json"))
            shutil.rmtree(src_dir, ignore_errors=True)
            moved.append(d)
        return moved

    # ------------------------------------------------------------ 미러

    def _walk(self, rel_dir, skip_top=()):
        base = os.path.join(self.root, rel_dir)
        out = []
        for cur, dirs, files in os.walk(base):
            rcur = os.path.relpath(cur, self.root).replace("\\", "/")
            dirs[:] = sorted(x for x in dirs if not x.endswith(".new") and not excluded(x)
                             and not (cur == base and x in skip_top))
            out += [f"{rcur}/{f}" for f in sorted(files)]
        return out

    def mirror_files(self):
        """미러 대상 (root 기준 상대 경로, / 구분). 정해 둔 목록 밖은 없다"""
        files = self._walk("logs/exec", skip_top=("backup_pending",))
        sd_rel = os.path.relpath(self.state_dir, self.root).replace("\\", "/")
        if not sd_rel.startswith(("logs/exec", "..")) and not os.path.isabs(sd_rel):
            files += self._walk(sd_rel)  # state_dir 을 logs/exec 밖으로 바꾼 PC
        files += self._walk("data/live_append") + self._walk("models_live/stage5")
        ds = os.path.join(self.root, "models_live", "datasets")
        if os.path.isdir(ds):
            files += [f"models_live/datasets/{m}/dataset.parquet" for m in sorted(os.listdir(ds))
                      if not m.endswith(".new") and os.path.isfile(os.path.join(ds, m, "dataset.parquet"))]
        dd = os.path.join(self.root, "data")
        if os.path.isdir(dd):
            files += [f"data/{n}" for n in sorted(os.listdir(dd))
                      if (fnmatch.fnmatch(n, "universe_*.csv") or n == "universe_current.json") and os.path.isfile(os.path.join(dd, n))]
        return [f for f in dict.fromkeys(files) if not excluded(f)]

    def mirror(self, st):
        idx = {}
        try:
            idx = jload(self.index_path)
        except Exception:  # noqa: BLE001
            pass
        new_idx, entries, copied, secret, gone = {}, [], 0, [], []
        alerted = st.setdefault("secret_alerted", {})
        for rel in self.mirror_files():
            src = os.path.join(self.root, rel)
            dst = os.path.join(self.dest, "mirror", rel)
            try:
                s = os.stat(src)
            except FileNotFoundError:
                gone.append(rel)  # 목록을 만든 뒤 지워짐 (버전 정리 등)
                continue
            try:
                ds = os.stat(dst)
                same = ds.st_size == s.st_size and abs(ds.st_mtime - s.st_mtime) < 2.0
            except FileNotFoundError:
                same = False
            if same:
                c = idx.get(rel)
                sha = c[2] if c and c[0] == s.st_size and c[1] == s.st_mtime_ns else file_sha(src)
                size = s.st_size
            else:
                if rel.startswith("logs/") and self.has_secret(src):
                    secret.append(rel)
                    if alerted.get(rel) != [s.st_size, s.st_mtime_ns]:  # 같은 내용이면 다시 알리지 않음
                        alerted[rel] = [s.st_size, s.st_mtime_ns]
                        self.send(f"[백업 비밀 값] {rel}: .env 값이 들어 있어 복사하지 않음 (값은 기록하지 않음)")
                    continue
                sha, size = copy_atomic(src, dst)
                copied += 1
            new_idx[rel] = [size, s.st_mtime_ns, sha]
            entries.append({"path": f"mirror/{rel}", "size": size, "sha256": sha})
        st["secret_alerted"] = {k: v for k, v in alerted.items() if k in secret}
        write_json_atomic(self.index_path, new_idx)
        return {"files": len(entries), "copied": copied, "skipped_secret": secret, "gone": len(gone)}, entries

    def thin_versions(self):
        """대상 일봉 추가분 버전: 최근 10거래일 + 월말 버전(그 달 마지막) + 대상 CURRENT.json + 원본에 있는 버전만 남김 (원본은 그대로)"""
        vd = os.path.join(self.dest, "mirror", "data", "live_append", "versions")
        if not os.path.isdir(vd):
            return []
        vs = sorted(v for v in os.listdir(vd) if VERSION_RE.match(v) and os.path.isdir(os.path.join(vd, v)))
        keep = set()
        cut = self.cal.keep_from(self.now()).strftime("%Y%m%d")
        keep |= {v for v in vs if v[:8] >= cut}
        by_month = {}
        for v in vs:
            by_month[v[:6]] = max(by_month.get(v[:6], v), v)
        keep |= set(by_month.values())
        try:
            keep.add(jload(os.path.join(self.dest, "mirror", "data", "live_append", "CURRENT.json"))["version"])
        except Exception:  # noqa: BLE001
            pass
        src_v = os.path.join(self.root, "data", "live_append", "versions")
        if os.path.isdir(src_v):
            keep |= set(os.listdir(src_v))
        removed = [v for v in vs if v not in keep]
        for v in removed:
            shutil.rmtree(os.path.join(vd, v), ignore_errors=True)
        return removed

    # ------------------------------------------------------------ 대상

    def reachable(self):
        """(닿음, 이유). 대상 폴더가 있고 그 안에 표지 파일이 있을 때만 닿음"""
        if not self.dest:
            return False, "대상 설정 없음 (backup.dest 또는 --dest)"
        try:
            if not os.path.isdir(self.dest):
                return False, f"대상에 못 닿음 (메인 PC 꺼짐·네트워크·자격 증명): {self.dest}"
            if not os.path.isfile(os.path.join(self.dest, MARKER)):
                return False, f"대상 폴더에 표지 파일 {MARKER} 없음 -> 복사 안 함: {self.dest}"
        except OSError as e:
            return False, f"대상 확인 오류: {e!r}"
        return True, ""

    def failed(self, why):
        today = str(self.now().date())
        st = self.load_state()
        days = sorted(set(st.get("fail_dates", [])) | {today})
        st["fail_dates"], st["last_fail"], st["last_fail_reason"] = days, f"{self.now():%Y-%m-%d %H:%M:%S}", why
        self.save_state(st)
        self.log(f"백업 안 함: {why} (연속 실패 {len(days)}일: {days[0]} ~ {days[-1]}, 로컬 대기 사본 {self.pending_list()})", "WARN")
        if len(days) >= FAIL_ALERT_DAYS and st.get("alerted_on") != today:
            st["alerted_on"] = today
            self.save_state(st)
            self.send(f"[백업 실패 {len(days)}일 연속] {why} (처음 {days[0]}). 밀린 그날 사본 {self.pending_list()} 은 서브 PC 에 있음, 닿는 날 옮김")
        return {"status": "failed", "reason": why, "fail_days": len(days)}

    def pending_list(self):
        return sorted(os.listdir(self.pending_dir)) if os.path.isdir(self.pending_dir) else []

    # ------------------------------------------------------------ 실행

    def run(self):
        now = self.now()
        if self.cal.in_session(now):
            self.log(f"장중 ({self.cal.times.get('day_start', '07:00:00')} ~ {self.cal.day_end(now)}) -> 아무것도 안 하고 끝냄")
            return {"status": "in_session"}
        lk = InstanceLock(os.path.join(self.log_dir, "backup.lock"))
        if not lk.acquire():
            self.log("다른 backup.py 가 실행 중 -> 끝냄", "WARN")
            return {"status": "busy"}
        try:
            return self._run(now)
        finally:
            lk.release()

    def _run(self, now):
        t0 = time.time()
        out = {"started": f"{now:%Y-%m-%d %H:%M:%S}"}
        try:
            out["features"] = self.run_features()
        except Exception as e:  # noqa: BLE001  (특징값 실패는 백업을 막지 않음)
            out["features"] = {"error": repr(e)[:300]}
        self.log(f"장후 특징값: {out['features']}")
        try:
            out["capture"] = self.capture(self.cal.label_day(now))
            self.log(f"그날 사본: {out['capture']}")
        except Exception as e:  # noqa: BLE001
            out["capture"] = {"error": repr(e)[:300]}
            self.log(f"그날 사본 실패: {e!r}", "ERROR")
        ok, why = self.reachable()
        if not ok:
            return {**out, **self.failed(why)}
        sec = self.load_state()  # mirror 가 secret_alerted (비밀 값 알림 보낸 파일) 를 고친다
        try:
            out["moved"] = self.move_pending()
            out["mirror"], entries = self.mirror(sec)
            out["thinned"] = self.thin_versions()
            gone = tuple(f"mirror/data/live_append/versions/{v}/" for v in out["thinned"])
            entries = [e for e in entries if not e["path"].startswith(gone)] if gone else entries
            write_json_atomic(os.path.join(self.dest, "manifests", f"{self.now().date()}.json"),
                              {"run": f"{self.now():%Y-%m-%d %H:%M:%S}", "host": os.environ.get("COMPUTERNAME", ""), "daily_moved": out["moved"],
                               "skipped_secret": out["mirror"]["skipped_secret"], "thinned": out["thinned"], "files": entries})
        except Exception as e:  # noqa: BLE001  (도중에 끊김 등)
            self.save_state({**self.load_state(), "secret_alerted": sec.get("secret_alerted", {})})
            return {**out, **self.failed(f"복사 중 오류: {e!r}"[:300])}
        st = {**self.load_state(), "secret_alerted": sec.get("secret_alerted", {})}
        prev = st.get("fail_dates", [])
        if prev and st.get("alerted_on"):
            self.send(f"[백업 다시 됨] {len(prev)}일 실패 뒤 복사함 (옮긴 그날 사본 {out['moved']})")
        st.update(fail_dates=[], alerted_on=None, last_ok=f"{self.now():%Y-%m-%d %H:%M:%S}")
        self.save_state(st)
        out["status"], out["seconds"] = "ok", round(time.time() - t0, 1)
        self.log(f"백업 끝: 그날 사본 옮김 {out['moved']}, 미러 {out['mirror']['files']}개 중 복사 {out['mirror']['copied']}, "
                 f"비밀 값으로 건너뜀 {out['mirror']['skipped_secret']}, 버전 솎음 {len(out['thinned'])}, {out['seconds']}초")
        return out

    def verify(self, date=None):
        """대상 파일을 목록 파일(manifests/{날짜}.json, 기본 마지막)과 daily/*/MANIFEST.json 의 크기·해시로 대조. 반환 dict(ok, ...)"""
        ok, why = self.reachable()
        if not ok:
            self.log(f"대조 못 함: {why}", "WARN")
            return {"ok": False, "reason": why}
        md = os.path.join(self.dest, "manifests")
        names = sorted(n for n in os.listdir(md) if n.endswith(".json")) if os.path.isdir(md) else []
        pick = f"{date}.json" if date else (names[-1] if names else None)
        bad, n = [], 0
        checks = []
        if pick and os.path.exists(os.path.join(md, pick)):
            checks += [(e["path"], e) for e in jload(os.path.join(md, pick))["files"]]
        elif date or not names:
            bad.append(f"목록 파일 없음: manifests/{pick or '*'}")
        dd = os.path.join(self.dest, "daily")
        days = [date] if date else (sorted(os.listdir(dd)) if os.path.isdir(dd) else [])
        for d in days:
            mp = os.path.join(dd, d, "MANIFEST.json")
            if not os.path.exists(mp):
                if date:
                    bad.append(f"daily/{d}/MANIFEST.json 없음")
                continue
            checks += [(f"daily/{d}/{e['path']}", e) for e in jload(mp)["files"]]
        for rel, e in checks:
            p = os.path.join(self.dest, rel)
            n += 1
            if not os.path.isfile(p):
                bad.append(f"{rel}: 없음")
            elif os.path.getsize(p) != e["size"] or file_sha(p) != e["sha256"]:
                bad.append(f"{rel}: 해시 다름")
        res = {"ok": not bad, "manifest": pick, "daily_days": len(days), "checked": n, "bad": bad[:50], "bad_n": len(bad)}
        self.log(f"대조 {'통과' if res['ok'] else '실패'}: 목록 {pick}, 날짜 사본 {len(days)}일, 파일 {n}개, 문제 {len(bad)} {bad[:5]}",
                 "INFO" if res["ok"] else "WARN")
        return res


def from_config(dest=None, cfg=None):
    cfg = cfg or settings.load()
    return Backup(dest or (cfg.get("backup") or {}).get("dest") or "", state_dir=cfg["state_dir"], times=cfg["times"], echo=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=None)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--date", default=None)
    a = ap.parse_args(argv)
    b = from_config(a.dest)
    if a.verify:
        return 0 if b.verify(a.date)["ok"] else 1
    try:
        r = b.run()
    except Exception as e:  # noqa: BLE001
        b.log(f"백업 오류: {e!r}", "ERROR")
        return 2
    return 0 if r.get("status") in ("ok", "in_session") else 1


if __name__ == "__main__":
    settings.refuse_test_env("backup.py")
    sys.exit(main())
