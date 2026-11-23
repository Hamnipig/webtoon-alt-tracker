"""
evaluate.py — 투자 판단용 평가 레이어

  1) 지표 스코어카드: 각 대체데이터 지표가 '분기 매출 QoQ'를 얼마나 설명했는지 (상관·방향 적중·표본 수)
     → 검증 기준(n≥6, 적중률≥60%, 상관≥0.3)을 통과한 지표만 나우캐스트 조정에 사용
  2) 실적 프리뷰: 기준선(직전 분기 매출, 과거 평균 오차로 범위 표시) + 검증된 지표의 조정
  3) 정합성 검사: 지역 매출 분류 급변, 매출합 불일치, 비용 이상치, 수집 실패
  4) 실적 지표 표(KPI), 동종사 비교, 다음 공시 예상일, 밸류에이션
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd

import nowcast as nc

GATE = dict(n=6, hit=0.6, corr=0.3)


# -----------------------------------------------------------------------------
# 공통
# -----------------------------------------------------------------------------
def fin_series(fin, scope, acc, clean=True):
    d = fin[(fin.scope == scope) & (fin.account == acc)]
    if clean:
        d = d[~d.basis.fillna("").str.startswith("⚠")]
    return d.set_index("quarter")["value"].sort_index()


def quarterly(df, source, entity, geo=None, metric="search_index", how="sum", as_of=None, min_weeks=11):
    s = nc.weekly(df, source, entity, geo, metric)
    if s.empty:
        return pd.Series(dtype=float)
    if as_of is not None:
        s = s[s.index + pd.Timedelta(days=6) <= pd.Timestamp(as_of)]
    if source == "similarweb":                      # 월간 → 분기 합, 3개월 모두 있어야
        g = s.groupby(s.index.map(nc.q_of))
        out = g.sum()[g.count() >= 3]
        return out
    g = s.groupby(s.index.map(nc.q_of))
    return (g.sum()[g.count() >= min_weeks]) if how == "sum" else g.mean()


def growth(s, k=1):
    out = {}
    for q in s.index:
        p = nc.q_shift(q, -k)
        if p in s.index and s[p]:
            out[q] = s[q] / s[p] - 1
    return pd.Series(out, dtype=float)


def fit(ind, tgt, start=None, excl=()):
    j = pd.concat([growth(ind), growth(tgt)], axis=1, keys=["i", "t"]).dropna()
    if j.empty:
        return dict(n=0, corr=None, hit=None)
    if start:
        j = j[j.index >= start]
    j = j[~j.index.isin(excl)]
    if len(j) < 3:
        return dict(n=len(j), corr=None, hit=None)
    c = j.i.corr(j.t)
    return dict(n=int(len(j)), corr=None if pd.isna(c) else float(c),
                hit=float((np.sign(j.i) == np.sign(j.t)).mean()))


def passes(r):
    return (r["n"] >= GATE["n"] and r["hit"] is not None and r["hit"] >= GATE["hit"]
            and r["corr"] is not None and r["corr"] >= GATE["corr"])


# -----------------------------------------------------------------------------
# 1) 스코어카드
# -----------------------------------------------------------------------------
def candidates(df, ccfg, as_of):
    """(이름, 지역, 분기 시계열, QTD 성장률 계산용 주간/월간 시계열, 주기)"""
    out = []
    groups = [g["name"] for g in ccfg.get("naver_datalab", {}).get("groups", [])]
    if groups:
        wk = nc._sum_series([nc.weekly(df, "naver_datalab", g, "KR") for g in groups], fill_zero=True)
        q = wk[wk.index + pd.Timedelta(days=6) <= pd.Timestamp(as_of)]
        qq = q.groupby(q.index.map(nc.q_of))
        out.append(("네이버 검색(자사 합산)", "domestic", qq.sum()[qq.count() >= 11], wk, "W"))
    gt = ccfg.get("google_trends") or {}
    for geo in gt.get("geos", []):
        for kw in gt.get("keywords", []):
            s = quarterly(df, "google_trends", kw, geo, as_of=as_of)
            if len(s) >= 4 and s.sum() > 0:
                out.append((f"Google Trends {geo} '{kw}'", "overseas", s,
                            nc.weekly(df, "google_trends", kw, geo), "W"))
    for s_ in ccfg.get("chat_sites", []):
        m = f"character_{s_.get('signal_metric', 'm2')}_delta_w"
        out.append((f"챗 활동량 {s_['id']}", s_["region"], quarterly(df, "chat_stats", s_["id"], metric=m, as_of=as_of),
                    nc.weekly(df, "chat_stats", s_["id"], metric=m), "W"))
    for p in ccfg.get("rank_pages", []):
        if p.get("parser") == "toptoon":   # 작품 조회수가 공개된 랭킹만 소비량 지표로 평가
            out.append((f"랭킹 조회수 증분 {p['id']}", p.get("region", "domestic"),
                        quarterly(df, "rank_stats", p["id"], metric="views_delta_w", as_of=as_of),
                        nc.weekly(df, "rank_stats", p["id"], metric="views_delta_w"), "W"))
    for d in ccfg.get("domains", []):
        reg = "domestic" if d["region"] in ("domestic", "split") else "overseas"
        v = nc._region_visits(df, d, reg)
        out.append((f"Similarweb {d['domain']}", reg, quarterly(df, "similarweb", d["domain"], metric="visits") if not v.empty else pd.Series(dtype=float), v, "M"))
    return out


def scorecard(df, fin, ccfg, as_of):
    brk = ccfg.get("structural_breaks", {})
    first = brk.get("domestic_first_full_quarter", "2000Q1")
    dom, ovs, tot = fin_series(fin, "REGION", "domestic"), fin_series(fin, "REGION", "overseas"), fin_series(fin, "CFS", "revenue", clean=False)
    rows = []
    for name, region, qs, raw, freq in candidates(df, ccfg, as_of):
        tgt = dom if region == "domestic" else ovs
        tname = "국내 매출" if region == "domestic" else "해외 매출"
        if tgt.empty:
            tgt, tname = tot, "전체 매출"
        start = nc.q_shift(first, 1) if region == "domestic" else None
        r = fit(qs, tgt, start=start, excl=(first,))
        status = "채택" if passes(r) else ("검증 대기(표본 부족)" if r["n"] < GATE["n"] else "검증 미달")
        rows.append(dict(indicator=name, region=region, target=tname, quarters=int(len(qs)), status=status, **r,
                         _raw=raw, _freq=freq))
    return rows


# -----------------------------------------------------------------------------
# 2) 실적 프리뷰
# -----------------------------------------------------------------------------
def baseline_error(rev, start):
    """직전 분기 매출을 그대로 쓰는 기준선의 과거 평균 절대 오차(%)."""
    errs = [abs(rev[nc.q_shift(q, -1)] / rev[q] - 1) for q in rev.index
            if q >= start and nc.q_shift(q, -1) in rev.index and rev[q]]
    return (float(np.mean(errs)), len(errs)) if errs else (None, 0)


def preview(df, fin, ccfg, cards, as_of, alt_nc):
    rev = fin_series(fin, "CFS", "revenue", clean=False)
    if rev.empty:
        return {}
    last = rev.index[-1]
    target = nc.q_shift(last, 1)
    brk = ccfg.get("structural_breaks", {})
    start = nc.q_shift(brk.get("domestic_first_full_quarter", "2000Q1"), 1)
    mape, n_err = baseline_error(rev, start)
    base = float(rev[last])
    # 검증 통과 지표의 QTD 성장률로 지역별 조정
    adj, used = {}, []
    for c in cards:
        if c["status"] != "채택":
            continue
        g, _ = (nc.qtd_growth_weekly if c["_freq"] == "W" else nc.qtd_growth_monthly)(c["_raw"], target, as_of)
        if g is not None:
            adj.setdefault(c["region"], []).append(g); used.append(c["indicator"])
    dom, ovs = fin_series(fin, "REGION", "domestic"), fin_series(fin, "REGION", "overseas")
    if last in dom.index and last in ovs.index and adj:
        est = sum(float(s[last]) * (1 + float(np.mean(adj[r]))) if r in adj else float(s[last])
                  for r, s in (("domestic", dom), ("overseas", ovs)))
        est *= base / float(dom[last] + ovs[last])          # 지역합과 연결매출 차이 보정
    elif adj:
        est = base * (1 + float(np.mean(sum(adj.values(), []))))
    else:
        est = base
    yoy_base = rev.get(nc.q_shift(target, -4))
    if yoy_base is not None and nc.q_shift(target, -4) < brk.get("domestic_first_full_quarter", "2000Q1"):
        yoy_base = None                                  # 합병 전 분기와의 YoY는 의미 없음
    # 과거 같은 분기의 QoQ 패턴 (계절성 참고 — 기준선에는 반영하지 않음: 전 분기 일률 적용 시 오차가 더 컸음)
    first = brk.get("domestic_first_full_quarter", "2000Q1")
    same = [(q, float(rev[q] / rev[nc.q_shift(q, -1)] - 1)) for q in rev.index
            if q[-1] == target[-1] and nc.q_shift(q, -1) in rev.index and nc.q_shift(q, -1) >= first and q != first]
    return dict(
        seasonal=dict(history=same, up=sum(1 for _, g in same if g > 0), n=len(same),
                      median=float(np.median([g for _, g in same])) if same else None),
        target_quarter=target, last_quarter=last, last_revenue=base,
        estimate=est, method="기준선(직전 분기)" + (" + 검증 지표 조정: " + ", ".join(used) if used else ""),
        band_pct=mape, band_n=n_err,
        low=est * (1 - mape) if mape else None, high=est * (1 + mape) if mape else None,
        implied_yoy=(est / yoy_base - 1) if yoy_base else None, yoy_base=float(yoy_base) if yoy_base else None,
        raw_alt_signal=alt_nc.get("g_total"), raw_alt_direction=alt_nc.get("direction"),
        validated=bool(used),
    )


# -----------------------------------------------------------------------------
# 3) 정합성 검사
# -----------------------------------------------------------------------------
def integrity(fin, status_rows):
    issues = []
    for _, r in fin[fin.basis.fillna("").str.startswith("⚠")].drop_duplicates("quarter").iterrows():
        issues.append(dict(level="경고", item=f"{r.quarter} 지역 매출", detail=r.basis.split("·")[0].strip("⚠ ").strip()))
    dom, ovs = fin_series(fin, "REGION", "domestic"), fin_series(fin, "REGION", "overseas")
    share = (ovs / (dom + ovs)).dropna()
    qs = list(share.index)
    for i in range(1, len(qs) - 1):
        nb = (share[qs[i - 1]] + share[qs[i + 1]]) / 2
        if abs(share[qs[i]] - nb) > 0.15 and abs(share[qs[i - 1]] - share[qs[i + 1]]) < 0.10:
            issues.append(dict(level="경고", item=f"{qs[i]} 지역 매출",
                               detail=f"해외 비중 {share[qs[i]]:.0%} — 앞뒤 분기({share[qs[i-1]]:.0%}, {share[qs[i+1]]:.0%})와 급변. "
                                      "공시 내 국내/해외 분류가 보고서마다 다를 수 있음 → 지역별 추세 판단에서 제외"))
    rev = fin_series(fin, "CFS", "revenue", clean=False)
    for acc in sorted(set(fin[fin.scope == "COST"].account)):
        c = fin_series(fin, "COST", acc)
        ratio = (c / rev).dropna()
        if len(ratio) >= 4:
            med = ratio.median()
            for q, v in ratio.items():
                if med > 0 and (v > med * 3 or v < med / 3 or v < 0):
                    issues.append(dict(level="주의", item=f"{q} {acc}",
                                       detail=f"매출 대비 {v:.0%} (중앙값 {med:.0%}) — 표 구조 차이로 인한 파싱 오류 가능"))
    for q, v in rev.items():
        if v <= 0:
            issues.append(dict(level="경고", item=f"{q} 매출", detail="0 이하 — 4분기 산출(연간−3Q누적) 오류 가능"))
    for s in status_rows:
        if s["status"] in ("FAILED", "CONFIG_REQUIRED", "MANUAL_REQUIRED"):
            issues.append(dict(level="수집", item=s["source"], detail=f"{s['status']} · {(s.get('message') or '')[:120]}"))
    return issues


def suspect_quarters(issues):
    return {i["item"].split()[0] for i in issues if i["item"].endswith("지역 매출")}


# -----------------------------------------------------------------------------
# 4) KPI 표·공시일·동종사·밸류에이션
# -----------------------------------------------------------------------------
def kpi_table(fin, issues, n=6, first_full="2000Q1"):
    rev, op = fin_series(fin, "CFS", "revenue", clean=False), fin_series(fin, "CFS", "operating_income", clean=False)
    ni = fin_series(fin, "CFS", "net_income", clean=False)
    dom, ovs = fin_series(fin, "REGION", "domestic", clean=False), fin_series(fin, "REGION", "overseas", clean=False)
    sus = suspect_quarters(issues)
    costs = {a: fin_series(fin, "COST", a) for a in sorted(set(fin[fin.scope == "COST"].account))}
    bad_cost = {(i["item"].split()[0], i["item"].split()[1]) for i in issues if i["level"] == "주의"}
    rows = []
    for q in rev.index[-n:]:
        y = nc.q_shift(q, -4); p = nc.q_shift(q, -1)
        g = lambda s, k: (s[q] / s[k] - 1) if (q in s.index and k in s.index and s[k] and k >= first_full) else None
        go = lambda s, k: (s[q] / s[k] - 1) if (q in s.index and k in s.index and s[k]) else None   # 해외는 합병과 무관
        row = dict(quarter=q, revenue=rev.get(q), rev_qoq=g(rev, p), rev_yoy=g(rev, y),
                   op=op.get(q), opm=(op[q] / rev[q]) if q in op.index and rev[q] else None,
                   ni=ni.get(q), domestic=dom.get(q), overseas=ovs.get(q),
                   dom_yoy=g(dom, y), ovs_yoy=go(ovs, y), region_suspect=q in sus or y in sus)
        for a, s in costs.items():
            row[f"cost_{a}"] = (s[q] / rev[q]) if (q in s.index and (q, a) not in bad_cost and rev[q]) else None
        rows.append(row)
    return rows


def next_filing(fin, target_q):
    """작년 같은 분기 보고서 접수일(rcept_no 앞 8자리) + 1년을 다음 공시 예상일로 사용."""
    last_year = nc.q_shift(target_q, -4)
    r = fin[(fin.quarter == last_year) & (fin.scope == "CFS") & (fin.account == "revenue")]
    if r.empty or not r.rcept_no.iloc[0]:
        return None
    d = r.rcept_no.iloc[0][:8]
    try:
        prev = date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        return (prev.replace(year=prev.year + 1)).isoformat()
    except ValueError:
        return None


def peers(con, cfg):
    out = []
    for p in cfg.get("peers", []):
        fin = nc.load_fin(con, f"peer:{p['name']}")
        rev = fin_series(fin, "CFS", "revenue", clean=False)
        op = fin_series(fin, "CFS", "operating_income", clean=False)
        if rev.empty:
            continue
        q = rev.index[-1]; y = nc.q_shift(q, -4)
        out.append(dict(name=p["name"], quarter=q, revenue=float(rev[q]),
                        yoy=float(rev[q] / rev[y] - 1) if y in rev.index and rev[y] else None,
                        opm=float(op[q] / rev[q]) if q in op.index and rev[q] else None))
    return out


def valuation(df, fin):
    px = nc.weekly(df, "market", "close", metric="close")
    sh = nc.weekly(df, "market", "shares", metric="shares")
    if px.empty or sh.empty:
        return None
    price, shares = float(px.iloc[-1]), float(sh.iloc[-1])
    mcap = price * shares
    rev, op, ni = (fin_series(fin, "CFS", a, clean=False) for a in ("revenue", "operating_income", "net_income"))
    ttm = lambda s: float(s.iloc[-4:].sum()) if len(s) >= 4 else None
    r, o, n = ttm(rev), ttm(op), ttm(ni)
    return dict(price=price, price_date=px.index[-1].date().isoformat(), shares=shares, mcap=mcap,
                ttm_revenue=r, ttm_op=o, ttm_ni=n,
                psr=mcap / r if r else None, por=mcap / o if o and o > 0 else None,
                per=mcap / n if n and n > 0 else None, ttm_through=rev.index[-1] if len(rev) else None)
