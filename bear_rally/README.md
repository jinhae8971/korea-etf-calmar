# 베어랠리 레이더 (Bear Rally Radar)

트레이딩뷰 지표 `pine/bear_rally_detector_v6.pine`과 같은 규칙을 파이썬으로 옮겨, 트레이딩뷰 없이 매일 판정해 전략채널(Brief Hub, @spytltgld_strategy_bot)로 발행한다. 2026-11-24 영길님 요청.

- 대상: SPY · QQQ · SOXX(미국) · KOSPI · BTC · ETH — `settings.json`에서 추가·변경
- 리포트: https://jinhae8971.github.io/korea-etf-calmar/bear-rally/
- 실행: `.github/workflows/bear-rally.yml` — KST 09:07 / 09:41 / 10:23 / 11:17 (크립토 일봉 09:00 마감 뒤, 멱등)
- 발송: Brief Hub `bear_rally` 소스 (`daily`, 09:05 이후, 신선도 `hub_date` = 마지막 크립토 봉 + 1일)

## 판정 흐름

```
강세 ─(베어 진입)→ 베어 ─(저점 대비 +10% / 크립토 +20%)→ 반등 감시
반등 감시 ─(저항 근거 3개↑ + 반등 고점 대비 2ATR 반락 + RSI<60)→ 베어랠리 판정
반등 감시 ─(전환 점수 기준↑)→ 전환 후보 ─(주봉 2주 연속 안착 + 200일선 위 + 50일선 상승)→ 강세
반등 중 기준 저점 이탈 → 베어 / 반등폭 50% 되돌림 → 베어(다음 반등 재감시)
```

## 파일

| 파일 | 역할 |
|---|---|
| `engine.py` | 판정 엔진 (Pine 실행 순서 그대로, 외부 의존성 0) |
| `sources.py` | 수집 — Nasdaq(미국, 마감 세션 보강) → Yahoo 폴백 / Coinbase → Kraken / 네이버 fchart + 레포 캐시 |
| `breadth.py` | 미국 브레드스 근사 — 대형주 97종으로 200·50일선 위 비율, 등락 비율(ZBT) |
| `charts.py` | 차트 PNG (영문 표기 — 러너에 한글 글꼴 없음) |
| `report.py` | 텔레그램 본문(짧게) · 리포트 HTML |
| `bmr.py` | 실행 진입점 — `data/latest.json`, `data/events.json`, `docs/bear-rally/` 생성 |

## 트레이딩뷰 지표와 다른 점

- 미국 브레드스: S5TH·S5FI·NYSE 등락 대신 대형주 97종 근사. 근사 ZBT는 원본보다 자주 발생해 **전환 점수 1점으로만** 반영(단독 전환 우선권 끔, `zbt_override`).
- Nasdaq 히스토리가 하루 늦는 날은 info·summary의 정규장 마감가로 마지막 봉을 보강(`patched`), 다음 날 실제 봉으로 교체.
- 브레드스 원천이 하루 늦으면 직전 값을 유지(트레이딩뷰 gaps_off와 같은 방식)하고 본문에 기준일을 적는다.

## 운영 메모

- 강제 재계산: Actions → Bear Rally Radar → Run workflow(force) 또는 이 폴더 코드 변경 푸시.
- 브레드스 시계열을 처음부터 다시 만들려면 `data/breadth_series.json`·`data/breadth_tails.json`을 지우면 다음 실행이 6년치를 다시 받는다(약 1분).
- 테스트: `python -m unittest discover -s bear_rally/tests`
