from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest
import yaml

from mentionbot.config import _validate
from mentionbot.engine import Engine
from mentionbot.execution import (DefinitelyNotFilled, LiveExecutor,
                                  _response_fill, capped_taker_price,
                                  maker_sell_price, profit_lock_eligible,
                                  profit_lock_floor,
                                  taker_window_open)
from mentionbot.microstructure import calculate_signal
from mentionbot.models import BookSignal, Market, MicrostructureSignal, Score
from mentionbot.market import PolymarketData
from mentionbot.news import NewsScorer
from mentionbot.scoring import combine, independent_probability, tier_for
from mentionbot.storage import Store
from mentionbot.transcript_history import (count_phrase, market_history_shape,
                                           parse_govinfo_transcript)
from mentionbot.subtitle_history import EpisodeTarget, infer_episode_target, subtitle_text
from mentionbot.youtube_history import (SOURCE_KIND, SupadataYouTubeHistory,
                                        YouTubeVideo, _published_before_event,
                                        comparable_event_query)


def test_production_option_c_limits_are_locked(monkeypatch):
    config_path = Path(__file__).parents[1] / "config.yaml"
    cfg = yaml.safe_load(config_path.read_text())
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", "0x" + "1" * 40)
    _validate(cfg)
    assert cfg["minimum_confidence"] == 65
    assert cfg["tiers"][0]["min_confidence"] == 65
    assert cfg["tiers"][-1]["max_confidence"] == 93
    assert cfg["risk"]["min_entry_price"] == .19
    assert cfg["risk"]["max_entry_price"] == .93
    assert cfg["execution"]["profit_lock"]["maker_only"] is True
    assert cfg["execution"]["profit_lock"]["max_entry_price_exclusive"] == .45

    too_cheap = deepcopy(cfg)
    too_cheap["risk"]["min_entry_price"] = .18
    with pytest.raises(ValueError, match="0.19 and 0.93"):
        _validate(too_cheap)

    too_lenient = deepcopy(cfg)
    too_lenient["minimum_confidence"] = 64
    with pytest.raises(ValueError, match="must remain 65"):
        _validate(too_lenient)


def test_confidence_band_includes_65_and_93_but_rejects_outside():
    tiers = [
        {"name": "C", "min_confidence": 65, "max_confidence": 80,
         "size_usd": 3},
        {"name": "B", "min_confidence": 80, "max_confidence": 90,
         "size_usd": 4},
        {"name": "A", "min_confidence": 90, "max_confidence": 93,
         "size_usd": 5},
    ]
    assert tier_for(64.99, tiers) == (None, 0.0)
    assert tier_for(65, tiers) == ("C", 3.0)
    assert tier_for(80, tiers) == ("B", 4.0)
    assert tier_for(90, tiers) == ("A", 5.0)
    assert tier_for(93, tiers) == ("A", 5.0)
    assert tier_for(93.01, tiers) == (None, 0.0)


def test_staged_profit_floor_never_creates_a_loss_exit():
    stages = [
        {"trigger_gain_pct": 50, "lock_gain_pct": 0},
        {"trigger_gain_pct": 100, "lock_gain_pct": 50},
        {"trigger_gain_pct": 200, "lock_gain_pct": 100},
    ]
    assert profit_lock_floor(.20, .29, stages) is None
    assert profit_lock_floor(.20, .30, stages) == pytest.approx(.20)
    assert profit_lock_floor(.20, .40, stages) == pytest.approx(.30)
    assert profit_lock_floor(.20, .60, stages) == pytest.approx(.40)


def test_profit_lock_exempts_entries_from_45_to_93_cents():
    cfg = {"enabled": True, "max_entry_price_exclusive": .45}
    assert profit_lock_eligible(.19, cfg) is True
    assert profit_lock_eligible(.4499, cfg) is True
    assert profit_lock_eligible(.45, cfg) is False
    assert profit_lock_eligible(.93, cfg) is False


def test_profit_exit_is_post_only_gtd_and_never_falls_back_to_taker():
    calls = {"maker": None, "cancel": 0, "taker": 0}

    class Client:
        def create_and_post_order(self, **kwargs):
            calls["maker"] = kwargs
            return {"orderID": "exit", "status": "live"}
        def get_order(self, order_id):
            return {"size_matched": "0", "associate_trades": []}
        def cancel_order(self, payload):
            calls["cancel"] += 1
        def create_and_post_market_order(self, **kwargs):
            calls["taker"] += 1
            pytest.fail("profit exit must never use a taker order")

    executor = LiveExecutor.__new__(LiveExecutor)
    executor.cfg = {"execution": {"profit_lock": {"maker_timeout_sec": 0},
                                   "maker_poll_sec": 0}}
    executor.client = Client()
    executor.OrderArgs = lambda **kwargs: kwargs
    executor.OrderPayload = lambda **kwargs: kwargs
    executor.Options = lambda **kwargs: kwargs
    executor.Side = type("Side", (), {"SELL": "SELL"})
    executor.OrderType = type("OrderType", (), {"GTD": "GTD"})
    book = BookSignal(50, .29, .31, 2, 100, 100)
    assert maker_sell_price(book, .30, "0.01") == .30
    with pytest.raises(DefinitelyNotFilled, match="no taker used"):
        executor.sell_maker("token", 10, book, .30, "0.01", False)
    assert calls["maker"]["post_only"] is True
    assert calls["maker"]["order_type"] == "GTD"
    assert calls["cancel"] == 1 and calls["taker"] == 0


