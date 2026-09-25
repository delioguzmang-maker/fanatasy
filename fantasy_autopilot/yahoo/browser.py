"""Real-browser (Playwright/Chromium) driver for the writes: lineups and waiver claims.

Why a browser: the add/drop + FAAB flow is a multi-step page that may depend
on JavaScript, and the lineup "Save Changes" button may too. The browser
loads your session cookies, does exactly the clicks you would do, takes
screenshots before/after, and then re-reads the page to verify.

The waiver-claim steps were written from Yahoo's classic add/drop form and
could not be tested against a live league from where this was built, so:
always run ``dry_run=True`` first and look at the screenshot.
"""

from __future__ import annotations

import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from ..models import WriteResult
from ..nfl import norm_slot
from ..sources.http import BROWSER_UA
from . import parse
from .cookies import CookieSource, parse_cookies, to_playwright
from .web import BASE_URL

SAVE_SELECTORS = (
    'button[name="jsubmit"]',
    'input[name="jsubmit"]',
    "button.roster-save-btn",
    'button:has-text("Save Changes")',
    'input[type="submit"][value*="Save"]',
)
SUBMIT_WORDS = (
    ("submit claim", 0), ("make claim", 0), ("add player", 1), ("confirm", 2),
    ("submit", 3), ("continue", 4), ("claim", 5), ("add", 6),
)
CANCEL_RE = re.compile(r"\b(cancel|back|undo|remove|close)\b", re.I)
SUCCESS_RE = re.compile(
    r"(claim|waiver|transaction)[^.\n]{0,80}(submitted|pending|placed|success|received)"
    r"|has been (added|submitted)|successfully|added to your (team|roster)",
    re.I,
)
ERROR_RE = re.compile(
    r"there was a problem|not allowed|invalid|exceeds?|insufficient|cannot be|can't be|unable to|roster is full",
    re.I,
)


def run_sync(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Playwright's sync API refuses to run inside a live asyncio loop (Jupyter/Colab).
    In that case, run it in a worker thread, which has no loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args, **kwargs)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args, **kwargs).result()


