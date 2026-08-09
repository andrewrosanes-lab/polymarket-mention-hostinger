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
from .transcript_history import GovInfoHistory, market_history_shape
from .subtitle_history import OpenSubtitlesHistory

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.data, self.news = PolymarketData(cfg), NewsScorer(cfg)
        self.store = Store(cfg["paths"]["database"], cfg["paths"]["journal"])
        self.executor = build(cfg)
        self.transcript_history = GovInfoHistory(cfg)
        self.subtitle_history = OpenSubtitlesHistory(cfg)
        self._last_history_refresh = 0.0

    def refresh_history(self) -> None:
        historical = self.cfg.get("historical") or {}
        if not historical.get("enabled", True):
            return
        interval = float(historical.get("refresh_hours", 6)) * 3600
        if self._last_history_refresh and time.monotonic() - self._last_history_refresh < interval:
            return
        try:
            learned = 0
            for item in self.data.resolved_observations():
                learned += self.store.add_observation(
                    item["subject"], item["phrase"], item["context"],
                    item["occurred"], item["condition_id"]
                )
            self._last_history_refresh = time.monotonic()
            log.info("historical Gamma refresh: learned=%d total=%d",
                     learned, self.store.observation_count())
        except Exception:
            log.exception("historical Gamma refresh failed; retaining existing history")

    def risk_ok(self, market, score, book) -> tuple[bool, str]:
        r = self.cfg["risk"]
        if os.path.exists(r["kill_switch_file"]): return False, "kill switch"
        if len(self.store.open_positions()) >= r["max_open_positions"]: return False, "max positions"
        if market.liquidity < r["min_liquidity_usd"]: return False, "low liquidity"
        if market.volume < r["min_volume_usd"]: return False, "low traded volume"
        if book.spread_pct > r["max_spread_pct"]: return False, "wide spread"
        if score.model_edge_pct < r["min_model_edge_pct"]: return False, "model edge below 6%"
        if market.event_start is None:
            if r["require_known_event_start"]: return False, "unknown event start"
        else:
            hours = (market.event_start - datetime.now(timezone.utc)).total_seconds()/3600
            max_hours = float(r["max_hours_before_event"])
            if hours > max_hours:
                return False, f"more than {max_hours:g} hours before event"
        if not r["min_entry_price"] <= book.best_ask <= r["max_entry_price"]:
            return False, "executable entry price gate"
        if r["one_position_per_condition"] and self.store.has_condition(market.condition_id): return False, "already open"
        return True, "ok"

    def tick(self) -> None:
        self.refresh_history()
        markets = self.data.discover()
        try:
            documents, rows, mentions = self.transcript_history.refresh(markets, self.store)
            if documents:
                log.info("official GovInfo transcript refresh: documents=%d rows=%d mentions=%d",
                         documents, rows, mentions)
        except Exception:
            log.exception("official GovInfo transcript refresh failed; retaining existing counts")
        try:
            downloads, rows, mentions = self.subtitle_history.refresh(markets, self.store)
            if downloads:
                log.info("OpenSubtitles historical refresh: episodes=%d rows=%d mentions=%d",
                         downloads, rows, mentions)
        except Exception:
            log.exception("OpenSubtitles historical refresh failed; retaining existing history")
        self.manage_positions({market.condition_id: market for market in markets})
        log.info("discovered %d mention markets", len(markets))
        for market in markets:
            try:
                book = self.data.book(market.yes_token)
                no_book = self.data.book(market.no_token)
                history_period, min_mentions = market_history_shape(market.question)
                hits, total, history_scope = self.store.historical_pattern(
                    market.subject, market.phrase, market.context,
                    history_period, min_mentions)
                hist = historical_score(hits, total)
                news, count = self.news.score(market.subject, market.phrase, market.context)
                momentum = self.data.momentum(market.yes_token, market.yes_price)
                score = combine(market, book, no_book, hist, news, momentum,
                                self.cfg, count, history_scope, total)
                log.info("%s | %s %.1f | %s", market.question, score.side, score.confidence, score.explanation)
                if score.confidence < self.cfg["minimum_confidence"] or not score.tier:
                    continue
                trade_book = book if score.side == "YES" else no_book
                ok, reason = self.risk_ok(market, score, trade_book)
                if not ok:
                    log.info("SKIP %s: %s", market.question, reason); continue
                fill = self.executor.buy(market, score.side, score.size_usd, trade_book)
                token = market.yes_token if score.side == "YES" else market.no_token
                self.store.record_position(market.condition_id, token, score.side,
                                           fill.size_usd, fill.price, fill.order_id,
                                           market.question, market.end_date.isoformat(),
                                           market.tick_size, market.neg_risk)
                log.warning("%s BUY %s tier=%s $%.2f @ %.3f order=%s",
                            self.cfg["mode"].upper(), score.side, score.tier,
                            fill.size_usd, fill.price, fill.order_id)
            except Exception:
                log.exception("market evaluation failed: %s", market.question)

    def manage_positions(self, markets: dict) -> None:
        r = self.cfg["risk"]
        for position in self.store.open_positions():
            market = markets.get(position["condition_id"])
            try:
                book = self.data.book(position["token_id"])
                current = book.best_bid
                if current <= 0:
                    continue
                entry = float(position["entry_price"])
                peak = self.store.update_peak(position["position_id"], current)
                gain = (current / entry - 1) * 100
                end_date = market.end_date if market else None
                if end_date is None and position["end_date"]:
                    end_date = datetime.fromisoformat(str(position["end_date"]).replace("Z", "+00:00"))
                minutes = ((end_date - datetime.now(timezone.utc)).total_seconds() / 60
                           if end_date else float("inf"))
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
                tick_size = market.tick_size if market else str(position["tick_size"] or "0.01")
                neg_risk = market.neg_risk if market else bool(position["neg_risk"])
                fill = self.executor.sell(position["token_id"], shares, current,
                                          tick_size, neg_risk)
                pnl = self.store.close_position(position["position_id"], fill.price,
                                                fill.order_id, fill.shares)
                log.warning("CLOSE %s reason=%s pnl=$%.2f order=%s",
                            market.question if market else position["question"],
                            reason, pnl, fill.order_id)
            except Exception:
                log.exception("position management failed: %s", position["condition_id"])

    def run(self) -> None:
        while True:
            self.tick()
            time.sleep(self.cfg["poll_interval_sec"])
