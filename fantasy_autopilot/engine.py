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

from . import report as rpt
from .config import Config
from .matchup import audit_lineup, lineup_sd, win_probability
from .models import Game, Player, Roster, WriteResult
from .nfl import eligible_slots, norm_name
from .notify import Notifier
from .optimizer import (
    OptimizerSettings,
    current_plan,
    optimize_lineup,
    plan_moves,
    plan_objective,
)
from .sources import espn
from .sources.sleeper import Sleeper, injury_fields
from .status import DEFAULT_PROBS, SEVERITY, apply_status_model, is_out, norm_status
from .trade import evaluate_trade
from .waivers import WaiverSettings, horizon, plan_claims, replacement_players
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
        if week not in self._games:
            self._games[week] = {}
            if self.cfg.use_espn:
                try:
                    self._games[week] = espn.games_by_team(self.games_fetcher(self.season(), week))
                except Exception as exc:  # noqa: BLE001
                    self.warnings.append(f"ESPN no respondió (sin horarios de partidos): {exc}")
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
                if g.state in ("in", "post") or g.kickoff <= now:
                    p.locked = True
            elif len(games) >= 10 and p.team:
                p.on_bye = True  # full slate known and his team is not in it
            else:
                # ESPN unavailable (it blocks some cloud IPs, e.g. Colab): use Yahoo's own
                # "Sun 1:00 pm" text, read in the configured time zone.
                p.kickoff = kickoff_from_text(p.game_text, now, self.tz)
                if p.kickoff is not None:
                    from_text += 1
                    if p.kickoff <= now:
                        p.locked = True
                if re.search(r"\bFinal\b", p.game_text):
                    p.locked = True
        if from_text:
            self.warnings.append(f"horarios tomados del texto de Yahoo ({from_text} jugadores), hora de {self.cfg.timezone}")
        missing_time = [p.name for p in players if p.kickoff is None and not p.on_bye and p.slot != "IR"]
        if missing_time:
            self.warnings.append("sin hora de partido para: " + ", ".join(missing_time[:6])
                                 + " (sin hora no se planean cambios de último minuto para ellos)")
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
                p.proj = round((1 - w) * p.proj + w * s, 2)

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
        opt = self.opt_settings()
        before = current_plan(roster.players, roster.slots, opt)
        plan = optimize_lineup(
            roster.players, roster.slots, opt,
            pins_start=self.resolve(self.cfg.pins_start, roster.players),
            pins_bench=self.resolve(self.cfg.pins_bench, roster.players),
        )
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
        failed = any(not w.ok for w in writes)
        urgent = failed or bool(stuck) or (gameday and decided and self.cfg.mode == "sugerir")
        if gameday:
            title = ("⚠ Cambia tu alineación YA" if decided and self.cfg.mode == "sugerir"
                     else "Alineación ajustada antes del partido" if decided else "Alineación: sin cambios")
        else:
            title = f"Alineación semana {week}: " + (f"{len(moves)} cambio(s) {plan.naive - before.naive:+.2f}" if decided else "sin cambios")
        if stuck:
            title = "⚠ Titular fuera y sin reemplazo disponible"
        notify = (not gameday) or decided or bool(stuck) or failed
        return RunResult("gameday" if gameday else "lineup", title, text, changed=decided, urgent=urgent,
                         notify=notify, writes=writes,
                         data={"week": week, "roster": roster, "plan": plan, "before": before, "moves": moves})

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
            for r in rows[: self.cfg.candidates_per_position]:
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
            on_waivers = owner.startswith("waiver") or row.availability.startswith("W")
            positions_ = row.positions or note.positions
            pos = positions_[0] if positions_ else ""
            out.append(Player(
                pid=pid, name=row.name or note.name, position=pos, team=row.team,
                eligible=eligible_slots(positions_, slots), proj=note.weekly.get(week, 0.0),
                status=row.status or note.status,
                weekly={w: v for w, v in note.weekly.items() if w >= week},
                owner=("W " + note.owner.split(" ", 1)[1]) if on_waivers and " " in note.owner else ("W" if on_waivers else "FA"),
                percent_owned=note.percent_owned,
            ))
            out[-1].notes.append(row.add_url)
        return out

    def _weekly_for_roster(self, players: Sequence[Player], week: int) -> None:
        for p in players:
            try:
                note = self.web.playernote(p.pid)
                p.weekly = {w: v for w, v in note.weekly.items() if w >= week}
            except YahooSessionExpired:
                raise
            except Exception:  # noqa: BLE001
                p.weekly = {}
            if not p.weekly:  # fall back to this week's projection, flat
                p.weekly = {w: p.proj for w in horizon(week, self.waiver_settings())}
            apply_injury_outlook(p, week)

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
        self._weekly_for_roster(waiver_roster, week)
        candidates = self._candidates(week, roster, slots)
        wset = self.waiver_settings()
        repl = replacement_players(candidates, slots, horizon(week, wset), wset)
        claims = plan_claims(waiver_roster, candidates, slots, week, roster_limit, balance, wset,
                             rival_max_balance=rival_max, replacements=repl)
        history: list[int] = []
        try:
            history = [t.amount for t in self.web.transactions()]
        except YahooSessionExpired:
            raise
        except Exception:  # noqa: BLE001
            pass
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
        title = f"Waivers semana {week}: " + (
            ", ".join(f"{c.add.name} ${c.bid}" if c.on_waivers else f"{c.add.name} (FA)" for c in claims)
            if claims else "no pujes")
        return RunResult("waivers", title, text, changed=bool(claims), urgent=any(not w.ok for w in writes),
                         writes=writes, data={"claims": claims, "candidates": candidates, "roster": roster})

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
        plan = current_plan(roster.players, roster.slots, self.opt_settings())
        opp_roster = self.load_roster(week, team_id=opp.team_id)
        self.enrich(opp_roster.players, week)
        audit = audit_lineup(opp_roster.players, roster.slots)
        my_sd = lineup_sd(plan.starters())
        opp_sd = lineup_sd(audit.starters)
        wp_now = win_probability(plan.ev, my_sd, audit.current, opp_sd)
        wp_fixed = win_probability(plan.ev, my_sd, audit.optimal, opp_sd)
        text = rpt.matchup_report(week, me.name or "Tú", opp.name or "Rival", plan.ev, me.projected, opp.projected,
                                  audit, wp_now, wp_fixed)
        return RunResult("matchup", f"Semana {week}: {_pct(wp_now)} de ganar", text,
                         data={"audit": audit, "win_probability": wp_now})

    def trade(self, give: Sequence[str], get: Sequence[str], week: Optional[int] = None) -> RunResult:
        self.warnings = []
        week = week or self.week()
        roster = self.load_roster(week)
        active = [p for p in roster.players if p.slot != "IR"]
        self._weekly_for_roster(active, week)
        give_players = [p for p in active if p.pid in self.resolve(give, active)]
        if len(give_players) != len(give):
            missing = set(give) - {p.name for p in give_players}
            return RunResult("trade", "Trade: jugador no encontrado", f"No están en tu roster: {missing}", notify=False)
        get_players = []
        for name in get:
            sp = self.sleeper.match(name=name) if self.sleeper else None
            yid = str(sp.get("yahoo_id") or "") if sp else ""
            if not yid:
                return RunResult("trade", "Trade: jugador no encontrado", f"No encontré el id de Yahoo de {name}.", notify=False)
            note = self.web.playernote(yid)
            positions = note.positions or [sp.get("position", "")]
            get_players.append(Player(pid=yid, name=note.name or name, position=positions[0], team=sp.get("team") or "",
                                      eligible=eligible_slots(positions, roster.slots),
                                      weekly={w: v for w, v in note.weekly.items() if w >= week}))
        rep = evaluate_trade(active, give_players, get_players, roster.slots, week,
                             len(roster.slots) + roster.bench_size, self.waiver_settings())
        return RunResult("trade", rep.verdict.split(":")[0], rpt.trade_report(rep), notify=False, data={"trade": rep})

    # --------------------------------------------------------- delivery
    def deliver(self, result: RunResult, dedupe: bool = True) -> RunResult:
        if not result.notify or not self.notifier.enabled:
            return result
        if dedupe and result.task == "gameday" and self.state.seen("gameday", result.text):
            return result
        errors = self.notifier.send(result.title, result.text, urgent=result.urgent, click_url=self.team_url)
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


def apply_injury_outlook(p: Player, week: int) -> None:
    """Yahoo's future-week projections assume everyone is healthy. Discount the
    next weeks for injured players: IR-type out 4 weeks, Out/Doubtful half next
    week, and this week times the chance of playing."""
    if not p.weekly:
        return
    if p.status in ("IR", "PUP", "NFI", "SUSP"):
        factors = {week + i: 0.0 for i in range(4)}
    elif p.status in ("O", "NA", "D"):
        factors = {week: p.prob if p.status == "D" else 0.0, week + 1: 0.5}
    elif p.status == "Q":
        factors = {week: p.prob}
    else:
        factors = {}
    for w, f in factors.items():
        if w in p.weekly:
            p.weekly[w] = round(p.weekly[w] * f, 2)
