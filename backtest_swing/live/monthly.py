"""
다음 달 모델 한 번에 (EXEC_SPEC 4장, 11K 1): 학습일 계산 -> 데이터셋 다시 만들기 -> 학습 -> 모델 검사 -> 종목 목록 갱신.

  python backtest_swing/live/monthly.py run 2026-11              # 서브 PC: 상주 프로그램이 그 달 마지막 거래일 16:30 뒤 자동 실행
  python backtest_swing/live/monthly.py run 2026-11 --preview    # 메인 PC 미리 돌리기: 설치·종목 목록 적용 없이 models_live/preview/{월}/,
                                                                 # 기준 예측을 live/model_baseline/{월}.json 에 저장 (커밋해서 서브 PC 와 비교)
  python backtest_swing/live/monthly.py plan 2026-11             # 학습일·학습 끝 날짜만

- 학습일 = 그 달 첫 거래일, KRX 휴장일 달력(krx_holidays.json)으로 계산 (데이터셋에 그 달 날짜가 없어도 됨 = 전 거래일 저녁에 학습).
  학습 끝 = 학습일 - 61 거래일 (stage4_model.months 의 LABEL_GAP 그대로, 달력으로 셈). 지수 일봉 날짜와 달력이 그 구간에서 같은지 확인.
- 데이터셋: 동결 stage5_features.build 를 그대로 부른다. 입력은 백테스트 일봉 + 추가분(data/live_append)을 임시 폴더에 합쳐 쓰고
  stage3_data 의 폴더·지수 경로 상수와 stage4_features.DEV_END(= 데이터 마지막 날)만 바꾼다 (run_day rerun 과 같은 방식).
  추가분에서 빠진 종목(수정 계수 변경 등)은 백테스트 일봉만으로.
- 학습: model.train (PARAMS·나무 500·시드 1 ~ 5), 검사: model.load (해시·특징 41개·고정 표본 예측·월·시드).
- 종목 목록: KRX 종목기본정보(코스피·코스닥) -> data/universe_{학습일}.csv, 포인터 data/universe_current.json (학습일부터 적용).
  기존 목록에서 빠지는 종목이 5% 를 넘으면 적용하지 않고 실패로 둔다 (응답 이상 방지).
- 설치: 검사를 통과하면 models_live/stage5/{월} 로 옮긴다 (이미 있으면 {월}.old-시각 으로 보관).
- 기준 예측 비교: live/model_baseline/{월}.json (메인 PC 미리 돌리기 결과)이 있으면 같은 X 로 이번 모델의 예측을 비교해 기록 (다르면 알림, 실패 아님).
결과: logs/exec/monthly/{월}.json (마지막 줄 JSON 도 출력). 실패면 종료 코드 1.
"""
import json
import os
import shutil
import sys
import time
import traceback

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import APPEND_DIR, LOG_DIR, MODEL_DIR, UNIVERSE_POINTER, atomic_json, data_file, git_commit, now_kst, sd, s4, s5  # noqa: E402
import state as S  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE_DIR = os.path.join(HERE, "model_baseline")
DATASET_DIR = "models_live/datasets"
PREVIEW_DIR = "models_live/preview"
OUT_DIR = os.path.join(LOG_DIR, "monthly")
UNIVERSE_MAX_DROP = 0.05
N_BASELINE = 300


def plan(month, hol=None):
    """{retrain_day, train_end, prev_day}: KRX 휴장일 달력. 학습 끝 = 학습일 - 61 거래일 (stage4_model.LABEL_GAP)"""
    import stage4_model as sm
    hol = S.holidays() if hol is None else hol
    per = pd.Period(month, "M")
    first = next(d for d in pd.date_range(per.start_time, per.end_time) if S.is_trading_day(d, hol))
    days = [d for d in pd.bdate_range(first - pd.Timedelta(days=200), first) if S.is_trading_day(d, hol)]
    i = len(days) - 1
    return {"month": month, "retrain_day": str(first.date()), "train_end": str(days[i - sm.LABEL_GAP].date()),
            "prev_day": str(days[i - 1].date()), "label_gap": sm.LABEL_GAP}


