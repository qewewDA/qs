"""Command-line application. Live dispatch is an explicit Windows-only mode."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from .config import load_config
from .demo import run_demo
from .engine import Engine, game_date, validate_live
from .journal import Journal
from .observe import LogTail, SaveObserver
from .planner import LLMPlanner, RulePlanner, public_state
from .saves import SaveError, read_save


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def install_mod(mod_dir: Path):
    source = Path(__file__).resolve().parents[2] / "mod" / "stellaris_ai_autopilot"
    if not source.exists():
        source = Path(sys.prefix) / "share" / "stellaris-ai-autopilot" / "mod"
    if not source.is_dir():
        raise ValueError("Bundled Mod data missing; install from a complete wheel or source checkout")
    destination = mod_dir.expanduser().resolve() / "stellaris_ai_autopilot"
    launcher = destination.parent / "stellaris_ai_autopilot.mod"
    if destination.exists() or destination.is_symlink() or launcher.exists() or launcher.is_symlink():
        raise ValueError("Mod already exists; preserve your changes and remove it before reinstalling")
    if '"' in str(destination) or "\n" in str(destination):
        raise ValueError("Unsupported quote or newline in Mod path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    with launcher.open("x", encoding="utf-8") as stream:
        stream.write('name="Stellaris AI Autopilot Bridge"\nversion="0.1.0"\n'
                     f'path="{destination.as_posix()}"\ntags={{ "Utilities" }}\n')
    emit({"installed": str(destination), "launcher": str(launcher),
          "next": "Enable in Paradox Launcher; test saa.1 and inspect error.log before enabling actions"})


def run_loop(args):
    cfg = load_config(args.config)
    if args.years <= 0 or args.years > 1000:
        raise ValueError("years must be in 1..1000")
    if not 0 <= args.arm_delay <= 30:
        raise ValueError("arm-delay must be 0..30 seconds")
    observer = SaveObserver(cfg.save_path, cfg.country_id)
    planner = LLMPlanner() if args.planner == "llm" else RulePlanner()
    driver = None
    if args.live:
        from .windows import WindowsDriver
        validate_live(cfg, observer.read())
        if not cfg.resolution:
            raise ValueError("Set resolution from the calibrate command before live execution")
        driver = WindowsDriver(expected_resolution=cfg.resolution, stop_file=cfg.state_dir / "STOP")
        emit({"status": "arming", "delay_seconds": args.arm_delay,
              "instruction": "Focus Stellaris now; input still requires locally authorized actions"})
        time.sleep(args.arm_delay)
    log = LogTail(cfg.log_path) if cfg.log_path else None
    end_date = None
    last_digest = None
    campaign = None
    read_failures = 0
    exit_code = 0
    journal = Journal(cfg.state_dir / "journal.sqlite")
    engine = Engine(cfg, journal, observer, driver)
    try:
        while True:
            if (cfg.state_dir / "STOP").exists():
                emit({"status": "stopped", "reason": "STOP file exists"})
                break
            try:
                observation = observer.read()
                read_failures = 0
            except (SaveError, OSError) as exc:
                read_failures += 1
                if not args.continuous or read_failures >= 3:
                    raise
                emit({"status": "waiting_for_save", "reason": str(exc)})
                time.sleep(cfg.poll_seconds)
                continue
            meta = observation.state.get("metadata", {})
            identity = (observation.state["country_id"], meta.get("campaign_id"), meta.get("game_version"))
            if campaign is None:
                campaign = identity
            elif campaign != identity:
                raise ValueError("Campaign, player or game version changed; stop and inspect before restarting")
            current_date = game_date(observation.state["date"])
            if end_date is None:
                end_date = (current_date[0] + args.years, current_date[1], current_date[2])
            if current_date >= end_date:
                emit({"status": "completed", "reason": "Requested in-game year horizon reached"})
                break
            if log:
                for line in log.read():
                    emit({"log_observation": line, "note": "Telemetry only; not action verification"})
            if observation.digest != last_digest:
                proposal = planner.propose(args.goal, observation.state, cfg, journal.recent_records(limit=100))
                result = engine.run(proposal, observation, live=args.live)
                emit({"date": observation.state["date"], "mode": "live" if args.live else "dry_run", **result})
                cfg.state_dir.mkdir(parents=True, exist_ok=True)
                with (cfg.state_dir / "decisions.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"save": observation.digest, "proposal": proposal.__dict__,
                                             "result": result}, ensure_ascii=False) + "\n")
                last_digest = observation.digest
                retry_snapshot = args.continuous and result.get("reason") == "Snapshot changed while planning; replan"
                if result["status"] in {"unverified", "blocked", "stopped"} and not retry_snapshot:
                    if result["status"] != "stopped":
                        exit_code = 2
                    break
            if not args.continuous:
                break
            time.sleep(cfg.poll_seconds)
    finally:
        journal.close()
    return exit_code


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="stellaris-autopilot", description="Single-player Stellaris AI Autopilot")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="Run an offline integration simulation; sends no game input")
    inspect = commands.add_parser("inspect", help="Read an existing .sav or text save")
    inspect.add_argument("save", type=Path)
    inspect.add_argument("--country-id")
    install = commands.add_parser("install-mod", help="Install bundled Mod without overwriting existing files")
    install.add_argument("--mod-dir", type=Path, required=True)
    calibration = commands.add_parser("calibrate", help="Print client resolution and normalized pointer position")
    calibration.add_argument("--delay", type=float, default=5)
    run = commands.add_parser("run", help="Plan and execute locally authorized operations")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--goal", required=True)
    run.add_argument("--planner", choices=["rules", "llm"], default="rules")
    run.add_argument("--live", action="store_true", help="Send real game input (Windows only)")
    run.add_argument("--continuous", action="store_true", help="Observe new saves until stopped or horizon reached")
    run.add_argument("--years", type=int, default=10)
    run.add_argument("--arm-delay", type=float, default=5, help="Seconds to focus the game before live dispatch")
    for name in ("stop", "journal", "reconcile", "mcp", "doctor"):
        child = commands.add_parser(name)
        child.add_argument("--config", type=Path, required=True)
        if name == "reconcile":
            child.add_argument("--id", type=int, required=True)
            child.add_argument("--note", required=True)
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            emit(run_demo())
        elif args.command == "inspect":
            emit(read_save(args.save, country_id=args.country_id))
        elif args.command == "install-mod":
            install_mod(args.mod_dir)
        elif args.command == "calibrate":
            if not 0 <= args.delay <= 30:
                raise ValueError("Calibration delay must be 0..30 seconds")
            print("Focus Stellaris and place pointer on the desired control...", flush=True)
            time.sleep(args.delay)
            from .windows import WindowsDriver
            emit(WindowsDriver().client_position())
        elif args.command == "run":
            return run_loop(args)
        else:
            cfg = load_config(args.config)
            if args.command == "stop":
                cfg.state_dir.mkdir(parents=True, exist_ok=True)
                (cfg.state_dir / "STOP").touch()
                emit({"status": "stop_requested"})
            elif args.command == "mcp":
                try:
                    from .mcp_server import build_server
                except ImportError:
                    raise ValueError('Install MCP integration with: pip install ".[mcp]"') from None
                build_server(args.config).run(transport="stdio")
            elif args.command == "doctor":
                observation = SaveObserver(cfg.save_path, cfg.country_id).read()
                ready, reason = True, "Save policy checks passed (not a game compatibility test)"
                try:
                    validate_live(cfg, observation)
                except ValueError as exc:
                    ready, reason = False, str(exc)
                emit({"platform": sys.platform, "save_policy_ready": ready, "reason": reason,
                      "game_metadata": public_state(observation.state)["metadata"],
                      "allowed_actions": cfg.allowed_actions, "resolution": cfg.resolution,
                      "llm_key_present": bool(__import__("os").environ.get("AUTOPILOT_LLM_API_KEY"))})
                return 0 if ready else 2
            else:
                journal = Journal(cfg.state_dir / "journal.sqlite")
                try:
                    if args.command == "reconcile":
                        journal.reconcile(args.id, args.note)
                    emit([{k: v for k, v in r.items() if k != "before_json"} for r in journal.records()])
                finally:
                    journal.close()
        return 0
    except KeyboardInterrupt:
        print("Stopped. Inspect the journal before resuming interrupted live operations.", file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, SaveError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
