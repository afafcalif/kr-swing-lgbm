"""
장후 특징값 저장 (EXEC_SPEC 11M, 2026-10-28). 키움 호출 없음. backup.py 가 백업 전에 같은 작업 안에서 부른다 (--pending).

  python backtest_swing/live/features_eod.py YYYY-MM-DD [--out-dir DIR]   # 그날 하루
  python backtest_swing/live/features_eod.py --pending                    # 최근 14일 중 아직 안 한 날 (새 날부터, 한 번에 최대 3일)

- 그날 로그(logs/exec/{날짜}.json)의 15:18:30 스냅샷·지수 값·장부 상태(ledger_before)로, 그날 쓴 데이터 버전(로그의 data_version 과 update.json 이 같은
  data/live_append/versions/{버전})에서 장후 대조(run_day.py rerun)와 같은 계산(동결 load_stock·market_features 그대로 = ShardPool frozen,
  E.run_day frozen_market) -> 대상 종목의 특징값·점수·순위(1 = 점수 최고)·두 장부 상위 3 표시를 logs/exec/features/{날짜}.parquet.
- 상주 프로그램이 낸 scores/{날짜}.parquet 와 점수 대조 (종목이 같고 최대 절대 차 0 이면 통과) -> logs/exec/features/{날짜}.json, features_eod.log.
  다르면 텔레그램 알림. 그날 버전 폴더가 남아 있지 않으면(로컬은 최근 3개만, 11S 부터 그날 15:18 버전은 고정) 계산하지 않고 version_missing 기록 + 알림.
- 데이터 버전은 이 프로세스와 계산 작업 프로세스 모두 common.data_dir 를 그 버전 폴더로 고정한다 (실행 중 CURRENT.json 이 바뀌어도 그대로).
  상주 코드·동결 코드는 고치지 않는다. 동결 load_stock 의 임시 파일은 이 실행의 임시 폴더에 쓰고 끝나면 지운다.
- 장중(거래일 07:00 ~ 하루 끝)에는 실행하지 않는다 (상주 프로그램과 CPU 를 나누지 않게). 1분봉은 저장하지 않는다.
"""
import argparse
import functools
import json
import os
import re
import shutil
import sys
import tempfile
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402

CFG = settings.load()
import common as C  # noqa: E402
from common import APPEND_DIR, LOG_DIR, atomic_json, now_kst  # noqa: E402

NAMES = ("strategy", "full")
FINAL = {"ok", "score_diff", "version_missing", "no_snapshot", "no_scores"}
MAX_TRIES = 3
PENDING_DAYS = 14
PENDING_MAX = 3


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def feat_dir(log_dir=LOG_DIR):
    return os.path.join(log_dir, "features")


def find_version(data_version, append_dir=APPEND_DIR, name=None):
    """그날 데이터 버전 폴더: 11S 고정 버전 이름(로그 data_dir_version)이 있으면 versions/{이름}, 아니면 update.json 의 data_version 이 같은
    versions/{버전} (예전 방식이면 append_dir 자체). 없으면 None. data_version 기록이 없는 예전 로그는 현재 버전 (CURRENT.json)"""
    if not data_version and not name:
        return C.data_dir(append_dir)
    return C.version_dir(name, data_version, append_dir)


def _pinned_loop(vdir, conn, codes, use_append, frozen=False):
    """계산 작업 프로세스: 데이터 버전 폴더를 고정한 뒤 engine._loop 를 그대로 (spawn 이라 부모의 고정이 넘어오지 않음)"""
    import common
    common.data_dir = lambda append_dir=None: vdir
    import engine
    engine._loop(conn, codes, use_append, frozen)


