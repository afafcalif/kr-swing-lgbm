"""
주문 모듈 (EXEC_SPEC 1-4, 1A, 2장, 3B, 5장; 2026-10-22 결정, 2026-10-24 결정 (코드 리뷰) 반영). 모의투자(KRX 만)부터.

- 매수: 장 마감 동시호가 지정가 trde_tp "0", 가격 = 15:19 가격 x 1.05 를 호가 단위로 올림. 15:21 ~ 15:25 에 보내고 15:28 이후 새 주문 없음.
- 랜덤엔드: 마감 동시호가는 15:30 이후 최대 30초 안에 체결될 수 있어 15:30 에는 취소하지 않는다. 15:35 에 체결·미체결 조회로
  체결 수량·체결가·체결 시각을 확인하고, 미체결 수량은 "미체결 건너뜀"으로 기록한 뒤 그때 남아 있으면 취소한다.
- 매도: 손절 = 1분 시세 조회 현재가 < 손절가(백테스트 저가 < 손절가 와 같게 미만)면 "손절 진행 중"으로 고정, 팔릴 때까지 매 분 KRX 시장가(trde_tp "3")
  (15:20 까지 감지, 15:29 까지 보냄 = 장 마감 동시호가 시장가는 종가 체결, 11J). 추세 매도 = 다음 날 08:30 ~ 08:59 시장가, 못 팔면 09:00 이후 장중 시장가(시가 매도 아님).
  하한가 잠김 등으로 남은 매도는 15:35 확인 후 취소하고 다음 날 시가 재주문 목록에 넣는다.
- 매도 의무는 종목 코드 단위 (11J 보완 3, positions.sell_duty): 증권사 잔고가 정답. 끝 = 성공한 계좌 조회(모든 쪽)에서 그 종목 보유 0 이 1분 이상 간격으로 두 번 연속일 때만 (R1).
  보낼 수량 = 같은 분 계좌의 매도 가능 수량(증권사가 걸린 매도 주문을 이미 뺀 값, 0 이면 그 분은 안 보냄). 계좌 조회 실패면 그 종목에
  오늘 접수·UNKNOWN·SENDING 매도가 없을 때만 보유 파일 수량 (초과분은 증권사 거부). 체결 수량으로 남은 수량을 계산하지 않는다 (OrderManager.duty_sell).
- 주문 의도 선기록 (2026-10-24, O-01·O-04·R2-01·R2-03·R2-04): 모든 주문은 보내기 직전에 의도(intent_id, 날짜, 단계, 종목, 매수/매도,
  수량, 가격, 시도 번호, 상태 SENDING)를 디스크에 원자적으로 저장하고, 응답을 받으면 바로 상태(ACCEPTED / REJECTED / UNKNOWN / NOT_SENT)와
  주문번호를 저장한다. 매수는 같은 날·같은 종목에 SENDING·UNKNOWN·ACCEPTED 의도가 있으면 다시 보내지 않는다 (매도는 위 계좌 수량 규칙).
  재시작 때 SENDING 은 UNKNOWN 으로 바꾸고 미체결(ka10075)·체결(ka10076)·잔고(kt00018)와 종목·수량으로 대조한다.
- 예외 분류 (O-03·O-18): 요청을 보내기 전 오류(인증·설정 오류)만 NOT_SENT. 서버의 명시적 거부(return_code != 0)만 REJECTED.
  시간 초과·연결 오류·5xx·JSON 파싱 오류·분류 안 된 예외는 UNKNOWN (재주문 안 함). 429 재시도 끝 실패는 REJECTED, 원인 "429".
- 응답 없음 확인 (O-02): UNKNOWN 은 5초·30초·2분 뒤 다시 조회하고 15:35 정산 때 종목·수량으로도 맞춘다. 조회에 안 보인다고 바로
  NOT_SENT 로 확정하지 않는다. 2분 뒤에도 확정 못 하면 그 종목은 그날 건드리지 않고 알림. 15:35 정산에도 없으면 그때 NOT_SENT.
- 재주문: 매수·08:30 추세 매도는 REJECTED 면 1초 뒤 1회만. 손절·늦은 추세 매도는 REJECTED 면 다음 분 감시에서 다시 (상한 없음, 5회 뒤 간격 늘림, 11J).
- 실행 점검 모드(3B): 필터 미통과일은 전체 장부 상위 3 을 "점검용", 통과일은 전략 장부 상위 3 을 "본래".
- 주문 TR 은 Broker(allow_orders=True) 일 때만 보낸다 (기본 False).
"""
import contextlib
import json
import os
import threading
import time

import numpy as np
import pandas as pd

try:
    import requests
except Exception:  # noqa: BLE001
    requests = None

ORDR, ACNT = "/api/dostk/ordr", "/api/dostk/acnt"
ORDER_TRS = {"kt10000", "kt10001", "kt10003"}
READ_TRS = {"kt00018", "kt00001", "ka10075", "ka10076"}
FILL_TIME_KEYS = ("cntr_tm", "tm", "ord_tm", "cnfm_tm")  # 체결 시각 필드 (모의 첫 주문일에 실제 이름 확인, EXEC_SPEC 9장)
SENDING, ACCEPTED, REJECTED, UNKNOWN, NOT_SENT = "SENDING", "ACCEPTED", "REJECTED", "UNKNOWN", "NOT_SENT"
BLOCKING = {SENDING, UNKNOWN, ACCEPTED}
CHECK_DELAYS = (5, 30, 120)  # 응답 없음 다시 조회 (초)
MAX_ATTEMPTS = {"buy": 2, "trend_sell": 2, "stop": None, "trend_late": None}  # None = 상한 없음 (거부가 계속되면 간격만 늘림, 11J)
RETRY_FREE = 5        # 상한 없는 단계: 거부 5회까지는 매 분, 그 뒤 2·4·8분 ... 최대 10분 간격 (장 끝까지)
RETRY_MAX_GAP = 10
RATE_RETRIES = 3      # 11L: 유량 초과면 한 번의 보내기 안에서 간격(order_interval, 최소 1초)을 두고 다시 보내는 횟수 (거부 횟수와 따로)


class NoResponse(Exception):
    """조회 TR 응답 없음 (시간 초과·연결 오류·HTTP 5xx)"""


class OrdersDisabled(Exception):
    pass


def num(x):
    s = str(x if x is not None else "").replace(",", "").replace("+", "").strip()
    try:
        return abs(float(s))
    except ValueError:
        return float("nan")


def code_of(x):
    s = str(x or "").strip()
    return s[1:] if s[:1] == "A" and len(s) == 7 else s.replace("_AL", "").replace("_NX", "")


AUTH_WORDS = ("토큰", "token", "인증")  # 11J 보완 3 N2: 인증 실패·토큰 만료 거부 메시지


def auth_msg(m):
    m = str(m or "").lower()
    return any(w in m for w in AUTH_WORDS)


def rate_msg(rc, m):
    """11L: 키움 유량 초과 응답 (return_code 5, "[1700:허용된 요청 개수를 초과하였습니다 ...]")"""
    m = str(m or "")
    return rc in (5, "5") or "1700" in m or "허용된 요청 개수" in m


def classify(call):
    """주문 TR 호출 결과 분류 -> dict(outcome, return_code, msg, ord_no, cause).
    11J 보완 3 N2: 인증 실패·토큰 만료(토큰 발급 실패로 안 보냄, HTTP 401, 인증·토큰 거부 메시지)는 서버가 주문을 처리하지 않은 것 ->
    NOT_SENT(not_sent_kind auth, 다시 보내도 됨, 거부 횟수에 안 셈)"""
    try:
        from kiwoom.errors import AuthError, ConfigError, HTTPError, NotSent
    except Exception:  # noqa: BLE001
        AuthError = ConfigError = HTTPError = NotSent = ()  # noqa: N806
    try:
        r = call()
    except (OrdersDisabled,) as e:
        return {"outcome": NOT_SENT, "cause": f"주문 끔: {e}"}
    except NotSent as e:  # 2차 확인 리뷰 4: 유량 대기 상한 초과·전송 직전 마감 시각 확인 실패 = 보내지 않음
        return {"outcome": NOT_SENT, "cause": str(e)[:200], "not_sent_kind": "rate" if "유량" in str(e) else "deadline"}
    except Exception as e:  # noqa: BLE001
        if AuthError and isinstance(e, AuthError):
            return {"outcome": NOT_SENT, "cause": f"인증·토큰 (보내기 전): {e!r}"[:200], "not_sent_kind": "auth"}
        if AuthError and isinstance(e, ConfigError):
            return {"outcome": NOT_SENT, "cause": f"보내기 전 오류: {e!r}"[:200]}
        status = getattr(e, "status_code", None)
        if HTTPError and isinstance(e, HTTPError) and status == 401:
            return {"outcome": NOT_SENT, "cause": f"HTTP 401 인증 거부: {getattr(e, 'response_text', '')}"[:200], "not_sent_kind": "auth"}
        if HTTPError and isinstance(e, HTTPError) and isinstance(status, int) and 400 <= status < 500:
            try:
                j = json.loads(getattr(e, "response_text", "") or "{}")
            except Exception:  # noqa: BLE001
                j = {}
            rc = j.get("return_code")
            if rc not in (None, 0, "0") and rate_msg(rc, j.get("return_msg")):
                return {"outcome": NOT_SENT, "return_code": rc, "msg": str(j.get("return_msg", ""))[:200], "cause": f"HTTP {status} 유량 초과",
                        "not_sent_kind": "rate"}
            if rc not in (None, 0, "0") and auth_msg(j.get("return_msg")):
                return {"outcome": NOT_SENT, "return_code": rc, "msg": str(j.get("return_msg", ""))[:200], "cause": f"HTTP {status} 인증·토큰 거부",
                        "not_sent_kind": "auth"}
            if rc not in (None, 0, "0"):
                return {"outcome": REJECTED, "return_code": rc, "msg": str(j.get("return_msg", ""))[:200], "cause": f"HTTP {status}"}
        return {"outcome": UNKNOWN, "cause": f"{type(e).__name__}: {e}"[:200]}
    if r is None:  # HTTP 429 (요청이 받아들여지지 않음, 주문 TR 은 클라이언트 재시도 없이 바로)
        # 11L: 유량 초과는 서버가 처리하지 않은 것 -> 거부(REJECTED)가 아니라 보내지 않음(NOT_SENT rate): 거부 횟수에 안 세고 간격을 두고 다시
        return {"outcome": NOT_SENT, "return_code": -429, "msg": "HTTP 429 유량 초과", "cause": "HTTP 429 유량 초과", "not_sent_kind": "rate"}
    try:
        j = r.json()
    except Exception as e:  # noqa: BLE001
        return {"outcome": UNKNOWN, "cause": f"JSON 파싱 오류: {e!r}"[:200]}
    if not isinstance(j, dict):  # Codex 9: 응답이 dict 가 아니면 접수 여부 모름
        return {"outcome": UNKNOWN, "cause": f"응답 형식 오류 (dict 아님): {type(j).__name__}"}
    rc = j.get("return_code")
    if rc in (0, "0"):
        return {"outcome": ACCEPTED, "return_code": 0, "msg": str(j.get("return_msg", ""))[:200], "ord_no": str(j.get("ord_no") or "") or None}
    if rc is None:
        return {"outcome": UNKNOWN, "cause": f"return_code 없음: {str(j)[:150]}"}
    if auth_msg(j.get("return_msg")):
        return {"outcome": NOT_SENT, "return_code": rc, "msg": str(j.get("return_msg", ""))[:200], "cause": "인증·토큰 거부", "not_sent_kind": "auth"}
    if rate_msg(rc, j.get("return_msg")):  # 11L: return_code 5 유량 초과 = 처리 안 됨
        return {"outcome": NOT_SENT, "return_code": rc, "msg": str(j.get("return_msg", ""))[:200], "cause": "유량 초과 (return_code 5)", "not_sent_kind": "rate"}
    return {"outcome": REJECTED, "return_code": rc, "msg": str(j.get("return_msg", ""))[:200], "cause": "return_code"}


