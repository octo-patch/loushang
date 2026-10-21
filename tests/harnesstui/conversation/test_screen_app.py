from __future__ import annotations

import asyncio
import threading
import time

import pytest

from loushang.harnesstui.conversation.screen_app import ScreenConversationApp
from loushang.harnesstui.conversation.screen_frame import (
    ScreenFrameCopy,
    ScreenFramePresentation,
)
from loushang.tui import RenderBaselineReset, RenderConstraints, strip_control_sequences
from loushang.tui.transcript import (
    AssistantMessageRecord,
    ContextCompactionRecord,
    ToolExecutionRecord,
    UserPromptRecord,
    WorkedDividerRecord,
)
from loushang.tui.ui_parts.transcript import TranscriptRegion


class _TestScreenConversationApp(ScreenConversationApp):
    def _create_frame_presentation(self) -> ScreenFramePresentation:
        return ScreenFramePresentation(
            ScreenFrameCopy(
                working_label="Running",
                steer_label="Steers",
                steer_hint="interrupt",
                followup_label="Follow-ups",
                followup_hint="edit",
            )
        )


def _app() -> _TestScreenConversationApp:
    return _TestScreenConversationApp(
        model_label="model",
        cwd="/workspace",
        branch="main",
        session_label="session",
        now=lambda: 2.0,
    )


@pytest.mark.tui_render_contract
@pytest.mark.parametrize("statusline_visible", [False, True])
@pytest.mark.parametrize("width", [30, 80])
def test_stopping_acknowledgement_survives_crowded_or_hidden_statusline(
    statusline_visible: bool, width: int
) -> None:
    app = _app()
    app.state.model_label = "long-model-name-" * 20
    app.state.cwd = "/workspace/" + "long-project-name-" * 20
    app.state.statusline_visible = statusline_visible
    constraints = RenderConstraints(width=width, max_height=24, visible_height=24)

    def visible() -> str:
        return strip_control_sequences(
            "\n".join(line.text for line in app.render(constraints).lines)
        )

    app.start_prompt("run", started_at=1.0)
    app.begin_stopping()
    assert "Stopping…" in visible()
    assert "1.00s" in visible()
    app.complete_run()
    assert "Stopping…" not in visible()
    app.start_prompt("next", started_at=1.0)
    assert "Running" in visible() and "Stopping…" not in visible()

    app.begin_stopping()
    app.state.abort(message="Interrupted", elapsed_seconds=1.0)
    app.begin_run(started_at=1.0)
    assert "Running" in visible() and "Stopping…" not in visible()


@pytest.mark.tui_render_contract
def test_screen_conversation_app_coordinates_neutral_streaming_state() -> None:
    app = _app()
    app.start_prompt("hello", started_at=1.0)
    app.begin_assistant()
    app.append_assistant_chunk("world")

    result = app.render(RenderConstraints(width=60, max_height=24, visible_height=24))

    assert type(app._transcript_region) is TranscriptRegion
    assert app._transcript_region.records is app.state.records
    assert tuple(line.text for line in result.lines)[:3] == (
        "> hello",
        "",
        "* world",
    )


@pytest.mark.tui_render_contract
def test_screen_conversation_app_keeps_presentation_and_region_instances() -> None:
    app = _app()
    presentation = app._transcript_presentation
    region = app._transcript_region
    app.add_status("ready")

    app.render(RenderConstraints(width=60, max_height=24, visible_height=24))
    app.render(RenderConstraints(width=60, max_height=24, visible_height=24))

    assert app._transcript_presentation is presentation
    assert app._transcript_region is region


@pytest.mark.tui_render_contract
@pytest.mark.parametrize("start_method", ("prompt", "begin_run"))
def test_active_frames_defer_context_usage_rebuild_until_run_completes(
    start_method: str,
) -> None:
    app = _app()
    calls: list[int] = []

    def context_usage() -> int:
        calls.append(app.state.records_revision)
        return len(calls)

    app.context_usage_provider = context_usage
    constraints = RenderConstraints(width=60, max_height=24, visible_height=24)
    app.render(constraints)
    assert calls == [0]

    if start_method == "prompt":
        app.start_prompt("run", started_at=1.0)
    else:
        app.begin_run(started_at=1.0)
    app.render(constraints)
    app.state.upsert_tool_record(
        "tool-1", ToolExecutionRecord(name="bash", state="running", elapsed_seconds=0.0)
    )
    app.render(constraints)

    assert calls == [0]
    assert app.state.context_usage == 1

    app.complete_run(elapsed_seconds=1.0)
    app.render(constraints)

    assert calls == [0, app.state.records_revision]
    assert app.state.context_usage == 2


def test_context_usage_refreshes_after_run_without_display_changes() -> None:
    app = _app()
    app.state.records.append(WorkedDividerRecord(1.0))
    app.state.mark_records_changed()
    calls = 0

    def context_usage() -> int:
        nonlocal calls
        calls += 1
        return calls

    app.context_usage_provider = context_usage
    app.statusline_preview_snapshot()
    revision = app.state.records_revision

    app.begin_run(started_at=1.0)
    app.statusline_preview_snapshot()
    app.complete_run(elapsed_seconds=1.0)
    app.statusline_preview_snapshot()

    assert app.state.records_revision == revision
    assert calls == 2


