#!/usr/bin/env python3
"""Loopback-only HTTP relay from DSH to the Windows llama-server.

urllib uses the already configured Kali HTTP proxy, which is the only working
WSL-to-Windows path in this environment.  This process deliberately accepts
connections only from 127.0.0.1 and exposes no new network surface.
"""

import base64
import io
import json
import logging
import os
import select
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError

from local_brain_context import REASONING_BUDGETS
from local_brain_runtime import (
    LocalBrainRuntime,
    LocalBrainRuntimeError,
    QueueFullError,
    RequestCancelledBeforeExecution,
)

UPSTREAM = os.environ.get("QWEN_UPSTREAM", "http://172.25.240.1:17861")
HOP_BY_HOP = {"connection", "host", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}
DEBUG_REASONING = os.environ.get("QWEN_RELAY_DEBUG") == "1"
LOG = logging.getLogger("dsh-local-qwen-relay")
RUNTIME = None
_BODY_NOT_READY = object()


class IncompleteRequestBodyError(LocalBrainRuntimeError):
    status_code = 400
    error_code = "LOCAL_BRAIN_INCOMPLETE_REQUEST_BODY"
    error_type = "invalid_request_error"


def read_exact_body(stream, content_length):
    """Read exactly Content-Length bytes, rejecting an early EOF."""
    if content_length < 0:
        raise IncompleteRequestBodyError("Content-Length must be non-negative")
    body = stream.read(content_length) if content_length else b""
    received = len(body) if body is not None else 0
    if received != content_length:
        raise IncompleteRequestBodyError(
            f"incomplete request body: expected {content_length} bytes, received {received}"
        )
    return body


def runtime():
    global RUNTIME
    if RUNTIME is None:
        RUNTIME = LocalBrainRuntime(UPSTREAM, logger=LOG)
    return RUNTIME

QWEN35_REASONING_PROFILES = {
    effort: {
        "enable_thinking": effort != "off",
        "budget": None if effort == "off" else budget,
    }
    for effort, budget in REASONING_BUDGETS.items()
}


def apply_qwen35_reasoning(body):
    """Translate DSH's generic effort into llama.cpp's Qwen request controls."""
    effort = body.get("reasoning_effort")
    if not isinstance(effort, str):
        return None

    profile = QWEN35_REASONING_PROFILES.get(effort.lower())
    if profile is None:
        return None

    kwargs = body.get("chat_template_kwargs")
    if not isinstance(kwargs, dict):
        kwargs = {}
    kwargs["enable_thinking"] = profile["enable_thinking"]
    body["chat_template_kwargs"] = kwargs

    if profile["budget"] is None:
        body.pop("thinking_budget_tokens", None)
    else:
        body["thinking_budget_tokens"] = profile["budget"]

    # The relay has translated the DSH-only abstraction; llama.cpp does not use it.
    body.pop("reasoning_effort", None)
    return effort.lower(), profile


def normalize_webp_data_url(url):
    """Convert one DSH-produced WebP data URL to PNG for llama.cpp.

    DSH may normalize transparent images into WebP.  This llama.cpp build
    accepts the OpenAI image_url shape but rejects WebP bytes, so retain every
    image item and change only that unsupported encoding.
    """
    if not isinstance(url, str):
        return url, False
    header, separator, encoded = url.partition(",")
    if separator != "," or not header.lower().startswith("data:image/webp") or ";base64" not in header.lower():
        return url, False
    try:
        source = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(source)) as image:
            image.load()
            mode = "RGBA" if "A" in image.getbands() else "RGB"
            output = io.BytesIO()
            image.convert(mode).save(output, format="PNG")
    except (OSError, UnidentifiedImageError, ValueError):
        return url, False
    normalized = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{normalized}", True


def normalize_llama_images(body):
    """Normalize every OpenAI image_url item in every message content array."""
    normalized = 0
    messages = body.get("messages")
    if not isinstance(messages, list):
        return normalized
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for item in message["content"]:
            if not isinstance(item, dict) or item.get("type") != "image_url":
                continue
            image_url = item.get("image_url")
            if not isinstance(image_url, dict):
                continue
            url, converted = normalize_webp_data_url(image_url.get("url"))
            if converted:
                image_url["url"] = url
                normalized += 1
    return normalized


