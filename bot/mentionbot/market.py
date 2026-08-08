from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import requests

from .models import BookSignal, Market


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        return json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []


def _date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def infer_context(text: str) -> str:
    lower = text.lower()
    groups = {
        "nfl_game": ("nfl", "football", "super bowl", "touchdown", "game"),
        "speech": ("speech", "rally", "remarks", "address"),
        "debate": ("debate",),
        "interview": ("interview", "podcast"),
        "press_conference": ("press conference", "news conference", "briefing"),
    }
    for context, words in groups.items():
        if any(w in lower for w in words):
            return context
    return "other"


def infer_subject_phrase(question: str) -> tuple[str, str]:
    q = question.strip(" ?")
    subject = "unknown"
    for pattern in (r"will\s+(.+?)\s+(?:say|mention|utter|use)",
                    r"(?:say|mention)\s+(.+?)\s+(?:during|in|at)"):
        match = re.search(pattern, q, re.I)
        if match:
            subject = match.group(1).strip()
            break
    quoted = re.findall(r"[\"'“](.+?)[\"'”]", q)
    if quoted:
        # Preserve alternatives from questions such as '"Karoline" or
        # "Leavitt"'. The transcript counter treats a pipe as logical OR.
        phrase = " | ".join(dict.fromkeys(quoted))
    else:
        match = re.search(r"(?:say|mention|utter|use)\s+(.+?)(?:\s+during|\s+in|\s+at|$)", q, re.I)
        phrase = match.group(1).strip() if match else q
    return subject[:100], phrase[:160]