def calendar_check(p, data_last):
    """학습 끝 ~ 데이터 마지막 날 구간에서 달력 거래일 = 지수 일봉 날짜 인지 (학습 끝 날짜를 세는 기준이 데이터와 같은지)"""
    from common import calendar
    cal = calendar()
    lo, hi = pd.Timestamp(p["train_end"]), pd.Timestamp(data_last)
    have = {d for d in cal if lo <= d <= hi}
    want = {d for d in pd.bdate_range(lo, hi) if S.is_trading_day(d)}
    return {"ok": have == want, "only_calendar": sorted(str(d.date()) for d in want - have),
            "only_index": sorted(str(d.date()) for d in have - want)}


def sources(tmp):
    """백테스트 일봉 + 추가분을 임시 폴더에 종목별 파일로 (load_stock 이 읽는 형식). 반환 (데이터 마지막 날, 기록)"""
    from daily import Store, index_series
    codes = sd.universe_codes()
    for sub in ("ext", "raw"):
        os.makedirs(os.path.join(tmp, sub), exist_ok=True)
    st = Store(codes, use_append=True)
    base_only = sorted(set(codes) - set(st.adj))
    if base_only:
        b = Store(base_only, use_append=False)
        st.adj.update(b.adj)
        st.raw.update(b.raw)
    last = pd.Timestamp("1900-01-01")
    for c in codes:
        a, r = st.adj[c], st.raw[c]
        a[[x for x in ("timestamp", "open", "high", "low", "close", "volume", "amount") if x in a]].to_parquet(
            os.path.join(tmp, "ext", f"{c}.parquet"), index=False)
        r[["timestamp", "open", "high", "low", "close", "volume", "amount", "factor"]].to_parquet(os.path.join(tmp, "raw", f"{c}.parquet"), index=False)
        last = max(last, pd.Timestamp(a.timestamp.max()))
    ix = index_series()
    for name, path in (("kospi", sd.KOSPI), ("kosdaq", s4.KOSDAQ)):
        f = pd.read_parquet(path)
        f["timestamp"] = pd.to_datetime(f.timestamp)
        p = data_file("index.parquet", APPEND_DIR)
        add = pd.read_parquet(p) if os.path.exists(p) else pd.DataFrame(columns=["name", "timestamp"])
        add = add[add.name == name].copy()
        add["timestamp"] = pd.to_datetime(add.timestamp)
        add = add[add.timestamp > f.timestamp.max()]
        pd.concat([f, add[f.columns]], ignore_index=True).sort_values("timestamp").to_parquet(os.path.join(tmp, f"{name}.parquet"), index=False)
    last_ix = max(ix["kospi"].index)
    return min(last, last_ix), {"codes": len(codes), "base_only": base_only, "index_last": str(last_ix.date()), "stock_last": str(last.date())}


def build_dataset(out_dir, tmp=None):
    """동결 stage5_features.build 를 그대로 (입력 경로·DEV_END 상수만 바꿈). 반환 기록 dict"""
    t0 = time.time()
    tmp = tmp or os.path.join(out_dir, "_src")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    until, info = sources(tmp)
    saved = (sd.EXT_DIR, sd.MERGED_DIR, sd.RAW_DIR, sd.KOSPI, s4.KOSDAQ, s4.DEV_END, s5.OUT)
    try:
        sd.EXT_DIR = sd.MERGED_DIR = os.path.join(tmp, "ext")
        sd.RAW_DIR = os.path.join(tmp, "raw")
        sd.KOSPI, s4.KOSDAQ = os.path.join(tmp, "kospi.parquet"), os.path.join(tmp, "kosdaq.parquet")
        s4.DEV_END, s5.OUT = until, out_dir
        s5.build()
    finally:
        sd.EXT_DIR, sd.MERGED_DIR, sd.RAW_DIR, sd.KOSPI, s4.KOSDAQ, s4.DEV_END, s5.OUT = saved
        shutil.rmtree(tmp, ignore_errors=True)
    path = os.path.join(out_dir, "dataset.parquet")
    import pyarrow.parquet as pq
    meta = pq.read_metadata(path)
    return {**info, "path": path, "data_last": str(until.date()), "rows": meta.num_rows, "seconds": round(time.time() - t0, 1)}


