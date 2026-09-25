"""Run the Colab notebook's cells (3-7) against the local Yahoo mock."""

import json
import sys
import types
from pathlib import Path

from fantasy_autopilot.config import Config
from mock_yahoo import LEAGUE, TEAM, MockYahoo

NOTEBOOK = Path(__file__).resolve().parents[1] / "FantasyAutopilot.ipynb"


def code_cells():
    nb = json.loads(NOTEBOOK.read_text())
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


def test_notebook_is_valid_and_has_the_steps():
    cells = code_cells()
    titles = [c.splitlines()[0] for c in cells]
    assert titles[0].startswith("#@title 1. Instalar")
    assert any("Piloto automático del domingo" in t for t in titles)
    for src in cells[1:]:  # everything but the install cell (shell magics) must be valid Python
        compile(src, "cell", "exec")


def test_notebook_cells_run_against_mock(tmp_path, monkeypatch, capsys):
    cookies = json.dumps([{"name": "T", "value": "session", "domain": "127.0.0.1", "path": "/"}])
    colab = types.ModuleType("google.colab")
    colab.userdata = types.SimpleNamespace(get=lambda key: cookies)
    google = types.ModuleType("google")
    google.colab = colab
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.colab", colab)
    cells = code_cells()
    with MockYahoo() as mock:
        cfg = Config(league_id=LEAGUE, team_id=TEAM, state_dir=str(tmp_path), quiet=True,
                     use_sleeper=False, use_espn=False, request_delay=0, yahoo_base_url=mock.base_url)
        ns = {"cfg": cfg, "display": print}
        for src in cells[2:7]:  # 3. connect, 4. lineup, 5. matchup, 6. waivers, 7. trade
            exec(compile(src, "cell", "exec"), ns)
    out = capsys.readouterr().out
    assert "Semana 3" in out
    assert "Alineación semana 3" in out
    assert "Rival FC" in out
    assert "Waivers semana 3" in out
    assert "NEUTRAL" in out
    assert not mock.posts  # default mode "sugerir" never writes
