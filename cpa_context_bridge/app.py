import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cpa-context-bridge")

UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "http://127.0.0.1:58317").rstrip("/")
CLIENT_VERSION = os.getenv("CLIENT_VERSION", "0.133.0")
MODELS_CACHE_TTL_SECONDS = int(os.getenv("MODELS_CACHE_TTL_SECONDS", "60"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))
ENRICH_MODE = os.getenv("ENRICH_MODE", "useful").strip().lower()  # useful|all|minimal

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}

# Useful metadata to copy from CLIProxyAPI's Codex-client model catalog into
# the standard OpenAI-compatible /v1/models response. Unknown fields are safe
# for OpenAI-compatible clients; they should ignore what they do not understand.
USEFUL_CODEX_FIELDS = {
    "context_window",
    "max_context_window",
    "max_completion_tokens",
    "display_name",
    "description",
    "input_modalities",
    "supported_in_api",
    "supported_reasoning_levels",
    "default_reasoning_level",
    "supports_reasoning_summaries",
    "reasoning_summary_format",
    "default_reasoning_summary",
    "supports_parallel_tool_calls",
    "supports_search_tool",
    "web_search_tool_type",
    "support_verbosity",
    "default_verbosity",
    "service_tiers",
    "available_in_plans",
    "visibility",
    "prefer_websockets",
    "minimal_client_version",
}

# Fields that are too client-specific/noisy for default enrichment.
NOISY_CODEX_FIELDS = {
    "slug",  # maps to OpenAI id
    "priority",
    "upgrade",
    "availability_nux",
    "model_messages",
    "base_instructions",
    "shell_type",
    "truncation_policy",
    "auto_compact_token_limit",
    "additional_speed_tiers",
    "experimental_supported_tools",
    "apply_patch_tool_type",
}


@dataclass
class CacheEntry:
    expires_at: float
    status: int
    headers: dict[str, str]
    body: bytes


_models_cache: dict[str, CacheEntry] = {}
_session: ClientSession | None = None


def upstream_url(path_qs: str) -> str:
    """Build an upstream URL.

    UPSTREAM_BASE should normally be the root, e.g. http://host:58317.
    If someone sets it to http://host:58317/v1, we avoid producing /v1/v1.
    """
    if not path_qs.startswith("/"):
        path_qs = "/" + path_qs
    if UPSTREAM_BASE.endswith("/v1") and path_qs.startswith("/v1/"):
        return UPSTREAM_BASE + path_qs[len("/v1"):]
    if UPSTREAM_BASE.endswith("/v1") and path_qs == "/v1":
        return UPSTREAM_BASE
    return UPSTREAM_BASE + path_qs


def filtered_request_headers(request: web.Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        lk = key.lower()
        if lk in HOP_BY_HOP_HEADERS or lk == "host":
            continue
        headers[key] = value
    return headers


def filtered_response_headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in HOP_BY_HOP_HEADERS:
            continue
        result[key] = value
    return result


def cache_key_for_models(request: web.Request) -> str:
    # Cache varies by Authorization because model availability can be key/account-specific.
    auth = request.headers.get("Authorization", "")
    digest = hashlib.sha256(auth.encode("utf-8")).hexdigest()
    return f"{UPSTREAM_BASE}|{CLIENT_VERSION}|{ENRICH_MODE}|{digest}"


def codex_model_id(entry: dict[str, Any]) -> str | None:
    value = entry.get("slug") or entry.get("id") or entry.get("name")
    return value if isinstance(value, str) and value else None


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value > 0 and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def fields_to_copy(codex_entry: dict[str, Any]) -> dict[str, Any]:
    if ENRICH_MODE == "minimal":
        allowed = {"context_window", "max_context_window", "max_completion_tokens"}
    elif ENRICH_MODE == "all":
        allowed = {k for k in codex_entry.keys() if k not in NOISY_CODEX_FIELDS}
    else:
        allowed = USEFUL_CODEX_FIELDS
    return {k: codex_entry[k] for k in allowed if k in codex_entry}


def merge_model_metadata(openai_models_payload: dict[str, Any], codex_payload: dict[str, Any]) -> dict[str, Any]:
    """Merge CLIProxyAPI Codex model catalog metadata into OpenAI /v1/models.

    Hermes reads context from OpenAI-compatible model entries. CLIProxyAPI exposes
    the context in its Codex-client catalog as context_window. This function keeps
    the standard OpenAI response shape but adds useful non-standard metadata.
    """
    data = openai_models_payload.get("data")
    codex_models = codex_payload.get("models")
    if not isinstance(data, list) or not isinstance(codex_models, list):
        return openai_models_payload

    by_id: dict[str, dict[str, Any]] = {}
    for item in codex_models:
        if not isinstance(item, dict):
            continue
        mid = codex_model_id(item)
        if mid:
            by_id[mid] = item

    for model in data:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str):
            continue
        meta = by_id.get(model_id)
        if not meta:
            continue

        for key, value in fields_to_copy(meta).items():
            if key == "slug":
                continue
            # Do not clobber canonical OpenAI fields if upstream supplied them.
            if key in {"id", "object", "created", "owned_by"} and key in model:
                continue
            model[key] = value

        # Hermes-friendly alias. Prefer the currently usable context window over
        # theoretical max_context_window.
        ctx = positive_int(meta.get("context_window")) or positive_int(meta.get("max_context_window"))
        if ctx:
            model["context_length"] = ctx

        max_out = positive_int(meta.get("max_completion_tokens")) or positive_int(meta.get("max_output_tokens"))
        if max_out:
            model["max_completion_tokens"] = max_out

    return openai_models_payload


