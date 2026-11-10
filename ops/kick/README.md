# ops/kick — 아침 수집기 깨우기

GitHub 예약 실행(cron)이 아침 혼잡 시간대에 3~7시간씩 밀려서(11/10: 07:48 예약이 13시까지 안 돎), swing-rebalancer의
매일 07:40 운영 점검(ops-atlas/tools/kick_briefs.py)이 오늘 스냅샷이 아직 없는 수집기의 파일을 커밋한다.
그 푸시가 해당 워크플로우의 `push: paths`를 건드려 바로 돈다. 예약(cron)·백업 슬롯은 그대로 두고, 이미 오늘 스냅샷이
있으면 깨우지 않는다. 파일 내용은 깨운 시각 한 줄뿐이다.

| 파일 | 워크플로우 | 깨우는 날 (KST) |
|---|---|---|
| top-signal.txt | top-signal.yml (크립토 고점신호) | 매일 |
| btc-macro.txt | btc-macro.yml (BTC 매크로 레이더) | 화~토 |
