"""
collector.py — 대체데이터 수집기 (GitHub Actions 주간 크론에서 실행)

  python collector.py                      # 활성화된 전 종목, 전 소스
  python collector.py --company enkeyax    # 특정 종목만
  python collector.py --only naver,dart    # 특정 소스만

설계 원칙
  - 소스마다 run_source()로 감싸서, 한 소스가 실패해도 나머지는 계속 진행합니다.
  - 결과는 run_log 테이블에 OK / FAILED / CONFIG_REQUIRED / MANUAL_REQUIRED로 남습니다.
    대시보드의 '데이터 상태' 표가 이 값을 그대로 보여줍니다.
  - 수치를 추정하거나 과거 값으로 채우지 않습니다. 못 가져오면 비워두고 상태만 기록합니다.
  - 외부 요청 사이에는 request_sleep_sec만큼 쉽니다.
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import traceback
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml

import db

ROOT = Path(__file__).parent
UA = "Mozilla/5.0 (compatible; alt-data-research-bot/1.0; +https://github.com/)"


# -----------------------------------------------------------------------------
# 공통 유틸
# -----------------------------------------------------------------------------
class Ctx:
    """수집 실행 중 공유되는 상태 (설정, DB 연결, HTTP 세션)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.con = db.connect(str(ROOT / cfg["db_path"]))
        self.sleep = cfg.get("request_sleep_sec", 1.5)
        self.timeout = cfg.get("http_timeout_sec", 20)
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.today = date.today()

    def get(self, url, **kw):
        time.sleep(self.sleep)                      # 요청 간격 준수
        kw.setdefault("timeout", self.timeout)
        return self.s.get(url, **kw)

    def post(self, url, **kw):
        time.sleep(self.sleep)
        kw.setdefault("timeout", self.timeout)
        return self.s.post(url, **kw)


class ConfigRequired(Exception):
    """설정/키가 없어 실행할 수 없음 (실패가 아니라 '사람이 할 일'로 표시)."""


class ManualRequired(Exception):
    """수동 입력이 필요함."""


def run_source(ctx, company, source, fn, *args):
    """소스 하나를 실행하고 결과를 run_log에 남긴다. 예외는 절대 밖으로 던지지 않는다."""
    try:
        n = fn(ctx, company, *args) or 0
        db.log_run(ctx.con, company, source, "OK", n)
        print(f"  [OK]   {source:<16} rows={n}")
    except ConfigRequired as e:
        db.log_run(ctx.con, company, source, "CONFIG_REQUIRED", 0, str(e))
        print(f"  [CFG]  {source:<16} {e}")
    except ManualRequired as e:
        db.log_run(ctx.con, company, source, "MANUAL_REQUIRED", 0, str(e))
        print(f"  [MAN]  {source:<16} {e}")
    except Exception as e:  # noqa: BLE001 - 파이프라인 보호가 목적
        db.log_run(ctx.con, company, source, "FAILED", 0, f"{type(e).__name__}: {e}")
        print(f"  [FAIL] {source:<16} {type(e).__name__}: {e}")
        traceback.print_exc(limit=1)


def week_start(d: date) -> str:
    """주 단위 키는 월요일 날짜로 통일."""
    return (d - timedelta(days=d.weekday())).isoformat()


