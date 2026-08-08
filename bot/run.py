#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging

from dotenv import load_dotenv

from mentionbot.config import load
from mentionbot.engine import Engine


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket mention-market trader")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--record", nargs=4, metavar=("SUBJECT", "PHRASE", "CONTEXT", "YES_NO"),
                        help="record a resolved historical mention")
    args = parser.parse_args()
    load_dotenv()
    cfg = load(args.config)
    logging.basicConfig(level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(cfg["paths"]["log"])])
    if args.check:
        print(f"config OK; mode={cfg['mode']}; live={cfg['allow_live_trading']}"); return
    engine = Engine(cfg)
    if args.record:
        subject, phrase, context, result = args.record
        if result.lower() not in {"yes", "no"}: raise SystemExit("result must be yes or no")
        engine.store.add_observation(subject, phrase, context, result.lower() == "yes")
        print("observation recorded"); return
    engine.tick() if args.once else engine.run()


if __name__ == "__main__":
    main()
