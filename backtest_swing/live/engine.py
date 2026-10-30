"""
점수·선택 엔진 (EXEC_SPEC 1-3, 2-3): 오늘 행을 붙여 동결 코드의 특징·대상 조건 함수로 계산 -> 그 달 모델 점수
-> 가상 신호 장부로 pick_nodup -> 상위 3 -> P4 주문 계획 (보내지 않음).

- 오늘 행: stage3_data.load_stock 원문에서 파일 읽는 네 줄만 메모리 원본(+ 오늘 행)으로 바꾼 사본으로 지표를 계산
  (exec_stage3_measure.make_load_x 와 같은 방식). 수정주가 = 원주가 x factor, 수정 거래량 = 원주가 거래량 x vscale.
- 특징: stage5_features.stock_features, 대상: stage4_features.eligible_mask & 신호 시작일 이후,
  같은 날 순위·시장 폭: 대상 종목만으로 float32 로 바꾼 뒤 (stage5_features.build 와 같은 계산),
  시장 특징: stage4_features.market_features 원문에서 지수 파일 읽는 두 줄만 메모리 지수(+ 오늘 값)로 바꾼 사본.
- 가상 신호 장부: 장부의 각 거래를 진입일부터 오늘 행까지 stage3_backtest.simulate (실제 규칙) 로 진행해
  오늘까지 안 끝났으면(구간끝) 그 종목은 건너뜀 (stage4_model.pick_nodup 의 "청산일 > 그날" 과 같은 판단).
- 계산은 종목을 나눈 작업 프로세스들이 장 전에 일봉을 메모리에 올려 두고 한다.
"""
import inspect
import multiprocessing as mp
import os
import sys
import threading
import time

# 11O: OpenBLAS(numpy·scipy 에 들어 있음)는 불러올 때 CPU 스레드 수만큼 버퍼를 잡아 프로세스마다 커밋 약 0.5~1GB (작업 프로세스 12개 = 수 GB).
# 계산은 BLAS 를 쓰지 않으므로 1 스레드로 (결과 같음, 2026-10-23 스냅샷 전 특징·점수 차이 0). 이 뒤에 띄우는 자식 프로세스(작업 프로세스·rerun)가 물려받는다
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CAPITAL, LIMIT_MULT, SLOTS, ceil_tick, sb, sd, s4, s5, sm  # noqa: E402

STOCK_ARR = ["open", "high", "low", "close", "ma20", "atr20", "volume", "raw_open", "raw_high", "raw_low", "raw_close", "raw_prev_close"]


# ---------------------------------------------------------------- 동결 함수의 메모리 사본

def make_load_mem(src_fn):
    """load_stock 원문 사본: 파일 읽는 네 줄 -> adj, raw = _SRC(code)"""
    src = inspect.getsource(sd.load_stock)
    a = ('    p = os.path.join(EXT_DIR, f"{code}.parquet")\n'
         '    adj = pd.read_parquet(p if os.path.exists(p) else os.path.join(MERGED_DIR, f"{code}.parquet"))\n'
         '    adj["timestamp"] = pd.to_datetime(adj["timestamp"])\n'
         '    raw = pd.read_parquet(os.path.join(RAW_DIR, f"{code}.parquet"))\n')
    assert src.count(a) == 1
    src = src.replace("def load_stock(code: str, until=None) -> pd.DataFrame:", "def load_stock_mem(code: str, until=None) -> pd.DataFrame:")
    src = src.replace(a, "    adj, raw = _SRC(code)\n")
    ns = dict(vars(sd))
    ns["_SRC"] = src_fn
    exec(src, ns)
    return ns["load_stock_mem"]


def make_market_mem(idx_fn):
    """market_features 원문 사본: 지수 파일 읽는 두 줄 -> k, q = _IDX()"""
    src = inspect.getsource(s4.market_features)
    a = ('    k = pd.read_parquet(sd.KOSPI).set_index("timestamp").close\n'
         '    q = pd.read_parquet(KOSDAQ).set_index("timestamp").close\n')
    assert src.count(a) == 1
    src = src.replace("def market_features(", "def market_features_mem(").replace(a, "    k, q = _IDX()\n")
    ns = dict(vars(s4))
    ns["_IDX"] = idx_fn
    exec(src, ns)
    return ns["market_features_mem"]


