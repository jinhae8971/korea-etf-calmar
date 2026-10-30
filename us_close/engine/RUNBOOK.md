# 미증시 마감 브리핑: 실행 절차 (v2, 2026-10-28 재설계)

- 요청: 영길님. 미국 거래일 다음날 07:40 KST, 텔레그램 전략 봇(Brief Hub, @spytltgld_strategy_bot)으로 발송한다.
- 구성: 사진 앨범 7장 + 본문 1건 + 전체 리포트 페이지(GitHub Pages)
  - 사진: 크로스에셋, 섹터·테마 히트맵, 대형주 트리맵, 브레드스·팩터, QQQ·SPY·SOXX·DRAM 일봉
  - 리포트: https://jinhae8971.github.io/korea-etf-calmar/us-close/ (날짜별 보관)
- 채팅에는 본문을 보내지 않는다(중복 금지). 마지막 답변 한 줄만 쓴다.
- 요약은 항상 마지막에 둔다(10/23 요청).
- 분석 종목: QQQ·SPY·SOXX(반도체 ETF)·DRAM(Roundhill Memory ETF). 10/31 요청으로 AAPL을 빼고 SOXX·DRAM을 넣었다(차트 8장). DRAM은 2026-04-02 상장이라 주봉 EMA120·주봉 SuperTrend 이력이 짧으니 스윙 판단은 일봉 위주로 보고 note에 한계를 적는다.
- **텔레그램 본문은 핵심만(10/31 요청 "메시지가 너무 길어 핵심위주로")**: 렌더러가 제목·한 줄 헤드라인·지수/금리·섹터 1줄·종목별 확률 1줄(상방/하방 기준 레벨)·일정 3개·요약 4줄만 낸다(1,500자 상한, 보통 900자 안팎). drivers·after_hours·view·breadth_note·note는 리포트 페이지에만 들어간다.
- 예약 이름: `QQQ·SPY·SOXX·DRAM 데일리 차트 분석`

## 0. 다음 회차 예약 (가장 먼저)
1. `python3 /home/claude/market_brief/brief.py --next-run` → `{"utc": ...}`
2. `list_triggers`로 같은 이름의 대기 예약을 확인하고, 없을 때만 `send_later`로 예약한다(`initiation`="human_schedule").
3. 예약 메시지 원문:

```
[예약] QQQ·SPY·SOXX·DRAM 데일리 차트 분석
① 다음 회차 예약부터: `python3 /home/claude/market_brief/brief.py --next-run` → list_triggers로 중복 확인 → send_later로 이 메시지 그대로 재예약 (initiation=human_schedule)
② /home/claude/market_brief/RUNBOOK.md 절차대로 미증시 마감 브리핑(v2: 수집→차트→뉴스→narrative→publish)을 만들어 전략 봇으로 발송, SENT OK 확인 — 텔레그램 본문은 핵심만(10/31), 맨 끝에 요약 필수, 채팅엔 한 줄만 (폴더가 없으면 korea-etf-calmar us_close/engine에서 복구)
```

## 1. 수집 (코드: /home/claude/market_brief/usclose/)
- `cd /home/claude/market_brief/usclose && python3 collect.py`
  - 출력: 다이제스트 + `out/<session>/data.json` + `bars.pkl`
  - 대상: 크로스에셋 14종, 섹터 11, 테마 9, 팩터 4쌍, 대형주 97종(시총은 Nasdaq API, 14일 캐시 `state/shares.json`)
  - QQQ·SPY·SOXX·DRAM 보정 지표도 계산한다(brief.snapshot, state/snap 저장).
- `stale=True`면 1회 재실행한다. 그래도 stale이면 "데이터 미갱신" 한 줄만 보고하고 끝낸다.
- 시세는 세션 날짜로 정렬된다. 비트코인만 최신값을 쓴다.

## 2. 차트
- `python3 charts.py <session>` → `out/<session>/img/01~07.png`
- 01_cross와 03_treemap은 Read로 한 번 보고 깨짐이 없는지 확인한다.
- 디자인 규칙: 다크 배경, 한국식 색(빨강 상승·파랑 하락), dataviz 팔레트, Noto Sans CJK KR.

## 3. 뉴스·이벤트 (WebSearch 2~4회)
- "stock market today <ET 날짜>": 마감 원인, 섹터·종목 재료
- 장 마감 후 실적·시간외 반응
- 다가오는 일정: FOMC, CPI, PCE, 고용, 빅테크·반도체 실적
- 시간외 가격: Yahoo chart API `range=1d&interval=5m&includePrePost=true`의 마지막 봉
- 일정 메모: 11/1부터 미국 서머타임이 끝나 마감이 06:00 KST가 된다(발송 07:40은 유지).

