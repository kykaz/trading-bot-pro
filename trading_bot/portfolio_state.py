from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from trading_bot.types import Portfolio, Position


def build_portfolio_state_path(root_dir: str, source: str) -> Path:
    safe_source = source.replace("/", "_").replace("\\", "_")
    return Path(root_dir) / f"{safe_source}.json"


def load_portfolio(path: Path, starting_cash: float) -> Portfolio:
    if not path.exists():
        return Portfolio(cash=starting_cash)

    payload = json.loads(path.read_text(encoding="utf-8"))
    positions: dict[str, Position] = {}
    for market_id, raw_position in payload.get("positions", {}).items():
        updated_at = raw_position.get("updated_at")
        positions[market_id] = Position(
            market_id=market_id,
            size=float(raw_position.get("size", 0.0)),
            average_price=float(raw_position.get("average_price", 0.0)),
            contract_size=float(raw_position.get("contract_size", 1.0)),
            updated_at=datetime.fromisoformat(updated_at) if updated_at else None,
        )
    return Portfolio(
        cash=float(payload.get("cash", starting_cash)),
        positions=positions,
        realized_pnl=float(payload.get("realized_pnl", 0.0)),
    )


def save_portfolio(path: Path, portfolio: Portfolio) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cash": portfolio.cash,
        "realized_pnl": portfolio.realized_pnl,
        "positions": {
            market_id: {
                "size": position.size,
                "average_price": position.average_price,
                "contract_size": position.contract_size,
                "updated_at": position.updated_at.isoformat() if position.updated_at else None,
            }
            for market_id, position in portfolio.positions.items()
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def reset_portfolio(path: Path) -> None:
    if path.exists():
        path.unlink()
