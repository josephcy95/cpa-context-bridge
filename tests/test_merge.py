from cpa_context_bridge.app import merge_model_metadata


def test_merge_context_window_into_context_length():
    normal = {
        "object": "list",
        "data": [
            {"id": "gpt-5.5", "object": "model", "owned_by": "openai"},
            {"id": "other", "object": "model", "owned_by": "x"},
        ],
    }
    codex = {
        "models": [
            {
                "slug": "gpt-5.5",
                "context_window": 272000,
                "max_context_window": 272000,
                "display_name": "GPT-5.5",
                "supports_parallel_tool_calls": True,
                "base_instructions": "should not be copied by useful mode",
            }
        ]
    }

    out = merge_model_metadata(normal, codex)
    model = out["data"][0]
    assert model["context_length"] == 272000
    assert model["context_window"] == 272000
    assert model["max_context_window"] == 272000
    assert model["display_name"] == "GPT-5.5"
    assert model["supports_parallel_tool_calls"] is True
    assert "base_instructions" not in model
    assert "context_length" not in out["data"][1]


def test_fallback_to_max_context_window():
    normal = {"data": [{"id": "m", "object": "model"}]}
    codex = {"models": [{"slug": "m", "max_context_window": "12345"}]}
    out = merge_model_metadata(normal, codex)
    assert out["data"][0]["context_length"] == 12345


def test_invalid_payloads_are_unchanged():
    normal = {"data": "nope"}
    assert merge_model_metadata(normal, {"models": []}) is normal
