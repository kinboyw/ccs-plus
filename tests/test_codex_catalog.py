from __future__ import annotations

import pytest

from ccs_plus.codex_catalog import build_model_catalog, parse_model_catalog
from ccs_plus.domain import CodexModel, ProviderError


def test_catalog_converts_cc_switch_metadata_to_native_models() -> None:
    models = parse_model_catalog(
        {
            "models": [
                {
                    "model": "grok-4.7",
                    "displayName": "Grok 编程",
                    "contextWindow": 500_000,
                    "inputModalities": ["text", "image"],
                    "reasoningLevels": ["low", "medium", "high", "xhigh"],
                },
                {"model": "grok-build-0.1"},
            ]
        },
        "grok-provider",
    )

    first, second = build_model_catalog(models, "grok-4.7", "xhigh")["models"]

    assert first["slug"] == "grok-4.7"
    assert first["display_name"] == "Grok 编程"
    assert first["context_window"] == first["max_context_window"] == 500_000
    assert first["input_modalities"] == ["text", "image"]
    assert first["default_reasoning_level"] == "xhigh"
    assert [level["effort"] for level in first["supported_reasoning_levels"]] == [
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    assert first["visibility"] == "list"
    assert first["supported_in_api"] is True
    assert second["slug"] == second["display_name"] == "grok-build-0.1"
    assert second["supported_reasoning_levels"] == []
    assert second["default_reasoning_level"] is None
    assert "context_window" not in second


@pytest.mark.parametrize("catalog", [None, {"models": []}])
def test_absent_catalog_falls_back_to_only_the_selected_model(catalog) -> None:
    models = parse_model_catalog(catalog, "grok-provider")

    entries = build_model_catalog(models, "grok-4.7", "high")["models"]

    assert [entry["slug"] for entry in entries] == ["grok-4.7"]
    assert entries[0]["default_reasoning_level"] == "high"
    assert entries[0]["supported_reasoning_levels"] == [{"effort": "high", "description": "high"}]


def test_model_override_is_added_without_changing_provider_metadata() -> None:
    models = (CodexModel(slug="grok-4.7", display_name="Grok", reasoning_levels=("high",)),)

    entries = build_model_catalog(models, "custom-model", "low")["models"]

    assert [entry["slug"] for entry in entries] == ["grok-4.7", "custom-model"]
    assert entries[0]["default_reasoning_level"] == "high"
    assert entries[1]["default_reasoning_level"] == "low"
    assert len(models) == 1


def test_reasoning_levels_allow_provider_defined_values() -> None:
    models = parse_model_catalog(
        {"models": [{"model": "custom-model", "reasoningLevels": ["none", "max", "max"]}]},
        "provider",
    )

    entry = build_model_catalog(models, None, "high")["models"][0]

    assert entry["default_reasoning_level"] == "none"
    assert [level["effort"] for level in entry["supported_reasoning_levels"]] == ["none", "max"]


@pytest.mark.parametrize(
    "catalog",
    [
        "catalog.json",
        {},
        {"models": {}},
        {"models": [None]},
        {"models": [{"model": " "}]},
        {"models": [{"model": "same"}, {"model": "same"}]},
        {"models": [{"model": "grok", "displayName": 123}]},
        {"models": [{"model": "grok", "contextWindow": True}]},
        {"models": [{"model": "grok", "contextWindow": 0}]},
        {"models": [{"model": "grok", "inputModalities": "image"}]},
        {"models": [{"model": "grok", "inputModalities": []}]},
        {"models": [{"model": "grok", "inputModalities": ["audio"]}]},
        {"models": [{"model": "grok", "reasoningLevels": "high"}]},
        {"models": [{"model": "grok", "reasoningLevels": [None]}]},
    ],
)
def test_invalid_catalog_reports_provider_instead_of_inheriting_global_models(catalog) -> None:
    with pytest.raises(ProviderError, match="Codex provider bad-provider modelCatalog"):
        parse_model_catalog(catalog, "bad-provider")


def test_no_catalog_or_selected_model_is_an_explicit_error() -> None:
    with pytest.raises(ProviderError, match="Codex model is required"):
        build_model_catalog((), None, None)
