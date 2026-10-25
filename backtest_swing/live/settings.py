"""실행 PC 별 설정 (live/config.json, 없으면 config.example.json). 서브 PC 로 옮길 때 경로만 바꾼다."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    p = os.path.join(HERE, "config.json")
    cfg = json.load(open(p if os.path.exists(p) else os.path.join(HERE, "config.example.json"), encoding="utf-8"))
    root = cfg["root"]
    for k in ("log_dir", "state_dir", "pid_file", "heartbeat_file"):
        if not os.path.isabs(cfg[k]):
            cfg[k] = os.path.join(root, cfg[k])
    return cfg
