from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil, floor

from .models import BookSignal, Market


class DefinitelyNotFilled(RuntimeError):
    """The executor proved that no shares were bought."""


@dataclass(frozen=True)
class Fill:
    order_id: str | None
    price: float
    size_usd: float
    shares: float


def taker_window_open(market: Market, execution_cfg: dict,
                      now: datetime | None = None) -> bool:
    """Allow aggressive execution only near or during the known event."""
    if market.event_start is None:
        return False
    now = now or datetime.now(timezone.utc)
    hours = (market.event_start - now).total_seconds() / 3600
    return hours <= float(execution_cfg.get("taker_window_hours", 2))


def capped_taker_price(book: BookSignal, maximum_entry_price: float,
                       tick_size: str, execution_cfg: dict) -> float:
    """Return a tick-valid cap preserving slippage and the entry-price limit."""
    slippage = taker_slippage_bps(book, execution_cfg) / 10_000
    raw_cap = min(
        float(maximum_entry_price),
        book.best_ask * (1 + slippage),
    )
    tick = float(tick_size)
    decimals = len(tick_size.partition(".")[2].rstrip("0"))
    return round(max(0.0, floor((raw_cap + 1e-12) / tick) * tick), decimals)


def profit_lock_floor(entry_price: float, peak_price: float,
                      stages: list[dict]) -> float | None:
    """Return the highest armed no-loss floor, or None before a 50% gain."""
    entry = float(entry_price)
    peak = float(peak_price)
    if not 0 < entry < 1 or peak < entry:
        return None
    peak_gain = (peak / entry - 1) * 100
    armed = [float(stage["lock_gain_pct"]) for stage in stages
             if peak_gain + 1e-9 >= float(stage["trigger_gain_pct"])]
    if not armed:
        return None
    return min(.99, entry * (1 + max(armed) / 100))


def maker_sell_price(book: BookSignal, minimum_price: float,
                     tick_size: str) -> float:
    """Choose a post-only sell price that never falls below the armed floor."""
    tick = float(tick_size)
    raw = max(float(minimum_price), book.best_bid + tick)
    steps = ceil((raw - 1e-12) / tick)
    decimals = len(tick_size.partition(".")[2].rstrip("0"))
    price = round(steps * tick, decimals)
    if price > .99 or price <= book.best_bid:
        raise DefinitelyNotFilled("no valid post-only sell price above the bid")
    return price


def _response_fill(result: dict, side: str, fallback_price: float) -> Fill:
    """Return only a confirmed CLOB fill using the actual exchanged amounts."""
    if not isinstance(result, dict) or str(result.get("status", "")).lower() != "matched":
        raise RuntimeError(f"order did not reach matched status: {result}")
    making = float(result.get("makingAmount") or result.get("making_amount") or 0)
    taking = float(result.get("takingAmount") or result.get("taking_amount") or 0)
    order_id = result.get("orderID") or result.get("order_id")
    if making <= 0 or taking <= 0:
        raise RuntimeError(f"matched order omitted confirmed fill amounts: {result}")
    if side == "BUY":
        spent, shares = making, taking
    else:
        shares, spent = making, taking
    return Fill(order_id, spent / shares if shares else fallback_price, spent, shares)


class PaperExecutor:
    def __init__(self, cfg: dict): self.cfg = cfg

    def buy(self, market: Market, side: str, usd: float, book: BookSignal,
            on_submitted=None, refresh_for_taker=None) -> Fill:
        price = maker_price(book, market.tick_size,
                            self.cfg["execution"]["price_buffer_ticks"])
        return Fill("paper-maker", price, usd, usd / price)

    def sell(self, token_id: str, shares: float, price: float, tick_size: str,
             neg_risk: bool) -> Fill:
        slip = self.cfg["execution"]["paper_slippage_bps"] / 10_000
        fill_price = max(0.01, price * (1 - slip))
        return Fill("paper-close", fill_price, shares * fill_price, shares)

    def sell_maker(self, token_id: str, shares: float, book: BookSignal,
                   minimum_price: float, tick_size: str, neg_risk: bool) -> Fill:
        price = maker_sell_price(book, minimum_price, tick_size)
        return Fill("paper-maker-close", price, shares * price, shares)


