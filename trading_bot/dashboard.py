from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path

from trading_bot.readiness import ReadinessReport
from trading_bot.storage import SQLiteStorage


@dataclass(slots=True)
class DashboardData:
    runs: list[dict[str, object]]
    signals: list[dict[str, object]]
    fills: list[dict[str, object]]
    readiness: ReadinessReport | None = None

    @property
    def total_signals(self) -> int:
        return sum(int(run["signals_count"]) for run in self.runs)

    @property
    def total_fills(self) -> int:
        return sum(int(run["fills_count"]) for run in self.runs)

    @property
    def latest_run(self) -> dict[str, object] | None:
        return self.runs[0] if self.runs else None

    @property
    def latest_pnl(self) -> float:
        latest = self.latest_run
        if latest is None:
            return 0.0
        return float(latest["pnl"] or 0.0)

    @property
    def hit_rate(self) -> float:
        if self.total_signals == 0:
            return 0.0
        return round((self.total_fills / self.total_signals) * 100.0, 1)

    @property
    def latest_status(self) -> str:
        latest = self.latest_run
        if latest is None:
            return "Sin actividad"
        fills = int(latest["fills_count"])
        signals = int(latest["signals_count"])
        if fills > 0:
            return "Activo y ejecutando"
        if signals > 0:
            return "Observando sin ejecutar"
        return "Inactivo"

    @property
    def current_bias(self) -> str:
        if not self.signals:
            return "Sin sesgo claro"
        score = 0
        for row in self.signals[:6]:
            score += 1 if str(row["side"]) == "buy" else -1
        if score > 0:
            return "Sesgo comprador"
        if score < 0:
            return "Sesgo vendedor"
        return "Balanceado"


