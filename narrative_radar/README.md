# Narrative Radar

크립토 내러티브 상대강도 관측 + 변화 탐지.

## 이 시스템이 하는 일
고정된 내러티브-종목 매핑에 대해 매일 측정한다.
- 30/7/1일 BTC 대비 초과수익 **중앙값** (섹터 대표값)
- 구성종목 확산 **폭** (BTC를 이긴 종목 비율)
- 거래 **회전율**(volume/mcap)과 그 횡단면 z
- 위 지표들의 **순위 변화**를 이력으로 누적

## 하지 않는 일
미래 수익률 예측. 이 지표들은 사전 검증된 예측력이 없다.
점수는 "지금 자금이 어디에 반응하는가"에 대한 서술일 뿐이다.

## 설계 원칙
1. 관측기이지 예측기가 아니다.
2. `universe.json` 매핑은 고정한다. 성과를 보고 갈아끼우면 사후선택 편향.
3. 섹터 대표값은 평균이 아닌 중앙값. 평균은 소수 극단치가 지배한다.
4. 수집 실패 시 "변화 없음"을 절대 발송하지 않는다. "판정 불가"로 명시한다.

## 구조
- `narrative_radar.py` — 수집·계산·탐지·렌더 (stdlib only)
- `universe.json` — 고정 매핑 (변경 시 frozen_at + changelog 갱신 필수)
- `data/latest.json` — 최신 스냅샷. `message` 필드에 렌더된 텔레그램 본문 포함(릴레이가 그대로 전달)
- `data/history.json` — 일별 순위·폭·도미넌스 이력 (최근 400일)
- `tests/` — 유닛 테스트 23개

## 실행
```
python narrative_radar.py            # 오늘
python narrative_radar.py 2026-08-21 # 특정일 라벨
```
환경변수: `PAGES_URL`, `DASHBOARD_DIR`, (선택) `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID`

## TVL 괴리 (v2)
가격 추세와 예치금(TVL) 추세의 이탈을 `가격변화 − TVL변화`(%p)로 측정한다.
- 체인 → `/v2/historicalChainTvl/{chain}` (일별 시계열 → 7일·30일 즉시)
- 프로토콜 → `/protocols` 1회 (change_7d). 상위 브랜드는 parentProtocol 자식을 TVL 가중 합산,
  그래도 없으면 `/tvl/{slug}` 현재값 폴백. 30일은 자체 누적 이력으로 약 30일 후부터 채워진다.
- $20M 미만 풀은 제외(소형 풀은 %변화가 쉽게 폭발한다).
- **TVL은 펀더멘털이 아니다.** 예치금일 뿐이며 인센티브 파밍으로 부풀고 중복 계상된다.
  프라이버시·DePIN·오라클은 TVL 개념이 무의미하므로 `llama` 필드를 비워 계산 대상에서 제외한다.