class QueryError(NoResponse):
    """조회 응답 검증 실패 (return_code != 0, 필수 필드 없음, 목록 형식·금액 이상). 빈 계좌·빈 목록으로 바꾸지 않고 조회 실패로 처리 (Codex 2)"""


READ_SPEC = {  # tr: (목록 필드, 필수 최상위 필드, 유한한 금액이어야 하는 필드)
    "ka10075": ("oso", (), ()),
    "ka10076": ("cntr", (), ()),
    "kt00018": ("acnt_evlt_remn_indv_tot", ("prsm_dpst_aset_amt",), ()),
    "kt00001": (None, ("entr", "ord_alow_amt", "d2_entra"), ("entr", "ord_alow_amt", "d2_entra")),
}
MAX_PAGES = 50


def validate_read(tr, j):
    """조회 응답 공통 검증 (Codex 2): dict, return_code == 0, 필수 최상위 필드, 목록 형식, 금액 유한성"""
    if not isinstance(j, dict):
        raise QueryError(f"{tr} 응답이 dict 아님 ({type(j).__name__})")
    rc = j.get("return_code")
    if rc not in (0, "0"):
        raise QueryError(f"{tr} return_code {rc!r}: {str(j.get('return_msg', ''))[:120]}")
    lk, req, money = READ_SPEC[tr]
    miss = [k for k in req if k not in j]
    if lk is not None and lk not in j:
        miss.append(lk)
    if miss:
        raise QueryError(f"{tr} 필수 필드 없음 {miss}")
    if lk is not None and not isinstance(j[lk], list):
        raise QueryError(f"{tr} {lk} 가 목록이 아님 ({type(j[lk]).__name__})")
    for k in money:
        v = num(j.get(k))
        if not np.isfinite(v):
            raise QueryError(f"{tr} {k} 금액 이상: {j.get(k)!r}")
    return j


class Broker:
    """키움 주문·계좌 TR. 주문은 classify() 결과 dict, 조회 실패는 NoResponse/QueryError"""

    ORDER_MAX_WAIT = 2.0  # 주문 TR 유량 대기 상한 (초). 넘으면 기다리지 않고 NOT_SENT (2차 확인 리뷰 4)
    READ_MAX_WAIT = 5.0   # 11L: 조회 TR 의 공용 제한기 대기 상한 (초). 넘으면 조회 실패 (주 반복을 오래 붙잡지 않음)

    def __init__(self, client, allow_orders=False, env=None):
        """env: 이 클라이언트 토큰의 서버 (mock/prod) = 공용 호출 제한기(common.RateGate)의 키. 없으면 클라이언트 설정에서"""
        self.client, self.allow_orders = client, allow_orders
        self.env = env or getattr(getattr(client, "settings", None), "env", None) or "mock"
        # 11L 보완 1: 키움 클라이언트면 공용 제한기를 클라이언트의 전송 바로 앞 대기로 (자기 제한기 대신, 대기 장치 하나).
        # send_gate 가 없는 클라이언트(가짜 서버)는 여기서 부르기 전에 기다림 (_gate_here)
        self._gate_here = not hasattr(client, "send_gate")
        if not self._gate_here:
            from common import RateGate
            client.send_gate = RateGate.client_gate(self.env, getattr(client, "rate_limiter", None))
        self.guard = None  # 주문 전송 직전 확인 함수 (OrderManager 가 매 전송마다 시간창 확인으로 넣는다)
        self._tl = threading.local()

    def gap(self, tr):
        """11L: 이 TR 의 공용 제한기 최소 간격 (초)"""
        from common import RateGate
        return RateGate.interval(self.env, tr)

    @contextlib.contextmanager
    def http_timeout(self, sec):
        """이 스레드의 HTTP 요청 하나의 시간 제한을 sec 초로 (손절 감시 안 계좌 조회·매도 전송 5초, 11J 보완 3)"""
        old = getattr(self._tl, "timeout", None)
        self._tl.timeout = sec
        try:
            yield
        finally:
            self._tl.timeout = old

    def _tkw(self):
        t = getattr(getattr(self, "_tl", None), "timeout", None)
        return {"timeout": t} if t else {}

    def _read(self, tr, path, body, quick=False):
        """조회 TR: 목록이 있으면 cont-yn/next-key 로 모든 페이지를 모은다 (Codex 3). 중간 페이지 실패는 전체 실패.
        quick: 클라이언트 안 429 재시도(2·4·8초 대기)를 끄고 429 면 바로 실패 (손절 감시 한 번의 대기 상한, 11J)"""
        assert tr in READ_TRS, tr
        lk = READ_SPEC[tr][0]
        cont, key, first, items = "N", "", None, []
        for page in range(MAX_PAGES):
            # 11L: 프로세스 전체 공용 제한기 (TR 별 간격·토큰 단위). 대기 상한을 넘으면 조회 실패. 클라이언트 안 429 재시도는 끔
            # (유량 오류 = 조회 실패, 주 반복을 2·4·8초 붙잡지 않음, 다음 차례에 다시)
            from common import RateGate
            if self._gate_here and RateGate.wait(self.env, tr, max_wait=self.READ_MAX_WAIT) is None:
                raise NoResponse(f"{tr} {page + 1}페이지: 유량 제한기 대기 {self.READ_MAX_WAIT:.0f}초 넘음 (조회 실패, 보내지 않음)")
            kw = {"retry_429": False, **({} if self._gate_here else {"max_wait": self.READ_MAX_WAIT}), **self._tkw()}
            try:
                r = self.client.request(tr_code=tr, method="POST", path=path, json=body, cont_yn=cont, next_key=key, **kw)
            except TypeError:  # cont_yn 인자를 모르는 클라이언트
                r = self.client.request(tr_code=tr, method="POST", path=path, json=body)
            except Exception as e:  # noqa: BLE001
                raise NoResponse(f"{tr} {page + 1}페이지: {str(e)[:180]}") from e
            if r is None:
                raise NoResponse(f"{tr} {page + 1}페이지: HTTP 429 유량 초과 (조회 실패)")
            try:
                j = r.json()
            except Exception as e:  # noqa: BLE001
                raise QueryError(f"{tr} {page + 1}페이지 JSON 파싱 오류: {e!r}"[:200]) from e
            if isinstance(j, dict) and rate_msg(j.get("return_code"), j.get("return_msg")):
                raise QueryError(f"{tr} {page + 1}페이지: 유량 초과 (조회 실패) {str(j.get('return_msg', ''))[:100]}")
            validate_read(tr, j)
            if first is None:
                first = dict(j)
            if lk is None:
                return first
            items += j[lk]
            h = getattr(r, "headers", {}) or {}
            more = str(h.get("cont-yn", "N")).upper() == "Y"
            if more and not h.get("next-key"):  # 11J 보완 3 R1: 다음 쪽이 있다는데 키가 없음 = 모든 쪽을 못 받음 -> 실패
                raise QueryError(f"{tr} {page + 1}페이지: cont-yn Y 인데 next-key 없음 (다음 쪽을 받을 수 없음)")
            if not more:
                first[lk] = items
                first["_pages"] = page + 1
                return first
            cont, key = "Y", h.get("next-key")
        raise QueryError(f"{tr} 페이지가 {MAX_PAGES}개를 넘음")

    def _order(self, tr, body):
        assert tr in ORDER_TRS, tr

        def call():
            if not self.allow_orders:
                raise OrdersDisabled(tr)
            from common import RateGate
            from kiwoom.errors import NotSent
            if self._gate_here and RateGate.wait(self.env, tr, max_wait=self.ORDER_MAX_WAIT) is None:  # 11L: 가짜 서버만 여기서 (키움 클라이언트는 전송 바로 앞)
                raise NotSent(f"유량 대기 상한 {self.ORDER_MAX_WAIT}초 초과 (공용 제한기, api-id={tr}) - 보내지 않음")
            # 확인 리뷰 26: 주문 TR 은 클라이언트 안 429 재시도(2·4·8초 대기)를 끈다. 429 = 받아들여지지 않음(REJECTED -429) ->
            # OrderManager 가 매 전송 직전 시간창을 확인하며 다시 보낸다
            return self.client.request(tr_code=tr, method="POST", path=ORDR, json=body, retry_429=False,
                                       max_wait=self.ORDER_MAX_WAIT, before_send=self.guard, **self._tkw())
        res = classify(call)
        if res.get("not_sent_kind") == "auth":  # N2: 토큰을 버려 다음 시도 때 다시 발급
            try:
                getattr(getattr(self.client, "auth", None), "invalidate", lambda: None)()
            except Exception:  # noqa: BLE001
                pass
        return res

    def buy_limit(self, code, qty, price):
        return self._order("kt10000", {"dmst_stex_tp": "KRX", "stk_cd": code, "ord_qty": str(int(qty)), "ord_uv": str(int(price)),
                                       "trde_tp": "0", "cond_uv": ""})

    def sell_market(self, code, qty):
        return self._order("kt10001", {"dmst_stex_tp": "KRX", "stk_cd": code, "ord_qty": str(int(qty)), "ord_uv": "",
                                       "trde_tp": "3", "cond_uv": ""})

    def cancel(self, orig_ord_no, code, qty=0):
        """kt10003 취소 (qty 0 = 잔량 전부)"""
        return self._order("kt10003", {"dmst_stex_tp": "KRX", "orig_ord_no": str(orig_ord_no), "stk_cd": code, "cncl_qty": str(int(qty))})

    def unfilled(self):
        return self._read("ka10075", ACNT, {"all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": "0"})["oso"]

    def fills(self):
        return self._read("ka10076", ACNT, {"stk_cd": "", "qry_tp": "0", "sell_tp": "0", "ord_no": "", "stex_tp": "0"})["cntr"]

    def balance(self, quick=False):
        return self._read("kt00018", ACNT, {"qry_tp": "1", "dmst_stex_tp": "KRX"}, quick=quick)

    def deposit(self):
        return self._read("kt00001", ACNT, {"qry_tp": "3"})

    def account(self):
        """잔고·예수금 (검증 끝난 값). 반환 dict: holdings, orderable(ord_alow_amt, 증거금 확인용), d2(d2_entra, D+2 추정예수금 = 총자산 계산용), entr"""
        bal = check_fields_bal(self.balance())
        dep = self.deposit()
        return {"holdings": holdings(bal), "orderable": num(dep["ord_alow_amt"]), "d2": num(dep["d2_entra"]), "entr": num(dep["entr"])}