def compare_datasets(new_path, old_path, tol=1e-6):
    """8장 8: 지난 데이터셋과 겹치는 행(code, date)의 특징 41개 상대 오차·날짜별 대상 집합. 기록만"""
    import stage5_model as m
    if not old_path or not os.path.exists(old_path):
        return {"note": "지난 데이터셋 없음"}
    new, old = pd.read_parquet(new_path), pd.read_parquet(old_path)
    cols = m.feature_cols(new)
    hi = old.date.max()
    a, b = new[new.date <= hi], old
    j = a.merge(b, on=["code", "date"], suffixes=("", "_o"))
    sa, sb_ = a.groupby("date").code.apply(frozenset), b.groupby("date").code.apply(frozenset)
    x, y = j[cols].to_numpy(np.float64), j[[c + "_o" for c in cols]].to_numpy(np.float64)
    same = (x == y) | (np.isnan(x) & np.isnan(y))                      # 같은 값(무한대 포함)·둘 다 결측
    fin = np.isfinite(x) & np.isfinite(y)
    rel = np.zeros_like(x)
    rel[fin] = np.abs(x[fin] - y[fin]) / np.maximum(np.abs(y[fin]), 1e-12)
    rel[~same & ~fin] = np.inf                                          # 한쪽만 결측·무한대
    rel[same] = 0
    bad = rel.max(axis=1) > tol
    return {"old": old_path, "old_last": str(hi.date()), "rows_new": int(len(a)), "rows_old": int(len(b)), "common": int(len(j)),
            "only_new": int(len(a) - len(j)), "only_old": int(len(b) - len(j)), "rows_feature_diff": int(bad.sum()),
            "max_rel_err": float(rel.max()) if len(j) else None,
            "label_R_diff_rows": int((~np.isclose(j.R.to_numpy(), j.R_o.to_numpy(), equal_nan=True)).sum()) if "R" in j and "R_o" in j else None,
            "days_set_diff": int(sum(sa[d] != sb_[d] for d in sa.index.intersection(sb_.index)))}


def fetch_universe(bas_dd):
    """KRX 종목기본정보 코스피·코스닥 -> DataFrame(code, name, market, reg_day, state, order_warning) (data/universe_20261019.csv 와 같은 열)"""
    import requests
    from kiwoom import krx_official as K
    key = os.getenv("KRX_API_KEY", "")
    if not key:
        raise RuntimeError("KRX_API_KEY 가 .env 에 없습니다")
    rows = []
    for path in ("sto/stk_isu_base_info", "sto/ksq_isu_base_info"):
        r = requests.get(f"{K.BASE_URL}/{path}", headers={"AUTH_KEY": key}, params={"basDd": bas_dd}, timeout=30)
        r.raise_for_status()
        got = r.json().get("OutBlock_1") or []
        if not got:
            raise RuntimeError(f"{path} {bas_dd} 응답 0건")
        for it in got:
            ld = str(it.get("LIST_DD") or "")
            rows.append({"code": str(it["ISU_SRT_CD"]), "name": it.get("ISU_ABBRV"), "market": it.get("MKT_TP_NM"),
                         "reg_day": f"{ld[:4]}-{ld[4:6]}-{ld[6:8]}" if len(ld) == 8 else "",
                         "state": "|".join(x for x in (it.get("SECUGRP_NM"), it.get("KIND_STKCERT_TP_NM"), it.get("SECT_TP_NM")) if x),
                         "order_warning": 0})
    return pd.DataFrame(rows).drop_duplicates("code").sort_values("code").reset_index(drop=True)


