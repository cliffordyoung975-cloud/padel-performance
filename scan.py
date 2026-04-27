#!/usr/bin/env python3
"""
Hoplon Lead Scanner v5
- HN: ONLY searches 'Ask HN' posts (not comments, not Show HN, not news)
- Three-gate relevance filter: reject dev chatter, require security context,
  require business/help-seeking signal
- Reddit: subreddit-targeted searches (when API creds available)
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
LOOKBACK_HOURS_SEED = 336  # 14 days — Ask HN security posts are rare

OUTPUT_FILE = Path(__file__).parent / "leads.json"
MAX_LEADS = 500
MIN_SCORE = 20

# ============ HN QUERIES ============
# CRITICAL CHANGE: we now search ONLY 'Ask HN' stories using the
# Algolia tag 'ask_hn'. This eliminates Show HN, news articles,
# and random comments where someone mentions "security" in passing.
#
# Because Ask HN posts are rarer, we use broader search terms —
# the three-gate filter handles false positives.

HN_QUERIES = [
    # Compliance
    ("iso27001",    "ISO 27001"),
    ("iso27001",    "SOC 2"),
    ("compliance",  "Cyber Essentials"),
    ("compliance",  "GDPR"),
    ("compliance",  "compliance"),
    ("compliance",  "cyber insurance"),
    # Security help
    ("starter",     "cybersecurity"),
    ("starter",     "security startup"),
    ("starter",     "security small business"),
    ("starter",     "secure my company"),
    ("starter",     "security checklist"),
    ("starter",     "security best practices"),
    # No expertise
    ("noexpertise", "hire security"),
    ("noexpertise", "CISO"),
    ("noexpertise", "security team"),
    ("noexpertise", "outsource security"),
    # Budget
    ("budget",      "security budget"),
    ("budget",      "affordable security"),
    # Breach
    ("breach",      "hacked"),
    ("breach",      "ransomware"),
    ("breach",      "phishing"),
    ("breach",      "data breach"),
    # Buying
    ("other",       "MSSP"),
    ("other",       "security consultant"),
    ("other",       "security vendor"),
    # Misc
    ("starter",     "password manager"),
    ("starter",     "MFA"),
    ("noexpertise", "offboarding"),
]

# ============ REDDIT QUERIES ============
REDDIT_QUERIES = [
    ("iso27001",    "smallbusiness",   "ISO 27001"),
    ("iso27001",    "ITManagers",      "ISO 27001"),
    ("iso27001",    "startups",        "SOC 2"),
    ("iso27001",    "SaaS",            "SOC 2"),
    ("compliance",  "smallbusiness",   "Cyber Essentials"),
    ("compliance",  "smallbusiness",   "GDPR compliance"),
    ("compliance",  "smallbusiness",   "cyber insurance"),
    ("compliance",  "Insurance",       "cyber insurance small business"),
    ("iso27001",    "smallbusiness",   "security questionnaire"),
    ("iso27001",    "ITManagers",      "security audit client"),
    ("starter",     "smallbusiness",   "cybersecurity where to start"),
    ("starter",     "smallbusiness",   "cyber security advice"),
    ("starter",     "Entrepreneur",    "cybersecurity help"),
    ("starter",     "smallbusiness",   "security checklist"),
    ("starter",     "smallbusiness",   "remote work security"),
    ("starter",     "smallbusiness",   "BYOD security"),
    ("starter",     "smallbusiness",   "Microsoft 365 security"),
    ("starter",     "smallbusiness",   "MFA enforce"),
    ("starter",     "smallbusiness",   "password manager team"),
    ("noexpertise", "smallbusiness",   "no IT security help"),
    ("noexpertise", "smallbusiness",   "need security help"),
    ("noexpertise", "cybersecurity",   "small business security help"),
    ("noexpertise", "msp",             "small business security"),
    ("noexpertise", "smallbusiness",   "employee left access"),
    ("budget",      "smallbusiness",   "cyber security cost"),
    ("budget",      "smallbusiness",   "affordable cybersecurity"),
    ("breach",      "smallbusiness",   "hacked"),
    ("breach",      "smallbusiness",   "ransomware"),
    ("breach",      "smallbusiness",   "phishing attack"),
    ("breach",      "smallbusiness",   "invoice scam"),
    ("other",       "cybersecurity",   "recommend MSSP"),
    ("other",       "msp",             "security vendor small business"),
]

# ============ THREE-GATE RELEVANCE FILTER ============

# Gate 1: Reject developer/technical chatter (2+ matches = rejected)
DEV_SIGNALS = [
    'github.com', 'pull request', 'merge request', 'npm', 'pip install',
    'docker', 'kubernetes', 'k8s', 'terraform', 'aws lambda',
    'api endpoint', 'oauth implementation', 'jwt token', 'csrf token',
    'sql injection', 'xss', 'buffer overflow', 'heap overflow',
    'cve-20', 'reverse shell', 'ctf challenge',
    'disassembl', 'decompil', 'fuzzing', 'pentest report',
    'bug bounty', 'responsible disclosure', 'zero day', '0day',
    'kernel', 'syscall', 'shellcode',
    'golang', 'python library', 'node.js', 'react',
    'encryption algorithm', 'hash function',
    'diffie-hellman', 'elliptic curve',
    'my side project', 'i built this',
    'open source project', 'self-hosted',
    'nginx', 'apache config',
    'our engineering team', 'engineering blog',
    'series a', 'series b', 'we raised',
    'we are hiring', 'job posting', 'hiring for',
    'agentic', 'llm', 'large language model', 'claude code',
    'machine learning', 'neural network', 'transformer',
    'saas product', 'show hn',
]

# Gate 2: Must be about cybersecurity (not healthcare, tax, etc.)
SECURITY_CONTEXT = [
    'cybersecurity', 'cyber security', 'infosec', 'information security',
    'data breach', 'breach', 'malware', 'ransomware', 'phishing',
    'firewall', 'antivirus', 'endpoint protection', 'endpoint security',
    'mfa', 'multi-factor', 'two-factor', '2fa',
    'password manager', 'password policy', 'credential',
    'soc 2', 'soc2', 'iso 27001', 'iso27001',
    'cyber essentials', 'gdpr', 'data protection act',
    'ciso', 'mssp', 'penetration test', 'vulnerability scan',
    'vpn', 'zero trust',
    'incident response', 'disaster recovery',
    'access control', 'identity management', 'iam',
    'siem', 'edr', 'xdr', 'mdm',
    'byod security', 'remote access security',
    'cyber insurance',
    'security questionnaire', 'security assessment',
    'security policy', 'security team', 'security audit',
    'security consultant', 'security vendor',
    'hacked', 'compromised', 'attacked',
    'backup strategy', 'offboarding security',
    'security awareness', 'security training',
]

# Gate 3: Must show help-seeking or business pain (not just discussing)
BUSINESS_SIGNALS = [
    # Asking for help
    'help', 'advice', 'recommend', 'suggestion',
    'how do i', 'how do we', 'how should', 'what should',
    'where do i start', 'where to start', 'looking for',
    'need', 'struggling', 'confused', 'overwhelmed',
    'no idea', 'not sure how', 'stuck',
    # Business context
    'my business', 'my company', 'our company', 'small business',
    'my team', 'our team', 'employees', 'staff', 'my startup',
    'founder', 'ceo', 'owner', 'managing director',
    'client asking', 'customer requires', 'contract requires',
    # Pain / urgency
    'deadline', 'urgent', 'asap', 'quickly',
    'lost a deal', 'blocking', 'denied',
    # Buying
    'vendor', 'provider', 'consultant',
    'mssp', 'outsource', 'hire',
    'quote', 'pricing', 'cost',
]

def is_relevant(text: str) -> bool:
    """Three-gate filter. ALL three must pass."""
    lower = text.lower()

    # Gate 1: reject dev/technical chatter
    dev_count = sum(1 for s in DEV_SIGNALS if s in lower)
    if dev_count >= 2:
        return False

    # Gate 2: must actually be about cybersecurity
    sec_count = sum(1 for s in SECURITY_CONTEXT if s in lower)
    if sec_count == 0:
        return False

    # Gate 3: must show help-seeking or business pain
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
    'immediately', 'need now', 'scrambling', 'desperate',
    'running out of time', 'client requires', 'customer asking',
    'audit coming', 'deal depends', 'blocking us',
    'lost a deal', 'contract requires', 'insurance requires',
]
BUYING_KW = [
    'recommend', 'looking for', 'who can', 'any good', 'consultant',
    'partner', 'vendor', 'quote', 'pay', 'hire', 'suggestions',
    'anyone use', 'what do you use', 'which provider',
    'need someone', 'looking to outsource', 'managed service',
    'how much does', 'what does it cost', 'pricing',
]
PAIN_KW = [
    'confused', 'overwhelmed', 'no idea', 'first time',
    'no team', 'solo', 'one-person', 'small team', 'clueless',
    'no clue', 'struggling', 'stuck',
    'out of my depth', 'way over my head', 'no experience',
    'wearing many hats', 'not my expertise', 'only IT person',
    'no security person', 'nobody on staff',
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


# ============ HTTP ============

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
    seen_ids = set()

    for trigger, query in HN_QUERIES:
        # KEY CHANGE: tags=ask_hn — ONLY search "Ask HN" posts.
        # No comments, no Show HN, no news links, no random discussions.
        url = (
            "https://hn.algolia.com/api/v1/search_by_date"
            f"?query={urllib.parse.quote(query)}"
            f"&numericFilters=created_at_i>{cutoff}"
            "&hitsPerPage=20&tags=ask_hn"
        )
        data = http_get_json(url)
        hits = data.get("hits", [])
        print(f"  HN [{trigger}] '{query}': {len(hits)} hits")

        for hit in hits:
            obj_id = hit.get("objectID")
            if obj_id in seen_ids:
                continue
            seen_ids.add(obj_id)

            title = hit.get("title") or ""
            text = hit.get("story_text") or ""
            # Combine title + body for filtering
            full_text = (title + "\n" + text).strip()
            if not full_text:
                continue

            # Strip HTML
            full_text = full_text.replace("<p>", "\n").replace("</p>", "")
            full_text = re.sub(r'<[^>]+>', '', full_text)
            full_text = full_text[:1000]

            # THREE-GATE RELEVANCE FILTER
            if not is_relevant(full_text):
                filtered_count += 1
                continue

            author = hit.get("author", "anonymous")
            score = score_lead(full_text, trigger)
            if score < MIN_SCORE:
                filtered_count += 1
                continue

            leads.append({
                "id": f"hn_{obj_id}",
                "url": f"https://news.ycombinator.com/item?id={obj_id}",
                "name": f"@{author} — {title[:80]}",
                "source": "HackerNews",
                "snippet": full_text.strip(),
                "trigger": trigger,
                "context": f"{hit.get('num_comments', 0)} comments",
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
    print(f"=== Hoplon scan v5 @ {datetime.now(timezone.utc).isoformat()} ===")

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
        print(f">> Seed mode: looking back {lookback}h (14 days)")
    else:
        lookback = LOOKBACK_HOURS_NORMAL
        print(f">> Normal mode: looking back {lookback}h")

    print("\n--- Hacker News (Ask HN only) ---")
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
