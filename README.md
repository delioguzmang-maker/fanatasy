# Fantasy Autopilot para Yahoo 🏈

Un "Yahoo Fantasy Plus + FantasyPros Auto-Pilot" propio que corre en **Google Colab** (y, si quieres, 24/7 gratis en GitHub Actions):

- **Alineación óptima** cada semana, con las proyecciones de tu propia página de Yahoo.
- **Cambios automáticos antes de cada partido**: si un titular sale *Out*/inactivo, entra el mejor suplente que todavía no haya jugado.
- **Jugadores dudosos (Q/D) con números**: probabilidad de que jueguen, y si hay un suplente que juegue igual o más tarde para cubrirlos (el caso Nacua de la Semana 2).
- **Waivers y pujas FAAB**: a quién agregar, a quién soltar y cuánto pujar; puede enviar los reclamos solo.
- **Rival y probabilidad de ganar**, con los errores de la alineación del rival.
- **Evaluador de trades** que suma puntos antes de opinar.
- **Avisos al celular** con la app gratis [ntfy](https://ntfy.sh) (también Telegram o correo).

## Empezar (Colab)

1. Abre `FantasyAutopilot.ipynb` en Colab: [abrir en Colab](https://colab.research.google.com/github/delioguzmang-maker/fanatasy/blob/claude/pensive-brown-ow1zrl/FantasyAutopilot.ipynb).
   Si el enlace no abre: en Colab, **Archivo → Abrir cuaderno → GitHub**, pega `delioguzmang-maker/fanatasy`, elige la rama `claude/pensive-brown-ow1zrl` y el archivo.
2. Corre las celdas en orden. La celda 2 pide tu **LIGA_ID** y **EQUIPO_ID**: salen de la dirección de tu equipo, `football.fantasysports.yahoo.com/f1/LIGA_ID/EQUIPO_ID`.
3. La celda 3 explica cómo copiar tu **cookie** de Yahoo y guardarla como secreto `YAHOO_COOKIES` en Colab.
4. Empieza en modo **`sugerir`** (no toca Yahoo). Cuando confíes en las recomendaciones, corre la celda 9 (**calibrar**, con capturas) y pasa a **`automatico`**.

## Por qué con cookies y no con la API de Yahoo

- Desde el **22 de julio de 2026** la API de Yahoo Fantasy responde *"This application is not authorized"* a las apps que no aprobó a mano ([yfpy #84](https://github.com/uberfastman/yfpy/issues/84)).
- Crear apps nuevas ya no da el permiso de escritura (desde octubre de 2025, [yfpy #79](https://github.com/uberfastman/yfpy/issues/79)), y el acceso nuevo es solo lectura, con revisión manual y sin plazo ([reporte del 11 de agosto de 2026](https://github.com/derekrbreese/fantasy-football-mcp-public/issues/18)).
- Por eso el programa usa **tu sesión del navegador**: lee las mismas páginas que ves tú y guarda con los mismos formularios. Las páginas que lee (tabla `statTable`, menús `select` de posiciones, `playernote`, `/teams`) fueron verificadas contra ligas reales en agosto y septiembre de 2026 por [ffl-automation](https://github.com/bryanyoung73/ffl-automation), [nfl-fantasy-football](https://github.com/slcherniak/nfl-fantasy-football) y [ffbot](https://github.com/amarvin/fantasy-football-bot).

**Riesgos que decides tú:**
- Automatizar tu cuenta puede ir contra los términos de uso de Yahoo. El programa hace pocas visitas y con pausas de un segundo.
- La cookie equivale a tu contraseña. Guárdala solo en los Secrets de Colab/GitHub. Nunca va al código ni a los registros.
- **El guardado de la alineación y el formulario de pujas no se pudieron probar contra Yahoo real** desde donde se construyó esto (solo contra una copia local del formulario). Por eso existen el modo `simulacro` y la celda de calibración con capturas. Cada escritura se **verifica** leyendo la página otra vez. Si Yahoo no aplicó el cambio, te llega una alerta urgente con los cambios exactos para hacerlos a mano.

## Cómo decide (todo con números)

**Alineación.** Asignación exacta jugador→posición (incluido el FLEX) para maximizar puntos. A un jugador dudoso se le cuenta `probabilidad × proyección`. Si hay un suplente cuyo partido empieza igual o más tarde, se suma el valor de ese respaldo, porque el piloto automático puede hacer el cambio cuando salgan los inactivos (~90 min antes del partido). Se descuenta un 15% por si el cambio falla y se cobra un margen de riesgo de 0.75 pts por titular dudoso.

- Con tus números de la Semana 3, el resultado es el mismo del análisis a mano: Tuten al FLEX, Nabers a WR y Bateman a la banca (104.66, +1.25).
- Nacua (SNF, 30%) queda en la banca, porque ningún suplente juega después que él.
- Si Warren sale inactivo, la alineación baja a 99.90.

**Probabilidad de jugar** (todas configurables): sano 100%, Q 75% (práctica completa 90%, limitada 75%, no practicó 40%), D 10%, O/IR/suspendido 0%. Las noticias frescas de Sleeper (menos de 72 h) pueden subir la gravedad del estado de Yahoo. Además puedes poner probabilidades a mano (`Puka Nacua=30%`).

**Waivers.** Para cada semana que queda se calcula la mejor alineación posible con y sin el cambio. El valor del cambio es la suma de esas diferencias, con más peso en las semanas cercanas (0.92 por semana). Todo se mide contra lo que podrías sacar de agentes libres esa semana: por eso un QB suplente que solo cubre un bye vale poco.

**Pujas FAAB (es una regla, no un dato):**

`puja = saldo × (puntos que agrega) ÷ (8 pts/semana × semanas efectivas)`

La puja tiene estos límites:
- nunca más del 50% del saldo;
- nunca más de lo que puede pujar el rival con más saldo, más $1;
- si sale un número redondo, sube $1 para no empatar.

Los agentes libres que no están en waivers se agregan sin puja.

**Trades.** Revisa esto en orden:
1. los puntos que das y los que recibes;
2. qué titulares se van;
3. si lo que recibes supera a tu mejor jugador de esa posición;
4. los puntos de alineación antes y después, en lo que queda de temporada.

Además te recuerda pasarlo por el evaluador de Yahoo.

## 24/7 con GitHub Actions (opcional)

Colab solo corre con la pestaña abierta. `.github/workflows/autopilot.yml` corre lo mismo en los servidores de GitHub:

| Cuándo (hora del Este) | Qué hace |
|---|---|
| Todos los días, ~10:07 am | Alineación de la semana |
| Martes, ~6:37 pm | Waivers y pujas |
| Cada 15 min cerca de los partidos (jue, sáb, dom, lun) | Inactivos del día de partido |

Configúralo en **Settings → Secrets and variables → Actions**:

- **Secrets**: `YAHOO_COOKIES` y `NTFY_TOPIC`. Opcional: `FA_CONFIG_JSON`, por ejemplo `{"never_drop": ["Alec Pierce"], "p_play_overrides": {"Puka Nacua": 0.3}}`.
- **Variables**: `FA_LEAGUE_ID`, `FA_TEAM_ID`, `FA_MODE` (`sugerir` o `automatico`), `AUTOPILOT_ENABLED=true`. Opcional: `FA_AUTO_CLAIMS=true`.

Notas:
- El repo es público: los registros solo muestran el título de cada corrida.
- GitHub corre los horarios desde la rama principal del repo, y puede atrasarlos unos minutos.

## Línea de comandos

```bash
pip install "git+https://github.com/delioguzmang-maker/fanatasy.git@claude/pensive-brown-ow1zrl"
export YAHOO_COOKIES='...'  FA_LEAGUE_ID=123456  FA_TEAM_ID=7  NTFY_TOPIC=mi-tema
python -m fantasy_autopilot check              # lee tu roster y lo muestra
python -m fantasy_autopilot lineup             # alineación de la semana
python -m fantasy_autopilot gameday --force    # revisión de inactivos
python -m fantasy_autopilot waivers            # pujas FAAB
python -m fantasy_autopilot matchup
python -m fantasy_autopilot trade --give "Nico Collins" --get "Josh Allen"
python -m fantasy_autopilot loop --hours 10 --every 10
```

Opciones principales (`--config archivo.json`, variables `FA_<OPCION>` o la celda 2):

| Opción | Por defecto | Qué es |
|---|---|---|
| `mode` | `sugerir` | `sugerir`, `simulacro` o `automatico` |
| `auto_claims` | `false` | enviar reclamos de waivers solos (requiere `automatico`) |
| `p_play_overrides` | `{}` | probabilidades a mano, ej. `{"Puka Nacua": 0.3}` |
| `never_drop` | `[]` | jugadores que nunca se sueltan |
| `min_gain` / `gameday_min_gain` | 0.3 / 0.5 | puntos mínimos para cambiar la alineación |
| `full_budget_ppw` | 8.0 | pts/semana que valdrían todo tu saldo FAAB |
| `max_bid_pct` | 0.5 | tope de puja como fracción del saldo |
| `hedge_reliability` | 0.85 | confianza en que el cambio de último minuto se haga |
| `uncertain_penalty` | 0.75 | margen de riesgo por titular dudoso |
| `projections` | `yahoo` | `yahoo`, `sleeper` o `blend` |

## Pruebas

```bash
pip install -e ".[test,browser]" && python -m playwright install chromium
python -m pytest -q
```

Las 62 pruebas incluyen:
- tu alineación real de la Semana 3;
- los parsers de las páginas de Yahoo;
- guardar la alineación y pujar contra una copia local del sitio, por HTTP y con un navegador real;
- el notebook ejecutado de principio a fin.
