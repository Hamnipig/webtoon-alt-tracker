"""
build_dashboard.py — DB → docs/data.json + docs/index.html (GitHub Pages로 게시)

  python build_dashboard.py
  python build_dashboard.py --as-of 2026-11-20   # 과거 시점 재현

- 차트 데이터, 나우캐스트, 백테스트, 데이터 상태, 이벤트, 코멘터리를 JSON 한 덩어리로 만들어
  templates/dashboard.html에 끼워 넣는다 (외부 서버 불필요).
- LLM 코멘터리(commentary/<company>.json)가 없거나 오래됐으면 규칙 기반 요약을 대신 보여준다.
"""
import argparse
import json
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

import db
import nowcast

ROOT = Path(__file__).parent


def clean(o):
    """NaN/inf → None (JSON 직렬화용)."""
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if hasattr(o, "item"):
        return clean(o.item())
    return o


def series(df, source, metric, entity, geo=None, since=None, period_type=None):
    m = (df.source == source) & (df.metric == metric) & (df.entity == entity)
    if geo is not None:
        m &= df.geo == geo
    s = df[m].sort_values("period")
    if since is not None:
        s = s[s.period >= pd.Timestamp(since)]
    return [[p.date().isoformat(), v] for p, v in zip(s.period, s.value)]


def weekly_resample(points, how="mean"):
    if not points:
        return []
    s = pd.Series([v for _, v in points], index=pd.to_datetime([p for p, _ in points]))
    r = s.resample("W-MON", label="left", closed="left")
    r = r.mean() if how == "mean" else r.last()
    return [[p.date().isoformat(), float(v)] for p, v in r.dropna().items()]


def status_table(con, company):
    q = """SELECT source, status, rows, message, MAX(run_at) AS run_at FROM run_log
           WHERE company=? GROUP BY source ORDER BY source"""
    return [dict(source=s, status=st, rows=r, message=m, run_at=t)
            for s, st, r, m, t in con.execute(q, (company,)).fetchall()]


def auto_summary(nc, status):
    """LLM 코멘터리가 없을 때 쓰는 규칙 기반 3줄 요약 (숫자만 사용)."""
    pct = lambda x: "N/A" if x is None else f"{x*100:+.1f}%"
    lines = [
        f"{nc['target_quarter']} QTD({nc['weeks_elapsed']}주 경과) 합산 신호 {pct(nc['g_total'])} → {nc['direction']} "
        f"(국내 {pct(nc['g_domestic'])}, 해외 {pct(nc['g_overseas'])}, 해외비중 {nc['overseas_share']*100:.0f}%)",
    ]
    if nc.get("revenue_nowcast"):
        lines.append(f"매출 추정 {nc['revenue_nowcast']/1e8:,.0f}억원 (기준 {nc['base_quarter']} "
                     f"{nc['revenue_base']/1e8:,.0f}억원, β={nc['beta']:.2f} · {nc['beta_note']})")
    else:
        lines.append("매출 추정 불가: 기준 분기 매출 또는 신호 부족")
    bad = [s["source"] for s in status if s["status"] in ("FAILED", "MANUAL_REQUIRED", "CONFIG_REQUIRED")]
    lines.append("데이터 점검 필요: " + ", ".join(bad) if bad else "모든 소스 정상 수집")
    return lines


