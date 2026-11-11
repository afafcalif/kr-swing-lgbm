"""
시험 실행 보호 (EXEC_SPEC 11W). 시험에서는 텔레그램·키움·KRX 실제 호출이 원천적으로 안 된다.

- 모든 test_*.py 첫 줄이 install() 을 부른다 (시험 실행 방법·갱신 프로그램 버전과 상관없이 적용. test_netguard 가 첫 줄을 확인).
- 환경 변수: 이름이 토큰·키·계좌 모양이거나 .env 에 있는 이름은 빈 값으로 (빈 값이 있으면 load_dotenv 가 덮어쓰지 않음). 매 시험 시작 때 다시 비움.
- .env: dotenv 를 읽지 않게. 토큰 캐시 경로는 임시 폴더로 (진짜 캐시를 읽거나 지우지 않음).
- 네트워크: 루프백 밖으로 나가는 소켓·DNS·requests 를 막고(NetworkBlocked) 기록. 자식 프로세스도 같음 (testguard_site/sitecustomize.py).
- 실제 호출을 시도하면 코드가 예외를 삼켜도 그 시험이 실패한다. 마지막 시험 뒤에 시도하면 프로세스가 종료 코드 70 으로 끝난다.
- 기록에는 호스트·메서드 이름만 (토큰·주소 전체·값 없음). import 만으로는 아무것도 바꾸지 않는다 (update.py 는 test_env 만 씀).
"""
import atexit
import os
import re
import socket
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SITE_DIR = os.path.join(HERE, "testguard_site")
FLAG, LOG_ENV, OWNER_ENV, CACHE_ENV = "LIVE_TEST_GUARD", "LIVE_TEST_GUARD_LOG", "LIVE_TEST_GUARD_OWNER", "KIWOOM_TOKEN_CACHE_PATH"
SECRET_RE = re.compile(r"TOKEN|SECRET|PASSW|KEY|ACCOUNT|TELEGRAM|KRX|KIWOOM|^MOCK_", re.I)
LOCAL = {"127.0.0.1", "::1", "localhost"}
TEST_ENV = "LIVE_TEST_GUARD_TEST"                                                # 지금 도는 시험 이름 (기록용, 자식 프로세스에도 넘어감)
_state = {"installed": False, "network": False, "requests": False, "seen": 0, "own_log": False}


class NetworkBlocked(ConnectionError):
    """시험 중 실제 네트워크 호출"""


def env_names(root=ROOT):
    """.env 에 있는 이름들 (값은 읽어 두지 않음)"""
    out = set()
    for p in (os.path.join(root, ".env"), os.path.join(root, "kiwoom", ".env")):
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        out.add(line.split("=", 1)[0].replace("export ", "").strip())
        except OSError:
            pass
    return out


def test_env(base=None, root=ROOT):
    """시험 프로세스에 줄 환경 변수 (비밀 이름은 빈 값, 토큰 캐시는 임시 폴더, 보호 켬, 자식 프로세스도 보호)"""
    env = dict(os.environ if base is None else base)
    names = env_names(root)
    for k in set(env) | names:
        if k in names or SECRET_RE.search(k):
            env[k] = ""
    env[CACHE_ENV] = os.path.join(tempfile.gettempdir(), "stock_test_guard_token_cache.json")
    env[FLAG] = "1"
    pp = env.get("PYTHONPATH") or ""
    if SITE_DIR not in pp.split(os.pathsep):
        env["PYTHONPATH"] = SITE_DIR + (os.pathsep + pp if pp else "")
    return env


def _blank_now():
    for k, v in list(os.environ.items()):
        if v and k != CACHE_ENV and SECRET_RE.search(k):
            os.environ[k] = ""


def _note(kind, host, detail=""):
    path = os.environ.get(LOG_ENV)
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{kind} {host} {detail} test={os.environ.get(TEST_ENV) or '-'} pid={os.getpid()} thread={threading.current_thread().name}\n")
        except OSError:
            pass


def _check(kind, host, detail=""):
    if host is None or str(host) in LOCAL:
        return
    _note(kind, host, detail)
    raise NetworkBlocked(f"시험 중 실제 네트워크 호출 차단: {host}")


def _block_network(use_requests):
    """소켓은 언제나, requests 는 시험 프로세스에서만. 따로 한 번씩 (update.py 가 PYTHONPATH 를 넘기면 sitecustomize 가 소켓을 먼저 막고 그 뒤 install 이 requests 를 막음)"""
    if not _state["network"]:
        _state["network"] = True
        _block_socket()
    if use_requests and not _state["requests"]:
        _state["requests"] = True
        _block_requests()