# -----------------------------------------------------------------------------
# 1) 네이버 데이터랩 (국내 검색량, 공식 API)
# -----------------------------------------------------------------------------
def collect_naver(ctx, company, ccfg):
    cid, csec = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not (cid and csec):
        raise ConfigRequired("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수 없음")
    nd = ccfg.get("naver_datalab", {})
    groups = (nd.get("groups", []) + nd.get("peer_groups", []))[:5]   # API 제한: 요청당 5그룹
    body = {
        # 데이터랩 지수는 '요청 구간 내 최댓값=100'으로 정규화된다.
        # 매번 백필 시작일부터 전체 구간을 다시 받아 덮어써야 스케일이 일관된다.
        "startDate": ctx.cfg.get("backfill_start", "2022-01-01"),
        "endDate": ctx.today.isoformat(),
        "timeUnit": "week",
        "keywordGroups": [{"groupName": g["name"], "keywords": g["keywords"][:20]} for g in groups],
    }
    # 2026-07-31부터 신규 키는 NAVER API HUB(네이버 클라우드)에서만 발급 → 호스트·헤더가 다름.
    # 기존 developers.naver.com 키는 NAVER_API_MODE=legacy 로 사용 (2027-06-30까지)
    nav = ctx.cfg.get("naver_api", {})
    if os.getenv("NAVER_API_MODE", nav.get("mode", "hub")) == "legacy":
        url = nav.get("legacy_url", "https://openapi.naver.com/v1/datalab/search")
        hdr = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec}
    else:
        url = nav.get("hub_url", "https://naverapihub.apigw.ntruss.com/search-trend/v1/search")
        hdr = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": csec}
    hdr["Content-Type"] = "application/json"
    r = ctx.post(url, headers=hdr, data=json.dumps(body, ensure_ascii=False).encode("utf-8"))
    if r.status_code in (401, 403, 404):
        raise RuntimeError(f"HTTP {r.status_code} @ {url} — 키 종류(hub/legacy)와 엔드포인트, "
                           f"애플리케이션에서 '검색어 트렌드' API 선택 여부 확인: {r.text[:200]}")
    r.raise_for_status()
    rows = []
    for res in r.json().get("results", []):
        for p in res.get("data", []):
            d = date.fromisoformat(p["period"])
            rows.append(dict(company=company, source="naver_datalab", metric="search_index",
                             entity=res["title"], geo="KR", period_type="W",
                             period=week_start(d), value=float(p["ratio"]), unit="index(0-100)"))
    # 이번 요청 구간 밖의 과거 값이 남아 스케일이 섞이지 않도록 기존 값 삭제 후 저장
    ctx.con.execute("DELETE FROM metrics WHERE company=? AND source='naver_datalab'", (company,))
    return db.upsert_metrics(ctx.con, rows)