def today_frames(adj, raw, day, sv, f, vscale):
    """day 앞까지의 원본 행 + 오늘 행 (sv: 원주가 open/high/low/close/volume, amount = 원)"""
    a = adj[adj.timestamp < day]
    r = raw[raw.timestamp < day]
    if sv is None:
        return a.copy(), r.copy()
    ra = {"timestamp": day, "volume": sv["volume"] * vscale, "amount": sv["amount"]}
    rr = {"timestamp": day, "volume": sv["volume"], "factor": f}
    for c in ("open", "high", "low", "close"):
        ra[c] = sv[c] * f
        rr[c] = sv[c]
    a = pd.concat([a, pd.DataFrame([ra])], ignore_index=True)
    r = pd.concat([r, pd.DataFrame([rr])], ignore_index=True)
    return a, r


BASE_EVENT = 0.005  # 11Q A: 기준가 / 전날 원주가 종가 가 이만큼(0.5%) 넘게 다르면 그날 이벤트 (그 아래는 스팩류 기준가 미세 조정 = 키움도 계수 안 바꿈)


def default_factor(adj, raw, day, flags=None, base=None, prev_day=None):
    """오늘 행의 factor·vscale: 전날 행 값 (빈칸이면 1). flags 를 주면 기본값 1.0 으로 대체했는지 기록 (2026-10-24 B7).
    11Q A: base(그날 기준가, 15:18 스냅샷)가 있고 저장 이력 마지막 행이 prev_day(직전 거래일)이면, k = 기준가 / 그 행 원주가 종가 가
    BASE_EVENT 넘게 1 과 다를 때(분할·병합·권리락 등 그날 이벤트) 오늘 factor = 전날 factor / k, vscale = 전날 vscale x k
    (키움 수정주가 계수 변화 = 기준가 / 전날 종가, 2025-03 ~ 2026-10 643건 0.1% 안). 저장 이력이 이벤트 전 기준이면 오늘 행을 그 기준으로,
    이미 새 기준(이전 행 factor = k)이면 오늘 factor 1 이 된다. 평소(차이 0.5% 미만·기준가 없음)는 예전과 같은 값"""
    r = raw[raw.timestamp < day]
    a = adj[adj.timestamp < day]
    f_ok = bool(len(r) and pd.notna(r.factor.iat[-1]))
    f = float(r.factor.iat[-1]) if f_ok else 1.0
    rv = float(r.volume.iat[-1]) if len(r) else 0.0
    vs_ok = bool(len(a) and rv > 0 and a.timestamp.iat[-1] == r.timestamp.iat[-1])
    vs = float(a.volume.iat[-1]) / rv if vs_ok else 1.0
    if flags is not None:
        flags["factor_default"], flags["vscale_default"] = not f_ok, not vs_ok
    if base is not None and len(r):
        pc = float(r.close.iat[-1])
        b = float(base)
        if np.isfinite(b) and b > 0 and pc > 0 and abs(b / pc - 1) >= BASE_EVENT:
            if prev_day is not None and pd.Timestamp(r.timestamp.iat[-1]) != pd.Timestamp(prev_day):
                if flags is not None:  # 이력 마지막 행이 직전 거래일이 아니면 기준가와 비교할 전날 종가를 모름 -> 예전 값, 기록만
                    flags["base_stale"] = str(pd.Timestamp(r.timestamp.iat[-1]).date())
            else:
                k = b / pc
                # 전날 거래량 0(이벤트 앞 거래정지 등)이면 vscale 기본값 1 대신 1 / 전날 factor (키움은 이벤트마다 거래량도 1/계수로 조정 -> vscale = 1/factor)
                f, vs = f / k, (vs if vs_ok else 1.0 / f) * k
                if flags is not None:
                    flags["base_k"] = k
    return f, vs