def build_dashboard_html(data: DashboardData) -> str:
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Panel del Bot de Trading</title>
  <style>
    :root {{
      --bg: #07111b;
      --bg-2: #0b1824;
      --panel: rgba(10, 24, 37, 0.88);
      --panel-2: rgba(12, 28, 42, 0.92);
      --line: rgba(137, 173, 201, 0.12);
      --ink: #edf5fb;
      --muted: #89a8bd;
      --teal: #22c55e;
      --cyan: #22d3ee;
      --amber: #f59e0b;
      --red: #ef4444;
      --glow: 0 24px 80px rgba(0, 0, 0, 0.35);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Arial, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(34,211,238,0.10), transparent 26%),
        radial-gradient(circle at 85% 15%, rgba(245,158,11,0.08), transparent 24%),
        linear-gradient(180deg, var(--bg) 0%, var(--bg-2) 100%);
      color: var(--ink);
      min-height: 100vh;
    }}
    .shell {{
      width: min(1280px, calc(100% - 28px));
      margin: 18px auto 36px;
      display: grid;
      gap: 16px;
    }}
    .hero {{
      padding: 22px;
      border-radius: 28px;
      background: linear-gradient(180deg, rgba(10,24,37,0.92), rgba(7,17,27,0.96));
      border: 1px solid var(--line);
      box-shadow: var(--glow);
    }}
    .eyebrow {{
      color: var(--cyan);
      font-size: 12px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 10px 0 8px;
      font-size: clamp(32px, 5vw, 58px);
      line-height: 0.95;
      letter-spacing: -0.05em;
      max-width: 820px;
    }}
    .sub {{
      margin: 0;
      color: var(--muted);
      max-width: 760px;
      line-height: 1.55;
      font-size: 15px;
    }}
    .hero-grid {{
      display: grid;
      grid-template-columns: 1.2fr 1fr;
      gap: 16px;
      margin-top: 20px;
    }}
    .big-board {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }}
    .metric {{
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      background: linear-gradient(180deg, rgba(14,30,44,0.98), rgba(9,22,34,0.92));
    }}
    .metric-label {{
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
    }}
    .metric-value {{
      margin-top: 10px;
      font-size: 28px;
      font-weight: 700;
      letter-spacing: -0.04em;
    }}
    .green {{ color: var(--teal); }}
    .amber {{ color: var(--amber); }}
    .red {{ color: var(--red); }}
    .cyan {{ color: var(--cyan); }}
    .decision {{
      border: 1px solid rgba(34,211,238,0.15);
      border-radius: 22px;
      padding: 18px;
      background: linear-gradient(180deg, rgba(8,22,33,0.98), rgba(8,22,33,0.82));
      display: grid;
      gap: 14px;
    }}
    .decision h2 {{
      margin: 0 0 8px;
      font-size: 22px;
      letter-spacing: -0.03em;
    }}
    .decision p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
    }}
    .decision-badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 9px 12px;
      border-radius: 999px;
      background: rgba(34,211,238,0.10);
      color: var(--cyan);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      width: fit-content;
    }}
    .status-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }}
    .status-card {{
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px;
      background: rgba(255,255,255,0.02);
    }}
    .status-card strong {{
      display: block;
      margin-top: 8px;
      font-size: 18px;
      letter-spacing: -0.03em;
    }}
    .blockers {{
      display: grid;
      gap: 8px;
      padding: 0;
      margin: 0;
      list-style: none;
    }}
    .blockers li {{
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(239,68,68,0.08);
      border: 1px solid rgba(239,68,68,0.16);
      color: #f8d7d7;
      font-size: 13px;
      line-height: 1.45;
    }}
    .grid {{
      display: grid;
      gap: 16px;
      grid-template-columns: 1.1fr 0.9fr;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--glow);
      overflow: hidden;
    }}
    .panel-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,0.01);
    }}
    .panel-title {{
      margin: 0;
      font-size: 22px;
      letter-spacing: -0.03em;
    }}
    .panel-sub {{
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .chip {{
      font-size: 11px;
      padding: 7px 10px;
      border-radius: 999px;
      background: rgba(34,211,238,0.09);
      color: var(--cyan);
      text-transform: uppercase;
      letter-spacing: 0.12em;
    }}
    .stack {{
      display: grid;
      gap: 12px;
      padding: 14px;
    }}
    .card {{
      background: linear-gradient(180deg, rgba(14,30,44,0.98), rgba(10,24,37,0.92));
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
    }}
    .card-top {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
    }}
    .card-title {{
      font-size: 18px;
      font-weight: 600;
      letter-spacing: -0.03em;
    }}
    .card-copy {{
      margin-top: 10px;
      color: var(--muted);
      line-height: 1.5;
      font-size: 14px;
    }}
    .meta-line {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px 16px;
      margin-top: 12px;
      color: var(--muted);
      font-size: 13px;
    }}
    .run-table {{
      display: grid;
      gap: 1px;
      background: var(--line);
    }}
    .run-row {{
      display: grid;
      grid-template-columns: 90px 1fr 120px;
      gap: 12px;
      align-items: center;
      padding: 14px 18px;
      background: var(--panel-2);
    }}
    .small {{
      font-size: 12px;
      color: var(--muted);
    }}
    .mono {{
      font-family: "Consolas", "Courier New", monospace;
      font-size: 12px;
      color: var(--muted);
    }}
    .empty {{
      padding: 18px;
      color: var(--muted);
    }}
    @media (max-width: 980px) {{
      .hero-grid, .grid {{ grid-template-columns: 1fr; }}
      .big-board {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .status-grid {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 720px) {{
      .shell {{ width: min(100% - 16px, 1280px); }}
      .hero {{ padding: 18px; }}
      .big-board {{ grid-template-columns: 1fr; }}
      .run-row {{ grid-template-columns: 70px 1fr; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="eyebrow">Trading Bot Pro</div>
      <h1>Primero mira la mesa. El detalle puede esperar.</h1>
      <p class="sub">Esta vista esta pensada para una lectura rapida de trader: primero el estado del sistema, luego el sesgo actual y la actividad ejecutada, y ahora tambien el semaforo operativo para saber si sigues en paper o si live esta bloqueado.</p>
      <div class="hero-grid">
        <div class="big-board">
          <article class="metric">
            <div class="metric-label">Estado de mesa</div>
            <div class="metric-value">{escape(data.latest_status)}</div>
          </article>
          <article class="metric">
            <div class="metric-label">Sesgo actual</div>
            <div class="metric-value amber">{escape(data.current_bias)}</div>
          </article>
          <article class="metric">
            <div class="metric-label">PnL ultima corrida</div>
            <div class="metric-value {'green' if data.latest_pnl >= 0 else 'red'}">{data.latest_pnl:.2f}</div>
          </article>
          <article class="metric">
            <div class="metric-label">Tasa de acierto</div>
            <div class="metric-value">{data.hit_rate:.1f}%</div>
          </article>
        </div>
        <aside class="decision">
          <div>
            <h2>Semaforo operativo</h2>
            <div class="decision-badge">{render_readiness_badge(data.readiness)}</div>
          </div>
          <p>{render_readiness_status(data.readiness)}</p>
          <div class="status-grid">
            {render_status_card("Operativa", data.readiness.operational_status if data.readiness else "PENDIENTE")}
            {render_status_card("Edge", data.readiness.edge_status if data.readiness else "PENDIENTE")}
            {render_status_card("Live", data.readiness.live_status if data.readiness else "PENDIENTE")}
          </div>
          <div>
            <h2>Bloqueos criticos</h2>
            {render_readiness_blockers(data.readiness)}
          </div>
          <div>
            <h2>Que mirar ahora</h2>
            <p>{build_quick_read(data)}</p>
          </div>
        </aside>
      </div>
    </section>

    <section class="grid">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2 class="panel-title">Tablero de accion</h2>
            <p class="panel-sub">Las senales mas recientes, convertidas en setups para ver primero lo importante.</p>
          </div>
          <span class="chip">{len(data.signals[:4])} setups</span>
        </div>
        {render_signal_cards(data.signals[:4])}
      </section>
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2 class="panel-title">Cinta de ejecucion</h2>
            <p class="panel-sub">Lo que realmente se ejecuto, con tamano y fee visibles de inmediato.</p>
          </div>
          <span class="chip">{len(data.fills[:4])} fills</span>
        </div>
        {render_fill_cards(data.fills[:4])}
      </section>
    </section>

    <section class="grid">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2 class="panel-title">Ultima corrida</h2>
            <p class="panel-sub">Una lectura corta y clara del ultimo ciclo.</p>
          </div>
          <span class="chip">ahora</span>
        </div>
        {render_latest_run(data.latest_run)}
      </section>
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2 class="panel-title">Historial de corridas</h2>
            <p class="panel-sub">Memoria corta. La suficiente para ver consistencia sin ahogarte en filas.</p>
          </div>
          <span class="chip">{len(data.runs[:6])} corridas</span>
        </div>
        {render_run_rows(data.runs[:6])}
      </section>
    </section>
  </main>
</body>
</html>"""


def build_quick_read(data: DashboardData) -> str:
    latest = data.latest_run
    if latest is None:
        return "Todavia no hay corridas registradas. Ejecuta un ciclo en paper trading para que el panel tenga contexto."
    fills = int(latest["fills_count"])
    signals = int(latest["signals_count"])
    pnl = float(latest["pnl"] or 0.0)
    bias = data.current_bias.lower()
    if fills == 0 and signals == 0:
        return "La mesa esta quieta ahora mismo. Nada supero el umbral, asi que el bot se quedo fuera."
    if fills == 0:
        return f"El sistema encontro {signals} setups pero no comprometio capital. Ahora mismo la mesa esta en {bias} y esperando entradas mas limpias."
    return f"El ultimo ciclo genero {signals} setups, ejecuto {fills} fills y cerro en {pnl:.2f}. La mesa esta ahora en {bias}."


def render_latest_run(run: dict[str, object] | None) -> str:
    if run is None:
        return '<div class="empty">Todavia no hay historial de corridas.</div>'
    pnl = float(run["pnl"] or 0.0)
    return f"""
    <div class="stack">
      <article class="card">
        <div class="card-top">
          <div class="card-title">Corrida #{run['id']}</div>
          <div class="small">{escape(str(run['data_source']))} / {escape(str(run['mode']))}</div>
        </div>
        <div class="card-copy">Equidad inicial {float(run['starting_cash']):.2f}. Equidad final {float(run['ending_cash'] or run['starting_cash']):.2f}. Esta corrida termino {'arriba' if pnl >= 0 else 'abajo'} en la sesion.</div>
        <div class="meta-line">
          <span>Senales {run['signals_count']}</span>
          <span>Fills {run['fills_count']}</span>
          <span>Abiertas {run['open_positions']}</span>
          <span class="{'green' if pnl >= 0 else 'red'}">PnL {pnl:.2f}</span>
        </div>
      </article>
    </div>
    """


def render_run_rows(rows: list[dict[str, object]]) -> str:
    if not rows:
        return '<div class="empty">Todavia no hay corridas.</div>'
    items = "".join(
        f"""
        <div class="run-row">
          <div class="mono">#{row['id']}</div>
          <div>
            <div>{escape(str(row['data_source']))} / {escape(str(row['command']))}</div>
            <div class="small">senales {row['signals_count']} | fills {row['fills_count']} | abiertas {row['open_positions']}</div>
          </div>
          <div class="{'green' if float(row['pnl'] or 0.0) >= 0 else 'red'}">{float(row['pnl'] or 0.0):.2f}</div>
        </div>
        """
        for row in rows
    )
    return f'<div class="run-table">{items}</div>'


def render_signal_cards(rows: list[dict[str, object]]) -> str:
    if not rows:
        return '<div class="empty">No hay setups ahora mismo.</div>'
    items = "".join(
        f"""
        <article class="card">
          <div class="card-top">
            <div class="card-title">{'Setup largo' if str(row['side']) == 'buy' else 'Setup corto'}</div>
            <div class="small">run #{row['run_id']}</div>
          </div>
          <div class="card-copy">{build_signal_summary(row)}</div>
          <div class="meta-line">
            <span>Confianza {float(row['confidence']):.3f}</span>
            <span>Edge {float(row['expected_edge']):.3f}</span>
            <span class="mono">{escape(short_market(str(row['market_id'])))}</span>
          </div>
        </article>
        """
        for row in rows
    )
    return f'<div class="stack">{items}</div>'


def render_fill_cards(rows: list[dict[str, object]]) -> str:
    if not rows:
        return '<div class="empty">Todavia no hay ejecuciones.</div>'
    items = "".join(
        f"""
        <article class="card">
          <div class="card-top">
            <div class="card-title">{'Compro exposicion' if str(row['side']) == 'buy' else 'Vendio exposicion'}</div>
            <div class="small">run #{row['run_id']}</div>
          </div>
          <div class="card-copy">Se ejecuto a {float(row['price']):.4f} con tamano {float(row['size']):.6f}. El fee pagado fue {float(row['fee_paid']):.2f}.</div>
          <div class="meta-line">
            <span>Nocional {float(row['price']) * float(row['size']):.2f}</span>
            <span>Fee {float(row['fee_paid']):.2f}</span>
            <span class="mono">{escape(short_market(str(row['market_id'])))}</span>
          </div>
        </article>
        """
        for row in rows
    )
    return f'<div class="stack">{items}</div>'


def build_signal_summary(row: dict[str, object]) -> str:
    side = "por debajo" if str(row["side"]) == "buy" else "por encima"
    return (
        f"El modelo ve al mercado cotizando {side} de su estimacion justa. "
        f"La confianza es {float(row['confidence']):.2f} y el edge esperado es {float(row['expected_edge']):.3f}."
    )


def short_market(market_id: str) -> str:
    if len(market_id) <= 18:
        return market_id
    return f"{market_id[:8]}...{market_id[-6:]}"


def render_readiness_badge(readiness: ReadinessReport | None) -> str:
    if readiness is None:
        return "SIN CHEQUEO"
    return escape(readiness.verdict)


def render_readiness_status(readiness: ReadinessReport | None) -> str:
    if readiness is None:
        return "Todavia no se evaluo readiness. Ejecuta el chequeo para ver si el sistema esta listo o bloqueado."
    return escape(readiness.summary)


def render_readiness_blockers(readiness: ReadinessReport | None) -> str:
    if readiness is None or not readiness.blockers:
        return '<ul class="blockers"><li>No hay bloqueos criticos registrados.</li></ul>'
    items = "".join(f"<li>{escape(blocker)}</li>" for blocker in readiness.blockers)
    return f'<ul class="blockers">{items}</ul>'


def render_status_card(label: str, status: str) -> str:
    class_name = {
        "LISTO": "green",
        "BLOQUEADO": "red",
        "FALLA": "red",
        "PENDIENTE": "amber",
    }.get(status, "cyan")
    return (
        '<article class="status-card">'
        f'<span class="small">{escape(label)}</span>'
        f'<strong class="{class_name}">{escape(status)}</strong>'
        "</article>"
    )


def build_dashboard_file(
    storage: SQLiteStorage,
    output_path: str,
    limit: int = 20,
    readiness: ReadinessReport | None = None,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    data = DashboardData(
        runs=storage.fetch_recent_runs(limit=limit),
        signals=storage.fetch_recent_signals(limit=limit),
        fills=storage.fetch_recent_fills(limit=limit),
        readiness=readiness,
    )
    output.write_text(build_dashboard_html(data), encoding="utf-8")
    return output
