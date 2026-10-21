"""
EXEC_SPEC.md 3A: 15:19 방식 확장 측정과 판정 (시세 조회 TR 만, 실전 토큰, 1초 5회 이하)

  python backtest_swing/exec_stage3_extend.py collect   # 22:00 이후 시작. 2025-10-01 ~ 2026-07-17 후보 1분봉 + 지수 분봉, 이미 받은 파일은 더 이른 분봉만 이어 붙임
  python backtest_swing/exec_stage3_extend.py judge     # 2025-10-01 ~ 2026-10-16 전체 겹침·순위·대상 변화 + 3A 판정 (2026-01-02 ~ 2026-08-14, 필터 통과일)

결과: backtest_swing/results/exec_prep/stage3_measure/ (minute/ 이어 붙임, extend_collect.json, extend_judge.md)
"""
import json
import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exec_stage3_measure as x  # noqa: E402
import exec_stage3_measure_all as xa  # noqa: E402
from exec_stage3_measure import OUT, MIN_DIR, sb, sd, sm  # noqa: E402

EXT_START, EXT_END = pd.Timestamp("2025-10-01"), pd.Timestamp("2026-07-17")
FULL_END = pd.Timestamp("2026-10-16")
JUDGE_START, JUDGE_END = pd.Timestamp("2026-01-02"), pd.Timestamp("2026-08-14")
START_AFTER = "22:00"
VAL_SIGNALS, VAL_R = 411, 0.328


def calendar(a, b):
    cal = pd.DatetimeIndex(pd.read_parquet(sd.KOSPI, columns=["timestamp"]).timestamp).sort_values()
    return cal[(cal >= a) & (cal <= b)]


