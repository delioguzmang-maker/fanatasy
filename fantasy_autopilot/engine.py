"""The autopilot: reads Yahoo + free sources, decides with numbers, writes, notifies.

Tasks (all return a ``RunResult`` with a Spanish report):

* ``lineup()``   weekly lineup optimization (like FantasyPros Auto-Pilot's daily set)
* ``gameday()``  near kickoff: swap out starters ruled Out/inactive (runs every few minutes)
* ``waivers()``  add/drop + FAAB bid suggestions, optional automatic claims
* ``matchup()``  win probability and the opponent's lineup mistakes
* ``trade()``    trade evaluator
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

from . import calibration
from . import report as rpt
from . import role as role_mod
from .config import Config
from .matchup import (
    audit_lineup,
    lineup_sd,
    movable_part,
    started,
    team_outlook,
    win_prob_swap,
    win_probability,
)
from .models import Game, Player, Roster, WriteResult
from .nfl import eligible_slots, norm_name, norm_team
from .notify import Notifier
from .optimizer import (
    OptimizerSettings,
    assign,
    best_total,
    current_plan,
    optimize_lineup,
    plan_moves,
    plan_objective,
)
from .sources import espn, schedule
from .sources.sleeper import Sleeper, injury_fields
from .status import DEFAULT_PROBS, SEVERITY, apply_status_model, is_out, norm_status
from .trade import evaluate_trade
from .waivers import WaiverSettings, bench_upgrades, horizon, plan_claims, replacement_players
from .yahoo import parse
from .yahoo.cookies import CookieSource
from .yahoo.web import YahooSessionExpired, YahooWeb

OUT_TYPES = {"O", "NA", "IR", "PUP", "NFI", "SUSP", "COVID"}
FRESH_NEWS_HOURS = 72


@dataclass
class RunResult:
    task: str
    title: str
    text: str
    changed: bool = False
    urgent: bool = False
    notify: bool = True
    writes: list[WriteResult] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.title}\n{self.text}"


class State:
    """Tiny JSON memory between runs: sent notifications, submitted claims."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1))
        except OSError:
            pass

    def seen(self, key: str, text: str) -> bool:
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        if self.data.get("sent", {}).get(key) == digest:
            return True
        self.data.setdefault("sent", {})[key] = digest
        self.save()
        return False

    def should_send(self, key: str, text: str, urgent: bool, now: float,
                    resend_minutes: int = 30, max_resends: int = 4) -> tuple[bool, int]:
        """(send?, how many times it was already sent). New text: send. Same urgent text
        nobody acted on: send again every `resend_minutes`, at most `max_resends` times."""
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        rec = self.data.setdefault("alerts", {}).get(key)
        if not rec or rec.get("digest") != digest:
            self.data["alerts"][key] = {"digest": digest, "t": now, "n": 1}
            self.save()
            return True, 0
        if urgent and rec.get("n", 1) <= max_resends and now - rec.get("t", 0) >= resend_minutes * 60:
            rec["t"], rec["n"] = now, rec.get("n", 1) + 1
            self.save()
            return True, rec["n"] - 1
        return False, rec.get("n", 1)

    def cached_note(self, pid: str, now: float, ttl: float = 600) -> Optional[str]:
        rec = self.data.get("notes", {}).get(pid)
        return rec[1] if rec and now - rec[0] < ttl else None

    def put_note(self, pid: str, text: str, now: float) -> None:
        notes = self.data.setdefault("notes", {})
        notes[pid] = [now, text[:4000]]
        for k in [k for k, v in notes.items() if now - v[0] > 86400]:
            notes.pop(k, None)
        self.save()

    def claimed(self, week: int) -> set[str]:
        return set(self.data.get("claims", {}).get(str(week), []))

    def add_claim(self, week: int, pid: str) -> None:
        self.data.setdefault("claims", {}).setdefault(str(week), []).append(pid)
        self.save()


