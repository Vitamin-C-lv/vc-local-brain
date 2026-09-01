import unittest

from local_brain_runtime import (
    ContextCapacityError,
    ContextSwitchError,
    HTTPResult,
    LocalBrainRuntime,
    ServerProbe,
)


class FakeRuntime(LocalBrainRuntime):
    def __init__(self, token_count=35_000):
        super().__init__(probe=False)
        self.context.current_context = 65_536
        self.token_count = token_count
        self.switches = []

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
        with self.assertRaises(ContextCapacityError):
            runtime.prepare_chat_request(
                {
                    "messages": [{"role": "user", "content": "too large"}],
                    "reasoning_effort": "max",
                    "tools": [{"type": "function"}],
                }
            )
        self.assertEqual(runtime.switches, [])

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


if __name__ == "__main__":
    unittest.main()