class Count429(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.n = 0

    def emit(self, record):
        if "429" in record.getMessage():
            self.n += 1


def merge_save(path, new):
    """이미 받은 파일은 그대로 두고 더 이른 분봉만 앞에 이어 붙인다"""
    if os.path.exists(path):
        old = pd.read_parquet(path)
        if len(old):
            older = new[new.ts < old.ts.min()] if len(new) else new
            out = pd.concat([older, old], ignore_index=True)
        else:
            out = new
    else:
        out = new
    out.to_parquet(path, index=False)
    return out


def covered(path, first_day):
    if not os.path.exists(path):
        return False
    d = pd.read_parquet(path, columns=["ts"])
    return len(d) > 0 and d.ts.min()[:8] <= first_day


def collect():
    now = pd.Timestamp.now()
    while now.strftime("%H:%M") < START_AFTER and now.strftime("%H:%M") >= "07:00":
        time.sleep(30)
        now = pd.Timestamp.now()
    days = calendar(EXT_START, EXT_END)
    first = days[0].strftime("%Y%m%d")
    t0 = time.time()
    cand = {}
    for code in sd.universe_codes():
        s = sd.load_stock(code).set_index("timestamp").amt20.reindex(days)
        d = s.index[(s >= sd.MIN_AMOUNT_20).to_numpy()]
        if len(d):
            cand[code] = [z.strftime("%Y%m%d") for z in d]
    json.dump(cand, open(os.path.join(OUT, "candidates_ext.json"), "w"))
    print(f"ext candidates: {len(cand)} stocks ({time.time() - t0:.0f}s)", flush=True)
    h429 = Count429()
    logging.getLogger("KiwoomRestClient").addHandler(h429)
    caller = x.Caller()
    t1 = time.time()
    done = skipped = 0
    try:
        for inds, name in (("001", "kospi"), ("101", "kosdaq")):
            p = os.path.join(OUT, f"index_{name}.parquet")
            if not covered(p, first):
                df, _ = x.pull(caller, "ka20005", {"inds_cd": inds, "tic_scope": "1"}, "inds_min_pole_qry", first, max_pages=300)
                for c in ("open", "high", "low", "close"):
                    df[c] = df[c] / 100.0
                merge_save(p, df)
        for i, code in enumerate(sorted(cand)):
            p = os.path.join(MIN_DIR, f"{code}.parquet")
            if covered(p, first):
                done += 1
                continue
            try:
                df, _ = x.pull(caller, "ka10080", {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "0"}, "stk_min_pole_chart_qry",
                               first, max_pages=160)
            except ValueError:
                skipped += 1
                continue
            merge_save(p, df)
            done += 1
            if (i + 1) % 25 == 0:
                print(f"  {i + 1}/{len(cand)} stocks, {caller.n} requests, {time.time() - t1:.0f}s, rate errors {len(caller.errors)}, "
                      f"client 429 {h429.n}", flush=True)
    except RuntimeError as e:
        print("STOPPED:", e, flush=True)
    rec = {"stocks_total": len(cand), "stocks_done": done, "stocks_skipped_other_error": skipped, "requests": caller.n,
           "seconds": time.time() - t1, "errors": caller.errors, "stopped": caller.stopped, "client_429": h429.n,
           "hourly": dict(caller.hourly), "started": now.strftime("%Y-%m-%d %H:%M:%S"),
           "window": [str(days[0].date()), str(days[-1].date())]}
    json.dump(rec, open(os.path.join(OUT, "extend_collect.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v_ for k, v_ in rec.items() if k != "errors"}, ensure_ascii=False), flush=True)


def judge():
    t0 = time.time()
    days = calendar(EXT_START, FULL_END)
    c_ext = json.load(open(os.path.join(OUT, "candidates_ext.json")))
    c_old = json.load(open(os.path.join(OUT, "candidates.json")))
    cand = {}
    for dct in (c_ext, c_old):
        for k, v_ in dct.items():
            cand.setdefault(k, set()).update(v_)
    tasks = [(k, sorted(v_)) for k, v_ in cand.items()]
    with ProcessPoolExecutor(max_workers=min(14, os.cpu_count() or 4)) as ex:
        rows = [r for part in ex.map(x._worker, tasks, chunksize=4) for r in part]
    snap = pd.DataFrame(rows)
    snap.to_parquet(os.path.join(OUT, "snap_rows_ext.parquet"), index=False)
    print(f"15:19 rows {len(snap):,} ({time.time() - t0:.0f}s)", flush=True)
    close_frame, snap_frame, chk, mk_note, prev_below = xa.build_frames(days, snap, (pd.Timestamp("2025-10-01"), pd.Timestamp("2026-10-31")))
    dl = list(days)
    cp, spk = close_frame.loc[xa.top3(close_frame, dl)], snap_frame.loc[xa.top3(snap_frame, dl)]
    ov, r_sc, r_cs = xa.compare(close_frame, snap_frame, cp, spk, dl)
    ov.to_csv(os.path.join(OUT, "daily_overlap_ext.csv"), index=False, encoding="utf-8-sig")

    # 3A 판정: 2026-01-02 ~ 2026-08-14 진입, 시장 필터 통과일, 장부는 이 날들만으로
    jd = [d for d in dl if JUDGE_START <= d <= JUDGE_END and bool(close_frame[close_frame.date == d].filter_ok.any())]
    cj, sj = close_frame.loc[xa.top3(close_frame, jd)], snap_frame.loc[xa.top3(snap_frame, jd)]

    def rand200(frame):
        rng = np.random.default_rng(42)
        rows_ = xa.day_rows(frame, jd)
        return np.array([frame.loc[sm.pick_nodup(frame, [rng.permutation(r) for r in rows_], sm.TOP_N), "R_real"].mean()
                         for _ in range(sm.N_RAND_REPS)])

    rs, rc = rand200(snap_frame), rand200(close_frame)
    e_s = sj.R_real.mean()
    ok_a, ok_b = e_s > 0, (e_s > rs.mean() and e_s > np.quantile(rs, .90))
    check_close = (len(cj) == VAL_SIGNALS and round(cj.R_real.mean(), 3) == VAL_R)
    col = json.load(open(os.path.join(OUT, "extend_collect.json"), encoding="utf-8"))
    ovj = ov[ov.date.isin(jd)]
    L = ["# EXEC_SPEC 3A: 15:19 방식 확장 측정과 판정 (해석 없음)\n",
         "## 수집 (야간)\n",
         f"- 시작 {col['started']}, 구간 {col['window']}, 요청 {col['requests']:,}회, {col['seconds'] / 3600:.2f}시간, 종목 {col['stocks_done']} / "
         f"{col['stocks_total']} (유량 아닌 오류로 건너뜀 {col['stocks_skipped_other_error']}), 유량 오류(백오프) {len(col['errors'])}건, "
         f"키움 클라이언트 429 재시도 {col['client_429']}회, 중단 {col['stopped'] or '없음'}",
         f"- 시간당 호출 수: {col['hourly']}",
         f"- 15:19 행 {len(snap):,}개 (15:19 까지 체결 없는 종목·날짜 {int((~snap.snap).sum())}), 지수 "
         f"{'업종 분봉 15:19' if not mk_note else '업종 분봉 없는 날은 종가로 대신 ' + str(len(mk_note)) + '건'}",
         f"- 모델: 그 달 모델 2025-10 ~ 2026-10 (검증·개발과 같은 학습 집합·시드 1~5). 저장 예측과의 최대 차이: {chk}\n",
         "## 3A 판정 (2026-01-02 ~ 2026-08-14 진입, 시장 필터 통과일, 15:19 기준 상위 3)\n",
         f"- 판정 구간 필터 통과일 {len(jd)}일. 확인: 같은 방식의 종가 기준 상위 3 = {len(cj)}건, 평균 {cj.R_real.mean():+.3f}R "
         f"(검증 실행 {VAL_SIGNALS}건 / {VAL_R:+.3f}R 와 같음: {check_close}), 종가 기준 무작위 200회 평균 {rc.mean():+.3f}, 90% 지점 {np.quantile(rc, .90):+.3f}",
         f"- 15:19 기준 상위 3: {len(sj)}건, 평균 {e_s:+.3f}R, % 순수익률 {sj.net_real.mean() * 100:+.2f}%",
         f"- 15:19 기준 무작위 200회: 평균 {rs.mean():+.3f}, 5% 지점 {np.quantile(rs, .05):+.3f}, 90% 지점 {np.quantile(rs, .90):+.3f}, 95% 지점 {np.quantile(rs, .95):+.3f}",
         f"- {'O' if ok_a else 'X'} (a) 평균 R > 0: {e_s:+.3f}",
         f"- {'O' if ok_b else 'X'} (b) 무작위 평균 {rs.mean():+.3f} 보다 높고 90% 지점 {np.quantile(rs, .90):+.3f} 초과: {e_s:+.3f}",
         f"\n**{'둘 다 만족 -> 15:19 방식으로 구현 진행' if (ok_a and ok_b) else '만족하지 못함 -> 구현 전에 멈추고 보고'}**"
         + ("" if check_close else " (단, 종가 기준 확인이 검증 실행과 다름 -> 판정 전에 멈추고 보고)") + "\n",
         "판정 구간 상위 3 겹침 (장부는 판정 구간 필터 통과일만으로):\n"]
    ovj2, r_sc2, r_cs2 = xa.compare(close_frame, snap_frame, cj, sj, jd)
    L += xa.overlap_table(ovj2)[:3]
    L += [f"- 15:19 상위 3 에만 있는 종목 ({len(r_sc2)}건)의 종가 기준 순위: {xa.rank_bins(r_sc2)}",
          f"- 종가 상위 3 에만 있는 종목 ({len(r_cs2)}건)의 15:19 기준 순위: {xa.rank_bins(r_cs2)}",
          f"\n## {dl[0].date()} ~ {dl[-1].date()} 전체 ({len(dl)}거래일, 시장 필터와 무관, 장부는 전체 기간 동안 이어짐)\n"]
    L += xa.overlap_table(ov)
    L += [f"\n- 15:19 상위 3 에만 있는 종목 ({len(r_sc)}건)의 종가 기준 순위: {xa.rank_bins(r_sc)}",
          f"- 종가 상위 3 에만 있는 종목 ({len(r_cs)}건)의 15:19 기준 순위: {xa.rank_bins(r_cs)}",
          f"- 대상 집합: 하루 평균 15:19 {ov.elig_snap.mean():.1f}, 종가 {ov.elig_close.mean():.1f} | 15:19 에만 대상 하루 평균 {ov.new_in_snap.mean():.1f} "
          f"(최대 {ov.new_in_snap.max()}), 종가에만 대상 하루 평균 {ov.dropped_in_snap.mean():.1f} (최대 {ov.dropped_in_snap.max()})",
          "\n| | 상위 3 신호 | 청산된 거래 | 평균 R | 평균 % 순수익률 |", "|---|---|---|---|---|",
          f"| 종가 기준 (전체 기간) | {xa.closed_stats(cp)} |", f"| 15:19 기준 (전체 기간) | {xa.closed_stats(spk)} |",
          f"\n({time.time() - t0:.0f}s)"]
    with open(os.path.join(OUT, "extend_judge.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    {"collect": collect, "judge": judge}[sys.argv[1]]()
