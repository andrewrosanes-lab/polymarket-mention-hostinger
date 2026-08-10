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
            hist: float, market_prior: float, momentum: float, cfg: dict,
            history_scope: str = "exact",
            history_samples: int = 0, book_confirmation: float = 50.0,
            book_samples: int = 0) -> Score:
    """Estimate direction, then apply a small persistent-book confirmation.

    News and complement-price arbitrage are deliberately excluded. Order-book
    pressure cannot choose the side or move confidence by more than the
    configured cap, and it has no effect until enough consecutive samples
    exist.
    """
    probability_weights = cfg["probability_weights"]
    # A neutral 50 means evidence was unavailable, not that an observation
    # supports a 50/50 outcome. Omit unavailable sources and renormalize the
    # remaining evidence so missing news/history cannot dilute real evidence.
    evidence = [(market_prior, probability_weights["market_prior"])]
    if history_samples > 0:
        evidence.append((hist, probability_weights["historical_context"]))
    probability_weight = sum(weight for _, weight in evidence)
    yes = sum(value * weight for value, weight in evidence) / probability_weight
    yes = max(0, min(100, yes))
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
    adjusted_probability = confidence / 100
    executable = yes_book.best_ask if side == "YES" else no_book.best_ask
    model_probability = adjusted_probability
    model_edge_pct = (model_probability - executable) * 100
    pricing_edge = max(0, min(100, 50 + model_edge_pct * 2))
    tier, size = tier_for(confidence, cfg["tiers"])
    explanation = (f"historical={hist:.1f} ({history_scope}, n={history_samples}); "
                   f"market_prior={market_prior:.1f}; book_confirmation={book_confirmation:.1f} "
                   f"(n={book_samples}, adjustment={book_adjustment:+.1f}); "
                   f"timing={timing:.1f}; pricing_edge={pricing_edge:.1f}; "
                   f"model_edge={model_edge_pct:.1f}%")
    return Score(yes, confidence, side, tier, size, hist, yes_book.score,
                 momentum, pricing_edge, model_edge_pct,
                 explanation, timing, book_confirmation,
                 book_adjustment, book_samples)
