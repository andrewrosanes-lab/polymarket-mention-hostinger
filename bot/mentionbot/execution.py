from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .models import BookSignal, Market


class DefinitelyNotFilled(RuntimeError):
    """The executor proved that no shares were bought."""


@dataclass(frozen=True)
class Fill:
    order_id: str | None
    price: float
    size_usd: float
    shares: float


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
            model_probability: float | None = None,
            minimum_edge_pct: float = 0.0, on_submitted=None) -> Fill:
        price = maker_price(book, market.tick_size,
                            self.cfg["execution"]["price_buffer_ticks"])
        return Fill("paper-maker", price, usd, usd / price)

    def sell(self, token_id: str, shares: float, price: float, tick_size: str,
             neg_risk: bool) -> Fill:
        slip = self.cfg["execution"]["paper_slippage_bps"] / 10_000
        fill_price = max(0.01, price * (1 - slip))
        return Fill("paper-close", fill_price, shares * fill_price, shares)


class LiveExecutor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        if cfg["mode"] != "live" or not cfg.get("allow_live_trading"):
            raise RuntimeError("live execution requires mode=live and allow_live_trading=true")
        try:
            from py_clob_client_v2 import (ApiCreds, ClobClient, MarketOrderArgs,
                OrderArgs, OrderPayload, OrderType, PartialCreateOrderOptions,
                Side, SignatureTypeV2)
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
        self.OrderType, self.Side = OrderType, Side

    def buy(self, market: Market, side: str, usd: float, book: BookSignal,
            model_probability: float | None = None,
            minimum_edge_pct: float = 0.0, on_submitted=None) -> Fill:
        token = market.yes_token if side == "YES" else market.no_token
        price = maker_price(book, market.tick_size,
                            self.cfg["execution"]["price_buffer_ticks"])
        shares = usd / price
        result = self.client.create_and_post_order(
            order_args=self.OrderArgs(token_id=token, price=price, size=shares, side=self.Side.BUY),
            options=self.Options(tick_size=market.tick_size, neg_risk=market.neg_risk),
            order_type=self.OrderType.GTC,
            post_only=True,
        )
        order_id = result.get("orderID") if isinstance(result, dict) else getattr(result, "order_id", None)
        status = result.get("status", "") if isinstance(result, dict) else getattr(result, "status", "")
        if order_id and on_submitted:
            on_submitted(str(order_id), str(status or "submitted"))
        if status.lower() == "matched":
            return _response_fill(result, "BUY", price)
        if not order_id:
            raise DefinitelyNotFilled(f"maker order rejected: {result}")

        deadline = time.monotonic() + self.cfg["execution"]["maker_timeout_sec"]
        while time.monotonic() < deadline:
            time.sleep(self.cfg["execution"]["maker_poll_sec"])
            order = self.client.get_order(order_id)
            matched = float(order.get("size_matched") or 0)
            if matched >= shares * 0.999:
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
            return Fill(order_id, price, matched * price, matched)

        slippage_bps = taker_slippage_bps(book, self.cfg["execution"])
        max_price = min(0.99, book.best_ask * (1 + slippage_bps / 10_000))
        if (model_probability is not None and
                (float(model_probability) - max_price) * 100 < minimum_edge_pct):
            raise DefinitelyNotFilled(
                "taker fallback cancelled: slippage erased the required model edge")
        taker = self.client.create_and_post_market_order(
            order_args=self.MarketOrderArgs(token_id=token, amount=usd,
                side=self.Side.BUY, price=max_price, order_type=self.OrderType.FAK),
            options=self.Options(tick_size=market.tick_size, neg_risk=market.neg_risk),
            order_type=self.OrderType.FAK,
        )
        taker_id = (taker.get("orderID") or taker.get("order_id")) \
            if isinstance(taker, dict) else getattr(taker, "order_id", None)
        taker_status = taker.get("status", "") if isinstance(taker, dict) \
            else getattr(taker, "status", "")
        if taker_id and on_submitted:
            on_submitted(str(taker_id), str(taker_status or "submitted"))
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