# -----------------------------------------------------------------------------
# 2) Google Trends (해외 보조 지표, 실패 허용)
# -----------------------------------------------------------------------------
def collect_gtrends(ctx, company, ccfg):
    gt = ccfg.get("google_trends") or {}
    if not gt.get("keywords"):
        raise ConfigRequired("google_trends.keywords 비어 있음")
    try:
        from pytrends.request import TrendReq   # 2025-04 아카이브된 비공식 라이브러리
    except ImportError as e:
        raise ConfigRequired("pytrends 미설치") from e
    py = TrendReq(hl="en-US", tz=0, timeout=(10, 25))
    rows, errors = [], []
    for geo in gt.get("geos", []):
        try:
            time.sleep(max(ctx.sleep, 6))           # 429 회피용으로 길게 대기
            py.build_payload(gt["keywords"][:5], timeframe="today 5-y", geo=geo)
            df = py.interest_over_time()
            if df.empty:
                errors.append(f"{geo}: empty"); continue
            for ts, rec in df.iterrows():
                if rec.get("isPartial"):            # 진행 중인 주는 저장하지 않음
                    continue
                for kw in gt["keywords"][:5]:
                    rows.append(dict(company=company, source="google_trends", metric="search_index",
                                     entity=kw, geo=geo, period_type="W",
                                     period=week_start(ts.date()), value=float(rec[kw]), unit="index(0-100)"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{geo}: {type(e).__name__}")
    if not rows:
        raise RuntimeError("; ".join(errors) or "no data")
    n = db.upsert_metrics(ctx.con, rows)
    if errors:
        db.log_run(ctx.con, company, "google_trends_partial", "FAILED", 0, "; ".join(errors))
    return n


# -----------------------------------------------------------------------------
# 3) 환율 (yfinance 우선, Frankfurter(ECB) 대체)
# -----------------------------------------------------------------------------
def collect_fx(ctx, company, ccfg):
    pairs = ccfg.get("fx_pairs", [])
    rows, errors = [], []
    for pair in pairs:
        base, quote = pair[:3], pair[3:]
        got = False
        try:
            import yfinance as yf
            df = yf.download(f"{base}{quote}=X", period="5y", interval="1d",
                             progress=False, auto_adjust=False)
            if not df.empty:
                close = df["Close"].squeeze()
                for ts, v in close.dropna().items():
                    rows.append(dict(company=company, source="fx", metric="close", entity=pair,
                                     period_type="D", period=ts.date().isoformat(),
                                     value=float(v), unit=f"{quote} per {base}"))
                got = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"{pair} yfinance {type(e).__name__}")
        if not got and base != "TWD":               # ECB 기준환율에는 TWD가 없음
            try:
                start = (ctx.today - timedelta(days=365 * 5)).isoformat()
                r = ctx.get(f"https://api.frankfurter.app/{start}..", params={"from": base, "to": quote})
                r.raise_for_status()
                for d, v in r.json()["rates"].items():
                    rows.append(dict(company=company, source="fx", metric="close", entity=pair,
                                     period_type="D", period=d, value=float(v[quote]),
                                     unit=f"{quote} per {base}"))
                got = True
            except Exception as e:  # noqa: BLE001
                errors.append(f"{pair} frankfurter {type(e).__name__}")
        if not got:
            errors.append(f"{pair}: 실패")
    if not rows:
        raise RuntimeError("; ".join(errors))
    return db.upsert_metrics(ctx.con, rows)


# -----------------------------------------------------------------------------
# 4) Tranco 도메인 순위 (무료·일간, 트래픽 보조 지표)
# -----------------------------------------------------------------------------
def collect_tranco(ctx, company, ccfg):
    rows, missing = [], []
    for d in ccfg.get("domains", []):
        dom = d["domain"]
        try:
            r = ctx.get(f"https://tranco-list.eu/api/ranks/domain/{dom}")
            r.raise_for_status()
            ranks = r.json().get("ranks", [])
            if not ranks:
                missing.append(dom)                 # 상위 100만 밖 → 값 없음(0으로 채우지 않음)
            for it in ranks:
                rows.append(dict(company=company, source="tranco", metric="rank", entity=dom,
                                 period_type="D", period=it["date"], value=float(it["rank"]), unit="rank"))
        except Exception as e:  # noqa: BLE001
            missing.append(f"{dom}({type(e).__name__})")
    if not rows and any("(" in m for m in missing):
        raise RuntimeError("전 도메인 조회 실패: " + ", ".join(missing))
    if missing:
        db.log_run(ctx.con, company, "tranco_missing", "SKIPPED", 0, "순위 없음: " + ", ".join(missing))
    return db.upsert_metrics(ctx.con, rows) if rows else 0


# -----------------------------------------------------------------------------
# 5) OpenDART 재무 (분기 단독 매출·영업이익·순이익)
# -----------------------------------------------------------------------------
REPRT = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}   # 1Q, 반기, 3Q, 사업보고서
ACCOUNTS = {
    "revenue": (["ifrs-full_Revenue", "ifrs_Revenue"], ["매출액", "수익(매출액)", "영업수익", "매출"]),
    "operating_income": (["dart_OperatingIncomeLoss"], ["영업이익", "영업이익(손실)", "영업손실"]),
    "net_income": (["ifrs-full_ProfitLoss", "ifrs_ProfitLoss"], ["당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익"]),
}


def _num(s):
    if s in (None, "", "-"):
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def dart_corp_code(ctx, stock_code):
    """종목코드 → DART 고유번호. corpCode.xml을 내려받아 캐시."""
    cache = ROOT / "data" / "corp_codes.json"
    if cache.exists():
        m = json.loads(cache.read_text(encoding="utf-8"))
        if stock_code in m:
            return m[stock_code]
    key = os.getenv("DART_API_KEY")
    r = ctx.get("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": key}, timeout=60)
    r.raise_for_status()
    from xml.etree import ElementTree as ET
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        root = ET.fromstring(z.read(z.namelist()[0]))
    m = {}
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if sc:
            m[sc] = el.findtext("corp_code")
    cache.write_text(json.dumps(m), encoding="utf-8")
    return m[stock_code]


