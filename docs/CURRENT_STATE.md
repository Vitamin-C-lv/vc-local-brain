# Current source state

This document records the accepted Local Qwen source contract at mirror
publication time. It is not a live health check and does not contain runtime
state.

```text
MODEL_ALIAS=li-huahua-local
MODEL_DISPLAY=Qwen3.5-4B Q4_K_M (Local)
CONTEXT_TIERS=16384,32768,65536,98304,131072
GLOBAL_MAX_CONTEXT=131072
CLIENT_SPECIFIC_POLICY=NONE
DYNAMIC_CONTEXT=ENABLED
DYNAMIC_SAFE_OUTPUT=ENABLED
NORMAL_MAX_OUTPUT=32768
BURST_MAX_OUTPUT=65536
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
```

The Engineering Protocol overlay is installed for the exact Local Qwen route
only. The Memory Garden acceptance work exposed patch-loop and false-pass
limitations; further changes are frozen pending source review.
