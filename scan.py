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

# How far back to look on each run. 36h gives overlap so nothing slips
# through if a run fails. Dedup handles the overlap.
LOOKBACK_HOURS = 36

# Output file consumed by the dashboard
OUTPUT_FILE = Path(__file__).parent / "leads.json"

# Keep at most this many leads in the file (oldest dropped). Prevents
# the JSON from growing unbounded over months.
MAX_LEADS = 500

# Pain trigger queries. Each query maps to a Hoplon trigger type.
# Keep queries specific — generic terms ("security") return too much noise.
HN_QUERIES = [
    ("iso27001",    '"ISO 27001"'),
    ("iso27001",    '"SOC 2" startup'),
    ("compliance",  '"Cyber Essentials"'),
    ("starter",     '"where to start" security'),
    ("starter",     '"first security hire"'),
    ("noexpertise", '"no security team"'),
    ("breach",      'startup ransomware'),
    ("budget",      'startup security budget'),
]

# Reddit: (trigger, subreddit, search query)
REDDIT_QUERIES = [
    ("iso27001",    "smallbusiness",   "ISO 27001"),
    ("iso27001",    "ITManagers",      "ISO 27001"),
    ("iso27001",    "cybersecurity",   "ISO 27001 help"),
    ("compliance",  "smallbusiness",   "SOC 2"),
    ("compliance",  "startups",        "Cyber Essentials"),
    ("starter",     "smallbusiness",   "where to start cyber security"),
    ("starter",     "startups",        "security where to start"),
    ("noexpertise", "smallbusiness",   "no IT team security"),
    ("noexpertise", "ITManagers",      "first security hire"),
    ("budget",      "smallbusiness",   "cyber security cheap"),
    ("budget",      "startups",        "security on a budget"),
    ("breach",      "smallbusiness",   "we got hacked"),
    ("breach",      "cybersecurity",   "small business breach help"),
]

# ============ SCORING (mirrors dashboard logic) ============

TRIG_WEIGHT = {
    "iso27001": 40, "breach": 45, "compliance": 30,
    "budget": 20, "noexpertise": 25, "starter": 15, "other": 10,
}
URGENT_KW = ['asap','urgent','deadline','this week','next month','quickly',
             'immediately','need now','scrambling','help']
BUYING_KW = ['recommend','looking for','who can','any good','consultant',
             'partner','vendor','quote','budget','pay','hire']
PAIN_KW   = ['confused','lost','overwhelmed','no idea','first time','never',
             'no team','solo','one-person','small team','clueless']

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
    """GET a URL and parse JSON. Returns {} on failure rather than raising."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        print(f"  ! Request failed: {url[:80]}... ({e})", file=sys.stderr)
        return {}


# ============ HACKER NEWS ============

def scan_hn() -> list:
    """Query HN via Algolia. No auth needed. Returns list of lead dicts."""
    leads = []
    cutoff = int((datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).timestamp())

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
            # HN returns either story_text, comment_text, or just title
            text = hit.get("story_text") or hit.get("comment_text") or hit.get("title") or ""
            if not text: continue
            # Strip basic HTML
            text = text.replace("<p>", "\n").replace("</p>", "")
            for tag in ["<i>", "</i>", "<b>", "</b>", "<a>", "</a>"]:
                text = text.replace(tag, "")
            text = text[:1000]  # cap snippet length

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
                "stage": "inbox",  # auto-discovered leads land in inbox
                "created": int(hit.get("created_at_i", time.time())) * 1000,
                "discovered": int(time.time() * 1000),
                "score": score_lead(text, trigger),
                "notes": "",
                "auto": True,
            })
        time.sleep(0.5)  # polite rate-limit
    return leads


# ============ REDDIT ============

def reddit_token() -> str | None:
    """Get OAuth token using client credentials flow. Returns None if creds missing."""
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


def scan_reddit() -> list:
    token = reddit_token()
    if not token: return []

    leads = []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).timestamp()
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}

    for trigger, sub, query in REDDIT_QUERIES:
        url = (
            f"https://oauth.reddit.com/r/{sub}/search"
            f"?q={urllib.parse.quote(query)}"
            "&restrict_sr=1&sort=new&limit=15&t=week"
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
        time.sleep(1.0)  # Reddit rate limit is generous but be polite
    return leads


# ============ MAIN ============

def main():
    print(f"=== Hoplon scan @ {datetime.now(timezone.utc).isoformat()} ===")

    # Load existing leads (preserve user-curated state)
    existing = []
    if OUTPUT_FILE.exists():
        try:
            existing = json.loads(OUTPUT_FILE.read_text())
            print(f"Loaded {len(existing)} existing leads")
        except json.JSONDecodeError:
            print("! Existing leads.json was corrupt, starting fresh", file=sys.stderr)

    existing_ids = {l["id"] for l in existing}

    # Run scans
    print("\n--- Hacker News ---")
    hn_leads = scan_hn()
    print(f"\n--- Reddit ---")
    rd_leads = scan_reddit()

    # Merge: only add genuinely new IDs
    new_leads = [l for l in (hn_leads + rd_leads) if l["id"] not in existing_ids]
    print(f"\n=== {len(new_leads)} new leads (after dedup) ===")

    combined = new_leads + existing

    # Cap total size, dropping oldest first (sorted by discovered timestamp)
    if len(combined) > MAX_LEADS:
        combined.sort(key=lambda l: l.get("discovered", l.get("created", 0)), reverse=True)
        combined = combined[:MAX_LEADS]

    # Write atomically
    tmp = OUTPUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(combined, indent=2, ensure_ascii=False))
    tmp.replace(OUTPUT_FILE)

    print(f"Wrote {len(combined)} leads to {OUTPUT_FILE}")

    # Also write a tiny meta file so the dashboard can show "last scan"
    meta = {
        "last_scan": datetime.now(timezone.utc).isoformat(),
        "new_this_run": len(new_leads),
        "total": len(combined),
    }
    (OUTPUT_FILE.parent / "scan-meta.json").write_text(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