# 동결 함수 원문 해시 (2026-10-24 결정 B7, 태그 forward-freeze-20261021 의 stage3_data.load_stock·stage4_features.market_features)
FROZEN_SOURCE_SHA256 = {"load_stock": "981e46306c5a9a6553361f460730407405560e907652f789827e2ee6252bcaae",
                        "market_features": "d9155e1f56826b78e8ac9f28d4364e4885ca815fdd383e747a983bee2632c071"}


def frozen_check(expected=None):
    """동결 load_stock·market_features 의 소스 해시가 기록과 같고 메모리용 사본 치환이 되는지. 반환 dict(ok, ...)"""
    import hashlib
    exp = expected or FROZEN_SOURCE_SHA256
    got = {"load_stock": hashlib.sha256(inspect.getsource(sd.load_stock).encode("utf-8")).hexdigest(),
           "market_features": hashlib.sha256(inspect.getsource(s4.market_features).encode("utf-8")).hexdigest()}
    out = {"hash": got, "hash_ok": got == exp}
    try:
        make_load_mem(lambda c: None)
        make_market_mem(lambda: None)
        out["substitution_ok"] = True
    except Exception as e:  # noqa: BLE001
        out["substitution_ok"], out["substitution_error"] = False, repr(e)[:200]
    out["ok"] = bool(out["hash_ok"] and out["substitution_ok"])
    return out


# ---------------------------------------------------------------- 작업 프로세스

def make_load_frozen(tmp):
    """동결 load_stock 을 그대로 부른다: 오늘 행을 붙인 원본을 임시 폴더 파일로 쓰고 stage3_data 의 폴더 상수만 그 폴더로 (장 마감 후 대조용)"""
    for sub in ("ext", "raw"):
        os.makedirs(os.path.join(tmp, sub), exist_ok=True)
    sd.EXT_DIR = sd.MERGED_DIR = os.path.join(tmp, "ext")
    sd.RAW_DIR = os.path.join(tmp, "raw")

    def load(code, until, frames):
        adj, raw = frames
        adj.to_parquet(os.path.join(sd.EXT_DIR, f"{code}.parquet"), index=False)
        raw.to_parquet(os.path.join(sd.RAW_DIR, f"{code}.parquet"), index=False)
        return sd.load_stock(code, until=until)
    return load


def _loop(conn, codes, use_append, frozen=False, vdir=None):
    if vdir:  # 11S: 데이터 버전 폴더 고정 (spawn 이라 부모의 고정이 넘어오지 않음, 실행 중 CURRENT.json 이 바뀌어도 그대로)
        import common
        common.data_dir = lambda append_dir=None: vdir
    from daily import Store
    store = Store(codes, use_append=use_append)
    cur = {}
    tmp = None
    if frozen:
        tmp = os.path.join(os.environ.get("TEMP", "."), "live_frozen", str(os.getpid()))
        lf = make_load_frozen(tmp)
        load_mem = lambda code, until: lf(code, until, cur[code])  # noqa: E731
    else:
        load_mem = make_load_mem(lambda code: cur[code])
    try:
        _serve(conn, codes, store, cur, load_mem)
    finally:
        if tmp is not None:  # 11O: 정상 종료(stop·부모 없어짐)면 임시 폴더 지움 (강제 종료로 남은 것은 다음 동결 묶음이 sweep_frozen_tmp 로)
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