class YahooBrowser:
    def __init__(
        self,
        league_id: str,
        team_id: str,
        cookies: CookieSource,
        base_url: str = BASE_URL,
        headless: bool = True,
        evidence_dir: Optional[Path] = None,
        timeout_ms: int = 30000,
        user_agent: str = BROWSER_UA,
        executable_path: Optional[str] = None,
    ):
        self.league_id, self.team_id = str(league_id), str(team_id)
        # Only needed when the installed Chromium does not match the Playwright version.
        self.executable_path = executable_path or os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
        self.cookies = parse_cookies(cookies)
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.timeout_ms = timeout_ms
        self.user_agent = user_agent

    # -------------------------------------------------------------- public
    def set_lineup(self, target: dict[str, str], week: Optional[int] = None, dry_run: bool = True) -> WriteResult:
        return run_sync(self._set_lineup, target, week, dry_run)

    def claim(
        self,
        add_pid: str,
        drop_pid: Optional[str] = None,
        bid: Optional[int] = None,
        add_url: str = "",
        dry_run: bool = True,
    ) -> WriteResult:
        return run_sync(self._claim, add_pid, drop_pid, bid, add_url, dry_run)

    def snapshot(self, path: str, label: str = "pagina") -> list[str]:
        """Save screenshot + HTML of any league page (for calibrating selectors)."""
        return run_sync(self._snapshot, path, label)

    # ------------------------------------------------------------ plumbing
    def _open(self, pw: Any) -> tuple[Any, Any, Any]:
        browser = pw.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            executable_path=self.executable_path,
        )
        ctx = browser.new_context(user_agent=self.user_agent, viewport={"width": 1400, "height": 1000}, locale="en-US")
        ctx.add_cookies(to_playwright(self.cookies))
        page = ctx.new_page()
        page.set_default_timeout(self.timeout_ms)
        return browser, ctx, page

    def _evidence(self, page: Any, label: str) -> list[str]:
        if not self.evidence_dir:
            return []
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        base = self.evidence_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{label}"
        out = []
        try:
            page.screenshot(path=f"{base}.png", full_page=True)
            out.append(f"{base}.png")
        except Exception:  # noqa: BLE001 - evidence must never break a run
            pass
        try:
            Path(f"{base}.html").write_text(page.content())
            out.append(f"{base}.html")
        except Exception:  # noqa: BLE001
            pass
        return out

    def _settle(self, page: Any) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001 - busy pages may never go idle
            page.wait_for_timeout(2500)

    def _logged_out(self, page: Any) -> bool:
        return parse.is_login_page(page.url, page.content())

    def _team_url(self, week: Optional[int]) -> str:
        url = f"{self.base_url}/f1/{self.league_id}/{self.team_id}?stat1=P&stat2=PW"
        return f"{url}&week={week}" if week else url

    # -------------------------------------------------------------- lineup
    def _set_lineup(self, target: dict[str, str], week: Optional[int], dry_run: bool) -> WriteResult:
        from playwright.sync_api import sync_playwright

        target = {pid: norm_slot(slot) for pid, slot in target.items()}
        evidence: list[str] = []
        url = self._team_url(week)
        with sync_playwright() as pw:
            browser, _, page = self._open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                if self._logged_out(page):
                    return WriteResult(False, "lineup", "sesión de Yahoo vencida (actualiza YAHOO_COOKIES)")
                evidence += self._evidence(page, "alineacion-antes")
                try:
                    parse.build_lineup_submission(page.content(), page.url, target)
                except parse.FormError as exc:
                    return WriteResult(False, "lineup", f"formulario de alineación: {exc}", evidence=evidence)
                for pid, slot in sorted(target.items(), key=lambda kv: kv[1] != "BN"):
                    select = page.locator(f'select[name="{pid}"]').first
                    value = select.evaluate(
                        """(el, wanted) => {
                            const norm = s => (s || '').trim().toUpperCase().replace(/\\s+/g, '');
                            const o = [...el.options].find(o => norm(o.value) === wanted || norm(o.text) === wanted);
                            return o ? o.value : null; }""",
                        slot,
                    )
                    if value is None:
                        return WriteResult(False, "lineup", f"{pid} no tiene la opción {slot}", evidence=evidence)
                    select.select_option(value=value, force=True)
                if dry_run:
                    evidence += self._evidence(page, "alineacion-simulacro")
                    return WriteResult(True, "lineup", "simulacro: se eligieron las posiciones pero no se guardó",
                                       applied=[f"{p}->{s}" for p, s in target.items()], dry_run=True, evidence=evidence)
                saved = False
                for sel in SAVE_SELECTORS:
                    button = page.locator(sel).first
                    if button.count() and button.is_visible():
                        button.click()
                        saved = True
                        break
                if not saved:
                    page.evaluate(
                        "pid => { const f = document.querySelector(`select[name='${pid}']`).form;"
                        " f.requestSubmit ? f.requestSubmit() : f.submit(); }",
                        next(iter(target)),
                    )
                self._settle(page)
                page.goto(url, wait_until="domcontentloaded")
                evidence += self._evidence(page, "alineacion-despues")
                rows = {r.pid: r.slot for r in parse.parse_team_page(page.content()).rows}
            finally:
                browser.close()
        applied = [f"{p}->{s}" for p, s in target.items() if rows.get(p) == s]
        failed = [f"{p}->{s} (ahora {rows.get(p, '?')})" for p, s in target.items() if rows.get(p) != s]
        ok = not failed
        return WriteResult(ok, "lineup", "alineación guardada y verificada" if ok else "Yahoo no aplicó todos los cambios",
                           applied=applied, failed=failed, evidence=evidence)

    # ------------------------------------------------------------ waivers
    def _find_drop_control(self, page: Any, drop_pid: str) -> Optional[Any]:
        for sel in (
            f'input[type="radio"][value="{drop_pid}"]',
            f'input[type="checkbox"][value="{drop_pid}"]',
            f'input[type="radio"][value$=".p.{drop_pid}"]',
            f'input[type="checkbox"][value$=".p.{drop_pid}"]',
            f'input[type="radio"][value*="{drop_pid}"]',
            f'input[type="checkbox"][value*="{drop_pid}"]',
        ):
            loc = page.locator(sel).first
            if loc.count():
                return loc
        row = page.locator("tr, li").filter(has=page.locator(f'a[href*="/players/{drop_pid}"]')).first
        if row.count():
            loc = row.locator('input[type="radio"], input[type="checkbox"]').first
            if loc.count():
                return loc
            loc = row.get_by_role("button", name=re.compile(r"drop", re.I)).first
            if loc.count():
                return loc
        return None

    def _find_bid_field(self, page: Any) -> Optional[Any]:
        for sel in (
            'input[name*="faab" i]',
            'input[id*="faab" i]',
            'input[name*="bid" i]',
            'input[id*="bid" i]',
            'input[name*="amount" i]',
            'form input[type="number"]',
        ):
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                return loc
        return None

    def _find_submit(self, page: Any) -> Optional[tuple[Any, str]]:
        buttons = page.locator("form").locator('button, input[type="submit"], input[type="button"]')
        best: Optional[tuple[int, Any, str]] = None
        for i in range(min(buttons.count(), 80)):
            b = buttons.nth(i)
            try:
                if not b.is_visible() or not b.is_enabled():
                    continue
                label = (b.inner_text() or b.get_attribute("value") or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not label or CANCEL_RE.search(label):
                continue
            low = label.lower()
            for word, rank in SUBMIT_WORDS:
                if word in low:
                    if best is None or rank < best[0]:
                        best = (rank, b, label)
                    break
        return (best[1], best[2]) if best else None

    def _claim(self, add_pid: str, drop_pid: Optional[str], bid: Optional[int], add_url: str, dry_run: bool) -> WriteResult:
        from playwright.sync_api import sync_playwright

        url = add_url or f"{self.base_url}/f1/{self.league_id}/addplayer?apid={add_pid}"
        evidence: list[str] = []
        steps: list[str] = []
        with sync_playwright() as pw:
            browser, _, page = self._open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                if self._logged_out(page):
                    return WriteResult(False, "claim", "sesión de Yahoo vencida (actualiza YAHOO_COOKIES)")
                evidence += self._evidence(page, f"reclamo-{add_pid}-inicio")
                drop_done = drop_pid is None
                bid_done = bid is None
                for step in range(3):
                    if not drop_done:
                        control = self._find_drop_control(page, drop_pid)
                        if control is not None:
                            if (control.get_attribute("type") or "").lower() in ("radio", "checkbox"):
                                control.check(force=True)
                            else:
                                control.click()
                            drop_done = True
                            steps.append(f"marcó soltar a {drop_pid}")
                    if not bid_done:
                        field = self._find_bid_field(page)
                        if field is not None:
                            field.fill(str(bid))
                            bid_done = True
                            steps.append(f"escribió ${bid}")
                    found = self._find_submit(page)
                    if found is None:
                        break
                    button, label = found
                    if dry_run:
                        evidence += self._evidence(page, f"reclamo-{add_pid}-simulacro")
                        missing = []
                        if not drop_done:
                            missing.append("no encontró dónde soltar al jugador")
                        if not bid_done:
                            missing.append("no encontró la casilla de la puja (puede estar en el paso siguiente)")
                        detail = (f"simulacro: {', '.join(steps) or 'sin pasos previos'}; haría clic en '{label}'"
                                  + (f". Atención: {'; '.join(missing)}" if missing else ""))
                        return WriteResult(not missing, "claim", detail,
                                           applied=steps, dry_run=True, evidence=evidence)
                    button.click()
                    steps.append(f"clic en '{label}'")
                    self._settle(page)
                    body = page.inner_text("body")
                    if SUCCESS_RE.search(body) or ERROR_RE.search(body[:3000]):
                        break  # done, or Yahoo complained: never keep clicking blindly
                body = page.inner_text("body")
                evidence += self._evidence(page, f"reclamo-{add_pid}-final")
            finally:
                browser.close()
        if not drop_done or not bid_done:
            return WriteResult(False, "claim", "no se completó el formulario: " + "; ".join(steps), applied=steps, evidence=evidence)
        if SUCCESS_RE.search(body) and not ERROR_RE.search(body[:3000]):
            return WriteResult(True, "claim", "Yahoo confirmó el reclamo", applied=steps, evidence=evidence)
        problem = ERROR_RE.search(body)
        detail = f"no pude confirmar el reclamo ({problem.group(0)})" if problem else "no pude confirmar el reclamo; revísalo en la app"
        return WriteResult(False, "claim", detail, applied=steps, evidence=evidence)

    def _snapshot(self, path: str, label: str) -> list[str]:
        from playwright.sync_api import sync_playwright

        url = path if path.startswith("http") else f"{self.base_url}/f1/{self.league_id}{path}"
        with sync_playwright() as pw:
            browser, _, page = self._open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                self._settle(page)
                return self._evidence(page, label)
            finally:
                browser.close()
