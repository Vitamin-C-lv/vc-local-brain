from __future__ import annotations

from copy import deepcopy
import uuid
from typing import Any, Mapping


PUBLIC_MODEL_ALIAS = "local-brain-v1"
BACKEND_MODEL_ALIAS = "li-huahua-local"
REQUEST_ID_HEADER = "X-Local-Brain-Request-ID"
REASONING_EFFORTS = frozenset({"off", "low", "medium", "high", "max"})


class ContractError(ValueError):
    """Invalid public Local Brain v1 request."""

    status_code = 400
    error_code = "LOCAL_BRAIN_INVALID_REQUEST"
    error_type = "invalid_request_error"
    retryable = False


def new_request_id() -> str:
    """Create an opaque request id used only for local diagnostics."""
    return f"lb-{uuid.uuid4().hex}"


def normalize_chat_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the small public v1 contract and hide backend model identity.

    All OpenAI-compatible extension fields not owned by this contract are kept
    untouched for DSH compatibility.  The public contract itself guarantees
    only messages, optional stream, optional reasoning_effort, and the stable
    service alias.
    """
    if not isinstance(payload, Mapping):
        raise ContractError("request body must be a JSON object")

    body = deepcopy(dict(payload))
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ContractError("messages must be a non-empty array")

    model = body.get("model")
    if model not in (None, "", PUBLIC_MODEL_ALIAS, BACKEND_MODEL_ALIAS):
        raise ContractError(
            f"unsupported model alias {model!r}; use {PUBLIC_MODEL_ALIAS!r} or omit model"
        )
    # Local Brain is a single-model service.  Public callers never choose the
    # backend model; normalize to the currently deployed backend alias here.
    body["model"] = BACKEND_MODEL_ALIAS

    if "stream" in body and not isinstance(body["stream"], bool):
        raise ContractError("stream must be a boolean")

    effort = body.get("reasoning_effort")
    if effort is not None:
        if not isinstance(effort, str) or effort.lower() not in REASONING_EFFORTS:
            raise ContractError(
                "reasoning_effort must be one of off, low, medium, high, max"
            )
        body["reasoning_effort"] = effort.lower()

    # v1 deliberately has no client identity / priority / memory contract.
    # If an experimental caller sends metadata, do not forward it to llama.
    body.pop("metadata", None)
    return body


_RETRYABLE_BY_CODE = {
    "LOCAL_BRAIN_QUEUE_FULL": True,
    "LOCAL_CONTEXT_SWITCH_FAILED": True,
    "LOCAL_CONTEXT_COMPACTION_REQUIRED": False,
    "LOCAL_BRAIN_INCOMPLETE_REQUEST_BODY": False,
    "LOCAL_BRAIN_INVALID_REQUEST": False,
    "LOCAL_BRAIN_UPSTREAM_ERROR": True,
    "LOCAL_BRAIN_RUNTIME_ERROR": True,
}


def public_error_payload(error: BaseException) -> dict[str, Any]:
    """Project an internal error to the stable, small public error envelope."""
    code = str(getattr(error, "error_code", "LOCAL_BRAIN_RUNTIME_ERROR"))
    retryable = getattr(error, "retryable", _RETRYABLE_BY_CODE.get(code, False))
    return {
        "error": {
            "message": str(error),
            "type": str(getattr(error, "error_type", "local_brain_runtime_error")),
            "code": code,
            "retryable": bool(retryable),
        }
    }


def public_status(internal: Mapping[str, Any]) -> dict[str, Any]:
    """Return only fields DSH/Pet may safely depend on long term."""
    ready = bool(internal.get("server_healthy"))
    return {
        "status": "ready" if ready else "unavailable",
        "model": PUBLIC_MODEL_ALIAS,
        "queue": {
            "active": int(internal.get("active_requests") or 0),
            "waiting": int(internal.get("queue_depth") or 0),
            "capacity": int(internal.get("queue_capacity") or 0),
        },
    }


def public_health(healthy: bool) -> tuple[int, dict[str, str]]:
    if healthy:
        return 200, {"status": "ok"}
    return 503, {"status": "unavailable"}


__all__ = [
    "BACKEND_MODEL_ALIAS",
    "ContractError",
    "PUBLIC_MODEL_ALIAS",
    "REASONING_EFFORTS",
    "REQUEST_ID_HEADER",
    "new_request_id",
    "normalize_chat_request",
    "public_error_payload",
    "public_health",
    "public_status",
]
