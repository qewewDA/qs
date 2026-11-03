"""A missing order field means empty only within an unambiguous full fleet."""
import pytest

from stellaris_autopilot.pdx import loads
from stellaris_autopilot.saves import _fleets


@pytest.mark.parametrize("orders", ["", "actions={} current_order={} order={}"])
def test_complete_owned_fleet_can_establish_empty_order_fields(orders):
    fleets, known = _fleets(loads('fleet={1={owner=0 ' + orders + '}}'), "0")
    assert known and fleets[0]["orders_known"] is True


@pytest.mark.parametrize("field", ["actions", "current_order", "order", "orders", "queued_orders"])
def test_repeated_order_container_stays_unknown_even_when_each_is_empty(field):
    fleets, known = _fleets(loads('fleet={1={owner=0 ' + field + '={} ' + field + '={}}}'), "0")
    assert known and fleets[0]["orders_known"] is False


@pytest.mark.parametrize("field", ["orders", "queued_orders"])
def test_unexpected_order_container_is_preserved_for_stop_rejection(field):
    fleets, _ = _fleets(loads('fleet={1={owner=0 ' + field + '={unrecognized=1}}}'), "0")
    assert fleets[0][field] == {"unrecognized": 1}
