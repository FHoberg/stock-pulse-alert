# Briefing for Claude

When opening a new Cowork session in this folder, paste the paragraph below
into the first message so the assistant has full context immediately. The
sections beneath give a deeper map of the codebase.

---

## Briefing paragraph (paste this into a new chat)

This is a personal stock alert system. The repo is
`https://github.com/FHoberg/stock-pulse-alert` and lives locally at
`~/Documents/GitHub/stock-pulse-alert` on my main computer. A GitHub
Actions cron polls five feeds — SEC EDGAR 8-K filings, Federal Reserve
press releases, Trump posts via trumpstruth.org, NIST news (for
Commerce / CHIPS-Act / federal-grant announcements that bypass the 8-K
channel, e.g. the May 2026 $2B quantum LOI batch), and SEC EDGAR
Schedule 13D/13G filings (for activist stakes and smart-money 5%
crossings, e.g. BlackRock's 13G in Rubrik) — every 10 minutes. New
items are classified by
Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) and pushed to my phone via
ntfy.sh, but ONLY when the call is a clear BUY or SELL; anything ambiguous
is suppressed by design. Each fired alert is logged with a Yahoo Finance
entry-price snapshot to `state/alerts.jsonl`, then automatically scored 24h
later into `state/scorecard.jsonl` with a `correct` flag. A daily digest is
pushed at 20:00 Europe/Zurich summarizing the last 24h of scorecard
activity. State is durable: the workflow commits `state/*.jsonl` and
`state/seen.json` back to the repo on each run. Read `monitor.py` end to
end before suggesting changes, check the latest rows of
`state/alerts.jsonl` and `state/scorecard.jsonl` to see what's actually
been firing, and run `git log --oneline -20` for recent direction.

---

## Architecture map

- **`monitor.py`** — single-file Python script, stdlib only, no pip deps.
  Sections in order: configuration, feed fetch + parse, dedup state,
  classification (LLM with keyword fallback), notification, performance
  tracking (alert log + 24h scoring + daily digest), main entry point.
- **`.github/workflows/monitor.yml`** — runs `monitor.py` every 10 min via
  cron, plus `workflow_dispatch` for manual triggers. Has
  `permissions: contents: write` so the final step can commit state files
  back to the repo for a durable audit trail.
- **`state/seen.json`** — dedup set of feed item IDs (capped at 5000).
- **`state/alerts.jsonl`** — append-only ledger; one row per fired alert
  with timestamp, source, headline, recommendation, impact, explanation,
  and per-ticker entry-price snapshot from Yahoo.
- **`state/scorecard.jsonl`** — append-only ledger; one row per scored
  alert with per-ticker exit price, signed pct move, and `correct` bool.
- **`state/digest_state.json`** — one-line file tracking the last Zurich
  date the daily digest was sent, so it fires exactly once per local day.
- **`README.md`** — original setup instructions (ntfy topic, GitHub
  secrets). Slightly stale on tracking — the source of truth is this file.

## Feed-specific notes

- **NIST** (`https://www.nist.gov/news-events/news/rss.xml`) — the LLM is
  tuned to fire on CHIPS-Act letters of intent, federal grants, equity
  stakes, and DOE loan guarantees naming publicly-traded recipients (BUY
  the named tickers or the sector bellwether ETF). Routine standards /
  research / regulatory news → SKIP.
- **SEC 13** (`...action=getcurrent&type=SC+13&output=atom`) — noisiest
  feed by far. The LLM is told to SKIP routine 13G repositioning by
  Vanguard / BlackRock-as-index / State Street / FMR in mega-caps, and
  only fire on (a) any 13D / 13D/A (activist stake → BUY), (b) any 13G
  whose filer is on the smart-money whitelist in `monitor.py`'s
  `SMART_MONEY_FILERS` constant, or (c) first-time 5%+ crossings by big
  index houses in smaller-cap names (which is what caught BlackRock →
  Rubrik). The keyword fallback applies the same logic.

## Required GitHub Actions secrets

`NTFY_TOPIC` (the ntfy.sh topic to publish to), `ANTHROPIC_API_KEY` (for
LLM classification — keyword fallback used if absent), `USER_AGENT` (must
contain a real contact email; SEC EDGAR returns 403 otherwise).

## Design rules that should not be relaxed without discussion

- **Buy-or-sell only.** If `recommendation` isn't `"buy"` or `"sell"`, the
  notification is suppressed. Haiku is explicitly instructed to use
  `"skip"` liberally rather than guess.
- **Never fabricate identifiers.** ISINs from the LLM are validated
  against `^[A-Z]{2}[A-Z0-9]{9}[0-9]$` and dropped silently if malformed.
  WKN support has been deferred because Haiku's WKN training data is
  weaker and hallucination risk is higher.
- **State is committed back, not cached.** Earlier versions used
  `actions/cache`; that was replaced because cache eviction lost history.
  Don't reintroduce caching for `state/` without a migration plan for the
  ledger files.

## Open follow-ups (not blocking)

- **Non-US ticker coverage.** Yahoo's free endpoint covers US tickers and
  ETFs reliably. If alerts ever feature EUR/Xetra/Asian tickers, swap or
  augment with Stooq or Alpha Vantage.
- **Node.js 20 deprecation in Actions.** Suppressible via
  `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"` in the workflow `env`. Not
  urgent — deadline is June 2026.
- **Prompt tuning.** The LLM `system` prompt in `monitor.py` was written
  without seeing real notification output. Once a few days of alerts have
  fired and the user has screenshots, tune wording, length, and direction
  quality based on actual output.
- **Repeated tickers in the same run.** Occasionally the same symbol
  appears in two alerts within minutes (e.g., two 8-Ks from the same
  issuer). Optional: dedup tickers within a single cron tick if it gets
  noisy.
