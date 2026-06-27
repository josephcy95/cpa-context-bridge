# CPA Context Bridge

Tiny reverse proxy for [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (CPA) that enriches the standard OpenAI-compatible `GET /v1/models` response with a reliable `context_length` per model, then byte-streams everything else (including streaming `/v1/chat/completions`) straight through.

It exists for clients such as Hermes Agent that read `context_length` from `/v1/models` to size their context budget.

## Why the layered sources

CPA serves channel-effective context for the providers it natively owns (ChatGPT/Codex, Antigravity), which is more accurate than any public catalog. But for **passthrough** providers added to CPA as upstreams (e.g. a 9router exposing free providers like OpenCode, Ollama, MiMo), CPA stamps a fabricated GPT-5.5 template (`context_length: 272000` + full metadata) onto every model it doesn't natively know. That fake 272k is worse than useless — it makes the client over-pack.

This bridge fixes that by resolving each model's context from the most trustworthy source for *that* model:

1. **`CONTEXT_OVERRIDES`** env — your hand-set / pinned values. Highest priority. Use for anything wrong or uncovered (e.g. `kr/claude-opus-4.8-thinking`).
2. **CPA native catalog** — for models whose `owned_by` is a native CPA channel (openai, antigravity, …): trust the value CPA supplies (it merges CPA's `/v1/models?client_version=` metadata). Channel-effective, machine-true.
3. **Baked CLIProxyAPI catalog** — `codex_client_models.json` + `models.json` (snapshots), for passthrough models or where CPA's value is missing. The codex file wins on shared slugs (effective, not nominal).
4. **Baked models.dev catalog** — nominal/direct-API windows for everything else (DeepSeek, Kimi, GLM, …). Correct for direct API-key providers.
5. **Nothing** — the field is omitted (honest "unknown"); the client falls back to its own default. Better than a fabricated number.

For **passthrough owners** (default `9router`, see `PASSTHROUGH_OWNERS`), CPA's fabricated value is *discarded* and re-resolved via 3→4→1, falling to blank if no source knows the model.

## Behavior

- `GET /v1/models`: fetch upstream `/v1/models` (+ CPA's `?client_version=` metadata), merge native context, then apply the layered fallback + overrides.
- Everything else, including streaming `/v1/chat/completions`, is byte-streamed through unchanged.

If enrichment fails, the bridge returns the plain upstream `/v1/models`. Completion requests are never coupled to enrichment.

## Baked snapshot & auto-refresh

Three JSON files are baked into the image so the bridge works offline:
`codex_client_models.json` + `models.json` (CLIProxyAPI) and `modelsdev.json` (models.dev). Kept current by the `Refresh catalog` GitHub Action (weekly + manual) and, optionally, by the runtime refresh (`CATALOG_REFRESH_HOURS`).

## Docker

```bash
docker run -d \
  --name cpa-context-bridge \
  --restart unless-stopped \
  -p 58318:58318 \
  -e UPSTREAM_BASE=http://192.168.1.138:58317 \
  ghcr.io/YOUR_GITHUB_USERNAME/cpa-context-bridge:latest
```

Hermes custom provider should point at the bridge:

```yaml
custom_providers:
  - name: CLI-PROXY-API
    base_url: http://192.168.1.138:58318/v1
    api_mode: chat_completions
    model: gpt-5.5
```

## Environment variables

| Variable | Default | Meaning |
|---|---:|---|
| `UPSTREAM_BASE` | `http://127.0.0.1:58317` | CLIProxyAPI root URL. Root is preferred, but `/v1` also works. |
| `PORT` | `58318` | Bridge listen port. |
| `HOST` | `0.0.0.0` | Bridge listen host. |
| `CLIENT_VERSION` | `0.133.0` | Query value used for CLIProxyAPI's Codex model catalog. |
| `MODELS_CACHE_TTL_SECONDS` | `60` | Cache TTL for enriched `/v1/models`, varied by Authorization header. |
| `ENRICH_MODE` | `useful` | `minimal`, `useful`, or `all` — how much native CPA metadata to copy. |
| `REQUEST_TIMEOUT_SECONDS` | `60` | Upstream connect timeout. Streaming responses have no total timeout. |
| `CATALOG_REFRESH_HOURS` | `24` | Re-pull the baked catalogs from their source URLs every N hours (`0` = never). |
| `PASSTHROUGH_OWNERS` | `9router` | Comma-separated `owned_by` values treated as passthrough (CPA's fabricated context discarded, re-resolved from fallback). |
| `CONTEXT_OVERRIDES` | _(empty)_ | JSON map of pinned context windows (see below). |

### CONTEXT_OVERRIDES format

Keys may be a bare slug or a full `alias/slug` id. Value is an int, or an object:

```json
{
  "kr/claude-opus-4.8-thinking": 200000,
  "oc/deepseek-v4-flash-free": { "context_length": 163840, "max_completion_tokens": 8192 }
}
```

## Enrichment modes (native CPA metadata)

- `minimal`: only context/completion fields.
- `useful`: context plus capabilities/reasoning/search/display metadata.
- `all`: all Codex catalog fields except known noisy/client-specific fields.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e . -r requirements-dev.txt
pytest -q
UPSTREAM_BASE=http://192.168.1.138:58317 python -m cpa_context_bridge.app
```

Health / introspection:

```bash
curl http://127.0.0.1:58318/healthz
# {"ok":true,"upstream_base":"...","cpa_slugs":N,"modelsdev_slugs":N,"overrides":N}
```

## Context data sources

- CLIProxyAPI registry (channel-effective windows for native owners):
  - [`codex_client_models.json`](https://github.com/router-for-me/CLIProxyAPI/blob/main/internal/registry/models/codex_client_models.json)
  - [`models.json`](https://github.com/router-for-me/CLIProxyAPI/blob/main/internal/registry/models/models.json)
- [models.dev](https://models.dev/api.json) — nominal/direct-API windows for everything else.
