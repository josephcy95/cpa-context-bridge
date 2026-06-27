"""Context-window catalog for the CPA context bridge.

Builds a ``slug -> ContextInfo`` map from baked snapshots:

* CLIProxyAPI ``codex_client_models.json`` — machine-true effective windows for
  the Codex channel.
* CLIProxyAPI ``models.json`` — Antigravity + other per-model windows.
* models.dev ``modelsdev.json`` — nominal/direct-API windows for everything else.

These are the *fallback* sources. The bridge first tries CPA's live
``/v1/models?client_version=`` catalog at request time; this module covers
models that live catalog doesn't return (e.g. 9router-upstream free providers).

Precedence inside the baked map:
    ``codex_client_models.json`` is authoritative for any slug it contains (its
    ``context_window`` is the effective channel cap). ``models.json`` fills the
    gaps. On intra-``models.json`` slug conflicts we keep the smallest
    (conservative). models.dev is layered separately and only consulted when the
    CLIProxyAPI catalog misses.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("cpa-context-bridge.catalog")

DATA_DIR = Path(__file__).resolve().parent / "data"
CODEX_FILE = DATA_DIR / "codex_client_models.json"
MODELS_FILE = DATA_DIR / "models.json"
MODELSDEV_FILE = DATA_DIR / "modelsdev.json"

CODEX_URL = (
    "https://raw.githubusercontent.com/router-for-me/CLIProxyAPI/"
    "refs/heads/main/internal/registry/models/codex_client_models.json"
)
MODELS_URL = (
    "https://raw.githubusercontent.com/router-for-me/CLIProxyAPI/"
    "refs/heads/main/internal/registry/models/models.json"
)
MODELSDEV_URL = "https://models.dev/api.json"

_CONTEXT_KEYS = ("context_window", "context_length", "inputTokenLimit", "max_context_window")
_MAXOUT_KEYS = ("max_completion_tokens", "max_output_tokens", "outputTokenLimit")


@dataclass(frozen=True)
class ContextInfo:
    context_length: int
    max_completion_tokens: int | None = None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value > 0 and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except (ValueError, AttributeError):
            return None
        return parsed if parsed > 0 else None
    return None


def _entry_id(entry: dict[str, Any]) -> str | None:
    value = entry.get("slug") or entry.get("id") or entry.get("name")
    return value if isinstance(value, str) and value else None


def _first_positive(entry: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        found = _positive_int(entry.get(key))
        if found is not None:
            return found
    return None


def normalize_slug(slug: str) -> str:
    """Canonical key for lookups: lowercased, dots folded to dashes."""
    return slug.strip().lower().replace(".", "-")


def strip_alias(model_id: str) -> str:
    """Drop a leading ``alias/`` prefix (e.g. ``opencode/``)."""
    return model_id.split("/", 1)[1] if "/" in model_id else model_id


def build_map(codex_json: dict[str, Any], models_json: dict[str, Any]) -> dict[str, ContextInfo]:
    """Build a ``normalized-slug -> ContextInfo`` map from the two CPA catalogs."""
    out: dict[str, ContextInfo] = {}

    if isinstance(models_json, dict):
        for _group, entries in models_json.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                mid = _entry_id(entry)
                ctx = _first_positive(entry, _CONTEXT_KEYS)
                if not mid or ctx is None:
                    continue
                key = normalize_slug(mid)
                maxout = _first_positive(entry, _MAXOUT_KEYS)
                existing = out.get(key)
                if existing is None or ctx < existing.context_length:
                    out[key] = ContextInfo(ctx, maxout)

    codex_models = codex_json.get("models") if isinstance(codex_json, dict) else None
    if isinstance(codex_models, list):
        for entry in codex_models:
            if not isinstance(entry, dict):
                continue
            mid = _entry_id(entry)
            ctx = _positive_int(entry.get("context_window")) or _positive_int(
                entry.get("max_context_window")
            )
            if not mid or ctx is None:
                continue
            maxout = _first_positive(entry, _MAXOUT_KEYS)
            out[normalize_slug(mid)] = ContextInfo(ctx, maxout)

    return out


def build_modelsdev_map(modelsdev_json: dict[str, Any]) -> dict[str, ContextInfo]:
    """Build a ``normalized-slug -> ContextInfo`` map from models.dev.

    models.dev shape: ``{provider_id: {..., models: {model_id: {limit: {
    context, output}}}}}``. model_id is often ``vendor/name``; key on both the
    full id and the bare name. On slug collisions keep the largest context.
    """
    out: dict[str, ContextInfo] = {}
    if not isinstance(modelsdev_json, dict):
        return out
    for _pid, pval in modelsdev_json.items():
        if not isinstance(pval, dict):
            continue
        models = pval.get("models")
        if not isinstance(models, dict):
            continue
        for mid, mval in models.items():
            if not isinstance(mval, dict):
                continue
            limit = mval.get("limit") or {}
            ctx = _positive_int(limit.get("context"))
            if ctx is None:
                continue
            maxout = _positive_int(limit.get("output"))
            info = ContextInfo(ctx, maxout)
            bare = mid.split("/", 1)[1] if "/" in mid else mid
            for key in {normalize_slug(bare), normalize_slug(mid)}:
                existing = out.get(key)
                if existing is None or ctx > existing.context_length:
                    out[key] = info
    return out


def lookup(ctx_map: dict[str, ContextInfo], model_id: str) -> ContextInfo | None:
    """Resolve a (possibly alias-prefixed) model id to ContextInfo."""
    slug = strip_alias(model_id)
    key = normalize_slug(slug)
    if key in ctx_map:
        return ctx_map[key]
    alt = key.replace("-", ".")
    return ctx_map.get(alt)


def load_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_baked() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Load the baked snapshots shipped with the image. Never raises.

    Returns ``(codex, models, modelsdev)``.
    """
    codex: dict[str, Any] = {}
    models: dict[str, Any] = {}
    modelsdev: dict[str, Any] = {}
    try:
        codex = load_json_file(CODEX_FILE)
    except Exception as exc:  # noqa: BLE001 - baked load must be non-fatal
        log.warning("failed to load baked codex catalog %s: %s", CODEX_FILE, exc)
    try:
        models = load_json_file(MODELS_FILE)
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to load baked models catalog %s: %s", MODELS_FILE, exc)
    try:
        modelsdev = load_json_file(MODELSDEV_FILE)
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to load baked models.dev catalog %s: %s", MODELSDEV_FILE, exc)
    return codex, models, modelsdev


def parse_context_overrides(raw: str | None) -> dict[str, ContextInfo]:
    """Parse CONTEXT_OVERRIDES env (JSON: ``{"slug": ctx}`` or
    ``{"slug": {"context_length": N, "max_completion_tokens": M}}``)."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("CONTEXT_OVERRIDES is not valid JSON; ignoring: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, ContextInfo] = {}
    for key, value in data.items():
        nk = normalize_slug(strip_alias(str(key)))
        if isinstance(value, dict):
            ctx = _positive_int(value.get("context_length"))
            maxout = _positive_int(value.get("max_completion_tokens"))
            if ctx is not None:
                out[nk] = ContextInfo(ctx, maxout)
        else:
            ctx = _positive_int(value)
            if ctx is not None:
                out[nk] = ContextInfo(ctx)
    return out
