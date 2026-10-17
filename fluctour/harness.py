"""Read-only inspection of current research instructions and captured executions."""

import copy
import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from urllib.parse import quote

from . import __version__
from .codex_transport import BASE_INSTRUCTIONS, DEVELOPER_INSTRUCTIONS, FINALIZE_INSTRUCTIONS
from .jobs import _public_context, job_state, validate_request
from .research import (ACTIVITY_PLAN_INSTRUCTIONS, CLI_SETTINGS, INSTRUCTIONS, SESSION_RESERVATION,
                       STAGES, STAGE_TASKS, STAGE_TOKEN_LIMIT, STAGE_FINALIZE_THRESHOLD, budget_state, usage_total)


MAX_PROMPT_BYTES = 360000
MODEL_STAGES = set(STAGES) | {"refresh"}


def _run_settings(captured):
    if not isinstance(captured, dict):
        return None
    cli = captured.get("cli_settings")
    safe_cli = {}
    if isinstance(cli, dict):
        allowed = {"sandbox": {"read-only", "workspace-write", "danger-full-access"},
                   "approval_policy": {"never", "untrusted", "on-request", "on-failure"},
                   "web_search": {"live", "cached", "disabled"}, "output": {"json"}}
        for key, values in allowed.items():
            if isinstance(cli.get(key), str) and cli[key] in values:
                safe_cli[key] = cli[key]
        if type(cli.get("ignore_user_config")) is bool:
            safe_cli["ignore_user_config"] = cli["ignore_user_config"]
        if type(cli.get("configuration_verification_required")) is bool:
            safe_cli["configuration_verification_required"] = cli["configuration_verification_required"]
        safe_cli["transport"] = cli.get("transport") if cli.get("transport") in ("exec", "app-server") else "exec"
        if isinstance(cli.get("disabled_capabilities"), list):
            safe_cli["disabled_capabilities"] = [name for name in cli["disabled_capabilities"]
                if isinstance(name, str) and name in CLI_SETTINGS["disabled_capabilities"]]
        if cli.get("model") is None:
            safe_cli["model"] = None
    timeout = captured.get("timeout_seconds")
    schema = captured.get("schema_name")
    digest = captured.get("schema_sha256")
    result = {"timeout_seconds": timeout if type(timeout) is int and 1 <= timeout <= 3600 else None,
            "schema_name": schema if schema in ("research.schema.json", "research.activities.schema.json") else None,
            "schema_sha256": digest if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) else None,
            "cli_settings": safe_cli, "transport": safe_cli.get("transport", "exec")}
    for key in ("observed_token_limit", "finalize_token_threshold"):
        value = captured.get(key)
        result[key] = value if type(value) is int and 0 < value <= 1000000000 else None
    status = captured.get("transport_status")
    reason = captured.get("stop_reason")
    result["transport_status"] = status if status in ("completed", "interrupted", "failed") else None
    reasons = {"token_limit", "timeout", "cancelled", "process_exited", "output_limit", "protocol_error", "rpc_error",
               "unsafe_configuration", "invalid_usage", "unexpected_server_request", "invalid_result", "transport_error"}
    result["stop_reason"] = reason if isinstance(reason, str) and reason in reasons else None
    verified = captured.get("configuration_verified")
    result["configuration_verified"] = False if reason == "unsafe_configuration" else verified if type(verified) is bool else None
    layers = captured.get("prompt_layers")
    result["prompt_layers"] = ({key: layers[key] for key in ("base", "developer")
        if isinstance(layers.get(key), str) and len(layers[key]) <= 6000} if isinstance(layers, dict) else None)
    finalize_requested = captured.get("finalize_requested")
    result["finalize_requested"] = finalize_requested if type(finalize_requested) is bool else None
    instruction = captured.get("finalize_instruction")
    result["finalize_instruction"] = (instruction if finalize_requested is True and isinstance(instruction, str)
                                       and len(instruction) <= 6000 else None)
    return result


def _captured_prompt(data_root, run_id, harness):
    missing = {"status": "not_captured", "text": None, "sha256": None,
               "message": "No exact prompt was captured for this historical run."}
    if not isinstance(harness, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(harness.get("prompt_sha256", ""))):
        return missing
    unavailable = {"status": "unavailable", "text": None, "sha256": None,
                   "message": "The captured prompt is missing, changed or unreadable."}
    directory = data_root / "research"
    target = directory / (run_id + ".prompt.txt")
    try:
        if directory.is_symlink() or target.is_symlink() or not target.resolve().is_relative_to(data_root):
            return unavailable
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PROMPT_BYTES:
                return unavailable
            raw = handle.read(MAX_PROMPT_BYTES + 1)
        if len(raw) > MAX_PROMPT_BYTES:
            return unavailable
        digest = hashlib.sha256(raw).hexdigest()
        if digest != harness["prompt_sha256"]:
            return unavailable
        text = raw.decode("utf-8")
        if len(text) > 90000:
            return unavailable
        return {"status": "captured", "text": text, "sha256": digest,
                "message": "Exact prompt saved before starting this run."}
    except (OSError, UnicodeError, RuntimeError):
        return unavailable


