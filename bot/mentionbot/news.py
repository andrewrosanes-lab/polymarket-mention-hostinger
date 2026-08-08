from __future__ import annotations

import email.utils
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)


POSITIVE = {"will say", "expected", "plans", "focus", "preview", "agenda", "topic"}
NEGATIVE = {"cancelled", "canceled", "unlikely", "denies", "avoids", "withdraws"}


class NewsScorer:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def score(self, subject: str, phrase: str, context: str) -> tuple[float, int]:
        if not self.cfg["news"]["enabled"]:
            return 50.0, 0
        useful_subject = "" if subject.lower() in {"anyone", "unknown"} else subject
        phrase_query = " OR ".join(f'"{part}"' for part in phrase.split(" | "))
        query = " ".join(x for x in (useful_subject, phrase_query, context.replace("_", " ")) if x)
        try:
            response = requests.get(self.cfg["news"]["rss_url"],
                params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=12)
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except (requests.RequestException, ET.ParseError) as exc:
            log.warning("news feed unavailable for %r: %s", query, exc)
            return 50.0, 0
        now = datetime.now(timezone.utc)
        score, count = 50.0, 0
        for item in root.findall(".//item")[:self.cfg["news"]["max_items"]]:
            title = (item.findtext("title") or "").lower()
            description = re.sub(r"<[^>]+>", " ", item.findtext("description") or "").lower()
            text = f"{title} {description}"
            parsed = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "")
            age = max(0.0, (now - parsed).total_seconds() / 3600) if parsed else 999
            if age > self.cfg["news"]["lookback_hours"]:
                continue
            phrase_relevant = any(part.lower() in text for part in phrase.split(" | "))
            relevance = int(bool(useful_subject) and useful_subject.lower() in text) + int(phrase_relevant)
            if not relevance:
                continue
            count += 1
            decay = 1 - age / self.cfg["news"]["lookback_hours"]
            score += 4 * relevance * decay
            score += 3 * sum(term in text for term in POSITIVE) * decay
            score -= 5 * sum(term in text for term in NEGATIVE) * decay
        return max(0, min(100, score)), count
