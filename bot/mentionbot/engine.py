from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from .execution import build
from .market import PolymarketData
from .news import NewsScorer
from .scoring import combine, historical_score
from .storage import Store

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.data, self.news = PolymarketData(cfg), NewsScorer(cfg)
        self.store = Store(cfg["paths"]["database"], cfg["paths"]["journal"])
        self.executor = build(cfg)

    def risk_ok(self, market, score, book) -> tuple[bool, str]:
        r = self.cfg["risk"]
        if os.path.exists(r["kill_switch_file"]): return False, "kill switch"
        if len(self.store.open_positions()) >= r["max_open_positions"]: return False, "max positions"
        deployed = sum(float(x["size_usd"]) for x in self.store.open_positions())
        if deployed + score.size_usd > r["max_deployed_usd"]: return False, "max deployed"
        if self.store.daily_loss() <= -r["daily_loss_limit_usd"]: return False, "daily loss limit"
        if market.liquidity < r["min_liquidity_usd"]: return False, "low liquidity"
        if market.volume < r["min_volume_usd"]: return False, "low traded volume"
        if book.spread_pct > r["max_spread_pct"]: return False, "wide spread"
        if score.model_edge_pct < r["min_model_edge_pct"]: return False, "model edge below 6%"
        if market.event_start is None:
            if r["require_known_event_start"]: return False, "unknown event start"
        else:
            hours = (market.event_start - datetime.now(timezone.utc)).total_seconds()/3600
            if hours > r["max_hours_before_event"]: return False, "more than 2 hours before event"
        price = market.yes_price if score.side == "YES" else market.no_price
        if not r["min_entry_price"] <= price <= r["max_entry_price"]: return False, "entry price gate"
        if r["one_position_per_condition"] and self.store.has_condition(market.condition_id): return False, "already open"
        return True, "ok"

    def tick(self) -> None:
        markets = self.data.discover()
        self.manage_positions({market.condition_id: market for market in markets})
        log.info("discovered %d mention markets", len(markets))
        for market in markets:
            try:
                book = self.data.book(market.yes_token)
                no_book = self.data.book(market.no_token)
                hits, total = self.store.historical(market.subject, market.phrase, market.context)
                hist = historical_score(hits, total)
                news, count = self.news.score(market.subject, market.phrase, market.context)
                momentum = self.data.momentum(market.yes_token, market.yes_price)
                score = combine(market, book, no_book, hist, news, momentum, self.cfg, count)
                log.info("%s | %s %.1f | %s", market.question, score.side, score.confidence, score.explanation)
                if score.confidence < self.cfg["minimum_confidence"] or not score.tier:
                    continue
                trade_book = book if score.side == "YES" else no_book
                ok, reason = self.risk_ok(market, score, trade_book)
                if not ok:
                    log.info("SKIP %s: %s", market.question, reason); continue
                price = trade_book.best_ask
                fill = self.executor.buy(market, score.side, score.size_usd, trade_book)
                token = market.yes_token if score.side == "YES" else market.no_token
                self.store.record_position(market.condition_id, token, score.side,
                                           fill.size_usd, fill.price, fill.order_id)
                log.warning("%s BUY %s tier=%s $%.2f @ %.3f order=%s",
                            self.cfg["mode"].upper(), score.side, score.tier,
                            fill.size_usd, fill.price, fill.order_id)
            except Exception:
                log.exception("market evaluation failed: %s", market.question)

    def manage_positions(self, markets: dict) -> None:
        r = self.cfg["risk"]
        for position in self.store.open_positions():
            market = markets.get(position["condition_id"])
            if not market:
                continue
            try:
                book = self.data.book(position["token_id"])
                current = book.best_bid
                if current <= 0:
                    continue
                entry = float(position["entry_price"])
                peak = self.store.update_peak(position["condition_id"], current)
                gain = (current / entry - 1) * 100
                minutes = (market.end_date - datetime.now(timezone.utc)).total_seconds() / 60
                reason = None
                low_band = r["low_price_band_min"] <= entry <= r["low_price_band_max"]
                low_stop = entry * (1 - r["low_price_stop_loss_pct"] / 100)
                trailing_stop = peak * (1 - r["low_price_trailing_stop_pct"] / 100)
                if gain >= r["take_profit_pct"]:
                    reason = "take-profit"
                elif low_band and current <= max(low_stop, trailing_stop):
                    reason = "50% low-price stop/trailing stop"
                elif not low_band and gain <= -r["stop_loss_pct"]:
                    reason = "stop-loss"
                elif minutes <= r["exit_minutes_before_end"]:
                    reason = "pre-resolution exit"
                if not reason:
                    continue
                shares = float(position["size_usd"]) / entry
                fill = self.executor.sell(position["token_id"], shares, current,
                                          market.tick_size, market.neg_risk)
                pnl = self.store.close_position(position["condition_id"], fill.price, fill.order_id)
                log.warning("CLOSE %s reason=%s pnl=$%.2f order=%s",
                            market.question, reason, pnl, fill.order_id)
            except Exception:
                log.exception("position management failed: %s", position["condition_id"])

    def run(self) -> None:
        while True:
            self.tick()
            time.sleep(self.cfg["poll_interval_sec"])
