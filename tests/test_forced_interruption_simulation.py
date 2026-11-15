#!/usr/bin/env python3
"""Isolated SIGKILL simulation for XAUTUSD Paper checkpoint safety.

This test never starts the trading loop and never calls Delta Exchange.
It creates a valid source checkpoint in a temporary directory, restores a
copy into a simulated run workspace, invalidates the restored manifest, then
SIGKILLs a child after it writes a changed Paper state but before it can write
an end-of-run manifest. It verifies that the incomplete workspace cannot be
accepted as a checkpoint and that the prior verified source remains usable.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


def import_bot(bot_path: Path):
    spec = importlib.util.spec_from_file_location("xaut_bot_forced_stop_test", bot_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load bot source: {bot_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def verify_manifest(directory: Path) -> dict:
    manifest_path = directory / "state_manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RuntimeError("missing or invalid state_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest.get("files")
    if not isinstance(records, dict) or "paper_state.json" not in records:
        raise RuntimeError("manifest missing paper_state.json")
    for name, record in records.items():
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"missing/non-regular checkpoint file: {name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != record.get("sha256"):
            raise RuntimeError(f"SHA-256 mismatch: {name}")
        if path.stat().st_size != int(record.get("size_bytes", -1)):
            raise RuntimeError(f"file-size mismatch: {name}")
    return manifest


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python tests/test_forced_interruption_simulation.py <private-bot/bot.py>", file=sys.stderr)
        return 2

    bot_path = Path(sys.argv[1]).resolve()
    if not bot_path.is_file():
        print(f"Bot source not found: {bot_path}", file=sys.stderr)
        return 2

    old_cwd = Path.cwd()
    try:
        with tempfile.TemporaryDirectory(prefix="xaut-forced-stop-test-") as temp_name:
            root = Path(temp_name)
            source_artifact = root / "verified-source-artifact"
            run_workspace = root / "simulated-run-workspace"
            retry_workspace = root / "simulated-retry-workspace"
            source_artifact.mkdir()
            run_workspace.mkdir()
            retry_workspace.mkdir()

            bot = import_bot(bot_path)
            bot.PAPER_MODE = True
            bot.PAPER_STATE_FILE = "paper_state.json"
            bot.TRADE_LOG_FILE = "trade_history.csv"
            bot.ALERT_STATE_FILE = "alert_state.json"
            bot.LEGACY_TRADE_LOG_FILE = "trade_history_legacy.csv"
            bot.EXECUTION_JOURNAL_FILE = "execution_journal.json"
            bot.STATE_MANIFEST_FILE = "state_manifest.json"

            # Establish a known-good source checkpoint with an open Paper BUY.
            os.chdir(run_workspace)
            source_state = {
                "position": 1,
                "entry_price": 3800.0,
                "entry_time": "2026-11-16T00:00:00+00:00",
                "entry_side": "buy",
                "realized_points": 0.0,
                "pending_csv_trade": None,
            }
            if not bot.save_paper_state(source_state):
                raise RuntimeError("could not create baseline Paper state")
            bot.write_state_manifest()
            verify_manifest(run_workspace)
            for name in ("paper_state.json", "state_manifest.json"):
                shutil.copy2(run_workspace / name, source_artifact / name)
            original_digest = hashlib.sha256((source_artifact / "paper_state.json").read_bytes()).hexdigest()
            print("PASS: baseline verified checkpoint created (open Paper BUY preserved).")

            # Restore the artifact into a fresh run workspace, then invalidate
            # its old manifest exactly as Setup A does before running the bot.
            for name in ("paper_state.json", "state_manifest.json"):
                shutil.copy2(source_artifact / name, run_workspace / name)
            bot.invalidate_previous_state_manifest()
            if (run_workspace / "state_manifest.json").exists():
                raise RuntimeError("restored manifest was not invalidated")
            print("PASS: previous-run manifest invalidated before simulated run.")

            # Child changes the Paper position and is then hard-killed before
            # it can complete shutdown/write_state_manifest(). This is isolated
            # from real trading and does not run the strategy or call APIs.
            child_code = r'''\
import importlib.util, os, signal, sys
from pathlib import Path
bot_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("xaut_bot_forced_stop_child", bot_path)
bot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bot
spec.loader.exec_module(bot)
bot.PAPER_MODE = True
bot.PAPER_STATE_FILE = "paper_state.json"
bot.TRADE_LOG_FILE = "trade_history.csv"
bot.ALERT_STATE_FILE = "alert_state.json"
bot.LEGACY_TRADE_LOG_FILE = "trade_history_legacy.csv"
bot.EXECUTION_JOURNAL_FILE = "execution_journal.json"
bot.STATE_MANIFEST_FILE = "state_manifest.json"
changed = {
    "position": -1,
    "entry_price": 3810.0,
    "entry_time": "2026-11-16T00:01:00+00:00",
    "entry_side": "sell",
    "realized_points": 0.0,
    "pending_csv_trade": None,
}
if not bot.save_paper_state(changed):
    raise SystemExit("could not persist simulated in-progress state")
os.kill(os.getpid(), signal.SIGKILL)
'''
            env = os.environ.copy()
            env.update({"ENABLE_LIVE_TRADING": "false", "API_KEY": "", "API_SECRET": ""})
            child = subprocess.run(
                [sys.executable, "-c", child_code, str(bot_path)],
                cwd=run_workspace,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if child.returncode != -signal.SIGKILL:
                raise RuntimeError(
                    f"child did not end via SIGKILL (return={child.returncode}); "
                    f"stdout={child.stdout!r}; stderr={child.stderr!r}"
                )
            print("PASS: child process was forcibly terminated after state write.")

            # The state file changed, but there must be no manifest for this run.
            changed_state = json.loads((run_workspace / "paper_state.json").read_text(encoding="utf-8"))
            if changed_state.get("state", {}).get("position") != -1:
                raise RuntimeError("simulated interrupted state was not written as expected")
            try:
                verify_manifest(run_workspace)
            except Exception as error:
                if "missing or invalid state_manifest.json" not in str(error):
                    raise
                print("PASS: incomplete run rejected; no valid new checkpoint manifest exists.")
            else:
                raise RuntimeError("incomplete run was incorrectly accepted as a checkpoint")

            # A later retry must use the previous verified artifact, not the
            # unmanifested files left in the interrupted workspace.
            for name in ("paper_state.json", "state_manifest.json"):
                shutil.copy2(source_artifact / name, retry_workspace / name)
            verify_manifest(retry_workspace)
            restored = json.loads((retry_workspace / "paper_state.json").read_text(encoding="utf-8"))
            restored_position = restored.get("state", {}).get("position")
            retry_digest = hashlib.sha256((retry_workspace / "paper_state.json").read_bytes()).hexdigest()
            if restored_position != 1 or retry_digest != original_digest:
                raise RuntimeError("prior verified checkpoint did not restore byte-for-byte")
            print("PASS: retry restored the previous verified Paper BUY checkpoint byte-for-byte.")
            print("FORCED-INTERRUPTION SIMULATION: ALL CHECKS PASSED (isolated; no Delta API calls).")
            return 0
    finally:
        os.chdir(old_cwd)


if __name__ == "__main__":
    raise SystemExit(main())

