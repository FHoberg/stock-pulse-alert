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
  - NIST news (RSS) — for Commerce / CHIPS-Act / federal-grant announcements
    that don't move through 8-Ks. Caught the May-2026 $2B quantum LOI batch.
  - SEC EDGAR Schedule 13D / 13G filings (Atom) — for institutional and
    activist position disclosures. Caught e.g. BlackRock 13G in Rubrik.

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

import datetime as _dt
import json
import os
import re
import sys
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _ZURICH = ZoneInfo("Europe/Zurich")
except Exception:  # pragma: no cover — only triggers on missing tzdata
    _ZURICH = None

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
    # NIST press feed — catches Commerce Dept / CHIPS-Act / federal-grant
    # announcements (e.g. equity stakes in publicly-traded quantum, semi,
    # nuclear, rare-earth, biotech recipients) that bypass the 8-K channel.
    ("NIST",
     "https://www.nist.gov/news-events/news/rss.xml"),
    # SEC EDGAR Schedule 13D/13G "latest filings" — beneficial ownership
    # disclosures. Noisier than 8-K; the LLM step is tuned to skip routine
    # index-mechanics and only fire on activist (13D) filings or 13Gs from
    # recognized active managers.
    ("SEC 13",
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13"
     "&company=&dateb=&owner=include&count=40&output=atom"),
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


# ---------- SEC 13 enrichment ----------
#
# The EDGAR "getcurrent" atom feed only carries the SUBJECT company in the
# entry title (e.g. "SC 13G - RUBRIK, INC."). The FILER (BlackRock, Pershing
# Square, Vanguard, etc.) is what tells us whether the filing is actionable,
# but the filer's name only appears inside the linked filing index page.
# This function fetches that page once per SC 13 item and prepends a
# "Filed by: <filer>" line to the item's summary so the LLM (and the keyword
# fallback) can apply the smart-money whitelist.

_FILED_BY_RE = re.compile(
    r'class="companyName">\s*([^<\n]+?)\s*\(Filed by\)',
    re.IGNORECASE,
)


def enrich_sec_13(item: dict) -> dict:
    """Fetch the linked SEC index page and prepend the filer name(s) to the
    item's summary. No-op for non-SEC-13 items or on any failure.

    Mutates and returns the item."""
    if item.get("source") != "SEC 13":
        return item
    link = item.get("link") or ""
    if "/Archives/edgar/data/" not in link:
        return item
    try:
        html = fetch(link, timeout=15).decode("utf-8", errors="replace")
    except Exception as e:
        print(
            f"# enrich_sec_13: fetch failed for {link}: {e}",
            file=sys.stderr,
        )
        return item

    filers = _FILED_BY_RE.findall(html)
    # Dedup case-insensitively while preserving order.
    seen: set[str] = set()
    unique_filers: list[str] = []
    for f in filers:
        key = f.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_filers.append(f)

    if not unique_filers:
        return item

    prefix = "Filed by: " + "; ".join(unique_filers) + ". "
    item["summary"] = (prefix + (item.get("summary") or ""))[:600]
    return item


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

# Smart-money filers — when one of these names appears in a SC 13 filing
# headline or summary, treat the filing as actionable regardless of form type.
# These are recognizable active managers / activist funds whose disclosed
# positions historically attract follow-on flows.
SMART_MONEY_FILERS: list[str] = [
    "berkshire hathaway", "berkshire", "warren buffett",
    "pershing square", "ackman",
    "scion asset", "michael burry",
    "soros fund", "soros management",
    "third point", "daniel loeb",
    "icahn", "carl icahn",
    "elliott management", "elliott investment",
    "trian", "nelson peltz",
    "starboard value",
    "valueact",
    "greenlight capital", "einhorn",
    "tiger global",
    "coatue",
    "lone pine",
    "viking global",
    "renaissance technologies",
    "citadel", "ken griffin",
    "d.e. shaw", "d. e. shaw",
    "two sigma",
    "bridgewater",
    "duquesne family office", "duquesne capital", "stanley druckenmiller",
    "situational awareness", "leopold aschenbrenner",
]

# Bullish NIST/Commerce keywords — federal money flowing toward named
# private-sector recipients. The keyword fallback can't reliably extract
# tickers, but it can at least tag the item as likely-bullish.
NIST_BULLISH_KEYWORDS: list[str] = [
    "chips act", "letters of intent", "letter of intent",
    "equity stake", "equity stakes",
    "billion in", "billion to", "billion for",
    "federal grant", "federal grants",
    "loan guarantee", "doe loan",
    "award to", "awarded to", "selected for",
    "quantum computing", "semiconductor manufacturing",
    "rare earth", "advanced packaging",
]


