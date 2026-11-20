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
import hashlib
import json
import math
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



V4_FEATURE_NAMES = (
    "ema_alignment",
    "momentum",
    "ema_spread",
    "candle_strength",
    "trend_efficiency",
    "multi_timeframe_alignment",
    "volume_confirmation",
)
V4_INITIAL_WEIGHTS = {
    "ema_alignment": 1.10,
    "momentum": 0.90,
    "ema_spread": 0.80,
    "candle_strength": 0.40,
    "trend_efficiency": 0.80,
    "multi_timeframe_alignment": 0.75,
    "volume_confirmation": 0.45,
}
V4_LEARNING_RATE = 0.025
V4_L2_PENALTY = 0.0005
V4_LEGACY_REASONS = {
    "EVALUATION_PRUNED",
    "LEGACY_QUALITY_BLOCK",
    "LEGACY_GATE_PROOF_MISSING",
    "LEGACY_OUTCOME_EVIDENCE_INVALID",
}


def _finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RecoveryValidationError(f"{label} is not numeric.") from exc
    if not math.isfinite(number):
        raise RecoveryValidationError(f"{label} is not finite.")
    return number


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(value, -60.0))
    return z / (1.0 + z)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_v4_financial_outcome(outcome: dict[str, Any], event: dict[str, Any]) -> float:
    metadata = outcome.get("metadata")
    if not isinstance(metadata, dict) or metadata != event.get("outcome_metadata"):
        raise RecoveryValidationError("Schema-v4 active outcome financial metadata does not match its evaluation.")
    source = metadata.get("source")
    if source not in ("PAPER_ESTIMATED", "LIVE_DELTA_FILLS"):
        raise RecoveryValidationError("Schema-v4 active outcome source is unsupported.")
    if outcome.get("source") != source or event.get("outcome_source") != source:
        raise RecoveryValidationError("Schema-v4 active outcome source identity does not match.")
    if not isinstance(metadata.get("currency"), str) or not metadata["currency"].strip():
        raise RecoveryValidationError("Schema-v4 active outcome currency is missing.")
    if not isinstance(metadata.get("funding_assumption"), str) or not metadata["funding_assumption"].strip():
        raise RecoveryValidationError("Schema-v4 active outcome funding assumption is missing.")
    net = _finite(outcome.get("net_result"), "Schema-v4 active net result")
    if not math.isclose(net, _finite(event.get("realized_net_result"), "Schema-v4 evaluation net result"), rel_tol=1e-8, abs_tol=1e-8):
        raise RecoveryValidationError("Schema-v4 active outcome net result differs from its evaluation.")
    gross = _finite(metadata.get("gross_pnl"), "Schema-v4 gross P&L")
    fees = _finite(metadata.get("fees"), "Schema-v4 fees")
    funding = _finite(metadata.get("funding"), "Schema-v4 funding")
    if fees < 0:
        raise RecoveryValidationError("Schema-v4 fees cannot be negative.")
    if source == "PAPER_ESTIMATED":
        slippage = _finite(metadata.get("slippage_cost"), "Schema-v4 slippage")
        if (
            slippage < 0
            or not isinstance(metadata.get("cost_model"), str)
            or not metadata["cost_model"].strip()
            or not math.isclose(funding, 0.0, rel_tol=0.0, abs_tol=1e-12)
        ):
            raise RecoveryValidationError("Schema-v4 paper outcome cost evidence is invalid.")
        expected_net = gross - fees - slippage
    else:
        fill_count = metadata.get("fill_count")
        if isinstance(fill_count, bool) or not isinstance(fill_count, int) or fill_count <= 0:
            raise RecoveryValidationError("Schema-v4 live fill count is invalid.")
        for field in ("entry_vwap", "exit_vwap", "contract_value"):
            if _finite(metadata.get(field), f"Schema-v4 {field}") <= 0:
                raise RecoveryValidationError(f"Schema-v4 {field} must be positive.")
        expected_net = gross - fees + funding
    if not math.isclose(net, expected_net, rel_tol=1e-8, abs_tol=1e-8):
        raise RecoveryValidationError("Schema-v4 active outcome net result does not reconcile to source costs.")
    expected_label = "WIN" if net > 0 else "LOSS_OR_BREAKEVEN"
    if outcome.get("label") != expected_label or event.get("outcome_label") != expected_label:
        raise RecoveryValidationError("Schema-v4 active outcome label is inconsistent with net P&L.")
    return net