def test_confirmed_buy_and_sell_amounts():
    buy = _response_fill({"status": "matched", "orderID": "b",
                          "makingAmount": "4", "takingAmount": "10"}, "BUY", .5)
    assert buy.price == .4 and buy.size_usd == 4
    sell = _response_fill({"status": "matched", "orderID": "s",
                           "makingAmount": "10", "takingAmount": "3"}, "SELL", .5)
    assert sell.price == .3 and sell.size_usd == 3
    with pytest.raises(RuntimeError):
        _response_fill({"status": "unmatched"}, "SELL", .5)


def test_taker_window_is_time_aware():
    now = datetime.now(timezone.utc)
    cfg = {"taker_window_hours": 2}

    def market(start):
        return Market("c", "q", "e", "s", "es", start,
            now + timedelta(hours=5), "y", "n", .5, .5,
            0, 0, False, "0.01", "Trump", "word", "speech")

    assert taker_window_open(market(now + timedelta(hours=3)), cfg, now) is False
    assert taker_window_open(market(now + timedelta(hours=1)), cfg, now) is True
    assert taker_window_open(market(now - timedelta(minutes=5)), cfg, now) is True
    assert taker_window_open(market(None), cfg, now) is False


def test_taker_cap_uses_slippage_and_absolute_price_limits_only():
    book = BookSignal(50, .39, .40, 20, 100, 100)
    cfg = {"taker_min_slippage_bps": 100, "taker_max_slippage_bps": 300}
    assert capped_taker_price(book, .93, "0.01", cfg) == .41
    expensive = BookSignal(50, .92, .93, 2, 100, 100)
    assert capped_taker_price(expensive, .93, "0.01", cfg) == .93


def test_option_c_microstructure_composite_requires_live_persistence_and_flow():
    samples = [
        (0.0, .20, .50, 60),
        (10.0, .30, .51, 65),
        (21.0, .40, .52, 70),
    ]
    signal = calculate_signal(samples, [(15.0, 8.0, .51), (20.0, 2.0, .52)])
    assert signal.ready is True
    assert signal.wobi == .40 and signal.delta_obi == .20
    assert signal.trade_flow == 1 and signal.persistence == 1
    assert signal.absorption is False and signal.score > 75
    assert calculate_signal(samples[:2], [(5.0, 1.0, .50)]).ready is False


def test_option_c_absorption_veto_detects_buying_without_price_response():
    samples = [(0.0, .30, .50, 60), (10.0, .35, .50, 60),
               (21.0, .40, .50, 60)]
    signal = calculate_signal(samples, [(10.0, 5.0, .50), (20.0, 5.0, .50)])
    assert signal.ready and signal.absorption


def test_option_c_weights_sum_to_confidence_without_changing_direction():
    cfg = {
        "probability_weights": {"historical_context": .35, "market_prior": .14},
        "timing_weights": {"order_book_imbalance": .20, "momentum": .14},
        "book_confidence": {},
        "option_c_confidence_weights": {
            "historical_mentions": .30, "event_context": .20,
            "market_prior": .15, "microstructure": .25, "momentum": .10,
        },
        "tiers": [{"name": "C", "min_confidence": 70,
                   "max_confidence": 80, "size_usd": 3}],
    }
    market = Market("c", "q", "e", "s", "es", None,
        datetime.now(timezone.utc) + timedelta(hours=2), "y", "n", .5, .5,
        0, 0, False, "0.01", "Trump", "word", "speech")
    yes_book = BookSignal(90, .39, .40, 2, 100, 100)
    no_book = BookSignal(10, .59, .60, 2, 100, 100)
    micro = MicrostructureSignal(score=90, ready=True, samples=5)
    score = combine(market, yes_book, no_book, 70, 60, 80, cfg,
                    "exact phrase/context", 12, microstructure=micro)
    assert independent_probability(70, 12) == 70
    assert score.side == "YES" and score.independent_probability == 70
    assert score.confidence == 80.5