def _run_details(root, ledger):
    run_id = ledger["id"]
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        return None
    data_root = Path(ledger["data_root"]).resolve()
    database = data_root / "state.sqlite"
    if (not data_root.is_relative_to(root) or database.is_symlink()
            or not database.resolve().is_relative_to(root) or not database.is_file()):
        return None
    connection = None
    try:
        connection = sqlite3.connect("file:" + quote(str(database), safe="/") + "?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT id,stage,status,started_at,ended_at,thread_id FROM runs WHERE id=? AND trip_id=? AND stage=?",
                                 (run_id, ledger["trip_id"], ledger["stage"])).fetchone()
        if row is None:
            return None
        saved = connection.execute("SELECT value FROM settings WHERE key=?", ("progress:" + run_id,)).fetchone()
        progress = json.loads(saved[0]) if saved else {}
        captured_settings = progress.get("harness") if isinstance(progress, dict) else None
        settings = _run_settings(captured_settings)
        metadata = {key: row[key] for key in ("id", "stage", "status", "started_at", "ended_at")}
        return metadata | {"source": "direct_research" if data_root == root else "public_research",
                            "usage": {"input_tokens": ledger["input_tokens"], "output_tokens": ledger["output_tokens"],
                                      "cached_input_tokens": ledger["cached_tokens"],
                                      "observed_input_tokens": ledger.get("observed_input_tokens", 0),
                                      "observed_output_tokens": ledger.get("observed_output_tokens", 0),
                                      "observed_cached_input_tokens": ledger.get("observed_cached_tokens", 0),
                                      "charged_or_reserved_tokens": usage_total(ledger)},
                            "prompt": _captured_prompt(data_root, run_id, captured_settings), "settings": settings}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None
    finally:
        if connection is not None:
            connection.close()


def inspect_harness(store, trip_id):
    """Expose instructions and execution evidence without inference or reconstructed prompts."""
    store.trip(trip_id)
    public_request = public_brief = None
    state = job_state(store, trip_id)
    if state is not None:
        try:
            public_request = {key: value for key, value in validate_request(state["request"]).items()
                              if key != "business_anchor"}
            public_brief, _ = _public_context(store, public_request)
        except ValueError:
            pass
    root = store.root.resolve()
    ledger, _ = store.job_usage(trip_id)
    model_runs = sorted((row for row in ledger if row["stage"] in MODEL_STAGES),
                        key=lambda row: (row["started_at"], row["id"]), reverse=True)
    runs = []
    for row in model_runs[:30]:
        details = _run_details(root, row)
        if details is not None:
            runs.append(details)
    current_schemas = {}
    for key, name in (("base", "research.schema.json"), ("activities", "research.activities.schema.json")):
        current_schemas[key] = json.loads(Path(__file__).with_name(name).read_text())
    return {"version": __version__, "current_public_request": public_request,
            "current_templates": {"instructions": INSTRUCTIONS, "public_request_brief": public_brief,
            "activity_plan_instructions": ACTIVITY_PLAN_INSTRUCTIONS, "stage_tasks": dict(STAGE_TASKS),
            "base_instructions": BASE_INSTRUCTIONS, "developer_instructions": DEVELOPER_INSTRUCTIONS,
            "finalize_instructions": FINALIZE_INSTRUCTIONS},
            "execution_settings": copy.deepcopy(CLI_SETTINGS) | {"default_timeout_seconds": 600,
                "cli_default_timeout_seconds": 900,
                "stages": list(STAGES), "context_limit_characters": 90000,
                "fresh_sessions": True, "model_policy": "Codex CLI default (not pinned)",
                "observed_token_limit": STAGE_TOKEN_LIMIT, "finalize_token_threshold": STAGE_FINALIZE_THRESHOLD,
                "session_admission_reservation_tokens": SESSION_RESERVATION,
                "reservation_note": "An unknown session tail reserves 50,000 tokens in addition to observed usage.",
                "token_limit_note": "A checkpoint is requested at the soft threshold and interruption at the observed limit. Usage events can arrive late, so billing can overshoot; this is not a hard provider cap.",
                "configuration_note": "App-server verifies effective configuration before inference; user configuration is overridden and checked rather than ignored."},
            "budget": budget_state(store, trip_id), "runs": runs,
            "runs_truncated": max(0, len(model_runs) - 30), "current_schemas": current_schemas,
            "notes": ["Templates and schemas shown here are current; they are not historical execution snapshots.",
                      "The public request brief is current builder output, not a captured execution prompt.",
                      "Captured user prompts are exact. Current transport instruction layers are shown separately; missing historical prompts are not reconstructed.",
                      "Execution settings describe browser research jobs; captured run settings take precedence.",
                      "Private local composition, profiles, activity history, stderr and authentication credentials are omitted."]}
