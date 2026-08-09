from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from mentionbot.engine import Engine
from mentionbot.execution import _response_fill
from mentionbot.models import BookSignal, Market, Score
from mentionbot.market import PolymarketData
from mentionbot.storage import Store
from mentionbot.transcript_history import (count_phrase, market_history_shape,
                                           parse_govinfo_transcript)
from mentionbot.subtitle_history import infer_episode_target, subtitle_text


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
        "min_model_edge_pct": 6, "require_known_event_start": False,
        "max_hours_before_event": 4, "min_entry_price": .16, "max_entry_price": .93,
        "one_position_per_condition": False}}
    engine.store = type("S", (), {"open_positions": lambda self: [],
        "daily_loss": lambda self: -999, "has_condition": lambda self, c: False})()
    market = Market("c", "q", "e", "s", "es",
        datetime.now(timezone.utc) + timedelta(minutes=30),
        datetime.now(timezone.utc) + timedelta(hours=3), "y", "n", .5, .5,
        1000, 1000, False, "0.01", "Trump", "word", "speech")
    score = Score(80, 80, "YES", "B", 4, 50, 50, 50, 50, 50, 10, 0, "")
    book = BookSignal(50, .93, .95, 2, 1000, 1000)
    assert engine.risk_ok(market, score, book) == (False, "executable entry price gate")