def test_unfilled_maker_outside_two_hours_never_calls_taker(monkeypatch):
    now = datetime.now(timezone.utc)
    market = Market("c", "q", "e", "s", "es", now + timedelta(hours=3),
        now + timedelta(hours=5), "y", "n", .5, .5,
        0, 0, False, "0.01", "Trump", "word", "speech")
    book = BookSignal(50, .39, .40, 2, 100, 100)
    calls = {"maker": None, "taker": 0, "cancel": 0}

    class Client:
        def create_and_post_order(self, **kwargs):
            calls["maker"] = kwargs
            return {"success": True, "orderID": "maker", "status": "live"}
        def get_order(self, order_id):
            return {"size_matched": "0", "associate_trades": []}
        def cancel_order(self, payload): calls["cancel"] += 1
        def create_and_post_market_order(self, **kwargs):
            calls["taker"] += 1
            pytest.fail("taker must not run outside two hours")

    executor = LiveExecutor.__new__(LiveExecutor)
    executor.cfg = {"execution": {"price_buffer_ticks": 1,
        "maker_timeout_sec": 0, "maker_poll_sec": 0,
        "taker_window_hours": 2}, "risk": {"max_entry_price": .93}}
    executor.client = Client()
    executor.OrderArgs = lambda **kwargs: kwargs
    executor.OrderPayload = lambda **kwargs: kwargs
    executor.Options = lambda **kwargs: kwargs
    executor.OrderType = type("OrderType", (), {"GTD": "GTD", "FOK": "FOK"})
    executor.Side = type("Side", (), {"BUY": "BUY"})
    monkeypatch.setattr("mentionbot.execution.time.time", lambda: 1000)

    with pytest.raises(DefinitelyNotFilled, match="outside two-hour"):
        executor.buy(market, "YES", 3, book,
                     refresh_for_taker=lambda: pytest.fail("no refresh"))
    assert calls["maker"]["order_type"] == "GTD"
    assert calls["maker"]["order_args"]["expiration"] == 1060
    assert calls["cancel"] == 1 and calls["taker"] == 0


def test_partial_maker_fill_is_not_topped_up_with_taker():
    now = datetime.now(timezone.utc)
    market = Market("c", "q", "e", "s", "es", now + timedelta(hours=1),
        now + timedelta(hours=5), "y", "n", .5, .5,
        0, 0, False, "0.01", "Trump", "word", "speech")
    book = BookSignal(50, .39, .40, 2, 100, 100)

    class Client:
        def create_and_post_order(self, **kwargs):
            return {"success": True, "orderID": "maker", "status": "live"}
        def get_order(self, order_id):
            return {"size_matched": "2", "associate_trades": ["trade"]}
        def cancel_order(self, payload): pass
        def create_and_post_market_order(self, **kwargs):
            pytest.fail("partial maker fill must never be topped up")

    executor = LiveExecutor.__new__(LiveExecutor)
    executor.cfg = {"execution": {"price_buffer_ticks": 1,
        "maker_timeout_sec": 0, "maker_poll_sec": 0,
        "taker_window_hours": 2}, "risk": {"max_entry_price": .93}}
    executor.client = Client()
    executor.OrderArgs = lambda **kwargs: kwargs
    executor.OrderPayload = lambda **kwargs: kwargs
    executor.Options = lambda **kwargs: kwargs
    executor.OrderType = type("OrderType", (), {"GTD": "GTD", "FOK": "FOK"})
    executor.Side = type("Side", (), {"BUY": "BUY"})
    executor._wait_for_confirmation = lambda trade_ids: None
    fill = executor.buy(market, "YES", 3, book,
                        refresh_for_taker=lambda: pytest.fail("no refresh"))
    assert fill.order_id == "maker" and fill.shares == 2