## 4. narrative.json 작성 → 발송
- 파일: `out/<session>/narrative.json`. 스키마와 문체는 `out/2026-10-27/narrative.json`을 그대로 따른다.
  - 텔레그램용으로 짧게 쓸 필드: headline 1문장(70자 이내), summary 3~4줄(각 45자 안팎, 시간외 급변이 있으면 한 줄에 반영), calendar 앞 3개가 가장 중요한 일정. tech.up/down은 "→" 앞부분이 기준 레벨로 표시된다.
  - 필드: headline, breadth_note, drivers(3~5), after_hours, tech{QQQ,SPY,SOXX,DRAM: short, swing, short_d, swing_d, note, up, down}, calendar, view, summary(3~4, 맨 끝), sources
- 숫자는 다이제스트나 출처에 있는 것만 쓴다. 지수·섹터·종목 수치는 렌더러가 data.json에서 직접 넣으므로 반복하지 않는다.
- 해석을 중심에 둔다: 무엇이 움직였나, 폭(브레드스·동일가중), 팩터 로테이션, 금리·유가·달러의 영향.
- 발송: `python3 publish.py <session> out/<session>/narrative.json`
  - `SENT OK`가 나와야 완료다. 렌더 → Pages → us_close outbox push → hub가 앨범과 본문을 보낸다.
  - 본문 1,500자, 캡션 1,000자를 넘으면 중단되니 narrative를 줄인다.
- 실패 시(SENT OK 없음)에만 SendUserMessage로 원인 한 줄 + 리포트 링크를 보낸다.
- **복구**: /home/claude/market_brief가 없으면 korea-etf-calmar의 `us_close/engine/`(코드·런북·확률 기록·narrative, publish 때마다 백업)을 /home/claude/market_brief로 복사하고 `pip install --break-system-packages pandas numpy matplotlib`(없을 때만)
- 클론이 없으면: `add_repo jinhae8971/korea-etf-calmar (push)` → `git clone --depth 1 https://github.com/jinhae8971/korea-etf-calmar /home/claude/korea-etf-calmar`.
- 마지막으로 확률을 기록한다:
  `python3 /home/claude/market_brief/brief.py --record '{"session":"<session>","QQQ":{"short_up":N,"swing_up":N},"SPY":{...},"SOXX":{...},"DRAM":{...},"note":"..."}'`

## 5. 확률 산정 원칙 (QQQ·SPY·SOXX·DRAM)
- 지표는 영길님 TradingView와 맞춰 두었다(10/16 검증, 36개 값 일치).
  - EMA50·EMA120
  - SuperTrend ATR 20(RMA) × 4
  - RSI 14 + SMA 14
  - MACD 12/26/9
  - 4H는 정규장 봉
- 출발점은 전 세션 확률(prev probs)이다. 바뀌면 근거를 note에 적는다.
- 단기(~1주)는 4H·1D 비중으로 본다.
  - RSI 30 미만이거나 지지 근접 → 반등을 올린다.
  - RSI 70 초과이거나 저항 근접 → 하락을 올린다.
- 스윙(2~4주)은 1D·1W 비중으로 본다: SuperTrend 방향, EMA50/120 위치, MACD 히스토그램 부호·기울기, 거래량.
- 이벤트(실적·FOMC)가 구간 안에 있으면 반영한다.
- 하루 변동은 보통 ±0~10%p로 둔다.
  - 레짐 변화(일봉·주봉 SuperTrend 전환, EMA120 이탈·회복)일 때만 크게 움직인다.
  - 범위는 25~75로 제한한다.
- 1D 종가가 EMA50 아래면 단기 확률은 "반등"으로 표기한다(렌더러가 자동 처리).

## 6. 파일
- `/home/claude/market_brief/brief.py`: QQQ·SPY·SOXX·DRAM 보정 지표, 다음 실행 시각(NYSE 휴장일 2026~2027), 확률 기록
- `usclose/`
  - `universe.py`: 종목 구성
  - `collect.py`: 수집·지표
  - `charts.py`: PNG
  - `render.py`: 텔레그램·HTML
  - `publish.py`: Pages·발송·확인
- Brief Hub(korea-etf-calmar/.github/workflows/brief-hub.yml)의 `us_close` 소스(mode outbox)가 `photos`가 있으면 앨범을 먼저 보낸다(2026-10-28 추가).
- 중단 요청이 오면 `list_triggers`로 대기 건을 찾아 `delete_trigger`로 지운다.