def _pick(items, ids, names):
    for it in items:
        if it.get("sj_div") not in ("IS", "CIS"):
            continue
        if it.get("account_id") in ids or it.get("account_nm", "").replace(" ", "") in names:
            return it
    return None


def collect_dart_fin(ctx, company, stock_code, start_year=2022):
    key = os.getenv("DART_API_KEY")
    if not key:
        raise ConfigRequired("DART_API_KEY 환경변수 없음")
    corp = dart_corp_code(ctx, stock_code)
    out = []
    for y in range(start_year, ctx.today.year + 1):
        cum = {}   # 분기 → 계정 → 누적값
        for rc, q in REPRT.items():
            if date(y, 3 * q, 1) > ctx.today:       # 아직 끝나지 않은 분기는 요청 안 함
                continue
            items = None
            for fs in ("CFS", "OFS"):               # 연결 없으면 별도
                r = ctx.get("https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
                            params={"crtfc_key": key, "corp_code": corp, "bsns_year": y,
                                    "reprt_code": rc, "fs_div": fs})
                js = r.json()
                if js.get("status") == "000":
                    items = js["list"]; break
            if not items:
                continue                            # 미공시(아직 제출 전) → 건너뜀
            rcept = items[0].get("rcept_no")
            for acc, (ids, names) in ACCOUNTS.items():
                it = _pick(items, ids, [n.replace(" ", "") for n in names])
                if not it:
                    continue
                three_m = _num(it.get("thstrm_amount"))       # 1Q·반기·3Q: 당분기(3개월), 사업: 연간
                accum = _num(it.get("thstrm_add_amount"))     # 반기·3Q: 누적
                cum.setdefault(q, {})[acc] = (three_m, accum, rcept)
        # 분기 단독값 만들기
        for q, accs in cum.items():
            for acc, (three_m, accum, rcept) in accs.items():
                val, basis = three_m, "reported"
                if q == 4:   # 4분기 = 연간 − 3분기 누적
                    q3 = cum.get(3, {}).get(acc)
                    if not q3 or q3[1] is None or three_m is None:
                        continue
                    val, basis = three_m - q3[1], "derived(annual-3Q_cum)"
                if val is None:
                    continue
                out.append(dict(company=company, stock_code=stock_code, quarter=f"{y}Q{q}",
                                account=acc, scope="CFS", value=val, basis=basis, rcept_no=rcept))
    return db.upsert_financials(ctx.con, out)


