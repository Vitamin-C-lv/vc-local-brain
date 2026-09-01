import unittest

from local_brain_context import (
    ContextManager,
    GLOBAL_CONTEXT_TIERS,
    OutputBurstState,
    RuntimeConfig,
    calculate_required_context,
    choose_context_tier,
    classify_completion,
    compute_effective_max_tokens,
    estimate_completion_reserve,
    request_has_tools,
    required_context_tokens,
    resolve_reasoning_budget,
    safe_completion_capacity,
)


class LocalBrainContextTests(unittest.TestCase):
    def test_required_context_and_tiers(self):
        required = calculate_required_context(18_000, 32_768)
        self.assertEqual(required, 58_960)
        self.assertEqual(choose_context_tier(required, (65_536, 98_304, 131_072)), 65_536)
        self.assertEqual(choose_context_tier(35_000 + 32_768 + 8_192, (65_536, 98_304, 131_072)), 98_304)
        self.assertIsNone(choose_context_tier(40_000, (16_384, 32_768)))

    def test_global_tiers_are_the_only_context_policy(self):
        low = ContextManager(current_context=16_384)
        for legacy_client in ("pet", "dsh", "future-client", None):
            self.assertEqual(low.decision(12_000, legacy_client).target_context, 16_384)
        middle = ContextManager(current_context=16_384)
        self.assertEqual(middle.decision(25_000, "pet").target_context, 32_768)
        high = ContextManager(current_context=32_768)
        self.assertEqual(high.decision(32_769, "dsh").target_context, 65_536)
        self.assertEqual(RuntimeConfig().allowed_context_tiers("pet"), GLOBAL_CONTEXT_TIERS)

    def test_small_request_uses_smallest_global_tier(self):
        required = required_context_tokens(prompt_tokens=100, completion_reserve=4_096)
        self.assertEqual(choose_context_tier(required), 16_384)

    def test_reasoning_and_tool_reservation_are_request_based(self):
        self.assertEqual(estimate_completion_reserve(explicit_max_tokens=None, reasoning_budget=16_384, has_tools=True), 24_576)
        self.assertEqual(estimate_completion_reserve(explicit_max_tokens=None, reasoning_budget=16_384, has_tools=False), 20_480)
        self.assertEqual(required_context_tokens(prompt_tokens=13_000, completion_reserve=24_576), 45_768)

    def test_tools_schema_detection(self):
        self.assertFalse(request_has_tools({"messages": []}))
        self.assertFalse(request_has_tools({"tools": []}))
        self.assertTrue(request_has_tools({"tools": [{"type": "function"}]}))

    def test_reasoning_budget_resolution(self):
        self.assertEqual(resolve_reasoning_budget({"reasoning_effort": "max"}), 16_384)
        self.assertEqual(resolve_reasoning_budget({"thinking_budget_tokens": 2_048}), 2_048)
        self.assertEqual(resolve_reasoning_budget({"chat_template_kwargs": {"enable_thinking": False}}), 0)

    def test_dynamic_safe_capacity_is_not_fixed_at_32k_or_64k(self):
        self.assertEqual(safe_completion_capacity(physical_ctx=65_536, prompt_tokens=13_000), 44_344)
        self.assertGreater(compute_effective_max_tokens(explicit_max_tokens=None, physical_ctx=65_536, prompt_tokens=13_000), 32_768)
        self.assertGreater(compute_effective_max_tokens(explicit_max_tokens=None, physical_ctx=131_072, prompt_tokens=40_000), 65_536)

    def test_explicit_max_tokens_is_preserved_without_double_reasoning(self):
        reserve = estimate_completion_reserve(explicit_max_tokens=50_000, reasoning_budget=16_384, has_tools=True)
        self.assertEqual(reserve, 50_000)
        self.assertEqual(compute_effective_max_tokens(explicit_max_tokens=50_000, physical_ctx=65_536, prompt_tokens=1_000), 50_000)

    def test_explicit_max_tokens_over_safe_capacity_is_rejected(self):
        with self.assertRaises(ValueError):
            compute_effective_max_tokens(explicit_max_tokens=60_000, physical_ctx=65_536, prompt_tokens=1_000, backend_completion_cap=50_000)

    def test_old_config_fields_cannot_reintroduce_client_policy(self):
        config = RuntimeConfig.from_mapping(
            {
                "physical_context_tiers": [16_384, 32_768, 65_536, 98_304, 131_072],
                "clients": {"pet": {"allowed_context_tiers": [16_384]}},
                "output": {"normal": 32_768, "burst": 65_536},
            }
        )
        self.assertEqual(config.allowed_context_tiers("pet"), GLOBAL_CONTEXT_TIERS)

    def test_promotion_can_skip_a_tier(self):
        manager = ContextManager(current_context=65_536)
        decision = manager.decision(65_000 + 32_768 + 8_192, "dsh")
        self.assertEqual(decision.action, "promote")
        self.assertEqual(decision.target_context, 131_072)

    def test_idle_demotion_requires_safety_margin(self):
        now = [0.0]
        config = RuntimeConfig(idle_demote_seconds=900)
        manager = ContextManager(current_context=131_072, config=config, clock=lambda: now[0])
        manager.record_request(16_000, 56_960, now=0)
        decision = manager.decision(16_000 + 32_768 + 8_192, "dsh", now=901)
        self.assertEqual(decision.action, "demote")
        self.assertEqual(decision.target_context, 65_536)

    def test_post_compaction_demotion_has_hysteresis(self):
        now = [0.0]
        config = RuntimeConfig(active_demotion_min_interval_seconds=0)
        manager = ContextManager(current_context=131_072, config=config, clock=lambda: now[0])
        manager.record_request(90_000, 110_000, now=0)
        first = manager.decision(19_000, "dsh", now=1, post_compaction=True)
        self.assertEqual(first.action, "keep")
        manager.record_request(19_000, 31_192, now=1)
        second = manager.decision(19_000, "dsh", now=2, post_compaction=True)
        self.assertEqual(second.action, "demote")
        self.assertEqual(second.target_context, 32_768)

    def test_max_context_enters_compaction(self):
        manager = ContextManager(current_context=131_072)
        decision = manager.decision(131_073, "dsh")
        self.assertEqual(decision.action, "compact")
        self.assertIsNone(decision.target_context)

    def test_output_observation_does_not_activate_fixed_burst_cap(self):
        state = OutputBurstState()
        state.begin_request(None, 44_344)
        state.observe_response(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "partial tool result", "tool_calls": [{"id": "1"}]},
                    }
                ]
            },
            "dynamic",
        )
        self.assertEqual(state.next_output_mode, "dynamic")
        self.assertEqual(state.begin_request(1_024, 1_024).max_output_tokens, 1_024)
        self.assertEqual(state.next_output_mode, "dynamic")
        state.observe_response(
            {
                "choices": [
                    {"finish_reason": "length", "message": {"reasoning_content": "loop"}}
                ]
            },
            "explicit",
        )
        self.assertEqual(state.next_output_mode, "dynamic")

    def test_completion_classifier(self):
        result = classify_completion(
            {"choices": [{"finish_reason": "length", "message": {"reasoning_content": "x"}}]}
        )
        self.assertTrue(result["reasoning_only"])
        self.assertFalse(result["substantive"])


if __name__ == "__main__":
    unittest.main()
