from __future__ import annotations

from pathlib import Path

from trading_bot.operator_panel import build_operator_panel_html, build_operator_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = PROJECT_ROOT / "public_operator.html"

PUBLIC_NOTE = (
    "Vista publicada en Vercel. Esta cabina es solo lectura: los mandos reales, "
    "credenciales y ejecucion siguen estando en la mesa local."
)

PUBLIC_CSS = """
.public-banner {
  border: 1px solid rgba(255, 181, 71, 0.28);
  border-radius: 18px;
  padding: 14px 16px;
  background: linear-gradient(180deg, rgba(46, 27, 7, 0.85), rgba(22, 13, 4, 0.92));
  color: #ffd48f;
  display: grid;
  gap: 6px;
}
.public-banner strong {
  font-size: 11px;
  letter-spacing: .16em;
  text-transform: uppercase;
}
.public-banner span {
  font-size: 14px;
  line-height: 1.45;
}
.readonly-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  border: 1px solid rgba(255, 181, 71, 0.30);
  border-radius: 999px;
  padding: 7px 10px;
  color: #ffd48f;
  background: rgba(255, 181, 71, 0.08);
  font-size: 11px;
  letter-spacing: .14em;
  text-transform: uppercase;
}
.readonly-note {
  border: 1px dashed rgba(255, 181, 71, 0.22);
  border-radius: 14px;
  padding: 12px 14px;
  color: #d6dce4;
  background: rgba(255, 255, 255, 0.02);
  font-size: 13px;
  line-height: 1.45;
}
form[data-readonly="true"] {
  pointer-events: none;
}
button[disabled] {
  opacity: 0.48;
  filter: saturate(0.7);
  cursor: not-allowed;
}
"""


def main() -> None:
    snapshot = build_operator_snapshot()
    html = build_operator_panel_html(
        snapshot,
        last_action="Modo publico",
        last_output=PUBLIC_NOTE,
    )
    html = html.replace(
        "<title>Cabina de Operador</title>",
        "<title>Cabina Publica del Bot</title>",
        1,
    )
    html = html.replace("</style>", f"{PUBLIC_CSS}\n  </style>", 1)
    html = html.replace(
        '<main class="shell">',
        '<main class="shell"><section class="public-banner"><strong>Modo publico</strong><span>'
        + PUBLIC_NOTE
        + "</span></section>",
        1,
    )
    html = html.replace(
        '<div><h2>Puente MT5</h2><p>Estado del terminal, credenciales locales y seguro de ejecucion demo.</p></div>',
        '<div><h2>Puente MT5</h2><p>Estado del terminal, credenciales locales y seguro de ejecucion demo.</p></div><span class="readonly-pill">solo lectura</span>',
        1,
    )
    html = html.replace(
        '<div><h2>Armado Live</h2><p>Claves, flags y seguro de fuego real.</p></div>',
        '<div><h2>Armado Live</h2><p>Claves, flags y seguro de fuego real.</p></div><span class="readonly-pill">solo lectura</span>',
        1,
    )
    html = html.replace(
        '<div><h2>Mandos</h2><p>Organizados por ciclo, cuenta, riesgo y fuego real.</p></div>',
        '<div><h2>Mandos</h2><p>Organizados por ciclo, cuenta, riesgo y fuego real.</p></div><span class="readonly-pill">solo lectura</span>',
        1,
    )
    html = html.replace(
        '<div class="panel-body">',
        '<div class="panel-body"><div class="readonly-note">Los botones de esta version web son informativos. Para ejecutar acciones reales o de paper, usa la cabina local en tu equipo.</div>',
        2,
    )
    html = html.replace('<form method="post" action="/action"', '<form method="post" action="/action" data-readonly="true"')
    html = html.replace('<button ', '<button disabled aria-disabled="true" ')
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"public_operator_page={OUTPUT_PATH}")


if __name__ == "__main__":
    main()