def _serve(conn, codes, store, cur, load_mem):
    conn.send(("ready", len(store.adj), store.excluded))
    while True:
        try:
            cmd, payload = conn.recv()
        except EOFError:  # 부모가 없어짐
            break
        if cmd == "stop":
            break
        if cmd == "amt20":  # Codex 27: 대상 후보 판정용 20일 평균 거래대금 (day 앞 20행)
            day = payload
            conn.send(("amt20", {c: float(a[a.timestamp < day].amount.tail(20).mean()) for c, a in store.adj.items()}))
            continue
        day, rows, ledger_from = payload
        out, arrs, errs = [], {}, []
        for code in codes:
            if code not in store.adj:
                continue
            row = rows.get(code)
            need_arr = code in ledger_from
            if row is None and not need_arr:
                continue
            adj, raw = store.adj[code], store.raw[code]
            try:
                if row is not None:
                    sv, f, vs = row
                    flags = {}
                    if f is None:  # 11Q A: 스냅샷 기준가(sv base)로 그날 이벤트 계수
                        f, vs = default_factor(adj, raw, day, flags, base=sv.get("base"), prev_day=sv.get("prev_day"))
                else:
                    sv, f, vs = None, None, None
                cur[code] = today_frames(adj, raw, day, sv, f, vs)
                cut = load_mem(code, until=day)
                if need_arr:
                    t = cut[cut.timestamp >= ledger_from[code]]
                    arrs[code] = {"timestamp": t.timestamp.to_numpy(), **{c: t[c].to_numpy(np.float64) for c in STOCK_ARR}}
                if row is None or len(cut) == 0 or cut.timestamp.iat[-1] != day:
                    continue
                feats = s5.stock_features(cut).iloc[-1]
                el = bool((s4.eligible_mask(cut) & (cut.timestamp >= s4.SIG_START)).iat[-1])
                out.append({"code": code, "eligible": el, "above_ma20": float(cut.close.iat[-1] > cut.ma20.iat[-1]),
                            "close_adj": float(cut.close.iat[-1]), "raw_close": float(cut.raw_close.iat[-1]),
                            "factor": float(cut.factor.iat[-1]), "factor_default": bool(flags.get("factor_default")) if row is not None else False,
                            "vscale_default": bool(flags.get("vscale_default")) if row is not None else False,
                            "base_k": flags.get("base_k") if row is not None else None, "base_stale": flags.get("base_stale") if row is not None else None,
                            **{c: float(feats[c]) for c in feats.index}})
            except Exception as e:  # noqa: BLE001
                errs.append((code, repr(e)[:200]))
            finally:
                cur.pop(code, None)
        conn.send(("done", out, arrs, errs))


class PoolError(Exception):
    """작업 프로세스가 죽었거나 제한 시간 안에 답하지 않음 (Codex 22). 그날 신규 매수만 막고 메인 루프는 계속"""


POOL_WORKERS_DEFAULT = 6  # 11O: config pool_workers 가 없을 때 (7500F 물리 코어 수)


def pool_workers(cfg=None):
    """11O: 작업 프로세스 수 = config pool_workers (없으면 6), 1 ~ CPU 스레드 수로 자름. 상주 적재·장후 rerun·features_eod 가 같은 값"""
    if cfg is None:
        import settings
        cfg = settings.load()
    try:
        n = int(cfg.get("pool_workers") or POOL_WORKERS_DEFAULT)
    except (TypeError, ValueError):
        n = POOL_WORKERS_DEFAULT
    return max(1, min(n, os.cpu_count() or n))


def sweep_frozen_tmp(root=None, max_age_s=6 * 3600):
    """11O: 동결 대조 작업 프로세스의 임시 폴더(%TEMP%/live_frozen/{pid}) 중 오래된 것 지움 (예전에는 하루 n개씩 남았음). 반환: 지운 수"""
    import shutil
    root = root or os.path.join(os.environ.get("TEMP", "."), "live_frozen")
    n = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        p = os.path.join(root, name)
        if not name.isdigit() or not os.path.isdir(p):
            continue
        try:
            if time.time() - os.path.getmtime(p) > max_age_s:
                shutil.rmtree(p, ignore_errors=True)
                n += 1
        except OSError:
            pass
    return n