def test_inside_two_hours_uses_refreshed_capped_fok():
    now = datetime.now(timezone.utc)
    market = Market("c", "q", "e", "s", "es", now + timedelta(hours=1),
        now + timedelta(hours=5), "y", "n", .5, .5,
        0, 0, False, "0.01", "Trump", "word", "speech")
    original = BookSignal(50, .39, .40, 2, 100, 100)
    refreshed = BookSignal(50, .40, .41, 2, 100, 100)
    calls = {"taker": None, "confirmed": None}

    class Client:
        def create_and_post_order(self, **kwargs):
            return {"success": True, "orderID": "maker", "status": "live"}
        def get_order(self, order_id):
            return {"size_matched": "0", "associate_trades": []}
        def cancel_order(self, payload): pass
        def create_and_post_market_order(self, **kwargs):
            calls["taker"] = kwargs
            return {"success": True, "orderID": "fok", "status": "matched",
                    "makingAmount": "3", "takingAmount": "7.317073",
                    "tradeIDs": ["trade"]}

    executor = LiveExecutor.__new__(LiveExecutor)
    executor.cfg = {"execution": {"price_buffer_ticks": 1,
        "maker_timeout_sec": 0, "maker_poll_sec": 0,
        "taker_window_hours": 2, "taker_min_slippage_bps": 100,
        "taker_max_slippage_bps": 300}, "risk": {"max_entry_price": .93}}
    executor.client = Client()
    executor.OrderArgs = lambda **kwargs: kwargs
    executor.MarketOrderArgs = lambda **kwargs: kwargs
    executor.OrderPayload = lambda **kwargs: kwargs
    executor.Options = lambda **kwargs: kwargs
    executor.OrderType = type("OrderType", (), {"GTD": "GTD", "FOK": "FOK"})
    executor.Side = type("Side", (), {"BUY": "BUY"})
    executor._confirm_response = lambda result: calls.update(confirmed=result)
    fill = executor.buy(market, "YES", 3, original,
                        refresh_for_taker=lambda: refreshed)
    assert calls["taker"]["order_type"] == "FOK"
    assert calls["taker"]["order_args"]["order_type"] == "FOK"
    assert calls["taker"]["order_args"]["price"] == .41
    assert calls["confirmed"]["orderID"] == "fok"
    assert round(fill.size_usd, 2) == 3


def test_trade_finality_retries_transient_reads_until_confirmed(monkeypatch):
    replies = iter((RuntimeError("temporary CLOB failure"),
                    [{"id": "trade", "status": "MATCHED"}],
                    [{"id": "trade", "status": "CONFIRMED"}]))

    class Client:
        def get_trades(self, *args, **kwargs):
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

    executor = LiveExecutor.__new__(LiveExecutor)
    executor.cfg = {"execution": {
        "trade_confirmation_timeout_sec": 45,
        "trade_confirmation_poll_sec": 1,
    }}
    executor.client = Client()
    executor.TradeParams = lambda **kwargs: kwargs
    clock = iter((0, 1, 2, 3, 4, 5))
    monkeypatch.setattr("mentionbot.execution.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("mentionbot.execution.time.sleep", lambda _: None)
    executor._wait_for_confirmation(["trade"])


def test_trade_finality_rejects_terminal_failure(monkeypatch):
    class Client:
        def get_trades(self, *args, **kwargs):
            return [{"id": "trade", "status": "FAILED"}]

    executor = LiveExecutor.__new__(LiveExecutor)
    executor.cfg = {"execution": {
        "trade_confirmation_timeout_sec": 45,
        "trade_confirmation_poll_sec": 1,
    }}
    executor.client = Client()
    executor.TradeParams = lambda **kwargs: kwargs
    monkeypatch.setattr("mentionbot.execution.time.sleep", lambda _: None)
    with pytest.raises(DefinitelyNotFilled, match="failed permanently"):
        executor._wait_for_confirmation(["trade"])


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


def test_repeated_condition_entry_is_rejected(tmp_path):
    store = Store(str(tmp_path / "state.db"), str(tmp_path / "journal.jsonl"))
    store.record_position("c", "t", "YES", 3, .3, "first", "Question",
                          "2030-01-01T00:00:00+00:00", "0.01", False,
                          "Trump", "word")
    with pytest.raises(RuntimeError, match="condition already entered"):
        store.record_position("c", "t", "YES", 3, .3, "second", "Question",
                              "2030-01-01T00:00:00+00:00", "0.01", False,
                              "Trump", "other")
    positions = store.open_positions()
    assert len(positions) == 1 and positions[0]["order_id"] == "first"


def test_pending_reservation_blocks_duplicate_and_finalizes_atomically(tmp_path):
    store = Store(str(tmp_path / "state.db"), str(tmp_path / "journal.jsonl"))
    store.reserve_order("c", "t", "YES", 3, "Question",
                        "2030-01-01T00:00:00Z", "0.01", False,
                        "Trump", "word")
    assert store.entry_allowed("c", "Trump", "word") == (
        False, "condition already entered")
    assert len(store.pending_orders()) == 1
    store.update_pending_order("c", "order-1", "matched")
    store.record_position("c", "t", "YES", 3, .3, "order-1", "Question",
                          "2030-01-01T00:00:00Z", "0.01", False,
                          "Trump", "word")
    assert store.pending_orders() == []
    assert store.open_positions()[0]["order_id"] == "order-1"


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
    with pytest.raises(RuntimeError, match="condition already entered"):
        store.record_position("c", "t", "YES", 4, .4, "new", "Question",
                              "2030-01-01T00:00:00Z", "0.01", False)
    assert len(store.open_positions()) == 1


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


def test_supadata_youtube_evidence_is_excluded_from_live_history(tmp_path):
    store = Store(str(tmp_path / "history.db"), str(tmp_path / "history.jsonl"))
    store.add_transcript_mention(
        "youtube:abc12345", "Anyone", "security", "interview", 4,
        "2026-08-01", "Podcast episode", "https://youtube.com/watch?v=abc12345",
        source_kind=SOURCE_KIND)
    assert store.historical_pattern(
        "Anyone", "security", "interview", allow_broad_fallback=False)[:2] == (0, 0)
    assert store.shadow_transcript_pattern(
        "Anyone", "security", "interview")[:2] == (1, 1)


def test_supadata_refresh_counts_and_discards_transcript(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "history.db"), str(tmp_path / "history.jsonl"))
    cfg = {"youtube_history": {"enabled": True, "minimum_interval_hours": 24,
                                "target_refresh_hours": 720,
                                "max_events_per_refresh": 1,
                                "max_videos_per_event": 2}}
    monkeypatch.setenv("SUPADATA_API_KEY", "test-key")
    history = SupadataYouTubeHistory(cfg)
    history._search = lambda query: [type("V", (), {
        "video_id": "abc12345", "title": "All-In Podcast episode",
        "upload_date": "2026-08-01T00:00:00Z",
        "url": "https://youtube.com/watch?v=abc12345"})()]
    history._transcript = lambda video: "Security first. More security."
    market = Market("c", 'Will anyone say "Security"?',
        "What will be said on the next All-In Podcast?", "s", "es", None,
        datetime.now(timezone.utc) + timedelta(hours=2), "y", "n", .5, .5,
        1000, 1000, False, "0.01", "Anyone", "Security", "interview")
    assert comparable_event_query(market) == "All-In Podcast"
    assert history.refresh([market], store) == (1, 1, 2)
    assert store.shadow_transcript_pattern(
        "Anyone", "Security", "interview")[:2] == (1, 1)


