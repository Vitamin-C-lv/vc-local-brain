# VC Local Brain

Public, source-only mirror of the VC Local Brain runtime and its DSH Local
Qwen integration. This repository is for review and continued source
maintenance; it is not a deployment bundle and contains no model weights,
runtime state, user conversations, memory databases, or credentials.

## Layout

- `runtime/` — Local Brain runtime, dynamic context manager, safe output policy,
  relay/normalization, and vision/WebP compatibility.
- `tests/` — focused runtime and context tests.
- `dsh/` — the Local Qwen Engineering Protocol bundle and the captured
  `llm-pi-ai` adapter source used by the Local Qwen route.
- `scripts/` — the dynamic-context fixture script and the exact-owner DSH Web
  restart helper source.
- `config/` — reviewable, credential-free configuration examples.
- `docs/CURRENT_STATE.md` — the current accepted source-state contract.

## Focused checks

From the repository root:

```bash
PYTHONPATH=runtime python3 -m unittest discover -s tests -p 'test_*.py'
```

The benchmark script is preserved as source for later review but is not run as
part of source-mirror publication.

## Scope rule

Changes here must remain source-only and must not silently modify the live DSH,
Local Brain, llama-server, Session, memory, Pet, or DeepSeek provider state.
