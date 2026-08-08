# Polymarket Mention Bot

This is a separate bot for markets such as “Will Trump say X during a
speech?” or mentions during NFL broadcasts. It does not share code or state
with the weather bot.

## Scoring model

| Component | Weight | Method |
|---|---:|---|
| Context-matched historical mentions | 35% | Beta-smoothed hit rate for the same subject, phrase, and context |
| Order-book imbalance | 20% | Bid versus ask notional near top-of-book |
| News/live impact | 25% | Recent relevant Google News RSS headlines with time decay |
| Market prior and momentum | 14% | Market probability plus one-day price movement |
| Pricing edge | 6% | Model estimate versus the executable ask |

It buys YES when the weighted score is 70–100. When the score is 0–30,
inverted confidence is 70–100 and it buys NO. Scores in the middle do not
trade.

| Tier | Confidence | Position |
|---|---:|---:|
| C | 70–<80 | $3 |
| B | 80–<90 | $4 |
| A | 90–100 | $5 |

The news component is a transparent headline heuristic, not human-level news
understanding. Historical data is segmented by context (`speech`, `debate`,
`interview`, `press_conference`, `nfl_game`, or `other`).

“Pricing edge” is not guaranteed arbitrage. The bot separately reports gross
cross-book arbitrage as `1 - YES ask - NO ask`. A real two-leg opportunity must
remain at least 6% after fees and slippage. This version reports that condition
but does not atomically execute both legs.

## Safety defaults

- Live-only execution with a separate owner-approval lock; it never silently paper trades
- Five positions and $20 maximum deployment
- $10 daily realized-loss halt
- Liquidity, spread, entry-price, and time gates
- Minimum six-percentage-point modeled edge over the executable ask
- At least $800 liquidity and $800 traded volume
- Entry prices restricted to $0.16–$0.93
- Entries only from two hours before the known event start through the live event
- Post-only GTC maker order first; cancel then FAK taker fallback after 20 seconds
- Taker fallback slippage dynamically bounded between 1% and 3%
- Entries from $0.16–$0.37 use both a 50% hard stop and 50% trailing drawdown
- 30% take profit, 25% stop, exit 15 minutes before scheduled resolution
- `state/HALT` kill switch
- SQLite state and JSONL journal

## Install and read-only check

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
./venv/bin/python run.py --check
```

Discovery primarily uses verified Gamma tag IDs; text queries are a secondary
fallback. Populate resolved, context-matched history:

```bash
./venv/bin/python run.py --record "Donald Trump" "tariff" speech yes
./venv/bin/python run.py --record "Chiefs broadcast" "three-peat" nfl_game no
```

For speech/game markets whose actual start is absent from Gamma, add the event
slug and verified UTC start time under `scheduled_events`. The bot skips an
unknown start rather than incorrectly using the resolution deadline.

## Live lock

Polymarket production uses CLOB V2, so this project requires
`py-clob-client-v2`; the legacy client does not work. Store credentials only in
`.env`, use a dedicated funded wallet, and verify its signature type and funder
address. Live mode requires both:

```yaml
mode: live
allow_live_trading: true
```

Never paste or commit the private key. The sample VPS unit is
`deploy/mention-bot.service`.

## Limitations

This is experimental trading software, not a profit guarantee. Exact market
resolution wording and source transcripts control settlement. Maker orders are
polled and reconciled before a position is journaled; a partial maker fill does
not trigger a second full-size taker leg. Automatic redemption and atomic
two-leg arbitrage are not included; the default strategy exits before the
scheduled end.