def test_option_c_calibration_is_segment_specific_and_requires_30(tmp_path):
    store = Store(str(tmp_path / "history.db"), str(tmp_path / "history.jsonl"))
    for number in range(29):
        condition = f"all-in-{number}"
        store.record_youtube_shadow_prediction(
            condition, "interview:all-in-podcast", 50, 90, 5)
        store.resolve_youtube_shadow_prediction(condition, True)
    assert not store.youtube_calibration("interview:all-in-podcast")["passed"]
    condition = "all-in-29"
    store.record_youtube_shadow_prediction(
        condition, "interview:all-in-podcast", 50, 90, 5)
    store.resolve_youtube_shadow_prediction(condition, True)
    result = store.youtube_calibration("interview:all-in-podcast")
    assert result["passed"] and result["optionCBrier"] < result["baselineBrier"]
    assert not store.youtube_calibration("interview:joe-rogan")["passed"]


def test_option_c_uses_29_10_10_only_when_explicitly_active():
    cfg = {"probability_weights": {"historical_context": .35,
                                    "market_prior": .14},
           "timing_weights": {"order_book_imbalance": .20, "momentum": .14},
           "tiers": [{"name": "C", "min_confidence": 65,
                      "max_confidence": 80, "size_usd": 3}]}
    market = Market("c", "q", "e", "s", "es", None,
        datetime.now(timezone.utc) + timedelta(hours=2), "y", "n", .5, .5,
        1000, 1000, False, "0.01", "Anyone", "word", "interview")
    yes_book = BookSignal(50, .39, .40, 2, 100, 100)
    no_book = BookSignal(50, .59, .60, 2, 100, 100)
    inactive = combine(market, yes_book, no_book, 80, 60, 50,
                       cfg, "exact", 12)
    weights = {"historical_context": .29, "youtube_history": .10,
               "market_prior": .10}
    active = combine(
        market, yes_book, no_book, 80, 60, 50, cfg, "exact", 12,
        youtube_history=90, youtube_samples=5,
        probability_weights_override=weights)
    assert inactive.yes_probability == (80 * .35 + 60 * .14) / .49
    assert active.yes_probability == (80 * .29 + 90 * .10 + 60 * .10) / .49
    assert active.yes_probability != inactive.yes_probability


def test_exact_subject_phrase_can_fallback_across_non_tv_contexts(tmp_path):
    store = Store(str(tmp_path / "history.db"), str(tmp_path / "history.jsonl"))
    store.add_transcript_mention(
        "govinfo:1", "Trump", "Gaza", "speech", 1,
        "2026-08-01", "Remarks", "https://www.govinfo.gov/example",
        source_kind="govinfo")

    hits, total, scope = store.historical_pattern(
        "Trump", "Gaza", "other", allow_broad_fallback=False)

    assert (hits, total) == (1, 1)
    assert "cross-context" in scope


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
    weak = combine(market, weak_book, no_book, 80, 65, 60, cfg, "exact", 1)
    strong = combine(market, strong_book, no_book, 80, 65, 60, cfg, "exact", 1)
    assert weak.yes_probability == strong.yes_probability
    assert weak.timing_score < strong.timing_score


