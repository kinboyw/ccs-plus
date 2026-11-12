from __future__ import annotations

import json
from dataclasses import replace

import pytest
import tomlkit

from ccs_plus.adapters import build_provider, runtime_from_provider
from ccs_plus.domain import AppKind, CodexAppConfig, NewProvider, ProviderError
from ccs_plus.managed_config import ensure_managed_config as _ensure_managed_config
from ccs_plus.managed_config import remove_managed_config

_CODEX = CodexAppConfig(approval_policy="never", sandbox_mode="danger-full-access")


def ensure_managed_config(*args, **kwargs):
    kwargs.pop("user_home", None)
    kwargs.pop("project_directory", None)
    return _ensure_managed_config(
        *args,
        session_model_provider="ccs-plus-managed",
        **kwargs,
    )


def _runtime(app: AppKind):
    provider = build_provider(
        NewProvider(
            app=app,
            name="Example Provider",
            endpoint="https://api.example.test/v1",
            api_key="managed-secret-key",
            model="example-model",
            effort="high" if app is AppKind.CODEX else None,
            notes=None,
        ),
        _CODEX,
    )
    return runtime_from_provider(provider)


def test_codex_profile_uses_provider_policy_but_no_api_key(tmp_path) -> None:
    runtime = _runtime(AppKind.CODEX)
    profile = ensure_managed_config(runtime, tmp_path, None, None)
    path = tmp_path / f"{profile.name}.config.toml"
    content = path.read_text(encoding="utf-8")
    document = tomlkit.parse(content)

    assert "managed-secret-key" not in content
    assert document["approval_policy"] == "never"
    assert document["default_permissions"] == ":danger-full-access"
    assert "permissions" not in document
    assert "windows" not in document
    # Permission profiles and legacy sandbox settings do not compose.
    assert "sandbox_mode" not in document
    assert "sandbox_workspace_write" not in document
    assert document["model_provider"] == "ccs-plus-managed"
    assert document["model_providers"]["ccs-plus-managed"]["env_key"] == profile.env_key


def test_codex_profile_follows_provider_sandbox_mode(tmp_path) -> None:
    provider = build_provider(
        NewProvider(
            app=AppKind.CODEX,
            name="Workspace Provider",
            endpoint="https://api.example.test/v1",
            api_key="managed-secret-key",
            model="example-model",
            effort=None,
            notes=None,
        ),
        CodexAppConfig(approval_policy="on-request", sandbox_mode="workspace-write"),
    )
    runtime = runtime_from_provider(provider)
    profile = ensure_managed_config(runtime, tmp_path, None, None)
    document = tomlkit.parse((tmp_path / f"{profile.name}.config.toml").read_text(encoding="utf-8"))
    assert document["approval_policy"] == "on-request"
    assert document["default_permissions"] == ":workspace"


def test_codex_profile_rejects_missing_provider_policy(tmp_path) -> None:
    runtime = _runtime(AppKind.CODEX)
    runtime = type(runtime)(
        **{
            **runtime.__dict__,
            "approval_policy": None,
            "sandbox_mode": None,
        }
    )
    with pytest.raises(ProviderError, match="approval_policy"):
        ensure_managed_config(runtime, tmp_path, None, None)


def test_codex_profile_falls_back_to_settings_policy(tmp_path) -> None:
    runtime = _runtime(AppKind.CODEX)
    runtime = type(runtime)(
        **{
            **runtime.__dict__,
            "approval_policy": None,
            "sandbox_mode": None,
        }
    )

    profile = ensure_managed_config(
        runtime,
        tmp_path,
        None,
        None,
        approval_policy="on-request",
        sandbox_mode="workspace-write",
    )
    document = tomlkit.parse((tmp_path / f"{profile.name}.config.toml").read_text(encoding="utf-8"))

    assert document["approval_policy"] == "on-request"
    assert document["default_permissions"] == ":workspace"


def test_codex_profile_removes_legacy_extension_and_preserves_project_trust(tmp_path) -> None:
    runtime = _runtime(AppKind.CODEX)
    profile = ensure_managed_config(runtime, tmp_path, None, None)
    path = tmp_path / f"{profile.name}.config.toml"
    path.write_text(
        f"""# ccs-plus-managed: codex:{runtime.provider.id}
[windows]
sandbox = "unelevated"

[projects.'d:\\workspace']
trust_level = "trusted"

[mcp_servers.legacy]
command = "legacy"

[plugins."legacy@local"]
enabled = true

[marketplaces.legacy]
source = "local"

[shell_environment_policy]
inherit = "all"
""",
        encoding="utf-8",
    )

    ensure_managed_config(runtime, tmp_path, None, None)
    document = tomlkit.parse(path.read_text(encoding="utf-8"))

    assert "windows" not in document
    assert document["projects"][r"d:\workspace"]["trust_level"] == "trusted"
    assert "mcp_servers" not in document
    assert "plugins" not in document
    assert "marketplaces" not in document
    assert "shell_environment_policy" not in document


