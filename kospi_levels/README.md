# KOSPI 매매기준점 (kospi_levels)

KOSPI 지지·저항·추세선·3-시나리오(매매기준점)를 계산해 `data/latest.json`에 남긴다.
발송은 **Brief Hub**(`.github/workflows/brief-hub.yml`, @spytltgld_strategy_bot)가 평일 08:00 KST 이후에 한다.

2026-10-24 `jinhae8971/kospi-levels` 레포에서 이전했다. 그쪽은 봇 토큰이 폐기돼(Telegram 401)
10/21부터 매일 실패했고, 크론 지연으로 "장 시작 전" 브리프가 실제로는 10~12시에 도착하고 있었다.

## 산출 로직 (이전 전과 동일 — 40개 합성 시계열 회귀 비교 차이 0)
- 데이터: Yahoo Finance `^KS11` — 가격·당일 등락은 일봉, 지표·레벨은 주봉(TradingView 1주 차트와 정합)
- 지표: SuperTrend(20,4), 20주선, RSI(14)+SMA(14), MACD(12,26,9), Wilder ATR(20)
- 레벨: 스윙 고/저, 피보나치(하락 되돌림=저항 / 상승 되돌림=지지), 심리 라운드 — 티어 가중(구조 > 기술 > 심리), 60pt 병합, 위아래 최대 4개
- 레짐: SuperTrend 방향 + MACD 배열 + 20주선 관계
- 시나리오: 강세(낙폭 61.8% 회복) / 기본(직전 저점~61.8% 레인지) / 약세(직전 저점 이탈 → 슈퍼트렌드 → 1,000단위 심리선)

## 실행 흐름
| 단계 | 내용 |
|---|---|
| 수집 | `.github/workflows/kospi-levels.yml` — KST 월~금 05:43 · 06:21 · 06:47 · 07:23 슬롯. 오늘 데이터가 있으면 계산 생략(멱등) |
| 대기 | 예약 실행이 08:00 전에 끝나면 08:00까지 잡을 붙잡아 둔다 |
| 발송 | 잡 종료 → Brief Hub `workflow_run` → `kospi_levels` 소스(daily, 월~금, earliest 08:00) 발송 |
| 지연 알림 | 정오(08:00+4h)까지 오늘 데이터가 없으면 허브가 하루 한 번 알림 |

## 로컬
```bash
pip install -r kospi_levels/requirements.txt
python kospi_levels/kospi_levels.py --dry       # 본문만 출력
python -m unittest discover -s kospi_levels/tests
```

> 참고용 기술 분석. 투자 판단·리스크 관리는 본인 책임.
