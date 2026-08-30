# HOOD RADAR — 로빈후드 체인 밈코인 시총 순위·변동 탐지

로빈후드 체인(Arbitrum Orbit L2, 2026-07-01 메인넷)의 커뮤니티 토큰 시가총액 순위를
6시간마다 관측하고, 순위·시총·유동성의 급변을 하루 안에 포착해 텔레그램 전략비서 채널로 보낸다.

## 구성

| 역할 | 위치 | 주기(KST) |
|---|---|---|
| 수집·판정·대시보드 | `korea-etf-calmar/hood_radar/` + `.github/workflows/hood-radar.yml` | 00:07 / 06:07 / 12:07 / 18:07 |
| 텔레그램 발송(릴레이) | `hynix-correction-monitor/.github/workflows/hood-radar-relay.yml` | 수집 +13분 |
| 대시보드 | `jinhae8971.github.io/korea-etf-calmar/hood-radar/` | 매 수집 시 갱신 |

릴레이는 `hood_radar/data/latest.json`의 `message` 필드를 그대로 전달한다(포매팅 이중관리 방지).

## 데이터 원천

GeckoTerminal 공개 API — `GET /networks/robinhood/pools?sort=h24_volume_usd_desc&include=base_token,quote_token`.
거래대금 상위 200개 풀을 스캔해 base token 단위로 집계한다. 외부 파이썬 의존성 0(stdlib only).

무료 티어 rate limit이 빡빡하므로 페이지 간 4초 간격 + `Retry-After` 존중 지수 백오프(8/20/45/90/150s)
+ User-Agent 로테이션을 둔다.

## 유니버스 규칙 (고정 목록 아님)

편입: 유동성 ≥ $25K **AND** 24h 거래대금 ≥ $50K **AND** 시총 ≥ $300K.
신규 상장 토큰도 임계를 넘으면 다음 실행에서 자동 편입된다.

제외:
- **INFRA** — 스테이블·랩드 토큰(USDG, WETH 등 심볼 목록)
- **RWA** — 토큰화 주식/ETF. 판정은 **이름 마커**(`• Robinhood Token`, `ETF Trust` 등) 또는
  **CoinGecko id 접미사**(`-robinhood`)로만 한다.
  티커 목록만으로 제외하지 않는다 — `NET`(NetNet, 체인 네이티브 밈코인 시총 $99M)이
  Cloudflare 티커와 겹친다는 이유로 사라지는 오제외를 막기 위함이다.

**토큰의 정체성은 컨트랙트 주소다.** 이 체인은 동일 티커 카피캣이 다수 존재해
(GG 3개, COPPERINU 3개, microduck 3개 실측) 심볼로는 구분되지 않는다.
카피캣이 있으면 표기를 `GG·b231`처럼 주소 앞자리로 분리한다.

## 시총 기준

`market_cap_usd`를 쓰되, **`MC > FDV × 1.1`이면 교차체인 브릿지 토큰**으로 보고 FDV로 대체한다
(예: VIRTUAL은 Base 기준 전체 시총 $457M이 딸려 들어오지만 이 체인 FDV는 $5.7M).
사용 기준은 대시보드에 MC/FDV로 표기한다.

## 변화 탐지

| 코드 | 조건 |
|---|---|
| `RANK_SURGE` / `RANK_DROP` | 24h 5계단 이상, 또는 6h 3계단 이상 |
| `NEW_ENTRY` | 최근 8스냅샷에 없던 주소가 25위 이내 진입 |
| `DROPPED_OUT` | 직전 25위 이내였는데 임계 미달로 이탈 |
| `MCAP_SURGE` / `MCAP_COLLAPSE` | 24h 시총 ±40% |
| `LIQ_DRAIN` | 6시간 유동성 −40% (러그풀 개연) |

비교 기준은 스냅샷 시각 기반이다 — 24h는 22시간 이상 경과분, 6h는 5시간 이상 경과분 중 가장 가까운 스냅샷.
스케줄이 밀려도 잘못된 구간과 비교하지 않는다.

## 리스크 플래그

`LIQ_THIN`(시총/유동성 ≥ 100배) · `LIQ_DRAIN` · `COPYCAT` · `SINGLE_POOL` · `YOUNG`(풀 생성 72시간 미만)
· `DATA_WARN`(음수 유동성 보고) · `BRIDGED_MC`.

상장 24시간 미만 풀의 h24 변동률은 0에 가까운 기준가 대비라 수백만 %로 튄다(실측 +3,968,711%).
그대로 쓰면 지표가 아니라 노이즈이므로 `YOUNG`은 "신규"로 표기하고, 그 외는 ±999%로 클램프한다.

## 실패 처리

수집 실패 또는 통과 종목 12종 미만이면 **"변화 없음"을 보내지 않고 "판정 불가"를 명시한 뒤 exit(1)** 한다.
데이터를 못 받고 정상 메시지를 보내는 오탐 경로를 원천 차단한다.

## 한계

- **관측기이지 예측기가 아니다.** 순위·변동은 자금 반응의 서술이며 미래 수익률을 주장하지 않는다.
- 시총은 온체인 DEX 집계 기준이라 CEX 유동성이 반영되지 않는다.
- 유동성 락·컨트랙트 권한(민팅·블랙리스트) 검증은 하지 않는다. 플래그는 개연성 신호일 뿐
  허니팟 판별이 아니므로 진입 전 컨트랙트를 직접 확인해야 한다.
- 24h 거래대금 상위 200풀 밖의 토큰은 보이지 않는다. 시총이 크더라도 거래가 멈춘 토큰은 누락될 수 있다.

## 테스트

```bash
python3 -m unittest discover -s tests
```
