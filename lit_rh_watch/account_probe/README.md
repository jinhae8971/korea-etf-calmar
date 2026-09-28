# S3b 계정 청구 API 감시 (로컬 도커)

Lighter RH 스택 옆에서 30분마다 인증 API 두 개를 읽는다.

| 엔드포인트 | 용도 |
|---|---|
| `/api/v1/airdrop?l1_address&auth` | 청구·배정 정보 — 값이 생기면 IMMINENT |
| `/api/v1/livePoints/total?account_index&auth` | 내 누적 포인트 — 스키마 변화 감시 |

## 설치 (스택 폴더에서)
1. 이 폴더를 스택 루트에 `lit_rh_probe/` 로 복사
2. `compose.snippet.yml` 내용을 docker-compose.yml에 붙이고, `*_ENV` 값을 스택 .env의 실제 변수명으로 맞춤
3. `docker compose up -d --build lit-rh-probe`
4. `docker compose logs -f lit-rh-probe` 에서 `[probe] SEED ...` 한 줄이 나오면 정상 (첫 주기는 기준선만 저장)

## 동작 규칙
- 첫 주기: 기준선 저장, 발송 없음
- 인증 실패(401/403): 기준선을 건드리지 않음. 6회(약 3시간) 연속이면 1회만 알림
- 야간(KST 22~07) 발생분은 07시 이후 첫 주기에 발송
- 비밀값은 로그에 찍지 않음