def _validate_v4_snapshot(root: Path, memory: dict[str, Any]) -> None:
    migration = memory.get("migration")
    legacy_outcomes = memory.get("legacy_outcomes")
    legacy_model = memory.get("legacy_model_snapshot")
    active_outcomes = memory.get("outcomes")
    model = memory.get("model")
    if not isinstance(migration, dict) or not isinstance(legacy_outcomes, list):
        raise RecoveryValidationError("Schema-v4 migration/archive fields are missing.")
    if not isinstance(legacy_model, dict) or not isinstance(active_outcomes, list):
        raise RecoveryValidationError("Schema-v4 model/outcome archive fields are invalid.")
    migration_id = migration.get("id")

    if migration_id == "verified-only-v1":
        if migration.get("from_schema_version") != 3:
            raise RecoveryValidationError("Schema-v4 migration ID/version is unsupported.")
        archive_path = _require_file(root, "ai_memory_v3_archive.json")
        archive_bytes = archive_path.read_bytes()
        archive_sha = hashlib.sha1(
            b"blob " + str(len(archive_bytes)).encode("ascii") + b"\0" + archive_bytes
        ).hexdigest()
        if migration.get("source_blob_sha") != archive_sha:
            raise RecoveryValidationError("Schema-v4 immutable v3 archive Git blob SHA does not match.")
        try:
            archive = json.loads(archive_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryValidationError(f"Schema-v4 immutable v3 archive is invalid JSON: {exc}") from exc
        if not isinstance(archive, dict) or archive.get("schema_version") != 3 or archive.get("symbol") != "XAUTUSD":
            raise RecoveryValidationError("Schema-v4 immutable archive is not the original XAUTUSD schema-v3 memory.")
        if archive.get("model") != legacy_model:
            raise RecoveryValidationError("Schema-v4 legacy model snapshot differs from immutable v3 archive.")
        archived_evaluations = archive.get("evaluations")
        current_evaluations = memory.get("evaluations")
        if not isinstance(archived_evaluations, list) or not isinstance(current_evaluations, list):
            raise RecoveryValidationError("Schema-v4 archive/current evaluations are invalid.")
        current_evaluations_by_id = {
            item.get("evaluation_id"): item
            for item in current_evaluations
            if isinstance(item, dict) and isinstance(item.get("evaluation_id"), str)
        }
        if len(current_evaluations_by_id) != len(current_evaluations):
            raise RecoveryValidationError("Schema-v4 current evaluation identities are missing or duplicated.")
        for archived_event in archived_evaluations:
            current_event = (
                current_evaluations_by_id.get(archived_event.get("evaluation_id"))
                if isinstance(archived_event, dict)
                else None
            )
            if current_event is None:
                raise RecoveryValidationError("Schema-v4 original evaluation is missing from the current memory.")
            archived_snapshot = dict(archived_event)
            current_snapshot = dict(current_event)
            # Counterfactual completion metadata is allowed to advance after the
            # v3 snapshot; all learning/identity fields remain immutable.
            for mutable_field in ("counterfactual_horizons_recorded", "counterfactual_complete"):
                archived_snapshot.pop(mutable_field, None)
                current_snapshot.pop(mutable_field, None)
            if _canonical(current_snapshot) != _canonical(archived_snapshot):
                raise RecoveryValidationError("Schema-v4 original evaluation learning evidence changed.")

        archived_counterfactuals = archive.get("counterfactual_outcomes")
        current_counterfactuals = memory.get("counterfactual_outcomes")
        if not isinstance(archived_counterfactuals, list) or not isinstance(current_counterfactuals, list):
            raise RecoveryValidationError("Schema-v4 archive/current counterfactual outcomes are invalid.")
        current_counterfactual_by_key = {
            (item.get("evaluation_id"), item.get("horizon_minutes")): item
            for item in current_counterfactuals
            if isinstance(item, dict)
        }
        if len(current_counterfactual_by_key) != len(current_counterfactuals):
            raise RecoveryValidationError("Schema-v4 current counterfactual identities are duplicated or invalid.")
        for archived_item in archived_counterfactuals:
            if (
                not isinstance(archived_item, dict)
                or current_counterfactual_by_key.get((archived_item.get("evaluation_id"), archived_item.get("horizon_minutes"))) is None
                or _canonical(current_counterfactual_by_key[(archived_item["evaluation_id"], archived_item["horizon_minutes"])]) != _canonical(archived_item)
            ):
                raise RecoveryValidationError("Schema-v4 original counterfactual outcomes are not preserved.")
        source_outcomes = archive.get("outcomes")
        if not isinstance(source_outcomes, list):
            raise RecoveryValidationError("Immutable v3 archive outcomes are invalid.")
    elif migration_id == "native-v4":
        if (
            migration.get("from_schema_version") is not None
            or migration.get("source_blob_sha") is not None
            or legacy_outcomes
        ):
            raise RecoveryValidationError("Native schema-v4 archive metadata is invalid.")
        source_outcomes = active_outcomes
    else:
        raise RecoveryValidationError("Schema-v4 migration ID/version is unsupported.")

    source_by_id: dict[str, dict[str, Any]] = {}
    source_trades: set[str] = set()
    for item in source_outcomes:
        if not isinstance(item, dict):
            raise RecoveryValidationError("Schema-v4 source outcomes must contain only objects.")
        evaluation_id, trade_id = item.get("evaluation_id"), item.get("trade_id")
        if not isinstance(evaluation_id, str) or not evaluation_id or evaluation_id in source_by_id:
            raise RecoveryValidationError("Schema-v4 source outcome evaluation IDs are missing or duplicated.")
        if not isinstance(trade_id, str) or not trade_id or trade_id in source_trades:
            raise RecoveryValidationError("Schema-v4 source outcome trade IDs are missing or duplicated.")
        source_by_id[evaluation_id] = item
        source_trades.add(trade_id)

    active_by_id: dict[str, dict[str, Any]] = {}
    active_trades: set[str] = set()
    for outcome in active_outcomes:
        if not isinstance(outcome, dict):
            raise RecoveryValidationError("Schema-v4 active outcomes must contain only objects.")
        evaluation_id, trade_id = outcome.get("evaluation_id"), outcome.get("trade_id")
        if (
            not isinstance(evaluation_id, str)
            or not evaluation_id
            or not isinstance(trade_id, str)
            or not trade_id
        ):
            raise RecoveryValidationError("Schema-v4 active outcome identity is invalid.")
        if evaluation_id in source_by_id:
            if _canonical(outcome) != _canonical(source_by_id[evaluation_id]):
                raise RecoveryValidationError("Schema-v4 active source outcome differs from immutable v3 archive.")
        elif migration_id != "verified-only-v1":
            raise RecoveryValidationError("Native schema-v4 active outcome is missing from its canonical outcome array.")
        if evaluation_id in active_by_id or trade_id in active_trades:
            raise RecoveryValidationError("Schema-v4 active outcomes contain duplicate identities.")
        active_by_id[evaluation_id] = outcome
        active_trades.add(trade_id)

    legacy_by_id: dict[str, dict[str, Any]] = {}
    for wrapper in legacy_outcomes:
        if not isinstance(wrapper, dict) or not isinstance(wrapper.get("outcome"), dict):
            raise RecoveryValidationError("Schema-v4 legacy outcome wrapper is invalid.")
        outcome = wrapper["outcome"]
        evaluation_id = outcome.get("evaluation_id")
        if (
            wrapper.get("reason") not in V4_LEGACY_REASONS
            or not isinstance(evaluation_id, str)
            or evaluation_id not in source_by_id
            or _canonical(outcome) != _canonical(source_by_id[evaluation_id])
            or evaluation_id in legacy_by_id
        ):
            raise RecoveryValidationError("Schema-v4 legacy outcomes are not a unique value-preserving archive.")
        legacy_by_id[evaluation_id] = outcome
    active_source_ids = set(active_by_id).intersection(source_by_id)
    if (
        active_source_ids.intersection(legacy_by_id)
        or active_source_ids.union(legacy_by_id) != set(source_by_id)
    ):
        raise RecoveryValidationError("Schema-v4 initial active and legacy outcomes do not exactly partition the source outcomes.")
    if (
        migration.get("active_outcome_count") != len(active_outcomes)
        or migration.get("legacy_outcome_count") != len(legacy_outcomes)
    ):
        raise RecoveryValidationError("Schema-v4 migration counts do not match active/legacy arrays.")

    evaluations = memory.get("evaluations")
    if not isinstance(evaluations, list):
        raise RecoveryValidationError("Schema-v4 evaluations are invalid.")
    evaluation_by_id = {}
    for event in evaluations:
        if not isinstance(event, dict):
            raise RecoveryValidationError("Schema-v4 evaluation records must contain only objects.")
        evaluation_id = event.get("evaluation_id")
        if not isinstance(evaluation_id, str) or not evaluation_id or evaluation_id in evaluation_by_id:
            raise RecoveryValidationError("Schema-v4 evaluation identities are missing or duplicated.")
        evaluation_by_id[evaluation_id] = event

    rebuilt = {
        "weights": dict(V4_INITIAL_WEIGHTS),
        "bias": 0.0,
        "samples": 0,
        "wins": 0,
        "losses": 0,
    }
    for outcome in active_outcomes:
        event = evaluation_by_id.get(outcome["evaluation_id"])
        direction = str(event.get("signal", "")).upper() if isinstance(event, dict) else ""
        if (
            not isinstance(event, dict)
            or event.get("outcome_recorded") is not True
            or event.get("would_decision") != "APPROVE"
            or event.get("entry_gate_decision") != "APPROVE"
            or direction not in ("BUY", "SELL")
            or str(event.get("flip_direction", "")).upper() != direction
            or str(event.get("proposal_direction", "")).upper() != direction
            or event.get("trade_id") != outcome.get("trade_id")
        ):
            raise RecoveryValidationError("Schema-v4 active outcome lacks exact approved same-direction trade proof.")
        features = event.get("features")
        if not isinstance(features, dict) or any(name not in features for name in V4_FEATURE_NAMES):
            raise RecoveryValidationError("Schema-v4 active outcome features are incomplete.")
        clean_features = {name: _finite(features[name], f"Schema-v4 feature {name}") for name in V4_FEATURE_NAMES}
        net = _validate_v4_financial_outcome(outcome, event)
        probability = _sigmoid(
            rebuilt["bias"] + sum(rebuilt["weights"][name] * clean_features[name] for name in V4_FEATURE_NAMES)
        )
        target = 1.0 if net > 0 else 0.0
        error = target - probability
        for name in V4_FEATURE_NAMES:
            old_weight = rebuilt["weights"][name]
            updated = old_weight + V4_LEARNING_RATE * (
                error * clean_features[name] - V4_L2_PENALTY * old_weight
            )
            rebuilt["weights"][name] = max(-8.0, min(8.0, updated))
        rebuilt["bias"] = max(-8.0, min(8.0, rebuilt["bias"] + V4_LEARNING_RATE * error))
        rebuilt["samples"] += 1
        if target == 1.0:
            rebuilt["wins"] += 1
        else:
            rebuilt["losses"] += 1

    if model.get("samples") != rebuilt["samples"] or model.get("wins") != rebuilt["wins"] or model.get("losses") != rebuilt["losses"]:
        raise RecoveryValidationError("Schema-v4 active model counters do not match deterministic replay.")
    for name in V4_FEATURE_NAMES:
        if not math.isclose(_finite(model["weights"].get(name), f"Schema-v4 model weight {name}"), rebuilt["weights"][name], rel_tol=1e-12, abs_tol=1e-12):
            raise RecoveryValidationError("Schema-v4 active model weights do not match deterministic verified-only rebuild.")
    if not math.isclose(_finite(model.get("bias"), "Schema-v4 model bias"), rebuilt["bias"], rel_tol=1e-12, abs_tol=1e-12):
        raise RecoveryValidationError("Schema-v4 active model bias does not match deterministic verified-only rebuild.")


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
    if schema_version not in (1, 2, 3, 4) or memory.get("symbol") != "XAUTUSD":
        raise RecoveryValidationError("Private AI memory is not a supported XAUTUSD memory object.")
    model = memory.get("model")
    if not isinstance(model, dict) or not isinstance(model.get("weights"), dict):
        raise RecoveryValidationError("Private AI memory model/weights are missing.")
    if not isinstance(memory.get("evaluations"), list) or not isinstance(memory.get("outcomes"), list):
        raise RecoveryValidationError("Private AI memory evaluation/outcome arrays are invalid.")

    if schema_version in (3, 4):
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
    if schema_version in (2, 3, 4):
        required_weights.update(SCHEMA_V2_WEIGHTS)
    if not required_weights.issubset(model["weights"]):
        raise RecoveryValidationError("Private AI memory is missing required model weights.")

    counters = (model.get("samples", 0), model.get("wins", 0), model.get("losses", 0))
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counters):
        raise RecoveryValidationError("Private AI memory sample counters are invalid.")
    samples, wins, losses = counters
    if samples != wins + losses:
        raise RecoveryValidationError("Private AI memory sample counters do not reconcile.")

    if schema_version == 4:
        _validate_v4_snapshot(root, memory)

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
