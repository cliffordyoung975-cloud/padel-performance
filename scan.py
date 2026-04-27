
#!/usr/bin/env python3
"""
Hoplon Lead Scanner
Runs daily via GitHub Actions. Queries Hacker News (Algolia) and Reddit
for posts matching Hoplon's ICP pain triggers, scores them, dedupes,
and writes results to leads.json for the dashboard to read.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.parse
import urllib.request
import urllib.error

# ============ CONFIG ============

LOOKBACK_HOURS_NORMAL = 36
LOOKBACK_HOURS_SEED = 168  # 7 days — used when leads.json is empty

OUTPUT_FILE = Path(__file__).parent / "leads.json"
MAX_LEADS = 500

# ============ QUERY DEFINITIONS ============

HN_QUERIES = [
    # --- COMPLIANCE AS SALES BLOCKER ---
    ("iso27001",    "ISO 27001"),
    ("iso27001",    "SOC 2"),
    ("iso27001",    "SOC2 compliance"),
    ("iso27001",    "SOC 2 audit"),
    ("compliance",  "Cyber Essentials"),
    ("compliance",  "GDPR compliance startup"),
    ("compliance",  "security certification startup"),

    # --- CYBER INSURANCE ---
    ("compliance",  "cyber insurance requirements"),
    ("compliance",  "cyber insurance denied"),
    ("compliance",  "cyber insurance application"),
    ("compliance",  "cyber insurance premium"),

    # --- SUPPLY CHAIN / VENDOR QUESTIONNAIRES ---
    ("iso27001",    "security questionnaire vendor"),
    ("iso27001",    "security assessment client"),
    ("iso27001",    "supplier security requirements"),
    ("iso27001",    "customer security audit"),

    # --- WHERE TO START / NEW TO SECURITY ---
    ("starter",     "Ask HN security startup"),
    ("starter",     "cybersecurity getting started"),
    ("starter",     "startup security checklist"),
    ("starter",     "security best practices startup"),
    ("starter",     "small business security basics"),

    # --- REMOTE / HYBRID / BYOD ---
    ("starter",     "remote team security"),
    ("starter",     "BYOD security policy"),
    ("starter",     "work from home security"),
    ("starter",     "personal laptop security company"),

    # --- M365 / GOOGLE WORKSPACE ---
    ("starter",     "Microsoft 365 security settings"),
    ("starter",     "Google Workspace security"),
    ("starter",     "MFA enforce company"),

    # --- PASSWORDS ---
    ("starter",     "shared passwords team"),
    ("starter",     "password manager company"),
    ("starter",     "password spreadsheet"),

    # --- NO EXPERTISE / HIRING ---
    ("noexpertise", "security hire startup"),
    ("noexpertise", "CISO small company"),
    ("noexpertise", "fractional CISO"),
    ("noexpertise", "outsource security"),
    ("noexpertise", "IT person security"),
    ("noexpertise", "only IT person"),

    # --- EMPLOYEE OFFBOARDING ---
    ("noexpertise", "employee left still has access"),
    ("noexpertise", "offboarding security checklist"),
    ("noexpertise", "revoke access employee"),

    # --- DATA HANDLING / PII ---
    ("compliance",  "customer data protection small business"),
    ("compliance",  "handling PII startup"),
    ("compliance",  "data protection policy"),

    # --- BUDGET / SMB ---
    ("budget",      "security budget startup"),
    ("budget",      "cheap cybersecurity"),
    ("budget",      "affordable security small business"),
    ("budget",      "security small team"),

    # --- BREACH / INCIDENT ---
    ("breach",      "startup hacked"),
    ("breach",      "ransomware small business"),
    ("breach",      "data breach startup"),
    ("breach",      "business email compromise"),
    ("breach",      "phishing attack small business"),
    ("breach",      "CEO fraud invoice"),

    # --- BUYING INTENT ---
    ("other",       "recommend MSSP"),
    ("other",       "security consultant startup"),
    ("other",       "security vendor recommendation"),
]

REDDIT_QUERIES = [
    # --- COMPLIANCE ---
    ("iso27001",    "smallbusiness",   "ISO 27001"),
    ("iso27001",    "ITManagers",      "ISO 27001"),
    ("iso27001",    "cybersecurity",   "ISO 27001 help"),
    ("iso27001",    "startups",        "SOC 2"),
    ("iso27001",    "SaaS",            "SOC 2"),
    ("compliance",  "smallbusiness",   "SOC 2"),
    ("compliance",  "startups",        "Cyber Essentials"),
    ("compliance",  "cybersecurity",   "GDPR compliance"),

    # --- CYBER INSURANCE ---
    ("compliance",  "smallbusiness",   "cyber insurance"),
    ("compliance",  "cybersecurity",   "cyber insurance requirements"),
    ("compliance",  "Insurance",       "cyber insurance small business"),

    # --- SUPPLY CHAIN / QUESTIONNAIRES ---
    ("iso27001",    "smallbusiness",   "security questionnaire client"),
    ("iso27001",    "cybersecurity",   "vendor security assessment"),
    ("iso27001",    "ITManagers",      "security audit client"),

    # --- STARTER ---
    ("starter",     "smallbusiness",   "cybersecurity where to start"),
    ("starter",     "startups",        "security getting started"),
    ("starter",     "smallbusiness",   "cyber security advice"),
    ("starter",     "Entrepreneur",    "cybersecurity"),
    ("starter",     "smallbusiness",   "security checklist"),

    # --- REMOTE / BYOD ---
    ("starter",     "smallbusiness",   "remote work security"),
    ("starter",     "sysadmin",        "BYOD policy"),
    ("starter",     "smallbusiness",   "personal laptop work security"),

    # --- M365 / GOOGLE ---
    ("starter",     "Office365",       "security settings"),
    ("starter",     "gsuite",          "security"),
    ("starter",     "sysadmin",        "MFA enforce small business"),

    # --- PASSWORDS ---
    ("starter",     "smallbusiness",   "password manager team"),
    ("starter",     "sysadmin",        "shared passwords company"),

    # --- NO EXPERTISE ---
    ("noexpertise", "smallbusiness",   "no IT security"),
    ("noexpertise", "ITManagers",      "first security hire"),
    ("noexpertise", "cybersecurity",   "small business security help"),
    ("noexpertise", "msp",             "small business security"),
    ("noexpertise", "smallbusiness",   "IT guy security"),

    # --- OFFBOARDING ---
    ("noexpertise", "smallbusiness",   "employee left access"),
    ("noexpertise", "sysadmin",        "offboarding checklist security"),

    # --- BUDGET ---
    ("budget",      "smallbusiness",   "cyber security cost"),
    ("budget",      "startups",        "security budget"),
    ("budget",      "smallbusiness",   "affordable cybersecurity"),

    # --- BREACH ---
    ("breach",      "smallbusiness",   "hacked"),
    ("breach",      "cybersecurity",   "small business breach"),
    ("breach",      "smallbusiness",   "ransomware"),
    ("breach",      "smallbusiness",   "phishing attack"),
    ("breach",      "smallbusiness",   "phishing email"),
    ("breach",      "smallbusiness",   "invoice scam"),
    ("breach",      "cybersecurity",   "business email compromise"),

    # --- BUYING INTENT ---
    ("other",       "cybersecurity",   "recommend MSSP"),
    ("other",       "msp",             "security vendor"),
]

# ============ SCORING ============

TRIG_WEIGHT = {
    "iso27001": 40, "breach": 45, "compliance": 30,
    "budget": 20, "noexpertise": 25, "starter": 15, "other": 10,
}
URGENT_KW = [
    'asap', 'urgent', 'deadline', 'this week', 'next month', 'quickly',
    'immediately', 'need now', 'scrambling', 'help', 'desperate',
    'running out of time', 'client requires', 'customer asking',
    'audit coming', 'renewal coming', 'deal depends', 'blocking us',
    'lost a deal', 'contract requires', 'insurance requires',
    'denied coverage', 'premium went up',
]
BUYING_KW = [
    'recommend', 'looking for', 'who can', 'any good', 'consultant',
    'partner', 'vendor', 'quote', 'budget', 'pay', 'hire', 'suggestions',
    'anyone use', 'what do you use', 'which provider', 'who do you use',
    'need someone', 'looking to outsource', 'managed service',
    'how much does', 'what does it cost', 'pricing',
]
PAIN_KW = [
    'confused', 'lost', 'overwhelmed', 'no idea', 'first time', 'never',
    'no team', 'solo', 'one-person', 'small team', 'clueless',
    'dont know where', 'no clue', 'struggling', 'stuck',
    'out of my depth', 'way over my head', 'no experience',
    'wearing many hats', 'not my expertise', 'only IT person',
    'no security person', 'nobody on staff', 'spreadsheet of passwords',
    'still has access', 'shared admin', 'no policy',
]

def score_lead(snippet: str, trigger: str) -> int:
    s = TRIG_WEIGHT.get(trigger, 10)
    text = snippet.lower()
    for k in URGENT_KW:
        if k in text: s += 6
    for k in BUYING_KW:
        if k in text: s += 5
    for k in PAIN_KW:
        if k in text: s += 3
    if len(snippet) > 200: s += 5
    return min(100, s)


# ============ HTTP HELPERS ============

USER_AGENT = "HoplonLeadScanner/1.0 (lead research tool)"

def http_get_json(url: str, headers: dict = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        print(f"  ! Request failed: {url[:80]}... ({e})", file=sys.stderr)
        return {}


# ============ HACKER NEWS ============

def scan_hn(lookback_hours: int) -> list:
    leads = []
    cutoff = int((datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp())

    for trigger, query in HN_QUERIES:
        url = (
            "https://hn.algolia.com/api/v1/search_by_date"
            f"?query={urllib.parse.quote(query)}"
            f"&numericFilters=created_at_i>{cutoff}"
            "&hitsPerPage=20&tags=(story,comment)"
        )
        data = http_get_json(url)
        hits = data.get("hits", [])
        print(f"  HN [{trigger}] '{query}': {len(hits)} hits")

        for hit in hits:
            text = hit.get("story_text") or hit.get("comment_text") or hit.get("title") or ""
            if not text: continue
            text = text.replace("<p>", "\n").replace("</p>", "")
            for tag in ["<i>", "</i>", "<b>", "</b>", "<a>", "</a>"]:
                text = text.replace(tag, "")
            text = text[:1000]

            author = hit.get("author", "anonymous")
            obj_id = hit.get("objectID")
            title = hit.get("title") or hit.get("story_title") or ""

            leads.append({
                "id": f"hn_{obj_id}",
                "url": f"https://news.ycombinator.com/item?id={obj_id}",
                "name": f"@{author}" + (f" — {title[:60]}" if title else ""),
                "source": "HackerNews",
                "snippet": text.strip(),
                "trigger": trigger,
                "context": "",
                "stage": "inbox",
                "created": int(hit.get("created_at_i", time.time())) * 1000,
                "discovered": int(time.time() * 1000),
                "score": score_lead(text, trigger),
                "notes": "",
                "auto": True,
            })
        time.sleep(0.5)
    return leads


# ============ REDDIT ============

def reddit_token() -> str | None:
    cid = os.environ.get("REDDIT_CLIENT_ID")
    secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        print("  ! REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set — skipping Reddit", file=sys.stderr)
        return None

    import base64
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token",
        data=b"grant_type=client_credentials",
        headers={
            "Authorization": f"Basic {auth}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())["access_token"]
    except Exception as e:
        print(f"  ! Reddit auth failed: {e}", file=sys.stderr)
        return None


def scan_reddit(lookback_hours: int) -> list:
    token = reddit_token()
    if not token: return []

    leads = []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp()
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    time_filter = "month" if lookback_hours > 48 else "week"

    for trigger, sub, query in REDDIT_QUERIES:
        url = (
            f"https://oauth.reddit.com/r/{sub}/search"
            f"?q={urllib.parse.quote(query)}"
            f"&restrict_sr=1&sort=new&limit=15&t={time_filter}"
        )
        data = http_get_json(url, headers=headers)
        posts = data.get("data", {}).get("children", [])
        fresh = [p for p in posts if p.get("data", {}).get("created_utc", 0) >= cutoff]
        print(f"  Reddit [{trigger}] r/{sub} '{query}': {len(fresh)}/{len(posts)} fresh")

        for post in fresh:
            d = post.get("data", {})
            title = d.get("title", "")
            body = d.get("selftext", "")
            text = (title + "\n\n" + body)[:1000].strip()
            if not text: continue

            leads.append({
                "id": f"rd_{d.get('id')}",
                "url": f"https://reddit.com{d.get('permalink', '')}",
                "name": f"u/{d.get('author', 'unknown')} — r/{sub}",
                "source": "Reddit",
                "snippet": text,
                "trigger": trigger,
                "context": f"{d.get('num_comments', 0)} comments, {d.get('score', 0)} upvotes",
                "stage": "inbox",
                "created": int(d.get("created_utc", time.time())) * 1000,
                "discovered": int(time.time() * 1000),
                "score": score_lead(text, trigger),
                "notes": "",
                "auto": True,
            })
        time.sleep(1.0)
    return leads


# ============ MAIN ============

def main():
    print(f"=== Hoplon scan @ {datetime.now(timezone.utc).isoformat()} ===")

    existing = []
    if OUTPUT_FILE.exists():
        try:
            existing = json.loads(OUTPUT_FILE.read_text())
            print(f"Loaded {len(existing)} existing leads")
        except json.JSONDecodeError:
            print("! Existing leads.json was corrupt, starting fresh", file=sys.stderr)

    existing_ids = {l["id"] for l in existing}

    if len(existing) == 0:
        lookback = LOOKBACK_HOURS_SEED
        print(f">> Seed mode: looking back {lookback}h (7 days)")
    else:
        lookback = LOOKBACK_HOURS_NORMAL
        print(f">> Normal mode: looking back {lookback}h")

    print("\n--- Hacker News ---")
    hn_leads = scan_hn(lookback)
    print(f"\n--- Reddit ---")
    rd_leads = scan_reddit(lookback)

    new_leads = [l for l in (hn_leads + rd_leads) if l["id"] not in existing_ids]
    print(f"\n=== {len(new_leads)} new leads (after dedup) ===")

    combined = new_leads + existing

    if len(combined) > MAX_LEADS:
        combined.sort(key=lambda l: l.get("discovered", l.get("created", 0)), reverse=True)
        combined = combined[:MAX_LEADS]

    tmp = OUTPUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(combined, indent=2, ensure_ascii=False))
    tmp.replace(OUTPUT_FILE)

    print(f"Wrote {len(combined)} leads to {OUTPUT_FILE}")

    meta = {
        "last_scan": datetime.now(timezone.utc).isoformat(),
        "new_this_run": len(new_leads),
        "total": len(combined),
    }
    (OUTPUT_FILE.parent / "scan-meta.json").write_text(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