def test_probability_uses_history_and_market_prior_only():
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
    score = combine(market, yes_book, no_book, 80, 65, 60,
                    cfg, "exact", 12)
    assert score.yes_probability == (80 * .35 + 65 * .14) / (.35 + .14)
    assert score.tier == "C"


def test_persistent_book_confirmation_is_bounded_and_arbitrage_is_absent():
    cfg = {"probability_weights": {"historical_context": .35,
                                    "news_live_impact": .25, "market_prior": .14},
           "timing_weights": {"order_book_imbalance": .20, "momentum": .14},
           "book_confidence": {"required_samples": 3, "sample_window": 5,
                               "adjustment_scale": .10,
                               "max_adjustment_points": 5},
           "tiers": [{"name": "C", "min_confidence": 65,
                      "max_confidence": 80, "size_usd": 3}]}
    market = Market("c", "q", "e", "s", "es", None,
        datetime.now(timezone.utc) + timedelta(hours=2), "y", "n", .5, .5,
        1000, 1000, False, "0.01", "Trump", "word", "speech")
    yes_book = BookSignal(60, .44, .45, 2, 100, 100)
    no_book = BookSignal(40, .47, .48, 2, 100, 100)
    base = combine(market, yes_book, no_book, 75, 60, 60,
                   cfg, "exact", 12, 100, 2)
    score = combine(market, yes_book, no_book, 75, 60, 60,
                    cfg, "exact", 12, 100, 3)
    expected_probability = (75 * .35 + 60 * .14) / (.35 + .14)
    assert score.yes_probability == expected_probability
    assert base.book_adjustment == 0
    assert score.book_adjustment == 5
    assert score.confidence <= (expected_probability + 5)
    # Market-derived confirmation may change the displayed tier confidence,
    # but it must never manufacture independent edge against the same book.
    assert score.model_edge_pct == base.model_edge_pct


def test_missing_youtube_publication_date_is_not_historical_evidence():
    market = Market("c", "q", "e", "s", "es",
        datetime.now(timezone.utc) + timedelta(hours=2),
        datetime.now(timezone.utc) + timedelta(hours=3), "y", "n", .5, .5,
        1000, 1000, False, "0.01", "Anyone", "word", "interview")
    undated = YouTubeVideo("abc12345", "Comparable episode", "",
                           "https://youtube.com/watch?v=abc12345")
    assert _published_before_event(undated, market) is False


def test_supadata_async_transcript_job_is_polled(monkeypatch):
    history = SupadataYouTubeHistory({"youtube_history": {
        "enabled": True, "job_poll_timeout_sec": 5,
        "job_poll_interval_sec": 1}})
    replies = iter(({"jobId": "job-1"}, {"content": "Security was mentioned."}))
    paths = []
    history._get = lambda path, params: (paths.append(path), next(replies))[1]
    monkeypatch.setattr("mentionbot.youtube_history.time.sleep", lambda _: None)
    video = YouTubeVideo("abc12345", "Episode", "2026-08-01T00:00:00Z",
                         "https://youtube.com/watch?v=abc12345")
    assert history._transcript(video) == "Security was mentioned."
    assert paths == ["/transcript", "/transcript/job-1"]


def test_closed_positions_use_documented_pagination():
    calls = []

    class Response:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    class Session:
        def get(self, url, params, timeout):
            calls.append(dict(params))
            count = 50 if params["offset"] == 0 else 2
            return Response([{"asset": str(i)} for i in range(count)])

    data = PolymarketData.__new__(PolymarketData)
    data.data_api = "https://data-api.polymarket.com"
    data.session = Session()
    rows = data.portfolio_positions("0x" + "1" * 40, closed=True)
    assert len(rows) == 52
    assert calls == [
        {"user": "0x" + "1" * 40, "limit": 50, "offset": 0},
        {"user": "0x" + "1" * 40, "limit": 50, "offset": 50},
    ]


def test_executable_depth_counts_only_asks_within_taker_cap():
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"asks": [
                {"price": ".40", "size": "5"},
                {"price": ".41", "size": "10"},
                {"price": ".42", "size": "100"},
            ]}

    data = PolymarketData.__new__(PolymarketData)
    data.clob = "https://clob.polymarket.com"
    data.session = type("Session", (), {
        "get": lambda self, *args, **kwargs: Response()})()
    assert data.executable_ask_depth("token", .41) == pytest.approx(6.1)


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