def buy_label(kind):
    """주문 이름표 (11I): 필터 통과일 "매수 (P4)", 필터 미통과일(전체 장부 상위 3) "매수 (점검용)" """
    return "매수 (P4)" if kind == "본래" else f"매수 ({kind})"


def p4_plan(top, price_col, equity, cash, held, kind, upper=None):
    """P4 (EXEC_SPEC 2-3): 종목당 = 총자산(예수금 + 보유 x 15:19 가격)/33, 수량 = 내림(금액 / (가격 x 1.001 x 1.00015)) 원주가,
    지정가 = min(가격 x 1.05 호가 올림, 그날 상한가) (11J, upper = {code: 상한가}, resident.upper_limits: 스냅샷 upl_pric, 없거나 비정상이면
    기준가(전일 종가) x 1.30 호가 내림. upper 에 있는데 값이 None 이면 그 종목은 매수 안 함 (데이터 이상)).
    건너뜀: 실제 보유 중 / 현금(주문 가능 금액 - 앞 매수 주문 금액) < 종목당 금액 / 수량 0. 대체 안 함"""
    from common import CAPITAL, LIMIT_MULT, SLOTS, ceil_tick, sb  # noqa: F401
    amt = equity / SLOTS
    plan = []
    for r in top.itertuples():
        p = float(getattr(r, price_col))
        qty = int(np.floor(amt / (p * (1 + sb.SLIPPAGE) * (1 + sb.COMMISSION))))
        limit = ceil_tick(p * LIMIT_MULT)
        up = (upper or {}).get(r.code)
        capped = bool(up and up > 0 and limit > up)
        if capped:
            limit = int(up)  # 상한가는 호가 단위에 맞는 값
        no_upper = upper is not None and r.code in upper and up is None  # 11J 보완 7: 상한가·전일 종가 모두 없음 = 데이터 이상, 매수 안 함
        why = "보유 중" if r.code in held else ("데이터 이상: 상한가·전일 종가 없음" if no_upper else
                                              ("현금 부족" if cash < amt else ("수량 0" if qty == 0 else "")))
        plan.append({"code": r.code, "score": float(r.score), "price_1519": p, "amount_target": round(amt), "qty": 0 if why else qty,
                     "limit_price": limit, "order_value": 0 if why else limit * qty, "skip": why, "kind": kind,
                     **({"limit_capped_at_upper": True, "limit_1p05": ceil_tick(p * LIMIT_MULT)} if capped else {})})
        if not why:
            cash -= limit * qty
    return plan


class AccountWatch:
    """장중 계좌 조회 실패 감시 (2026-10-23 결정): 실패는 기록만, 연속 n분 실패면 알림 한 번, 회복되면 알림 한 번"""

    def __init__(self, notify=lambda m: None, n_alert=3):
        self.notify, self.n_alert = notify, n_alert
        self.fail_count, self.streak, self.streak_start, self.alerted = 0, 0, None, False
        self.longest_min, self.log = 0.0, []

    def fail(self, t, err):
        self.fail_count += 1
        self.streak += 1
        if self.streak_start is None:
            self.streak_start = t
        self.longest_min = max(self.longest_min, (t - self.streak_start).total_seconds() / 60 + 1)
        self.log.append({"time": t.strftime("%H:%M:%S"), "error": str(err)[:150]})
        if self.streak >= self.n_alert and not self.alerted:
            self.alerted = True
            self.notify(f"[계좌 조회 연속 실패] {self.streak}분 연속 (첫 실패 {self.streak_start:%H:%M}): {str(err)[:100]} — 손절 판단은 시세로 계속")

    def ok(self, t):
        if self.alerted:
            self.notify(f"[회복] 계좌 조회 다시 됨 ({t:%H:%M}, 연속 실패 {self.streak}분)")
        self.streak, self.streak_start, self.alerted = 0, None, False

    def summary(self):
        return {"fail_count": self.fail_count, "longest_streak_min": round(self.longest_min, 1), "log": self.log[-50:]}


def new_duty(kind, since, reason, **kw):
    """매도 의무 표시 (종목 코드 단위, 11J 보완 3): kind = stop(손절 진행 중) / trend(추세 매도), since = 발생일, at = 발생 시각, reason = 사유"""
    since = str(since or "")
    return {"kind": kind, "since": since[:10] or None, "at": since[11:19] or None, "reason": reason, **kw}


ZERO_CONFIRM_SEC = 60  # 11J 보완 3 R1: 보유 0 을 이 간격(초) 이상 두고 두 번 연속 확인해야 의무 끝


def end_duties(positions, acct, now):
    """11J 보완 3 R1: 성공한 계좌 조회(acct = holdings, 모든 쪽을 받은 응답)에서 보유 0 이 ZERO_CONFIRM_SEC 이상 간격으로 두 번 연속일 때만
    매도 의무를 끝낸다 (다음 날 시가 매도 목록에서도 뺌). 첫 0 은 의무에 zero_at 으로 기록만, 그 사이 보유가 보이면 지움 (응답에서 한 번 빠진 종목).
    반환: (끝난 종목, 이번에 처음 0 을 본 종목)"""
    duty = positions.setdefault("sell_duty", {})
    now = pd.Timestamp(now)
    ended, first = [], []
    for c, du in list(duty.items()):
        if int((acct.get(c) or {}).get("qty", 0)) > 0:
            du.pop("zero_at", None)
            continue
        z = du.get("zero_at")
        if z is None or now < pd.Timestamp(z):
            du["zero_at"] = f"{now:%Y-%m-%d %H:%M:%S}"
            first.append(c)
        elif now - pd.Timestamp(z) >= pd.Timedelta(seconds=ZERO_CONFIRM_SEC):
            duty.pop(c)
            positions.get("next_open_sells", {}).pop(c, None)
            ended.append(c)
    return ended, first


def qty_changes(held, acct, records, date):
    """11K 7: 체결 없이 보유 수량이 바뀐 종목 = 상태 파일 수량과 성공한 계좌 조회 수량이 다르고(둘 다 > 0) 오늘 그 종목 매수·매도 의도가
    하나도 없음(NOT_SENT 만 있으면 없는 것으로). 액면분할·병합·무상증자 추정. 반환 {code: {file, account}}"""
    out = {}
    for c, p in held.items():
        fq, aq = int(p.get("qty", 0)), int((acct.get(c) or {}).get("qty", 0))
        if fq <= 0 or aq <= 0 or fq == aq:
            continue
        mine = [r for r in records if r.get("date") == date and r.get("code") == c and r.get("side") in ("buy", "sell") and r.get("status") != NOT_SENT]
        if not mine:
            out[c] = {"file": fq, "account": aq}
    return out


def duty_inputs(positions, codes, acct):
    """OrderManager.duty_sell 에 줄 값: {code: {file_qty: 보유 파일 수량, acct: 같은 분 계좌 {qty, sellable} (조회 실패면 None)}}"""
    held = positions.get("held", {})
    return {c: {"file_qty": int((held.get(c) or {}).get("qty", 0)),
                "acct": None if acct is None else (acct.get(c) or {"qty": 0, "sellable": 0})} for c in codes}


