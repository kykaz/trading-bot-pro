from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import webbrowser

from trading_bot.config import AlertsConfig


@dataclass(slots=True)
class AlertPayload:
    signals_count: int
    fills_count: int
    run_id: int
    dashboard_path: Path
    dashboard_url: str | None


def maybe_alert(config: AlertsConfig, payload: AlertPayload) -> bool:
    if not config.enabled:
        return False

    should_alert = (
        payload.signals_count >= config.min_signals
        or payload.fills_count >= config.min_fills
    )
    if not should_alert:
        return False

    if config.sound_on_alert:
        _play_sound()

    if config.open_dashboard_on_alert:
        target = payload.dashboard_url or payload.dashboard_path.resolve().as_uri()
        webbrowser.open(target)

    return True


def _play_sound() -> None:
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        return None
