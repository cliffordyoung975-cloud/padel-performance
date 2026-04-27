#!/usr/bin/env python3
"""
Hoplon Lead Scanner
Runs daily via GitHub Actions. Queries Hacker News (Algolia) and Reddit
for posts matching Hoplon's ICP pain triggers, scores them, dedupes,
and writes results to leads.json for the dashboard to read.

v3: Added relevance filtering to reject developer/technical chatter
and focus on business owners/founders actually asking for help.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.parse
import urllib.request
import urllib.error

# ============ CONFIG ============

LOOKBACK_HOURS_NORMAL = 36
LOOKBACK_HOURS_SEED = 168  # 7 days

OUTPUT_FILE = Path(__file__).parent / "leads.json"
MAX_LEADS = 500

# Minimum relevance score to keep a lead. Posts below this are discarded.
MIN_SCORE = 20

# ============ QUERY DEFINITIONS ============
# Focused on "Ask HN" style queries and terms that indicate someone
# ASKING for help, not discussing security as a topic.

HN_QUERIES = [
    # --- COMPLIANCE URGENCY ---
    ("iso27001",    "Ask HN ISO 27001"),
    ("iso27001",    "Ask HN SOC 2"),
    ("iso27001",    "need ISO 27001"),
    ("iso27001",    "need SOC 2"),
    ("iso27001",    "client wants SOC 2"),
    ("iso27001",    "customer requires ISO"),
    ("compliance",  "Ask HN Cyber Essentials"),
    ("compliance",  "Ask HN GDPR compliance"),
    ("compliance",  "need GDPR compliant"),

    # --- CYBER INSURANCE ---
    ("compliance",  "cyber insurance denied"),
    ("compliance",  "cyber insurance requirements"),
    ("compliance",  "cyber insurance small business"),

    # --- VENDOR QUESTIONNAIRES ---
    ("iso27001",    "security questionnaire customer"),
    ("iso27001",    "vendor security assessment help"),

    # --- ASKING FOR HELP / WHERE TO START ---
    ("starter",     "Ask HN cybersecurity startup"),
    ("starter",     "Ask HN security small business"),
    ("starter",     "Ask HN security checklist"),
    ("starter",     "how to secure my startup"),
    ("starter",     "how to secure my business"),
    ("starter",     "security for non-technical founder"),
    ("starter",     "small business cybersecurity help"),

    # --- REMOTE / BYOD ---
    ("starter",     "secure remote team small business"),
    ("starter",     "BYOD policy small company"),

    # --- NO EXPERTISE ---
    ("noexpertise", "Ask HN hire security"),
    ("noexpertise", "fractional CISO"),
    ("noexpertise", "outsource cybersecurity small"),
    ("noexpertise", "no security team startup"),
    ("noexpertise", "need security help startup"),

    # --- OFFBOARDING ---
    ("noexpertise", "employee left still has access"),

    # --- BUDGET ---
    ("budget",      "Ask HN affordable security"),
    ("budget",      "cybersecurity on a budget"),
    ("budget",      "security startup budget"),

    # --- BREACH / INCIDENT ---
    ("breach",      "my startup got hacked"),
    ("breach",      "small business ransomware help"),
    ("breach",      "we got hacked what do"),
    ("breach",      "business email compromise help"),
    ("breach",      "phishing attack small business"),

    # --- BUYING INTENT ---
    ("other",       "recommend MSSP small"),
    ("other",       "recommend security consultant"),
    ("other",       "looking for security vendor"),

    # --- PASSWORDS ---
    ("starter",     "password manager small team"),
    ("starter",     "team password management"),
]

REDDIT_QUERIES = [
    # --- COMPLIANCE ---
    ("iso27001",    "smallbusiness",   "ISO 27001"),
    ("iso27001",    "ITManagers",      "ISO 27001"),
    ("iso27001",    "startups",        "SOC 2"),
    ("iso27001",    "SaaS",            "SOC 2"),
    ("compliance",  "smallbusiness",   "Cyber Essentials"),
    ("compliance",  "smallbusiness",   "GDPR compliance"),

    # --- CYBER INSURANCE ---
    ("compliance",  "smallbusiness",   "cyber insurance"),
    ("compliance",  "Insurance",       "cyber insurance small business"),

    # --- VENDOR QUESTIONNAIRES ---
    ("iso27001",    "smallbusiness",   "security questionnaire"),
    ("iso27001",    "ITManagers",      "security audit client"),

    # --- STARTER ---
    ("starter",     "smallbusiness",   "cybersecurity where to start"),
    ("starter",     "smallbusiness",   "cyber security advice"),
    ("starter",     "Entrepreneur",    "cybersecurity help"),
    ("starter",     "smallbusiness",   "security checklist"),

    # --- REMOTE / BYOD ---
    ("starter",     "smallbusiness",   "remote work security"),
    ("starter",     "smallbusiness",   "BYOD security"),

    # --- M365 / GOOGLE ---
    ("starter",     "smallbusiness",   "Microsoft 365 security"),
    ("starter",     "smallbusiness",   "MFA enforce"),

    # --- PASSWORDS ---
    ("starter",     "smallbusiness",   "password manager team"),

    # --- NO EXPERTISE ---
    ("noexpertise", "smallbusiness",   "no IT security help"),
    ("noexpertise", "smallbusiness",   "need security help"),
    ("noexpertise", "cybersecurity",   "small business security help"),
    ("noexpertise", "msp",             "small business security"),

    # --- OFFBOARDING ---
    ("noexpertise", "smallbusiness",   "employee left access"),

    # --- BUDGET ---
    ("budget",      "smallbusiness",   "cyber security cost"),
    ("budget",      "smallbusiness",   "affordable cybersecurity"),

    # --- BREACH ---
    ("breach",      "smallbusiness",   "hacked"),
    ("breach",      "smallbusiness",   "ransomware"),
    ("breach",      "smallbusiness",   "phishing attack"),
    ("breach",      "smallbusiness",   "invoice scam"),

    # --- BUYING INTENT ---
    ("other",       "cybersecurity",   "recommend MSSP"),
    ("other",       "msp",             "security vendor small business"),
]

# ============ RELEVANCE FILTERING ============
# The key insight: we want BUSINESS OWNERS asking for help,
# not DEVELOPERS discussing security as a topic.

# If a post contains 2+ of these, it's probably dev/technical chatter
DEV_SIGNALS = [
    'github.com', 'pull request', 'merge', 'npm', 'pip install',
    'docker', 'kubernetes', 'k8s', 'terraform', 'aws lambda',
    'api endpoint', 'oauth implementation', 'jwt', 'csrf',
    'sql injection', 'xss', 'buffer overflow', 'heap overflow',
    'cve-20', 'exploit', 'payload', 'reverse shell', 'ctf',
    'binary', 'disassembl', 'decompil', 'fuzzing', 'pentest',
    'bug bounty', 'responsible disclosure', 'zero day', '0day',
    'kernel', 'syscall', 'elf', 'shellcode',
    'rust', 'golang', 'python security library', 'node.js',
    'cryptograph', 'encryption algorithm', 'hash function',
    'public key', 'private key', 'diffie-hellman',
    'saas product', 'my side project', 'i built',
    'open source', 'self-hosted', 'self hosted',
    'linux server', 'nginx', 'apache',
    'startup i work at', 'our engineering team',
    'series a', 'series b', 'raised', 'valuation',
    'hiring for', 'we are hiring', 'job posting',
]

# Posts MUST contain at least one of these to be considered relevant
# (signals that someone is asking for help or has a business problem)
BUSINESS_SIGNALS = [
    # Asking for help
    'help', 'advice', 'recommend', 'suggestion', 'anyone',
    'how do i', 'how do we', 'how should', 'what should',
    'where do i start', 'where to start', 'looking for',
    'need', 'require', 'struggling', 'confused', 'overwhelmed',
    'no idea', 'not sure', 'stuck',
    # Business context
    'my business', 'my company', 'our company', 'small business',
    'my team', 'our team', 'employees', 'staff', 'my startup',
    'founder', 'ceo', 'owner', 'director', 'managing director',
    'client', 'customer', 'contract', 'deal', 'sales',
    'revenue', 'cost', 'budget', 'afford', 'pricing',
    'insurance', 'compliance', 'audit', 'certification',
    'policy', 'procedure', 'risk', 'liability',
    # Pain / urgency
    'deadline', 'urgent', 'asap', 'quickly', 'immediately',
    'lost a deal', 'blocking', 'denied', 'failed',
    'hacked', 'breached', 'ransomware', 'phishing',
    'incident', 'compromised',
    # Buying
    'vendor', 'provider', 'consultant', 'managed service',
    'mssp', 'outsource', 'hire', 'pay for',
    'quote', 'proposal',
]

def is_relevant(text: str) -> bool:
    """Filter out developer/technical chatter. Keep business pain signals."""
    lower = text.lower()

    # Count dev signals
    dev_count = sum(1 for s in DEV_SIGNALS if s in lower)
    if dev_count >= 2:
        return False

    # Must have at least one business signal
    biz_count = sum(1 for s in BUSINESS_SIGNALS if s in lower)
    if biz_count == 0:
        return False

    return True


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
    filtered_count = 0

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
            # Also strip href attributes left behind
            text = re.sub(r'href="[^"]*"', '', text)
            text = text[:1000]

            # RELEVANCE FILTER — skip dev/technical chatter
            if not is_relevant(text):
                filtered_count += 1
                continue

            author = hit.get("author", "anonymous")
            obj_id = hit.get("objectID")
            title = hit.get("title") or hit.get("story_title") or ""

            score = score_lead(text, trigger)
            if score < MIN_SCORE:
                filtered_count += 1
                continue

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
                "score": score,
                "notes": "",
                "auto": True,
            })
        time.sleep(0.5)

    print(f"  >> Filtered out {filtered_count} irrelevant HN results")
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
    filtered_count = 0
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

            # RELEVANCE FILTER
            if not is_relevant(text):
                filtered_count += 1
                continue

            score = score_lead(text, trigger)
            if score < MIN_SCORE:
                filtered_count += 1
                continue

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
                "score": score,
                "notes": "",
                "auto": True,
            })
        time.sleep(1.0)

    print(f"  >> Filtered out {filtered_count} irrelevant Reddit results")
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
