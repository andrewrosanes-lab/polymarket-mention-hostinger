from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import sqlite3

import pytest

from mentionbot.engine import Engine
from mentionbot.execution import _response_fill
from mentionbot.models import BookSignal, Market, Score
from mentionbot.market import PolymarketData
from mentionbot.news import NewsScorer
from mentionbot.scoring import combine
from mentionbot.storage import Store
from mentionbot.transcript_history import (count_phrase, market_history_shape,
                                           parse_govinfo_transcript)
from mentionbot.subtitle_history import EpisodeTarget, infer_episode_target, subtitle_text


def test_confirmed_buy_and_sell_amounts():
    buy = _response_fill({"status": "matched", "orderID": "b",
                          "makingAmount": "4", "takingAmount": "10"}, "BUY", .5)
    assert buy.price == .4 and buy.size_usd == 4
    sell = _response_fill({"status": "matched", "orderID": "s",
                           "makingAmount": "10", "takingAmount": "3"}, "SELL", .5)
    assert sell.price == .3 and sell.size_usd == 3
    with pytest.raises(RuntimeError):
        _response_fill({"status": "unmatched"}, "SELL", .5)


def test_position_metadata_survives_missing_discovery_market(tmp_path):
    store = Store(str(tmp_path / "state.db"), str(tmp_path / "journal.jsonl"))
    store.record_position("c", "t", "YES", 3, .3, "o", "Question",
                          "2030-01-01T00:00:00+00:00", "0.01", False)
    row = store.open_positions()[0]
    assert row["question"] == "Question"
    assert row["end_date"].startswith("2030")
    assert row["tick_size"] == "0.01"


def test_partial_exit_keeps_remaining_position_open(tmp_path):
    store = Store(str(tmp_path / "state.db"), str(tmp_path / "journal.jsonl"))
    store.record_position("c", "t", "YES", 4, .4, "o", "Question",
                          "2030-01-01T00:00:00+00:00", "0.01", False)
    position_id = store.open_positions()[0]["position_id"]
    pnl = store.close_position(position_id, .5, "sell", 4)
    row = store.open_positions()[0]
    assert round(pnl, 4) == .4
    assert round(row["size_usd"], 4) == 2.4


def test_repeated_condition_entries_remain_separate_and_counted(tmp_path):
    store = Store(str(tmp_path / "state.db"), str(tmp_path / "journal.jsonl"))
    for order in ("first", "second"):
        store.record_position("c", "t", "YES", 3, .3, order, "Question",
                              "2030-01-01T00:00:00+00:00", "0.01", False)
    positions = store.open_positions()
    assert len(positions) == 2
    assert len({row["position_id"] for row in positions}) == 2
    assert {row["order_id"] for row in positions} == {"first", "second"}


def test_legacy_position_table_migrates_without_losing_open_trade(tmp_path):
    database = tmp_path / "legacy.db"
    db = sqlite3.connect(database)
    db.execute(
        """CREATE TABLE positions (
          condition_id TEXT PRIMARY KEY, token_id TEXT NOT NULL,
          side TEXT NOT NULL, size_usd REAL NOT NULL, entry_price REAL NOT NULL,
          status TEXT NOT NULL, opened_at TEXT NOT NULL, order_id TEXT,
          peak_price REAL NOT NULL DEFAULT 0, question TEXT NOT NULL DEFAULT '',
          end_date TEXT, tick_size TEXT NOT NULL DEFAULT '0.01',
          neg_risk INTEGER NOT NULL DEFAULT 0)"""
    )
    db.execute(
        """INSERT INTO positions
           VALUES ('c','t','YES',3,.3,'open','2026-08-08T00:00:00Z','old',
                   .3,'Question','2030-01-01T00:00:00Z','0.01',0)"""
    )
    db.commit()
    db.close()

    store = Store(str(database), str(tmp_path / "journal.jsonl"))
    legacy = store.open_positions()[0]
    assert legacy["position_id"] > 0
    assert legacy["condition_id"] == "c" and legacy["order_id"] == "old"
    store.record_position("c", "t", "YES", 4, .4, "new", "Question",
                          "2030-01-01T00:00:00Z", "0.01", False)
    assert len(store.open_positions()) == 2


