"""SQLite storage for requests and their journal of events.

Every status change goes through `Tracker.apply`, which checks it against the state
machine and journals it in `events`, in the same transaction. The database holds no
profile data: requests reference brokers by slug. It is still created with mode 600,
since notes and broker answers may mention the user.
"""

import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from gomme.brokers.schema import Broker
from gomme.legal.deadlines import Deadline, deadline_for, french_public_holidays
from gomme.legal.statutes import Jurisdiction
from gomme.privacy import PRIVATE_FILE_MODE, ensure_private_dir
from gomme.tracker.state import Action, Status, transition

DB_FILE = "gomme.db"
SCHEMA_VERSION = 2
CREATE = "create"  # journal entry of a new request (not a state-machine action)

_SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY,
    broker_slug TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('email', 'web_form', 'postal')),
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('gdpr', 'ccpa')),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_on TEXT,
    extended INTEGER NOT NULL DEFAULT 0 CHECK (extended IN (0, 1)),
    reference TEXT,
    artifact TEXT,
    received_on TEXT,
    suspended_days INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX requests_broker ON requests (broker_slug);
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES requests (id),
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX events_request ON events (request_id);
"""

# Upgrades from each older version to the next one.
_MIGRATIONS = {
    1: """
ALTER TABLE requests ADD COLUMN received_on TEXT;
ALTER TABLE requests ADD COLUMN suspended_days INTEGER NOT NULL DEFAULT 0;
""",
}

CLOSED = frozenset({Status.COMPLETED, Status.ESCALATED})
"""Statuses where nothing more is expected from the broker, short of a re-check."""


class TrackerError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RequestRecord:
    id: int
    broker_slug: str
    channel: str
    jurisdiction: Jurisdiction
    status: Status
    created_at: datetime
    sent_on: date | None
    extended: bool
    reference: str | None  # e.g. the Message-ID of the email
    artifact: str | None  # path of the .eml, letter or screenshot
    received_on: date | None = None  # when the broker received it, if known (letters)
    suspended_days: int = 0  # days spent waiting for information the broker asked for

    def deadline(self, broker: Broker | None = None) -> Deadline | None:
        """The legal deadline, once the request is sent. French holidays apply to French
        brokers only."""
        if self.sent_on is None:
            return None
        holidays: frozenset[date] = frozenset()
        if broker is not None and broker.country == "FR":
            holidays = french_public_holidays(self.sent_on.year) | french_public_holidays(
                self.sent_on.year + 1
            )
        return deadline_for(
            self.jurisdiction,
            self.received_on or self.sent_on,
            extended=self.extended,
            holidays=holidays,
            suspended_days=self.suspended_days,
        )


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: int
    request_id: int
    at: datetime
    action: str
    from_status: Status | None
    to_status: Status
    note: str


def default_db_path(data_dir: Path) -> Path:
    return data_dir / DB_FILE


class Tracker:
    """Access to the request database. Use as a context manager."""

    def __init__(self, path: Path) -> None:
        ensure_private_dir(path.parent)
        if not path.exists():
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT, PRIVATE_FILE_MODE))
        self._db = sqlite3.connect(path, isolation_level=None)  # transactions are explicit
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._migrate()
        except BaseException:
            self._db.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION or (version and version not in _MIGRATIONS):
            raise TrackerError(
                f"base de version {version} inconnue (attendue : {SCHEMA_VERSION}) : "
                "mettre Gomme à jour"
            )
        # executescript() commits any open transaction first: each script carries its own.
        if version == 0:
            script = _SCHEMA
        else:
            script = "".join(_MIGRATIONS[v] for v in range(version, SCHEMA_VERSION))
        self._db.executescript(
            f"BEGIN IMMEDIATE;\n{script}PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
        )

    # --- Writes ---------------------------------------------------------------------

    def create(
        self, broker_slug: str, channel: str, jurisdiction: Jurisdiction, now: datetime
    ) -> RequestRecord:
        with self._transaction() as db:
            cursor = db.execute(
                "INSERT INTO requests (broker_slug, channel, jurisdiction, status, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (broker_slug, channel, jurisdiction.value, Status.DRAFT.value, now.isoformat()),
            )
            request_id = cursor.lastrowid
            if request_id is None:  # pragma: no cover - an INSERT always sets it
                raise TrackerError("insertion impossible")
            self._journal(db, request_id, now, CREATE, None, Status.DRAFT, "")
        return self.get(request_id)

    def apply(
        self,
        request_id: int,
        action: Action,
        now: datetime,
        *,
        note: str = "",
        reference: str | None = None,
        artifact: str | None = None,
        sent_on: date | None = None,
        received_on: date | None = None,
    ) -> RequestRecord:
        """Apply `action`, checked by the state machine, and journal it.

        For SEND, `sent_on` records a request sent earlier (by hand) and `received_on` the
        day the broker received it (a letter's acknowledgement of receipt): the deadline
        starts on receipt. ACTION_DONE adds the days spent since the broker's last request
        for information to the suspension of the deadline.
        """
        today = now.date()
        if (sent_on or received_on) and action is not Action.SEND:
            raise TrackerError("les dates d'envoi et de réception ne valent que pour send")
        if sent_on is not None and sent_on > today:
            raise TrackerError("la date d'envoi ne peut pas être dans le futur")
        if received_on is not None and not ((sent_on or today) <= received_on <= today):
            raise TrackerError(
                "la date de réception doit suivre l'envoi, sans dépasser aujourd'hui"
            )
        with self._transaction() as db:
            current = self._get(db, request_id)
            target = transition(current.status, action)
            updates: dict[str, object] = {"status": target.value}
            if action is Action.SEND:
                updates["sent_on"] = (sent_on or today).isoformat()
                updates["received_on"] = received_on.isoformat() if received_on else None
            if action is Action.ACTION_DONE:
                asked = self._last_event_day(db, request_id, Action.REQUEST_ACTION)
                if asked is not None and today > asked:
                    updates["suspended_days"] = current.suspended_days + (today - asked).days
            if action is Action.EXTEND:
                updates["extended"] = 1
            if action is Action.QUEUE and current.status is Status.RECHECK_DUE:
                # A new request: the old deadline and extension no longer apply.
                updates |= {
                    "sent_on": None,
                    "received_on": None,
                    "extended": 0,
                    "suspended_days": 0,
                    "reference": None,
                    "artifact": None,
                }
            if reference is not None:
                updates["reference"] = reference
            if artifact is not None:
                updates["artifact"] = artifact
            assignments = ", ".join(f"{column} = ?" for column in updates)
            db.execute(
                f"UPDATE requests SET {assignments} WHERE id = ?",  # noqa: S608 - fixed columns
                (*updates.values(), request_id),
            )
            self._journal(db, request_id, now, action.value, current.status, target, note)
        return self.get(request_id)

    def record_receipt(
        self, request_id: int, received_on: date, now: datetime, broker: Broker | None = None
    ) -> RequestRecord:
        """Record, after sending, the day the broker received the request (a letter's
        acknowledgement of receipt): the deadline starts that day. If the corrected deadline
        has not passed, an overdue request goes back to sent (journaled as RESUME)."""
        today = now.date()
        with self._transaction() as db:
            current = self._get(db, request_id)
            if current.status not in (
                Status.SENT,
                Status.ACKNOWLEDGED,
                Status.NEEDS_ACTION,
                Status.OVERDUE,
            ):
                raise TrackerError(
                    f"demande n° {request_id} : réception à noter pour une demande envoyée "
                    f"(état : {current.status})"
                )
            assert current.sent_on is not None  # noqa: S101 - every status above is sent
            if not current.sent_on <= received_on <= today:
                raise TrackerError("la date de réception doit suivre l'envoi, sans être future")
            db.execute(
                "UPDATE requests SET received_on = ? WHERE id = ?",
                (received_on.isoformat(), request_id),
            )
            note = f"reçue par le broker le {received_on.isoformat()}"
            self._journal(db, request_id, now, "note", current.status, current.status, note)
            updated = self._get(db, request_id)
            deadline = updated.deadline(broker)
            if (
                current.status is Status.OVERDUE
                and deadline is not None
                and not deadline.is_overdue(today)
            ):
                target = transition(current.status, Action.RESUME)
                db.execute(
                    "UPDATE requests SET status = ? WHERE id = ?", (target.value, request_id)
                )
                self._journal(
                    db,
                    request_id,
                    now,
                    Action.RESUME.value,
                    current.status,
                    target,
                    f"nouvelle échéance : {deadline.due.isoformat()}",
                )
        return self.get(request_id)

    def retarget(
        self, request_id: int, channel: str, jurisdiction: Jurisdiction, now: datetime
    ) -> RequestRecord:
        """Change the channel or law of a request not under way (draft or re-check)."""
        with self._transaction() as db:
            current = self._get(db, request_id)
            if current.status not in (Status.DRAFT, Status.RECHECK_DUE):
                raise TrackerError(
                    f"demande n° {request_id} en cours ({current.status}) : canal inchangé"
                )
            if (current.channel, current.jurisdiction) == (channel, jurisdiction):
                return current
            db.execute(
                "UPDATE requests SET channel = ?, jurisdiction = ? WHERE id = ?",
                (channel, jurisdiction.value, request_id),
            )
            note = (
                f"canal {current.channel} → {channel}, "
                f"loi {current.jurisdiction.value} → {jurisdiction.value}"
            )
            self._journal(db, request_id, now, "note", current.status, current.status, note)
        return self.get(request_id)

    def note(self, request_id: int, now: datetime, note: str) -> None:
        """Journal a note without changing the status."""
        with self._transaction() as db:
            current = self._get(db, request_id)
            self._journal(db, request_id, now, "note", current.status, current.status, note)

    @staticmethod
    def _last_event_day(db: sqlite3.Connection, request_id: int, action: Action) -> date | None:
        row = db.execute(
            "SELECT at FROM events WHERE request_id = ? AND action = ? ORDER BY id DESC LIMIT 1",
            (request_id, action.value),
        ).fetchone()
        return datetime.fromisoformat(row["at"]).date() if row else None

    @staticmethod
    def _journal(
        db: sqlite3.Connection,
        request_id: int,
        now: datetime,
        action: str,
        from_status: Status | None,
        to_status: Status,
        note: str,
    ) -> None:
        db.execute(
            "INSERT INTO events (request_id, at, action, from_status, to_status, note)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                request_id,
                now.isoformat(),
                action,
                from_status.value if from_status else None,
                to_status.value,
                note,
            ),
        )

    # --- Reads ----------------------------------------------------------------------

    def get(self, request_id: int) -> RequestRecord:
        return self._get(self._db, request_id)

    def requests(
        self, *, status: Status | None = None, broker_slug: str | None = None
    ) -> tuple[RequestRecord, ...]:
        query, params = "SELECT * FROM requests WHERE 1 = 1", []
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        if broker_slug is not None:
            query += " AND broker_slug = ?"
            params.append(broker_slug)
        rows = self._db.execute(query + " ORDER BY id", params).fetchall()
        return tuple(_request(row) for row in rows)

    def latest_for(self, broker_slug: str) -> RequestRecord | None:
        row = self._db.execute(
            "SELECT * FROM requests WHERE broker_slug = ? ORDER BY id DESC LIMIT 1",
            (broker_slug,),
        ).fetchone()
        return _request(row) if row else None

    def events(self, request_id: int) -> tuple[EventRecord, ...]:
        rows = self._db.execute(
            "SELECT * FROM events WHERE request_id = ? ORDER BY id", (request_id,)
        ).fetchall()
        return tuple(
            EventRecord(
                id=row["id"],
                request_id=row["request_id"],
                at=datetime.fromisoformat(row["at"]),
                action=row["action"],
                from_status=Status(row["from_status"]) if row["from_status"] else None,
                to_status=Status(row["to_status"]),
                note=row["note"],
            )
            for row in rows
        )

    def notes_on(self, day: date, prefixes: Sequence[str]) -> int:
        """Number of events journaled on `day` whose note starts with one of `prefixes`
        (used to count the emails sent through SMTP, for the daily limit)."""
        if not prefixes:
            return 0
        clauses = " OR ".join("note LIKE ? ESCAPE '\\'" for _ in prefixes)
        patterns = [
            prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for prefix in prefixes
        ]
        row = self._db.execute(
            f"SELECT COUNT(*) FROM events WHERE substr(at, 1, 10) = ? AND ({clauses})",  # noqa: S608
            (day.isoformat(), *patterns),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _get(db: sqlite3.Connection, request_id: int) -> RequestRecord:
        if not 0 < request_id < 2**63:  # beyond SQLite integers: cannot exist
            raise TrackerError(f"demande n° {request_id} introuvable")
        row = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise TrackerError(f"demande n° {request_id} introuvable")
        return _request(row)


def _request(row: sqlite3.Row) -> RequestRecord:
    return RequestRecord(
        id=row["id"],
        broker_slug=row["broker_slug"],
        channel=row["channel"],
        jurisdiction=Jurisdiction(row["jurisdiction"]),
        status=Status(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        sent_on=date.fromisoformat(row["sent_on"]) if row["sent_on"] else None,
        extended=bool(row["extended"]),
        reference=row["reference"],
        artifact=row["artifact"],
        received_on=date.fromisoformat(row["received_on"]) if row["received_on"] else None,
        suspended_days=int(row["suspended_days"]),
    )