def build_company(con, cid, ccfg, cfg, as_of):
    df = nowcast.load_metrics(con, cid)
    fin = nowcast.load_fin(con, cid)
    nc = nowcast.run(con, cid, ccfg, cfg, as_of)
    status = status_table(con, cid)
    since = (as_of - timedelta(days=365 * 3)).isoformat()

    nd = ccfg.get("naver_datalab", {})
    naver = {g["name"]: series(df, "naver_datalab", "search_index", g["name"], "KR", since)
             for g in nd.get("groups", []) + nd.get("peer_groups", [])}
    own_groups = [g["name"] for g in nd.get("groups", [])]

    gt_cfg = ccfg.get("google_trends") or {}
    gtrends = {}
    for geo in gt_cfg.get("geos", []):
        for kw in gt_cfg.get("keywords", []):
            pts = series(df, "google_trends", "search_index", kw, geo, since)
            if pts:
                gtrends[f"{geo} · {kw}"] = pts

    sw = {}
    for d in ccfg.get("domains", []):
        pts = series(df, "similarweb", "visits", d["domain"])
        if pts:
            sw[d["domain"]] = dict(points=pts, region=d["region"],
                                   duration=series(df, "similarweb", "avg_duration_sec", d["domain"]),
                                   kr_share=series(df, "similarweb", "kr_share", d["domain"]))

    fx = {}
    for pair in ccfg.get("fx_pairs", []):
        pts = weekly_resample(series(df, "fx", "close", pair, since=since), "mean")
        if pts:
            base = pts[0][1]
            fx[pair] = [[p, v / base * 100] for p, v in pts]   # 시작점=100 지수화 (단일 축)

    tranco = {}
    for d in ccfg.get("domains", []):
        pts = weekly_resample(series(df, "tranco", "rank", d["domain"], since=since), "mean")
        if pts:
            tranco[d["domain"]] = pts

    ip_mix = {}
    for src in ("own", "novelpia", "other", "unknown"):
        pts = series(df, "chat_catalog", "ip_share", src)
        if pts:
            ip_mix[src] = pts

    quarters = sorted(set(fin[fin.scope == "CFS"].quarter))
    fin_tbl = []
    for q in quarters:
        row = {"quarter": q}
        for acc in ("revenue", "operating_income", "net_income"):
            v = fin[(fin.quarter == q) & (fin.scope == "CFS") & (fin.account == acc)]
            row[acc] = float(v.value.iloc[0]) if not v.empty else None
            if acc == "revenue" and not v.empty:
                row["basis"] = v.basis.iloc[0]
        fin_tbl.append(row)
    seg_tbl = fin[fin.scope != "CFS"].sort_values(["quarter", "scope", "account"]).to_dict("records")
    rev_map = {r["quarter"]: r.get("revenue") for r in fin_tbl}
    region = {}
    for acc in ("domestic", "overseas"):
        sub = fin[(fin.scope == "REGION") & (fin.account == acc)].sort_values("quarter")
        region[acc] = [[q, v] for q, v in zip(sub.quarter, sub.value)]
    cost_ratio = {}
    for acc in sorted(set(fin[fin.scope == "COST"].account)):
        sub = fin[(fin.scope == "COST") & (fin.account == acc)].sort_values("quarter")
        cost_ratio[acc] = [[q, v / rev_map[q]] for q, v in zip(sub.quarter, sub.value) if rev_map.get(q)]
    chat = {}
    for s in ccfg.get("chat_sites", []):
        for metric, label in (("character_m2_delta_w", "주간 증분"), ("character_m2_total", "누적")):
            pts = series(df, "chat_stats", metric, s["id"])
            if pts:
                chat.setdefault(label, {})[s["id"]] = pts

    ev = pd.read_sql_query("SELECT * FROM events WHERE company=? ORDER BY detected_on DESC LIMIT 60",
                           con, params=(cid,))
    events = [dict(r, tags=json.loads(r["tags"] or "[]")) for r in ev.to_dict("records")]

    # LLM 코멘터리 (Claude 주간 예약 작업이 commentary/<cid>.json 으로 커밋)
    cpath = ROOT / "commentary" / f"{cid}.json"
    commentary = None
    if cpath.exists():
        commentary = json.loads(cpath.read_text(encoding="utf-8"))
        try:
            age = (as_of - date.fromisoformat(commentary.get("as_of_date", "1900-01-01"))).days
            commentary["_stale"] = age > 10
        except ValueError:
            commentary["_stale"] = True

    return dict(id=cid, name=ccfg["name"], stock_code=ccfg["stock_code"],
                nowcast=nc, status=status, auto_summary=auto_summary(nc, status),
                commentary=commentary, naver=naver, own_groups=own_groups, gtrends=gtrends,
                similarweb=sw, fx=fx, tranco=tranco, ip_mix=ip_mix, financials=fin_tbl,
                segments=seg_tbl, events=events, region=region, cost_ratio=cost_ratio, chat=chat,
                domains=[d["domain"] for d in ccfg.get("domains", [])])


def build_peers(con, cfg):
    out = []
    for p in cfg.get("peers", []):
        fin = nowcast.load_fin(con, f"peer:{p['name']}")
        rev = fin[(fin.scope == "CFS") & (fin.account == "revenue")].sort_values("quarter")
        out.append(dict(name=p["name"], revenue=[[q, v] for q, v in zip(rev.quarter, rev.value)]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--as-of")
    ap.add_argument("--out", default=str(ROOT / "docs"))
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config, encoding="utf-8"))
    as_of = date.fromisoformat(a.as_of) if a.as_of else date.today()
    con = db.connect(str(ROOT / cfg["db_path"]))

    companies = [build_company(con, cid, c, cfg, as_of)
                 for cid, c in cfg["companies"].items() if c.get("enabled")]
    payload = clean(dict(generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"), as_of=as_of.isoformat(),
                         repo=os.getenv("GITHUB_REPOSITORY", ""), companies=companies,
                         peers=build_peers(con, cfg)))
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    js = json.dumps(payload, ensure_ascii=False)
    (out / "data.json").write_text(js, encoding="utf-8")
    tpl = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    (out / "index.html").write_text(tpl.replace("/*__DATA__*/null", js.replace("</", "<\\/")), encoding="utf-8")
    print(f"dashboard → {out/'index.html'} ({len(companies)} companies)")


if __name__ == "__main__":
    main()
