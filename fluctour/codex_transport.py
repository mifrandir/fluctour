"""Codex app-server stdio transport with live usage and cooperative interruption.

Official protocol: https://learn.chatgpt.com/docs/app-server
The installed CLI's generated schemas define thread/tokenUsage/updated,
turn/interrupt, ephemeral thread/start and turn/start.outputSchema.

``limits`` requires token_limit and optionally timeout_seconds (900) and
interrupt_grace_seconds (2). Optional finalize_token_threshold, below token_limit,
requests one early checkpoint with turn/steer; unsupported steering leaves the
usage interruption guard active. on_usage receives cumulative provider counts;
returning False requests interruption. progress receives selected protocol
notifications, never configuration responses or authentication data.

Observed usage is a lower bound until a successful turn finishes. Usage events
can arrive after an inference request has already been billed, so interruption
is a spending guard, not a hard provider billing cap. Interrupted, failed or
unobserved tails remain unknown and require reservation by the caller.
"""

import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import time


DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "multi_agent", "apps", "browser_use", "computer_use",
    "hooks", "plugins", "skill_search", "code_mode", "code_mode_host", "image_generation",
    "view_image", "workspace_dependencies", "shell_snapshot",
)
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_STREAM_BYTES = 16 * 1024 * 1024
BASE_INSTRUCTIONS = "You research public travel sources using web search only."
DEVELOPER_INSTRUCTIONS = "Use only the supplied public request. Never inspect files, accounts, tools other than web search, or unrelated instructions."
FINALIZE_INSTRUCTIONS = "Finish research now using gathered sources; do not call further tools; return the requested complete JSON with unknowns explicit."


def app_server_command():
    executable = shutil.which("codex")
    if not executable:
        raise ValueError("Codex CLI is missing. Install it and run codex login, then retry.")
    command = [executable, "app-server", "--listen", "stdio://"]
    for feature in DISABLED_FEATURES:
        command.extend(("--disable", feature))
    # App-server lacks exec's ignore-user-config flag; overrides are verified before inference.
    for setting in ('approval_policy="never"', 'sandbox_mode="read-only"',
                    'web_search="live"', 'mcp_servers={}', 'model_providers={}',
                    'model_provider="openai"', 'project_doc_max_bytes=0'):
        command.extend(("-c", setting))
    return command


def _safe_configuration(config):
    if not isinstance(config, dict):
        return False
    features = config.get("features", {})
    return (isinstance(features, dict)
            and all(features.get(name) is False for name in DISABLED_FEATURES)
            and config.get("mcp_servers", {}) == {}
            and config.get("model_providers", {}) == {}
            and config.get("model_provider") == "openai"
            and config.get("web_search") == "live"
            and config.get("project_doc_max_bytes") == 0
            and not any(config.get(key) for key in (
                "instructions", "developer_instructions", "model_instructions_file")))


def _usage(params):
    raw = params.get("tokenUsage", {}).get("total")
    if not isinstance(raw, dict):
        raise ValueError("Invalid usage event")
    keys = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
            "cachedInputTokens": "cached_input_tokens", "totalTokens": "total_tokens",
            "reasoningOutputTokens": "reasoning_output_tokens"}
    result = {}
    for key, target in keys.items():
        value = raw.get(key)
        if type(value) is not int or value < 0:
            raise ValueError("Invalid usage event")
        result[target] = value
    if result["cached_input_tokens"] > result["input_tokens"]:
        raise ValueError("Invalid cached usage")
    result["total_tokens"] = max(result["total_tokens"], result["input_tokens"] + result["output_tokens"])
    return result


