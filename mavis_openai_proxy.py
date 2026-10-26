"""Loopback OpenAI-compatible adapter for the MAVIS chat API.

Copilot CLI speaks OpenAI-compatible chat completions in BYOK mode. MAVIS
exposes a different session/message SSE API, so this adapter translates the
request and response formats locally. OAuth tokens remain in keyring storage.
"""

from __future__ import annotations

import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from fetch_copilot_context import Config, OAuthError, authenticate, env, load_dotenv_file


@dataclass(frozen=True)
class MavisConfig:
    api_base_url: str
    user_id: str
    model_id: str
    host: str
    port: int
    web_search: bool

    @classmethod
    def load(cls) -> "MavisConfig":
        api_base_url = env("MAVIS_API_BASE_URL")
        user_id = env("MAVIS_USER_ID")
        model_id = env("MAVIS_MODEL_ID")
        if not api_base_url or not user_id or not model_id:
            raise ValueError(
                "MAVIS_API_BASE_URL, MAVIS_USER_ID, and MAVIS_MODEL_ID are required"
            )
        try:
            port = int(env("MAVIS_PROXY_PORT", "8765"))
        except ValueError as exc:
            raise ValueError("MAVIS_PROXY_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("MAVIS_PROXY_PORT must be between 1 and 65535")
        return cls(
            api_base_url=api_base_url.rstrip("/"),
            user_id=user_id,
            model_id=model_id,
            host=env("MAVIS_PROXY_HOST", "127.0.0.1"),
            port=port,
            web_search=env("MAVIS_WEB_SEARCH", "false").lower() == "true",
        )


def api_request(
    config: MavisConfig,
    token: dict[str, Any],
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{config.api_base_url}/{path.lstrip('/')}",
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token['access_token']}",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"MAVIS API returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("MAVIS API request failed") from exc


def message_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
            )
        if content:
            parts.append(f"{role.upper()}:\n{content}")
    return "\n\n".join(parts)


def create_session(config: MavisConfig, token: dict[str, Any]) -> str:
    status, _, raw = api_request(
        config,
        token,
        "POST",
        "/chat/sessions",
        {"user_id": config.user_id, "model_id": config.model_id},
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"MAVIS session creation returned HTTP {status}")
    try:
        session = json.loads(raw.decode("utf-8"))
        session_id = session["session_id"]
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("MAVIS session response was invalid") from exc
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("MAVIS session response did not contain a session ID")
    return session_id


def stream_message(
    config: MavisConfig,
    token: dict[str, Any],
    session_id: str,
    text: str,
    web_search: bool,
):
    status, headers, raw = api_request(
        config,
        token,
        "POST",
        f"/chat/{session_id}/message/stream",
        {
            "user_id": config.user_id,
            "text": text,
            "model_id": config.model_id,
            "web_search": web_search,
            "image_ids": [],
        },
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"MAVIS stream returned HTTP {status}")
    content_type = headers.get("Content-Type", "")
    if "text/event-stream" not in content_type:
        raise RuntimeError("MAVIS response was not an SSE stream")
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("text"), str):
            yield event["text"]


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "MavisOpenAIProxy/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/health", "/v1/health"}:
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": {"message": "Not found", "type": "invalid_request_error"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/v1/chat/completions", "/chat/completions"}:
            self._send_json(404, {"error": {"message": "Not found", "type": "invalid_request_error"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            response = self.server.adapter.handle(request)  # type: ignore[attr-defined]
            if request.get("stream"):
                self._send_stream(response)
            else:
                self._send_json(200, response)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
        except (OAuthError, RuntimeError) as exc:
            self._send_json(502, {"error": {"message": str(exc), "type": "api_error"}})

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_stream(self, response: dict[str, Any]) -> None:
        choice = response["choices"][0]
        content = choice["message"].get("content", "")
        chunks = [content[index : index + 512] for index in range(0, len(content), 512)]
        encoded_chunks: list[bytes] = []
        for index, chunk in enumerate(chunks):
            payload = {
                "id": response["id"],
                "object": "chat.completion.chunk",
                "created": response["created"],
                "model": response["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": (
                            {"role": "assistant", "content": chunk}
                            if index == 0
                            else {"content": chunk}
                        ),
                        "finish_reason": None,
                    }
                ],
            }
            encoded_chunks.append(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
        final = {
            "id": response["id"],
            "object": "chat.completion.chunk",
            "created": response["created"],
            "model": response["model"],
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        encoded_chunks.append(f"data: {json.dumps(final)}\n\n".encode("utf-8"))
        encoded_chunks.append(b"data: [DONE]\n\n")
        encoded = b"".join(encoded_chunks)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
        self.wfile.flush()

    def log_message(self, *_args: Any) -> None:
        return


class Adapter:
    def __init__(self, config: MavisConfig, oauth: Config) -> None:
        self.config = config
        self.oauth = oauth

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        messages = request.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty array")
        if any(not isinstance(message, dict) for message in messages):
            raise ValueError("messages must contain objects")
        if request.get("tools"):
            raise ValueError(
                "MAVIS adapter currently does not support OpenAI tool definitions"
            )
        token = authenticate(self.oauth)
        text = message_text(messages)
        session_id = create_session(self.config, token)
        chunks = stream_message(
            self.config,
            token,
            session_id,
            text,
            bool(request.get("web_search", self.config.web_search)),
        )
        content = "".join(chunks)
        response_id = f"chatcmpl-{secrets.token_hex(12)}"
        return {
            "id": response_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.get("model", self.config.model_id),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }


def main() -> int:
    try:
        load_dotenv_file()
        oauth = Config.load(require_api=False)
        config = MavisConfig.load()
        adapter = Adapter(config, oauth)
        server = ThreadingHTTPServer((config.host, config.port), ProxyHandler)
        server.adapter = adapter  # type: ignore[attr-defined]
        print(
            f"MAVIS BYOK adapter listening on http://{config.host}:{config.port}/v1"
        )
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (ValueError, OAuthError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