class ShardPool:
    """종목을 n 묶음으로 나눈 작업 프로세스 (장 전에 일봉을 메모리에 올림).
    Codex 22: 부모의 쓰지 않는 파이프 끝(자식 쪽)은 시작 직후 닫고, 응답은 poll(timeout) + 자식 생존 확인으로 받는다.
    11O: n 을 안 주면 pool_workers() (config pool_workers). 11S: vdir 를 주면 작업 프로세스가 그 데이터 버전 폴더만 읽음"""

    def __init__(self, codes, n=None, use_append=True, frozen=False, start_timeout=600, call_timeout=180, vdir=None):
        ctx = mp.get_context("spawn")
        n = pool_workers() if n is None else n
        self.n = n
        self.lock = threading.RLock()  # 11I: 장 마감 후 계산 스레드와 주 반복이 같이 써도 요청·응답 한 쌍씩 (파이프가 섞이지 않게)
        if frozen:
            sweep_frozen_tmp()
        self.shards = [codes[i::n] for i in range(n)]
        self.conns, self.procs = [], []
        self.call_timeout = call_timeout
        t0 = time.time()
        try:
            for sh in self.shards:
                a, b = ctx.Pipe()
                p = ctx.Process(target=_loop, args=(b, sh, use_append, frozen) + ((vdir,) if vdir else ()), daemon=True)
                p.start()
                b.close()  # 부모는 자식 쪽 끝을 쓰지 않는다 (자식이 죽으면 recv 가 EOFError 로 끝나게)
                self.conns.append(a)
                self.procs.append(p)
            msgs = [self._recv(i, start_timeout) for i in range(len(self.conns))]
        except Exception:
            self.close()
            raise
        self.loaded = sum(x[1] for x in msgs)
        self.excluded = sorted(set().union(*[set(x[2]) for x in msgs]))
        self.load_seconds = time.time() - t0

    def _recv(self, i, timeout=None):
        c, p = self.conns[i], self.procs[i]
        deadline = time.time() + (timeout or self.call_timeout)
        while True:
            try:
                ready = c.poll(1.0)
                if ready:
                    return c.recv()
            except (EOFError, BrokenPipeError, OSError) as e:  # Windows: 상대 끝이 닫히면 poll 도 BrokenPipeError
                raise PoolError(f"작업 프로세스 {i} 연결 끊김 (pid {p.pid}, 종료 코드 {p.exitcode}): {e!r}") from e
            if not p.is_alive():
                raise PoolError(f"작업 프로세스 {i} 죽음 (pid {p.pid}, 종료 코드 {p.exitcode})")
            if time.time() > deadline:
                raise PoolError(f"작업 프로세스 {i} 응답 없음 ({timeout or self.call_timeout}초)")

    def _send(self, i, msg):
        if not self.procs[i].is_alive():
            raise PoolError(f"작업 프로세스 {i} 죽음 (pid {self.procs[i].pid}, 종료 코드 {self.procs[i].exitcode})")
        try:
            self.conns[i].send(msg)
        except (BrokenPipeError, EOFError, OSError) as e:
            raise PoolError(f"작업 프로세스 {i} 보내기 실패: {e!r}") from e

    def compute(self, day, rows, ledger_from):
        with self.lock:
            return self._compute(day, rows, ledger_from)

    def _compute(self, day, rows, ledger_from):
        for i, sh in enumerate(self.shards):
            s = set(sh)
            self._send(i, ("compute", (day, {k: v for k, v in rows.items() if k in s}, {k: v for k, v in ledger_from.items() if k in s})))
        out, arrs, errs = [], {}, []
        for i in range(len(self.conns)):
            _, o, a, e = self._recv(i)
            out += o
            arrs.update(a)
            errs += e
        return out, arrs, errs

    def amt20(self, day):
        """종목별 day 앞 20거래일 평균 거래대금 (원)"""
        return self._ask("amt20", day)

    def _ask(self, cmd, day):
        with self.lock:
            return self._ask_locked(cmd, day)

    def _ask_locked(self, cmd, day):
        for i in range(len(self.conns)):
            self._send(i, (cmd, pd.Timestamp(day)))
        out = {}
        for i in range(len(self.conns)):
            out.update(self._recv(i)[1])
        return out

    def close(self):
        with self.lock:
            self._close()

    def shutdown(self, wait=2.0):
        """버리는 묶음 정리 (11I 보완): 파이프를 닫고 terminate -> 짧은 대기(wait 초) -> 아직 살아 있으면 kill.
        잠금을 잡지 않는다 (그 묶음으로 계산 중이던 쪽은 PoolError 로 끝남). 상주 프로그램은 별도 스레드에서 부른다 (주 반복은 기다리지 않음)"""
        for c in self.conns:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        for p in self.procs:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
        end = time.time() + wait
        for p in self.procs:
            p.join(max(0.0, end - time.time()))
        left = [p for p in self.procs if p.is_alive()]
        for p in left:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        for p in left:
            p.join(1.0)
        return {"killed": len(left), "alive": sum(p.is_alive() for p in self.procs)}

    def _close(self):
        for c in self.conns:
            try:
                c.send(("stop", None))
            except Exception:  # noqa: BLE001
                pass
        for p in self.procs:
            p.join(timeout=10)
            if p.is_alive():
                p.terminate()


