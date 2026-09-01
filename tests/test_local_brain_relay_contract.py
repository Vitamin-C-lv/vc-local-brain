import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import dsh_local_qwen_relay as relay_module
from dsh_local_qwen_relay import Relay
from local_brain_contract import BACKEND_MODEL_ALIAS, PUBLIC_MODEL_ALIAS, REQUEST_ID_HEADER


class FakeResponse:
    status = 200
    headers = {"Content-Type": "application/json", "Content-Length": "2"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        if getattr(self, "read_once", False):
            return b""
        self.read_once = True
        return b"{}"


class CaptureRelay:
    @staticmethod
    def build(path, command="GET", headers=None):
        handler = Relay.__new__(Relay)
        handler.path = path
        handler.command = command
        handler.headers = headers or {}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.close_connection = False
        handler.sent_headers = []
        handler.sent_statuses = []
        handler.send_response = lambda status: handler.sent_statuses.append(status)
        handler.send_header = lambda key, value: handler.sent_headers.append((key, value))
        handler.end_headers = lambda: None
        return handler


class RelayContractTests(unittest.TestCase):
    def test_health_bypasses_admission_and_returns_minimal_payload(self):
        sent = []
        runtime = Mock()
        runtime.refresh_server_state.return_value = SimpleNamespace(healthy=True)
        runtime.request_slot.side_effect = AssertionError("health must bypass admission")
        handler = Relay.__new__(Relay)
        handler.path = "/health"
        handler._send_json = lambda status, payload: sent.append((status, payload))

        with patch.object(relay_module, "runtime", return_value=runtime):
            handler.do_GET()

        self.assertEqual(sent, [(200, {"status": "ok"})])
        runtime.request_slot.assert_not_called()

    def test_status_is_whitelist_projection_without_internal_runtime_fields(self):
        sent = []
        runtime = Mock()
        runtime.refresh_server_state.return_value = SimpleNamespace(healthy=True)
        runtime.status.return_value = {
            "server_healthy": True,
            "active_requests": 1,
            "queue_depth": 2,
            "queue_capacity": 4,
            "model": BACKEND_MODEL_ALIAS,
            "current_context": 131072,
            "last_restart": {"command": ["private-path"]},
        }
        handler = Relay.__new__(Relay)
        handler.path = "/vc-local-brain/status"
        handler._send_json = lambda status, payload: sent.append((status, payload))

        with patch.object(relay_module, "runtime", return_value=runtime):
            handler.do_GET()

        self.assertEqual(
            sent,
            [
                (
                    200,
                    {
                        "status": "ready",
                        "model": PUBLIC_MODEL_ALIAS,
                        "queue": {"active": 1, "waiting": 2, "capacity": 4},
                    },
                )
            ],
        )
        payload_text = json.dumps(sent[0][1])
        self.assertNotIn("current_context", payload_text)
        self.assertNotIn("private-path", payload_text)

    def test_json_response_contains_server_request_id_header(self):
        handler = CaptureRelay.build("/health")

        handler._send_json(200, {"status": "ok"})

        request_id_headers = [value for key, value in handler.sent_headers if key == REQUEST_ID_HEADER]
        self.assertEqual(len(request_id_headers), 1)
        self.assertTrue(request_id_headers[0].startswith("lb-"))
        self.assertEqual(handler._request_id_value(), request_id_headers[0])

    def test_public_and_omitted_model_aliases_reach_backend_as_legacy_alias(self):
        for model in (None, PUBLIC_MODEL_ALIAS):
            with self.subTest(model=model):
                runtime = Mock()
                captured = []
                prepared_body = {}

                def prepare(payload):
                    prepared_body.clear()
                    prepared_body.update(payload)
                    return SimpleNamespace(body=dict(payload), budget=SimpleNamespace(mode=None))

                runtime.prepare_chat_request.side_effect = prepare
                handler = CaptureRelay.build(
                    "/v1/chat/completions",
                    command="POST",
                    headers={"Content-Type": "application/json"},
                )
                request_payload = {"messages": [{"role": "user", "content": "Reply exactly: OK"}]}
                if model is not None:
                    request_payload["model"] = model
                request_payload["metadata"] = {"source": "test"}

                def capture_request(request, timeout):
                    captured.append((request, timeout))
                    return FakeResponse()

                with patch.object(relay_module, "runtime", return_value=runtime), patch.object(
                    relay_module, "urlopen", side_effect=capture_request
                ):
                    handler._forward(json.dumps(request_payload).encode("utf-8"))

                self.assertEqual(prepared_body["model"], BACKEND_MODEL_ALIAS)
                self.assertNotIn("metadata", prepared_body)
                forwarded = json.loads(captured[0][0].data.decode("utf-8"))
                self.assertEqual(forwarded["model"], BACKEND_MODEL_ALIAS)

    def test_invalid_model_or_reasoning_is_rejected_before_upstream(self):
        for field, value in (("model", "other-model"), ("reasoning_effort", "ultra")):
            with self.subTest(field=field):
                sent = []
                handler = CaptureRelay.build("/v1/chat/completions", command="POST")
                handler._send_json = lambda status, payload: sent.append((status, payload))
                payload = {"messages": [{"role": "user", "content": "hi"}], field: value}
                upstream = Mock()

                with patch.object(relay_module, "runtime", side_effect=AssertionError("runtime must not run")), patch.object(
                    relay_module, "urlopen", upstream
                ):
                    handler._forward(json.dumps(payload).encode("utf-8"))

                self.assertEqual(sent[0][0], 400)
                self.assertEqual(sent[0][1]["error"]["code"], "LOCAL_BRAIN_INVALID_REQUEST")
                self.assertFalse(sent[0][1]["error"]["retryable"])
                upstream.assert_not_called()

    def test_upstream_network_failure_is_generic_retryable_error(self):
        sent = []
        handler = CaptureRelay.build("/v1/models")
        handler._send_json = lambda status, payload: sent.append((status, payload))

        with patch.object(relay_module, "urlopen", side_effect=OSError("private gateway details")):
            handler._forward()

        self.assertEqual(sent[0][0], 502)
        error = sent[0][1]["error"]
        self.assertEqual(error["code"], "LOCAL_BRAIN_UPSTREAM_ERROR")
        self.assertEqual(error["message"], "Local Brain backend unavailable")
        self.assertTrue(error["retryable"])
        self.assertNotIn("private gateway details", json.dumps(sent[0][1]))


if __name__ == "__main__":
    unittest.main()
