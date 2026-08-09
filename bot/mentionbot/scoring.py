from __future__ import annotations

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
            hist: float, news: float, market_prior: float,
            momentum: float, cfg: dict,
            news_count: int, history_scope: str = "exact",
            history_samples: int = 0) -> Score:
    """Separate probability estimation from market-timing confirmation."""
    probability_weights = cfg["probability_weights"]
    probability_weight = sum(probability_weights.values())
    yes = (hist*probability_weights["historical_context"]
           + news*probability_weights["news_live_impact"]
           + market_prior*probability_weights["market_prior"]) / probability_weight
    yes = max(0, min(100, yes))
    side = "YES" if yes >= 50 else "NO"
    confidence = yes if side == "YES" else 100 - yes
    selected_book = yes_book if side == "YES" else no_book
    selected_momentum = momentum if side == "YES" else 100 - momentum
    timing_weights = cfg["timing_weights"]
    timing_weight = sum(timing_weights.values())
    timing = (selected_book.score*timing_weights["order_book_imbalance"]
              + selected_momentum*timing_weights["momentum"]) / timing_weight
    executable = yes_book.best_ask if side == "YES" else no_book.best_ask
    model_probability = yes / 100 if side == "YES" else (100 - yes) / 100
    model_edge_pct = (model_probability - executable) * 100
    cross_book_arb_pct = (1 - yes_book.best_ask - no_book.best_ask) * 100
    pricing_edge = max(0, min(100, 50 + model_edge_pct * 2))
    tier, size = tier_for(confidence, cfg["tiers"])
    explanation = (f"historical={hist:.1f} ({history_scope}, n={history_samples}); "
                   f"news={news:.1f} ({news_count} relevant); market_prior={market_prior:.1f}; "
                   f"timing={timing:.1f}; pricing_edge={pricing_edge:.1f}; "
                   f"model_edge={model_edge_pct:.1f}%; cross_book_arb={cross_book_arb_pct:.1f}%")
    return Score(yes, confidence, side, tier, size, hist, yes_book.score, news,
                 momentum, pricing_edge, model_edge_pct, cross_book_arb_pct,
                 explanation, timing)
