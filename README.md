# Stock Pulse Alert

Polls market-moving news feeds every 10 minutes via GitHub Actions, classifies
new items by estimated impact on stock prices, and pushes high-impact items to
your phone via [ntfy.sh](https://ntfy.sh).

**Sources monitored**

- SEC EDGAR — current 8-K filings (mergers, executive departures, bankruptcies, restatements, etc.)
- Federal Reserve — official press releases (rate decisions, FOMC, regulatory)
- Trump posts — via the public `trumpstruth.org` mirror

**How alerts are decided**

Each new item is rated 1–10 for likely market impact. Only items rated **≥ 7**
trigger a push notification. Calibration:

- 7 = meaningful (1–3% move likely on relevant ticker)
- 8 = significant (3–7% move or sector-wide effect)
- 9–10 = major (large moves, broad market impact)

If `ANTHROPIC_API_KEY` is set, classification uses Claude Haiku 4.5 for nuanced
judgment and ticker extraction. Otherwise it falls back to deterministic
keyword rules (coarser, but free and works offline).

---

## Setup (~15 minutes)

### 1. Push this folder to GitHub

```bash
cd stock-pulse-alert
git init
git add .
git commit -m "Initial commit"
git branch -M main
# Create the repo on github.com first (recommended: PUBLIC — see note below)
git remote add origin https://github.com/<your-username>/stock-pulse-alert.git
git push -u origin main
```

> **Public vs private repo:** GitHub Actions is unlimited on public repos
> and capped at 2,000 free minutes/month on private repos. At a 10-minute
> cadence this monitor uses ~2,200 min/month — slightly over the free private
> tier. **Recommend a public repo.** Nothing in this codebase is sensitive;
> the SEC filings and Trump posts it processes are already public. Your
> ntfy topic and Anthropic key live in GitHub Secrets, never in the code.

### 2. Set up ntfy (the notification channel)

1. Install the **ntfy** app on your phone (App Store or Google Play — search "ntfy").
2. Open it, tap the **+** to subscribe to a topic.
3. Pick a hard-to-guess topic name. Anything that's a long random string works,
   for example: `stock-pulse-h7k2m4q9j3rd`. **Anyone who knows the topic
   name can read your alerts**, so don't share it.
4. That's it — you're subscribed. No account, no signup.

You can test the channel right now from any terminal:

```bash
curl -d "Hello from stock-pulse" https://ntfy.sh/<your-topic>
```

The notification should arrive on your phone within a couple seconds.

### 3. (Optional but recommended) Get an Anthropic API key

For nuanced classification, sign up at
[console.anthropic.com](https://console.anthropic.com), create an API key, and
add a small amount of credit. Expected cost at 10-minute cadence: ~$0.40/month.

Skip this step to use the keyword-rule fallback — still useful, just noisier.

### 4. Add GitHub repository secrets

In your repo, go to **Settings → Secrets and variables → Actions → New repository secret**
and add:

| Name                | Value                                                         |
| ------------------- | ------------------------------------------------------------- |
| `NTFY_TOPIC`        | The topic name from step 2                                    |
| `USER_AGENT`        | Required by SEC EDGAR. Format: `Your Name your-email@host.com` |
| `ANTHROPIC_API_KEY` | Your key from step 3 (optional)                               |

> **Why the `USER_AGENT` secret?** SEC EDGAR rejects requests that don't
> include a real contact email in the User-Agent header — without it you'll
> get HTTP 403 on the 8-K feed (Fed and Trump feeds still work). Use any
> email you can be reached at; SEC only uses it if your scraper misbehaves.

### 5. Trigger the first run

Go to the **Actions** tab in GitHub, pick the "Stock Pulse Monitor"
workflow, and click **Run workflow → Run workflow**.

The first run **seeds state without alerting** — otherwise you'd get
hundreds of alerts for items already in the feeds. Subsequent runs will only
alert on genuinely new items.

After that the workflow runs itself every 10 minutes.

---

## Local testing

Run the monitor against live feeds without committing or pushing:

```bash
# No API key, keyword-only classification, no ntfy posting
USER_AGENT="Your Name your-email@host.com" \
  NTFY_TOPIC=test-noop-topic \
  ALERT_THRESHOLD=99 \
  python monitor.py
```

Setting `ALERT_THRESHOLD=99` means nothing will actually be posted — useful
for verifying the fetch/parse path works on your machine.

To see the full pipeline locally including alerts:

```bash
USER_AGENT="Your Name your-email@host.com" \
  NTFY_TOPIC=<your-real-topic> \
  ANTHROPIC_API_KEY=sk-ant-... \
  python monitor.py
```

State is written to `./state/seen.json`. Delete that file to force a re-seed.

---

## Tuning

| Knob                 | Where                 | Default | Notes                                      |
| -------------------- | --------------------- | ------- | ------------------------------------------ |
| Alert threshold      | `ALERT_THRESHOLD` env | 7       | Set to 8 for fewer/quieter alerts          |
| Polling cadence      | `monitor.yml` cron    | `*/10`  | Use `*/15` to stay safely under free tier  |
| Sources              | `SOURCES` in `monitor.py` | 3 feeds | Add Business Wire, PR Newswire, etc.       |
| User-Agent           | `USER_AGENT` env      | repo URL| SEC blocks generic UAs — supply contact    |

## Caveats

- **GitHub cron can drift.** Schedules like `*/10` may run every 15–25 min
  under load. For sub-minute reactions you'd need a paid runner or a real VM.
- **The first run does not alert.** This is by design — without it you'd
  get hundreds of historical-item alerts at startup.
- **Beating professional traders is unrealistic.** Hedge funds running on
  co-located hardware will always be faster on 8-Ks. This tool is for
  catching things you'd otherwise miss while sleeping or working, not for
  HFT-style trading.
- **State lives in Actions cache.** GitHub evicts unused caches after 7
  days. As long as the workflow runs at least weekly, state persists. If
  cache is lost, the monitor re-seeds without alerting — minimal harm.

## Project structure

```
stock-pulse-alert/
├── .github/
│   └── workflows/
│       └── monitor.yml      # 10-min cron + cache-backed state
├── state/
│   └── .gitkeep             # placeholder; seen.json is gitignored
├── monitor.py               # fetcher + classifier + ntfy poster
├── README.md
└── .gitignore
```

No third-party dependencies — pure Python stdlib. The Anthropic API call uses
`http.client`, not the SDK, to avoid a `pip install` in CI.
