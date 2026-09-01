#!/usr/bin/env python3
"""Small fixed-prompt benchmark for the one physical Local Qwen server."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.request import Request, urlopen


UPSTREAM = os.environ.get("QWEN_UPSTREAM", "http://172.25.240.1:17861")
START_SCRIPT = os.environ.get("QWEN_START_SCRIPT", "/mnt/d/VC-AI-Pet/runtime/Start-LocalQwen.ps1")
TIERS = (65_536, 98_304, 131_072)
REPORT = Path(__file__).with_name("dynamic-context-benchmark.md")


def powershell_executable():
    executable = shutil.which("powershell.exe")
    if executable:
        return executable
    for candidate in (
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return "powershell.exe"


def json_request(path: str, method: str = "GET", payload=None, timeout: float = 30.0):
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(f"{UPSTREAM}{path}", data=body, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            remaining = int(content_length)
            chunks = []
            while remaining:
                chunk = response.read(min(8192, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        else:
            raw = response.read()
        return response.status, json.loads(raw.decode("utf-8")) if raw else None


def probe():
    try:
        health_status, health = json_request("/health", timeout=8)
        slots_status, slots = json_request("/slots", timeout=8)
        n_ctx = None
        processing = False
        if slots_status == 200 and isinstance(slots, list):
            for slot in slots:
                if isinstance(slot, dict) and isinstance(slot.get("n_ctx"), int):
                    n_ctx = slot["n_ctx"]
                    processing = processing or slot.get("is_processing") is True
        return health_status == 200 and isinstance(health, dict) and health.get("status") == "ok", n_ctx, processing
    except Exception:
        return False, None, False


def sample_gpu():
    try:
        raw = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip().split(",")
        return {"used_mib": int(raw[0].strip()), "free_mib": int(raw[1].strip()), "util_pct": int(raw[2].strip())}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {}


def sample_server_ram():
    command = (
        "$p=Get-CimInstance Win32_Process | Where-Object { $_.Name -eq \"llama-server.exe\" }; "
        "if ($p) { $p | Select-Object -First 1 WorkingSetSize,PrivatePageCount | Format-List | Out-String }"
    )
    try:
        output = subprocess.run(
            [powershell_executable(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        ).stdout
        result = {}
        for key in ("WorkingSetSize", "PrivatePageCount"):
            match = re.search(rf"{key}\s*:\s*(\d+)", output)
            if match:
                result[key] = int(match.group(1))
        return result
    except (OSError, subprocess.SubprocessError):
        return {}


def start_context(context: int):
    started_at = time.perf_counter()
    command = [
        powershell_executable(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "D:\\VC-AI-Pet\\runtime\\Start-LocalQwen.ps1",
        "-ContextSize",
        str(context),
    ]
    launcher = ["setsid", "-f", *command] if shutil.which("setsid") else command
    result = subprocess.run(
        launcher,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15 if launcher is not command else 180,
        check=False,
    )
    last = (False, None, False)
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        last = probe()
        if last[0] and last[1] == context and not last[2]:
            return {
                "ok": True,
                "load_seconds": time.perf_counter() - started_at,
                "helper_returncode": result.returncode,
                "helper_output": "detached launcher started",
                "probe": last,
            }
        time.sleep(1)
    return {
        "ok": False,
        "load_seconds": time.perf_counter() - started_at,
        "helper_returncode": result.returncode,
        "helper_output": "detached launcher started",
        "probe": last,
    }


def build_fixed_prompt():
    paragraph = (
        "This is a fixed local context benchmark line. It contains stable text, no images, "
        "no tools, no randomness, and a small numeric marker for repeatable token pressure. "
    )
    for repeats in range(250, 401, 10):
        text = "\n".join(f"{paragraph}line={index:04d}." for index in range(repeats))
        _, template = json_request(
            "/apply-template",
            "POST",
            {"messages": [{"role": "user", "content": text}], "chat_template_kwargs": {"enable_thinking": False}},
        )
        _, tokens = json_request("/tokenize", "POST", {"content": template["prompt"]})
        count = len(tokens["tokens"])
        if 12_000 <= count <= 16_000:
            return text, count
    raise RuntimeError("could not construct a 12K-16K fixed prompt")


def run_request(text: str):
    payload = {
        "model": "li-huahua-local",
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 256,
        "temperature": 0,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    request = Request(
        f"{UPSTREAM}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    first_data = None
    last = {}
    error = None
    try:
        with urlopen(request, timeout=240) as response:
            while True:
                line = response.readline()
                if not line:
                    break
                if not line.startswith(b"data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == b"[DONE]":
                    continue
                first_data = first_data or time.perf_counter()
                try:
                    item = json.loads(raw.decode("utf-8"))
                    if isinstance(item, dict):
                        last = item
                except ValueError:
                    pass
    except Exception as exc:
        error = str(exc)
    return {
        "ttft_seconds": None if first_data is None else first_data - started,
        "timings": last.get("timings", {}) if isinstance(last, dict) else {},
        "error": error,
    }


def benchmark_tier(context: int, text: str, prompt_tokens: int):
    switch = start_context(context)
    if not switch["ok"]:
        return {"context": context, "switch": switch, "error": "context switch did not become ready"}
    time.sleep(3)
    idle_gpu = sample_gpu()
    idle_ram = sample_server_ram()
    samples = [idle_gpu]
    ram_samples = [idle_ram]
    request_result = {}

    def worker():
        nonlocal request_result
        request_result = run_request(text)

    thread = threading.Thread(target=worker)
    thread.start()
    while thread.is_alive():
        samples.append(sample_gpu())
        if len(samples) % 2 == 0:
            ram_samples.append(sample_server_ram())
        time.sleep(0.25)
    thread.join()
    samples.append(sample_gpu())
    ram_samples.append(sample_server_ram())
    peak_used = max((sample.get("used_mib", 0) for sample in samples), default=None)
    peak_private = max((sample.get("PrivatePageCount", 0) for sample in ram_samples), default=None)
    return {
        "context": context,
        "prompt_tokens": prompt_tokens,
        "switch": switch,
        "idle_gpu": idle_gpu,
        "peak_gpu_used_mib": peak_used,
        "idle_ram": idle_ram,
        "peak_ram": {"PrivatePageCount": peak_private} if peak_private else {},
        "request": request_result,
    }


def fmt(value):
    return "N/A" if value is None or value == "" else str(value)


def main():
    text, prompt_tokens = build_fixed_prompt()
    rows = []
    try:
        for context in TIERS:
            print(f"BENCHMARK_CTX={context}", flush=True)
            row = benchmark_tier(context, text, prompt_tokens)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        print("RESTORE_CTX=131072", flush=True)
        restore = start_context(131_072)
        print(json.dumps(restore, ensure_ascii=False), flush=True)

    successful = [row for row in rows if row.get("request", {}).get("timings")]
    prompt_speeds = [row["request"]["timings"].get("prompt_per_second") for row in successful]
    gen_speeds = [row["request"]["timings"].get("predicted_per_second") for row in successful]
    peak_vram = [row.get("peak_gpu_used_mib") for row in successful]
    metric_spreads = []
    for values in (prompt_speeds, gen_speeds):
        numeric = [value for value in values if isinstance(value, (int, float))]
        if len(numeric) >= 2:
            metric_spreads.append((max(numeric) - min(numeric)) / max(numeric))
    if metric_spreads:
        speed_label = "SIGNIFICANT" if any(spread >= 0.20 for spread in metric_spreads) else "SMALL"
    else:
        speed_label = "MIXED"
    numeric_vram = [value for value in peak_vram if isinstance(value, (int, float))]
    if len(numeric_vram) >= 2:
        vram_spread = (max(numeric_vram) - min(numeric_vram)) / max(numeric_vram)
        vram_label = "SIGNIFICANT" if vram_spread >= 0.10 else "SMALL"
    else:
        vram_label = "UNAVAILABLE"

    lines = [
        "# Local Qwen Dynamic Context Benchmark",
        "",
        "Fixed conditions: Qwen3.5-4B-Q4_K_M, mmproj-F16, one server, no images, reasoning off, stream output, max_tokens=256.",
        f"BENCHMARK_PROMPT_TOKENS={prompt_tokens}",
        "",
        "| CTX_SIZE | MODEL_LOAD_TIME_S | VRAM_IDLE_MIB | VRAM_PEAK_MIB | RAM_PRIVATE_IDLE_BYTES | RAM_PRIVATE_PEAK_BYTES | TTFT_S | PROMPT_TPS | GEN_TPS | STATUS |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        timings = row.get("request", {}).get("timings", {})
        lines.append(
            "| {context} | {load} | {idle_vram} | {peak} | {ram} | {ram_peak} | {ttft} | {prompt} | {gen} | {status} |".format(
                context=row["context"],
                load=fmt(round(row.get("switch", {}).get("load_seconds"), 2) if row.get("switch", {}).get("load_seconds") else None),
                idle_vram=fmt(row.get("idle_gpu", {}).get("used_mib")),
                peak=fmt(row.get("peak_gpu_used_mib")),
                ram=fmt(row.get("idle_ram", {}).get("PrivatePageCount")),
                ram_peak=fmt(row.get("peak_ram", {}).get("PrivatePageCount")),
                ttft=fmt(round(row.get("request", {}).get("ttft_seconds"), 4) if row.get("request", {}).get("ttft_seconds") is not None else None),
                prompt=fmt(round(timings.get("prompt_per_second"), 2) if isinstance(timings.get("prompt_per_second"), (int, float)) else None),
                gen=fmt(round(timings.get("predicted_per_second"), 2) if isinstance(timings.get("predicted_per_second"), (int, float)) else None),
                status="PASS" if row.get("request", {}).get("error") is None and timings else "FAIL",
            )
        )
    lines.extend(["", f"SMALLER_CTX_SPEED_BENEFIT={speed_label}", f"VRAM_BENEFIT={vram_label}", ""])
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"REPORT={REPORT}")


if __name__ == "__main__":
    main()
