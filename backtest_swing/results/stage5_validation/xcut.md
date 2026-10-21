# 5단계 검증 구간 진입 범위 (X) — 예측·평가 전 기록

계산 (stage5_validation.xcut, stage5_model.py:109-113 과 같은 조건):

    crossing = ds[ds.filter_ok & (ds.reason_real == sb.R_END) & ~ds.halt_stuck]
    X = crossing.date.min() if len(crossing) else None
    kept = ds[ds.date < X] if X is not None else ds

- X (처음 제외되는 진입일): 2026-08-18
- 마지막 포함 진입일 (X 직전 거래일): 2026-08-14 (포함된 신호의 마지막 날짜 2026-08-14)
- 재학습 월: 2026-01, 2026-02, 2026-03, 2026-04, 2026-05, 2026-06, 2026-07, 2026-08
- 포함 구간 시장 필터 통과 거래일 수: 137, 필터 통과 신호 후보 수: 41,785
- X 이후로 빠지는 필터 통과 신호 후보: 1,749
