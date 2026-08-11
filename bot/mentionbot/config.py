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
    if float(cfg["minimum_confidence"]) < 50:
        raise ValueError("minimum_confidence must be at least 50")
    tiers = sorted(cfg["tiers"], key=lambda x: x["min_confidence"])
    for tier in tiers:
        if tier["min_confidence"] >= tier["max_confidence"]:
            raise ValueError(f"invalid tier {tier['name']}")
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
