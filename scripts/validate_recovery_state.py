#!/usr/bin/env python3
"""Offline validator for the private XAUT recovery snapshot.

This module intentionally performs no network access and never modifies input
files. The trading workflow downloads a snapshot into a temporary directory,
then calls this validator before staging the AI memory into the bot checkout.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from pathlib import Path
from typing import Any


class RecoveryValidationError(ValueError):
    """Raised when a recovery snapshot fails a preflight integrity check."""


EXPECTED_MANIFEST = {
    "source_repository": "mishraarvind463-del/xaut-runner",
    "source_branch": "xaut-recovery-state",
    "destination_repository": "mishraarvind463-del/xaut-bot",
    "destination_branch": "xaut-recovery-state",
}

BASE_WEIGHTS = {
    "ema_alignment",
    "momentum",
    "ema_spread",
    "candle_strength",
    "trend_efficiency",
}
SCHEMA_V2_WEIGHTS = {"multi_timeframe_alignment", "volume_confirmation"}
COUNTERFACTUAL_STATUSES = {
    "LABELLED",
    "SKIPPED_MISSING_TARGET_CANDLE",
    "SKIPPED_GAPPED_FUTURE_PATH",
}
COUNTERFACTUAL_LABELS = {"FAVOURABLE", "ADVERSE", "NEUTRAL"}
TRADE_HISTORY_COLUMNS = {
    "symbol",
    "mode",
    "direction",
    "quantity",
    "entry_time_utc",
    "exit_time_utc",
    "net_pnl",
}


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryValidationError(f"{label} is missing or invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RecoveryValidationError(f"{label} must contain a JSON object.")
    return value


def _require_file(root: Path, name: str) -> Path:
    path = root / name
    if not path.is_file() or path.stat().st_size == 0:
        raise RecoveryValidationError(f"Required recovery file is missing or empty: {name}")
    return path


def validate_recovery_state(root: Path) -> dict[str, int]:
    """Validate a downloaded recovery snapshot without changing it."""
    root = Path(root)
    manifest = _load_json(_require_file(root, "manifest.json"), "Private recovery manifest")
    if manifest.get("migration_complete") is not True:
        raise RecoveryValidationError("Private recovery manifest is not marked complete.")
    for key, expected in EXPECTED_MANIFEST.items():
        if manifest.get(key) != expected:
            raise RecoveryValidationError(f"Private recovery manifest mismatch: {key}")

    recovery = _load_json(
        _require_file(root, "bot_recovery_state.json"),
        "Private recovery state",
    )
    if recovery.get("product_id") != 131253:
        raise RecoveryValidationError("Private recovery state is not a valid XAUTUSD state object.")

    memory = _load_json(_require_file(root, "ai_memory.json"), "Private AI memory")
    schema_version = memory.get("schema_version")
    if schema_version not in (1, 2, 3) or memory.get("symbol") != "XAUTUSD":
        raise RecoveryValidationError("Private AI memory is not a supported XAUTUSD memory object.")
    model = memory.get("model")
    if not isinstance(model, dict) or not isinstance(model.get("weights"), dict):
        raise RecoveryValidationError("Private AI memory model/weights are missing.")
    if not isinstance(memory.get("evaluations"), list) or not isinstance(memory.get("outcomes"), list):
        raise RecoveryValidationError("Private AI memory evaluation/outcome arrays are invalid.")

    if schema_version == 3:
        counterfactual_outcomes = memory.get("counterfactual_outcomes")
        if not isinstance(counterfactual_outcomes, list):
            raise RecoveryValidationError("Private AI memory counterfactual outcome array is invalid.")
        seen_counterfactual: set[tuple[str, int]] = set()
        for index, item in enumerate(counterfactual_outcomes):
            if not isinstance(item, dict):
                raise RecoveryValidationError(
                    f"Private AI counterfactual outcome {index} is not an object."
                )
            evaluation_id = item.get("evaluation_id")
            horizon = item.get("horizon_minutes")
            status = item.get("status")
            label = item.get("label")
            if not isinstance(evaluation_id, str) or not evaluation_id or horizon not in (15, 30, 60):
                raise RecoveryValidationError(
                    f"Private AI counterfactual outcome {index} has invalid identity/horizon."
                )
            key = (evaluation_id, horizon)
            if key in seen_counterfactual:
                raise RecoveryValidationError(
                    "Private AI counterfactual outcomes contain a duplicate evaluation/horizon."
                )
            seen_counterfactual.add(key)
            if status not in COUNTERFACTUAL_STATUSES:
                raise RecoveryValidationError(
                    f"Private AI counterfactual outcome {index} has an unsupported status."
                )
            if (
                status == "LABELLED" and label not in COUNTERFACTUAL_LABELS
            ) or (
                status != "LABELLED" and label is not None
            ):
                raise RecoveryValidationError(
                    f"Private AI counterfactual outcome {index} has an inconsistent label/status."
                )
            if item.get("execution_allowed") is not False or item.get("used_for_live_weight_update") is not False:
                raise RecoveryValidationError(
                    f"Private AI counterfactual outcome {index} is not marked research-only."
                )

    required_weights = set(BASE_WEIGHTS)
    if schema_version in (2, 3):
        required_weights.update(SCHEMA_V2_WEIGHTS)
    if not required_weights.issubset(model["weights"]):
        raise RecoveryValidationError("Private AI memory is missing required model weights.")

    counters = (model.get("samples", 0), model.get("wins", 0), model.get("losses", 0))
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counters):
        raise RecoveryValidationError("Private AI memory sample counters are invalid.")
    samples, wins, losses = counters
    if samples != wins + losses:
        raise RecoveryValidationError("Private AI memory sample counters do not reconcile.")

    history_path = _require_file(root, "trade_history.csv")
    try:
        with history_path.open("r", newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or not TRADE_HISTORY_COLUMNS.issubset(set(reader.fieldnames)):
                raise RecoveryValidationError("Private trade history CSV is missing required columns.")
            trade_rows = sum(1 for _ in reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise RecoveryValidationError(f"Private trade history CSV is invalid: {exc}") from exc
    if trade_rows < 1:
        raise RecoveryValidationError("Private trade history CSV has no historical rows.")

    journal_path = _require_file(root, "shadow_trade_journal.jsonl.gz")
    try:
        journal_bytes = gzip.decompress(journal_path.read_bytes())
    except (OSError, EOFError) as exc:
        raise RecoveryValidationError(f"Private shadow journal gzip is invalid: {exc}") from exc

    journal_events = 0
    for line_number, line in enumerate(journal_bytes.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryValidationError(
                f"Shadow journal event {line_number} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(event, dict) or not isinstance(event.get("event_type"), str):
            raise RecoveryValidationError(f"Shadow journal event {line_number} is invalid.")
        journal_events += 1

    return {
        "ai_memory_schema_version": schema_version,
        "trade_history_rows": trade_rows,
        "shadow_journal_events": journal_events,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Temporary directory containing the five downloaded recovery files.")
    args = parser.parse_args(argv)
    try:
        summary = validate_recovery_state(args.root)
    except RecoveryValidationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print("PASS: private recovery manifest and all state files validated.")
    print(
        "PASS: private AI memory validated; schema:",
        summary["ai_memory_schema_version"],
        "trade-history rows:",
        summary["trade_history_rows"],
    )
    print(
        "PASS: shadow journal gzip validated; event rows:",
        summary["shadow_journal_events"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
