"""
ingest_issue.py — GitHub Issue(Similarweb 입력 폼) 본문을 data/manual/similarweb.csv에 반영.

GitHub Actions(manual_input.yml)에서 ISSUE_BODY 환경변수로 호출된다.
같은 company·month·domain 행이 있으면 덮어쓴다 (오입력 정정 가능).
"""
import csv
import os
import re
import sys
from pathlib import Path

CSV = Path(__file__).parent / "data" / "manual" / "similarweb.csv"
FIELDS = ["company", "month", "domain", "visits", "avg_duration_sec", "kr_share", "top_country", "note"]
LABELS = {  # 이슈 폼 라벨 → CSV 컬럼
    "종목 ID": "company", "월 (YYYY-MM)": "month", "도메인": "domain", "월 방문 수": "visits",
    "평균 체류 시간(초)": "avg_duration_sec", "한국 트래픽 비중 (0~1)": "kr_share",
    "1위 유입 국가": "top_country", "메모": "note",
}


def parse(body: str) -> dict:
    out, cur = {}, None
    for line in body.splitlines():
        m = re.match(r"^###\s+(.*)$", line.strip())
        if m:
            cur = LABELS.get(m.group(1).strip()); continue
        if cur and line.strip() and line.strip() != "_No response_":
            out[cur] = (out.get(cur, "") + " " + line.strip()).strip()
    return out


def validate(rec: dict):
    errs = []
    if not re.fullmatch(r"\d{4}-\d{2}", rec.get("month", "")):
        errs.append("month 형식은 YYYY-MM")
    for k in ("visits", "avg_duration_sec", "kr_share"):
        v = rec.get(k, "").replace(",", "")
        if v and not re.fullmatch(r"\d+(\.\d+)?", v):
            errs.append(f"{k} 숫자 아님: {v}")
        rec[k] = v
    if rec.get("kr_share") and float(rec["kr_share"]) > 1:
        errs.append("kr_share는 0~1 (예: 0.82)")
    if not rec.get("visits"):
        errs.append("visits 필수")
    return errs


def main():
    rec = parse(os.environ.get("ISSUE_BODY", ""))
    errs = validate(rec)
    if errs:
        print("ERROR: " + "; ".join(errs)); return 1
    rows = list(csv.DictReader(CSV.open(encoding="utf-8"))) if CSV.exists() else []
    key = (rec["company"], rec["month"], rec["domain"])
    rows = [r for r in rows if (r["company"], r["month"], r["domain"]) != key] + [{f: rec.get(f, "") for f in FIELDS}]
    rows.sort(key=lambda r: (r["company"], r["month"], r["domain"]))
    CSV.parent.mkdir(parents=True, exist_ok=True)
    with CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader(); w.writerows(rows)
    print(f"OK: {key} 반영")
    return 0


if __name__ == "__main__":
    sys.exit(main())
