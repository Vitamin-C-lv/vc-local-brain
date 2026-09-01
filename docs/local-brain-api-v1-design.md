# Local Brain API v1 Contract Design

Status: implementation contract for the first HTTP-boundary cutover
Baseline: `a727fdd`
Scope: reduce DSH/Pet caller complexity without changing model-runtime behavior

## 1. API positioning

Local Brain is an **inference abstraction layer**.

It is not an agent framework, memory system, workflow engine, or model router.
The v1 boundary hides the physical model, context supervision, output policy,
runtime recovery, and multimodal adaptation that callers should not have to
understand.

## 2. Current architecture audit

### A. DSH's current real entry point

The DSH `llm-pi-ai` provider currently selects the local provider profile
`local-qwen` with the legacy backend model alias `li-huahua-local`. Its
OpenAI-compatible `baseURL` is `http://127.0.0.1:17862/v1`, which reaches the
loopback `dsh-local-qwen-relay`.

The relay performs the existing image normalization and Qwen reasoning
translation, then calls the existing `LocalBrainRuntime`. The runtime remains
responsible for prompt estimation, physical context supervision, admission,
dynamic safe output, and recovery before forwarding to the Windows
llama-server upstream. This v1 work changes the HTTP projection at the relay
boundary only.

### B. Parameters DSH currently knows

The existing DSH integration currently needs the provider/profile identity,
the loopback base URL, OpenAI-completions compatibility, the legacy model
alias, and the request fields it already sends: `messages`, streaming state,
reasoning effort, tools, temperature, and output-token fields.

Those existing DSH fields continue to work for compatibility. They are not a
request for DSH to learn physical runtime details.

### C. Parameters that should be hidden

The v1 public boundary must hide physical context tiers and `n_ctx`, model
file and mmproj paths, KV-cache details, llama-server slots, restart commands,
backend model identity, reasoning budgets, image reserves, session/memory
storage, and internal queue/context diagnostics beyond the small status
projection defined below.

`metadata.source` is not a v1 requirement. The supplied contract removes
`metadata` before forwarding rather than making it a caller dependency.

### D. Minimum future Pet interface

Pet is not migrated by this change. When a future migration is authorized, its
minimum dependency should be the loopback Local Brain API at
`127.0.0.1:17862`: `GET /health` for readiness and
`POST /v1/chat/completions` for inference. Pet keeps conversation, memory,
personality, and prompt construction; it does not take ownership of context
tiers, llama lifecycle, or model files.

The current Pet direct-server/lifecycle integration remains unchanged in this
commit.

## 3. Responsibility boundary

| Caller-owned | Local Brain-owned |
| --- | --- |
| Conversation history | Inference execution |
| Memory | Physical model execution |
| Personality | Context management |
| Task planning | Runtime recovery |
| Prompt construction | Multimodal adaptation |

The contract is deliberately small. It does not add memory, agent, priority,
permission, or tool APIs.

## 4. Public v1 surface

The first stable surface contains exactly:

- `POST /v1/chat/completions`
- `GET /health`
- `GET /vc-local-brain/status`

No new service process is introduced. The existing loopback relay remains the
production entry point and projects the existing runtime into this contract.

## 5. Chat contract

`POST /v1/chat/completions` remains OpenAI-compatible at the wire boundary.

Stable v1 request fields are:

- required `messages` (a non-empty array)
- optional `stream` (boolean)
- optional `reasoning_effort` (`off`, `low`, `medium`, `high`, or `max`)

The `model` field may be omitted. If present, the stable public alias is
`local-brain-v1`. The legacy `li-huahua-local` alias is also accepted during
the DSH compatibility period, and both forms are normalized internally to the
currently deployed backend alias. DSH is not migrated to a new provider or
model profile in this change.

Existing DSH OpenAI-compatible extensions such as `tools`, `tool_choice`,
`temperature`, and output-token fields continue to pass through as
compatibility fields. They are not a promise that callers may control
physical runtime parameters.

Callers must not depend on `context_size`, `n_ctx`, context tiers, llama
arguments, mmproj, KV cache, model paths, or backend model filenames.

## 6. Health contract

Health is relay-owned and is a read-only readiness probe:

```http
GET /health
```

Ready response:

```json
{"status":"ok"}
```

Unavailable response:

```json
{"status":"unavailable"}
```

The relay checks current upstream health without entering inference
admission. Health never starts or restarts llama-server and never changes
physical context.

## 7. Status contract

Status is a small whitelist projection:

```http
GET /vc-local-brain/status
```

Example:

```json
{
  "status": "ready",
  "model": "local-brain-v1",
  "queue": {"active": 0, "waiting": 0, "capacity": 4}
}
```

The public response contains only readiness, the public model alias, and
queue active/waiting/capacity. It does **not** expose current context tier or
`n_ctx`, backend model identity, paths, PIDs, restart information, reasoning
budgets, image reserve, llama slots, session data, or memory data.

Status bypasses inference admission and refreshes the existing runtime probe
before producing the whitelist projection. It does not introduce a second
runtime or a second recovery path.

## 8. Error contract

Public errors use one envelope:

```json
{
  "error": {
    "message": "...",
    "type": "...",
    "code": "...",
    "retryable": false
  }
}
```

The first cutover keeps existing DSH-visible error codes, including context
and admission codes. `retryable` is added as a stable boolean field. Only the
public upstream-network error is generalized from the legacy Qwen-specific
name to `LOCAL_BRAIN_UPSTREAM_ERROR`; context and admission algorithms and
their codes are not renamed.

Invalid JSON, invalid request objects, unsupported model aliases, and invalid
reasoning effort are rejected at the contract boundary before an upstream
call. Upstream failures do not expose gateway details, Windows paths, or
backend-specific error text.

## 9. Request correlation

Every inbound request receives a server-generated opaque request ID. JSON
responses and proxied/streaming responses return it in:

```http
X-Local-Brain-Request-ID: lb-...
```

The ID is for local log correlation and debugging only; this is not a full
tracing system. Minimal server logs may contain request ID, method, path,
status, and duration. They must not contain prompts, messages, images, tool
results, credentials, or session contents.

## 10. Implementation design

The existing relay is the contract projection:

```text
DSH llm-pi-ai  ----\
Pet brain       -----> 127.0.0.1:17862
                         Local Brain API v1 contract projection
                         existing LocalBrainRuntime
                         llama.cpp / Qwen backend
```

The minimal implementation is:

1. Add the supplied pure `runtime/local_brain_contract.py` module.
2. Normalize and validate public chat input at the relay boundary.
3. Keep image normalization, reasoning translation, and existing runtime
   preparation in their current order after contract normalization.
4. Project Health and Status before inference admission.
5. Add the request-ID response header to JSON and proxied responses.
6. Project existing errors into the public envelope.

There is no `LocalBrainService` wrapper, new process, configuration center,
database, or model-router layer. `LocalBrainRuntime`, `ContextManager`, the
admission gate, output policy, reasoning mapping, vision handling, compaction,
the DSH adapter, the Engineering Protocol, the Pet production tree, the
Windows launcher, and the systemd production path remain outside this
contract-only change.

## 11. Validation boundary

Validation covers the supplied contract tests plus relay-boundary tests and
preserves the existing runtime/context/admission suite. It does not benchmark,
stress context switching, exercise long output, migrate Pet, or change DSH
provider/model identity.

The live cutover, after tests pass, is limited to synchronizing the changed
relay-boundary source files and gracefully reloading
`dsh-local-qwen-relay`. llama-server, DSH Web, Pet, session data, and memory
data are preserved.
