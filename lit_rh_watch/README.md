# lit-rh-watch — 라이터 로빈후드 포인트 → LIT 전환 신호 감시

2시간마다 수집(`lit-rh-watch.yml`), +12분 뒤 `hynix-correction-monitor/lit-rh-relay.yml`이 트레이드채널로 발송.
대시보드: https://jinhae8971.github.io/korea-etf-calmar/lit-rh-watch/

| 신호군 | 원천 | 강신호(ALERT) 조건 |
|---|---|---|
| S1 온체인 | ETH·RH RPC `eth_getLogs`, RH `totalSupply`, Blockscout 주소 메타 | 풀 규모(1,100만±15%) 단건·7일 누적 / 신규 컨트랙트(14일 내) 수령 / RH체인 LIT 공급 +1M 이상 |
| S2 공식 | docs `.md` 4종, `api.rh.lighter.xyz` 공지·리더보드 | 전환·청구·종료 키워드 추가 / 포인트 관련 공지 / 적립 48h 정지 / 상위10 합계 3%↓(시빌 정리) |
| S3 UI·API | 프론트 번들 모듈명·API 경로, 로컬 `account_probe`(인증) | 청구·보상 명칭 신규 / 청구 API 배정값 등장(IMMINENT) |
| S4 보조 | Google News, X, Polymarket | WATCH까지만 |

등급: 서로 다른 신호군 2종 이상이 7일 안에 ALERT → IMMINENT. 발송은 등급이 오를 때만, KST 07~21시.
판정 불가(핵심 원천 6종 중 3종 미만 수신) 2회 연속이면 잡 실패, 스냅샷 5시간 정체면 릴레이가 1회 알림.
