# Mention Edge — Hostinger Docker deployment

This repository contains the Polymarket mention-market bot and dashboard.

## Safe initial deployment

Import the repository URL in Hostinger Docker Manager. The initial Compose
configuration builds the dashboard and runs only `python run.py --check` for
the bot. It cannot place an order because `allow_live_trading` remains false
and no wallet credentials are included.

Open the dashboard at `http://YOUR_VPS_IP:3000` after Hostinger reports the
deployment healthy.

## Live activation

Live activation is a separate owner-approved step. Never commit a private key.
Add credentials only through Hostinger's protected environment-variable UI,
then change the bot command only after reviewing the final checks.
