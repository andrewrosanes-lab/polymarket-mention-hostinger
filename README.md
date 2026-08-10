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

Historical context and the current market prior estimate mention probability.
Persistent order-book pressure requires three samples and may adjust confidence
by no more than five points. Liquidity and volume remain $200 hard execution
gates but never add confidence. The executable ask must still leave at least
six percentage points of model edge.

Each condition can be entered only once for its lifetime. A normalized
subject/phrase can be entered only once per UTC day. Positions are held through
resolution; resolved positions are reconciled against Polymarket's portfolio
data and flagged when onchain redemption is required.

## Authenticated dashboard controls

The dashboard can tune a strict whitelist without exposing wallet credentials:
minimum confidence (65–90), minimum model edge (6–20%), timing confirmation
(45–90), known-event entry window (1–8 hours), and pause/resume for new entries.
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