def collect_dart_docs(ctx, company, stock_code, ccfg):
    """정기보고서 원문에서 부문·지역·비용 관련 표를 찾아 '검토용' 파일로 저장.
    표의 열 구조(당분기/누적/전기)가 보고서마다 달라 자동으로 숫자를 넣지 않는다.
    확인된 값은 data/manual/segments.csv에 입력 → ingest_manual()이 DB에 반영."""
    key = os.getenv("DART_API_KEY")
    if not key:
        raise ConfigRequired("DART_API_KEY 환경변수 없음")
    from bs4 import BeautifulSoup
    corp = dart_corp_code(ctx, stock_code)
    r = ctx.get("https://opendart.fss.or.kr/api/list.json",
                params={"crtfc_key": key, "corp_code": corp, "pblntf_ty": "A",
                        "bgn_de": (ctx.today - timedelta(days=400)).strftime("%Y%m%d"), "page_count": 100})
    reports = [x for x in r.json().get("list", []) if re.search("분기보고서|반기보고서|사업보고서", x["report_nm"])]
    if not reports:
        return 0
    review_dir = ROOT / "data" / "review"; review_dir.mkdir(parents=True, exist_ok=True)
    kw = ccfg.get("dart", {})
    seg_kw, cost_kw = kw.get("segment_keywords", []), kw.get("cost_keywords", [])
    n = 0
    for rep in reports[:2]:                         # 최근 2건만 (주간 실행이라 충분)
        out_md = review_dir / f"{company}_{rep['rcept_no']}.md"
        if out_md.exists():
            continue
        z = ctx.get("https://opendart.fss.or.kr/api/document.xml",
                    params={"crtfc_key": key, "rcept_no": rep["rcept_no"]}, timeout=90)
        with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
            xml = max((zf.read(nm) for nm in zf.namelist()), key=len).decode("utf-8", "ignore")
        soup = BeautifulSoup(xml, "lxml")
        lines = [f"# {rep['report_nm']} ({rep['rcept_no']})", ""]
        for t in soup.find_all("table"):
            txt = t.get_text(" ", strip=True)
            hit_seg = sum(k in txt for k in seg_kw) >= 2
            hit_cost = any(k in txt for k in cost_kw)
            if not (hit_seg or hit_cost) or not re.search(r"\d{1,3}(,\d{3})+", txt):
                continue
            lines.append(f"## {'부문/지역' if hit_seg else '비용'} 후보 표")
            for tr in t.find_all("tr")[:40]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th", "te", "tu"])]
                if cells:
                    lines.append("| " + " | ".join(cells) + " |")
            lines.append(""); n += 1
        out_md.write_text("\n".join(lines), encoding="utf-8")
    return n


# -----------------------------------------------------------------------------
# 6) 공개 페이지 변경 감시 → 이벤트(무료 프로모션·신작·출시) 감지
# -----------------------------------------------------------------------------
TAG_RULES = {
    "free_promo": ["무료", "free", "0코인", "코인 지급", "코인지급", "포인트", "할인", "세일", "sale",
                   "페이백", "증정", "無料", "セール", "割引", "プレゼント"],
    "new_title": ["신작", "독점", "런칭", "연재 시작", "오픈", "新作", "独占", "配信開始", "新連載"],
    "launch": ["출시", "사전등록", "사전 등록", "事前登録", "リリース", "正式サービス"],
    "chat": ["챗", "채팅", "캐릭터", "チャット", "キャラ"],
}


def tag_text(t: str):
    low = t.lower()
    return [tag for tag, kws in TAG_RULES.items() if any(k.lower() in low for k in kws)]


