from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque

from .models import MicrostructureSignal

log = logging.getLogger(__name__)


def _wobi(bids: dict[float, float], asks: dict[float, float]) -> float:
    bid_levels = sorted(bids.items(), reverse=True)[:5]
    ask_levels = sorted(asks.items())[:5]
    weights = (5, 4, 3, 2, 1)
    bid = sum(weights[i] * size for i, (_, size) in enumerate(bid_levels))
    ask = sum(weights[i] * size for i, (_, size) in enumerate(ask_levels))
    return (bid - ask) / (bid + ask) if bid + ask else 0.0


def calculate_signal(samples, trades, minimum_seconds: float = 20,
                     minimum_snapshots: int = 3,
                     minimum_trades: int = 1) -> MicrostructureSignal:
    if not samples:
        return MicrostructureSignal()
    now = samples[-1][0]
    recent = [row for row in samples if now - row[0] <= 30]
    age = recent[-1][0] - recent[0][0] if len(recent) > 1 else 0.0
    latest_wobi = recent[-1][1]
    delta = latest_wobi - recent[0][1]
    relevant_trades = [row for row in trades if now - row[0] <= 30]
    gross = sum(abs(row[1]) for row in relevant_trades)
    flow = sum(row[1] for row in relevant_trades) / gross if gross else 0.0
    persistence = sum(row[1] > .15 for row in recent) / len(recent)
    micro = recent[-1][3]
    wobi_score = (latest_wobi + 1) * 50
    flow_score = (flow + 1) * 50
    delta_score = max(0.0, min(100.0, 50 + delta * 100))
    persistence_score = persistence * 100
    score = (.30 * wobi_score + .25 * flow_score + .20 * delta_score
             + .15 * persistence_score + .10 * micro)
    price_change = recent[-1][2] - recent[0][2]
    absorption = len(relevant_trades) >= minimum_trades and flow > .30 and price_change <= 0
    ready = (len(recent) >= minimum_snapshots and age >= minimum_seconds
             and len(relevant_trades) >= minimum_trades)
    return MicrostructureSignal(
        max(0, min(100, score)), latest_wobi, flow, delta, persistence,
        micro, len(recent), len(relevant_trades), age, absorption, ready)


class MarketMicrostructure:
    """Public Polymarket market-channel collector for live Option C inputs."""

    def __init__(self, cfg: dict):
        self.cfg = cfg.get("microstructure") or {}
        self.url = self.cfg.get("websocket_url",
            "wss://ws-subscriptions-clob.polymarket.com/ws/market")
        self._books = defaultdict(lambda: {"bids": {}, "asks": {}})
        self._samples = defaultdict(lambda: deque(maxlen=240))
        self._trades = defaultdict(lambda: deque(maxlen=500))
        self._wanted: set[str] = set()
        self._subscribed: set[str] = set()
        self._lock = threading.RLock()
        self._socket = None
        if self.cfg.get("enabled", True):
            threading.Thread(target=self._run, daemon=True,
                             name="mention-market-ws").start()

    def watch(self, tokens) -> None:
        with self._lock:
            self._wanted.update(str(token) for token in tokens if token)

    def signal(self, token: str) -> MicrostructureSignal:
        with self._lock:
            return calculate_signal(
                list(self._samples[str(token)]), list(self._trades[str(token)]),
                float(self.cfg.get("minimum_persistence_sec", 20)),
                int(self.cfg.get("minimum_snapshots", 3)),
                int(self.cfg.get("minimum_trades", 1)))

    def _snapshot(self, token: str, timestamp: float) -> None:
        book = self._books[token]
        bids, asks = book["bids"], book["asks"]
        if not bids or not asks:
            return
        bid, ask = max(bids), min(asks)
        bid_size, ask_size = bids[bid], asks[ask]
        mid = (bid + ask) / 2
        microprice = ((ask * bid_size + bid * ask_size) /
                      (bid_size + ask_size))
        half_spread = max((ask - bid) / 2, 1e-9)
        micro_score = max(0, min(100,
            50 + 50 * (microprice - mid) / half_spread))
        self._samples[token].append(
            (timestamp, _wobi(bids, asks), mid, micro_score))

    def _message(self, payload) -> None:
        for event in payload if isinstance(payload, list) else [payload]:
            if not isinstance(event, dict):
                continue
            kind = event.get("event_type")
            token = str(event.get("asset_id") or "")
            raw_timestamp = float(event.get("timestamp") or time.time())
            timestamp = (raw_timestamp / 1000 if raw_timestamp > 100_000_000_000
                         else raw_timestamp)
            with self._lock:
                if kind == "book" and token:
                    self._books[token] = {
                        "bids": {float(x["price"]): float(x["size"])
                                 for x in event.get("bids") or []},
                        "asks": {float(x["price"]): float(x["size"])
                                 for x in event.get("asks") or []},
                    }
                    self._snapshot(token, timestamp)
                elif kind == "price_change":
                    for change in event.get("price_changes") or []:
                        asset = str(change.get("asset_id") or "")
                        side = "bids" if str(change.get("side")).upper() == "BUY" else "asks"
                        price, size = float(change["price"]), float(change["size"])
                        if size:
                            self._books[asset][side][price] = size
                        else:
                            self._books[asset][side].pop(price, None)
                        self._snapshot(asset, timestamp)
                elif kind == "last_trade_price" and token:
                    size = float(event.get("size") or 0)
                    signed = size if str(event.get("side")).upper() == "BUY" else -size
                    self._trades[token].append((timestamp, signed,
                                                float(event.get("price") or 0)))

    def _run(self) -> None:
        import websocket
        while True:
            try:
                self._socket = websocket.create_connection(self.url, timeout=5)
                self._subscribed.clear()
                while True:
                    with self._lock:
                        new = sorted(self._wanted - self._subscribed)
                    if new:
                        message = ({"assets_ids": new, "type": "market",
                                    "custom_feature_enabled": True}
                                   if not self._subscribed else
                                   {"assets_ids": new, "operation": "subscribe"})
                        self._socket.send(json.dumps(message))
                        self._subscribed.update(new)
                    try:
                        raw = self._socket.recv()
                    except websocket.WebSocketTimeoutException:
                        self._socket.send("PING")
                        continue
                    if raw and raw != "PONG":
                        self._message(json.loads(raw))
            except Exception:
                log.exception("market microstructure WebSocket disconnected")
                time.sleep(5)
