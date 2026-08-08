from datetime import datetime, timedelta, timezone

import pytest

from mentionbot.engine import Engine
from mentionbot.execution import _response_fill
from mentionbot.models import BookSignal, Market, Score
from mentionbot.storage import Store


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
    pnl = store.close_position("c", .5, "sell", 4)
    row = store.open_positions()[0]
    assert round(pnl, 4) == .4
    assert round(row["size_usd"], 4) == 2.4


def test_risk_uses_executable_ask_not_cached_gamma_price():
    engine = Engine.__new__(Engine)
    engine.cfg = {"risk": {"kill_switch_file": "/never", "max_open_positions": 5,
        "max_deployed_usd": 20, "daily_loss_limit_usd": 10,
        "min_liquidity_usd": 800, "min_volume_usd": 800, "max_spread_pct": 12,
        "min_model_edge_pct": 6, "require_known_event_start": True,
        "max_hours_before_event": 2, "min_entry_price": .16, "max_entry_price": .93,
        "one_position_per_condition": True}}
    engine.store = type("S", (), {"open_positions": lambda self: [],
        "daily_loss": lambda self: 0, "has_condition": lambda self, c: False})()
    market = Market("c", "q", "e", "s", "es",
        datetime.now(timezone.utc) + timedelta(minutes=30),
        datetime.now(timezone.utc) + timedelta(hours=3), "y", "n", .5, .5,
        1000, 1000, False, "0.01", "Trump", "word", "speech")
    score = Score(80, 80, "YES", "B", 4, 50, 50, 50, 50, 50, 10, 0, "")
    book = BookSignal(50, .93, .95, 2, 1000, 1000)
    assert engine.risk_ok(market, score, book) == (False, "executable entry price gate")
