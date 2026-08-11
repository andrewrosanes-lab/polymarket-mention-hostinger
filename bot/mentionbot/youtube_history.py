from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from .transcript_history import count_phrase


log = logging.getLogger(__name__)
API = "https://api.supadata.ai/v1"
SOURCE_KIND = "supadata_youtube_shadow"
_STOP_WORDS = {
    "a", "an", "and", "at", "be", "during", "first", "for", "in", "next",
    "of", "on", "or", "say", "said", "the", "their", "this", "to", "what",
    "will", "with",
}


@dataclass(frozen=True)
class YouTubeVideo:
    video_id: str
    title: str
    upload_date: str
    url: str


def _tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 1 and token not in _STOP_WORDS
    }


def comparable_event_query(market) -> str:
    """Build a recurring-event query without using the wagered phrase."""
    target = getattr(market, "episode_target", None)
    if target:
        return str(target.series).strip()
    text = str(getattr(market, "event_title", "") or market.question)
    text = re.sub(r'["\u201c].+?["\u201d]', " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b(?:january|february|march|april|may|june|july|august|"
                  r"september|october|november|december)\s+\d{1,2}\b", " ",
                  text, flags=re.I)
    text = re.sub(r"\b20\d{2}\b", " ", text)
    text = re.sub(r"^what will (.+?) say during (?:his|her|their) next ",
                  r"\1 ", text, flags=re.I)
    text = re.sub(r"^what will be said (?:on|during) (?:the )?(?:next |first )?",
                  "", text, flags=re.I)
    text = re.sub(r"[^A-Za-z0-9&+.' -]+", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -?")[:140]


def calibration_segment(market) -> str:
    query = re.sub(r"[^a-z0-9]+", "-", comparable_event_query(market).lower())
    return f"{market.context}:{query.strip('-')}"


def _transcript_text(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(item.get("text") or "") for item in content
                        if isinstance(item, dict))
    return ""


def _published_before_event(video: YouTubeVideo, market) -> bool:
    event_start = getattr(market, "event_start", None)
    # Unknown publication time is not historical evidence.  Accepting it can
    # leak the target episode itself (or a later upload) into a live forecast.
    if not video.upload_date:
        return False
    if event_start is None:
        return True
    try:
        published = datetime.fromisoformat(video.upload_date.replace("Z", "+00:00"))
    except ValueError:
        return False
    return published < event_start


class SupadataYouTubeHistory:
    """Collect published YouTube transcript counts as shadow-only evidence."""

    def __init__(self, cfg: dict):
        self.cfg = cfg.get("youtube_history") or {}
        self.api_key = os.getenv("SUPADATA_API_KEY", "").strip()
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({
                "x-api-key": self.api_key,
                "Accept": "application/json",
            })

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False) and self.api_key)

    def _get(self, path: str, params: dict) -> dict:
        response = self.session.get(
            f"{API}{path}", params=params,
            timeout=float(self.cfg.get("timeout_sec", 30)))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Supadata response was not an object")
        return payload

    def _search(self, query: str) -> list[YouTubeVideo]:
        payload = self._get("/youtube/search", {
            "query": query,
            "type": "video",
            "limit": int(self.cfg.get("search_limit", 8)),
            "sortBy": "date",
            "uploadDate": str(self.cfg.get("upload_date", "year")),
            "features[]": "subtitles",
        })
        wanted = _tokens(query)
        videos: list[YouTubeVideo] = []
        for item in payload.get("results") or []:
            if not isinstance(item, dict) or item.get("type") != "video":
                continue
            title = str(item.get("title") or "")
            common = wanted & _tokens(title)
            required = 1 if len(wanted) <= 1 else 2
            if len(common) < required:
                continue
            video_id = str(item.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
                continue
            videos.append(YouTubeVideo(
                video_id, title, str(item.get("uploadDate") or ""),
                f"https://www.youtube.com/watch?v={video_id}"))
        return videos

    def _transcript(self, video: YouTubeVideo) -> str:
        payload = self._get("/transcript", {
            "url": video.url,
            "text": "true",
            "mode": "native",
        })
        job_id = payload.get("jobId")
        if job_id:
            deadline = time.monotonic() + float(
                self.cfg.get("job_poll_timeout_sec", 30))
            interval = max(1.0, float(self.cfg.get("job_poll_interval_sec", 1)))
            while time.monotonic() < deadline:
                time.sleep(interval)
                payload = self._get(f"/transcript/{job_id}", {})
                if not payload.get("jobId") and payload.get("content") is not None:
                    break
            else:
                log.info("Supadata transcript still processing; retrying video %s later",
                         video.video_id)
                return ""
        return _transcript_text(payload)

    def refresh(self, markets: list, store) -> tuple[int, int, int]:
        if not self.enabled:
            return 0, 0, 0
        minimum_interval = float(self.cfg.get("minimum_interval_hours", 24))
        if not store.transcript_refresh_due(
                "__supadata__", "__global__", minimum_interval,
                source_kind=SOURCE_KIND):
            return 0, 0, 0

        target_refresh = float(self.cfg.get("target_refresh_hours", 720))
        grouped: dict[str, list] = {}
        for market in markets:
            query = comparable_event_query(market)
            refresh_key = f"{query} @ {market.context}"
            if (len(_tokens(query)) >= 1 and store.transcript_refresh_due(
                    market.subject, refresh_key, target_refresh,
                    source_kind=SOURCE_KIND)):
                grouped.setdefault(query, []).append((market, refresh_key))
        if not grouped:
            return 0, 0, 0

        max_events = max(1, int(self.cfg.get("max_events_per_refresh", 1)))
        max_videos = max(1, int(self.cfg.get("max_videos_per_event", 2)))
        videos_read = rows = mentions = 0
        attempted = False
        refreshed: set[tuple[str, str]] = set()
        for query, targets in sorted(grouped.items())[:max_events]:
            try:
                videos = self._search(query)[:max_videos]
            except (requests.RequestException, ValueError) as exc:
                log.warning("Supadata YouTube search skipped %r: %s", query, exc)
                continue
            attempted = True
            for video in videos:
                eligible = [(market, refresh_key) for market, refresh_key in targets
                            if _published_before_event(video, market)]
                if not eligible:
                    log.info("Supadata video excluded without verified pre-event date: %s",
                             video.video_id)
                    continue
                try:
                    text = self._transcript(video)
                except (requests.RequestException, ValueError) as exc:
                    log.warning("Supadata transcript skipped %s: %s",
                                video.video_id, exc)
                    continue
                if not text:
                    continue
                videos_read += 1
                date = video.upload_date[:10] or datetime.now(
                    timezone.utc).date().isoformat()
                for market, refresh_key in eligible:
                    count = count_phrase(text, market.phrase)
                    rows += int(store.add_transcript_mention(
                        f"youtube:{video.video_id}", market.subject,
                        market.phrase, market.context, count, date, video.title,
                        video.url, source_kind=SOURCE_KIND))
                    mentions += count
                    refreshed.add((market.subject, refresh_key))

        # A target is put on its long refresh interval only after a dated,
        # pre-event transcript was actually retrieved and counted.  Failed or
        # asynchronous jobs become eligible again after the global daily budget.
        for subject, refresh_key in refreshed:
            store.mark_transcript_refreshed(
                subject, refresh_key, videos_read, source_kind=SOURCE_KIND)
        if attempted:
            store.mark_transcript_refreshed(
                "__supadata__", "__global__", videos_read,
                source_kind=SOURCE_KIND)
        return videos_read, rows, mentions
