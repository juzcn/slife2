"""The plan: the model's list, the file it lives in, and the shape both share.

The plan is not a turn, so these are the rules that keep it one thing across
three readers — the tool, the prompt marker and the strip: whole-list writes,
four statuses, the caps, and an unreadable file that is an error rather than an
empty plan.
"""

from __future__ import annotations

import pytest

from slife2.libs.turn import plan

pytestmark = pytest.mark.unit


ITEMS = [
    {"text": "read the config", "status": plan.DONE},
    {"text": "write the tool", "status": plan.ACTIVE},
    {"text": "run the tests", "status": plan.PENDING},
]


def test_a_conversation_with_no_plan_reads_as_none() -> None:
    assert plan.read("jack") == []


def test_a_written_plan_round_trips_through_its_file() -> None:
    assert plan.write("jack", "", ITEMS) == ITEMS

    assert plan.read("jack") == ITEMS
    assert plan.plan_path("jack").is_file()


def test_a_subagents_plan_is_its_own() -> None:
    plan.write("jack", "researcher", ITEMS)

    assert plan.read("jack") == []
    assert plan.read("jack", "researcher") == ITEMS


def test_an_empty_write_removes_the_plan_file() -> None:
    """The empty plan and the missing file are one state, and one spelling."""
    plan.write("jack", "", ITEMS)
    assert plan.plan_path("jack").is_file()

    assert plan.write("jack", "", []) == []
    assert not plan.plan_path("jack").exists()
    assert plan.read("jack") == []

    # Clearing a plan that is not there is the same empty plan, and an empty
    # write does not make the directory a read would find nothing in.
    assert plan.write("jill", "", []) == []
    assert not plan.plan_path("jill").exists()


def test_the_four_statuses_are_the_vocabulary() -> None:
    for status in plan.STATUSES:
        assert plan.from_wire([{"text": "one", "status": status}]) == [
            {"text": "one", "status": status}
        ]

    with pytest.raises(plan.PlanError, match="pending"):
        plan.from_wire([{"text": "one", "status": "in-progress"}])


def test_an_item_is_one_line_of_text() -> None:
    with pytest.raises(plan.PlanError, match="line break"):
        plan.from_wire([{"text": "one\ntwo", "status": plan.PENDING}])

    with pytest.raises(plan.PlanError, match="no `text`"):
        plan.from_wire([{"text": "   ", "status": plan.PENDING}])


def test_an_unknown_field_is_refused_rather_than_dropped() -> None:
    with pytest.raises(plan.PlanError, match="'id'"):
        plan.from_wire([{"text": "one", "status": plan.PENDING, "id": "a"}])


def test_the_caps_are_refused_before_anything_is_written() -> None:
    over = [{"text": f"item {index}", "status": plan.PENDING} for index in range(25)]
    with pytest.raises(plan.PlanError, match="over the limit"):
        plan.from_wire(over)

    long = [{"text": "x" * (plan.MAX_TEXT + 1), "status": plan.PENDING}]
    with pytest.raises(plan.PlanError, match="limit is"):
        plan.from_wire(long)


def test_a_refused_write_leaves_the_previous_plan_standing() -> None:
    plan.write("jack", "", ITEMS)

    with pytest.raises(plan.PlanError):
        plan.write("jack", "", [{"text": "one", "status": "doing"}])

    assert plan.read("jack") == ITEMS


def test_an_unreadable_file_is_not_an_empty_plan() -> None:
    path = plan.plan_path("jack")
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text("not: [", encoding="utf-8")
    with pytest.raises(plan.PlanError, match="not a plan file"):
        plan.read("jack")

    # A document without `items` is a different file, not a plan with none.
    path.write_text("other: 1\n", encoding="utf-8")
    with pytest.raises(plan.PlanError, match="no `items`"):
        plan.read("jack")
