from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3

from trading_bot.config import StorageConfig
from trading_bot.types import Fill, Signal


@dataclass(slots=True)
class RunRecord:
    run_id: int
    started_at: datetime


class SQLiteStorage:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    command TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    starting_cash REAL NOT NULL,
                    ending_cash REAL,
                    signals_count INTEGER NOT NULL DEFAULT 0,
                    fills_count INTEGER NOT NULL DEFAULT 0,
                    open_positions INTEGER NOT NULL DEFAULT 0,
                    publish_status TEXT,
                    publish_url TEXT,
                    publish_error TEXT
                );

                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    expected_edge REAL NOT NULL,
                    fair_probability REAL NOT NULL,
                    market_price REAL NOT NULL,
                    reason TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    size REAL NOT NULL,
                    fee_paid REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                """
            )
            self._ensure_column(connection, "runs", "publish_status", "TEXT")
            self._ensure_column(connection, "runs", "publish_url", "TEXT")
            self._ensure_column(connection, "runs", "publish_error", "TEXT")

    def _ensure_column(self, connection: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def start_run(self, *, mode: str, data_source: str, command: str, starting_cash: float) -> RunRecord:
        started_at = datetime.now(UTC)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO runs(mode, data_source, command, started_at, starting_cash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (mode, data_source, command, started_at.isoformat(), starting_cash),
            )
        return RunRecord(run_id=int(cursor.lastrowid), started_at=started_at)

    def log_signal(self, run_id: int, signal: Signal) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO signals(
                    run_id, market_id, side, confidence, expected_edge, fair_probability,
                    market_price, reason, features_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    signal.market_id,
                    signal.side.value,
                    signal.confidence,
                    signal.expected_edge,
                    signal.fair_probability,
                    signal.market_price,
                    signal.reason,
                    json.dumps(signal.features, sort_keys=True),
                    signal.timestamp.isoformat(),
                ),
            )

    def log_fill(self, run_id: int, fill: Fill) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO fills(run_id, market_id, side, price, size, fee_paid, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    fill.market_id,
                    fill.side.value,
                    fill.price,
                    fill.size,
                    fill.fee_paid,
                    fill.timestamp.isoformat(),
                ),
            )

    def finish_run(
        self,
        run_id: int,
        *,
        ending_cash: float,
        signals_count: int,
        fills_count: int,
        open_positions: int,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE runs
                SET ended_at = ?, ending_cash = ?, signals_count = ?, fills_count = ?, open_positions = ?
                WHERE id = ?
                """,
                (
                    datetime.now(UTC).isoformat(),
                    ending_cash,
                    signals_count,
                    fills_count,
                    open_positions,
                    run_id,
                ),
            )

    def mark_publish_result(
        self,
        run_id: int,
        *,
        status: str,
        url: str | None = None,
        error: str | None = None,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE runs
                SET publish_status = ?, publish_url = ?, publish_error = ?
                WHERE id = ?
                """,
                (status, url, error, run_id),
            )

    def fetch_run_summary(self, run_id: int) -> dict[str, object]:
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    def fetch_recent_runs(self, limit: int = 10) -> list[dict[str, object]]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    command,
                    mode,
                    data_source,
                    started_at,
                    ended_at,
                    starting_cash,
                    ending_cash,
                    ROUND(COALESCE(ending_cash, starting_cash) - starting_cash, 2) AS pnl,
                    signals_count,
                    fills_count,
                    open_positions,
                    publish_status,
                    publish_url,
                    publish_error
                FROM runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def fetch_recent_signals(self, limit: int = 10, run_id: int | None = None) -> list[dict[str, object]]:
        query = """
            SELECT
                s.id,
                s.run_id,
                s.market_id,
                s.side,
                s.confidence,
                s.expected_edge,
                s.fair_probability,
                s.market_price,
                s.reason,
                s.features_json,
                s.created_at
            FROM signals s
        """
        params: tuple[object, ...]
        if run_id is not None:
            query += " WHERE s.run_id = ?"
            params = (run_id, limit)
        else:
            params = (limit,)
        query += " ORDER BY s.id DESC LIMIT ?"

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(query, params).fetchall()

        results: list[dict[str, object]] = []
        for row in rows:
            payload = dict(row)
            payload["features"] = json.loads(str(payload.pop("features_json")))
            results.append(payload)
        return results

    def fetch_recent_fills(self, limit: int = 10, run_id: int | None = None) -> list[dict[str, object]]:
        query = """
            SELECT
                id,
                run_id,
                market_id,
                side,
                price,
                size,
                fee_paid,
                created_at
            FROM fills
        """
        params: tuple[object, ...]
        if run_id is not None:
            query += " WHERE run_id = ?"
            params = (run_id, limit)
        else:
            params = (limit,)
        query += " ORDER BY id DESC LIMIT ?"

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        return None


def build_storage(config: StorageConfig) -> SQLiteStorage:
    if config.backend != "sqlite":
        raise ValueError(f"Unsupported storage backend: {config.backend}")
    return SQLiteStorage(config.sqlite_path)
