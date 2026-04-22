from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()


def save_kraken_credentials(path: Path, api_key: str | None, api_secret: str | None) -> None:
    save_api_credentials(path, "KRAKEN_API_KEY", "KRAKEN_API_SECRET", api_key, api_secret)


def save_api_credentials(
    path: Path,
    key_name: str,
    secret_name: str,
    api_key: str | None,
    api_secret: str | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read_env_file(path)
    if api_key:
        current[key_name] = api_key.strip()
    if api_secret:
        current[secret_name] = api_secret.strip()
    payload = "\n".join(f"{key}={value}" for key, value in current.items()) + "\n"
    path.write_text(payload, encoding="utf-8")


def ensure_env_template(path: Path, keys: list[str]) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(f"# {key}=" for key in keys) + "\n"
    path.write_text(payload, encoding="utf-8")


def read_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def mask_secret(value: str | None) -> str:
    if not value:
        return "missing"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def update_live_flags(
    config_path: Path,
    *,
    kraken_enable_live_trading: bool | None = None,
    kraken_live_enabled: bool | None = None,
    kraken_live_dry_run: bool | None = None,
    alpaca_paper_enabled: bool | None = None,
) -> None:
    text = config_path.read_text(encoding="utf-8")
    replacements: list[tuple[str, str]] = []
    if kraken_enable_live_trading is not None:
        replacements.append(
            ("enable_live_trading = false", "enable_live_trading = true")
            if kraken_enable_live_trading
            else ("enable_live_trading = true", "enable_live_trading = false")
        )
    if kraken_live_enabled is not None:
        replacements.append(
            ("enabled = false", "enabled = true")
            if kraken_live_enabled
            else ("enabled = true", "enabled = false")
        )
    if kraken_live_dry_run is not None:
        replacements.append(
            ("dry_run = true", "dry_run = false")
            if not kraken_live_dry_run
            else ("dry_run = false", "dry_run = true")
        )

    text = _replace_in_section(
        text,
        "kraken",
        {
            "enable_live_trading": "true" if kraken_enable_live_trading else "false"
            if kraken_enable_live_trading is not None
            else None,
        },
    )
    text = _replace_in_section(
        text,
        "kraken_live",
        {
            "enabled": "true" if kraken_live_enabled else "false"
            if kraken_live_enabled is not None
            else None,
            "dry_run": "true" if kraken_live_dry_run else "false"
            if kraken_live_dry_run is not None
            else None,
        },
    )
    text = _replace_in_section(
        text,
        "alpaca_paper",
        {
            "enabled": "true" if alpaca_paper_enabled else "false"
            if alpaca_paper_enabled is not None
            else None,
        },
    )
    config_path.write_text(text, encoding="utf-8")


def _replace_in_section(text: str, section: str, values: dict[str, str | None]) -> str:
    lines = text.splitlines()
    in_section = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == f"[{section}]"
            continue
        if not in_section:
            continue
        for key, replacement in values.items():
            if replacement is None:
                continue
            if stripped.startswith(f"{key} = "):
                indent = line[: len(line) - len(line.lstrip())]
                lines[index] = f"{indent}{key} = {replacement}"
    return "\n".join(lines) + "\n"
