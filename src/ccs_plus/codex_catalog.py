from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ccs_plus.domain import CodexModel, ProviderError


def parse_model_catalog(value: object, provider_id: str) -> tuple[CodexModel, ...]:
    if value is None:
        return ()
    label = f"Codex provider {provider_id} modelCatalog"
    if not isinstance(value, Mapping) or not isinstance(value.get("models"), list):
        raise ProviderError(f"{label}.models must be a list.")
    models = []
    slugs: set[str] = set()
    for index, entry in enumerate(value["models"]):
        entry_label = f"{label}.models[{index}]"
        if not isinstance(entry, Mapping):
            raise ProviderError(f"{entry_label} must be an object.")
        slug = _string(entry.get("model"), f"{entry_label}.model")
        if slug in slugs:
            raise ProviderError(f"{entry_label}.model is duplicated.")
        slugs.add(slug)
        display_name = entry.get("displayName")
        context_window = entry.get("contextWindow")
        if context_window is not None and (type(context_window) is not int or context_window <= 0):
            raise ProviderError(f"{entry_label}.contextWindow must be a positive integer.")
        modalities = _strings(entry.get("inputModalities", ["text"]), entry_label)
        if not modalities or any(modality not in {"text", "image"} for modality in modalities):
            raise ProviderError(f"{entry_label}.inputModalities must contain text and/or image.")
        models.append(
            CodexModel(
                slug=slug,
                display_name=(
                    _string(display_name, f"{entry_label}.displayName")
                    if display_name is not None
                    else slug
                ),
                context_window=context_window,
                input_modalities=modalities,
                reasoning_levels=_strings(entry.get("reasoningLevels", []), entry_label),
            )
        )
    return tuple(models)


def build_model_catalog(
    models: tuple[CodexModel, ...], model: str | None, effort: str | None
) -> dict[str, Any]:
    entries = list(models)
    if model and not any(entry.slug == model for entry in entries):
        entries.append(
            CodexModel(
                slug=model,
                display_name=model,
                reasoning_levels=(effort,) if effort else (),
            )
        )
    if not entries:
        raise ProviderError("Codex model is required when the provider has no modelCatalog models.")
    return {"models": [_native_model(entry, index, effort) for index, entry in enumerate(entries)]}


def _native_model(model: CodexModel, priority: int, effort: str | None) -> dict[str, Any]:
    levels = model.reasoning_levels
    default_effort = (
        effort if effort in levels else "high" if "high" in levels else next(iter(levels), None)
    )
    result: dict[str, Any] = {
        "slug": model.slug,
        "display_name": model.display_name,
        "description": model.display_name,
        "default_reasoning_level": default_effort,
        "supported_reasoning_levels": [{"effort": level, "description": level} for level in levels],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "base_instructions": (
            "You are Codex, a coding agent. You and the user share the same workspace "
            "and collaborate to achieve the user's goals."
        ),
        "supports_reasoning_summaries": bool(levels),
        "default_reasoning_summary": "none",
        "support_verbosity": False,
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "supports_parallel_tool_calls": False,
        "experimental_supported_tools": [],
        "input_modalities": list(model.input_modalities),
    }
    if model.context_window is not None:
        result["context_window"] = model.context_window
        result["max_context_window"] = model.context_window
    return result


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(f"{label} must be a non-empty string.")
    return value.strip()


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProviderError(f"{label} model capabilities must be lists of strings.")
    return tuple(dict.fromkeys(_string(item, label) for item in value))
