"""The SDK retires processes and calendars (CP-ADR-0074, amendment Zh4; S012)."""

from collections.abc import Callable

import httpx
import pytest

from control_plane_client import ControlPlaneClient
from control_plane_client.errors import ConflictError
from tests.helpers import auth
from tests.integration.test_calendars import DOCUMENT as CALENDAR
from tests.integration.test_process_instances import GOAL, _publish, _setup


async def test_retire_a_process_and_a_calendar(
    client: httpx.AsyncClient, sdk: Callable[[str], ControlPlaneClient]
) -> None:
    key = (await _setup(client))["key"]
    published = await client.post(
        "/api/v1/calendars", json={"key": "ru", "spec": CALENDAR["spec"]}, headers=auth(key)
    )
    assert published.status_code == 201, published.text
    await _publish(client, key, "sample-goal", {**GOAL, "calendar": "ru"})
    async with sdk(key) as admin:
        with pytest.raises(ConflictError) as in_use:
            await admin.retire_calendar("ru", "old year")
        assert in_use.value.code == "calendar_in_use"

        dry = await admin.retire_process_definition("sample-goal", "replaced", dry_run=True)
        assert (dry["status"], dry["openInstances"], dry["byVersion"]) == ("retired", 0, [])
        assert (await admin.get_process_definition("sample-goal"))["status"] == "active"
        retired = await admin.retire_process_definition("sample-goal", "replaced")
        assert retired["retired"]["reason"] == "replaced"
        assert (await admin.get_process_definition("sample-goal"))["status"] == "retired"

        assert (await admin.retire_calendar("ru", "old year", dry_run=True))["status"] == "retired"
        calendar = await admin.retire_calendar("ru", "old year")
        assert (calendar["key"], calendar["retired"]["reason"]) == ("ru", "old year")
