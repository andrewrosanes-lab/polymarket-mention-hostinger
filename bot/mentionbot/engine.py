from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .execution import DefinitelyNotFilled, build, capped_taker_price
from .market import PolymarketData
from .scoring import combine, historical_score, probability_from_evidence
from .storage import Store
from .transcript_history import GovInfoHistory, market_history_shape
from .subtitle_history import OpenSubtitlesHistory
from .youtube_history import SupadataYouTubeHistory, calibration_segment

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.data = PolymarketData(cfg)
        self.store = Store(cfg["paths"]["database"], cfg["paths"]["journal"])
        self.executor = build(cfg)
        self.transcript_history = GovInfoHistory(cfg)
        self.subtitle_history = OpenSubtitlesHistory(cfg)
        self.youtube_history = SupadataYouTubeHistory(cfg)
        self._last_history_refresh = 0.0
        self._control_path = Path(os.getenv(
            "MENTION_BOT_CONTROL_FILE", "state/control.json"))
        self._runtime_control = self._control_defaults()
        self._book_history: dict[str, deque[float]] = {}

    def _control_defaults(self) -> dict:
        risk = self.cfg["risk"]
        return {
            "paused": False,
            "minimumConfidence": float(self.cfg["minimum_confidence"]),
            "minTimingScore": float(risk["min_timing_score"]),
            "maxHoursBeforeEvent": float(risk["max_hours_before_event"]),
        }

    def _load_runtime_control(self) -> dict:
        defaults = self._control_defaults()
        try:
            if not self._control_path.exists():
                return defaults
            payload = json.loads(self._control_path.read_text())
            # Copy only supported controls. This deliberately discards the
            # retired minModelEdgePct field from existing state volumes.
            control = {key: payload.get(key, default)
                       for key, default in defaults.items()}
            limits = {
                "minimumConfidence": (65, 90),
                "minTimingScore": (0, 90),
                "maxHoursBeforeEvent": (1, 24),
            }
            for key, (lower, upper) in limits.items():
                value = float(control[key])
                if not lower <= value <= upper:
                    raise ValueError(f"{key} outside safe range")
                control[key] = value
            control["paused"] = bool(control["paused"])
            return control
        except Exception:
            # A malformed or partially-written control file must never loosen
            # entry gates. Position management continues while entries pause.
            log.exception("invalid dashboard control; pausing new entries")
            return {**defaults, "paused": True}

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
                self.store.resolve_youtube_shadow_prediction(
                    item["condition_id"], item["occurred"])
            self._last_history_refresh = time.monotonic()
            log.info("historical Gamma refresh: learned=%d total=%d",
                     learned, self.store.observation_count())
        except Exception:
            log.exception("historical Gamma refresh failed; retaining existing history")

    def risk_ok(self, market, score, book, control: dict | None = None,
                check_entry_lock: bool = True,
                required_size_usd: float | None = None) -> tuple[bool, str]:
        r = self.cfg["risk"]
        control = control or getattr(self, "_runtime_control", {
            "minTimingScore": float(r["min_timing_score"]),
            "maxHoursBeforeEvent": float(r["max_hours_before_event"]),
        })
        if os.path.exists(r["kill_switch_file"]): return False, "kill switch"
        if len(self.store.open_positions()) >= r["max_open_positions"]: return False, "max positions"
        if market.liquidity < r["min_liquidity_usd"]: return False, "low liquidity"
        if market.volume < r["min_volume_usd"]: return False, "low traded volume"
        min_timing = float(control["minTimingScore"])
        if score.timing_score < min_timing:
            return False, f"timing score below {min_timing:g}"
        if market.event_start is None:
            if r["require_known_event_start"]: return False, "unknown event start"
        else:
            hours = (market.event_start - datetime.now(timezone.utc)).total_seconds()/3600
            max_hours = float(control["maxHoursBeforeEvent"])
            if hours > max_hours:
                return False, f"more than {max_hours:g} hours before event"
        if not r["min_entry_price"] <= book.best_ask <= r["max_entry_price"]:
            return False, "executable entry price gate"
        required_size = (score.size_usd if required_size_usd is None
                         else float(required_size_usd))
        if book.ask_depth + 1e-9 < required_size:
            return False, "insufficient executable ask depth"
        if check_entry_lock:
            entry_allowed, reason = self.store.entry_allowed(
                market.condition_id, market.subject, market.phrase)
            if not entry_allowed:
                return False, reason
        return True, "ok"

    def _remember_book(self, token_id: str, score: float) -> tuple[float, int]:
        sample_count = int((self.cfg.get("book_confidence") or {}).get(
            "sample_window", 5))
        history = self._book_history.setdefault(
            token_id, deque(maxlen=max(3, sample_count)))
        history.append(float(score))
        return sum(history) / len(history), len(history)

    def tick(self) -> None:
        self._runtime_control = self._load_runtime_control()
        control = self._runtime_control
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
        try:
            videos, rows, mentions = self.youtube_history.refresh(markets, self.store)
            if videos:
                log.info("Supadata YouTube shadow refresh: videos=%d rows=%d mentions=%d",
                         videos, rows, mentions)
        except Exception:
            log.exception("Supadata YouTube shadow refresh failed; retaining existing evidence")
        self.reconcile_positions()
        log.info("discovered %d mention markets", len(markets))
        signals = []
        for market in markets:
            try:
                book = self.data.book(market.yes_token)
                no_book = self.data.book(market.no_token)
                yes_confirmation = self._remember_book(market.yes_token, book.score)
                no_confirmation = self._remember_book(market.no_token, no_book.score)
                history_period, min_mentions = market_history_shape(market.question)
                history_context = (
                    f"tv:{market.episode_target.series.lower()}"
                    if market.episode_target else market.context
                )
                hits, total, history_scope = self.store.historical_pattern(
                    market.subject, market.phrase, history_context,
                    history_period, min_mentions,
                    allow_broad_fallback=False)
                hist = historical_score(hits, total)
                if "cross-context" in history_scope:
                    # Context-mismatched evidence is useful, but less
                    # predictive. Pull it halfway back toward neutral before
                    # it enters the probability model.
                    hist = 50.0 + (hist - 50.0) * 0.5
                youtube_hits, youtube_total, youtube_scope = (
                    self.store.shadow_transcript_pattern(
                        market.subject, market.phrase, market.context,
                        min_mentions=min_mentions))
                youtube_shadow = historical_score(youtube_hits, youtube_total)
                momentum = self.data.momentum(market.yes_token, market.yes_price)
                midpoint = ((book.best_bid + book.best_ask) / 2
                            if book.best_ask >= book.best_bid else market.yes_price)
                market_prior = max(0, min(100, midpoint * 100))
                youtube_cfg = self.cfg.get("youtube_history") or {}
                option_weights = youtube_cfg.get("option_c_weights") or {
                    "historical_context": 0.29,
                    "youtube_history": 0.10,
                    "market_prior": 0.10,
                }
                segment = calibration_segment(market)
                minimum_transcripts = int(youtube_cfg.get(
                    "minimum_comparable_transcripts", 5))
                minimum_resolved = int(youtube_cfg.get(
                    "minimum_resolved_predictions", 30))
                minimum_improvement = float(youtube_cfg.get(
                    "minimum_brier_improvement", 0.005))
                baseline_probability = probability_from_evidence(
                    hist, total, market_prior, self.cfg["probability_weights"])
                option_c_probability = probability_from_evidence(
                    hist, total, market_prior, option_weights,
                    youtube_shadow, youtube_total)
                if youtube_total >= minimum_transcripts:
                    self.store.record_youtube_shadow_prediction(
                        market.condition_id, segment, baseline_probability,
                        option_c_probability, youtube_total)
                calibration = self.store.youtube_calibration(
                    segment, minimum_resolved, minimum_improvement)
                youtube_active = bool(
                    youtube_cfg.get("option_c_armed", True)
                    and youtube_total >= minimum_transcripts
                    and calibration["passed"])
                active_youtube = youtube_shadow if youtube_active else None
                active_youtube_samples = youtube_total if youtube_active else 0
                active_weights = option_weights if youtube_active else None
                provisional = combine(
                    market, book, no_book, hist, market_prior, momentum,
                    self.cfg, history_scope, total,
                    youtube_history=active_youtube,
                    youtube_samples=active_youtube_samples,
                    probability_weights_override=active_weights)
                confirmation, sample_count = (
                    yes_confirmation if provisional.side == "YES" else no_confirmation)
                score = combine(
                    market, book, no_book, hist, market_prior, momentum,
                    self.cfg, history_scope, total,
                    confirmation, sample_count,
                    youtube_history=active_youtube,
                    youtube_samples=active_youtube_samples,
                    probability_weights_override=active_weights)
                log.info("%s | %s %.1f | %s", market.question, score.side, score.confidence, score.explanation)
                trade_book = book if score.side == "YES" else no_book
                minimum_confidence = float(control["minimumConfidence"])
                if control["paused"]:
                    ok, reason = False, "new entries paused from dashboard"
                elif score.confidence < minimum_confidence or not score.tier:
                    ok, reason = False, f"confidence below {minimum_confidence:g}%"
                else:
                    ok, reason = self.risk_ok(market, score, trade_book, control)
                signals.append({
                    "question": market.question,
                    "side": score.side,
                    "confidence": round(score.confidence, 1),
                    "tier": score.tier,
                    "historyScore": round(hist, 1),
                    "historyScope": history_scope,
                    "historySamples": total,
                    "youtubeShadowScore": round(youtube_shadow, 1),
                    "youtubeShadowHits": youtube_hits,
                    "youtubeShadowSamples": youtube_total,
                    "youtubeShadowScope": youtube_scope,
                    "youtubeShadowLiveWeight": 10 if youtube_active else 0,
                    "youtubeOptionCArmed": bool(youtube_cfg.get("option_c_armed", True)),
                    "youtubeOptionCActive": youtube_active,
                    "youtubeCalibration": calibration,
                    "modelEdge": round(score.model_edge_pct, 1),
                    "marketPrior": round(market_prior, 1),
                    "timingScore": round(score.timing_score, 1),
                    "liquidity": round(market.liquidity, 2),
                    "volume": round(market.volume, 2),
                    "bookSpread": round(trade_book.spread_pct, 2),
                    "entryAsk": round(trade_book.best_ask, 4),
                    "bookConfirmation": round(score.book_confirmation, 1),
                    "bookAdjustment": round(score.book_adjustment, 1),
                    "bookSamples": score.book_samples,
                    "strength": ("STRONG NO" if score.side == "NO" and
                                 score.confidence >= 70 else
                                 "STRONG YES" if score.side == "YES" and
                                 score.confidence >= 70 else "WATCH"),
                    "route": "DIRECTIONAL" if ok else "NONE",
                    "qualified": ok,
                    "gate": reason,
                })
                if not ok:
                    log.info("SKIP %s: %s", market.question, reason); continue
                token = market.yes_token if score.side == "YES" else market.no_token
                self.store.reserve_order(
                    market.condition_id, token, score.side, score.size_usd,
                    market.question, market.end_date.isoformat(),
                    market.tick_size, market.neg_risk, market.subject,
                    market.phrase)

                def refresh_for_taker():
                    """Rebuild the complete signal from a fresh executable book."""
                    fresh_yes_book = self.data.book(market.yes_token)
                    fresh_no_book = self.data.book(market.no_token)
                    fresh_momentum = self.data.momentum(
                        market.yes_token, market.yes_price)
                    fresh_midpoint = (
                        (fresh_yes_book.best_bid + fresh_yes_book.best_ask) / 2
                        if fresh_yes_book.best_ask >= fresh_yes_book.best_bid
                        else market.yes_price)
                    fresh_prior = max(0, min(100, fresh_midpoint * 100))
                    fresh_provisional = combine(
                        market, fresh_yes_book, fresh_no_book, hist,
                        fresh_prior, fresh_momentum, self.cfg, history_scope,
                        total, youtube_history=active_youtube,
                        youtube_samples=active_youtube_samples,
                        probability_weights_override=active_weights)
                    fresh_selected_book = (
                        fresh_yes_book if fresh_provisional.side == "YES"
                        else fresh_no_book)
                    fresh_confirmation, fresh_samples = self._remember_book(
                        market.yes_token if fresh_provisional.side == "YES"
                        else market.no_token, fresh_selected_book.score)
                    fresh_score = combine(
                        market, fresh_yes_book, fresh_no_book, hist,
                        fresh_prior, fresh_momentum, self.cfg, history_scope,
                        total, fresh_confirmation, fresh_samples,
                        youtube_history=active_youtube,
                        youtube_samples=active_youtube_samples,
                        probability_weights_override=active_weights)
                    if fresh_score.side != score.side:
                        raise DefinitelyNotFilled(
                            "taker fallback cancelled: model direction changed")
                    if (fresh_score.confidence < minimum_confidence
                            or not fresh_score.tier
                            or fresh_score.size_usd < score.size_usd):
                        raise DefinitelyNotFilled(
                            "taker fallback cancelled: refreshed confidence tier weakened")
                    allowed, fresh_reason = self.risk_ok(
                        market, fresh_score, fresh_selected_book, control,
                        check_entry_lock=False,
                        required_size_usd=score.size_usd)
                    if not allowed:
                        raise DefinitelyNotFilled(
                            f"taker fallback cancelled: {fresh_reason}")
                    maximum_taker_price = capped_taker_price(
                        fresh_selected_book,
                        float(self.cfg["risk"]["max_entry_price"]),
                        market.tick_size, self.cfg["execution"])
                    exact_depth = self.data.executable_ask_depth(
                        market.yes_token if fresh_score.side == "YES"
                        else market.no_token, maximum_taker_price)
                    if exact_depth + 1e-9 < score.size_usd:
                        raise DefinitelyNotFilled(
                            "taker fallback cancelled: insufficient depth at capped price")
                    log.info(
                        "TAKER RECHECK %s: side=%s confidence=%.1f ask=%.3f cap=%.3f depth=$%.2f",
                        market.question, fresh_score.side,
                        fresh_score.confidence,
                        fresh_selected_book.best_ask, maximum_taker_price,
                        exact_depth)
                    return fresh_selected_book

                try:
                    fill = self.executor.buy(
                        market, score.side, score.size_usd, trade_book,
                        on_submitted=lambda order_id, status: self.store.update_pending_order(
                            market.condition_id, order_id, status),
                        refresh_for_taker=refresh_for_taker,
                    )
                except DefinitelyNotFilled as exc:
                    self.store.release_order_reservation(market.condition_id)
                    log.info("NO FILL %s: %s", market.question, exc)
                    continue
                except Exception as exc:
                    # Unknown network/order outcomes remain locked.  A later
                    # wallet reconciliation can adopt a real fill; the next
                    # cycle must never submit a duplicate condition order.
                    self.store.update_pending_order(
                        market.condition_id, status="uncertain", error=str(exc))
                    raise
                self.store.record_position(market.condition_id, token, score.side,
                                           fill.size_usd, fill.price, fill.order_id,
                                           market.question, market.end_date.isoformat(),
                                           market.tick_size, market.neg_risk,
                                           market.subject, market.phrase)
                log.warning("%s BUY %s tier=%s $%.2f @ %.3f order=%s",
                            self.cfg["mode"].upper(), score.side, score.tier,
                            fill.size_usd, fill.price, fill.order_id)
            except Exception:
                log.exception("market evaluation failed: %s", market.question)
        self.write_status(markets, signals)

    def write_status(self, markets: list, signals: list[dict]) -> None:
        try:
            positions = self.store.open_positions()
            path = Path(self.cfg["paths"]["status"])
            path.parent.mkdir(parents=True, exist_ok=True)
            evidence = self.store.evidence_summary()
            execution_cfg = self.cfg.get("execution") or {}
            covered = sum(int(float(item.get("historySamples") or 0) > 0)
                          for item in signals)
            evidence["liveHistoryCoverage"] = {
                "covered": covered,
                "total": len(signals),
                "percent": round(covered / len(signals) * 100, 1)
                if signals else 0.0,
            }
            payload = {
                "connected": True,
                "mode": self.cfg["mode"].upper(),
                "markets": len(markets),
                "positions": len(positions),
                "deployed": round(sum(float(row["size_usd"]) for row in positions), 2),
                "dailyPnl": round(self.store.daily_pnl(), 2),
                "lastCycle": datetime.now(timezone.utc).isoformat(),
                "signals": sorted(signals, key=lambda item: item["confidence"], reverse=True)[:25],
                "evidence": evidence,
                "strategy": {
                    "directional": True,
                    "holdUntilResolution": True,
                    "maxPositionsPerContract": 1,
                    "makerOrderType": "GTD_POST_ONLY",
                    "makerEffectiveLifetimeSec": int(
                        execution_cfg.get("maker_timeout_sec", 45)),
                    "makerOnlyOutsideHours": float(
                        execution_cfg.get("taker_window_hours", 2)),
                    "takerFallback": "FOK",
                    "terminalFillRequired": "CONFIRMED",
                },
                "redeemable": len(self.store.redeemable_positions()),
                "pendingOrders": len(self.store.pending_orders()),
                "control": getattr(self, "_runtime_control", self._control_defaults()),
            }
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, separators=(",", ":")))
            os.replace(temporary, path)
        except Exception:
            log.exception("failed to write dashboard status")

    def reconcile_positions(self) -> None:
        """Hold every trade to resolution and reconcile it from Polymarket."""
        positions = self.store.open_positions()
        pending = self.store.pending_orders()
        if not positions and not pending:
            return
        address = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
        if not address:
            log.error("cannot reconcile positions: missing funder address")
            return
        try:
            current = self.data.portfolio_positions(address, closed=False)
            closed = self.data.portfolio_positions(address, closed=True)
        except Exception:
            log.exception("position reconciliation API failed; no local state changed")
            return
        active_by_token = {str(item.get("asset") or item.get("tokenId") or ""): item
                           for item in current}
        closed_by_token = {str(item.get("asset") or item.get("tokenId") or ""): item
                           for item in closed}
        for order in pending:
            active = active_by_token.get(str(order["token_id"]))
            if not active:
                continue
            try:
                shares = float(active.get("size") or 0)
                price = float(active.get("avgPrice") or 0)
                if shares <= 0 or not 0 < price < 1:
                    continue
                size_usd = shares * price
                self.store.record_position(
                    order["condition_id"], order["token_id"], order["side"],
                    size_usd, price, order["order_id"], order["question"],
                    order["end_date"], order["tick_size"],
                    bool(order["neg_risk"]), order["subject_key"],
                    order["phrase_key"])
                log.warning(
                    "RECONCILED PENDING FILL %s $%.2f @ %.3f order=%s",
                    order["question"], size_usd, price, order["order_id"])
            except Exception:
                log.exception("pending order reconciliation failed: %s",
                              order["condition_id"])
        for position in positions:
            token_id = str(position["token_id"])
            active = active_by_token.get(token_id)
            finished = closed_by_token.get(token_id)
            try:
                redeemable = str((active or {}).get("redeemable", "")).lower() in {
                    "1", "true", "yes"
                }
                if active and redeemable:
                    payout = float(active.get("curPrice") or active.get("currentPrice") or 0)
                    pnl = self.store.settle_position(
                        position["position_id"], payout, "redeemable",
                        str(active.get("slug") or ""))
                    log.warning("RESOLVED %s payout=%.2f pnl=$%.2f; redemption required",
                                position["question"], payout, pnl)
                elif finished:
                    entry = float(position["entry_price"])
                    shares = float(position["size_usd"]) / entry
                    realized = float(finished.get("realizedPnl") or
                                     finished.get("cashPnl") or 0)
                    payout = (realized + float(position["size_usd"])) / shares
                    pnl = self.store.settle_position(
                        position["position_id"], payout, "closed-on-polymarket",
                        str(finished.get("slug") or ""))
                    log.warning("RECONCILED %s payout=%.2f pnl=$%.2f",
                                position["question"], payout, pnl)
            except Exception:
                log.exception("position reconciliation failed: %s",
                              position["condition_id"])

    def run(self) -> None:
        while True:
            self.tick()
            time.sleep(self.cfg["poll_interval_sec"])
