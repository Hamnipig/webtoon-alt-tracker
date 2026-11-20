"""
nowcast.py — 지역 분리 QTD 나우캐스팅 + 백테스트

모델 구조
  국내 신호  g_dom : 네이버 데이터랩(자사 키워드 그룹) + Similarweb 국내 도메인 방문수(chat은 KR 비중만)
  해외 신호  g_ovs : Similarweb 해외 도메인 방문수 (없으면 Google Trends JP/TW로 대체)
                     × 환율 효과 (분기 평균 환율 QoQ)
  합산 신호  g_tot = (1-s)·g_dom + s·g_ovs     s = 최근 해외 매출 비중 (DART 확인값 없으면 config 기본값)

  QTD 성장률: 이번 분기 경과 k주(또는 k개월) 합계 ÷ 직전 분기 같은 k주 합계 − 1
  계절성: 네이버 백필(합병 이전 구간 포함)로 분기별 계절 계수를 구해 '계절조정 신호'를 따로 표시
  매출 추정: 직전 분기 매출 × (1 + β·g_tot)
            β는 백테스트 구간 분기들로 원점 통과 회귀. 표본이 min_obs_for_beta 미만이면 β=1(가정)로 표시

  백테스트 구간
    - 국내/합산: 합병 후 첫 온전한 분기의 '다음' 분기부터 (QoQ 비교가 합병 전 분기를 건드리지 않도록)
    - 해외: 합병 전 상장사 매출 ≈ 일본 사업이므로 합병 전 분기도 사용. 단 유통→플랫폼 전환 전후로 적중률 분리
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd


# -----------------------------------------------------------------------------
# 분기 유틸
# -----------------------------------------------------------------------------
def q_of(d) -> str:
    d = pd.Timestamp(d)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def q_start(q: str) -> date:
    y, n = int(q[:4]), int(q[-1])
    return date(y, 3 * (n - 1) + 1, 1)


def q_shift(q: str, k: int) -> str:
    y, n = int(q[:4]), int(q[-1])
    idx = y * 4 + (n - 1) + k
    return f"{idx // 4}Q{idx % 4 + 1}"


def q_ge(a: str, b: str) -> bool:
    return (int(a[:4]), int(a[-1])) >= (int(b[:4]), int(b[-1]))


# -----------------------------------------------------------------------------
# 데이터 로드
# -----------------------------------------------------------------------------
def load_metrics(con, company):
    df = pd.read_sql_query("SELECT * FROM metrics WHERE company=?", con, params=(company,))
    if not df.empty:
        df["period"] = pd.to_datetime(df["period"])
    return df


def load_fin(con, company):
    return pd.read_sql_query("SELECT * FROM financials WHERE company=?", con, params=(company,))


def weekly(df, source, entity, geo=None, metric="search_index"):
    m = (df.source == source) & (df.entity == entity) & (df.metric == metric)
    if geo is not None:
        m &= df.geo == geo
    s = df[m].set_index("period")["value"].sort_index()
    if s.empty:   # 데이터가 아직 없는 종목/소스 → 빈 날짜 인덱스로 반환 (비교 연산 오류 방지)
        return pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    return s[~s.index.duplicated()]


# -----------------------------------------------------------------------------
# 성장률 계산
# -----------------------------------------------------------------------------
def qtd_growth_weekly(s: pd.Series, q: str, as_of: date):
    """이번 분기 경과 완결 주 k개 합 vs 직전 분기 첫 k주 합."""
    if s.empty:
        return None, 0
    qs, qn = pd.Timestamp(q_start(q)), pd.Timestamp(q_start(q_shift(q, 1)))
    cur = s[(s.index >= qs) & (s.index < qn) & (s.index + pd.Timedelta(days=6) <= pd.Timestamp(as_of))]
    k = len(cur)
    if k == 0:
        return None, 0
    ps = pd.Timestamp(q_start(q_shift(q, -1)))
    prev = s[(s.index >= ps) & (s.index < qs)].iloc[:k]
    if len(prev) < k or prev.sum() <= 0:
        return None, k
    return float(cur.sum() / prev.sum() - 1), k


def qtd_growth_monthly(s: pd.Series, q: str, as_of: date):
    """월간 시계열(Similarweb). 이번 분기 '완결 월' 수만큼 직전 분기와 비교."""
    if s.empty:
        return None, 0
    qs, qn = pd.Timestamp(q_start(q)), pd.Timestamp(q_start(q_shift(q, 1)))
    first_of_this_month = pd.Timestamp(as_of.replace(day=1))
    cur = s[(s.index >= qs) & (s.index < qn) & (s.index < first_of_this_month)]
    k = len(cur)
    if k == 0:
        return None, 0
    ps = pd.Timestamp(q_start(q_shift(q, -1)))
    prev = s[(s.index >= ps) & (s.index < qs)].iloc[:k]
    if len(prev) < k or prev.sum() <= 0:
        return None, k
    return float(cur.sum() / prev.sum() - 1), k


def fx_qoq(df, pair, q, as_of):
    s = weekly(df, "fx", pair, metric="close")
    if s.empty:
        return None
    qs, qn = pd.Timestamp(q_start(q)), pd.Timestamp(q_start(q_shift(q, 1)))
    ps = pd.Timestamp(q_start(q_shift(q, -1)))
    cur = s[(s.index >= qs) & (s.index < qn) & (s.index <= pd.Timestamp(as_of))]
    prev = s[(s.index >= ps) & (s.index < qs)]
    if cur.empty or prev.empty:
        return None
    return float(cur.mean() / prev.mean() - 1)


def seasonal_factors(s: pd.Series):
    """분기별 계절 계수 = (해당 분기 합 / 그해 분기 평균)의 연도 평균. 완결 연도만 사용."""
    if s.empty:
        return {}
    qsum = s.groupby([s.index.year, (s.index.month - 1) // 3 + 1]).sum()
    years = [y for y in qsum.index.get_level_values(0).unique()
             if all((y, n) in qsum.index for n in (1, 2, 3, 4))]
    if not years:
        return {}
    ratios = {n: [] for n in (1, 2, 3, 4)}
    for y in years:
        avg = np.mean([qsum[(y, n)] for n in (1, 2, 3, 4)])
        for n in (1, 2, 3, 4):
            ratios[n].append(qsum[(y, n)] / avg if avg else np.nan)
    return {n: float(np.nanmean(v)) for n, v in ratios.items()} | {"years": years}


def seasonally_adjust(g, q, sf):
    if g is None or not sf or 1 not in sf:
        return None
    n, pn = int(q[-1]), int(q_shift(q, -1)[-1])
    return (1 + g) / (sf[n] / sf[pn]) - 1


def mean_valid(vals):
    v = [x for x in vals if x is not None and np.isfinite(x)]
    return float(np.mean(v)) if v else None


# -----------------------------------------------------------------------------
# 신호 구성
# -----------------------------------------------------------------------------
def _sum_series(series_list, fill_zero=False):
    """같은 단위 시계열 합산.
    fill_zero=True: 네이버 데이터랩은 검색이 없는 주를 응답에서 빼므로 결측=0으로 보고 합산
    fill_zero=False: 수동 입력 등은 모든 구성요소가 있는 시점만 합산"""
    s = [x for x in series_list if not x.empty]
    if not s:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    df = pd.concat(s, axis=1)
    return (df.fillna(0) if fill_zero else df.dropna()).sum(axis=1)


def _region_visits(df, d, region):
    """도메인 방문수. split 도메인은 kr_share로 국내/해외 몫만 떼어낸다."""
    v = weekly(df, "similarweb", d["domain"], metric="visits")
    if d["region"] == "split":
        sh = weekly(df, "similarweb", d["domain"], metric="kr_share")
        if sh.empty:
            return pd.Series(dtype=float, index=pd.DatetimeIndex([]))   # 국가 비중 없으면 섞어 쓰지 않음
        sh = sh.reindex(v.index)
        v = (v * (sh if region == "domestic" else 1 - sh)).dropna()
    return v


def domestic_signal(df, ccfg, q, as_of):
    """국내 신호 = 평균(네이버 자사 키워드 지수 합계의 QTD 성장, 국내 방문수 합계의 QTD 성장).
    성장률을 그룹별로 평균하면 기저가 작은 신규 키워드(탑툰챗)가 신호를 지배하므로,
    같은 요청에서 받아 스케일이 같은 네이버 지수는 '합산 후' 성장률을 구한다."""
    parts = {}
    groups = ccfg.get("naver_datalab", {}).get("groups", [])
    naver = _sum_series([weekly(df, "naver_datalab", g["name"], "KR") for g in groups], fill_zero=True)
    parts["naver:자사합계"], _ = qtd_growth_weekly(naver, q, as_of)
    for g in groups:   # 참고용 (신호 계산에는 미사용)
        parts[f"(참고)naver:{g['name']}"], _ = qtd_growth_weekly(weekly(df, "naver_datalab", g["name"], "KR"), q, as_of)
    # Similarweb: 루트 도메인(toptoon.com)에 하위 도메인(chat.)이 포함되는지 무료판에서 확인할 수 없으므로
    # 방문수를 합산하지 않고 도메인별 성장률을 각각 구해 평균 → 중복 집계돼도 비중만 바뀌고 수준 왜곡은 없음
    doms = [d for d in ccfg.get("domains", []) if d["region"] in ("domestic", "split")]
    sw = {}
    for d in doms:
        g, _ = qtd_growth_monthly(_region_visits(df, d, "domestic"), q, as_of)
        sw[d["domain"]] = g
        parts[f"(참고)sw:{d['domain']}"] = g
    parts["sw:국내도메인평균"] = mean_valid(sw.values())
    # 탑툰챗 국내 사이트 주간 활동량(작품 카드 누적수치 증분) — 이력이 한 분기 이상 쌓이면 자동 반영
    for s in [x for x in ccfg.get("chat_sites", []) if x["region"] == "domestic"]:
        parts[f"chat:{s['id']}"], _ = qtd_growth_weekly(
            weekly(df, "chat_stats", s["id"], metric=f"character_{s.get('signal_metric', 'm2')}_delta_w"), q, as_of)
    core = [v for k, v in parts.items() if not k.startswith("(참고)")]
    return mean_valid(core), parts


def overseas_signal(df, ccfg, q, as_of):
    """해외 신호 = Σ w_d · [(1+방문수 성장_d)(1+환율 변동_d) − 1],  w_d = 직전 분기 방문수 비중."""
    parts, fx_parts, weights = {}, {}, {}
    ps, qs = pd.Timestamp(q_start(q_shift(q, -1))), pd.Timestamp(q_start(q))
    for d in ccfg.get("domains", []):
        if d["region"] not in ("overseas", "split"):
            continue
        v = _region_visits(df, d, "overseas")
        g, _ = qtd_growth_monthly(v, q, as_of)
        if g is None:
            continue
        fx = fx_qoq(df, d["fx"], q, as_of) if d.get("fx") else 0.0
        fx_parts[d["domain"]] = fx
        parts[f"sw:{d['domain']}"] = (1 + g) * (1 + (fx or 0)) - 1
        weights[f"sw:{d['domain']}"] = float(v[(v.index >= ps) & (v.index < qs)].sum())
    # 해외도 같은 이유로 방문수 가중 대신 도메인별 단순 평균 (루트·하위 도메인 중복 가능성)
    g_sw = mean_valid(parts.values()) if parts else None
    # 탑툰챗 해외 사이트 활동량 × 환율 (사이트 간 단순 평균)
    chat = {}
    for s in [x for x in ccfg.get("chat_sites", []) if x["region"] == "overseas"]:
        g, _ = qtd_growth_weekly(weekly(df, "chat_stats", s["id"], metric=f"character_{s.get('signal_metric', 'm2')}_delta_w"), q, as_of)
        if g is not None:
            fx = fx_qoq(df, s["fx"], q, as_of) if s.get("fx") else 0.0
            chat[f"chat:{s['id']}"] = (1 + g) * (1 + (fx or 0)) - 1
    parts.update(chat)
    if g_sw is not None or chat:
        return mean_valid([g_sw, mean_valid(chat.values())]), parts, fx_parts
    if not parts:   # Similarweb 이력이 없으면 Google Trends(JP·TW)로 대체
        gt = ccfg.get("google_trends") or {}
        fx_jp = fx_qoq(df, "JPYKRW", q, as_of) or 0.0
        for geo in [g for g in gt.get("geos", []) if g in ("JP", "TW")]:
            for kw in gt.get("keywords", []):
                g, _ = qtd_growth_weekly(weekly(df, "google_trends", kw, geo), q, as_of)
                if g is not None:
                    parts[f"gt:{geo}:{kw}"] = (1 + g) * (1 + (fx_jp if geo == "JP" else 0)) - 1
        fx_parts["JPYKRW"] = fx_jp
    return mean_valid(parts.values()), parts, fx_parts


def overseas_share(fin, ccfg):
    reg = fin[(fin.scope == "REGION") & (fin.account == "overseas")]
    rev = fin[(fin.scope == "CFS") & (fin.account == "revenue")].set_index("quarter")["value"]
    if not reg.empty:
        last = reg.sort_values("quarter").iloc[-1]
        if last.quarter in rev.index and rev[last.quarter]:
            return float(last.value / rev[last.quarter]), f"DART 확인값({last.quarter})"
    return float(ccfg.get("default_overseas_share", 0.35)), "config 기본값(출처 주석 참조)"


def total_signal(g_dom, g_ovs, s):
    if g_dom is None and g_ovs is None:
        return None
    if g_dom is None:
        return g_ovs
    if g_ovs is None:
        return g_dom
    return (1 - s) * g_dom + s * g_ovs


def direction(g, th):
    if g is None:
        return "N/A"
    return "POSITIVE" if g > th else "NEGATIVE" if g < -th else "NEUTRAL"


# -----------------------------------------------------------------------------
# 메인: 나우캐스트 + 백테스트
# -----------------------------------------------------------------------------
def run(con, company, ccfg, cfg, as_of: date | None = None):
    as_of = as_of or date.today()
    th = cfg.get("nowcast", {}).get("direction_threshold", 0.03)
    min_obs = cfg.get("nowcast", {}).get("min_obs_for_beta", 4)
    df, fin = load_metrics(con, company), load_fin(con, company)
    s_share, s_src = overseas_share(fin, ccfg)

    rev = fin[(fin.scope == "CFS") & (fin.account == "revenue")].set_index("quarter")["value"].sort_index()
    op = fin[(fin.scope == "CFS") & (fin.account == "operating_income")].set_index("quarter")["value"]

    # 계절 계수: 자사 대표 키워드(첫 그룹)
    groups = ccfg.get("naver_datalab", {}).get("groups", [])
    main_kw = groups[0]["name"] if groups else None
    sf = seasonal_factors(weekly(df, "naver_datalab", main_kw, "KR")) if main_kw else {}

    def signals_for(q, at):
        gd, dparts = domestic_signal(df, ccfg, q, at)
        go, oparts, fxp = overseas_signal(df, ccfg, q, at)
        return gd, go, total_signal(gd, go, s_share), dparts, oparts, fxp

    # --- 백테스트 (분기 종료 시점 기준 신호 vs 실제 매출 QoQ)
    brk = ccfg.get("structural_breaks", {})
    first_full = brk.get("domestic_first_full_quarter", "2000Q1")
    dom_start = q_shift(first_full, 1)
    regime = brk.get("overseas_regime_change", "2000Q1")
    bt = []
    for q in rev.index:
        pq = q_shift(q, -1)
        if pq not in rev.index or not rev[pq] or q == first_full:   # 합병 분기 QoQ는 구조적 점프 → 제외
            continue
        q_end = q_start(q_shift(q, 1)) + timedelta(days=6)
        gd, go, gt, *_ = signals_for(q, q_end)
        actual = float(rev[q] / rev[pq] - 1)
        post = q_ge(q, dom_start)
        bt.append(dict(quarter=q, actual_qoq=actual, g_dom=gd, g_ovs=go,
                       g_signal=gt if post else go,          # 합병 전은 해외 신호만으로 상장사 매출 비교
                       model="total" if post else "overseas_proxy",
                       regime="post" if q_ge(q, regime) else "pre"))
    bt = pd.DataFrame(bt)

    # 지역별 백테스트 (DART 주석의 국내/해외 매출이 있을 때)
    reg = fin[fin.scope == "REGION"].pivot_table(index="quarter", columns="account", values="value")
    rbt = []
    for q in reg.index:
        pq = q_shift(q, -1)
        if pq not in reg.index or q == first_full:
            continue
        q_end = q_start(q_shift(q, 1)) + timedelta(days=6)
        gd, go, *_ = signals_for(q, q_end)
        rec = dict(quarter=q)
        if "domestic" in reg and q_ge(q, dom_start) and reg.loc[pq, "domestic"]:
            rec.update(dom_actual=float(reg.loc[q, "domestic"] / reg.loc[pq, "domestic"] - 1), g_dom=gd)
        if "overseas" in reg and reg.loc[pq, "overseas"]:
            rec.update(ovs_actual=float(reg.loc[q, "overseas"] / reg.loc[pq, "overseas"] - 1), g_ovs=go)
        rbt.append(rec)
    rbt = pd.DataFrame(rbt)

    def hit_stats(sub):
        sub = sub.dropna(subset=["g_signal"])
        if sub.empty:
            return dict(n=0, hit_ratio=None)
        hits = (np.sign(sub.g_signal) == np.sign(sub.actual_qoq)).sum()
        return dict(n=int(len(sub)), hit_ratio=float(hits / len(sub)))

    # β 추정 (합산 모델 구간, 원점 통과 OLS) + LOO 오차
    beta, beta_note, loo = 1.0, "가정치(표본 부족)", []
    if not bt.empty:
        tot = bt[(bt.model == "total")].dropna(subset=["g_signal"])
        if len(tot) >= min_obs:
            x, y = tot.g_signal.values, tot.actual_qoq.values
            if (x ** 2).sum() > 0:
                beta = float((x * y).sum() / (x ** 2).sum())
                beta_note = f"OLS(원점), n={len(tot)}"
                for i in range(len(tot)):
                    xi, yi = np.delete(x, i), np.delete(y, i)
                    b = (xi * yi).sum() / (xi ** 2).sum() if (xi ** 2).sum() else 1.0
                    loo.append(abs(b * x[i] - y[i]))
        bt["est_qoq"] = bt.g_signal * beta

    stats = {
        "total_post_merger": hit_stats(bt[bt.model == "total"]) if not bt.empty else dict(n=0, hit_ratio=None),
        "overseas_proxy_pre_regime": hit_stats(bt[(bt.model == "overseas_proxy") & (bt.regime == "pre")]) if not bt.empty else dict(n=0, hit_ratio=None),
        "overseas_proxy_post_regime": hit_stats(bt[(bt.model == "overseas_proxy") & (bt.regime == "post")]) if not bt.empty else dict(n=0, hit_ratio=None),
        "loo_mae_qoq": float(np.mean(loo)) if loo else None,
    }
    for key, gcol, acol in (("domestic_region", "g_dom", "dom_actual"), ("overseas_region", "g_ovs", "ovs_actual")):
        if not rbt.empty and acol in rbt:
            sub = rbt.dropna(subset=[acol]).rename(columns={gcol: "g_signal", acol: "actual_qoq"})
            stats[key] = hit_stats(sub[["g_signal", "actual_qoq"]].dropna(subset=["actual_qoq"]))
        else:
            stats[key] = dict(n=0, hit_ratio=None)

    # --- 현재 분기 나우캐스트
    cur_q = q_of(as_of)
    last_rep = rev.index[-1] if len(rev) else None
    target_q = cur_q
    # 직전 분기 실적이 아직 공시 전이면 직전 분기를 먼저 추정
    if last_rep and q_shift(last_rep, 1) != cur_q:
        target_q = q_shift(last_rep, 1)
    gd, go, gt, dparts, oparts, fxp = signals_for(target_q, as_of)
    _, k_weeks = qtd_growth_weekly(weekly(df, "naver_datalab", main_kw, "KR"), target_q, as_of) if main_kw else (None, 0)
    base_q = q_shift(target_q, -1)
    rev_hat = float(rev[base_q] * (1 + beta * gt)) if (gt is not None and base_q in rev.index) else None

    # --- 이벤트 탄력성: 무료 프로모션이 감지된 주의 검색지수 / 직전 8주 중앙값 − 1
    elasticity = None
    pw = df[(df.source == "page_watch") & (df.metric == "free_promo") & (df.value > 0)]
    s_main = weekly(df, "naver_datalab", main_kw, "KR") if main_kw else pd.Series(dtype=float)
    if not pw.empty and not s_main.empty:
        ups = []
        for wk in pw.period.unique():
            base = s_main[(s_main.index < wk) & (s_main.index >= wk - pd.Timedelta(weeks=8))]
            if wk in s_main.index and len(base) >= 4 and base.median() > 0:
                ups.append(s_main[wk] / base.median() - 1)
        elasticity = dict(n=len(ups), avg_uplift=float(np.mean(ups)) if ups else None)

    # --- WoW / 4주 추이 (네이버 그룹별)
    wow = {}
    for g in groups + ccfg.get("naver_datalab", {}).get("peer_groups", []):
        s = weekly(df, "naver_datalab", g["name"], "KR")
        s = s[s.index + pd.Timedelta(days=6) <= pd.Timestamp(as_of)]   # 완결 주만
        if len(s) >= 9:
            wow[g["name"]] = dict(last=float(s.iloc[-1]), wow=float(s.iloc[-1] / s.iloc[-2] - 1) if s.iloc[-2] else None,
                                  m4=float(s.iloc[-4:].mean() / s.iloc[-8:-4].mean() - 1) if s.iloc[-8:-4].mean() else None,
                                  week=s.index[-1].date().isoformat())

    return dict(
        as_of=as_of.isoformat(), target_quarter=target_q, base_quarter=base_q,
        weeks_elapsed=k_weeks,
        g_domestic=gd, g_overseas=go, g_total=gt,
        g_domestic_sa=seasonally_adjust(gd, target_q, sf),
        direction=direction(gt, th), parts_domestic=dparts, parts_overseas=oparts, fx_effect=fxp,
        overseas_share=s_share, overseas_share_source=s_src,
        beta=beta, beta_note=beta_note,
        revenue_base=float(rev[base_q]) if base_q in rev.index else None,
        revenue_nowcast=rev_hat,
        seasonal_factors=sf, backtest=bt.to_dict("records") if not bt.empty else [], backtest_stats=stats,
        event_elasticity=elasticity, wow=wow,
        opm={q: float(op[q] / rev[q]) for q in rev.index if q in op.index and rev[q]},
        region_backtest=rbt.to_dict("records") if not rbt.empty else [],
        dom_backtest_start=dom_start,
    )