def test_historical_observations_are_resolved_and_deduplicated(tmp_path):
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"events": [{"title": "Trump rally speech", "description": "address",
                "markets": [
                    {"conditionId": "yes-condition", "question": 'Will Trump say "tariff"?',
                     "description": "speech", "outcomes": '["Yes","No"]',
                     "outcomePrices": '["1","0"]'},
                    {"conditionId": "no-condition", "question": 'Will Trump say "moon"?',
                     "description": "speech", "outcomes": '["Yes","No"]',
                     "outcomePrices": '["0","1"]'},
                    {"conditionId": "unresolved", "question": 'Will Trump say "maybe"?',
                     "outcomes": '["Yes","No"]', "outcomePrices": '["0.5","0.5"]'},
                ]}], "next_cursor": None}
    cfg = {"historical": {"enabled": True, "max_events_per_tag": 5,
            "resolution_confidence": .99},
           "discovery": {"tag_ids": [{"id": 1}, {"id": 2}]},
           "execution": {"host": "https://clob.polymarket.com"}}
    data = PolymarketData(cfg)
    data.session = type("Session", (), {"get": lambda self, *a, **k: Response()})()
    observations = data.resolved_observations()
    assert len(observations) == 2
    assert {x["condition_id"]: x["occurred"] for x in observations} == {
        "yes-condition": True, "no-condition": False}

    store = Store(str(tmp_path / "history.db"), str(tmp_path / "history.jsonl"))
    for item in observations + observations:
        store.add_observation(item["subject"], item["phrase"], item["context"],
                              item["occurred"], item["condition_id"])
    assert store.observation_count() == 2
    hits, total, scope = store.historical_pattern("Trump", "new phrase", "speech")
    assert total == 2 and hits == 1 and scope == "subject/context"


def test_phrase_level_history_can_disable_misleading_broad_fallback(tmp_path):
    store = Store(str(tmp_path / "history.db"), str(tmp_path / "history.jsonl"))
    store.add_observation("Anyone", "different word", "other", True, "one")
    store.add_observation("Anyone", "another word", "other", False, "two")
    hits, total, scope = store.historical_pattern(
        "Anyone", "Power", "other", allow_broad_fallback=False)
    assert (hits, total) == (0, 0)
    assert scope == "neutral — no phrase-level evidence"


def test_subtitle_history_does_not_leak_between_series(tmp_path):
    store = Store(str(tmp_path / "history.db"), str(tmp_path / "history.jsonl"))
    store.add_transcript_mention(
        "opensubtitles:1", "Anyone", "Power", "tv:big brother", 2,
        "2026-08-01", "Big Brother S28E16", "https://example.com/subtitle",
        source_kind="opensubtitles")
    assert store.historical_pattern(
        "Anyone", "Power", "tv:big brother", allow_broad_fallback=False)[:2] == (1, 1)
    hits, total, scope = store.historical_pattern(
        "Anyone", "Power", "tv:house of the dragon", allow_broad_fallback=False)
    assert (hits, total) == (0, 0)
    assert scope == "neutral — no phrase-level evidence"


def test_news_requires_event_entity_and_phrase_and_deduplicates():
    published = format_datetime(datetime.now(timezone.utc))
    xml = f"""<rss><channel>
      <item><title>Big Brother preview: Power may decide the episode</title>
        <description>The house plans to focus on Power.</description>
        <pubDate>{published}</pubDate><link>https://example.com/a</link>
        <source>Example News</source></item>
      <item><title>Big Brother preview: Power may decide the episode</title>
        <description>Duplicate syndication.</description>
        <pubDate>{published}</pubDate><link>https://duplicate.example/a</link></item>
      <item><title>Power demand rises across Texas</title>
        <description>Unrelated energy coverage.</description>
        <pubDate>{published}</pubDate><link>https://example.com/b</link></item>
      <item><title>Big Brother episode preview</title>
        <description>No target phrase here.</description>
        <pubDate>{published}</pubDate><link>https://example.com/c</link></item>
    </channel></rss>""".encode()

    class Response:
        content = xml
        def raise_for_status(self): pass

    cfg = {"news": {"enabled": True, "rss_url": "https://news.example/rss",
                     "max_items": 30, "lookback_hours": 24}}
    scorer = NewsScorer(cfg)
    scorer.session = type("Session", (), {
        "get": lambda self, *args, **kwargs: Response()})()
    market = Market("c", 'Will anyone say "Power" during Big Brother E17?',
        "Big Brother Episode 17", "s", "es", None,
        datetime.now(timezone.utc) + timedelta(hours=2), "y", "n", .5, .5,
        1000, 1000, False, "0.01", "Anyone", "Power", "other",
        EpisodeTarget("Big Brother", 28, 17))
    evidence = scorer.score(market)
    assert evidence.count == 1
    assert evidence.score > 50
    assert evidence.entity == "Big Brother"
    assert len(evidence.sources) == 1


