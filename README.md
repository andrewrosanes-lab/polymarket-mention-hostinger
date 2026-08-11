# Mention Edge — Hostinger Docker deployment

This repository contains the Polymarket mention-market bot and dashboard.

## Safe initial deployment

Import the repository URL in Hostinger Docker Manager. The initial Compose
configuration builds the dashboard and starts the live bot. Compose fails
closed unless the required wallet variables are supplied through Hostinger.
No wallet credentials are included in this repository.

Open the dashboard at `http://YOUR_VPS_IP:3000` after Hostinger reports the
deployment healthy.

The historical learner uses the official GovInfo presidential-document
sitemaps for Trump transcript statistics. It stores counts and source metadata,
not transcript bodies. No GovInfo API key is required.

Optional television history uses `OPENSUBTITLES_API_KEY`, supplied only in the
VPS environment. Missing credentials do not prevent the live bot from running.

## Evidence and dashboard status

The bot writes an atomic, read-only operational snapshot to
`state/status.json`. Docker mounts the state volume read-only in the dashboard,
which reports the latest cycle, open exposure, book confirmation, and the
exact historical scope and sample size used for each evaluated contract. A
snapshot older than ten minutes is shown as disconnected.

TV subtitle statistics are isolated by series, and markets without phrase-level
evidence receive a neutral history input instead of a generic cross-market
fallback. News and arbitrage are excluded from scoring and the dashboard.

Option C uses 30% historical mentions, 20% event/context relevance, 15% market
prior, 25% live microstructure, and 10% momentum. The independent mention model
chooses direction and must exceed the actual entry price by at least three
percentage points. Live microstructure requires five-level weighted OBI,
executed flow, delta OBI, persistence, and microprice; adverse absorption vetoes
entry. Liquidity and volume remain execution-capacity inputs, never confidence.

Each condition can be entered only once for its lifetime. A normalized
subject/phrase can be entered only once per UTC day. A maker-only staged profit
lock arms break-even after +50%, +50% profit after +100%, and +100% profit after
+200%. There is no loss exit or taker fallback; an unfilled maker can miss the
floor. Remaining positions are reconciled through resolution and flagged when
onchain redemption is required.

## Authenticated dashboard controls

The dashboard can tune a strict whitelist without exposing wallet credentials:
minimum confidence (65–90), with a fixed maximum tradeable confidence of 93%;
timing confirmation (0–90), known-event entry
window (1–24 hours), and pause/resume for new entries.
Resolution reconciliation continues while entries are paused. Changes are validated twice: once by
the dashboard API and again by the bot. Invalid control files fail closed by
pausing new entries.

Set a unique admin token of at least 24 characters in the VPS `.env`:

```bash
DASHBOARD_ADMIN_TOKEN=replace-with-a-long-random-secret
```

The browser holds the token only for the save request; it is never written to
the status feed or bundled into the dashboard.

## Live activation

Live activation was explicitly approved by the owner. Never commit a private
key. Add credentials only through Hostinger's protected environment-variable
UI. Required variables are `POLYMARKET_PRIVATE_KEY` and
`POLYMARKET_FUNDER_ADDRESS`; CLOB API credentials are optional because the
client can derive them from the wallet.
