"""
오프라인 테스트용 '합성' 데이터 생성기. 실제 수치가 아님 — 파이프라인 동작 확인 전용.
  python tests/make_mock_data.py && python build_dashboard.py --config tests/mock_config.yaml --out tests/out --as-of 2026-11-20
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import db  # noqa: E402

rng = np.random.default_rng(7)
cfg = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
cfg["db_path"] = "tests/mock.db"
yaml.safe_dump(cfg, open(ROOT / "tests" / "mock_config.yaml", "w", encoding="utf-8"), allow_unicode=True)
p = ROOT / "tests" / "mock.db"
p.unlink(missing_ok=True)
con = db.connect(str(p))
C = "enkeyax"

weeks = [date(2022, 1, 3) + timedelta(weeks=i) for i in range(255)]
rows = []
for i, w in enumerate(weeks):
    season = 1 + 0.08 * np.sin(2 * np.pi * (w.timetuple().tm_yday / 365))
    base = 40 + (25 if w >= date(2025, 4, 1) else 0) + 0.05 * i
    rows.append(dict(company=C, source="naver_datalab", metric="search_index", entity="탑툰", geo="KR",
                     period_type="W", period=w.isoformat(), value=float(base * season * rng.normal(1, .04))))
    chat = 0 if w < date(2026, 3, 1) else 5 + 0.6 * (w - date(2026, 3, 1)).days / 7
    rows.append(dict(company=C, source="naver_datalab", metric="search_index", entity="탑툰챗", geo="KR",
                     period_type="W", period=w.isoformat(), value=float(max(chat * rng.normal(1, .1), 0))))
    for peer, lvl in (("봄툰", 55), ("레진코믹스", 35)):
        rows.append(dict(company=C, source="naver_datalab", metric="search_index", entity=peer, geo="KR",
                         period_type="W", period=w.isoformat(), value=float(lvl * season * rng.normal(1, .05))))
    for geo, lvl in (("JP", 30), ("TW", 15)):
        rows.append(dict(company=C, source="google_trends", metric="search_index", entity="toptoon", geo=geo,
                         period_type="W", period=w.isoformat(), value=float((lvl + 0.08 * i) * rng.normal(1, .08))))
    for pair, lvl in (("JPYKRW", 9.2), ("TWDKRW", 43), ("USDKRW", 1350)):
        rows.append(dict(company=C, source="fx", metric="close", entity=pair, period_type="D",
                         period=w.isoformat(), value=float(lvl * (1 + 0.0004 * i) * rng.normal(1, .01))))
    if w >= date(2026, 6, 1):
        rows.append(dict(company=C, source="tranco", metric="rank", entity="toptoon.com", period_type="D",
                         period=w.isoformat(), value=float(42000 - 60 * (i - 230) + rng.normal(0, 800))))
for m in range(1, 11):
    for dom, v in (("toptoon.com", 9e6), ("toptoon.jp", 6e6), ("chat.toptoon.com", 0.3e6 * m)):
        rows.append(dict(company=C, source="similarweb", metric="visits", entity=dom, period_type="M",
                         period=f"2026-{m:02d}-01", value=float(v * (1 + 0.02 * m) * rng.normal(1, .03)), status="MANUAL"))
    rows.append(dict(company=C, source="similarweb", metric="kr_share", entity="chat.toptoon.com",
                     period_type="M", period=f"2026-{m:02d}-01", value=0.75, status="MANUAL"))
for k, wk in enumerate([date(2026, 9, 28), date(2026, 10, 5), date(2026, 10, 12)]):
    for src, sh in (("own", .7 - .05 * k), ("novelpia", .25 + .05 * k), ("unknown", .05)):
        rows.append(dict(company=C, source="chat_catalog", metric="ip_share", entity=src,
                         period_type="W", period=wk.isoformat(), value=sh))
rows.append(dict(company=C, source="page_watch", metric="free_promo", entity="kr_event", geo="domestic",
                 period_type="W", period="2026-10-05", value=2.0))
db.upsert_metrics(con, rows)

fin, rev = [], 55e8
for y in range(2022, 2027):
    for q in range(1, 5):
        if (y, q) > (2026, 3):
            break
        rev *= 2.4 if (y, q) == (2025, 2) else rng.normal(1.04, .05)
        fin += [dict(company=C, stock_code="134580", quarter=f"{y}Q{q}", account="revenue", scope="CFS",
                     value=rev, basis="synthetic"),
                dict(company=C, stock_code="134580", quarter=f"{y}Q{q}", account="operating_income", scope="CFS",
                     value=rev * (0.18 if y >= 2026 else -0.05), basis="synthetic")]
db.upsert_financials(con, fin)
db.insert_event(con, dict(event_id="x1", company=C, detected_on="2026-10-06", page_id="kr_event",
                          region="domestic", title="(합성) 신규가입 100코인 무료 증정", url="https://toptoon.com/event",
                          tags=["free_promo"]))
for s, st in (("naver_datalab", "OK"), ("dart_financials", "OK"), ("google_trends", "FAILED"),
              ("similarweb", "MANUAL_REQUIRED"), ("chat_catalog", "CONFIG_REQUIRED")):
    db.log_run(con, C, s, st, 1, "synthetic")
print("mock db →", p)
