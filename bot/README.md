# Polymarket Mention Bot

This is a separate bot for markets such as “Will Trump say X during a
speech?” or mentions during NFL broadcasts. It does not share code or state
with the weather bot.

## Strategy model

| Role | Component | Relative weight | Method |
|---|---|---:|---|
| Probability | Context-matched historical mentions | 35 | Beta-smoothed hit rate from official GovInfo presidential transcripts, with resolved Gamma markets as fallback |
| Probability | News/live impact | 25 | Grounded, deduplicated Google News RSS headlines with time decay |
| Probability | Market prior | 14 | Current executable-book midpoint |
| Timing | Order-book imbalance | 20 | Bid versus ask notional near top-of-book |
| Timing | Momentum | 14 | Selected outcome's recent price direction |
| Hard gate | Pricing edge | 6 percentage points | Model probability versus executable ask |

The probability components are normalized into a YES probability. It buys YES
when confidence is 65–100, or NO when inverted confidence is 65–100, only when
timing is at least 45 and executable model edge is at least six percentage
points. Scores in the middle do not trade.

| Tier | Confidence | Position |
|---|---:|---:|
| C | 65–<80 | $3 |
| B | 80–<90 | $4 |
| A | 90–100 | $5 |

The authenticated dashboard may tighten Tier C's effective minimum up to 90,
but can never lower it below 65. It can also tighten model edge, timing, and
the known-event window, disable news participation, or pause new entries.
Position monitoring remains active while entries are paused.

The news component is a transparent headline heuristic, not human-level news
understanding. Historical data is segmented by context (`speech`, `debate`,
`interview`, `press_conference`, `nfl_game`, or `other`).

For Trump markets, the bot reads the official GovInfo Daily Compilation of
Presidential Documents through GovInfo's harvesting sitemaps. It counts only
paragraphs attributed to `The President`, stores the phrase count, title, and
source URL in SQLite, and discards transcript text. The refresh is limited to
the newest 120 qualifying documents over the last two years and runs at most
once per phrase per day. GovInfo does not cover NFL or entertainment audio, so
those markets continue to use resolved Gamma outcomes until a permitted
transcript source is configured.

Television markets can additionally learn from prior English subtitle files
through the official OpenSubtitles REST API. Set `OPENSUBTITLES_API_KEY` only
in the VPS `.env`; the key is optional and the bot retains Gamma history when
it is absent or the service is unavailable. Subtitle counts are historical
evidence only and are never represented as the official resolution source.

Questions phrased as “this week” are evaluated by historical calendar week,
not per transcript. Numeric wording such as “10+ times” is also preserved as a
count threshold instead of being reduced to a simple mentioned/not-mentioned
flag.

When no grounded news items exist, news is marked unavailable and excluded
from probability instead of contributing a misleading neutral 50. “Pricing
edge” is not arbitrage. The bot separately qualifies gross cross-book
arbitrage as `1 - YES ask - NO ask` at 6% or more and reports a distinct
paired-execution confidence metric based on arb edge and both book spreads. It reports qualified
opportunities as `ARB WATCH`; live paired execution is safety-locked because a
batch of two FOK orders is not documented as atomic across both outcome legs.

## Safety defaults

- Live-only execution with a separate owner-approval lock; it never silently paper trades
- Five total open positions and no more than two entries per contract
- Liquidity, spread, entry-price, and time gates
- Minimum six-percentage-point modeled edge over the executable ask
- Minimum timing score of 45
- At least $800 liquidity and $800 traded volume
- Entry prices restricted to $0.16–$0.93
- Entries with known starts only from four hours before the event through the
  live event; markets without a published start time remain eligible
- Multiple entries are tracked separately, capped at two per contract, and
  still count against the five-position limit
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
slug and verified UTC start time under `scheduled_events`. Unknown starts remain
eligible as previously approved; known starts must be within four hours.

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
