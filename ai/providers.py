"""Validation and model discovery for orcad-managed compatible endpoints."""
from __future__ import annotations

import ipaddress
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

MAX_MODELS_RESPONSE = 1024 * 1024
API_TYPES = ("openai-completions", "openai-responses", "anthropic-messages", "google-generative-ai")
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def custom_provider_id(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:40].rstrip("-")
    if not value or not _PROVIDER_ID.fullmatch(value):
        raise ValueError("Provider name must include an ASCII letter or number")
    return value


def validate_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Enter a valid HTTP(S) API base URL")
    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    if (len(url) > 500 or parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
            or any(ord(char) < 0x20 for char in url)):
        raise ValueError("Enter a valid HTTP(S) API base URL without credentials, query, or fragment")
    if parsed.scheme == "http":
        host = parsed.hostname.lower().rstrip(".")
        local = host in ("localhost", "host.docker.internal") or host.endswith((".localhost", ".local"))
        try:
            local = local or ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if not local:
            raise ValueError("Use HTTPS for remote endpoints; plain HTTP is limited to local/private hosts")
    return url


def _clean_models(items) -> list[dict[str, str]]:
    if not isinstance(items, list):
        raise ValueError("The endpoint response did not include a model list")
    result = []
    seen = set()
    for item in items[:100]:
        if isinstance(item, str):
            model_id, name = item.strip(), item.strip()
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("name")
            name = item.get("name") or model_id
            if not isinstance(model_id, str) or not isinstance(name, str):
                continue
            model_id, name = model_id.strip(), name.strip()
        else:
            continue
        if not model_id or len(model_id) > 160 or any(ord(char) < 0x20 for char in model_id) or model_id in seen:
            continue
        result.append({"id": model_id, "name": name[:160] or model_id})
        seen.add(model_id)
    if not result:
        raise ValueError("No model IDs were found at this endpoint")
    return result


def detect_models(base_url: str, api_key: str | None = None) -> list[dict[str, str]]:
    base = validate_base_url(base_url)
    if api_key is not None and (not isinstance(api_key, str) or len(api_key) > 4096 or "\r" in api_key or "\n" in api_key):
        raise ValueError("The API key is invalid")
    headers = {"Accept": "application/json", "User-Agent": "orcad-plugin"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(base + "/models", headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            if not response.geturl().startswith(("https://", "http://")):
                raise ValueError("The model endpoint redirected to an unsupported URL")
            size = int(response.headers.get("Content-Length") or 0)
            if size > MAX_MODELS_RESPONSE:
                raise ValueError("The endpoint response is too large")
            body = response.read(MAX_MODELS_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            raise ValueError("The endpoint redirected; enter its final API base URL") from None
        raise ValueError(f"Model discovery failed (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError("Could not reach the model endpoint") from None
    if len(body) > MAX_MODELS_RESPONSE:
        raise ValueError("The endpoint response is too large")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("The endpoint did not return valid JSON") from None
    items = payload.get("data", payload.get("models")) if isinstance(payload, dict) else payload
    return _clean_models(items)


def build_models_config(providers: list[dict[str, object]]) -> dict[str, object]:
    configured = {}
    for provider in providers:
        provider_id = str(provider["id"])
        env_name = "ORCAD_CUSTOM_" + provider_id.upper().replace("-", "_") + "_API_KEY"
        configured[provider_id] = {
            "name": provider["name"],
            "baseUrl": provider["baseUrl"],
            "api": provider["api"],
            "apiKey": f"${{{env_name}}}",
            "models": [{"id": model["id"], "name": model["name"]} for model in provider["models"]],
        }
    return {"providers": configured}