def test_codex_profile_rejects_unmanaged_file(tmp_path) -> None:
    runtime = _runtime(AppKind.CODEX)
    profile = ensure_managed_config(runtime, tmp_path, None, None)
    path = tmp_path / f"{profile.name}.config.toml"
    path.write_text('model = "user-owned"\n', encoding="utf-8")

    with pytest.raises(ProviderError, match="unmanaged"):
        ensure_managed_config(runtime, tmp_path, None, None)


def test_remove_managed_codex_profile_requires_matching_marker(tmp_path) -> None:
    runtime = _runtime(AppKind.CODEX)
    profile = ensure_managed_config(runtime, tmp_path, None, None)
    path = tmp_path / f"{profile.name}.config.toml"

    assert remove_managed_config(tmp_path, runtime.provider.id) is True
    assert not path.exists()

    path.write_text('model = "user-owned"\n', encoding="utf-8")
    assert remove_managed_config(tmp_path, runtime.provider.id) is False
    assert path.exists()


def test_codex_profiles_share_model_provider_across_providers(tmp_path) -> None:
    first = _runtime(AppKind.CODEX)
    second_provider = build_provider(
        NewProvider(
            app=AppKind.CODEX,
            name="Second Provider",
            endpoint="https://second.example.test/v1",
            api_key="second-secret-key",
            model="second-model",
            effort="minimal",
            notes=None,
        ),
        _CODEX,
    )
    second = runtime_from_provider(second_provider)

    first_profile = ensure_managed_config(first, tmp_path, None, None)
    second_profile = ensure_managed_config(second, tmp_path, None, None)
    first_document = tomlkit.parse(
        (tmp_path / f"{first_profile.name}.config.toml").read_text(encoding="utf-8")
    )
    second_document = tomlkit.parse(
        (tmp_path / f"{second_profile.name}.config.toml").read_text(encoding="utf-8")
    )

    assert first_profile.name != second_profile.name
    assert first_document["model_provider"] == "ccs-plus-managed"
    assert second_document["model_provider"] == "ccs-plus-managed"
    assert list(first_document["model_providers"]) == ["ccs-plus-managed"]
    assert list(second_document["model_providers"]) == ["ccs-plus-managed"]


def test_codex_profile_requires_configured_session_model_provider(tmp_path) -> None:
    with pytest.raises(ProviderError, match="session_model_provider"):
        _ensure_managed_config(_runtime(AppKind.CODEX), tmp_path, None, None)


def test_codex_profile_owns_a_catalog_without_touching_cc_switch_config(tmp_path) -> None:
    runtime = _runtime(AppKind.CODEX)
    base_path = tmp_path / "config.toml"
    base_content = 'model_catalog_json = "cc-switch-model-catalog.json"\n'
    base_path.write_text(base_content, encoding="utf-8")
    shared_path = tmp_path / "cc-switch-model-catalog.json"
    shared_content = '{"models": [{"slug": "unrelated-gpt-model"}]}\n'
    shared_path.write_text(shared_content, encoding="utf-8")

    profile = ensure_managed_config(runtime, tmp_path, None, None)
    profile_path = tmp_path / f"{profile.name}.config.toml"
    document = tomlkit.parse(profile_path.read_text(encoding="utf-8"))
    catalog_path = tmp_path / document["model_catalog_json"]
    catalog_content = catalog_path.read_text(encoding="utf-8")
    catalog = json.loads(catalog_content)

    assert catalog_path.name == f"{profile.name}.model-catalog.json"
    assert [entry["slug"] for entry in catalog["models"]] == ["example-model"]
    assert catalog["_ccs_plus_managed"] == f"ccs-plus-managed: codex:{runtime.provider.id}"
    assert "managed-secret-key" not in catalog_content
    assert base_path.read_text(encoding="utf-8") == base_content
    assert shared_path.read_text(encoding="utf-8") == shared_content
    first_profile = profile_path.read_bytes()
    first_catalog = catalog_path.read_bytes()

    assert ensure_managed_config(runtime, tmp_path, None, None) == profile
    assert profile_path.read_bytes() == first_profile
    assert catalog_path.read_bytes() == first_catalog