# ---------------------------------------------------------------- 시장

def market_frozen(k, q):
    """동결 market_features 를 그대로 부른다: 지수를 임시 파일로 쓰고 경로 상수만 바꿈 (장 마감 후 대조용)"""
    tmp = os.path.join(os.environ.get("TEMP", "."), "live_frozen", "index")
    os.makedirs(tmp, exist_ok=True)
    saved = sd.KOSPI, s4.KOSDAQ
    try:
        sd.KOSPI, s4.KOSDAQ = os.path.join(tmp, "kospi.parquet"), os.path.join(tmp, "kosdaq.parquet")
        for s, p in ((k, sd.KOSPI), (q, s4.KOSDAQ)):
            s.rename("close").rename_axis("timestamp").reset_index().to_parquet(p, index=False)
        return s4.market_features(None)
    finally:
        sd.KOSPI, s4.KOSDAQ = saved


def market_row(index, day, values, frozen=False):
    """index: {"kospi": 종가 Series, "kosdaq": ...} (day 전날까지), values: 오늘 지수 값 -> 시장 특징 7개 + 필터"""
    k = index["kospi"][index["kospi"].index < day].copy()
    q = index["kosdaq"][index["kosdaq"].index < day].copy()
    ma = k.rolling(120).mean()
    prev_close, prev_ma = float(k.iat[-1]), float(ma.iat[-1])
    tail = k.tail(120).to_numpy(np.float64)
    n_ok = int((np.isfinite(tail) & (tail > 0)).sum()) if len(k) else 0  # 결측·0 이하가 없는 종가 수
    k[day], q[day] = values["kospi"], values["kosdaq"]
    mf = market_frozen(k, q) if frozen else make_market_mem(lambda: (k, q))(None)
    row = mf.loc[day]
    bad = filter_invalid(prev_close, prev_ma, n_ok)
    filt = {"prev_date": str(k.index[-2].date()), "prev_kospi_close": prev_close, "prev_kospi_ma120": prev_ma,
            # 11K 보완 2: 입력이 결측·0·비정상이면 통과 아님 (예전 not (NaN < NaN) = True 로 통과되던 경로 막음)
            "filter_ok": bool(bad is None and prev_close >= prev_ma), "filter_valid": bad is None, "filter_invalid": bad}
    return row, filt


def filter_invalid(prev_close, prev_ma, n_ok=120):
    """시장 필터 입력 확인 (11K 보완 2). 정상이면 None, 아니면 이유. 정상 = 전날 코스피 종가·120일선이 유한한 양수, 최근 120일 종가가 모두 있음,
    종가 / 120일선 이 0.5 ~ 2.0 (지수 단위 오류·잘못된 행 방지)"""
    try:
        c, m = float(prev_close), float(prev_ma)
    except (TypeError, ValueError):
        return f"숫자 아님 (종가 {prev_close!r}, 120일선 {prev_ma!r})"
    if not (np.isfinite(c) and np.isfinite(m)):
        return f"결측 (종가 {c}, 120일선 {m})"
    if c <= 0 or m <= 0:
        return f"0 이하 (종가 {c}, 120일선 {m})"
    if n_ok < 120:
        return f"최근 120일 종가 중 결측·0 이하 {120 - n_ok}개"
    if not (0.5 <= c / m <= 2.0):
        return f"비정상 비율 종가/120일선 {c / m:.3f}"
    return None


# ---------------------------------------------------------------- 가상 신호 장부

class _G:
    pass


