"""
텔레그램 조회 명령 (EXEC_SPEC 11W A·M): /holdings /today /status /pnl /help (그 밖의 글은 /help 응답).

- 조회만. 상태를 바꾸는 명령 없음. 키움 새 호출 없음: 상주 프로그램이 이미 저장한 값(매 분 손절 감시의 계좌·시세, positions.json,
  오늘 주문 의도, 그날·지난 날 상태)으로만 답하고 값의 시각을 함께 적는다.
- 11W M: 장중 보유 = 매 분 손절 감시가 받은 계좌 수량 (15:40 정산 뒤는 정산 계좌, 계좌 조회 전이면 장부). 오늘 판 종목은 보유에서 빼고 "오늘 매도",
  그 대금은 현금 쪽 (체결 확인 전이면 추정). 현재가 = 오늘 시세 -> 오늘 계좌 -> 15:40 정산 계좌 현재가(오늘·지난 날) -> 일봉 마지막 종가, 끝내 모르면 "모름".
- .env 의 TELEGRAM_CHAT_ID 대화방에서 온 메시지만 답한다. 다른 곳은 무시하고 상주 로그에만.
- 수신은 별도 스레드(긴 폴링 getUpdates). 실패해도 주 반복·손절 감시·주문에 영향 없음 (예외는 이 스레드 안에서 로그만, 잠시 쉬고 다시).
  상태 잠금은 읽는 순간만 (복사한 뒤 놓고 글을 만든다).
- 15:17 ~ 15:22 (주문 계획 스냅샷·매수 시작 앞뒤, 거래시간 변경일은 함께 밂) 에 온 명령은 답을 미루고 15:22 뒤 보낸다.
- 보내는 글: 고정폭(<pre>, 숫자 정렬), 길면(텔레그램 4096자) 줄 단위로 나눠 여러 번. 고정폭 전송이 거부되면 같은 글을 일반 글로.
- 토큰은 로그·오류 글에 남기지 않는다 (scrub: 토큰 값과 bot<숫자>:<문자> 모양을 지움).
"""
import html
import json
import re
import threading
import time
import unicodedata

HOLD = ("15:17:00", "15:22:00")  # 답을 미루는 시간 (평소 시각표, 거래시간 변경일은 resident 가 그날 시각으로 바꿔 넘김)
MAX_AGE_MIN = 10                 # 이보다 오래된 메시지(재시작 전 쌓인 것)는 답하지 않음 (로그만)
POLL_TIMEOUT = 50                # 긴 폴링 대기 (초)
CHUNK = 3500                     # 한 번에 보내는 글 길이 상한 (텔레그램 4096자, <pre>·이스케이프 여유)
MONO = True                      # 고정폭(<pre>)으로 보냄 (숫자 정렬)
HELP = ("[조회 명령] 조회만 (주문·설정 바꾸는 명령 없음)\n"
        "값은 상주 프로그램이 이미 저장한 것 + 시각\n"
        "\n"
        "/holdings 보유 (장중은 매 분 계좌 기준)\n"
        "  수량·평균가·현재가·손절가까지·진입일\n"
        "  오늘 판 종목은 '오늘 매도' 로 따로\n"
        "/today 오늘 주문·체결·손절·추세 매도\n"
        "/status 하트비트·코드·env·주문·필터\n"
        "  매수 차단·수동 확인·데이터 버전\n"
        "/pnl 총자산·총손익·오늘·실현(누적)\n"
        "  보유 평가손익·전략/점검용\n"
        "/help 이 글")
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+|\b\d{6,}:[A-Za-z0-9_-]{20,}")
SENT = ("ACCEPTED", "UNKNOWN", "SENDING")  # 체결됐을 수 있는 의도 (REJECTED·NOT_SENT 는 체결 없음)
STAGE = {"buy": "매수", "stop": "손절", "trend_sell": "추세 매도", "trend_late": "추세 매도 (늦게)", "cancel": "취소"}


def scrub(text, token=""):
    """토큰 지우기 (requests 오류 글에는 주소 /bot<토큰>/... 가 들어감)"""
    s = str(text)
    if token:
        s = s.replace(token, "***")
    return _TOKEN_RE.sub("bot***", s)


def won(x):
    return "-" if x is None else f"{float(x):,.0f}"


def sgn(x):
    return "모름" if x is None else ("0" if round(float(x)) == 0 else f"{float(x):+,.0f}")


def pct(x):
    return "" if x is None else f"({float(x):+.2f}%)"


def kind_label(kind):
    return {"본래": "전략", None: "-"}.get(kind, kind)


