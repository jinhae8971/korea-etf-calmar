# BTC 매크로 레이더

비트코인 거시 환경(순풍/중립/역풍) 변화를 매일 관측해 트레이드채널로 보내는 모니터.

| 지표 | 원천(1순위 → 폴백) | 판정 기준(frozen 2026-09-06) |
|---|---|---|
| 글로벌 유동성 (US+EA M2 프록시, USD 환산) | Fed H.6 DDP → DBnomics / ECB BSI → DBnomics / Frankfurter FX | YoY ≥3% & 비둔화 🟢 · YoY <0 또는 둔화 🔴 |
| 미10Y 실질금리 | Treasury.gov XML → DBnomics(FED/TIPS) → FRED csv | <1.5% 🟢 · 1.5~2.0 🟡 · ≥2.0 🔴 |
| 달러 인덱스 | Yahoo DX-Y.NYB(q1/q2) → Fed 광의 달러지수(추세만) | <100 🟢 · 100~105 🟡 · ≥105 🔴 |
| 현물 ETF 순유입 | bitcoin-data.com (BTC 단위) | 20일·5일 부호 조합 |
| 나스닥 동조화 | CoinGecko + Yahoo ^NDX | 30일 상관 ≥0.5 면 NDX 20일 추세 따라 판정 |

- 실행: `python3 btc_macro/btc_macro.py` (stdlib only). 산출물 `btc_macro/data/{latest,history}.json`, `docs/btc-macro/index.html`
- 릴레이는 `latest.json` 의 `message` 를 그대로 전달한다 (포매팅 이중관리 금지)
- `data_status`: OK / DEGRADED(3~4개 성공) / FAIL(2개 이하, exit 1)
- 테스트: `cd btc_macro && python3 -m unittest discover -s tests`
- 유동성 프록시에서 일본·중국 M2는 공개 원천 지연(IMF IFS 18개월+)으로 제외