class LiveExecutor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        if cfg["mode"] != "live" or not cfg.get("allow_live_trading"):
            raise RuntimeError("live execution requires mode=live and allow_live_trading=true")
        try:
            from py_clob_client_v2 import (ApiCreds, ClobClient, MarketOrderArgs,
                OrderArgs, OrderPayload, OrderType, PartialCreateOrderOptions,
                Side, SignatureTypeV2, TradeParams)
        except ImportError as exc:
            raise RuntimeError("install py-clob-client-v2; legacy py-clob-client cannot trade CLOB V2") from exc
        creds = None
        if os.getenv("CLOB_API_KEY"):
            creds = ApiCreds(api_key=os.environ["CLOB_API_KEY"], api_secret=os.environ["CLOB_SECRET"],
                             api_passphrase=os.environ["CLOB_PASSPHRASE"])
        common = dict(host=cfg["execution"]["host"], chain_id=cfg["execution"]["chain_id"],
            key=os.environ["POLYMARKET_PRIVATE_KEY"], creds=creds,
            signature_type=SignatureTypeV2.POLY_1271 if cfg["execution"]["signature_type"] == 3 else cfg["execution"]["signature_type"],
            funder=os.environ["POLYMARKET_FUNDER_ADDRESS"])
        if creds is None:
            temporary = ClobClient(**common)
            common["creds"] = temporary.create_or_derive_api_key()
        self.client = ClobClient(**common)
        self.OrderArgs, self.MarketOrderArgs = OrderArgs, MarketOrderArgs
        self.OrderPayload, self.Options = OrderPayload, PartialCreateOrderOptions
        self.OrderType, self.Side, self.TradeParams = OrderType, Side, TradeParams

    def _wait_for_confirmation(self, trade_ids: list[str]) -> None:
        """Return only after every associated trade reaches terminal success."""
        ids = [str(item) for item in dict.fromkeys(trade_ids) if item]
        if not ids:
            raise RuntimeError("matched order omitted trade IDs; awaiting wallet reconciliation")
        deadline = time.monotonic() + float(
            self.cfg["execution"].get("trade_confirmation_timeout_sec", 45))
        interval = max(1.0, float(
            self.cfg["execution"].get("trade_confirmation_poll_sec", 2)))
        while time.monotonic() < deadline:
            statuses: dict[str, str] = {}
            try:
                for trade_id in ids:
                    trades = self.client.get_trades(
                        self.TradeParams(id=trade_id), only_first_page=True)
                    for trade in trades:
                        if str(trade.get("id")) == trade_id:
                            statuses[trade_id] = str(
                                trade.get("status") or "").upper()
            except Exception:
                # A transient CLOB read failure must not turn a MATCHED trade
                # into either a false success or a retryable entry. Keep the
                # reservation locked and continue checking until the deadline.
                time.sleep(interval)
                continue
            if any(status == "FAILED" for status in statuses.values()):
                raise DefinitelyNotFilled("matched trade failed permanently")
            if all(statuses.get(trade_id) == "CONFIRMED" for trade_id in ids):
                return
            time.sleep(interval)
        raise RuntimeError("trade finality is still pending; retaining entry lock")

    def _confirm_response(self, result: dict) -> None:
        trade_ids = result.get("tradeIDs") or result.get("trade_ids") or []
        self._wait_for_confirmation(list(trade_ids))

    def buy(self, market: Market, side: str, usd: float, book: BookSignal,
            on_submitted=None, refresh_for_taker=None) -> Fill:
        token = market.yes_token if side == "YES" else market.no_token
        execution_cfg = self.cfg["execution"]
        price = maker_price(book, market.tick_size,
                            execution_cfg["price_buffer_ticks"])
        shares = usd / price
        maker_lifetime = int(execution_cfg.get("maker_timeout_sec", 45))
        # Polymarket requires a 60-second security threshold in addition to
        # the desired effective GTD lifetime.
        expiration = int(time.time()) + 60 + maker_lifetime
        result = self.client.create_and_post_order(
            order_args=self.OrderArgs(token_id=token, price=price, size=shares,
                side=self.Side.BUY, expiration=expiration),
            options=self.Options(tick_size=market.tick_size, neg_risk=market.neg_risk),
            order_type=self.OrderType.GTD,
            post_only=True,
        )
        order_id = result.get("orderID") if isinstance(result, dict) else getattr(result, "order_id", None)
        status = result.get("status", "") if isinstance(result, dict) else getattr(result, "status", "")
        if order_id and on_submitted:
            on_submitted(str(order_id), str(status or "submitted"))
        if status.lower() == "matched":
            self._confirm_response(result)
            return _response_fill(result, "BUY", price)
        if not order_id:
            raise DefinitelyNotFilled(f"maker order rejected: {result}")

        deadline = time.monotonic() + maker_lifetime
        while time.monotonic() < deadline:
            time.sleep(execution_cfg["maker_poll_sec"])
            order = self.client.get_order(order_id)
            matched = float(order.get("size_matched") or 0)
            if matched >= shares * 0.999:
                self._wait_for_confirmation(order.get("associate_trades") or [])
                return Fill(order_id, price, matched * price, matched)

        # Cancel first, then re-read the final matched amount. This closes the
        # race where a maker fill lands between the last poll and cancellation
        # and a second taker order would otherwise oversize the position.
        self.client.cancel_order(self.OrderPayload(orderID=order_id))
        order = self.client.get_order(order_id)
        matched = float(order.get("size_matched") or 0)
        if matched > 0:
            # Do not submit a second leg after a partial maker fill; reconcile
            # only the confirmed maker shares to prevent accidental oversizing.
            self._wait_for_confirmation(order.get("associate_trades") or [])
            return Fill(order_id, price, matched * price, matched)

        if not taker_window_open(market, execution_cfg):
            raise DefinitelyNotFilled(
                "maker expired outside two-hour taker window")
        if refresh_for_taker is None:
            raise DefinitelyNotFilled("fresh taker re-evaluation unavailable")
        refreshed = refresh_for_taker()
        if isinstance(refreshed, tuple):
            book, strategy_cap = refreshed
        else:
            book, strategy_cap = refreshed, float(
                self.cfg["risk"]["max_entry_price"])
        if book.ask_depth + 1e-9 < usd:
            raise DefinitelyNotFilled("taker fallback lacks full displayed ask depth")
        max_price = capped_taker_price(
            book, min(float(self.cfg["risk"]["max_entry_price"]),
                      float(strategy_cap)),
            market.tick_size, execution_cfg)
        if max_price + 1e-9 < book.best_ask:
            raise DefinitelyNotFilled(
                "taker fallback cancelled: ask exceeds the permitted slippage cap")
        taker = self.client.create_and_post_market_order(
            order_args=self.MarketOrderArgs(token_id=token, amount=usd,
                side=self.Side.BUY, price=max_price, order_type=self.OrderType.FOK),
            options=self.Options(tick_size=market.tick_size, neg_risk=market.neg_risk),
            order_type=self.OrderType.FOK,
        )
        taker_id = (taker.get("orderID") or taker.get("order_id")) \
            if isinstance(taker, dict) else getattr(taker, "order_id", None)
        taker_status = taker.get("status", "") if isinstance(taker, dict) \
            else getattr(taker, "status", "")
        if taker_id and on_submitted:
            on_submitted(str(taker_id), str(taker_status or "submitted"))
        if not taker_id and taker_status.lower() != "matched":
            raise DefinitelyNotFilled(f"FOK taker was not filled: {taker}")
        self._confirm_response(taker)
        return _response_fill(taker, "BUY", book.best_ask)

    def sell(self, token_id: str, shares: float, price: float, tick_size: str,
             neg_risk: bool) -> Fill:
        min_price = max(float(tick_size), price *
                        (1 - self.cfg["execution"]["taker_max_slippage_bps"] / 10_000))
        result = self.client.create_and_post_market_order(
            order_args=self.MarketOrderArgs(token_id=token_id, amount=shares,
                side=self.Side.SELL, price=min_price, order_type=self.OrderType.FAK),
            options=self.Options(tick_size=tick_size, neg_risk=neg_risk),
            order_type=self.OrderType.FAK,
        )
        return _response_fill(result, "SELL", price)

    def sell_maker(self, token_id: str, shares: float, book: BookSignal,
                   minimum_price: float, tick_size: str, neg_risk: bool) -> Fill:
        """Attempt a GTD post-only profit exit; never cross or use a taker."""
        profit_cfg = self.cfg["execution"]["profit_lock"]
        price = maker_sell_price(book, minimum_price, tick_size)
        lifetime = int(profit_cfg.get("maker_timeout_sec", 45))
        expiration = int(time.time()) + 60 + lifetime
        result = self.client.create_and_post_order(
            order_args=self.OrderArgs(token_id=token_id, price=price, size=shares,
                side=self.Side.SELL, expiration=expiration),
            options=self.Options(tick_size=tick_size, neg_risk=neg_risk),
            order_type=self.OrderType.GTD,
            post_only=True,
        )
        order_id = result.get("orderID") if isinstance(result, dict) else None
        status = str(result.get("status") or "") if isinstance(result, dict) else ""
        if status.lower() == "matched":
            self._confirm_response(result)
            return _response_fill(result, "SELL", price)
        if not order_id:
            raise DefinitelyNotFilled(f"maker profit exit rejected: {result}")

        deadline = time.monotonic() + lifetime
        while time.monotonic() < deadline:
            time.sleep(self.cfg["execution"]["maker_poll_sec"])
            order = self.client.get_order(order_id)
            matched = float(order.get("size_matched") or 0)
            if matched >= shares * .999:
                self._wait_for_confirmation(order.get("associate_trades") or [])
                return Fill(order_id, price, matched * price, matched)

        self.client.cancel_order(self.OrderPayload(orderID=order_id))
        order = self.client.get_order(order_id)
        matched = float(order.get("size_matched") or 0)
        if matched > 0:
            self._wait_for_confirmation(order.get("associate_trades") or [])
            return Fill(order_id, price, matched * price, matched)
        raise DefinitelyNotFilled("maker profit exit expired unfilled; no taker used")


def build(cfg: dict):
    if cfg["mode"] != "live":
        raise RuntimeError("paper execution is disabled; set mode=live")
    if not cfg.get("allow_live_trading"):
        raise RuntimeError("live execution is locked pending explicit owner approval")
    return LiveExecutor(cfg)


def taker_slippage_bps(book: BookSignal, execution_cfg: dict) -> int:
    """Scale with spread while remaining inside the configured 1%-3% band."""
    minimum = int(execution_cfg.get("taker_min_slippage_bps", 100))
    maximum = int(execution_cfg.get("taker_max_slippage_bps", 300))
    return max(minimum, min(maximum, round(book.spread_pct * 100)))


def maker_price(book: BookSignal, tick_size: str, buffer_ticks: int) -> float:
    tick = float(tick_size)
    proposed = book.best_bid + tick * max(1, int(buffer_ticks))
    if proposed >= book.best_ask:
        proposed = book.best_bid
    steps = round(proposed / tick)
    decimals = len(tick_size.partition(".")[2].rstrip("0"))
    return round(max(tick, min(0.99, steps * tick)), decimals)
