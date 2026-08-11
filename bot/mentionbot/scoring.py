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


def probability_from_evidence(hist: float, history_samples: int,
                              market_prior: float, weights: dict,
                              youtube_history: float | None = None,
                              youtube_samples: int = 0) -> float:
    """Normalize available probability evidence without treating missing as 50."""
    evidence = [(market_prior, float(weights["market_prior"]))]
    if history_samples > 0:
        evidence.append((hist, float(weights["historical_context"])))
    youtube_weight = float(weights.get("youtube_history", 0))
    if youtube_history is not None and youtube_samples > 0 and youtube_weight > 0:
        evidence.append((youtube_history, youtube_weight))
    total_weight = sum(weight for _, weight in evidence)
    probability = sum(value * weight for value, weight in evidence) / total_weight
    return max(0, min(100, probability))


def combine(market: Market, yes_book: BookSignal, no_book: BookSignal,
            hist: float, market_prior: float, momentum: float, cfg: dict,
            history_scope: str = "exact",
            history_samples: int = 0, book_confirmation: float = 50.0,
            book_samples: int = 0, youtube_history: float | None = None,
            youtube_samples: int = 0,
            probability_weights_override: dict | None = None) -> Score:
    """Estimate direction, then apply a small persistent-book confirmation.

    News and complement-price arbitrage are deliberately excluded. Order-book
    pressure cannot choose the side or move confidence by more than the
    configured cap, and it has no effect until enough consecutive samples
    exist.
    """
    probability_weights = probability_weights_override or cfg["probability_weights"]
    yes = probability_from_evidence(
        hist, history_samples, market_prior, probability_weights,
        youtube_history, youtube_samples)
    side = "YES" if yes >= 50 else "NO"
    base_confidence = yes if side == "YES" else 100 - yes
    selected_book = yes_book if side == "YES" else no_book
    selected_momentum = momentum if side == "YES" else 100 - momentum
    timing_weights = cfg["timing_weights"]
    timing_weight = sum(timing_weights.values())
    timing = (selected_book.score*timing_weights["order_book_imbalance"]
              + selected_momentum*timing_weights["momentum"]) / timing_weight
    confirmation_cfg = cfg.get("book_confidence") or {}
    required_samples = int(confirmation_cfg.get("required_samples", 3))
    maximum_adjustment = float(confirmation_cfg.get("max_adjustment_points", 5))
    adjustment_scale = float(confirmation_cfg.get("adjustment_scale", 0.10))
    book_adjustment = 0.0
    if book_samples >= required_samples:
        book_adjustment = max(
            -maximum_adjustment,
            min(maximum_adjustment, (book_confirmation - 50.0) * adjustment_scale),
        )
    confidence = max(50.0, min(100.0, base_confidence + book_adjustment))
    executable = yes_book.best_ask if side == "YES" else no_book.best_ask
    # Edge must come only from the probability model.  The same order book
    # supplies both the executable ask and the bounded confirmation adjustment;
    # Model edge remains a diagnostic comparison against the executable ask;
    # it is not an entry gate and does not affect position size.
    model_probability = base_confidence / 100
    model_edge_pct = (model_probability - executable) * 100
    pricing_edge = max(0, min(100, 50 + model_edge_pct * 2))
    tier, size = tier_for(confidence, cfg["tiers"])
    explanation = (f"historical={hist:.1f} ({history_scope}, n={history_samples}); "
                   f"youtube={youtube_history if youtube_history is not None else 'inactive'} "
                   f"(n={youtube_samples}); "
                   f"market_prior={market_prior:.1f}; book_confirmation={book_confirmation:.1f} "
                   f"(n={book_samples}, adjustment={book_adjustment:+.1f}); "
                   f"timing={timing:.1f}; pricing_edge={pricing_edge:.1f}; "
                   f"base_confidence={base_confidence:.1f}; "
                   f"model_edge={model_edge_pct:.1f}%")
    return Score(yes, confidence, side, tier, size, hist, yes_book.score,
                 momentum, pricing_edge, model_edge_pct,
                 explanation, timing, book_confirmation,
                 book_adjustment, book_samples)
