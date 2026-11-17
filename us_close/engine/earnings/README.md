# 미국 종목 실적 분석 도구

## 구성
- `nvda_q3fy27.py` — 엔비디아 3Q FY27 실적 카드 이미지 3장(성적표·추이·주가) 생성 → `out/NVDA_2026-11-17/`
- `send_outbox.py` — 이미지+텍스트를 us_close outbox로 Brief Hub(전략 봇)에 전송하고 sent.json으로 수신 확인
  - 사용: `python3 send_outbox.py <suffix> <text.html> <caption.html> <img_dir> <docs_subdir>`
- `build_desk.py` — US Earnings Desk 대시보드 데이터 생성 → `desk/*.json` ({rows, meta}, meta에 스크립트 원문 포함)
  - 데이터: Nasdaq API(실적 캘린더·서프라이즈·컨센서스, Zacks 수치) + Yahoo chart v8(종가) + 회사 보도자료 수치
- `desk/index.html` — 대시보드 페이지(Data 아티팩트의 `files/index.html`)

## 대시보드
- 아티팩트: https://claude.ai/artifact/JGWmASVxZR7GpEDoU37LfJ (비공개, 소유자만 열람)
- 데이터셋: quarters·scorecard·report·price·eps·notes(엔비디아 고정 스냅샷, fixed) / reactions·calendar(갱신 대상)
- 페이지는 모든 데이터셋을 `ticker` 열로 거르고, 종목 선택 목록은 report.json의 행에서 만든다.
  - 새 종목 추가 = 6개 종목별 파일에 해당 ticker 행 추가 → 업로드 → datasets/<id>의 source.url·updated 갱신

## 캘린더·반응표 갱신 절차
1. `build_desk.py`의 TODAY_ET·PAST_FROM·FUT_TO 조정 후 실행
2. `desk/calendar.json`, `desk/reactions.json`을 아티팩트 에셋으로 업로드(파일당 1회 호출)
3. ArtifactData로 `datasets/calendar`, `datasets/reactions`의 `source.url`과 `updated.at` 갱신(if_version 필수)
4. `data/users/me`의 check·runs 확인
