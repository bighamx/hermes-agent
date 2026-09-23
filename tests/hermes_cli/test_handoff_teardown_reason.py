"""Tests for the ``handoff_completed`` end reason: a completed ``/handoff`` leaves a stamp recovery
accepts, instead of the terminal ``cli_close`` that orphans the handed-off leg.

Incident shape (QQ DM, one ``session_key``, 2026-09-22): the handoff completed and the gateway
dispatched the synthetic turn, then the CLI teardown stamped ``cli_close`` on the row the gateway
now owned; the next inbound DM took the stale-route self-heal, which refuses non-recoverable ends,
and minted a brand-new empty session — the leg with 253 messages was left behind.

Two halves, tested together:
  * writer — ``CLITuiRuntimeMixin._tui_shutdown`` stamps ``handoff_completed`` (and skips the
    empty-row prune) for a session ``/handoff`` transferred, ``cli_close`` otherwise;
  * reader — ``handoff_completed`` is in the recoverable set, so the peer finder reopens that row.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import cli as cli_mod
from hermes_state import SessionDB

PEER = dict(
    source="qqbot",
    user_id="D425866BDDB69A1305199BAE0B166AFB",
    session_key="agent:main:qqbot:dm:D425866BDDB69A1305199BAE0B166AFB",
    chat_id="D425866BDDB69A1305199BAE0B166AFB",
    chat_type="dm",
    thread_id=None,
)


# ── writer half: the CLI teardown ───────────────────────────────────────────

def _reset_cli_globals():
    cli_mod._cleanup_done = False
    cli_mod._cleanup_in_progress = False
    cli_mod._single_query_finalize_attempted_session_ids.clear()
    cli_mod._handed_off_session_ids.clear()
    cli_mod._active_agent_ref = None


def _tui(session_id, session_db):
    """A stand-in HermesCLI with only the attributes ``_tui_shutdown`` touches."""
    return SimpleNamespace(
        _should_exit=False,
        _pet_stop_anim=MagicMock(),
        agent=SimpleNamespace(session_id=session_id),
        _agent_running=False,
        _voice_recorder=None,
        _session_db=session_db,
        _delete_session_on_exit=False,
        _persist_active_session_before_close=MagicMock(),
        _discard_session_if_empty=MagicMock(),
        _print_exit_summary=MagicMock(),
        _release_active_session=MagicMock(),
    )


def _run_tui_shutdown(tui):
    from hermes_cli.cli_tui_runtime_mixin import CLITuiRuntimeMixin

    with (
        patch("cli.set_sudo_password_callback"),
        patch("cli.set_approval_callback"),
        patch("cli.set_secret_capture_callback"),
        patch("cli._run_cleanup"),
        patch("agent.vault_backends.unlock.lock"),
        patch("agent.vault_backends.unlock.set_code_prompt_callback"),
        patch("agent.vault_backends.unlock.set_save_login_prompt_callback"),
        patch("agent.vault_backends.unlock.set_unlock_prompt_callback"),
        patch("tools.voice_mode.cleanup_temp_recordings"),
    ):
        CLITuiRuntimeMixin._tui_shutdown(tui)


def test_tui_shutdown_stamps_handoff_completed_for_handed_off_session():
    """The teardown that runs after /handoff must not close the gateway-owned row terminally."""
    _reset_cli_globals()
    session_id = "handoff-leg-tui"
    cli_mod._handed_off_session_ids.add(session_id)
    session_db = MagicMock()

    _run_tui_shutdown(_tui(session_id, session_db))

    session_db.end_session.assert_called_once_with(session_id, "handoff_completed")


def test_tui_shutdown_does_not_prune_handed_off_row():
    """A handed-off row is never pruned as an empty CLI session."""
    _reset_cli_globals()
    cli_mod._handed_off_session_ids.add("handoff-leg-prune")
    tui = _tui("handoff-leg-prune", MagicMock())

    _run_tui_shutdown(tui)

    tui._discard_session_if_empty.assert_not_called()


def test_tui_shutdown_stamps_cli_close_for_normal_session():
    """Control: an ordinary interactive exit keeps the explicit close and the empty-row prune."""
    _reset_cli_globals()
    tui = _tui("normal-session-tui", MagicMock())

    _run_tui_shutdown(tui)

    tui._session_db.end_session.assert_called_once_with("normal-session-tui", "cli_close")
    tui._discard_session_if_empty.assert_called_once_with("normal-session-tui")


# ── reader half: recovery accepts the new reason ────────────────────────────

@pytest.fixture
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    try:
        d.close()
    except Exception:
        pass


def _mk(db, session_id, *, end_reason=None, ended_at=None, handoff_state=None, msgs=3):
    db.create_session(
        session_id, PEER["source"], user_id=PEER["user_id"],
        session_key=PEER["session_key"], chat_id=PEER["chat_id"], chat_type=PEER["chat_type"],
    )
    for i in range(msgs):
        db.append_message(session_id, "user" if i % 2 == 0 else "assistant", f"m{i}")
    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET handoff_state=?, last_activity_at=? WHERE id=?",
            (handoff_state, time.time(), session_id),
        )
        if end_reason is not None:
            db._conn.execute(
                "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
                (ended_at, end_reason, session_id),
            )
        db._conn.commit()
    return session_id


def test_handoff_completed_reason_is_recoverable(db):
    """The stamp the teardown now writes resolves to its durable row."""
    _mk(db, "handoff-leg", end_reason="handoff_completed", ended_at=1000.0, handoff_state="completed")
    assert db.find_latest_gateway_session_for_peer(**PEER)["id"] == "handoff-leg"


def test_recoverable_set_matches_the_finder(db):
    """The published set and the recovery SQL must not drift apart."""
    assert "handoff_completed" in SessionDB.RECOVERABLE_END_REASONS


def test_plain_cli_close_still_not_recoverable(db):
    """Control: an ordinary CLI exit is still not auto-revived."""
    _mk(db, "plain-cli", end_reason="cli_close", ended_at=1000.0)
    assert db.find_latest_gateway_session_for_peer(**PEER) is None