class Autopilot:
    def __init__(
        self,
        config: Config,
        cookies: CookieSource = "",
        web: Optional[YahooWeb] = None,
        sleeper: Optional[Sleeper] = None,
        notifier: Optional[Notifier] = None,
        browser: Any = None,
        games_fetcher: Optional[Callable[[int, int], list[Game]]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        schedule_fetcher: Optional[Callable[[int, int], list[Game]]] = schedule.week_games,
    ):
        self.cfg = config
        self.cookies = cookies
        self.web = web or YahooWeb(config.league_id, config.team_id, cookies,
                                   base_url=config.yahoo_base_url, delay=config.request_delay)
        if sleeper is None and config.use_sleeper:
            sleeper = Sleeper(cache_dir=config.state_path / "cache")
        self.sleeper = sleeper
        self.notifier = notifier or Notifier(
            ntfy_topic=config.ntfy_topic, ntfy_server=config.ntfy_server,
            telegram_token=config.telegram_token, telegram_chat_id=config.telegram_chat_id,
            email_to=config.email_to, smtp_user=config.smtp_user, smtp_password=config.smtp_password,
            echo=not config.quiet,
        )
        self._browser = browser
        self.games_fetcher = games_fetcher or espn.week_games
        self.schedule_fetcher = schedule_fetcher
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.tz = ZoneInfo(config.timezone)
        self.state = State(config.state_path / "state.json")
        self.warnings: list[str] = []
        self._home: Optional[str] = None
        self._settings: Optional[parse.LeagueSettings] = None
        self._games: dict[int, dict[str, Game]] = {}

    # ------------------------------------------------------------ helpers
    @property
    def team_url(self) -> str:
        return f"{self.web.base_url}/f1/{self.cfg.league_id}/{self.cfg.team_id}"

    def opt_settings(self) -> OptimizerSettings:
        return OptimizerSettings(
            hedge_reliability=self.cfg.hedge_reliability,
            uncertain_penalty=self.cfg.uncertain_penalty,
            assume_autoswap=self.cfg.assume_autoswap,
        )

    def waiver_settings(self) -> WaiverSettings:
        return WaiverSettings(
            discount=self.cfg.discount, last_week=self.cfg.last_week,
            full_budget_ppw=self.cfg.full_budget_ppw, max_pct=self.cfg.max_bid_pct,
            min_bid=self.cfg.min_bid, max_claims=self.cfg.max_claims,
            min_gain=self.cfg.min_claim_gain, never_drop=set(self.cfg.never_drop),
            max_bid_kdef=self.cfg.max_bid_kdef,
        )

    def browser(self) -> Any:
        if self._browser is None:
            if importlib.util.find_spec("playwright") is None:
                return None
            from .yahoo.browser import YahooBrowser

            evidence = self.cfg.state_path / "evidencia" if self.cfg.save_evidence else None
            self._browser = YahooBrowser(
                self.cfg.league_id, self.cfg.team_id, self.cookies, base_url=self.web.base_url,
                headless=self.cfg.headless, evidence_dir=evidence,
                executable_path=self.cfg.chromium_path or None,
            )
        return self._browser

    def league_home(self) -> str:
        if self._home is None:
            self._home = self.web.league_home().html
        return self._home

    def week(self) -> int:
        if self.cfg.week:
            return int(self.cfg.week)
        try:
            week = parse.parse_current_week(self.league_home())
            if week:
                return week
        except YahooSessionExpired:
            raise
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"no pude leer la semana en Yahoo: {exc}")
        if self.sleeper:
            return int(self.sleeper.state()["week"])
        raise RuntimeError("No se pudo determinar la semana; ponla en la configuración (week).")

    def season(self) -> int:
        if self.cfg.season:
            return int(self.cfg.season)
        if self.sleeper:
            try:
                return int(self.sleeper.state()["season"])
            except Exception:  # noqa: BLE001
                pass
        now = self.now_fn()
        return now.year if now.month >= 3 else now.year - 1

    def league_settings(self) -> parse.LeagueSettings:
        if self._settings is None:
            try:
                self._settings = self.web.settings()
            except YahooSessionExpired:
                raise
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"no pude leer la configuración de la liga: {exc}")
                self._settings = parse.LeagueSettings()
        return self._settings

    def games(self, week: int) -> dict[str, Game]:
        """Kickoff times: ESPN first, then the public nflverse schedule (ESPN blocks Colab)."""
        if week not in self._games:
            self._games[week] = {}
            if self.cfg.use_espn:
                errors = []
                for name, fetch in (("ESPN", self.games_fetcher), ("nflverse", self.schedule_fetcher)):
                    if fetch is None:
                        continue
                    try:
                        found = fetch(self.season(), week)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{name}: {exc}")
                        continue
                    if found:
                        self._games[week] = espn.games_by_team(found)
                        break
                    errors.append(f"{name}: sin partidos de la semana {week}")
                if not self._games[week]:
                    self.warnings.append("sin calendario de partidos (" + "; ".join(errors) + ")")
        return self._games[week]

    def resolve(self, names: Sequence[str], players: Sequence[Player]) -> set[str]:
        keys = {norm_name(n) for n in names} | set(names)
        return {p.pid for p in players if p.pid in keys or norm_name(p.name) in keys}

    # ---------------------------------------------------------- loading
    def load_roster(self, week: int, team_id: Optional[str] = None) -> Roster:
        page, tp = self.web.team_page(team_id=team_id, week=week)
        if not tp.rows:
            raise RuntimeError(
                "No se leyó ningún jugador en la página del equipo. Puede que Yahoo haya cambiado la "
                f"página; revisa {page.url}"
            )
        mine = team_id is None or str(team_id) == self.cfg.team_id
        if mine and not tp.has_selects:
            self.warnings.append("La página no muestra los menús de posición; no se podrá guardar la alineación.")
        slots = tp.starting_slots or self.league_settings().starting_slots
        if not slots:
            raise RuntimeError("No se pudieron leer las posiciones de la alineación de tu liga.")
        players = []
        for r in tp.rows:
            positions = r.positions or [s for s in r.eligible if s in ("QB", "RB", "WR", "TE", "K", "DEF")][:1]
            players.append(Player(
                pid=r.pid,
                name=r.name,
                position=positions[0] if positions else "",
                team=r.team,
                eligible=r.eligible or eligible_slots(positions, slots),
                slot=r.slot,
                proj=r.proj or 0.0,
                status=r.status,
                locked=bool(mine and tp.has_selects and r.editable is not True),
                ir_eligible=r.ir_eligible,
                on_bye=r.on_bye,
                game_text=r.game_text,
                raw_text=r.raw_text,
            ))
        return Roster(
            league_id=self.cfg.league_id, team_id=str(team_id or self.cfg.team_id), week=week, slots=slots,
            players=players, bench_size=tp.bench_size, ir_size=tp.ir_size, team_name=tp.team_name,
            faab_balance=tp.faab_balance, fetched_at=self.now_fn(),
        )

    def enrich(self, players: Sequence[Player], week: int) -> None:
        """Kickoff times (ESPN), injury news (Sleeper), probabilities of playing."""
        now = self.now_fn()
        games = self.games(week)
        from_text = 0
        for p in players:
            g = games.get(p.team)
            if g is not None:
                p.kickoff = g.kickoff
                p.game_state = g.state if g.state in ("pre", "in", "post") else ""
                if g.state in ("in", "post") or g.kickoff <= now:
                    p.locked = True
                    p.game_state = p.game_state if p.game_state != "pre" else "in"
            elif len(games) >= 10 and p.team:
                p.on_bye = True  # full slate known and his team is not in it
            else:
                # no schedule at all: use Yahoo's own "Sun 1:00 pm" text (configured time zone)
                p.kickoff = kickoff_from_text(p.game_text, now, self.tz)
                if p.kickoff is not None:
                    from_text += 1
                    if p.kickoff <= now:
                        p.locked = True
                        p.game_state = "in"
            if re.search(r"\bFinal\b", p.game_text):
                p.locked, p.game_state = True, "post"
        if from_text:
            self.warnings.append(f"horarios tomados del texto de Yahoo ({from_text} jugadores), hora de {self.cfg.timezone}")
        missing_time = [p for p in players if p.kickoff is None and not p.on_bye and p.slot != "IR"
                        and p.game_state != "post"]
        if missing_time:
            sample = missing_time[0]
            self.warnings.append(
                "sin hora de partido para: " + ", ".join(p.name for p in missing_time[:6])
                + " (sin hora no se planean cambios de último minuto). Texto de Yahoo para "
                + f"{sample.name}: «{sample.raw_text[:120]}»"
            )
        if self.sleeper:
            try:
                self._merge_sleeper(players, now)
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"Sleeper no respondió (sin noticias de lesiones extra): {exc}")
        missing = any(p.proj == 0 and not p.on_bye and not is_out(p.status) and p.slot != "IR" for p in players)
        if self.cfg.projections in ("sleeper", "blend") or missing:
            self._sleeper_projections(players, week)
        probs = dict(DEFAULT_PROBS)
        probs.update(self.cfg.status_probs)
        apply_status_model(players, self.cfg.p_play_overrides, probs)
        lead = timedelta(minutes=self.cfg.inactives_posted_minutes)
        for p in players:
            if (p.status in ("Q", "D") and p.kickoff is not None and not p.locked
                    and now < p.kickoff <= now + lead and p.prob < 0.95):
                p.p_play = 0.95  # inactive lists are out (~90 min before kickoff) and he is not on it
                p.notes.append("activo: la lista de inactivos ya salió y no está en ella")

    def _merge_sleeper(self, players: Sequence[Player], now: datetime) -> None:
        rank = {code: i for i, code in enumerate(SEVERITY)}
        for p in players:
            sp = self.sleeper.match(p.pid, p.name, p.team, p.position)
            if not sp:
                continue
            info = injury_fields(sp)
            p.sleeper_id = info["sleeper_id"]
            p.practice = info["practice"] or p.practice
            s_status = norm_status(info["injury_status"])
            if not s_status or rank.get(s_status, 99) >= rank.get(p.status, 99):
                continue
            fresh = info["news_updated"] and now.timestamp() - info["news_updated"] < FRESH_NEWS_HOURS * 3600
            agree_injured = p.status in ("Q", "D") and s_status in OUT_TYPES
            if fresh or agree_injured:
                p.notes.append(f"Sleeper: {info['injury_status']} (Yahoo: {p.status or 'sano'})")
                p.status = s_status

    def _sleeper_projections(self, players: Sequence[Player], week: int) -> None:
        if not self.sleeper:
            return
        try:
            rec = self.league_settings().reception_points
            proj = self.sleeper.projections(self.season(), week, 1.0 if rec is None else rec)
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"sin proyecciones de Sleeper: {exc}")
            return
        w = self.cfg.blend_weight
        for p in players:
            s = proj.get(p.sleeper_id or "")
            if s is None:
                continue
            if self.cfg.projections == "sleeper" or (p.proj == 0 and not p.on_bye and not is_out(p.status)):
                p.proj = s
            elif self.cfg.projections == "blend":
                wp = self.cfg.blend_weight_pos.get(p.position, 0.0) if self.cfg.blend_weight_pos else w
                scale = self.cfg.blend_scale_pos.get(p.position, 1.0)
                p.proj = round((1 - wp) * p.proj + wp * s * scale, 2)

    # ------------------------------------------------------------ writes
    def write_lineup(self, target: dict[str, str], week: int) -> Optional[WriteResult]:
        """Save slot changes (None in "sugerir" mode, where nothing is written)."""
        if self.cfg.mode == "sugerir":
            return None
        dry = self.cfg.mode == "simulacro"
        result: Optional[WriteResult] = None
        if self.cfg.writer in ("auto", "http"):
            try:
                result = self.web.set_lineup(target, week, dry_run=dry)
            except YahooSessionExpired:
                raise
            except Exception as exc:  # noqa: BLE001
                result = WriteResult(False, "lineup", f"guardado por HTTP falló: {exc}")
            if result.ok or self.cfg.writer == "http":
                return result
            self.warnings.append(f"{result.detail}; intento con el navegador")
        browser = self.browser()
        if browser is None:
            return result or WriteResult(False, "lineup", "Playwright no está instalado (pip install playwright)")
        return browser.set_lineup(target, week, dry_run=dry)

    # ------------------------------------------------------------- tasks
    def lineup(self, gameday: bool = False, week: Optional[int] = None) -> RunResult:
        self.warnings = []
        week = week or self.week()
        roster = self.load_roster(week)
        self.enrich(roster.players, week)
        active = [p for p in roster.players if p.slot != "IR"]
        if active and all(p.locked for p in active):
            self.attach_actuals(roster)
            scored = sum(p.actual or 0.0 for p in roster.players if p.is_starter)
            text = (f"Todos tus partidos de la semana {week} ya empezaron o terminaron: no se puede cambiar nada."
                    + (f"\nPuntos de tus titulares: {scored:.2f}." if scored else "")
                    + f"\nPara preparar la semana {week + 1} usa «Próximo rival» y «Waivers».")
            return RunResult("gameday" if gameday else "lineup", f"Semana {week} cerrada", text,
                             notify=not gameday, data={"week": week, "roster": roster, "moves": []})
        if self.cfg.log_status:
            try:
                calibration.log_statuses(self.cfg.state_path / "estado_log.csv", week, roster.players, self.now_fn())
            except OSError as exc:
                self.warnings.append(f"no pude guardar estado_log.csv: {exc}")
        role_alerts: list[role_mod.RoleAlert] = []
        role_good: list[str] = []
        role_applied = False
        if self.cfg.role_check:
            role_alerts, role_good = self.role_alerts(roster)
            if role_alerts and self.cfg.role_auto_swap and self.cfg.writes_enabled:
                by_pid = roster.by_pid()
                for a in role_alerts:
                    by_pid[a.player_pid].proj = round(by_pid[a.player_pid].proj * a.factor, 2)
                role_applied = True
        opt = self.opt_settings()
        before = current_plan(roster.players, roster.slots, opt)
        plan = optimize_lineup(
            roster.players, roster.slots, opt,
            pins_start=self.resolve(self.cfg.pins_start, roster.players),
            pins_bench=self.resolve(self.cfg.pins_bench, roster.players),
        )
        role_line, role_gain, role_out = "", 0.0, ""
        if role_alerts and not role_applied:
            role_line, role_gain, role_out = self._role_replan(roster, plan, role_alerts, opt)
        moves = plan_moves(roster.players, plan)
        obj_before, _ = plan_objective(before, opt)
        obj_after, _ = plan_objective(plan, opt)
        gain = obj_after - obj_before
        forced = [a.player for a in before.assignments
                  if a.player and not a.player.locked and a.player.prob == 0.0]
        empty = [a.slot for a in before.assignments if a.player is None]
        threshold = self.cfg.gameday_min_gain if gameday else self.cfg.min_gain
        decided = bool(moves) and (gain >= threshold or ((forced or empty) and gain > 0.01))
        writes = []
        if decided:
            written = self.write_lineup({m.player.pid: m.to_slot for m in moves}, week)
            if written is not None:
                writes.append(written)
        stuck = [p for p in forced if p.pid in {x.pid for x in plan.starters()}]
        text = rpt.lineup_report(
            week, self.cfg.mode, roster.players, before, plan, moves, obj_before, obj_after,
            decided, threshold, writes, opt, self.tz, self.warnings, self.team_url,
        )
        extra: list[str] = []
        if role_alerts:
            extra.append("ALERTA DE ROL (titular activo que puede no jugar mucho):")
            extra += [f"  ⚠ {a.describe(with_swap=False)}" for a in role_alerts]
            if role_line:
                extra.append(f"  → {role_line}")
            if role_applied:
                extra.append("  (modo automático con role_auto_swap: la alineación de arriba ya lo cuenta)")
            else:
                extra.append("  Las palabras clave pueden dar falsas alarmas: lee la noticia y cambia tú en Yahoo.")
        if role_good:
            extra += ["  ✓ " + g for g in role_good]
        holes = [a.slot for a in plan.assignments if a.player is None or (a.player.prob == 0 and not a.player.locked)]
        rescue: list[str] = []
        if holes and self.cfg.fa_rescue:
            rescue = self.fa_rescue(week, roster, holes)
            if rescue:
                extra.append("PUESTO SIN REEMPLAZO EN TU BANCA: agente libre que juega más tarde:")
                extra += [f"  → {r}" for r in rescue]
        wswap = None
        if not gameday and self.cfg.win_prob_lineup:
            wswap = self.win_prob_advice(week, roster, plan)
            if wswap is not None:
                extra.append("PARA GANAR, NO SOLO SUMAR PUNTOS:")
                extra.append(f"  → {wswap.describe()}")
        if extra:
            text += "\n" + "\n".join(extra)
        failed = any(not w.ok for w in writes)
        role_urgent = role_gain > 0.01 and not role_applied
        urgent = (failed or bool(stuck) or (gameday and decided and self.cfg.mode == "sugerir")
                  or role_urgent or bool(rescue))
        if gameday:
            title = ("⚠ Cambia tu alineación YA" if decided and self.cfg.mode == "sugerir"
                     else "Alineación ajustada antes del partido" if decided else "Alineación: sin cambios")
        else:
            title = f"Alineación semana {week}: " + (f"{len(moves)} cambio(s) {plan.naive - before.naive:+.2f}" if decided else "sin cambios")
        if role_urgent:
            title = f"⚠ Alerta de rol: saca a {role_out} ({role_gain:+.2f})"
        if stuck:
            title = "⚠ Titular fuera y sin reemplazo disponible"
        if rescue:
            title = "⚠ Puesto vacío: agrega un agente libre"
        notify = (not gameday) or decided or bool(stuck) or failed or role_urgent or bool(rescue)
        return RunResult("gameday" if gameday else "lineup", title, text, changed=decided, urgent=urgent,
                         notify=notify, writes=writes,
                         data={"week": week, "roster": roster, "plan": plan, "before": before, "moves": moves,
                               "role_alerts": role_alerts, "rescue": rescue, "win_swap": wswap})

    # ------------------------------------------------------- lineup helpers
    def note_text(self, pid: str) -> str:
        now = time.time()
        cached = self.state.cached_note(pid, now)
        if cached is not None:
            return cached
        page = self.web.get("/playernote", {"pid": pid})
        text = role_mod.note_text(page.html)
        self.state.put_note(pid, text, now)
        return text

    def role_alerts(self, roster: Roster) -> tuple[list[role_mod.RoleAlert], list[str]]:
        try:
            alerts, good, failures = role_mod.check_roster(
                roster.players, self.note_text, self.now_fn(), self.tz,
                penalty=self.cfg.role_penalty, penalty_undated=self.cfg.role_penalty_undated,
                max_age_hours=self.cfg.role_news_hours, with_swap=False,
                usage=self.usage(roster.players, roster.week), penalty_confirmed=self.cfg.role_penalty_confirmed,
            )
        except YahooSessionExpired:
            raise
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"alerta de rol no se pudo revisar: {exc}")
            return [], []
        if failures:
            self.warnings.append("sin noticias (alerta de rol) de: " + ", ".join(failures[:5]))
        return alerts, good

    def usage(self, players: Sequence[Player], week: int) -> dict[str, tuple[float, Optional[float]]]:
        """Touches (carries + targets) in the last played game against the games before it."""
        if not self.sleeper or week <= 2 or not hasattr(self.sleeper, "weekly_stats"):
            return {}
        stats = {}
        for w in range(max(1, week - 4), week):
            try:
                stats[w] = self.sleeper.weekly_stats(self.season(), w)
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"sin estadísticas de uso de la semana {w} (Sleeper): {str(exc)[:60]}")
                return {}
        out = {}
        for p in players:
            if p.position not in ("RB", "WR", "TE") or not p.sleeper_id:
                continue
            games = [float(s.get("rush_att", 0) or 0) + float(s.get("rec_tgt", 0) or 0)
                     for w in sorted(stats) for s in [stats[w].get(str(p.sleeper_id))]
                     if s and (s.get("off_snp") or s.get("gp") or s.get("rush_att") or s.get("rec_tgt"))]
            if len(games) >= 2:
                out[p.pid] = (games[-1], sum(games[:-1]) / len(games[:-1]))
        return out

    def _role_replan(self, roster: Roster, plan: Any, alerts: Sequence[role_mod.RoleAlert],
                     opt: OptimizerSettings) -> tuple[str, float, str]:
        """Best lineup if the alerted starters are counted with their penalty, compared
        with the normal plan (both valued with the penalty). Projections are restored."""
        by_pid = roster.by_pid()
        saved = {}
        for a in alerts:
            p = by_pid[a.player_pid]
            saved[p.pid] = p.proj
            p.proj = round(p.proj * a.factor, 2)
        try:
            alt = optimize_lineup(
                roster.players, roster.slots, opt,
                pins_start=self.resolve(self.cfg.pins_start, roster.players),
                pins_bench=self.resolve(self.cfg.pins_bench, roster.players),
            )
            obj_alt, _ = plan_objective(alt, opt)
            obj_plan, _ = plan_objective(plan, opt)
        finally:
            for pid, proj in saved.items():
                by_pid[pid].proj = proj
        now_t, alt_t = plan.target_slots(), alt.target_slots()
        changes = [(by_pid[pid].name, now_t.get(pid, "BN"), slot) for pid, slot in alt_t.items()
                   if now_t.get(pid, "BN") != slot and pid in by_pid]
        gain = obj_alt - obj_plan
        if not changes or gain <= 0.01:
            return "Aun contándolo con el castigo, sigue en tu mejor alineación: déjalo.", 0.0, ""
        benched = [n for n, a, b in changes if b == "BN"]
        moves = ", ".join(f"{n} {a}→{b}" for n, a, b in sorted(changes, key=lambda c: c[2] != "BN"))
        line = (f"Contando la alerta, la mejor alineación cambia: {moves}. "
                f"{obj_alt:.2f} - {obj_plan:.2f} = {gain:+.2f} (con el castigo de rol).")
        return line, gain, ", ".join(benched) or changes[0][0]

    def _sd_of(self, p: Player) -> Optional[float]:
        sd = self.cfg.player_sd
        val = sd.get(p.pid, sd.get(norm_name(p.name)))
        return float(val) if val is not None else None

    def win_prob_advice(self, week: int, roster: Roster, plan: Any) -> Any:
        """Load this week's opponent and look for a swap that raises the chance to win."""
        try:
            html = self.league_home() if not self.cfg.week else self.web.league_home(week).html
            pair = next(((a, b) if a.team_id == self.cfg.team_id else (b, a)
                         for a, b in parse.parse_matchups(html) if self.cfg.team_id in (a.team_id, b.team_id)), None)
            if pair is None:
                return None
            opp = self.load_roster(week, team_id=pair[1].team_id)
            self.enrich(opp.players, week)
            self.attach_actuals(opp)
        except YahooSessionExpired:
            raise
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"sin rival para la probabilidad de ganar: {exc}")
            return None
        if self.cfg.log_status:
            try:  # rival players' pre-game status too: more cases to calibrate "Questionable"
                calibration.log_statuses(self.cfg.state_path / "estado_log.csv", week, opp.players, self.now_fn())
            except OSError:
                pass
        theirs = team_outlook(p for p in opp.players if p.is_starter)
        bench = [p for p in roster.players if p.slot != "IR" and p.pid not in {x.pid for x in plan.starters()}]
        return win_prob_swap(plan.assignments, bench, plan.ev, theirs.mean, theirs.sd,
                             sd_of=self._sd_of, max_sacrifice=self.cfg.max_sacrifice)

    def fa_rescue(self, week: int, roster: Roster, holes: Sequence[str]) -> list[str]:
        """A starter is out and nobody on the bench can replace him: best free agent
        (no waiver wait) whose game has not started, and who to drop."""
        now = self.now_fn()
        games = self.games(week)
        out, used = [], set()
        bench = [p for p in roster.players if p.slot == "BN" and not p.locked
                 and not role_mod_protected(p, self.cfg.never_drop)]
        open_spot = roster.open_bench_spots() > 0
        drop_value: dict[str, float] = {}
        for slot in dict.fromkeys(holes):
            positions = [pos for pos in ("QB", "RB", "WR", "TE", "K", "DEF") if slot in eligible_slots([pos], roster.slots)]
            best = None
            for pos in positions:
                try:
                    rows = self.web.players(pos, status="A")
                except YahooSessionExpired:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.warnings.append(f"no pude leer libres {pos}: {exc}")
                    continue
                for r in rows[:6]:
                    if r.positions and pos not in r.positions:
                        continue
                    if r.pid in used or not r.availability.upper().startswith("FA") or is_out(r.status) or r.status == "D":
                        continue
                    g = games.get(norm_team(r.team))
                    if g is None or g.kickoff <= now:
                        continue  # bye, unknown, or already started
                    try:
                        note = self.web.playernote(r.pid)
                    except YahooSessionExpired:
                        raise
                    except Exception:  # noqa: BLE001
                        continue
                    proj = projected_weekly(note, week).get(week, 0.0)
                    if best is None or proj > best[1]:
                        best = (r, proj, pos)
            if best is None:
                out.append(f"{slot}: no encontré un agente libre que juegue más tarde. Revisa Yahoo > Players.")
                continue
            r, proj, pos = best
            used.add(r.pid)
            if open_spot:
                drop_txt = "tienes espacio libre en la banca, no sueltas a nadie"
                open_spot = False
            else:
                for b in bench:
                    if b.pid not in drop_value:
                        try:
                            weekly = projected_weekly(self.web.playernote(b.pid), week)
                        except YahooSessionExpired:
                            raise
                        except Exception:  # noqa: BLE001
                            weekly = {}
                        drop_value[b.pid] = sum(weekly.values()) if weekly else b.proj
                choices = [b for b in bench if b.pid not in used]
                victim = min(choices, key=lambda b: drop_value.get(b.pid, 0.0), default=None)
                if victim is None:
                    drop_txt = "no hay a quién soltar sin perder un titular: decide tú"
                else:
                    used.add(victim.pid)
                    drop_txt = (f"suelta a {victim.name} (le quedan {drop_value.get(victim.pid, 0.0):.1f} pts "
                                "proyectados en toda la temporada, el menor de tu banca)")
            out.append(f"{slot}: agrega a {r.name} ({pos}, {r.team}, agente libre, sin espera) que proyecta "
                       f"{proj:.2f}: {proj:.2f} - 0.00 = +{proj:.2f}; {drop_txt}. "
                       "Verifica en Yahoo > Players que siga libre.")
        return out

    def gameday(self, window_minutes: int = 120, force: bool = False) -> RunResult:
        """Cheap check first (ESPN only): is any game starting soon? Then the full lineup pass."""
        self.warnings = []
        now = self.now_fn()
        if not force and self.cfg.use_espn:
            week = self.cfg.week
            if not week and self.sleeper:
                try:
                    week = int(self.sleeper.state()["week"])
                except Exception:  # noqa: BLE001 - fall through to the full check
                    week = None
            if week:
                upcoming = [g for g in {id(g): g for g in self.games(week).values()}.values()
                            if now <= g.kickoff <= now + timedelta(minutes=window_minutes)]
                if self.games(week) and not upcoming:
                    return RunResult("gameday", "Sin partidos próximos", "No empieza ningún partido en las "
                                     f"próximas {window_minutes // 60} horas.", notify=False)
        return self.lineup(gameday=True)

    def _candidates(self, week: int, roster: Roster, slots: Sequence[str]) -> list[Player]:
        listed: dict[str, parse.ListedPlayer] = {}
        positions = [p for p in ("QB", "RB", "WR", "TE", "K", "DEF") if eligible_slots([p], slots)]
        for pos in positions:
            try:
                rows = self.web.players(pos, status="A")
            except YahooSessionExpired:
                raise
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"no pude leer agentes libres {pos}: {exc}")
                continue
            limit = self.cfg.candidates_kdef if pos in ("K", "DEF") else self.cfg.candidates_per_position
            for r in rows[:limit]:
                listed.setdefault(r.pid, r)
        mine = {p.pid for p in roster.players}
        if self.sleeper and self.cfg.use_trending:
            try:
                db = self.sleeper.players()
                for t in self.sleeper.trending_adds():
                    sp = db.get(str(t.get("player_id")), {})
                    yid = str(sp.get("yahoo_id") or "")
                    if yid and yid not in mine and yid not in listed and sp.get("position") in positions:
                        listed[yid] = parse.ListedPlayer(pid=yid, name=sp.get("full_name", ""), team=sp.get("team") or "",
                                                         positions=[sp["position"]], availability="?")
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"sin tendencias de Sleeper: {exc}")
        out = []
        for pid, row in listed.items():
            if pid in mine:
                continue
            try:
                note = self.web.playernote(pid)
            except YahooSessionExpired:
                raise
            except Exception:  # noqa: BLE001
                continue
            owner = note.owner.lower()
            if note.owner_team_id or not (owner.startswith("free") or owner.startswith("waiver") or owner == "fa"
                                          or row.availability in ("FA",) or row.availability.startswith("W")):
                continue  # already on someone's roster
            if not (row.team or note.positions):
                continue  # no NFL team: retired / unsigned
            on_waivers = owner.startswith("waiver") or row.availability.startswith("W")
            positions_ = row.positions or note.positions
            pos = positions_[0] if positions_ else ""
            weekly = projected_weekly(note, week)
            date = re.search(r"\(([^)]*)\)", note.owner or row.availability)
            out.append(Player(
                pid=pid, name=row.name or note.name, position=pos, team=row.team,
                eligible=eligible_slots(positions_, slots), proj=weekly.get(week, 0.0),
                status=row.status or note.status, weekly=weekly,
                owner=(f"W ({date.group(1).strip()})" if date else "W") if on_waivers else "FA",
                percent_owned=note.percent_owned,
            ))
            out[-1].notes.append(row.add_url)
        # free agents get the same injury discount as your own players (Out = 0 this week, etc.)
        probs = dict(DEFAULT_PROBS)
        probs.update(self.cfg.status_probs)
        apply_status_model(out, self.cfg.p_play_overrides, probs)
        for c in out:
            apply_injury_outlook(c, week, status_is_for_this_game=True)
            c.proj = c.weekly.get(week, 0.0)
            c.p_play = None  # the discount is already inside weekly/proj: never count it twice
        return out

    _CANDIDATES_INJURY = True  # notebook cell 3b checks this to avoid a second discount

    def _weekly_for_roster(self, players: Sequence[Player], week: int) -> int:
        """Future-week projections for the roster. Returns the first week still to be played
        (the current week once most of its games are over counts as played)."""
        open_now = 0
        for p in players:
            try:
                note = self.web.playernote(p.pid)
                p.weekly = projected_weekly(note, week)
                open_now += week in note.projected_weeks
            except YahooSessionExpired:
                raise
            except Exception:  # noqa: BLE001
                p.weekly = {}
        start = week if open_now * 2 >= max(1, len(players)) else week + 1
        for p in players:
            p.weekly = {w: v for w, v in p.weekly.items() if w >= start}
            if not p.weekly:  # fall back to this week's projection, flat
                p.weekly = {w: p.proj for w in horizon(start, self.waiver_settings())}
            apply_injury_outlook(p, start, status_is_for_this_game=(start == week))
        return start

    def waivers(self, week: Optional[int] = None) -> RunResult:
        self.warnings = []
        week = week or self.week()
        roster = self.load_roster(week)
        self.enrich(roster.players, week)
        slots = roster.slots
        roster_limit = len(slots) + roster.bench_size
        balance = self.cfg.faab_balance
        rival_max = None
        try:
            teams = self.web.teams()
            mine = next((t for t in teams if t.team_id == self.cfg.team_id), None)
            if balance is None and mine is not None:
                balance = mine.faab
            others = [t.faab for t in teams if t.team_id != self.cfg.team_id and t.faab is not None]
            rival_max = max(others) if others else None
        except YahooSessionExpired:
            raise
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"no pude leer los saldos FAAB: {exc}")
        if balance is None:
            balance = roster.faab_balance
        active = [p for p in roster.players if p.slot != "IR"]
        ir_moves = [p for p in active if p.ir_eligible and p.slot == "BN"][: roster.open_ir_spots()]
        waiver_roster = [p for p in active if p not in ir_moves]
        week = self._weekly_for_roster(waiver_roster, week)  # skip a week that is already played
        candidates = self._candidates(week, roster, slots)
        wset = self.waiver_settings()
        repl = replacement_players(candidates, slots, horizon(week, wset), wset)
        history: list[int] = []
        try:
            history = [t.amount for t in self.web.transactions()]
        except YahooSessionExpired:
            raise
        except Exception:  # noqa: BLE001
            pass
        claims = plan_claims(waiver_roster, candidates, slots, week, roster_limit, balance, wset,
                             rival_max_balance=rival_max, replacements=repl,
                             league_bids=history if self.cfg.bid_history_cap else ())
        after = [p for p in waiver_roster if p.pid not in {c.drop.pid for c in claims if c.drop}] + [c.add for c in claims]
        dead, upgrades = bench_upgrades(after, candidates, slots, week, wset, repl,
                                        exclude={c.add.pid for c in claims})
        writes = []
        if claims and (self.cfg.rehearsal or (self.cfg.writes_enabled and self.cfg.auto_claims)):
            browser = self.browser()
            if browser is None:
                self.warnings.append("para pujar automáticamente instala Playwright (celda 1 del notebook)")
            else:
                dry = not (self.cfg.writes_enabled and self.cfg.auto_claims)
                if ir_moves and any(c.drop is None for c in claims):
                    # free the roster spot the claim counts on
                    if dry:
                        writes.append(WriteResult(True, "lineup", "simulacro: movería a IR a " +
                                                  ", ".join(p.name for p in ir_moves), dry_run=True))
                    else:
                        written = self.write_lineup({p.pid: "IR" for p in ir_moves}, week)
                        if written is not None:
                            writes.append(written)
                done = self.state.claimed(week)
                for c in claims:
                    if c.add.pid in done:
                        self.warnings.append(f"ya se había reclamado a {c.add.name} esta semana")
                        continue
                    add_url = next((n for n in c.add.notes if n.startswith("http")), "")
                    res = browser.claim(c.add.pid, c.drop.pid if c.drop else None,
                                        c.bid if c.on_waivers else None, add_url=add_url, dry_run=dry)
                    writes.append(res)
                    if res.ok and not res.dry_run:
                        self.state.add_claim(week, c.add.pid)
        text = rpt.claims_report(week, self.cfg.mode, claims, balance, rival_max, history, writes,
                                 ir_moves, self.warnings, f"{self.web.url('/players')}?status=A")
        text += rpt.bench_report(dead, upgrades)
        title = f"Waivers semana {week}: " + (
            ", ".join(f"{c.add.name} ${c.bid}" if c.on_waivers else f"{c.add.name} (FA)" for c in claims)
            if claims else "no pujes")
        return RunResult("waivers", title, text, changed=bool(claims), urgent=any(not w.ok for w in writes),
                         writes=writes, data={"claims": claims, "candidates": candidates, "roster": roster,
                                              "dead_spots": dead, "bench_upgrades": upgrades})

    def matchup(self, week: Optional[int] = None) -> RunResult:
        self.warnings = []
        week = week or self.week()
        html = self.league_home() if not self.cfg.week else self.web.league_home(week).html
        games = parse.parse_matchups(html)
        pair = next(((a, b) if a.team_id == self.cfg.team_id else (b, a)
                     for a, b in games if self.cfg.team_id in (a.team_id, b.team_id)), None)
        if pair is None:
            return RunResult("matchup", "Matchup no encontrado", "No encontré tu partido de esta semana en Yahoo.", notify=False)
        me, opp = pair
        roster = self.load_roster(week)
        self.enrich(roster.players, week)
        self.attach_actuals(roster)
        opp_roster = self.load_roster(week, team_id=opp.team_id)
        self.enrich(opp_roster.players, week)
        self.attach_actuals(opp_roster)
        mine = team_outlook(p for p in roster.players if p.is_starter)
        theirs = team_outlook(p for p in opp_roster.players if p.is_starter)
        if not any(started(p) for p in roster.players):  # before kickoff: count swap hedges too
            plan = current_plan(roster.players, roster.slots, self.opt_settings())
            mine.mean = plan.ev
        movable, free_slots = movable_part(opp_roster.players, roster.slots)
        audit = audit_lineup(movable, free_slots)
        wp_now = win_probability(mine.mean, mine.sd, theirs.mean, theirs.sd)
        wp_fixed = win_probability(mine.mean, mine.sd, theirs.mean + audit.fixable, theirs.sd)
        text = rpt.matchup_report(week, me.name or "Tú", opp.name or "Rival", mine, theirs, audit, wp_now, wp_fixed)
        return RunResult("matchup", f"Semana {week}: {_pct(wp_now)} de ganar", text,
                         data={"audit": audit, "win_probability": wp_now, "mine": mine, "opp": theirs})

    def preview(self, with_waivers: bool = True, week: Optional[int] = None) -> RunResult:
        """Scouting report on the next opponent: past scores, week projections slot by
        slot, win probability, byes, and free agents that raise *that* week's lineup."""
        self.warnings = []
        current = self.week()
        if week is None:
            now_roster = self.load_roster(current)
            self.enrich(now_roster.players, current)
            active = [p for p in now_roster.players if p.slot != "IR"]
            done = sum(1 for p in active if started(p) or p.locked)
            week = current + 1 if done * 2 >= max(1, len(active)) else current
        games = parse.parse_matchups(self.web.league_home(week).html)
        pair = next(((a, b) if a.team_id == self.cfg.team_id else (b, a)
                     for a, b in games if self.cfg.team_id in (a.team_id, b.team_id)), None)
        if pair is None:
            return RunResult("preview", "Rival no encontrado", f"No encontré tu partido de la semana {week}.", notify=False)
        me, opp = pair

        history = []
        for w in range(1, week):
            try:
                past = parse.parse_matchups(self.web.league_home(w).html)
            except YahooSessionExpired:
                raise
            except Exception:  # noqa: BLE001
                continue
            row = {"week": w, "mine": None, "mine_result": "", "opp": None, "opp_result": ""}
            for a, b in past:
                for side, other in ((a, b), (b, a)):
                    for key, tid in (("mine", me.team_id), ("opp", opp.team_id)):
                        if side.team_id == tid and side.score:
                            row[key] = side.score
                            row[f"{key}_result"] = ("G" if side.score > (other.score or 0)
                                                    else "P" if side.score < (other.score or 0) else "E")
            history.append(row)
        mine_scores = [h["mine"] for h in history if h["mine"]]
        opp_scores = [h["opp"] for h in history if h["opp"]]

        roster = self.load_roster(week)
        self.enrich(roster.players, week)
        opp_roster = self.load_roster(week, team_id=opp.team_id)
        self.enrich(opp_roster.players, week)
        plan = optimize_lineup(roster.players, roster.slots, self.opt_settings())
        opp_pool = [p for p in opp_roster.players if p.slot != "IR"]
        opp_best = assign(opp_pool, roster.slots, lambda p, s: p.expected + 1e-9)
        opp_total = sum(p.expected for p in opp_best if p is not None)
        slots_cmp = []
        for code in dict.fromkeys(roster.slots):  # same position: best vs best, second vs second
            mine_s = sorted((a.player for a in plan.assignments if a.slot == code and a.player),
                            key=lambda p: -p.expected)
            opp_s = sorted((o for s, o in zip(roster.slots, opp_best) if s == code and o), key=lambda p: -p.expected)
            for i in range(roster.slots.count(code)):
                m = mine_s[i] if i < len(mine_s) else None
                o = opp_s[i] if i < len(opp_s) else None
                slots_cmp.append({"slot": code, "mine": m, "opp": o,
                                  "diff": (m.expected if m else 0.0) - (o.expected if o else 0.0)})
        my_sd = lineup_sd(plan.starters())
        opp_sd = lineup_sd([p for p in opp_best if p is not None])
        wp = win_probability(plan.ev, my_sd, opp_total, opp_sd)

        strategy = []
        gap = plan.ev - opp_total
        weak = sorted((r for r in slots_cmp if r["diff"] < -1.0), key=lambda r: r["diff"])
        if opp_scores and len(opp_scores) >= 2:
            avg = sum(opp_scores) / len(opp_scores)
            strategy.append(f"El rival promedia {avg:.1f} pts en {len(opp_scores)} semanas; tu mejor alineación "
                            f"proyecta {plan.ev:.1f} ({plan.ev - avg:+.1f} contra su promedio).")
        if wp < 0.45:
            strategy.append(f"Llegas {abs(gap):.1f} pts abajo. Entre dos jugadores parecidos elige el de más techo "
                            "(el que puede hacer 25+), no el más seguro: necesitas una semana por encima de tu promedio.")
        elif wp > 0.55:
            strategy.append(f"Llegas {gap:.1f} pts arriba. Entre dos jugadores parecidos elige el más seguro "
                            "(más balones garantizados): no te hace falta arriesgar.")
        else:
            strategy.append("Partido parejo: cada punto de banca cuenta. Revisa inactivos el domingo antes de las 11:30 am (Este).")
        if weak:
            strategy.append("Donde él es más fuerte: " + ", ".join(f"{r['slot']} ({r['diff']:+.1f})" for r in weak[:3])
                            + ". Ahí es donde un agente libre te acerca más.")
        risky = [p for p in plan.starters() if 0 < p.prob < 1]
        if risky:
            strategy.append("Dudosos en tu alineación: " + ", ".join(f"{p.name} ({p.status}, {_pct(p.prob)})" for p in risky)
                            + ". Ten listo un suplente que juegue igual o más tarde.")

        targets = []
        if with_waivers:
            pool = [p for p in roster.players if p.slot != "IR"]
            base = best_total(pool, roster.slots, lambda p: p.expected)
            for c in self._candidates(week, roster, roster.slots):
                val = c.weekly.get(week, 0.0)
                gain = best_total(pool + [c], roster.slots,
                                  lambda p, c=c, val=val: val if p.pid == c.pid else p.expected) - base
                if gain > 0.5:
                    ros = sum(c.weekly.values()) / max(1, len(c.weekly))
                    targets.append({"name": c.name, "pos": c.position, "avail": c.owner, "proj": val,
                                    "gain": gain, "ros": ros})
            targets.sort(key=lambda t: -t["gain"])
            targets = targets[:6]

        pv = {
            "week": week, "my_name": me.name or "Tú", "opp_name": opp.name or "Rival", "history": history,
            "my_avg": sum(mine_scores) / len(mine_scores) if mine_scores else None,
            "opp_avg": sum(opp_scores) / len(opp_scores) if opp_scores else None,
            "my_total": plan.ev, "opp_total": opp_total, "win_probability": wp, "slots": slots_cmp,
            "my_byes": [p.name for p in roster.players if p.on_bye and p.slot != "IR"],
            "opp_byes": [p.name for p in opp_roster.players if p.on_bye and p.slot != "IR"],
            "strategy": strategy, "targets": targets,
        }
        text = rpt.preview_report(pv)
        if self.warnings:
            text += "\nAvisos:\n" + "\n".join(f"  ⚠ {w}" for w in self.warnings)
        return RunResult("preview", f"Semana {week} vs {pv['opp_name']}: {_pct(wp)} de ganar", text, data=pv)

    def attach_actuals(self, roster: Roster) -> None:
        """Real points for players whose game started (the lineup page shows projections)."""
        if not any(started(p) or (p.locked and p.kickoff is None) for p in roster.players):
            return
        try:
            points = self.web.team_points(team_id=roster.team_id, week=roster.week)
        except YahooSessionExpired:
            raise
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"no pude leer los puntos reales: {exc}")
            return
        for p in roster.players:
            if p.pid not in points:
                continue
            if not p.game_state and p.locked:
                p.game_state = "in"  # locked but no schedule: treat as under way
            if started(p):
                p.actual = points[p.pid]

    def trade(self, give: Sequence[str], get: Sequence[str], week: Optional[int] = None) -> RunResult:
        self.warnings = []
        week = week or self.week()
        roster = self.load_roster(week)
        self.enrich(roster.players, week)
        active = [p for p in roster.players if p.slot != "IR"]
        give_players = [p for p in active if p.pid in self.resolve(give, active)]
        if len(give_players) != len(give):
            missing = set(give) - {p.name for p in give_players}
            return RunResult("trade", "Trade: jugador no encontrado", f"No están en tu roster: {missing}", notify=False)
        start = self._weekly_for_roster(active, week)
        get_players, free = [], []
        for name in get:
            found = self.find_player(name, start, roster.slots)
            if found is None:
                return RunResult("trade", "Trade: jugador no encontrado",
                                 f"No encontré a «{name}» en Yahoo. Escríbelo como aparece en Yahoo.", notify=False)
            get_players.append(found)
            if re.match(r"(free agent|waivers?\b|fa$|w \()", found.owner.strip().lower()):
                free.append(found.name)
        rep = evaluate_trade(active, give_players, get_players, roster.slots, start,
                             len(roster.slots) + roster.bench_size, self.waiver_settings())
        text = rpt.trade_report(rep)
        if free:
            text = (f"Ojo: {', '.join(free)} no tiene dueño ({'agente libre / waivers'}). No se consigue con un "
                    "trade: se agrega en waivers (celda 6). Abajo va el cálculo como si lo agregaras.\n\n" + text)
        return RunResult("trade", rep.verdict.split(":")[0], text, notify=False, data={"trade": rep})

    def find_player(self, name: str, week: int, slots: Sequence[str]) -> Optional[Player]:
        """Any NFL player by name: Yahoo's own search first, then Sleeper's id map."""
        key = norm_name(name)
        hit = None
        try:
            hits = self.web.search_players(name)
            hit = next((h for h in hits if norm_name(h.name) == key), hits[0] if len(hits) == 1 else None)
        except YahooSessionExpired:
            raise
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"búsqueda en Yahoo falló: {exc}")
        pid = hit.pid if hit else ""
        sp = None
        if not pid and self.sleeper:
            try:
                sp = self.sleeper.match(name=name)
            except Exception:  # noqa: BLE001
                sp = None
            pid = str((sp or {}).get("yahoo_id") or "")
        if not pid:
            return None
        note = self.web.playernote(pid)
        positions = note.positions or (hit.positions if hit else []) or [(sp or {}).get("position", "")]
        return Player(
            pid=pid, name=note.name or (hit.name if hit else name), position=positions[0] if positions else "",
            team=(hit.team if hit else norm_team((sp or {}).get("team"))), eligible=eligible_slots(positions, slots),
            weekly=projected_weekly(note, week), owner=note.owner,
        )

    # --------------------------------------------------------- delivery
    def deliver(self, result: RunResult, dedupe: bool = True) -> RunResult:
        if not result.notify or not self.notifier.enabled:
            return result
        title = result.title
        if dedupe and result.task == "gameday":
            send, times = self.state.should_send("gameday", result.text, result.urgent, time.time(),
                                                 self.cfg.resend_minutes, self.cfg.max_resends)
            if not send:
                return result
            if times:
                title = f"RECORDATORIO {times}: {title}"
        errors = self.notifier.send(title, result.text, urgent=result.urgent, click_url=self.team_url)
        if errors:
            print("Avisos de notificación:", "; ".join(errors))
        return result

    def run(self, task: str, **kwargs: Any) -> RunResult:
        """Run a task and always deliver something: failures become urgent notifications."""
        try:
            result = getattr(self, task)(**kwargs)
        except YahooSessionExpired as exc:
            result = RunResult(task, "⚠ Sesión de Yahoo vencida", str(exc), urgent=True)
        except Exception as exc:  # noqa: BLE001
            result = RunResult(task, f"⚠ Error en {task}", f"{type(exc).__name__}: {exc}", urgent=True)
        return self.deliver(result)

    def loop(self, hours: float = 10.0, every_minutes: int = 10) -> None:
        """Keep checking game-day inactives (for a Colab session left open on Sunday)."""
        end = time.monotonic() + hours * 3600
        while time.monotonic() < end:
            result = self.run("gameday")
            stamp = datetime.now(self.tz).strftime("%H:%M")
            print(f"[{stamp}] {result.title}")
            time.sleep(every_minutes * 60)


