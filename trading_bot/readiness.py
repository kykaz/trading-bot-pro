from __future__ import annotations

from dataclasses import dataclass
import os

from trading_bot.config import AppConfig
from trading_bot.storage import SQLiteStorage


@dataclass(slots=True)
class GateResult:
    category: str
    name: str
    status: str
    detail: str
    critical: bool = False


@dataclass(slots=True)
class ReadinessMetrics:
    total_runs: int
    real_runs: int
    real_fills: int
    positive_real_run_rate: float
    max_run_drawdown: float
    zero_fill_streak: int


@dataclass(slots=True)
class ReadinessReport:
    verdict: str
    summary: str
    operational_status: str
    edge_status: str
    live_status: str
    metrics: ReadinessMetrics
    gates: list[GateResult]

    @property
    def blockers(self) -> list[str]:
        return [
            gate.detail
            for gate in self.gates
            if gate.critical and gate.status in {"fail", "pending"}
        ][:3]


def evaluate_readiness(config: AppConfig, storage: SQLiteStorage) -> ReadinessReport:
    runs = storage.fetch_recent_runs(limit=config.readiness.lookback_runs)
    real_runs = [run for run in runs if str(run["data_source"]) != "mock"]
    real_fills = sum(int(run["fills_count"]) for run in real_runs)
    positive_real_runs = sum(1 for run in real_runs if float(run["pnl"] or 0.0) > 0.0)
    positive_real_run_rate = (positive_real_runs / len(real_runs)) if real_runs else 0.0
    max_run_drawdown = _calculate_run_drawdown(real_runs)
    zero_fill_streak = _calculate_zero_fill_streak(real_runs)
    latest_run = runs[0] if runs else None
    latest_publish_status = str(latest_run.get("publish_status") or "unknown") if latest_run else "unknown"
    latest_publish_error = str(latest_run.get("publish_error") or "").strip() if latest_run else ""

    metrics = ReadinessMetrics(
        total_runs=len(runs),
        real_runs=len(real_runs),
        real_fills=real_fills,
        positive_real_run_rate=round(positive_real_run_rate, 3),
        max_run_drawdown=round(max_run_drawdown, 2),
        zero_fill_streak=zero_fill_streak,
    )

    gates = [
        GateResult(
            category="operational",
            name="Paper protegido",
            status="pass" if config.bot.mode == "paper" else "fail",
            detail=(
                "El bot sigue en paper y no expone capital real."
                if config.bot.mode == "paper"
                else "El bot ya no esta en paper; vuelve a un modo seguro antes de validar."
            ),
            critical=True,
        ),
        GateResult(
            category="operational",
            name="Fuente externa probada",
            status="pass" if metrics.real_runs >= 1 else "fail",
            detail=(
                f"Ya hay {metrics.real_runs} corridas contra venues externos."
                if metrics.real_runs >= 1
                else "Todavia no existe ninguna corrida contra datos externos."
            ),
            critical=True,
        ),
        GateResult(
            category="operational",
            name="Ultima corrida cerrada",
            status="pass" if latest_run and latest_run["ended_at"] else "fail",
            detail=(
                f"La ultima corrida #{latest_run['id']} cerro y quedo registrada."
                if latest_run and latest_run["ended_at"]
                else "La ultima corrida no quedo cerrada correctamente en storage."
            ),
            critical=True,
        ),
        GateResult(
            category="operational",
            name="Alertas activas",
            status="pass" if config.alerts.enabled else "fail",
            detail=(
                "Las alertas locales estan activas."
                if config.alerts.enabled
                else "Las alertas estan apagadas; perderias visibilidad operativa."
            ),
        ),
        GateResult(
            category="operational",
            name="Dashboard autopublicado",
            status=(
                "pass"
                if not config.vercel.auto_publish_dashboard
                else ("pass" if latest_publish_status in {"success", "skipped"} else "fail")
            ),
            detail=(
                "La autopublicacion esta apagada a proposito; el panel depende de builds manuales."
                if not config.vercel.auto_publish_dashboard
                else (
                    "La ultima corrida publico el dashboard correctamente."
                    if latest_publish_status == "success"
                    else (
                        f"La ultima corrida omitio el deploy: {latest_publish_error}"
                        if latest_publish_status == "skipped"
                        else (
                            f"La ultima publicacion fallo: {latest_publish_error}"
                            if latest_publish_error
                            else "La ultima corrida no logro publicar el dashboard."
                        )
                    )
                )
            ),
        ),
        GateResult(
            category="edge",
            name="Muestra real suficiente",
            status="pass" if metrics.real_runs >= config.readiness.min_real_runs else "fail",
            detail=(
                f"Corridas reales {metrics.real_runs}/{config.readiness.min_real_runs}."
                if metrics.real_runs >= config.readiness.min_real_runs
                else f"Faltan corridas reales: {metrics.real_runs}/{config.readiness.min_real_runs}."
            ),
            critical=True,
        ),
        GateResult(
            category="edge",
            name="Actividad real suficiente",
            status="pass" if metrics.real_fills >= config.readiness.min_real_fills else "fail",
            detail=(
                f"Fills reales {metrics.real_fills}/{config.readiness.min_real_fills}."
                if metrics.real_fills >= config.readiness.min_real_fills
                else f"Faltan fills reales: {metrics.real_fills}/{config.readiness.min_real_fills}."
            ),
            critical=True,
        ),
        GateResult(
            category="edge",
            name="Consistencia positiva",
            status="pass"
            if real_runs and metrics.positive_real_run_rate >= config.readiness.min_positive_run_rate
            else "fail",
            detail=(
                f"Corridas positivas {metrics.positive_real_run_rate:.1%}."
                if real_runs and metrics.positive_real_run_rate >= config.readiness.min_positive_run_rate
                else (
                    "No hay base real suficiente para medir consistencia."
                    if not real_runs
                    else f"La tasa positiva real esta en {metrics.positive_real_run_rate:.1%} y el minimo es {config.readiness.min_positive_run_rate:.1%}."
                )
            ),
            critical=True,
        ),
        GateResult(
            category="edge",
            name="Drawdown por corridas",
            status="pass"
            if real_runs and metrics.max_run_drawdown <= config.readiness.max_run_drawdown
            else "fail",
            detail=(
                f"Drawdown de corridas {metrics.max_run_drawdown:.2f} dentro del limite."
                if real_runs and metrics.max_run_drawdown <= config.readiness.max_run_drawdown
                else (
                    "No hay corridas reales suficientes para medir drawdown."
                    if not real_runs
                    else f"Drawdown de corridas {metrics.max_run_drawdown:.2f} supera el limite {config.readiness.max_run_drawdown:.2f}."
                )
            ),
            critical=True,
        ),
        GateResult(
            category="edge",
            name="Racha sin fills",
            status="pass" if metrics.zero_fill_streak <= config.readiness.max_zero_fill_streak else "fail",
            detail=(
                f"Racha actual sin fills {metrics.zero_fill_streak}."
                if metrics.zero_fill_streak <= config.readiness.max_zero_fill_streak
                else f"La racha sin fills ya va en {metrics.zero_fill_streak} corridas."
            ),
        ),
        GateResult(
            category="edge",
            name="Expectancy neta cerrada",
            status="pending",
            detail="Falta registrar trades cerrados para calcular expectancy neta real.",
            critical=True,
        ),
        GateResult(
            category="live",
            name="Trading real habilitado",
            status="pass" if (config.polymarket.enable_live_trading or config.kraken.enable_live_trading) else "fail",
            detail=(
                "La config ya permite live trading en al menos un venue."
                if (config.polymarket.enable_live_trading or config.kraken.enable_live_trading)
                else "Live trading sigue deshabilitado en Polymarket y Kraken."
            ),
            critical=True,
        ),
        GateResult(
            category="live",
            name="Auth live cargada",
            status=(
                "pass"
                if (
                    bool(os.getenv(config.kraken_live.api_key_env))
                    and bool(os.getenv(config.kraken_live.api_secret_env))
                )
                else "fail"
            ),
            detail=(
                "Las credenciales Kraken estan cargadas en el entorno."
                if (
                    bool(os.getenv(config.kraken_live.api_key_env))
                    and bool(os.getenv(config.kraken_live.api_secret_env))
                )
                else "Faltan KRAKEN_API_KEY y/o KRAKEN_API_SECRET en el entorno."
            ),
            critical=True,
        ),
        GateResult(
            category="live",
            name="Executor live real",
            status="pass",
            detail="Ya existe un flujo live integrado para Kraken con balance, preview, submit y cancelacion.",
        ),
        GateResult(
            category="live",
            name="Slippage real instrumentado",
            status="pending",
            detail="Falta medir slippage real contra fills cerrados.",
            critical=True,
        ),
        GateResult(
            category="live",
            name="Maker vs taker instrumentado",
            status="pending",
            detail="Falta separar maker y taker para validar fees y edge neto.",
            critical=True,
        ),
    ]

    operational_status = _category_status(gates, "operational")
    edge_status = _category_status(gates, "edge")
    live_status = _category_status(gates, "live", blocked_label="BLOQUEADO")

    if operational_status != "LISTO":
        verdict = "NO GO"
        summary = "La tuberia base todavia no esta estable. Primero asegura operativa, storage y datos reales."
    elif edge_status == "LISTO" and live_status == "LISTO":
        verdict = "LIVE CANDIDATO"
        summary = "La operativa y el edge pasan las puertas basicas; todavia conviene arrancar con tamano minimo."
    elif edge_status == "LISTO":
        verdict = "PAPER LISTO / LIVE BLOQUEADO"
        summary = "La operativa en paper esta bien, pero live sigue bloqueado por ejecucion y metricas pendientes."
    else:
        verdict = "PAPER OPERATIVO"
        summary = "El sistema corre, guarda y publica bien, pero el edge todavia no esta demostrado."

    return ReadinessReport(
        verdict=verdict,
        summary=summary,
        operational_status=operational_status,
        edge_status=edge_status,
        live_status=live_status,
        metrics=metrics,
        gates=gates,
    )


def _calculate_run_drawdown(rows: list[dict[str, object]]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for row in reversed(rows):
        equity += float(row["pnl"] or 0.0)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _calculate_zero_fill_streak(rows: list[dict[str, object]]) -> int:
    streak = 0
    for row in rows:
        if int(row["fills_count"]) > 0:
            break
        streak += 1
    return streak


def _category_status(gates: list[GateResult], category: str, blocked_label: str = "FALLA") -> str:
    relevant = [gate for gate in gates if gate.category == category]
    if any(gate.status == "fail" and gate.critical for gate in relevant):
        return blocked_label
    if any(gate.status == "pending" and gate.critical for gate in relevant):
        return "PENDIENTE"
    if any(gate.status == "fail" for gate in relevant):
        return blocked_label
    if any(gate.status == "pending" for gate in relevant):
        return "PENDIENTE"
    return "LISTO"
