# CPA Context Bridge

Tiny reverse proxy for [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) that enriches the standard OpenAI-compatible `GET /v1/models` response with model context metadata from CLIProxyAPI's Codex-client catalog.

It exists for clients such as Hermes Agent that probe plain `/v1/models` and expect context metadata like `context_length`, while CLIProxyAPI currently exposes that data through `/v1/models?client_version=...`.

## Behavior

- `GET /v1/models`:
  - fetches upstream `/v1/models`
  - fetches upstream `/v1/models?client_version=0.133.0`
  - merges useful metadata by `data[].id == models[].slug`
  - injects `context_length` from `context_window`
- Everything else, including streaming `/v1/chat/completions`, is byte-streamed through unchanged.

If enrichment fails, the bridge returns the plain upstream `/v1/models` response. Completion requests are not coupled to enrichment.

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
| `ENRICH_MODE` | `useful` | `minimal`, `useful`, or `all`. |
| `REQUEST_TIMEOUT_SECONDS` | `60` | Upstream connect timeout. Streaming responses have no total timeout. |

## Enrichment modes

- `minimal`: only context/completion fields.
- `useful`: context plus capabilities/reasoning/search/display metadata.
- `all`: all Codex catalog fields except known noisy/client-specific fields.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
python -m cpa_context_bridge.app
```

Health check:

```bash
curl http://127.0.0.1:58318/healthz
```
