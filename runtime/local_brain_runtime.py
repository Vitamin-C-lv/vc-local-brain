"""HTTP/token/runtime supervision for the Local Qwen relay.

This module intentionally does not know anything about DSH session storage.
It only estimates an incoming local request, selects a physical context tier,
and coordinates the single Windows llama-server process when a switch is
needed.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from local_brain_context import (
    ContextDecision,
    ContextManager,
    OutputBudget,
    OutputBurstState,
    RuntimeConfig,
    compute_effective_max_tokens,
    coerce_optional_nonnegative_int,
    estimate_completion_reserve,
    request_has_tools,
    required_context_tokens,
    resolve_reasoning_budget,
)


LOG = logging.getLogger("local-brain-runtime")
DEFAULT_UPSTREAM = "http://172.25.240.1:17861"
DEFAULT_RESTART_HELPER = "/mnt/d/VC-AI-Pet/runtime/Start-LocalQwen.ps1"
MAX_OBSERVE_BYTES = 16 * 1024 * 1024


class LocalBrainRuntimeError(RuntimeError):
    """Base error raised before a request is forwarded upstream."""

    status_code = 503
    error_code = "LOCAL_BRAIN_RUNTIME_ERROR"


class ContextCapacityError(LocalBrainRuntimeError):
    status_code = 503
    error_code = "LOCAL_CONTEXT_COMPACTION_REQUIRED"


class ContextSwitchError(LocalBrainRuntimeError):
    status_code = 503
    error_code = "LOCAL_CONTEXT_SWITCH_FAILED"


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class ServerProbe:
    healthy: bool
    context: int | None
    processing: bool
    source: str
    error: str | None = None


@dataclass(frozen=True)
class PromptEstimate:
    tokens: int
    image_count: int
    image_token_reserve: int
    method: str


@dataclass(frozen=True)
class PreparedRequest:
    body: dict[str, Any]
    estimate: PromptEstimate
    budget: OutputBudget
    decision: ContextDecision


def _json_headers(headers: Mapping[str, str] | None = None) -> dict[str, str]:
    result = {"Content-Type": "application/json"}
    if headers:
        result.update(headers)
    return result


def _read_exact_or_eof(response: Any) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            remaining = max(0, int(content_length))
        except (TypeError, ValueError):
            remaining = None
        if remaining is not None:
            chunks: list[bytes] = []
            while remaining:
                chunk = response.read(min(8192, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
    chunks = []
    while True:
        chunk = response.read(8192)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _image_count(messages: Any) -> int:
    if not isinstance(messages, list):
        return 0
    count = 0
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, Mapping) and item.get("type") in {"image_url", "image", "input_image"}:
                count += 1
    return count


def _without_image_payload(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_image_payload(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    result = {key: _without_image_payload(item) for key, item in value.items()}
    if result.get("type") in {"image_url", "image", "input_image"}:
        if isinstance(result.get("image_url"), Mapping):
            result["image_url"] = {"url": "[image]"}
        result.pop("image", None)
        result["text"] = "[image]"
    return result


def _fallback_text_token_estimate(body: Mapping[str, Any]) -> int:
    messages = _without_image_payload(body.get("messages", []))
    tools = _without_image_payload(body.get("tools", []))
    serialized = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, separators=(",", ":"))
    return max(1, (len(serialized) + 3) // 4)


class LocalBrainRuntime:
    """Stateful Local Qwen request planner and single-server supervisor."""

    def __init__(
        self,
        upstream: str | None = None,
        config_path: str | os.PathLike[str] | None = None,
        restart_helper: str | None = None,
        *,
        probe: bool = True,
        logger: logging.Logger | None = None,
    ):
        self.upstream = (upstream or os.environ.get("QWEN_UPSTREAM", DEFAULT_UPSTREAM)).rstrip("/")
        self.config = RuntimeConfig.load(config_path)
        self.context = ContextManager(config=self.config)
        self.output = OutputBurstState(self.config)
        self.restart_helper = restart_helper or os.environ.get("QWEN_RESTART_HELPER", DEFAULT_RESTART_HELPER)
        self.log = logger or LOG
        self.request_lock = threading.Lock()
        self.switch_lock = threading.RLock()
        self.server_healthy = False
        self.server_probe_source = "unprobed"
        self.last_probe_error: str | None = None
        self.last_context_switch: str | None = None
        self.last_error: str | None = None
        self.last_restart: dict[str, Any] | None = None
        self.last_estimate_method = "none"
        self.last_image_count = 0
        self.last_image_token_reserve = self.config.image_token_reserve
        self.last_request_mode = "dynamic"
        self.last_explicit_max_tokens: int | None = None
        self.last_completion_reserve = 0
        self.last_effective_max_tokens = 0
        self.last_reasoning_budget = 0
        self.last_has_tools = False
        if probe:
            self.refresh_server_state()

    @contextmanager
    def request_slot(self) -> Iterator[None]:
        """Serialize upstream requests because the physical server has one slot."""
        with self.request_lock:
            yield

    def _request(self, path: str, method: str = "GET", payload: Any = None, timeout: float = 12.0) -> HTTPResult:
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers = _json_headers()
        request = Request(f"{self.upstream}{path}", data=body, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=timeout)
        except HTTPError as error:
            with error:
                return HTTPResult(error.code, dict(error.headers.items()), _read_exact_or_eof(error))
        except (OSError, URLError, TimeoutError) as error:
            raise LocalBrainRuntimeError(f"upstream {method} {path} failed: {error}") from error
        with response:
            return HTTPResult(response.status, dict(response.headers.items()), _read_exact_or_eof(response))

    def _json_request(self, path: str, method: str = "GET", payload: Any = None, timeout: float = 12.0) -> tuple[HTTPResult, Any]:
        result = self._request(path, method, payload, timeout)
        try:
            decoded = json.loads(result.body.decode("utf-8")) if result.body else None
        except (UnicodeDecodeError, ValueError):
            decoded = None
        return result, decoded

    def probe_server(self) -> ServerProbe:
        healthy = False
        processing = False
        errors: list[str] = []
        try:
            health, health_body = self._json_request("/health", timeout=5)
            healthy = health.status == 200 and isinstance(health_body, Mapping) and health_body.get("status") == "ok"
            if not healthy:
                errors.append(f"health HTTP {health.status}")
        except LocalBrainRuntimeError as error:
            errors.append(str(error))

        context: int | None = None
        source = "none"
        try:
            slots, slot_body = self._json_request("/slots", timeout=8)
            if slots.status == 200 and isinstance(slot_body, list):
                contexts = [item.get("n_ctx") for item in slot_body if isinstance(item, Mapping) and isinstance(item.get("n_ctx"), int)]
                if contexts:
                    context = contexts[0]
                    source = "slots"
                    processing = any(item.get("is_processing") is True for item in slot_body if isinstance(item, Mapping))
            elif slots.status != 200:
                errors.append(f"slots HTTP {slots.status}")
        except LocalBrainRuntimeError as error:
            errors.append(str(error))

        if context is None:
            try:
                models, models_body = self._json_request("/v1/models", timeout=8)
                if models.status == 200 and isinstance(models_body, Mapping):
                    data = models_body.get("data")
                    if isinstance(data, list) and data and isinstance(data[0], Mapping):
                        meta = data[0].get("meta")
                        if isinstance(meta, Mapping) and isinstance(meta.get("n_ctx"), int):
                            context = meta["n_ctx"]
                            source = "v1/models"
                elif models.status != 200:
                    errors.append(f"models HTTP {models.status}")
            except LocalBrainRuntimeError as error:
                errors.append(str(error))

        return ServerProbe(healthy, context, processing, source, "; ".join(errors) if errors else None)

    def refresh_server_state(self) -> ServerProbe:
        probe = self.probe_server()
        self.server_healthy = probe.healthy
        self.server_probe_source = probe.source
        self.last_probe_error = probe.error
        if probe.context is not None:
            self.context.current_context = probe.context
        return probe

    def _template_payload(self, body: Mapping[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {"messages": deepcopy(body.get("messages", []))}
        for key in ("tools", "tool_choice", "chat_template", "chat_template_kwargs", "reasoning_format"):
            if key in body:
                payload[key] = deepcopy(body[key])
        return payload

    def _tokenize(self, content: str) -> int:
        result, decoded = self._json_request("/tokenize", "POST", {"content": content}, timeout=20)
        if result.status != 200 or not isinstance(decoded, Mapping) or not isinstance(decoded.get("tokens"), list):
            raise LocalBrainRuntimeError(f"tokenize returned HTTP {result.status} without a token list")
        return len(decoded["tokens"])

    def estimate_prompt(self, body: Mapping[str, Any]) -> PromptEstimate:
        image_count = _image_count(body.get("messages"))
        reserve = self.config.image_token_reserve
        try:
            template_result, template_body = self._json_request(
                "/apply-template", "POST", self._template_payload(body), timeout=20
            )
            prompt = template_body.get("prompt") if isinstance(template_body, Mapping) else None
            if template_result.status != 200 or not isinstance(prompt, str):
                raise LocalBrainRuntimeError(f"apply-template returned HTTP {template_result.status}")
            text_tokens = self._tokenize(prompt)
            method = "apply-template+tokenize"
        except LocalBrainRuntimeError:
            # The fallback removes image bytes before estimating; visual cost is
            # represented only by the centralized reserve below.
            text_tokens = _fallback_text_token_estimate(body)
            method = "fallback-redacted-text"

        total = text_tokens + image_count * reserve
        if image_count:
            method += "+image-reserve"
        estimate = PromptEstimate(total, image_count, reserve, method)
        self.last_estimate_method = method
        self.last_image_count = image_count
        self.last_image_token_reserve = reserve
        return estimate

    def _requested_output(self, body: Mapping[str, Any]) -> Any:
        if "max_tokens" in body and body.get("max_tokens") is not None:
            return body.get("max_tokens")
        if "max_completion_tokens" in body and body.get("max_completion_tokens") is not None:
            return body.get("max_completion_tokens")
        return None

    def _set_output_budget(self, body: dict[str, Any], budget: OutputBudget) -> None:
        # DSH's local provider explicitly uses max_tokens.  Remove the newer
        # alias when present so llama.cpp receives one unambiguous field.
        body["max_tokens"] = budget.max_output_tokens
        body.pop("max_completion_tokens", None)

    def prepare_chat_request(self, body: Mapping[str, Any], _legacy_client: Any = None) -> PreparedRequest:
        working = deepcopy(dict(body))
        try:
            explicit_max_tokens = coerce_optional_nonnegative_int(self._requested_output(working))
        except ValueError as error:
            raise LocalBrainRuntimeError(f"invalid max_tokens: {error}") from error
        reasoning_budget = resolve_reasoning_budget(working, self.config.reasoning_budgets)
        has_tools = request_has_tools(working)
        estimate = self.estimate_prompt(working)
        completion_reserve = estimate_completion_reserve(
            explicit_max_tokens=explicit_max_tokens,
            reasoning_budget=reasoning_budget,
            has_tools=has_tools,
        )
        required = required_context_tokens(
            prompt_tokens=estimate.tokens,
            completion_reserve=completion_reserve,
            safety_margin=self.config.safety_margin_tokens,
        )
        decision = self.context.decision(required)
        if decision.action == "compact":
            self.last_error = decision.reason
            raise ContextCapacityError(
                f"Local Qwen request requires {required} tokens, above the {self.config.max_physical_context} token ceiling; "
                "DSH Local Qwen compaction is required before retry"
            )
        if decision.action in {"promote", "demote"} and decision.target_context is not None:
            self.switch_context(decision.target_context)
        physical_context = decision.target_context or self.context.current_context
        try:
            effective_max_tokens = compute_effective_max_tokens(
                explicit_max_tokens=explicit_max_tokens,
                physical_ctx=physical_context,
                prompt_tokens=estimate.tokens,
                safety_margin=self.config.safety_margin_tokens,
                backend_completion_cap=self.config.backend_completion_cap,
            )
        except ValueError as error:
            self.last_error = str(error)
            requested = explicit_max_tokens or 0
            required_explicit = required_context_tokens(
                prompt_tokens=estimate.tokens,
                completion_reserve=requested,
                safety_margin=self.config.safety_margin_tokens,
            )
            raise ContextCapacityError(
                f"Local Qwen explicit max_tokens={requested} exceeds safe capacity for context "
                f"{physical_context} (requires {required_explicit} tokens)"
            ) from error
        budget = self.output.begin_request(explicit_max_tokens, effective_max_tokens)
        self._set_output_budget(working, budget)
        self.context.record_request(estimate.tokens, required)
        self.last_request_mode = budget.mode
        self.last_explicit_max_tokens = explicit_max_tokens
        self.last_completion_reserve = completion_reserve
        self.last_effective_max_tokens = effective_max_tokens
        self.last_reasoning_budget = reasoning_budget
        self.last_has_tools = has_tools
        return PreparedRequest(working, estimate, budget, decision)

    def _windows_path(self, path: str) -> str:
        if not path.startswith("/mnt/"):
            return path
        try:
            converted = subprocess.run(
                ["wslpath", "-w", path], capture_output=True, text=True, check=True, timeout=5
            ).stdout.strip()
            return converted or path
        except (OSError, subprocess.SubprocessError):
            return path

    def _restart_command(self, context: int) -> list[str]:
        helper = self.restart_helper
        if not helper:
            raise ContextSwitchError("QWEN_RESTART_HELPER is not configured; cannot switch physical context")
        if helper.lower().endswith(".ps1"):
            powershell = shutil.which("powershell.exe")
            if powershell is None:
                for candidate in (
                    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                    "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe",
                ):
                    if Path(candidate).is_file():
                        powershell = candidate
                        break
            if powershell is None:
                raise ContextSwitchError("powershell.exe is not available in PATH or the standard WSL interop path")
            return [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                self._windows_path(helper),
                "-ContextSize",
                str(context),
            ]
        return [helper, str(context)]

    def _run_restart(self, context: int) -> None:
        command = self._restart_command(context)
        self.log.info("switching Local Qwen physical context to %s", context)
        # Use a detached POSIX session and return as soon as the PowerShell
        # helper is spawned.  Waiting on a long-lived Windows interop child
        # would hold the relay request open until llama-server exits.
        self.last_restart = {
            "target_context": context,
            "command": command,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            self.last_restart["pid"] = process.pid
            # A malformed helper path normally fails immediately.  Give the
            # interop layer a short window to publish that failure, while
            # allowing a healthy helper to continue asynchronously.
            time.sleep(0.20)
            returncode = process.poll()
            self.last_restart["returncode"] = returncode
        except (OSError, subprocess.SubprocessError) as error:
            self.last_restart["error"] = str(error)
            raise ContextSwitchError(f"restart helper failed to run: {error}") from error
        if returncode not in (None, 0):
            raise ContextSwitchError(f"restart helper exited {returncode}")

    def _wait_for_context(self, context: int, timeout: float = 240.0) -> ServerProbe:
        deadline = time.monotonic() + timeout
        last = ServerProbe(False, None, False, "none", "not ready")
        while time.monotonic() < deadline:
            try:
                last = self.probe_server()
            except Exception as error:  # pragma: no cover - defensive around live process startup
                last = ServerProbe(False, None, False, "none", str(error))
            if last.healthy and last.context == context and not last.processing:
                self.server_healthy = True
                self.server_probe_source = last.source
                self.last_probe_error = last.error
                return last
            time.sleep(1.0)
        return last

    def switch_context(self, target_context: int) -> None:
        with self.switch_lock:
            old_context = self.context.current_context
            if target_context == old_context:
                return
            probe = self.refresh_server_state()
            if probe.processing:
                raise ContextSwitchError("upstream llama-server is processing a request; context switch deferred")
            try:
                self._run_restart(target_context)
                ready = self._wait_for_context(target_context)
                if not (ready.healthy and ready.context == target_context):
                    raise ContextSwitchError(
                        f"target context {target_context} not ready; observed {ready.context!r} ({ready.error or 'no error'})"
                    )
                self.context.record_switch(target_context)
                self.last_context_switch = datetime.now(timezone.utc).isoformat()
                self.last_error = None
            except LocalBrainRuntimeError as error:
                self.last_error = str(error)
                # Exactly one recovery attempt, as required by the runtime
                # contract.  Do not loop if rollback also fails.
                try:
                    self._run_restart(old_context)
                    rollback = self._wait_for_context(old_context)
                    if rollback.healthy and rollback.context == old_context:
                        self.context.current_context = old_context
                        self.server_healthy = True
                except LocalBrainRuntimeError as rollback_error:
                    self.last_error = f"{error}; rollback failed: {rollback_error}"
                raise

    def observe_response(self, response_bytes: bytes, request_mode: str | None = None, streamed: bool = False) -> dict[str, Any]:
        if len(response_bytes) > MAX_OBSERVE_BYTES:
            return {"observed": False, "reason": "response-too-large"}
        decoded: Mapping[str, Any] | None = None
        if streamed:
            aggregate: dict[str, Any] = {"choices": [{"finish_reason": None, "message": {}}]}
            choice = aggregate["choices"][0]
            message = choice["message"]
            for line in response_bytes.decode("utf-8", errors="ignore").splitlines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                for item_choice in item.get("choices", []) if isinstance(item, Mapping) else []:
                    if not isinstance(item_choice, Mapping):
                        continue
                    if item_choice.get("finish_reason") is not None:
                        choice["finish_reason"] = item_choice.get("finish_reason")
                    delta = item_choice.get("delta")
                    if not isinstance(delta, Mapping):
                        continue
                    for key in ("content", "reasoning_content"):
                        value = delta.get(key)
                        if value:
                            message[key] = str(message.get(key, "")) + str(value)
                    if isinstance(delta.get("tool_calls"), list):
                        message.setdefault("tool_calls", []).extend(delta["tool_calls"])
            decoded = aggregate
        else:
            try:
                item = json.loads(response_bytes.decode("utf-8"))
                if isinstance(item, Mapping):
                    decoded = item
            except (UnicodeDecodeError, ValueError):
                decoded = None
        if decoded is None:
            return {"observed": False, "reason": "non-json-response"}
        return {"observed": True, **self.output.observe_response(decoded, request_mode)}

    def status(self, _legacy_client: Any = None) -> dict[str, Any]:
        status = self.context.status()
        status.update(
            {
                "model": "li-huahua-local",
                "client_policy": "request-based",
                "output_capability_mode": "dynamic-safe-max",
                "output_reservation_mode": "request-based",
                "visible_reserve_no_tools": 4_096,
                "visible_reserve_with_tools": 8_192,
                "safety_margin": self.config.safety_margin_tokens,
                "reasoning_max": self.config.reasoning_budgets.get("max", 16_384),
                "fixed_normal_output_cap_active": False,
                "fixed_burst_output_cap_active": False,
                "last_explicit_max_tokens": self.last_explicit_max_tokens,
                "last_completion_reserve": self.last_completion_reserve,
                "last_effective_max_tokens": self.last_effective_max_tokens,
                "last_reasoning_budget": self.last_reasoning_budget,
                "last_has_tools": self.last_has_tools,
                "server_healthy": self.server_healthy,
                "server_context_source": self.server_probe_source,
                "server_probe_error": self.last_probe_error,
                "last_restart": deepcopy(self.last_restart),
                "last_context_switch": self.last_context_switch,
                "next_output_mode": self.output.next_output_mode,
                "last_output_mode": self.output.last_mode,
                "last_estimate_method": self.last_estimate_method,
                "last_image_count": self.last_image_count,
                "image_token_reserve": self.last_image_token_reserve,
                "image_reserve_calibrated": self.config.image_reserve_calibrated,
                "last_error": self.last_error,
            }
        )
        return status


__all__ = [
    "ContextCapacityError",
    "ContextSwitchError",
    "HTTPResult",
    "LocalBrainRuntime",
    "LocalBrainRuntimeError",
    "PreparedRequest",
    "PromptEstimate",
    "ServerProbe",
]
