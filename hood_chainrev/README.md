# HOOD CHAIN REVENUE RADAR

로빈후드 체인(Robinhood Chain)을 기준점으로, **체인별 프로토콜 수익**을 매일 순위화한다.

- 수집·판정·대시보드: `korea-etf-calmar/hood_chainrev/` (KST 08:40)
- 텔레그램 발송: `hynix-correction-monitor` 릴레이 (KST 08:52, 전략 Agent 봇)
- 대시보드: https://jinhae8971.github.io/korea-etf-calmar/hood-chainrev/

## 원천
| 엔드포인트 | 용도 |
|---|---|
| `api.llama.fi/overview/fees?dataType=dailyRevenue` | 프로토콜별 체인 분해(24h·30d) → 체인 수익 합계 |
| `api.llama.fi/overview/fees?dataType=dailyFees` | 동일 구조의 수수료 → 수익 전환율 |
| `api.llama.fi/v2/chains` | 체인 TVL → 자본효율(TVL당 연환산 수익) |

실행당 3회 호출, 약 8MB, 4~10초. 무인증.

## 지표
| 이름 | 정의 |
|---|---|
| 30일 수익 | 그 체인에서 발생한 모든 프로토콜 revenue의 30일 합 (주 순위 기준) |
| 점유율 | 체인 수익 ÷ 전체 온체인 수익 |
| 모멘텀 | 24시간 수익 ÷ 30일 일평균 |
| 전환율 | 수익 ÷ 수수료 (사용자가 낸 돈 중 프로토콜이 취한 비율) |
| TVL당 연수익 | (30일 수익 × 365/30) ÷ 체인 TVL |
| 앱레이어 수익 | 전체 수익 − `Chain` 카테고리(가스·시퀀서) |

## 변화 감지
`HOOD_RANK_SHIFT` `SHARE_SHIFT` `REV_SURGE` `REV_COLLAPSE` `RANK_UP` `RANK_DOWN`
`NEW_ENTRANT` `LEADER_SHIFT`

## 설계상 지켜야 할 것
1. **순위 비교는 교집합 재산정으로만 한다.** 유니버스 크기가 실행마다 달라지므로,
   모집단이 다른 순위를 그대로 빼면 허위 급변이 생긴다(hood-radar v1 실측 결함).
2. **수집 실패 시 "변화 없음"을 보내지 않는다.** 판정 불가로 명시하고 exit 1.
3. **하루 반짝은 승격하지 않는다.** REV_COLLAPSE는 이틀 연속일 때만 이벤트가 된다
   (DefiLlama 어댑터 지연으로 24h가 비는 경우가 실재).
4. `off_chain`은 체인이 아니라 집계 버킷이므로 순위에서 제외한다.
5. 배포 시 `data/`는 번들에서 제외한다 — 레포에 누적된 `history.json`을 덮어쓴다.

## 한계
- TVL·수익 모두 DefiLlama 어댑터 커버리지에 의존한다. 어댑터가 없는 체인은 0으로 보인다.
- 관측기이지 예측기가 아니다. 순위는 현재 자금 흐름의 서술이며 가격 전망이 아니다.
