import unittest

from local_brain_contract import (
    BACKEND_MODEL_ALIAS,
    ContractError,
    PUBLIC_MODEL_ALIAS,
    new_request_id,
    normalize_chat_request,
    public_error_payload,
    public_status,
)


class LocalBrainContractTests(unittest.TestCase):
    def test_model_can_be_omitted_and_is_hidden_from_backend_selection(self):
        body = normalize_chat_request({"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(body["model"], BACKEND_MODEL_ALIAS)

    def test_public_and_legacy_aliases_are_both_accepted_during_migration(self):
        for alias in (PUBLIC_MODEL_ALIAS, BACKEND_MODEL_ALIAS):
            body = normalize_chat_request(
                {"model": alias, "messages": [{"role": "user", "content": "hi"}]}
            )
            self.assertEqual(body["model"], BACKEND_MODEL_ALIAS)

    def test_unknown_model_alias_is_rejected(self):
        with self.assertRaises(ContractError):
            normalize_chat_request(
                {"model": "some-other-model", "messages": [{"role": "user", "content": "hi"}]}
            )

    def test_reasoning_effort_is_small_stable_contract(self):
        body = normalize_chat_request(
            {"messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "HIGH"}
        )
        self.assertEqual(body["reasoning_effort"], "high")
        with self.assertRaises(ContractError):
            normalize_chat_request(
                {"messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "ultra"}
            )

    def test_metadata_is_not_a_v1_backend_dependency(self):
        body = normalize_chat_request(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "metadata": {"source": "pet", "priority": "high"},
            }
        )
        self.assertNotIn("metadata", body)

    def test_public_status_is_a_whitelist_projection(self):
        status = public_status(
            {
                "server_healthy": True,
                "active_requests": 1,
                "queue_depth": 2,
                "queue_capacity": 4,
                "model": "li-huahua-local",
                "current_context": 131072,
                "last_restart": {"command": ["secret-path"]},
            }
        )
        self.assertEqual(
            status,
            {
                "status": "ready",
                "model": PUBLIC_MODEL_ALIAS,
                "queue": {"active": 1, "waiting": 2, "capacity": 4},
            },
        )
        self.assertNotIn("current_context", status)
        self.assertNotIn("last_restart", status)

    def test_public_error_adds_retryable_without_renaming_existing_codes(self):
        class QueueFull(Exception):
            error_code = "LOCAL_BRAIN_QUEUE_FULL"
            error_type = "local_brain_runtime_error"

        payload = public_error_payload(QueueFull("busy"))
        self.assertEqual(payload["error"]["code"], "LOCAL_BRAIN_QUEUE_FULL")
        self.assertTrue(payload["error"]["retryable"])

    def test_request_ids_are_opaque_and_unique(self):
        left = new_request_id()
        right = new_request_id()
        self.assertTrue(left.startswith("lb-"))
        self.assertNotEqual(left, right)


if __name__ == "__main__":
    unittest.main()