def update_universe(p, data_last, apply=True, out_csv=None, pointer=None):
    """종목 목록 갱신. 반환 기록 dict (ok False 면 적용 안 함)"""
    old_path = sd.UNIVERSE
    old = pd.read_csv(old_path, dtype={"code": str})
    old_codes = set(old[old.market.isin(["KOSPI", "KOSDAQ"])].code)
    new = fetch_universe(pd.Timestamp(data_last).strftime("%Y%m%d"))
    new_codes = set(new[new.market.isin(["KOSPI", "KOSDAQ"])].code)
    have = {f[:-8] for f in os.listdir(sd.RAW_DIR) if f.endswith(".parquet")}
    rec = {"old": old_path, "old_n": len(old_codes), "new_n": len(new_codes), "added": len(new_codes - old_codes),
           "removed": len(old_codes - new_codes), "removed_sample": sorted(old_codes - new_codes)[:20],
           "added_with_daily": sorted((new_codes - old_codes) & have)[:20],
           "used_old": len((old_codes & have) - sd.EXCLUDE), "used_new": len((new_codes & have) - sd.EXCLUDE)}
    drop = rec["removed"] / max(1, len(old_codes))
    if drop > UNIVERSE_MAX_DROP:
        return {**rec, "ok": False, "error": f"빠지는 종목 {rec['removed']} ({drop:.1%}) > {UNIVERSE_MAX_DROP:.0%} -> 적용 안 함 (응답 이상 의심)"}
    out_csv = out_csv or f"data/universe_{pd.Timestamp(p['retrain_day']):%Y%m%d}.csv"
    new.to_csv(out_csv, index=False, encoding="utf-8-sig")
    rec.update(ok=True, path=out_csv)
    if apply:
        pp = pointer or UNIVERSE_POINTER
        cur = json.load(open(pp, encoding="utf-8")) if os.path.exists(pp) else {"items": []}
        cur["items"] = [i for i in cur["items"] if i["from"] != p["retrain_day"]] + [{"from": p["retrain_day"], "path": out_csv, "source": "KRX 종목기본정보",
                                                                                    "bas_dd": str(pd.Timestamp(data_last).date()), "written": f"{now_kst():%Y-%m-%d %H:%M:%S}"}]
        atomic_json(pp, cur)
        rec["pointer"] = pp
    return rec


def baseline_rows(ds_path, cols, n=N_BASELINE, seed=0):
    """기준 예측용 행: 데이터셋 마지막 20거래일(실행 때 점수 매기는 최근 행)에서 n 개 고정 추출"""
    ds = pd.read_parquet(ds_path, columns=["code", "date"] + cols)
    days = np.sort(ds.date.unique())[-20:]
    x = ds[ds.date.isin(days)]
    idx = np.sort(np.random.default_rng(seed).choice(len(x), min(n, len(x)), replace=False))
    return x.iloc[idx]


def predict(model_dir, month, X):
    import model as M
    b, _, _, chk = M.load(month, model_dir=model_dir)
    return M.score(b, np.asarray(X, np.float32)), chk


def compare_baseline(month, model_dir, meta):
    """메인 PC 미리 돌리기의 기준 예측과 비교 (같은 X). 반환 기록 (기준 파일이 없으면 note)"""
    p = os.path.join(BASELINE_DIR, f"{month}.json")
    if not os.path.exists(p):
        return {"note": f"기준 예측 없음 ({p})"}
    bl = json.load(open(p, encoding="utf-8"))
    pred, _ = predict(model_dir, month, bl["X"])
    ref = np.asarray(bl["pred_mean"])
    d = np.abs(pred - ref)
    top = lambda v: set(np.argsort(-v)[:30])  # noqa: E731
    return {"baseline": p, "baseline_data_last": bl.get("data_last"), "baseline_train_rows": bl.get("train_rows"),
            "same_train_end": bl.get("train_end") == meta["train_end"], "same_train_rows": bl.get("train_rows") == meta["train_rows"],
            "n": int(len(ref)), "max_abs_diff": float(d.max()), "mean_abs_diff": float(d.mean()), "identical": bool(d.max() == 0.0),
            "corr": float(np.corrcoef(pred, ref)[0, 1]) if len(ref) > 2 else None, "top30_overlap": len(top(pred) & top(ref))}