def role_mod_protected(p: Player, never_drop: Sequence[str]) -> bool:
    keys = {norm_name(x) for x in never_drop} | set(never_drop)
    return p.pid in keys or norm_name(p.name) in keys


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


_DAYS = {"tue": 0, "wed": 1, "thu": 2, "fri": 3, "sat": 4, "sun": 5, "mon": 6}
_KICK_RE = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2}):(\d{2})\s*([ap]m)", re.I)


def kickoff_from_text(text: str, now: datetime, tz: ZoneInfo) -> Optional[datetime]:
    """'Sun 8:20 pm' -> that day of the current Yahoo week (Tuesday..Monday), in UTC."""
    m = _KICK_RE.search(text or "")
    if not m:
        return None
    day, hour, minute, ampm = m.group(1).lower(), int(m.group(2)), int(m.group(3)), m.group(4).lower()
    hour = hour % 12 + (12 if ampm == "pm" else 0)
    local_now = now.astimezone(tz)
    week_start = local_now.date() - timedelta(days=(local_now.weekday() - 1) % 7)  # last Tuesday
    date = week_start + timedelta(days=_DAYS[day])
    return datetime(date.year, date.month, date.day, hour, minute, tzinfo=tz).astimezone(timezone.utc)


def projected_weekly(note: parse.PlayerNote, week: int) -> dict[int, float]:
    """Only Yahoo's projections (weeks marked with *), never points already scored."""
    return {w: v for w, v in note.weekly.items() if w >= week and w in note.projected_weeks}


def apply_injury_outlook(p: Player, week: int, status_is_for_this_game: bool = True) -> None:
    """Yahoo's future-week projections assume everyone is healthy. Discount the
    next weeks for injured players: IR-type out 4 weeks, Out/Doubtful half next
    week, and this week times the chance of playing.

    When `week` is the week *after* the game the status was given for (waivers
    run on Monday/Tuesday), Out/Doubtful count half in `week` instead of zero."""
    if not p.weekly:
        return
    if p.status in ("IR", "PUP", "NFI", "SUSP"):
        factors = {week + i: 0.0 for i in range(4)}
    elif p.status in ("O", "NA", "D"):
        if status_is_for_this_game:
            factors = {week: p.prob if p.status == "D" else 0.0, week + 1: 0.5}
        else:
            factors = {week: 0.5}
    elif p.status == "Q" and status_is_for_this_game:
        factors = {week: p.prob}
    else:
        factors = {}
    for w, f in factors.items():
        if w in p.weekly:
            p.weekly[w] = round(p.weekly[w] * f, 2)