class Relay(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _send_runtime_error(self, error):
        self._send_json(
            getattr(error, "status_code", 503),
            {
                "error": {
                    "message": str(error),
                    "type": getattr(error, "error_type", "local_brain_runtime_error"),
                    "code": getattr(error, "error_code", "LOCAL_BRAIN_RUNTIME_ERROR"),
                }
            },
        )

    def _safe_send_runtime_error(self, error):
        try:
            self._send_runtime_error(error)
        except OSError:
            self.close_connection = True

    def _serve_status(self):
        try:
            self._send_json(200, runtime().status())
        except LocalBrainRuntimeError as error:
            self._send_runtime_error(error)

    def _read_request_body(self):
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return None
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as error:
            raise IncompleteRequestBodyError(f"invalid Content-Length: {raw_length!r}") from error
        return read_exact_body(self.rfile, length)

    def _client_disconnected(self):
        """Poll for an EOF without consuming a queued request's bytes."""
        connection = getattr(self, "connection", None)
        if connection is None:
            return False
        try:
            readable, _, _ = select.select([connection], [], [], 0)
        except (OSError, ValueError):
            return True
        if not readable:
            return False
        try:
            return connection.recv(1, socket.MSG_PEEK) == b""
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True

    def _admit_and_forward(self, body=_BODY_NOT_READY):
        try:
            with runtime().request_slot(cancelled=self._client_disconnected):
                self._forward(body)
        except RequestCancelledBeforeExecution:
            self.close_connection = True
        except (QueueFullError, LocalBrainRuntimeError) as error:
            self._safe_send_runtime_error(error)

    def _forward(self, body=_BODY_NOT_READY):
        if body is _BODY_NOT_READY:
            body = self._read_request_body()
        headers = {key: value for key, value in self.headers.items() if key.lower() not in HOP_BY_HOP and key.lower() != "content-length"}
        translated = None
        request_mode = None
        streamed = False
        payload = None
        try:
            if body and urlsplit(self.path).path == "/v1/chat/completions":
                try:
                    payload = json.loads(body)
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict):
                    normalize_llama_images(payload)
                    translated = apply_qwen35_reasoning(payload)
                    prepared = runtime().prepare_chat_request(payload)
                    payload = prepared.body
                    request_mode = prepared.budget.mode
                    streamed = bool(payload.get("stream"))
                    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

            if body is not None:
                headers["Content-Length"] = str(len(body))
            if DEBUG_REASONING and translated is not None:
                effort, profile = translated
                print(
                    f"reasoning_profile={effort} enable_thinking={str(profile['enable_thinking']).lower()} budget={profile['budget']}",
                    flush=True,
                )
            request = Request(f"{UPSTREAM}{self.path}", data=body, headers=headers, method=self.command)
            try:
                response = urlopen(request, timeout=185)
            except HTTPError as error:
                response = error
            except Exception as error:
                self._send_json(
                    502,
                    {
                        "error": {
                            "message": f"Windows llama-server relay error: {error}",
                            "type": "upstream_error",
                            "code": "LOCAL_QWEN_UPSTREAM_ERROR",
                        }
                    },
                )
                return
            with response:
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() not in HOP_BY_HOP and key.lower() != "content-length":
                        self.send_header(key, value)
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    self.send_header("Content-Length", content_length)
                self.send_header("Connection", "close")
                self.end_headers()
                observed = bytearray()
                remaining = None
                if content_length is not None:
                    try:
                        remaining = max(0, int(content_length))
                    except (TypeError, ValueError):
                        remaining = None
                while remaining is None or remaining > 0:
                    chunk = response.read(8192 if remaining is None else min(8192, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if remaining is not None:
                        remaining -= len(chunk)
                    if request_mode is not None and len(observed) < 16 * 1024 * 1024:
                        observed.extend(chunk[: 16 * 1024 * 1024 - len(observed)])
            if request_mode is not None:
                runtime().observe_response(bytes(observed), request_mode, streamed)
            self.close_connection = True
        except LocalBrainRuntimeError as error:
            self._send_runtime_error(error)

    def do_GET(self):
        if urlsplit(self.path).path == "/vc-local-brain/status":
            self._serve_status()
            return
        self._admit_and_forward()

    def do_POST(self):
        try:
            body = self._read_request_body()
        except LocalBrainRuntimeError as error:
            self._safe_send_runtime_error(error)
            return
        self._admit_and_forward(body)

    def do_OPTIONS(self):
        self._admit_and_forward()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    relay_port = int(os.environ.get("QWEN_RELAY_PORT", "17862"))
    RUNTIME = LocalBrainRuntime(UPSTREAM, logger=LOG)
    ThreadingHTTPServer(("127.0.0.1", relay_port), Relay).serve_forever()