def stop_cycle(om, watch, positions, quote_fn, balance_fn, now, mismatch_seen=None, detect=True, budget=None, on_trigger=None,
               wall=time.time):
    """1분 손절 감시 한 번 (2026-10-23 결정, 11J, 11J 보완 3). 보유 종목·손절가 = positions(상태 파일), 현재가 = quote_fn(codes[, deadline]).
    매도 의무 = positions["sell_duty"] (종목 코드 단위: 손절 진행 중·추세 매도). 증권사 잔고가 정답 (OrderManager.duty_sell).
    ① 계좌 조회 (balance_fn). 성공하면 보유 0 을 기록, 1분 이상 간격 두 번 연속 0 이면 의무를 끝냄 (end_duties)
    ② 의무 종목마다 같은 분 계좌 매도 가능 수량으로 시장가 (0 이면 걸린 주문 대기). 계좌 조회 실패면 걸린 매도가 없을 때만 보유 파일 수량
    ③ 새 손절 감지 (시세 조회, 감지 창은 이 감시가 시작된 시각 now, 의무가 있는 종목은 뺌). 새 의무는 on_trigger 로 먼저 저장(선기록)한 뒤 보냄
    체결 조회는 하지 않는다. budget(초): 시세 조회의 대기 상한 (wall() 기준, 감시 시작부터). 반환: dict"""
    held_pos = {c: p for c, p in positions.get("held", {}).items() if p.get("qty", 0) > 0}
    duty = positions.setdefault("sell_duty", {})
    kind_of = lambda c: (positions.get("held", {}).get(c) or {}).get("kind", "본래")  # noqa: E731
    at = pd.Timestamp(now).strftime("%H:%M:%S")
    wall_end = wall() + budget if budget else None
    # ① 계좌 조회
    acct, acct_err, ended, zero = None, None, [], []
    try:
        acct = holdings(check_fields_bal(balance_fn()))  # 모든 쪽을 받고 필드가 맞는 응답만 성공 (R1)
        watch.ok(now)
        ended, zero = end_duties(positions, acct, now)
    except Exception as e:  # noqa: BLE001  (응답 없음·HTTP 오류·쪽 일부 실패 모두)
        acct_err = e
        watch.fail(now, e)
    # 11K 보완 3: 체결로 설명 안 되는 수량 변화(이번 분 새로 본 것 = changed, 호출한 쪽이 수동 확인 표시)와 수동 확인 중인 종목(paused)은
    # 자동 손절·추세 매도와 새 손절 감지를 멈춘다 (자동 조정 없음, 사람이 확인할 때까지)
    from positions import in_review
    changed = qty_changes(held_pos, acct, om.records, om.date()) if acct is not None else {}
    paused = sorted(c for c, p in held_pos.items() if in_review(p))
    stopped = set(changed) | set(paused)
    # ② 의무 매도
    go = {c: x for c, x in duty.items() if c not in stopped}
    orders = om.send_duties(go, duty_inputs(positions, go, acct), kind_of)
    # ③ 새 손절 감지 (2026-10-24 F-01: 계좌 조회가 됐는데 계좌에 없으면 매도 대상에서 뺀다)
    not_in_acct = [c for c in held_pos if acct is not None and (c not in acct or acct[c]["qty"] <= 0)]
    prices, quote_err, new = {}, None, {}
    if held_pos and detect:
        try:
            codes = sorted(held_pos)
            prices = quote_fn(codes, deadline=wall_end) if wall_end else quote_fn(codes)
        except Exception as e:  # noqa: BLE001  (시세 조회 실패·시간 상한)
            quote_err = e
        cand = {c: {"cur_price": float(prices[c])} for c in held_pos
                if c not in duty and c not in not_in_acct and c not in stopped and c in prices and prices[c] > 0}
        stops = {c: p["stop_price"] for c, p in held_pos.items() if p.get("stop_price")}
        new = om.detect_stops(cand, stops, at=at)
        if new:
            for c, rec in new.items():
                duty[c] = new_duty("stop", rec["since"], f"현재가 {rec['price']:.0f} < 손절가 {rec['stop']:.0f}", price=rec["price"], stop=rec["stop"],
                                   **({"stop_temp": True} if held_pos[c].get("stop_temp") else {}))  # N1: 임시 손절가로 만든 의무
            if on_trigger is not None:
                on_trigger(new)  # 손절 진행 중 표시를 주문 전송 전에 먼저 저장
            for c, rec in new.items():
                om.notify(f"[손절 진행 중] {c} 현재가 {rec['price']:.0f} < 손절가 {rec['stop']:.0f} — 팔릴 때까지 매 분 시장가 (가격이 반등해도)")
            orders += om.send_duties({c: duty[c] for c in new}, duty_inputs(positions, new, acct), kind_of)
    diff = None
    if acct is not None:
        a = {c: h["qty"] for c, h in acct.items() if h["qty"] > 0}
        s = {c: int(p["qty"]) for c, p in held_pos.items()}
        # 수량 변화·수동 확인 종목은 그 종목만 멈춤 (전체 매수 차단인 보유 불일치에 넣지 않음)
        diff = {c: v for c, v in unexplained_diff(s, a, om.records, om.date()).items() if c not in stopped}
        not_in_acct = [c for c in not_in_acct if c in diff]  # Codex 14: 당일 매도로 설명되면 불일치 아님 (매도 대상에서는 그대로 뺀다)
        if diff and mismatch_seen is not None:
            key = json_key(diff)
            if key not in mismatch_seen:
                mismatch_seen.add(key)
                om.notify(f"[보유 불일치] 상태 파일 vs 계좌: {diff}")
    return {"orders": orders, "quotes": prices, "account_ok": acct is not None, "account_error": str(acct_err)[:150] if acct_err else None,
            "mismatch": diff, "not_in_account": not_in_acct, "triggered_new": new, "duties": sorted(duty), "duty_ended": ended,
            "duty_zero": zero, "acct": acct, "qty_changed": changed, "review_paused": paused,
            "quote_error": str(quote_err)[:150] if quote_err else None}


def unexplained_diff(state, acct, records, date):
    """상태 파일 수량 vs 계좌 수량. 같은 날 매도·매수 의도(ACCEPTED·UNKNOWN·SENDING)로 설명되는 차이는 뺀다 (Codex 14).
    설명 = 계좌가 줄었고 그 차이 <= 그날 매도 의도 수량 합, 또는 계좌가 늘었고 그 차이 <= 그날 매수 의도 수량 합. 반환: 설명 안 되는 {code: {state, account}}"""
    out = {}
    for c in set(state) | set(acct):
        sq, aq = int(state.get(c, 0)), int(acct.get(c, 0))
        if sq == aq:
            continue
        mine = [r for r in records if r.get("date") == date and r.get("code") == c and r.get("status") in BLOCKING]
        sell = sum(int(r.get("qty", 0)) for r in mine if r.get("side") == "sell")
        buy = sum(int(r.get("qty", 0)) for r in mine if r.get("side") == "buy")
        if (aq < sq and sq - aq <= sell) or (aq > sq and aq - sq <= buy):
            continue
        out[c] = {"state": sq, "account": aq}
    return out


def fill_known(it):
    """3차 확인 리뷰 R3: 이 주문의 체결 수량을 아는가 (15:35 확인으로 미체결 잔량을 채웠거나 체결 조회가 된 주문). 잔고로만 확정한 주문은 모름"""
    return it.get("unfilled_qty") is not None or bool(it.get("fills_checked") or it.get("fill_refreshed"))


def json_key(d):
    return ";".join(f"{k}:{v['state']}/{v['account']}" for k, v in sorted(d.items()))


class FieldError(Exception):
    """응답 필드가 없거나 형식이 다름 (정산을 SUCCEEDED 로 하지 않음, 2026-10-24 F-04)"""


REQUIRED = {"fills": ("ord_no", "stk_cd", "cntr_qty", "cntr_pric"), "unfilled": ("ord_no", "stk_cd", "oso_qty"),
            "balance": ("stk_cd", "rmnd_qty")}


def check_fields(rows, kind):
    """키움 문서 기준 필드가 있고 숫자 칸이 숫자인지. 목록이 비면 확인할 것이 없음"""
    for x in rows:
        miss = [k for k in REQUIRED[kind] if k not in x]
        if miss:
            raise FieldError(f"{kind} 응답에 필드 없음 {miss} (있는 필드 {sorted(x)[:20]})")
        for k in REQUIRED[kind]:
            if k in ("ord_no", "stk_cd"):
                continue
            if np.isnan(num(x.get(k))):
                raise FieldError(f"{kind} 응답 {k} 형식 다름: {x.get(k)!r}")
    return rows


def check_fields_bal(bal):
    """kt00018 응답의 보유 목록 필드 확인 (F-04, Codex 2: 목록 필드가 없으면 실패) 후 그대로 반환"""
    if not isinstance(bal, dict) or not isinstance(bal.get("acnt_evlt_remn_indv_tot"), list):
        raise FieldError("balance 응답에 보유 목록(acnt_evlt_remn_indv_tot) 없음 또는 목록 아님")
    check_fields(bal["acnt_evlt_remn_indv_tot"], "balance")
    return bal


def holdings(bal):
    """kt00018 -> {code: {qty, avg_price, cur_price, sellable}}"""
    out = {}
    for it in bal.get("acnt_evlt_remn_indv_tot", []) or []:
        c = code_of(it.get("stk_cd"))
        out[c] = {"qty": int(num(it.get("rmnd_qty"))), "avg_price": num(it.get("pur_pric")), "cur_price": num(it.get("cur_prc")),
                  "sellable": int(num(it.get("trde_able_qty", it.get("rmnd_qty"))))}
    return out


# ---------------------------------------------------------------- 주문 의도 기록

