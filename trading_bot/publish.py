from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess

from trading_bot.config import VercelConfig


@dataclass(slots=True)
class PublishResult:
    url: str | None
    alias: str | None
    raw_output: str


class PublishError(RuntimeError):
    def __init__(self, message: str, raw_output: str) -> None:
        super().__init__(message)
        self.raw_output = raw_output


def publish_dashboard(project_root: Path, config: VercelConfig) -> PublishResult:
    vercel_binary = shutil.which("vercel") or shutil.which("vercel.cmd")
    if vercel_binary is None:
        raise FileNotFoundError("Vercel CLI not found in PATH")

    command = [vercel_binary, "deploy", "-y", "--scope", config.scope]
    if config.production:
        command.append("--prod")

    try:
        completed = subprocess.run(
            command,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        output = "\n".join(part for part in [exc.stdout, exc.stderr] if part).strip()
        raise PublishError(_summarize_publish_failure(output), output) from exc

    output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
    return PublishResult(
        url=_extract_marker(output, "Production: https://"),
        alias=_extract_marker(output, "Aliased: https://"),
        raw_output=output,
    )


def _extract_marker(output: str, prefix: str) -> str | None:
    for line in output.splitlines():
        if prefix in line:
            return "https://" + line.split(prefix, 1)[1].strip()
    return None


def _summarize_publish_failure(output: str) -> str:
    lowered = output.lower()
    if "api-deployments-free-per-day" in lowered or "more than 100" in lowered:
        return "Vercel alcanzo el limite diario de deployments del plan actual."
    if "resource is limited" in lowered:
        return "Vercel rechazo el deploy por limite de recursos."
    if output:
        first_line = output.splitlines()[0].strip()
        return first_line[:220]
    return "Fallo el deploy de Vercel."
