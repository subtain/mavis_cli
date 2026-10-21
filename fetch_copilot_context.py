"""Fetch sanitized OAuth-protected API data for use as Copilot context.

Provider-specific URLs, scopes, client IDs, and endpoints are intentionally
loaded from environment variables. This module never prints or writes tokens.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import keyring
except ImportError as exc:  # pragma: no cover - exercised in an uninstalled environment
    raise SystemExit(
        "Missing dependency: install requirements.txt with Python 3.11+."
    ) from exc


ROOT = Path(__file__).resolve().parent
JSON_OUTPUT = ROOT / "copilot-context.json"
MARKDOWN_OUTPUT = ROOT / "copilot-context.md"
TOKEN_SERVICE_PREFIX = "copilot-context"

SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "refresh_token",
    "secret",
    "set-cookie",
    "token",
}
PERSONAL_KEYS = {
    "address",
    "date_of_birth",
    "email",
    "phone",
    "mobile",
    "ssn",
}
DEFAULT_FIELDS = {
    "created_at",
    "description",
    "id",
    "name",
    "status",
    "summary",
    "title",
    "type",
    "updated_at",
    "url",
}


class ConfigurationError(ValueError):
    """Raised when required, provider-specific configuration is missing."""


class OAuthError(RuntimeError):
    """Raised when an OAuth operation fails without exposing credentials."""


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_dotenv_file() -> None:
    """Load simple KEY=VALUE entries without overriding process variables."""
    dotenv = ROOT / ".env"
    if not dotenv.exists():
        return
    for raw_line in dotenv.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if name and name not in os.environ:
            os.environ[name] = value


def required(name: str) -> str:
    value = env(name)
    if not value or value.startswith("<") or value.endswith(">"):
        raise ConfigurationError(
            f"{name} is not configured. Copy .env.example and provide the "
            "provider-issued value."
        )
    return value


def integer_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(env(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class Config:
    provider_name: str
    api_base_url: str
    endpoints: tuple[str, ...]
    client_id: str
    client_secret: str
    auth_url: str
    token_url: str
    device_url: str
    scopes: str
    flow: str
    page_param: str
    page_size_param: str
    page_size: int
    max_pages: int
    max_items: int
    date_from_param: str
    date_from: str
    context_fields: tuple[str, ...]
    max_output_bytes: int

    @classmethod
    def load(cls) -> "Config":
        flow = env("OAUTH_FLOW", "auto").lower()
        if flow not in {"auto", "device", "authorization_code_pkce", "client_credentials"}:
            raise ConfigurationError(
                "OAUTH_FLOW must be auto, device, authorization_code_pkce, "
                "or client_credentials"
            )
        endpoints = tuple(
            item.strip() for item in required("API_ENDPOINTS").split(",") if item.strip()
        )
        if not endpoints:
            raise ConfigurationError("API_ENDPOINTS must contain at least one endpoint")
        fields = tuple(
            item.strip() for item in env("CONTEXT_FIELDS").split(",") if item.strip()
        )
        return cls(
            provider_name=required("PROVIDER_NAME"),
            api_base_url=required("API_BASE_URL").rstrip("/"),
            endpoints=endpoints,
            client_id=required("OAUTH_CLIENT_ID"),
            client_secret=env("OAUTH_CLIENT_SECRET"),
            auth_url=env("AUTHORIZATION_URL"),
            token_url=required("TOKEN_URL"),
            device_url=env("DEVICE_AUTHORIZATION_URL"),
            scopes=required("SCOPES"),
            flow=flow,
            page_param=env("API_PAGE_PARAM", "page"),
            page_size_param=env("API_PAGE_SIZE_PARAM", "page_size"),
            page_size=integer_env("API_PAGE_SIZE", 50),
            max_pages=integer_env("API_MAX_PAGES", 20),
            max_items=integer_env("API_MAX_ITEMS", 500),
            date_from_param=env("API_DATE_FROM_PARAM"),
            date_from=env("API_DATE_FROM"),
            context_fields=fields or tuple(sorted(DEFAULT_FIELDS)),
            max_output_bytes=integer_env("MAX_OUTPUT_BYTES", 1_000_000),
        )


def form_post(url: str, data: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error_body = json.loads(exc.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            error_body = {}
        error_code = error_body.get("error")
        if isinstance(error_code, str) and error_code:
            raise OAuthError(f"OAuth request failed: {error_code}") from exc
        raise OAuthError(f"OAuth request failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise OAuthError("OAuth request failed") from exc


def token_service(config: Config) -> str:
    safe_name = "".join(
        char if char.isalnum() or char in "-_" else "_" for char in config.provider_name
    )
    return f"{TOKEN_SERVICE_PREFIX}:{safe_name}"


def load_token(config: Config) -> dict[str, Any] | None:
    stored = keyring.get_password(token_service(config), config.client_id)
    if not stored:
        return None
    try:
        token = json.loads(stored)
    except json.JSONDecodeError:
        keyring.delete_password(token_service(config), config.client_id)
        return None
    if not isinstance(token, dict) or "access_token" not in token:
        keyring.delete_password(token_service(config), config.client_id)
        return None
    return token


def save_token(config: Config, token: dict[str, Any]) -> None:
    safe_token = {
        key: value
        for key, value in token.items()
        if key in {"access_token", "refresh_token", "token_type", "scope", "expires_at"}
    }
    keyring.set_password(token_service(config), config.client_id, json.dumps(safe_token))


def token_expired(token: dict[str, Any]) -> bool:
    expires_at = token.get("expires_at")
    return not isinstance(expires_at, int) or expires_at <= int(time.time()) + 60


def with_expiry(token: dict[str, Any]) -> dict[str, Any]:
    result = dict(token)
    if "expires_in" in result:
        try:
            result["expires_at"] = int(time.time()) + int(result["expires_in"])
        except (TypeError, ValueError):
            pass
    return result


def refresh_access_token(config: Config, token: dict[str, Any]) -> dict[str, Any]:
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise OAuthError("Stored access token expired and no refresh token is available")
    refreshed = form_post(
        config.token_url,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.client_id,
            **({"client_secret": config.client_secret} if config.client_secret else {}),
        },
    )
    if "access_token" not in refreshed:
        raise OAuthError("OAuth refresh response did not contain an access token")
    refreshed.setdefault("refresh_token", refresh_token)
    refreshed = with_expiry(refreshed)
    save_token(config, refreshed)
    return refreshed


def device_flow(config: Config) -> dict[str, Any]:
    if not config.device_url:
        raise ConfigurationError(
            "DEVICE_AUTHORIZATION_URL is required for the device flow"
        )
    device = form_post(
        config.device_url,
        {"client_id": config.client_id, "scope": config.scopes},
    )
    verification_uri = device.get("verification_uri") or device.get("verification_url")
    user_code = device.get("user_code")
    device_code = device.get("device_code")
    if not verification_uri or not user_code or not device_code:
        raise OAuthError("Device authorization response was incomplete")
    print(f"Open {verification_uri} and enter the displayed device code: {user_code}")
    interval = int(device.get("interval", 5))
    expires_at = time.time() + int(device.get("expires_in", 600))
    while time.time() < expires_at:
        time.sleep(interval)
        try:
            result = form_post(
                config.token_url,
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": config.client_id,
                    **(
                        {"client_secret": config.client_secret}
                        if config.client_secret
                        else {}
                    ),
                },
            )
        except OAuthError as exc:
            message = str(exc)
            if "authorization_pending" in message:
                continue
            if "slow_down" in message:
                interval += 5
                continue
            raise
        if "access_token" in result:
            result = with_expiry(result)
            save_token(config, result)
            return result
    raise OAuthError("Device authorization timed out")


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    authorization_code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        CallbackHandler.authorization_code = query.get("code", [None])[0]
        CallbackHandler.error = query.get("error", [None])[0]
        self.send_response(200 if CallbackHandler.authorization_code else 400)
        self.end_headers()
        self.wfile.write(b"Authentication received. You may close this window.")

    def log_message(self, *_args: Any) -> None:
        return


def authorization_code_pkce(config: Config) -> dict[str, Any]:
    if not config.auth_url:
        raise ConfigurationError("AUTHORIZATION_URL is required for PKCE flow")
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode()
    server = http.server.HTTPServer(("127.0.0.1", 0), CallbackHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": redirect_uri,
            "scope": config.scopes,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    print("Complete OAuth authentication in the provider authorization page.")
    webbrowser.open(f"{config.auth_url}?{query}")
    server.handle_request()
    server.server_close()
    if CallbackHandler.error or not CallbackHandler.authorization_code:
        raise OAuthError("Authorization code flow was not completed")
    result = form_post(
        config.token_url,
        {
            "grant_type": "authorization_code",
            "client_id": config.client_id,
            "code": CallbackHandler.authorization_code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            **({"client_secret": config.client_secret} if config.client_secret else {}),
        },
    )
    if "access_token" not in result:
        raise OAuthError("Authorization response did not contain an access token")
    result = with_expiry(result)
    save_token(config, result)
    return result


def client_credentials(config: Config) -> dict[str, Any]:
    if not config.client_secret:
        raise ConfigurationError(
            "OAUTH_CLIENT_SECRET is required for client credentials flow"
        )
    result = form_post(
        config.token_url,
        {
            "grant_type": "client_credentials",
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "scope": config.scopes,
        },
    )
    if "access_token" not in result:
        raise OAuthError("Client credentials response did not contain an access token")
    return with_expiry(result)


def authenticate(config: Config) -> dict[str, Any]:
    token = load_token(config)
    if token and not token_expired(token):
        return token
    if token and token.get("refresh_token"):
        return refresh_access_token(config, token)
    flow = config.flow
    if flow == "auto":
        flow = "device" if config.device_url else "authorization_code_pkce"
    if flow == "device":
        return device_flow(config)
    if flow == "authorization_code_pkce":
        return authorization_code_pkce(config)
    if flow == "client_credentials":
        return client_credentials(config)
    raise ConfigurationError(f"Unsupported OAuth flow: {flow}")


def endpoint_url(config: Config, endpoint: str) -> str:
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    return f"{config.api_base_url}/{endpoint.lstrip('/')}"


def api_get(url: str, access_token: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"API request failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError("API request failed or returned invalid JSON") from exc


def extract_records(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return [payload]
    for key in ("items", "data", "results", "records", "value"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return [payload]


def fetch_endpoint(config: Config, endpoint: str, access_token: str) -> list[Any]:
    records: list[Any] = []
    for page in range(1, config.max_pages + 1):
        params = {
            config.page_param: str(page),
            config.page_size_param: str(config.page_size),
        }
        if config.date_from_param and config.date_from:
            params[config.date_from_param] = config.date_from
        separator = "&" if "?" in endpoint else "?"
        url = endpoint_url(config, endpoint) + separator + urllib.parse.urlencode(params)
        page_records = extract_records(api_get(url, access_token))
        records.extend(page_records)
        if len(page_records) < config.page_size or len(records) >= config.max_items:
            break
    return records[: config.max_items]


def redact(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            item_key: "[REDACTED]"
            if item_key.lower().replace("-", "_") in SENSITIVE_KEYS.union(PERSONAL_KEYS)
            else redact(item_value, item_key)
            for item_key, item_value in value.items()
            if item_key.lower().replace("-", "_")
            not in SENSITIVE_KEYS.union(PERSONAL_KEYS)
        }
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "…[TRUNCATED]"
    return value


def pick_fields(record: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {"value": record}
    result: dict[str, Any] = {}
    for field in fields:
        value: Any = record
        for component in field.split("."):
            if not isinstance(value, dict) or component not in value:
                value = None
                break
            value = value[component]
        if value is not None:
            result[field] = value
    return result


def build_context(config: Config, token: dict[str, Any]) -> dict[str, Any]:
    raw_records: list[Any] = []
    for endpoint in config.endpoints:
        raw_records.extend(fetch_endpoint(config, endpoint, token["access_token"]))
    selected = [pick_fields(record, config.context_fields) for record in raw_records]
    return {
        "provider": config.provider_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(selected),
        "records": redact(selected),
    }


def write_outputs(config: Config, context: dict[str, Any]) -> None:
    serialized = json.dumps(context, indent=2, ensure_ascii=True)
    if len(serialized.encode("utf-8")) > config.max_output_bytes:
        records = context["records"]
        while records and len(json.dumps(context).encode("utf-8")) > config.max_output_bytes:
            records.pop()
        context["truncated"] = True
        context["records"] = records
        serialized = json.dumps(context, indent=2, ensure_ascii=True)
    JSON_OUTPUT.write_text(serialized + "\n", encoding="utf-8")
    markdown = [
        f"# Copilot context: {config.provider_name}",
        "",
        f"- Generated: `{context['generated_at']}`",
        f"- Records: `{len(context['records'])}`",
        f"- Fields: `{', '.join(config.context_fields)}`",
    ]
    if context.get("truncated"):
        markdown.append("- Note: output was truncated to the configured size limit.")
    markdown.extend(["", "```json", serialized, "```", ""])
    MARKDOWN_OUTPUT.write_text("\n".join(markdown), encoding="utf-8")


def main() -> int:
    try:
        load_dotenv_file()
        config = Config.load()
        token = authenticate(config)
        context = build_context(config, token)
        write_outputs(config, context)
        print(f"Wrote sanitized context to {JSON_OUTPUT.name} and {MARKDOWN_OUTPUT.name}")
        return 0
    except (ConfigurationError, OAuthError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