class PolymarketData:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.session = requests.Session()
        self.gamma = "https://gamma-api.polymarket.com"
        self.clob = cfg["execution"]["host"]

    def _validate_tags(self) -> None:
        if not self.cfg["discovery"].get("validate_tags", True):
            return
        for tag in self.cfg["discovery"].get("tag_ids", []):
            response = self.session.get(f"{self.gamma}/tags/{int(tag['id'])}", timeout=15)
            response.raise_for_status()
            actual = response.json()
            actual_label = str(actual.get("label") or "").strip().lower()
            expected_label = str(tag["label"]).strip().lower()
            if actual_label != expected_label:
                raise RuntimeError(
                    f"Gamma tag {tag['id']} changed: expected {tag['label']!r}, "
                    f"received {actual.get('label')!r}; refusing to trade"
                )

    def _collect_event(self, event: dict, found: dict[str, Market], markers: tuple[str, ...]) -> int:
        scanned = 0
        for raw in event.get("markets") or []:
            scanned += 1
            question = raw.get("question", "")
            if not any(marker in question.lower() for marker in markers):
                continue
            item = dict(raw)
            item["_event_title"] = event.get("title", "")
            item["_event_slug"] = event.get("slug", "")
            item["_event_start"] = (event.get("eventStartTime") or event.get("startTime")
                                    or event.get("eventDate"))
            item["_event_description"] = event.get("description", "")
            market = self._parse(item)
            if market:
                found[market.condition_id] = market
        return scanned

    def _discover_by_tags(self, found: dict[str, Market], markers: tuple[str, ...]) -> None:
        for tag in self.cfg["discovery"].get("tag_ids", []):
            cursor = None
            scanned = 0
            while scanned < self.cfg["max_markets_per_scan"]:
                params = {
                    "tag_id": int(tag["id"]), "closed": "false",
                    "limit": min(100, self.cfg["max_markets_per_scan"] - scanned),
                    "related_tags": "false",
                }
                if cursor:
                    params["after_cursor"] = cursor
                response = self.session.get(f"{self.gamma}/events/keyset", params=params, timeout=20)
                response.raise_for_status()
                payload = response.json()
                events = payload.get("events") or []
                for event in events:
                    scanned += self._collect_event(event, found, markers)
                cursor = payload.get("next_cursor")
                if not events or not cursor:
                    break

    def discover(self) -> list[Market]:
        found: dict[str, Market] = {}
        markers = tuple(x.lower() for x in self.cfg["discovery"]["mention_markers"])
        self._validate_tags()
        self._discover_by_tags(found, markers)
        if not self.cfg["discovery"].get("use_text_search_fallback", True):
            return sorted(found.values(), key=lambda m: (m.end_date, -m.liquidity))[: int(self.cfg.get("max_candidates_per_cycle", 25))]
        for query in self.cfg["discovery"]["queries"]:
            scanned = 0
            page = 1
            while scanned < self.cfg["max_markets_per_scan"]:
                params = {"q": query, "events_status": "active", "limit_per_type": 50,
                          "page": page, "search_profiles": "false"}
                response = self.session.get(f"{self.gamma}/public-search", params=params, timeout=20)
                response.raise_for_status()
                payload = response.json()
                events = payload.get("events") or []
                for event in events:
                    scanned += self._collect_event(event, found, markers)
                pagination = payload.get("pagination") or {}
                if not events or not pagination.get("hasMore"):
                    break
                page += 1
        ranked = sorted(found.values(), key=lambda m: (m.end_date, -m.liquidity))
        return ranked[: int(self.cfg.get("max_candidates_per_cycle", 25))]

    def resolved_observations(self) -> list[dict]:
        """Fetch deduplicated, definitively resolved mention outcomes from Gamma."""
        historical = self.cfg.get("historical") or {}
        if not historical.get("enabled", True):
            return []
        threshold = float(historical.get("resolution_confidence", 0.99))
        max_events = int(historical.get("max_events_per_tag", 75))
        found: dict[str, dict] = {}
        for tag in self.cfg["discovery"].get("tag_ids", []):
            cursor = None
            event_count = 0
            while event_count < max_events:
                params = {"tag_id": int(tag["id"]), "closed": "true",
                          "limit": min(100, max_events - event_count),
                          "related_tags": "false"}
                if cursor:
                    params["after_cursor"] = cursor
                response = self.session.get(f"{self.gamma}/events/keyset", params=params, timeout=30)
                response.raise_for_status()
                payload = response.json()
                events = payload.get("events") or []
                for event in events:
                    event_count += 1
                    text = f"{event.get('title', '')} {event.get('description', '')}"
                    for raw in event.get("markets") or []:
                        condition_id = str(raw.get("conditionId") or "")
                        outcomes = [str(x).lower() for x in _json_list(raw.get("outcomes"))]
                        prices = _json_list(raw.get("outcomePrices"))
                        if not condition_id or len(outcomes) != 2 or len(prices) != 2:
                            continue
                        if "yes" not in outcomes or "no" not in outcomes:
                            continue
                        try:
                            yes_price = float(prices[outcomes.index("yes")])
                            no_price = float(prices[outcomes.index("no")])
                        except (TypeError, ValueError):
                            continue
                        if yes_price >= threshold and no_price <= 1 - threshold:
                            occurred = True
                        elif no_price >= threshold and yes_price <= 1 - threshold:
                            occurred = False
                        else:
                            continue
                        question = str(raw.get("question") or "")
                        subject, phrase = infer_subject_phrase(question)
                        found[condition_id] = {"condition_id": condition_id,
                            "subject": subject, "phrase": phrase,
                            "context": infer_context(f"{question} {raw.get('description', '')} {text}"),
                            "occurred": occurred}
                cursor = payload.get("next_cursor")
                if not events or not cursor:
                    break
        return list(found.values())

    def _parse(self, raw: dict) -> Market | None:
        outcomes, tokens, prices = map(_json_list, (raw.get("outcomes"), raw.get("clobTokenIds"), raw.get("outcomePrices")))
        if len(outcomes) != 2 or len(tokens) != 2 or len(prices) != 2:
            return None
        normalized = [str(x).lower() for x in outcomes]
        if "yes" not in normalized or "no" not in normalized:
            return None
        yi, ni = normalized.index("yes"), normalized.index("no")
        end = _date(raw.get("endDate"))
        if not end or end <= datetime.now(timezone.utc):
            return None
        subject, phrase = infer_subject_phrase(raw.get("question", ""))
        event_slug = raw.get("_event_slug", "")
        override = (self.cfg.get("scheduled_events") or {}).get(event_slug)
        event_start = _date(override or raw.get("_event_start"))
        # Import here to keep the transcript adapter optional and avoid a
        # module cycle during basic market parsing tests.
        from .subtitle_history import infer_episode_target
        question = raw.get("question", "")
        event_title = raw.get("_event_title") or (raw.get("events") or [{}])[0].get("title", "")
        description = f"{raw.get('description', '')} {raw.get('_event_description', '')}"
        return Market(
            condition_id=raw.get("conditionId", ""), question=question,
            event_title=event_title,
            slug=raw.get("slug", ""), event_slug=event_slug, event_start=event_start,
            end_date=end, yes_token=str(tokens[yi]), no_token=str(tokens[ni]),
            yes_price=float(prices[yi]), no_price=float(prices[ni]),
            liquidity=float(raw.get("liquidityNum") or raw.get("liquidity") or 0),
            volume=float(raw.get("volumeNum") or raw.get("volume") or 0),
            neg_risk=bool(raw.get("negRisk", False)), tick_size=str(raw.get("orderPriceMinTickSize") or "0.01"),
            subject=subject, phrase=phrase, context=infer_context(question + " " + description),
            episode_target=infer_episode_target(question, event_title, description),
        )

    def book(self, token_id: str) -> BookSignal:
        raw = self.session.get(f"{self.clob}/book", params={"token_id": token_id}, timeout=15).json()
        bids, asks = raw.get("bids") or [], raw.get("asks") or []
        best_bid = max((float(x["price"]) for x in bids), default=0.0)
        best_ask = min((float(x["price"]) for x in asks), default=1.0)
        cutoff_bid, cutoff_ask = best_bid - 0.05, best_ask + 0.05
        bid_depth = sum(float(x["price"]) * float(x["size"]) for x in bids if float(x["price"]) >= cutoff_bid)
        ask_depth = sum(float(x["price"]) * float(x["size"]) for x in asks if float(x["price"]) <= cutoff_ask)
        total = bid_depth + ask_depth
        imbalance = bid_depth / total if total else 0.5
        mid = (best_bid + best_ask) / 2 if best_ask >= best_bid else 0
        spread = ((best_ask - best_bid) / mid * 100) if mid else 100
        return BookSignal(100 * imbalance, best_bid, best_ask, spread, bid_depth, ask_depth)

    def momentum(self, token_id: str, current: float) -> float:
        try:
            raw = self.session.get(f"{self.clob}/prices-history",
                params={"market": token_id, "interval": "1d", "fidelity": 5}, timeout=15).json()
            history = raw.get("history") or []
            old = float(history[0]["p"]) if history else current
            delta_score = max(-20, min(20, (current - old) * 100))
            return max(0, min(100, current * 100 + delta_score))
        except (requests.RequestException, KeyError, ValueError, TypeError):
            return current * 100
