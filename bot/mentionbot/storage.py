from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY,
  subject TEXT NOT NULL,
  phrase TEXT NOT NULL,
  context TEXT NOT NULL,
  occurred INTEGER NOT NULL CHECK (occurred IN (0,1)),
  observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
  condition_id TEXT PRIMARY KEY,
  token_id TEXT NOT NULL,
  side TEXT NOT NULL,
  size_usd REAL NOT NULL,
  entry_price REAL NOT NULL,
  status TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  order_id TEXT
  ,peak_price REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS daily_pnl (
  day TEXT PRIMARY KEY,
  realized_usd REAL NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, path: str, journal: str):
        self.path, self.journal = path, journal
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(positions)")}
        if "peak_price" not in columns:
            self.db.execute("ALTER TABLE positions ADD COLUMN peak_price REAL NOT NULL DEFAULT 0")
            self.db.commit()

    def historical(self, subject: str, phrase: str, context: str) -> tuple[int, int]:
        row = self.db.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(occurred),0) hits
               FROM observations WHERE lower(subject)=lower(?)
               AND lower(phrase)=lower(?) AND lower(context)=lower(?)""",
            (subject, phrase, context),
        ).fetchone()
        return int(row["hits"]), int(row["n"])

    def add_observation(self, subject: str, phrase: str, context: str, occurred: bool) -> None:
        self.db.execute(
            "INSERT INTO observations(subject,phrase,context,occurred,observed_at) VALUES(?,?,?,?,?)",
            (subject, phrase, context, int(occurred), datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()

    def open_positions(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM positions WHERE status='open'"))

    def has_condition(self, condition_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM positions WHERE condition_id=? AND status='open'", (condition_id,)
        ).fetchone() is not None

    def record_position(self, condition_id: str, token_id: str, side: str,
                        size: float, price: float, order_id: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT OR REPLACE INTO positions
               (condition_id,token_id,side,size_usd,entry_price,status,opened_at,order_id,peak_price)
               VALUES(?,?,?,?,?,'open',?,?,?)""",
            (condition_id, token_id, side, size, price, now, order_id, price),
        )
        self.db.commit()
        Path(self.journal).parent.mkdir(parents=True, exist_ok=True)
        with open(self.journal, "a") as fh:
            fh.write(json.dumps({"event": "OPEN", "time": now, "condition_id": condition_id,
                                 "token_id": token_id, "side": side, "size_usd": size,
                                 "price": price, "order_id": order_id}) + "\n")

    def daily_loss(self) -> float:
        day = datetime.now(timezone.utc).date().isoformat()
        row = self.db.execute("SELECT realized_usd FROM daily_pnl WHERE day=?", (day,)).fetchone()
        return min(0.0, float(row[0])) if row else 0.0

    def update_peak(self, condition_id: str, price: float) -> float:
        self.db.execute(
            "UPDATE positions SET peak_price=MAX(peak_price, ?) WHERE condition_id=? AND status='open'",
            (price, condition_id),
        )
        self.db.commit()
        row = self.db.execute("SELECT peak_price FROM positions WHERE condition_id=?", (condition_id,)).fetchone()
        return float(row[0]) if row else price

    def close_position(self, condition_id: str, exit_price: float, order_id: str | None) -> float:
        row = self.db.execute("SELECT * FROM positions WHERE condition_id=? AND status='open'",
                              (condition_id,)).fetchone()
        if not row:
            return 0.0
        shares = float(row["size_usd"]) / float(row["entry_price"])
        pnl = shares * exit_price - float(row["size_usd"])
        self.db.execute("UPDATE positions SET status='closed' WHERE condition_id=?", (condition_id,))
        day = datetime.now(timezone.utc).date().isoformat()
        self.db.execute(
            """INSERT INTO daily_pnl(day,realized_usd) VALUES(?,?)
               ON CONFLICT(day) DO UPDATE SET realized_usd=realized_usd+excluded.realized_usd""",
            (day, pnl),
        )
        self.db.commit()
        with open(self.journal, "a") as fh:
            fh.write(json.dumps({"event": "CLOSE", "time": datetime.now(timezone.utc).isoformat(),
                                 "condition_id": condition_id, "exit_price": exit_price,
                                 "pnl_usd": pnl, "order_id": order_id}) + "\n")
        return pnl
