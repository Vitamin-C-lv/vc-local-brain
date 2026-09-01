import io
import os
import threading
import time
import unittest
from unittest.mock import patch

import dsh_local_qwen_relay as relay_module
from dsh_local_qwen_relay import IncompleteRequestBodyError, Relay, read_exact_body
from local_brain_runtime import (
    LocalBrainRuntime,
    QueueFullError,
    RequestAdmissionGate,
    RequestCancelledBeforeExecution,
)


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class LocalBrainAdmissionTests(unittest.TestCase):
    def test_fifo_order(self):
        gate = RequestAdmissionGate(waiting_capacity=4)
        acquired = []
        started = {name: threading.Event() for name in "ABC"}
        release = {name: threading.Event() for name in "ABC"}
        errors = []

        def worker(name):
            try:
                with gate.acquire():
                    acquired.append(name)
                    started[name].set()
                    release[name].wait(2.0)
            except Exception as error:  # pragma: no cover - failure cleanup/reporting
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(name,)) for name in "ABC"]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(started["A"].wait(1.0))
            self.assertTrue(wait_until(lambda: gate.status()["queue_depth"] == 2))
            release["A"].set()
            self.assertTrue(started["B"].wait(1.0))
            self.assertEqual(acquired, ["A", "B"])
            release["B"].set()
            self.assertTrue(started["C"].wait(1.0))
            self.assertEqual(acquired, ["A", "B", "C"])
            release["C"].set()
        finally:
            for event in release.values():
                event.set()
            for thread in threads:
                thread.join(1.0)
        self.assertEqual(errors, [])

    def test_queue_capacity_and_full_error(self):
        gate = RequestAdmissionGate(waiting_capacity=4)
        active_started = threading.Event()
        release_active = threading.Event()
        release_waiters = threading.Event()
        errors = []

        def active_worker():
            try:
                with gate.acquire():
                    active_started.set()
                    release_active.wait(2.0)
            except Exception as error:  # pragma: no cover - failure cleanup/reporting
                errors.append(error)

        def queued_worker():
            try:
                with gate.acquire():
                    release_waiters.wait(2.0)
            except Exception as error:  # pragma: no cover - failure cleanup/reporting
                errors.append(error)

        active = threading.Thread(target=active_worker)
        waiters = [threading.Thread(target=queued_worker) for _ in range(4)]
        active.start()
        self.assertTrue(active_started.wait(1.0))
        for thread in waiters:
            thread.start()
        try:
            self.assertTrue(wait_until(lambda: gate.status()["queue_depth"] == 4))
            with self.assertRaises(QueueFullError) as caught:
                with gate.acquire():
                    pass
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(caught.exception.error_code, "LOCAL_BRAIN_QUEUE_FULL")
            self.assertEqual(str(caught.exception), "Local Brain request queue is full")
        finally:
            release_active.set()
            release_waiters.set()
            active.join(1.0)
            for thread in waiters:
                thread.join(1.0)
        self.assertEqual(errors, [])

    def test_queue_full_is_exposed_as_http_503_json(self):
        handler = Relay.__new__(Relay)
        handler.close_connection = False
        sent = []
        handler._send_json = lambda status, payload: sent.append((status, payload))

        class FullRuntime:
            def request_slot(self, cancelled=None):
                raise QueueFullError()

        with patch.object(relay_module, "runtime", return_value=FullRuntime()):
            handler._admit_and_forward(b"{}")

        self.assertEqual(sent[0][0], 503)
        self.assertEqual(sent[0][1]["error"]["type"], "local_brain_runtime_error")
        self.assertEqual(sent[0][1]["error"]["code"], "LOCAL_BRAIN_QUEUE_FULL")

    def test_status_reports_active_and_queue_fields(self):
        runtime = LocalBrainRuntime(probe=False)
        active_started = threading.Event()
        release_active = threading.Event()
        queued_acquired = threading.Event()

        def queued_worker():
            with runtime.request_slot():
                queued_acquired.set()

        with runtime.request_slot():
            active_started.set()
            queued = threading.Thread(target=queued_worker)
            queued.start()
            self.assertTrue(active_started.is_set())
            self.assertTrue(wait_until(lambda: runtime.status()["queue_depth"] == 1))
            status = runtime.status()
            self.assertEqual(status["admission_mode"], "bounded-fifo")
            self.assertEqual(status["active_requests"], 1)
            self.assertEqual(status["queue_depth"], 1)
            self.assertEqual(status["queue_capacity"], 4)
            self.assertEqual(status["max_active_requests"], 1)
            release_active.set()
        queued.join(1.0)
        self.assertTrue(queued_acquired.is_set())
        idle = runtime.status()
        self.assertEqual(idle["active_requests"], 0)
        self.assertEqual(idle["queue_depth"], 0)

    def test_queue_capacity_env_override_allows_zero(self):
        with patch.dict(os.environ, {"QWEN_QUEUE_CAPACITY": "0"}):
            runtime = LocalBrainRuntime(probe=False)
        self.assertEqual(runtime.status()["queue_capacity"], 0)

    def test_queued_client_disconnect_removes_ticket(self):
        gate = RequestAdmissionGate(waiting_capacity=4)
        cancel = threading.Event()
        cancelled = threading.Event()
        acquired = threading.Event()

        def worker():
            try:
                with gate.acquire(cancelled=cancel.is_set):
                    acquired.set()
            except RequestCancelledBeforeExecution:
                cancelled.set()

        with gate.acquire():
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(wait_until(lambda: gate.status()["queue_depth"] == 1))
            cancel.set()
            self.assertTrue(cancelled.wait(1.0))
            self.assertEqual(gate.status()["queue_depth"], 0)
            self.assertEqual(gate.status()["active_requests"], 1)
        thread.join(1.0)
        self.assertFalse(acquired.is_set())

    def test_cancelled_request_never_becomes_active(self):
        gate = RequestAdmissionGate(waiting_capacity=4)
        cancel = threading.Event()
        executed = []
        result = []

        def worker():
            try:
                with gate.acquire(cancelled=cancel.is_set):
                    executed.append(True)
            except RequestCancelledBeforeExecution:
                result.append("cancelled")

        with gate.acquire():
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(wait_until(lambda: gate.status()["queue_depth"] == 1))
            cancel.set()
            thread.join(1.0)
        self.assertEqual(result, ["cancelled"])
        self.assertEqual(executed, [])
        self.assertEqual(gate.status()["queue_depth"], 0)

    def test_cancelled_head_waiter_cannot_win_active_handoff(self):
        gate = RequestAdmissionGate(waiting_capacity=4)
        cancel = threading.Event()
        active_started = threading.Event()
        release_active = threading.Event()
        cancelled = threading.Event()
        executed = []

        def active_worker():
            with gate.acquire():
                active_started.set()
                release_active.wait(2.0)

        def queued_worker():
            try:
                with gate.acquire(cancelled=cancel.is_set):
                    executed.append(True)
            except RequestCancelledBeforeExecution:
                cancelled.set()

        active_thread = threading.Thread(target=active_worker)
        queued_thread = threading.Thread(target=queued_worker)
        active_thread.start()
        self.assertTrue(active_started.wait(1.0))
        queued_thread.start()
        try:
            self.assertTrue(wait_until(lambda: gate.status()["queue_depth"] == 1))
            cancel.set()
            release_active.set()
            self.assertTrue(cancelled.wait(1.0))
        finally:
            cancel.set()
            release_active.set()
            active_thread.join(1.0)
            queued_thread.join(1.0)

        self.assertEqual(executed, [])
        self.assertEqual(gate.status()["active_requests"], 0)
        self.assertEqual(gate.status()["queue_depth"], 0)

    def test_truncated_body_rejected_before_forward(self):
        handler = Relay.__new__(Relay)
        handler.rfile = io.BytesIO(b"abc")
        handler.headers = {"Content-Length": "5"}
        handler.close_connection = False
        sent = []
        handler._send_json = lambda status, payload: sent.append((status, payload))

        with patch.object(relay_module, "urlopen") as upstream:
            handler.do_POST()

        self.assertEqual(upstream.call_count, 0)
        self.assertEqual(sent[0][0], 400)
        self.assertEqual(sent[0][1]["error"]["type"], "invalid_request_error")
        self.assertEqual(sent[0][1]["error"]["code"], "LOCAL_BRAIN_INCOMPLETE_REQUEST_BODY")

    def test_read_exact_body_reports_short_stream(self):
        with self.assertRaises(IncompleteRequestBodyError):
            read_exact_body(io.BytesIO(b"abc"), 5)

    def test_no_transparent_replay_on_upstream_failure(self):
        handler = Relay.__new__(Relay)
        handler.path = "/v1/models"
        handler.command = "GET"
        handler.headers = {}
        handler.rfile = io.BytesIO()
        handler.close_connection = False
        sent = []
        handler._send_json = lambda status, payload: sent.append((status, payload))

        with patch.object(relay_module, "urlopen", side_effect=OSError("synthetic upstream failure")) as upstream:
            handler._forward()

        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(sent[0][1]["error"]["code"], "LOCAL_BRAIN_UPSTREAM_ERROR")
        self.assertTrue(sent[0][1]["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()