class IntentStore:
    """하루 주문 의도 (state_dir/intents/{날짜}.json). 바뀔 때마다 원자적 쓰기. 재시작 때 SENDING -> UNKNOWN (전송 직후 꺼졌을 수 있음)"""

    def __init__(self, path, clock=None):
        from common import atomic_json, now_kst
        self._write = atomic_json
        self.path, self.clock = path, clock or now_kst
        self.lock = threading.RLock()
        self.corrupt = None
        self.items = []
        self.block_path = f"{path}.block"  # Codex 1: 손상이 한 번이라도 발견된 날짜의 차단 파일 (재시작해도 유지)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.items = json.load(f)
                assert isinstance(self.items, list)
            except Exception as e:  # noqa: BLE001  (2026-10-24 F-03: 있는데 못 읽으면 그날 모든 주문을 막고 손상 파일은 보관)
                kept = f"{path}.corrupt-{self.clock():%Y%m%d%H%M%S}"
                os.replace(path, kept)
                self.items, self.corrupt = [], f"의도 파일 읽기 실패 ({e!r})"[:200] + f" -> {kept} 로 보관"
                atomic_json(self.block_path, {"since": self.clock().strftime("%Y-%m-%d %H:%M:%S"), "reason": self.corrupt,
                                              "clear": "복구·대조가 끝난 뒤 사람이 이 파일을 지운다"})
        if self.corrupt is None and os.path.exists(self.block_path):
            try:
                with open(self.block_path, encoding="utf-8") as f:
                    b = json.load(f)
            except Exception:  # noqa: BLE001
                b = {}
            self.corrupt = f"이 날짜에 의도 파일 손상이 발견됨 ({b.get('since')}) — 차단 파일 {self.block_path} 이 있는 동안 계속 차단"
        now = self.clock()
        for it in self.items:
            if it.get("status") == SENDING:
                it["status"] = UNKNOWN
                it["cause"] = "재시작 때 SENDING (전송 직후 꺼졌을 수 있음)"
                it["checks_due"] = [str(now)] + [str(now + pd.Timedelta(seconds=s)) for s in CHECK_DELAYS]
                it.setdefault("history", []).append({"time": now.strftime("%H:%M:%S"), "status": UNKNOWN, "note": "재시작"})
        if self.items:
            self.save()

    def save(self):
        with self.lock:
            self._write(self.path, self.items)

    def add(self, **kw):
        with self.lock:
            t = self.clock()
            it = {"intent_id": f"{kw['date']}-{kw['stage']}-{kw['code']}-{len(self.items) + 1}", "status": SENDING,
                  "created": t.strftime("%H:%M:%S.%f")[:-3], "history": [{"time": t.strftime("%H:%M:%S.%f")[:-3], "status": SENDING}],
                  "ord_no": None, "filled_qty": 0, "fill_price": None, "fill_time": None, "fill_time_field": None, "unfilled_qty": None,
                  "fill_status": None, "cancel": None, "assumed_price": None, "diff_won": None, "diff_pct": None, **kw}
            self.items.append(it)
            self.save()
        self._emit("add", it, kw)
        return it

    def update(self, it, **kw):
        with self.lock:
            st = kw.get("status")
            it.update(kw)
            if st:
                it.setdefault("history", []).append({"time": self.clock().strftime("%H:%M:%S.%f")[:-3], "status": st,
                                                     **{k: v for k, v in kw.items() if k in ("cause", "ord_no", "matched_by")}})
            self.save()
        self._emit("update", it, kw)

    def _emit(self, kind, it, kw):
        """사람이 읽을 기록용 (상주 프로그램이 listener 를 붙임). 기록 실패가 주문을 막지 않게"""
        if getattr(self, "listener", None) is not None:
            try:
                self.listener(kind, it, kw)
            except Exception:  # noqa: BLE001
                pass

    def of(self, date, code, stage=None, side=None):
        return [i for i in self.items if i["date"] == date and i["code"] == code and (stage is None or i["stage"] == stage)
                and (side is None or i["side"] == side)]

    def blocking(self, date, code, stage=None, side=None):
        b = [i for i in self.of(date, code, stage, side) if i["status"] in BLOCKING]
        return b[-1] if b else None

    def rejected_count(self, date, code, stage):
        # 서버에 닿지 않은 NOT_SENT(유량 대기 상한·전송 직전 마감 확인, 2차 확인 리뷰 4)는 시도 횟수에 안 셈
        return sum(1 for i in self.of(date, code, stage) if i["status"] in (REJECTED, NOT_SENT) and not i.get("not_sent_kind"))


