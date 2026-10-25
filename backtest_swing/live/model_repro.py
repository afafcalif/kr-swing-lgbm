"""
모델 재현 확인 (서브 PC 이전, 2026-10-24).

  python backtest_swing/live/model_repro.py ref 2026-10       # (메인 PC) 기준 예측 저장: models_live/stage5/2026-10/repro_ref.parquet
  python backtest_swing/live/model_repro.py check 2026-10     # (서브 PC) 복사한 모델의 예측이 기준과 같은지 (차이 0 이어야 함)
  python backtest_swing/live/model_repro.py retrain 2026-10   # (서브 PC) 그 달 모델을 임시 폴더에 다시 학습해 기준과 비교 (결과만 보고)

표본: 검증 데이터셋(results/stage5_validation/dataset.parquet)의 마지막 5개 신호일 전 종목 + 학습 기간에서 고정 시드로 뽑은 2000행.
비교: 시드별 예측 최대 절대 차, 시드 평균 예측 최대 절대 차, 날짜별 상위 3(시드 평균 예측 순) 일치 여부.
판정: 차이 0 = "동일", 1e-9 이하 = "부동소수 수준", 그 밖 = "다름" (다르면 다음 달 모델은 메인 PC 에서 학습해 옮기는 쪽으로 판단).
결과는 화면과 logs/exec/model_repro_{month}_{명령}.json 에 남는다.
"""
import json
import os
import shutil
import sys
import tempfile
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import LOG_DIR, MODEL_DIR, m  # noqa: E402
import model as M  # noqa: E402

N_RANDOM, N_DATES, SEED = 2000, 5, 0


def sample_rows(ds, month):
    """표본 행 번호: 마지막 N_DATES 신호일 전 종목 + 학습 기간 고정 시드 N_RANDOM 행"""
    dates = sorted(ds.date.unique())[-N_DATES:]
    last = np.flatnonzero(ds.date.isin(dates).to_numpy())
    plan = M.month_plan(ds, month)
    cutoff = plan[1] if plan else ds.date.max()
    tr = np.flatnonzero((ds.date <= cutoff).to_numpy())
    rnd = np.random.default_rng(SEED).choice(tr, min(N_RANDOM, len(tr)), replace=False)
    return np.unique(np.concatenate([last, rnd]))


def predict(boosters, X):
    return {f"seed{i + 1}": b.predict(X) for i, b in enumerate(boosters)}


def frame(ds, idx, preds, cols_seeds):
    out = pd.DataFrame({"row": idx, "code": ds.code.to_numpy()[idx], "date": ds.date.to_numpy()[idx]})
    for k in cols_seeds:
        out[k] = preds[k]
    out["mean"] = np.mean([preds[k] for k in cols_seeds], axis=0)
    return out


def compare(ref, new):
    seeds = [c for c in ref.columns if c.startswith("seed")]
    per_seed = {s: float(np.max(np.abs(ref[s].to_numpy() - new[s].to_numpy()))) for s in seeds}
    mean_diff = float(np.max(np.abs(ref["mean"].to_numpy() - new["mean"].to_numpy())))
    top = {}
    for d in sorted(ref.date.unique())[-N_DATES:]:
        a = ref[ref.date == d].sort_values(["mean", "code"], ascending=[False, True]).code.head(3).tolist()
        b = new[new.date == d].sort_values(["mean", "code"], ascending=[False, True]).code.head(3).tolist()
        top[str(pd.Timestamp(d).date())] = {"ref": a, "new": b, "same": a == b}
    worst = max(max(per_seed.values()), mean_diff)
    verdict = "동일 (차이 0)" if worst == 0.0 else ("부동소수 수준 (1e-9 이하)" if worst <= 1e-9 else "다름")
    return {"rows": len(ref), "max_abs_diff_by_seed": per_seed, "mean_pred_max_abs_diff": mean_diff,
            "top3_by_date": top, "top3_all_same": all(v["same"] for v in top.values()), "verdict": verdict}


def load_ds():
    ds = pd.read_parquet(M.DATASET)
    return ds, m.feature_cols(ds)


def ref_path(month):
    return os.path.join(MODEL_DIR, month, "repro_ref.parquet")


def cmd_ref(month):
    ds, cols = load_ds()
    b, feats, meta, chk = M.load(month)
    assert chk["ok"], chk
    idx = sample_rows(ds, month)
    X = ds.iloc[idx][feats].to_numpy(np.float32)
    seeds = [f"seed{s}" for s in meta["seeds"]]
    out = frame(ds, idx, predict(b, X), seeds)
    out.to_parquet(ref_path(month), index=False)
    return {"month": month, "ref": ref_path(month), "rows": len(out), "dates": sorted(str(pd.Timestamp(d).date()) for d in out.date.unique())[-N_DATES:]}


def predictions_with(month, model_dir=None):
    ds, cols = load_ds()
    ref = pd.read_parquet(ref_path(month))
    b, feats, meta, chk = M.load(month, model_dir)
    X = ds.iloc[ref.row.to_numpy()][feats].to_numpy(np.float32)
    seeds = [c for c in ref.columns if c.startswith("seed")]
    new = frame(ds, ref.row.to_numpy(), predict(b, X), seeds)
    return ref, new, chk


def cmd_check(month):
    ref, new, chk = predictions_with(month)
    r = compare(ref, new)
    r.update(month=month, what="복사한 모델 vs 메인 PC 기준 예측", model_load_check=chk)
    return r


def cmd_retrain(month):
    tmp = tempfile.mkdtemp(prefix=f"model_repro_{month}_")
    t0 = time.time()
    try:
        tr = M.train(month, out_dir=tmp)
        ref, new, chk = predictions_with(month, tmp)
        r = compare(ref, new)
        r.update(month=month, what="이 PC 에서 다시 학습한 모델 vs 메인 PC 기준 예측", train=tr, train_seconds=round(time.time() - t0, 1),
                 fixed_sample_vs_copied_meta=chk.get("fixed_pred_max_abs_diff"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r


if __name__ == "__main__":
    cmd, month = sys.argv[1], sys.argv[2]
    res = {"ref": cmd_ref, "check": cmd_check, "retrain": cmd_retrain}[cmd](month)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"model_repro_{month}_{cmd}.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str), flush=True)