def test_codex_catalogs_are_isolated_and_refreshed_from_provider_settings(tmp_path) -> None:
    first = _runtime(AppKind.CODEX)
    first.provider.settings_config["modelCatalog"] = {
        "models": [{"model": "grok-4.7"}, {"model": "grok-4.5"}]
    }
    first = runtime_from_provider(first.provider)
    second_provider = replace(
        first.provider,
        id="second-provider",
        settings_config={
            **first.provider.settings_config,
            "modelCatalog": {"models": [{"model": "second-model"}]},
        },
    )
    second = runtime_from_provider(second_provider)

    first_profile = ensure_managed_config(first, tmp_path, "grok-4.7", None)
    second_profile = ensure_managed_config(second, tmp_path, "second-model", None)
    first_path = tmp_path / f"{first_profile.name}.model-catalog.json"
    second_path = tmp_path / f"{second_profile.name}.model-catalog.json"
    second_content = second_path.read_bytes()

    assert first_path != second_path
    assert [entry["slug"] for entry in json.loads(first_path.read_text())["models"]] == [
        "grok-4.7",
        "grok-4.5",
    ]
    assert [entry["slug"] for entry in json.loads(second_content)["models"]] == ["second-model"]

    first.provider.settings_config.pop("modelCatalog")
    ensure_managed_config(runtime_from_provider(first.provider), tmp_path, "grok-4.7", None)

    assert [entry["slug"] for entry in json.loads(first_path.read_text())["models"]] == ["grok-4.7"]
    assert second_path.read_bytes() == second_content

    assert remove_managed_config(tmp_path, first.provider.id) is True
    assert not first_path.exists()
    assert second_path.read_bytes() == second_content


@pytest.mark.parametrize("content", ['{"models": []}', '{"_ccs_plus_managed": "other"}', "broken"])
def test_codex_catalog_refuses_to_overwrite_or_delete_unmanaged_files(tmp_path, content) -> None:
    runtime = _runtime(AppKind.CODEX)
    profile = ensure_managed_config(runtime, tmp_path, None, None)
    catalog_path = tmp_path / f"{profile.name}.model-catalog.json"
    catalog_path.write_text(content, encoding="utf-8")

    with pytest.raises(ProviderError, match="unmanaged Codex catalog"):
        ensure_managed_config(runtime, tmp_path, None, None)
    assert catalog_path.read_text(encoding="utf-8") == content
    assert remove_managed_config(tmp_path, runtime.provider.id) is True
    assert catalog_path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("dangling", [False, True])
def test_codex_catalog_refuses_symlinks_even_with_matching_marker(tmp_path, dangling) -> None:
    runtime = _runtime(AppKind.CODEX)
    profile = ensure_managed_config(runtime, tmp_path, None, None)
    catalog_path = tmp_path / f"{profile.name}.model-catalog.json"
    target = tmp_path / "user-catalog.json"
    content = catalog_path.read_text(encoding="utf-8")
    if not dangling:
        target.write_text(content, encoding="utf-8")
    catalog_path.unlink()
    catalog_path.symlink_to(target)

    with pytest.raises(ProviderError, match="unmanaged Codex catalog"):
        ensure_managed_config(runtime, tmp_path, None, None)
    assert remove_managed_config(tmp_path, runtime.provider.id) is True
    assert catalog_path.is_symlink()
    assert target.exists() is not dangling
    if not dangling:
        assert target.read_text(encoding="utf-8") == content


def test_codex_cleanup_removes_only_the_providers_owned_catalog(tmp_path) -> None:
    runtime = _runtime(AppKind.CODEX)
    profile = ensure_managed_config(runtime, tmp_path, None, None)
    profile_path = tmp_path / f"{profile.name}.config.toml"
    catalog_path = tmp_path / f"{profile.name}.model-catalog.json"
    shared_path = tmp_path / "cc-switch-model-catalog.json"
    shared_path.write_text("shared catalog", encoding="utf-8")
    document = tomlkit.parse(profile_path.read_text(encoding="utf-8"))
    document["model_catalog_json"] = shared_path.name
    profile_path.write_text(tomlkit.dumps(document), encoding="utf-8")

    assert remove_managed_config(tmp_path, runtime.provider.id) is True
    assert not profile_path.exists()
    assert not catalog_path.exists()
    assert shared_path.read_text(encoding="utf-8") == "shared catalog"
    assert remove_managed_config(tmp_path, runtime.provider.id) is False


