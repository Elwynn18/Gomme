"""Tests for legal response deadlines (written before the implementation)."""

from datetime import date

import pytest

from gomme.legal.deadlines import (
    Deadline,
    add_months,
    ccpa_deadline,
    deadline_for,
    french_public_holidays,
    gdpr_deadline,
    next_working_day,
)
from gomme.legal.statutes import Jurisdiction

# --- Calendar months (Regulation 1182/71, as applied by EDPB Guidelines 01/2022) ---


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 3, 5), 1, date(2026, 4, 5)),  # EDPB example 32
        (date(2026, 8, 31), 1, date(2026, 9, 30)),  # EDPB example 33: no 31 September
        (date(2026, 1, 31), 1, date(2026, 2, 28)),
        (date(2028, 1, 31), 1, date(2028, 2, 29)),  # leap year
        (date(2026, 12, 15), 1, date(2027, 1, 15)),
        (date(2026, 11, 30), 3, date(2027, 2, 28)),
        (date(2026, 10, 25), 0, date(2026, 10, 25)),
    ],
)
def test_add_months(start: date, months: int, expected: date) -> None:
    assert add_months(start, months) == expected


def test_add_months_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="months"):
        add_months(date(2026, 1, 1), -1)


# --- Working days ----------------------------------------------------------------


def test_french_public_holidays_2026() -> None:
    # Code du travail, art. L3133-1. Easter 2026 is on 5 April.
    assert french_public_holidays(2026) == {
        date(2026, 1, 1),
        date(2026, 4, 6),  # lundi de Pâques
        date(2026, 5, 1),
        date(2026, 5, 8),
        date(2026, 5, 14),  # Ascension
        date(2026, 5, 25),  # lundi de Pentecôte
        date(2026, 7, 14),
        date(2026, 8, 15),
        date(2026, 11, 1),
        date(2026, 11, 11),
        date(2026, 12, 25),
    }


@pytest.mark.parametrize(
    ("year", "easter_monday"),
    [(2027, date(2027, 3, 29)), (2028, date(2028, 4, 17)), (2030, date(2030, 4, 22))],
)
def test_easter_monday_moves_every_year(year: int, easter_monday: date) -> None:
    assert easter_monday in french_public_holidays(year)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 23), date(2026, 10, 23)),  # Friday stays
        (date(2026, 10, 24), date(2026, 10, 26)),  # Saturday -> Monday
        (date(2026, 10, 25), date(2026, 10, 26)),  # Sunday -> Monday
    ],
)
def test_next_working_day_skips_weekends(day: date, expected: date) -> None:
    assert next_working_day(day) == expected


def test_next_working_day_skips_holidays() -> None:
    holidays = french_public_holidays(2026)

    # Wednesday 11 November is a holiday -> Thursday.
    assert next_working_day(date(2026, 11, 11), holidays) == date(2026, 11, 12)
    # Sunday 24 May, then Whit Monday 25 May -> Tuesday 26 May.
    assert next_working_day(date(2026, 5, 24), holidays) == date(2026, 5, 26)


# --- GDPR (art. 12.3) ---------------------------------------------------------------


def test_gdpr_deadline_is_one_calendar_month() -> None:
    deadline = gdpr_deadline(date(2026, 3, 5))

    assert deadline == Deadline(
        received=date(2026, 3, 5),
        due=date(2026, 4, 6),  # 5 April 2026 is a Sunday
        nominal=date(2026, 4, 5),
        extended=False,
    )


def test_gdpr_deadline_is_not_thirty_days() -> None:
    # 1 February + 30 days would be 3 March; one calendar month is 1 March (a Sunday in 2026).
    assert gdpr_deadline(date(2026, 2, 1)).nominal == date(2026, 3, 1)


def test_gdpr_deadline_uses_the_last_day_of_a_shorter_month() -> None:
    assert gdpr_deadline(date(2026, 8, 31)).nominal == date(2026, 9, 30)


def test_gdpr_extension_adds_two_months() -> None:
    deadline = gdpr_deadline(date(2026, 1, 31), extended=True)

    # Counted from receipt (three months), not from the end of the first month
    # (28 February + 2 months = 28 April): the later, safer date for the user.
    assert deadline.nominal == date(2026, 4, 30)
    assert deadline.extended is True


def test_gdpr_deadline_rolls_over_holidays() -> None:
    # 11 October + 1 month = Wednesday 11 November, a French public holiday.
    deadline = gdpr_deadline(date(2026, 10, 11), holidays=french_public_holidays(2026))

    assert deadline.due == date(2026, 11, 12)


# --- CCPA (§1798.130(a)(2), 11 CCR §7021(b)) -------------------------------------


def test_ccpa_deadline_is_45_calendar_days() -> None:
    deadline = ccpa_deadline(date(2026, 10, 1))

    assert deadline.nominal == date(2026, 11, 15)
    assert deadline.due == deadline.nominal  # calendar days: no weekend roll-over


def test_ccpa_extension_allows_90_days_in_total() -> None:
    assert ccpa_deadline(date(2026, 10, 1), extended=True).nominal == date(2026, 12, 30)


# --- Dispatch ------------------------------------------------------------------------


def test_deadline_for_dispatches_on_jurisdiction() -> None:
    received = date(2026, 10, 1)

    assert deadline_for(Jurisdiction.GDPR, received) == gdpr_deadline(received)
    assert deadline_for(Jurisdiction.CCPA, received) == ccpa_deadline(received)


def test_deadline_is_overdue_only_after_the_due_day() -> None:
    deadline = gdpr_deadline(date(2026, 3, 5))  # due 6 April

    assert not deadline.is_overdue(date(2026, 4, 6))
    assert deadline.is_overdue(date(2026, 4, 7))
    assert deadline.days_left(date(2026, 4, 1)) == 5


def test_suspension_pushes_the_gdpr_deadline_back() -> None:
    deadline = gdpr_deadline(date(2026, 3, 5), suspended_days=10)

    assert deadline.nominal == date(2026, 4, 15)
    with pytest.raises(ValueError, match="suspended_days"):
        gdpr_deadline(date(2026, 3, 5), suspended_days=-1)
