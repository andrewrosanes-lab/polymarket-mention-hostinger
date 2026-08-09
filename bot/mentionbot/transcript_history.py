from __future__ import annotations

import logging
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

from .market import infer_context


log = logging.getLogger(__name__)

GOVINFO = "https://www.govinfo.gov"
_PACKAGE_RE = re.compile(r"DCPD-(\d{4})(\d{5})$")
_TRANSCRIPT_TITLE_RE = re.compile(
    r"\b(remarks?|address|news conference|exchange with reporters|"
    r"interview|question-and-answer|telerally)\b",
    re.I,
)
_TRUMP_SUBJECT_RE = re.compile(r"\b(trump|donald j\.? trump|the president)\b", re.I)


@dataclass(frozen=True)
class Transcript:
    document_id: str
    title: str
    document_date: str
    context: str
    source_url: str
    president_text: str


class _GovInfoHTML(HTMLParser):
    """Extract block text without retaining the source transcript."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._tag: str | None = None
        self._attrs: dict[str, str] = {}
        self._parts: list[str] = []
        self.blocks: list[tuple[str, dict[str, str], str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"p", "h1"}:
            self._tag = tag
            self._attrs = dict(attrs)
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._tag:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._tag == tag:
            text = re.sub(r"\s+", " ", "".join(self._parts)).strip()
            if text:
                self.blocks.append((tag, self._attrs, text))
            self._tag = None
            self._attrs = {}
            self._parts = []


def _normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().replace("’", "'")
    return re.sub(r"[^a-z0-9']+", " ", text).strip()


def phrase_variants(phrase: str) -> list[str]:
    return [part.strip() for part in phrase.split(" | ") if part.strip()]


def count_phrase(text: str, phrase: str) -> int:
    haystack = f" {_normalized(text)} "
    total = 0
    for variant in phrase_variants(phrase):
        needle = _normalized(variant)
        if needle:
            total += len(re.findall(rf"(?<![a-z0-9']){re.escape(needle)}(?![a-z0-9'])", haystack))
    return total


def market_history_shape(question: str) -> tuple[str, int]:
    """Treat every comparable past event as one historical observation."""
    lower = question.lower()
    threshold = re.search(r"\b(\d+)\s*\+\s*times?\b", lower)
    return "event", int(threshold.group(1)) if threshold else 1


def parse_govinfo_transcript(document_id: str, raw_html: str) -> Transcript | None:
    parser = _GovInfoHTML()
    parser.feed(raw_html)
    title = next((text for tag, _, text in parser.blocks if tag == "h1"), "")
    if not title or not _TRANSCRIPT_TITLE_RE.search(title):
        return None
    opening = " ".join(text for _, _, text in parser.blocks[:4])
    if "Administration of Donald J. Trump" not in opening:
        return None
    date_text = next((text for tag, attrs, text in parser.blocks
                      if tag == "p" and "s1" in attrs.get("class", "").split()
                      and re.fullmatch(r"[A-Z][a-z]+ \d{1,2}, \d{4}", text)), "")
    try:
        document_date = datetime.strptime(date_text, "%B %d, %Y").date().isoformat()
    except ValueError:
        return None

    president = False
    spoken: list[str] = []
    for tag, attrs, text in parser.blocks:
        if tag != "p":
            continue
        normalized = _normalized(text)
        if normalized.startswith("note ") or normalized.startswith("categories "):
            break
        css_class = attrs.get("class", "")
        if text.startswith("The President."):
            president = True
            spoken.append(text.removeprefix("The President.").strip())
        elif text.startswith("President Trump."):
            president = True
            spoken.append(text.removeprefix("President Trump.").strip())
        elif "s2" in css_class.split():
            # GovInfo uses s2 for a new explicitly named speaker. Anything not
            # labeled as the President must not enter the President-only count.
            president = False
        elif president and "s1" not in css_class.split():
            # Plain paragraphs continue the preceding speaker's turn.
            spoken.append(text)

    source_url = f"{GOVINFO}/content/pkg/{document_id}/html/{document_id}.htm"
    return Transcript(document_id, title, document_date, infer_context(title),
                      source_url, " ".join(spoken))


class GovInfoHistory:
    """Count phrases in official presidential transcripts discovered by sitemap."""

    def __init__(self, cfg: dict):
        self.cfg = cfg.get("transcript_history") or {}
        self.session = requests.Session()
        self.session.headers["User-Agent"] = str(
            self.cfg.get("user_agent", "MentionEdge/1.0 (GovInfo transcript statistics)")
        )

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    def _get(self, url: str) -> requests.Response:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "www.govinfo.gov":
            raise ValueError(f"refusing non-GovInfo transcript URL: {url}")
        response = self.session.get(url, timeout=float(self.cfg.get("timeout_sec", 25)))
        response.raise_for_status()
        return response

    def _package_ids(self) -> list[str]:
        now_year = datetime.now(timezone.utc).year
        lookback_years = max(1, int(self.cfg.get("lookback_years", 2)))
        package_ids: set[str] = set()
        for year in range(now_year - lookback_years + 1, now_year + 1):
            url = f"{GOVINFO}/sitemap/DCPD_{year}_sitemap.xml"
            root = ET.fromstring(self._get(url).content)
            for loc in root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url/{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
                document_id = (loc.text or "").rstrip("/").rsplit("/", 1)[-1]
                if _PACKAGE_RE.fullmatch(document_id):
                    package_ids.add(document_id)
        return sorted(package_ids, reverse=True)

    def refresh(self, markets: list, store) -> tuple[int, int, int]:
        if not self.enabled:
            return 0, 0, 0
        refresh_hours = float(self.cfg.get("refresh_hours", 24))
        targets = sorted({(market.subject, market.phrase) for market in markets
                          if _TRUMP_SUBJECT_RE.search(market.subject or "")
                          and store.transcript_refresh_due(
                              market.subject, market.phrase, refresh_hours)})
        if not targets:
            return 0, 0, 0

        max_documents = max(1, int(self.cfg.get("max_documents", 120)))
        request_delay = max(0.0, float(self.cfg.get("request_delay_sec", 0.15)))
        documents = rows = mentions = 0
        for document_id in self._package_ids():
            if documents >= max_documents:
                break
            url = f"{GOVINFO}/content/pkg/{document_id}/html/{document_id}.htm"
            try:
                transcript = parse_govinfo_transcript(document_id, self._get(url).text)
            except (requests.RequestException, ET.ParseError, ValueError) as exc:
                log.warning("GovInfo transcript skipped %s: %s", document_id, exc)
                continue
            finally:
                if request_delay:
                    time.sleep(request_delay)
            if transcript is None:
                continue
            documents += 1
            for subject, phrase in targets:
                count = count_phrase(transcript.president_text, phrase)
                rows += int(store.add_transcript_mention(
                    transcript.document_id, subject, phrase, transcript.context,
                    count, transcript.document_date, transcript.title,
                    transcript.source_url,
                ))
                mentions += count

        for subject, phrase in targets:
            store.mark_transcript_refreshed(subject, phrase, documents)
        return documents, rows, mentions