def keyword_classify(item: dict) -> dict:
    title = item["title"].lower()
    summary = item["summary"].lower()
    text = f"{title} {summary}"
    source = item["source"]

    impact = 1
    explanation = "Routine."
    recommendation = "skip"
    tickers: list[dict] = []
    is_macro = False

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
        explanation = (
            f"{(company + ' filed a ') if company else 'A company filed a '}"
            f"{best_label} disclosure with the SEC. "
            "8-K item codes of this type usually indicate material news that "
            "moves the stock."
        )
        # Only emit a recommendation when the item code points clearly one way.
        if any(c in codes for c in ("1.03", "2.04", "2.06", "3.01", "4.02")):
            recommendation = "sell"
        elif "2.01" in codes:
            recommendation = "buy"
        # Otherwise leave as "skip" — keyword rules can't tell from the code alone.

    elif source == "Trump":
        if not text.strip():
            explanation = "Empty post."
        else:
            for score, kws in TRUMP_KEYWORDS.items():
                hit = next((kw for kw in kws if kw in text), None)
                if hit and score > impact:
                    impact = score
                    is_macro = True
                    if hit in ("tariff", "tariffs", "sanction", "sanctions",
                              "embargo", "rate hike", "shutdown", "default",
                              "recession"):
                        recommendation = "sell"
                        explanation = (
                            f"Trump post mentions '{hit}'. Historically these "
                            "announcements pressure US equities and the named "
                            "sector lower in the following session."
                        )
                    elif hit in ("trade deal", "rate cut"):
                        recommendation = "buy"
                        explanation = (
                            f"Trump post mentions '{hit}'. Such announcements "
                            "typically lift broad US equities and risk assets "
                            "in the following session."
                        )
                    else:
                        explanation = f"Trump post mentions '{hit}'."

    elif source == "Fed":
        is_macro = True
        for score, kws in FED_KEYWORDS.items():
            hit = next((kw for kw in kws if kw in text), None)
            if hit and score > impact:
                impact = score
                explanation = (
                    f"Fed release mentioning '{hit}'. Direction is not "
                    "determinable from the headline alone."
                )
                # Direction depends on hawkish vs dovish content — leave as skip.

    elif source == "NIST":
        # Default to low impact. Only escalate if the headline names a federal
        # funding/equity action that historically benefits named recipients.
        impact = 3
        explanation = "Federal agency release; no obvious market signal."
        is_macro = True
        for hit in NIST_BULLISH_KEYWORDS:
            if hit in text:
                impact = 7
                recommendation = "buy"
                is_macro = True  # no ticker extraction in fallback
                explanation = (
                    f"NIST/Commerce release mentions '{hit}'. Federal "
                    "money flowing toward named publicly-traded recipients "
                    "typically lifts the sector's bellwether ETF (SOXX for "
                    "semis, ICLN clean energy, URA uranium, REMX rare "
                    "earths) and the named companies on the day."
                )
                break

    elif source == "SEC 13":
        # Default to skip — this feed is noisy. Two trigger conditions:
        #   (a) form is 13D/13D/A → activist stake → BUY
        #   (b) filer is a smart-money name → BUY
        # Otherwise (routine index-fund mechanics) → skip.
        is_13d = bool(re.search(r"sc\s*13d|schedule\s*13d|13d/a", text))
        smart_hit = next(
            (f for f in SMART_MONEY_FILERS if f in text), None
        )
        if is_13d:
            impact = 8
            recommendation = "buy"
            explanation = (
                "Schedule 13D filing — an activist investor disclosed a "
                "5%+ stake with stated intent to influence the issuer. "
                "Historically these filings precede a value-unlocking "
                "catalyst (board changes, strategic review, spin-off, or "
                "sale) and attract follow-on buying within days."
            )
        elif smart_hit:
            impact = 7
            recommendation = "buy"
            explanation = (
                f"Schedule 13G filing by {smart_hit.title()} — a "
                "recognized active manager. Concentrated stakes from "
                "track-record investors signal fundamental conviction "
                "and tend to attract follow-on flows from other funds "
                "in the days after the disclosure."
            )
        else:
            impact = 2
            explanation = (
                "Routine 13G passive-ownership disclosure; most likely "
                "an index-fund mechanics adjustment with no directional "
                "signal."
            )

    return {
        "impact": impact,
        "recommendation": recommendation,
        "explanation": explanation,
        "tickers": tickers,
        "is_macro": is_macro,
    }