def ledger_status(ledger, arrs, day):
    """장부 거래별로 진입일부터 day 까지 실제 규칙 진행. 반환: busy 종목 집합, 거래별 상태"""
    segs, unknown = [], []
    for tr in ledger:
        a = arrs.get(tr["code"])
        ts = pd.DatetimeIndex(a["timestamp"]) if a is not None else pd.DatetimeIndex([])
        i0 = np.flatnonzero(ts == pd.Timestamp(tr["entry"]))
        if a is None or len(i0) == 0:
            unknown.append(tr)  # 일봉이 없는 종목 (제외 종목 등): 열린 거래로 둔다
            continue
        sl = slice(int(i0[0]), int(np.flatnonzero(ts <= day)[-1]) + 1)
        segs.append((tr, {c: a[c][sl] for c in STOCK_ARR}, ts[sl]))
    status = [{"code": tr["code"], "entry": tr["entry"], "open": True, "exit_date": None, "reason": "일봉 없음 (열린 거래로 둠)"} for tr in unknown]
    busy = {tr["code"] for tr in unknown}
    if not segs:
        return busy, status
    g = _G()
    names = {"open": "o", "high": "h", "low": "l", "close": "c", "ma20": "ma20", "atr20": "atr20", "volume": "vol",
             "raw_open": "ro", "raw_high": "rh", "raw_low": "rl", "raw_close": "rc", "raw_prev_close": "rpc"}
    for c, n in names.items():
        setattr(g, n, np.concatenate([s[1][c] for s in segs]))
    tss = pd.DatetimeIndex(np.concatenate([s[2].to_numpy() for s in segs]))
    lens = [len(s[2]) for s in segs]
    starts = np.cumsum([0] + lens[:-1])
    g.dev_last = np.concatenate([np.full(n, st + n - 1) for st, n in zip(starts, lens)])
    g.tax = np.array([sb.SELL_TAX.get(int(y), 0.0025) for y in tss.year], dtype=float)
    T = starts.astype(int)
    risk = s4.STOP_ATR * g.atr20[T]
    sim = sb.simulate(g, T, g.c[T] - risk, np.full(len(T), np.nan), "full", want_legs=True)
    for (tr, _, ts), st, rs, j1 in zip(segs, starts, sim["reason"], sim["j1"]):
        open_ = int(rs) == sb.R_END
        if open_:
            busy.add(tr["code"])
        status.append({"code": tr["code"], "entry": tr["entry"], "open": open_,
                       "exit_date": None if open_ else str(tss[j1].date()), "reason": sb.REASON.get(int(rs), str(rs))})
    return busy, status


# ---------------------------------------------------------------- 하루 계산

def pick_top(e, ledger_status_list, day):
    """stage4_model.pick_nodup 을 그대로 부른다: 장부 거래를 진입일별 행(청산일 = 끝난 날, 안 끝났으면 무한대)으로 앞에 두고
    오늘 대상 행을 점수 순으로 뒤에 둔다. 오늘 행 중 고른 것만 반환 (e 의 위치)."""
    big = 10 ** 9
    led = pd.DataFrame([{"code": s["code"], "cal": pd.Timestamp(s["entry"]).toordinal(),
                         "exit_cal": big if s["open"] else pd.Timestamp(s["exit_date"]).toordinal()} for s in ledger_status_list],
                       columns=["code", "cal", "exit_cal"])
    led = led.sort_values("cal", kind="stable").reset_index(drop=True)
    today = pd.DataFrame({"code": e.code.to_numpy(), "cal": day.toordinal(), "exit_cal": day.toordinal()})
    frame = pd.concat([led, today], ignore_index=True)
    n0 = len(led)
    ordered = [np.flatnonzero(led.cal.to_numpy() == c) for c in np.unique(led.cal.to_numpy())]
    order = np.argsort(-e.score.to_numpy(), kind="stable")
    if len(order):
        ordered.append(n0 + order)
    picks = sm.pick_nodup(frame, ordered, sm.TOP_N)
    led_picked = picks[picks < n0]
    return [int(i - n0) for i in picks if i >= n0], len(led_picked) == n0


