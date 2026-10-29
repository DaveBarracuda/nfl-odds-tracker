# NFL Odds Tracker

Toma snapshots de momios NFL de 10 casas, junta los % de tickets y dinero, clasifica los movimientos de línea y manda alertas a Telegram. Corre en GitHub Actions y genera un dashboard HTML.

## Cómo corre

El `cron` de GitHub Actions no sirve para esto: medido sobre 40 ejecuciones programadas, los huecos fueron de **124 a 444 minutos** y ninguno bajó de 40, cuando el cron pedía cada 30. GitHub desprioriza los disparos programados de los repos poco activos y no hay forma de exigirle puntualidad.

Así que la cadencia no la pone el cron, sino el propio job de [`.github/workflows/tracker.yml`](.github/workflows/tracker.yml):

1. Recupera la base SQLite de la rama `state`.
2. Le pregunta a `python -m tracker next --seconds` cuánto falta para el próximo snapshot y **duerme exactamente eso**. No despierta en balde, así que no gasta créditos de más.
3. Toma el snapshot, busca splits, detecta movimientos y manda alertas a Telegram.
4. Guarda la base y el dashboard en la rama `state` (un solo commit que se reemplaza, así el repo no crece).
5. Repite hasta cumplir 5.5 h (el límite de un job son 6) y entonces **se relanza a sí mismo** con `workflow_dispatch`, que es la excepción documentada a que el token del workflow no puede disparar workflows. La cadena se sostiene sola.

El `cron` cada 3 horas quedó solo como respaldo: mientras el loop vive, los disparos programados esperan detrás del grupo de concurrencia, y si la cadena se rompe, el siguiente la revive. Si un snapshot llega con un hueco de más de 2.5 h, el tracker avisa por Telegram que la cadena se cortó.

Para detener la cadena: `gh workflow disable tracker.yml` (reactivar con `enable`).

### Cadencia

El espaciado se aprieta conforme se acerca el kickoff, porque lo que vale para el modelo de picks es la línea de cierre:

| Cuándo | Un snapshot cada | Nivel |
|---|---|---|
| Más de 2 h antes del kickoff | 55 min | `horario` |
| 2 h antes o menos | 25 min | `pregame` |
| 30 min antes o menos | 9 min | `cierre` |

Medido en simulación contra un calendario real de 14 partidos, el último snapshot antes del kickoff cae entre **3 y 7 minutos** del arranque. Los umbrales están en `tracker/config.py` (`HOURLY_GAP_MIN`, `PREGAME_GAP_MIN`, `CLOSING_GAP_MIN` y sus ventanas).

Los logs de cada ejecución están en la pestaña **Actions** → *Tracker NFL*.

### Ver el dashboard

Doble clic en `ver_dashboard.bat` (o `python -m tracker sync --open`). Descarga de la rama `state` el dashboard y la base más recientes a `dashboard/remote.html` y `data/remote.db`, y abre el dashboard en el navegador. Usa tu sesión de git/GitHub, así que el repo sigue privado.

### Ejecución manual

**Actions → Tracker NFL → Run workflow**. Opciones: `force` toma snapshot aunque no toque (gasta 3 créditos) y `dry_run` no envía a Telegram.

### Secretos del repo

`ODDS_API_KEY`, `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` (Settings → Secrets and variables → Actions). Desde la carpeta del proyecto con tu `.env`:

```bash
gh secret set -f .env
```

## Uso local

Python 3.10+, solo librería estándar. Con un `.env` como `.env.example`:

| Comando | Qué hace |
|---|---|
| `python -m tracker run` | Decide si toca snapshot, detecta y alerta. `--force` toma snapshot siempre, `--dry-run` no envía a Telegram. |
| `python -m tracker status` | Último snapshot, créditos, próximo snapshot y estado de Telegram. |
| `python -m tracker next [--seconds]` | Cuándo toca el próximo snapshot. Con `--seconds` imprime solo la espera: es el reloj del loop del workflow. |
| `python -m tracker analyze [--snapshot N]` | Repite la detección sobre un snapshot sin enviar nada. |
| `python -m tracker dashboard --open` | Regenera y abre `dashboard/index.html`. |
| `python -m tracker telegram-setup` / `telegram-test` | Guarda el chat ID de Telegram / manda un mensaje de prueba. |
| `python -m unittest` | Pruebas. |

La base local (`data/tracker.db`) es independiente de la rama `state`. `scripts/install_task.ps1` programa la ejecución en Windows como alternativa; no lo actives a la vez que el workflow o habrá snapshots y alertas duplicadas.

## Créditos y minutos

- **The Odds API:** cada snapshot cuesta 3 créditos (3 mercados; hasta 10 casas cuentan como una región). La lista de partidos es gratis. Con la cadencia de arriba son ~965 snapshots al mes, unos **2,900 créditos mensuales**. Si no hay partidos en los próximos 7 días no gasta.
- **GitHub Actions:** el repo es público, así que los minutos son gratis e ilimitados. Es justo lo que hace viable el loop: en un repo privado, 5.5 h de job encadenadas todo el día costarían ~43,000 minutos al mes contra los 2,000 del plan gratis.

## Fuentes

