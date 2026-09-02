# DSH meow-memory route isolation

```text
PERSISTENT_MEMORY_POLICY=DEEPSEEK_OFFICIAL_ONLY
LOCAL_BRAIN_PERSISTENT_MEMORY=DISABLED
LEGACY_LOCAL_QWEN_PERSISTENT_MEMORY=DISABLED
UNKNOWN_PROVIDER_PERSISTENT_MEMORY=DISABLED
SESSION_CONTEXT_FOR_LOCAL_BRAIN=ENABLED
COMPACTION_FOR_LOCAL_BRAIN=ENABLED
```

Persistent meow-memory is an allowlist capability. The only allowed identity is
the production DSH adapter route `deepseek-official` with one of the current
official models:

- `deepseek-v4-flash`
- `deepseek-v4-pro`
- `deepseek-v4-flash-vision-exp`

The gate applies to every model-facing and backend-facing entry point:

- `system-prompt/assemble` removes the meow-memory guide, memory contexts, and
  `memory_*` tool schemas for denied routes.
- `agent/pre-step` skips first-turn and related-memory injection.
- `tools.guard()` denies any `memory_*` execution for denied or unknown routes.
- `agent/turn-stopping` skips reflection for denied routes.
- dream start, scheduling, command, and tool paths require the same allowlist.

The selected route is taken from the model-selection variables produced during
prompt assembly, then cached only for the current agent/session so later
pre-step, reflection, dream, and tool-execution callbacks reuse the same
identity. Missing or unrecognized route data remains deny-by-default.

This is deliberately separate from DSH conversation/session context. Local
Brain sessions retain normal conversation history and remain eligible for the
existing DSH compaction path; those features do not grant access to
persistent meow-memory.

The legacy identifiers `local-qwen/li-huahua-local` and the formal
`local-brain/local-brain-v1` route are both denied. Unknown provider/model
identities fail closed.
