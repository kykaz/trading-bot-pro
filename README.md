# Trading Bot Pro

Base profesional para un bot de trading con tres flujos separados: mercados de prediccion, `BTC/USD` spot y un puente inicial a `MetaTrader 5`, siempre con `paper trading` o `demo` por defecto, control de riesgo y backtesting.

## Que incluye

- Arquitectura separada por `strategy`, `risk`, `execution` y `backtest`.
- Modo seguro por defecto (`paper`) para validar la logica sin arriesgar capital.
- Estrategia inicial basada en valor esperado entre probabilidad "justa" y precio de mercado.
- Cliente de mercado de ejemplo y punto de integracion para la API oficial de Polymarket.
- Ingestion publica real desde Gamma + CLOB para ejecutar el bot con datos reales en modo `paper`.
- Ingestion spot de `BTC/USD` con venue configurable: Kraken o Alpaca Paper.
- Soporte de broker `paper` real con Alpaca para operar `BTC/USD` sin dinero real.
- Integracion inicial con `MetaTrader 5` para leer velas/ticks, validar la cuenta demo y enviar ordenes demo al terminal.
- Persistencia local de runs, senales y fills en SQLite para auditoria y metricas.
- Persistencia de portfolio `paper` por fuente para poder abrir, mantener y cerrar posiciones entre corridas.
- Publicacion automatica del dashboard a Vercel despues de cada `run-once`.
- Alertas locales cuando una corrida produce senales o fills.
- Configuracion centralizada en `config.toml`.

## Estructura

```text
trading-bot-pro/
  config.toml
  README.md
  trading_bot/
    config.py
    execution.py
    main.py
    market.py
    polymarket.py
    risk.py
    strategy.py
    types.py
```

## Como ejecutar

Para evitar el Python alpha global, usa el wrapper estable del proyecto:

```powershell
cd C:\machine-root\Workspace\trading-bot-pro
.\scripts\trading-bot-venv.cmd live-check --venue mt5
```

Tambien puedes seguir usando `python -m trading_bot.main ...` desde el `.venv`.

```powershell
cd C:\machine-root\Workspace\trading-bot-pro
python -m trading_bot.main backtest
python -m trading_bot.main run-once
python -m trading_bot.main run-once --source real
python -m trading_bot.main run-once --source btcusd
python -m trading_bot.main run-once --source btcusd --live
python -m trading_bot.main run-once --source mt5
python -m trading_bot.main run-once --source mt5 --live
python -m trading_bot.main gold-backtest --preset high-win
python -m trading_bot.main mt5-gold-backtest
python -m trading_bot.main gold-backtest --preset balanced
python -m trading_bot.main gold-backtest --preset defensive
python -m trading_bot.main report runs --limit 5
python -m trading_bot.main report signals --run-id 1 --limit 5
python -m trading_bot.main report fills --limit 5
python -m trading_bot.main readiness
python -m trading_bot.main alpaca-connect --open-browser
python -m trading_bot.main mt5-connect --open-browser
python -m trading_bot.main live-check --venue alpaca
python -m trading_bot.main live-check --venue kraken
python -m trading_bot.main live-check --venue mt5
python -m trading_bot.main mt5-session-status
python -m trading_bot.main preview-order --source btcusd
python -m trading_bot.main preview-order --source btcusd --validate-live
python -m trading_bot.main preview-order --source mt5
python -m trading_bot.main preview-order --source mt5 --validate-live
python -m trading_bot.main force-demo-order --side buy
python -m trading_bot.main force-demo-order --side sell --size 0.02
python -m trading_bot.main kraken-balance
python -m trading_bot.main kraken-open-orders
python -m trading_bot.main submit-order --source btcusd
python -m trading_bot.main submit-order --source btcusd --live
python -m trading_bot.main submit-order --source mt5
python -m trading_bot.main submit-order --source mt5 --live
python -m trading_bot.main cancel-order <txid>
python -m trading_bot.main cancel-all-orders
python -m trading_bot.main dead-man-switch --seconds 60
python -m trading_bot.main portfolio show --source btcusd
python -m trading_bot.main portfolio show --source mt5
python -m trading_bot.main operator-panel --open-browser
python -m trading_bot.main kill-switch status
python -m trading_bot.main dashboard --open-browser
```