def llm_classify(items: list[dict], api_key: str) -> list[dict]:
    """Classify all items with one Claude API call. Returns aligned list."""
    import http.client

    system = (
        "You are a financial news triage system for a retail investor who "
        "wants ONLY actionable BUY or SELL signals. Anything ambiguous must "
        "be marked 'skip' so it's never shown to the user.\n\n"
        "IMPACT CALIBRATION (1-10):\n"
        "  1-3: routine (compensation amendments, regular dividends, personal "
        "posts, empty content)\n"
        "  4-6: notable but unlikely to move stocks much\n"
        "  7: meaningful (1-3% move likely on relevant ticker)\n"
        "  8: significant (3-7% move OR sector-wide effect)\n"
        "  9-10: major (large moves, broad market impact)\n\n"
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
        "NIST / Commerce / federal-agency releases — calibrate by program "
        "scope:\n"
        "  - CHIPS-Act letters of intent, equity stakes, federal grants, DOE "
        "loan guarantees, or major procurement awards naming publicly-traded "
        "recipients -> 8-9, recommendation BUY for named recipients. The "
        "stock typically rallies on validation + new revenue.\n"
        "  - Sector-wide CHIPS / clean-energy / nuclear / rare-earth funding "
        "announcements without named companies -> 7-8, BUY on the sector "
        "ETF bellwether (SOXX semis, ICLN clean energy, URA uranium, REMX "
        "rare earths).\n"
        "  - Quantum-specific federal money -> name the listed pure-plays "
        "(IONQ, RGTI, QBTS, QUBT, ARQQ) plus IBM if mentioned.\n"
        "  - Routine standards/research/regulatory news -> 1-4, SKIP.\n\n"
        "SEC SC 13 (Schedule 13D / 13G) — this is the noisiest feed, default "
        "to SKIP aggressively. Only fire when one of these is true:\n"
        "  - Form is SC 13D or 13D/A (an ACTIVIST stake, i.e. filer intends "
        "to influence the issuer) -> impact 8, BUY. 13Ds historically "
        "precede a catalyst (board push, strategic review, spin-off, sale).\n"
        "  - Form is SC 13G/13G/A AND the filer is on the smart-money "
        "whitelist below -> impact 7-8, BUY. Concentrated stakes from "
        "track-record investors are followed by other money.\n"
        "  - Form is SC 13G/13G/A AND the subject company is a recent IPO "
        "or sub-$20B mid/small-cap AND filer is BlackRock / Vanguard / "
        "State Street first-time crossing 5% -> impact 7, BUY (this caught "
        "BlackRock's 5.3% in Rubrik). Their concentrated bets in "
        "smaller-cap names are meaningful; their mega-cap repositioning "
        "is not.\n"
        "  - Otherwise (Vanguard/BlackRock/State Street/FMR/Fidelity in "
        "mega-caps, micro position changes, generic index-mechanics) -> "
        "SKIP. Use SKIP liberally here.\n"
        "  - SMART-MONEY WHITELIST (always fire if you see the filer name "
        "in the headline or summary, even on a 13G): Berkshire Hathaway / "
        "Buffett, Pershing Square / Ackman, Scion Asset Management / "
        "Burry, Soros Fund Management / Soros, Third Point / Loeb, Icahn "
        "Enterprises / Icahn, Elliott Management / Singer, Trian Partners "
        "/ Peltz, Starboard Value / Smith, ValueAct / Ubben, Greenlight "
        "Capital / Einhorn, Tiger Global, Coatue, Lone Pine, Viking "
        "Global, Renaissance Technologies, Citadel / Griffin, D.E. Shaw, "
        "Two Sigma, Bridgewater, Duquesne Family Office / Stanley "
        "Druckenmiller, Situational Awareness LP / Leopold Aschenbrenner.\n"
        "  - For tickers on a 13 filing, list ONLY the subject company "
        "(the issuer the stake is in), NOT the filer.\n\n"
        "OUTPUT SCHEMA per item (one JSON object, in input order):\n"
        '  "impact": int 1-10\n'
        '  "recommendation": "buy" | "sell" | "skip"\n'
        "      - 'buy' = the news will push the named/affected instrument(s) "
        "UP. The user should consider opening a long position.\n"
        "      - 'sell' = the news will push the named/affected instrument(s) "
        "DOWN. The user should consider opening a short / closing a long.\n"
        "      - 'skip' = direction is genuinely uncertain, mixed across "
        "tickers, or the news is too low-impact to act on. Use 'skip' "
        "liberally — better silent than wrong. If you cannot decisively pick "
        "one of {buy, sell}, pick 'skip'.\n"
        '  "explanation": 2-3 plain-English sentences (40-70 words total). '
        "Sentence 1: what concretely happened (who, what, magnitude/scope). "
        "Sentence 2: WHY this pushes the stock or sector in the chosen "
        "direction (mechanism: revenue hit, margin pressure, demand boost, "
        "rate sensitivity, etc.). Sentence 3 (optional): the practical "
        "implication for the listed tickers. Avoid jargon. Write so a "
        "non-finance reader understands instantly. If recommendation is "
        "'skip', explain briefly why direction is unclear.\n"
        '  "tickers": list of up to 4 objects {"symbol": str, "name": str, '
        '"isin": str (OPTIONAL — include ONLY if you are certain; never '
        "guess an ISIN; omit the field entirely when unsure)}.\n"
        "      - If specific companies are named in the news, list those.\n"
        "      - If the news is macro-only (tariffs, rates, geopolitics), "
        "list 1-2 BELLWETHER instruments most likely to move (e.g. SPY for "
        "broad US market, QQQ for tech, SOXX for semis, XLE for energy, "
        "TLT for long-duration treasuries, EWZ for Brazil, MCHI for China).\n"
        '  "is_macro": bool — true if tickers are illustrative bellwethers '
        "rather than companies directly named in the news.\n\n"
        "Reply with a raw JSON array, no prose, no markdown fences."
    )

    user_payload = json.dumps([
        {"i": idx, "source": it["source"],
         "title": it["title"], "summary": it["summary"]}
        for idx, it in enumerate(items)
    ], indent=2)

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 4096,
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

    # Normalize fields. Be tolerant of older "reason" key and string tickers
    # in case the model deviates from the schema.
    out: list[dict] = []
    for cls in parsed:
        raw_tickers = cls.get("tickers") or []
        tickers: list[dict] = []
        for t in raw_tickers[:4]:
            if isinstance(t, dict):
                sym = str(t.get("symbol") or t.get("ticker") or "").strip()
                if not sym:
                    continue
                entry = {"symbol": sym}
                if t.get("name"):
                    entry["name"] = str(t["name"])[:80]
                isin = (t.get("isin") or "").strip()
                # ISIN is exactly 12 alphanumeric chars (2-letter country prefix).
                # Drop anything that doesn't match — better silent than a bogus ID.
                if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin):
                    entry["isin"] = isin
                tickers.append(entry)
            elif isinstance(t, str) and t.strip():
                tickers.append({"symbol": t.strip()})

        # Recommendation: only "buy" / "sell" trigger an alert; everything
        # else (including legacy "up"/"down"/"mixed"/"unclear") is normalized
        # so we can still send if the model used the old vocabulary.
        rec = str(cls.get("recommendation") or "").lower().strip()
        if not rec:
            # Backward-compat: tolerate old "direction" key
            legacy = str(cls.get("direction") or "").lower().strip()
            rec = {"up": "buy", "down": "sell"}.get(legacy, "skip")
        if rec == "up":
            rec = "buy"
        elif rec == "down":
            rec = "sell"
        if rec not in ("buy", "sell"):
            rec = "skip"

        explanation = str(
            cls.get("explanation") or cls.get("summary")
            or cls.get("reason") or ""
        )[:600]

        out.append({
            "impact": int(cls.get("impact", 1)),
            "recommendation": rec,
            "explanation": explanation,
            "tickers": tickers,
            "is_macro": bool(cls.get("is_macro", False)),
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

RECOMMENDATION_GLYPH = {
    "buy": "BUY",
    "sell": "SELL",
}

RECOMMENDATION_TAG = {
    "buy": "chart_with_upwards_trend",
    "sell": "chart_with_downwards_trend",
}


def _format_ticker_line(t: dict) -> str:
    sym = t.get("symbol", "?")
    name = t.get("name")
    isin = t.get("isin")
    extras = []
    if name:
        extras.append(name)
    if isin:
        extras.append(f"ISIN {isin}")
    suffix = f" ({', '.join(extras)})" if extras else ""
    return f"  - {sym}{suffix}"


def ntfy_post(item: dict, classification: dict, retries: int = 2) -> bool:
    if not NTFY_TOPIC:
        return False

    impact = classification["impact"]
    recommendation = classification.get("recommendation", "skip")
    tickers = classification.get("tickers") or []
    explanation = (classification.get("explanation")
                   or classification.get("summary")
                   or classification.get("reason") or "").strip()
    is_macro = classification.get("is_macro", False)

    # Caller is responsible for filtering "skip"; defensive guard here too.
    if recommendation not in ("buy", "sell"):
        return False

    # Title: short, scannable. Format example:
    #   BUY [Trump] i8 - SOXX, NVDA
    if tickers:
        ticker_label = ", ".join(
            (t.get("symbol", "?") if isinstance(t, dict) else str(t))
            for t in tickers[:3]
        )
    else:
        ticker_label = "macro"
    title = (
        f"{RECOMMENDATION_GLYPH[recommendation]} [{item['source']}] "
        f"i{impact} - {ticker_label}"
    )

    # Body: recommendation banner, headline, plain-English explanation,
    # ticker list, link.
    verb = "Consider BUYING" if recommendation == "buy" else "Consider SELLING"
    body_lines: list[str] = [
        f"{verb}: {ticker_label}",
        "",
        item["title"],
    ]
    if explanation:
        body_lines += ["", explanation]
    if tickers:
        heading = "Likely affected (bellwether examples):" if is_macro \
                  else "Affected:"
        body_lines += ["", heading]
        for t in tickers:
            if isinstance(t, dict):
                body_lines.append(_format_ticker_line(t))
            else:
                body_lines.append(f"  - {t}")
    if item.get("link"):
        body_lines += ["", item["link"]]
    body = "\n".join(body_lines).encode("utf-8")

    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "high",
        "Tags": RECOMMENDATION_TAG[recommendation],
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


# ---------- Performance tracking ----------
#
# Two append-only JSONL ledgers committed to the repo for a permanent audit
# trail of every alert and how the underlying ticker(s) moved 24h later:
#
#   state/alerts.jsonl     One row per alert sent. Includes timestamp, source,
#                          recommendation, impact, headline, explanation, and
#                          per-ticker entry_price snapshot from Yahoo.
#   state/scorecard.jsonl  One row per alert that has reached its 24h scoring
#                          window. Includes per-ticker exit_price, signed pct
#                          move, and a boolean "correct" flag.
#   state/digest_state.json
#                          Tracks the local Zurich date of the last digest
#                          push so we send the 8pm summary exactly once/day.

ALERTS_LOG = STATE_DIR / "alerts.jsonl"
SCORECARD_LOG = STATE_DIR / "scorecard.jsonl"
DIGEST_STATE_FILE = STATE_DIR / "digest_state.json"

DIGEST_HOUR_LOCAL = 20         # 8pm Europe/Zurich
SCORE_DELAY_HOURS = 24
PRICE_TIMEOUT = 10


def fetch_yahoo_price(symbol: str) -> float | None:
    """Latest regular-market price for a Yahoo symbol; None on any failure."""
    if not symbol:
        return None
    sym = symbol.strip().upper()
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
        "?interval=1m&range=1d"
    )
    try:
        # Yahoo is picky about UAs — use a generic browser-ish one.
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (stock-pulse-alert)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=PRICE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        if isinstance(price, (int, float)) and price > 0:
            return float(price)
    except Exception as e:
        print(f"# Yahoo price fetch failed for {symbol}: {e}", file=sys.stderr)
    return None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"# Failed to read {path}: {e}", file=sys.stderr)
    return rows