def run_codex(prompt, schema, workspace, limits, progress=None, on_usage=None, cancel_event=None):
    """Run one fresh public-only thread and return result plus conservative usage."""
    if not isinstance(prompt, str) or not prompt or len(prompt.encode()) > MAX_LINE_BYTES // 2:
        raise ValueError("Provide a bounded research prompt.")
    if not isinstance(schema, dict):
        schema = json.loads(Path(schema).read_text())
    if not isinstance(schema, dict):
        raise ValueError("Output schema must be a JSON object.")
    if not isinstance(limits, dict) or type(limits.get("token_limit")) is not int or limits["token_limit"] < 1:
        raise ValueError("A positive token_limit is required.")
    finalize_threshold = limits.get("finalize_token_threshold")
    if finalize_threshold is not None and (type(finalize_threshold) is not int
                                           or not 0 < finalize_threshold < limits["token_limit"]):
        raise ValueError("finalize_token_threshold must be a positive integer below token_limit.")
    timeout = limits.get("timeout_seconds", 900)
    grace = limits.get("interrupt_grace_seconds", 2)
    for value, maximum in ((timeout, 86400), (grace, 10)):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError("Timeout and interrupt grace must be positive finite seconds.")
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = app_server_command()
    result = {"result": None, "usage": {}, "usage_complete": False,
              "thread_id": None, "turn_id": None, "status": "failed", "stop_reason": None,
              "finalize_requested": False, "finalize_supported": None}
    if cancel_event is not None and cancel_event.is_set():
        return result | {"status": "interrupted", "stop_reason": "cancelled"}
    process = None
    started = time.monotonic()
    stop_at = None
    interrupt_sent = False
    terminal = False
    final_text = None
    buffer, outbound = b"", bytearray()
    streamed = 0

    def emit(message):
        outbound.extend((json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode())

    def stop(reason):
        nonlocal stop_at
        if result["stop_reason"] is None:
            result["stop_reason"] = reason
            stop_at = time.monotonic() + grace

    def notify(message):
        if progress is not None:
            progress(message)

    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, cwd=workspace, start_new_session=True)
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        emit({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "fluctour", "version": "0.1.0"}}})
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not terminal:
                clock = time.monotonic()
                if cancel_event is not None and cancel_event.is_set():
                    stop("cancelled")
                if clock - started >= timeout:
                    stop("timeout")
                if stop_at is not None:
                    if result["thread_id"] and result["turn_id"] and not interrupt_sent:
                        emit({"id": 5, "method": "turn/interrupt", "params": {
                            "threadId": result["thread_id"], "turnId": result["turn_id"]}})
                        interrupt_sent = True
                    if clock >= stop_at:
                        break
                if outbound:
                    try:
                        written = os.write(process.stdin.fileno(), outbound)
                        del outbound[:written]
                    except BlockingIOError:
                        pass
                    except BrokenPipeError:
                        stop("process_exited")
                        break
                events = selector.select(0.05)
                if not events and process.poll() is not None:
                    stop("process_exited")
                    break
                for key, _ in events:
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        stop("process_exited")
                        terminal = True
                        break
                    streamed += len(chunk)
                    buffer += chunk
                    if streamed > MAX_STREAM_BYTES or (len(buffer) > MAX_LINE_BYTES and b"\n" not in buffer):
                        stop("output_limit")
                        break
                    while b"\n" in buffer and not terminal:
                        line, buffer = buffer.split(b"\n", 1)
                        if not line.strip():
                            continue
                        if len(line) > MAX_LINE_BYTES:
                            stop("output_limit")
                            break
                        try:
                            message = json.loads(line)
                        except (ValueError, UnicodeError, RecursionError):
                            stop("protocol_error")
                            break
                        if not isinstance(message, dict):
                            stop("protocol_error")
                            break
                        if "id" in message and "method" in message:
                            emit({"id": message["id"], "error": {"code": -32601,
                                  "message": "External actions and approvals are disabled."}})
                            stop("unexpected_server_request")
                            continue
                        if "id" in message:
                            if "error" in message:
                                if message["id"] == 6:
                                    result["finalize_supported"] = False
                                    continue
                                stop("rpc_error")
                                continue
                            response = message.get("result", {})
                            if message["id"] == 1:
                                emit({"method": "initialized", "params": {}})
                                emit({"id": 2, "method": "config/read", "params": {
                                    "cwd": str(workspace), "includeLayers": False}})
                            elif message["id"] == 2:
                                if not _safe_configuration(response.get("config")):
                                    stop("unsafe_configuration")
                                    continue
                                if result["stop_reason"] is None:
                                    emit({"id": 3, "method": "thread/start", "params": {
                                        "cwd": str(workspace), "ephemeral": True, "sandbox": "read-only",
                                        "approvalPolicy": "never", "baseInstructions": BASE_INSTRUCTIONS,
                                        "developerInstructions": DEVELOPER_INSTRUCTIONS}})
                            elif message["id"] == 3:
                                result["thread_id"] = response.get("thread", {}).get("id")
                                if (not isinstance(result["thread_id"], str)
                                        or response.get("approvalPolicy") != "never"
                                        or response.get("sandbox", {}).get("type") != "readOnly"
                                        or response.get("instructionSources")):
                                    stop("unsafe_configuration")
                                    continue
                                notify({"method": "thread/started", "params": {"thread": {"id": result["thread_id"]}}})
                                if result["stop_reason"] is None:
                                    emit({"id": 4, "method": "turn/start", "params": {
                                        "threadId": result["thread_id"], "input": [{"type": "text", "text": prompt}],
                                        "outputSchema": schema, "approvalPolicy": "never",
                                        "sandboxPolicy": {"type": "readOnly", "networkAccess": False}}})
                            elif message["id"] == 4:
                                result["turn_id"] = response.get("turn", {}).get("id")
                                if not isinstance(result["turn_id"], str):
                                    stop("protocol_error")
                            elif message["id"] == 6:
                                result["finalize_supported"] = True
                            continue
                        method, params = message.get("method"), message.get("params", {})
                        if not isinstance(params, dict):
                            stop("protocol_error")
                            continue
                        if params.get("threadId") not in (None, result["thread_id"]):
                            continue
                        if method == "turn/started":
                            result["turn_id"] = params.get("turn", {}).get("id")
                        elif method == "thread/tokenUsage/updated":
                            result["turn_id"] = result["turn_id"] or params.get("turnId")
                            try:
                                observed = _usage(params)
                            except (ValueError, TypeError, AttributeError):
                                stop("invalid_usage")
                                continue
                            result["usage"] = {key: max(value, result["usage"].get(key, 0)) for key, value in observed.items()}
                            allowed = on_usage(dict(result["usage"])) if on_usage is not None else True
                            if result["usage"]["total_tokens"] >= limits["token_limit"] or allowed is False:
                                stop("token_limit")
                            elif (finalize_threshold is not None and not result["finalize_requested"]
                                  and result["usage"]["total_tokens"] >= finalize_threshold
                                  and result["stop_reason"] is None and result["turn_id"]):
                                emit({"id": 6, "method": "turn/steer", "params": {
                                    "threadId": result["thread_id"], "expectedTurnId": result["turn_id"],
                                    "input": [{"type": "text", "text": FINALIZE_INSTRUCTIONS}]}})
                                result["finalize_requested"] = True
                        elif method == "item/completed":
                            item = params.get("item", {})
                            if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer"):
                                final_text = item.get("text")
                        elif method == "turn/completed":
                            turn = params.get("turn", {})
                            result["turn_id"] = result["turn_id"] or turn.get("id")
                            result["status"] = turn.get("status", "failed")
                            if result["status"] not in ("completed", "interrupted", "failed"):
                                result["status"] = "failed"
                            terminal = True
                        if method in ("turn/started", "turn/completed", "thread/tokenUsage/updated",
                                      "item/started", "item/completed", "item/agentMessage/delta"):
                            notify(message)
        if not terminal or result["stop_reason"] is not None and result["status"] == "failed":
            result["status"] = "interrupted" if result["stop_reason"] in ("token_limit", "timeout", "cancelled") else "failed"
        if isinstance(final_text, str):
            try:
                result["result"] = json.loads(final_text)
            except (ValueError, RecursionError):
                result["stop_reason"] = result["stop_reason"] or "invalid_result"
        result["usage_complete"] = bool(result["usage"] and result["status"] == "completed"
                                         and result["stop_reason"] not in ("protocol_error", "invalid_usage", "output_limit"))
        return result
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        result["stop_reason"] = result["stop_reason"] or "transport_error"
        return result
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=1)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
