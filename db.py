"""
SQLite 스키마 및 Upsert 헬퍼.

모든 시계열은 하나의 long-format 테이블(metrics)에 저장한다.
(company, source, metric, entity, geo, period) 가 PK → 같은 값을 다시 수집해도 중복 없이 덮어쓴다.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    company      TEXT NOT NULL,
    source       TEXT NOT NULL,   -- naver_datalab / google_trends / similarweb / tranco / fx / page_watch ...
    metric       TEXT NOT NULL,   -- search_index / visits / avg_duration_sec / rank / close / new_items ...
    entity       TEXT NOT NULL,   -- 키워드 그룹명 / 도메인 / 통화쌍 / 페이지 id
    geo          TEXT NOT NULL DEFAULT '',  -- KR / JP / TW / US / '' (해당 없음)
    period_type  TEXT NOT NULL,   -- W(주, 월요일 시작일) / M(월, YYYY-MM-01) / D(일)
    period       TEXT NOT NULL,   -- ISO 날짜
    value        REAL,
    unit         TEXT,
    status       TEXT DEFAULT 'OK',  -- OK / MANUAL / ESTIMATED
    collected_at TEXT NOT NULL,
    PRIMARY KEY (company, source, metric, entity, geo, period)
);

CREATE TABLE IF NOT EXISTS financials (
    company     TEXT NOT NULL,
    stock_code  TEXT NOT NULL,
    quarter     TEXT NOT NULL,     -- 2026Q3
    account     TEXT NOT NULL,     -- revenue / operating_income / net_income / 부문·지역·비용 항목
    scope       TEXT NOT NULL,     -- CFS(연결) / SEG(부문) / REGION(지역) / COST(비용)
    value       REAL,              -- 원 단위, '분기 단독' 값
    basis       TEXT,              -- reported / derived(누적 차감) / manual / parsed
    rcept_no    TEXT,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (company, quarter, account, scope)
);

CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,  -- url 또는 제목 해시
    company     TEXT NOT NULL,
    detected_on TEXT NOT NULL,
    page_id     TEXT,
    region      TEXT,
    title       TEXT,
    url         TEXT,
    tags        TEXT               -- JSON 배열: ["free_promo","new_title","launch",...]
);

CREATE TABLE IF NOT EXISTS catalog (
    company       TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    item_key      TEXT NOT NULL,
    name          TEXT,
    origin        TEXT,
    ip_source     TEXT,            -- own / novelpia / other / unknown
    PRIMARY KEY (company, snapshot_date, item_key)
);

CREATE TABLE IF NOT EXISTS snapshots (
    company     TEXT NOT NULL,
    page_id     TEXT NOT NULL,
    taken_on    TEXT NOT NULL,
    content_hash TEXT,
    links_json  TEXT,
    PRIMARY KEY (company, page_id, taken_on)
);

CREATE TABLE IF NOT EXISTS run_log (
    run_at   TEXT NOT NULL,
    company  TEXT NOT NULL,
    source   TEXT NOT NULL,
    status   TEXT NOT NULL,        -- OK / FAILED / SKIPPED / CONFIG_REQUIRED / MANUAL_REQUIRED
    rows     INTEGER,
    message  TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def upsert_metrics(con, rows):
    """rows: dict 목록. 필수 키 company, source, metric, entity, period_type, period, value"""
    ts = now_iso()
    data = [(
        r["company"], r["source"], r["metric"], r["entity"], r.get("geo", ""),
        r["period_type"], r["period"], r["value"], r.get("unit"), r.get("status", "OK"), ts,
    ) for r in rows]
    con.executemany(
        """INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(company, source, metric, entity, geo, period)
           DO UPDATE SET value=excluded.value, unit=excluded.unit,
                         status=excluded.status, collected_at=excluded.collected_at""",
        data,
    )
    con.commit()
    return len(data)


def upsert_financials(con, rows):
    ts = now_iso()
    data = [(r["company"], r["stock_code"], r["quarter"], r["account"], r["scope"],
             r["value"], r.get("basis"), r.get("rcept_no"), ts) for r in rows]
    con.executemany(
        """INSERT INTO financials VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(company, quarter, account, scope)
           DO UPDATE SET value=excluded.value, basis=excluded.basis,
                         rcept_no=excluded.rcept_no, collected_at=excluded.collected_at""",
        data,
    )
    con.commit()
    return len(data)


def insert_event(con, ev) -> bool:
    """신규 이벤트면 True. 이미 본 이벤트는 무시(최초 감지일 유지)."""
    cur = con.execute(
        "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?)",
        (ev["event_id"], ev["company"], ev["detected_on"], ev.get("page_id"),
         ev.get("region"), ev.get("title"), ev.get("url"), json.dumps(ev.get("tags", []), ensure_ascii=False)),
    )
    con.commit()
    return cur.rowcount > 0


def log_run(con, company, source, status, rows=0, message=""):
    con.execute("INSERT INTO run_log VALUES (?,?,?,?,?,?)",
                (now_iso(), company, source, status, rows, (message or "")[:500]))
    con.commit()
