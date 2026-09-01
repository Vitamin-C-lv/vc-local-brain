import json
import unittest

from local_brain_runtime import (
    ContextCapacityError,
    ContextSwitchError,
    HTTPResult,
    LocalBrainRuntime,
    LocalBrainRuntimeError,
    ServerProbe,
    _fallback_text_token_estimate,
)


class FakeRuntime(LocalBrainRuntime):
    def __init__(self, token_count=35_000):
        super().__init__(probe=False)
        self.context.current_context = 65_536
        self.token_count = token_count
        self.switches = []

    def probe_server(self):
        return ServerProbe(True, self.context.current_context, False, "fake")

    def _json_request(self, path, method="GET", payload=None, timeout=12.0):
        if path == "/apply-template":
            return HTTPResult(200, {}, b""), {"prompt": "fake-template"}
        if path == "/tokenize":
            return HTTPResult(200, {}, b""), {"tokens": [0] * self.token_count}
        raise AssertionError(f"unexpected fake request: {method} {path}")

    def switch_context(self, target_context):
        self.switches.append(target_context)
        self.context.record_switch(target_context)


class FailingSwitchRuntime(LocalBrainRuntime):
    def __init__(self):
        super().__init__(probe=False)
        self.context.current_context = 65_536
        self.run_calls = []

    def refresh_server_state(self):
        return ServerProbe(True, self.context.current_context, False, "fake")

    def _run_restart(self, context):
        self.run_calls.append(context)
        if context == 98_304:
            raise ContextSwitchError("synthetic target failure")

    def _wait_for_context(self, context, timeout=240.0):
        return ServerProbe(True, context, False, "fake")


class ProbeRuntime(LocalBrainRuntime):
    def __init__(self, probes, token_count=1_000):
        super().__init__(probe=False)
        self.context.current_context = 65_536
        self.probes = list(probes)
        self.token_count = token_count
        self.run_calls = []
        self.wait_calls = []

    def _json_request(self, path, method="GET", payload=None, timeout=12.0):
        if path == "/apply-template":
            return HTTPResult(200, {}, b""), {"prompt": "fake-template"}
        if path == "/tokenize":
            return HTTPResult(200, {}, b""), {"tokens": [0] * self.token_count}
        raise AssertionError(f"unexpected fake request: {method} {path}")

    def probe_server(self):
        if self.probes:
            return self.probes.pop(0)
        return ServerProbe(False, None, False, "fake", "no more probes")

    def _run_restart(self, context):
        self.run_calls.append(context)

    def _wait_for_context(self, context, timeout=240.0):
        self.wait_calls.append(context)
        return ServerProbe(True, context, False, "fake")


class FallbackRuntime(LocalBrainRuntime):
    def __init__(self):
        super().__init__(probe=False)

    def _json_request(self, path, method="GET", payload=None, timeout=12.0):
        raise LocalBrainRuntimeError("synthetic unavailable tokenizer")


