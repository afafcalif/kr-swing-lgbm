"""
서브 PC 이전 묶음 만들기 (메인 PC, 2026-10-24).

  python backtest_swing/live/make_bundle.py [--root-on-subpc D:/kr-swing-lgbm]   # dist/subpc_bundle_{YYYYMMDD}.zip

git 에 없는 것 중 상주 프로그램이 쓰는 것만 담는다 (저장소 루트 기준 같은 상대 경로):
- data: 대상 종목(universe)의 일봉 파일(daily_ext 가 있으면 그것, 없으면 daily_merged — load_stock 과 같은 선택) + daily_raw,
  지수 2개·종목 목록, live_append(추가분·버전 폴더), krx_daily(공식 시세 캐시). 분봉·npz·DB·큰 로그는 넣지 않는다.
- models_live/ (그달 모델 + 재현 확인 기준 repro_ref.parquet), results/stage5_validation/dataset.parquet (특징 목록 확인·재학습용)
- logs/exec: 장부(ledger*.json), 상태(state/: positions·날짜별 상태·intents), 스냅샷, 점수, summary*.csv, 날짜별 그날 로그(json)
- backtest_swing/live/config.json (서브 PC 경로, 주석 _note 포함, orders_enabled false)
- SUBPC_MANIFEST.sha256 (파일별 SHA-256), backtest_swing/live/verify_bundle.ps1 (서브 PC 에서 검사)
.env·토큰 캐시는 넣지 않는다 (.env 는 사용자가 USB 로 따로). 만든 뒤 묶음 안 텍스트 파일에 .env 값·키·토큰 모양이 없는지 검사한다.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import LOG_DIR, MODEL_DIR, ROOT, VAL_DIR, sd  # noqa: E402

FORBIDDEN = re.compile(r"(^|/)(\.env(\..*)?|.*token.*cache.*|.*\.pem|id_rsa.*|id_ed25519.*)$", re.I)
TEXT_EXT = (".json", ".csv", ".txt", ".md", ".ps1", ".log", ".py", ".bat")
SECRET_PATTERNS = [re.compile(r"(?i)(app_?key|secret|token|password|api_?key)\s*[:=]\s*['\"][A-Za-z0-9_\-:]{12,}['\"]"),
                   re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"), re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_.]{20,}")]


def rel(p):
    return os.path.relpath(p, ROOT).replace("\\", "/")


def walk(d):
    out = []
    for base, _, files in os.walk(d):
        out += [os.path.join(base, f) for f in files]
    return sorted(out)


def file_list():
    files = []
    for code in sd.universe_codes():
        ext = os.path.join(sd.EXT_DIR, f"{code}.parquet")
        files.append(ext if os.path.exists(ext) else os.path.join(sd.MERGED_DIR, f"{code}.parquet"))
        raw = os.path.join(sd.RAW_DIR, f"{code}.parquet")
        if os.path.exists(raw):
            files.append(raw)
    import stage4_features as s4
    from common import APPEND_DIR
    from kiwoom import krx_official
    files += [sd.KOSPI, s4.KOSDAQ, sd.UNIVERSE]
    files += walk(APPEND_DIR) + walk(krx_official.CACHE_DIR) + walk(MODEL_DIR) + [os.path.join(VAL_DIR, "dataset.parquet")]
    ex = LOG_DIR
    files += [os.path.join(ex, f) for f in sorted(os.listdir(ex)) if re.fullmatch(r"(ledger.*\.json|summary.*\.csv|\d{4}-\d{2}-\d{2}(_shadow)?\.json)", f)]
    for sub in ("state", "snapshots", "scores"):
        if os.path.isdir(os.path.join(ex, sub)):
            files += walk(os.path.join(ex, sub))
    files = [f for f in dict.fromkeys(files) if os.path.isfile(f)]
    bad = [rel(f) for f in files if FORBIDDEN.search(rel(f))]
    assert not bad, f"넣으면 안 되는 파일: {bad}"
    return files


def subpc_config(root):
    with open(os.path.join(ROOT, "backtest_swing", "live", "config.example.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_note"] = ("서브 PC 설정 (이전 묶음에서 옴). root·python 을 이 PC 의 git clone 폴더에 맞게 고친다. "
                    "orders_enabled 는 SETUP.md 12절 첫 실행 확인을 모두 통과한 뒤 true 로, allow_prod_orders 는 false 유지 (env mock 인지 시작 알림으로 확인).")
    cfg["_note_paths"] = "root = git clone 한 저장소 폴더, python = 그 안 .venv-feat\\Scripts\\python.exe (python.org 설치판으로 만든 가상 환경, 스토어판 금지)"
    cfg.update(root=root, python=f"{root}/.venv-feat/Scripts/python.exe", env="mock", orders_enabled=False, allow_prod_orders=False)
    return json.dumps(cfg, ensure_ascii=False, indent=2)


def env_values():
    vals = []
    for envf in (".env", os.path.join("kiwoom", ".env")):
        p = os.path.join(ROOT, envf)
        if os.path.exists(p):
            for line in open(p, encoding="utf-8", errors="ignore"):
                if "=" in line and not line.strip().startswith("#"):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if len(v) >= 8 and not v.startswith(("http://", "https://")):
                        vals.append(v)
    return vals


def scan_zip(zpath):
    """묶음 안: 금지 파일 이름 + 텍스트 파일의 .env 값·키 모양 (값은 출력하지 않음)"""
    vals, hits = env_values(), []
    # 숫자만인 값(채팅 ID·계좌 번호 등)은 앞뒤가 숫자·소수점이 아닐 때만 일치로 본다 (모델 파일의 긴 실수 안에 우연히 들어가는 경우 제외)
    rx = [re.compile(rf"(?<![\d.]){re.escape(v)}(?!\d)") if v.isdigit() else re.compile(re.escape(v)) for v in vals]
    with zipfile.ZipFile(zpath) as z:
        for n in z.namelist():
            if FORBIDDEN.search(n):
                hits.append(f"{n}: 금지 파일 이름")
            if n.lower().endswith(TEXT_EXT + (".sha256",)) or n.startswith("models_live/"):
                txt = z.read(n).decode("utf-8", "ignore")
                if any(r.search(txt) for r in rx):
                    hits.append(f"{n}: .env 값 일치")
                if any(p.search(txt) for p in SECRET_PATTERNS):
                    hits.append(f"{n}: 키·토큰 모양")
    return {"env_values_checked": len(vals), "hits": hits}


def main(root_on_subpc):
    t0 = time.time()
    files = file_list()
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    zpath = os.path.join(ROOT, "dist", f"subpc_bundle_{time.strftime('%Y%m%d')}.zip")
    manifest, total = [], 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in files:
            r = rel(f)
            h = hashlib.sha256(open(f, "rb").read()).hexdigest()
            manifest.append(f"{h}  {r}")
            total += os.path.getsize(f)
            z.write(f, r)
        extra = {"backtest_swing/live/config.json": subpc_config(root_on_subpc).encode("utf-8"),
                 "backtest_swing/live/verify_bundle.ps1": open(os.path.join(ROOT, "backtest_swing", "live", "verify_bundle.ps1"), "rb").read()}
        for r, data in extra.items():
            manifest.append(f"{hashlib.sha256(data).hexdigest()}  {r}")
            total += len(data)
            z.writestr(r, data)
        head = [f"# 서브 PC 이전 묶음 {os.path.basename(zpath)} {time.strftime('%Y-%m-%d %H:%M:%S')}, 파일 {len(manifest)}개, "
                f"{total / 1e6:.1f} MB (압축 전). 저장소 폴더에 푼 뒤 backtest_swing/live/verify_bundle.ps1 로 검사"]
        z.writestr("SUBPC_MANIFEST.sha256", "\n".join(head + manifest) + "\n")
    scan = scan_zip(zpath)
    groups = {}
    for line in manifest:
        r = line.split("  ", 1)[1]
        k = "/".join(r.split("/")[:2]) if not r.startswith("logs/") else "logs/exec"
        groups[k] = groups.get(k, 0) + 1
    res = {"zip": zpath, "files": len(manifest) + 1, "bytes_uncompressed": total, "zip_bytes": os.path.getsize(zpath),
           "by_folder": groups, "secret_scan": scan, "seconds": round(time.time() - t0, 1)}
    print(json.dumps(res, ensure_ascii=False, indent=1), flush=True)
    return 1 if scan["hits"] else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root-on-subpc", default="D:/kr-swing-lgbm")
    sys.exit(main(ap.parse_args().root_on_subpc))
