from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Market:
    condition_id: str
    question: str
    event_title: str
    slug: str
    event_slug: str
    event_start: datetime | None
    end_date: datetime
    yes_token: str
    no_token: str
    yes_price: float
    no_price: float
    liquidity: float
    volume: float
    neg_risk: bool
    tick_size: str
    subject: str
    phrase: str
    context: str


@dataclass(frozen=True)
class BookSignal:
    score: float
    best_bid: float
    best_ask: float
    spread_pct: float
    bid_depth: float
    ask_depth: float


@dataclass(frozen=True)
class Score:
    yes_probability: float
    confidence: float
    side: str
    tier: str | None
    size_usd: float
    historical: float
    orderbook: float
    news: float
    momentum: float
    pricing_edge: float
    model_edge_pct: float
    cross_book_arb_pct: float
    explanation: str
