from __future__ import annotations

import json
import re
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
  observed_at TEXT NOT NULL,
  condition_id TEXT
);
CREATE TABLE IF NOT EXISTS positions (
  position_id INTEGER PRIMARY KEY AUTOINCREMENT,
  condition_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  side TEXT NOT NULL,
  size_usd REAL NOT NULL,
  entry_price REAL NOT NULL,
  status TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  order_id TEXT
  ,peak_price REAL NOT NULL DEFAULT 0
  ,question TEXT NOT NULL DEFAULT ''
  ,end_date TEXT
  ,tick_size TEXT NOT NULL DEFAULT '0.01'
  ,neg_risk INTEGER NOT NULL DEFAULT 0
  ,subject_key TEXT NOT NULL DEFAULT ''
  ,phrase_key TEXT NOT NULL DEFAULT ''
  ,closed_at TEXT
  ,exit_price REAL
  ,realized_pnl REAL
  ,redemption_status TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS entry_locks (
  condition_id TEXT PRIMARY KEY,
  subject_key TEXT NOT NULL,
  phrase_key TEXT NOT NULL,
  entry_day TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_pnl (
  day TEXT PRIMARY KEY,
  realized_usd REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transcript_mentions (
  document_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  phrase TEXT NOT NULL,
  context TEXT NOT NULL,
  mention_count INTEGER NOT NULL CHECK (mention_count >= 0),
  document_date TEXT NOT NULL,
  title TEXT NOT NULL,
  source_url TEXT NOT NULL,
  source_kind TEXT NOT NULL DEFAULT 'govinfo',
  observed_at TEXT NOT NULL,
  PRIMARY KEY (document_id, subject, phrase)
);
CREATE TABLE IF NOT EXISTS transcript_refreshes (
  subject TEXT NOT NULL,
  phrase TEXT NOT NULL,
  refreshed_at TEXT NOT NULL,
  documents_scanned INTEGER NOT NULL,
  source_kind TEXT NOT NULL DEFAULT 'govinfo',
  PRIMARY KEY (subject, phrase, source_kind)
);
CREATE TABLE IF NOT EXISTS transcript_source_refreshes (
  subject TEXT NOT NULL,
  phrase TEXT NOT NULL,
  refreshed_at TEXT NOT NULL,
  documents_scanned INTEGER NOT NULL,
  source_kind TEXT NOT NULL,
  PRIMARY KEY (subject, phrase, source_kind)
);
CREATE TABLE IF NOT EXISTS youtube_shadow_predictions (
  condition_id TEXT PRIMARY KEY,
  segment TEXT NOT NULL,
  baseline_probability REAL NOT NULL,
  option_c_probability REAL NOT NULL,
  transcript_samples INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  resolved_outcome INTEGER CHECK (resolved_outcome IN (0,1)),
  resolved_at TEXT
);
"""


class Store:
    def __init__(self, path: str, journal: str):
        self.path, self.journal = path, journal
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(positions)")}
        migrations = {
            "peak_price": "ALTER TABLE positions ADD COLUMN peak_price REAL NOT NULL DEFAULT 0",
            "question": "ALTER TABLE positions ADD COLUMN question TEXT NOT NULL DEFAULT ''",
            "end_date": "ALTER TABLE positions ADD COLUMN end_date TEXT",
            "tick_size": "ALTER TABLE positions ADD COLUMN tick_size TEXT NOT NULL DEFAULT '0.01'",
            "neg_risk": "ALTER TABLE positions ADD COLUMN neg_risk INTEGER NOT NULL DEFAULT 0",
            "subject_key": "ALTER TABLE positions ADD COLUMN subject_key TEXT NOT NULL DEFAULT ''",
            "phrase_key": "ALTER TABLE positions ADD COLUMN phrase_key TEXT NOT NULL DEFAULT ''",
            "closed_at": "ALTER TABLE positions ADD COLUMN closed_at TEXT",
            "exit_price": "ALTER TABLE positions ADD COLUMN exit_price REAL",
            "realized_pnl": "ALTER TABLE positions ADD COLUMN realized_pnl REAL",
            "redemption_status": "ALTER TABLE positions ADD COLUMN redemption_status TEXT NOT NULL DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in columns:
                self.db.execute(statement)
        self.db.commit()
        position_columns = {row[1] for row in self.db.execute("PRAGMA table_info(positions)")}
        if "position_id" not in position_columns:
            self._migrate_positions_for_multiple_entries()
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS positions_condition_status_idx "
            "ON positions(condition_id, status)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS entry_locks_word_day_idx "
            "ON entry_locks(subject_key, phrase_key, entry_day)"
        )
        self.db.commit()
        observation_columns = {row[1] for row in self.db.execute("PRAGMA table_info(observations)")}
        if "condition_id" not in observation_columns:
            self.db.execute("ALTER TABLE observations ADD COLUMN condition_id TEXT")
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS observations_condition_id_uq "
            "ON observations(condition_id) WHERE condition_id IS NOT NULL"
        )
        self.db.commit()
        transcript_columns = {row[1] for row in self.db.execute(
            "PRAGMA table_info(transcript_mentions)")}
        if "document_date" not in transcript_columns:
            self.db.execute(
                "ALTER TABLE transcript_mentions ADD COLUMN document_date TEXT NOT NULL DEFAULT ''")
            self.db.commit()
        if "source_kind" not in transcript_columns:
            self.db.execute(
                "ALTER TABLE transcript_mentions ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'govinfo'")
            self.db.commit()

    def _migrate_positions_for_multiple_entries(self) -> None:
        """Replace the legacy condition-keyed table without losing positions."""
        self.db.execute("ALTER TABLE positions RENAME TO positions_legacy")
        self.db.execute(
            """CREATE TABLE positions (
              position_id INTEGER PRIMARY KEY AUTOINCREMENT,
              condition_id TEXT NOT NULL,
              token_id TEXT NOT NULL,
              side TEXT NOT NULL,
              size_usd REAL NOT NULL,
              entry_price REAL NOT NULL,
              status TEXT NOT NULL,
              opened_at TEXT NOT NULL,
              order_id TEXT,
              peak_price REAL NOT NULL DEFAULT 0,
              question TEXT NOT NULL DEFAULT '',
              end_date TEXT,
              tick_size TEXT NOT NULL DEFAULT '0.01',
              neg_risk INTEGER NOT NULL DEFAULT 0
              ,subject_key TEXT NOT NULL DEFAULT ''
              ,phrase_key TEXT NOT NULL DEFAULT ''
              ,closed_at TEXT
              ,exit_price REAL
              ,realized_pnl REAL
              ,redemption_status TEXT NOT NULL DEFAULT ''
            )"""
        )
        self.db.execute(
            """INSERT INTO positions
               (condition_id,token_id,side,size_usd,entry_price,status,opened_at,
                order_id,peak_price,question,end_date,tick_size,neg_risk)
               SELECT condition_id,token_id,side,size_usd,entry_price,status,opened_at,
                      order_id,peak_price,question,end_date,tick_size,neg_risk
               FROM positions_legacy"""
        )
        self.db.execute("DROP TABLE positions_legacy")
        self.db.commit()

    def historical(self, subject: str, phrase: str, context: str) -> tuple[int, int]:
        row = self.db.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(occurred),0) hits
               FROM observations WHERE lower(subject)=lower(?)
               AND lower(phrase)=lower(?) AND lower(context)=lower(?)""",
            (subject, phrase, context),
        ).fetchone()
        return int(row["hits"]), int(row["n"])

    def historical_pattern(self, subject: str, phrase: str,
                           context: str, period: str = "event",
                           min_mentions: int = 1,
                           allow_broad_fallback: bool = True) -> tuple[int, int, str]:
        transcript = self.transcript_pattern(
            subject, phrase, context, period, min_mentions,
            allow_phrase_fallback=False)
        if transcript[1]:
            return transcript
        exact_hits, exact_total = self.historical(subject, phrase, context)
        if exact_total:
            return exact_hits, exact_total, "exact phrase/context"

        # Context inference is intentionally conservative and the upstream
        # market often labels a rally, briefing, or earnings call only as
        # "other". Reuse only the same subject and exact phrase across
        # contexts. This is materially safer than falling back to unrelated
        # phrases for the same speaker or to global market history.
        useful_subject = subject.lower() not in {"anyone", "unknown"}
        if useful_subject and not context.lower().startswith("tv:"):
            transcript_phrase = self.transcript_pattern(
                subject, phrase, context, period, min_mentions,
                allow_phrase_fallback=True)
            if transcript_phrase[1]:
                hits, total, scope = transcript_phrase
                return hits, total, f"{scope} (cross-context)"

            phrase_row = self.db.execute(
                """SELECT COUNT(*) n, COALESCE(SUM(occurred),0) hits
                   FROM observations WHERE lower(subject)=lower(?)
                   AND lower(phrase)=lower(?)""",
                (subject, phrase),
            ).fetchone()
            if int(phrase_row["n"]):
                return (int(phrase_row["hits"]), int(phrase_row["n"]),
                        "exact subject/phrase (cross-context)")
        if not allow_broad_fallback:
            return 0, 0, "neutral — no phrase-level evidence"
        queries = []
        if useful_subject:
            queries.extend([
                ("subject/context", "lower(subject)=lower(?) AND lower(context)=lower(?)",
                 (subject, context)),
                ("subject", "lower(subject)=lower(?)", (subject,)),
            ])
        queries.extend([
            ("context", "lower(context)=lower(?)", (context,)),
            ("global", "1=1", ()),
        ])
        for scope, where, params in queries:
            row = self.db.execute(
                f"SELECT COUNT(*) n, COALESCE(SUM(occurred),0) hits FROM observations WHERE {where}",
                params,
            ).fetchone()
            if int(row["n"]):
                return int(row["hits"]), int(row["n"]), scope
        return 0, 0, "neutral"

    def transcript_pattern(self, subject: str, phrase: str,
                           context: str, period: str = "event",
                           min_mentions: int = 1,
                           allow_phrase_fallback: bool = True) -> tuple[int, int, str]:
        if subject.lower() == "unknown":
            return 0, 0, "neutral"
        queries = [
            ("official transcript phrase/context",
             "lower(subject)=lower(?) AND lower(phrase)=lower(?) AND lower(context)=lower(?)",
             (subject, phrase, context)),
        ]
        if allow_phrase_fallback:
            queries.append(("official transcript phrase",
                            "lower(subject)=lower(?) AND lower(phrase)=lower(?)",
                            (subject, phrase)))
        for base_scope, where, params in queries:
            row = self.db.execute(
                f"""SELECT COUNT(*) n,
                    COALESCE(SUM(CASE WHEN mention_count >= ? THEN 1 ELSE 0 END),0) hits
                    FROM transcript_mentions WHERE {where}
                    AND source_kind NOT LIKE '%_shadow'""",
                (int(min_mentions), *params),
            ).fetchone()
            scope = base_scope
            if int(row["n"]):
                kinds = self.db.execute(
                    f"SELECT GROUP_CONCAT(DISTINCT source_kind) kinds "
                    f"FROM transcript_mentions WHERE {where} "
                    f"AND source_kind NOT LIKE '%_shadow'", params,
                ).fetchone()[0] or ""
                if kinds == "opensubtitles":
                    scope = scope.replace("official transcript", "third-party subtitle")
                return int(row["hits"]), int(row["n"]), scope
        return 0, 0, "neutral"

    def shadow_transcript_pattern(self, subject: str, phrase: str,
                                  context: str,
                                  min_mentions: int = 1) -> tuple[int, int, str]:
        """Return YouTube evidence for display/calibration, never live scoring."""
        row = self.db.execute(
            """SELECT COUNT(*) n,
                      COALESCE(SUM(CASE WHEN mention_count >= ? THEN 1 ELSE 0 END),0) hits
               FROM transcript_mentions
               WHERE lower(subject)=lower(?) AND lower(phrase)=lower(?)
                 AND lower(context)=lower(?) AND source_kind=?""",
            (int(min_mentions), subject, phrase, context,
             "supadata_youtube_shadow"),
        ).fetchone()
        return (int(row["hits"]), int(row["n"]),
                "Supadata YouTube shadow evidence")

    def record_youtube_shadow_prediction(
            self, condition_id: str, segment: str,
            baseline_probability: float, option_c_probability: float,
            transcript_samples: int) -> None:
        self.db.execute(
            """INSERT OR IGNORE INTO youtube_shadow_predictions
               (condition_id,segment,baseline_probability,option_c_probability,
                transcript_samples,created_at)
               VALUES(?,?,?,?,?,?)""",
            (condition_id, segment, float(baseline_probability),
             float(option_c_probability), int(transcript_samples),
             datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()

    def resolve_youtube_shadow_prediction(self, condition_id: str,
                                          occurred: bool) -> None:
        self.db.execute(
            """UPDATE youtube_shadow_predictions
               SET resolved_outcome=?, resolved_at=?
               WHERE condition_id=? AND resolved_outcome IS NULL""",
            (int(occurred), datetime.now(timezone.utc).isoformat(), condition_id),
        )
        self.db.commit()

    def youtube_calibration(self, segment: str, minimum_resolved: int = 30,
                            minimum_improvement: float = 0.005) -> dict:
        rows = list(self.db.execute(
            """SELECT baseline_probability,option_c_probability,resolved_outcome
               FROM youtube_shadow_predictions
               WHERE segment=? AND resolved_outcome IS NOT NULL""", (segment,)))
        count = len(rows)
        baseline_brier = option_c_brier = None
        if rows:
            baseline_brier = sum(
                (float(row["baseline_probability"]) / 100 - int(row["resolved_outcome"])) ** 2
                for row in rows) / count
            option_c_brier = sum(
                (float(row["option_c_probability"]) / 100 - int(row["resolved_outcome"])) ** 2
                for row in rows) / count
        passed = bool(
            count >= int(minimum_resolved)
            and option_c_brier is not None
            and baseline_brier is not None
            and option_c_brier + float(minimum_improvement) < baseline_brier
        )
        return {
            "segment": segment,
            "resolved": count,
            "minimumResolved": int(minimum_resolved),
            "baselineBrier": baseline_brier,
            "optionCBrier": option_c_brier,
            "minimumImprovement": float(minimum_improvement),
            "passed": passed,
        }

    def add_transcript_mention(self, document_id: str, subject: str, phrase: str,
                               context: str, mention_count: int,
                               document_date: str, title: str,
                               source_url: str, source_kind: str = "govinfo") -> bool:
        cursor = self.db.execute(
            """INSERT OR REPLACE INTO transcript_mentions
               (document_id,subject,phrase,context,mention_count,document_date,title,source_url,observed_at,source_kind)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (document_id, subject, phrase, context, int(mention_count), document_date,
             title, source_url, datetime.now(timezone.utc).isoformat(), source_kind),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def transcript_refresh_due(self, subject: str, phrase: str,
                               refresh_hours: float,
                               source_kind: str = "govinfo") -> bool:
        row = self.db.execute(
            """SELECT refreshed_at FROM transcript_source_refreshes
               WHERE lower(subject)=lower(?) AND lower(phrase)=lower(?)
               AND source_kind=?""",
            (subject, phrase, source_kind),
        ).fetchone()
        if not row:
            return True
        try:
            refreshed = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        except ValueError:
            return True
        age = (datetime.now(timezone.utc) - refreshed).total_seconds()
        return age >= refresh_hours * 3600

    def mark_transcript_refreshed(self, subject: str, phrase: str,
                                  documents_scanned: int,
                                  source_kind: str = "govinfo") -> None:
        self.db.execute(
            """INSERT INTO transcript_source_refreshes
               (subject,phrase,refreshed_at,documents_scanned,source_kind)
               VALUES(?,?,?,?,?)
               ON CONFLICT(subject,phrase,source_kind) DO UPDATE SET
                 refreshed_at=excluded.refreshed_at,
                 documents_scanned=excluded.documents_scanned""",
            (subject, phrase, datetime.now(timezone.utc).isoformat(),
             documents_scanned, source_kind),
        )
        self.db.commit()

    def add_observation(self, subject: str, phrase: str, context: str,
                        occurred: bool, condition_id: str | None = None) -> bool:
        cursor = self.db.execute(
            """INSERT OR IGNORE INTO observations
               (subject,phrase,context,occurred,observed_at,condition_id)
               VALUES(?,?,?,?,?,?)""",
            (subject, phrase, context, int(occurred),
             datetime.now(timezone.utc).isoformat(), condition_id),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def observation_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0])

    def evidence_summary(self) -> dict:
        sources = {
            str(row["source_kind"]): {
                "documents": int(row["documents"]),
                "mentions": int(row["mentions"]),
            }
            for row in self.db.execute(
                """SELECT source_kind, COUNT(DISTINCT document_id) documents,
                          COALESCE(SUM(mention_count),0) mentions
                   FROM transcript_mentions GROUP BY source_kind"""
            )
        }
        return {"gammaObservations": self.observation_count(),
                "transcriptSources": sources}

    def open_positions(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM positions WHERE status='open'"))

    def redeemable_positions(self) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM positions WHERE redemption_status='redeemable'"))

    def has_condition(self, condition_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM positions WHERE condition_id=? AND status='open'", (condition_id,)
        ).fetchone() is not None

    def open_position_count(self, condition_id: str) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM positions WHERE condition_id=? AND status='open'",
            (condition_id,),
        ).fetchone()[0])

    @staticmethod
    def entry_key(value: str) -> str:
        parts = [re.sub(r"[^a-z0-9]+", " ", part.lower()).strip()
                 for part in str(value).split("|")]
        return " | ".join(sorted(part for part in set(parts) if part))

    def entry_allowed(self, condition_id: str, subject: str,
                      phrase: str, day: str | None = None) -> tuple[bool, str]:
        """Enforce one lifetime condition entry and one word entry per UTC day."""
        if self.db.execute(
            "SELECT 1 FROM positions WHERE condition_id=? LIMIT 1", (condition_id,)
        ).fetchone() or self.db.execute(
            "SELECT 1 FROM entry_locks WHERE condition_id=?", (condition_id,)
        ).fetchone():
            return False, "condition already entered"
        subject_key = self.entry_key(subject)
        phrase_key = self.entry_key(phrase)
        entry_day = day or datetime.now(timezone.utc).date().isoformat()
        if self.db.execute(
            """SELECT 1 FROM entry_locks
               WHERE subject_key=? AND phrase_key=? AND entry_day=? LIMIT 1""",
            (subject_key, phrase_key, entry_day),
        ).fetchone():
            return False, "word mention already entered today"
        return True, "ok"

    def record_position(self, condition_id: str, token_id: str, side: str,
                        size: float, price: float, order_id: str | None,
                        question: str, end_date: str, tick_size: str,
                        neg_risk: bool, subject: str = "",
                        phrase: str = "") -> int:
        now = datetime.now(timezone.utc).isoformat()
        entry_day = datetime.now(timezone.utc).date().isoformat()
        subject_key = self.entry_key(subject)
        phrase_key = self.entry_key(phrase)
        allowed, reason = self.entry_allowed(
            condition_id, subject, phrase, entry_day)
        if not allowed:
            raise RuntimeError(f"entry lock rejected confirmed position: {reason}")
        self.db.execute(
            """INSERT INTO entry_locks
               (condition_id,subject_key,phrase_key,entry_day,created_at)
               VALUES(?,?,?,?,?)""",
            (condition_id, subject_key, phrase_key, entry_day, now),
        )
        cursor = self.db.execute(
            """INSERT INTO positions
               (condition_id,token_id,side,size_usd,entry_price,status,opened_at,
                order_id,peak_price,question,end_date,tick_size,neg_risk,
                subject_key,phrase_key)
               VALUES(?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?)""",
            (condition_id, token_id, side, size, price, now, order_id, price,
             question, end_date, tick_size, int(neg_risk), subject_key, phrase_key),
        )
        self.db.commit()
        Path(self.journal).parent.mkdir(parents=True, exist_ok=True)
        with open(self.journal, "a") as fh:
            fh.write(json.dumps({"event": "OPEN", "time": now,
                                 "position_id": cursor.lastrowid,
                                 "condition_id": condition_id,
                                 "token_id": token_id, "side": side, "size_usd": size,
                                 "price": price, "order_id": order_id}) + "\n")
        return int(cursor.lastrowid)

    def settle_position(self, position_id: int, payout_price: float,
                        redemption_status: str, reference: str = "") -> float:
        """Close local exposure at its resolved payout without placing a sell."""
        row = self.db.execute(
            "SELECT * FROM positions WHERE position_id=? AND status='open'",
            (position_id,),
        ).fetchone()
        if not row:
            return 0.0
        entry = float(row["entry_price"])
        shares = float(row["size_usd"]) / entry
        payout = max(0.0, min(1.0, float(payout_price)))
        pnl = shares * payout - float(row["size_usd"])
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """UPDATE positions SET status='resolved', closed_at=?, exit_price=?,
                      realized_pnl=?, redemption_status=?
               WHERE position_id=?""",
            (now, payout, pnl, redemption_status, position_id),
        )
        day = datetime.now(timezone.utc).date().isoformat()
        self.db.execute(
            """INSERT INTO daily_pnl(day,realized_usd) VALUES(?,?)
               ON CONFLICT(day) DO UPDATE SET realized_usd=realized_usd+excluded.realized_usd""",
            (day, pnl),
        )
        self.db.commit()
        Path(self.journal).parent.mkdir(parents=True, exist_ok=True)
        with open(self.journal, "a") as fh:
            fh.write(json.dumps({"event": "RESOLVED", "time": now,
                                 "position_id": position_id,
                                 "condition_id": row["condition_id"],
                                 "payout_price": payout, "pnl_usd": pnl,
                                 "redemption_status": redemption_status,
                                 "reference": reference}) + "\n")
        return pnl

    def daily_loss(self) -> float:
        return min(0.0, self.daily_pnl())

    def daily_pnl(self) -> float:
        day = datetime.now(timezone.utc).date().isoformat()
        row = self.db.execute("SELECT realized_usd FROM daily_pnl WHERE day=?", (day,)).fetchone()
        return float(row[0]) if row else 0.0

    def update_peak(self, position_id: int, price: float) -> float:
        self.db.execute(
            "UPDATE positions SET peak_price=MAX(peak_price, ?) WHERE position_id=? AND status='open'",
            (price, position_id),
        )
        self.db.commit()
        row = self.db.execute(
            "SELECT peak_price FROM positions WHERE position_id=?", (position_id,)
        ).fetchone()
        return float(row[0]) if row else price

    def close_position(self, position_id: int, exit_price: float,
                       order_id: str | None, shares_sold: float) -> float:
        row = self.db.execute(
            "SELECT * FROM positions WHERE position_id=? AND status='open'",
            (position_id,),
        ).fetchone()
        if not row:
            return 0.0
        entry = float(row["entry_price"])
        held_shares = float(row["size_usd"]) / entry
        sold = max(0.0, min(float(shares_sold), held_shares))
        if sold <= 0:
            raise ValueError("cannot close a position without confirmed sold shares")
        cost_basis = sold * entry
        pnl = sold * exit_price - cost_basis
        remaining_usd = max(0.0, float(row["size_usd"]) - cost_basis)
        fully_closed = sold >= held_shares * 0.999
        if fully_closed:
            self.db.execute(
                "UPDATE positions SET status='closed', size_usd=0 WHERE position_id=?",
                (position_id,),
            )
        else:
            self.db.execute(
                "UPDATE positions SET size_usd=? WHERE position_id=? AND status='open'",
                (remaining_usd, position_id),
            )
        day = datetime.now(timezone.utc).date().isoformat()
        self.db.execute(
            """INSERT INTO daily_pnl(day,realized_usd) VALUES(?,?)
               ON CONFLICT(day) DO UPDATE SET realized_usd=realized_usd+excluded.realized_usd""",
            (day, pnl),
        )
        self.db.commit()
        with open(self.journal, "a") as fh:
            fh.write(json.dumps({"event": "CLOSE" if fully_closed else "PARTIAL_CLOSE",
                                 "time": datetime.now(timezone.utc).isoformat(),
                                 "position_id": position_id,
                                 "condition_id": row["condition_id"], "exit_price": exit_price,
                                 "shares_sold": sold, "pnl_usd": pnl,
                                 "order_id": order_id}) + "\n")
        return pnl
