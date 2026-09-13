# NFL Odds Tracker

Toma snapshots de momios NFL de 10 casas, junta los % de tickets y dinero, clasifica los movimientos de línea y manda alertas a Telegram. Corre en GitHub Actions y genera un dashboard HTML.

## Cómo corre

El workflow [`.github/workflows/tracker.yml`](.github/workflows/tracker.yml) se ejecuta cada 30 minutos (:07 y :37). En cada ejecución:

1. Recupera la base SQLite de la rama `state`.
2. Decide si toca snapshot: si pasaron 55 min o más desde el último, o 25 min o más cuando un partido arranca en las próximas 2 horas. Se compara contra el último snapshot y no contra el reloj, porque el cron de GitHub puede retrasarse varios minutos.
3. Toma el snapshot, busca splits, detecta movimientos y manda alertas a Telegram.
4. Guarda la base y el dashboard en la rama `state` (un solo commit que se reemplaza, así el repo no crece).

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
| `python -m tracker status` | Último snapshot, créditos, próxima ejecución y estado de Telegram. |
| `python -m tracker analyze [--snapshot N]` | Repite la detección sobre un snapshot sin enviar nada. |
| `python -m tracker dashboard --open` | Regenera y abre `dashboard/index.html`. |
| `python -m tracker telegram-setup` / `telegram-test` | Guarda el chat ID de Telegram / manda un mensaje de prueba. |
| `python -m unittest` | Pruebas. |

La base local (`data/tracker.db`) es independiente de la rama `state`. `scripts/install_task.ps1` programa la ejecución en Windows como alternativa; no lo actives a la vez que el workflow o habrá snapshots y alertas duplicadas.

## Créditos y minutos

- **The Odds API:** cada snapshot cuesta 3 créditos (3 mercados; hasta 10 casas cuentan como una región). La lista de partidos es gratis. Una semana normal ronda 534 créditos (~2,300 al mes). Si no hay partidos en los próximos 7 días no gasta.
- **GitHub Actions:** 48 ejecuciones al día, ~1 minuto facturable cada una (~1,440 min/mes). En un repo privado del plan gratis el límite es 2,000 min/mes compartidos con tus otros repos privados.

## Fuentes

- **Líneas:** The Odds API — Pinnacle, LowVig, BetOnline (sharp), DraftKings, FanDuel, BetMGM, ESPN Bet, BetRivers, Hard Rock, Bovada.
- **Splits:** Action Network (consenso, JSON) y DK Network (splits propios de DraftKings, HTML). No son APIs oficiales: si una falla, el snapshot sigue y queda registrado en el log.

## Cómo se clasifica

Para cada partido y mercado se calcula el consenso y se compara contra el snapshot anterior. En spread y total el consenso es la línea que más casas repiten (en empate, la de las casas sharp); en moneyline es la mediana de la probabilidad implícita sin vig del local. Las casas que se alejan demasiado de la mediana quedan fuera.

| Tipo | Regla | Telegram |
|---|---|---|
| Información | Consenso salta ≥ 1.5 pts (spread), ≥ 2 (total) o ≥ 6 pp (ML) con ≥ 70% de las casas en ≤ 75 min. Suma si pasa por un número clave. | Sí |
| Vapor | ≥ 3 casas se mueven igual en ≤ 75 min y ≤ 1 en contra. Suma si una casa sharp se movió primero. | Sí |
| RLM | El consenso se mueve ≥ 0.5 pts (2 pp en ML) contra el lado con ≥ 60% de tickets. Suma si el dinero va con el movimiento. | Sí |
| Gradual | ≥ 3 pasos seguidos en la misma dirección, ≥ 1 pt (1.5 total, 4 pp ML) acumulado en ≥ 3 h dentro de 72 h. | Sí |
| Ruido | 1–2 casas se mueven, direcciones mixtas, solo juice, huecos sin snapshots o una casa fuera de consenso. | No, solo dashboard |

Las alertas del mismo partido y mercado se agrupan en un mensaje. Si falla un snapshot o quedan menos de 100 créditos llega un aviso único. Los umbrales están en `tracker/config.py` (`THRESHOLDS`). Se conservan 8 días de líneas y 30 de alertas.