def test_history_uses_comparable_events_not_calendar_weeks(tmp_path):
    assert market_history_shape('Will Trump say "war" 5+ times this week?') == ("event", 5)
    store = Store(str(tmp_path / "weekly.db"), str(tmp_path / "journal.jsonl"))
    for doc, date, count in (("d1", "2026-08-03", 3), ("d2", "2026-08-04", 2),
                             ("d3", "2026-07-27", 4)):
        store.add_transcript_mention(doc, "Trump", "war", "speech", count,
                                     date, "Remarks", f"https://example/{doc}")
    hits, total, scope = store.historical_pattern(
        "Trump", "war", "speech", period="week", min_mentions=5)
    assert (hits, total) == (0, 3)
    assert scope == "official transcript phrase/context"


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
        "max_hours_before_event": 8, "min_entry_price": .19, "max_entry_price": .93,
        "max_positions_per_condition": 1}}
    engine.store = type("S", (), {"open_positions": lambda self: [],
        "daily_loss": lambda self: -999,
        "open_position_count": lambda self, c: 0,
        "entry_allowed": lambda self, c, s, p: (True, "ok")})()
    market = Market("c", "q", "e", "s", "es",
        datetime.now(timezone.utc) + timedelta(minutes=30),
        datetime.now(timezone.utc) + timedelta(hours=3), "y", "n", .5, .5,
        1000, 1000, False, "0.01", "Trump", "word", "speech")
    score = Score(80, 80, "YES", "B", 4, 50, 50, 50, 50, 10, "")
    book = BookSignal(50, .93, .95, 2, 1000, 1000)
    assert engine.risk_ok(market, score, book) == (False, "executable entry price gate")


def test_risk_requires_enough_near_ask_depth_for_the_order():
    engine = Engine.__new__(Engine)
    engine.cfg = {"risk": {"kill_switch_file": "/never", "max_open_positions": 5,
        "min_liquidity_usd": 0, "min_volume_usd": 0, "max_spread_pct": 100,
        "min_timing_score": 0, "min_model_edge_pct": 6,
        "require_known_event_start": False, "max_hours_before_event": 24,
        "min_entry_price": .19, "max_entry_price": .93,
        "max_positions_per_condition": 1}}
    engine.store = type("S", (), {"open_positions": lambda self: [],
        "entry_allowed": lambda self, c, s, p: (True, "ok")})()
    market = Market("c", "q", "e", "s", "es", None,
        datetime.now(timezone.utc) + timedelta(hours=3), "y", "n", .5, .5,
        0, 0, False, "0.01", "Trump", "word", "speech")
    score = Score(75, 75, "YES", "C", 3, 50, 50, 50, 50, 10, "")
    book = BookSignal(50, .39, .40, 2, 100, 2.99)
    assert engine.risk_ok(market, score, book) == (
        False, "insufficient executable ask depth")


def test_risk_does_not_reject_wide_spread():
    engine = Engine.__new__(Engine)
    engine.cfg = {"risk": {"kill_switch_file": "/never", "max_open_positions": 5,
        "min_liquidity_usd": 0, "min_volume_usd": 0, "max_spread_pct": 1,
        "min_timing_score": 0, "min_model_edge_pct": 6,
        "require_known_event_start": False, "max_hours_before_event": 24,
        "min_entry_price": .19, "max_entry_price": .93,
        "max_positions_per_condition": 1}}
    engine.store = type("S", (), {"open_positions": lambda self: [],
        "entry_allowed": lambda self, c, s, p: (True, "ok")})()
    market = Market("c", "q", "e", "s", "es", None,
        datetime.now(timezone.utc) + timedelta(hours=3), "y", "n", .5, .5,
        0, 0, False, "0.01", "Trump", "word", "speech")
    score = Score(75, 75, "YES", "C", 3, 50, 50, 50, 50, 10, "")
    book = BookSignal(50, .16, .40, 150, 100, 100)
    assert engine.risk_ok(market, score, book) == (True, "ok")


def test_option_c_requires_three_point_independent_mispricing():
    engine = Engine.__new__(Engine)
    engine.cfg = {"risk": {"kill_switch_file": "/never",
        "max_open_positions": 5, "min_liquidity_usd": 0,
        "min_volume_usd": 0, "min_timing_score": 0,
        "require_known_event_start": False, "max_hours_before_event": 24,
        "min_entry_price": .19, "max_entry_price": .93,
        "max_positions_per_condition": 1}}
    engine.store = type("S", (), {"open_positions": lambda self: [],
        "entry_allowed": lambda self, c, s, p: (True, "ok")})()
    market = Market("c", "q", "e", "s", "es", None,
        datetime.now(timezone.utc) + timedelta(hours=3), "y", "n", .5, .5,
        0, 0, False, "0.01", "Trump", "word", "speech")
    rejected = Score(75, 75, "YES", "C", 3, 50, 50, 50, 0, -20, "",
                     independent_probability=75)
    expensive = BookSignal(50, .89, .90, 2, 100, 100)
    assert engine.risk_ok(market, rejected, expensive) == (
        False, "independent model mispricing below 3%")
    accepted = Score(75, 75, "YES", "C", 3, 50, 50, 50, 0, 5, "",
                     independent_probability=75)
    cheap = BookSignal(50, .69, .70, 2, 100, 100)
    assert engine.risk_ok(market, accepted, cheap) == (True, "ok")


