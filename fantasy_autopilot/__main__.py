"""Command line: python -m fantasy_autopilot <task> [--config config.json]

Tasks: check, lineup, gameday, waivers, matchup, trade, loop.
Cookies come from the YAHOO_COOKIES environment variable (or --cookies FILE).
Settings come from --config and/or FA_* environment variables.
"""

from __future__ import annotations

import argparse
import os
import sys

from .config import Config
from .engine import Autopilot
from .yahoo.cookies import describe, parse_cookies


def build(args: argparse.Namespace) -> Autopilot:
    base = Config.from_file(args.config) if args.config else None
    cfg = Config.from_env(base)
    if args.mode:
        cfg.mode = args.mode
    if not cfg.league_id or not cfg.team_id:
        sys.exit("Falta league_id / team_id (usa --config o FA_LEAGUE_ID y FA_TEAM_ID).")
    cookies = open(args.cookies).read() if args.cookies else os.environ.get("YAHOO_COOKIES", "")
    if not cookies.strip():
        sys.exit("Falta YAHOO_COOKIES (el valor de 'Cookie' de tu navegador; ver README).")
    return Autopilot(cfg, cookies=cookies)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fantasy_autopilot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", choices=["check", "lineup", "gameday", "waivers", "matchup", "trade", "loop"])
    ap.add_argument("--config", help="archivo JSON de configuración")
    ap.add_argument("--cookies", help="archivo con las cookies (si no, variable YAHOO_COOKIES)")
    ap.add_argument("--mode", choices=["sugerir", "simulacro", "automatico"])
    ap.add_argument("--give", default="", help="trade: jugadores que das, separados por coma")
    ap.add_argument("--get", default="", help="trade: jugadores que recibes, separados por coma")
    ap.add_argument("--hours", type=float, default=10.0, help="loop: horas que se queda vigilando")
    ap.add_argument("--every", type=int, default=10, help="loop: minutos entre revisiones")
    ap.add_argument("--force", action="store_true", help="gameday: revisar aunque no haya partidos cerca")
    args = ap.parse_args(argv)

    pilot = build(args)
    if args.task == "check":
        cookies = parse_cookies(pilot.cookies)
        print("Cookies:", describe(cookies))
        week = pilot.week()
        roster = pilot.load_roster(week)
        print(f"Semana {week}: {len(roster.players)} jugadores, posiciones {roster.slots}, "
              f"banca {roster.bench_size}, IR {roster.ir_size}")
        for p in roster.players:
            print(f"  {p.slot:<6} {p.name:<26} {p.position:<3} {p.team:<4} {p.proj:6.2f} {p.status}")
        return 0
    if args.task == "loop":
        pilot.loop(hours=args.hours, every_minutes=args.every)
        return 0
    kwargs = {}
    if args.task == "gameday":
        kwargs["force"] = args.force
    if args.task == "trade":
        kwargs = {"give": [x.strip() for x in args.give.split(",") if x.strip()],
                  "get": [x.strip() for x in args.get.split(",") if x.strip()]}
    result = pilot.run(args.task, **kwargs)
    if pilot.cfg.quiet:
        print(result.title)
    elif not (pilot.notifier.enabled and result.notify):
        print(result)  # otherwise the notifier already echoed it
    return 1 if result.title.startswith("⚠ Error") or result.title.startswith("⚠ Sesión") else 0


if __name__ == "__main__":
    sys.exit(main())