def test_codex_cleanup_removes_an_orphaned_owned_catalog(tmp_path) -> None:
    runtime = _runtime(AppKind.CODEX)
    profile = ensure_managed_config(runtime, tmp_path, None, None)
    (tmp_path / f"{profile.name}.config.toml").unlink()

    assert remove_managed_config(tmp_path, runtime.provider.id) is True
    assert not (tmp_path / f"{profile.name}.model-catalog.json").exists()


def test_grok_config_preserves_default_and_uses_managed_model(tmp_path) -> None:
    runtime = _runtime(AppKind.GROK)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """# user configuration
[models]
default = "user-model"

[model.user-model]
model = "user-model"
base_url = "https://user.example.test/v1"
name = "User"
env_key = "USER_KEY"
api_backend = "responses"
context_window = 500000
""",
        encoding="utf-8",
    )

    profile = ensure_managed_config(runtime, tmp_path, None, "xhigh")
    content = config_path.read_text(encoding="utf-8")
    document = tomlkit.parse(content)

    assert "managed-secret-key" not in content
    assert f"ccs-plus-managed: grok:{runtime.provider.id}" in content
    assert document["models"]["default"] == "user-model"
    assert document["models"]["default_reasoning_effort"] == "xhigh"
    assert document["model"][profile.name]["model"] == "example-model"
    assert document["model"][profile.name]["context_window"] == 500_000
    assert not (tmp_path / "config.toml.ccs-plus.bak").exists()


def test_grok_config_keeps_only_current_managed_model(tmp_path) -> None:
    first = _runtime(AppKind.GROK)
    second_provider = build_provider(
        NewProvider(
            app=AppKind.GROK,
            name="Second Grok Provider",
            endpoint="https://second.example.test/v1",
            api_key="second-managed-secret-key",
            model="second-model",
            effort=None,
            notes=None,
        ),
        _CODEX,
    )
    second = runtime_from_provider(second_provider)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[models]
default = "user-model"

[model.user-model]
model = "user-model"
base_url = "https://user.example.test/v1"
name = "User model"
env_key = "USER_KEY"
api_backend = "responses"
context_window = 500000
""",
        encoding="utf-8",
    )

    first_profile = ensure_managed_config(first, tmp_path, None, None)
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    document["models"]["default"] = first_profile.name
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")

    second_profile = ensure_managed_config(second, tmp_path, None, None)
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))

    assert set(document["model"]) == {"user-model", second_profile.name}
    assert first_profile.name not in document["model"]
    assert document["models"].get("default") is None


def test_grok_config_rejects_unmanaged_profile(tmp_path) -> None:
    runtime = _runtime(AppKind.GROK)
    profile = ensure_managed_config(runtime, tmp_path, None, None)
    (tmp_path / "config.toml").write_text(
        f"""
[model.{profile.name}]
model = "user-owned"
base_url = "https://user.example.test/v1"
name = "User"
env_key = "USER_KEY"
api_backend = "responses"
context_window = 500000
""",
        encoding="utf-8",
    )

    with pytest.raises(ProviderError, match="unmanaged Grok"):
        ensure_managed_config(runtime, tmp_path, None, None)


def test_grok_config_rewrites_profile_after_cli_strips_marker(tmp_path) -> None:
    runtime = _runtime(AppKind.GROK)
    profile = ensure_managed_config(runtime, tmp_path, None, None)
    (tmp_path / "config.toml").write_text(
        f"""
[models]
default = "user-model"

[model.{profile.name}]
model = "stale-model"
base_url = "https://stale.example.test/v1"
name = "Stale"
env_key = "{profile.env_key}"
api_backend = "responses"
context_window = 500000
""",
        encoding="utf-8",
    )

    ensure_managed_config(runtime, tmp_path, "fresh-model", None)
    content = (tmp_path / "config.toml").read_text(encoding="utf-8")
    document = tomlkit.parse(content)

    assert f"ccs-plus-managed: grok:{runtime.provider.id}" in content
    assert document["models"]["default"] == "user-model"
    assert document["model"][profile.name]["model"] == "fresh-model"
    assert document["model"][profile.name]["base_url"] == "https://api.example.test/v1"
    assert document["model"][profile.name]["env_key"] == profile.env_key
