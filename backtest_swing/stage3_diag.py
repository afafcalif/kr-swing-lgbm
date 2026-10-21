"""
3단계 진단 (선택·판정에는 쓰지 않고 보고만): 신호 뒤 1~60거래일 "그냥 들고 있었다면" 수익률 곡선

- 대상: 탐지기 눌림·돌파 신호 각각, 시장 필터 없음 / 있음 따로. 손익비 필터는 손절 여유폭마다 달라서 적용하지 않는다
  (진입 가능 조건만: 진입일 거래량 > 0, 상한가 마감 아님, 개발 구간 안).
- 수익률: 진입가(진입일 종가) 대비 h 거래일 뒤 종가, 비용·손절·익절·추세 청산 없음. h 거래일 뒤가 개발 구간 밖이면 그 h 에서 뺀다.
- 무작위 진입: 대조 1 과 같은 후보(대상 종목, 그날 종가 아래 탐지기 지지선이 있는 종목·날짜). 같은 날짜끼리 비교하려고
  날짜마다 후보 평균을 구한 뒤 그날 신호 수로 가중한다 (중앙값은 같은 가중의 가중 중앙값).
- 차이 = 신호 평균 − 같은 날 무작위 평균. 95% 범위는 날짜 단위 부트스트랩 1,000회.

결과: results/stage3_v2/diag_forward.csv, results/stage3_v2/diag_forward.html, 표는 diag_forward.md
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage3_backtest as sb  # noqa: E402

H, B = 60, 1000
OUT = sb.OUT_RUN


def forward(g, T):
    idx = T[:, None] + np.arange(1, H + 1)[None, :]
    valid = idx <= g.dev_last[T][:, None]
    r = g.c[np.minimum(idx, len(g.c) - 1)] / g.c[T][:, None] - 1
    return np.where(valid, r, np.nan)


def wquantile(x, w, q):
    m = ~np.isnan(x)
    x, w = x[m], w[m]
    if len(x) == 0:
        return np.nan
    o = np.argsort(x)
    cw = np.cumsum(w[o])
    return x[o][np.searchsorted(cw, q * cw[-1])]


def analyse(g, Tsig, Tpool, rng):
    Rs, Rp = forward(g, Tsig), forward(g, Tpool)
    ds, dp = g.cal[Tsig], g.cal[Tpool]
    dates = np.unique(ds)
    pos = {d: i for i, d in enumerate(dates)}
    D = len(dates)
    S = np.zeros((D, H)); N = np.zeros((D, H))
    for i, d in enumerate(ds):
        k = pos[d]
        v = ~np.isnan(Rs[i])
        S[k, v] += Rs[i, v]; N[k, v] += 1
    PM = np.full((D, H), np.nan)
    pool_by_date = pd.Series(np.arange(len(dp))).groupby(dp).apply(np.array)
    w_pool = np.zeros(len(dp))
    for d, rows in pool_by_date.items():
        if d not in pos:
            continue
        k = pos[d]
        PM[k] = np.nanmean(Rp[rows], axis=0) if len(rows) else np.nan
        w_pool[rows] = N[k].max() / len(rows)
    have = ~np.isnan(PM) & (N > 0)
    Sx, Nx, PMx = np.where(have, S, 0), np.where(have, N, 0), np.where(have, PM, 0)
    mean_sig = Sx.sum(0) / Nx.sum(0)
    mean_rnd = (Nx * PMx).sum(0) / Nx.sum(0)
    diff = mean_sig - mean_rnd
    W = rng.multinomial(D, np.full(D, 1 / D), size=B).astype(float)
    num = W @ (Sx - Nx * PMx)
    den = W @ Nx
    boot = num / den
    lo, hi = np.nanquantile(boot, 0.025, axis=0), np.nanquantile(boot, 0.975, axis=0)
    med_sig = np.nanmedian(Rs, axis=0)
    med_rnd = np.array([wquantile(Rp[:, h], w_pool, 0.5) for h in range(H)])
    return pd.DataFrame({"h": np.arange(1, H + 1), "n_signals": Nx.sum(0).astype(int), "mean_signal": mean_sig * 100,
                         "mean_random": mean_rnd * 100, "diff": diff * 100, "diff_lo": lo * 100, "diff_hi": hi * 100,
                         "median_signal": med_sig * 100, "median_random": med_rnd * 100})


def svg_panel(df, title, w=460, h=260):
    pad_l, pad_r, pad_t, pad_b = 44, 12, 26, 28
    ys = np.concatenate([df.mean_signal, df.mean_random, df.median_signal, df.median_random, df.diff_lo, df.diff_hi])
    ymin, ymax = float(np.nanmin(ys)), float(np.nanmax(ys))
    span = max(ymax - ymin, 1e-6)
    ymin, ymax = ymin - 0.08 * span, ymax + 0.08 * span
    X = lambda x: pad_l + (x - 1) / (H - 1) * (w - pad_l - pad_r)
    Y = lambda y: pad_t + (ymax - y) / (ymax - ymin) * (h - pad_t - pad_b)
    pl = lambda col, color, dash="", width=2: (
        f'<polyline fill="none" stroke="{color}" stroke-width="{width}" stroke-dasharray="{dash}" points="'
        + " ".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in zip(df.h, df[col]) if np.isfinite(b)) + '"/>')
    band = ('<polygon fill="var(--band)" stroke="none" points="'
            + " ".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in zip(df.h, df.diff_hi))
            + " " + " ".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in zip(df.h[::-1], df.diff_lo[::-1])) + '"/>')
    ticks = np.linspace(ymin, ymax, 5)
    grid = "".join(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{Y(t):.1f}" y2="{Y(t):.1f}" stroke="var(--grid)"/>'
                   f'<text x="{pad_l - 6}" y="{Y(t) + 4:.1f}" text-anchor="end">{t:.1f}</text>' for t in ticks)
    xt = "".join(f'<text x="{X(x):.1f}" y="{h - 8}" text-anchor="middle">{x}</text>' for x in (1, 10, 20, 30, 40, 50, 60))
    zero = f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" stroke="var(--fg)" stroke-width="1"/>' if ymin < 0 < ymax else ""
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{title}"><text x="{pad_l}" y="16" class="t">{title}</text>'
            f'{grid}{xt}{zero}{band}{pl("diff", "var(--diff)", width=2.5)}{pl("mean_random", "var(--rnd)")}'
            f'{pl("median_random", "var(--rnd)", "4 3", 1.5)}{pl("mean_signal", "var(--sig)")}{pl("median_signal", "var(--sig)", "4 3", 1.5)}</svg>')


def main():
    g = sb.G()
    z = g.z
    rng = np.random.default_rng(11)
    rS = z["rnd_S"]
    zeros = lambda n: np.zeros(n)
    nans = lambda n: np.full(n, np.nan)
    results, panels, md = [], [], []
    for typ, name in ((0, "눌림"), (1, "돌파")):
        T = z["det_T"][z["det_type"] == typ]
        for filt in (False, True):
            ok = sb.entry_ok(g, T, zeros(len(T)), nans(len(T)), filt)
            pok = sb.entry_ok(g, rS, zeros(len(rS)), nans(len(rS)), filt)
            df = analyse(g, T[ok], rS[pok], rng)
            label = f"{name}, 시장 필터 {'있음' if filt else '없음'}"
            df.insert(0, "set", label)
            results.append(df)
            panels.append(svg_panel(df, f"{label} (신호 {int(ok.sum()):,}건)"))
            sel = df[df.h.isin([1, 5, 10, 20, 40, 60])]
            md.append(f"### {label}\n\n| h | 신호 평균 | 무작위 평균 | 차이 [95%] | 신호 중앙값 | 무작위 중앙값 |\n|---|---|---|---|---|---|\n"
                      + "".join(f"| {r.h} | {r.mean_signal:+.2f}% | {r.mean_random:+.2f}% | {r.diff:+.2f} [{r.diff_lo:+.2f} ~ {r.diff_hi:+.2f}] | "
                                f"{r.median_signal:+.2f}% | {r.median_random:+.2f}% |\n" for r in sel.itertuples()))
    allr = pd.concat(results)
    allr.to_csv(os.path.join(OUT, "diag_forward.csv"), index=False, encoding="utf-8-sig")
    with open(os.path.join(OUT, "diag_forward.md"), "w", encoding="utf-8") as f:
        f.write("# 진단: 신호 뒤 h 거래일 종가 수익률 (그냥 들고 있었다면, 비용 없음)\n\n선택·판정에는 쓰지 않음.\n\n" + "\n".join(md))
    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>신호 뒤 수익률 곡선</title><style>
:root{{--bg:#fff;--fg:#1f2328;--grid:#e6e8eb;--sig:#2563eb;--rnd:#8a8f98;--diff:#d97706;--band:rgba(217,119,6,.18)}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#e6e8eb;--grid:#2b2f36;--sig:#60a5fa;--rnd:#9aa1ab;--diff:#f59e0b;--band:rgba(245,158,11,.22)}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:24px 16px;max-width:980px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px}} svg{{width:100%;height:auto}}
svg text{{fill:var(--fg);font-size:11px}} svg .t{{font-size:13px;font-weight:600}} .k span{{display:inline-block;margin-right:16px}}
.sw{{display:inline-block;width:18px;height:3px;vertical-align:middle;margin-right:6px}}</style></head><body>
<h1 style="font-size:18px">신호 뒤 1~60거래일 수익률 (진입가 대비, 비용·청산 규칙 없음)</h1>
<p>개발 구간 2021~2025. 선택·판정에는 쓰지 않는 진단입니다. 가로축: 진입 후 거래일, 세로축: 수익률(%).</p>
<p class="k"><span><i class="sw" style="background:var(--sig)"></i>신호 평균 (점선: 중앙값)</span>
<span><i class="sw" style="background:var(--rnd)"></i>같은 날 무작위 진입 평균 (점선: 중앙값)</span>
<span><i class="sw" style="background:var(--diff)"></i>차이 (신호 − 무작위), 띠: 날짜 부트스트랩 95%</span></p>
<div class="grid">{''.join(panels)}</div></body></html>"""
    with open(os.path.join(OUT, "diag_forward.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print("\n".join(md))


if __name__ == "__main__":
    main()
