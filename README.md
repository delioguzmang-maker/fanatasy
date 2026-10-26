# Fantasy Autopilot para Yahoo 🏈

Un "Yahoo Fantasy Plus + FantasyPros Auto-Pilot" propio que corre en **Google Colab** (y, si quieres, 24/7 gratis en GitHub Actions):

- **Proyección propia**: Sleeper convertido a la puntuación de tu liga y corregido por un modelo (ridge) entrenado con
  cada partido de la NFL desde la temporada pasada (forma contra su proyección, snaps, targets, acarreos, puntos que
  permite el rival, línea de apuestas). Examen semana por semana sin ver el futuro: en las semanas 1–7 de la liga
  se equivocó 4.76 pts por jugador contra 4.98 de Yahoo y ordenó mejor titular/banca (71% vs 69%). K y DEF siguen
  con Yahoo. Si en tu liga no le gana a Yahoo, se usa Yahoo (`ownproj.py`, celda 7e).
- **Alineación óptima** cada semana, con esa proyección.
- **Ganar la semana** (`win_week`, celda 5d): altas, bajas y pujas que más suben tu probabilidad de ganar el próximo
  partido (descansos, lesiones), cobrando lo que cuestan en el resto de la temporada; **radar de descansos**
  (`bye_radar`, celda 5e).
- **Cambios automáticos antes de cada partido**: si un titular sale *Out*/inactivo, entra el mejor suplente que todavía no haya jugado.
- **Jugadores dudosos (Q/D) con números**: probabilidad de que jueguen, y si hay un suplente que juegue igual o más tarde para cubrirlos (el caso Nacua de la Semana 2).
- **Waivers y pujas FAAB**: a quién agregar, a quién soltar y cuánto pujar; puede enviar los reclamos solo.
- **Rival y probabilidad de ganar**, con los errores de la alineación del rival (con puntos reales cuando ya se jugó).
- **Próximo rival**: resultados anteriores de los dos, posición por posición, semanas de descanso, estrategia y agentes libres que suben tu alineación de esa semana.
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

**Horarios.** Salen de ESPN; como ESPN bloquea a Colab, el respaldo es el calendario público de [nflverse](https://github.com/nflverse/nfldata) en GitHub, y como último recurso el texto de Yahoo ("Sun 1:00 pm"). Cuando un partido ya empezó o terminó se usan los **puntos reales** de Yahoo, no la proyección.

**Probabilidad de jugar** (todas configurables): sano 100%, Q 75% (práctica completa 90%, limitada 75%, no practicó 40%), D 10%, O/IR/suspendido 0%. Las noticias frescas de Sleeper (menos de 72 h) pueden subir la gravedad del estado de Yahoo. Además puedes poner probabilidades a mano (`Puka Nacua=30%`).

**Waivers.** Para cada semana que queda se calcula la mejor alineación posible con y sin el cambio. El valor del cambio es la suma de esas diferencias, con más peso en las semanas cercanas (0.92 por semana). Todo se mide contra lo que podrías sacar de agentes libres esa semana: por eso un QB suplente que solo cubre un bye vale poco.

**Pujas FAAB (es una regla, no un dato):**

`puja = saldo × (puntos que agrega) ÷ (8 pts/semana × semanas efectivas)`

La puja tiene estos límites:
- nunca más del 50% del saldo;
- nunca más de lo que puede pujar el rival con más saldo, más $1;
- si sale un número redondo, sube $1 para no empatar.

Los agentes libres que no están en waivers se agregan sin puja. Además:
- nunca propone soltar a un titular de esta semana;
- pateadores y defensas: máximo $2 (se consiguen gratis casi cada semana);
- no agrega un tercer QB, un segundo K ni una segunda DEF;
- solo recomienda un cambio si sube tu alineación al menos 8 pts ponderados (~1 pt por semana);
- a los lesionados les baja también las semanas siguientes (IR: 4 semanas en cero; Out/Doubtful: la mitad la semana siguiente), porque las proyecciones de Yahoo para semanas futuras los cuentan sanos.

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

## Arreglos de la auditoría del 20 de octubre

| Problema | Arreglo | Dónde |
|---|---|---|
| Un titular **activo** que perdió el puesto (Skattebo, semana 6) no se detectaba | Alerta de rol: lee la nota de Yahoo, usa solo noticias recientes (por fecha), recalcula la alineación contándolo × 0.5 y manda alerta urgente | `role.py`, `lineup()`, celdas 4b y 8 |
| Palabras de lesión contadas dos veces (estado + alerta) | La alerta de rol solo usa palabras de **rol**; las de lesión las cuenta el estado | `role.py` |
| Una alerta urgente sin atender se mandaba una sola vez | Se repite cada 30 min (máx. 4) mientras siga igual | `State.should_send` |
| Titular fuera sin suplente en la banca | Sugiere el mejor agente libre (sin espera) que juega más tarde y a quién soltar | `fa_rescue()` |
| Elegir solo por puntos aunque vayas abajo | Busca un cambio que suba tu probabilidad de ganar cediendo ≤ 1.5 pts | `win_prob_swap`, celda 7d llena `player_sd` |
| ¿Doble descuento de "Questionable"? | Se guarda el estado antes de cada partido (`estado_log.csv`) y la celda 7d mide el factor real con 30+ casos | `calibration.py` |
| Pujas sin relación con lo que paga la liga | Tope = 90.º percentil de pujas ganadoras + 1, salvo jugadores clave (≥ 4 pts/semana); siempre impares | `suggest_bid` |
| Libres lesionados valorados como sanos | Mismo descuento de lesión que tus jugadores (dentro del paquete) | `_candidates()` |
| Acción de Gracias, Black Friday y Navidad sin revisión de inactivos | Horarios extra en el workflow | `autopilot.yml` |

Opciones nuevas en `Config`: `role_check`, `role_penalty`, `role_penalty_undated`, `role_news_hours`,
`role_auto_swap`, `resend_minutes`, `max_resends`, `win_prob_lineup`, `max_sacrifice`, `player_sd`,
`fa_rescue`, `log_status`, `bid_history_cap`, `candidates_kdef`.

## Línea de comandos

```bash
pip install "git+https://github.com/delioguzmang-maker/fanatasy.git@claude/pensive-brown-ow1zrl"
export YAHOO_COOKIES='...'  FA_LEAGUE_ID=123456  FA_TEAM_ID=7  NTFY_TOPIC=mi-tema
python -m fantasy_autopilot check              # lee tu roster y lo muestra
python -m fantasy_autopilot lineup             # alineación de la semana
python -m fantasy_autopilot gameday --force    # revisión de inactivos
python -m fantasy_autopilot waivers            # pujas FAAB
python -m fantasy_autopilot matchup
python -m fantasy_autopilot preview            # próximo rival y estrategia
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
| `max_bid_kdef` | 2 | tope de puja para K y DEF |
| `min_claim_gain` | 8.0 | puntos ponderados mínimos para recomendar una puja |
| `hedge_reliability` | 0.85 | confianza en que el cambio de último minuto se haga |
| `uncertain_penalty` | 0.75 | margen de riesgo por titular dudoso |
| `projections` | `yahoo` | `yahoo`, `sleeper` o `blend` |

## Pruebas

```bash
pip install -e ".[test,browser]" && python -m playwright install chromium
python -m pytest -q
```

Las 71 pruebas incluyen:
- tu alineación real de la Semana 3;
- los parsers de las páginas de Yahoo;
- guardar la alineación y pujar contra una copia local del sitio, por HTTP y con un navegador real;
- el notebook ejecutado de principio a fin.
