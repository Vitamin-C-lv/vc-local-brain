"""Pure policy/state helpers for the Local Qwen runtime.

The relay owns HTTP and process supervision.  This module deliberately keeps
the context, output and reasoning decisions deterministic and dependency-free
so they can also be reused by the future Pet client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Any, Iterable, Mapping


GLOBAL_CONTEXT_TIERS = (16_384, 32_768, 65_536, 98_304, 131_072)
PHYSICAL_CONTEXT_TIERS = GLOBAL_CONTEXT_TIERS
MAX_PHYSICAL_CONTEXT = 131_072
SAFETY_MARGIN_TOKENS = 8_192

VISIBLE_OUTPUT_RESERVE_NO_TOOLS = 4_096
VISIBLE_OUTPUT_RESERVE_WITH_TOOLS = 8_192

REASONING_BUDGETS = {
    "off": 0,
    "low": 512,
    "medium": 2_048,
    "high": 8_192,
    "max": 16_384,
}

DEFAULT_IMAGE_TOKEN_RESERVE = 4_096
IMAGE_RESERVE_SAFETY_FACTOR = 1.20

IDLE_DEMOTE_SECONDS = 900.0
ACTIVE_DEMOTION_MIN_INTERVAL_SECONDS = 180.0
ACTIVE_DEMOTION_PRESSURE_THRESHOLD = 80_000
ACTIVE_DEMOTION_STREAK = 2


def _as_positive_int(value: Any, default: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _as_float(value: Any, default: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


@dataclass(frozen=True)
class RuntimeConfig:
    physical_context_tiers: tuple[int, ...] = PHYSICAL_CONTEXT_TIERS
    reasoning_budgets: Mapping[str, int] = field(default_factory=lambda: dict(REASONING_BUDGETS))
    safety_margin_tokens: int = SAFETY_MARGIN_TOKENS
    max_physical_context: int = MAX_PHYSICAL_CONTEXT
    backend_completion_cap: int | None = None
    image_token_reserve: int = DEFAULT_IMAGE_TOKEN_RESERVE
    image_reserve_calibrated: bool = False
    idle_demote_seconds: float = IDLE_DEMOTE_SECONDS
    active_demotion_min_interval_seconds: float = ACTIVE_DEMOTION_MIN_INTERVAL_SECONDS
    active_demotion_pressure_threshold: int = ACTIVE_DEMOTION_PRESSURE_THRESHOLD
    active_demotion_streak: int = ACTIVE_DEMOTION_STREAK

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RuntimeConfig":
        reasoning = raw.get("reasoning") if isinstance(raw.get("reasoning"), Mapping) else {}
        demotion = raw.get("demotion") if isinstance(raw.get("demotion"), Mapping) else {}
        image = raw.get("image") if isinstance(raw.get("image"), Mapping) else {}

        def tiers(value: Any, fallback: tuple[int, ...]) -> tuple[int, ...]:
            if not isinstance(value, (list, tuple)):
                return fallback
            parsed = tuple(sorted({_as_positive_int(item, 0) for item in value if _as_positive_int(item, 0)}))
            return parsed or fallback

        max_context = _as_positive_int(raw.get("max_physical_context"), MAX_PHYSICAL_CONTEXT)
        physical = tuple(
            tier for tier in tiers(raw.get("physical_context_tiers"), PHYSICAL_CONTEXT_TIERS)
            if tier <= max_context
        ) or (max_context,)
        backend_cap = raw.get("backend_completion_cap")
        if backend_cap is not None:
            backend_cap = _as_positive_int(backend_cap, 0) or None
        return cls(
            physical_context_tiers=physical,
            reasoning_budgets={
                key: _as_positive_int(reasoning.get(key), default)
                for key, default in REASONING_BUDGETS.items()
            },
            safety_margin_tokens=_as_positive_int(raw.get("safety_margin_tokens"), SAFETY_MARGIN_TOKENS),
            max_physical_context=max_context,
            backend_completion_cap=backend_cap,
            image_token_reserve=_as_positive_int(image.get("token_reserve"), DEFAULT_IMAGE_TOKEN_RESERVE),
            image_reserve_calibrated=bool(image.get("calibrated", False)),
            idle_demote_seconds=_as_float(demotion.get("idle_seconds"), IDLE_DEMOTE_SECONDS),
            active_demotion_min_interval_seconds=_as_float(
                demotion.get("active_min_interval_seconds"), ACTIVE_DEMOTION_MIN_INTERVAL_SECONDS
            ),
            active_demotion_pressure_threshold=_as_positive_int(
                demotion.get("pressure_threshold"), ACTIVE_DEMOTION_PRESSURE_THRESHOLD
            ),
            active_demotion_streak=_as_positive_int(demotion.get("streak"), ACTIVE_DEMOTION_STREAK),
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "RuntimeConfig":
        config_path = Path(path) if path else Path(__file__).with_name("local_brain_config.json")
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return cls()
        return cls.from_mapping(raw) if isinstance(raw, Mapping) else cls()

    def allowed_context_tiers(self, _legacy_client: Any = None) -> tuple[int, ...]:
        """Return the single request-based tier set.

        The optional argument is accepted only for source compatibility with
        the v1.0 relay.  It is deliberately ignored: client identity cannot
        affect context selection.
        """
        return self.physical_context_tiers


def coerce_optional_nonnegative_int(value: Any) -> int | None:
    """Coerce an optional request integer without inventing a default."""
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"expected a non-negative integer, got {value!r}") from error
    if parsed < 0:
        raise ValueError(f"expected a non-negative integer, got {value!r}")
    return parsed


def resolve_reasoning_budget(
    request: Mapping[str, Any],
    reasoning_budgets: Mapping[str, int] = REASONING_BUDGETS,
) -> int:
    """Resolve the already-normalized reasoning budget for one request."""
    explicit_budget = request.get("thinking_budget_tokens")
    if explicit_budget is not None:
        return coerce_optional_nonnegative_int(explicit_budget) or 0
    effort = request.get("reasoning_effort")
    if isinstance(effort, str):
        return int(reasoning_budgets.get(effort.lower(), 0))
    kwargs = request.get("chat_template_kwargs")
    if isinstance(kwargs, Mapping) and kwargs.get("enable_thinking") is False:
        return 0
    return 0


def request_has_tools(request: Mapping[str, Any]) -> bool:
    """Whether a request carries a non-empty tools schema."""
    tools = request.get("tools")
    if isinstance(tools, Mapping):
        return bool(tools)
    return isinstance(tools, (list, tuple)) and any(bool(tool) for tool in tools)


def estimate_completion_reserve(
    *,
    explicit_max_tokens: int | None,
    reasoning_budget: int,
    has_tools: bool,
) -> int:
    """Estimate completion space only for physical-tier selection.

    This reservation is not the model's output cap.  An explicit caller limit
    is already a total completion allowance and must not be combined with the
    reasoning budget a second time.
    """
    if explicit_max_tokens is not None:
        return coerce_optional_nonnegative_int(explicit_max_tokens) or 0
    visible_reserve = VISIBLE_OUTPUT_RESERVE_WITH_TOOLS if has_tools else VISIBLE_OUTPUT_RESERVE_NO_TOOLS
    return max(0, int(reasoning_budget)) + visible_reserve


def required_context_tokens(
    *,
    prompt_tokens: int,
    completion_reserve: int,
    safety_margin: int = SAFETY_MARGIN_TOKENS,
) -> int:
    """Return the physical context needed before sending a request."""
    return max(
        0,
        _as_positive_int(prompt_tokens, 0)
        + _as_positive_int(completion_reserve, 0)
        + _as_positive_int(safety_margin, SAFETY_MARGIN_TOKENS),
    )


def calculate_required_context(
    estimated_prompt_tokens: int,
    effective_max_output_tokens: int,
    safety_margin_tokens: int = SAFETY_MARGIN_TOKENS,
) -> int:
    """v1.0 compatibility wrapper for the request-based calculation."""
    return required_context_tokens(
        prompt_tokens=estimated_prompt_tokens,
        completion_reserve=effective_max_output_tokens,
        safety_margin=safety_margin_tokens,
    )


def choose_context_tier(
    required_context: int,
    available_tiers: Iterable[int] = GLOBAL_CONTEXT_TIERS,
) -> int | None:
    required = _as_positive_int(required_context, 0)
    tiers = sorted({_as_positive_int(tier, 0) for tier in available_tiers if _as_positive_int(tier, 0)})
    return next((tier for tier in tiers if tier >= required), None)


def safe_completion_capacity(
    *,
    physical_ctx: int,
    prompt_tokens: int,
    safety_margin: int = SAFETY_MARGIN_TOKENS,
) -> int:
    """Return all completion space safely available in a chosen tier."""
    return max(
        0,
        _as_positive_int(physical_ctx, 0)
        - _as_positive_int(prompt_tokens, 0)
        - _as_positive_int(safety_margin, SAFETY_MARGIN_TOKENS),
    )


def compute_effective_max_tokens(
    *,
    explicit_max_tokens: int | None,
    physical_ctx: int,
    prompt_tokens: int,
    safety_margin: int = SAFETY_MARGIN_TOKENS,
    backend_completion_cap: int | None = None,
) -> int:
    """Resolve actual outbound max_tokens after physical-tier selection.

    An omitted caller limit receives the complete safe capacity of the chosen
    tier. An explicit limit is preserved exactly or rejected; it is never
    silently clamped.
    """
    safe_max = safe_completion_capacity(
        physical_ctx=physical_ctx,
        prompt_tokens=prompt_tokens,
        safety_margin=safety_margin,
    )
    if backend_completion_cap is not None:
        safe_max = min(safe_max, coerce_optional_nonnegative_int(backend_completion_cap) or 0)
    if explicit_max_tokens is not None:
        requested = coerce_optional_nonnegative_int(explicit_max_tokens) or 0
        if requested > safe_max:
            raise ValueError(
                f"explicit max_tokens={requested} exceeds safe completion capacity={safe_max}"
            )
        return requested
    return safe_max


@dataclass(frozen=True)
class OutputBudget:
    mode: str
    max_output_tokens: int


def _choice_from_response(response: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        return choices[0]
    return {}


def _message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for item in message:
            if isinstance(item, Mapping) and item.get("type") in {"text", "input_text"}:
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


def classify_completion(response: Mapping[str, Any]) -> dict[str, bool | str]:
    """Classify a completed chat response for the one-shot burst guard."""
    choice = _choice_from_response(response)
    message = choice.get("message") if isinstance(choice.get("message"), Mapping) else choice
    content = _message_text(message.get("content")) if isinstance(message, Mapping) else ""
    tool_calls = message.get("tool_calls") if isinstance(message, Mapping) else None
    reasoning = ""
    if isinstance(message, Mapping):
        reasoning = _message_text(message.get("reasoning_content") or message.get("reasoning"))
    finish_reason = str(choice.get("finish_reason") or "").lower()
    length_stop = finish_reason in {"length", "max_tokens", "max-tokens"}
    has_tool_call = isinstance(tool_calls, list) and bool(tool_calls)
    has_visible_output = bool(content.strip())
    reasoning_only = bool(reasoning.strip()) and not has_visible_output and not has_tool_call
    substantive = has_tool_call or has_visible_output
    return {
        "length_stop": length_stop,
        "has_tool_call": has_tool_call,
        "has_visible_output": has_visible_output,
        "reasoning_only": reasoning_only,
        "substantive": substantive,
        "finish_reason": finish_reason,
    }


class OutputBurstState:
    """Track request/output observations without imposing a fixed cap."""

    def __init__(self, config: RuntimeConfig | None = None):
        self.config = config or RuntimeConfig()
        self._burst_pending = False
        self.last_mode = "dynamic"
        self.last_classification: dict[str, bool | str] = {}

    @property
    def next_output_mode(self) -> str:
        return "dynamic"

    def begin_request(
        self,
        requested_max_output: Any = None,
        effective_max_output: Any = None,
    ) -> OutputBudget:
        """Record a dynamic or explicit request after its tier is selected."""
        requested = coerce_optional_nonnegative_int(requested_max_output)
        effective = coerce_optional_nonnegative_int(effective_max_output)
        mode = "explicit" if requested is not None else "dynamic"
        self.last_mode = mode
        self._burst_pending = False
        return OutputBudget(mode, effective if effective is not None else requested or 0)

    def observe_response(self, response: Mapping[str, Any], request_mode: str | None = None) -> dict[str, bool | str]:
        classification = classify_completion(response)
        self.last_classification = classification
        self._burst_pending = False
        return classification

    def status(self) -> dict[str, Any]:
        return {
            "next_output_mode": self.next_output_mode,
            "last_output_mode": self.last_mode,
            "last_classification": dict(self.last_classification),
        }


@dataclass(frozen=True)
class ContextDecision:
    required_context: int
    current_context: int
    target_context: int | None
    action: str
    reason: str


class ContextManager:
    """Promotion/demotion policy; process restart is supplied by the relay."""

    def __init__(
        self,
        current_context: int = MAX_PHYSICAL_CONTEXT,
        config: RuntimeConfig | None = None,
        clock=time.monotonic,
    ):
        self.config = config or RuntimeConfig()
        self.current_context = _as_positive_int(current_context, MAX_PHYSICAL_CONTEXT)
        self.clock = clock
        now = self.clock()
        self.last_request_at = now
        self.last_switch_at: float | None = None
        self.last_prompt_pressure = 0
        self.last_required_context = 0
        self._low_pressure_streak = 0
        self._pressure_drop_eligible = False

    def decision(
        self,
        required_context: int,
        _legacy_client: Any = None,
        *,
        now: float | None = None,
        post_compaction: bool = False,
    ) -> ContextDecision:
        required = _as_positive_int(required_context, 0)
        current = self.current_context
        target = choose_context_tier(required, self.config.allowed_context_tiers())
        if target is None or required > self.config.max_physical_context:
            return ContextDecision(required, current, None, "compact", "max_physical_context_reached")
        if target > current:
            return ContextDecision(required, current, target, "promote", "required_context_exceeds_current")
        if target == current:
            return ContextDecision(required, current, target, "keep", "current_tier_satisfies_request")

        timestamp = self.clock() if now is None else now
        idle = timestamp - self.last_request_at
        last_switch = self.last_switch_at if self.last_switch_at is not None else float("-inf")
        interval_ok = timestamp - last_switch >= self.config.active_demotion_min_interval_seconds
        # `required_context` already includes the completion reserve and the
        # safety margin.  Do not subtract the margin a second time here.
        safe_for_demotion = required <= target

        if idle >= self.config.idle_demote_seconds and safe_for_demotion:
            self._low_pressure_streak = 0
            return ContextDecision(required, current, target, "demote", "server_idle_long_enough")

        if (
            post_compaction
            and self._pressure_drop_eligible
            and required <= target * 0.80
            and interval_ok
        ):
            self._low_pressure_streak += 1
        else:
            self._low_pressure_streak = 0
        if self._low_pressure_streak >= self.config.active_demotion_streak and safe_for_demotion:
            self._low_pressure_streak = 0
            return ContextDecision(required, current, target, "demote", "post_compaction_pressure_drop")
        return ContextDecision(required, current, current, "keep", "demotion_hysteresis")

    def record_request(self, prompt_pressure: int, required_context: int, now: float | None = None) -> None:
        pressure = _as_positive_int(prompt_pressure, 0)
        if pressure >= self.config.active_demotion_pressure_threshold:
            self._pressure_drop_eligible = True
            self._low_pressure_streak = 0
        self.last_prompt_pressure = pressure
        self.last_required_context = _as_positive_int(required_context, 0)
        self.last_request_at = self.clock() if now is None else now

    def record_switch(self, context: int, now: float | None = None) -> None:
        self.current_context = _as_positive_int(context, self.current_context)
        self.last_switch_at = self.clock() if now is None else now
        self.last_request_at = self.last_switch_at
        self._pressure_drop_eligible = False
        self._low_pressure_streak = 0

    def status(self, _legacy_client: Any = None) -> dict[str, Any]:
        return {
            "current_context": self.current_context,
            "allowed_contexts": list(self.config.allowed_context_tiers()),
            "max_context": self.config.max_physical_context,
            "last_prompt_estimate": self.last_prompt_pressure,
            "last_required_context": self.last_required_context,
            "last_context_switch": self.last_switch_at,
        }


__all__ = [
    "ACTIVE_DEMOTION_MIN_INTERVAL_SECONDS",
    "GLOBAL_CONTEXT_TIERS",
    "ContextDecision",
    "ContextManager",
    "DEFAULT_IMAGE_TOKEN_RESERVE",
    "OutputBurstState",
    "PHYSICAL_CONTEXT_TIERS",
    "REASONING_BUDGETS",
    "RuntimeConfig",
    "SAFETY_MARGIN_TOKENS",
    "calculate_required_context",
    "choose_context_tier",
    "classify_completion",
    "coerce_optional_nonnegative_int",
    "compute_effective_max_tokens",
    "estimate_completion_reserve",
    "request_has_tools",
    "required_context_tokens",
    "resolve_reasoning_budget",
    "safe_completion_capacity",
]
