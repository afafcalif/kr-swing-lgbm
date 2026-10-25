"""
그 달 모델 학습·저장·로드 확인 (EXEC_SPEC 4장)

  python backtest_swing/live/model.py train 2026-10 [2026-09 ...]   # models_live/stage5/{YYYY-MM}/seed1~5.txt + meta.json
  python backtest_swing/live/model.py check 2026-10                 # 로드 확인 (파일 SHA-256, 특징 목록·순서, 고정 표본 10행 예측값)

학습 집합은 stage4_model.months 와 같은 R−61 기준 (재학습일 = 그 달 첫 거래일, 그 61거래일 전까지 신호), 라벨 y_clip,
PARAMS·나무 500·시드 1~5 (stage4_model.fit). 데이터셋은 stage5_features.build 로 만든 검증 데이터셋
(results/stage5_validation/dataset.parquet, 신호 2020-01-02 ~ 2026-10-16) 을 쓴다.
학습 집합의 라벨(60거래일 상한)이 데이터셋 끝(sim 의 dev_last) 전에 끝나야 학습한다: 재학습일 직전 거래일 <= 데이터셋 마지막 날.
"""
import json
import os
import sys
import time

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import MODEL_DIR, VAL_DIR, git_commit, m, sha256, sm  # noqa: E402

DATASET = os.path.join(VAL_DIR, "dataset.parquet")
N_FIXED, FIXED_SEED = 10, 0


def month_plan(ds, month):
    """(재학습일, 학습 끝 날짜) — stage4_model.months 와 같은 계산. 그 달 첫 거래일이 데이터셋에 없으면 None"""
    per = pd.Period(month, "M")
    saved = sm.FIRST_PRED, sm.LAST_PRED
    sm.FIRST_PRED, sm.LAST_PRED = per.start_time, per.end_time
    try:
        ms = sm.months(ds)
    finally:
        sm.FIRST_PRED, sm.LAST_PRED = saved
    return (ms[0][1], ms[0][2]) if ms else None


def expected_features():
    import pyarrow.parquet as pq
    names = pq.read_schema(DATASET).names
    return [c for c in names if c not in m.s5.NON_FEATURES and c != "is_kospi"]


def train(month, out_dir=None):
    """out_dir 를 주면 그 폴더에 저장 (모델 재현 확인용 임시 폴더, model_repro.py). 없으면 models_live/stage5/{month}"""
    t0 = time.time()
    ds = pd.read_parquet(DATASET)
    cols = m.feature_cols(ds)
    assert len(cols) == 41 and "is_kospi" not in cols
    plan = month_plan(ds, month)
    last_day = ds.date.max()
    if plan is None:
        return {"month": month, "trained": False,
                "reason": f"데이터셋(신호 끝 {last_day.date()})에 {month} 첫 거래일이 없어 재학습일·학습 끝 날짜(R−61)를 정할 수 없다. "
                          f"학습 집합 라벨이 재학습일 직전 거래일까지 필요해 그 날까지의 일봉으로 데이터셋을 다시 만들어야 한다"}
    first_day, cutoff = plan
    tr_idx = np.flatnonzero((ds.date <= cutoff).to_numpy())
    Xtr = ds.iloc[tr_idx][cols].to_numpy(np.float32)
    y = ds.y_clip.to_numpy()[tr_idx]
    out = out_dir or os.path.join(MODEL_DIR, month)
    os.makedirs(out, exist_ok=True)
    rng = np.random.default_rng(FIXED_SEED)
    fixed = np.sort(rng.choice(len(tr_idx), N_FIXED, replace=False))
    Xf = Xtr[fixed]
    files, preds = {}, {}
    for seed in m.SEEDS_MAIN:
        b = sm.fit(Xtr, y, seed)
        p = os.path.join(out, f"seed{seed}.txt")
        b.save_model(p)
        files[f"seed{seed}.txt"] = sha256(p)
        preds[f"seed{seed}"] = [float(x) for x in b.predict(Xf)]
    meta = {"month": month, "retrain_day": str(first_day.date()), "train_end": str(cutoff.date()), "train_rows": int(len(tr_idx)),
            "features": cols, "files": files, "git_commit": git_commit(), "dataset": DATASET, "dataset_sha256": sha256(DATASET),
            "params": sm.PARAMS, "n_trees": sm.N_TREES, "seeds": m.SEEDS_MAIN, "lightgbm": lgb.__version__,
            "fixed_sample": {"rows": [{"code": str(ds.code.iat[tr_idx[i]]), "date": str(ds.date.iat[tr_idx[i]].date())} for i in fixed],
                             "X": [[float(v) for v in row] for row in Xf], "pred": preds,
                             "pred_mean": [float(v) for v in np.mean([preds[f"seed{s}"] for s in m.SEEDS_MAIN], axis=0)]},
            "created": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.time() - t0, 1)}
    json.dump(meta, open(os.path.join(out, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return {"month": month, "trained": True, "retrain_day": meta["retrain_day"], "train_end": meta["train_end"],
            "train_rows": meta["train_rows"], "seconds": meta["seconds"]}


def load(month, model_dir=None):
    """반환: (boosters, features, meta, check). check["ok"] 가 False 면 장애 (EXEC_SPEC 6장). model_dir: 다른 폴더의 모델 (재현 확인용)"""
    d = model_dir or os.path.join(MODEL_DIR, month)
    meta = json.load(open(os.path.join(d, "meta.json"), encoding="utf-8"))
    chk = {"month": month, "path": d, "sha256_ok": True, "files": {}}
    boosters = []
    for seed in meta["seeds"]:
        p = os.path.join(d, f"seed{seed}.txt")
        h = sha256(p)
        chk["files"][p] = h
        chk["sha256_ok"] &= h == meta["files"][f"seed{seed}.txt"]
        boosters.append(lgb.Booster(model_file=p))
    exp = expected_features()
    chk["features_ok"] = meta["features"] == exp and len(exp) == 41
    X = np.asarray(meta["fixed_sample"]["X"], np.float32)
    diffs = [float(np.max(np.abs(b.predict(X) - np.asarray(meta["fixed_sample"]["pred"][f"seed{s}"]))))
             for b, s in zip(boosters, meta["seeds"])]
    chk["fixed_pred_max_abs_diff"] = max(diffs)
    chk["fixed_pred_ok"] = max(diffs) == 0.0
    chk["ok"] = bool(chk["sha256_ok"] and chk["features_ok"] and chk["fixed_pred_ok"])
    return boosters, meta["features"], meta, chk


def score(boosters, X):
    """시드 5개 예측 평균 (exec_stage3_measure_all.build_frames 와 같은 계산)"""
    return np.mean([b.predict(X) for b in boosters], axis=0)


if __name__ == "__main__":
    cmd, months = sys.argv[1], sys.argv[2:]
    for mo in months:
        if cmd == "train":
            print(json.dumps(train(mo), ensure_ascii=False), flush=True)
        else:
            print(json.dumps(load(mo)[3], ensure_ascii=False), flush=True)