def test_discovery_prioritizes_tradeable_candidates_before_outside_window():
    data = PolymarketData.__new__(PolymarketData)
    data.cfg = {"max_candidates_per_cycle": 3, "risk": {
        "max_hours_before_event": 4, "min_liquidity_usd": 800,
        "min_volume_usd": 800}}
    now = datetime.now(timezone.utc)

    def candidate(condition, start, liquidity, volume):
        return Market(condition, condition, "event", "slug", "event-slug",
            start, now + timedelta(hours=12), "yes", "no", .5, .5,
            liquidity, volume, False, "0.01", "Trump", "word", "speech")

    liquid_near = candidate("liquid-near", now + timedelta(hours=2), 1200, 3000)
    thin_near = candidate("thin-near", now + timedelta(hours=1), 200, 3000)
    liquid_unknown = candidate("liquid-unknown", None, 1000, 2000)
    liquid_late = candidate("liquid-late", now + timedelta(hours=8), 5000, 5000)

    ranked = data._rank_candidates([
        liquid_late, thin_near, liquid_unknown, liquid_near])
    assert [market.condition_id for market in ranked] == [
        "liquid-near", "liquid-unknown", "thin-near"]


def test_entry_locks_are_lifetime_per_condition_and_daily_per_word(tmp_path):
    store = Store(str(tmp_path / "locks.db"), str(tmp_path / "journal.jsonl"))
    today = datetime.now(timezone.utc).date().isoformat()
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    assert store.entry_allowed("c1", "Donald Trump", "War | Peace", today) == (
        True, "ok")
    store.record_position("c1", "t", "NO", 3, .3, "o", "Question",
                          "2030-01-01T00:00:00Z", "0.01", False,
                          "Donald Trump", "Peace | War")
    assert store.entry_allowed("c1", "Donald Trump", "Other", tomorrow) == (
        False, "condition already entered")
    assert store.entry_allowed("c2", "Donald Trump", "War | Peace", today) == (
        False, "word mention already entered today")
    assert store.entry_allowed("c2", "Donald Trump", "War | Peace", tomorrow) == (
        True, "ok")


def test_resolution_settlement_releases_slot_without_early_sell(tmp_path):
    store = Store(str(tmp_path / "settle.db"), str(tmp_path / "journal.jsonl"))
    store.record_position("c", "t", "YES", 3, .3, "o", "Question",
                          "2030-01-01T00:00:00Z", "0.01", False,
                          "Trump", "word")
    position_id = store.open_positions()[0]["position_id"]
    pnl = store.settle_position(position_id, 1.0, "redeemable", "market")
    assert round(pnl, 2) == 7.0
    assert store.open_positions() == []
    row = store.redeemable_positions()[0]
    assert row["status"] == "resolved" and row["redemption_status"] == "redeemable"


def test_dashboard_status_is_atomic_and_contains_operational_evidence(tmp_path):
    store = Store(str(tmp_path / "state.db"), str(tmp_path / "journal.jsonl"))
    store.add_observation("Trump", "Security", "speech", True, "resolved")
    store.record_position("c", "t", "YES", 3, .3, "o", "Question",
                          "2030-01-01T00:00:00+00:00", "0.01", False)
    engine = Engine.__new__(Engine)
    engine.cfg = {"mode": "live", "minimum_confidence": 65,
                  "risk": {"min_model_edge_pct": 6, "min_timing_score": 45,
                           "max_hours_before_event": 8},
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
    engine.cfg = {"minimum_confidence": 65,
                  "risk": {"min_model_edge_pct": 6, "min_timing_score": 45,
                           "max_hours_before_event": 8}}
    engine._control_path = tmp_path / "control.json"
    engine._control_path.write_text('{"minimumConfidence": 10}')
    control = engine._load_runtime_control()
    assert control["paused"] is True
    assert control["minimumConfidence"] == 65


def test_legacy_model_edge_control_is_discarded(tmp_path):
    engine = Engine.__new__(Engine)
    engine.cfg = {"minimum_confidence": 65,
                  "risk": {"min_timing_score": 0,
                           "max_hours_before_event": 24}}
    engine._control_path = tmp_path / "control.json"
    engine._control_path.write_text(
        '{"paused":false,"minimumConfidence":65,'
        '"minModelEdgePct":20,"minTimingScore":0,'
        '"maxHoursBeforeEvent":24}')
    control = engine._load_runtime_control()
    assert control["paused"] is False
    assert control["minimumConfidence"] == 65
    assert "minModelEdgePct" not in control
