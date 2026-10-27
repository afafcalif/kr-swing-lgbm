"""실행 PC 별 설정 (live/config.json, 없으면 config.example.json). 서브 PC 로 옮길 때 경로만 바꾼다.

11K 보완 6: config.example.json 을 기본값으로 깔고 config.json 을 그 위에 키 단위로 깊게 덮어쓴다 (times 같은 하위 항목도 키마다).
config.json 에 없는 키(예전에 복사한 뒤 예시에 새로 생긴 키)는 기본값이 들어가고, 채운 키는 cfg["_filled"] 에 "times.stop_send_until" 처럼 남는다.
11K 보완 8: 환경 변수 LIVE_CONFIG=example 이면 config.json 을 읽지 않는다 (시험 격리: 서브 PC 의 오래된 config.json 이 시험에 섞이지 않게)."""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "config.example.json")
LOCAL = os.path.join(HERE, "config.json")


def merge(base, over, path="", filled=None):
    """base(기본값) 위에 over 를 깊게 덮어쓴 새 dict. over 에 없어 기본값으로 채운 키는 filled 에 경로로"""
    filled = [] if filled is None else filled
    out = copy.deepcopy(over)
    for k, v in base.items():
        if k.startswith("_"):
            continue
        key = f"{path}{k}"
        if k not in over:
            out[k] = copy.deepcopy(v)
            filled.append(key)
        elif isinstance(v, dict) and isinstance(over[k], dict):
            out[k] = merge(v, over[k], key + ".", filled)
    return out


def load(path=None, example=None):
    ex = json.load(open(example or EXAMPLE, encoding="utf-8"))
    p = path or LOCAL
    filled = []
    if os.environ.get("LIVE_CONFIG") == "example" and path is None:
        cfg, src = copy.deepcopy(ex), "config.example.json (LIVE_CONFIG=example)"
    elif os.path.exists(p):
        cfg, src = merge(ex, json.load(open(p, encoding="utf-8")), filled=filled), os.path.basename(p)
    else:
        cfg, src = copy.deepcopy(ex), "config.example.json"
    root = cfg["root"]
    for k in ("log_dir", "state_dir", "pid_file", "heartbeat_file"):
        if not os.path.isabs(cfg[k]):
            cfg[k] = os.path.join(root, cfg[k])
    cfg["_filled"], cfg["_source"] = filled, src
    return cfg


def refuse_test_env(name):
    """11K 보완 3 (2): 상주·감시 작업·update·control 을 프로세스로 시작할 때 LIVE_CONFIG 가 설정돼 있으면 거부 + 알림 (시험 전용 환경 변수,
    설정돼 있으면 config.json 을 읽지 않아 PC 설정·주문 설정이 빠진 채로 돈다). 각 스크립트의 __main__ 에서만 부른다 (시험이 모듈을 가져오는 것은 그대로)"""
    v = os.environ.get("LIVE_CONFIG")
    if v is None:
        return
    msg = (f"[시작 거부] {name}: 환경 변수 LIVE_CONFIG={v!r} 가 설정돼 있음 (시험 전용, config.json 을 읽지 않는 상태) -> 시작 안 함. "
           f"환경 변수를 지우고 다시 시작")
    print(msg, file=sys.stderr, flush=True)
    try:
        import notify
        notify.send(msg)
    except Exception:  # noqa: BLE001
        pass
    sys.exit(9)
