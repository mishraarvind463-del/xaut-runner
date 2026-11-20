from __future__ import annotations

import copy
import csv
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from scripts.validate_recovery_state import RecoveryValidationError, validate_recovery_state


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
        memory["schema_version"] = 4
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

        validator_call = 'python scripts/validate_recovery_state.py "$TMP_DIR"'
        stage_call = 'cp "$TMP_DIR/ai_memory.json" private-bot/ai_memory.json'
        self.assertIn(validator_call, workflow)
        self.assertLess(workflow.index(validator_call), workflow.index(stage_call))
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("default: false", workflow)
        self.assertIn("ref: ai-managed-trading-v1", workflow)
        self.assertIn('PRIVATE_BRANCH="xaut-recovery-state"', workflow)
        self.assertIn("Wait for Delta IP Whitelisting", workflow)
        self.assertIn("sleep 90", workflow)
        self.assertIn("ENABLE_LIVE_TRADING: ${{ inputs.enable_live_trading && 'true' || 'false' }}", workflow)
        self.assertIn("BOT_RUNTIME_MINUTES: ${{ inputs.runtime_minutes }}", workflow)
        self.assertIn("run: python -u bot.py", workflow)
        self.assertNotIn("<<'PY'", workflow)


if __name__ == "__main__":
    unittest.main()
