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
    episode_target: object | None = None


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
    momentum: float
    pricing_edge: float
    model_edge_pct: float
    explanation: str
    timing_score: float = 50.0
    book_confirmation: float = 50.0
    book_adjustment: float = 0.0
    book_samples: int = 0
    independent_probability: float = 50.0
    context_confidence: float = 50.0
    microstructure_ready: bool = False
    absorption: bool = False


@dataclass(frozen=True)
class MicrostructureSignal:
    score: float = 50.0
    wobi: float = 0.0
    trade_flow: float = 0.0
    delta_obi: float = 0.0
    persistence: float = 0.0
    microprice_score: float = 50.0
    samples: int = 0
    trade_samples: int = 0
    age_seconds: float = 0.0
    absorption: bool = False
    ready: bool = False
