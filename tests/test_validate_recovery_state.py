from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import json
import math
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.validate_recovery_state import (
    RecoveryValidationError,
    V4_FEATURE_NAMES,
    V4_INITIAL_WEIGHTS,
    V4_L2_PENALTY,
    V4_LEARNING_RATE,
    _sigmoid,
    validate_recovery_state,
)


class RecoveryStateValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.memory = {
            "schema_version": 3,
            "symbol": "XAUTUSD",
            "model": {
                "weights": {
                    "ema_alignment": 0.1,
                    "momentum": 0.1,
                    "ema_spread": 0.1,
                    "candle_strength": 0.1,
                    "trend_efficiency": 0.1,
                    "multi_timeframe_alignment": 0.1,
                    "volume_confirmation": 0.1,
                },
                "samples": 1,
                "wins": 1,
                "losses": 0,
            },
            "evaluations": [],
            "outcomes": [],
            "counterfactual_outcomes": [
                {
                    "evaluation_id": "evaluation-1",
                    "horizon_minutes": 15,
                    "status": "LABELLED",
                    "label": "FAVOURABLE",
                    "execution_allowed": False,
                    "used_for_live_weight_update": False,
                }
            ],
        }
        self.write_json(
            "manifest.json",
            {
                "migration_complete": True,
                "source_repository": "mishraarvind463-del/xaut-runner",
                "source_branch": "xaut-recovery-state",
                "destination_repository": "mishraarvind463-del/xaut-bot",
                "destination_branch": "xaut-recovery-state",
            },
        )
        self.write_json("bot_recovery_state.json", {"product_id": 131253})
        self.write_json("ai_memory.json", self.memory)
        self.write_history()
        self.write_journal([{"event_type": "DECISION_RECORDED", "event_id": "event-1"}])

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_json(self, name, value):
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")

    def read_memory(self):
        return json.loads((self.root / "ai_memory.json").read_text(encoding="utf-8"))

    def save_memory(self, value):
        self.write_json("ai_memory.json", value)

    def write_history(self, fieldnames=None, rows=None):
        fields = fieldnames or [
            "symbol",
            "mode",
            "direction",
            "quantity",
            "entry_time_utc",
            "exit_time_utc",
            "net_pnl",
        ]
        content_rows = rows if rows is not None else [
            {
                "symbol": "XAUTUSD",
                "mode": "PAPER",
                "direction": "BUY",
                "quantity": "1",
                "entry_time_utc": "2026-11-20T00:00:00Z",
                "exit_time_utc": "2026-11-20T00:01:00Z",
                "net_pnl": "1.0",
            }
        ]
        with (self.root / "trade_history.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(content_rows)

    def write_journal(self, events):
        payload = b"".join(json.dumps(event).encode("utf-8") + b"\n" for event in events)
        (self.root / "shadow_trade_journal.jsonl.gz").write_bytes(gzip.compress(payload))

    def assert_invalid(self, message):
        with self.assertRaisesRegex(RecoveryValidationError, message):
            validate_recovery_state(self.root)

    def test_accepts_valid_schema_v3_fixture(self):
        summary = validate_recovery_state(self.root)
        self.assertEqual(summary, {
            "ai_memory_schema_version": 3,
            "trade_history_rows": 1,
            "shadow_journal_events": 1,
        })

    def test_accepts_legacy_schema_v1_and_v2_without_counterfactual_array(self):
        for version in (1, 2):
            with self.subTest(version=version):
                memory = copy.deepcopy(self.memory)
                memory["schema_version"] = version
                memory.pop("counterfactual_outcomes", None)
                self.save_memory(memory)
                self.assertEqual(validate_recovery_state(self.root)["ai_memory_schema_version"], version)


    def git_blob_sha(self, raw):
        return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()

    def write_valid_v4_fixture(self):
        archive = copy.deepcopy(self.memory)
        archive["schema_version"] = 3
        metadata = {
            "source": "PAPER_ESTIMATED",
            "currency": "USD",
            "gross_pnl": 1.3,
            "fees": 0.1,
            "slippage_cost": 0.1,
            "funding": 0.0,
            "funding_assumption": "No funding interval crossed",
            "cost_model": "test cost model",
        }
        features = {name: 0.25 for name in V4_FEATURE_NAMES}
        event = {
            "evaluation_id": "eval-v4-approved",
            "trade_id": "trade-v4-approved",
            "signal": "BUY",
            "would_decision": "APPROVE",
            "entry_gate_decision": "APPROVE",
            "flip_direction": "BUY",
            "proposal_direction": "BUY",
            "outcome_recorded": True,
            "features": features,
            "realized_net_result": 1.1,
            "outcome_label": "WIN",
            "outcome_source": "PAPER_ESTIMATED",
            "outcome_metadata": metadata,
        }
        outcome = {
            "evaluation_id": "eval-v4-approved",
            "trade_id": "trade-v4-approved",
            "net_result": 1.1,
            "label": "WIN",
            "source": "PAPER_ESTIMATED",
            "metadata": metadata,
        }
        archive["evaluations"] = [event]
        archive["outcomes"] = [outcome]
        archive["model"] = {
            "weights": {name: 0.1 for name in V4_FEATURE_NAMES},
            "bias": 0.4,
            "samples": 1,
            "wins": 1,
            "losses": 0,
        }
        archive_bytes = (json.dumps(archive, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        source_sha = self.git_blob_sha(archive_bytes)

        weights = dict(V4_INITIAL_WEIGHTS)
        bias = 0.0
        probability = _sigmoid(bias + sum(weights[name] * features[name] for name in V4_FEATURE_NAMES))
        error = 1.0 - probability
        for name in V4_FEATURE_NAMES:
            old_weight = weights[name]
            weights[name] = max(-8.0, min(8.0, old_weight + V4_LEARNING_RATE * (
                error * features[name] - V4_L2_PENALTY * old_weight
            )))
        bias = max(-8.0, min(8.0, bias + V4_LEARNING_RATE * error))

        memory = copy.deepcopy(archive)
        memory["schema_version"] = 4
        memory["model"] = {
            "weights": weights,
            "bias": bias,
            "samples": 1,
            "wins": 1,
            "losses": 0,
        }
        memory["legacy_outcomes"] = []
        memory["legacy_model_snapshot"] = copy.deepcopy(archive["model"])
        memory["migration"] = {
            "id": "verified-only-v1",
            "from_schema_version": 3,
            "source_blob_sha": source_sha,
            "active_outcome_count": 1,
            "legacy_outcome_count": 0,
        }
        (self.root / "ai_memory_v3_archive.json").write_bytes(archive_bytes)
        self.save_memory(memory)
        return archive, memory, archive_bytes

    def test_accepts_valid_verified_only_schema_v4_fixture(self):
        _, memory, _ = self.write_valid_v4_fixture()
        summary = validate_recovery_state(self.root)
        self.assertEqual(summary["ai_memory_schema_version"], 4)
        self.assertEqual(memory["model"]["samples"], 1)
        self.assertEqual(memory["migration"]["active_outcome_count"], 1)

    def test_rejects_schema_v4_when_original_archive_is_missing_or_changed(self):
        self.write_valid_v4_fixture()
        (self.root / "ai_memory_v3_archive.json").unlink()
        self.assert_invalid("Required recovery file is missing or empty: ai_memory_v3_archive.json")

        self.write_valid_v4_fixture()
        (self.root / "ai_memory_v3_archive.json").write_bytes(b"{}")
        self.assert_invalid("archive Git blob SHA does not match")

    def test_rejects_schema_v4_when_deterministic_model_or_archive_partition_differs(self):
        self.write_valid_v4_fixture()
        memory = self.read_memory()
        memory["model"]["weights"]["ema_alignment"] += 0.01
        self.save_memory(memory)
        self.assert_invalid("weights do not match deterministic verified-only rebuild")

        self.write_valid_v4_fixture()
        memory = self.read_memory()
        memory["outcomes"] = []
        self.save_memory(memory)
        self.assert_invalid("do not exactly partition")

    def test_recovery_preflight_fetches_v3_archive_before_staging_v4_memory(self):
        self.write_valid_v4_fixture()
        result, workspace, sentinel = self._run_staging_script(valid=True)
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("Schema-v4", result.stdout)
        self.assertEqual(
            (workspace / "ai_memory.json").read_bytes(),
            (self.root / "ai_memory.json").read_bytes(),
        )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve this workspace file")

    def test_recovery_preflight_does_not_stage_v4_when_archive_hash_is_wrong(self):
        self.write_valid_v4_fixture()
        (self.root / "ai_memory_v3_archive.json").write_bytes(b"not the original archive")
        result, workspace, sentinel = self._run_staging_script(valid=True)
        self.assertNotEqual(result.returncode, 0, msg=result.stdout)
        self.assertIn("archive Git blob SHA does not match", result.stderr)
        self.assertFalse((workspace / "ai_memory.json").exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve this workspace file")



    def test_verified_only_v4_accepts_new_verified_outcomes_after_migration(self):
        self.write_valid_v4_fixture()
        memory = self.read_memory()
        features = {name: 0.25 for name in V4_FEATURE_NAMES}
        metadata = {
            "source": "PAPER_ESTIMATED",
            "currency": "USD",
            "gross_pnl": 0.7,
            "fees": 0.1,
            "slippage_cost": 0.1,
            "funding": 0.0,
            "funding_assumption": "No funding interval crossed",
            "cost_model": "test cost model",
        }
        event = {
            "evaluation_id": "eval-v4-new",
            "trade_id": "trade-v4-new",
            "signal": "SELL",
            "would_decision": "APPROVE",
            "entry_gate_decision": "APPROVE",
            "flip_direction": "SELL",
            "proposal_direction": "SELL",
            "outcome_recorded": True,
            "features": features,
            "realized_net_result": 0.5,
            "outcome_label": "WIN",
            "outcome_source": "PAPER_ESTIMATED",
            "outcome_metadata": metadata,
        }
        outcome = {
            "evaluation_id": "eval-v4-new",
            "trade_id": "trade-v4-new",
            "net_result": 0.5,
            "label": "WIN",
            "source": "PAPER_ESTIMATED",
            "metadata": metadata,
        }
        weights = dict(memory["model"]["weights"])
        bias = memory["model"]["bias"]
        probability = _sigmoid(bias + sum(weights[name] * features[name] for name in V4_FEATURE_NAMES))
        error = 1.0 - probability
        for name in V4_FEATURE_NAMES:
            old_weight = weights[name]
            weights[name] = max(-8.0, min(8.0, old_weight + V4_LEARNING_RATE * (
                error * features[name] - V4_L2_PENALTY * old_weight
            )))
        memory["model"]["weights"] = weights
        memory["model"]["bias"] = max(-8.0, min(8.0, bias + V4_LEARNING_RATE * error))
        memory["model"]["samples"] = 2
        memory["model"]["wins"] = 2
        memory["evaluations"].append(event)
        memory["outcomes"].append(outcome)
        memory["migration"]["active_outcome_count"] = 2
        self.save_memory(memory)

        summary = validate_recovery_state(self.root)
        self.assertEqual(summary["ai_memory_schema_version"], 4)
        self.assertEqual(self.read_memory()["model"]["samples"], 2)

    def test_rejects_incomplete_or_mismatched_manifest(self):
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        manifest["destination_branch"] = "main"
        self.write_json("manifest.json", manifest)
        self.assert_invalid("manifest mismatch: destination_branch")

    def test_rejects_manifest_not_marked_complete(self):
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        manifest["migration_complete"] = False
        self.write_json("manifest.json", manifest)
        self.assert_invalid("not marked complete")

    def test_rejects_wrong_recovery_product(self):
        self.write_json("bot_recovery_state.json", {"product_id": 999})
        self.assert_invalid("not a valid XAUTUSD state object")

    def test_rejects_unsupported_ai_schema(self):
        memory = copy.deepcopy(self.memory)
        memory["schema_version"] = 5
        self.save_memory(memory)
        self.assert_invalid("not a supported XAUTUSD memory object")

    def test_rejects_duplicate_counterfactual_evaluation_horizon(self):
        memory = copy.deepcopy(self.memory)
        memory["counterfactual_outcomes"].append(copy.deepcopy(memory["counterfactual_outcomes"][0]))
        self.save_memory(memory)
        self.assert_invalid("duplicate evaluation/horizon")

    def test_rejects_invalid_counterfactual_label_status(self):
        memory = copy.deepcopy(self.memory)
        memory["counterfactual_outcomes"][0]["status"] = "SKIPPED_GAPPED_FUTURE_PATH"
        memory["counterfactual_outcomes"][0]["label"] = "FAVOURABLE"
        self.save_memory(memory)
        self.assert_invalid("inconsistent label/status")

    def test_rejects_counterfactual_marked_for_execution(self):
        memory = copy.deepcopy(self.memory)
        memory["counterfactual_outcomes"][0]["execution_allowed"] = True
        self.save_memory(memory)
        self.assert_invalid("not marked research-only")

    def test_rejects_missing_required_model_weight(self):
        memory = copy.deepcopy(self.memory)
        del memory["model"]["weights"]["volume_confirmation"]
        self.save_memory(memory)
        self.assert_invalid("missing required model weights")

    def test_rejects_unbalanced_model_sample_counters(self):
        memory = copy.deepcopy(self.memory)
        memory["model"]["samples"] = 2
        self.save_memory(memory)
        self.assert_invalid("sample counters do not reconcile")

    def test_rejects_non_integer_model_sample_counters(self):
        memory = copy.deepcopy(self.memory)
        memory["model"]["wins"] = True
        self.save_memory(memory)
        self.assert_invalid("sample counters are invalid")

    def test_rejects_missing_trade_history_columns(self):
        self.write_history(fieldnames=["symbol", "mode"], rows=[])
        self.assert_invalid("missing required columns")

    def test_rejects_empty_trade_history(self):
        self.write_history(rows=[])
        self.assert_invalid("has no historical rows")

    def test_rejects_corrupt_shadow_journal_gzip(self):
        (self.root / "shadow_trade_journal.jsonl.gz").write_bytes(b"not a gzip file")
        self.assert_invalid("gzip is invalid")

    def test_rejects_invalid_shadow_journal_jsonl_event(self):
        self.write_journal([{"event_type": 7}])
        self.assert_invalid("event 1 is invalid")

    def test_rejects_missing_required_snapshot_file(self):
        (self.root / "ai_memory.json").unlink()
        self.assert_invalid("ai_memory.json")


    def test_trading_workflow_uses_tested_preflight_before_staging_memory(self):
        workflow_path = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "setup-a.yml"
        )
        workflow = workflow_path.read_text(encoding="utf-8")

        prepare_script_path = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "prepare_private_recovery.sh"
        )
        prepare_script = prepare_script_path.read_text(encoding="utf-8")
        self.assertIn("run: bash scripts/prepare_private_recovery.sh", workflow)
        validator_call = 'python "$SCRIPT_DIR/validate_recovery_state.py" "$TMP_DIR"'
        stage_call = 'cp "$TMP_DIR/ai_memory.json" "$BOT_WORKSPACE/ai_memory.json"'
        self.assertIn(validator_call, prepare_script)
        self.assertLess(prepare_script.index(validator_call), prepare_script.index(stage_call))
        self.assertIn("PRIVATE_REPO: mishraarvind463-del/xaut-bot", workflow)
        self.assertIn("PRIVATE_BRANCH: xaut-recovery-state", workflow)
        self.assertIn("BOT_WORKSPACE: private-bot", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("default: false", workflow)
        self.assertIn("ref: ai-managed-trading-v1", workflow)
        self.assertIn("PRIVATE_BRANCH: xaut-recovery-state", workflow)
        self.assertIn("Wait for Delta IP Whitelisting", workflow)
        self.assertIn("sleep 90", workflow)
        self.assertIn("ENABLE_LIVE_TRADING: ${{ inputs.enable_live_trading && 'true' || 'false' }}", workflow)
        self.assertIn("BOT_RUNTIME_MINUTES: ${{ inputs.runtime_minutes }}", workflow)
        self.assertIn("run: python -u bot.py", workflow)
        self.assertNotIn("<<'PY'", workflow)


    def _run_staging_script(self, *, valid=True):
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "prepare_private_recovery.sh"
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        fake_gh = fake_bin / "gh"
        fake_gh.write_text(
            "#!/usr/bin/env python3\n"
            "import base64, os, sys\n"
            "args = sys.argv[1:]\n"
            "if len(args) < 2 or args[0] != 'api': sys.exit(2)\n"
            "endpoint = args[1]\n"
            "if '/branches/' in endpoint: print('{}'); sys.exit(0)\n"
            "remote = endpoint.split('/contents/', 1)[1].split('?ref=', 1)[0]\n"
            "names = {"
            "'runtime-state/PRIVATE_STORAGE_MIGRATION.json': 'manifest.json', "
            "'runtime-state/bot_recovery_state.json': 'bot_recovery_state.json', "
            "'runtime-state/trade_history.csv': 'trade_history.csv', "
            "'runtime-state/ai_memory.json': 'ai_memory.json', 'runtime-state/ai_memory_v3_archive.json': 'ai_memory_v3_archive.json', "
            "'runtime-state/shadow_trade_journal.jsonl.gz': 'shadow_trade_journal.jsonl.gz'}\n"
            "name = names.get(remote)\n"
            "if name is None: sys.exit(3)\n"
            "path = os.path.join(os.environ['FAKE_GH_FIXTURE_ROOT'], name)\n"
            "with open(path, 'rb') as handle: print(base64.b64encode(handle.read()).decode('ascii'))\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)

        if not valid:
            manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
            manifest["migration_complete"] = False
            self.write_json("manifest.json", manifest)

        workspace = self.root / "private-bot"
        workspace.mkdir(exist_ok=True)
        sentinel = workspace / "do-not-touch.txt"
        sentinel.write_text("preserve this workspace file", encoding="utf-8")

        env = os.environ.copy()
        env.update({
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "GH_TOKEN": "offline-fixture-token",
            "PRIVATE_REPO": "fixture/private-bot",
            "PRIVATE_BRANCH": "xaut-recovery-state",
            "BOT_WORKSPACE": str(workspace),
            "FAKE_GH_FIXTURE_ROOT": str(self.root),
        })
        result = subprocess.run(
            ["bash", str(script)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result, workspace, sentinel

    def test_prepare_script_fetches_validates_and_stages_exact_memory_bytes(self):
        result, workspace, sentinel = self._run_staging_script(valid=True)
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("PASS: private recovery manifest", result.stdout)
        self.assertIn("Verified private AI memory staged", result.stdout)
        self.assertEqual(
            (workspace / "ai_memory.json").read_bytes(),
            (self.root / "ai_memory.json").read_bytes(),
        )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve this workspace file")
        self.assertEqual(
            {item.name for item in workspace.iterdir()},
            {"do-not-touch.txt", "ai_memory.json"},
        )

    def test_prepare_script_fails_closed_before_staging_invalid_snapshot(self):
        result, workspace, sentinel = self._run_staging_script(valid=False)
        self.assertNotEqual(result.returncode, 0, msg=result.stdout)
        self.assertIn("FAIL: Private recovery manifest is not marked complete.", result.stderr)
        self.assertFalse((workspace / "ai_memory.json").exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve this workspace file")


if __name__ == "__main__":
    unittest.main()
