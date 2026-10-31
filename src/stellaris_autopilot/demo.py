"""Deterministic integration demonstration; explicitly NOT a game connection."""
from __future__ import annotations

import tempfile
from pathlib import Path

from .config import Action, Config
from .engine import Engine, Proposal
from .journal import Journal
from .observe import SaveObserver


def run_demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="saa-demo-") as tmp:
        directory = Path(tmp)
        save = directory / "replay.txt"
        template = '''version="DEMO-ONLY" date="2200.01.01" ironman=no multiplayer=no
galaxy={{name="saa-demo-campaign"}}
player={{name="Player" country=0}}
country={{0={{name="Demonstration" at_war=no modules={{standard_economy_module={{resources={{alloys=120}}}}}}
research={{physics="{tech}"}}}}}}
'''
        save.write_text(template.format(tech="none"), encoding="utf-8")

        class ReplayDriver:
            def execute(self, steps):
                save.write_text(template.format(tech="tech_physics_1"), encoding="utf-8")

        cfg = Config(save_path=save, state_dir=directory, verified_game_version="DEMO-ONLY",
                     campaign_id="saa-demo-campaign",
                     verification_timeout_seconds=1, poll_seconds=0.01, resolution=(1920, 1080),
                     actions={"select_physics": Action("select_physics", "Replay research selection", "research",
                         steps=[{"kind": "key", "key": "f4"}],
                         expected=[{"path": "research.physics", "op": "eq", "value": "tech_physics_1"}])},
                     allowed_actions=["select_physics"])
        journal = Journal(directory / "journal.sqlite")
        try:
            observer = SaveObserver(save)
            before = observer.read()
            result = Engine(cfg, journal, observer, ReplayDriver()).run(
                Proposal("select_physics", "Demo replay adapter"), before, live=True)
            return {"mode": "SIMULATION — no game input", "before": before.state["research"],
                    "after": observer.read().state["research"], "result": result,
                    "journal": [{"action": r["action"], "status": r["status"]} for r in journal.records()]}
        finally:
            journal.close()
