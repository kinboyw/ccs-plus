from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from ccs_plus.adapters import build_provider, runtime_from_provider
from ccs_plus.domain import AppKind, CodexAppConfig, CodexModel, CodexRuntime, NewProvider
from ccs_plus.model_discovery import discover_model_catalog


@pytest.fixture()
def runtime() -> CodexRuntime:
    provider = build_provider(
        NewProvider(
            app=AppKind.CODEX,
            name="Selected provider",
            endpoint="https://selected.example.test/v1",
            api_key="selected-secret-key",
            model="grok-default",
            effort="high",
            notes=None,
        ),
        CodexAppConfig(approval_policy="never", sandbox_mode="danger-full-access"),
    )
    result = runtime_from_provider(provider)
    assert isinstance(result, CodexRuntime)
    return replace(
        result,
        model_catalog=(CodexModel("grok-default", "Grok", 500_000, ("text", "image"), ("high",)),),
    )


@pytest.fixture()
def mock_client(monkeypatch):
    client_type = httpx.Client

    def install(handler):
        options = []

        def factory(**kwargs):
            options.append(kwargs)
            return client_type(transport=httpx.MockTransport(handler), trust_env=False)

        monkeypatch.setattr("ccs_plus.model_discovery.httpx.Client", factory)
        return options

    return install


def test_discovery_adds_upstream_models_and_preserves_configured_metadata(runtime, mock_client):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "grok-default"},
                    {"id": "new-upstream-model"},
                    {"id": "grok-default"},
                ]
            },
        )

    options = mock_client(respond)
    result = discover_model_catalog(runtime, {})

    assert [entry.slug for entry in result] == ["grok-default", "new-upstream-model"]
    assert result[0] == runtime.model_catalog[0]
    assert result[1] == CodexModel("new-upstream-model", "new-upstream-model")
    assert str(requests[0].url) == "https://selected.example.test/v1/models"
    assert requests[0].method == "GET"
    assert requests[0].headers["Authorization"] == "Bearer selected-secret-key"
    assert options == [
        {"proxy": None, "trust_env": False, "timeout": 3.0, "follow_redirects": False}
    ]


def test_discovery_retains_configured_aliases_not_advertised_by_upstream(runtime, mock_client):
    mock_client(lambda request: httpx.Response(200, json={"data": [{"id": "new-model"}]}))

    result = discover_model_catalog(runtime, {})

    assert [entry.slug for entry in result] == ["new-model", "grok-default"]
    assert result[1] == runtime.model_catalog[0]


@pytest.mark.parametrize("catalog", [(), (CodexModel("grok-default", "Grok"),)])
def test_discovery_works_with_no_catalog_or_only_one_default_model(runtime, mock_client, catalog):
    mock_client(lambda request: httpx.Response(200, json={"data": [{"id": "another-model"}]}))

    result = discover_model_catalog(replace(runtime, model_catalog=catalog), {})

    assert result[0].slug == "another-model"


@pytest.mark.parametrize(
    "endpoint, expected",
    [
        ("https://selected.example.test/v1/", "https://selected.example.test/v1/models"),
        (
            "https://selected.example.test/api/openai/v1",
            "https://selected.example.test/api/openai/v1/models",
        ),
        ("https://selected.example.test", "https://selected.example.test/models"),
        (
            "https://selected.example.test/v1?version=1",
            "https://selected.example.test/v1/models?version=1",
        ),
    ],
)
def test_discovery_uses_the_configured_api_base_path(runtime, mock_client, endpoint, expected):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "model"}]})

    mock_client(respond)
    discover_model_catalog(replace(runtime, endpoint=endpoint), {})

    assert requests == [expected]


@pytest.mark.parametrize("status", [301, 302, 401, 403, 404, 429, 500])
def test_http_errors_and_redirects_fall_back_without_forwarding_credentials(
    runtime, mock_client, status
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://unrelated.example.test/models"})

    mock_client(respond)

    assert discover_model_catalog(runtime, {}) == runtime.model_catalog
    assert len(requests) == 1


def test_network_failure_falls_back_without_logging_secrets(runtime, mock_client, caplog):
    def fail(request):
        raise httpx.ReadTimeout(f"private upstream error containing {runtime.api_key}")

    mock_client(fail)

    assert discover_model_catalog(runtime, {}) == runtime.model_catalog
    assert "ReadTimeout" in caplog.text
    assert runtime.api_key not in caplog.text


@pytest.mark.parametrize(
    "content",
    [
        b"not JSON",
        b"null",
        b"[]",
        b"{}",
        b'{"data": []}',
        b'{"data": {}}',
        b'{"data": [{"id": " "}, {"id": null}, 12]}',
    ],
)
def test_empty_or_invalid_responses_fall_back(runtime, mock_client, content):
    mock_client(lambda request: httpx.Response(200, content=content))

    assert discover_model_catalog(runtime, {}) == runtime.model_catalog


def test_model_ids_are_trimmed_deduplicated_and_invalid_rows_ignored(runtime, mock_client):
    mock_client(
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    None,
                    {},
                    {"id": 4},
                    {"id": " "},
                    {"id": "new "},
                    {"id": "new"},
                ]
            },
        )
    )

    result = discover_model_catalog(runtime, {})

    assert [entry.slug for entry in result] == ["new", "grok-default"]


