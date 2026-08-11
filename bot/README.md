# Polymarket Mention Bot

This is a separate bot for markets such as “Will Trump say X during a
speech?” or mentions during NFL broadcasts. It does not share code or state
with the weather bot.

## Strategy model

| Role | Component | Relative weight | Method |
|---|---|---:|---|
| Confidence | Context-matched historical mentions | 30% | Beta-smoothed resolved mention rate |
| Confidence | Event/context relevance | 20% | Exact-context evidence coverage or calibrated YouTube context |
| Confidence | Market prior | 15% | Current selected-outcome midpoint |
| Confidence | Live microstructure | 25% | WOBI, executed flow, delta OBI, persistence, and microprice |
| Confidence | Momentum | 10% | Selected outcome's recent price direction |
| Hard gate | Model mispricing | 3 points | Independent mention probability versus actual order price |

The independent historical/context model selects YES or NO. Final Option C
confidence must be 70–100, while the independent model must exceed the actual
maker price or worst FOK price by at least three points. Scores in the middle,
microstructure windows without at least 20 seconds of persistent snapshots and
executed flow, and absorption signals do not trade.

| Tier | Confidence | Position |
|---|---:|---:|
| C | 70–<80 | $3 |
| B | 80–<90 | $4 |
| A | 90–100 | $5 |

The authenticated dashboard may tighten Tier C's effective minimum up to 90,
but can never lower it below 70. It can also tighten timing and the known-event
window, or pause new entries. Resolution reconciliation remains
active while entries are paused.

Historical data is segmented by context (`speech`, `debate`,
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

Published YouTube transcripts can be discovered through Supadata by setting
`SUPADATA_API_KEY` in the VPS `.env`. The adapter uses native captions only,
counts target phrases, stores only counts and source metadata, and discards
transcript text. It is rate-limited to approximately three credits per day by
default. Option C is armed with probability weights of 29% existing history,
10% YouTube history, and 10% market prior. Each exact event/context remains
**shadow-only with zero live weight** until it has at least five comparable
transcripts and 30 resolved shadow predictions, and its Brier score improves
on the existing model by at least 0.005. Missing evidence is omitted and the
available weights are renormalized rather than silently replaced with 50%.

Questions phrased as “this week” are evaluated by historical calendar week,
not per transcript. Numeric wording such as “10+ times” is also preserved as a
count threshold instead of being reduced to a simple mentioned/not-mentioned
flag.

News and arbitrage are excluded from both confidence and dashboard indicators.
Liquidity and volume are execution-capacity gates only.

## Safety defaults

- Live-only execution with a separate owner-approval lock; it never silently paper trades
- Five total open positions and one lifetime entry per condition
- One entry per normalized subject/phrase per UTC day
- Liquidity, spread, entry-price, and time gates
- Minimum three-point independent model mispricing over the actual order price
- Option C microstructure must be live for 20 seconds with executed-flow data
- Aggressive flow without favorable price response triggers an absorption veto
- Configurable timing score (currently zero)
- Liquidity and volume are capacity checks only (currently disabled)
- Entry prices restricted to $0.19–$0.93
- Entries require a known start no more than 24 hours away
- Post-only GTD maker order first; cancel then FOK taker fallback inside two hours
- Taker fallback slippage dynamically bounded between 1% and 3%
- No stop-loss and no taker exit
- Maker-only staged profit lock: +50% arms break-even, +100% locks +50%,
  and +200% locks +100%; an unfilled maker exit may miss its floor
- Positions not closed by the profit lock remain held through resolution
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
slug and verified UTC start time under `scheduled_events`. Unknown starts are
skipped; verified starts must be within eight hours.

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
not trigger a second full-size taker leg. The taker fallback is cancelled if
slippage removes the required edge. Proxy-wallet auto-redemption is not guessed:
resolved tokens are marked redeemable for an official/manual redemption flow.