def test_news_without_reliable_event_entity_stays_neutral_without_request():
    cfg = {"news": {"enabled": True, "rss_url": "https://news.example/rss",
                     "max_items": 30, "lookback_hours": 24}}
    scorer = NewsScorer(cfg)
    scorer.session = type("Session", (), {
        "get": lambda self, *args, **kwargs: pytest.fail("network should not be called")})()
    market = Market("c", 'Will anyone say "Power"?', "", "s", "es", None,
        datetime.now(timezone.utc) + timedelta(hours=2), "y", "n", .5, .5,
        1000, 1000, False, "0.01", "Anyone", "Power", "other")
    evidence = scorer.score(market)
    assert (evidence.score, evidence.count, evidence.entity) == (50.0, 0, "ungrounded")


def test_news_does_not_count_phrase_only_present_inside_series_name():
    published = format_datetime(datetime.now(timezone.utc))
    xml = f"""<rss><channel><item>
      <title>House of the Dragon season finale preview</title>
      <description>The HBO series returns tonight.</description>
      <pubDate>{published}</pubDate><link>https://example.com/dragon</link>
    </item></channel></rss>""".encode()

    class Response:
        content = xml
        def raise_for_status(self): pass

    scorer = NewsScorer({"news": {"enabled": True, "rss_url": "https://news.example/rss",
        "max_items": 30, "lookback_hours": 24}})
    scorer.session = type("Session", (), {
        "get": lambda self, *args, **kwargs: Response()})()
    market = Market("c", 'Will anyone say "Dragon"?', "House of the Dragon",
        "s", "es", None, datetime.now(timezone.utc) + timedelta(hours=2),
        "y", "n", .5, .5, 1000, 1000, False, "0.01", "Anyone", "Dragon",
        "other", EpisodeTarget("House of the Dragon", 3, 8))
    evidence = scorer.score(market)
    assert evidence.count == 0 and evidence.score == 50


def test_probability_is_separate_from_book_timing():
    cfg = {"probability_weights": {"historical_context": .35,
                                    "news_live_impact": .25, "market_prior": .14},
           "timing_weights": {"order_book_imbalance": .20, "momentum": .14},
           "tiers": [{"name": "C", "min_confidence": 70,
                      "max_confidence": 80, "size_usd": 3}]}
    market = Market("c", "q", "e", "s", "es", None,
        datetime.now(timezone.utc) + timedelta(hours=2), "y", "n", .5, .5,
        1000, 1000, False, "0.01", "Trump", "word", "speech")
    weak_book = BookSignal(10, .39, .40, 2, 100, 100)
    strong_book = BookSignal(90, .39, .40, 2, 100, 100)
    no_book = BookSignal(50, .59, .60, 2, 100, 100)
    weak = combine(market, weak_book, no_book, 80, 70, 65, 60, cfg, 1)
    strong = combine(market, strong_book, no_book, 80, 70, 65, 60, cfg, 1)
    assert weak.yes_probability == strong.yes_probability
    assert weak.timing_score < strong.timing_score


def test_unavailable_news_is_excluded_instead_of_diluting_probability():
    cfg = {"probability_weights": {"historical_context": .35,
                                    "news_live_impact": .25, "market_prior": .14},
           "timing_weights": {"order_book_imbalance": .20, "momentum": .14},
           "tiers": [{"name": "C", "min_confidence": 65,
                      "max_confidence": 80, "size_usd": 3}]}
    market = Market("c", "q", "e", "s", "es", None,
        datetime.now(timezone.utc) + timedelta(hours=2), "y", "n", .5, .5,
        1000, 1000, False, "0.01", "Trump", "word", "speech")
    yes_book = BookSignal(60, .39, .40, 2, 100, 100)
    no_book = BookSignal(40, .59, .60, 2, 100, 100)
    unavailable = combine(market, yes_book, no_book, 80, 50, 65, 60,
                          cfg, 0, "exact", 12)
    observed_neutral = combine(market, yes_book, no_book, 80, 50, 65, 60,
                               cfg, 1, "exact", 12)
    assert unavailable.yes_probability > observed_neutral.yes_probability
    assert unavailable.tier == "C"


