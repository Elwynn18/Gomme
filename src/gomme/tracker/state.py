"""Request state machine: every status change is an explicit action.

    draft -> queued -> sent -> (acknowledged | needs_action) -> completed
    branches: refused, overdue, escalated, recheck_due

Nothing changes implicitly: even a missed deadline is an action (MARK_OVERDUE), taken by
`gomme monitor` and journaled like the others. Pure module: storage lives in tracker.db.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class Status(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"  # the broker answered and is handling the request
    NEEDS_ACTION = "needs_action"  # the broker waits for the user (identity, link...)
    COMPLETED = "completed"
    REFUSED = "refused"
    OVERDUE = "overdue"  # legal deadline passed without a final answer
    ESCALATED = "escalated"  # a complaint to the supervisory authority was prepared
    RECHECK_DUE = "recheck_due"  # time to check the data did not come back


class Action(StrEnum):
    QUEUE = "queue"
    UNQUEUE = "unqueue"
    SEND = "send"
    ACKNOWLEDGE = "acknowledge"
    EXTEND = "extend"  # the broker announces an extension of the deadline
    REQUEST_ACTION = "request_action"
    ACTION_DONE = "action_done"
    COMPLETE = "complete"
    REFUSE = "refuse"
    MARK_OVERDUE = "mark_overdue"
    ESCALATE = "escalate"
    RECHECK = "recheck"
    CONFIRM_REMOVED = "confirm_removed"
    RESUME = "resume"  # a corrected receipt date shows the deadline has not passed yet


_PENDING = (Status.SENT, Status.ACKNOWLEDGED)  # waiting for the broker, deadline running

TRANSITIONS: Mapping[tuple[Status, Action], Status] = MappingProxyType(
    {
        (Status.DRAFT, Action.QUEUE): Status.QUEUED,
        (Status.QUEUED, Action.UNQUEUE): Status.DRAFT,
        (Status.QUEUED, Action.SEND): Status.SENT,
        (Status.SENT, Action.ACKNOWLEDGE): Status.ACKNOWLEDGED,
        (Status.OVERDUE, Action.ACKNOWLEDGE): Status.ACKNOWLEDGED,
        **{(status, Action.EXTEND): Status.ACKNOWLEDGED for status in _PENDING},
        **{(status, Action.REQUEST_ACTION): Status.NEEDS_ACTION for status in _PENDING},
        (Status.NEEDS_ACTION, Action.ACTION_DONE): Status.ACKNOWLEDGED,
        **{
            (status, Action.COMPLETE): Status.COMPLETED
            for status in (*_PENDING, Status.NEEDS_ACTION, Status.OVERDUE, Status.ESCALATED)
        },
        **{
            (status, Action.REFUSE): Status.REFUSED
            for status in (*_PENDING, Status.NEEDS_ACTION, Status.OVERDUE)
        },
        # Not from needs_action: while the broker waits for the user, it is not late.
        **{(status, Action.MARK_OVERDUE): Status.OVERDUE for status in _PENDING},
        (Status.OVERDUE, Action.ESCALATE): Status.ESCALATED,
        (Status.REFUSED, Action.ESCALATE): Status.ESCALATED,
        (Status.COMPLETED, Action.RECHECK): Status.RECHECK_DUE,
        (Status.RECHECK_DUE, Action.CONFIRM_REMOVED): Status.COMPLETED,
        (Status.RECHECK_DUE, Action.QUEUE): Status.QUEUED,  # the data came back: ask again
        (Status.OVERDUE, Action.RESUME): Status.SENT,
    }
)


class InvalidTransitionError(ValueError):
    def __init__(self, current: Status, action: Action) -> None:
        self.current, self.action = current, action
        allowed = ", ".join(allowed_actions(current)) or "aucune"
        super().__init__(
            f"action « {action} » impossible depuis l'état « {current} » (possibles : {allowed})"
        )


def transition(current: Status, action: Action) -> Status:
    """The status after `action`, or InvalidTransitionError."""
    try:
        return TRANSITIONS[current, action]
    except KeyError:
        raise InvalidTransitionError(current, action) from None


def allowed_actions(current: Status) -> tuple[Action, ...]:
    return tuple(action for action in Action if (current, action) in TRANSITIONS)