def collect_pages(ctx, company, ccfg):
    from bs4 import BeautifulSoup
    pages = ccfg.get("page_watch", [])
    if not pages:
        raise ConfigRequired("page_watch 비어 있음")
    snap_dir = ROOT / "data" / "snapshots"
    total_new, failures = 0, []
    wk = week_start(ctx.today)
    for p in pages:
        try:
            r = ctx.get(p["url"])
            if r.status_code != 200:
                failures.append(f"{p['id']}:HTTP{r.status_code}"); continue
            soup = BeautifulSoup(r.text, "lxml")
            links = {}
            for a in soup.find_all("a"):
                text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
                href = a.get("href") or ""
                if 2 <= len(text) <= 120 and not href.startswith("javascript"):
                    links[urljoin(p["url"], href)] = text
            if len(links) < 3:
                # 성인인증/로그인 벽 또는 JS 렌더링 페이지일 가능성
                failures.append(f"{p['id']}:링크 {len(links)}개(인증벽/JS 렌더링 의심)")
            content_hash = hashlib.sha256(json.dumps(sorted(links.items())).encode()).hexdigest()[:16]
            prev = ctx.con.execute(
                "SELECT links_json FROM snapshots WHERE company=? AND page_id=? ORDER BY taken_on DESC LIMIT 1",
                (company, p["id"])).fetchone()
            prev_links = json.loads(prev[0]) if prev else None
            ctx.con.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?)",
                            (company, p["id"], ctx.today.isoformat(), content_hash,
                             json.dumps(links, ensure_ascii=False)))
            (snap_dir / f"{company}_{p['id']}_{ctx.today.isoformat()}.json").write_text(
                json.dumps(links, ensure_ascii=False, indent=1), encoding="utf-8")
            if prev_links is None:
                continue                            # 첫 실행은 기준선만 저장 (전부 '신규'로 잡지 않음)
            counts = {"new_items": 0, "free_promo": 0, "new_title": 0, "launch": 0}
            for url, text in links.items():
                if url in prev_links:
                    continue
                tags = tag_text(text)
                ev_id = hashlib.sha1(f"{company}|{url}|{text}".encode()).hexdigest()[:16]
                if db.insert_event(ctx.con, dict(event_id=ev_id, company=company,
                                                 detected_on=ctx.today.isoformat(), page_id=p["id"],
                                                 region=p.get("region"), title=text, url=url, tags=tags)):
                    counts["new_items"] += 1; total_new += 1
                    for t in tags:
                        if t in counts:
                            counts[t] += 1
            db.upsert_metrics(ctx.con, [dict(company=company, source="page_watch", metric=m, entity=p["id"],
                                             geo=p.get("region", ""), period_type="W", period=wk,
                                             value=float(v), unit="count") for m, v in counts.items()])
        except Exception as e:  # noqa: BLE001
            failures.append(f"{p['id']}:{type(e).__name__}")
    if len(failures) >= len(pages):
        raise RuntimeError("; ".join(failures))
    if failures:
        db.log_run(ctx.con, company, "page_watch_issues", "FAILED", 0, "; ".join(failures))
    return total_new


# -----------------------------------------------------------------------------
# 7) 탑툰챗 캐릭터 카탈로그 → IP 출처 비중
# -----------------------------------------------------------------------------
def classify_ip(text, rules):
    low = (text or "").lower()
    for src, kws in rules.items():
        if any(k.lower() in low for k in kws):
            return src
    return "unknown"


def collect_catalog(ctx, company, ccfg):
    from bs4 import BeautifulSoup
    cc = ccfg.get("chat_catalog") or {}
    if not (cc.get("url") and cc.get("item_selector")):
        raise ConfigRequired("chat_catalog.item_selector 미설정 (페이지 구조 확인 필요)")
    r = ctx.get(cc["url"]); r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    items = soup.select(cc["item_selector"])
    if not items:
        raise RuntimeError("selector에 걸리는 항목 0개 (페이지 구조 변경/인증벽)")
    snap = ctx.today.isoformat()
    counts = {}
    for it in items:
        name_el = it.select_one(cc["name_selector"]) if cc.get("name_selector") else it
        origin_el = it.select_one(cc["origin_selector"]) if cc.get("origin_selector") else it
        name = name_el.get_text(" ", strip=True) if name_el else ""
        origin = origin_el.get_text(" ", strip=True) if origin_el else ""
        src = classify_ip(origin + " " + name, cc.get("ip_rules", {}))
        key = hashlib.sha1(name.encode()).hexdigest()[:12]
        ctx.con.execute("INSERT OR REPLACE INTO catalog VALUES (?,?,?,?,?,?)",
                        (company, snap, key, name, origin, src))
        counts[src] = counts.get(src, 0) + 1
    ctx.con.commit()
    total = sum(counts.values())
    rows = [dict(company=company, source="chat_catalog", metric="ip_share", entity=src,
                 period_type="W", period=week_start(ctx.today), value=c / total, unit="share")
            for src, c in counts.items()]
    rows.append(dict(company=company, source="chat_catalog", metric="catalog_size", entity="all",
                     period_type="W", period=week_start(ctx.today), value=float(total), unit="count"))
    return db.upsert_metrics(ctx.con, rows)