def run(month, preview=False):
    t0 = time.time()
    rec = {"month": month, "preview": preview, "started": f"{now_kst():%Y-%m-%d %H:%M:%S}", "git_commit": git_commit(), "ok": False, "steps": {}}
    out_json = os.path.join(PREVIEW_DIR if preview else OUT_DIR, f"{month}.json")
    try:
        p = plan(month)
        rec["plan"] = p
        ds_dir = os.path.join(PREVIEW_DIR if preview else DATASET_DIR, month)
        b = build_dataset(ds_dir)
        rec["steps"]["dataset"] = b
        rec["data_last"] = b["data_last"]
        rec["data_up_to_prev_day"] = b["data_last"] == p["prev_day"]  # 학습일 전 거래일까지 있으면 라벨이 백테스트와 같다
        rec["calendar_check"] = cc = calendar_check(p, b["data_last"])
        if not cc["ok"]:
            raise RuntimeError(f"달력 거래일과 지수 일봉 날짜가 다름: {cc}")
        ds = pd.read_parquet(b["path"], columns=["date", "reason"])
        tr = ds.date <= pd.Timestamp(p["train_end"])
        import stage3_backtest as sb
        rec["steps"]["labels"] = {"train_rows": int(tr.sum()), "train_rows_label_end": int((tr & (ds.reason == sb.R_END)).sum()),
                                  "note": "label_end = 데이터 끝(그날까지 안 끝난 거래)으로 닫힌 라벨. 데이터가 학습일 전 거래일까지 있으면 백테스트와 같다"}
        del ds
        prev_m = (pd.Period(month, "M") - 1).strftime("%Y-%m")
        old = os.path.join(DATASET_DIR, prev_m, "dataset.parquet")
        import model as M
        rec["steps"]["dataset_compare"] = compare_datasets(b["path"], old if os.path.exists(old) else M.DATASET)
        stage = os.path.join(PREVIEW_DIR, month, "model") if preview else os.path.join(MODEL_DIR, f"{month}.new")
        shutil.rmtree(stage, ignore_errors=True)
        rec["steps"]["train"] = M.train(month, out_dir=stage, dataset=b["path"], plan=(p["retrain_day"], p["train_end"]))
        if not rec["steps"]["train"].get("trained"):
            raise RuntimeError(f"학습 안 됨: {rec['steps']['train']}")
        _, _, meta, chk = M.load(month, model_dir=stage)
        rec["steps"]["check"] = {k: v for k, v in chk.items() if k != "files"}
        if not chk["ok"]:
            raise RuntimeError(f"모델 검사 실패: {rec['steps']['check']}")
        if meta["train_end"] != p["train_end"] or meta["retrain_day"] != p["retrain_day"]:
            raise RuntimeError(f"모델 meta 학습일·학습 끝 다름: {meta['retrain_day']} {meta['train_end']} vs {p}")
        cols = meta["features"]
        if preview:
            x = baseline_rows(b["path"], cols)
            pred, _ = predict(stage, month, x[cols].to_numpy(np.float32))
            os.makedirs(BASELINE_DIR, exist_ok=True)
            bl = {"month": month, "made": f"{now_kst():%Y-%m-%d %H:%M:%S}", "git_commit": git_commit(), "retrain_day": p["retrain_day"],
                  "train_end": p["train_end"], "train_rows": meta["train_rows"], "data_last": b["data_last"], "dataset_rows": b["rows"],
                  "model_sha256": meta["files"], "features": cols, "rows": [{"code": c, "date": str(pd.Timestamp(d).date())} for c, d in zip(x.code, x.date)],
                  "X": [[float(v) for v in r] for r in x[cols].to_numpy(np.float64)], "pred_mean": [float(v) for v in pred],
                  "note": "메인 PC 미리 돌리기. 서브 PC 학습 결과와 같은 X 로 예측을 비교 (데이터 끝이 다르면 라벨이 달라 예측도 조금 다를 수 있음)"}
            atomic_json(os.path.join(BASELINE_DIR, f"{month}.json"), bl)
            rec["steps"]["baseline"] = {"path": os.path.join(BASELINE_DIR, f"{month}.json"), "n": len(pred)}
            rec["steps"]["universe"] = update_universe(p, b["data_last"], apply=False, out_csv=os.path.join(PREVIEW_DIR, month, "universe.csv"))
        else:
            rec["steps"]["baseline_compare"] = compare_baseline(month, stage, meta)
            final = os.path.join(MODEL_DIR, month)
            if os.path.exists(final):
                os.replace(final, f"{final}.old-{now_kst():%Y%m%d%H%M%S}")
            os.replace(stage, final)
            _, _, _, chk2 = M.load(month)
            rec["steps"]["installed"] = {"path": final, "check_ok": chk2["ok"]}
            if not chk2["ok"]:
                raise RuntimeError(f"설치 뒤 모델 검사 실패: {chk2}")
            u = update_universe(p, b["data_last"])
            rec["steps"]["universe"] = u
            if not u.get("ok"):
                raise RuntimeError(f"종목 목록 갱신 실패 (모델은 설치됨): {u.get('error')}")
        rec["ok"] = True
    except Exception as e:  # noqa: BLE001
        rec["error"] = repr(e)[:600]
        rec["trace"] = traceback.format_exc()[-1500:]
    rec["seconds"] = round(time.time() - t0, 1)
    rec["finished"] = f"{now_kst():%Y-%m-%d %H:%M:%S}"
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    atomic_json(out_json, rec)
    return rec