def test_oversized_response_falls_back(runtime, mock_client, monkeypatch):
    monkeypatch.setattr("ccs_plus.model_discovery._MAX_RESPONSE_BYTES", 16)
    mock_client(
        lambda request: httpx.Response(
            200, content=json.dumps({"data": [{"id": "model"}]}).encode()
        )
    )

    assert discover_model_catalog(runtime, {}) == runtime.model_catalog


@pytest.mark.parametrize(
    "environment, expected",
    [
        ({"HTTPS_PROXY": "http://proxy:8080"}, "http://proxy:8080"),
        ({"ALL_PROXY": "socks5://proxy:1080"}, "socks5://proxy:1080"),
        ({"https_proxy": "socks5h://proxy:1080"}, "socks5h://proxy:1080"),
        ({"HTTPS_PROXY": "http://proxy:8080", "NO_PROXY": "selected.example.test"}, None),
        ({"HTTPS_PROXY": "http://proxy:8080", "no_proxy": "*"}, None),
        ({"HTTPS_PROXY": "", "ALL_PROXY": ""}, None),
    ],
)
def test_discovery_obeys_launch_proxy_and_no_proxy(
    runtime, mock_client, monkeypatch, environment, expected
):
    monkeypatch.setenv("HTTPS_PROXY", "http://unwanted-inherited-proxy:8080")
    options = mock_client(lambda request: httpx.Response(200, json={"data": [{"id": "model"}]}))

    discover_model_catalog(runtime, environment)

    assert options[0]["proxy"] == expected
    assert options[0]["trust_env"] is False


@pytest.mark.parametrize(
    "change", [{"api_key": None}, {"endpoint": None}, {"endpoint": "file:///tmp/models"}]
)
def test_missing_auth_or_invalid_endpoints_do_not_make_requests(runtime, mock_client, change):
    options = mock_client(lambda request: pytest.fail("Unexpected network request"))

    assert discover_model_catalog(replace(runtime, **change), {}) == runtime.model_catalog
    assert options == []


def test_official_provider_keeps_native_model_discovery(runtime, mock_client):
    options = mock_client(lambda request: pytest.fail("Official discovery must remain native"))
    runtime = replace(runtime, provider=replace(runtime.provider, category="official"))

    assert discover_model_catalog(runtime, {}) == runtime.model_catalog
    assert options == []


def test_invalid_http_url_falls_back_without_network(runtime, mock_client):
    mock_client(lambda request: pytest.fail("Invalid URLs must not reach the network"))

    result = discover_model_catalog(
        replace(runtime, endpoint="https://selected.test:invalid/v1"), {}
    )

    assert result == runtime.model_catalog


def test_discovery_refreshes_each_launch_and_does_not_share_provider_credentials(
    runtime, mock_client
):
    requests = []

    def respond(request):
        requests.append((str(request.url), request.headers["Authorization"]))
        return httpx.Response(200, json={"data": [{"id": f"remote-{len(requests)}"}]})

    mock_client(respond)
    second = replace(
        runtime,
        provider=replace(runtime.provider, id="second-provider"),
        endpoint="https://second.test/api/v1",
        api_key="second-secret-key",
    )

    first_result = discover_model_catalog(runtime, {})
    second_result = discover_model_catalog(second, {})
    refreshed_result = discover_model_catalog(runtime, {})

    assert first_result[0].slug == "remote-1"
    assert second_result[0].slug == "remote-2"
    assert refreshed_result[0].slug == "remote-3"
    assert requests == [
        ("https://selected.example.test/v1/models", "Bearer selected-secret-key"),
        ("https://second.test/api/v1/models", "Bearer second-secret-key"),
        ("https://selected.example.test/v1/models", "Bearer selected-secret-key"),
    ]
    assert [entry.slug for entry in runtime.model_catalog] == ["grok-default"]