def compute(day, log, vdir):
    """run_day.rerun 과 같은 계산 (그날 대상 종목 목록, 그달 모델, 저장한 스냅샷, 그날 지수 값, 장부 상태). 반환: (E.run_day 결과, 정보)"""
    import engine as E
    import model as M
    import run_day as R
    saved = C.data_dir, E._loop, os.environ.get("TEMP"), C.sd.UNIVERSE
    C.apply_universe(day)
    tmp = tempfile.mkdtemp(prefix="kr_feat_")
    C.data_dir = lambda append_dir=None: vdir
    E._loop = functools.partial(_pinned_loop, vdir)
    os.environ["TEMP"] = tmp  # 동결 load_stock·market_features 의 임시 파일 (작업 프로세스도 물려받음)
    try:
        from daily import index_series
        b, cols, meta, chk = M.load(day.strftime("%Y-%m"))
        full = pd.read_parquet(log["live"]["snapshot"]["file"])
        snap = pd.DataFrame({c[2:]: full[c] for c in full.columns if c.startswith("p_")})
        rows = R.snap_rows(snap, full, day)
        pool = E.ShardPool(C.sd.universe_codes(), use_append=True, frozen=True)
        try:
            res = E.run_day(pool, day, rows, index_series(), log["live"]["index_values"], b, cols, log["ledger_before"], frozen_market=True)
        finally:
            pool.close()
        info = {"model_month": day.strftime("%Y-%m"), "model_check": bool(chk["ok"]), "features": list(cols), "rows": len(rows),
                "loaded": pool.loaded, "excluded_n": len(pool.excluded), "errors": res["errors"][:20], "universe": C.sd.UNIVERSE}
    finally:
        C.data_dir, E._loop, C.sd.UNIVERSE = saved[0], saved[1], saved[3]
        if saved[2] is None:
            os.environ.pop("TEMP", None)
        else:
            os.environ["TEMP"] = saved[2]
        shutil.rmtree(tmp, ignore_errors=True)
    return res, info


def frame(res, day, data_version):
    """대상 종목 표: 특징값·점수·순위(1 = 점수 최고, 엔진과 같은 안정 정렬)·두 장부 상위 3"""
    e = res["eligible"].copy().reset_index(drop=True)
    order = np.argsort(-e.score.to_numpy(), kind="stable")
    rank = np.empty(len(e), dtype=np.int64)
    rank[order] = np.arange(1, len(e) + 1)
    e.insert(0, "date", pd.Timestamp(day))
    e["rank"] = rank
    for n in NAMES:
        e[f"top3_{n}"] = e.code.isin(set(res["ledgers"][n]["top3"].code))
    e["data_version"] = data_version or ""
    return e


def compare_scores(eod, live):
    """장후 점수 vs 상주 점수 (code, score). 통과 = 종목이 같고 최대 절대 차 0"""
    a = live.set_index("code").score.astype(np.float64)
    b = eod.set_index("code").score.astype(np.float64)
    com = a.index.intersection(b.index)
    mx = float((a[com] - b[com]).abs().max()) if len(com) else None
    only_l, only_e = sorted(set(a.index) - set(b.index)), sorted(set(b.index) - set(a.index))
    return {"n_live": int(len(a)), "n_eod": int(len(b)), "n_common": int(len(com)), "only_live": only_l[:20], "only_eod": only_e[:20],
            "only_live_n": len(only_l), "only_eod_n": len(only_e), "max_abs_diff": mx,
            "ok": bool(not only_l and not only_e and len(com) > 0 and mx == 0.0)}


def _send(text, log_dir, send):
    try:
        if send is not None:
            return send(text)
        import notify as N
        return N.send(text, log_path=os.path.join(log_dir, "alerts.log"))
    except Exception:  # noqa: BLE001
        return None


