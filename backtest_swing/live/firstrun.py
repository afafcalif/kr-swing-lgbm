"""
서브 PC 첫 실행 확인 (SETUP.md 12절, 2026-10-24). 상주 프로그램을 켜지 않고 차례로 확인만 한다.

  python backtest_swing/live/firstrun.py [2026-10]

1. 설정: 시스템 시간대 KST, config 의 경로·env·주문 설정, 파이썬이 스토어판이 아닌지
2. 시험 전체 (backtest_swing/live/test_*.py)
3. 동결 코드: stage3 ~ 5 파일이 태그 forward-freeze-20261021 과 같은지 + 동결 함수 해시·치환
4. 모델: 복사한 models_live 로 기준 표본(repro_ref.parquet) 예측이 메인 PC 와 같은지 (차이 0)
5. 시세 조회 1회 (ka10095, 005930)
6. 텔레그램 시험 메시지 1개
결과는 화면과 logs/exec/firstrun.json. 모두 통과하면 작업 등록 -> 재부팅 확인 -> orders_enabled true 순서로 (SETUP.md 12절).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402

CFG = settings.load()
from common import LOG_DIR, Caller, system_tz_ok  # noqa: E402


def venv_home(py):
    """가상 환경 파이썬이면 pyvenv.cfg 의 home (바탕 파이썬 폴더), 아니면 빈 문자열"""
    cfg = os.path.join(os.path.dirname(os.path.dirname(py)), "pyvenv.cfg")
    try:
        with open(cfg, encoding="utf-8") as f:
            return next((ln.split("=", 1)[1].strip() for ln in f if ln.lower().startswith("home")), "")
    except OSError:
        return ""


def check_config():
    py = CFG.get("python", "")
    items = {"시간대 KST": system_tz_ok(), "root 폴더 있음": os.path.isdir(CFG.get("root", "")), "python 파일 있음": os.path.exists(py),
             # 가상 환경 경로에는 WindowsApps 가 안 보이므로 바탕 파이썬(sys.base_prefix, pyvenv.cfg home)으로 확인
             "스토어판 파이썬 아님": "WindowsApps" not in py and "WindowsApps" not in sys.base_prefix and "WindowsApps" not in venv_home(py),
             "env = mock": CFG.get("env") == "mock", "allow_prod_orders = false": not CFG.get("allow_prod_orders")}
    return all(items.values()), {**items, "orders_enabled": CFG.get("orders_enabled"), "python": py, "root": CFG.get("root")}


def check_tests():
    import update
    r = update.run_tests()
    return r["ok"], r


def check_frozen():
    import update
    r = update.frozen_status()
    return r["ok"], r


def check_model(month):
    import model_repro as MR
    r = MR.cmd_check(month)
    return r["verdict"].startswith("동일") and r["top3_all_same"] and r["model_load_check"]["ok"], \
        {k: r[k] for k in ("verdict", "mean_pred_max_abs_diff", "top3_all_same", "rows")} | {"load_ok": r["model_load_check"]["ok"]}


def check_quote():
    import quotes as Q
    _, snap, rec = Q.snapshot(Caller("prod", 5), ["005930"])
    px = float(snap.close.iat[0]) if len(snap) else 0.0
    return px > 0, {"code": "005930", "price": px, "seconds": rec.get("seconds")}


def check_telegram():
    import notify as N
    ok = N.send(f"[서브 PC 첫 실행 확인] 텔레그램 시험 ({time.strftime('%Y-%m-%d %H:%M:%S')})", log_path=os.path.join(LOG_DIR, "alerts.log"))
    return bool(ok), {"sent": bool(ok)}


def main(month):
    steps = [("1 설정", check_config), ("2 시험 전체", check_tests), ("3 동결 코드", check_frozen), ("4 모델 예측", lambda: check_model(month)),
             ("5 시세 조회", check_quote), ("6 텔레그램", check_telegram)]
    out, all_ok = {}, True
    for name, fn in steps:
        t0 = time.time()
        try:
            ok, info = fn()
        except Exception as e:  # noqa: BLE001
            ok, info = False, {"error": repr(e)[:300]}
        all_ok &= ok
        out[name] = {"ok": ok, "seconds": round(time.time() - t0, 1), **info}
        print(f"[{'통과' if ok else '실패'}] {name} ({out[name]['seconds']}초): {json.dumps(info, ensure_ascii=False, default=str)[:300]}", flush=True)
    out["all_ok"] = all_ok
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "firstrun.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print("모두 통과 -> SETUP.md 12절 다음 단계(작업 등록, 재부팅 확인, orders_enabled true)" if all_ok else "실패한 항목을 먼저 해결", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "2026-10"))