def run_day(pool, day, rows, index, index_values, boosters, cols, ledger, frozen_market=False):
    """rows: code -> (sv, f, vscale). ledger: 거래 목록 하나 또는 {장부 이름: 거래 목록}.
    반환 dict (장부별 상위 3·장부 상태, 대상 표, 시간). 장부가 하나면 top3 등은 그 장부 것."""
    t0 = time.time()
    day = pd.Timestamp(day)
    ledgers = ledger if isinstance(ledger, dict) else {"main": ledger}
    ledger_from = {}
    for trs in ledgers.values():
        for tr in trs:
            e = pd.Timestamp(tr["entry"])
            ledger_from[tr["code"]] = min(e, ledger_from.get(tr["code"], e))
    out, arrs, errs = pool.compute(day, rows, ledger_from)
    t1 = time.time()
    fr = pd.DataFrame(out)
    e = fr[fr.eligible].copy().sort_values("code").reset_index(drop=True) if len(fr) else fr
    feat_stock = [c for c in cols if c not in s5.XS_FEATS and not c.startswith(("kospi_", "kosdaq_"))]
    for c in feat_stock:
        e[c] = e[c].astype(np.float32)
    e["rank_ret20"] = e.ret_20.rank(pct=True).astype(np.float32)
    e["rank_ret60"] = e.ret_60.rank(pct=True).astype(np.float32)
    e["rank_amt20"] = e.log_amt20.rank(pct=True).astype(np.float32)
    e["breadth_ma20"] = np.float32(e.above_ma20.mean())
    mrow, filt = market_row(index, day, index_values, frozen=frozen_market)
    for c in mrow.index:
        e[c] = np.float32(mrow[c])
    X = e[cols].to_numpy(np.float32)
    e["score"] = np.mean([b.predict(X) for b in boosters], axis=0) if len(e) else []
    ranked = list(e.code.to_numpy()[np.argsort(-e.score.to_numpy(), kind="stable")])
    per = {}
    for name, trs in ledgers.items():
        busy, status = ledger_status(trs, arrs, day)
        picks, ledger_ok = pick_top(e, status, day)
        top = e.iloc[picks]
        last = ranked.index(top.code.iat[-1]) if len(top) else -1
        per[name] = {"top3": top, "busy": sorted(busy), "skipped_busy": [c for c in ranked[:last + 1] if c in busy],
                     "ledger_status": status, "ledger_reselected_all": ledger_ok}
    first = per[next(iter(per))]
    fd = int(fr.factor_default.sum()) if "factor_default" in fr else 0
    vd = int(fr.vscale_default.sum()) if "vscale_default" in fr else 0
    # 11Q A: 기준가로 계수를 바꾼 종목 {code: k}·이력이 직전 거래일까지 없어 못 비교한 종목
    bev = {r.code: round(float(r.base_k), 6) for r in fr.itertuples() if "base_k" in fr and pd.notna(r.base_k)} if len(fr) else {}
    bst = {r.code: r.base_stale for r in fr.itertuples() if "base_stale" in fr and isinstance(r.base_stale, str)} if len(fr) else {}
    return {"day": day, "frame": fr, "eligible": e, "ledgers": per, **first, "factor_default_n": fd, "vscale_default_n": vd,
            "base_events": bev, "base_stale": bst,
            "filter": filt, "market": {k: float(v) for k, v in mrow.items()}, "index_values": index_values,
            "errors": errs, "n_rows": len(rows), "seconds_compute": round(t1 - t0, 2), "seconds_total": round(time.time() - t0, 2)}


def order_plan(top, price_col="price_1519", equity=CAPITAL, held=()):
    """P4 주문 계획 (보내지 않음): 종목당 = 총자산/33, 수량 = 내림(금액 / (가격 x 1.001 x 1.00015)), 지정가 = 가격 x 1.05 호가 올림"""
    amt = equity / SLOTS
    cash = equity
    plan = []
    for r in top.itertuples():
        p = float(getattr(r, price_col))
        qty = int(np.floor(amt / (p * (1 + sb.SLIPPAGE) * (1 + sb.COMMISSION))))
        limit = ceil_tick(p * LIMIT_MULT)
        why = "보유 중" if r.code in held else ("현금 부족" if cash < amt else ("수량 0" if qty == 0 else ""))
        plan.append({"code": r.code, "score": float(r.score), "price_1519": p, "amount_target": round(amt), "qty": qty if not why else 0,
                     "limit_price": limit, "order_value": limit * qty if not why else 0, "skip": why})
        if not why:
            cash -= limit * qty
    return plan
