from __future__ import annotations

import email.utils
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from .models import Market

log = logging.getLogger(__name__)


POSITIVE = {"will say", "expected", "plans", "focus", "preview", "agenda", "topic"}
NEGATIVE = {"cancelled", "canceled", "unlikely", "denies", "avoids", "withdraws"}


@dataclass(frozen=True)
class NewsSource:
    title: str
    url: str
    publisher: str


@dataclass(frozen=True)
class NewsEvidence:
    score: float
    count: int
    entity: str
    sources: tuple[NewsSource, ...] = ()


def _normalized(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def _contains_phrase(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {text} "


def _event_entities(market: Market) -> tuple[str, ...]:
    episode = market.episode_target
    series = str(getattr(episode, "series", "") or "").strip()
    if series:
        return (series,)
    if market.context == "nfl_game":
        text = market.event_title or market.question
        text = re.sub(r"^(?:what|which|who)\b.*?\bduring\s+", "", text, flags=re.I)
        match = re.search(
            r"([A-Za-z][A-Za-z .'-]{2,40}?)\s+(?:vs\.?|at)\s+"
            r"([A-Za-z][A-Za-z .'-]{2,40}?)(?:\s+(?:game|on|during)\b|[?]|$)",
            text,
            re.I,
        )
        if match:
            return tuple(part.strip(" .?-") for part in match.groups())
    subject = market.subject.strip()
    if subject.lower() not in {"anyone", "unknown"}:
        return (subject,)
    return ()


class NewsScorer:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.session = requests.Session()

    def score(self, market: Market) -> NewsEvidence:
        if not self.cfg["news"]["enabled"]:
            return NewsEvidence(50.0, 0, "disabled")
        entities = _event_entities(market)
        if not entities:
            return NewsEvidence(50.0, 0, "ungrounded")
        phrase_parts = tuple(part.strip() for part in market.phrase.split(" | ") if part.strip())
        if not phrase_parts:
            return NewsEvidence(50.0, 0, "ungrounded")
        entity_query = " ".join(f'"{entity}"' for entity in entities)
        phrase_query = " OR ".join(f'"{part}"' for part in phrase_parts)
        query = f"{entity_query} ({phrase_query})"
        try:
            response = self.session.get(self.cfg["news"]["rss_url"],
                params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=12)
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except (requests.RequestException, ET.ParseError) as exc:
            log.warning("news feed unavailable for %r: %s", query, exc)
            return NewsEvidence(50.0, 0, " + ".join(entities))
        now = datetime.now(timezone.utc)
        score, count = 50.0, 0
        seen: set[str] = set()
        sources: list[NewsSource] = []
        normalized_entities = tuple(value for entity in entities
                                    if (value := _normalized(entity)))
        normalized_phrases = tuple(value for part in phrase_parts
                                   if (value := _normalized(part)))
        if not normalized_entities or not normalized_phrases:
            return NewsEvidence(50.0, 0, "ungrounded")
        for item in root.findall(".//item")[:self.cfg["news"]["max_items"]]:
            raw_title = (item.findtext("title") or "").strip()
            description = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
            text = _normalized(f"{raw_title} {description}")
            dedupe_key = _normalized(raw_title)
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            try:
                parsed = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "")
            except (TypeError, ValueError):
                parsed = None
            if parsed and parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age = max(0.0, (now - parsed).total_seconds() / 3600) if parsed else 999
            if age > self.cfg["news"]["lookback_hours"]:
                continue
            entity_relevant = all(_contains_phrase(text, entity)
                                  for entity in normalized_entities)
            # Remove the grounding entity before looking for the target phrase.
            # Otherwise a contract for "Dragon" is spuriously supported by every
            # headline containing the series name "House of the Dragon".
            phrase_text = text
            for entity in normalized_entities:
                phrase_text = re.sub(
                    rf"(?<![a-z0-9]){re.escape(entity)}(?![a-z0-9])", " ", phrase_text)
            phrase_text = " ".join(phrase_text.split())
            phrase_relevant = any(_contains_phrase(phrase_text, part)
                                  for part in normalized_phrases)
            if not (entity_relevant and phrase_relevant):
                continue
            count += 1
            decay = 1 - age / self.cfg["news"]["lookback_hours"]
            score += 8 * decay
            score += 3 * sum(term in text for term in POSITIVE) * decay
            score -= 5 * sum(term in text for term in NEGATIVE) * decay
            if len(sources) < 3:
                url = (item.findtext("link") or "").strip()
                publisher = (item.findtext("source") or urlparse(url).netloc or "News").strip()
                sources.append(NewsSource(raw_title[:180], url, publisher[:80]))
        return NewsEvidence(max(0, min(100, score)), count,
                            " + ".join(entities), tuple(sources))
