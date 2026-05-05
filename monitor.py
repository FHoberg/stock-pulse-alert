#!/usr/bin/env python3
"""
Stock Pulse Alert
=================
Polls market-moving news feeds, deduplicates against persistent state,
classifies new items by estimated market impact, and pushes high-impact
alerts to ntfy.sh.

Sources:
  - SEC EDGAR current 8-K filings (Atom)
  - Federal Reserve press releases (RSS)
  - Trump posts via trumpstruth.org (RSS)

Classification:
  - If ANTHROPIC_API_KEY is set, uses Claude Haiku 4.5 via the Anthropic API
    for nuanced impact + ticker extraction.
  - Otherwise falls back to deterministic keyword rules (coarser).

Notification:
  - ntfy.sh — POST to https://ntfy.sh/$NTFY_TOPIC. Phone alert via the ntfy app.

Environment variables:
  NTFY_TOPIC          (required) — ntfy topic to publish to
  ANTHROPIC_API_KEY   (optional) — enables LLM classification
  ALERT_THRESHOLD     (default 7) — minimum impact 1-10 to alert on
  STATE_DIR           (default "state") — directory for seen.json
  USER_AGENT          (optional) — override the HTTP UA (SEC requires contact info)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------- Configuration ----------

STATE_DIR = Path(os.environ.get("STATE_DIR", "state"))
STATE_FILE = STATE_DIR / "seen.json"
MAX_SEEN = 5000

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

try:
    ALERT_THRESHOLD = int(os.environ.get("ALERT_THRESHOLD", "7"))
except ValueError:
    ALERT_THRESHOLD = 7

USER_AGENT = os.environ.get(
    "USER_AGENT",
    # SEC EDGAR requires a UA containing a real contact (typically an email).
    # The placeholder below WILL get a 403 from SEC. Set USER_AGENT to
    # something like "Your Name your-email@example.com" before running.
    "stock-pulse-alert (please set USER_AGENT to include your email)",
)

SOURCES: list[tuple[str, str]] = [
    ("SEC 8-K",
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K"
     "&company=&dateb=&owner=include&count=40&output=atom"),
    ("Fed",
     "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("Trump",
     "https://trumpstruth.org/feed"),
]


# ---------- Fetch + parse ----------

def fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def parse_feed(xml_bytes: bytes, source: str) -> list[dict]:
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"# parse error for {source}: {e}", file=sys.stderr)
        return items

    _strip_ns(root)

    # Atom entries
    for entry in root.iter("entry"):
        title = (entry.findtext("title") or "").strip()
        link_el = entry.find("link")
        link = link_el.get("href") if link_el is not None else ""
        eid = (entry.findtext("id") or link or title).strip()
        summary = (entry.findtext("summary")
                   or entry.findtext("content") or "").strip()
        when = (entry.findtext("updated")
                or entry.findtext("published") or "").strip()
        if eid:
            items.append({
                "id": f"{source}::{eid}",
                "source": source,
                "title": title,
                "link": link,
                "summary": summary[:600],
                "time": when,
            })

    # RSS items
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link or title).strip()
        desc = (it.findtext("description") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        if guid:
            items.append({
                "id": f"{source}::{guid}",
                "source": source,
                "title": title,
                "link": link,
                "summary": desc[:600],
                "time": pub,
            })

    return items


# ---------- State ----------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen": []}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"seen": []}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["seen"] = state["seen"][-MAX_SEEN:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------- Classification ----------

# Coarse keyword-based fallback (used when ANTHROPIC_API_KEY is not set).

SEC_8K_ITEM_IMPACT: dict[str, tuple[str, int]] = {
    "1.01": ("material definitive agreement", 7),
    "1.02": ("agreement termination", 7),
    "1.03": ("bankruptcy", 9),
    "2.01": ("acquisition completed", 8),
    "2.02": ("results of operations", 7),
    "2.03": ("material direct obligation", 6),
    "2.04": ("triggering event accelerating debt", 8),
    "2.05": ("restructuring / exit costs", 7),
    "2.06": ("material impairment", 7),
    "3.01": ("delisting / non-compliance", 8),
    "3.02": ("unregistered equity sale", 6),
    "4.01": ("auditor change", 7),
    "4.02": ("restatement / non-reliance", 8),
    "5.01": ("change of control", 8),
    "5.02": ("officer/director change", 7),
}

TRUMP_KEYWORDS: dict[int, list[str]] = {
    9: ["tariff", "tariffs", "sanction", "sanctions", "embargo"],
    8: ["trade deal", "executive order", "shutdown", "default"],
    7: ["china", "fed", "interest rate", "powell", "rate cut", "rate hike"],
    6: ["economy", "stock", "market", "recession"],
}

FED_KEYWORDS: dict[int, list[str]] = {
    9: ["fomc statement", "federal funds rate", "discount rate", "emergency"],
    8: ["monetary policy", "interest rate", "stress test"],
    7: ["regulatory", "supervision"],
}


def keyword_classify(item: dict) -> dict:
    title = item["title"].lower()
    summary = item["summary"].lower()
    text = f"{title} {summary}"
    source = item["source"]

    impact = 1
    reason = "Routine."
    tickers: list[str] = []

    if source == "SEC 8-K":
        codes = re.findall(r"item\s+(\d+\.\d+)", text)
        best_score = 1
        best_label = "routine 8-K"
        for code in codes:
            label, score = SEC_8K_ITEM_IMPACT.get(code, (None, 0))
            if score > best_score:
                best_score, best_label = score, label
        impact = best_score
        m = re.search(r"8-K\s*-\s*([^\(]+)", item["title"])
        company = (m.group(1).strip().rstrip(",.") if m else "")
        reason = f"{best_label}{(' for ' + company) if company else ''}."

    elif source == "Trump":
        if not text.strip():
            reason = "Empty post."
        else:
            for score, kws in TRUMP_KEYWORDS.items():
                hit = next((kw for kw in kws if kw in text), None)
                if hit and score > impact:
                    impact = score
                    reason = f"Mentions '{hit}'."

    elif source == "Fed":
        for score, kws in FED_KEYWORDS.items():
            hit = next((kw for kw in kws if kw in text), None)
            if hit and score > impact:
                impact = score
                reason = f"Fed press mentioning '{hit}'."

    return {"impact": impact, "tickers": tickers, "reason": reason}


def llm_classify(items: list[dict], api_key: str) -> list[dict]:
    """Classify all items with one Claude API call. Returns aligned list."""
    import http.client

    system = (
        "You are a financial news triage system. For each news item, estimate "
        "market impact 1-10 and list affected tickers.\n\n"
        "IMPACT CALIBRATION:\n"
        "1-3: routine (compensation amendments, regular dividends, personal "
        "posts, empty content)\n"
        "4-6: notable but unlikely to move stocks much\n"
        "7: meaningful (1-3% move likely on relevant ticker)\n"
        "8: significant (3-7% move OR sector-wide effect)\n"
        "9-10: major (large moves, broad market impact)\n\n"
        "SEC 8-K — calibrate by Item code:\n"
        "  1.01, 1.02, 2.01, 2.05, 2.06, 4.01, 4.02, 5.02 -> typically 7+\n"
        "  1.03 (bankruptcy), 5.01 (change of control) -> 8-9\n"
        "  8.01 -> varies, read the summary\n"
        "  5.07, 7.01, 9.01 alone, routine compensation -> 1-5\n\n"
        "Trump posts: tariff/sanction announcements naming a country/sector/"
        "company -> 8-9. Trade deals, executive orders -> 7-8. Personal/"
        "political content unrelated to economy -> 1-3. Empty -> 1.\n\n"
        "Fed: rate decisions, FOMC statements, emergency actions -> 8+. "
        "Stress tests, big bank rules -> 6-7. Routine speeches -> 2-4.\n\n"
        "OUTPUT: Reply with a raw JSON array, no prose, no markdown fences. "
        "One object per input item, in order: "
        '{"impact": int 1-10, "tickers": [string up to 5], '
        '"reason": string one sentence}. '
        "Use empty tickers list if macro-only."
    )

    user_payload = json.dumps([
        {"i": idx, "source": it["source"],
         "title": it["title"], "summary": it["summary"]}
        for idx, it in enumerate(items)
    ], indent=2)

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 2048,
        "system": system,
        "messages": [{
            "role": "user",
            "content": f"Classify these items, return JSON only:\n\n{user_payload}",
        }],
    }).encode("utf-8")

    conn = http.client.HTTPSConnection("api.anthropic.com", timeout=60)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    conn.request("POST", "/v1/messages", body, headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", errors="replace")
    if resp.status >= 400:
        raise RuntimeError(f"Anthropic API error {resp.status}: {raw[:300]}")

    data = json.loads(raw)
    text = "".join(
        b.get("text", "") for b in data.get("content", [])
        if b.get("type") == "text"
    )

    m = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
    if not m:
        raise RuntimeError(f"No JSON array in LLM response: {text[:300]}")
    parsed = json.loads(m.group(0))
    if len(parsed) != len(items):
        raise RuntimeError(
            f"LLM returned {len(parsed)} classifications "
            f"for {len(items)} items"
        )

    # Normalize fields
    out: list[dict] = []
    for cls in parsed:
        out.append({
            "impact": int(cls.get("impact", 1)),
            "tickers": [str(t) for t in (cls.get("tickers") or [])][:5],
            "reason": str(cls.get("reason", ""))[:300],
        })
    return out


def classify(items: list[dict]) -> list[dict]:
    if ANTHROPIC_API_KEY and items:
        try:
            return llm_classify(items, ANTHROPIC_API_KEY)
        except Exception as e:
            print(
                f"# LLM classification failed ({e}); "
                "falling back to keyword rules",
                file=sys.stderr,
            )
    return [keyword_classify(it) for it in items]


# ---------- Notification ----------

def ntfy_post(item: dict, classification: dict, retries: int = 2) -> bool:
    if not NTFY_TOPIC:
        return False

    impact = classification["impact"]
    tickers = classification.get("tickers") or []
    reason = classification.get("reason", "")

    ticker_str = ",".join(tickers) if tickers else "macro"
    title = f"[{item['source']}] {ticker_str} (impact {impact})"

    body_lines = [item["title"], "", reason]
    if item.get("link"):
        body_lines.extend(["", item["link"]])
    body = "\n".join(body_lines).encode("utf-8")

    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "high",
        "Tags": "chart_with_upwards_trend",
    }
    if item.get("link"):
        headers["Click"] = item["link"]

    url = f"https://ntfy.sh/{NTFY_TOPIC}"

    last_err: str = ""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, data=body, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                if 200 <= r.status < 300:
                    return True
                last_err = f"HTTP {r.status}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(1 + attempt)

    print(
        f"# ntfy POST failed after {retries + 1} attempts: {last_err}",
        file=sys.stderr,
    )
    return False


# ---------- Main ----------

def main() -> int:
    if not NTFY_TOPIC:
        print("ERROR: NTFY_TOPIC env var is required", file=sys.stderr)
        return 1

    state = load_state()
    seen = list(state.get("seen", []))
    seen_set = set(seen)
    is_first_run = len(seen_set) == 0

    all_items: list[dict] = []
    errors: list[str] = []

    for name, url in SOURCES:
        try:
            all_items.extend(parse_feed(fetch(url), name))
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")

    new_items: list[dict] = []
    for it in all_items:
        if it["id"] not in seen_set:
            new_items.append(it)
            seen_set.add(it["id"])
            seen.append(it["id"])

    save_state({"seen": seen})

    if is_first_run:
        print(
            f"First run: seeded {len(new_items)} items from "
            f"{len(SOURCES)} sources, no alerts. "
            f"Errors: {errors if errors else 'none'}"
        )
        return 0

    if not new_items:
        print(f"No new items. Errors: {errors if errors else 'none'}")
        return 0

    classifications = classify(new_items)

    alerted: list[str] = []
    for item, cls in zip(new_items, classifications):
        if cls["impact"] >= ALERT_THRESHOLD:
            if ntfy_post(item, cls):
                tickers = cls.get("tickers") or ["macro"]
                alerted.append(
                    f"{item['source']}/{','.join(tickers)}({cls['impact']})"
                )

    print(
        f"Fetched {len(all_items)} items, {len(new_items)} new, "
        f"{len(alerted)} alerted [{', '.join(alerted) if alerted else '-'}]. "
        f"Errors: {errors if errors else 'none'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
