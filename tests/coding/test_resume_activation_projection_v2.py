from dataclasses import replace
from datetime import UTC, datetime

import pytest

from loushang.ai.types import UserMessage
from loushang.coding.resume_activation_attempt_v2 import (
    prepare_coding_activation_attempt_v2_record,
)
from loushang.coding.resume_activation_projection_v2 import (
    project_coding_activation_attempt_path_v2,
)
from loushang.coding.resume_binding import (
    project_coding_runtime_binding_path,
)
from loushang.coding.resume_binding_v2 import prepare_coding_resume_v2_user_record
from loushang.harness.transcript.types import ExtensionData
from tests.coding.test_resume_activation_attempt_v2 import _intent
from tests.coding.test_resume_binding_v2 import _binding


def _path(session_id="session-1"):
    ancestry = project_coding_runtime_binding_path(
        (), current_session_id=session_id
    ).ancestry_digest
    intent = _intent(session_id=session_id, ancestry_digest=ancestry)
    intent_record = prepare_coding_activation_attempt_v2_record(
        intent, created_at=datetime(2026, 10, 13, tzinfo=UTC)
    )
    message = UserMessage(role="user", content="continue", timestamp=1.0)
    binding = _binding(message, intent)
    message_record = prepare_coding_resume_v2_user_record(
        binding, message, intent=intent, created_at=datetime(2026, 10, 13, tzinfo=UTC)
    )
    return intent_record, message_record


def _project(records):
    return project_coding_activation_attempt_path_v2(
        records, current_session_id="session-1"
    )


def test_v2_exact_selected_path_moves_from_pending_to_historical_correspondence():
    intent, message = _path()
    pending = _project((intent,))
    assert pending.status == "pending"
    assert pending.record_id == intent.record_id
    assert _project((intent, message)).status == "none"


def test_foreign_session_intent_is_historical_and_not_current_pending():
    intent, message = _path("origin-session")
    assert _project((intent,)).status == "none"
    assert _project((intent, message)).status == "none"


def test_v1_binding_cannot_close_v2_intent():
    intent, message = _path()
    raw = dict(message.metadata["coding.runtimeBindingV2"])
    raw.pop("intentDigest")
    raw["version"] = 1
    message = replace(message, metadata={"coding.runtimeBindingV1": raw})
    with pytest.raises(ValueError, match="v1 cannot close a v2"):
        _project((intent, message))


@pytest.mark.parametrize(
    "fault",
    ["missing_intent", "wrong_digest", "wrong_parent", "duplicate", "wrong_ancestry"],
)
def test_v2_projection_rejects_missing_or_conflicting_selected_path_facts(fault):
    intent, message = _path()
    records = (intent, message)
    if fault == "missing_intent":
        records = (replace(message, parent_id=None),)
    elif fault == "wrong_digest":
        raw = dict(message.metadata["coding.runtimeBindingV2"])
        raw["intentDigest"] = "e" * 64
        records = (intent, replace(message, metadata={"coding.runtimeBindingV2": raw}))
    elif fault == "wrong_parent":
        records = (intent, replace(message, parent_id="another"))
    elif fault == "duplicate":
        records = (intent, replace(message, record_id=intent.record_id))
    else:
        raw = dict(intent.payload.data)
        raw["ancestryDigest"] = "e" * 64
        records = (
            replace(intent, payload=ExtensionData(intent.payload.extension_type, raw)),
            message,
        )
    with pytest.raises(ValueError):
        _project(records)


@pytest.mark.parametrize(
    "fault", ["future_key", "future_version", "mixed_keys", "future_intent"]
)
def test_future_wire_stays_unknown_even_with_other_known_facts(fault):
    intent, message = _path()
    if fault == "future_intent":
        intent = replace(
            intent, payload=ExtensionData("coding.activation-attempt/v3", {})
        )
        assert _project((intent,)).status == "unknown_version"
        return
    raw = dict(message.metadata["coding.runtimeBindingV2"])
    if fault == "future_key":
        metadata = {"coding.runtimeBindingV3": raw}
    elif fault == "future_version":
        raw["version"] = 3
        metadata = {"coding.runtimeBindingV2": raw}
    else:
        metadata = {"coding.runtimeBindingV2": raw, "coding.runtimeBindingV1": raw}
    assert (
        _project((intent, replace(message, metadata=metadata))).status
        == "unknown_version"
    )


def test_bool_version_is_malformed_not_a_future_schema():
    intent, message = _path()
    raw = dict(message.metadata["coding.runtimeBindingV2"])
    raw["version"] = True
    with pytest.raises(ValueError):
        _project((intent, replace(message, metadata={"coding.runtimeBindingV2": raw})))
