"""비공개 실행 저장소 → 이 공개 저장소 동기화 (허용 목록 방식).

    python tools/sync_public.py --src <비공개 저장소 폴더> --held <기록 폴더> [--held ...] [--rev <커밋>]    # 복사 + 검사
    python tools/sync_public.py --src <비공개 저장소 폴더> --held <기록 폴더> --check-only                  # 검사만 (커밋 전)

- 비공개 저장소는 읽기만 한다: `git show <rev>:<경로>` 로 커밋된 내용을 읽는다 (작업 폴더·인덱스를 건드리지 않음).
- --held 는 실제 장부·보유 장부·주문 기록이 있는 폴더 (logs/exec 또는 그 백업 사본). 거기 나온 종목 코드는 복사본에서
  가짜 코드로 바꾸고, 저장소 검사에서도 남아 있으면 실패한다 (백테스트 결과 폴더 제외). 코드는 이 파일에 적지 않는다.
  같은 기록의 실제 체결·주문 가격, 체결 금액, 체결 수량(10주 이상)도 실행 쪽 파일·문서에 남아 있으면 실패한다
  (명세·시험에 인용된 체결 값은 FILL_TEMPLATES 로 가짜 체결로 바꿈).
- ALLOW 에 적은 파일만 복사한다. DENY 에 걸리는 경로는 ALLOW 에 있어도 복사하지 않고 실패한다.
- 개인 경로·저장소 이름 같은 서술은 일반 표현으로 바꾼다 (REWRITE, PATH_REWRITE). 바꿀 곳이 없으면 실패
  (원본이 바뀐 것을 놓치지 않게).
- 복사본은 임시 폴더에 먼저 만들고 검사를 통과해야 저장소에 쓴다. 쓴 뒤 저장소 전체를 한 번 더 검사한다.
- 검사에 걸리면 "경로:줄:칸 [규칙]" 만 출력한다. 걸린 값은 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from fractions import Fraction
from pathlib import Path

DST = Path(__file__).resolve().parents[1]

LIVE = "backtest_swing/live/"
ALLOW = [
    # 실행 코드
    *(LIVE + f for f in (
        "__init__.py", "backup.py", "bar_timing.py", "common.py", "control.py", "daily.py", "engine.py",
        "features_eod.py", "firstrun.py", "gapfill.py", "make_bundle.py", "measure.py", "model.py", "model_repro.py", "monthly.py", "notify.py", "orders.py",
        "positions.py", "quotes.py", "replay.py", "resident.py", "run_day.py", "settings.py", "speed.py",
        "state.py", "update.py", "watchdog.py",
    )),
    # 시험 (가짜 서버·가짜 시계)
    *(LIVE + f for f in (
        "test_broad1.py", "test_broad2.py", "test_broad3.py", "test_broad4.py", "test_broad5.py",
        "test_broad6.py", "test_broad7.py", "test_broad8.py", "test_broad9.py", "test_broad10.py",
        "test_broad11.py", "test_broad12.py",
        "test_backup.py", "test_basefactor.py", "test_closed.py", "test_codex.py", "test_confirm.py",
        "test_failshare.py", "test_fetchfill.py", "test_fix.py", "test_fix11r.py", "test_intraday.py",
        "test_memcash.py", "test_mock1.py", "test_orders.py", "test_rebase.py", "test_safety.py", "test_second.py",
        "test_swap.py", "test_third.py",
        # 시험용 고정 자료: 과거 일봉(일봉 파일 일부)·KRX 공식 시세 (공개 정보). 보유·주문 종목이면 --held 가림이 바꾼다
        "testdata/rebase_real.json",
    )),
    # 스크립트·설정 예시·문서
    *(LIVE + f for f in (
        "start_resident.bat", "stop_resident.bat", "update.bat", "view_log.bat",
        "register_tasks.ps1", "verify_bundle.ps1", "view_log.ps1",
        "config.example.json", "requirements_live.txt", "SETUP.md",
        "krx_holidays.json",  # 한국거래소 휴장일 (공개 정보, 거래일 판정에 필요)
        "krx_special_hours.json",  # 거래시간 변경일 (공개 정보, 수능일·새해 첫날)
    )),
    # live/ 가 import 하는 키움 클라이언트 (키는 .env 에서 읽음, 값 없음)
    "kiwoom/__init__.py", "kiwoom/auth.py", "kiwoom/client.py", "kiwoom/config.py", "kiwoom/errors.py",
    "kiwoom/krx_official.py",
    # 실행 명세
    "backtest_swing/EXEC_SPEC.md",
]

# 절대 복사하지 않는 것 (폴더 조각 또는 파일 이름 패턴)
DENY_DIRS = ("logs/", "data/", "models_live/", "state/", "intents/", "snapshots/", "scores/", "dist/")
DENY_NAMES = (
    "config.json", "summary*.csv", ".env", ".env.*", "*.env", "*token_cache*", "positions*.json", "ledger*.json",
    "intents*.json", "*.intent*", "*snapshot*", "*.pid", "*.lock", "resident.info.json", "resident.stop",
    "heartbeat*", "SUBPC_MANIFEST*", "*.zip", "*.parquet", "*.npz", "*.pkl", "*.log",
)

# 저장소에 있으면 안 되는 것 (검사 단계). results/ 의 백테스트 summary.csv 는 예전부터 공개한 결과라 여기서는 보지 않는다.
REPO_DENY_DIRS = ("logs/", "data/", "models_live/", "dist/")
REPO_DENY_NAMES = (
    "backtest_swing/live/config.json", ".env", ".env.*", "*.env", "*token_cache*", "positions*.json", "ledger*.json",
    "resident.pid", "resident.lock", "resident.info.json", "resident.stop", "heartbeat.log", "SUBPC_MANIFEST*",
    "*.zip",
)

PUB_ROOT = "D:\\kr-swing-lgbm"
# 드라이브 바로 아래 원본 저장소 이름 폴더 (서브 PC 예시 경로). 구분자(\ 또는 /)는 그대로 둔다.
DRIVE_REPO = re.compile(r"\b[A-Z]:([\\/])stock\b")

# (경로, 원본 문자열 또는 정규식, 바꿀 문자열). 원본에 없으면 실패. 순서대로 적용, 모두 바꾼다.
# 개인 경로는 이 파일에 적지 않는다: --src 폴더 경로와 DRIVE_REPO 는 PATH_REWRITE 파일에서 따로 바꾼다.
REWRITE = [
    (LIVE + "SETUP.md", "# 서브 PC 이전 (상주 프로그램만, 2026-10-24)\n",
     "# 서브 PC 이전 (상주 프로그램만, 2026-10-24)\n\n"
     "> 공개 사본: 비공개 실행 저장소의 문서를 옮긴 것이다. 개인 경로·저장소 이름은 일반 표현(`D:\\kr-swing-lgbm`, "
     "`<계정>/<저장소>`)으로 바꿨다. 이 공개 저장소에는 data/·models_live/·.env 가 없어 시험 밖의 실행은 되지 않는다.\n"),
    (LIVE + "SETUP.md", re.compile(r"`[A-Z]:\\Users\\\.\.\.\\AppData\\Local\\"), "`%LOCALAPPDATA%\\"),
    (LIVE + "SETUP.md", "**Only select repositories → afafcalif/stock**", "**Only select repositories → 실행용 저장소 하나**"),
    (LIVE + "SETUP.md", "`subpc-stock-read`", "`subpc-read`"),
    (LIVE + "SETUP.md", "git@github.com:afafcalif/stock.git", "git@github.com:<계정>/<저장소>.git"),
    (LIVE + "SETUP.md", "https://github.com/afafcalif/stock.git", "https://github.com/<계정>/<저장소>.git"),
    ("backtest_swing/EXEC_SPEC.md", "3,000만 원 (모의투자 계좌 예수금 30,000,000원)", "3,000만 원 (모의투자 시작 예수금)"),
    # 모의 주문 로그 인용의 종목 코드 (보유 종목이 드러나지 않게)
    ("backtest_swing/EXEC_SPEC.md", re.compile(r"15:21:01 \d{6} 전송 -> 15:21:02 \d{6} 전송"),
     "15:21:01 첫 매수 전송 -> 15:21:02 둘째 매수 전송"),
    # 계좌 금액 (현금 대조 근거): 문서는 차이만 남기고, 시험은 두 값을 같은 만큼 옮긴다 (기대값이 전일 값에 1 대 1 이라 차이·허용치 그대로)
    ("backtest_swing/EXEC_SPEC.md", re.compile(r"D\+2 추정예수금 [\d,]+ vs 기대 [\d,]+ \(차이 (\d+), 허용 (\d+)\)"),
     lambda m: f"D+2 추정예수금이 기대값보다 {m.group(1)}원 많음, 허용 {m.group(2)}"),
    ("backtest_swing/EXEC_SPEC.md", re.compile(r" 전일 D\+2 = [\d,]+\."), ""),
]

# 실제 체결 값 (10-29 현금 대조 시험·명세 인용): 종목 코드를 가려도 날짜 + 가격으로 종목을 알 수 있어 가짜 체결로 바꾼다.
# 실제 값은 원본 시험에서 읽고(이 파일에는 적지 않음), 아래 계산식으로 만든 문구가 원본과 정확히 같아야 한다 (원본이 바뀌면 실패).
# 가짜 값은 실제 값과 관계없이 정한 값: 비율로 바꾸면 비율이 공개돼 실제 가격을 되돌려 알 수 있어서. 같은 호가 구간(매도
# 2,000 ~ 5,000원, 5원 단위)·같은 모양(매도 하나는 두 호가에 나눠 체결, 기록 체결가는 원 단위로 끊은 평균, 매수 셋)이라 시험의
# 수수료 10원 절사·세금 원 절사·허용치·예전 계산 실패 조건이 그대로 성립한다.
FAKE_FILLS = {"s_hi": 2845, "n_hi": 47, "s_lo": 2840, "n_lo": 109,
              "buys": [(1, 447500), (18, 41250), (1, 723000)], "prev": 10_000_000}
FILLS_SRC = LIVE + "test_memcash.py"
FILLS_READ = [  # (이름, 원본 시험에서 읽는 정규식)
    ("sell", re.compile(r'"side": "sell", "code": "\d{6}", "filled_qty": (\d+), "fill_price": (\d+)\.0\}')),
    ("buys", re.compile(r'"side": "buy", "code": "\d{6}", "filled_qty": (\d+), "fill_price": (\d+)\.0\}')),
    ("split", re.compile(r"# 실제 체결 금액 [\d,]+ \((\d+) x (\d+) \+ (\d+) x (\d+)\)")),
    ("d2", re.compile(r"PREV_D2, ACTUAL_D2 = ([\d_]+)\.0, ([\d_]+)\.0")),
]
# (파일, 문구 틀, 원본에 나오는 횟수). EXEC_SPEC 은 위 REWRITE(계좌 금액) 뒤의 문구.
FILL_TEMPLATES = [
    (FILLS_SRC, "PREV_D2, ACTUAL_D2 = {prev_u}.0, {actual_u}.0{note}", 1),
    (FILLS_SRC, '"filled_qty": {q_s}, "fill_price": {p_s}.0}},    # 시장가 손절: 실제 평균 약 {avg} (기록은 끊긴 값)', 1),
    (FILLS_SRC, '"filled_qty": {q1}, "fill_price": {p1}.0}}', 1),
    (FILLS_SRC, '"filled_qty": {q2}, "fill_price": {p2}.0}}', 1),
    (FILLS_SRC, '"filled_qty": {q3}, "fill_price": {p3}.0}}', 1),
    (FILLS_SRC, "P.kiwoom_fee({a1}, 0.0035), {f1})", 1),
    (FILLS_SRC, "# {raw_f1} -> 10원 미만 절사", 1),
    (FILLS_SRC, "P.kiwoom_fee({a2}, 0.0035), {f2})", 1),
    (FILLS_SRC, "P.kiwoom_fee({a3}, 0.0035), {f3})", 1),
    (FILLS_SRC, "# {raw_f3}\n", 1),
    (FILLS_SRC, "P.kiwoom_tax({exact}, 0.002), {ts})", 1),
    (FILLS_SRC, "# {raw_ts} -> 원 미만 절사", 1),
    (FILLS_SRC, "fill_price={exact} / {q_s})", 1),
    (FILLS_SRC, "# 실제 체결 금액 {exact_c} ({s_hi} x {n_hi} + {s_lo} x {n_lo})", 1),
    (FILLS_SRC, "ACTUAL_D2 - exp, {diff}.0, delta=0.01)", 1),
    (FILLS_SRC, "# 남는 차이 = 체결가 끊김 ({exact_c} - {rec_c})", 1),
    (FILLS_SRC, "tol, 1 + ({q_s} + 12) + ({q1} + 12) + ({q2} + 12) + ({q3} + 12))", 1),
    (FILLS_SRC, "# {tol}\n", 1),
    (FILLS_SRC, "+ {p_s} * {q_s} * (1 - 0.0035 - 0.002)", 1),
    (FILLS_SRC, "허용치(81)로는 {old} -> 실패였음", 1),
    (FILLS_SRC, '"cntr_qty": "{q_s}", "cntr_pric": "{p_s}", "tdy_trde_cmsn": "{fs}", "tdy_trde_tax": "{ts}"', 1),
    (FILLS_SRC, '{{"fee_won": {fs}.0, "tax_won": {ts}.0}}', 2),
    (FILLS_SRC, '(c["cash_diff"], c["tolerance"], c["n_fills"]), ({diff}.0, {tol}.0, 4)', 1),
    (FILLS_SRC, 'c["fee_tax_rule"], {f1} + {f2} + {f3} + {fs} + {ts})', 1),
    (LIVE + "positions.py", "{q_s}주 기록 {p_s} = 실제 평균 약 {avg})", 1),
    ("backtest_swing/EXEC_SPEC.md", "{q_s}주 시장가 접수 -> 전량 체결 {p_s} ->", 1),
    ("backtest_swing/EXEC_SPEC.md", "갱신 전까지 {q_s}주로 남는다", 1),
    ("backtest_swing/EXEC_SPEC.md", "기대값보다 {old}원 많음, 허용 81", 1),
    ("backtest_swing/EXEC_SPEC.md", " {q_s}주 시장가 (기록 체결가 {p_s}), 매수 ", 1),
    ("backtest_swing/EXEC_SPEC.md", " {q1}주 {p1_c}·", 1),
    ("backtest_swing/EXEC_SPEC.md", " {q2}주 {p2_c}·", 1),
    ("backtest_swing/EXEC_SPEC.md", " {q3}주 {p3_c}. 원인", 1),
    ("backtest_swing/EXEC_SPEC.md", "기록 체결가 {p_s} 은 5원 호가 단위", 1),
    ("backtest_swing/EXEC_SPEC.md", "{s_lo}·{s_hi} 에 나눠 체결되면 실제 금액은 {q_s} x {p_s} = {rec_c} 보다 최대 {maxgap}원 많다 "
                                    "({s_hi} x {n_hi} + {s_lo} x {n_lo} = {exact_c} 이면 {diff}원)", 1),
    ("backtest_swing/EXEC_SPEC.md", "(이번 4건 약 {trunc}원)", 1),
    ("backtest_swing/EXEC_SPEC.md", "실제 금액 {exact_c} + 수수료", 1),
    ("backtest_swing/EXEC_SPEC.md", "10-29 = 1 + {q_s12} + {q1_12} + {q2_12} + {q3_12} = {tol}원, 남는 차이 {diff}원", 1),
    ("backtest_swing/EXEC_SPEC.md", "기록 체결가면 {diff}원 < {tol}원, 예전 81원 넘음", 1),
]
FEE_RATE, TAX_RATE = Fraction(35, 10000), Fraction(2, 1000)  # 모의 수수료 0.35%, 2026 매도세 0.20% (시험과 같은 값)
# 백업 공유 폴더·작업·계정 이름: 원본에서 읽어(이 파일에는 적지 않음) 복사본 전체에서 일반 표현으로 바꾼다. 못 찾으면 실패.
NAME_SOURCES = {
    "folder": (LIVE + "SETUP.md", re.compile(r"New-SmbShare -Name \S+ -Path (\S+)")),
    "share": (LIVE + "SETUP.md", re.compile(r"New-SmbShare -Name (\S+) -Path")),
    "task": (LIVE + "register_tasks.ps1", re.compile(r'Register-ScheduledTask -TaskName "([^"]+)" -Action \$ba\b')),
    "account": (LIVE + "SETUP.md", re.compile(r"net user (\S+) \* /add")),
}
# 실제 모의 주문 종목 코드: 원본 문서의 로그 인용에서 읽어(이 파일에는 적지 않음) 복사본 전체에서 가짜 코드로 바꾼다.
# (경로, 코드를 잡는 정규식). 못 찾으면 실패. REWRITE 보다 먼저 읽는다.
MASK_CODES = [
    ("backtest_swing/EXEC_SPEC.md", re.compile(r"15:21:0[12] (\d{6}) 전송")),
]
MASK_BASE = 990001  # 가짜 코드 990001, 990002, ...
# 원본 저장소 폴더(--src 경로, DRIVE_REPO)를 PUB_ROOT 로 바꿔야 하는 파일. 바뀐 곳이 없으면 실패.
PATH_REWRITE = [LIVE + "config.example.json", LIVE + "register_tasks.ps1", LIVE + "verify_bundle.ps1",
                LIVE + "make_bundle.py", LIVE + "SETUP.md", LIVE + "test_broad8.py"]

TEXT_EXT = {".py", ".md", ".json", ".txt", ".bat", ".ps1", ".csv", ".cfg", ".toml", ".yml", ".yaml", ".ini",
            ".gitignore", ".gitattributes"}

# 키·토큰 모양 (알려진 형식)
KEY_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("api-key-prefix", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}\b|\bxox[abpr]-[A-Za-z0-9-]{10,}")),
    ("telegram-bot-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}")),
    ("bearer-literal", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("secret-assignment", re.compile(
        r"(?i)\b(?:app_?key|app_?secret|secret_?key|secret|api_?key|access_?token|bot_?token|password|passwd)\b"
        r"[\"']?\s*[:=]\s*[\"']([^\"'\s]{12,})[\"']")),
]
# 대소문자·숫자가 섞인 긴 문자열 (키 모양). 16진수(해시)와 밑줄로 이은 식별자는 제외.
LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9+/=_-])")
# 숫자 8 ~ 11자리 연속 (계좌번호·채팅 ID 모양). 앞뒤가 숫자·영문·소수점·밑줄이면 제외 (해시·16진수·소수).
DIGIT_RUN = re.compile(r"(?<![0-9A-Za-z_.])\d{8,11}(?![0-9A-Za-z_])")
PLACEHOLDER_DIGITS = ("1234567890", "0123456789", "9876543210")  # 시험의 가짜 값
# 검사에 걸리지만 괜찮은 값: 백업 시험의 가짜 .env 값·가짜 계좌번호, 기준가 시험의 공개 시세 거래대금
KNOWN_OK = {"Zx9FakeValue_abcdefghijklmnopqrstuvwxyz0123", "81234567", "950465597"}
# 숫자가 많은 공개 자료 (백테스트 결과·시험용 과거 일봉): 숫자·키 모양 규칙을 빼고 본다
LIGHT_PREFIXES = ("backtest_swing/results/", LIVE + "testdata/")
CHAT_ID = re.compile(r"(?i)chat[_ ]?id[\"']?\s*[:=]\s*[\"']?-?\d{5,}")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
EMAIL_OK = {"git@github.com"}  # SSH clone 주소의 사용자 이름
# 개인 경로: 드라이브 아래 Users, 홈 폴더, 드라이브 아래 원본 저장소 이름 폴더
PERSONAL_PATHS = [
    ("users-path", re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]")),
    ("home-path", re.compile(r"(?i)/(?:home|Users)/[a-z0-9_.-]+/")),
    ("private-repo-path", re.compile(r"(?i)\b[A-Z]:[\\/]+stock\b|/[a-z]/stock\b")),
]
LIGHT_RULES = {"env-value", "local-name", "src-path", "email", "chat-id", "private-key", "github-token",
               "api-key-prefix", "telegram-bot-token", "jwt", "users-path", "home-path", "private-repo-path"}
SECRET_ENV_KEY = re.compile(r"(?i)(KEY|SECRET|TOKEN|ACCOUNT|CHAT|PASS|ID)")


def git_show(src: Path, rev: str, path: str) -> bytes:
    return subprocess.run(["git", "-C", str(src), "show", f"{rev}:{path}"], check=True, capture_output=True).stdout


def resolve_rev(src: Path, rev: str) -> str:
    return subprocess.run(["git", "-C", str(src), "rev-parse", "--verify", rev + "^{commit}"],
                          check=True, capture_output=True, text=True).stdout.strip()


def denied(path: str, dirs, names) -> bool:
    p = "/" + path
    if any(("/" + d) in p for d in dirs):
        return True
    base = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(base, n) or fnmatch.fnmatch(path, n) for n in names)


def env_values(src: Path) -> list[str]:
    """비공개 저장소 .env 의 비밀 값 (키 이름에 KEY·SECRET·TOKEN·ACCOUNT·CHAT·PASS·ID). URL·*_PATH 값과 6자 미만은 제외."""
    vals = []
    for name in (".env", ".env.local"):
        f = src / name
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().removeprefix("export ").strip(), v.strip().strip("\"'")
            if k.endswith("_PATH") or not SECRET_ENV_KEY.search(k) or len(v) < 6 or v.startswith("http"):
                continue
            vals.append(v)
    return vals


def valid_yyyymmdd(s: str) -> bool:
    if len(s) != 8 or not s.startswith("20"):
        return False
    try:
        date(int(s[:4]), int(s[4:6]), int(s[6:]))
        return True
    except ValueError:
        return False


def scan_text(rel: str, text: str, ctx: dict) -> list[str]:
    hits = []

    def hit(i, col, rule):
        hits.append((f"{rel}:{i + 1}:{col + 1}", rule))

    for i, line in enumerate(text.splitlines()):
        low = line.lower()
        for v in ctx["secrets"]:
            if v in line:
                hit(i, line.find(v), "env-value")
        for s in ctx["src_strs"]:
            if s in low:
                hit(i, low.find(s), "src-path")
        for w in ctx["words"]:
            for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])", line, re.I):
                hit(i, m.start(), "local-name")
        for rule, rx in KEY_PATTERNS + PERSONAL_PATHS:
            for m in rx.finditer(line):
                hit(i, m.start(), rule)
        for m in LONG_TOKEN.finditer(line):
            s = m.group(0)
            if re.fullmatch(r"[0-9a-f]+|[0-9A-F]+", s) or s in KNOWN_OK:
                continue
            if not (re.search(r"[a-z]", s) and re.search(r"[A-Z]", s) and re.search(r"\d", s)):
                continue
            parts = s.split("_")
            if len(parts) >= 3 and all(0 < len(x) <= 16 for x in parts):
                continue  # 밑줄로 이은 식별자 (예: test_N2_fills_query_fails_at_1540)
            hit(i, m.start(), "key-like")
        for m in DIGIT_RUN.finditer(line):
            s = m.group(0)
            if valid_yyyymmdd(s) or s in KNOWN_OK or any(p.startswith(s) or s.startswith(p) for p in PLACEHOLDER_DIGITS):
                continue
            hit(i, m.start(), "digit-run-8-11")
        for m in CHAT_ID.finditer(line):
            if re.search(r"\d+$", m.group(0)).group(0) not in PLACEHOLDER_DIGITS:
                hit(i, m.start(), "chat-id")
        for m in EMAIL.finditer(line):
            if m.group(0).lower() not in EMAIL_OK:
                hit(i, m.start(), "email")
    return hits


def encoding_rules(rel: str, data: bytes) -> list[tuple[str, str]]:
    """파일 인코딩 규칙: .bat·requirements*.txt 는 ASCII (cmd·pip 이 cp949 로 읽음), .ps1 은 BOM 있는 UTF-8 (PowerShell 5.1)"""
    base = rel.rsplit("/", 1)[-1]
    if rel.endswith(".bat") or (base.startswith("requirements") and base.endswith(".txt")):
        try:
            data.decode("ascii")
        except UnicodeDecodeError:
            return [(rel, "encoding-not-ascii")]
    if rel.endswith(".ps1") and not data.startswith(b"\xef\xbb\xbf"):
        return [(rel, "encoding-no-bom")]
    return []


def is_text(rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1]
    return Path(base).suffix.lower() in TEXT_EXT or base in TEXT_EXT


def scan_files(root: Path, rels, ctx, light: set[str] = frozenset()) -> list[tuple[str, str]]:
    """rels 를 검사. light 에 든 파일(숫자가 많은 결과 표)은 숫자·키 모양 규칙을 빼고 본다."""
    hits = []
    for rel in rels:
        p = root / rel
        if not p.is_file() or not is_text(rel):
            continue
        data = p.read_bytes()
        hits += encoding_rules(rel, data)
        found = scan_text(rel, data.decode("utf-8", errors="replace"), ctx)
        hits += [h for h in found if rel not in light or h[1] in LIGHT_RULES]
    return hits


def repo_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         check=True, capture_output=True).stdout
    return sorted({f for f in out.decode("utf-8").split("\0") if f and (root / f).is_file()})


def report(title: str, hits) -> None:
    print(f"{title}: {len(hits)}건")
    for where, rule in hits:
        print(f"  {where} [{rule}]")


def held_values(dirs: list[str]) -> set[str]:
    """--held 폴더의 보유 장부·주문 기록에 나온 실제 체결·주문 가격(1,000원 이상)·체결 금액·체결 수량(10주 이상) 문구"""
    toks = set()

    def money(x):
        toks.update({str(x), f"{x:,}", f"{x:_}"})

    def walk(o):
        if isinstance(o, dict):
            q = o.get("filled_qty")
            for k in ("fill_price", "avg_price", "entry_price", "price"):
                x = o.get(k)
                if isinstance(x, (int, float)) and x >= 1000 and x == int(x):
                    money(int(x))
            if isinstance(q, int) and q > 0 and isinstance(o.get("fill_price"), (int, float)):
                if q * o["fill_price"] >= 10_000 and q * o["fill_price"] == int(q * o["fill_price"]):
                    money(int(q * o["fill_price"]))
                if q >= 10:
                    toks.update({f"{q}주", f'"filled_qty": {q}', f'"cntr_qty": "{q}"'})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for d in map(Path, dirs):
        for f in [*d.glob("state/positions.json*"), *d.glob("state/intents/*.json"),
                  *d.glob("*/state/positions.json*"), *d.glob("*/state/intents/*.json")]:
            walk(json.loads(f.read_text(encoding="utf-8")))
    return toks


def value_hits(root: Path, rels, toks) -> list[tuple[str, str]]:
    """실제 체결 가격·금액·수량 문구가 남은 곳 (값은 출력하지 않음)"""
    if not toks:
        return []
    rx = re.compile(r"(?<![\d,_.])(?:" + "|".join(sorted(map(re.escape, toks), key=len, reverse=True)) + r")(?![\d_]|[.,]\d)")
    hits = []
    for rel in rels:
        for i, line in enumerate((root / rel).read_bytes().decode("utf-8", errors="replace").splitlines()):
            hits += [(f"{rel}:{i + 1}:{m.start() + 1}", "held-fill-value") for m in rx.finditer(line)]
    return hits


def held_codes(dirs: list[str]) -> set[str]:
    """--held 폴더(logs/exec 또는 그 백업 사본)의 장부·보유 장부·주문 기록에 나온 종목 코드 (실제 보유·주문 종목)"""
    codes = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "code" and isinstance(v, str):
                    codes.add(v)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for d in map(Path, dirs):
        files = [d / "ledger.json", *d.glob("state/positions.json*"), *d.glob("state/intents/*.json"),
                 *d.glob("*/ledger.json"), *d.glob("*/state/positions.json*"), *d.glob("*/state/intents/*.json")]
        for f in filter(Path.is_file, files):
            o = json.loads(f.read_text(encoding="utf-8"))
            walk(o)
            if f.name.startswith("positions"):
                for key in ("held", "closed_today"):
                    codes.update(o.get(key) or {})
        # 그날 로그의 장부 (지금 장부에는 없는 지난 보유도)
        for f in [*d.glob("20??-??-??.json"), *d.glob("*/20??-??-??.json")]:
            walk(json.loads(f.read_text(encoding="utf-8")).get("ledger_before") or {})
    return {c for c in codes if re.fullmatch(r"\d{6}", c)}


def mask_codes(stage: Path, held: set[str]) -> tuple[dict[str, str], list[str]]:
    errs, codes = [], []
    for p, rx in MASK_CODES:
        found = rx.findall((stage / p).read_bytes().decode("utf-8"))
        if not found:
            errs.append(f"{p}: 가릴 종목 코드가 원본에 없음 (MASK_CODES 항목 확인)")
        codes += [c for c in found if c not in codes]
    present = set()
    for p in ALLOW:
        if is_text(p):
            present.update(re.findall(r"(?<!\d)\d{6}(?!\d)", (stage / p).read_bytes().decode("utf-8")))
    codes += sorted((held & present) - set(codes))
    return {c: str(MASK_BASE + i) for i, c in enumerate(codes)}, errs


def fill_fields(v: dict) -> dict[str, str]:
    """체결 값 v 로 시험·명세 문구의 숫자를 계산 (positions.kiwoom_fee·kiwoom_tax·cash_tolerance·expected_cash 와 같은 규칙)"""
    def fee(x):
        return (Fraction(x) * FEE_RATE // 10) * 10

    def tax(x):
        return Fraction(x) * TAX_RATE // 1

    def short(x):  # 2278.5, 1578.38, 3461.36
        return f"{float(x):.2f}".rstrip("0").rstrip(".")

    q_s = v["n_hi"] + v["n_lo"]
    exact = v["s_hi"] * v["n_hi"] + v["s_lo"] * v["n_lo"]
    p_s = exact // q_s
    rec = p_s * q_s
    buys = v["buys"]
    amts = [q * p for q, p in buys]
    fs, ts, fb = fee(exact), tax(exact), [fee(a) for a in amts]
    diff = (exact - fs - ts) - (rec - fee(rec) - tax(rec))
    net = exact - fs - ts - sum(a + f for a, f in zip(amts, fb))
    old = net - (-sum(a * (1 + FEE_RATE) for a in amts) + rec * (1 - FEE_RATE - TAX_RATE))
    trunc = sum(Fraction(a) * FEE_RATE - f for a, f in zip([exact, *amts], [fs, *fb])) + Fraction(exact) * TAX_RATE - ts
    f = {"q_s": q_s, "p_s": p_s, "exact": exact, "rec": rec, "s_hi": v["s_hi"], "n_hi": v["n_hi"], "s_lo": v["s_lo"],
         "n_lo": v["n_lo"], "fs": fs, "ts": ts, "diff": diff, "tol": 1 + sum(q + 12 for q in [q_s] + [q for q, _ in buys]),
         "old": round(old), "trunc": int(trunc), "maxgap": q_s - 1, "q_s12": q_s + 12,
         "prev_u": f"{v['prev']:_}", "actual_u": f"{v['prev'] + net:_}", "note": v.get("note", ""),
         "avg": short(Fraction(exact, q_s)), "raw_f1": short(amts[0] * FEE_RATE), "raw_f3": short(amts[2] * FEE_RATE),
         "raw_ts": short(exact * TAX_RATE), "exact_c": f"{exact:,}", "rec_c": f"{rec:,}"}
    for i, ((q, p), a, fe) in enumerate(zip(buys, amts, fb), 1):
        f.update({f"q{i}": q, f"p{i}": p, f"p{i}_c": f"{p:,}", f"a{i}": a, f"f{i}": fe, f"q{i}_12": q + 12})
    return {k: str(x) for k, x in f.items()}


def real_fills(stage: Path) -> tuple[dict | None, list[str]]:
    t = (stage / FILLS_SRC).read_bytes().decode("utf-8")
    got = {k: rx.findall(t) for k, rx in FILLS_READ}
    if [len(got[k]) for k, _ in FILLS_READ] != [1, 3, 1, 1]:
        return None, [f"{FILLS_SRC}: 체결 값을 원본에서 못 읽음 (FILLS_READ 확인)"]
    s_hi, n_hi, s_lo, n_lo = map(int, got["split"][0])
    prev, actual = (int(x.replace("_", "")) for x in got["d2"][0])
    v = {"s_hi": s_hi, "n_hi": n_hi, "s_lo": s_lo, "n_lo": n_lo, "prev": prev,
         "buys": [(int(q), int(p)) for q, p in got["buys"]]}
    f = fill_fields(v)
    errs = []
    if (got["sell"][0] != (f["q_s"], f["p_s"])) or int(f["actual_u"].replace("_", "")) != actual:
        errs.append(f"{FILLS_SRC}: 원본 체결 값이 계산식과 맞지 않음 (fill_fields 확인)")
    return v, errs


def replace_fills(stage: Path) -> tuple[list[str], list[str]]:
    """체결 값 문구를 가짜로 바꿈. 반환 (오류, 원본 체결 값 문구들 = 남았는지 검사할 값)"""
    v, errs = real_fills(stage)
    if errs:
        return errs, []
    real, fake = fill_fields(v), fill_fields({**FAKE_FILLS, "note": "  # 공개 사본: 계좌 금액·체결 값은 같은 계산이 성립하는 가짜 값"})
    for p, tpl, count in FILL_TEMPLATES:
        f = stage / p
        t = f.read_bytes().decode("utf-8")
        old, new = tpl.format(**real), tpl.format(**fake)
        if t.count(old) != count:
            errs.append(f"{p}: 체결 값 문구가 원본에 {t.count(old)}번 (기대 {count}번, FILL_TEMPLATES 확인)")
            continue
        f.write_bytes(t.replace(old, new).encode("utf-8"))
    keys = ("p_s", "exact_c", "rec_c", "p1", "p2", "p3", "p1_c", "p2_c", "p3_c", "a2", "s_hi", "s_lo")
    return errs, sorted({real[k] for k in keys} | {f"{real['q_s']}주", real["actual_u"], real["prev_u"]})


def held_hits(root: Path, rels, held: set[str]) -> list[tuple[str, str]]:
    """보유·주문 종목 코드가 남은 곳 (백테스트 결과 폴더는 예전부터 공개한 결과라 보지 않음)"""
    rx = re.compile(r"(?<!\d)(?:" + "|".join(sorted(held)) + r")(?!\d)")
    hits = []
    for rel in rels:
        for i, line in enumerate((root / rel).read_bytes().decode("utf-8", errors="replace").splitlines()):
            hits += [(f"{rel}:{i + 1}:{m.start() + 1}", "held-code") for m in rx.finditer(line)]
    return hits


def local_names(src: Path, rev: str) -> tuple[dict[str, str], list[str]]:
    names, errs = {}, []
    for k, (p, rx) in NAME_SOURCES.items():
        m = rx.search(git_show(src, rev, p).decode("utf-8"))
        if m:
            names[k] = m.group(1)
        else:
            errs.append(f"{p}: {k} 이름을 원본에서 못 찾음 (NAME_SOURCES 항목 확인)")
    return names, errs


def mask_names(t: str, names: dict[str, str]) -> str:
    t = t.replace(names["folder"], "<백업 폴더>")
    t = re.sub(r"(\\|-Name )" + re.escape(names["share"]) + r"(?![\w-])", lambda m: m.group(1) + "<공유 이름>", t)
    t = re.sub(r"(?<![\w-])" + re.escape(names["task"]) + r"(?![\w-])", "<백업 작업>", t)
    return re.sub(r"(?<![\w-])" + re.escape(names["account"]) + r"(?![\w-])", "<백업 계정>", t)


def apply_rewrites(stage: Path, src: Path, held: set[str], names: dict[str, str]) -> tuple[list[str], list[str]]:
    """반환 (오류, 원본 체결 값 문구 = 복사본에 남았는지 볼 값)"""
    masks, errs = mask_codes(stage, held)
    for p, old, new in REWRITE:
        f = stage / p
        t = f.read_bytes().decode("utf-8")
        if isinstance(old, re.Pattern):
            t2, n = old.subn(new if callable(new) else (lambda m: new), t)
        else:
            n, t2 = t.count(old), t.replace(old, new)
        if not n:
            errs.append(f"{p}: 바꿀 곳이 원본에 없음 (REWRITE 항목 확인)")
        f.write_bytes(t2.encode("utf-8"))
    fill_errs, fill_toks = replace_fills(stage)
    errs += fill_errs
    src_forms = sorted({str(src), str(src).replace("\\", "/")}, key=len, reverse=True)
    for p in PATH_REWRITE:
        f = stage / p
        t = f.read_bytes().decode("utf-8")
        n = 0
        for s in src_forms:
            rx = re.compile(re.escape(s), re.I)
            t, k = rx.subn(lambda m: PUB_ROOT.replace("\\", "/") if "/" in m.group(0) else PUB_ROOT, t)
            n += k
        t, k = DRIVE_REPO.subn(lambda m: PUB_ROOT.replace("\\", m.group(1)), t)
        n += k
        if not n:
            errs.append(f"{p}: 바꿀 경로가 원본에 없음 (PATH_REWRITE 항목 확인)")
        f.write_bytes(t.encode("utf-8"))
    rx = re.compile(r"(?<!\d)(" + "|".join(masks) + r")(?!\d)") if masks else None
    for p in ALLOW:
        f = stage / p
        if is_text(p):
            t = f.read_bytes().decode("utf-8")
            t = rx.sub(lambda m: masks[m.group(1)], t) if rx else t
            f.write_bytes(mask_names(t, names).encode("utf-8"))
    return errs, fill_toks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="비공개 실행 저장소 폴더 (읽기만 함)")
    ap.add_argument("--rev", default="HEAD", help="복사할 커밋 (기본 HEAD)")
    ap.add_argument("--held", action="append", required=True,
                    help="실제 장부·보유·주문 기록 폴더 (logs/exec 또는 그 백업 사본, 여러 번 가능, 읽기만 함). 이 종목 코드는 가림")
    ap.add_argument("--check-only", action="store_true", help="복사하지 않고 이 저장소만 검사")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    src = Path(a.src).resolve()
    if src == DST:
        print("src 와 이 저장소가 같음", file=sys.stderr)
        return 2
    held, held_vals = held_codes(a.held), held_values(a.held)
    if not held:
        print("--held 폴더에서 종목 코드를 못 읽음 (장부·보유·주문 기록 폴더인지 확인)", file=sys.stderr)
        return 2
    rev = resolve_rev(src, a.rev)
    names, errs = local_names(src, rev)
    if errs:
        print("이름 찾기 실패:", *errs, sep="\n  ")
        return 1
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    ctx = {"secrets": env_values(src), "words": [w for w in [user, *names.values()] if len(w) >= 3],
           "src_strs": {str(src).lower(), str(src).replace("\\", "/").lower()}}

    if not a.check_only:
        bad = [p for p in ALLOW if denied(p, DENY_DIRS, DENY_NAMES)]
        if bad:
            print("허용 목록에 금지 경로가 있음:", *bad, sep="\n  ")
            return 1
        stage = Path(tempfile.mkdtemp(prefix="sync_public_"))
        try:
            for p in ALLOW:
                out = stage / p
                out.parent.mkdir(parents=True, exist_ok=True)
                data = git_show(src, rev, p)
                if p.endswith((".bat", ".ps1")):  # cmd·PowerShell 용은 CRLF (.gitattributes 와 같게)
                    data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                out.write_bytes(data)
            errs, fill_toks = apply_rewrites(stage, src, held, names)
            if errs:
                print("바꾸기 실패:", *errs, sep="\n  ")
                return 1
            hits = scan_files(stage, ALLOW, ctx, light={p for p in ALLOW if p.startswith(LIGHT_PREFIXES)})
            hits += value_hits(stage, [p for p in ALLOW if is_text(p) and not p.startswith(LIGHT_PREFIXES)],
                               held_vals | set(fill_toks))
            if hits:
                report("검사 실패 (저장소에 쓰지 않음)", hits)
                return 1
            for p in ALLOW:
                d = DST / p
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(stage / p, d)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        print(f"복사: {len(ALLOW)}개 파일 (원본 커밋 {rev[:7]}), 복사본 검사 통과")
        managed = ("backtest_swing/live/", "kiwoom/")
        extra = [f for f in repo_files(DST) if f.startswith(managed) and f not in ALLOW]
        if extra:
            print("허용 목록 밖 파일 (원본에서 빠졌는지 확인):", *extra, sep="\n  ")

    files = repo_files(DST)
    hits = [(f, "denied-path") for f in files if denied(f, REPO_DENY_DIRS, REPO_DENY_NAMES)]
    hits += scan_files(DST, files, ctx, light={f for f in files if f.startswith(LIGHT_PREFIXES)})
    # 보유·주문 종목 코드: 실행 쪽 파일과 설명 문서만 (백테스트 코드·결과는 실행 전부터 공개한 것이라 우연히 겹칠 수 있음)
    held_scope = ("backtest_swing/live/", "kiwoom/", "backtest_swing/EXEC_SPEC.md", "README.md", "docs/")
    scoped = [f for f in files if f.startswith(held_scope) and is_text(f)]
    hits += held_hits(DST, scoped, held)
    hits += value_hits(DST, [f for f in scoped if not f.startswith(LIGHT_PREFIXES)], held_vals)
    if hits:
        report("저장소 검사 실패", hits)
        return 1
    print(f"저장소 검사 통과: 파일 {len(files)}개, .env 비밀 값 {len(ctx['secrets'])}개 대조")
    return 0


if __name__ == "__main__":
    sys.exit(main())
