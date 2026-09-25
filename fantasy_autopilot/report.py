"""Human-readable reports (Spanish). Every recommendation carries its numbers."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

from .matchup import LineupAudit
from .models import LineupPlan, Move, Player, WriteResult
from .optimizer import OptimizerSettings, breakeven_probability
from .trade import TradeReport
from .waivers import ClaimPlan

MODE_TEXT = {
    "sugerir": "solo sugerencias (no toca Yahoo)",
    "simulacro": "simulacro (prueba los cambios sin guardarlos)",
    "automatico": "automático (guarda los cambios en Yahoo)",
}


def when(dt: Optional[datetime], tz: ZoneInfo) -> str:
    if dt is None:
        return "hora ?"
    local = dt.astimezone(tz)
    days = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    hour = local.strftime("%I:%M %p").lstrip("0").lower()
    return f"{days[local.weekday()]} {hour}"


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def lineup_table(plan: LineupPlan) -> list[str]:
    lines = []
    for a in plan.assignments:
        if a.player is None:
            lines.append(f"  {a.slot:<6} (vacío)")
            continue
        p = a.player
        flag = f" [{p.status} {_pct(p.prob)}]" if p.status else ""
        lock = " 🔒" if p.locked else ""
        lines.append(f"  {a.slot:<6} {p.name:<24} {p.proj:6.2f}{flag}{lock}")
    return lines


def move_lines(moves: Sequence[Move]) -> list[str]:
    out = []
    for i, m in enumerate(moves, 1):
        p = m.player
        if m.to_slot == "BN":
            out.append(f"  {i}. {p.name} ({m.from_slot}, {p.proj:.2f}) → banca")
        elif m.from_slot == "BN":
            out.append(f"  {i}. {p.name} ({p.position}, {p.proj:.2f}) → {m.to_slot}")
        else:
            out.append(f"  {i}. {p.name}: {m.from_slot} → {m.to_slot}")
    return out


def uncertain_lines(players: Iterable[Player], plan: LineupPlan, settings: OptimizerSettings, tz: ZoneInfo) -> list[str]:
    by_pid = {p.pid: p for p in players}
    starters = {a.player.pid for a in plan.assignments if a.player}
    out = []
    for p in sorted(by_pid.values(), key=lambda x: -x.proj):
        if p.slot == "IR" or not p.status or p.prob >= 1.0:
            continue
        head = f"  {p.name} ({p.status}, juega {_pct(p.prob)}, {when(p.kickoff, tz)})"
        if p.practice:
            head += f" práctica: {p.practice}"
        if p.pid in starters:
            backup = by_pid.get(plan.hedges.get(p.pid, ""))
            tail = (f" — titular; si sale inactivo entra {backup.name} ({backup.proj:.2f}, {when(backup.kickoff, tz)})"
                    if backup else " — titular SIN respaldo que juegue después")
        elif p.prob == 0.0:
            tail = " — fuera"
        else:
            need = breakeven_probability(p, plan, settings)
            tail = f" — banca: necesita ≥{_pct(need)} para entrar" if need else " — banca"
        out.append(head + tail)
    return out


def write_lines(results: Sequence[WriteResult]) -> list[str]:
    out = []
    for r in results:
        mark = "✅" if r.ok else "❌"
        tag = " (simulacro)" if r.dry_run else ""
        out.append(f"{mark} {r.detail}{tag}")
        out += [f"   - no aplicado: {f}" for f in r.failed]
        out += [f"   - evidencia: {e}" for e in r.evidence if e.endswith(".png")]
    return out


def lineup_report(
    week: int,
    mode: str,
    players: Sequence[Player],
    before: LineupPlan,
    plan: LineupPlan,
    moves: Sequence[Move],
    obj_before: float,
    obj_after: float,
    decided: bool,
    threshold: float,
    writes: Sequence[WriteResult],
    settings: OptimizerSettings,
    tz: ZoneInfo,
    warnings: Sequence[str] = (),
    link: str = "",
) -> str:
    lines = [f"Semana {week} · modo: {MODE_TEXT.get(mode, mode)}"]
    if moves:
        verb = "Cambios" if decided else f"Cambios posibles (no alcanzan el mínimo de {threshold:.2f} pts)"
        lines.append(f"{verb} ({plan.naive - before.naive:+.2f} pts proyectados):")
        lines += move_lines(moves)
    else:
        lines.append("Tu alineación ya es la óptima. No hay cambios.")
    lines.append(f"Proyección Yahoo (suma simple): {before.naive:.2f} → {plan.naive:.2f}")
    lines.append(f"Valor esperado con riesgo de inactivos: {before.ev:.2f} → {plan.ev:.2f}")
    lines.append("Alineación recomendada:")
    lines += lineup_table(plan)
    doubts = uncertain_lines(players, plan, settings, tz)
    if doubts:
        lines.append("Jugadores dudosos:")
        lines += doubts
    if writes:
        lines.append("Resultado en Yahoo:")
        lines += write_lines(writes)
    elif moves and decided and mode == "sugerir":
        lines.append("Hazlo en la app de Yahoo (modo sugerir: no se tocó nada).")
    if warnings:
        lines.append("Avisos:")
        lines += [f"  ⚠ {w}" for w in warnings]
    if link:
        lines.append(link)
    return "\n".join(lines)


def claims_report(
    week: int,
    mode: str,
    claims: Sequence[ClaimPlan],
    balance: Optional[int],
    rival_max: Optional[int],
    history: Sequence[int],
    writes: Sequence[WriteResult],
    ir_moves: Sequence[Player] = (),
    warnings: Sequence[str] = (),
    link: str = "",
) -> str:
    lines = [f"Waivers semana {week} · modo: {MODE_TEXT.get(mode, mode)}"]
    lines.append(f"Saldo FAAB: {'$' + str(balance) if balance is not None else 'desconocido'}"
                 + (f" · el rival con más saldo tiene ${rival_max}" if rival_max is not None else ""))
    if history:
        lines.append("Pujas ganadoras recientes en tu liga: " + ", ".join(f"${x}" for x in history[:10]))
    for p in ir_moves:
        lines.append(f"• Mueve a {p.name} al puesto IR: libera un lugar sin soltar a nadie.")
    if not claims:
        lines.append("No hay ningún jugador disponible que mejore tu alineación lo suficiente. No pujes.")
    for i, c in enumerate(claims, 1):
        drop = f"soltar a {c.drop.name}" if c.drop else "sin soltar a nadie (hay espacio)"
        kind = f"puja ${c.bid}" if c.on_waivers else "agente libre: agrégalo ya ($0)"
        nxt = ", ".join(f"{x:.1f}" for x in c.next_weeks)
        lines.append(f"{i}. Agregar {c.add.name} ({c.add.position}, {c.add.team}) · {drop} · {kind}")
        lines.append(f"   Sube tu alineación {c.gain:.1f} pts ponderados en lo que queda "
                     f"({c.avg_gain:.2f}/semana). Proyección próximas semanas: {nxt}")
        lines.append(f"   Puja: {c.bid_reason}")
    if writes:
        lines.append("Resultado en Yahoo:")
        lines += write_lines(writes)
    if warnings:
        lines.append("Avisos:")
        lines += [f"  ⚠ {w}" for w in warnings]
    if link:
        lines.append(link)
    return "\n".join(lines)


def matchup_report(
    week: int,
    my_name: str,
    opp_name: str,
    my_mean: float,
    yahoo_mine: Optional[float],
    yahoo_opp: Optional[float],
    audit: LineupAudit,
    wp_now: float,
    wp_fixed: float,
) -> str:
    lines = [f"Semana {week}: {my_name} vs {opp_name}"]
    if yahoo_mine is not None and yahoo_opp is not None:
        lines.append(f"Yahoo proyecta {yahoo_mine:.2f} vs {yahoo_opp:.2f}")
    lines.append(f"Tu valor esperado (con riesgo de inactivos): {my_mean:.2f}")
    lines.append(f"Rival tal como está: {audit.current:.2f} · si corrige su alineación: {audit.optimal:.2f} "
                 f"({audit.fixable:+.2f})")
    lines.append(f"Probabilidad de ganar (modelo normal): {_pct(wp_now)} hoy · {_pct(wp_fixed)} si el rival corrige")
    if audit.issues:
        lines.append("Errores en la alineación del rival:")
        lines += [f"  • {x}" for x in audit.issues]
    lines.append("La probabilidad es una estimación con volatilidades típicas por posición, no un dato de Yahoo.")
    return "\n".join(lines)


def trade_report(rep: TradeReport) -> str:
    lines = [
        "Trade: das " + ", ".join(p.name for p in rep.give) + " · recibes " + ", ".join(p.name for p in rep.get),
        *rep.checks,
        "Por semana (+ = mejor para ti): " + ", ".join(f"S{w} {d:+.1f}" for w, d in sorted(rep.per_week.items())),
        f"Veredicto: {rep.verdict}",
    ]
    return "\n".join(lines)
