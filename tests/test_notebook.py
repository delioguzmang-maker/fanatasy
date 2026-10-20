"""Run the Colab notebook's cells (3-7) against the local Yahoo mock."""

import json
import sys
import types
from pathlib import Path

from datetime import datetime, timezone

from fantasy_autopilot.config import Config
from mock_yahoo import LEAGUE, TEAM, MockYahoo

FRIDAY = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)
NOTEBOOK = Path(__file__).resolve().parents[1] / "FantasyAutopilot.ipynb"


def code_cells():
    nb = json.loads(NOTEBOOK.read_text())
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


def cell(prefix):
    return next(c for c in code_cells() if c.startswith(prefix))


def test_notebook_is_valid_and_has_the_steps():
    cells = code_cells()
    titles = [c.splitlines()[0] for c in cells]
    assert titles[0].startswith("#@title 1. Instalar")
    for step in ("3b. Parches", "4b. Alerta de rol", "6c. Inteligencia", "6d. Lesionados",
                 "Piloto automático del domingo"):
        assert any(step in t for t in titles), step
    assert any(c.startswith("# ===== CELDA 7d") for c in cells)
    assert any(c.startswith("# ===== CELDA 7c") for c in cells)
    for src in cells[1:]:  # everything but the install cell (shell magics) must be valid Python
        compile(src, "cell", "exec")


def test_notebook_cells_run_against_mock(tmp_path, monkeypatch, capsys):
    import time

    from fantasy_autopilot import engine
    from fantasy_autopilot.sources import sleeper
    from fantasy_autopilot.yahoo import web

    cookies = json.dumps([{"name": "T", "value": "session", "domain": "127.0.0.1", "path": "/"}])
    colab = types.ModuleType("google.colab")
    colab.userdata = types.SimpleNamespace(get=lambda key: cookies)
    google = types.ModuleType("google")
    google.colab = colab
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.colab", colab)
    # cell 3b patches classes for the whole Colab session: undo it after this test
    monkeypatch.setattr(time, "sleep", lambda s: None)
    for cls, names in ((web.YahooWeb, ("get", "playernote", "_get_original", "_playernote_original")),
                       (sleeper.Sleeper, ("players", "_players_original")),
                       (engine.Autopilot, ("_candidates", "_candidates_original"))):
        for name in names:
            monkeypatch.setattr(cls, name, getattr(cls, name, None), raising=False)
            if getattr(cls, name) is None:
                monkeypatch.delattr(cls, name)
    with MockYahoo() as mock:
        cfg = Config(league_id=LEAGUE, team_id=TEAM, state_dir=str(tmp_path), quiet=True,
                     use_sleeper=False, use_espn=False, request_delay=0, yahoo_base_url=mock.base_url)
        ns = {"cfg": cfg, "display": print}
        exec(compile(cell("#@title 3. Conectar"), "cell3", "exec"), ns)
        ns["pilot"].now_fn = lambda: FRIDAY                   # before week 3's games
        for prefix in ("#@title 3b.", "#@title 4. Alineaci", "#@title 4b.", "#@title 5. Tu rival",
                       "#@title 5b.", "#@title 6. Waivers", "#@title 7. Evaluar"):
            exec(compile(cell(prefix), prefix, "exec"), ns)
    out = capsys.readouterr().out
    assert "Semana 3" in out
    assert "Alineación semana 3" in out
    assert "Parches activos" in out
    assert "Ninguna noticia de pérdida de rol" in out
    assert "Rival FC" in out
    assert "Waivers semana 3" in out
    assert "NEUTRAL" in out
    assert "Posición por posición" in out
    assert not mock.posts  # default mode "sugerir" never writes
