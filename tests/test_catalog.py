from cpa_context_bridge import catalog
from cpa_context_bridge.app import apply_context_fallbacks, resolve_fallback
import cpa_context_bridge.app as app


CODEX = {
    "models": [
        {"slug": "gpt-5.5", "context_window": 272000, "max_context_window": 272000},
        {"slug": "gpt-5.4", "context_window": 272000, "max_context_window": 1000000},
    ]
}
MODELS = {
    "codex-pro": [
        {"id": "gpt-5.4", "context_length": 1050000},  # nominal; codex file must win
    ],
    "antigravity": [
        {"id": "gemini-pro-agent", "context_length": 1048576},
    ],
}


def build():
    return catalog.build_map(CODEX, MODELS)


def test_codex_file_wins_over_models_json():
    m = build()
    assert m[catalog.normalize_slug("gpt-5.4")].context_length == 272000


def test_antigravity_filled_from_models_json():
    m = build()
    assert m[catalog.normalize_slug("gemini-pro-agent")].context_length == 1048576


def test_modelsdev_map_parsing():
    md = {
        "deepseek": {"models": {"deepseek-v4": {"limit": {"context": 163840, "output": 8192}}}},
        "openai": {"models": {"openai/gpt-5.4": {"limit": {"context": 1050000, "output": 128000}}}},
    }
    m = catalog.build_modelsdev_map(md)
    assert m[catalog.normalize_slug("deepseek-v4")].context_length == 163840
    assert catalog.lookup(m, "openai/gpt-5.4").context_length == 1050000


def test_resolve_fallback_prefers_cpa_then_modelsdev(monkeypatch):
    monkeypatch.setattr(app, "_cpa_map", build())
    monkeypatch.setattr(
        app, "_modelsdev_map",
        {catalog.normalize_slug("deepseek-v4"): catalog.ContextInfo(163840)},
    )
    # CPA has gpt-5.5 -> used; models.dev not consulted.
    assert resolve_fallback("gpt-5.5").context_length == 272000
    # CPA misses deepseek -> models.dev fallback.
    assert resolve_fallback("opencode/deepseek-v4").context_length == 163840
    # neither has it -> None
    assert resolve_fallback("mystery-model") is None


def test_apply_fallbacks_does_not_clobber_live_cpa(monkeypatch):
    monkeypatch.setattr(app, "_cpa_map", build())
    monkeypatch.setattr(app, "_modelsdev_map", {})
    monkeypatch.setattr(app, "_overrides", {})
    payload = {
        "data": [
            # live CPA merge already set this; fallback must NOT overwrite.
            {"id": "gpt-5.5", "object": "model", "context_length": 999},
            # not covered by live merge; fallback fills from baked CPA.
            {"id": "gemini-pro-agent", "object": "model"},
            # only in models.dev; (empty here) -> stays blank.
            {"id": "opencode/unknownish", "object": "model"},
        ]
    }
    out = apply_context_fallbacks(payload)
    assert out["data"][0]["context_length"] == 999       # preserved
    assert out["data"][1]["context_length"] == 1048576    # filled from baked
    assert "context_length" not in out["data"][2]          # unknown stays blank


def test_overrides_win_over_everything(monkeypatch):
    monkeypatch.setattr(app, "_cpa_map", build())
    monkeypatch.setattr(app, "_modelsdev_map", {})
    monkeypatch.setattr(
        app, "_overrides", catalog.parse_context_overrides('{"gpt-5.5": 123456}')
    )
    payload = {"data": [{"id": "gpt-5.5", "object": "model", "context_length": 272000}]}
    out = apply_context_fallbacks(payload)
    # override beats the already-present live value
    assert out["data"][0]["context_length"] == 123456


def test_baked_files_load_and_build():
    codex, models, modelsdev = catalog.load_baked()
    m = catalog.build_map(codex, models)
    assert len(m) > 10
    assert m[catalog.normalize_slug("gpt-5.5")].context_length == 272000
    md = catalog.build_modelsdev_map(modelsdev)
    assert len(md) > 100


def test_parse_overrides_bad_json_is_empty():
    assert catalog.parse_context_overrides("not json") == {}
    assert catalog.parse_context_overrides("") == {}


def test_passthrough_owner_skips_live_cpa_template(monkeypatch):
    # CPA stamps a fake 272k template onto passthrough models; merge must skip
    # them so the fallback chain can supply the real number.
    from cpa_context_bridge.app import merge_model_metadata
    monkeypatch.setattr(app, "PASSTHROUGH_OWNERS", {"9router"})
    normal = {
        "data": [
            {"id": "ollama/minimax-m3", "object": "model", "owned_by": "9router"},
            {"id": "gpt-5.5", "object": "model", "owned_by": "openai"},
        ]
    }
    codex = {
        "models": [
            # CPA fabricates a 272k template entry for the unknown passthrough model
            {"slug": "ollama/minimax-m3", "context_window": 272000},
            {"slug": "gpt-5.5", "context_window": 272000},
        ]
    }
    out = merge_model_metadata(normal, codex)
    # passthrough model: live template NOT applied
    assert "context_length" not in out["data"][0]
    # native model: live value applied
    assert out["data"][1]["context_length"] == 272000


def test_passthrough_then_modelsdev_fills(monkeypatch):
    monkeypatch.setattr(app, "PASSTHROUGH_OWNERS", {"9router"})
    monkeypatch.setattr(app, "_cpa_map", {})
    monkeypatch.setattr(
        app, "_modelsdev_map",
        {catalog.normalize_slug("minimax-m3"): catalog.ContextInfo(1048576)},
    )
    monkeypatch.setattr(app, "_overrides", {})
    payload = {"data": [{"id": "ollama/minimax-m3", "object": "model", "owned_by": "9router"}]}
    out = apply_context_fallbacks(payload)
    assert out["data"][0]["context_length"] == 1048576
