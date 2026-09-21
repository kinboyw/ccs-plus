"""Discover resume-able native CLI sessions under ccs-plus state homes."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from ccs_plus.domain import AppKind
from ccs_plus.home_visibility import OpenCodeHomeVisibility
from ccs_plus.settings import AppSettings

logger = logging.getLogger(__name__)

_MAX_SESSIONS = 80
_TITLE_MAX = 72


@dataclass(frozen=True)
class Session:
    app: AppKind
    session_id: str
    title: str
    cwd: str
    modified_at: float
    source_path: Path | None = None


@dataclass(frozen=True)
class SessionMessage:
    role: str
    text: str
    timestamp: float | None = None


class SessionReader:
    def prepare(self, settings: AppSettings) -> None:
        pass

    def list(self, home: Path, app: AppKind) -> list[Session]:
        raise NotImplementedError


class CodexSessionReader(SessionReader):
    def list(self, home: Path, app: AppKind) -> list[Session]:
        return _list_rollouts(home, app)


class ClaudeSessionReader(SessionReader):
    def list(self, home: Path, app: AppKind) -> list[Session]:
        return _list_project_logs(home, app)


class GrokSessionReader(SessionReader):
    def list(self, home: Path, app: AppKind) -> list[Session]:
        return _list_prompt_histories(home, app)


class OpenCodeSessionReader(SessionReader):
    def prepare(self, settings: AppSettings) -> None:
        OpenCodeHomeVisibility(
            state_home=settings.opencode.home,
            user_home=settings.opencode.user_home,
            user_data_home=settings.opencode.user_data_home,
            is_official=True,
        ).expose_data()

    def list(self, home: Path, app: AppKind) -> list[Session]:
        return _list_opencode_db(home, app)


class GeminiSessionReader(SessionReader):
    def list(self, home: Path, app: AppKind) -> list[Session]:
        del home, app
        return []


def session_reader_for(app: AppKind) -> SessionReader:
    readers: dict[AppKind, SessionReader] = {
        AppKind.CODEX: CodexSessionReader(),
        AppKind.CLAUDE: ClaudeSessionReader(),
        AppKind.GEMINI: GeminiSessionReader(),
        AppKind.GROK: GrokSessionReader(),
        AppKind.OPENCODE: OpenCodeSessionReader(),
    }
    return readers[app]


def list_sessions(settings: AppSettings, app: AppKind) -> list[Session]:
    """Return recent sessions for ``app``, newest first."""
    home = settings.runtime_home(app.value)
    reader = session_reader_for(app)
    try:
        reader.prepare(settings)
        sessions = reader.list(home, app)
    except OSError as exc:
        logger.warning("Unable to list %s sessions under %s: %s", app.value, home, exc)
        return []
    sessions.sort(key=lambda item: item.modified_at, reverse=True)
    return sessions[:_MAX_SESSIONS]


def _list_rollouts(home: Path, app: AppKind) -> list[Session]:
    root = home / "sessions"
    if not root.is_dir():
        return []
    # Stat/sort first so we only parse the newest files (jsonl can be huge).
    ranked = _newest_files(root.rglob("rollout-*.jsonl"), limit=_MAX_SESSIONS * 2)
    sessions: list[Session] = []
    for path in ranked:
        session = _parse_rollout(path, app)
        if session is not None:
            sessions.append(session)
        if len(sessions) >= _MAX_SESSIONS:
            break
    return sessions


def _parse_rollout(path: Path, app: AppKind) -> Session | None:
    session_id = ""
    cwd = ""
    title = ""
    try:
        timestamp = path.stat().st_mtime
    except OSError:
        return None
    # Filename embeds UUID: rollout-...-<uuid>.jsonl
    parts = path.stem.split("-")
    if len(parts) >= 5:
        candidate_id = "-".join(parts[-5:])
        if len(candidate_id) >= 36:
            session_id = candidate_id
    try:
        with path.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index > 40:
                    break
                # Huge first-line payloads (skills blobs) — skip if oversized.
                if len(line) > 64_000 and index > 0:
                    continue
                try:
                    document = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = document.get("type")
                if kind == "session_meta":
                    payload = document.get("payload")
                    if isinstance(payload, dict):
                        session_id = str(
                            payload.get("id") or payload.get("session_id") or session_id
                        )
                        cwd = str(payload.get("cwd") or "")
                elif kind == "response_item" and not title:
                    payload = document.get("payload")
                    if isinstance(payload, dict) and payload.get("role") == "user":
                        candidate = _text_from_content(payload.get("content"))
                        if candidate and not candidate.startswith("<"):
                            title = candidate
                if session_id and cwd and title:
                    break
                if session_id and cwd and index >= 8:
                    # Title is optional polish; stop early once identity is known.
                    break
    except OSError:
        return None
    if not session_id:
        return None
    if not title:
        title = Path(cwd).name if cwd else session_id[:8]
    return Session(
        app=app,
        session_id=session_id,
        title=_clip(title),
        cwd=cwd,
        modified_at=timestamp,
        source_path=path,
    )


def _newest_files(paths: Iterable[Path], *, limit: int) -> list[Path]:
    ranked: list[tuple[float, Path]] = []
    for path in paths:
        try:
            if path.is_file():
                ranked.append((path.stat().st_mtime, path))
        except OSError:
            continue
    # Filesystems may give several rollouts the same mtime; use the filename's
    # embedded timestamp/sequence as a deterministic tie-breaker.
    ranked.sort(key=lambda item: (item[0], str(item[1])), reverse=True)
    return [path for _, path in ranked[:limit]]


def _list_project_logs(home: Path, app: AppKind) -> list[Session]:
    root = home / "projects"
    if not root.is_dir():
        return []
    paths: list[Path] = []
    try:
        for project in root.iterdir():
            if project.is_dir():
                paths.extend(project.glob("*.jsonl"))
    except OSError:
        return []
    ranked = _newest_files(paths, limit=_MAX_SESSIONS * 2)
    sessions: list[Session] = []
    for path in ranked:
        session = _parse_project_log(path, app)
        if session is not None:
            sessions.append(session)
        if len(sessions) >= _MAX_SESSIONS:
            break
    return sessions


def _parse_project_log(path: Path, app: AppKind) -> Session | None:
    session_id = path.stem
    cwd = ""
    title = ""
    try:
        timestamp = path.stat().st_mtime
    except OSError:
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index > 40:
                    break
                if len(line) > 64_000:
                    continue
                try:
                    document = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(document, dict):
                    continue
                sid = document.get("sessionId")
                if isinstance(sid, str) and sid:
                    session_id = sid
                if not cwd and isinstance(document.get("cwd"), str):
                    cwd = document["cwd"]
                if document.get("type") == "user" and not title:
                    message = document.get("message")
                    if isinstance(message, dict):
                        candidate = _text_from_content(message.get("content"))
                    else:
                        candidate = _text_from_content(message)
                    if candidate:
                        title = candidate
                if document.get("type") == "summary" and not title:
                    summary = document.get("summary")
                    if isinstance(summary, str) and summary.strip():
                        title = summary.strip()
                if session_id and title and cwd:
                    break
                if session_id and cwd and index >= 8:
                    break
    except OSError:
        return None
    if not session_id:
        return None
    if not title:
        title = Path(cwd).name if cwd else session_id[:8]
    return Session(
        app=app,
        session_id=session_id,
        title=_clip(title),
        cwd=cwd,
        modified_at=timestamp,
        source_path=path,
    )


def _list_prompt_histories(home: Path, app: AppKind) -> list[Session]:
    root = home / "sessions"
    if not root.is_dir():
        return []
    by_id: dict[str, Session] = {}
    for history_path in root.glob("*/prompt_history.jsonl"):
        if not history_path.is_file():
            continue
        encoded = history_path.parent.name
        cwd = unquote(encoded)
        try:
            with history_path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        document = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(document, dict):
                        continue
                    session_id = document.get("session_id")
                    if not isinstance(session_id, str) or not session_id:
                        continue
                    prompt = document.get("prompt")
                    title = prompt.strip() if isinstance(prompt, str) else ""
                    raw_ts = document.get("timestamp")
                    if isinstance(raw_ts, str):
                        timestamp = _parse_iso(raw_ts) or history_path.stat().st_mtime
                    else:
                        timestamp = history_path.stat().st_mtime
                    if timestamp is None:
                        timestamp = history_path.stat().st_mtime
                    existing = by_id.get(session_id)
                    if existing is None:
                        by_id[session_id] = Session(
                            app=app,
                            session_id=session_id,
                            title=_clip(title or Path(cwd).name or session_id[:8]),
                            cwd=cwd,
                            modified_at=timestamp,
                            source_path=history_path,
                        )
                    elif timestamp > existing.modified_at:
                        by_id[session_id] = Session(
                            app=existing.app,
                            session_id=existing.session_id,
                            title=existing.title,
                            cwd=existing.cwd or cwd,
                            modified_at=timestamp,
                            source_path=history_path,
                        )
        except OSError:
            continue
    return list(by_id.values())


def _text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts).strip()
    return ""


def _parse_iso(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _list_opencode_db(home: Path, app: AppKind) -> list[Session]:
    """Read sessions from OpenCode SQLite under XDG data home."""
    db_path = home / "share" / "opencode" / "opencode.db"
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.warning("Unable to open OpenCode db %s: %s", db_path, exc)
        return []
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cols = {row["name"] for row in cursor.execute("PRAGMA table_info(session)").fetchall()}
        where = "WHERE time_archived IS NULL"
        if "parent_id" in cols:
            where += " AND (parent_id IS NULL OR parent_id = '')"
        rows = cursor.execute(
            f"""
            SELECT id, title, directory, time_updated, time_archived
            FROM session
            {where}
            ORDER BY time_updated DESC
            LIMIT ?
            """,
            (_MAX_SESSIONS,),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("Unable to query OpenCode sessions: %s", exc)
        return []
    finally:
        conn.close()

    sessions: list[Session] = []
    for row in rows:
        session_id = row["id"]
        if not isinstance(session_id, str) or not session_id:
            continue
        title = row["title"] if isinstance(row["title"], str) else session_id
        cwd = row["directory"] if isinstance(row["directory"], str) else ""
        raw_ts = row["time_updated"]
        if isinstance(raw_ts, (int, float)):
            # OpenCode stores ms epoch.
            timestamp = float(raw_ts) / 1000.0 if raw_ts > 1_000_000_000_000 else float(raw_ts)
        else:
            timestamp = db_path.stat().st_mtime
        sessions.append(
            Session(
                app=app,
                session_id=session_id,
                title=_clip(title or Path(cwd).name or session_id[:8]),
                cwd=cwd,
                modified_at=timestamp,
                source_path=db_path,
            )
        )
    return sessions


def _clip(text: str) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= _TITLE_MAX:
        return cleaned
    return cleaned[: _TITLE_MAX - 1] + "…"


def delete_session(settings: AppSettings, session: Session) -> bool:
    """Delete or archive a session file or database entry."""
    if session.app in {AppKind.CODEX, AppKind.CLAUDE}:
        if session.source_path is not None and session.source_path.is_file():
            try:
                session.source_path.unlink()
                return True
            except OSError as exc:
                logger.warning("Failed to delete session file %s: %s", session.source_path, exc)
                return False
        home = settings.runtime_home(session.app.value)
        if session.app is AppKind.CODEX:
            pattern = f"*{session.session_id}*.jsonl"
            for candidate in (home / "sessions").rglob(pattern):
                if candidate.is_file():
                    try:
                        candidate.unlink()
                        return True
                    except OSError:
                        return False
        elif session.app is AppKind.CLAUDE:
            pattern = f"{session.session_id}.jsonl"
            for candidate in (home / "projects").rglob(pattern):
                if candidate.is_file():
                    try:
                        candidate.unlink()
                        return True
                    except OSError:
                        return False
        return False

    if session.app is AppKind.OPENCODE:
        db_path = session.source_path or (
            settings.runtime_home(session.app.value) / "share" / "opencode" / "opencode.db"
        )
        if not db_path.is_file():
            return False
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE session SET time_archived = ? WHERE id = ?",
                    (int(datetime.now().timestamp() * 1000), session.session_id),
                )
                conn.commit()
            return True
        except sqlite3.Error as exc:
            logger.warning("Failed to archive OpenCode session %s: %s", session.session_id, exc)
            return False

    if session.app is AppKind.GROK:
        history_path = session.source_path
        if history_path is None or not history_path.is_file():
            return False
        try:
            with history_path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()
            kept: list[str] = []
            for line in lines:
                try:
                    doc = json.loads(line)
                    if isinstance(doc, dict) and doc.get("session_id") == session.session_id:
                        continue
                except json.JSONDecodeError:
                    pass
                kept.append(line)
            if kept:
                with history_path.open("w", encoding="utf-8") as handle:
                    handle.writelines(kept)
            else:
                history_path.unlink()
            return True
        except OSError as exc:
            logger.warning("Failed to delete Grok session %s: %s", session.session_id, exc)
            return False

    return False


def read_session_messages(
    settings: AppSettings, session: Session, limit: int = 50
) -> list[SessionMessage]:
    """Read conversation messages for a session across supported apps."""
    if session.app is AppKind.CODEX:
        path = session.source_path
        if path is None or not path.is_file():
            home = settings.runtime_home(session.app.value)
            candidates = list((home / "sessions").rglob(f"*{session.session_id}*.jsonl"))
            if not candidates:
                return []
            path = candidates[0]
        messages: list[SessionMessage] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if len(line) > 64_000:
                        continue
                    try:
                        doc = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if doc.get("type") == "response_item":
                        payload = doc.get("payload")
                        if isinstance(payload, dict):
                            role = payload.get("role")
                            if role in ("user", "assistant"):
                                text = _text_from_content(payload.get("content"))
                                if text and not text.startswith(
                                    ("<INSTRUCTIONS>", "<environment_context>")
                                ):
                                    messages.append(SessionMessage(role=role, text=text))
                    elif doc.get("type") == "event_msg":
                        payload = doc.get("payload")
                        if isinstance(payload, dict):
                            item = payload.get("item")
                            if isinstance(item, dict):
                                itype = item.get("type")
                                if itype in ("UserMessage", "AgentMessage"):
                                    role = "user" if itype == "UserMessage" else "assistant"
                                    text = _text_from_content(item.get("content"))
                                    if (
                                        text
                                        and not text.startswith(
                                            ("<INSTRUCTIONS>", "<environment_context>")
                                        )
                                        and (not messages or messages[-1].text != text)
                                    ):
                                        messages.append(SessionMessage(role=role, text=text))
        except OSError:
            return []
        return messages[-limit:]

    if session.app is AppKind.CLAUDE:
        path = session.source_path
        if path is None or not path.is_file():
            home = settings.runtime_home(session.app.value)
            candidates = list((home / "projects").rglob(f"{session.session_id}.jsonl"))
            if not candidates:
                return []
            path = candidates[0]
        messages = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if len(line) > 64_000:
                        continue
                    try:
                        doc = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind = doc.get("type")
                    if kind in ("user", "assistant"):
                        msg = doc.get("message")
                        content = msg.get("content") if isinstance(msg, dict) else msg
                        text = _text_from_content(content)
                        if text:
                            messages.append(SessionMessage(role=kind, text=text))
        except OSError:
            return []
        return messages[-limit:]

    if session.app is AppKind.OPENCODE:
        db_path = session.source_path or (
            settings.runtime_home(session.app.value) / "share" / "opencode" / "opencode.db"
        )
        if not db_path.is_file():
            return []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            rows = conn.execute(
                """
                SELECT m.data, p.data
                FROM message m
                JOIN part p ON m.id = p.message_id
                WHERE m.session_id = ?
                ORDER BY m.time_created ASC, p.time_created ASC
                """,
                (session.session_id,),
            ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            logger.warning("Unable to read OpenCode session messages: %s", exc)
            return []
        messages = []
        for m_raw, p_raw in rows:
            try:
                m_data = json.loads(m_raw)
                p_data = json.loads(p_raw)
            except json.JSONDecodeError:
                continue
            if p_data.get("type") == "text":
                text = str(p_data.get("text", "")).strip()
                if text:
                    role = str(m_data.get("role", "user"))
                    messages.append(SessionMessage(role=role, text=text))
        return messages[-limit:]

    if session.app is AppKind.GROK:
        path = session.source_path
        if path is None or not path.is_file():
            return []
        messages = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        doc = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if doc.get("session_id") == session.session_id:
                        prompt = doc.get("prompt")
                        if isinstance(prompt, str) and prompt.strip():
                            messages.append(SessionMessage(role="user", text=prompt.strip()))
        except OSError:
            return []
        return messages[-limit:]

    return []
