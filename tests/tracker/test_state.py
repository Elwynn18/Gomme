"""Tests for the request state machine (written before the implementation)."""

import itertools

import pytest

from gomme.tracker.state import (
    TRANSITIONS,
    Action,
    InvalidTransitionError,
    Status,
    allowed_actions,
    transition,
)

# The documented path: draft -> queued -> sent -> (acknowledged | needs_action) -> completed
HAPPY_PATH = [
    (Status.DRAFT, Action.QUEUE, Status.QUEUED),
    (Status.QUEUED, Action.SEND, Status.SENT),
    (Status.SENT, Action.ACKNOWLEDGE, Status.ACKNOWLEDGED),
    (Status.ACKNOWLEDGED, Action.COMPLETE, Status.COMPLETED),
]


@pytest.mark.parametrize(("current", "action", "expected"), HAPPY_PATH)
def test_happy_path(current: Status, action: Action, expected: Status) -> None:
    assert transition(current, action) is expected


@pytest.mark.parametrize(
    ("current", "action", "expected"),
    [
        (Status.QUEUED, Action.UNQUEUE, Status.DRAFT),
        (Status.SENT, Action.REQUEST_ACTION, Status.NEEDS_ACTION),
        (Status.ACKNOWLEDGED, Action.REQUEST_ACTION, Status.NEEDS_ACTION),
        (Status.NEEDS_ACTION, Action.ACTION_DONE, Status.ACKNOWLEDGED),
        (Status.SENT, Action.COMPLETE, Status.COMPLETED),
        (Status.NEEDS_ACTION, Action.COMPLETE, Status.COMPLETED),
        (Status.SENT, Action.EXTEND, Status.ACKNOWLEDGED),
        (Status.ACKNOWLEDGED, Action.EXTEND, Status.ACKNOWLEDGED),
        (Status.SENT, Action.REFUSE, Status.REFUSED),
        (Status.NEEDS_ACTION, Action.REFUSE, Status.REFUSED),
        (Status.SENT, Action.MARK_OVERDUE, Status.OVERDUE),
        (Status.ACKNOWLEDGED, Action.MARK_OVERDUE, Status.OVERDUE),
        (Status.OVERDUE, Action.ACKNOWLEDGE, Status.ACKNOWLEDGED),
        (Status.OVERDUE, Action.COMPLETE, Status.COMPLETED),
        (Status.OVERDUE, Action.REFUSE, Status.REFUSED),
        (Status.OVERDUE, Action.ESCALATE, Status.ESCALATED),
        (Status.REFUSED, Action.ESCALATE, Status.ESCALATED),
        (Status.ESCALATED, Action.COMPLETE, Status.COMPLETED),
        (Status.COMPLETED, Action.RECHECK, Status.RECHECK_DUE),
        (Status.RECHECK_DUE, Action.CONFIRM_REMOVED, Status.COMPLETED),
        (Status.RECHECK_DUE, Action.QUEUE, Status.QUEUED),
    ],
)
def test_branches(current: Status, action: Action, expected: Status) -> None:
    assert transition(current, action) is expected


def test_every_other_pair_is_refused() -> None:
    for current, action in itertools.product(Status, Action):
        if (current, action) in TRANSITIONS:
            continue
        with pytest.raises(InvalidTransitionError):
            transition(current, action)


@pytest.mark.parametrize(
    ("current", "action"),
    [
        (Status.DRAFT, Action.SEND),  # never sent without being queued first
        (Status.NEEDS_ACTION, Action.MARK_OVERDUE),  # the broker waits for the user
        (Status.COMPLETED, Action.ESCALATE),
        (Status.ESCALATED, Action.ESCALATE),
    ],
)
def test_notable_refusals(current: Status, action: Action) -> None:
    with pytest.raises(InvalidTransitionError, match=current.value):
        transition(current, action)


def test_every_status_is_reachable_from_draft() -> None:
    reached = {Status.DRAFT}
    frontier = [Status.DRAFT]
    while frontier:
        current = frontier.pop()
        for action in allowed_actions(current):
            target = transition(current, action)
            if target not in reached:
                reached.add(target)
                frontier.append(target)

    assert reached == set(Status)


def test_allowed_actions() -> None:
    assert allowed_actions(Status.DRAFT) == (Action.QUEUE,)
    assert Action.ESCALATE in allowed_actions(Status.OVERDUE)