def test_arbitrage_has_separate_execution_confidence_not_directional_boost():
    cfg = {"probability_weights": {"historical_context": .35,
                                    "news_live_impact": .25, "market_prior": .14},
           "timing_weights": {"order_book_imbalance": .20, "momentum": .14},
           "tiers": [{"name": "C", "min_confidence": 65,
                      "max_confidence": 80, "size_usd": 3}],
           "arbitrage": {"min_edge_pct": 6}}
    market = Market("c", "q", "e", "s", "es", None,
        datetime.now(timezone.utc) + timedelta(hours=2), "y", "n", .5, .5,
        1000, 1000, False, "0.01", "Trump", "word", "speech")
    yes_book = BookSignal(60, .44, .45, 2, 100, 100)
    no_book = BookSignal(40, .47, .48, 2, 100, 100)
    score = combine(market, yes_book, no_book, 75, 50, 60, 60,
                    cfg, 0, "exact", 12)
    assert round(score.cross_book_arb_pct) == 7
    assert score.arb_confidence > 75
    expected_probability = (75 * .35 + 60 * .14) / (.35 + .14)
    assert score.yes_probability == expected_probability


def test_official_transcript_counts_only_president_and_overrides_gamma(tmp_path):
    raw = """<html><body>
      <p class="s1">Administration of Donald J. Trump, 2026</p>
      <h1>Remarks and an Exchange With Reporters</h1>
      <p class="s1">August 8, 2026</p>
      <p class="s2">The President. Security is important.</p>
      <p>We need security again.</p>
      <p class="s2">Q. Will you discuss security?</p>
      <p class="s2">Secretary Smith. Security.</p>
      <p>More security from the Secretary.</p>
      <p>NOTE: released later.</p>
    </body></html>"""
    transcript = parse_govinfo_transcript("DCPD-202600001", raw)
    assert transcript is not None
    assert count_phrase(transcript.president_text, "security") == 2

    store = Store(str(tmp_path / "history.db"), str(tmp_path / "history.jsonl"))
    store.add_observation("Trump", "security", "speech", False, "gamma")
    store.add_transcript_mention("DCPD-202600001", "Trump", "security", "speech",
                                 2, transcript.document_date, transcript.title,
                                 transcript.source_url)
    hits, total, scope = store.historical_pattern("Trump", "security", "speech")
    assert (hits, total) == (1, 1)
    assert scope == "official transcript phrase/context"


def test_phrase_alternatives_are_counted_as_or():
    assert count_phrase("Karoline spoke. Leavitt answered.", "Karoline | Leavitt") == 2


def test_weekly_history_and_count_threshold_match_market_wording(tmp_path):
    assert market_history_shape('Will Trump say "war" 5+ times this week?') == ("week", 5)
    store = Store(str(tmp_path / "weekly.db"), str(tmp_path / "journal.jsonl"))
    for doc, date, count in (("d1", "2026-08-03", 3), ("d2", "2026-08-04", 2),
                             ("d3", "2026-07-27", 4)):
        store.add_transcript_mention(doc, "Trump", "war", "speech", count,
                                     date, "Remarks", f"https://example/{doc}")
    hits, total, scope = store.historical_pattern(
        "Trump", "war", "speech", period="week", min_mentions=5)
    assert (hits, total) == (1, 2)
    assert scope == "official transcript weekly phrase/context"


def test_episode_metadata_and_subtitle_cleanup():
    target = infer_episode_target(
        'Will anyone say "Power" during Big Brother E17?',
        'What will be said during Episode 17 of Big Brother?',
        'Episode 17 of Big Brother Season 28 is scheduled to air.')
    assert target and (target.series, target.season, target.episode) == (
        "Big Brother", 28, 17)
    target = infer_episode_target(
        'Will anyone say "King" during House of the Dragon E8 S3?', '', '')
    assert target and (target.series, target.season, target.episode) == (
        "House of the Dragon", 3, 8)
    raw = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<b>Power</b> wins."
    assert subtitle_text(raw) == "Power wins."


