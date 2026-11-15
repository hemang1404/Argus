"""Repeatable offline fake OpenAI-compatible server for integration and unit testing."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, List, Optional
import httpx


class MockHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler simulating OpenAI chat completions endpoints."""

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stderr HTTP request logging in test runs."""
        pass

    def do_GET(self) -> None:
        if self.path in ("/v1/models", "/models"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = {
                "object": "list",
                "data": [{"id": "mock-model", "object": "model", "owned_by": "argus-test"}],
            }
            self.wfile.write(json.dumps(data).encode("utf-8"))
        elif self.path in ("/health", "/v1/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""
        
        req_data = {}
        try:
            req_data = json.loads(body) if body else {}
        except Exception:
            pass

        # Record captured request
        server: MockOpenAIServer = self.server  # type: ignore
        server.requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": req_data,
        })

        # Check for simulated errors
        if server.simulated_error:
            code, err_msg = server.simulated_error
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            err_payload = {"error": {"message": err_msg, "type": "mock_error", "code": code}}
            self.wfile.write(json.dumps(err_payload).encode("utf-8"))
            return

        # Check for response generator callback
        if server.response_callback:
            content = server.response_callback(req_data)
        elif server.responses:
            content = server.responses.pop(0) if len(server.responses) > 1 else server.responses[0]
        elif server.canned_response is not None:
            content = server.canned_response
        else:
            content = "Mock response from Argus test server."

        # Compute mock usage
        prompt_tokens = 25
        completion_tokens = len(content.split())

        resp_payload = {
            "id": f"chatcmpl-mock-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req_data.get("model", "mock-model"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(resp_payload).encode("utf-8"))


class MockOpenAIServer(HTTPServer):
    """Threaded local HTTP server simulating OpenAI-compatible endpoints."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        canned_response: Optional[str] = None,
        responses: Optional[List[str]] = None,
    ) -> None:
        super().__init__((host, port), MockHTTPHandler)
        self.requests: List[Dict[str, Any]] = []
        self.canned_response: Optional[str] = canned_response
        self.responses: List[str] = list(responses) if responses else []
        self.response_callback: Optional[Callable[[Dict[str, Any]], str]] = None
        self.simulated_error: Optional[tuple[int, str]] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        """Return the base URL including /v1 prefix."""
        host, port = self.server_address
        return f"http://{host}:{port}/v1"

    def start(self) -> MockOpenAIServer:
        """Start the mock server in a daemon background thread."""
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        # Brief pause to ensure socket is listening
        time.sleep(0.05)
        return self

    def stop(self) -> None:
        """Shutdown and close the server."""
        self.shutdown()
        self.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def __enter__(self) -> MockOpenAIServer:
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


def create_mock_transport(
    canned_response: Optional[str] = None,
    error_status: Optional[int] = None,
) -> httpx.MockTransport:
    """Create an in-memory httpx.MockTransport for ultra-fast unit testing."""
    def handler(request: httpx.Request) -> httpx.Response:
        if error_status:
            return httpx.Response(
                status_code=error_status,
                json={"error": {"message": f"Simulated error {error_status}"}},
            )

        content = canned_response or "Mock completion content"
        payload = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": "mock-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 15, "completion_tokens": 10},
        }
        return httpx.Response(status_code=200, json=payload)

    return httpx.MockTransport(handler)