Cada `run-once` guarda un registro en `var/trading_bot.db`.
Ademas, cada `run-once` regenera `index.html` y publica el dashboard a Vercel.
Si hay actividad relevante, tambien puede abrir el dashboard y lanzar una alerta local.

## MetaTrader 5

El flujo `mt5` esta pensado para probar estrategias en una cuenta demo y ver el bot enlazado con el terminal.

Hoy incluye:

- `Mt5DataSource`: abre el terminal, lee tick actual y velas recientes del simbolo configurado
- `Mt5XauScalpStrategy`: estrategia base para `XAUUSD` con filtro `M5`, pullback `M1`, `EMA 9`, `RSI(2)` y `ATR`
- `live-check --venue mt5`: confirma si hay terminal, credenciales y cuenta demo
- `preview-order --source mt5`: enseña la orden candidata
- `submit-order --source mt5 --live`: valida y envia una orden demo al terminal
- `run-once --source mt5 --live`: integra ese mismo flujo en un ciclo completo del bot
- `force-demo-order --side buy|sell`: abre una capa demo manual para probar ejecucion
- `mt5_layers`: permite apilar varias entradas del mismo lado y administrar `SL/TP` por capas

Calibracion activa `high-win`:

- sesion `14:00-17:00 UTC`
- `RSI(2) <= 38 / >= 62`
- `TP = 0.53 ATR`
- `SL = 1.7 ATR`
- `core_reclaim_points = 10`
- el benchmark autoritativo ahora es `mt5-gold-backtest`, que replica `modo mixto + capas + administracion`

Configuracion principal:

```toml
[mt5]
symbol = "XAUUSD"
timeframe = "M1"
order_size_lots = 0.01
enable_demo_trading = true
require_demo_account = true
```

Credenciales locales:

- `MT5_LOGIN`
- `MT5_PASSWORD`
- `MT5_SERVER`
- `MT5_TERMINAL_PATH` si el terminal no se detecta solo

La forma correcta de arrancarlo es:

1. `python -m trading_bot.main mt5-connect --open-browser`
2. instalar MetaTrader 5
3. iniciar sesion en una cuenta demo
4. guardar las credenciales en `.env.mt5.local`
5. correr `live-check --venue mt5`
6. luego `preview-order --source mt5 --validate-live`
7. y solo despues `run-once --source mt5 --live`

Atajo para la demo de oro:

```powershell
.\scripts\mt5-xau-demo.cmd
```

Palanca manual para ver una orden demo cuando quieras:

```powershell
python -m trading_bot.main force-demo-order --side buy
python -m trading_bot.main force-demo-order --side sell --size 0.02
```

Comportamiento por capas en `XAUUSD`:

- el bot puede abrir hasta `4` capas `buy` y `4` capas `sell`
- cada capa nueva exige separacion minima de `0.10 ATR` respecto a la ultima entrada del mismo lado
- no hay cooldown fijo entre capas; manda la distancia y el contexto del setup
- al llegar a `2` capas abiertas, el bot mueve `SL/TP` de las capas vivas hacia una gestion de break-even mas defensiva
- si aparece senal del lado opuesto, primero cierra las capas contrarias y luego evalua la nueva entrada
- `mt5-session-status` te dice si la ventana esta activa, si hay setup ahora, cuantas capas hay abiertas y el benchmark vigente del setup

Backtest fiel del flujo MT5:

```powershell
python -m trading_bot.main mt5-gold-backtest
python -m trading_bot.main mt5-gold-backtest --data download\xauusd-m1-bid-2025-10-22-2026-04-22.json
```

Ese comando usa la estrategia MT5 real, `modo mixed`, capas, cierres por cambio de lado y drawdown por `equity`, asi que es la referencia correcta para validar la estrategia de demo.

Si el terminal no esta instalado, el bot ya falla limpio con mensajes como `IPC initialize failed, MetaTrader 5 x64 not found` en vez de soltar un traceback.

## XAUUSD scalping

Tambien dejÃ© un backtest simple de `XAUUSD` usando datos `M1` de Dukascopy ya descargados en `download/`.

La idea es deliberadamente simple:

- filtro de tendencia con `EMA 20/50` en `M5`
- entrada de pullback sobre `EMA 9` en `M1`
- confirmacion con `RSI(2)`
- `TP/SL` expresados en `ATR`

Puedes correrlo asi:

```powershell
python -m trading_bot.main gold-backtest --preset high-win
python -m trading_bot.main gold-backtest --preset balanced
```

Presets incluidos:

- `high-win`: prioriza superar `65%` de win rate con `TP` pequeno y `SL` amplio
- `balanced`: sigue siendo simple, pero intenta una relacion riesgo/beneficio menos extrema
- `defensive`: reduce algo el drawdown, aunque suele sacrificar parte de la ganancia esperada

Importante: que el `win rate` sea alto no significa que la estrategia sea solida. En oro scalping es muy facil inflar el porcentaje de acierto a costa de stops demasiado grandes.

## BTC/USD spot

El flujo `BTC/USD` ahora se resuelve segun `[spot].venue` en `config.toml`.

Hoy el default queda en `alpaca`, asi que `run-once --source btcusd` intenta operar contra Alpaca Paper si cargaste credenciales. Si quieres volver a lectura pura de mercado con Kraken, cambia:

```toml
[spot]
venue = "kraken"
```

### Alpaca Paper

Con `spot.venue = "alpaca"` el bot usa:

- market data crypto desde `data.alpaca.markets`
- cuenta y ordenes paper desde `paper-api.alpaca.markets`
- credenciales `APCA_API_KEY_ID` y `APCA_API_SECRET_KEY`
- archivo local `.env.alpaca.local` desde la cabina o el entorno

En ese modo:

- `run-once --source btcusd` ya no solo simula localmente: manda una orden al paper broker si hay setup
- `alpaca-connect --open-browser` abre el alta, el login y la documentacion correcta de Alpaca, y deja sembrado `.env.alpaca.local`
- `preview-order --source btcusd --validate-live` valida acceso a la cuenta paper
- `kraken-balance` y `kraken-open-orders` siguen existiendo como comandos, pero muestran el venue spot activo; con Alpaca te devuelven cuenta y ordenes de Alpaca
- `portfolio show --source btcusd` lee portfolio remoto del paper broker cuando Alpaca paper esta habilitado
- `portfolio reset --source btcusd` cancela ordenes abiertas y puede cerrar la posicion del par en Alpaca paper

### Kraken Spot

Si `spot.venue = "kraken"`, el comando `run-once --source btcusd` usa Kraken Spot REST:

- `GET /0/public/AssetPairs` para metadatos del par
- `GET /0/public/Depth` para libro de ordenes
- `GET /0/public/Ticker` para ultimo trade, VWAP y volumen

Encima de eso, el bot:

- calcula `mid`, `microprice`, profundidad y desequilibrio
- genera una senal spot separada del flujo binario
- persiste un portfolio `paper` en `var/portfolio/btcusd.json`
- marca la equidad al cierre, no solo el cash

Esto ya permite un flujo operativo basico para `BTC/USD`: comprar, mantener la posicion entre corridas y luego vender cuando la senal gire.

Si activas live real en config y cargas credenciales, `run-once --source btcusd --live` hace esto:

- lee balance real de Kraken para calcular el sizing sobre la cuenta real
- revisa si ya existen ordenes abiertas en `XBT/USD`
- arma el dead-man switch si `kraken_live.auto_arm_dead_man_switch = true`
- intenta enviar la orden solo si `kraken.enable_live_trading = true`, `kraken_live.enabled = true`, `kraken_live.dry_run = false` y el kill switch esta apagado

Con Alpaca, `--live` sigue bloqueado por defecto; la integracion operativa fuerte hoy es `paper broker`, no dinero real.

## Datos reales

El comando `run-once --source real` hace esto:

- descubre mercados activos via `https://gamma-api.polymarket.com/markets`
- toma el token `Yes` de cada mercado
- consulta orderbooks por lote via `POST https://clob.polymarket.com/books`
- estima un `fair_probability` usando midpoint y desbalance de liquidez

Esta senal es una base microestructural, no una ventaja estadistica demostrada. Sirve para montar y validar la tuberia completa antes de conectar un modelo mejor.

## Persistencia

El bot guarda:

- `runs`: una fila por ejecucion con cash inicial/final y conteos
- `signals`: todas las senales emitidas con features JSON
- `fills`: todas las ejecuciones simuladas

La base vive en `var/trading_bot.db` y la estructura esta pensada para migrar luego a Postgres sin cambiar el modelo de datos.

Tambien puedes inspeccionar la actividad sin abrir SQLite manualmente:

- `report runs`: resumen de ejecuciones recientes
- `report signals`: senales recientes con features
- `report fills`: ejecuciones recientes con nocional y fees
- `readiness`: semaforo operativo con veredicto `paper` vs `live`
- `live-check`: valida entorno, auth y kill switch para live trading
- `preview-order`: muestra la siguiente orden candidata sin ejecutarla
- `kill-switch`: enciende o apaga el bloqueo total de live trading
- `dashboard`: genera `var/dashboard.html` con una vista visual local
- `operator-panel`: cabina local para operar `BTC/USD` paper con el venue spot activo

## Readiness

El comando `readiness` convierte la idea de `go / no-go` en algo operativo:

- `Operativa`: confirma si la tuberia base esta sana
- `Edge`: confirma si ya existe evidencia suficiente en corridas reales
- `Live`: marca que sigue bloqueado mientras falten ejecucion real y metricas cerradas

Hoy el veredicto maximo realista del proyecto es `PAPER OPERATIVO` o `PAPER LISTO / LIVE BLOQUEADO`.

## Vercel

El proyecto esta enlazado a Vercel y publicado en produccion.

- Produccion: `https://trading-bot-pro.vercel.app`
- El bot regenera `index.html` y publica solo cuando hay actividad relevante
- Tambien respeta un cooldown entre deploys para no quemar cuota de Vercel

Si quieres pausar esto, cambia `vercel.auto_publish_dashboard = false` en `config.toml`.

## Live layer

La capa live sigue en modo seguro:

- `live.enabled = false`
- `live.dry_run = true`
- `live-check` te dice si faltan credenciales y si el kill switch esta activo
- `preview-order` te deja inspeccionar una orden candidata antes de construir el executor real
- `preview-order --source btcusd --validate-live` valida una orden Kraken sin enviarla
- `preview-order --source btcusd --validate-live` con Alpaca valida acceso a la cuenta paper
- `kraken-balance` y `kraken-open-orders` ya hablan con el venue spot autenticado
- `submit-order --source btcusd` valida la orden candidata contra Kraken
- `submit-order --source btcusd --live` intenta enviarla de verdad, pero solo si desactivaste `dry_run`, activaste `enable_live_trading` y el kill switch esta apagado
- `run-once --source btcusd --live` integra ese mismo flujo en el ciclo principal del bot
- `cancel-order` y `cancel-all-orders` permiten limpiar riesgo desde terminal
- `dead-man-switch` arma el "dead man's switch" de Kraken si ya cargaste credenciales
- para Kraken necesitas `KRAKEN_API_KEY` y `KRAKEN_API_SECRET`
- para Alpaca Paper necesitas `APCA_API_KEY_ID` y `APCA_API_SECRET_KEY`
- `[alpaca_paper]` controla:
  - `enabled`
  - `cancel_existing_before_submit`
  - `skip_if_open_orders`
  - `close_positions_on_reset`
- `[kraken_live]` tambien controla:
  - `auto_arm_dead_man_switch`
  - `skip_if_open_orders`
  - `cancel_existing_before_submit`
- `operator-panel` abre una cabina local con botones reales para paper, live-check, preview, kill switch, submit y cancel-all
- la cabina tambien deja guardar credenciales del venue spot activo:
  - `.env.alpaca.local` si usas Alpaca
  - `.env.kraken.local` si usas Kraken

El kill switch usa `var/kill_switch.flag`. Si existe, cualquier capa live futura debe quedar bloqueada.

## Alertas

El bot puede avisarte cuando haya actividad:

- sonido local
- apertura automatica del dashboard
- linea `alert_triggered=...` en consola

Controlalo desde `[alerts]` en `config.toml`.

## Proximos pasos recomendados

1. Reemplazar la heuristica de fair value por un modelo real de prediccion o research pipeline.
2. Persistir fills, equity curve y metricas en Postgres.
3. Anadir alertas, logs estructurados y dashboard.
4. Implementar credenciales L1/L2 y ordenes reales solo despues de validar paper trading.
5. Mantener `paper trading` hasta validar backtests, simulacion y limites de riesgo.

## Nota importante

La ejecucion real esta deshabilitada deliberadamente. Antes de activar live trading, valida:

- restricciones regulatorias y geograficas aplicables
- claves seguras y rotacion
- limites de perdida diarios y por mercado
- monitoreo y kill switch
