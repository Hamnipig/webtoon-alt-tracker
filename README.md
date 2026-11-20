# 웹툰 기업 실적 트래커 (엔키AX 134580 · 키다리스튜디오 확장 예정)

공개 대체데이터로 분기 매출의 **방향과 크기를 공시 전에** 추정하는 파이프라인과 정적 대시보드입니다.
GitHub Actions가 매주 수집하고, GitHub Pages가 `docs/index.html`을 게시합니다. 서버는 필요 없습니다.

## 구조
```
config.yaml              종목·키워드·도메인·구조 단절 설정 (종목 추가는 여기만)
collector.py             수집기: 네이버 데이터랩, Google Trends, 환율, Tranco, DART, 페이지 감시, 탑툰챗 카탈로그, 수동입력
nowcast.py               국내/해외 분리 QTD 나우캐스트 + 백테스트
build_dashboard.py       DB → docs/data.json + docs/index.html
ingest_issue.py          Similarweb 입력 Issue → data/manual/similarweb.csv
templates/dashboard.html Chart.js 대시보드 템플릿
commentary/              LLM 주간 코멘터리 (WEEKLY_PROMPT.md = 작업 지시서)
data/tracker.db          SQLite (Actions가 커밋)
data/manual/*.csv        수동 입력 (Similarweb, DART 부문·지역 확인값)
data/review/*.md         DART 원문에서 자동 추출한 부문·지역·비용 '후보 표' (검토용)
tests/make_mock_data.py  합성 데이터로 오프라인 동작 확인
```

## 최초 설정 (약 20분, 1회)
1. **API 키 발급**
   - 네이버: 2026-07-31부터 신규 키는 **NAVER API HUB**(네이버 클라우드 플랫폼 콘솔)에서만 발급됩니다.
     콘솔 → Services → Application Services → NAVER API HUB → Application 등록 → '검색어 트렌드' 선택 → 인증 정보에서 Client ID/Secret 복사.
     예전에 developers.naver.com에서 받은 키가 있으면 config.yaml의 `naver_api.mode`를 `legacy`로 바꾸세요(2027-06-30까지).
   - OpenDART(opendart.fss.or.kr) → 인증키 신청
2. **GitHub 저장소 생성** (비공개 권장) 후 이 폴더 전체를 push
3. **Settings → Secrets and variables → Actions**에 `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`, `DART_API_KEY` 등록
4. **Settings → Pages**에서 Source를 'Deploy from a branch', Branch를 `main` / `/docs`로 설정
   - 비공개 저장소의 Pages는 유료 플랜(Pro/Team)이 필요합니다. 무료 플랜이면 공개 저장소로 쓰거나,
     `docs/index.html`을 내려받아 로컬에서 열면 됩니다(데이터가 HTML 안에 들어 있음).
5. **Actions 탭 → weekly-collect → Run workflow**로 첫 실행 (네이버 백필 포함)
6. **Settings → General → Features**에서 Issues 활성화 (Similarweb 입력 폼에 사용)

## 정기 작업
| 누가 | 무엇을 | 언제 |
|---|---|---|
| Actions | 전체 수집 → 대시보드 재빌드 → 커밋 | 매주 월 09:37 KST |
| 사용자 | Similarweb 5개 도메인 방문수·체류시간·한국 비중 입력 (대시보드 하단 폼) | 매월 3일 이후, 약 10분 |
| Claude 예약 작업 | 노이즈 점검·IP 태깅·DART 후보 표 검토 → `commentary/enkeyax.json` 커밋 | 매주 월 수집 후 |
| 사용자 | `data/review/` 후보 표 또는 Claude 제안을 확인한 뒤 `segments.csv` 반영 | 분기 공시 후 1회 |

## 첫 실행 후 확인할 것 (config.yaml)
- `domains`: 대만 사이트 실제 도메인(`toptoon.com.tw`는 임시값)
- `page_watch` URL: 이벤트·공지 페이지 실제 주소. 성인인증 벽에 막히면 데이터 상태에 '링크 n개(인증벽 의심)'로 표시됩니다.
- `chat_catalog`: 탑툰챗 캐릭터 목록의 CSS selector (비어 있으면 CONFIG_REQUIRED). JS로 렌더링되는 페이지면 수집되지 않습니다.
- 키다리스튜디오: 플랫폼·도메인 확인 후 `enabled: true`

## 방법론 메모
- **국내 신호** = 평균(네이버 자사 키워드 지수 '합산'의 QTD QoQ, 국내 방문수 합계 QTD QoQ).
  키워드별 성장률을 평균하지 않는 이유: 기저가 작은 신규 키워드(탑툰챗)가 신호를 지배하기 때문입니다.
- **해외 신호** = 도메인별 (1+방문수 성장)(1+환율 변동)−1을 직전 분기 방문수 비중으로 가중.
  Similarweb 이력이 없으면 Google Trends JP·TW로 대체합니다. 외부 유통(라인망가 등) 매출은 관측되지 않는 잔차입니다.
- **합산** = (1−s)·국내 + s·해외. s는 DART 지역 매출 확인값이 있으면 그 값을, 없으면 config 기본값 35%를 씁니다
  (아시아경제 2025-10-28, 독립리서치 아리스 '일본 비중 30~40%').
- **백테스트**: 합산 모델은 합병(2025-04-01) 후 첫 온전한 분기의 다음 분기(3Q25)부터, 합병 분기는 제외합니다.
  합병 전 상장사 매출은 일본 사업 중심이라 '해외 대리 모델'로 따로 검증하고, 유통→플랫폼 전환 전후로 적중률을 나눕니다.
  표본이 작아서 적중률은 검증이 아니라 모니터링 지표입니다(n을 항상 함께 표시).
- **계절성**: 네이버 백필(2022~)로 분기 계절 계수를 구해 국내 신호의 계절조정값을 같이 보여줍니다.
- **DART 분기 단독값**: 1Q·2Q·3Q는 보고서의 당분기(3개월) 금액, 4Q는 연간 − 3Q 누적입니다.
- 수집에 실패하면 값을 비워두고 상태만 기록합니다. 추정치나 과거 값으로 채우지 않습니다.

## 로컬 테스트
```bash
pip install -r requirements.txt
python tests/make_mock_data.py
python build_dashboard.py --config tests/mock_config.yaml --out tests/out --as-of 2026-11-20
# tests/out/index.html 열기 — 전부 '합성' 데이터
```
