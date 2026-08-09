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
    if (cfg.get("arbitrage") or {}).get("execution_enabled"):
        raise ValueError(
            "live paired arbitrage is safety-locked: Polymarket does not document "
            "batch FOK orders as atomic across both outcome legs"
        )
    if cfg["mode"] == "live" and cfg.get("allow_live_trading"):
        required = ["POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS"]
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ValueError(f"live mode missing environment variables: {missing}")
