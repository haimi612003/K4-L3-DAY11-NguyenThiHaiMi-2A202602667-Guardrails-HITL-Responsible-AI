"""
Local UI server for the Day 11 lab — stdlib only, no extra dependencies.

    python ui/server.py            # then open http://127.0.0.1:8011

Endpoints
  GET  /api/results   read-only view of outputs/*.json (never writes them)
  POST /api/check     run the guardrail functions offline (no LLM call)
  POST /api/chat      send one message through the Blue DefensePipeline
                      (rate limit → input guardrail → OpenRouter → output guardrail)

The server keeps its own audit/monitor instances in memory, so chatting here
never overwrites the graded files in outputs/.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_DIR = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
sys.path.insert(0, str(ROOT / "src"))

from assignment.pipeline import (  # noqa: E402  (needs sys.path above)
    DefensePipeline,
    build_observability,
    build_production_plugins,
    is_egress_allowed,
)
from guardrails.input_guardrails import detect_injection, topic_filter  # noqa: E402
from guardrails.output_guardrails import (  # noqa: E402
    contains_protected_secret,
    content_filter,
)

HOST, PORT = "127.0.0.1", 8011
MAX_BODY = 20_000

DAILY_QUOTA_MARKER = "free-models-per-day"
DAILY_QUOTA_MESSAGE = (
    "OpenRouter đã hết hạn mức 50 lượt gọi model free trong ngày, nên Blue chưa thể "
    "trả lời câu hỏi hợp lệ. Hạn mức reset lúc 07:00 sáng (giờ VN); nạp 10 credits "
    "vào OpenRouter sẽ nâng lên 1000 lượt/ngày. Các câu bị guardrail chặn và "
    "Playground vẫn chạy bình thường vì không gọi model."
)


class UIDefensePipeline(DefensePipeline):
    """Same pipeline, but fail fast on the daily free-tier cap: retrying a
    per-day limit only makes the chat hang for ~90s before erroring anyway."""

    async def _chat_with_retry(self, text: str, attempts: int = 4) -> str:
        from openai import RateLimitError

        agent, runner = self._llm()
        for attempt in range(attempts):
            try:
                return await runner.chat(agent, text)
            except RateLimitError as e:
                if DAILY_QUOTA_MARKER in str(e) or attempt == attempts - 1:
                    raise
                await asyncio.sleep(10 * (attempt + 1))


# One long-lived pipeline so the per-user rate limiter keeps its window.
_plugins = build_production_plugins()
_audit, _monitor = build_observability()
PIPELINE = UIDefensePipeline(_plugins, _audit, _monitor)

# Dedicated event loop: HTTP handler threads submit coroutines to it.
LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()


def _load(name: str):
    path = OUTPUTS / name
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def results_payload() -> dict:
    audit = _load("audit_log.json") or []
    latencies = [e["latency_ms"] for e in audit if e.get("latency_ms") is not None]
    # Safe-query latency only: the concurrent spam burst queues behind the LLM
    llm_latencies = [
        e["latency_ms"] for e in audit
        if e.get("latency_ms") is not None and e.get("user_id") == "customer-safe"
    ]
    return {
        "results": _load("results.json"),
        "attacks": _load("attack_results.json"),
        "metrics": _load("metrics.json"),
        "audit": {
            "count": len(audit),
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "avg_llm_latency_ms": (
                round(sum(llm_latencies) / len(llm_latencies), 1) if llm_latencies else None
            ),
            "recent": audit[-12:],
        },
    }


def check_payload(text: str, destination: str | None) -> dict:
    out = content_filter(text)
    payload = {
        "injection": detect_injection(text),
        "topic": topic_filter(text),
        "output": {**out, "secret_obfuscated": contains_protected_secret(text)},
    }
    if destination:
        payload["egress"] = is_egress_allowed(destination, text)
    return payload


class Handler(SimpleHTTPRequestHandler):
    server_version = "GuardrailsUI/1.0"

    def log_message(self, fmt, *args):  # quieter console: API calls only
        if self.path.startswith("/api/"):
            sys.stderr.write(f"[ui] {self.command} {self.path} {args[1] if len(args) > 1 else ''}\n")

    def _json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def do_GET(self):
        if self.path.split("?")[0] == "/api/results":
            return self._json(results_payload())
        if self.path.startswith("/api/"):
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        return super().do_GET()

    def do_POST(self):
        data = self._read_json()
        if data is None:
            return self._json({"error": "invalid JSON body"}, HTTPStatus.BAD_REQUEST)

        if self.path == "/api/check":
            text = str(data.get("text", ""))
            return self._json(check_payload(text, data.get("destination")))

        if self.path == "/api/chat":
            message = str(data.get("message", ""))
            user_id = str(data.get("user_id") or "ui-guest")[:64]
            future = asyncio.run_coroutine_threadsafe(
                PIPELINE.handle(message, user_id=user_id), LOOP
            )
            try:
                row = future.result(timeout=240)
            except Exception as e:  # timeout or unexpected pipeline error
                return self._json(
                    {"error": f"{type(e).__name__}: {e}"}, HTTPStatus.GATEWAY_TIMEOUT
                )
            row = dict(row)
            # Full reply for the chat bubble (the suite keeps only a preview)
            last = _audit.logs[-1] if _audit.logs else {}
            row["response"] = last.get("output") or row.get("response_preview", "")
            if DAILY_QUOTA_MARKER in (row.get("error") or ""):
                row["response"] = DAILY_QUOTA_MESSAGE
            row["latency_ms"] = last.get("latency_ms")
            row["metrics"] = _monitor.snapshot()
            return self._json(row)

        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def main():
    handler = partial(Handler, directory=str(UI_DIR))
    server = ThreadingHTTPServer((HOST, PORT), handler)
    print(f"Guardrails UI → http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        LOOP.call_soon_threadsafe(LOOP.stop)


if __name__ == "__main__":
    main()