- **Líneas:** The Odds API — Pinnacle, LowVig, BetOnline (sharp), DraftKings, FanDuel, BetMGM, ESPN Bet, BetRivers, Hard Rock, Bovada.
- **Splits:** Action Network (consenso, JSON) y DK Network (splits propios de DraftKings, HTML). No son APIs oficiales: si una falla, el snapshot sigue y queda registrado en el log.

## La línea de apertura es la base

Cada snapshot se mide contra **dos** referencias:

1. **El snapshot anterior**, que da la velocidad: cuántas casas se movieron y en cuánto tiempo.
2. **La línea de apertura**, que dice si el movimiento es real o ya se deshizo.

La apertura se guarda la primera vez que el tracker ve el partido, por casa y para el consenso (este último solo cuando hay al menos 3 casas, para que una casa madrugadora no fije la referencia). Es la primera línea **observada**, no necesariamente la primera que publicó la casa: por eso el dashboard muestra siempre la fecha y con cuántas casas se registró.

De ahí salen tres lecturas, visibles en cada alerta y en el dashboard:

| Estado | Qué significa |
|---|---|
| Sostenido | El neto contra la apertura llega al umbral de movimiento: hubo un aumento o un decaimiento real. |
| Leve | Se movió, pero por debajo del umbral. |
| Retorno | La línea volvió exactamente a su apertura después de un recorrido de ida y vuelta real (≥ 1 pt en spread, 1.5 en total, 4 pp en ML). |
| Estable | Nunca se despegó de la apertura. |

Además se guarda el **máximo alcanzado**: hasta dónde llegó la línea desde que abrió. Un partido puede estar hoy a medio punto de su apertura habiendo recorrido punto y medio, y eso se ve en el dashboard y en la tabla de partidos. Los extremos sobreviven al borrado de historial, así que el recorrido no se pierde aunque se compacten las líneas viejas.

Cuando se detecta un retorno, las alertas previas de ese partido y mercado quedan marcadas como **revertidas** en el dashboard: su movimiento ya no se sostiene.

## Cómo se clasifica

Para cada partido y mercado se calcula el consenso y se compara contra el snapshot anterior y contra la apertura. En spread y total el consenso es la línea que más casas repiten (en empate, la de las casas sharp); en moneyline es la mediana de la probabilidad implícita sin vig del local. Las casas que se alejan demasiado de la mediana quedan fuera.

| Tipo | Regla | Telegram |
|---|---|---|
| Información | Consenso salta ≥ 1.5 pts (spread), ≥ 2 (total) o ≥ 6 pp (ML) con ≥ 70% de las casas en ≤ 75 min. Suma si pasa por un número clave. | Sí |
| Vapor | ≥ 3 casas se mueven igual en ≤ 75 min y ≤ 1 en contra. Suma si una casa sharp se movió primero. | Sí |
| RLM | El consenso se mueve ≥ 0.5 pts (2 pp en ML) contra el lado con ≥ 60% de tickets. Suma si el dinero va con el movimiento. | Sí |
| Gradual | ≥ 3 pasos seguidos en la misma dirección, ≥ 1 pt (1.5 total, 4 pp ML) acumulado en ≥ 3 h dentro de 72 h. | Sí |
| Retorno | La línea regresa a su apertura tras alejarse ≥ 1 pt (1.5 total, 4 pp ML). El movimiento anterior no se sostuvo. | Sí |
| Ruido | 1–2 casas se mueven, direcciones mixtas, solo juice, huecos sin snapshots o una casa fuera de consenso. | No, solo dashboard |

Un movimiento que se aleja de la apertura suma fuerza; uno que la deshace resta. Cada alerta incluye su posición contra la apertura, tanto en Telegram como en el dashboard.

Las alertas del mismo partido y mercado se agrupan en un mensaje. Si falla un snapshot o quedan menos de 100 créditos llega un aviso único. Los umbrales están en `tracker/config.py` (`THRESHOLDS`).

El código está repartido así: `market.py` (valor de cada casa, consenso y formato), `openings.py` (apertura y comparación contra ella), `rules.py` (una función por tipo de movimiento) y `detect.py` (orquestación).

## Retención

Se conserva toda la temporada sin que la base pase de ~56 MB (GitHub rechaza archivos de más de 100 MB):

| Antigüedad | Qué se guarda |
|---|---|
| Últimos 4 días (`KEEP_DAYS`) | Todas las líneas y splits. La detección usa 72 h, así que 4 días dan margen. |
| Hasta 200 días (`ARCHIVE_DAYS`) | Por partido: apertura, cierre (último snapshot antes del kickoff) y el primer snapshot de cada día, con sus splits. Alertas de información, vapor, RLM y graduales, y los eventos. |
| Más de 200 días | Se borra todo. |

Las aperturas se conservan mientras exista el partido, aunque sus líneas ya se hayan compactado.

Las alertas de ruido se borran a los 4 días: solo sirven para el dashboard.

`KEEP_DAYS` bajó de 8 a 4 al apretar la cadencia: con ~32 snapshots diarios cada día de retención completa pesa ~7 MB, y con 8 días la base habría llegado a ~83 MB a fin de temporada, demasiado cerca del límite donde GitHub rechaza el push y la cadena se detiene. El dashboard sigue graficando 7 días, pero más allá del cuarto la serie se dibuja con el archivo diario.
