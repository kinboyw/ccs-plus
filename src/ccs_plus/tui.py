"""Opencode-style multi-pane interactive launcher (prompt_toolkit)."""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText, StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout import (
    ConditionalContainer,
    FormattedTextControl,
    HSplit,
    Layout,
    VSplit,
    Window,
    WindowAlign,
)
from prompt_toolkit.layout.containers import Float, FloatContainer
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Box

from ccs_plus.adapters import (
    check_provider_connectivity,
    display_configuration,
    runtime_from_provider,
)
from ccs_plus.domain import (
    AppKind,
    ClaudeRuntime,
    CodexRuntime,
    GeminiRuntime,
    GrokRuntime,
    OpenCodeRuntime,
    Provider,
    ProviderError,
)
from ccs_plus.launch_history import LaunchHistory
from ccs_plus.sessions import (
    Session,
    SessionMessage,
    delete_session,
    list_sessions,
    read_session_messages,
)
from ccs_plus.settings import AppSettings

# High-contrast neon-on-dark (bright selection, clear active pane).
STYLE = Style.from_dict(
    {
        "root": "bg:#0a0e14 #d0d7e2",
        "header": "bg:#010409 #8b949e",
        "header.brand": "bg:#010409 #58a6ff bold",
        "header.accent": "bg:#010409 #d2a8ff bold",
        "header.mode": "bg:#010409 #3fb950 bold",
        "footer": "bg:#010409 #6e7681",
        "footer.key": "bg:#010409 #f0f6fc bold",
        "footer.filter": "bg:#010409 #ffa657 bold",
        "frame.border": "#30363d",
        "frame.label": "#8b949e",
        "frame.active.border": "#58a6ff bold",
        "frame.active.label": "#58a6ff bold",
        "frame.scroll": "#8b949e",
        "frame.active.scroll": "#58a6ff bold",
        "popup": "bg:#161b22 #f0f6fc",
        "popup.border": "bg:#161b22 #58a6ff bold",
        "popup.title.badge": "bg:#1f6feb #ffffff bold",
        "popup.title.text": "bg:#161b22 #ffffff bold",
        "popup.title.hint": "bg:#161b22 #8b949e",
        "popup.footer.badge.latest": "bg:#238636 #ffffff bold",
        "popup.footer.badge.history": "bg:#d29922 #0a0e14 bold",
        "popup.footer.hint": "bg:#161b22 #8b949e",
        "popup.header.title": "bg:#161b22 #ffffff bold",
        "popup.header.meta": "bg:#161b22 #8b949e",
        "popup.user.badge": "bg:#238636 #ffffff bold",
        "popup.user.pipe": "bg:#161b22 #3fb950 bold",
        "popup.user.text": "bg:#161b22 #f0f6fc bold",
        "popup.assistant.badge": "bg:#1f6feb #ffffff bold",
        "popup.assistant.pipe": "bg:#161b22 #58a6ff bold",
        "popup.assistant.text": "bg:#161b22 #c9d1d9",
        "popup.code": "bg:#0d1117 #79c0ff",
        "popup.code.pipe": "bg:#161b22 #79c0ff",
        "popup.key": "bg:#21262d #58a6ff bold",
        "popup.divider": "bg:#161b22 #30363d",
        "item": "#c9d1d9",
        "item.selected": "bg:#3b4554 #ffffff bold",
        "item.focused": "bg:#1f6feb #ffffff bold",
        "item.focused-sub": "bg:#1f6feb #dbeafe",
        "item.marker.selected": "bg:#3b4554 #58a6ff bold",
        "item.marker.focused": "bg:#1f6feb #ffffff bold",
        "item.shortcut": "#58a6ff bold",
        "item.muted": "#8b949e",
        "badge.claude": "bg:#d97706 #0a0e14 bold",
        "badge.codex": "bg:#10b981 #0a0e14 bold",
        "badge.gemini": "bg:#eab308 #0a0e14 bold",
        "badge.grok": "bg:#a855f7 #0a0e14 bold",
        "badge.opencode": "bg:#38bdf8 #0a0e14 bold",
        "badge.claude.focused": "bg:#fbbf24 #0a0e14 bold",
        "badge.codex.focused": "bg:#34d399 #0a0e14 bold",
        "badge.gemini.focused": "bg:#fde047 #0a0e14 bold",
        "badge.grok.focused": "bg:#c084fc #0a0e14 bold",
        "badge.opencode.focused": "bg:#7dd3fc #0a0e14 bold",
        "status.ok": "#3fb950 bold",
        "status.err": "#ff7b72 bold",
        "button.launch": "bg:#238636 #ffffff bold",
        "button.launch.focused": "bg:#3fb950 #0a0e14 bold",
        "button.launch.border": "bg:#238636 #2ea043 bold",
        "button.launch.border.focused": "bg:#3fb950 #56d364 bold",
        "button.cancel": "bg:#6e2121 #ffffff bold",
        "button.cancel.focused": "bg:#ff7b72 #0a0e14 bold",
        "button.cancel.border": "bg:#6e2121 #da3633 bold",
        "button.cancel.border.focused": "bg:#ff7b72 #ff9b95 bold",
        "text-area": "bg:#0d1117 #f0f6fc",
        "text-area.focused": "bg:#161b22 #ffffff bold",
    }
)

_SESSION_ROW = 2
_PROVIDER_ROW = 2
_PERMISSION_ROW = 2

SessionScope = Literal["this_dir", "all"]


@dataclass(frozen=True)
class PermissionPreset:
    """Per-app launch permission choice exposed in the TUI."""

    key: str
    label: str
    description: str
    # Claude
    permission_mode: str | None = None
    # Codex
    approval_policy: str | None = None
    sandbox_mode: str | None = None
    # Grok
    always_approve: bool | None = None


# Values are constrained to flags documented by each native CLI's --help.
PERMISSION_PRESETS: dict[AppKind, tuple[PermissionPreset, ...]] = {
    AppKind.CLAUDE: (
        PermissionPreset(
            "bypass",
            "Bypass",
            "skip all permission prompts",
            permission_mode="bypassPermissions",
        ),
        PermissionPreset(
            "accept-edits",
            "Accept edits",
            "auto-accept file edits",
            permission_mode="acceptEdits",
        ),
        PermissionPreset(
            "auto",
            "Auto",
            "automatic permission mode",
            permission_mode="auto",
        ),
        PermissionPreset(
            "plan",
            "Plan",
            "plan only · no tool execution",
            permission_mode="plan",
        ),
        PermissionPreset(
            "manual",
            "Manual",
            "confirm each sensitive action",
            permission_mode="manual",
        ),
        PermissionPreset(
            "dont-ask",
            "Don't ask",
            "proceed without interactive prompts",
            permission_mode="dontAsk",
        ),
    ),
    AppKind.CODEX: (
        PermissionPreset(
            "yolo",
            "YOLO",
            "never ask · full disk access",
            approval_policy="never",
            sandbox_mode="danger-full-access",
        ),
        PermissionPreset(
            "auto-workspace",
            "Auto (workspace)",
            "never ask · workspace write",
            approval_policy="never",
            sandbox_mode="workspace-write",
        ),
        PermissionPreset(
            "on-request",
            "On request",
            "model decides when to ask · workspace write",
            approval_policy="on-request",
            sandbox_mode="workspace-write",
        ),
        PermissionPreset(
            "untrusted",
            "Untrusted",
            "only trusted commands · read-only",
            approval_policy="untrusted",
            sandbox_mode="read-only",
        ),
    ),
    AppKind.GEMINI: (
        PermissionPreset(
            "default",
            "Default",
            "confirm actions when needed",
            permission_mode="default",
        ),
        PermissionPreset(
            "auto-edit",
            "Auto edit",
            "auto-approve edit tools",
            permission_mode="auto_edit",
        ),
        PermissionPreset(
            "yolo",
            "YOLO",
            "auto-approve all tools",
            permission_mode="yolo",
        ),
        PermissionPreset(
            "plan",
            "Plan",
            "read-only mode",
            permission_mode="plan",
        ),
    ),
    AppKind.GROK: (
        PermissionPreset(
            "yolo",
            "YOLO",
            "no sandbox · auto-approve tools",
            sandbox_mode="off",
            always_approve=True,
        ),
        PermissionPreset(
            "workspace-auto",
            "Workspace + auto",
            "workspace sandbox · auto-approve",
            sandbox_mode="workspace",
            always_approve=True,
        ),
        PermissionPreset(
            "workspace-ask",
            "Workspace + ask",
            "workspace sandbox · confirm tools",
            sandbox_mode="workspace",
            always_approve=False,
        ),
        PermissionPreset(
            "read-only",
            "Read-only",
            "read-only sandbox · confirm tools",
            sandbox_mode="read-only",
            always_approve=False,
        ),
        PermissionPreset(
            "strict",
            "Strict",
            "strict sandbox · confirm tools",
            sandbox_mode="strict",
            always_approve=False,
        ),
    ),
    AppKind.OPENCODE: (
        PermissionPreset(
            "allow",
            "Allow all",
            "permission allow · confirm when needed",
            permission_mode="allow",
            always_approve=False,
        ),
        PermissionPreset(
            "allow-auto",
            "Allow + auto",
            "permission allow · auto-approve prompts",
            permission_mode="allow",
            always_approve=True,
        ),
        PermissionPreset(
            "ask",
            "Ask",
            "permission ask · confirm tools",
            permission_mode="ask",
            always_approve=False,
        ),
        PermissionPreset(
            "ask-auto",
            "Ask + auto",
            "permission ask · auto-approve non-denied",
            permission_mode="ask",
            always_approve=True,
        ),
        PermissionPreset(
            "deny",
            "Deny",
            "permission deny · block tool actions",
            permission_mode="deny",
            always_approve=False,
        ),
    ),
}

# Back-compat alias for tests that still import the old name.
APPROVAL_PRESETS = PERMISSION_PRESETS[AppKind.CODEX]


@dataclass(frozen=True)
class LaunchPlan:
    provider: Provider
    cwd: Path
    session: Session | None
    approval_policy: str | None = None
    sandbox_mode: str | None = None
    permission_mode: str | None = None
    always_approve: bool | None = None


def run_launcher(
    *,
    settings: AppSettings,
    providers: Sequence[Provider],
    history: LaunchHistory,
    default_cwd: Path | None = None,
) -> LaunchPlan | None:
    """Show the multi-pane launcher and return a launch plan, or None on cancel."""
    screen = _LaunchScreen(
        settings=settings,
        providers=list(providers),
        history=history,
        default_cwd=(default_cwd or Path.cwd()).resolve(),
    )
    return screen.run()


class _ScrollListControl(FormattedTextControl):
    """List control: app-level keys; mouse click + wheel handled here."""

    def __init__(
        self,
        get_text: Callable[[], Any],
        *,
        on_click_row: Callable[[int], None],
        on_scroll: Callable[[int], None],
        on_activate: Callable[[], None],
        get_cursor_position: Callable[[], Point | None] | None = None,
    ) -> None:
        # focusable=True so the window reliably receives mouse wheel events.
        # Arrow keys are still handled by eager app-level bindings.
        super().__init__(
            get_text,
            focusable=True,
            show_cursor=False,
            modal=False,
            get_cursor_position=get_cursor_position,
        )
        self._on_click_row = on_click_row
        self._on_scroll = on_scroll
        self._on_activate = on_activate

    def mouse_handler(self, mouse_event: MouseEvent) -> object:
        # Prefer MOUSE_DOWN for selection: changing focus on down would otherwise
        # drop the matching MOUSE_UP on another control.
        event = mouse_event.event_type
        if event == MouseEventType.SCROLL_UP:
            self._on_activate()
            self._on_scroll(-1)
            with contextlib.suppress(Exception):
                get_app().invalidate()
            return None
        if event == MouseEventType.SCROLL_DOWN:
            self._on_activate()
            self._on_scroll(1)
            with contextlib.suppress(Exception):
                get_app().invalidate()
            return None
        if event == MouseEventType.MOUSE_DOWN:
            # Fragment-level handlers (3-tuples) are dispatched by the base class.
            result = super().mouse_handler(mouse_event)
            if result is not NotImplemented:
                return result
            # Fallback: map y → row for plain 2-tuple lines.
            self._on_activate()
            self._on_click_row(mouse_event.position.y)
            with contextlib.suppress(Exception):
                get_app().invalidate()
            return None
        # Consume UP so Window default handlers don't steal it.
        if event == MouseEventType.MOUSE_UP:
            return None
        return NotImplemented


