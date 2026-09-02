# Current source state

This document records the accepted Local Qwen source contract at mirror
publication time. It is not a live health check and does not contain runtime
state.

```text
MODEL_ALIAS=li-huahua-local
MODEL_DISPLAY=Qwen3.5-4B Q4_K_M (Local)
DSH_COMPAT_ROUTE=provider=local-qwen model=li-huahua-local
PUBLIC_LOCAL_BRAIN_API_MODEL=local-brain-v1
BACKEND_MODEL_ALIAS=li-huahua-local
CONTEXT_TIERS=16384,32768,65536,98304,131072
GLOBAL_MAX_CONTEXT=131072
CLIENT_SPECIFIC_POLICY=NONE
DYNAMIC_CONTEXT=ENABLED
DYNAMIC_SAFE_OUTPUT=ENABLED
NORMAL_MAX_OUTPUT=32768
NORMAL_MAX_OUTPUT_STATUS=LEGACY_INACTIVE
BURST_MAX_OUTPUT=65536
BURST_MAX_OUTPUT_STATUS=LEGACY_INACTIVE
FIXED_NORMAL_OUTPUT_CAP_ACTIVE=NO
FIXED_BURST_OUTPUT_CAP_ACTIVE=NO
OUTPUT_CAPABILITY_MODE=DYNAMIC_SAFE_MAX
REASONING_LEVELS=off:0,low:512,medium:2048,high:8192,max:16384
LOCAL_COMPACTION_PROVIDER=local-qwen
LOCAL_COMPACTION_MODEL=li-huahua-local
LOCAL_COMPACTION_THRESHOLD_RATIO=0.68
LOCAL_COMPACTION_RETAIN_RATIO=0.15
LOCAL_COMPACTION_MAX_TOKENS=8192
EXPECTED_POST_COMPACTION_PROMPT=~48K
EXPECTED_POST_COMPACTION_TIER=C96
ENGINEERING_PROTOCOL=v1 experimental
OLD_REASONING_REPLAYED_IN_COMPACTION=YES
OLD_HUGE_TOOL_RESULTS_REPLAYED=YES
OLD_IMAGES_REPROCESSED=YES
DEEPSEEK_GLOBAL_COMPACTION_POLICY=UNCHANGED
WINDOWS_LLAMA_LAUNCHER=D:\VC-AI-Pet\runtime\Start-LocalQwen.ps1
RELAY_SYSTEMD_UNIT=dsh-local-qwen-relay.service
RELAY_SYSTEMD_UNIT_KIND=TRANSIENT
RELAY_EXECSTART=/usr/bin/python3 /home/vitamin_c/Documents/Codex/2026-08-31/kali-dsh-windows-qwen3-5-4b/work/dsh_local_qwen_relay.py
RELAY_ENVIRONMENT=NONE
RELAY_ENVIRONMENT_FILE=NONE
QWEN_RESTART_HELPER=/mnt/d/VC-AI-Pet/runtime/Start-LocalQwen.ps1
```

The Engineering Protocol overlay is installed for the exact Local Qwen route
only. The Memory Garden acceptance work exposed patch-loop and false-pass
limitations; further changes are frozen pending source review.

The current DSH route is a compatibility identity for the exact
`local-qwen/li-huahua-local` backend pair. The public Local Brain API identity
is `local-brain-v1`; a later D2 migration may move callers to that public
identity without changing the backend alias documented above. The
`LOCAL_COMPACTION_*` fields describe DSH's upper-layer conversation
compaction policy, not the Local Brain Runtime policy.