class OrderManager:
    """주문·체결 기록 (EXEC_SPEC 5장 체결 대조 형식). clock() -> pd.Timestamp, sleep(초). store: IntentStore (없으면 메모리용 임시 파일 없음)"""

    def __init__(self, broker, clock=None, sleep=time.sleep, notify=lambda msg: None, times=None, store=None):
        if clock is None:
            from common import now_kst as clock
        self.b, self.clock, self.sleep, self.notify = broker, clock, sleep, notify
        t = times or {}
        self.t = {"buy_from": t.get("buy_from", "15:21:00"),   # 11L 보완 2: buy_until 은 안 씀 (매수 마감 하나 = no_new_order_after)
                  "no_new_order_after": t.get("no_new_order_after", "15:28:00"), "close_check": t.get("close_check", "15:35:00"),
                  "trend_sell_from": t.get("trend_sell_from", "08:30:00"), "trend_sell_until": t.get("trend_sell_until", "08:59:00"),
                  "stop_from": t.get("stop_from", "09:00:00"), "stop_until": t.get("stop_until", "15:20:00"),
                  # 11J: 손절 진행 중 매도는 장 마감 동시호가(15:20 ~ 15:30) 안 15:29 까지 보냄 (시장가 = 종가 체결)
                  "stop_send_until": t.get("stop_send_until", "15:29:00")}
        self.store = store if store is not None else _MemStore(clock)
        self.next_open_sells = []
        self.alerted_unresolved = set()
        self.corrupt_alerted = False

    @property
    def records(self):
        return self.store.items

    def hms(self):
        return self.clock().strftime("%H:%M:%S")

    def date(self):
        return str(self.clock().date())

    # ---------------------------------------------------------- 보내기

    def in_window(self, window):
        """window = (시작, 끝, 끝 포함 여부). 매 전송 직전에 확인 (Codex 26)"""
        if window is None:
            return True
        h = self.hms()
        return window[0] <= h and (h <= window[1] if window[2] else h < window[1])

    def _send(self, stage, side, code, qty, price, trde_tp, kind, reason, assumed_kind, fn, retry_now, extra=None, window=None, blocking=True):
        """주문 의도 선기록 -> 보내기 -> 결과 기록. retry_now: REJECTED 면 1초 뒤 바로 1회 더 (매수·추세 매도).
        window: 매 전송(재시도 포함) 직전에 시간창 확인, 넘으면 보내지 않음 (Codex 26).
        전송 뒤 어떤 예외가 나도 의도를 UNKNOWN 으로 저장 (Codex 9)."""
        d = self.date()
        if getattr(self.store, "corrupt", None):
            # 11K 6: 의도 파일 손상이면 매수만 막고, 매도 의무(손절·추세 매도)와 그 취소는 계속. 손상 전 의도를 모르므로 매도 수량은
            # 같은 분 계좌 매도 가능 수량으로만 (증권사가 걸린 매도를 이미 뺀 값, duty_sell 의 qty_basis) — 보유 파일 수량으로는 안 보냄
            sell_ok = side == "sell" and stage in ("stop", "trend_sell", "trend_late") and (extra or {}).get("qty_basis") == "계좌 매도 가능 수량"
            if not self.corrupt_alerted:
                self.corrupt_alerted = True
                self.notify(f"[주문 차단] {self.store.corrupt} — 오늘 매수 막음, 매도 의무(손절·추세 매도)는 계좌 매도 가능 수량 안에서 계속")
            if not (sell_ok or stage == "cancel"):
                why = "매수 막음" if side == "buy" else "매도는 계좌 매도 가능 수량을 알 때만 (계좌 조회 실패)"
                return {"skipped": f"의도 파일 손상 — {why} ({self.store.corrupt})", "code": code}
        if blocking:  # 매도 의무는 막지 않음 (계좌 매도 가능 수량 규칙 duty_sell 로 이미 확인, 11J 보완 3)
            blk = self.store.blocking(d, code, side=side)
            if blk is not None:
                return {"skipped": f"같은 날 {side} 주문 의도 {blk['intent_id']} 가 {blk['status']} — 다시 보내지 않음", "code": code}
        rate_waits = 0
        while True:
            if not self.in_window(window):
                return {"skipped": f"시간창 밖 ({self.hms()}, {window[0]} ~ {window[1]}) — 보내지 않음", "code": code}
            n = self.store.rejected_count(d, code, stage)
            if MAX_ATTEMPTS[stage] is not None and n >= MAX_ATTEMPTS[stage]:
                return {"skipped": f"{stage} 시도 {n}회 (최대 {MAX_ATTEMPTS[stage]}) — 더 보내지 않음", "code": code}
            it = self.store.add(date=d, stage=stage, side=side, code=code, qty=int(qty), price=price, trde_tp=trde_tp, attempt=n + 1,
                                kind=kind, reason=reason, assumed_kind=assumed_kind, **(extra or {}))
            self.b.guard = (lambda: self.in_window(window))  # 2차 확인 리뷰 4: 유량 대기가 끝난 뒤 보내기 바로 앞에서 다시 확인
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001  (보내는 함수 자체의 예외는 아래와 같이 UNKNOWN)
                res = e
            finally:
                self.b.guard = None
            if isinstance(res, dict) and res.get("outcome") == NOT_SENT and res.get("not_sent_kind") == "rate":
                # 11L: 유량 초과(HTTP 429·return_code 5·유량 대기 상한) = 처리 안 됨. 거부 횟수에 안 세고 간격을 두고 다시 (모든 단계, 한 번에 최대 RATE_RETRIES).
                # 시간창 안에서만 (매수 = 15:28 전, 11L 보완 2). 다 쓰면 NOT_SENT(다시 보내도 됨) -> 매수·08:30 추세 매도는 다음 분 다시 보내기, 매도 의무는 다음 분
                self.store.update(it, status=NOT_SENT, cause=res.get("cause"), resendable=True, not_sent_kind="rate", return_msg=res.get("msg"),
                                  return_code=res.get("return_code"))
                tr = "kt10000" if side == "buy" else "kt10003" if stage == "cancel" else "kt10001"
                gap = max(self.b.gap(tr) if hasattr(self.b, "gap") else 0.0, 1.0)  # 공용 제한기 간격, 최소 1초
                later = (self.clock() + pd.Timedelta(seconds=gap)).strftime("%H:%M:%S")
                if rate_waits < RATE_RETRIES and (window is None or (later <= window[1] if window[2] else later < window[1])):
                    rate_waits += 1
                    self.sleep(gap)
                    continue
                self.notify(f"[주문 유량 초과] {stage} {side} {code} {qty}주: {res.get('cause')} {rate_waits + 1}번 — 거부 횟수에 안 셈, 다음 차례(다음 분)에 다시"[:300])
                return it
            try:
                if isinstance(res, Exception):
                    raise res
                done = self._apply(it, res, stage, side, code, qty, n)
            except Exception as e:  # noqa: BLE001  (전송 뒤 처리 중 어떤 예외든 접수 여부 모름)
                t = self.clock()
                self.store.update(it, status=UNKNOWN, cause=f"전송 뒤 처리 오류: {e!r}"[:200],
                                  checks_due=[str(t + pd.Timedelta(seconds=x)) for x in CHECK_DELAYS])
                self.notify(f"[주문 응답 없음] {side} {code} {qty}주 (전송 뒤 처리 오류 {e!r}) — 조회로 확정 전 재주문 안 함"[:300])
                return it
            if done or not retry_now or (MAX_ATTEMPTS[stage] is not None and n + 1 >= MAX_ATTEMPTS[stage]):
                return it
            self.sleep(1.0)

    def _apply(self, it, res, stage, side, code, qty, n):
        """응답 분류 결과를 의도에 저장. 반환: 끝났으면 True (REJECTED 만 False)"""
        if not isinstance(res, dict) or res.get("outcome") not in (ACCEPTED, REJECTED, UNKNOWN, NOT_SENT):
            res = {"outcome": UNKNOWN, "cause": f"분류 결과 형식 오류: {str(res)[:120]}"}
        out = res["outcome"]
        t = self.clock()
        if out == ACCEPTED:
            self.store.update(it, status=ACCEPTED, ord_no=res.get("ord_no"), order_time=t.strftime("%H:%M:%S.%f")[:-3],
                              return_msg=res.get("msg"))
            return True
        if out == UNKNOWN:
            self.store.update(it, status=UNKNOWN, cause=res.get("cause"),
                              checks_due=[str(t + pd.Timedelta(seconds=s)) for s in CHECK_DELAYS])
            self.notify(f"[주문 응답 없음] {side} {code} {qty}주 ({res.get('cause')}) — 조회로 확정 전 재주문 안 함")
            return True
        if out == NOT_SENT:
            self.store.update(it, status=NOT_SENT, cause=res.get("cause"),
                              **({"resendable": True, "not_sent_kind": res["not_sent_kind"]} if res.get("not_sent_kind") else {}))
            if res.get("not_sent_kind") == "auth":
                self.notify(f"[주문 보내지 않음] 인증·토큰 {stage} {side} {code} {qty}주: {res.get('cause')} — 거부 횟수에 안 셈, 다음 차례에 다시"[:300])
            return True
        self.store.update(it, status=REJECTED, return_code=res.get("return_code"), return_msg=res.get("msg"), cause=res.get("cause"))
        self.notify(f"[주문 거부] {stage} {side} {code} {qty}주 (시도 {n + 1}/{MAX_ATTEMPTS[stage] or '상한 없음'}): {res.get('msg')} ({res.get('cause')})")
        return False

    # ---------------------------------------------------------- 응답 없음 확인

    def _match(self, it, rows, claimed):
        side_word = "매수" if it["side"] == "buy" else "매도"
        for src, x in rows:
            ono = str(x.get("ord_no", ""))
            if not ono or ono in claimed or code_of(x.get("stk_cd")) != it["code"]:
                continue
            io = str(x.get("io_tp_nm", "")) + str(x.get("sell_tp", ""))
            if x.get("io_tp_nm") is not None and side_word not in io:
                continue
            if int(num(x.get("ord_qty"))) != int(it["qty"]):
                continue
            return src, ono
        return None

    def reconcile(self, stage=None, force=False, final=False):
        """UNKNOWN 의도를 미체결·체결 조회와 종목·수량으로 대조. force: 예정 시각과 무관하게 지금. final: 15:35 정산 (없으면 NOT_SENT)"""
        now = self.clock()
        due = [i for i in self.records if i["status"] == UNKNOWN and i["stage"] != "cancel" and (stage is None or i["stage"] == stage)
               and (force or final or any(pd.Timestamp(x) <= now for x in i.get("checks_due", [])))]
        if not due:
            return 0
        try:
            rows = [("미체결", x) for x in self.b.unfilled()] + [("체결", x) for x in self.b.fills()]
        except Exception as e:  # noqa: BLE001  (Codex 10: 결과 불명확과 구분 — "확인 실패", 1분 뒤 다시 예약)
            for it in due:
                rest = [x for x in it.get("checks_due", []) if pd.Timestamp(x) > now] or [str(now + pd.Timedelta(seconds=60))]
                self.store.update(it, checks=it.get("checks", []) + [{"time": now.strftime("%H:%M:%S"), "result": f"확인 실패 (조회 오류): {e!r}"[:150]}],
                                  checks_due=rest, last_check="확인 실패")
            return 0
        claimed = {i["ord_no"] for i in self.records if i.get("ord_no")}
        multi = self.multi_unknown()
        resolved = 0
        for it in due:
            m = self._match(it, rows, claimed)
            rest = [x for x in it.get("checks_due", []) if pd.Timestamp(x) > now]
            checks = it.get("checks", []) + [{"time": now.strftime("%H:%M:%S"), "result": f"{m[0]} {m[1]}" if m else "조회에 없음"}]
            if m:
                claimed.add(m[1])
                self.store.update(it, status=ACCEPTED, ord_no=m[1], matched_by=f"{m[0]} 조회 (종목·수량)", checks=checks, checks_due=[])
                resolved += 1
            elif final:
                self.store.update(it, status=NOT_SENT, cause="15:35 정산 조회에도 없음", checks=checks, checks_due=[])
                resolved += 1
            elif not rest and it["side"] == "sell" and (it["date"], it["code"]) not in multi and (u := self._sell_untouched(it, checks)) is None:
                # Codex 10: 잔고 조회 실패 = 확인 실패 -> 1분 뒤 다시 (장중 계속)
                self.store.update(it, checks=checks, checks_due=[str(now + pd.Timedelta(seconds=60))], last_check="확인 실패")
            elif not rest and it["side"] == "sell" and (it["date"], it["code"]) not in multi and u:
                # (3차 확인 리뷰 R1: 같은 날 같은 종목에 UNKNOWN 이 둘 이상이면 잔고 하나로 확정하지 않는다 -> 아래 else, 15:35 체결 조회로)
                # 2026-10-24 (A3 보완): 매도 UNKNOWN 이 2분 뒤에도 미체결·체결 어디에도 없고 잔고 수량이 그대로면 NOT_SENT 로 확정 -> 다시 보낼 수 있음
                # (첫 매도가 접수됐다면 매도 가능 수량이 묶여 두 번째 매도는 증권사가 수량 부족으로 거부한다)
                self.store.update(it, status=NOT_SENT, cause="2분 뒤 미체결·체결에 없음, 잔고 수량 그대로", resendable=True, checks=checks, checks_due=[])
                self.notify(f"[매도 다시 보냄 가능] {it['stage']} {it['code']} {it['qty']}주 — 2분 뒤 흔적 없음·잔고 그대로")
                resolved += 1
            else:
                self.store.update(it, checks=checks, checks_due=rest)
                if not rest and it["intent_id"] not in self.alerted_unresolved:
                    self.alerted_unresolved.add(it["intent_id"])
                    self.notify(f"[주문 확인 안 됨] {it['side']} {it['code']} {it['qty']}주 — 2분 동안 조회에 없음, 오늘 이 종목은 건드리지 않음 (15:35 정산 때 다시 맞춤)")
        return resolved

    def _sell_untouched(self, it, checks):
        """매도 의도: 지금 잔고 수량이 보내기 전 수량과 같으면 True, 다르면 False, 조회 실패면 None (확인 실패)"""
        before = it.get("held_qty_before")
        if before is None:
            return False
        try:
            h = holdings(check_fields_bal(self.b.balance()))
        except Exception as e:  # noqa: BLE001
            checks.append({"time": self.hms(), "result": f"확인 실패 (잔고 조회 오류): {e!r}"[:120]})
            return None
        now_qty = h.get(it["code"], {}).get("qty", 0)
        checks.append({"time": self.hms(), "result": f"잔고 {now_qty} (보내기 전 {before})"})
        return int(now_qty) == int(before)

    def resendable(self, stage):
        """그날 그 단계에서 NOT_SENT(다시 보내도 됨)로 끝났고 막는 의도가 없는 종목"""
        d = self.date()
        out = []
        for i in self.records:
            if i["date"] == d and i["stage"] == stage and i["status"] == NOT_SENT and i.get("resendable") \
                    and self.store.blocking(d, i["code"], side=i["side"]) is None and i["code"] not in out:
                out.append(i["code"])
        return out

    def multi_unknown(self):
        """3차 확인 리뷰 R1: 같은 날 같은 종목에 UNKNOWN·SENDING 주문(취소 제외)이 둘 이상인 (날짜, 종목). 잔고 하나로는 각각 확정할 수 없다"""
        n = {}
        for i in self.records:
            if i["status"] in (UNKNOWN, SENDING) and i["stage"] != "cancel":
                n[(i["date"], i["code"])] = n.get((i["date"], i["code"]), 0) + 1
        return {k for k, v in n.items() if v >= 2}

    def unresolved(self, stage=None):
        return [i for i in self.records if i["status"] in (UNKNOWN, SENDING) and i["stage"] != "cancel" and (stage is None or i["stage"] == stage)]

    # ---------------------------------------------------------- 매수 (장 마감 동시호가)

    def place_buys(self, plan, kind, orderable=None, pre_qty=None):
        """plan: p4_plan 결과 (code, qty, limit_price, skip). kind: "본래" / "점검용". 15:21 ~ 15:25 에만 새로 보냄.
        orderable: 주문가능금액(kt00001 ord_alow_amt). 지정가 x 수량 합이 넘으면 그 신호는 건너뛰고 "증거금 부족(백테스트는 매수)" 기록 (2026-10-24 B4)"""
        out = []
        left = orderable
        for p in plan:
            if p.get("skip") or not p.get("qty"):
                continue
            need = float(p["limit_price"]) * int(p["qty"])
            if left is not None and need > left:
                if not self.store.of(self.date(), p["code"], stage="buy"):
                    it = self.store.add(date=self.date(), stage="buy", side="buy", code=p["code"], qty=int(p["qty"]), price=p["limit_price"],
                                        trde_tp="0", attempt=0, kind=kind, reason=buy_label(kind), assumed_kind="buy")
                    self.store.update(it, status=NOT_SENT, cause=f"증거금 부족(백테스트는 매수): 필요 {need:,.0f} > 남은 주문가능금액 {left:,.0f}")
                out.append({"skipped": "증거금 부족(백테스트는 매수)", "code": p["code"], "need": need, "left": left})
                continue
            now = self.hms()
            if not (self.t["buy_from"] <= now < self.t["no_new_order_after"]):  # 11L 보완 2: 매수 전송은 15:21 부터 15:28 전까지 하나 (동시호가 = 종가 체결)
                out.append({"skipped": f"보내지 않음 (시각 {now}, 보내는 시각 {self.t['buy_from']} ~ {self.t['no_new_order_after']} 전)", "code": p["code"]})
                continue
            r = self._send("buy", "buy", p["code"], p["qty"], p["limit_price"], "0", kind, buy_label(kind), "buy",
                           lambda p=p: self.b.buy_limit(p["code"], p["qty"], p["limit_price"]), retry_now=True,
                           window=(self.t["buy_from"], self.t["no_new_order_after"], False),
                           extra={"pre_qty": int((pre_qty or {}).get(p["code"], 0))} if pre_qty is not None else None)
            out.append(r)
            if left is not None and r.get("status") in (ACCEPTED, UNKNOWN):
                left -= need
        return out

    # ---------------------------------------------------------- 매도

    def retry_wait(self, stage, code):
        """상한 없는 단계(손절·늦은 추세 매도)의 거부 뒤 간격 (11J): 거부 5회까지는 매 분, 그 뒤 2·4·8분 ... 최대 10분. 반환: 기다릴 이유, 아니면 None"""
        rej = [i for i in self.store.of(self.date(), code, stage) if i["status"] == REJECTED]
        n = len(rej)
        if n < RETRY_FREE:
            return None
        gap = min(2 ** (n - RETRY_FREE + 1), RETRY_MAX_GAP)
        last = pd.Timestamp(f"{self.date()} {str(rej[-1].get('created', '00:00:00'))[:8]}")
        if self.clock() < last + pd.Timedelta(minutes=gap):
            return f"거부 {n}회 -> {gap}분 간격 (다음 {last + pd.Timedelta(minutes=gap):%H:%M})"
        return None

    def duty_sell(self, stage, c, h, kind, reason, assumed_kind, window, retry_now, extra=None):
        """매도 의무 한 번 (손절·추세 매도·늦은 추세 매도 같은 경로, 11J 보완 3). h = {file_qty: 보유 파일 수량, acct: 같은 분 계좌 {qty, sellable} 또는 None}
        (도구·시험의 {qty, sellable} 은 계좌 보유로 봄). 계좌 조회 됨: 매도 가능 수량 (증권사가 걸린 매도를 이미 뺀 값, 0 이면 그 분은 안 보냄).
        계좌 조회 실패: 오늘 이 종목에 접수·UNKNOWN·SENDING 매도가 하나도 없을 때만 보유 파일 수량 (초과분은 증권사 거부로 끝남)"""
        if "acct" not in h and "file_qty" not in h:
            h = {"file_qty": int(h.get("qty", 0)), "acct": None if h.get("no_acct") else h}
        a = h.get("acct")
        if a is not None:
            q = int(a.get("sellable") or 0) if int(a.get("qty") or 0) > 0 else 0
            if q <= 0:
                return {"skipped": f"계좌 매도 가능 0 (보유 {int(a.get('qty') or 0)}주) — 걸린 매도 주문 대기", "code": c}
            basis, before = "계좌 매도 가능 수량", int(a["qty"])
        else:
            blk = self.store.blocking(self.date(), c, side="sell")
            if blk is not None:
                return {"skipped": f"계좌 조회 실패, 오늘 걸린 매도 {blk['intent_id']} ({blk['status']}) — 보내지 않음", "code": c}
            q = before = int(h.get("file_qty") or 0)
            if q <= 0:
                return {"skipped": "계좌 조회 실패, 보유 파일 수량 0 — 보내지 않음", "code": c}
            basis = "보유 파일 수량 (계좌 조회 실패)"
        if MAX_ATTEMPTS[stage] is None:
            wait = self.retry_wait(stage, c)
            if wait:
                return {"skipped": wait, "code": c}
        ext = {"held_qty_before": before, "pre_qty": before, "qty_basis": basis, **(extra or {})}
        return self._send(stage, "sell", c, q, None, "3", kind, reason + ")", assumed_kind, lambda: self.b.sell_market(c, q),
                          retry_now=retry_now, window=window, extra=ext, blocking=False)

    def detect_stops(self, held, stops, at=None):
        """새 손절 감지: 현재가 < 손절가 (백테스트 저가 < 손절가 와 같게 미만). 감지 창(stop_from ~ stop_until 의 분, 15:20 포함)은
        at = 이 감시가 시작된 시각으로 판단 (조회가 늦어 지금 시각이 15:21 이어도 15:20 에 시작한 감시면 감지, 11J 보완 3). 반환 {code: 기록}"""
        at = at or self.hms()
        if not (self.t["stop_from"] <= at and at[:5] <= self.t["stop_until"][:5]):
            return {}
        out = {}
        for c, h in held.items():
            s = stops.get(c)
            if s is not None and h["cur_price"] == h["cur_price"] and h["cur_price"] < s:
                out[c] = {"since": f"{self.date()} {at}", "price": float(h["cur_price"]), "stop": float(s)}
        return out

    def send_duties(self, duties, inputs, kind_of=lambda c: "본래"):
        """09:00 ~ 15:29 매도 의무 시장가 (15:20 뒤는 마감 동시호가 = 종가 체결). duties = {code: 의무 표시(new_duty)}, inputs = duty_inputs.
        손절 진행 중(kind stop) = 단계 stop, 추세 매도(kind trend) = 단계 trend_late ("시가 매도 아님", 08:30 ~ 08:59 에 못 판 것·남은 수량).
        거부 횟수 상한 없음 (retry_wait 로 간격만 늘림). 한 종목은 한 분에 한 번"""
        now = self.hms()
        if not (self.t["stop_from"] <= now < self.t["stop_send_until"]):
            return []
        win = (self.t["stop_from"], self.t["stop_send_until"], False)
        auction = {"closing_auction": True} if now >= self.t["stop_until"] else {}
        out = []
        for c, du in duties.items():
            h = inputs.get(c)
            if h is None:
                continue
            if du.get("kind", "stop") == "stop":
                seen = " ".join(x for x in (du.get("since"), du.get("at")) if x)
                why = du.get("reason") or (f"현재가 {du['price']:.0f} < 손절가 {du['stop']:.0f}" if du.get("price") else "")
                out.append(self.duty_sell("stop", c, h, kind_of(c), f"손절 (감지 {seen} {why}, 지금 {now}", "stop", win, False,
                                          extra={"stop_price": du.get("stop"), "first_seen": {"time": du.get("at"), "price": du.get("price")}, **auction}))
            else:
                out.append(self.duty_sell("trend_late", c, h, kind_of(c), f"추세 매도 늦음 — 시가 매도 아님 ({du.get('reason')}, {now} 장중 시장가",
                                          "open", win, False, extra={"not_open": True, **auction}))
        return out

    def stop_check(self, held, stops, kind_of=lambda c: "본래", triggered=None, detect=True, at=None):
        """감지 + 보내기 한 번에 (도구·시험용). held = {code: {qty, sellable, cur_price}} (계좌 보유로 봄).
        triggered: 매도 의무 {code: 표시} (새로 감지한 것을 넣음, 없으면 이번 호출 안에서만)"""
        triggered = triggered if triggered is not None else {}
        if detect:
            new = {c: r for c, r in self.detect_stops(held, stops, at=at).items() if c not in triggered}
            for c, r in new.items():
                self.notify(f"[손절 진행 중] {c} 현재가 {r['price']:.0f} < 손절가 {r['stop']:.0f} — 팔릴 때까지 매 분 시장가 (가격이 반등해도)")
                triggered[c] = new_duty("stop", r["since"], f"현재가 {r['price']:.0f} < 손절가 {r['stop']:.0f}", price=r["price"], stop=r["stop"])
        return self.send_duties(triggered, held, kind_of)

    def trend_sells(self, targets, held, kind_of=lambda c: "본래"):
        """targets: 다음 날 시가 매도 목록 (추세 매도·전날 남은 매도 의무). 08:30 ~ 08:59 시장가, 거부면 1초 뒤 1회.
        held = duty_inputs (도구·시험은 계좌 보유 {qty, sellable}). 못 판 것·남은 수량은 09:00 이후 send_duties 가 맡음"""
        now = self.hms()
        if not (self.t["trend_sell_from"] <= now <= self.t["trend_sell_until"]):
            return []
        out = []
        for c in dict.fromkeys(list(targets)):
            h = held.get(c)
            if not h:
                continue
            out.append(self.duty_sell("trend_sell", c, h, kind_of(c), "추세 매도 (전날 종가 < 20일선, 또는 전날 남은 매도", "open",
                                      (self.t["trend_sell_from"], self.t["trend_sell_until"], True), True))
        return out

    # ---------------------------------------------------------- 15:35 확인 (랜덤엔드)

    def _read_retry(self, fn, kind, n=3, wait=2.0):
        """조회 다시 시도 (B1). 필드 확인까지 (F-04)"""
        last = None
        for i in range(n):
            try:
                return check_fields(fn(), kind)
            except FieldError:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                self.sleep(wait)
        raise NoResponse(f"{kind} 조회 {n}회 실패: {last!r}"[:200])

    def _cancel(self, r, remain, oso_rows):
        """취소도 의도로 기록 (SENDING -> ACCEPTED / REJECTED / UNKNOWN). 같은 원주문에 ACCEPTED·UNKNOWN·SENDING 취소가 있으면 다시 안 보냄.
        거부된 취소는 정산을 다시 돌릴 때 2회까지 다시 보낸다"""
        d = self.date()
        prev = [i for i in self.records if i["stage"] == "cancel" and i.get("orig_ord_no") == r["ord_no"]]
        if any(i["status"] in BLOCKING for i in prev) or len(prev) >= 2:
            return prev[-1]
        it = self.store.add(date=d, stage="cancel", side="cancel", code=r["code"], qty=int(remain), price=None, trde_tp="cancel",
                            attempt=len(prev) + 1, kind=r.get("kind"), reason="15:35 미체결 잔량 취소", assumed_kind="cancel",
                            orig_ord_no=r["ord_no"])
        res = self.b.cancel(r["ord_no"], r["code"], 0)
        st = res["outcome"] if res["outcome"] in (ACCEPTED, REJECTED, UNKNOWN, NOT_SENT) else UNKNOWN
        self.store.update(it, status=st, ord_no=res.get("ord_no"), return_code=res.get("return_code"), return_msg=res.get("msg"), cause=res.get("cause"))
        if st != ACCEPTED:
            self.notify(f"[취소 {st}] {r['code']} 원주문 {r['ord_no']}: {res.get('msg') or res.get('cause')}")
        return it

    def confirm_cancels(self, oso_rows):
        """UNKNOWN 취소: 원주문이 미체결 조회에 없거나 잔량 0 이면 취소된 것으로 확정 (ACCEPTED), 아직 남아 있으면 UNKNOWN 유지·알림"""
        live = {str(x.get("ord_no", "")): int(num(x.get("oso_qty"))) for x in oso_rows}
        n_unknown = 0
        for it in self.records:
            if it["stage"] != "cancel" or it["status"] != UNKNOWN:
                continue
            if live.get(str(it["orig_ord_no"]), 0) <= 0:
                self.store.update(it, status=ACCEPTED, matched_by="원주문이 미체결 조회에 없음 (취소 확인)")
            else:
                n_unknown += 1
                if it["intent_id"] not in self.alerted_unresolved:
                    self.alerted_unresolved.add(it["intent_id"])
                    self.notify(f"[취소 확인 안 됨] {it['code']} 원주문 {it['orig_ord_no']} 아직 미체결 {live[str(it['orig_ord_no'])]}주")
        return n_unknown

    def close_check(self, close_prices=None, open_prices=None):
        """15:35 이후: UNKNOWN 의도를 종목·수량으로 마지막 대조(없으면 NOT_SENT) -> 체결·미체결 조회(실패면 다시 시도, 필드 확인)로
        체결 수량·체결가·체결 시각. 매수 미체결 = "미체결 건너뜀" 후 남아 있으면 취소(취소도 의도 기록). 매도 미체결(하한가 잠김 등) = 취소 후
        다음 날 시가 재주문 목록. 다시 돌려도 같은 원주문 취소는 또 보내지 않는다. 반환 dict(done, cancel_unknown ...)"""
        if self.hms() < self.t["close_check"]:
            return {"done": False, "reason": f"{self.t['close_check']} 전 (랜덤엔드로 15:30 이후 체결될 수 있어 기다림)"}
        self.reconcile(final=True)
        fills = self._read_retry(self.b.fills, "fills")
        oso = self._read_retry(self.b.unfilled, "unfilled")
        by_no = {}
        for x in fills:
            by_no.setdefault(str(x.get("ord_no", "")), []).append(x)
        oso_by = {str(x.get("ord_no", "")): x for x in oso}
        for r in list(self.records):
            if r["status"] != ACCEPTED or not r.get("ord_no") or r["side"] not in ("buy", "sell"):
                continue
            upd = {}
            fx = by_no.get(r["ord_no"], [])
            q = sum(num(x.get("cntr_qty")) for x in fx)
            if q > 0:
                upd["filled_qty"] = int(q)
                upd["fill_price"] = float(sum(num(x.get("cntr_qty")) * num(x.get("cntr_pric")) for x in fx) / q)
                for k in FILL_TIME_KEYS:
                    if any(x.get(k) for x in fx):
                        upd["fill_time"] = max(str(x.get(k)) for x in fx if x.get(k))
                        upd["fill_time_field"] = k
                        break
            filled = upd.get("filled_qty", r.get("filled_qty") or 0)
            o = oso_by.get(r["ord_no"])
            remain = int(num(o.get("oso_qty"))) if o is not None else int(r["qty"]) - filled
            upd["unfilled_qty"] = max(remain, 0)
            if upd["unfilled_qty"] > 0:
                upd["fill_status"] = ("부분 체결, 나머지 미체결 건너뜀" if filled else "미체결 건너뜀") if r["side"] == "buy" else \
                                     ("부분 체결, 나머지 다음 날 시가 재주문" if filled else "미체결, 다음 날 시가 재주문")
                if o is not None and int(num(o.get("oso_qty"))) > 0:
                    c = self._cancel(r, int(num(o.get("oso_qty"))), oso)
                    upd["cancel"] = {"intent_id": c["intent_id"], "status": c["status"]}
                if r["side"] == "sell" and r["code"] not in self.next_open_sells:
                    self.next_open_sells.append(r["code"])
            elif filled >= r["qty"]:
                upd["fill_status"] = "체결"
            self.store.update(r, **upd)
            self._assumed(r, close_prices or {}, open_prices or {})
        oso2 = self._read_retry(self.b.unfilled, "unfilled")
        cancel_unknown = self.confirm_cancels(oso2)
        self.store.save()
        # Codex 13: 취소 거부·미확정 원주문·남은 잔량이 있으면 done=False (Resident 가 실패로 보고 다시 확인, 최종은 15:40 계좌 대조)
        mine = {r["ord_no"] for r in self.records if r["status"] == ACCEPTED and r.get("ord_no") and r["side"] in ("buy", "sell")}
        left = {str(x.get("ord_no", "")): int(num(x.get("oso_qty"))) for x in oso2 if str(x.get("ord_no", "")) in mine and num(x.get("oso_qty")) > 0}
        cancel_rejected = [i["intent_id"] for i in self.records if i["stage"] == "cancel" and i["status"] in (REJECTED, NOT_SENT)
                           and not any(j["stage"] == "cancel" and j.get("orig_ord_no") == i.get("orig_ord_no") and j["status"] == ACCEPTED for j in self.records)]
        # 3차 확인 리뷰 R7: 취소가 거부됐어도 그 원주문이 다시 조회한 미체결에 없거나 잔량 0 이면(다 체결·취소·소멸) 해결로 본다.
        # 남은 미체결 0, UNKNOWN 0 이면 done (보유 수량은 15:40 계좌 대조가 마지막 기준)
        cancel_resolved = [x for x in cancel_rejected if str(next(i for i in self.records if i["intent_id"] == x).get("orig_ord_no")) not in left]
        if cancel_resolved:  # 체결도 맞춰 둔다: 체결 조회를 다시 해 원주문 체결 수량을 채움 (실패면 해결로 안 봄, 다시 확인)
            try:
                self.refresh_fills()
            except Exception as e:  # noqa: BLE001
                self.notify(f"[취소 거부 확인] 원주문 잔량은 0 이지만 체결 조회 실패 -> 다시 확인: {e!r}"[:200])
                cancel_resolved = []
            for i in [i for i in self.records if i["intent_id"] in cancel_resolved]:
                if not i.get("resolved_by"):
                    self.store.update(i, resolved_by=f"{self.hms()} 원주문 남은 미체결 0 (미체결 조회에 없음), 체결 다시 조회")
                r = next((x for x in self.records if x.get("ord_no") == i.get("orig_ord_no") and x["side"] in ("buy", "sell")), None)
                if r is None:
                    continue
                f, q = int(r.get("filled_qty") or 0), int(r["qty"])
                if f >= q:
                    self.store.update(r, unfilled_qty=0, fill_status="체결")
                    if r["side"] == "sell" and r["code"] in self.next_open_sells:
                        self.next_open_sells.remove(r["code"])
                elif r["side"] == "buy":
                    self.store.update(r, unfilled_qty=q - f, fill_status="부분 체결, 나머지 미체결 건너뜀" if f else "미체결 건너뜀")
        cancel_rejected = [x for x in cancel_rejected if x not in cancel_resolved]
        unresolved = self.unresolved()
        reasons = []
        if cancel_unknown:
            reasons.append(f"취소 확인 안 됨 {cancel_unknown}건")
        if cancel_rejected:
            reasons.append(f"취소 거부 {len(cancel_rejected)}건")
        if left:
            reasons.append(f"남은 잔량 {left}")
        if unresolved:
            reasons.append(f"미확정 주문 {len(unresolved)}건")
        return {"done": not reasons, "reasons": reasons, "fills": len(fills), "unfilled": len(oso), "cancel_unknown": cancel_unknown,
                "cancel_rejected": cancel_rejected, "cancel_resolved": cancel_resolved, "left": left, "unresolved": len(unresolved)}

    def refresh_fills(self):
        """체결 조회(ka10076, 모든 페이지·필드 확인)로 접수된 매수·매도의 체결 수량·체결가·체결 시각을 다시 채운다 (확인 리뷰 N2).
        조회 실패면 NoResponse/FieldError (거래 정보 미확정). 반환: 체결 행 수"""
        fills = self._read_retry(self.b.fills, "fills")
        by_no = {}
        for x in fills:
            by_no.setdefault(str(x.get("ord_no", "")), []).append(x)
        for r in list(self.records):
            if r["status"] != ACCEPTED or not r.get("ord_no") or r["side"] not in ("buy", "sell"):
                continue
            fx = by_no.get(r["ord_no"], [])
            q = sum(num(x.get("cntr_qty")) for x in fx)
            r["fills_checked"] = self.clock().strftime("%H:%M:%S")  # 3차 확인 리뷰 R3: 체결 조회가 된 주문 = 체결 수량을 앎 (0 포함)
            if q > 0 and int(q) != int(r.get("filled_qty") or 0):
                upd = {"filled_qty": int(q), "fill_price": float(sum(num(x.get("cntr_qty")) * num(x.get("cntr_pric")) for x in fx) / q),
                       "fill_refreshed": self.clock().strftime("%H:%M:%S")}
                for k in FILL_TIME_KEYS:
                    if any(x.get(k) for x in fx):
                        upd["fill_time"], upd["fill_time_field"] = max(str(x.get(k)) for x in fx if x.get(k)), k
                        break
                self.store.update(r, **upd)
        self.store.save()
        return len(fills)

    def _assumed(self, r, close_prices, open_prices):
        """가정 체결가 (5장): 매수 = 정규장 종가 x 1.001, 손절 = 손절가 또는 시가(시가 < 손절가), 추세 매도 = 시가 x 0.999"""
        a = None
        if r["assumed_kind"] == "buy" and r["code"] in close_prices:
            a = close_prices[r["code"]] * 1.001
        elif r["assumed_kind"] == "stop" and r.get("stop_price") is not None:
            o = open_prices.get(r["code"])
            a = min(o, r["stop_price"]) if o is not None else r["stop_price"]
        elif r["assumed_kind"] == "open" and r["code"] in open_prices:
            a = open_prices[r["code"]] * 0.999
        if a is not None and r.get("fill_price"):
            r.update(assumed_price=float(a), diff_won=float(r["fill_price"] - a), diff_pct=float((r["fill_price"] / a - 1) * 100))


class _MemStore(IntentStore):
    """파일 없이 쓰는 의도 기록 (시험·도구용)"""

    def __init__(self, clock):
        self.path, self.clock, self.lock, self.items, self.corrupt = None, clock, threading.RLock(), [], None

    def save(self):
        pass