# -----------------------------------------------------------------------------
# 8) 수동 입력 병합 (Similarweb 월간, DART 부문/지역 확인값)
# -----------------------------------------------------------------------------
def ingest_manual(ctx, company, ccfg):
    n = 0
    sw = ROOT / "data" / "manual" / "similarweb.csv"
    if sw.exists():
        rows = []
        for rec in csv.DictReader(sw.open(encoding="utf-8")):
            if rec.get("company", company) != company:
                continue
            period = f"{rec['month']}-01"
            for metric in ("visits", "avg_duration_sec", "kr_share"):
                v = _num(rec.get(metric))
                if v is not None:
                    rows.append(dict(company=company, source="similarweb", metric=metric,
                                     entity=rec["domain"].strip(), period_type="M", period=period,
                                     value=v, unit={"visits": "visits", "avg_duration_sec": "sec",
                                                    "kr_share": "share"}[metric], status="MANUAL"))
        n += db.upsert_metrics(ctx.con, rows) if rows else 0
    # 직전 월 입력 여부 확인 (매월 3일 이후)
    if ctx.today.day >= 3:
        prev_m = (ctx.today.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat()
        have = ctx.con.execute("SELECT COUNT(*) FROM metrics WHERE company=? AND source='similarweb' AND period=?",
                               (company, prev_m)).fetchone()[0]
        if have == 0:
            db.log_run(ctx.con, company, "similarweb", "MANUAL_REQUIRED", 0,
                       f"{prev_m[:7]} Similarweb 수치 미입력 — 대시보드 입력폼 사용")
        else:
            db.log_run(ctx.con, company, "similarweb", "OK", have, f"{prev_m[:7]} 입력됨")
    seg = ROOT / "data" / "manual" / "segments.csv"
    if seg.exists():
        rows = []
        for rec in csv.DictReader(seg.open(encoding="utf-8")):
            if rec.get("company", company) != company:
                continue
            v = _num(rec.get("value_krw_100m"))
            if v is None:
                continue
            rows.append(dict(company=company, stock_code=ccfg["stock_code"], quarter=rec["quarter"],
                             account=rec["account"], scope=rec["scope"], value=v * 1e8,
                             basis=f"manual:{rec.get('source', '')}", rcept_no=rec.get("rcept_no")))
        n += db.upsert_financials(ctx.con, rows) if rows else 0
    return n


# -----------------------------------------------------------------------------
# 실행
# -----------------------------------------------------------------------------
SOURCES = ["manual", "naver", "gtrends", "fx", "tranco", "dart", "dart_docs", "pages", "catalog", "peers"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--company")
    ap.add_argument("--only", help="쉼표 구분: " + ",".join(SOURCES))
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config, encoding="utf-8"))
    ctx = Ctx(cfg)
    only = set(a.only.split(",")) if a.only else set(SOURCES)

    for cid, c in cfg["companies"].items():
        if (a.company and cid != a.company) or (not a.company and not c.get("enabled")):
            continue
        print(f"== {c['name']} ({cid})")
        if "manual" in only:    run_source(ctx, cid, "manual_input", ingest_manual, c)
        if "naver" in only:     run_source(ctx, cid, "naver_datalab", collect_naver, c)
        if "gtrends" in only:   run_source(ctx, cid, "google_trends", collect_gtrends, c)
        if "fx" in only:        run_source(ctx, cid, "fx", collect_fx, c)
        if "tranco" in only:    run_source(ctx, cid, "tranco", collect_tranco, c)
        if "dart" in only:      run_source(ctx, cid, "dart_financials", collect_dart_fin, c["stock_code"])
        if "dart_docs" in only: run_source(ctx, cid, "dart_documents", collect_dart_docs, c["stock_code"], c)
        if "pages" in only:     run_source(ctx, cid, "page_watch", collect_pages, c)
        if "catalog" in only:   run_source(ctx, cid, "chat_catalog", collect_catalog, c)

    if "peers" in only:
        print("== 동종사")
        for p in cfg.get("peers", []):
            run_source(ctx, f"peer:{p['name']}", "dart_financials", collect_dart_fin, p["stock_code"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