def _block_socket():
    gai, ghbn, ghbnx = socket.getaddrinfo, socket.gethostbyname, socket.gethostbyname_ex
    conn, conn_ex, sendto = socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto

    def host_of(addr):
        return addr[0] if isinstance(addr, tuple) else None                     # 문자열 = 로컬 파일 소켓

    def getaddrinfo(host, *a, **k):
        _check("dns", host)
        return gai(host, *a, **k)

    def gethostbyname(host):
        _check("dns", host)
        return ghbn(host)

    def gethostbyname_ex(host):
        _check("dns", host)
        return ghbnx(host)

    def connect(self, addr):
        _check("connect", host_of(addr))
        return conn(self, addr)

    def connect_ex(self, addr):
        _check("connect", host_of(addr))
        return conn_ex(self, addr)

    def send_to(self, data, *a):
        _check("sendto", host_of(a[-1]))
        return sendto(self, data, *a)

    socket.getaddrinfo, socket.gethostbyname, socket.gethostbyname_ex = getaddrinfo, gethostbyname, gethostbyname_ex
    socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto = connect, connect_ex, send_to


def _block_requests():
    try:
        from urllib.parse import urlsplit

        from requests.adapters import HTTPAdapter
    except ImportError:
        return
    real_send = HTTPAdapter.send

    def send(self, request, *a, **k):
        u = urlsplit(request.url)
        seg = re.sub(r"^bot.*|\d{6,}:.*", "***", u.path.rsplit("/", 1)[-1])[:40]
        _check("requests", u.hostname, f"{seg} {request.headers.get('api-id') or ''}".strip())
        return real_send(self, request, *a, **k)
    HTTPAdapter.send = send


def _block_dotenv():
    try:
        import dotenv
        import dotenv.main as dm
    except ImportError:
        return
    dotenv.load_dotenv = dm.load_dotenv = lambda *a, **k: False
    dotenv.find_dotenv = dm.find_dotenv = lambda *a, **k: ""
    dotenv.dotenv_values = dm.dotenv_values = lambda *a, **k: {}


def take():
    """지난번 뒤로 기록된 실제 호출 시도 (자식 프로세스 포함). 읽은 것은 다시 보이지 않음"""
    path = os.environ.get(LOG_ENV)
    if not path or not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        f.seek(_state["seen"])
        data = f.read()
    _state["seen"] += len(data)
    return data.decode("utf-8", "replace").splitlines()


def _wrap_unittest():
    import unittest
    real_run = unittest.TestCase.run

    def run(self, result=None):
        _blank_now()
        os.environ[TEST_ENV] = self.id()
        got = real_run(self, result)
        new = take()
        if new:
            res = got if got is not None else result
            try:
                raise AssertionError("시험 중 실제 네트워크 호출 시도: " + "; ".join(new)[:500])
            except AssertionError:
                res.addFailure(self, sys.exc_info())
        return got
    unittest.TestCase.run = run


def _at_exit():
    new = take()
    if _state["own_log"]:
        try:
            os.remove(os.environ[LOG_ENV])
        except OSError:
            pass
    if new:
        print("[시험 보호] 마지막 시험 뒤 실제 네트워크 호출 시도: " + "; ".join(new)[:500], file=sys.stderr, flush=True)
        os._exit(70)


def install():
    """시험 프로세스 보호 켜기 (test_*.py 첫 줄). 여러 번 불러도 한 번만"""
    if _state["installed"]:
        return
    _state["installed"] = True
    os.environ.update(test_env())
    owner = OWNER_ENV not in os.environ
    if not os.environ.get(LOG_ENV):
        fd, path = tempfile.mkstemp(prefix="stock_test_guard_", suffix=".log")
        os.close(fd)
        os.environ[LOG_ENV] = path
        _state["own_log"] = True
    _state["seen"] = os.path.getsize(os.environ[LOG_ENV]) if os.path.exists(os.environ[LOG_ENV]) else 0
    _block_dotenv()
    _block_network(True)
    _wrap_unittest()
    if owner:                                                                    # 자식 프로세스는 판정하지 않음 (기록만, 판정은 시험 프로세스)
        os.environ[OWNER_ENV] = str(os.getpid())
        atexit.register(_at_exit)


def install_child():
    """시험이 띄운 자식 프로세스 (sitecustomize): 네트워크만 막음 (환경 변수는 부모가 비워서 넘김)"""
    _block_network(False)