def _record(rec, log_dir, out_dir):
    od = out_dir or feat_dir(log_dir)
    atomic_json(os.path.join(od, f"{rec['date']}.json"), rec)
    line = (f"{now_kst():%Y-%m-%d %H:%M:%S} {rec['date']} {rec['status']} "
            + json.dumps({k: rec[k] for k in ("score_compare", "n", "data_version", "reason", "seconds") if k in rec}, ensure_ascii=False, default=str))
    with open(os.path.join(od, "features_eod.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return rec


def run_one(day, log_dir=LOG_DIR, out_dir=None, append_dir=APPEND_DIR, compute_fn=None, send=None):
    """하루: 계산 -> parquet -> 점수 대조 -> json·로그 (다르면 알림). 반환: 기록 dict"""
    t0 = time.time()
    day = pd.Timestamp(day).normalize()
    d = str(day.date())
    od = out_dir or feat_dir(log_dir)
    os.makedirs(od, exist_ok=True)
    try:
        prev = jload(os.path.join(od, f"{d}.json"))
    except Exception:  # noqa: BLE001
        prev = {}
    rec = {"date": d, "run": f"{now_kst():%Y-%m-%d %H:%M:%S}", "tries": int(prev.get("tries", 0)) + 1}
    lp = os.path.join(log_dir, f"{d}.json")
    try:
        log = jload(lp)
    except (OSError, ValueError):
        log = None
    sf = ((log or {}).get("live") or {}).get("snapshot", {}).get("file")
    if not log or not sf or not os.path.exists(sf) or "index_values" not in log["live"] or "ledger_before" not in log:
        return _record({**rec, "status": "no_snapshot", "reason": f"그날 로그·15:18:30 스냅샷 없음 ({lp})"}, log_dir, out_dir)
    dv = log.get("data_version") or (log.get("data") or {}).get("data_version")
    rec["data_version"] = dv
    vdir = find_version(dv, append_dir, name=log.get("data_dir_version"))
    if vdir is None:
        rec = _record({**rec, "status": "version_missing", "reason": f"데이터 버전 {dv} 폴더가 남아 있지 않음 -> 계산 안 함"}, log_dir, out_dir)
        _send(f"[장후 특징값] {d}: 그날 데이터 버전 {dv} 폴더가 없어 저장 못 함", log_dir, send)
        return rec
    rec["version_dir"] = vdir
    try:
        res, info = (compute_fn or compute)(day, log, vdir)
        df = frame(res, day, dv)
        pq = os.path.join(od, f"{d}.parquet")
        tmp = f"{pq}.{os.getpid()}.tmp"
        df.to_parquet(tmp, index=False)
        os.replace(tmp, pq)
    except Exception as e:  # noqa: BLE001
        rec = _record({**rec, "status": "error", "reason": repr(e)[:500], "seconds": round(time.time() - t0, 1)}, log_dir, out_dir)
        if rec["tries"] >= MAX_TRIES:
            _send(f"[장후 특징값] {d}: {rec['tries']}번 실패 -> 그만둠: {e!r}"[:300], log_dir, send)
        return rec
    rec.update(n=len(df), file=pq, info=info, top3={n: list(res["ledgers"][n]["top3"].code) for n in NAMES})
    scf = log["live"].get("scores_file") or os.path.join(log_dir, "scores", f"{d}.parquet")
    if not os.path.exists(scf):
        rec.update(status="no_scores", reason=f"상주 점수 파일 없음 ({scf}) -> 대조 못 함 (특징값은 저장함)")
    else:
        cmp = compare_scores(df[["code", "score"]], pd.read_parquet(scf))
        rec.update(status="ok" if cmp["ok"] else "score_diff", score_compare=cmp)
        if not cmp["ok"]:
            _send(f"[장후 특징값 점수 차이] {d}: 최대 차 {cmp['max_abs_diff']}, 공통 {cmp['n_common']}, 상주에만 {cmp['only_live_n']}, "
                  f"장후에만 {cmp['only_eod_n']} (데이터 버전 {dv})", log_dir, send)
    rec["seconds"] = round(time.time() - t0, 1)
    return _record(rec, log_dir, out_dir)


def pending(log_dir=LOG_DIR, out_dir=None, today=None, days=PENDING_DAYS, limit=PENDING_MAX):
    """아직 안 한 날 (최근 days 일 안 그날 로그가 있고, 끝난 기록(FINAL)이 없고, 실패 MAX_TRIES 번 미만). 새 날부터 limit 개"""
    today = pd.Timestamp(today or now_kst()).normalize()
    od = out_dir or feat_dir(log_dir)
    out = []
    names = sorted((n for n in os.listdir(log_dir) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", n)), reverse=True) if os.path.isdir(log_dir) else []
    for n in names:
        d = pd.Timestamp(n[:10])
        if not (today - pd.Timedelta(days=days) <= d <= today):
            continue
        try:
            r = jload(os.path.join(od, f"{n[:10]}.json"))
        except Exception:  # noqa: BLE001
            r = {}
        if r.get("status") in FINAL or int(r.get("tries", 0)) >= MAX_TRIES:
            continue
        out.append(n[:10])
        if len(out) >= limit:
            break
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?")
    ap.add_argument("--pending", action="store_true")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--force", action="store_true", help="장중에도 실행 (시험·손으로 확인할 때만)")
    a = ap.parse_args(argv)
    import backup as B
    cal = B.Calendar(times=CFG["times"])
    if cal.in_session(now_kst()) and not a.force:
        print("장중 -> 실행 안 함 (상주 프로그램과 CPU 를 나누지 않게)", flush=True)
        return 0
    days = pending(out_dir=a.out_dir) if a.pending else [a.date]
    for d in days:
        if not d:
            continue
        r = run_one(d, out_dir=a.out_dir)
        print(json.dumps({k: r.get(k) for k in ("date", "status", "n", "score_compare", "reason", "seconds")}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    settings.refuse_test_env("features_eod.py")
    sys.exit(main())