def log_alert(item: dict, classification: dict) -> None:
    """Append one alert row to alerts.jsonl with a Yahoo entry-price snapshot
    for each ticker. Called only after a successful ntfy push."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    enriched: list[dict] = []
    for t in classification.get("tickers") or []:
        if isinstance(t, dict):
            sym = (t.get("symbol") or "").strip()
        else:
            sym = str(t).strip()
        if not sym:
            continue
        entry: dict = {"symbol": sym}
        if isinstance(t, dict):
            if t.get("name"):
                entry["name"] = t["name"]
            if t.get("isin"):
                entry["isin"] = t["isin"]
        price = fetch_yahoo_price(sym)
        if price is not None:
            entry["entry_price"] = price
        enriched.append(entry)

    row = {
        "id": uuid.uuid4().hex[:12],
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "source": item.get("source", ""),
        "headline": item.get("title", ""),
        "link": item.get("link", ""),
        "explanation": classification.get("explanation", ""),
        "impact": int(classification.get("impact", 0)),
        "recommendation": classification.get("recommendation", "skip"),
        "is_macro": bool(classification.get("is_macro", False)),
        "tickers": enriched,
    }
    with ALERTS_LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")


def score_pending_alerts() -> int:
    """For any alert whose ts is ≥SCORE_DELAY_HOURS old and which doesn't yet
    have a scorecard row, fetch current prices and append a scorecard row.
    Returns the number of newly-scored alerts."""
    alerts = _read_jsonl(ALERTS_LOG)
    if not alerts:
        return 0
    scored = _read_jsonl(SCORECARD_LOG)
    scored_ids = {s.get("alert_id") for s in scored if s.get("alert_id")}
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    cutoff_secs = SCORE_DELAY_HOURS * 3600
    written = 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with SCORECARD_LOG.open("a") as f:
        for a in alerts:
            aid = a.get("id")
            if not aid or aid in scored_ids:
                continue
            try:
                ts = _dt.datetime.fromisoformat(
                    str(a.get("ts", "")).replace("Z", "+00:00")
                )
            except Exception:
                continue
            if (now_utc - ts).total_seconds() < cutoff_secs:
                continue
            rec = a.get("recommendation")
            if rec not in ("buy", "sell"):
                continue

            ticker_results: list[dict] = []
            for t in a.get("tickers") or []:
                sym = t.get("symbol")
                entry = t.get("entry_price")
                if not sym:
                    continue
                exit_price = fetch_yahoo_price(sym) if sym else None
                pct: float | None = None
                correct: bool | None = None
                if exit_price is not None and entry is not None and entry > 0:
                    pct = (exit_price - entry) / entry * 100
                    correct = (rec == "buy" and pct > 0) \
                        or (rec == "sell" and pct < 0)
                ticker_results.append({
                    "symbol": sym,
                    "entry_price": entry,
                    "exit_price": exit_price,
                    "pct": round(pct, 3) if pct is not None else None,
                    "correct": correct,
                })

            row = {
                "alert_id": aid,
                "ts": a.get("ts"),
                "scored_at": now_utc.isoformat(),
                "source": a.get("source"),
                "headline": a.get("headline"),
                "recommendation": rec,
                "impact": a.get("impact"),
                "is_macro": a.get("is_macro", False),
                "tickers": ticker_results,
            }
            f.write(json.dumps(row) + "\n")
            scored_ids.add(aid)
            written += 1
    return written


def _now_zurich() -> _dt.datetime:
    if _ZURICH is not None:
        return _dt.datetime.now(_ZURICH)
    return _dt.datetime.now(_dt.timezone.utc)


def maybe_send_daily_digest() -> bool:
    """If the local Zurich time is in the configured digest hour and we
    haven't already sent today's digest, push a one-line ntfy notification
    summarizing the last 24h of scorecard activity. Returns True iff a
    digest message was sent."""
    if not NTFY_TOPIC:
        return False
    now_zurich = _now_zurich()
    if now_zurich.hour != DIGEST_HOUR_LOCAL:
        return False
    today_str = now_zurich.date().isoformat()

    state: dict = {}
    if DIGEST_STATE_FILE.exists():
        try:
            state = json.loads(DIGEST_STATE_FILE.read_text())
        except Exception:
            state = {}
    if state.get("last_sent_date") == today_str:
        return False

    scored = _read_jsonl(SCORECARD_LOG)
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24)
    recent: list[dict] = []
    for s in scored:
        try:
            scored_at = _dt.datetime.fromisoformat(
                str(s.get("scored_at", "")).replace("Z", "+00:00")
            )
        except Exception:
            continue
        if scored_at >= cutoff:
            recent.append(s)

    # Always mark today as sent (even if there's nothing) so we don't
    # re-evaluate on every cron tick within the 8pm hour.
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if not recent:
        DIGEST_STATE_FILE.write_text(
            json.dumps({"last_sent_date": today_str})
        )
        return False

    correct_n = 0
    incorrect_n = 0
    no_data_n = 0
    moves: list[float] = []
    pnl_moves: list[float] = []  # signed by recommendation
    detail_lines: list[str] = []

    for r in recent:
        rec = (r.get("recommendation") or "?").upper()
        src = r.get("source") or "?"
        for t in r.get("tickers") or []:
            sym = t.get("symbol", "?")
            pct = t.get("pct")
            corr = t.get("correct")
            if corr is True:
                correct_n += 1
                marker = "✓"
            elif corr is False:
                incorrect_n += 1
                marker = "✗"
            else:
                no_data_n += 1
                marker = "·"
            if isinstance(pct, (int, float)):
                moves.append(float(pct))
                pnl_moves.append(
                    float(pct) if rec == "BUY" else -float(pct)
                )
                detail_lines.append(
                    f"  {marker} [{src}] {rec} {sym}: {pct:+.2f}%"
                )
            else:
                detail_lines.append(
                    f"  {marker} [{src}] {rec} {sym}: no price data"
                )

    decided = correct_n + incorrect_n
    hit_rate = (correct_n / decided * 100) if decided > 0 else 0.0
    avg_move = (sum(moves) / len(moves)) if moves else 0.0
    avg_pnl = (sum(pnl_moves) / len(pnl_moves)) if pnl_moves else 0.0

    title = (
        f"Daily Scorecard {today_str}: {correct_n}/{decided} hits "
        f"({hit_rate:.0f}%)"
    )
    body_lines = [
        f"Last 24h: {len(recent)} alerts, "
        f"{correct_n + incorrect_n + no_data_n} ticker outcomes.",
        f"Hits: {correct_n}  Misses: {incorrect_n}  No-data: {no_data_n}",
        f"Avg 24h move: {avg_move:+.2f}%   "
        f"Avg return per call: {avg_pnl:+.2f}%",
        "",
        "Detail:",
        *detail_lines,
    ]
    body = "\n".join(body_lines).encode("utf-8")
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "default",  # informational, not a market alert
        "Tags": "bar_chart",
    }
    url = f"https://ntfy.sh/{NTFY_TOPIC}"
    sent = False
    try:
        req = urllib.request.Request(
            url, data=body, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            sent = 200 <= r.status < 300
    except Exception as e:
        print(f"# Digest push failed: {e}", file=sys.stderr)

    if sent:
        DIGEST_STATE_FILE.write_text(
            json.dumps({"last_sent_date": today_str})
        )
    return sent


# ---------- Managed-filer 13F tracking ----------
#
# Some hedge fund / family office managers' quarterly Form 13F-HR filings
# are watched specifically. Every new 13F is parsed, diffed against the
# prior quarter's snapshot, and pushed as one informational ntfy digest
# (new positions / exits / big adds / big trims). This channel bypasses
# the BUY/SELL gate — 13Fs are inherently retrospective digests, not
# directional signals.
#
# Coverage caveat: a 13F captures only US-listed long positions (and
# put/call options) held on the last day of the quarter. It does NOT
# include shorts, intra-quarter trades, futures, FX, crypto, bonds, or
# foreign-only positions. For these managers, the 13F IS the public
# trade record — they don't file Form 4 (not officers/directors of
# public companies) and their "personal" capital sits inside the same
# fund that files the 13F.

MANAGED_FILERS: list[tuple[str, str]] = [
    # (display label, CIK)
    ("Druckenmiller", "0001536411"),   # Duquesne Family Office LLC
    ("Aschenbrenner", "0002045724"),   # Situational Awareness LP
]

THIRTEEN_F_DIR = STATE_DIR / "13f"
# Triggers for "big add" / "big trim" detection — must satisfy BOTH:
#   (a) share-count delta beyond BIG_DELTA (50%), AND
#   (b) trade dollar size at least MIN_TRADE_PCT_OF_PORTFOLIO of the
#       relevant portfolio (current for adds, prior for trims).
# (b) is what filters out the noise case of doubling a $500K residue
# in a $4B portfolio — that's a "+100% share count" event but only
# 0.01% of the portfolio.
#
# The same MIN_TRADE_PCT_OF_PORTFOLIO threshold also gates which NEW
# positions and EXITS make the digest (otherwise Aschenbrenner's
# 28-position-to-42-position quarter would push 23 new-position lines).
BIG_DELTA = 0.50
MIN_TRADE_PCT_OF_PORTFOLIO = 0.005  # 0.5% of portfolio

_PERIOD_OF_REPORT_RE = re.compile(
    r"Period of Report[^\d]*?(\d{4}-\d{2}-\d{2})"
)
_HOLDINGS_XML_RE = re.compile(
    r'href="(/Archives/edgar/data/[^"]+?\.xml)"'
)


def _13f_atom_url(cik: str) -> str:
    """Per-CIK EDGAR atom feed scoped to 13F-HR filings (last 10)."""
    return (
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={cik}&type=13F-HR&dateb=&owner=include&count=10&output=atom"
    )


def _parse_13f_atom(xml_bytes: bytes) -> list[dict]:
    """Parse the per-CIK 13F-HR atom feed. EDGAR uses a nested
    <content type="text/xml"> block with <accession-number>, <filing-date>,
    and <filing-href> tags rather than the flat <summary>/<link> shape
    used by getcurrent."""
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"# 13F atom parse error: {e}", file=sys.stderr)
        return out
    _strip_ns(root)
    for entry in root.iter("entry"):
        cat = entry.find("category")
        form_type = cat.get("term", "") if cat is not None else ""
        if not form_type.startswith("13F-HR"):
            continue
        content = entry.find("content")
        if content is None:
            continue
        accession = (content.findtext("accession-number") or "").strip()
        filing_date = (content.findtext("filing-date") or "").strip()
        filing_href = (content.findtext("filing-href") or "").strip()
        if accession and filing_href:
            out.append({
                "accession": accession,
                "filing_date": filing_date,
                "index_link": filing_href,
                "form_type": form_type,
            })
    return out


def _resolve_holdings_xml_url(index_url: str) -> tuple[str, str]:
    """Locate the holdings XML inside an EDGAR 13F filing-index page.

    A 13F filing has these XML files: primary_doc.xml (form header,
    contains Period of Report), holdings XML (variable filename per
    filer — e.g. form13f_YYYYMMDD.xml or salp13fq1xml.xml), plus
    xsl-styled previews under xslForm13F_X02/. The holdings XML is the
    .xml that is NOT primary_doc.xml and NOT inside xslForm13F_X02/.

    Returns (holdings_xml_full_url, period_of_report_iso)."""
    html = fetch(index_url, timeout=20).decode("utf-8", errors="replace")
    period_m = _PERIOD_OF_REPORT_RE.search(html)
    period = period_m.group(1) if period_m else ""
    holdings_path = None
    for p in _HOLDINGS_XML_RE.findall(html):
        if "xslForm13F" in p:
            continue
        if p.endswith("/primary_doc.xml"):
            continue
        holdings_path = p
        break
    if not holdings_path:
        raise RuntimeError(f"No holdings XML found in index {index_url}")
    return ("https://www.sec.gov" + holdings_path, period)


def _parse_holdings_xml(xml_bytes: bytes) -> list[dict]:
    """Parse the holdings XML of a 13F-HR filing into a list of dicts.
    Handles both unnamespaced (Duquesne) and ns1-namespaced (Situational
    Awareness) variants via the existing _strip_ns helper. Returns raw
    values as filed — caller normalizes thousands vs dollars."""
    root = ET.fromstring(xml_bytes)
    _strip_ns(root)
    out: list[dict] = []
    for it in root.iter("infoTable"):
        issuer = (it.findtext("nameOfIssuer") or "").strip()
        cusip = (it.findtext("cusip") or "").strip()
        title_class = (it.findtext("titleOfClass") or "").strip()
        try:
            value_raw = float((it.findtext("value") or "0").strip())
        except ValueError:
            value_raw = 0.0
        shrs_el = it.find("shrsOrPrnAmt")
        shares = 0
        amount_type = ""
        if shrs_el is not None:
            try:
                shares = int(shrs_el.findtext("sshPrnamt") or "0")
            except ValueError:
                shares = 0
            amount_type = (shrs_el.findtext("sshPrnamtType") or "").strip()
        putcall = (it.findtext("putCall") or "").strip().upper() or None
        if not cusip:
            continue
        out.append({
            "issuer": issuer,
            "cusip": cusip,
            "title_class": title_class,
            "value_raw": value_raw,
            "shares": shares,
            "amount_type": amount_type,
            "putcall": putcall,
        })
    return out


def _normalize_holdings_to_usd(holdings: list[dict]) -> list[dict]:
    """Detect filer's value units (thousands vs dollars) by checking if the
    total raw value is below the 13F filing threshold ($100M AUM). If so,
    the filer is using the pre-2023 thousands format; multiply by 1000.
    Stamps each holding with a 'value_usd' field in actual USD."""
    total_raw = sum(h["value_raw"] for h in holdings)
    multiplier = 1000.0 if total_raw < 50_000_000 else 1.0
    for h in holdings:
        h["value_usd"] = h["value_raw"] * multiplier
    return holdings


def _13f_snapshot_path(cik: str, period: str) -> Path:
    safe = (period or "unknown").replace("-", "")
    return THIRTEEN_F_DIR / f"{cik}_{safe}.json"


def _latest_prior_snapshot(cik: str, period: str) -> dict | None:
    """Return the snapshot for this CIK with the highest period_end strictly
    older than `period`, or None if no prior snapshot exists yet."""
    if not THIRTEEN_F_DIR.exists():
        return None
    cur_key = (period or "").replace("-", "")
    prefix = f"{cik}_"
    candidates: list[tuple[str, Path]] = []
    for p in THIRTEEN_F_DIR.iterdir():
        if not p.name.startswith(prefix) or p.suffix != ".json":
            continue
        key = p.stem[len(prefix):]
        if cur_key and key >= cur_key:
            continue
        candidates.append((key, p))
    if not candidates:
        return None
    candidates.sort()
    try:
        return json.loads(candidates[-1][1].read_text())
    except Exception:
        return None


def _is_option(h: dict) -> bool:
    """True if this holding row represents a put or call (rather than a
    common-stock or ETF position). Used to exempt option opens/closes from
    the portfolio-% noise filter — options are directional bets where even
    a small position size signals a real thesis."""
    return bool((h.get("putcall") or "").strip())


def _trade_size_usd(h_new: dict | None, h_old: dict | None) -> float:
    """Estimate the dollar size of the trade between two quarter-end
    snapshots, using the implied current-period price to isolate trade
    activity from price drift.

    For a pure add/trim, we approximate the trade as
        delta_shares × current_implied_price
    where current_implied_price = new_value / new_shares. This is a
    quarter-end snapshot, so the actual fill price could differ; the
    approximation is good for headline-quality reporting."""
    new_shares = (h_new or {}).get("shares", 0) or 0
    new_value = float((h_new or {}).get("value_usd", 0) or 0)
    old_shares = (h_old or {}).get("shares", 0) or 0
    old_value = float((h_old or {}).get("value_usd", 0) or 0)
    if new_shares == 0 and old_shares > 0:
        return old_value  # full exit — sold what was the prior position
    if old_shares == 0 and new_shares > 0:
        return new_value  # new entry — bought the full current position
    if new_shares <= 0 or new_value <= 0:
        return abs(new_value - old_value)
    implied_price = new_value / new_shares
    return abs(new_shares - old_shares) * implied_price


def diff_13f(old: dict | None, new_holdings: list[dict]) -> dict:
    """Diff two snapshots' holdings. Key by (cusip, putcall) so calls vs
    puts vs commons in the same name are tracked as separate lines (which
    matches how the SEC files them).

    Each emitted holding line is annotated with:
      trade_size_usd  — approximate dollar size of the trade
      trade_pct       — trade_size_usd / appropriate portfolio total
      portfolio_pct   — position value / current portfolio total
      delta_pct       — share-count delta vs prior quarter (big adds/trims)

    Lines are filtered to those whose trade_pct exceeds
    MIN_TRADE_PCT_OF_PORTFOLIO (to suppress micro-position noise) and
    sorted by trade size descending."""
    def key(h: dict) -> tuple:
        return (h["cusip"], (h.get("putcall") or ""))

    new_map = {key(h): h for h in new_holdings}
    old_holdings = (old or {}).get("holdings") or []
    old_map = {key(h): h for h in old_holdings}

    total_new = sum(h.get("value_usd", 0) for h in new_holdings) or 1.0
    total_prior = sum(
        h.get("value_usd", 0) for h in old_holdings
    ) or total_new  # fall back to current portfolio if no prior

    new_positions: list[dict] = []
    exited: list[dict] = []
    big_adds: list[dict] = []
    big_trims: list[dict] = []

    # NEW positions — denominator is current portfolio. Option opens
    # (puts and calls) bypass the noise filter; commons must clear it.
    for k, h in new_map.items():
        if k in old_map:
            continue
        trade = _trade_size_usd(h, None)
        pct = trade / total_new
        if not _is_option(h) and pct < MIN_TRADE_PCT_OF_PORTFOLIO:
            continue
        new_positions.append({
            **h, "trade_size_usd": trade, "trade_pct": pct,
            "portfolio_pct": h.get("value_usd", 0) / total_new,
        })

    # EXITED positions — denominator is prior portfolio. Same option exemption
    # as NEW: every option close is reported regardless of size.
    for k, h in old_map.items():
        if k in new_map:
            continue
        trade = _trade_size_usd(None, h)
        pct = trade / total_prior
        if not _is_option(h) and pct < MIN_TRADE_PCT_OF_PORTFOLIO:
            continue
        exited.append({
            **h, "trade_size_usd": trade, "trade_pct": pct,
        })

    # BIG ADDS / BIG TRIMS — must satisfy both share-delta AND trade-pct
    for k, h_new in new_map.items():
        h_old = old_map.get(k)
        if not h_old or h_old.get("shares", 0) == 0:
            continue
        share_delta = (h_new["shares"] - h_old["shares"]) / h_old["shares"]
        if abs(share_delta) < BIG_DELTA:
            continue
        trade = _trade_size_usd(h_new, h_old)
        denom = total_new if share_delta > 0 else total_prior
        pct = trade / denom
        if pct < MIN_TRADE_PCT_OF_PORTFOLIO:
            continue
        annotated = {
            **h_new,
            "delta_pct": share_delta,
            "prior_shares": h_old["shares"],
            "prior_value_usd": h_old.get("value_usd", 0),
            "trade_size_usd": trade,
            "trade_pct": pct,
            "portfolio_pct": h_new.get("value_usd", 0) / total_new,
        }
        if share_delta > 0:
            big_adds.append(annotated)
        else:
            big_trims.append(annotated)

    for lst in (new_positions, exited, big_adds, big_trims):
        lst.sort(key=lambda h: -h.get("trade_size_usd", 0))

    return {
        "new_positions": new_positions,
        "exited": exited,
        "big_adds": big_adds,
        "big_trims": big_trims,
        "total_value_usd": total_new,
        "total_value_prior_usd": total_prior,
        "holding_count": len(new_map),
        "is_first_snapshot": old is None,
    }


def _fmt_money(usd: float) -> str:
    if usd >= 1e9:
        return f"${usd / 1e9:.2f}B"
    if usd >= 1e6:
        return f"${usd / 1e6:.0f}M"
    if usd >= 1e3:
        return f"${usd / 1e3:.0f}K"
    return f"${usd:.0f}"


def _fmt_pct(pct: float) -> str:
    """Format a 0-1 fraction as a percentage. Drops to two decimals
    for sub-0.1% values so a tiny option opening doesn't display as
    a meaningless '0.0%'."""
    v = pct * 100
    if 0 < v < 0.1:
        return f"{v:.2f}%"
    return f"{v:.1f}%"


def _fmt_new_or_exit_line(h: dict, marker: str) -> str:
    """Format line for a NEW position or full EXIT — the trade size IS
    the position size, so we show the dollar amount and its share of the
    relevant portfolio."""
    pc = h.get("putcall")
    suffix = f" [{pc}]" if pc else ""
    return (
        f"  {marker} {h.get('issuer', '?')}{suffix}: "
        f"{_fmt_money(h.get('trade_size_usd', 0))} "
        f"({_fmt_pct(h.get('trade_pct', 0))})"
    )


def _fmt_change_line(h: dict, marker: str, sign: str) -> str:
    """Format line for a BIG ADD or BIG TRIM — shows trade size (with
    sign), the new position value, and the trade as % of portfolio."""
    pc = h.get("putcall")
    suffix = f" [{pc}]" if pc else ""
    trade = h.get("trade_size_usd", 0)
    return (
        f"  {marker} {h.get('issuer', '?')}{suffix}: "
        f"{sign}{_fmt_money(trade)} -> {_fmt_money(h.get('value_usd', 0))} "
        f"({_fmt_pct(h.get('trade_pct', 0))})"
    )


def notify_13f_digest(label: str, cik: str, period: str, diff: dict) -> bool:
    """Push one ntfy digest summarizing a 13F-HR filing's diff against the
    prior quarter (or top-10 holdings if no prior snapshot exists)."""
    if not NTFY_TOPIC:
        return False

    title = (
        f"13F: {label} {period} "
        f"({_fmt_money(diff['total_value_usd'])}, "
        f"{diff['holding_count']} positions)"
    )

    body_lines: list[str] = []
    if diff["is_first_snapshot"]:
        body_lines.append(
            "First snapshot for this manager — top 10 holdings by value "
            "(no prior quarter to diff against):"
        )
        top10 = sorted(
            diff["new_positions"], key=lambda h: -h.get("value_usd", 0),
        )[:10]
        for h in top10:
            # First-snapshot holdings have trade_pct = portfolio_pct because
            # the whole position is "new" against an empty prior.
            body_lines.append(_fmt_new_or_exit_line(h, "*"))
    else:
        # Split each section into commons (capped) and options (uncapped) —
        # the user wants EVERY option open/close surfaced regardless of size.
        new_opts = [h for h in diff["new_positions"] if _is_option(h)]
        new_commons = [h for h in diff["new_positions"] if not _is_option(h)]
        exit_opts = [h for h in diff["exited"] if _is_option(h)]
        exit_commons = [h for h in diff["exited"] if not _is_option(h)]

        sections = 0
        if new_commons:
            body_lines.append(
                f"NEW commons ({len(new_commons)}) "
                f"— % of current portfolio:"
            )
            for h in new_commons[:8]:
                body_lines.append(_fmt_new_or_exit_line(h, "+"))
            body_lines.append("")
            sections += 1
        if new_opts:
            body_lines.append(
                f"NEW options ({len(new_opts)}) — all shown, "
                f"% of current portfolio:"
            )
            for h in new_opts:
                body_lines.append(_fmt_new_or_exit_line(h, "+"))
            body_lines.append("")
            sections += 1
        if exit_commons:
            body_lines.append(
                f"EXITED commons ({len(exit_commons)}) "
                f"— % of prior portfolio:"
            )
            for h in exit_commons[:8]:
                body_lines.append(_fmt_new_or_exit_line(h, "-"))
            body_lines.append("")
            sections += 1
        if exit_opts:
            body_lines.append(
                f"EXITED options ({len(exit_opts)}) — all shown, "
                f"% of prior portfolio:"
            )
            for h in exit_opts:
                body_lines.append(_fmt_new_or_exit_line(h, "-"))
            body_lines.append("")
            sections += 1
        if diff["big_adds"]:
            body_lines.append(
                f"BIG ADDS ({len(diff['big_adds'])}) "
                f"— +X% trade size of current portfolio:"
            )
            for h in diff["big_adds"][:6]:
                body_lines.append(_fmt_change_line(h, "^", "+"))
            body_lines.append("")
            sections += 1
        if diff["big_trims"]:
            body_lines.append(
                f"BIG TRIMS ({len(diff['big_trims'])}) "
                f"— -X% trade size of prior portfolio:"
            )
            for h in diff["big_trims"][:6]:
                body_lines.append(_fmt_change_line(h, "v", "-"))
            body_lines.append("")
            sections += 1
        if sections == 0:
            body_lines.append(
                "No material changes from prior quarter "
                f"(all trades < {_fmt_pct(MIN_TRADE_PCT_OF_PORTFOLIO)} "
                "of portfolio and no option opens/closes)."
            )

    body_lines.append("")
    body_lines.append(
        f"https://www.sec.gov/cgi-bin/browse-edgar?"
        f"action=getcompany&CIK={cik}&type=13F-HR"
    )
    body = "\n".join(body_lines).rstrip().encode("utf-8")
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "default",
        "Tags": "moneybag",
    }
    url = f"https://ntfy.sh/{NTFY_TOPIC}"
    try:
        req = urllib.request.Request(
            url, data=body, headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(
            f"# 13F digest push failed for {label} {period}: {e}",
            file=sys.stderr,
        )
        return False


def poll_managed_filers() -> int:
    """For each MANAGED_FILERS entry, check EDGAR for new 13F-HR filings,
    parse holdings, diff against the prior quarter's snapshot, push one
    digest per new filing, and persist the snapshot.

    First-run behaviour per filer: if no snapshots exist yet for the CIK,
    backfill the atom feed's history silently (no digests pushed) so that
    future filings have a real diff baseline. Subsequent runs push a digest
    on every genuinely-new filing.

    Returns the number of digests pushed this cycle."""
    THIRTEEN_F_DIR.mkdir(parents=True, exist_ok=True)
    pushed = 0
    for label, cik in MANAGED_FILERS:
        # Snapshot existence pre-loop is the "first run for this CIK" flag.
        has_prior_for_cik = any(
            p.name.startswith(f"{cik}_") and p.suffix == ".json"
            for p in THIRTEEN_F_DIR.iterdir()
        )
        try:
            atom = fetch(_13f_atom_url(cik), timeout=20)
        except Exception as e:
            print(
                f"# 13F atom fetch failed for {label} ({cik}): {e}",
                file=sys.stderr,
            )
            continue
        entries = _parse_13f_atom(atom)
        # Process oldest -> newest so each filing's diff sees the prior one.
        entries.sort(key=lambda e: e.get("filing_date") or "")
        for entry in entries:
            try:
                xml_url, period = _resolve_holdings_xml_url(entry["index_link"])
            except Exception as e:
                print(
                    f"# 13F index parse failed for {label} {entry['accession']}: {e}",
                    file=sys.stderr,
                )
                continue
            if not period:
                continue
            snap_path = _13f_snapshot_path(cik, period)
            if snap_path.exists():
                continue  # already processed this filing

            try:
                holdings_raw = _parse_holdings_xml(
                    fetch(xml_url, timeout=20)
                )
                holdings = _normalize_holdings_to_usd(holdings_raw)
            except Exception as e:
                print(
                    f"# 13F holdings parse failed for {label} {entry['accession']}: {e}",
                    file=sys.stderr,
                )
                continue

            prior = _latest_prior_snapshot(cik, period)

            # Persist the snapshot regardless of push success — durable trail.
            snap_path.write_text(json.dumps({
                "cik": cik,
                "manager": label,
                "period_end": period,
                "accession": entry["accession"],
                "filed_date": entry.get("filing_date"),
                "holdings": holdings,
            }, indent=2))

            # On first-run backfill for this CIK, skip pushing so the user
            # isn't spammed with one digest per historical quarter.
            if not has_prior_for_cik:
                continue

            d = diff_13f(prior, holdings)
            try:
                if notify_13f_digest(label, cik, period, d):
                    pushed += 1
            except Exception as e:
                print(f"# 13F digest push exception: {e}", file=sys.stderr)
    return pushed


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

    # Enrich SEC 13 items with their filer's name (extracted from the linked
    # index.htm — the atom feed only carries the subject company). Done after
    # state save so a failed enrichment doesn't cause us to re-process the
    # item on the next cron tick.
    for it in new_items:
        if it.get("source") == "SEC 13":
            try:
                enrich_sec_13(it)
            except Exception as e:
                print(
                    f"# enrich_sec_13 failed for {it.get('link')}: {e}",
                    file=sys.stderr,
                )

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
    suppressed_skip = 0
    for item, cls in zip(new_items, classifications):
        if cls["impact"] < ALERT_THRESHOLD:
            continue
        # User policy: ONLY notify on a clear buy or sell. Skip everything else.
        if cls.get("recommendation") not in ("buy", "sell"):
            suppressed_skip += 1
            continue
        if ntfy_post(item, cls):
            # Persist the alert + entry-price snapshot to the durable ledger.
            try:
                log_alert(item, cls)
            except Exception as e:
                print(f"# log_alert failed: {e}", file=sys.stderr)
            tickers = cls.get("tickers") or []
            ticker_syms = [
                t["symbol"] if isinstance(t, dict) else str(t)
                for t in tickers
            ] or ["macro"]
            rec = cls.get("recommendation", "?")
            alerted.append(
                f"{item['source']}/{','.join(ticker_syms)}"
                f"({cls['impact']},{rec})"
            )

    # Score any alerts that have aged past the 24h window since their
    # entry-price snapshot. Idempotent — already-scored alerts are skipped.
    try:
        newly_scored = score_pending_alerts()
    except Exception as e:
        print(f"# score_pending_alerts failed: {e}", file=sys.stderr)
        newly_scored = 0

    # Push the daily digest if we're inside the 8pm Zurich hour and haven't
    # sent yet today.
    try:
        digest_sent = maybe_send_daily_digest()
    except Exception as e:
        print(f"# maybe_send_daily_digest failed: {e}", file=sys.stderr)
        digest_sent = False

    # Check the managed filers (Duquesne / Druckenmiller, Situational
    # Awareness / Aschenbrenner) for new 13F-HR filings and push a diff
    # digest. Their 13Fs land 45 days after quarter-end and are rare, so
    # this is a no-op on most cron ticks.
    try:
        managed_pushed = poll_managed_filers()
    except Exception as e:
        print(f"# poll_managed_filers failed: {e}", file=sys.stderr)
        managed_pushed = 0

    print(
        f"Fetched {len(all_items)} items, {len(new_items)} new, "
        f"{len(alerted)} alerted, {suppressed_skip} suppressed (no clear "
        f"buy/sell), {newly_scored} newly scored, "
        f"digest_sent={digest_sent}, "
        f"managed_13f_pushed={managed_pushed} "
        f"[{', '.join(alerted) if alerted else '-'}]. "
        f"Errors: {errors if errors else 'none'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
