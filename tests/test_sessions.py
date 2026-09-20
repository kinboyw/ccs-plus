from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

from conftest import make_app_settings

from ccs_plus.domain import AppKind
from ccs_plus.sessions import delete_session, list_sessions, read_session_messages


def test_list_codex_sessions_parses_rollout_meta(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    day = settings.codex.user_home / "sessions" / "2026" / "08" / "13"
    day.mkdir(parents=True)
    path = day / "rollout-2026-08-13T10-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                            "timestamp": "2026-08-13T02:00:00.000Z",
                            "cwd": str(tmp_path / "project"),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "fix the flaky test"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "project").mkdir()

    sessions = list_sessions(settings, AppKind.CODEX)

    assert len(sessions) == 1
    assert sessions[0].session_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert sessions[0].title == "fix the flaky test"
    assert sessions[0].cwd == str(tmp_path / "project")


def test_list_claude_sessions_parses_project_jsonl(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    project = settings.claude.home / "projects" / "-tmp-demo"
    project.mkdir(parents=True)
    path = project / "11111111-2222-3333-4444-555555555555.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "11111111-2222-3333-4444-555555555555",
                "cwd": str(tmp_path / "demo"),
                "timestamp": "2026-08-13T03:00:00.000Z",
                "message": {"role": "user", "content": "summarize the PR"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "demo").mkdir()

    sessions = list_sessions(settings, AppKind.CLAUDE)

    assert len(sessions) == 1
    assert sessions[0].session_id == "11111111-2222-3333-4444-555555555555"
    assert sessions[0].title == "summarize the PR"


def test_list_grok_sessions_groups_prompt_history(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    cwd = str(tmp_path / "repo")
    folder = settings.grok.home / "sessions" / quote(cwd, safe="")
    folder.mkdir(parents=True)
    path = folder / "prompt_history.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "session_id": "g1",
                        "prompt": "first prompt",
                        "timestamp": "2026-08-13T01:00:00.000Z",
                    }
                ),
                json.dumps(
                    {
                        "session_id": "g1",
                        "prompt": "second prompt",
                        "timestamp": "2026-08-13T04:00:00.000Z",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    sessions = list_sessions(settings, AppKind.GROK)

    assert len(sessions) == 1
    assert sessions[0].session_id == "g1"
    assert sessions[0].title == "first prompt"
    assert sessions[0].cwd == cwd


def test_list_opencode_sessions_reads_sqlite(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    db_dir = settings.opencode.home / "share" / "opencode"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            slug TEXT NOT NULL,
            directory TEXT NOT NULL,
            title TEXT NOT NULL,
            version TEXT NOT NULL,
            cost REAL DEFAULT 0 NOT NULL,
            tokens_input INTEGER DEFAULT 0 NOT NULL,
            tokens_output INTEGER DEFAULT 0 NOT NULL,
            tokens_reasoning INTEGER DEFAULT 0 NOT NULL,
            tokens_cache_read INTEGER DEFAULT 0 NOT NULL,
            tokens_cache_write INTEGER DEFAULT 0 NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            time_archived INTEGER
        )
        """
    )
    cwd = str(tmp_path / "project")
    conn.execute(
        """
        INSERT INTO session (
            id, project_id, slug, directory, title, version,
            time_created, time_updated, time_archived
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ses_abc",
            "proj1",
            "slug",
            cwd,
            "OpenCode session title",
            "1.0",
            1_700_000_000_000,
            1_700_000_100_000,
            None,
        ),
    )
    conn.commit()
    conn.close()

    sessions = list_sessions(settings, AppKind.OPENCODE)

    assert len(sessions) == 1
    assert sessions[0].session_id == "ses_abc"
    assert sessions[0].title == "OpenCode session title"
    assert sessions[0].cwd == cwd


def test_delete_codex_session(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    day = settings.codex.user_home / "sessions" / "2026" / "08" / "13"
    day.mkdir(parents=True)
    path = day / "rollout-2026-08-13T10-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "timestamp": "2026-08-13T02:00:00.000Z",
                    "cwd": str(tmp_path),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    sessions = list_sessions(settings, AppKind.CODEX)
    assert len(sessions) == 1
    assert delete_session(settings, sessions[0]) is True
    assert not path.exists()
    assert list_sessions(settings, AppKind.CODEX) == []


def test_delete_claude_session(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    project = settings.claude.home / "projects" / "-tmp-demo"
    project.mkdir(parents=True)
    path = project / "11111111-2222-3333-4444-555555555555.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "11111111-2222-3333-4444-555555555555",
                "cwd": str(tmp_path),
                "timestamp": "2026-08-13T03:00:00.000Z",
                "message": {"role": "user", "content": "summarize the PR"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    sessions = list_sessions(settings, AppKind.CLAUDE)
    assert len(sessions) == 1
    assert delete_session(settings, sessions[0]) is True
    assert not path.exists()
    assert list_sessions(settings, AppKind.CLAUDE) == []


def test_delete_grok_session(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    cwd = str(tmp_path / "repo")
    folder = settings.grok.home / "sessions" / quote(cwd, safe="")
    folder.mkdir(parents=True)
    path = folder / "prompt_history.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "session_id": "g1",
                        "prompt": "first prompt",
                        "timestamp": "2026-08-13T01:00:00.000Z",
                    }
                ),
                json.dumps(
                    {
                        "session_id": "g2",
                        "prompt": "second prompt",
                        "timestamp": "2026-08-13T02:00:00.000Z",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    sessions = list_sessions(settings, AppKind.GROK)
    assert len(sessions) == 2
    g1 = next(s for s in sessions if s.session_id == "g1")
    assert delete_session(settings, g1) is True
    remaining = list_sessions(settings, AppKind.GROK)
    assert len(remaining) == 1
    assert remaining[0].session_id == "g2"


def test_delete_opencode_session(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    db_dir = settings.opencode.home / "share" / "opencode"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            slug TEXT NOT NULL,
            directory TEXT NOT NULL,
            title TEXT NOT NULL,
            version TEXT NOT NULL,
            cost REAL DEFAULT 0 NOT NULL,
            tokens_input INTEGER DEFAULT 0 NOT NULL,
            tokens_output INTEGER DEFAULT 0 NOT NULL,
            tokens_reasoning INTEGER DEFAULT 0 NOT NULL,
            tokens_cache_read INTEGER DEFAULT 0 NOT NULL,
            tokens_cache_write INTEGER DEFAULT 0 NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            time_archived INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO session (
            id, project_id, slug, directory, title, version,
            time_created, time_updated, time_archived
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ses_abc",
            "proj1",
            "slug",
            str(tmp_path),
            "OpenCode session title",
            "1.0",
            1_700_000_000_000,
            1_700_000_100_000,
            None,
        ),
    )
    conn.commit()
    conn.close()

    sessions = list_sessions(settings, AppKind.OPENCODE)
    assert len(sessions) == 1
    assert delete_session(settings, sessions[0]) is True
    assert list_sessions(settings, AppKind.OPENCODE) == []


def test_read_session_messages_codex(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    day = settings.codex.user_home / "sessions" / "2026" / "08" / "13"
    day.mkdir(parents=True)
    path = day / "rollout-2026-08-13T10-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                            "timestamp": "2026-08-13T02:00:00.000Z",
                            "cwd": str(tmp_path),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Hello Codex"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Hello human"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    sessions = list_sessions(settings, AppKind.CODEX)
    assert len(sessions) == 1
    messages = read_session_messages(settings, sessions[0])
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[0].text == "Hello Codex"
    assert messages[1].role == "assistant"
    assert messages[1].text == "Hello human"


def test_read_session_messages_claude(tmp_path: Path) -> None:
    settings = make_app_settings(tmp_path)
    project = settings.claude.home / "projects" / "-tmp-demo"
    project.mkdir(parents=True)
    path = project / "11111111-2222-3333-4444-555555555555.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "11111111-2222-3333-4444-555555555555",
                        "cwd": str(tmp_path),
                        "timestamp": "2026-08-13T03:00:00.000Z",
                        "message": {"role": "user", "content": "summarize the PR"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "11111111-2222-3333-4444-555555555555",
                        "cwd": str(tmp_path),
                        "timestamp": "2026-08-13T03:01:00.000Z",
                        "message": {"role": "assistant", "content": "Here is the summary"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    sessions = list_sessions(settings, AppKind.CLAUDE)
    assert len(sessions) == 1
    messages = read_session_messages(settings, sessions[0])
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[0].text == "summarize the PR"
    assert messages[1].role == "assistant"
    assert messages[1].text == "Here is the summary"


