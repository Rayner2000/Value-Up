"""
DART Value-Up Plan Checker (기업가치제고계획 모니터링)
=======================================================
Checks Korea's DART (OpenDART API) weekly for value-up plan filings
from a configured list of companies. Sends email alerts and/or saves
results to CSV when new disclosures are found.

Requirements:
    pip install requests pandas

Setup:
    1. Get a free OpenDART API key at: https://opendart.fss.or.kr/uat/uia/egovLoginUsr.do
    2. Edit config.py (or set environment variables) with your settings
    3. Add company names or stock codes to companies.txt
    4. Run: python check_value_up.py
"""

import os
import json
import logging
import smtplib
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ──────────────────────────────────────────────
# CONFIG — override with environment variables
# or edit directly here
# ──────────────────────────────────────────────
DART_API_KEY   = os.getenv("DART_API_KEY", "YOUR_DART_API_KEY_HERE")

# Email settings (leave blank to skip email)
EMAIL_SENDER   = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")   # Gmail App Password recommended
EMAIL_TO       = os.getenv("EMAIL_TO", "")         # Comma-separated recipients

# Slack settings (leave blank to skip Slack)
SLACK_WEBHOOK  = os.getenv("SLACK_WEBHOOK_URL", "")

# File paths
COMPANIES_FILE = Path(__file__).parent / "companies.txt"
SEEN_FILE      = Path(__file__).parent / "seen_filings.json"
OUTPUT_CSV     = Path(__file__).parent / "value_up_filings.csv"

# DART API base URL
DART_BASE      = "https://opendart.fss.or.kr/api"

# Keywords that identify a value-up plan disclosure
VALUE_UP_KEYWORDS = [
    "기업가치제고",
    "기업가치제고계획",
    "밸류업",
    "value up",
    "value-up",
    "기업가치 제고",
]

# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# DART API HELPERS
# ──────────────────────────────────────────────

def get_corp_code(company: str) -> str | None:
    """
    Resolve a company name or stock ticker to an 8-digit DART corp code.
    Downloads the full company list from DART (ZIP → XML) and searches it.
    Results are cached locally as corp_codes.json for speed.
    """
    cache_file = Path(__file__).parent / "corp_codes.json"

    # Load or refresh the cache
    if cache_file.exists():
        cache_age = datetime.now() - datetime.fromtimestamp(cache_file.stat().st_mtime)
        if cache_age < timedelta(days=7):
            with open(cache_file) as f:
                corp_map = json.load(f)
            return _search_corp_map(corp_map, company)

    log.info("Downloading company list from DART …")
    url = f"{DART_BASE}/corpCode.xml"
    params = {"crtfc_key": DART_API_KEY}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()

    import zipfile, io, xml.etree.ElementTree as ET
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    xml_data = zf.read(zf.namelist()[0])
    root = ET.fromstring(xml_data)

    corp_map = {}
    for item in root.findall("list"):
        name     = (item.findtext("corp_name") or "").strip()
        code     = (item.findtext("corp_code") or "").strip()
        stock_cd = (item.findtext("stock_code") or "").strip()
        if code:
            corp_map[name.lower()]     = code
            corp_map[stock_cd.lower()] = code

    with open(cache_file, "w") as f:
        json.dump(corp_map, f, ensure_ascii=False)
    log.info(f"Cached {len(corp_map)//2} companies.")

    return _search_corp_map(corp_map, company)


def _search_corp_map(corp_map: dict, query: str) -> str | None:
    q = query.strip().lower()
    # Exact match first
    if q in corp_map:
        return corp_map[q]
    # Partial match (first hit)
    for key, code in corp_map.items():
        if q in key:
            return code
    return None


def search_filings(corp_code: str, bgn_de: str, end_de: str) -> list[dict]:
    """Search all DART filings for a company between two dates.
    Searches both regular (A) and voluntary (B) disclosure types to catch
    value-up plans filed as 자율공시.
    """
    url = f"{DART_BASE}/list.json"
    all_filings = []

    for pblntf_ty in ["A", "B"]:   # A = 정기공시, B = 자율공시
        params = {
            "crtfc_key":  DART_API_KEY,
            "corp_code":  corp_code,
            "bgn_de":     bgn_de,
            "end_de":     end_de,
            "pblntf_ty":  pblntf_ty,
            "page_count": 100,
            "sort":       "date",
            "sort_mth":   "desc",
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "000":
                all_filings.extend(data.get("list", []))
            elif data.get("status") == "013":
                pass   # No results — normal
            else:
                log.warning(f"DART API returned status {data.get('status')}: {data.get('message')}")
        except Exception as e:
            log.error(f"Error searching filings for {corp_code} (type {pblntf_ty}): {e}")

    return all_filings


def is_value_up_filing(filing: dict) -> bool:
    """Return True if a filing's title matches a value-up keyword."""
    title = (filing.get("report_nm") or "").lower()
    for kw in VALUE_UP_KEYWORDS:
        if kw.lower() in title:
            return True
    return False


def filing_url(filing: dict) -> str:
    """Build a direct DART viewer URL for a filing."""
    rcp = filing.get("rcept_no", "")
    return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}"


# ──────────────────────────────────────────────
# STATE: track which filings we've already seen
# ──────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


# ──────────────────────────────────────────────
# NOTIFICATIONS
# ──────────────────────────────────────────────

