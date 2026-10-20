"""User-requested sixteen-minute scenarios using real scheduling and isolated PG."""

from datetime import timedelta

import pytest
from test_attention import add_message, decision, raw_message, row
from test_attention import system as system


@pytest.mark.integration
@pytest.mark.parametrize(
    "scenario,expected_status,expected_phone_count",
    [
        ("important_ready", "phone_accepted", 1),
        ("important_permission_missing", "blocked_permission", 0),
        ("ordinary_mention", "normal", 0),
        ("already_read", "responded", 0),
        ("already_replied", "responded", 0),
        ("manually_handled", "acknowledged", 0),
    ],
)
def test_sixteen_minutes_without_handling(
    system, scenario, expected_status, expected_phone_count,
):
    if scenario == "ordinary_mention":
        system.model.result = decision(importance="normal", requires_response=False)
    add_message(system)
    created = system.clock.now
    system.service.tick()
    assert not system.feishu.sent and not system.feishu.calls

    system.clock.now = created + timedelta(minutes=14, seconds=59)
    assert system.service.tick() is None
    assert not system.feishu.sent and not system.feishu.calls

    if scenario == "important_permission_missing":
        system.feishu.ready = False
    elif scenario == "already_read":
        system.feishu.read = True
    elif scenario == "already_replied":
        reply = raw_message(system.clock.now, "om_owner_reply", sender={
            "id": "ou_owner", "id_type": "open_id", "sender_type": "user",
        })
        system.feishu.tail = {"items": [reply], "has_more": False}
    elif scenario == "manually_handled":
        system.service.acknowledge("om_source")

    system.clock.now = created + timedelta(minutes=16)
    system.service.tick()
    assert row(system).status == expected_status
    assert len(system.feishu.sent) == expected_phone_count
    assert len(system.feishu.calls) == expected_phone_count

    # More scheduling cycles do not generate another reminder or call.
    for _ in range(3):
        system.clock.now += timedelta(seconds=5)
        system.service.tick()
    assert len(system.feishu.sent) == len(system.feishu.calls) == expected_phone_count
    print({
        "scenario": scenario, "elapsed_minutes": 16,
        "status": row(system).status, "simulated_reminders": len(system.feishu.sent),
        "simulated_phone_requests": len(system.feishu.calls),
    })