class LocalBrainRuntimeTests(unittest.TestCase):
    def test_apply_template_tokenize_and_image_reserve(self):
        runtime = FakeRuntime(token_count=12_000)
        prepared = runtime.prepare_chat_request(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "hello"},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                        ],
                    }
                ]
            }
        )
        self.assertEqual(prepared.estimate.tokens, 14_048)
        self.assertEqual(prepared.estimate.method, "apply-template+tokenize+image-reserve")
        self.assertEqual(prepared.body["max_tokens"], 43_296)
        self.assertEqual(prepared.budget.mode, "dynamic")

    def test_prompt_pressure_promotes_to_smallest_satisfying_tier(self):
        runtime = FakeRuntime(token_count=70_000)
        prepared = runtime.prepare_chat_request(
            {
                "messages": [{"role": "user", "content": "large"}],
                "reasoning_effort": "max",
                "tools": [{"type": "function"}],
            }
        )
        self.assertEqual(prepared.decision.action, "promote")
        self.assertEqual(prepared.decision.target_context, 131_072)
        self.assertEqual(runtime.switches, [131_072])

    def test_max_ceiling_requests_compaction(self):
        runtime = FakeRuntime(token_count=120_000)
        with self.assertRaises(ContextCapacityError) as caught:
            runtime.prepare_chat_request(
                {
                    "messages": [{"role": "user", "content": "too large"}],
                    "reasoning_effort": "max",
                    "tools": [{"type": "function"}],
                }
            )
        self.assertEqual(runtime.switches, [])
        self.assertIn("Local Brain request exceeds the maximum physical context capacity", str(caught.exception))
        self.assertNotIn("DSH", str(caught.exception))
        self.assertEqual(caught.exception.error_code, "LOCAL_CONTEXT_COMPACTION_REQUIRED")

    def test_dsh_like_request_uses_tools_and_reasoning_reservation(self):
        runtime = FakeRuntime(token_count=13_000)
        prepared = runtime.prepare_chat_request(
            {
                "messages": [{"role": "user", "content": "agent"}],
                "reasoning_effort": "max",
                "tools": [{"type": "function"}],
            }
        )
        self.assertEqual(prepared.decision.target_context, 65_536)
        self.assertEqual(prepared.budget.max_output_tokens, 44_344)

    def test_explicit_50k_request_selects_c64_and_preserves_value(self):
        runtime = FakeRuntime(token_count=1_000)
        prepared = runtime.prepare_chat_request(
            {"messages": [{"role": "user", "content": "OK"}], "max_tokens": 50_000}
        )
        self.assertEqual(prepared.decision.target_context, 65_536)
        self.assertEqual(prepared.budget.mode, "explicit")
        self.assertEqual(prepared.body["max_tokens"], 50_000)

    def test_max_completion_tokens_alias_is_preserved_as_explicit(self):
        runtime = FakeRuntime(token_count=1_000)
        prepared = runtime.prepare_chat_request(
            {"messages": [{"role": "user", "content": "OK"}], "max_completion_tokens": 50_000}
        )
        self.assertEqual(prepared.body["max_tokens"], 50_000)
        self.assertNotIn("max_completion_tokens", prepared.body)

    def test_client_identity_cannot_change_runtime_selection(self):
        pet = FakeRuntime(token_count=40_000)
        dsh = FakeRuntime(token_count=40_000)
        pet_request = pet.prepare_chat_request({"messages": [{"role": "user", "content": "same"}]}, "pet")
        dsh_request = dsh.prepare_chat_request({"messages": [{"role": "user", "content": "same"}]}, "dsh")
        self.assertEqual(pet_request.decision.target_context, dsh_request.decision.target_context)
        self.assertEqual(pet_request.body["max_tokens"], dsh_request.body["max_tokens"])

    def test_failed_switch_rolls_back_once(self):
        runtime = FailingSwitchRuntime()
        with self.assertRaises(ContextSwitchError):
            runtime.switch_context(98_304)
        self.assertEqual(runtime.run_calls, [98_304, 65_536])
        self.assertEqual(runtime.context.current_context, 65_536)

    def test_stale_context_cache_forces_switch_to_target(self):
        runtime = ProbeRuntime([ServerProbe(True, 131_072, False, "slots")])

        runtime.switch_context(65_536)

        self.assertEqual(runtime.run_calls, [65_536])
        self.assertEqual(runtime.wait_calls, [65_536])
        self.assertEqual(runtime.context.current_context, 65_536)

    def test_healthy_matching_context_does_not_restart(self):
        runtime = ProbeRuntime([ServerProbe(True, 65_536, False, "slots")])

        runtime.switch_context(65_536)

        self.assertEqual(runtime.run_calls, [])
        self.assertEqual(runtime.wait_calls, [])
        self.assertEqual(runtime.context.current_context, 65_536)

    def test_policy_uses_actual_physical_context_before_decision(self):
        runtime = ProbeRuntime(
            [
                ServerProbe(True, 131_072, False, "slots"),
                ServerProbe(True, 131_072, False, "slots"),
            ],
            token_count=100,
        )

        prepared = runtime.prepare_chat_request({"messages": [{"role": "user", "content": "tiny"}]})

        self.assertEqual(prepared.decision.action, "keep")
        self.assertEqual(prepared.decision.current_context, 131_072)
        self.assertEqual(prepared.decision.target_context, 131_072)
        self.assertEqual(runtime.context.current_context, 131_072)
        self.assertEqual(runtime.run_calls, [])

    def test_dead_policy_falls_back_to_last_known_c64_before_recovery(self):
        runtime = ProbeRuntime([ServerProbe(False, None, False, "none", "dead")], token_count=1_000)

        prepared = runtime.prepare_chat_request(
            {"messages": [{"role": "user", "content": "recover"}], "max_tokens": 50_000}
        )

        self.assertEqual(prepared.decision.action, "keep")
        self.assertEqual(prepared.decision.current_context, 65_536)
        self.assertEqual(prepared.decision.target_context, 65_536)
        self.assertEqual(runtime.run_calls, [65_536])
        self.assertEqual(runtime.context.current_context, 65_536)

    def test_dead_server_recovery_restarts_cached_target_before_request(self):
        runtime = ProbeRuntime([ServerProbe(False, None, False, "none", "dead")])

        prepared = runtime.prepare_chat_request(
            {"messages": [{"role": "user", "content": "recover"}], "max_tokens": 50_000}
        )

        self.assertEqual(runtime.run_calls, [65_536])
        self.assertEqual(runtime.wait_calls, [65_536])
        self.assertEqual(prepared.decision.target_context, 65_536)
        self.assertEqual(prepared.body["max_tokens"], 50_000)
        self.assertEqual(runtime.context.current_context, 65_536)

    def test_english_fallback_estimate_is_positive(self):
        estimate = _fallback_text_token_estimate(
            {"messages": [{"role": "user", "content": "hello"}], "tools": []}
        )

        self.assertGreater(estimate, 0)

    def test_cjk_fallback_estimate_is_conservative(self):
        body = {"messages": [{"role": "user", "content": "汉字" * 1_000}], "tools": []}
        serialized_length = len(
            json.dumps(
                {"messages": body["messages"], "tools": body["tools"]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        old_estimate = max(1, (serialized_length + 3) // 4)

        estimate = _fallback_text_token_estimate(body)

        self.assertGreaterEqual(estimate, old_estimate * 3)

    def test_image_base64_is_not_counted_in_fallback_text(self):
        short_url = "data:image/png;base64,AA=="
        long_url = "data:image/png;base64," + ("A" * 100_000)
        short_body = {
            "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": short_url}}]}]
        }
        long_body = {
            "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": long_url}}]}]
        }

        short_estimate = _fallback_text_token_estimate(short_body)
        long_estimate = _fallback_text_token_estimate(long_body)
        runtime = FallbackRuntime()
        prompt_estimate = runtime.estimate_prompt(long_body)

        self.assertLessEqual(long_estimate - short_estimate, 20)
        self.assertEqual(prompt_estimate.tokens, long_estimate + runtime.config.image_token_reserve)
        self.assertEqual(prompt_estimate.image_count, 1)


if __name__ == "__main__":
    unittest.main()
