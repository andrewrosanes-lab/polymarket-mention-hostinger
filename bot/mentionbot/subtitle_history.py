from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from .transcript_history import count_phrase


log = logging.getLogger(__name__)
API = "https://api.opensubtitles.com/api/v1"


@dataclass(frozen=True)
class EpisodeTarget:
    series: str
    season: int
    episode: int


def infer_episode_target(question: str, event_title: str,
                         description: str) -> EpisodeTarget | None:
    text = " ".join((question, event_title, description))
    compact = re.search(r"during\s+(.+?)\s+E(\d+)\s+S(\d+)\b", question, re.I)
    if compact:
        return EpisodeTarget(compact.group(1).strip(), int(compact.group(3)),
                             int(compact.group(2)))
    verbose = re.search(
        r"Episode\s+(\d+)\s+of\s+(.+?)(?:\s+Season\s+(\d+))?(?:\s+is|\?|$)",
        text, re.I,
    )
    season = re.search(r"\bSeason\s+(\d+)\b", text, re.I)
    if verbose and (verbose.group(3) or season):
        return EpisodeTarget(verbose.group(2).strip(" :?"),
                             int(verbose.group(3) or season.group(1)),
                             int(verbose.group(1)))
    return None


def subtitle_text(raw: str) -> str:
    lines: list[str] = []
    for line in raw.replace("\r", "").split("\n"):
        stripped = line.strip()
        if (not stripped or stripped.isdigit() or "-->" in stripped
                or stripped.startswith(("WEBVTT", "NOTE", "STYLE", "[Script Info]"))
                or re.match(r"^(Dialogue|Comment):", stripped, re.I)):
            if re.match(r"^Dialogue:", stripped, re.I):
                # ASS dialogue has nine metadata commas before the subtitle.
                parts = stripped.split(",", 9)
                if len(parts) == 10:
                    lines.append(parts[-1])
            continue
        stripped = re.sub(r"<[^>]+>|\{\\[^}]+\}", " ", stripped)
        lines.append(re.sub(r"\s+", " ", stripped).strip())
    return " ".join(lines)


class OpenSubtitlesHistory:
    """Optional historical TV subtitle counts; never a resolution oracle."""

    def __init__(self, cfg: dict):
        self.cfg = cfg.get("subtitle_history") or {}
        self.api_key = os.getenv("OPENSUBTITLES_API_KEY", "").strip()
        self.session = requests.Session()
        self.session.headers.update({
            "Api-Key": self.api_key,
            "User-Agent": str(self.cfg.get("user_agent", "MentionEdge v1.0")),
            "Content-Type": "application/json",
        })

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True) and self.api_key)

    def _search(self, target: EpisodeTarget, episode: int) -> dict | None:
        response = self.session.get(f"{API}/subtitles", params={
            "query": target.series,
            "season_number": target.season,
            "episode_number": episode,
            "languages": "en",
            "type": "episode",
        }, timeout=float(self.cfg.get("timeout_sec", 20)))
        response.raise_for_status()
        candidates = response.json().get("data") or []
        if not candidates:
            return None
        def rank(item: dict) -> tuple:
            attrs = item.get("attributes") or {}
            release = " ".join(attrs.get("release") or []).lower()
            trusted = int(bool(attrs.get("from_trusted")))
            web = int(any(marker in release for marker in ("web", "hmax", "amzn", "paramount")))
            downloads = int(attrs.get("download_count") or 0)
            return trusted, web, downloads
        return max(candidates, key=rank)

    def _download(self, candidate: dict) -> tuple[str, str] | None:
        attrs = candidate.get("attributes") or {}
        files = attrs.get("files") or []
        if not files:
            return None
        file_id = files[0].get("file_id")
        if not file_id:
            return None
        response = self.session.post(f"{API}/download", json={"file_id": file_id},
                                     timeout=float(self.cfg.get("timeout_sec", 20)))
        response.raise_for_status()
        link = response.json().get("link")
        if not link:
            return None
        raw = requests.get(link, timeout=float(self.cfg.get("timeout_sec", 20)))
        raw.raise_for_status()
        return str(file_id), subtitle_text(raw.text)

    def refresh(self, markets: list, store) -> tuple[int, int, int]:
        if not self.cfg.get("enabled", True):
            return 0, 0, 0
        if not self.api_key:
            return 0, 0, 0
        refresh_hours = float(self.cfg.get("refresh_hours", 24))
        grouped: dict[EpisodeTarget, set[tuple[str, str, str, str]]] = {}
        for market in markets:
            target = getattr(market, "episode_target", None)
            history_context = f"tv:{target.series.lower()}" if target else ""
            refresh_key = f"{market.phrase} @ {history_context}"
            if (target and target.episode > 1
                    and store.transcript_refresh_due(
                        market.subject, refresh_key, refresh_hours,
                        source_kind="opensubtitles")):
                grouped.setdefault(target, set()).add(
                    (market.subject, market.phrase, history_context, refresh_key))
        if not grouped:
            return 0, 0, 0

        max_downloads = max(1, int(self.cfg.get("max_downloads_per_refresh", 5)))
        lookback = max(1, int(self.cfg.get("max_prior_episodes", 12)))
        downloads = rows = mentions = 0
        refreshed: set[tuple[str, str]] = set()
        for target, phrases in grouped.items():
            first = max(1, target.episode - lookback)
            for episode in range(first, target.episode):
                if downloads >= max_downloads:
                    break
                try:
                    candidate = self._search(target, episode)
                    downloaded = self._download(candidate) if candidate else None
                except requests.RequestException as exc:
                    log.warning("OpenSubtitles skipped %s S%02dE%02d: %s",
                                target.series, target.season, episode, exc)
                    continue
                if not downloaded:
                    continue
                file_id, text = downloaded
                downloads += 1
                document_id = f"opensubtitles:{file_id}"
                title = f"{target.series} S{target.season:02d}E{episode:02d}"
                source_url = f"https://www.opensubtitles.com/en/subtitles/{file_id}"
                date = datetime.now(timezone.utc).date().isoformat()
                for subject, phrase, context, refresh_key in phrases:
                    count = count_phrase(text, phrase)
                    rows += int(store.add_transcript_mention(
                        document_id, subject, phrase, context, count, date, title,
                        source_url, source_kind="opensubtitles"))
                    mentions += count
                    refreshed.add((subject, refresh_key))
            if downloads >= max_downloads:
                break
        for subject, phrase in refreshed:
            store.mark_transcript_refreshed(
                subject, phrase, downloads, source_kind="opensubtitles")
        return downloads, rows, mentions
