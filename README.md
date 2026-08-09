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
which reports the latest cycle, open exposure, grounded news sources, and the
exact historical scope and sample size used for each evaluated contract. A
snapshot older than ten minutes is shown as disconnected.

News evidence is deliberately fail-neutral: an article must contain both the
market's event entity (person, TV series, or NFL matchup) and the target phrase.
Duplicate headlines are counted once. TV subtitle statistics are isolated by
series, and TV/NFL markets without phrase-level evidence receive a neutral 50
instead of a generic cross-market fallback.

## Live activation

Live activation was explicitly approved by the owner. Never commit a private
key. Add credentials only through Hostinger's protected environment-variable
UI. Required variables are `POLYMARKET_PRIVATE_KEY` and
`POLYMARKET_FUNDER_ADDRESS`; CLOB API credentials are optional because the
client can derive them from the wallet.