def send_email(new_filings: list[dict]):
    if not (EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_TO):
        log.info("Email not configured — skipping.")
        return

    recipients = [r.strip() for r in EMAIL_TO.split(",")]
    subject = f"[DART Value-Up Alert] {len(new_filings)} new filing(s) found"

    rows = ""
    for f in new_filings:
        rows += (
            f"<tr>"
            f"<td>{f['corp_name']}</td>"
            f"<td>{f['report_nm']}</td>"
            f"<td>{f['rcept_dt']}</td>"
            f"<td><a href='{filing_url(f)}'>View on DART</a></td>"
            f"</tr>\n"
        )

    html = f"""
    <html><body>
    <h2>🇰🇷 DART Value-Up Plan Alert</h2>
    <p>The following new <strong>기업가치제고계획</strong> (Value-Up Plan) disclosures
    were found on DART:</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif;">
        <thead style="background:#f0f0f0;">
            <tr><th>Company</th><th>Report Title</th><th>Date Filed</th><th>Link</th></tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>
    <p style="color:gray;font-size:12px;">
        Automated alert from dart_value_up_checker · {datetime.now().strftime("%Y-%m-%d")}
    </p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        log.info(f"Email sent to {recipients}")
    except Exception as e:
        log.error(f"Failed to send email: {e}")


def send_slack(new_filings: list[dict]):
    if not SLACK_WEBHOOK:
        return

    blocks = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"🇰🇷 DART Value-Up Alert — {len(new_filings)} new filing(s)"}},
    ]
    for f in new_filings:
        url = filing_url(f)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                "text": (f"*{f['corp_name']}*\n"
                         f"> {f['report_nm']}\n"
                         f"> Filed: {f['rcept_dt']}\n"
                         f"> <{url}|View on DART>")},
        })

    try:
        resp = requests.post(SLACK_WEBHOOK, json={"blocks": blocks}, timeout=10)
        resp.raise_for_status()
        log.info("Slack notification sent.")
    except Exception as e:
        log.error(f"Failed to send Slack message: {e}")


def save_to_csv(new_filings: list[dict]):
    rows = []
    for f in new_filings:
        rows.append({
            "company":     f.get("corp_name", ""),
            "stock_code":  f.get("stock_code", ""),
            "report_title": f.get("report_nm", ""),
            "filed_date":  f.get("rcept_dt", ""),
            "receipt_no":  f.get("rcept_no", ""),
            "dart_url":    filing_url(f),
            "checked_on":  datetime.now().strftime("%Y-%m-%d"),
        })
    df_new = pd.DataFrame(rows)

    if OUTPUT_CSV.exists():
        df_existing = pd.read_csv(OUTPUT_CSV)
        df_all = pd.concat([df_existing, df_new], ignore_index=True)
        df_all.drop_duplicates(subset=["receipt_no"], inplace=True)
    else:
        df_all = df_new

    df_all.to_csv(OUTPUT_CSV, index=False)
    log.info(f"Saved {len(df_new)} new row(s) to {OUTPUT_CSV}")


# ──────────────────────────────────────────────
# MAIN CHECKER
# ──────────────────────────────────────────────

def load_companies() -> list[str]:
    """Read companies from companies.txt (one per line, # for comments)."""
    if not COMPANIES_FILE.exists():
        log.warning(f"{COMPANIES_FILE} not found — using built-in example list.")
        return ["삼성전자", "005930", "현대차", "POSCO홀딩스"]
    
    companies = []
    with open(COMPANIES_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                companies.append(line)
    return companies


def run():
    log.info("=" * 60)
    log.info("DART Value-Up Plan Checker — starting run")
    log.info("=" * 60)

    if DART_API_KEY == "YOUR_DART_API_KEY_HERE":
        log.error("Please set your DART_API_KEY in the environment or config!")
        return

    companies = load_companies()
    log.info(f"Monitoring {len(companies)} companies.")

    seen = load_seen()

    # Search the past 90 days (3 months)
    end_de   = datetime.now().strftime("%Y%m%d")
    bgn_de   = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")

    new_filings = []

    for company in companies:
        corp_code = get_corp_code(company)
        if not corp_code:
            log.warning(f"Could not find DART corp code for: {company!r}")
            continue

        log.info(f"Checking {company!r} (corp_code={corp_code}) …")
        filings = search_filings(corp_code, bgn_de, end_de)
        log.info(f"  → {len(filings)} filing(s) returned from DART")

        for filing in filings:
            rcept_no = filing.get("rcept_no", "")
            title    = filing.get("report_nm", "")
            date     = filing.get("rcept_dt", "")
            log.info(f"  [{date}] {title!r}")
            if rcept_no in seen:
                log.info(f"    (already seen — skipping)")
                continue

            if is_value_up_filing(filing):
                log.info(f"  ✅ NEW value-up filing: {title} ({date})")
                new_filings.append(filing)
                seen.add(rcept_no)
            else:
                seen.add(rcept_no)

    save_seen(seen)

    if new_filings:
        log.info(f"\n🎉 Found {len(new_filings)} new value-up filing(s)!")
        save_to_csv(new_filings)
        send_email(new_filings)
        send_slack(new_filings)
    else:
        log.info("No new value-up plan filings found this week.")

    log.info("Done.\n")


if __name__ == "__main__":
    run()
