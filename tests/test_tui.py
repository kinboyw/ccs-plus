"""Pipe-input tests for the multi-pane launcher TUI."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from conftest import make_app_settings
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ccs_plus.adapters import build_provider
from ccs_plus.domain import AppKind, CodexAppConfig, NewProvider, Provider
from ccs_plus.launch_history import LaunchHistory
from ccs_plus.tui import PERMISSION_PRESETS, LaunchPlan, _LaunchScreen, run_launcher

_CODEX = CodexAppConfig(approval_policy="never", sandbox_mode="danger-full-access")

# app → provider → permissions → sessions → buttons
_NEW_SESSION_KEYS = "\r\r\r\r\r"


def _drive[T](func: Callable[[], T], keys: str, *, delay: float = 0.35) -> T:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):

        def send() -> None:
            time.sleep(delay)
            pipe.send_text(keys)

        threading.Thread(target=send, daemon=True).start()
        return func()


def _provider(app: AppKind = AppKind.CLAUDE, name: str = "Example"):
    return build_provider(
        NewProvider(
            app=app,
            name=name,
            endpoint="https://api.example.test/v1",
            api_key="secret",
            model="model-a",
            effort="high" if app is not AppKind.GROK else None,
            notes=None,
        ),
        _CODEX,
    )


def _run(tmp_path: Path, providers, keys: str) -> LaunchPlan | None:
    settings = make_app_settings(tmp_path)
    history = LaunchHistory.load(tmp_path / "history.json")
    return _drive(
        lambda: run_launcher(
            settings=settings,
            providers=providers,
            history=history,
            default_cwd=tmp_path,
        ),
        keys,
    )


def test_launcher_escape_cancels(tmp_path: Path) -> None:
    assert _run(tmp_path, [_provider()], "\x1b") is None


def test_launcher_default_path_launches_new_session(tmp_path: Path) -> None:
    provider = _provider(AppKind.CLAUDE)
    plan = _run(tmp_path, [provider], _NEW_SESSION_KEYS)
    assert plan is not None
    assert plan.provider.id == provider.id
    assert plan.cwd == tmp_path.resolve()
    assert plan.session is None
    assert plan.approval_policy is None
    assert plan.permission_mode is None
    assert plan.always_approve is None


def test_launcher_ctrl_enter_launches_immediately(tmp_path: Path) -> None:
    provider = _provider(AppKind.CLAUDE)
    plan = _run(tmp_path, [provider], "\n")
    assert plan is not None
    assert plan.provider.id == provider.id
    assert plan.cwd == tmp_path.resolve()
    assert plan.session is None


def test_launcher_keeps_provider_list_order_instead_of_sorting_by_name(tmp_path: Path) -> None:
    first = _provider(AppKind.CLAUDE, "Zulu")
    second = _provider(AppKind.CLAUDE, "Alpha")

    plan = _run(tmp_path, [first, second], _NEW_SESSION_KEYS)

    assert plan is not None
    assert plan.provider.id == first.id


def test_launcher_selects_codex_and_permission_preset(tmp_path: Path) -> None:
    claude = _provider(AppKind.CLAUDE, "Claude P")
    codex = _provider(AppKind.CODEX, "Codex P")
    presets = PERMISSION_PRESETS[AppKind.CODEX]
    on_request = next(preset for preset in presets if preset.key == "on-request")
    # app: down to codex → enter → sessions → provider → permissions
    # → down to On request (index 2) → buttons → Launch
    keys = "\x1b[B\r\r\r\x1b[B\x1b[B\r\r"
    plan = _run(tmp_path, [claude, codex], keys)
    assert plan is not None
    assert plan.provider.app is AppKind.CODEX
    assert plan.approval_policy == on_request.approval_policy
    assert plan.sandbox_mode == on_request.sandbox_mode


def test_launcher_opencode_app_cursor_stays_inside_rendered_rows(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    providers = [_provider(app, app.display_name) for app in AppKind]
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        screen = _LaunchScreen(
            settings=settings,
            providers=providers,
            history=LaunchHistory.load(tmp_path / "history.json"),
            default_cwd=tmp_path,
        )

        screen._set_app(screen.apps.index(AppKind.OPENCODE))
        content = screen._app_window.content.create_content(width=40, height=len(screen.apps))

    assert content.cursor_position.y == screen.app_index
    assert content.cursor_position.y < content.line_count


def test_permission_presets_match_native_cli_values() -> None:
    claude_modes = {preset.permission_mode for preset in PERMISSION_PRESETS[AppKind.CLAUDE]}
    assert claude_modes <= {
        "acceptEdits",
        "auto",
        "bypassPermissions",
        "manual",
        "dontAsk",
        "plan",
    }

    codex_approvals = {preset.approval_policy for preset in PERMISSION_PRESETS[AppKind.CODEX]}
    codex_sandboxes = {preset.sandbox_mode for preset in PERMISSION_PRESETS[AppKind.CODEX]}
    assert codex_approvals <= {"never", "on-request", "untrusted"}
    assert codex_sandboxes <= {"read-only", "workspace-write", "danger-full-access"}

    grok_sandboxes = {preset.sandbox_mode for preset in PERMISSION_PRESETS[AppKind.GROK]}
    assert grok_sandboxes <= {"off", "workspace", "devbox", "read-only", "strict"}
    assert all(
        isinstance(preset.always_approve, bool) for preset in PERMISSION_PRESETS[AppKind.GROK]
    )

    oc_modes = {preset.permission_mode for preset in PERMISSION_PRESETS[AppKind.OPENCODE]}
    assert oc_modes <= {"allow", "ask", "deny"}
    assert all(
        isinstance(preset.always_approve, bool) for preset in PERMISSION_PRESETS[AppKind.OPENCODE]
    )


def test_launcher_selects_claude_permission_preset(tmp_path: Path) -> None:
    provider = _provider(AppKind.CLAUDE, "Claude P")
    # Default preset is bypass (index 0). Move to plan (index 3).
    # app -> sessions -> provider -> permissions x3 down -> buttons -> launch
    keys = "\r\r\r\x1b[B\x1b[B\x1b[B\r\r"
    plan = _run(tmp_path, [provider], keys)
    assert plan is not None
    assert plan.permission_mode == "plan"
    assert plan.approval_policy is None
    assert plan.sandbox_mode is None


def test_launcher_selects_grok_permission_preset(tmp_path: Path) -> None:
    provider = _provider(AppKind.GROK, "Grok P")
    # Default matches settings workspace+auto (index 1). Move to read-only (index 3).
    # app -> sessions -> provider -> permissions x2 down -> buttons -> launch
    keys = "\r\r\r\x1b[B\x1b[B\r\r"
    plan = _run(tmp_path, [provider], keys)
    assert plan is not None
    assert plan.sandbox_mode == "read-only"
    assert plan.always_approve is False
    assert plan.permission_mode is None


def test_launcher_keeps_provider_permissions_without_explicit_override(tmp_path: Path) -> None:
    provider = _provider(AppKind.CODEX, "Codex P")
    config = provider.settings_config["config"].replace(
        'approval_policy = "never"', 'approval_policy = "on-request"'
    )
    config = config.replace(
        'sandbox_mode = "danger-full-access"', 'sandbox_mode = "workspace-write"'
    )
    provider = Provider(
        **{
            **provider.__dict__,
            "settings_config": {**provider.settings_config, "config": config},
        }
    )

    plan = _run(tmp_path, [provider], _NEW_SESSION_KEYS)

    assert plan is not None
    assert plan.approval_policy is None
    assert plan.sandbox_mode is None
    assert plan.permission_mode is None
    assert plan.always_approve is None


def _write_codex_session(
    settings,
    *,
    session_id: str,
    cwd: Path,
    title: str,
    stamp: str = "2026-08-13T12-00-00",
) -> None:
    import json

    day = settings.codex.user_home / "sessions" / "2026" / "08" / "13"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"rollout-{stamp}-{session_id}.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": session_id,
                            "timestamp": "2026-08-13T04:00:00.000Z",
                            "cwd": str(cwd),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": title}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_launcher_resume_selects_session(tmp_path: Path) -> None:
    provider = _provider(AppKind.CODEX, "Codex P")
    settings = make_app_settings(tmp_path)
    session_cwd = tmp_path / "work"
    session_cwd.mkdir()
    sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    _write_codex_session(settings, session_id=sid, cwd=session_cwd, title="resume me")
    history = LaunchHistory.load(tmp_path / "history.json")
    # Nested under default_cwd matches this-dir scope, so launcher starts
    # focused on sessions with the recent session already selected.
    # sessions → provider → permissions → buttons → launch
    keys = "\r\r\r\r"
    plan = _drive(
        lambda: run_launcher(
            settings=settings,
            providers=[provider],
            history=history,
            default_cwd=tmp_path,
        ),
        keys,
        delay=0.4,
    )
    assert plan is not None
    assert plan.session is not None
    assert plan.session.session_id == sid
    assert plan.cwd == session_cwd.resolve()


def test_launcher_this_dir_hides_foreign_sessions_until_all_scope(tmp_path: Path) -> None:
    provider = _provider(AppKind.CODEX, "Codex P")
    settings = make_app_settings(tmp_path)
    local = tmp_path / "local"
    foreign = tmp_path.parent / "foreign-project"
    local.mkdir()
    foreign.mkdir()
    local_sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    foreign_sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    _write_codex_session(
        settings,
        session_id=local_sid,
        cwd=local,
        title="local session",
        stamp="2026-08-13T12-00-01",
    )
    _write_codex_session(
        settings,
        session_id=foreign_sid,
        cwd=foreign,
        title="foreign session",
        stamp="2026-08-13T12-00-02",
    )
    history = LaunchHistory.load(tmp_path / "history.json")

    # Default this-dir from local: launcher starts focused on sessions with
    # the local session selected.
    # sessions → provider → permissions → buttons → launch
    local_plan = _drive(
        lambda: run_launcher(
            settings=settings,
            providers=[provider],
            history=history,
            default_cwd=local,
        ),
        "\r\r\r\r",
        delay=0.4,
    )
    assert local_plan is not None
    assert local_plan.session is not None
    assert local_plan.session.session_id == local_sid

    # Launcher is already focused on sessions; pressing 'a' toggles scope to show all projects.
    # Newest foreign is listed first after New session, so one down selects it.
    all_plan = _drive(
        lambda: run_launcher(
            settings=settings,
            providers=[provider],
            history=history,
            default_cwd=local,
        ),
        "a\x1b[B\r\r\r\r",
        delay=0.4,
    )
    assert all_plan is not None
    assert all_plan.session is not None
    assert all_plan.session.session_id == foreign_sid
    assert all_plan.cwd == foreign.resolve()


def test_session_matches_cwd_exact_and_nested(tmp_path: Path) -> None:
    from ccs_plus.tui import _session_matches_cwd

    root = tmp_path / "repo"
    nested = root / "pkg"
    other = tmp_path / "other"
    root.mkdir()
    nested.mkdir()
    other.mkdir()

    assert _session_matches_cwd(str(root), root)
    assert _session_matches_cwd(str(nested), root)
    assert not _session_matches_cwd(str(other), root)
    assert not _session_matches_cwd("", root)


def test_launcher_defaults_to_recent_session_and_focuses_sessions(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    provider = _provider(AppKind.CODEX, "Codex P")
    session_cwd = tmp_path / "work"
    session_cwd.mkdir()
    older_sid = "11111111-1111-1111-1111-111111111111"
    newer_sid = "22222222-2222-2222-2222-222222222222"
    _write_codex_session(
        settings,
        session_id=older_sid,
        cwd=session_cwd,
        title="older session",
        stamp="2026-08-13T10-00-00",
    )
    _write_codex_session(
        settings,
        session_id=newer_sid,
        cwd=session_cwd,
        title="newer session",
        stamp="2026-08-13T11-00-00",
    )
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        screen = _LaunchScreen(
            settings=settings,
            providers=[provider],
            history=LaunchHistory.load(tmp_path / "history.json"),
            default_cwd=tmp_path,
        )

        assert screen.focus == "sessions"
        assert screen.session_index == 1
        assert screen.selected_session is not None
        assert screen.selected_session.session_id == newer_sid
        assert screen.application.layout.current_window == screen._sessions_window


def test_launcher_defaults_to_app_focus_when_no_sessions(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    provider = _provider(AppKind.CODEX, "Codex P")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        screen = _LaunchScreen(
            settings=settings,
            providers=[provider],
            history=LaunchHistory.load(tmp_path / "history.json"),
            default_cwd=tmp_path,
        )

        assert screen.focus == "app"
        assert screen.session_index == 0
        assert screen.selected_session is None
        assert screen.application.layout.current_window == screen._app_window


def test_launcher_n_shortcut_selects_new_session(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    provider = _provider(AppKind.CODEX, "Codex P")
    session_cwd = tmp_path / "work"
    session_cwd.mkdir()
    sid = "33333333-3333-3333-3333-333333333333"
    _write_codex_session(settings, session_id=sid, cwd=session_cwd, title="active session")
    history = LaunchHistory.load(tmp_path / "history.json")

    # In a directory with an active session, default launch would resume it.
    # Pressing 'n' jumps to New Session, so the resulting plan has session=None.
    # sessions (press n) → provider → permissions → buttons → launch
    keys = "n\r\r\r\r"
    plan = _drive(
        lambda: run_launcher(
            settings=settings,
            providers=[provider],
            history=history,
            default_cwd=tmp_path,
        ),
        keys,
        delay=0.4,
    )
    assert plan is not None
    assert plan.session is None


def test_launcher_d_shortcut_deletes_session_with_confirmation(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    provider = _provider(AppKind.CODEX, "Codex P")
    session_cwd = tmp_path / "work"
    session_cwd.mkdir()
    sid = "44444444-4444-4444-4444-444444444444"
    _write_codex_session(settings, session_id=sid, cwd=session_cwd, title="to be deleted")
    history = LaunchHistory.load(tmp_path / "history.json")

    day = settings.codex.user_home / "sessions" / "2026" / "08" / "13"
    session_file = day / f"rollout-2026-08-13T12-00-00-{sid}.jsonl"
    assert session_file.exists()

    # sessions (press d to request delete, y to confirm, esc to cancel/exit)
    keys = "dy\x1b"
    plan = _drive(
        lambda: run_launcher(
            settings=settings,
            providers=[provider],
            history=history,
            default_cwd=tmp_path,
        ),
        keys,
        delay=0.4,
    )
    assert plan is None
    assert not session_file.exists()


def test_launcher_d_shortcut_cancelled_by_escape(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    provider = _provider(AppKind.CODEX, "Codex P")
    session_cwd = tmp_path / "work"
    session_cwd.mkdir()
    sid = "55555555-5555-5555-5555-555555555555"
    _write_codex_session(settings, session_id=sid, cwd=session_cwd, title="keep me")
    history = LaunchHistory.load(tmp_path / "history.json")

    day = settings.codex.user_home / "sessions" / "2026" / "08" / "13"
    session_file = day / f"rollout-2026-08-13T12-00-00-{sid}.jsonl"
    assert session_file.exists()

    # sessions (press d to request delete, first esc cancels deletion, second esc exits)
    keys = "d\x1b\x1b"
    plan = _drive(
        lambda: run_launcher(
            settings=settings,
            providers=[provider],
            history=history,
            default_cwd=tmp_path,
        ),
        keys,
        delay=0.4,
    )
    assert plan is None
    assert session_file.exists()


def test_launcher_t_shortcut_tests_provider(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    provider = _provider(AppKind.CLAUDE, "Test Provider")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        screen = _LaunchScreen(
            settings=settings,
            providers=[provider],
            history=LaunchHistory.load(tmp_path / "history.json"),
            default_cwd=tmp_path,
        )

        screen._set_focus("provider")
        screen._test_current_provider()
        time.sleep(0.1)
        assert "Test Provider" in screen.status


def test_launcher_p_shortcut_opens_preview_and_closes_with_esc(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    provider = _provider(AppKind.CODEX, "Codex P")
    session_cwd = tmp_path / "work"
    session_cwd.mkdir()
    sid = "66666666-6666-6666-6666-666666666666"
    _write_codex_session(settings, session_id=sid, cwd=session_cwd, title="previewable session")
    history = LaunchHistory.load(tmp_path / "history.json")

    # Press 'p' to open preview, 'esc' to close preview, then 'esc' to exit launcher
    keys = "p\x1b\x1b"
    plan = _drive(
        lambda: run_launcher(
            settings=settings,
            providers=[provider],
            history=history,
            default_cwd=tmp_path,
        ),
        keys,
        delay=0.4,
    )
    assert plan is None


def test_launcher_p_shortcut_cannot_preview_new_session(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    provider = _provider(AppKind.CODEX, "Codex P")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        screen = _LaunchScreen(
            settings=settings,
            providers=[provider],
            history=LaunchHistory.load(tmp_path / "history.json"),
            default_cwd=tmp_path,
        )

        screen.session_index = 0
        screen._open_preview()
        assert screen._preview_session is None
        assert "Cannot preview" in screen.status
        assert screen.status_error is True


def test_launcher_preview_defaults_to_latest_and_scrolls(tmp_path: Path) -> None:
    from prompt_toolkit.data_structures import Size

    settings = make_app_settings(tmp_path)
    provider = _provider(AppKind.CODEX, "Codex P")
    session_cwd = tmp_path / "work"
    session_cwd.mkdir()
    sid = "77777777-7777-7777-7777-777777777777"
    _write_codex_session(settings, session_id=sid, cwd=session_cwd, title="scroll test")
    out = DummyOutput()
    out.get_size = lambda: Size(rows=30, columns=100)

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=out):
        screen = _LaunchScreen(
            settings=settings,
            providers=[provider],
            history=LaunchHistory.load(tmp_path / "history.json"),
            default_cwd=tmp_path,
        )

        screen.session_index = 1
        screen._open_preview()
        assert screen._preview_session is not None
        # Verify rendered lines and scroll
        total = screen._preview_total_lines()
        assert total > 0
        cursor_pos = screen._preview_window.content.get_cursor_position()
        assert cursor_pos is not None
        assert cursor_pos.y == screen._preview_scroll

        # Test actual render scrolling
        screen.application.renderer.render(screen.application, screen.application.layout)
        assert screen._preview_window.render_info is not None

        # Scrolling up decreases scroll
        initial_scroll = screen._preview_scroll
        screen._scroll_preview(-2)
        assert screen._preview_scroll == max(0, initial_scroll - 2)
        cursor_pos = screen._preview_window.content.get_cursor_position()
        assert cursor_pos is not None
        assert cursor_pos.y == screen._preview_scroll

        # Closing clears preview
        screen._close_preview()
        assert screen._preview_session is None


def test_launcher_help_modal_toggle(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    provider = _provider(AppKind.CODEX, "Codex P")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        screen = _LaunchScreen(
            settings=settings,
            providers=[provider],
            history=LaunchHistory.load(tmp_path / "history.json"),
            default_cwd=tmp_path,
        )

        assert screen._show_help is False
        screen._open_help()
        assert screen._show_help is True
        assert screen._help_total_lines() > 10

        screen._close_help()
        assert screen._show_help is False


def test_launcher_space_opens_preview(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    provider = _provider(AppKind.CODEX, "Codex P")
    session_cwd = tmp_path / "work"
    session_cwd.mkdir()
    sid = "88888888-8888-8888-8888-888888888888"
    _write_codex_session(settings, session_id=sid, cwd=session_cwd, title="space test")
    history = LaunchHistory.load(tmp_path / "history.json")

    # Space opens preview, esc closes it, second esc exits launcher
    keys = " \x1b\x1b"
    plan = _drive(
        lambda: run_launcher(
            settings=settings,
            providers=[provider],
            history=history,
            default_cwd=tmp_path,
        ),
        keys,
        delay=0.4,
    )
    assert plan is None


def test_active_list_shows_shortcut_numbers_when_focused(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    codex_p = _provider(AppKind.CODEX, "Codex P")
    claude_p = _provider(AppKind.CLAUDE, "Claude P")
    session_cwd = tmp_path / "work"
    session_cwd.mkdir()
    sid = "99999999-9999-9999-9999-999999999999"
    _write_codex_session(settings, session_id=sid, cwd=session_cwd, title="num test")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        screen = _LaunchScreen(
            settings=settings,
            providers=[codex_p, claude_p],
            history=LaunchHistory.load(tmp_path / "history.json"),
            default_cwd=tmp_path,
        )

        # When sessions is focused, sessions list lines contain shortcut numbers
        screen._set_focus("sessions")
        session_text = "".join(part[1] for part in screen._session_lines())
        assert "▸" in session_text
        assert "1" in session_text or "2" in session_text

        # App list does NOT show numbers when unfocused
        app_text_unfocused = "".join(part[1] for part in screen._app_lines())
        assert " 1▸" not in app_text_unfocused
        assert "●" in app_text_unfocused

        # When app is focused, app list shows numbers
        screen._set_focus("app")
        app_text_focused = "".join(part[1] for part in screen._app_lines())
        assert " 1▸" in app_text_focused
        assert " 2 " in app_text_focused


def test_launcher_cwd_selector_modal_and_switch(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    provider = _provider(AppKind.CODEX, "Codex P")
    dir1 = tmp_path / "project1"
    dir2 = tmp_path / "project2"
    dir1.mkdir()
    dir2.mkdir()
    _write_codex_session(
        settings,
        session_id="11111111-1111-1111-1111-111111111111",
        cwd=dir1,
        title="proj1 session",
    )
    _write_codex_session(
        settings,
        session_id="22222222-2222-2222-2222-222222222222",
        cwd=dir2,
        title="proj2 session",
    )

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        screen = _LaunchScreen(
            settings=settings,
            providers=[provider],
            history=LaunchHistory.load(tmp_path / "history.json"),
            default_cwd=dir1,
        )

        assert screen.default_cwd.resolve() == dir1.resolve()
        assert screen._show_cwd_selector is False
        screen._open_cwd_selector()
        assert screen._show_cwd_selector is True
        dirs = [d.resolve() for d in screen.filtered_directories]
        assert dir1.resolve() in dirs
        assert dir2.resolve() in dirs

        # Select dir2
        screen._select_cwd(dir2)
        assert screen._show_cwd_selector is False
        assert screen.default_cwd.resolve() == dir2.resolve()
        assert screen.selected_session is not None
        assert screen.selected_session.session_id == "22222222-2222-2222-2222-222222222222"


def test_launcher_c_key_switches_directory_and_launches(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    provider = _provider(AppKind.CODEX, "Codex P")
    dir1 = tmp_path / "project1"
    dir2 = tmp_path / "project2"
    dir1.mkdir()
    dir2.mkdir()
    _write_codex_session(
        settings,
        session_id="11111111-1111-1111-1111-111111111111",
        cwd=dir1,
        title="proj1 session",
    )
    _write_codex_session(
        settings,
        session_id="22222222-2222-2222-2222-222222222222",
        cwd=dir2,
        title="proj2 session",
    )
    history = LaunchHistory.load(tmp_path / "history.json")

    # Press 'c' to open cwd selector, down arrow to select dir2, enter to confirm,
    # ctrl-enter (\n) to launch directly
    keys = "c\x1b[B\r\n"
    plan = _drive(
        lambda: run_launcher(
            settings=settings,
            providers=[provider],
            history=history,
            default_cwd=dir1,
        ),
        keys,
        delay=0.4,
    )
    assert plan is not None
    assert plan.cwd == dir2.resolve()
