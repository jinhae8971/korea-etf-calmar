# 주간 마감 리포트 (주식 / 크립토): 실행 절차

- 요청: 영길님 (2026-11-15). 매주 일요일 아침 전략 봇(Brief Hub, @spytltgld_strategy_bot)으로 **주식·크립토 메시지 2건**을 보낸다.
- 구성: 메시지마다 사진 앨범(주식 4장, 크립토 3장) + 본문 1건, 그리고 블로그형 전체 글 1편(GitHub Pages).
  - 전체 글: https://jinhae8971.github.io/korea-etf-calmar/weekly-close/<week>/ (지난 호: archive.html, 블로그 붙여넣기용: blog.md)
- 기준: 주식 = 금요일 뉴욕 종가까지의 한 주(week = 그 금요일 날짜). 크립토 = 직전 UTC 일봉 7개(토요일 UTC 마감 = 일요일 09:00 KST).
- 글의 목적(요청 원문): "블로그에 올렸을 때 구독자가 관심 가질 핵심 내용과 인사이트". 숫자 나열이 아니라 **왜 움직였나, 숫자 뒤의 의미, 다음 주에 볼 것**이 중심이다.
- 문체: 해요체, 장식 이모지 없음, 색 표시 없음(차트가 색을 담당). 채팅에는 본문을 다시 쓰지 않고 한두 줄 결과만 남긴다.
- 메모리 규칙: 텔레그램 작업은 실제 발송(SENT OK)까지 확인하고 끝낸다. 밤 22시~07시 KST에는 허브가 보내지 않는다.

## 0. 준비 (새 세션이면)
1. `add_repo jinhae8971/korea-etf-calmar (push)` → `git clone --depth 1 https://github.com/jinhae8971/korea-etf-calmar /home/claude/korea-etf-calmar`
2. `/home/claude/weekly_close`가 없으면 `cp -r /home/claude/korea-etf-calmar/weekly_close/engine /home/claude/weekly_close`
3. `pip install --break-system-packages pandas numpy matplotlib` (없을 때만). 한글 폰트는 Noto Sans CJK KR.

## 1. 수집
- `cd /home/claude/weekly_close && python3 -W ignore collect.py` → 다이제스트 + `out/<week>/data.json`
- `stale=True`거나 errors가 핵심 항목(지수, BTC)에 있으면 1회 재실행. 그래도 안 되면 "데이터 미갱신" 한 줄만 보고하고 끝낸다.
- ETF 순유입은 원천이 하루 늦을 수 있다(금요일분 누락 시 글 하단에 자동 표기).

## 2. 차트
- `python3 -W ignore charts.py <week>` → `out/<week>/img/` s1~s4(주식), c1~c3(크립토). 7개 모두 ok인지 확인하고 s1, c1은 Read로 한 번 본다.

## 3. 뉴스 조사 (WebSearch 4~6회, 필요 시 WebFetch)
- 주식 주간 리캡("stock market weekly recap <금요일 날짜>"), 한국 증시 주간(코스피 주간, 외국인 수급), 크립토 주간 급등락 원인, 다음 주 일정(week ahead: 실적, FOMC, 지표).
- 날짜별로 무엇이 시장을 움직였는지, 다음 주 일정의 KST 시각을 확인한다. 출처 URL을 sources에 남긴다.

## 4. narrative.json 작성 → 발송
- 파일: `out/<week>/narrative.json`. 스키마와 분량은 직전 호(`korea-etf-calmar/weekly_close/engine/narratives/<지난 week>.json`)를 따른다.
  - title(블로그 제목: 그 주를 한 문장으로, 대비·숫자 포함), lead(2~3문장), tldr(5~6줄)
  - stock / crypto 각각: title, headline(1문장), key(3줄, 각 40자 안팎: 텔레그램 '핵심'), tg_insight(2문장 이내), next(3줄: 텔레그램 '다음 주'), next_long(4~5줄), drivers(4~5개 {h,b}), insights(3~5개 {h,b})
  - cross({h,b}: 주식과 크립토를 함께 본 해석), calendar([{d(KST), e, w}]), sources([{t,u}])
- 숫자는 다이제스트나 출처에 있는 것만 쓴다. 지수·코인 등락, 상위100 집계 등 **렌더러가 data.json에서 넣는 숫자는 본문에서 되풀이할 때 반드시 최종 data.json 값과 맞춘다**(수집을 다시 돌리면 CoinGecko 7일 값이 바뀐다 → narrative는 마지막 수집 뒤에 쓴다).
- 인사이트 작성 원칙: ① 대비되는 두 숫자로 시작 ② 해석은 해석이라고 밝히고 반대 가능성·확인 조건을 붙인다 ③ 예측 단정 금지, 종목 추천 금지 ④ 직전 주와 4주 흐름 속에서 이번 주의 위치를 말한다.
- 발송: `python3 -W ignore publish.py <week> out/<week>/narrative.json`
  - `SENT OK`가 나와야 완료. 본문 1,500자 초과 시 중단되니 key/tg_insight/next를 줄인다.
  - 같은 주를 다시 보내야 하면 `--resend`(outbox id에 접미사), 한쪽만 보내려면 `--only stock|crypto`.
- 실패 시에만 SendUserMessage로 원인 한 줄 + 리포트 링크.

## 5. 파일
- collect.py(수집) · charts.py(PNG) · render.py(텔레그램·HTML·blog.md) · publish.py(Pages·outbox·확인)
- Brief Hub(`.github/workflows/brief-hub.yml`)의 `weekly_stock`, `weekly_crypto` 소스(mode outbox)가 `weekly_close/data/{stock,crypto}.json` 푸시로 깨어나 앨범과 본문을 보낸다.
- 엔진·런북·호별 narrative/data는 publish 때마다 `korea-etf-calmar/weekly_close/engine/`에 백업된다.