def test_risk_uses_executable_ask_not_cached_gamma_price():
    engine = Engine.__new__(Engine)
    engine.cfg = {"risk": {"kill_switch_file": "/never", "max_open_positions": 5,
        "min_liquidity_usd": 800, "min_volume_usd": 800, "max_spread_pct": 12,
        "min_timing_score": 45, "min_model_edge_pct": 6, "require_known_event_start": False,
        "max_hours_before_event": 4, "min_entry_price": .16, "max_entry_price": .93,
        "max_positions_per_condition": 2}}
    engine.store = type("S", (), {"open_positions": lambda self: [],
        "daily_loss": lambda self: -999,
        "open_position_count": lambda self, c: 0})()
    market = Market("c", "q", "e", "s", "es",
        datetime.now(timezone.utc) + timedelta(minutes=30),
        datetime.now(timezone.utc) + timedelta(hours=3), "y", "n", .5, .5,
        1000, 1000, False, "0.01", "Trump", "word", "speech")
    score = Score(80, 80, "YES", "B", 4, 50, 50, 50, 50, 50, 10, 0, "")
    book = BookSignal(50, .93, .95, 2, 1000, 1000)
    assert engine.risk_ok(market, score, book) == (False, "executable entry price gate")


def test_arbitrage_is_detected_but_execution_remains_safety_locked():
    engine = Engine.__new__(Engine)
    engine.cfg = {
        "arbitrage": {"enabled": True, "execution_enabled": False,
                      "min_edge_pct": 6, "bundle_size_usd": 5},
        "risk": {"kill_switch_file": "/never", "max_open_positions": 5,
                 "min_cross_book_arb_pct": 6, "min_liquidity_usd": 800,
                 "min_volume_usd": 800, "max_spread_pct": 12,
                 "min_entry_price": .16, "max_entry_price": .93,
                 "max_hours_before_event": 4},
    }
    engine.store = type("S", (), {"open_positions": lambda self: [],
        "open_position_count": lambda self, condition: 0})()
    market = Market("c", "q", "e", "s", "es", None,
        datetime.now(timezone.utc) + timedelta(hours=2), "y", "n", .45, .48,
        1000, 1000, False, "0.01", "Trump", "word", "speech")
    yes_book = BookSignal(50, .44, .45, 2, 1000, 1000)
    no_book = BookSignal(50, .47, .48, 2, 1000, 1000)
    assert engine.arbitrage_status(market, yes_book, no_book, 7) == (
        True, "qualified arb watch — paired execution safety lock")
    assert engine.arbitrage_status(market, yes_book, no_book, 5)[0] is False


def test_dashboard_status_is_atomic_and_contains_operational_evidence(tmp_path):
    store = Store(str(tmp_path / "state.db"), str(tmp_path / "journal.jsonl"))
    store.add_observation("Trump", "Security", "speech", True, "resolved")
    store.record_position("c", "t", "YES", 3, .3, "o", "Question",
                          "2030-01-01T00:00:00+00:00", "0.01", False)
    engine = Engine.__new__(Engine)
    engine.cfg = {"mode": "live", "minimum_confidence": 65,
                  "news": {"enabled": True},
                  "risk": {"min_model_edge_pct": 6, "min_timing_score": 45,
                           "max_hours_before_event": 4},
                  "paths": {"status": str(tmp_path / "status.json")}}
    engine.store = store
    engine.write_status([object()], [{"confidence": 71.0, "question": "Question"}])
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["connected"] is True and status["mode"] == "LIVE"
    assert status["markets"] == 1 and status["positions"] == 1
    assert status["deployed"] == 3 and status["evidence"]["gammaObservations"] == 1
    assert status["control"]["minimumConfidence"] == 65
    assert not (tmp_path / "status.json.tmp").exists()


def test_invalid_dashboard_control_fails_closed(tmp_path):
    engine = Engine.__new__(Engine)
    engine.cfg = {"minimum_confidence": 65, "news": {"enabled": True},
                  "risk": {"min_model_edge_pct": 6, "min_timing_score": 45,
                           "max_hours_before_event": 4}}
    engine._control_path = tmp_path / "control.json"
    engine._control_path.write_text('{"minimumConfidence": 10}')
    control = engine._load_runtime_control()
    assert control["paused"] is True
    assert control["minimumConfidence"] == 65
