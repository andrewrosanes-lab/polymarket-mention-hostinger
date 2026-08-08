from __future__ import annotations

import os
from pathlib import Path

import yaml


def load(path: str = "config.yaml") -> dict:
    with open(path) as fh:
        cfg = yaml.safe_load(fh)
    _validate(cfg)
    for key in ("database", "journal", "log"):
        Path(cfg["paths"][key]).parent.mkdir(parents=True, exist_ok=True)
    return cfg


def _validate(cfg: dict) -> None:
    if cfg.get("mode") not in {"paper", "live"}:
        raise ValueError("mode must be paper or live")
    weights = cfg["weights"]
    if abs(sum(float(v) for v in weights.values()) - 1.0) > 1e-9:
        raise ValueError("weights must sum to 1.0")
    if float(cfg["minimum_confidence"]) < 50:
        raise ValueError("minimum_confidence must be at least 50")
    tiers = sorted(cfg["tiers"], key=lambda x: x["min_confidence"])
    for tier in tiers:
        if tier["min_confidence"] >= tier["max_confidence"]:
            raise ValueError(f"invalid tier {tier['name']}")
    if cfg["mode"] == "live" and cfg.get("allow_live_trading"):
        required = ["POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS"]
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ValueError(f"live mode missing environment variables: {missing}")
