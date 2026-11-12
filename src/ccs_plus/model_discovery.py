from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from ccs_plus.domain import CodexModel, CodexRuntime

logger = logging.getLogger(__name__)
_TIMEOUT = 3.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def discover_model_catalog(
    runtime: CodexRuntime, environment: Mapping[str, str]
) -> tuple[CodexModel, ...]:
    if runtime.provider.is_official or not runtime.endpoint or not runtime.api_key:
        return runtime.model_catalog
    try:
        url = _models_url(runtime.endpoint)
        with (
            httpx.Client(
                proxy=_proxy(url, environment),
                trust_env=False,
                timeout=_TIMEOUT,
                follow_redirects=False,
            ) as client,
            client.stream(
                "GET",
                url,
                headers={"Authorization": f"Bearer {runtime.api_key}", "User-Agent": "ccs-plus"},
            ) as response,
        ):
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                content.extend(chunk)
                if len(content) > _MAX_RESPONSE_BYTES:
                    raise ValueError("Model discovery response is too large.")
        slugs = _model_ids(json.loads(content))
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as exc:
        reason = (
            f"HTTP {exc.response.status_code}"
            if isinstance(exc, httpx.HTTPStatusError)
            else type(exc).__name__
        )
        logger.warning(
            "Codex model discovery failed (%s); using the selected provider's configured models.",
            reason,
        )
        return runtime.model_catalog
    configured = {model.slug: model for model in runtime.model_catalog}
    discovered = [configured.get(slug) or CodexModel(slug, slug) for slug in slugs]
    discovered.extend(model for model in runtime.model_catalog if model.slug not in slugs)
    logger.info("Discovered %d upstream Codex models for the selected provider.", len(slugs))
    return tuple(discovered)


def _models_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Model discovery requires an HTTP(S) endpoint.")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/models", parsed.query, "")
    )


def _proxy(url: str, environment: Mapping[str, str]) -> str | None:
    parsed = urlsplit(url)
    no_proxy = environment.get("no_proxy") or environment.get("NO_PROXY")
    if no_proxy:
        hostname = (parsed.hostname or "").lower()
        host_with_port = parsed.netloc.lower()
        for entry in no_proxy.split(","):
            pattern = entry.strip().lstrip(".").lower()
            if pattern and (
                pattern == "*"
                or hostname == pattern
                or host_with_port == pattern
                or hostname.endswith(f".{pattern}")
                or host_with_port.endswith(f".{pattern}")
            ):
                return None
    return (
        environment.get(f"{parsed.scheme}_proxy")
        or environment.get(f"{parsed.scheme.upper()}_PROXY")
        or environment.get("all_proxy")
        or environment.get("ALL_PROXY")
        or None
    )


def _model_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping) or not isinstance(value.get("data"), list):
        raise ValueError("Model discovery response must contain a data array.")
    slugs = tuple(
        dict.fromkeys(
            entry["id"].strip()
            for entry in value["data"]
            if isinstance(entry, Mapping)
            and isinstance(entry.get("id"), str)
            and entry["id"].strip()
        )
    )
    if not slugs:
        raise ValueError("Model discovery returned no valid model IDs.")
    return slugs
