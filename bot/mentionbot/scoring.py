from __future__ import annotations

import math

from .models import BookSignal, Market, Score


def historical_score(hits: int, total: int) -> float:
    # Beta(2,2) prior prevents tiny samples from claiming certainty. Additional
    # shrinkage keeps fewer than 12 context-matched observations near neutral.
    posterior = (hits + 2) / (total + 4)
    trust = min(1.0, total / 12)
    return 100 * (0.5 * (1 - trust) + posterior * trust)


def tier_for(confidence: float, tiers: list[dict]) -> tuple[str | None, float]:
    for tier in tiers:
        upper_ok = confidence <= tier["max_confidence"] if tier["max_confidence"] == 100 else confidence < tier["max_confidence"]
        if confidence >= tier["min_confidence"] and upper_ok:
            return tier["name"], float(tier["size_usd"])
    return None, 0.0


def combine(market: Market, yes_book: BookSignal, no_book: BookSignal,
            hist: float, news: float, momentum: float, cfg: dict,
            news_count: int) -> Score:
    w = cfg["weights"]
    non_price_weight = (w["historical_context"] + w["order_book_imbalance"]
                        + w["news_live_impact"])
    fundamental = (hist*w["historical_context"]
                   + yes_book.score*w["order_book_imbalance"]
                   + news*w["news_live_impact"]) / non_price_weight
    edge_yes = fundamental - yes_book.best_ask * 100
    pricing_edge = max(0, min(100, 50 + edge_yes * 2))
    yes = hist*w["historical_context"] + yes_book.score*w["order_book_imbalance"] \
        + news*w["news_live_impact"] + momentum*w["market_prior_momentum"] \
        + pricing_edge*w["pricing_edge"]
    yes = max(0, min(100, yes))
    side = "YES" if yes >= 50 else "NO"
    confidence = yes if side == "YES" else 100 - yes
    executable = yes_book.best_ask if side == "YES" else no_book.best_ask
    model_probability = yes / 100 if side == "YES" else (100 - yes) / 100
    model_edge_pct = (model_probability - executable) * 100
    cross_book_arb_pct = (1 - yes_book.best_ask - no_book.best_ask) * 100
    tier, size = tier_for(confidence, cfg["tiers"])
    explanation = (f"historical={hist:.1f}; book={yes_book.score:.1f}; news={news:.1f} "
                   f"({news_count} relevant); momentum={momentum:.1f}; pricing_edge={pricing_edge:.1f}; "
                   f"model_edge={model_edge_pct:.1f}%; cross_book_arb={cross_book_arb_pct:.1f}%")
    return Score(yes, confidence, side, tier, size, hist, yes_book.score, news,
                 momentum, pricing_edge, model_edge_pct, cross_book_arb_pct, explanation)