async def get_session() -> ClientSession:
    global _session
    if _session is None or _session.closed:
        timeout = ClientTimeout(total=None, sock_connect=REQUEST_TIMEOUT_SECONDS)
        _session = ClientSession(timeout=timeout)
    return _session


async def fetch_bytes(method: str, path_qs: str, *, headers: dict[str, str], body: bytes | None = None):
    session = await get_session()
    async with session.request(method, upstream_url(path_qs), headers=headers, data=body) as resp:
        return resp.status, filtered_response_headers(resp.headers), await resp.read()


async def enrich_models_response(request: web.Request) -> web.Response:
    key = cache_key_for_models(request)
    now = time.time()
    cached = _models_cache.get(key)
    if cached and cached.expires_at > now:
        return web.Response(status=cached.status, headers=cached.headers, body=cached.body)

    headers = filtered_request_headers(request)
    normal_status, normal_headers, normal_body = await fetch_bytes("GET", "/v1/models", headers=headers)
    if normal_status < 200 or normal_status >= 300:
        return web.Response(status=normal_status, headers=normal_headers, body=normal_body)

    try:
        normal_json = json.loads(normal_body)
    except json.JSONDecodeError:
        return web.Response(status=normal_status, headers=normal_headers, body=normal_body)

    # Best-effort enrichment. If the Codex catalog is unavailable, return the
    # standard /models response unchanged so this bridge never breaks completions.
    try:
        codex_path = f"/v1/models?client_version={CLIENT_VERSION}"
        codex_status, _codex_headers, codex_body = await fetch_bytes("GET", codex_path, headers=headers)
        if 200 <= codex_status < 300:
            codex_json = json.loads(codex_body)
            if isinstance(normal_json, dict) and isinstance(codex_json, dict):
                normal_json = merge_model_metadata(normal_json, codex_json)
    except Exception as exc:  # noqa: BLE001 - enrichment must be non-fatal
        log.warning("model metadata enrichment failed; returning plain /models: %s", exc)

    body = json.dumps(normal_json, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers_out = {k: v for k, v in normal_headers.items() if k.lower() != "content-type"}
    headers_out["Content-Type"] = "application/json; charset=utf-8"
    entry = CacheEntry(
        expires_at=now + MODELS_CACHE_TTL_SECONDS,
        status=normal_status,
        headers=headers_out,
        body=body,
    )
    _models_cache[key] = entry
    return web.Response(status=entry.status, headers=entry.headers, body=entry.body)


async def proxy_stream(request: web.Request) -> web.StreamResponse:
    session = await get_session()
    path_qs = request.rel_url.raw_path
    if request.rel_url.raw_query_string:
        path_qs += "?" + request.rel_url.raw_query_string
    headers = filtered_request_headers(request)

    # read() is fine here: chat-completion request bodies are small. Responses
    # are streamed chunk-by-chunk below, so SSE/token streaming remains intact.
    body = await request.read()
    upstream_resp = await session.request(
        request.method,
        upstream_url(path_qs),
        headers=headers,
        data=body if body else None,
    )

    response = web.StreamResponse(
        status=upstream_resp.status,
        reason=upstream_resp.reason,
        headers=filtered_response_headers(upstream_resp.headers),
    )
    await response.prepare(request)
    try:
        async for chunk in upstream_resp.content.iter_chunked(64 * 1024):
            await response.write(chunk)
    finally:
        upstream_resp.release()
    await response.write_eof()
    return response


async def handle(request: web.Request) -> web.StreamResponse:
    if request.method == "GET" and request.path.rstrip("/") == "/healthz":
        return web.json_response({"ok": True, "upstream_base": UPSTREAM_BASE})

    if (
        request.method == "GET"
        and request.path.rstrip("/") == "/v1/models"
        and "client_version" not in request.query
    ):
        return await enrich_models_response(request)

    return await proxy_stream(request)


async def close_session(app: web.Application) -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()


def create_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_route("*", "/{tail:.*}", handle)
    app.on_cleanup.append(close_session)
    return app


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "58318"))
    log.info("starting cpa-context-bridge on %s:%s -> %s", host, port, UPSTREAM_BASE)
    web.run_app(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
