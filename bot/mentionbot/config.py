from __future__ import annotations

import os
from pathlib import Path

import yaml


def load(path: str = "config.yaml") -> dict:
    with open(path) as fh:
        cfg = yaml.safe_load(fh)
    _validate(cfg)
    for key in ("database", "journal", "log", "status"):
        Path(cfg["paths"][key]).parent.mkdir(parents=True, exist_ok=True)
    return cfg


def _validate(cfg: dict) -> None:
    if cfg.get("mode") not in {"paper", "live"}:
        raise ValueError("mode must be paper or live")
    for group in ("probability_weights", "timing_weights"):
        weights = cfg.get(group) or {}
        if not weights or any(float(value) <= 0 for value in weights.values()):
            raise ValueError(f"{group} must contain positive weights")
    if float(cfg["minimum_confidence"]) != 65:
        raise ValueError("Option C minimum_confidence must remain 65")
    tiers = sorted(cfg["tiers"], key=lambda x: x["min_confidence"])
    for tier in tiers:
        if tier["min_confidence"] >= tier["max_confidence"]:
            raise ValueError(f"invalid tier {tier['name']}")
    if float(tiers[0]["min_confidence"]) != 65:
        raise ValueError("Option C Tier C must begin at 65 confidence")
    if float(tiers[-1]["max_confidence"]) != 93:
        raise ValueError("Option C confidence must stop at 93")
    risk = cfg.get("risk") or {}
    if int(risk.get("max_positions_per_condition", 1)) != 1:
        raise ValueError("max_positions_per_condition must remain 1")
    if float(risk.get("max_hours_before_event", 0)) > 24:
        raise ValueError("max_hours_before_event cannot exceed 24")
    execution = cfg.get("execution") or {}
    if str(execution.get("taker_fallback_order_type", "")).upper() != "FOK":
        raise ValueError("mention taker fallback must remain FOK")
    taker_window = float(execution.get("taker_window_hours", 0))
    if not 0 < taker_window <= float(risk.get("max_hours_before_event", 24)):
        raise ValueError("taker_window_hours must be inside the event-entry window")
    maker_timeout = float(execution.get("maker_timeout_sec", 0))
    if not 30 <= maker_timeout <= 60:
        raise ValueError("maker_timeout_sec must remain between 30 and 60 seconds")
    profit_lock = execution.get("profit_lock") or {}
    if not profit_lock.get("enabled") or not profit_lock.get("maker_only"):
        raise ValueError("staged profit protection must remain maker-only")
    if float(profit_lock.get("max_entry_price_exclusive", 0)) != .45:
        raise ValueError("profit lock must apply only below a 0.45 entry price")
    stages = [
        (float(item.get("trigger_gain_pct", -1)),
         float(item.get("lock_gain_pct", -1)))
        for item in profit_lock.get("stages") or []
    ]
    if stages != [(50.0, 0.0), (100.0, 50.0), (200.0, 100.0)]:
        raise ValueError("profit-lock stages must remain 50/0, 100/50, 200/100")
    if any(lock < 0 or lock >= trigger for trigger, lock in stages):
        raise ValueError("profit locks cannot create a loss exit")
    exit_timeout = float(profit_lock.get("maker_timeout_sec", 0))
    if not 30 <= exit_timeout <= 60:
        raise ValueError("profit-lock maker timeout must be 30 to 60 seconds")
    option_c = cfg.get("option_c_confidence_weights") or {}
    required_option_c = {"historical_mentions", "event_context", "market_prior",
                         "microstructure", "momentum"}
    if set(option_c) != required_option_c:
        raise ValueError("Option C confidence weights are incomplete")
    if abs(sum(float(value) for value in option_c.values()) - 1.0) > 1e-9:
        raise ValueError("Option C confidence weights must sum to 1")
    if float(risk.get("min_model_mispricing_pct", 0)) != 3:
        raise ValueError("Option C requires exactly three points of model mispricing")
    if float(risk.get("min_entry_price", 0)) != .19 or float(
            risk.get("max_entry_price", 0)) != .93:
        raise ValueError("Option C entry prices must remain between 0.19 and 0.93")
    microstructure = cfg.get("microstructure") or {}
    if not microstructure.get("enabled", False):
        raise ValueError("Option C requires live microstructure")
    if float(microstructure.get("minimum_persistence_sec", 0)) < 20:
        raise ValueError("Option C requires at least 20 seconds of persistence")
    if int(microstructure.get("minimum_snapshots", 0)) < 3:
        raise ValueError("Option C requires at least three book snapshots")
    if int(microstructure.get("minimum_trades", 0)) < 1:
        raise ValueError("Option C requires executed-flow evidence")
    youtube = cfg.get("youtube_history") or {}
    if youtube.get("option_c_armed"):
        if not youtube.get("shadow_only_until_calibrated", False):
            raise ValueError("Option C must remain shadow-only until calibrated")
        if int(youtube.get("minimum_comparable_transcripts", 0)) < 5:
            raise ValueError("Option C requires at least five comparable transcripts")
        if int(youtube.get("minimum_resolved_predictions", 0)) < 30:
            raise ValueError("Option C requires at least 30 resolved predictions")
        weights = youtube.get("option_c_weights") or {}
        expected = {"historical_context", "youtube_history", "market_prior"}
        if set(weights) != expected or any(float(value) <= 0 for value in weights.values()):
            raise ValueError("Option C weights are incomplete or invalid")
        if abs(sum(float(value) for value in weights.values()) - 0.49) > 1e-9:
            raise ValueError("Option C probability weights must total 0.49")
    if cfg["mode"] == "live" and cfg.get("allow_live_trading"):
        required = ["POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS"]
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ValueError(f"live mode missing environment variables: {missing}")