def next_month(day):
    return (pd.Period(pd.Timestamp(day), "M") + 1).strftime("%Y-%m")


def last_trading_day_of_month(day, hol=None):
    """day 가 그 달 마지막 거래일인가 (다음 거래일이 다른 달)"""
    hol = S.holidays() if hol is None else hol
    day = pd.Timestamp(day).normalize()
    if not S.is_trading_day(day, hol):
        return False
    d = day + pd.Timedelta(days=1)
    while not S.is_trading_day(d, hol):
        d += pd.Timedelta(days=1)
    return d.month != day.month


def summary_line(r):
    s = r.get("steps", {})
    if not r.get("ok"):
        return f"[월별 모델 실패] {r['month']}: {r.get('error')} — 수동: python backtest_swing/live/monthly.py run {r['month']}"[:400]
    bc = s.get("baseline_compare") or {}
    bct = (f"기준 예측 저장 {s['baseline']['path']}" if s.get("baseline") else "기준 예측 없음" if not bc or bc.get("note") else f"기준 예측 {'같음' if bc.get('identical') else '다름'} (최대 차이 {bc.get('max_abs_diff'):.2e}, "
           f"상위 30 겹침 {bc.get('top30_overlap')})")
    u = s.get("universe") or {}
    return (f"[월별 모델 {'미리 돌리기' if r.get('preview') else '설치'}] {r['month']}: 학습일 {r['plan']['retrain_day']}, 학습 끝 {r['plan']['train_end']}, "
            f"데이터 끝 {r.get('data_last')}, 학습 행 {s['train']['train_rows']:,}, 검사 {'통과' if s['check']['ok'] else '실패'}, "
            f"종목 목록 {u.get('new_n')} (빠짐 {u.get('removed')}, 추가 {u.get('added')}), {bct}, {r['seconds']:.0f}초")


if __name__ == "__main__":
    cmd, mo = sys.argv[1], sys.argv[2]
    if cmd == "plan":
        print(json.dumps(plan(mo), ensure_ascii=False))
        sys.exit(0)
    r = run(mo, preview="--preview" in sys.argv[3:])
    print(summary_line(r), flush=True)
    print(json.dumps({k: v for k, v in r.items() if k != "trace"}, ensure_ascii=False, default=str), flush=True)
    sys.exit(0 if r["ok"] else 1)
