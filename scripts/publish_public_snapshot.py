from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "var" / "trading_bot.db"
STATE_PATH = PROJECT_ROOT / "var" / "publish_sync_state.json"
PUBLIC_PAGE_SCRIPT = PROJECT_ROOT / "scripts" / "generate_public_operator_page.py"
TRACKED_FILES = ["index.html", "public_operator.html"]


def main() -> int:
    latest_run = fetch_latest_mt5_run(DB_PATH)
    if latest_run is None:
        print("publish_sync=skip reason=no_mt5_runs")
        return 0

    state = load_state()
    last_published_run_id = int(state.get("last_published_run_id") or 0)
    run_id = int(latest_run["id"])
    activity = has_relevant_activity(latest_run)

    print(
        "publish_sync_context "
        f"run_id={run_id} activity={'yes' if activity else 'no'} "
        f"signals={latest_run['signals_count']} fills={latest_run['fills_count']} "
        f"open_positions={latest_run['open_positions']}"
    )

    if run_id <= last_published_run_id and not has_local_snapshot_changes():
        print("publish_sync=skip reason=already_published")
        return 0

    if not activity:
        print("publish_sync=skip reason=no_relevant_activity")
        return 0

    run_command([sys.executable, str(PUBLIC_PAGE_SCRIPT)])
    if not has_local_snapshot_changes():
        save_state({"last_published_run_id": run_id, "updated_at": datetime.now(UTC).isoformat()})
        print("publish_sync=skip reason=no_snapshot_diff")
        return 0

    commit_message = build_commit_message(latest_run)
    run_command(["git", "add", *TRACKED_FILES], cwd=PROJECT_ROOT)
    run_command(["git", "commit", "-m", commit_message], cwd=PROJECT_ROOT)
    run_command(["git", "push", "origin", "main"], cwd=PROJECT_ROOT)
    deploy_output = run_command(["vercel", "deploy", "--prod", "-y", "--no-wait"], cwd=PROJECT_ROOT)
    save_state(
        {
            "last_published_run_id": run_id,
            "updated_at": datetime.now(UTC).isoformat(),
            "last_commit_message": commit_message,
            "last_deploy_output": deploy_output.strip(),
        }
    )
    print("publish_sync=ok")
    return 0


def fetch_latest_mt5_run(db_path: Path) -> dict[str, object] | None:
    if not db_path.exists():
        return None
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT
                id,
                mode,
                data_source,
                command,
                started_at,
                ended_at,
                starting_cash,
                ending_cash,
                signals_count,
                fills_count,
                open_positions
            FROM runs
            WHERE data_source = 'mt5'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else None


def has_relevant_activity(run: dict[str, object]) -> bool:
    starting_cash = float(run.get("starting_cash") or 0.0)
    ending_cash = float(run.get("ending_cash") or starting_cash)
    pnl = ending_cash - starting_cash
    return (
        int(run.get("signals_count") or 0) > 0
        or int(run.get("fills_count") or 0) > 0
        or int(run.get("open_positions") or 0) > 0
        or abs(pnl) >= 0.01
    )


def has_local_snapshot_changes() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", *TRACKED_FILES],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def build_commit_message(run: dict[str, object]) -> str:
    signals = int(run.get("signals_count") or 0)
    fills = int(run.get("fills_count") or 0)
    return f"chore: sync public snapshot (run #{int(run['id'])}, s{signals}, f{fills})"


def load_state() -> dict[str, object]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(payload: dict[str, object]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def run_command(command: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=str(cwd or PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    return (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")


if __name__ == "__main__":
    raise SystemExit(main())