def test_worked_frame_does_not_wait_for_context_usage_rebuild() -> None:
    async def scenario() -> None:
        app = _app()
        started = threading.Event()
        release = threading.Event()

        def slow_context_usage() -> int:
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("context usage worker did not finish")
            return 42

        app.context_usage_provider = slow_context_usage
        constraints = RenderConstraints(width=60, max_height=24, visible_height=24)
        app.begin_run(started_at=1.0)
        app.complete_run(elapsed_seconds=1.0)
        try:
            frame = app.render(constraints)
            assert any("Worked for" in line.text for line in frame.lines)
            assert app.state.context_usage is None
            assert await asyncio.to_thread(started.wait, 1)
        finally:
            release.set()
        task = app._context_usage_refresh_task
        assert task is not None
        await asyncio.wait_for(task, timeout=2)
        assert app.state.context_usage == 42

    asyncio.run(scenario())


def test_context_usage_from_previous_run_cannot_replace_newer_snapshot() -> None:
    async def scenario() -> None:
        app = _app()
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def context_usage() -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("context usage worker did not finish")
            return calls

        app.context_usage_provider = context_usage
        constraints = RenderConstraints(width=60, max_height=24, visible_height=24)
        app.render(constraints)
        try:
            assert await asyncio.to_thread(started.wait, 1)
            app.start_prompt("new run", started_at=1.0)
            app.complete_run(elapsed_seconds=1.0)
        finally:
            release.set()
        first_task = app._context_usage_refresh_task
        assert first_task is not None
        await asyncio.wait_for(first_task, timeout=2)
        assert app.state.context_usage is None
        app.render(constraints)
        second_task = app._context_usage_refresh_task
        assert second_task is not None
        await asyncio.wait_for(second_task, timeout=2)
        assert app.state.context_usage == 2

    asyncio.run(scenario())


def test_cancel_context_refresh_keeps_exited_screen_unmodified() -> None:
    async def scenario() -> None:
        app = _app()
        started = threading.Event()
        release = threading.Event()

        def context_usage() -> int:
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("context usage worker did not finish")
            return 42

        app.context_usage_provider = context_usage
        app.render(RenderConstraints(width=60, max_height=24, visible_height=24))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            task = app._context_usage_refresh_task
            assert task is not None
            app.cancel_context_usage_refresh()
            assert app._context_usage_refresh_task is None
        finally:
            release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert app.state.context_usage is None

    asyncio.run(scenario())


def test_cancelled_context_refresh_does_not_delay_event_loop_shutdown() -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_context_usage() -> int:
        started.set()
        release.wait(timeout=5)
        return 42

    async def scenario() -> None:
        app = _app()
        app.context_usage_provider = slow_context_usage
        app.render(RenderConstraints(width=60, max_height=24, visible_height=24))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        app.cancel_context_usage_refresh()

    start = time.perf_counter()
    try:
        asyncio.run(scenario())
        assert time.perf_counter() - start < 1.0
    finally:
        release.set()


def test_screen_conversation_app_reports_window_replacement_reason_once() -> None:
    app = _app()

    app.replace_transcript_window((), reason="test")

    assert app.consume_render_baseline_reset_reason() == (
        "transcript_window_replaced:test"
    )
    assert app.consume_render_baseline_reset_reason() is None


def test_screen_conversation_app_atomically_installs_bounded_resumed_history() -> None:
    app = _app()
    app.active_transcript_line_budget = 2

    app.install_resumed_history(
        (
            UserPromptRecord("old"),
            AssistantMessageRecord("new"),
        )
    )

    assert app.state.records == [AssistantMessageRecord("new")]
    assert app.state.evicted_prefix_record_count == 1
    assert app.consume_render_baseline_reset_reason() == RenderBaselineReset(
        reason="transcript_window_replaced:resume",
        replay_hidden_prefix=True,
    )
    assert app.consume_render_baseline_reset_reason() is None


def test_screen_conversation_app_owns_compaction_window_mechanics() -> None:
    app = _app()
    app.state.records.extend(
        [
            UserPromptRecord("old"),
            AssistantMessageRecord("middle"),
            UserPromptRecord("new"),
        ]
    )

    app.compact_transcript_window(summary=" condensed ", max_records=2)

    assert app.state.records == [
        AssistantMessageRecord("condensed"),
        UserPromptRecord("new"),
    ]
    assert app.state.evicted_prefix_record_count == 2
    assert app.consume_render_baseline_reset_reason() == (
        "transcript_window_replaced:compaction"
    )


def test_screen_conversation_app_appends_compaction_fact_without_trimming_records() -> (
    None
):
    app = _app()
    app.state.records.extend(UserPromptRecord(str(index)) for index in range(3))

    app.append_context_compaction_record(
        summary="condensed",
        tokens_before=42,
    )

    assert app.state.records == [
        UserPromptRecord("0"),
        UserPromptRecord("1"),
        UserPromptRecord("2"),
        ContextCompactionRecord(summary="condensed", tokens_before=42),
    ]
    assert app.state.evicted_prefix_record_count == 0
    assert app.consume_render_baseline_reset_reason() is None


def test_screen_conversation_app_applies_active_logical_line_budget() -> None:
    app = _app()
    app.active_transcript_line_budget = 2
    app.state.records.extend(
        [
            UserPromptRecord("old"),
            AssistantMessageRecord("new"),
        ]
    )

    app.trim_active_transcript_window()

    assert app.state.records == [AssistantMessageRecord("new")]
    assert app.state.evicted_prefix_record_count == 1
    assert app.consume_render_baseline_reset_reason() == (
        "transcript_window_trimmed:active_line_budget"
    )
