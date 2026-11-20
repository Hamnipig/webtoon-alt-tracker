# 주간 애널리스트 코멘터리 작업 지시서 (Claude 예약 작업용)

매주 월요일 수집 Action이 끝난 뒤(10:30 KST 이후) 실행한다. 결과는 `commentary/<company>.json`으로 커밋하며,
push 이벤트가 대시보드를 다시 빌드한다.

## 입력
1. `docs/data.json` — 나우캐스트, WoW, 데이터 상태, 이벤트, IP 믹스 (이번 주 수집 결과)
2. `data/snapshots/` 중 최근 2개 날짜의 `chat_home`, `kr_event`, `nijigame` 스냅샷 — 신규 캐릭터·이벤트 확인
3. `data/review/*.md` — DART 원문에서 뽑은 부문·지역·비용 후보 표 (새 파일이 있을 때만)
4. 웹 검색: 최근 7일 '탑툰', '탑툰챗', '엔키AX', 'トップトゥーン', 'ニジGAME トップトゥーン' 뉴스·보도자료

## 판단 항목
- **노이즈 점검**: 검색량·트래픽 급등 주에 대규모 무료 코인/할인/신규가입 프로모션, 독점작 런칭, 언론 노출이 있었는지 확인한다.
  있으면 `has_major_free_promo=true`로 두고, 그 주 상승분은 일회성일 가능성이 있다고 적는다.
- **IP 출처 태깅**: 이번 주 신규 탑툰챗 캐릭터를 자체 IP(탑툰/탑코), 노벨피아, 기타 외부 IP로 분류한다.
  카탈로그 누적 비중의 변화와 함께 적는다. 이 값은 매출 비중이 아니므로 OPM에 대해서는 '방향성' 표현만 쓴다.
- **DART 후보 표**: 새 `data/review` 파일이 있으면 지역별(국내/일본/기타) 또는 부문별(플랫폼/유통) **분기 단독** 매출과
  지급수수료·외주비를 찾는다. 확실한 값만 `data/manual/segments.csv` 행으로 제안한다.
  열 구분(당분기 3개월/누적/전기)이 애매하면 넣지 않고 `analyst_caution`에 '확인 필요'로 적는다.

## 규칙
- 모든 수치는 data.json이나 확인한 출처에 있는 값만 쓴다. 추정·보간·기억에 의존한 수치는 금지한다.
- 데이터 상태가 FAILED나 MANUAL_REQUIRED인 소스에 기반한 결론은 쓰지 않는다. 대신 그 사실을 적는다.
- key_takeaways는 최대 3줄이고, 각 줄에 수치를 최소 1개 넣는다.

## 출력 (commentary/enkeyax.json)
```json
{
  "as_of_date": "YYYY-MM-DD",
  "company": "엔키AX (134580)",
  "metrics_collected": {
    "event_noise_check": {"has_major_free_promo": false, "promo_summary": "", "sources": ["URL"]},
    "ip_mix_note": "신규 캐릭터 n개: 자체 a / 노벨피아 b / 기타 c, 카탈로그 외부 IP 비중 x%→y%",
    "dlsite_activity_note": ""
  },
  "dashboard_commentary": {
    "qoq_revenue_signal": "POSITIVE | NEUTRAL | NEGATIVE",
    "key_takeaways": ["핵심 변화 1 (수치 기반)", "핵심 변화 2", "핵심 변화 3"],
    "analyst_caution": "트래픽 대비 결제 전환(ARPPU)·IP 로열티·데이터 공백 관련 점검 포인트"
  },
  "segments_csv_proposals": []
}
```
`segments_csv_proposals`에 행이 있으면 같은 커밋에서 `data/manual/segments.csv`에 추가한다.
커밋 메시지는 `commentary: YYYY-MM-DD`로 한다.