def _fuzzy_match(query: str, *parts: str) -> bool:
    """Case-insensitive subsequence match across joined parts."""
    needle = "".join(query.split()).casefold()
    if not needle:
        return True
    hay = " ".join(parts).casefold()
    if needle in hay.replace(" ", ""):
        return True
    if needle in hay:
        return True
    pos = 0
    for char in needle:
        pos = hay.find(char, pos)
        if pos < 0:
            return False
        pos += 1
    return True


def _normalize_cwd(path: str | Path) -> Path | None:
    try:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        else:
            candidate = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def _session_matches_cwd(session_cwd: str, scope_cwd: Path) -> bool:
    """True when the session belongs to scope_cwd (exact or nested under it)."""
    if not session_cwd:
        return False
    session_path = _normalize_cwd(session_cwd)
    if session_path is None:
        return False
    try:
        scope = scope_cwd.resolve()
    except (OSError, RuntimeError):
        scope = scope_cwd
    if os.path.normcase(str(session_path)) == os.path.normcase(str(scope)):
        return True
    with contextlib.suppress(Exception):
        if scope == Path.home().resolve() or scope == Path("/"):
            return False
    try:
        session_path.relative_to(scope)
        return True
    except ValueError:
        return False


class _LaunchScreen:
    """Single-screen launcher: config (left) | sessions (right)."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        providers: list[Provider],
        history: LaunchHistory,
        default_cwd: Path,
    ) -> None:
        self.settings = settings
        self.history = history
        self.default_cwd = default_cwd
        self._all_providers = providers
        self.apps = [app for app in AppKind if any(p.app is app for p in providers)]
        if not self.apps:
            self.apps = list(AppKind)

        self.app_index = 0
        last_app = self._last_used_app()
        if last_app is not None and last_app in self.apps:
            self.app_index = self.apps.index(last_app)

        self._sessions_cache: dict[AppKind, list[Session]] = {}
        self._filtered_sessions_cache: list[Session] | None = None
        self._filtered_providers_cache: list[Provider] | None = None
        self._provider_scroll = 0
        self._session_scroll = 0
        self.provider_filter = ""
        self.session_filter = ""
        self.filter_mode = False
        # Default: only sessions for the launch/working directory (like native CLIs).
        self.sessions_scope: SessionScope = "this_dir"

        if self.filtered_sessions:
            self.session_index = 1
            self.focus = "sessions"
        else:
            self.session_index = 0
            self.focus = "app"

        self.provider_index = 0
        self.button_index = 0
        self.status = ""
        self.status_error = False
        self._pending_delete_session: Session | None = None
        self._preview_session: Session | None = None
        self._preview_messages: list[SessionMessage] = []
        self._preview_scroll = 0
        self._show_help = False
        self._help_scroll = 0
        self._show_cwd_selector = False
        self._cwd_index = 0
        self._cwd_scroll = 0
        self._cwd_filter = ""
        self._recent_directories: list[Path] = []

        self._focus_sink = Window(
            content=FormattedTextControl("", focusable=True, show_cursor=False),
            height=0,
            dont_extend_height=True,
        )

        self._sync_provider_index()
        self._sync_permission_selection()
        self._ensure_session_visible()
        self._build_application()

    def run(self) -> LaunchPlan | None:
        result: LaunchPlan | None = self.application.run()
        return result

    # --- data helpers -------------------------------------------------

    def _last_used_app(self) -> AppKind | None:
        best: AppKind | None = None
        best_ts = -1
        for app in AppKind:
            for provider in self._all_providers:
                if provider.app is not app:
                    continue
                usage = self.history.usage(provider)
                if usage.last_launched_at > best_ts:
                    best_ts = usage.last_launched_at
                    best = app
        return best

    def _permission_presets(self) -> tuple[PermissionPreset, ...]:
        return PERMISSION_PRESETS[self.current_app]

    def _default_permission_index(self) -> int:
        presets = self._permission_presets()
        mode, approval, sandbox, always = self._effective_permission_values()
        for index, preset in enumerate(presets):
            if (
                self.current_app in {AppKind.CLAUDE, AppKind.GEMINI}
                and preset.permission_mode == mode
            ):
                return index
            if (
                self.current_app is AppKind.CODEX
                and preset.approval_policy == approval
                and preset.sandbox_mode == sandbox
            ):
                return index
            if (
                self.current_app is AppKind.GROK
                and preset.sandbox_mode == sandbox
                and preset.always_approve is always
            ):
                return index
            if (
                self.current_app is AppKind.OPENCODE
                and preset.permission_mode == mode
                and preset.always_approve is always
            ):
                return index
        return 0

    def _effective_permission_values(
        self,
    ) -> tuple[str | None, str | None, str | None, bool | None]:
        """Return (permission_mode, approval_policy, sandbox_mode, always_approve)."""
        provider = self.current_provider
        app = self.current_app
        if provider is None:
            if app is AppKind.CLAUDE:
                return self.settings.claude.permission_mode, None, None, None
            if app is AppKind.GEMINI:
                return self.settings.gemini.approval_mode, None, None, None
            if app is AppKind.CODEX:
                return (
                    None,
                    self.settings.codex.approval_policy,
                    self.settings.codex.sandbox_mode,
                    None,
                )
            if app is AppKind.OPENCODE:
                return (
                    self.settings.opencode.permission_mode,
                    None,
                    None,
                    self.settings.opencode.always_approve,
                )
            return None, None, self.settings.grok.sandbox_mode, self.settings.grok.always_approve
        try:
            runtime = runtime_from_provider(provider).with_permission_defaults(self.settings)
        except ProviderError:
            if app is AppKind.CLAUDE:
                return self.settings.claude.permission_mode, None, None, None
            if app is AppKind.GEMINI:
                return self.settings.gemini.approval_mode, None, None, None
            if app is AppKind.CODEX:
                return (
                    None,
                    self.settings.codex.approval_policy,
                    self.settings.codex.sandbox_mode,
                    None,
                )
            if app is AppKind.OPENCODE:
                return (
                    self.settings.opencode.permission_mode,
                    None,
                    None,
                    self.settings.opencode.always_approve,
                )
            return None, None, self.settings.grok.sandbox_mode, self.settings.grok.always_approve
        if isinstance(runtime, ClaudeRuntime):
            return runtime.permission_mode, None, None, None
        if isinstance(runtime, GeminiRuntime):
            return runtime.approval_mode, None, None, None
        if isinstance(runtime, CodexRuntime):
            return None, runtime.approval_policy, runtime.sandbox_mode, None
        if isinstance(runtime, GrokRuntime):
            return None, None, runtime.sandbox_mode, runtime.always_approve
        if isinstance(runtime, OpenCodeRuntime):
            return runtime.permission_mode, None, None, runtime.always_approve
        return None, None, None, None

    def _sync_permission_selection(self) -> None:
        self.permission_index = self._default_permission_index()
        self.permission_override = False

    @property
    def current_app(self) -> AppKind:
        return self.apps[self.app_index]

    @property
    def all_app_providers(self) -> list[Provider]:
        return [provider for provider in self._all_providers if provider.app is self.current_app]

    @property
    def filtered_providers(self) -> list[Provider]:
        if self._filtered_providers_cache is not None:
            return self._filtered_providers_cache
        providers = self.all_app_providers
        query = self.provider_filter
        if not query:
            self._filtered_providers_cache = providers
            return providers
        result: list[Provider] = []
        for provider in providers:
            display = display_configuration(provider)
            if _fuzzy_match(query, provider.name, display.model or "", provider.id):
                result.append(provider)
        self._filtered_providers_cache = result
        return result

    @property
    def current_provider(self) -> Provider | None:
        providers = self.filtered_providers
        if not providers:
            return None
        return providers[min(self.provider_index, len(providers) - 1)]

    @property
    def all_sessions(self) -> list[Session]:
        app = self.current_app
        if app not in self._sessions_cache:
            self._sessions_cache[app] = list_sessions(self.settings, app)
        return self._sessions_cache[app]

    @property
    def filtered_sessions(self) -> list[Session]:
        if self._filtered_sessions_cache is not None:
            return self._filtered_sessions_cache
        sessions = self.all_sessions
        if self.sessions_scope == "this_dir":
            scope_cwd = self._session_scope_cwd()
            sessions = [
                session for session in sessions if _session_matches_cwd(session.cwd, scope_cwd)
            ]
        query = self.session_filter
        if query:
            sessions = [
                session
                for session in sessions
                if _fuzzy_match(query, session.title, session.cwd, session.session_id)
            ]
        self._filtered_sessions_cache = sessions
        return sessions

    def _session_scope_cwd(self) -> Path:
        """Directory used for the 'this dir' session scope."""
        return self.default_cwd

    def _invalidate_provider_filter(self) -> None:
        self._filtered_providers_cache = None

    def _invalidate_session_filter(self) -> None:
        self._filtered_sessions_cache = None

    def _toggle_sessions_scope(self) -> None:
        self.sessions_scope = "all" if self.sessions_scope == "this_dir" else "this_dir"
        self._invalidate_session_filter()
        self.session_index = 0
        self._session_scroll = 0
        self._clamp_session_index()
        self.status = (
            "Showing all projects' sessions"
            if self.sessions_scope == "all"
            else "Showing current directory sessions"
        )
        self.status_error = False

    @property
    def selected_session(self) -> Session | None:
        if self.session_index <= 0:
            return None
        sessions = self.filtered_sessions
        index = self.session_index - 1
        if 0 <= index < len(sessions):
            return sessions[index]
        return None

    @property
    def current_preset(self) -> PermissionPreset:
        presets = self._permission_presets()
        return presets[min(self.permission_index, len(presets) - 1)]

    def _session_entry_count(self) -> int:
        return 1 + len(self.filtered_sessions)

    def _sync_provider_index(self) -> None:
        providers = self.filtered_providers
        default_id = self.history.default_provider_id(self.current_app, self.all_app_providers)
        if default_id:
            for index, provider in enumerate(providers):
                if provider.id == default_id:
                    self.provider_index = index
                    self._provider_scroll = 0
                    return
        self.provider_index = 0
        self._provider_scroll = 0

    def _set_app(self, index: int) -> None:
        if index == self.app_index:
            return
        self.app_index = index
        self.session_index = 0
        self._session_scroll = 0
        self.provider_filter = ""
        self.session_filter = ""
        self.filter_mode = False
        self._invalidate_provider_filter()
        self._invalidate_session_filter()
        self._sync_provider_index()
        self._sync_permission_selection()
        self._pending_delete_session = None
        self.status = ""
        self.status_error = False
        self._ensure_provider_visible()
        self._ensure_session_visible()
        self._sync_layout_focus()

    def _set_session(self, index: int) -> None:
        if self._pending_delete_session is not None:
            self._pending_delete_session = None
            self.status = ""
            self.status_error = False
        max_index = max(0, self._session_entry_count() - 1)
        self.session_index = max(0, min(index, max_index))
        self._ensure_session_visible()

    def _request_delete_session(self) -> None:
        session = self.selected_session
        if session is None:
            self.status = "Cannot delete 'New session'"
            self.status_error = True
            return
        self._pending_delete_session = session
        self.status = f"Delete '{session.title}'? Press y to confirm (esc to cancel)"
        self.status_error = True

    def _confirm_delete_session(self) -> None:
        session = self._pending_delete_session
        self._pending_delete_session = None
        if session is None:
            return
        success = delete_session(self.settings, session)
        if not success:
            self.status = f"Failed to delete session: {session.title}"
            self.status_error = True
            return
        if self.current_app in self._sessions_cache:
            self._sessions_cache[self.current_app] = [
                s
                for s in self._sessions_cache[self.current_app]
                if s.session_id != session.session_id
            ]
        self._invalidate_session_filter()
        max_index = max(0, self._session_entry_count() - 1)
        if self.session_index > max_index:
            self.session_index = max(0, max_index)
        self._ensure_session_visible()
        self.status = f"Deleted session: {session.title}"
        self.status_error = False

    def _test_current_provider(self) -> None:
        provider = self.current_provider
        if provider is None:
            self.status = "No provider selected"
            self.status_error = True
            return
        self.status = f"Testing connection to {provider.name}..."
        self.status_error = False

        def worker() -> None:
            ok, msg = check_provider_connectivity(provider)
            self.status = f"{provider.name}: {msg}"
            self.status_error = not ok
            with contextlib.suppress(Exception):
                get_app().invalidate()

        threading.Thread(target=worker, daemon=True).start()

    def _open_help(self) -> None:
        self._show_help = True
        self._help_scroll = 0
        help_win = getattr(self, "_help_window", None)
        if help_win is not None:
            help_win.vertical_scroll = 0
        self._sync_layout_focus()

    def _close_help(self) -> None:
        self._show_help = False
        self._help_scroll = 0
        self._sync_layout_focus()

    def _scroll_help(self, delta: int) -> None:
        max_scroll = max(0, self._help_total_lines() - self._help_height())
        self._help_scroll = max(0, min(self._help_scroll + delta, max_scroll))
        help_win = getattr(self, "_help_window", None)
        if help_win is not None:
            help_win.vertical_scroll = self._help_scroll

    def _help_height(self, default: int = 20) -> int:
        win = getattr(self, "_help_window", None)
        info = getattr(win, "render_info", None) if win is not None else None
        height = getattr(info, "window_height", None) if info is not None else None
        if isinstance(height, int):
            return max(5, height)
        return default

    def _help_width(self, default: int = 70) -> int:
        win = getattr(self, "_help_window", None)
        info = getattr(win, "render_info", None) if win is not None else None
        width = getattr(info, "window_width", None) if info is not None else None
        if isinstance(width, int):
            return max(30, width + 2)
        return default

    def _help_total_lines(self) -> int:
        lines = self._help_lines()
        return max(1, sum(item[1].count("\n") for item in lines))

    def _help_top_text(self) -> StyleAndTextTuples:
        width = self._help_width()
        border = "class:popup.border"
        badge_style = "class:popup.title.badge"
        hint_style = "class:popup.title.hint"

        badge = " HELP "
        title = "KEYBOARD SHORTCUTS"
        hint = " [Esc / ?: close] "

        prefix_len = len("╔═╡") + len(badge) + len("╞═ ")
        suffix_len = len(" ═╡") + len(hint) + len("╞═╗")
        used = prefix_len + len(title) + suffix_len
        pad = max(0, width - used)

        return [
            (border, "╔═╡"),
            (badge_style, badge),
            (border, "╞═ "),
            ("class:popup.title.text", f"{title}"),
            (border, " ═╡"),
            (hint_style, hint),
            (border, "╞" + "═" * (pad + 1) + "╗"),
        ]

    def _help_bottom_text(self) -> StyleAndTextTuples:
        width = self._help_width()
        border = "class:popup.border"
        hint_style = "class:popup.footer.hint"

        hint = " ↑↓/jk: scroll · Esc/Enter/?: close "
        left_len = len("╚═╡") + len(hint)
        right_len = len("╞═╝")
        used = left_len + right_len
        pad = max(0, width - used)

        return [
            (border, "╚═╡"),
            (hint_style, hint),
            (border, "╞" + "═" * (pad + 1) + "╝"),
        ]

    def _help_vert_text(self) -> StyleAndTextTuples:
        height = max(1, self._help_height())
        return [("class:popup.border", "║\n" * height)]

    def _help_lines(self) -> StyleAndTextTuples:
        lines: StyleAndTextTuples = []
        lines.append(("", "\n"))
        lines.append(("class:popup.header.title", "  ⌨  SHORTCUTS CHEAT SHEET\n"))
        lines.append(("class:popup.divider", "  " + "┄" * 58 + "\n\n"))

        sections: list[tuple[str, list[tuple[str, str]]]] = [
            (
                "Global Navigation",
                [
                    ("Tab / S-Tab", "Cycle focus forward / backward across panels"),
                    ("↑ / ↓  or  j / k", "Navigate items in active list"),
                    ("← / →", "Switch between columns (or Launch ↔ Cancel)"),
                    ("1 - 9", "Quick jump to item number in active list"),
                    ("Enter", "Advance to next pane / activate button"),
                    ("Ctrl+Enter / C-j", "Direct launch immediately with selection"),
                    ("c", "Switch working directory (recent projects popup)"),
                    ("?", "Toggle this help cheat sheet"),
                    ("Esc", "Cancel current mode or exit"),
                ],
            ),
            (
                "Sessions Panel",
                [
                    ("p  or  Space", "Preview conversation history (popup)"),
                    ("a", "Toggle scope: current directory ↔ all history"),
                    ("n", "Jump to 'New session' (start clean session)"),
                    ("d", "Delete session (prompts 'y' to confirm)"),
                    ("/", "Fuzzy search / filter sessions"),
                ],
            ),
            (
                "Provider Panel",
                [
                    ("t", "Test connectivity & latency to selected provider"),
                    ("/", "Fuzzy search / filter providers"),
                ],
            ),
            (
                "Preview / CWD Popups",
                [
                    ("↑ / ↓  or  j / k", "Navigate / scroll line-by-line"),
                    ("PgUp / PgDn", "Scroll preview page-by-page"),
                    ("1 - 9", "Quick select item number (CWD selector)"),
                    ("Home / End", "Jump to top / bottom"),
                    ("Esc / Enter / q", "Close popup (Enter selects in CWD)"),
                ],
            ),
        ]
        for section_title, shortcuts in sections:
            lines.append(("class:header.accent", f"  ◆ {section_title}\n"))
            for key, desc in shortcuts:
                lines.append(("class:popup.key", f"    {key:<18}"))
                lines.append(("class:item", f"  {desc}\n"))
            lines.append(("", "\n"))
        return lines

    def _discover_recent_directories(self) -> list[Path]:
        current = self.default_cwd.resolve()
        dirs = [current]
        seen = {os.path.normcase(str(current))}

        for app in self.apps:
            if app not in self._sessions_cache:
                self._sessions_cache[app] = list_sessions(self.settings, app)
            for session in self._sessions_cache[app]:
                if not session.cwd:
                    continue
                try:
                    p = Path(session.cwd).expanduser().resolve()
                    key = os.path.normcase(str(p))
                    if p.is_dir() and key not in seen:
                        seen.add(key)
                        dirs.append(p)
                except (OSError, RuntimeError):
                    continue
        return dirs

    @property
    def filtered_directories(self) -> list[Path]:
        if not self._cwd_filter:
            return self._recent_directories
        return [
            d
            for d in self._recent_directories
            if _fuzzy_match(self._cwd_filter, d.name, str(d))
        ]

    def _open_cwd_selector(self) -> None:
        self._recent_directories = self._discover_recent_directories()
        self._cwd_filter = ""
        self._cwd_index = 0
        self._cwd_scroll = 0
        self._show_cwd_selector = True
        self._sync_layout_focus()

    def _close_cwd_selector(self) -> None:
        self._show_cwd_selector = False
        self._cwd_filter = ""
        self._sync_layout_focus()

    def _select_cwd(self, directory: Path) -> None:
        self._close_cwd_selector()
        self.default_cwd = directory.resolve()
        self._invalidate_session_filter()
        self.session_index = 1 if self.filtered_sessions else 0
        self._session_scroll = 0
        self._clamp_session_index()
        self.status = f"Working directory set to: {_short_path(str(self.default_cwd))}"
        self.status_error = False

    def _navigate_cwd(self, delta: int) -> None:
        count = len(self.filtered_directories)
        if count == 0:
            return
        self._cwd_index = max(0, min(self._cwd_index + delta, count - 1))
        cwd_win = getattr(self, "_cwd_window", None)
        if cwd_win is not None:
            cwd_win.vertical_scroll = self._cwd_index * _SESSION_ROW

    def _jump_cwd(self, index: int) -> None:
        count = len(self.filtered_directories)
        if 0 <= index < count:
            self._cwd_index = index
            cwd_win = getattr(self, "_cwd_window", None)
            if cwd_win is not None:
                cwd_win.vertical_scroll = self._cwd_index * _SESSION_ROW

    def _cwd_height(self, default: int = 15) -> int:
        win = getattr(self, "_cwd_window", None)
        info = getattr(win, "render_info", None) if win is not None else None
        height = getattr(info, "window_height", None) if info is not None else None
        if isinstance(height, int):
            return max(5, height)
        return default

    def _cwd_width(self, default: int = 70) -> int:
        win = getattr(self, "_cwd_window", None)
        info = getattr(win, "render_info", None) if win is not None else None
        width = getattr(info, "window_width", None) if info is not None else None
        if isinstance(width, int):
            return max(30, width + 2)
        return default

    def _cwd_top_text(self) -> StyleAndTextTuples:
        width = self._cwd_width()
        border = "class:popup.border"
        badge_style = "class:popup.title.badge"
        hint_style = "class:popup.title.hint"

        badge = " CWD "
        title = "SWITCH WORKING DIRECTORY"
        filt = f" /{self._cwd_filter}█" if self._cwd_filter else ""
        hint = " [Esc: cancel · Enter: select] "

        prefix_len = len("╔═╡") + len(badge) + len("╞═ ")
        suffix_len = len(" ═╡") + len(hint) + len("╞═╗")
        avail = max(10, width - prefix_len - suffix_len)
        full_title = f"{title}{filt}"
        if len(full_title) > avail:
            full_title = full_title[: max(0, avail - 1)] + "…"

        used = prefix_len + len(full_title) + suffix_len
        pad = max(0, width - used)

        return [
            (border, "╔═╡"),
            (badge_style, badge),
            (border, "╞═ "),
            ("class:popup.title.text", full_title),
            (border, " ═╡"),
            (hint_style, hint),
            (border, "╞" + "═" * (pad + 1) + "╗"),
        ]

    def _cwd_bottom_text(self) -> StyleAndTextTuples:
        width = self._cwd_width()
        border = "class:popup.border"
        hint_style = "class:popup.footer.hint"

        hint = " ↑↓/jk: nav · 1-9: jump · type: filter · Esc: close "
        left_len = len("╚═╡") + len(hint)
        right_len = len("╞═╝")
        used = left_len + right_len
        pad = max(0, width - used)

        return [
            (border, "╚═╡"),
            (hint_style, hint),
            (border, "╞" + "═" * (pad + 1) + "╝"),
        ]

    def _cwd_vert_text(self) -> StyleAndTextTuples:
        height = max(1, self._cwd_height())
        return [("class:popup.border", "║\n" * height)]

    def _cwd_lines(self) -> StyleAndTextTuples:
        lines: StyleAndTextTuples = []
        dirs = self.filtered_directories
        if not dirs:
            lines.append(("", "\n"))
            lines.append(("class:item.muted", "    (no matching directories found)\n"))
            return lines

        for index, path in enumerate(dirs):
            selected = index == self._cwd_index
            is_current = path == self.default_cwd.resolve()
            name = path.name or str(path)
            mark = " (current)" if is_current else ""
            title = f"{name}{mark}"
            subtitle = _short_path(str(path))
            shortcut_num = (index + 1) if index < 9 else None

            def handler(mouse_event: MouseEvent, target_path: Path = path) -> object:
                if mouse_event.event_type != MouseEventType.MOUSE_DOWN:
                    return None
                self._select_cwd(target_path)
                with contextlib.suppress(Exception):
                    get_app().invalidate()
                return None

            self._append_entry(
                lines,
                focused=True,
                selected=selected,
                title=title,
                subtitle=subtitle,
                shortcut_num=shortcut_num,
                mouse_handler=handler,
            )
        return lines

    def _preview_width(self, default: int = 80) -> int:
        win = getattr(self, "_preview_window", None)
        info = getattr(win, "render_info", None) if win is not None else None
        width = getattr(info, "window_width", None) if info is not None else None
        if isinstance(width, int):
            return max(30, width + 2)
        return default

    def _preview_height(self, default: int = 20) -> int:
        win = getattr(self, "_preview_window", None)
        info = getattr(win, "render_info", None) if win is not None else None
        height = getattr(info, "window_height", None) if info is not None else None
        if isinstance(height, int):
            return max(5, height)
        return default

    def _preview_total_lines(self) -> int:
        lines = self._preview_lines()
        total = 0
        for item in lines:
            text = item[1]
            total += text.count("\n")
        return max(1, total)

    def _open_preview(self) -> None:
        session = self.selected_session
        if session is None:
            self.status = "Cannot preview 'New session'"
            self.status_error = True
            return
        self._preview_session = session
        self._preview_messages = read_session_messages(self.settings, session)
        self.status = ""
        self.status_error = False

        # Default: scroll to the latest conversation turn at the bottom
        height = self._preview_height(default=20)
        total_lines = self._preview_total_lines()
        self._preview_scroll = max(0, total_lines - height)
        preview_win = getattr(self, "_preview_window", None)
        if preview_win is not None:
            preview_win.vertical_scroll = self._preview_scroll
        self._sync_layout_focus()

    def _close_preview(self) -> None:
        self._preview_session = None
        self._preview_messages = []
        self._preview_scroll = 0
        self._sync_layout_focus()

    def _scroll_preview(self, delta: int) -> None:
        height = self._preview_height(default=20)
        max_scroll = max(0, self._preview_total_lines() - height)
        self._preview_scroll = max(0, min(self._preview_scroll + delta, max_scroll))
        preview_win = getattr(self, "_preview_window", None)
        if preview_win is not None:
            preview_win.vertical_scroll = self._preview_scroll

    def _popup_top_text(self) -> StyleAndTextTuples:
        if self._preview_session is None:
            return []
        width = self._preview_width()
        border = "class:popup.border"
        badge_style = "class:popup.title.badge"
        hint_style = "class:popup.title.hint"

        title = self._preview_session.title or self._preview_session.session_id
        badge = " PREVIEW "
        hint = " [Esc / Enter: close] "

        prefix_len = len("╔═╡") + len(badge) + len("╞═ ")
        suffix_len = len(" ═╡") + len(hint) + len("╞═╗")
        avail = max(10, width - prefix_len - suffix_len)
        if len(title) > avail:
            title = title[: max(0, avail - 1)] + "…"

        used = prefix_len + len(title) + suffix_len
        pad = max(0, width - used)

        return [
            (border, "╔═╡"),
            (badge_style, badge),
            (border, "╞═ "),
            ("class:popup.title.text", f"{title}"),
            (border, " ═╡"),
            (hint_style, hint),
            (border, "╞" + "═" * (pad + 1) + "╗"),
        ]

    def _popup_bottom_text(self) -> StyleAndTextTuples:
        if self._preview_session is None:
            return []
        width = self._preview_width()
        border = "class:popup.border"
        hint_style = "class:popup.footer.hint"

        height = self._preview_height()
        total_lines = self._preview_total_lines()
        max_scroll = max(0, total_lines - height)

        if max_scroll == 0 or self._preview_scroll >= max_scroll:
            status_text = " LATEST "
            status_style = "class:popup.footer.badge.latest"
        elif self._preview_scroll == 0:
            status_text = " TOP "
            status_style = "class:popup.footer.badge.history"
        else:
            pct = int((self._preview_scroll / max_scroll) * 100)
            status_text = f" {pct}% "
            status_style = "class:popup.footer.badge.history"

        hint = " ↑↓/jk: scroll · PgUp/PgDn: page "
        left_len = len("╚═╡") + len(hint) + len("╞")
        right_len = len("╡") + len(status_text) + len("╞═╝")
        used = left_len + right_len
        pad = max(0, width - used)

        return [
            (border, "╚═╡"),
            (hint_style, hint),
            (border, "╞" + "═" * pad + "╡"),
            (status_style, status_text),
            (border, "╞═╝"),
        ]

    def _popup_vert_text(self) -> StyleAndTextTuples:
        height = max(1, self._preview_height())
        return [("class:popup.border", "║\n" * height)]

    def _preview_lines(self) -> StyleAndTextTuples:
        if self._preview_session is None:
            return []
        lines: StyleAndTextTuples = []
        app_name = self.current_app.display_name
        time_str = datetime.fromtimestamp(self._preview_session.modified_at).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cwd_str = _short_path(self._preview_session.cwd) if self._preview_session.cwd else "—"

        lines.append(("", "\n"))
        lines.append(("class:popup.header.title", f"  ◆ {self._preview_session.title}\n"))
        lines.append(
            (
                "class:popup.header.meta",
                f"    App: {app_name}  ·  Updated: {time_str}  ·  CWD: {cwd_str}\n",
            )
        )
        lines.append(("class:popup.divider", "  " + "┄" * 58 + "\n\n"))

        if not self._preview_messages:
            lines.append(
                ("class:item.muted", "  (no conversation messages found for this session)\n\n")
            )
            return lines

        for msg in self._preview_messages:
            if msg.role == "user":
                lines.append(("class:popup.user.badge", "  ▸ USER "))
                lines.append(("", "\n"))
                for line in msg.text.strip().splitlines():
                    lines.append(("class:popup.user.pipe", "  │ "))
                    lines.append(("class:popup.user.text", f"{line}\n"))
            else:
                lines.append(("class:popup.assistant.badge", f"  ▸ {app_name.upper()} "))
                lines.append(("", "\n"))
                in_code = False
                for line in msg.text.strip().splitlines():
                    trimmed = line.strip()
                    if trimmed.startswith("```"):
                        in_code = not in_code
                        lines.append(("class:popup.code.pipe", "  │ "))
                        lines.append(("class:popup.code", f"{line}\n"))
                    elif in_code:
                        lines.append(("class:popup.code.pipe", "  │ "))
                        lines.append(("class:popup.code", f"{line}\n"))
                    else:
                        lines.append(("class:popup.assistant.pipe", "  │ "))
                        lines.append(("class:popup.assistant.text", f"{line}\n"))
            lines.append(("", "\n"))

        return lines

    def _focus_order(self) -> list[str]:
        return ["app", "sessions", "provider", "permissions", "buttons"]

    def _set_focus(self, pane: str) -> None:
        order = self._focus_order()
        if pane not in order:
            pane = order[0]
        if pane not in {"provider", "sessions"}:
            self.filter_mode = False
        self.focus = pane
        self._sync_layout_focus()

    def _move_focus(self, delta: int) -> None:
        self.filter_mode = False
        order = self._focus_order()
        if self.focus not in order:
            self.focus = order[0]
        else:
            index = order.index(self.focus)
            self.focus = order[(index + delta) % len(order)]
        self._sync_layout_focus()

    def _sync_layout_focus(self) -> None:
        if self._show_cwd_selector:
            target = getattr(self, "_cwd_window", None)
            if target is not None:
                with contextlib.suppress(Exception):
                    self.application.layout.focus(target)
                    return
        if self._show_help:
            target = getattr(self, "_help_window", None)
            if target is not None:
                with contextlib.suppress(Exception):
                    self.application.layout.focus(target)
                    return
        if self._preview_session is not None:
            target = getattr(self, "_preview_window", None)
            if target is not None:
                with contextlib.suppress(Exception):
                    self.application.layout.focus(target)
                    return
        target = {
            "app": getattr(self, "_app_window", None),
            "provider": getattr(self, "_provider_window", None),
            "permissions": getattr(self, "_permission_window", None),
            "sessions": getattr(self, "_sessions_window", None),
            "buttons": getattr(self, "_buttons_window", None),
        }.get(self.focus)
        if target is not None:
            with contextlib.suppress(Exception):
                self.application.layout.focus(target)
                return
        with contextlib.suppress(Exception):
            self.application.layout.focus(self._focus_sink)

    def _provider_capacity(self) -> int:
        # Prefer live height; fall back to the configured preferred rows so the
        # first paint after a layout change does not under-fill the pane.
        return max(
            1,
            self._visible_lines(getattr(self, "_provider_window", None), default=10)
            // _PROVIDER_ROW,
        )

    def _session_capacity(self) -> int:
        return max(
            1,
            self._visible_lines(getattr(self, "_sessions_window", None), default=20)
            // _SESSION_ROW,
        )

    def _ensure_provider_visible(self) -> None:
        count = len(self.filtered_providers)
        capacity = self._provider_capacity()
        max_scroll = max(0, count - capacity)
        if self.provider_index < self._provider_scroll:
            self._provider_scroll = self.provider_index
        elif self.provider_index >= self._provider_scroll + capacity:
            self._provider_scroll = self.provider_index - capacity + 1
        self._provider_scroll = max(0, min(self._provider_scroll, max_scroll))
        self._apply_window_scroll()

    def _ensure_session_visible(self) -> None:
        count = self._session_entry_count()
        capacity = self._session_capacity()
        max_scroll = max(0, count - capacity)
        if self.session_index < self._session_scroll:
            self._session_scroll = self.session_index
        elif self.session_index >= self._session_scroll + capacity:
            self._session_scroll = self.session_index - capacity + 1
        self._session_scroll = max(0, min(self._session_scroll, max_scroll))
        self._apply_window_scroll()

    def _apply_window_scroll(self) -> None:
        """Drive Window.vertical_scroll from logical entry offsets.

        Full lists are rendered; the Window clips. This avoids stale
        content-windowing when sibling panes resize (e.g. directory hide).
        """
        provider_window = getattr(self, "_provider_window", None)
        if provider_window is not None:
            provider_window.vertical_scroll = self._provider_scroll * _PROVIDER_ROW
        sessions_window = getattr(self, "_sessions_window", None)
        if sessions_window is not None:
            sessions_window.vertical_scroll = self._session_scroll * _SESSION_ROW

    def _visible_lines(self, window: Window | None, *, default: int) -> int:
        if window is None:
            return default
        info = window.render_info
        if info is None:
            return default
        # Prefer content height (rows actually painted for the control body).
        height = getattr(info, "window_height", None)
        if not isinstance(height, int) or height < 1:
            return default
        return height

    def _clamp_provider_index(self) -> None:
        providers = self.filtered_providers
        if not providers:
            self.provider_index = 0
        else:
            self.provider_index = max(0, min(self.provider_index, len(providers) - 1))
        self._sync_permission_selection()
        self._ensure_provider_visible()

    def _clamp_session_index(self) -> None:
        max_index = max(0, self._session_entry_count() - 1)
        self.session_index = max(0, min(self.session_index, max_index))
        self._ensure_session_visible()

    # --- filter -------------------------------------------------------

    def _active_filter(self) -> str:
        if self.focus == "provider":
            return self.provider_filter
        if self.focus == "sessions":
            return self.session_filter
        return ""

    def _set_active_filter(self, value: str) -> None:
        if self.focus == "provider":
            self.provider_filter = value
            self._invalidate_provider_filter()
            self._clamp_provider_index()
        elif self.focus == "sessions":
            self.session_filter = value
            self._invalidate_session_filter()
            self._clamp_session_index()

    def _start_filter(self) -> None:
        if self.focus not in {"provider", "sessions"}:
            return
        self.filter_mode = True
        self._sync_layout_focus()

    def _filter_append(self, text: str) -> None:
        if not self.filter_mode:
            return
        self._set_active_filter(self._active_filter() + text)

    def _filter_backspace(self) -> None:
        if not self.filter_mode:
            return
        current = self._active_filter()
        if current:
            self._set_active_filter(current[:-1])
        else:
            self.filter_mode = False

    def _clear_filter(self) -> None:
        if self.focus == "provider":
            self.provider_filter = ""
            self._invalidate_provider_filter()
            self._clamp_provider_index()
        elif self.focus == "sessions":
            self.session_filter = ""
            self._invalidate_session_filter()
            self._clamp_session_index()
        self.filter_mode = False

    # --- launch / cancel ----------------------------------------------

    def _cancel(self) -> None:
        self.application.exit(result=None)

    def _try_launch(self) -> None:
        provider = self.current_provider
        if provider is None:
            self.status = f"No matching {self.current_app.display_name} providers."
            self.status_error = True
            return
        session = self.selected_session
        cwd_text = session.cwd if session is not None and session.cwd else str(self.default_cwd)
        path = Path(cwd_text).expanduser()
        path = (Path.cwd() / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_dir():
            self.status = f"Directory does not exist: {path}"
            self.status_error = True
            return
        approval: str | None = None
        sandbox: str | None = None
        permission_mode: str | None = None
        always_approve: bool | None = None
        if self.permission_override:
            preset = self.current_preset
            approval = preset.approval_policy
            sandbox = preset.sandbox_mode
            permission_mode = preset.permission_mode
            always_approve = preset.always_approve
        self.application.exit(
            result=LaunchPlan(
                provider=provider,
                cwd=path,
                session=session,
                approval_policy=approval,
                sandbox_mode=sandbox,
                permission_mode=permission_mode,
                always_approve=always_approve,
            )
        )

    # --- rendering ----------------------------------------------------

    def _header_text(self) -> StyleAndTextTuples:
        provider = self.current_provider
        name = provider.name if provider else "—"
        mode = "resume" if self.selected_session else "new"
        app = self.current_app
        cwd = (
            self.selected_session.cwd
            if self.selected_session and self.selected_session.cwd
            else str(self.default_cwd)
        )
        badge_style = f"class:badge.{app.style_key}"
        return [
            ("class:header.brand", " ccs-plus "),
            ("class:header", "▸ "),
            (badge_style, f" {app.badge} "),
            ("class:header.accent", f" {app.display_name}"),
            ("class:header", f" · {name} · "),
            ("class:header.mode", mode),
            ("class:header", f" · cwd: {_short_path(cwd)} "),
        ]

    def _footer_text(self) -> StyleAndTextTuples:
        if self._show_cwd_selector:
            return [
                ("class:footer", " "),
                ("class:footer.key", "cwd"),
                ("class:footer", " · "),
                ("class:footer.key", "esc / c"),
                ("class:footer", " close · "),
                ("class:footer.key", "enter"),
                ("class:footer", " select · "),
                ("class:footer.key", "↑↓/jk"),
                ("class:footer", " nav · "),
                ("class:footer.key", "1-9"),
                ("class:footer", " jump · "),
                ("class:footer.key", "/"),
                ("class:footer", " filter "),
            ]
        if self._show_help:
            return [
                ("class:footer", " "),
                ("class:footer.key", "help"),
                ("class:footer", " · "),
                ("class:footer.key", "esc / enter / ?"),
                ("class:footer", " close · "),
                ("class:footer.key", "↑↓/jk"),
                ("class:footer", " scroll "),
            ]
        if self._preview_session is not None:
            return [
                ("class:footer", " "),
                ("class:footer.key", "preview"),
                ("class:footer", " · "),
                ("class:footer.key", "esc / enter"),
                ("class:footer", " close · "),
                ("class:footer.key", "↑↓/jk"),
                ("class:footer", " scroll · "),
                ("class:footer.key", "pgup/pgdn"),
                ("class:footer", " page "),
            ]
        pane = self.focus
        parts: StyleAndTextTuples = [
            ("class:footer", " "),
            ("class:footer.key", pane),
            ("class:footer", " · "),
            ("class:footer.key", "tab"),
            ("class:footer", " · "),
            ("class:footer.key", "↑↓"),
            ("class:footer", " · "),
            ("class:footer.key", "enter"),
            ("class:footer", " nav · "),
            ("class:footer.key", "ctrl-enter"),
            ("class:footer", " launch · "),
            ("class:footer.key", "/"),
            ("class:footer", " filter · "),
        ]
        if pane == "sessions":
            parts.extend(
                [
                    ("class:footer.key", "p/space"),
                    ("class:footer", " prev · "),
                    ("class:footer.key", "n"),
                    ("class:footer", " new · "),
                    ("class:footer.key", "d"),
                    ("class:footer", " del · "),
                    ("class:footer.key", "a"),
                    ("class:footer", " scope · "),
                ]
            )
        elif pane == "provider":
            parts.extend(
                [
                    ("class:footer.key", "t"),
                    ("class:footer", " test · "),
                ]
            )
        else:
            parts.extend(
                [
                    ("class:footer.key", "a"),
                    ("class:footer", " scope · "),
                ]
            )
        parts.extend(
            [
                ("class:footer.key", "c"),
                ("class:footer", " cwd · "),
                ("class:footer.key", "?"),
                ("class:footer", " help · "),
                ("class:footer.key", "esc"),
                ("class:footer", " "),
            ]
        )
        filt = self._active_filter()
        if self.filter_mode or filt:
            parts.extend(
                [
                    ("class:footer", "│ "),
                    ("class:footer.filter", f"/{filt}█ " if self.filter_mode else f"/{filt} "),
                ]
            )
        if self.status:
            style = "class:status.err" if self.status_error else "class:status.ok"
            parts.extend([("class:footer", "│ "), (style, f"{self.status} ")])
        return parts

    def _pane_border_style(self, pane: str) -> str:
        return "class:frame.active.border" if self.focus == pane else "class:frame.border"

    def _pane_label_style(self, pane: str) -> str:
        return "class:frame.active.label" if self.focus == pane else "class:frame.label"

    def _pane_title_text(self, pane: str, label: str) -> str:
        filt = ""
        if pane == "provider":
            if self.provider_filter:
                filt = f" /{self.provider_filter}"
            total = len(self.filtered_providers)
            capacity = self._provider_capacity()
            if total > capacity:
                start = self._provider_scroll + 1
                end = min(total, self._provider_scroll + capacity)
                filt += f" · {start}-{end}/{total}"
        elif pane == "sessions":
            scope = "this dir" if self.sessions_scope == "this_dir" else "all"
            filt = f" · {scope}"
            if self.session_filter:
                filt += f" /{self.session_filter}"
            total = self._session_entry_count()
            capacity = self._session_capacity()
            if total > capacity:
                start = self._session_scroll + 1
                end = min(total, self._session_scroll + capacity)
                filt += f" · {start}-{end}/{total}"
        if self.focus == pane:
            suffix = " · filter" if self.filter_mode and pane in {"provider", "sessions"} else ""
            return f" ▶ {label}{filt}{suffix} "
        return f" {label}{filt} "

    def _window_width(self, pane: str, default: int = 40) -> int:
        win = {
            "app": getattr(self, "_app_window", None),
            "provider": getattr(self, "_provider_window", None),
            "permissions": getattr(self, "_permission_window", None),
            "sessions": getattr(self, "_sessions_window", None),
        }.get(pane)
        info = getattr(win, "render_info", None) if win is not None else None
        width = getattr(info, "window_width", None) if info is not None else None
        if isinstance(width, int):
            return max(12, width + 2)
        return default

    def _highlighted_frame(self, body: Any, pane: str, label: str) -> Any:
        """Stable frame tree: body stays put so mouse hit-testing keeps working.

        Only border glyphs/styles are recomputed each paint via FormattedTextControl
        callables. DynamicContainer must NOT wrap the body — rebuilding the tree
        every frame drops mouse handlers.
        """

        def top_text() -> StyleAndTextTuples:
            border = self._pane_border_style(pane)
            label_style = self._pane_label_style(pane)
            title = self._pane_title_text(pane, label)
            if self.focus == pane:
                tl, tr, h = "╔", "╗", "═"
            else:
                tl, tr, h = "┌", "┐", "─"
            width = self._window_width(pane)
            inner = max(0, width - 2)
            title_text = title if len(title) <= inner else title[: max(0, inner - 1)] + "…"
            pad = max(0, inner - len(title_text))
            left_pad = 1 if pad else 0
            right_pad = max(0, pad - left_pad)
            return [
                (border, tl + h * left_pad),
                (label_style, title_text),
                (border, h * right_pad + tr),
            ]

        def bottom_text() -> StyleAndTextTuples:
            border = self._pane_border_style(pane)
            bl, br, h = ("╚", "╝", "═") if self.focus == pane else ("└", "┘", "─")
            width = self._window_width(pane)
            return [(border, bl + h * max(0, width - 2) + br)]

        def vert_text() -> StyleAndTextTuples:
            border = self._pane_border_style(pane)
            glyph = "║" if self.focus == pane else "│"
            # Enough lines to fill tall panes; Window clips to its height.
            height = 80
            win = {
                "app": getattr(self, "_app_window", None),
                "provider": getattr(self, "_provider_window", None),
                "permissions": getattr(self, "_permission_window", None),
                "sessions": getattr(self, "_sessions_window", None),
            }.get(pane)
            info = getattr(win, "render_info", None) if win is not None else None
            if info is not None:
                height = max(1, info.window_height)
            return [(border, (glyph + "\n") * height)]

        top = Window(
            FormattedTextControl(top_text, focusable=False, show_cursor=False),
            height=1,
            dont_extend_height=True,
        )
        bottom = Window(
            FormattedTextControl(bottom_text, focusable=False, show_cursor=False),
            height=1,
            dont_extend_height=True,
        )
        left = Window(
            FormattedTextControl(vert_text, focusable=False, show_cursor=False),
            width=1,
            dont_extend_width=True,
        )
        right = Window(
            FormattedTextControl(vert_text, focusable=False, show_cursor=False),
            width=1,
            dont_extend_width=True,
        )
        return HSplit([top, VSplit([left, body, right]), bottom], style="class:frame")

    def _append_entry(
        self,
        lines: StyleAndTextTuples,
        *,
        focused: bool,
        selected: bool,
        title: str,
        subtitle: str,
        shortcut_num: int | None = None,
        mouse_handler: Callable[[MouseEvent], object] | None = None,
    ) -> None:
        style = self._row_style(focused=focused and selected, selected=selected)
        sub_style = "class:item.focused-sub" if focused and selected else "class:item.muted"
        if focused and shortcut_num is not None:
            marker = f"▸{shortcut_num}" if selected else f" {shortcut_num}"
            marker_style = (
                "class:item.marker.focused"
                if selected
                else "class:item.shortcut"
            )
        else:
            marker = "▸ " if selected else "  "
            marker_style = (
                "class:item.marker.focused"
                if focused and selected
                else "class:item.marker.selected"
                if selected
                else style
            )
        title_text = f"{title}\n"
        sub_text = f"    {subtitle}\n"
        if mouse_handler is None:
            lines.append((marker_style, marker))
            lines.append((style, title_text))
            lines.append((sub_style, sub_text))
        else:
            # 3-tuple fragments register per-cell mouse handlers in FormattedTextControl.
            lines.append((marker_style, marker, mouse_handler))
            lines.append((style, title_text, mouse_handler))
            lines.append((sub_style, sub_text, mouse_handler))

    def _session_lines(self) -> StyleAndTextTuples:
        lines: StyleAndTextTuples = []
        scope_hint = (
            f"in {_short_path(str(self._session_scope_cwd()))}"
            if self.sessions_scope == "this_dir"
            else "any project"
        )
        entries: list[tuple[int, str, str]] = [(-1, "New session", f"start fresh · {scope_hint}")]
        for index, session in enumerate(self.filtered_sessions):
            when = _relative_time(session.modified_at)
            subtitle = f"{_short_path(session.cwd)} · {when}" if session.cwd else when
            entries.append((index, session.title or session.session_id[:8], subtitle))
        if len(entries) == 1 and self.session_filter:
            entries.append((-2, "(no matches)", f"filter: {self.session_filter}"))
        elif len(entries) == 1 and self.sessions_scope == "this_dir":
            entries.append((-2, "(no sessions here)", "press a for all projects"))
        elif len(entries) == 1:
            entries.append((-2, "(no sessions)", "launch first to populate"))

        # Full list + Window.vertical_scroll (no content windowing) so sibling
        # pane resize cannot leave the list under-filled for a frame.
        focused = self.focus == "sessions"
        for row, (key, title, subtitle) in enumerate(entries):
            selected = row == self.session_index if key != -2 else False
            shortcut_num = (row + 1) if (row < 9 and key != -2) else None

            def handler(mouse_event: MouseEvent, entry: int = row, selectable: int = key) -> object:
                if mouse_event.event_type != MouseEventType.MOUSE_DOWN:
                    return None
                if selectable == -2:
                    self._set_focus("sessions")
                    return None
                self._set_focus("sessions")
                self._set_session(entry)
                with contextlib.suppress(Exception):
                    get_app().invalidate()
                return None

            self._append_entry(
                lines,
                focused=focused,
                selected=selected,
                title=title,
                subtitle=subtitle,
                shortcut_num=shortcut_num,
                mouse_handler=handler,
            )
        return lines

    def _app_lines(self) -> StyleAndTextTuples:
        lines: StyleAndTextTuples = []
        focused = self.focus == "app"
        for index, app in enumerate(self.apps):
            selected = index == self.app_index
            row_focused = focused and selected
            style = self._row_style(focused=row_focused, selected=selected)
            if focused and index < 9:
                marker = f"{index + 1}▸" if selected else f"{index + 1} "
                marker_style = (
                    "class:item.marker.focused"
                    if selected
                    else "class:item.shortcut"
                )
            else:
                marker = "● " if selected else "○ "
                marker_style = (
                    "class:item.marker.focused"
                    if row_focused
                    else "class:item.marker.selected"
                    if selected
                    else style
                )
            badge_style = (
                f"class:badge.{app.style_key}.focused"
                if row_focused
                else f"class:badge.{app.style_key}"
            )

            def handler(mouse_event: MouseEvent, entry: int = index) -> object:
                if mouse_event.event_type != MouseEventType.MOUSE_DOWN:
                    return None
                self._set_focus("app")
                self._set_app(entry)
                with contextlib.suppress(Exception):
                    get_app().invalidate()
                return None

            lines.extend(
                [
                    (marker_style, f" {marker}", handler),
                    (badge_style, f" {app.badge} ", handler),
                    (style, f" {app.display_name}\n", handler),
                ]
            )
        return lines

    def _provider_lines(self) -> StyleAndTextTuples:
        lines: StyleAndTextTuples = []
        providers = self.filtered_providers
        focused = self.focus == "provider"
        if not providers:
            msg = (
                f"(no matches: {self.provider_filter})"
                if self.provider_filter
                else "(no providers)"
            )
            lines.append(("class:item.muted", f"  {msg}\n"))
            return lines
        default_id = self.history.default_provider_id(self.current_app, self.all_app_providers)
        for index, provider in enumerate(providers):
            selected = index == self.provider_index
            display = display_configuration(provider)
            model = display.model or "no model"
            uses = self.history.usage(provider).launches
            mark = " · last" if provider.id == default_id else ""
            shortcut_num = (index + 1) if index < 9 else None

            def handler(mouse_event: MouseEvent, entry: int = index) -> object:
                if mouse_event.event_type != MouseEventType.MOUSE_DOWN:
                    return None
                self._set_focus("provider")
                self.provider_index = entry
                self._sync_permission_selection()
                self._ensure_provider_visible()
                with contextlib.suppress(Exception):
                    get_app().invalidate()
                return None

            self._append_entry(
                lines,
                focused=focused,
                selected=selected,
                title=f"{provider.name}{mark}",
                subtitle=f"{model} · {uses} use{'s' if uses != 1 else ''}",
                shortcut_num=shortcut_num,
                mouse_handler=handler,
            )
        return lines

    def _permission_lines(self) -> StyleAndTextTuples:
        lines: StyleAndTextTuples = []
        focused = self.focus == "permissions"
        presets = self._permission_presets()
        for index, preset in enumerate(presets):
            selected = index == self.permission_index
            style = self._row_style(focused=focused and selected, selected=selected)
            sub_style = "class:item.focused-sub" if focused and selected else "class:item.muted"
            if focused and index < 9:
                marker = f"{index + 1}▸" if selected else f"{index + 1} "
                marker_style = (
                    "class:item.marker.focused"
                    if selected
                    else "class:item.shortcut"
                )
            else:
                marker = "● " if selected else "○ "
                marker_style = (
                    "class:item.marker.focused"
                    if focused and selected
                    else "class:item.marker.selected"
                    if selected
                    else style
                )

            def handler(mouse_event: MouseEvent, entry: int = index) -> object:
                if mouse_event.event_type != MouseEventType.MOUSE_DOWN:
                    return None
                self._set_focus("permissions")
                self._set_permission(entry)
                with contextlib.suppress(Exception):
                    get_app().invalidate()
                return None

            lines.append((marker_style, f" {marker}", handler))
            lines.append((style, f"{preset.label}\n", handler))
            lines.append((sub_style, f"    {preset.description}\n", handler))
        return lines

    def _row_style(self, *, focused: bool, selected: bool) -> str:
        if focused:
            return "class:item.focused"
        if selected:
            return "class:item.selected"
        return "class:item"

    def _make_list_control(
        self,
        get_text: Callable[[], Any],
        pane: str,
        *,
        on_click_row: Callable[[int], None],
        on_scroll: Callable[[int], None],
        get_cursor_position: Callable[[], Point | None],
    ) -> _ScrollListControl:
        return _ScrollListControl(
            get_text,
            on_click_row=on_click_row,
            on_scroll=on_scroll,
            on_activate=lambda: self._set_focus(pane),
            get_cursor_position=get_cursor_position,
        )

    def _build_application(self) -> None:
        session_control = self._make_list_control(
            lambda: FormattedText(self._session_lines()),
            "sessions",
            on_click_row=self._click_session,
            on_scroll=self._scroll_sessions,
            get_cursor_position=lambda: Point(x=0, y=self.session_index * _SESSION_ROW),
        )
        app_control = self._make_list_control(
            lambda: FormattedText(self._app_lines()),
            "app",
            on_click_row=self._click_app,
            on_scroll=lambda d: self._navigate(d),
            # App entries are single-line rows. Using the two-line session row
            # height here places the cursor beyond the rendered content for
            # Grok/OpenCode and crashes prompt_toolkit while scrolling.
            get_cursor_position=lambda: Point(x=0, y=self.app_index),
        )
        provider_control = self._make_list_control(
            lambda: FormattedText(self._provider_lines()),
            "provider",
            on_click_row=self._click_provider,
            on_scroll=self._scroll_providers,
            get_cursor_position=lambda: Point(x=0, y=self.provider_index * _PROVIDER_ROW),
        )
        permission_control = self._make_list_control(
            lambda: FormattedText(self._permission_lines()),
            "permissions",
            on_click_row=self._click_permission,
            on_scroll=lambda d: self._navigate(d),
            get_cursor_position=lambda: Point(x=0, y=self.permission_index * _PERMISSION_ROW),
        )

        def button_fragments() -> StyleAndTextTuples:
            focused = self.focus == "buttons"
            launch_focus = focused and self.button_index == 0
            cancel_focus = focused and self.button_index == 1
            launch = "class:button.launch.focused" if launch_focus else "class:button.launch"
            cancel = "class:button.cancel.focused" if cancel_focus else "class:button.cancel"
            launch_border = (
                "class:button.launch.border.focused"
                if launch_focus
                else "class:button.launch.border"
            )
            cancel_border = (
                "class:button.cancel.border.focused"
                if cancel_focus
                else "class:button.cancel.border"
            )

            def launch_handler(mouse_event: MouseEvent) -> object:
                if mouse_event.event_type != MouseEventType.MOUSE_DOWN:
                    return None
                self._set_focus("buttons")
                self.button_index = 0
                self._try_launch()
                return None

            def cancel_handler(mouse_event: MouseEvent) -> object:
                if mouse_event.event_type != MouseEventType.MOUSE_DOWN:
                    return None
                self._set_focus("buttons")
                self.button_index = 1
                self._cancel()
                return None

            # Side-by-side square buttons; inner width must match top/bottom bar (12).
            inner = 12
            launch_label = f"{'▶ Launch':^{inner}}"
            cancel_label = f"{'✕ Cancel':^{inner}}"
            bar = "─" * inner
            gap = "  "
            return [
                (launch_border, f"┌{bar}┐", launch_handler),
                ("", gap),
                (cancel_border, f"┌{bar}┐", cancel_handler),
                ("", "\n"),
                (launch_border, "│", launch_handler),
                (launch, launch_label, launch_handler),
                (launch_border, "│", launch_handler),
                ("", gap),
                (cancel_border, "│", cancel_handler),
                (cancel, cancel_label, cancel_handler),
                (cancel_border, "│", cancel_handler),
                ("", "\n"),
                (launch_border, f"└{bar}┘", launch_handler),
                ("", gap),
                (cancel_border, f"└{bar}┘", cancel_handler),
                ("", "\n"),
            ]

        button_control = FormattedTextControl(
            button_fragments,
            focusable=True,
            show_cursor=False,
        )

        self._sessions_window = Window(
            content=session_control,
            wrap_lines=False,
            always_hide_cursor=True,
            allow_scroll_beyond_bottom=False,
            height=D(preferred=20, min=6),
        )
        self._app_window = Window(
            content=app_control, height=D(min=3, max=5), always_hide_cursor=True
        )
        self._provider_window = Window(
            content=provider_control,
            wrap_lines=False,
            always_hide_cursor=True,
            allow_scroll_beyond_bottom=False,
            # Fixed preferred height so sibling panes do not reflow peers.
            height=D(preferred=12, min=8, max=14),
        )
        self._permission_window = Window(
            content=permission_control,
            height=D(min=8, preferred=12, max=14),
            always_hide_cursor=True,
        )
        self._buttons_window = Window(
            content=button_control,
            height=3,
            always_hide_cursor=True,
            align=WindowAlign.CENTER,
        )

        left = HSplit(
            [
                self._highlighted_frame(self._app_window, "app", "app"),
                self._highlighted_frame(self._provider_window, "provider", "provider"),
                ConditionalContainer(
                    self._highlighted_frame(self._permission_window, "permissions", "permissions"),
                    filter=Condition(lambda: self.current_app.supports_permission_overrides),
                ),
                Box(self._buttons_window, padding=1),
            ],
            padding=0,
            # Keep the sessions pane anchored while content and labels refresh.
            width=D(min=42, preferred=42, max=42),
        )
        sessions_frame = self._highlighted_frame(self._sessions_window, "sessions", "sessions")
        body = VSplit([left, sessions_frame], padding=1)

        root = HSplit(
            [
                Window(
                    FormattedTextControl(lambda: FormattedText(self._header_text())),
                    height=1,
                    style="class:header",
                ),
                body,
                self._focus_sink,
                Window(
                    FormattedTextControl(lambda: FormattedText(self._footer_text())),
                    height=1,
                    style="class:footer",
                    align=WindowAlign.LEFT,
                ),
            ],
            style="class:root",
        )
        self._root_container = root
        left_border = Window(
            FormattedTextControl(self._popup_vert_text, focusable=False, show_cursor=False),
            width=1,
            dont_extend_width=True,
            style="class:popup.border",
        )
        right_border = Window(
            FormattedTextControl(self._popup_vert_text, focusable=False, show_cursor=False),
            width=1,
            dont_extend_width=True,
            style="class:popup.border",
        )
        top_border = Window(
            FormattedTextControl(self._popup_top_text, focusable=False, show_cursor=False),
            height=1,
            dont_extend_height=True,
            style="class:popup.border",
        )
        bottom_border = Window(
            FormattedTextControl(self._popup_bottom_text, focusable=False, show_cursor=False),
            height=1,
            dont_extend_height=True,
            style="class:popup.border",
        )
        self._preview_window = Window(
            content=_ScrollListControl(
                lambda: FormattedText(self._preview_lines()),
                on_click_row=lambda row: None,
                on_scroll=lambda d: self._scroll_preview(d * 3),
                on_activate=lambda: None,
                get_cursor_position=lambda: Point(
                    x=0, y=min(self._preview_scroll, max(0, self._preview_total_lines() - 1))
                ),
            ),
            wrap_lines=True,
            style="class:popup",
        )
        popup_box = HSplit(
            [
                top_border,
                VSplit([left_border, self._preview_window, right_border]),
                bottom_border,
            ],
            style="class:popup",
        )
        float_popup = Float(
            content=ConditionalContainer(
                content=popup_box,
                filter=Condition(lambda: self._preview_session is not None),
            ),
            top=1,
            bottom=1,
            left=2,
            right=2,
        )
        help_left_border = Window(
            FormattedTextControl(self._help_vert_text, focusable=False, show_cursor=False),
            width=1,
            dont_extend_width=True,
            style="class:popup.border",
        )
        help_right_border = Window(
            FormattedTextControl(self._help_vert_text, focusable=False, show_cursor=False),
            width=1,
            dont_extend_width=True,
            style="class:popup.border",
        )
        help_top_border = Window(
            FormattedTextControl(self._help_top_text, focusable=False, show_cursor=False),
            height=1,
            dont_extend_height=True,
            style="class:popup.border",
        )
        help_bottom_border = Window(
            FormattedTextControl(self._help_bottom_text, focusable=False, show_cursor=False),
            height=1,
            dont_extend_height=True,
            style="class:popup.border",
        )
        self._help_window = Window(
            content=_ScrollListControl(
                lambda: FormattedText(self._help_lines()),
                on_click_row=lambda row: None,
                on_scroll=lambda d: self._scroll_help(d * 2),
                on_activate=lambda: None,
                get_cursor_position=lambda: Point(
                    x=0, y=min(self._help_scroll, max(0, self._help_total_lines() - 1))
                ),
            ),
            wrap_lines=True,
            style="class:popup",
        )
        help_box = HSplit(
            [
                help_top_border,
                VSplit([help_left_border, self._help_window, help_right_border]),
                help_bottom_border,
            ],
            style="class:popup",
        )
        float_help = Float(
            content=ConditionalContainer(
                content=help_box,
                filter=Condition(lambda: self._show_help),
            ),
            top=2,
            bottom=2,
            left=4,
            right=4,
        )
        cwd_left_border = Window(
            FormattedTextControl(self._cwd_vert_text, focusable=False, show_cursor=False),
            width=1,
            dont_extend_width=True,
            style="class:popup.border",
        )
        cwd_right_border = Window(
            FormattedTextControl(self._cwd_vert_text, focusable=False, show_cursor=False),
            width=1,
            dont_extend_width=True,
            style="class:popup.border",
        )
        cwd_top_border = Window(
            FormattedTextControl(self._cwd_top_text, focusable=False, show_cursor=False),
            height=1,
            dont_extend_height=True,
            style="class:popup.border",
        )
        cwd_bottom_border = Window(
            FormattedTextControl(self._cwd_bottom_text, focusable=False, show_cursor=False),
            height=1,
            dont_extend_height=True,
            style="class:popup.border",
        )
        self._cwd_window = Window(
            content=_ScrollListControl(
                lambda: FormattedText(self._cwd_lines()),
                on_click_row=lambda row: None,
                on_scroll=lambda d: self._navigate_cwd(d),
                on_activate=lambda: None,
                get_cursor_position=lambda: Point(x=0, y=self._cwd_index * _SESSION_ROW),
            ),
            wrap_lines=False,
            style="class:popup",
        )
        cwd_box = HSplit(
            [
                cwd_top_border,
                VSplit([cwd_left_border, self._cwd_window, cwd_right_border]),
                cwd_bottom_border,
            ],
            style="class:popup",
        )
        float_cwd = Float(
            content=ConditionalContainer(
                content=cwd_box,
                filter=Condition(lambda: self._show_cwd_selector),
            ),
            top=2,
            bottom=2,
            left=4,
            right=4,
        )
        container: FloatContainer = FloatContainer(
            content=root, floats=[float_popup, float_help, float_cwd]
        )
        bindings = self._key_bindings()
        initial_focus = {
            "app": self._app_window,
            "sessions": self._sessions_window,
            "provider": self._provider_window,
            "permissions": self._permission_window,
            "buttons": self._buttons_window,
        }.get(self.focus, self._app_window)
        self.application: Application[LaunchPlan | None] = Application(
            layout=Layout(container, focused_element=initial_focus),
            key_bindings=bindings,
            style=STYLE,
            mouse_support=True,
            full_screen=True,
        )

    # --- mouse handlers -----------------------------------------------

    def _click_session(self, row: int) -> None:
        # Full-list content: Window maps screen y → content row (incl. scroll).
        entry = row // _SESSION_ROW
        max_index = self._session_entry_count() - 1
        if 0 <= entry <= max_index:
            self._set_session(entry)

    def _click_app(self, row: int) -> None:
        if 0 <= row < len(self.apps):
            self._set_app(row)

    def _click_provider(self, row: int) -> None:
        entry = row // _PROVIDER_ROW
        providers = self.filtered_providers
        if 0 <= entry < len(providers):
            self.provider_index = entry
            self._sync_permission_selection()
            self._ensure_provider_visible()

    def _click_permission(self, row: int) -> None:
        entry = row // _PERMISSION_ROW
        presets = self._permission_presets()
        if 0 <= entry < len(presets):
            self._set_permission(entry)

    def _set_permission(self, index: int) -> None:
        presets = self._permission_presets()
        self.permission_index = max(0, min(index, len(presets) - 1))
        self.permission_override = True

    def _scroll_sessions(self, delta: int) -> None:
        self._set_focus("sessions")
        self._navigate(delta)

    def _scroll_providers(self, delta: int) -> None:
        self._set_focus("provider")
        self._navigate(delta)

    # --- keys ---------------------------------------------------------

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()
        cwd_open = Condition(lambda: self._show_cwd_selector)
        help_open = Condition(lambda: self._show_help and not self._show_cwd_selector)
        preview_open = Condition(
            lambda: self._preview_session is not None
            and not self._show_help
            and not self._show_cwd_selector
        )
        list_nav = Condition(
            lambda: not self.filter_mode
            and not self._show_help
            and not self._show_cwd_selector
            and self._preview_session is None
        )
        filtering = Condition(
            lambda: self.filter_mode
            and not self._show_help
            and not self._show_cwd_selector
            and self._preview_session is None
        )
        can_filter = Condition(
            lambda: not self.filter_mode
            and not self._show_help
            and not self._show_cwd_selector
            and self._preview_session is None
            and self.focus in {"provider", "sessions"}
        )

        @bindings.add("escape", filter=cwd_open, eager=True)
        def _cwd_esc(event: Any) -> None:
            self._close_cwd_selector()

        @bindings.add("c", filter=cwd_open, eager=True)
        def _cwd_c_toggle(event: Any) -> None:
            if not self._cwd_filter:
                self._close_cwd_selector()
            else:
                self._cwd_filter += "c"
                self._cwd_index = 0

        @bindings.add("q", filter=cwd_open, eager=True)
        def _cwd_q(event: Any) -> None:
            if not self._cwd_filter:
                self._close_cwd_selector()
            else:
                self._cwd_filter += "q"
                self._cwd_index = 0

        @bindings.add("enter", filter=cwd_open, eager=True)
        def _cwd_enter(event: Any) -> None:
            dirs = self.filtered_directories
            if dirs and 0 <= self._cwd_index < len(dirs):
                self._select_cwd(dirs[self._cwd_index])
            else:
                self._close_cwd_selector()

        @bindings.add("up", filter=cwd_open, eager=True)
        def _cwd_up(event: Any) -> None:
            self._navigate_cwd(-1)

        @bindings.add("k", filter=cwd_open, eager=True)
        def _cwd_k(event: Any) -> None:
            if not self._cwd_filter:
                self._navigate_cwd(-1)
            else:
                self._cwd_filter += "k"
                self._cwd_index = 0

        @bindings.add("down", filter=cwd_open, eager=True)
        def _cwd_down(event: Any) -> None:
            self._navigate_cwd(1)

        @bindings.add("j", filter=cwd_open, eager=True)
        def _cwd_j(event: Any) -> None:
            if not self._cwd_filter:
                self._navigate_cwd(1)
            else:
                self._cwd_filter += "j"
                self._cwd_index = 0

        @bindings.add("pageup", filter=cwd_open, eager=True)
        def _cwd_pgup(event: Any) -> None:
            self._navigate_cwd(-5)

        @bindings.add("pagedown", filter=cwd_open, eager=True)
        def _cwd_pgdn(event: Any) -> None:
            self._navigate_cwd(5)

        @bindings.add("home", filter=cwd_open, eager=True)
        def _cwd_home(event: Any) -> None:
            self._jump_cwd(0)

        @bindings.add("end", filter=cwd_open, eager=True)
        def _cwd_end(event: Any) -> None:
            self._jump_cwd(len(self.filtered_directories) - 1)

        @bindings.add("backspace", filter=cwd_open, eager=True)
        def _cwd_bs(event: Any) -> None:
            if self._cwd_filter:
                self._cwd_filter = self._cwd_filter[:-1]
                self._cwd_index = 0

        @bindings.add("c-u", filter=cwd_open, eager=True)
        def _cwd_clear(event: Any) -> None:
            self._cwd_filter = ""
            self._cwd_index = 0

        for digit in range(1, 10):

            @bindings.add(str(digit), filter=cwd_open, eager=True)
            def _cwd_num(event: Any, n: int = digit) -> None:
                if not self._cwd_filter:
                    self._jump_cwd(n - 1)
                else:
                    self._cwd_filter += str(n)
                    self._cwd_index = 0

        @bindings.add("escape", filter=help_open, eager=True)
        def _help_esc(event: Any) -> None:
            self._close_help()

        @bindings.add("enter", filter=help_open, eager=True)
        def _help_enter(event: Any) -> None:
            self._close_help()

        @bindings.add("q", filter=help_open, eager=True)
        def _help_q(event: Any) -> None:
            self._close_help()

        @bindings.add("?", filter=help_open, eager=True)
        def _help_toggle_close(event: Any) -> None:
            self._close_help()

        @bindings.add("up", filter=help_open, eager=True)
        def _help_up(event: Any) -> None:
            self._scroll_help(-2)

        @bindings.add("k", filter=help_open, eager=True)
        def _help_k(event: Any) -> None:
            self._scroll_help(-2)

        @bindings.add("down", filter=help_open, eager=True)
        def _help_down(event: Any) -> None:
            self._scroll_help(2)

        @bindings.add("j", filter=help_open, eager=True)
        def _help_j(event: Any) -> None:
            self._scroll_help(2)

        @bindings.add("pageup", filter=help_open, eager=True)
        def _help_pgup(event: Any) -> None:
            self._scroll_help(-10)

        @bindings.add("pagedown", filter=help_open, eager=True)
        def _help_pgdn(event: Any) -> None:
            self._scroll_help(10)

        @bindings.add("home", filter=help_open, eager=True)
        def _help_home(event: Any) -> None:
            self._scroll_help(-999999)

        @bindings.add("end", filter=help_open, eager=True)
        def _help_end(event: Any) -> None:
            self._scroll_help(999999)

        @bindings.add("escape", eager=True)
        def _esc(event: Any) -> None:
            if self._show_cwd_selector:
                self._close_cwd_selector()
                return
            if self._show_help:
                self._close_help()
                return
            if self._preview_session is not None:
                self._close_preview()
                return
            if self._pending_delete_session is not None:
                self._pending_delete_session = None
                self.status = "Deletion cancelled"
                self.status_error = False
                return
            if self.filter_mode:
                self._clear_filter()
                return
            if self._active_filter():
                self._clear_filter()
                return
            event.app.exit(result=None)

        @bindings.add("enter", filter=preview_open, eager=True)
        def _preview_enter(event: Any) -> None:
            self._close_preview()

        @bindings.add("p", filter=preview_open, eager=True)
        def _preview_toggle(event: Any) -> None:
            self._close_preview()

        @bindings.add("q", filter=preview_open, eager=True)
        def _preview_q(event: Any) -> None:
            self._close_preview()

        @bindings.add("home", filter=preview_open, eager=True)
        def _preview_home(event: Any) -> None:
            self._scroll_preview(-999999)

        @bindings.add("end", filter=preview_open, eager=True)
        def _preview_end(event: Any) -> None:
            self._scroll_preview(999999)

        @bindings.add("up", filter=preview_open, eager=True)
        def _preview_up(event: Any) -> None:
            self._scroll_preview(-2)

        @bindings.add("k", filter=preview_open, eager=True)
        def _preview_k(event: Any) -> None:
            self._scroll_preview(-2)

        @bindings.add("down", filter=preview_open, eager=True)
        def _preview_down(event: Any) -> None:
            self._scroll_preview(2)

        @bindings.add("j", filter=preview_open, eager=True)
        def _preview_j(event: Any) -> None:
            self._scroll_preview(2)

        @bindings.add("pageup", filter=preview_open, eager=True)
        def _preview_pgup(event: Any) -> None:
            self._scroll_preview(-10)

        @bindings.add("pagedown", filter=preview_open, eager=True)
        def _preview_pgdn(event: Any) -> None:
            self._scroll_preview(10)

        @bindings.add("c-c", eager=True)
        def _ctrl_c(event: Any) -> None:
            event.app.exit(result=None)

        @bindings.add("tab", filter=list_nav, eager=True)
        def _tab(event: Any) -> None:
            self._move_focus(1)

        @bindings.add("s-tab", filter=list_nav, eager=True)
        def _s_tab(event: Any) -> None:
            self._move_focus(-1)

        @bindings.add("down", filter=list_nav, eager=True)
        def _down(event: Any) -> None:
            self._navigate(1)

        @bindings.add("up", filter=list_nav, eager=True)
        def _up(event: Any) -> None:
            self._navigate(-1)

        @bindings.add("j", filter=list_nav, eager=True)
        def _j(event: Any) -> None:
            self._navigate(1)

        @bindings.add("k", filter=list_nav, eager=True)
        def _k(event: Any) -> None:
            self._navigate(-1)

        @bindings.add("right", filter=list_nav, eager=True)
        def _right(event: Any) -> None:
            # Left/right switch columns (and Launch↔Cancel), not list items.
            if self.focus == "buttons":
                self.button_index = 1
            elif self.focus == "sessions":
                pass
            else:
                self._set_focus("sessions")

        @bindings.add("left", filter=list_nav, eager=True)
        def _left(event: Any) -> None:
            if self.focus == "buttons":
                if self.button_index == 1:
                    self.button_index = 0
                else:
                    self._set_focus("app")
            elif self.focus == "sessions":
                self._set_focus("app")
            else:
                pass

        @bindings.add("enter", eager=True)
        def _enter(event: Any) -> None:
            if self._show_cwd_selector:
                dirs = self.filtered_directories
                if dirs and 0 <= self._cwd_index < len(dirs):
                    self._select_cwd(dirs[self._cwd_index])
                else:
                    self._close_cwd_selector()
                return
            if self._show_help:
                self._close_help()
                return
            if self._preview_session is not None:
                self._close_preview()
                return
            if self._pending_delete_session is not None:
                self._pending_delete_session = None
                self.status = "Deletion cancelled"
                self.status_error = False
                return
            if self.filter_mode:
                self.filter_mode = False
                self._sync_layout_focus()
                return
            if self.focus == "buttons":
                if self.button_index == 0:
                    self._try_launch()
                else:
                    self._cancel()
            else:
                self._move_focus(1)

        # Traditional terminals often encode Ctrl+Enter as ordinary Enter.
        # Ctrl+J is the distinguishable control-newline form supported by
        # prompt_toolkit and is treated as the direct-launch command.
        @bindings.add("c-j", eager=True)
        def _ctrl_enter(event: Any) -> None:
            if not self.filter_mode:
                self._try_launch()

        @bindings.add("c-l", filter=list_nav, eager=True)
        def _launch_now(event: Any) -> None:
            self._try_launch()

        @bindings.add("pageup", filter=list_nav, eager=True)
        def _pgup(event: Any) -> None:
            self._navigate(-5)

        @bindings.add("pagedown", filter=list_nav, eager=True)
        def _pgdn(event: Any) -> None:
            self._navigate(5)

        @bindings.add("/", filter=can_filter, eager=True)
        def _slash(event: Any) -> None:
            self._start_filter()

        scope_toggleable = Condition(
            lambda: not self.filter_mode
            and self._pending_delete_session is None
            and self._preview_session is None
            and self.focus in {"app", "sessions"}
        )
        sessions_scope = Condition(
            lambda: self.focus == "sessions"
            and not self.filter_mode
            and self._preview_session is None
        )
        provider_scope = Condition(
            lambda: self.focus == "provider"
            and not self.filter_mode
            and self._preview_session is None
        )
        pending_delete = Condition(
            lambda: self._pending_delete_session is not None
            and self._preview_session is None
        )

        @bindings.add("a", filter=scope_toggleable, eager=True)
        def _toggle_scope(event: Any) -> None:
            self._toggle_sessions_scope()
            with contextlib.suppress(Exception):
                get_app().invalidate()

        @bindings.add("p", filter=sessions_scope, eager=True)
        def _preview_session_key(event: Any) -> None:
            self._open_preview()
            with contextlib.suppress(Exception):
                get_app().invalidate()

        @bindings.add("space", filter=sessions_scope, eager=True)
        def _preview_session_space(event: Any) -> None:
            self._open_preview()
            with contextlib.suppress(Exception):
                get_app().invalidate()

        @bindings.add("?", filter=list_nav, eager=True)
        def _help_key(event: Any) -> None:
            self._open_help()
            with contextlib.suppress(Exception):
                get_app().invalidate()

        @bindings.add("c", filter=list_nav, eager=True)
        def _cwd_key(event: Any) -> None:
            self._open_cwd_selector()
            with contextlib.suppress(Exception):
                get_app().invalidate()

        @bindings.add("n", filter=sessions_scope, eager=True)
        def _new_session(event: Any) -> None:
            self._set_session(0)
            self.status = "Selected new session"
            self.status_error = False
            with contextlib.suppress(Exception):
                get_app().invalidate()

        @bindings.add("d", filter=sessions_scope, eager=True)
        def _delete_session_key(event: Any) -> None:
            self._request_delete_session()
            with contextlib.suppress(Exception):
                get_app().invalidate()

        @bindings.add("y", filter=pending_delete, eager=True)
        def _confirm_delete(event: Any) -> None:
            self._confirm_delete_session()
            with contextlib.suppress(Exception):
                get_app().invalidate()

        @bindings.add("t", filter=provider_scope, eager=True)
        def _test_provider(event: Any) -> None:
            self._test_current_provider()
            with contextlib.suppress(Exception):
                get_app().invalidate()

        @bindings.add("backspace", filter=filtering, eager=True)
        def _bs(event: Any) -> None:
            self._filter_backspace()

        @bindings.add("c-u", filter=filtering, eager=True)
        def _clear(event: Any) -> None:
            self._set_active_filter("")

        typing_start = Condition(
            lambda: not self.filter_mode
            and not self._show_help
            and not self._show_cwd_selector
            and self._pending_delete_session is None
            and self._preview_session is None
            and self.focus in {"provider", "sessions"}
        )

        # Bind printable characters explicitly. Never use eager ``<any>``:
        # it also matches Vt100MouseEvent and would swallow clicks/scroll.
        # Shortcuts on sessions/provider (not filtering) are reserved above.
        _printable = (
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789"
            " -_.,:@+*=[]{}()!#$%^&;?~`'\"|<>"
        )
        for ch in _printable:

            @bindings.add(ch, filter=filtering, eager=True)
            def _filter_char(event: KeyPressEvent, char: str = ch) -> None:
                self._filter_append(char)

            if ch in {"n", "d", "p", " "}:
                # Covered by session pane shortcuts when focus is sessions.
                start_filter = Condition(
                    lambda: not self.filter_mode
                    and not self._show_help
                    and not self._show_cwd_selector
                    and self._pending_delete_session is None
                    and self._preview_session is None
                    and self.focus == "provider"
                )
            elif ch in {"a", "?", "c"}:
                # Covered by scope toggle / help modal / cwd selector when focus is app or sessions.
                start_filter = Condition(
                    lambda: not self.filter_mode
                    and not self._show_help
                    and not self._show_cwd_selector
                    and self._pending_delete_session is None
                    and self._preview_session is None
                    and self.focus == "provider"
                )
            elif ch == "t":
                # Covered by provider test shortcut when focus is provider.
                start_filter = Condition(
                    lambda: not self.filter_mode
                    and not self._show_help
                    and not self._show_cwd_selector
                    and self._pending_delete_session is None
                    and self._preview_session is None
                    and self.focus == "sessions"
                )
            else:
                start_filter = typing_start

            @bindings.add(ch, filter=start_filter, eager=True)
            def _start_char(event: KeyPressEvent, char: str = ch) -> None:
                if self._pending_delete_session is not None:
                    self._pending_delete_session = None
                    self.status = "Deletion cancelled"
                    self.status_error = False
                if char.isdigit() and char != "0":
                    self._jump(int(char) - 1)
                    return
                if char in "jk":
                    self._navigate(1 if char == "j" else -1)
                    return
                self.filter_mode = True
                self._filter_append(char)
                self._sync_layout_focus()

        for digit in range(1, 10):

            @bindings.add(str(digit), filter=list_nav, eager=True)
            def _num(event: Any, n: int = digit) -> None:
                self._jump(n - 1)

        return bindings

    def _navigate(self, delta: int) -> None:
        if self._pending_delete_session is not None:
            self._pending_delete_session = None
            self.status = ""
            self.status_error = False
        if self.focus == "sessions":
            self._set_session(self.session_index + delta)
        elif self.focus == "app":
            self._set_app(max(0, min(self.app_index + delta, len(self.apps) - 1)))
        elif self.focus == "provider":
            providers = self.filtered_providers
            if providers:
                next_index = max(0, min(self.provider_index + delta, len(providers) - 1))
                if next_index != self.provider_index:
                    self.provider_index = next_index
                    self._sync_permission_selection()
                self._ensure_provider_visible()
        elif self.focus == "permissions":
            self._set_permission(self.permission_index + delta)
        elif self.focus == "buttons":
            self.button_index = 0 if delta < 0 else 1

    def _jump(self, index: int) -> None:
        if self.focus == "sessions":
            max_index = self._session_entry_count() - 1
            if 0 <= index <= max_index:
                self._set_session(index)
        elif self.focus == "app":
            if 0 <= index < len(self.apps):
                self._set_app(index)
        elif self.focus == "provider":
            providers = self.filtered_providers
            if 0 <= index < len(providers):
                if index != self.provider_index:
                    self.provider_index = index
                    self._sync_permission_selection()
                self._ensure_provider_visible()
        elif self.focus == "permissions":
            if 0 <= index < len(self._permission_presets()):
                self._set_permission(index)


def _relative_time(timestamp: float) -> str:
    try:
        delta = datetime.now().timestamp() - timestamp
    except (OverflowError, OSError, ValueError):
        return ""
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 86400 * 14:
        return f"{int(delta // 86400)}d ago"
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def _short_path(path: str) -> str:
    if not path:
        return ""
    home = str(Path.home())
    text = path
    if text.startswith(home):
        text = "~" + text[len(home) :]
    if len(text) <= 40:
        return text
    return "…" + text[-39:]