def dw(s):
    """화면 폭 (한글 등 넓은 글자 2칸)"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(s))


def row(label, value, lw=16, vw=11, tail=""):
    """이름 + 오른쪽 맞춘 숫자 (고정폭에서 숫자 끝이 한 줄로)"""
    return f"{label}{' ' * max(1, lw - dw(label))}{str(value).rjust(vw)}" + (f" {tail}" if tail else "")


def hm(t):
    """'2026-11-09 09:43:12' / '09:43:12' -> '09:43'"""
    s = str(t or "")
    return s[-8:-3] if len(s) >= 8 else s


def ts(t):
    """'2026-11-08 16:41:00' -> '11-08 16:41' (없으면 -)"""
    s = str(t or "")
    return f"{s[5:10]} {s[11:16]}" if len(s) >= 16 else (s or "-")


def md(day):
    return str(day or "")[5:10]


# ---------------------------------------------------------------- 값 고르기 (v = resident.query_view() 가 복사한 값, 여기서는 읽기만)

def _is_today(v, x):
    """view 항목이 오늘 것인가 (날짜 없는 예전 값은 오늘로)"""
    return str((x or {}).get("d") or v["today"]) == v["today"]


def _day(v):
    d = v.get("day") or {}
    return d if str(d.get("date") or v["today"]) == v["today"] else {}


def _acct(v):
    a = (v.get("view") or {}).get("acct")
    return a if a and _is_today(v, a) else None


def price_of(v, c):
    """현재가와 출처: 오늘 손절 감시 시세 -> 오늘 그 분 계좌 현재가 -> 오늘 15:40 정산 계좌 현재가 -> 지난 15:40 정산·일봉 마지막 종가 (fallback_px).
    반환 (가격 또는 None, 출처 글, 종류 'quote'|'acct'|'settle'|'fallback'|None)"""
    q = (v.get("view") or {}).get("quotes", {}).get(c)
    if q and _is_today(v, q):
        return float(q["px"]), f"시세 {hm(q['t'])}", "quote"
    a = _acct(v)
    h = ((a or {}).get("holdings") or {}).get(c)
    if h and h.get("cur_price"):
        return float(h["cur_price"]), f"계좌 {hm(a.get('t'))}", "acct"
    st = _day(v).get("settlement") or {}
    if (st.get("cur_price") or {}).get(c):
        return float(st["cur_price"][c]), f"정산 {hm(st.get('time'))}", "settle"
    f = (v.get("fallback_px") or {}).get(c)
    if f and f.get("px"):
        return float(f["px"]), str(f.get("src") or "-"), "fallback"
    return None, "모름", None


def _recs(v):
    return [r for r in v.get("orders") or [] if r.get("date") == v["today"] and r.get("side") in ("buy", "sell")]


def _filled(r):
    return int(r.get("filled_qty") or 0) > 0 and bool(r.get("fill_price"))


def rate(v):
    """이 환경 수수료율 (값이 없으면 실전 0.015%)"""
    return float(v.get("commission") or 0.00015)


def _fee_tax(v):
    import positions as P
    from common import sb
    return rate(v), sb.SELL_TAX.get(int(v["today"][:4]), 0.002), P


def book(v):
    """지금 보유·오늘 매도·현금.
    보유 수량: 15:40 정산 뒤 = 정산 계좌, 그 전 = 매 분 손절 감시 계좌(오늘), 계좌가 아직 없으면 그날 시작 수량(없으면 장부) + 오늘 확정 체결.
    오늘 그 종목 의도가 모두 체결 확인됐으면(15:35 뒤) 시작 수량 + 확정 체결 (그 분 계좌가 동시호가 체결 전 값일 수 있음).
    오늘 매도 = 오늘 매도 의도가 있고 판 수량 > 0 (확인 전이면 시작 수량 - 계좌 수량, 대금은 현재가로 추정). 매수도 같게 (확인 전 = 계좌 - 시작).
    현금: 오늘 정산 D+2, 전이면 전 거래일 정산 D+2 + 오늘 확정 체결 ± 비용 + 확인 전 추정분 ± 비용"""
    fee, tax, P = _fee_tax(v)
    d = _day(v)
    pos = v.get("positions") or {}
    pheld = {c: p for c, p in (pos.get("held") or {}).items() if int(p.get("qty") or 0) > 0}
    base = v.get("baseline") or {}
    qs = (d.get("qty_start") or {}).get("held")
    if qs:
        start = {c: int(x.get("qty") or 0) for c, x in qs.items()}
    elif base:
        start = {c: int(p.get("qty") or 0) for c, p in base.items()}
    else:
        start = {c: int(p["qty"]) for c, p in pheld.items()}
    st = d.get("settlement") or {}
    a = _acct(v)
    if st.get("account") is not None:
        cur, avg = {c: int(q) for c, q in st["account"].items()}, dict(st.get("avg_price") or {})
        src, basis = "settle", f"15:40 정산 계좌 {hm(st.get('time'))}"
    elif a is not None:
        cur = {c: int(h.get("qty") or 0) for c, h in (a.get("holdings") or {}).items()}
        avg = {c: h.get("avg_price") for c, h in (a.get("holdings") or {}).items()}
        src, basis = "acct", f"계좌 {hm(a.get('t'))} (매 분 손절 감시)"
    else:
        cur, avg, src = None, {}, "book"
        basis = f"장부 (저장 {ts(pos.get('_saved_at'))}, 오늘 계좌 조회 전)"
    recs = [r for r in _recs(v) if r.get("status") in SENT or _filled(r)]
    by = {}
    for r in recs:
        by.setdefault(r["code"], []).append(r)
    codes = sorted(set(start) | set(pheld) | set(cur or {}) | set(by))
    held, sold, est = {}, {}, []
    for c in codes:
        rs = by.get(c, [])
        conf = bool(rs) and all(r.get("fill_status") is not None for r in rs)
        fb = sum(int(r.get("filled_qty") or 0) for r in rs if r["side"] == "buy" and _filled(r))
        fs = sum(int(r.get("filled_qty") or 0) for r in rs if r["side"] == "sell" and _filled(r))
        s0 = start.get(c, 0)
        if src == "settle":
            q = cur.get(c, 0)
        elif conf or cur is None:
            q = s0 + fb - fs
        else:
            q = cur.get(c, 0)
        sells = [r for r in rs if r["side"] == "sell"]
        buys = [r for r in rs if r["side"] == "buy"]
        meta = pheld.get(c) or base.get(c) or {}
        if sells:
            sq = fs if (conf or src == "settle" or cur is None) else max(fs, s0 + fb - q, 0)
            eq_ = max(sq - fs, 0)
            if sq > 0:
                px, psrc, _ = price_of(v, c)
                amt_c = sum(float(r["fill_price"]) * int(r["filled_qty"]) for r in sells if _filled(r))
                e = {"qty": sq, "conf_qty": fs, "est_qty": eq_, "amount": amt_c, "est_px": px if eq_ else None, "est_src": psrc if eq_ else None,
                     "stages": sorted({STAGE.get(r.get("stage"), r.get("stage")) for r in sells}), "kind": meta.get("kind") or sells[-1].get("kind"),
                     "avg": meta.get("entry_price") or meta.get("avg_price"), "pending": not conf and src != "settle"}
                rz_recs = [r for r in sells if _filled(r)]
                if eq_:
                    if px is None:
                        e["amount"] = None
                    else:
                        e["amount"] = amt_c + eq_ * px
                        est.append({"side": "sell", "code": c, "filled_qty": eq_, "fill_price": px, "kind": e["kind"]})
                        rz_recs.append(est[-1])
                ent = meta.get("entry_price") or meta.get("avg_price")
                e["realized"] = (None if (eq_ and px is None) or not ent
                                 else P.realized_pnl(rz_recs, {c: {"entry_price": ent}}, None, fee, int(v["today"][:4]))["pnl"])
                sold[c] = e
        if buys and not conf and src == "acct":
            bq = max(q - (s0 - (sold.get(c) or {}).get("qty", 0)) - fb, 0)  # 계좌에 보이는데 확인 안 된 매수
            if bq:
                bp = avg.get(c) or next((r.get("price") for r in buys if r.get("price")), None)
                if bp:
                    est.append({"side": "buy", "code": c, "filled_qty": bq, "fill_price": float(bp), "kind": buys[-1].get("kind")})
        if q > 0:
            h = dict(meta)
            h.update(qty=q, avg_price=avg.get(c) or meta.get("avg_price") or meta.get("entry_price"),
                     in_book=c in pheld, sell_pending=bool(sells) and c not in sold and not conf)
            if c not in pheld and buys:
                h.setdefault("kind", buys[-1].get("kind"))
                h["bought_today"] = True
            held[c] = h
    if st.get("d2") is not None:
        cash, cnote, cest = float(st["d2"]), f"오늘 정산 D+2 ({hm(st.get('time'))})", False
    elif v.get("prev_d2") is not None:
        fills = [r for r in recs if _filled(r)]
        cash = P.expected_cash(v["prev_d2"], fills, int(v["today"][:4]), commission=fee)[0]
        for r in est:
            amt = r["filled_qty"] * r["fill_price"]
            cash += (amt - P.kiwoom_fee(amt, fee) - P.kiwoom_tax(amt, tax)) if r["side"] == "sell" else -(amt + P.kiwoom_fee(amt, fee))
        cest = bool(est) or any(r.get("fill_status") is None for r in recs)
        cnote = (f"전 거래일({md(v.get('prev_day'))}) 정산 D+2 {won(v['prev_d2'])} + 오늘 체결 확인 {len(fills)}건"
                 + (f" + 확인 전 추정 {len(est)}건" if est else ""))
    else:
        cash, cnote, cest = None, "전 거래일 정산 기록 없음", False
    return {"held": held, "sold": sold, "est": est, "basis": basis, "src": src, "cash": cash, "cash_note": cnote, "cash_est": cest}


def valuation(v, b):
    """보유 평가: 주식 = 수량 x 현재가, 평가손익 = 수량 x (현재가 - 평균가). 현재가를 모르는 종목이 있으면 주식·평가손익 None (모름)"""
    val, unreal, miss, srcs, by_kind = 0.0, 0.0, [], set(), {}
    for c, p in b["held"].items():
        px, src, _ = price_of(v, c)
        if px is None:
            miss.append(c)
            continue
        srcs.add(src)
        val += p["qty"] * px
        u = p["qty"] * (px - float(p["avg_price"])) if p.get("avg_price") else 0.0
        unreal += u
        k = kind_label(p.get("kind"))
        by_kind[k] = by_kind.get(k, 0.0) + u
    if miss:
        return {"value": None, "unreal": None, "miss": miss, "srcs": srcs, "by_kind": {}}
    return {"value": val, "unreal": unreal, "miss": [], "srcs": srcs, "by_kind": by_kind}


def _trade_pnl(P, r, entry, fee, tax_year):
    return P.realized_pnl([r], {r["code"]: {"entry_price": entry}}, None, fee, tax_year)["pnl"]


def cumulative(v, b):
    """누적 실현손익 (v['since'] 부터 판 거래, 수수료·세금 포함): 지난 날 = 그날 의도 파일의 매도 체결 x (체결가 - 정산 기준 장부 진입가),
    오늘 = 확정 체결 + 확인 전 추정. 거래 = trade_id (없으면 종목·날짜) 단위로 승/패. 실전 수수료였다면 = 같은 계산을 실전 수수료로.
    fill_amount = 모든 체결 금액 합 (총손익의 실전 수수료 환산: 요약과 같은 체결 금액 x (이 환경 - 실전 수수료))"""
    import positions as P
    fee, prod = rate(v), v.get("prod_commission", 0.00015)
    trades, unknown, amt, n_days = {}, [], 0.0, 0

    def add(day, r, meta, est=False):
        entry = (meta or {}).get("entry_price") or (meta or {}).get("avg_price")
        if not entry:
            unknown.append(f"{r['code']}({md(day)})")
            return
        y = int(str(day)[:4])
        tid = (meta or {}).get("trade_id") or f"{r['code']}-{day}"
        t = trades.setdefault(tid, {"pnl": 0.0, "pnl_prod": 0.0, "kind": kind_label(r.get("kind") or (meta or {}).get("kind")), "est": False})
        t["pnl"] += _trade_pnl(P, r, entry, fee, y)
        t["pnl_prod"] += _trade_pnl(P, r, entry, prod, y)
        t["est"] = t["est"] or est
    for x in v.get("past") or []:
        n_days += 1
        base = x.get("baseline") or {}
        for r in x.get("records") or []:
            if not _filled(r):
                continue
            amt += float(r["fill_price"]) * int(r["filled_qty"])
            if r.get("side") == "sell":
                add(x["day"], r, base.get(r["code"]))
    pheld = (v.get("positions") or {}).get("held") or {}
    base = v.get("baseline") or {}
    for r in _recs(v):
        if not _filled(r):
            continue
        amt += float(r["fill_price"]) * int(r["filled_qty"])
        if r["side"] == "sell":
            add(v["today"], r, pheld.get(r["code"]) or base.get(r["code"]))
    for r in b["est"]:
        amt += r["filled_qty"] * r["fill_price"]
        if r["side"] == "sell":
            add(v["today"], r, pheld.get(r["code"]) or base.get(r["code"]), est=True)
    pnl = sum(t["pnl"] for t in trades.values())
    by_kind = {}
    for t in trades.values():
        by_kind[t["kind"]] = by_kind.get(t["kind"], 0.0) + t["pnl"]
    return {"pnl": round(pnl, 1), "pnl_prod": round(sum(t["pnl_prod"] for t in trades.values()), 1), "n": len(trades),
            "win": sum(1 for t in trades.values() if t["pnl"] > 0), "loss": sum(1 for t in trades.values() if t["pnl"] < 0),
            "est": any(t["est"] for t in trades.values()), "unknown": unknown, "by_kind": by_kind, "fill_amount": round(amt, 1),
            "fee_back": round(amt * (fee - prod), 1), "days": n_days}


def today_realized(v, b):
    """오늘 실현손익: 15:40 정산 뒤 = 정산 기록, 전 = 오늘 확정 매도 체결 + 확인 전 추정 (positions.realized_pnl). 반환 (값 dict 또는 None, 추정 여부)"""
    st = _day(v).get("settlement") or {}
    if st.get("realized") is not None:
        return st["realized"], False
    import positions as P
    held = (v.get("positions") or {}).get("held") or {}
    rz = P.realized_pnl([r for r in _recs(v) if _filled(r)] + [r for r in b["est"] if r["side"] == "sell"], held, v.get("baseline") or {},
                        rate(v), int(v["today"][:4]))
    return rz, any(r["side"] == "sell" for r in b["est"])


# ---------------------------------------------------------------- 글 만들기

def _price_head(v, val):
    srcs = sorted(val["srcs"])
    q = [s for s in srcs if s.startswith("시세 ")]
    if len(srcs) == 1:
        return srcs[0]
    if q:
        return max(q) + (" 등" if len(srcs) > 1 else "")
    return ", ".join(srcs) or "-"


def holdings_text(v):
    b = book(v)
    names = v.get("names") or {}
    duty = (v.get("positions") or {}).get("sell_duty") or {}
    val = valuation(v, b)
    head_src = _price_head(v, val) if b["held"] else "-"
    lines = [f"[보유] {len(b['held'])}종목 · {head_src}"]
    if not b["held"]:
        lines.append("보유 없음")
    for c in sorted(b["held"]):
        p = b["held"][c]
        px, src, _ = price_of(v, c)
        avg = p.get("avg_price")
        chg = f" ({(px / float(avg) - 1) * 100:+.1f}%)" if px and avg else ""
        stop = p.get("stop_price")
        gap = f"{(float(stop) / px - 1) * 100:+.1f}%" if px and stop else "모름"
        lines += ["", f"{names.get(c, '') or '-'} {c}",
                  f"  {p['qty']}주 · 평균 {won(avg)} → 현재 {won(px) if px else '모름'}{chg}" + (f" [{src}]" if px and src != head_src else ""),
                  f"  손절가 {won(stop)}까지 {gap}" if stop else "  손절가 없음"]
        flags = [k for k in ("stop_temp", "entry_temp", "info_pending", "ambiguous") if p.get(k)]
        rv = [k for k in ("qty_review", "base_review") if p.get(k)]
        extra = [f"진입 {md(p.get('entry_date')) or '-'}", kind_label(p.get("kind"))]
        if p.get("bought_today"):
            extra.append("오늘 매수")
        if p.get("sell_pending"):
            extra.append("매도 주문 중 (체결 전)")
        elif c in duty:
            extra.append("매도 의무")
        lines.append("  " + " · ".join(extra) + (f" · 표시 {flags}" if flags else "") + (f" · 수동 확인 {rv}" if rv else ""))
    if b["sold"]:
        lines += ["", f"[오늘 매도] {len(b['sold'])}종목"]
        for c in sorted(b["sold"]):
            s = b["sold"][c]
            lines += ["", f"{names.get(c, '') or '-'} {c}", f"  {s['qty']}주 · {'·'.join(s['stages'])} · {kind_label(s.get('kind'))}"
                      + (" · 체결 확인 전" if s["est_qty"] else "")]
            if s["est_qty"]:
                lines.append(f"  대금 약 {won(s['amount'])} (추정, {s['est_src']})" if s["amount"] is not None else "  대금 모름 (현재가 모름)")
            else:
                lines.append(f"  대금 {won(s['amount'])} (체결 평균 {won(s['amount'] / s['qty'])})")
            lines.append(f"  실현 {sgn(s['realized'])} (진입 {won(s['avg'])} 대비, 비용 포함" + (", 추정)" if s["est_qty"] else ")"))
    lines += ["", f"[기준: 보유 {b['basis']}" + (f", 현재가 모름 {val['miss']}" if val["miss"] else "") + f" · 답 {hm(v['now'])}]"]
    return "\n".join(lines)


def today_text(v):
    recs = [r for r in v.get("orders") or [] if r.get("date") == v["today"]]
    names = v.get("names") or {}
    lines = [f"[오늘 주문] {md(v['today'])} {len(recs)}건 · 답 {hm(v['now'])}"]
    if not recs:
        lines.append("주문 없음")
    for r in recs:
        fill = f"체결 {r['filled_qty']}주 @{won(r.get('fill_price'))}" if r.get("filled_qty") else (r.get("fill_status") or "체결 확인 전")
        t = str(r.get("order_time") or r.get("created") or r.get("time") or "")[:8]  # 주문 시각 (HH:MM:SS.mmm)
        lines += ["", f"{t or '-'} {STAGE.get(r.get('stage'), r.get('stage'))} {kind_label(r.get('kind'))}",
                  f"  {names.get(r.get('code'), '') or '-'} {r.get('code')} {r.get('qty')}주"
                  + (f" @{won(r['price'])}" if r.get("price") else " 시장가" if r.get("side") == "sell" else ""),
                  f"  {r.get('status')} · {fill}"]
    lines += ["", "[기준: 오늘 주문 의도 기록]"]
    return "\n".join(lines)


def status_text(v):
    d = _day(v)
    hb = v.get("heartbeat") or "없음"
    live = d.get("live") or {}
    f = live.get("filter") or {}
    filt = ("통과" if f.get("filter_ok") else "미통과") if f else "아직 모름 (스냅샷 전)"
    bd = d.get("buy_decision")
    if bd:
        buy = f"{'가능' if bd.get('new_buy_ok') else '안 함'} ({hm(bd.get('time'))}: {bd.get('reason') or '-'})"
    else:
        tags = d.get("buy_block") or []
        buy = (f"차단 {tags}" if tags else "차단 없음") + ("" if v.get("buy_allowed") else " · 준비 확인 전 또는 장애로 매수 안 함") + " (매수 판단 전)"
    pos = v.get("positions") or {}
    rv = {c: [k for k in ("qty_review", "base_review") if p.get(k)] for c, p in (pos.get("held") or {}).items() if v.get("in_review", {}).get(c)}
    cs = (d.get("core_starts") or [None])[-1]
    pr = v.get("proc") or {}
    # 11W M 보완: 그날 15:40 정산이 끝났으면 "장 마감 후", 장 마감 뒤 재시작(그날 상태를 파일에서 읽음)이면 지금 상주 설정
    closed = (f"장 마감 후 ({md(v['today'])} 정산 끝)" if (d.get("settlement") or {}).get("time")
              else "장 마감 후 (정산 기록 없음)" if v.get("day_from_file") and d.get("steps") and not d["steps"].get("holiday") else None)
    if cs and not v.get("day_from_file"):
        code, env, orders = cs.get("code"), cs.get("env") or pr.get("env"), cs.get("orders_enabled")
        when = f"하루 시작 {hm(cs.get('time'))}" + (f" · {closed}" if closed else "")
    else:
        code, env, orders, cs = pr.get("code"), pr.get("env"), pr.get("orders"), None
        when = (f"{closed} · " if closed else "휴장일 · " if d.get("steps", {}).get("holiday") else "하루 시작 전 · ") \
            + f"상주 설정 (시작 {pr.get('started') or '-'})"
    lines = [f"[상태] {md(v['today'])} {hm(v['now'])}",
             f"하트비트 {hb}",
             "",
             f"코드  {code or '-'}",
             f"env   {env or '-'} · 주문 {'켬' if orders else '끔'}" + (f" ({pr['orders_note']})" if not cs and pr.get("orders_note") else ""),
             f"      ({when})",
             "",
             f"필터  {filt}"]
    if f:
        lines.append(f"      코스피 {f.get('prev_kospi_close')} / 120일선 {f.get('prev_kospi_ma120')}")
    lines += [f"신규 매수 {buy}",
              f"수동 확인 {rv or '없음'}",
              f"데이터 버전 {v.get('data_version') or '-'}",
              f"오류 {len(d.get('errors') or [])} (해결 {len({r.get('i') for r in d.get('errors_resolved') or []})})"]
    return "\n".join(lines)


def pnl_text(v):
    """총자산 = 현금 + 보유 x 현재가. 총손익 = 총자산 - 시작 금액. 오늘 = 전 거래일 정산 총자산 대비. 실현(누적) = since 부터 판 거래"""
    b = book(v)
    val = valuation(v, b)
    cash = b["cash"]
    eq = None if cash is None or val["value"] is None else cash + val["value"]
    cap = float(v.get("capital") or 30_000_000)
    tot = None if eq is None else eq - cap
    cu = cumulative(v, b)
    rz, rz_est = today_realized(v, b)
    prev_eq = v.get("prev_equity")
    lines = [f"[손익] {md(v['today'])} {hm(v['now'])}",
             row("총자산", won(eq) if eq is not None else "모름"),
             row("  현금", won(cash) if cash is not None else "모름", tail="(추정)" if b["cash_est"] else ""),
             row("  주식", won(val["value"]) if val["value"] is not None else "모름"),
             row("총손익", sgn(tot), tail=pct(None if tot is None else tot / cap * 100)),
             row("  실전 수수료면", sgn(None if tot is None else tot + cu["fee_back"]),
                 tail=pct(None if tot is None else (tot + cu["fee_back"]) / cap * 100)),
             row("오늘", sgn(None if eq is None or prev_eq is None else eq - float(prev_eq))),
             row("오늘 실현", sgn(None if rz is None else rz.get("pnl")), tail="(추정 포함)" if rz_est else ""),
             row("실현(누적)", sgn(cu["pnl"]), tail=f"({cu['win']}승 {cu['loss']}패" + (", 추정 포함" if cu["est"] else "") + ")"),
             row("  실전 수수료면", sgn(cu["pnl_prod"])),
             row("보유 평가손익", sgn(val["unreal"])),
             (f"{_price_head(v, val)} 기준" if b["held"] else "보유 없음") + (f" (현재가 모름 {val['miss']})" if val["miss"] else "")]
    kinds = sorted(set(cu["by_kind"]) | set(val["by_kind"]))
    if len(kinds) > 1:
        lines += ["", row("구분", "전략", vw=10) + "점검용".rjust(10),
                  row("실현(누적)", sgn(cu["by_kind"].get("전략", 0)), vw=10) + sgn(cu["by_kind"].get("점검용", 0)).rjust(10),
                  row("평가손익", sgn(val["by_kind"].get("전략", 0) if val["unreal"] is not None else None), vw=10)
                  + sgn(val["by_kind"].get("점검용", 0) if val["unreal"] is not None else None).rjust(10)]
    elif kinds:
        lines += ["", f"구분: 전부 {kinds[0]}"]
    if b["sold"]:
        lines.append(f"오늘 매도 {len(b['sold'])}종목 대금은 현금 쪽" + (" (체결 확인 전 추정)" if any(s["est_qty"] for s in b["sold"].values()) else ""))
    notes = [f"현금 {b['cash_note']}", f"보유 {b['basis']}",
             f"오늘 = 전 거래일 정산 총자산 {won(prev_eq)} 대비", f"총손익 = 시작 {won(cap)} 대비",
             f"실현 = {v.get('since') or '-'} 부터 {cu['n']}거래 진입가 대비, 수수료·세금 포함 (수수료 {rate(v) * 100:.3f}%, 실전 {v.get('prod_commission', 0.00015) * 100:.3f}%)",
             f"총손익 실전 수수료면 = 체결 금액 {won(cu['fill_amount'])} x 수수료 차이 되돌림"]
    if cu["unknown"]:
        notes.append(f"진입가 모름 {cu['unknown'][:10]} (실현에서 뺌)")
    lines += ["", "[" + " / ".join(notes) + "]"]
    return "\n".join(lines)


def reply(cmd, v):
    fn = {"/holdings": holdings_text, "/today": today_text, "/status": status_text, "/pnl": pnl_text}.get(cmd)
    return fn(v) if fn else HELP


def chunks(text, size=CHUNK):
    """줄 단위로 size 안에서 나눔 (빈 줄 = 종목 사이에서 먼저 자름). 한 줄이 size 보다 길면 그 줄을 자름"""
    out, cur = [], ""
    for ln in str(text).split("\n"):
        while len(ln) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(ln[:size])
            ln = ln[size:]
        if cur and len(cur) + 1 + len(ln) > size:
            out.append(cur.rstrip("\n"))
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur or not out:
        out.append(cur)
    return out


# ---------------------------------------------------------------- 수신 스레드

class Bot:
    """긴 폴링 수신 스레드. view() = 지금 값 복사 (resident.query_view), log(msg, level) = 상주 로그, hold() = 답을 미루는 (시작, 끝) HH:MM:SS.
    http_get·http_post·clock·epoch·sleep 은 시험에서 바꿈 (기본 requests·한국 시간·time.time·stop 이벤트 대기).
    clock = 한국 벽시계 (답 미루는 시간·표시), epoch = 메시지 나이 (message.date 와 비교)"""

    def __init__(self, token, chat, view, log, hold=lambda: HOLD, clock=None, http_get=None, http_post=None, sleep=None, epoch=None):
        from common import now_kst
        self.token, self.chat = str(token or ""), str(chat or "").strip()
        self.view, self.log, self.hold = view, log, hold
        self.clock = clock or now_kst
        self.epoch = epoch or time.time
        self.sleep = sleep or (lambda sec: self.stop.wait(sec))
        self.http_get, self.http_post = http_get, http_post
        self.offset, self.stop, self.thread = None, threading.Event(), None
        self.fail = 0
        self.answered = 0

    def _get(self, method, params, timeout):
        if self.http_get is not None:
            return self.http_get(method, params, timeout)
        import requests
        r = requests.get(f"https://api.telegram.org/bot{self.token}/{method}", params=params, timeout=timeout)
        return r.json()

    def _post(self, method, data):
        if self.http_post is not None:
            return self.http_post(method, data)
        import requests
        r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}", data=data, timeout=10)
        return r.json()

    def start(self):
        self.thread = threading.Thread(target=self.loop, daemon=True, name="telegram-cmd")
        self.thread.start()
        return self.thread

    def loop(self):
        while not self.stop.is_set():
            try:
                self.poll_once()
                self.fail = 0
            except Exception as e:  # noqa: BLE001  (이 스레드 밖으로 나가지 않음)
                self.fail += 1
                if self.fail in (1, 10) or self.fail % 100 == 0:
                    self.log(f"텔레그램 조회 명령 수신 실패 {self.fail}번째: {scrub(repr(e), self.token)}"[:300], "WARN")
                self.stop.wait(min(5 * self.fail, 60))

    def poll_once(self, timeout=POLL_TIMEOUT):
        params = {"timeout": timeout, "allowed_updates": json.dumps(["message"])}
        if self.offset is not None:
            params["offset"] = self.offset
        j = self._get("getUpdates", params, timeout + 15)
        if not j.get("ok"):
            raise RuntimeError(f"getUpdates ok=false: {str(j.get('description'))[:100]}")
        for u in j.get("result") or []:
            self.offset = int(u["update_id"]) + 1
            self.handle(u)
        return len(j.get("result") or [])

    def in_hold(self):
        a, b = self.hold()
        return a <= self.clock().strftime("%H:%M:%S") < b

    def send(self, body):
        """글 보내기: 길면 나눠서, 고정폭(<pre>, HTML 이스케이프). 고정폭이 거부되면 그 조각을 일반 글로. 반환 모두 ok 인가"""
        ok = True
        for part in chunks(body):
            r = None
            if MONO:
                r = self._post("sendMessage", {"chat_id": self.chat, "text": f"<pre>{html.escape(part, quote=False)}</pre>", "parse_mode": "HTML"})
            if not MONO or not (r or {}).get("ok"):
                r = self._post("sendMessage", {"chat_id": self.chat, "text": part})
            ok = ok and bool((r or {}).get("ok"))
        return ok

    def handle(self, u):
        m = u.get("message") or {}
        chat = str((m.get("chat") or {}).get("id", ""))
        text = str(m.get("text") or "").strip()
        if not self.chat or chat != self.chat:
            self.log(f"텔레그램 조회 명령: 다른 대화방 메시지 무시 (chat {chat}, 글 {scrub(text, self.token)[:40]!r})", "WARN")
            return
        age = (self.epoch() - float(m.get("date") or 0)) / 60  # 둘 다 epoch (date 는 UTC epoch, 한국 시계의 시간대 없는 값 .timestamp() 는 9시간 앞섬)
        if age > MAX_AGE_MIN:
            self.log(f"텔레그램 조회 명령: {age:.0f}분 지난 메시지 답 안 함 ({scrub(text, self.token)[:40]!r})", "INFO")
            return
        cmd = text.split()[0].split("@")[0].lower() if text else ""
        late = None
        while self.in_hold() and not self.stop.is_set():  # 15:17 ~ 15:22: 답을 미룸 (끝난 뒤 그때 값으로)
            late = late or self.clock().strftime("%H:%M:%S")
            self.sleep(1.0)
        try:
            body = reply(cmd, self.view())
        except Exception as e:  # noqa: BLE001
            body = f"[조회 오류] {cmd}: {scrub(repr(e), self.token)}"[:300]
            self.log(body, "WARN")
        if late:
            a, b = self.hold()
            body += f"\n({late} 요청, {a[:5]} ~ {b[:5]} 에는 답을 미룸)"
        ok = self.send(body)
        self.answered += 1
        self.log(f"텔레그램 조회 명령 {cmd or '(글)'} 답 {'보냄' if ok else '보내지 못함'}", "INFO")
