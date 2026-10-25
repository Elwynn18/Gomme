"""Legal response deadlines. Pure functions: no clock, no I/O.

GDPR, art. 12.3: one month from receipt, extendable by two further months (the
controller must say so within the first month). The month is a calendar month, computed
under Regulation 1182/71 as the EDPB applies it (Guidelines 01/2022 on the right of
access, v2.0, §160-161 and examples 32-33):
- the period starts on the day of receipt and ends on the same date of the next month;
- without such a date (31 August), it ends on the last day of that month (30 September);
- if that day is a weekend or a public holiday, it ends on the next working day.
https://www.edpb.europa.eu/system/files/2023-04/edpb_guidelines_202201_data_subject_rights_access_v2_en.pdf

CCPA: 45 calendar days from receipt, extendable once by 45 days with notice
(Cal. Civ. Code §1798.130(a)(2); 11 CCR §7021(b): "no later than 45 calendar days",
"will begin on the day that the business receives the request").
https://www.law.cornell.edu/regulations/california/11-CCR-7021
"""

import calendar
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, timedelta

from gomme.legal.statutes import Jurisdiction

GDPR_MONTHS = 1
GDPR_EXTENSION_MONTHS = 2
CCPA_DAYS = 45
CCPA_EXTENSION_DAYS = 45


@dataclass(frozen=True, slots=True)
class Deadline:
    received: date
    due: date  # last day to answer, after any roll-over to a working day
    nominal: date  # before the roll-over
    extended: bool

    def is_overdue(self, today: date) -> bool:
        return today > self.due

    def days_left(self, today: date) -> int:
        return (self.due - today).days


def add_months(start: date, months: int) -> date:
    """Same date `months` later, or the last day of that month if it has no such date."""
    if months < 0:
        raise ValueError("months must not be negative")
    month_index = start.month - 1 + months
    year, month = start.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def easter_sunday(year: int) -> date:
    """Gregorian Easter (anonymous Gregorian algorithm, Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    lw = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * lw) // 433
    month = (h + lw - 7 * m + 90) // 25
    day = (h + lw - 7 * m + 33 * month + 19) % 32
    return date(year, month, day)


def french_public_holidays(year: int) -> frozenset[date]:
    """The eleven public holidays of Code du travail, art. L3133-1 (list unchanged by
    loi n° 2016-1088). TODO(verify): link the Légifrance page of the article.
    Alsace-Moselle's extra days (Good Friday, 26 December) are not included: leaving them
    out can only make a deadline earlier by a day, never hide a late answer.
    TODO(verify): whether a broker outside France can rely on French holidays at all.
    """
    easter = easter_sunday(year)
    return frozenset(
        {
            date(year, 1, 1),
            easter + timedelta(days=1),  # lundi de Pâques
            date(year, 5, 1),
            date(year, 5, 8),
            easter + timedelta(days=39),  # Ascension
            easter + timedelta(days=50),  # lundi de Pentecôte
            date(year, 7, 14),
            date(year, 8, 15),
            date(year, 11, 1),
            date(year, 11, 11),
            date(year, 12, 25),
        }
    )


def next_working_day(day: date, holidays: Collection[date] = frozenset()) -> date:
    """`day` itself if it is a working day, else the first working day after it."""
    while day.weekday() >= calendar.SATURDAY or day in holidays:
        day += timedelta(days=1)
    return day


def gdpr_deadline(
    received: date,
    *,
    extended: bool = False,
    holidays: Collection[date] = frozenset(),
    suspended_days: int = 0,
) -> Deadline:
    """`suspended_days`: while the controller waits for information it asked the user to
    confirm their identity, the period is suspended (EDPB Guidelines 01/2022, §159 and
    example 31). TODO(verify): whether the suspension also requires the controller to
    have asked "without undue delay", as §159 says; Gomme does not check it."""
    if suspended_days < 0:
        raise ValueError("suspended_days must not be negative")
    # The extension is counted from receipt (three months in all) rather than added to
    # the end of the first month: on 31 January the first gives 30 April, the second
    # 28 April. Art. 12.3 does not settle it; the later date never wrongly flags a
    # broker as late. TODO(verify): case law or DPA guidance on this point.
    months = GDPR_MONTHS + (GDPR_EXTENSION_MONTHS if extended else 0)
    nominal = add_months(received, months) + timedelta(days=suspended_days)
    return Deadline(received, next_working_day(nominal, holidays), nominal, extended)


def ccpa_deadline(received: date, *, extended: bool = False) -> Deadline:
    # Counted as receipt + 45 days, the later of the two readings of "45 days beginning on
    # the day of receipt". TODO(verify): whether day 1 is the day of receipt.
    days = CCPA_DAYS + (CCPA_EXTENSION_DAYS if extended else 0)
    nominal = received + timedelta(days=days)
    return Deadline(received, nominal, nominal, extended)


def deadline_for(
    jurisdiction: Jurisdiction,
    received: date,
    *,
    extended: bool = False,
    holidays: Collection[date] = frozenset(),
    suspended_days: int = 0,
) -> Deadline:
    match jurisdiction:
        case Jurisdiction.GDPR:
            return gdpr_deadline(
                received, extended=extended, holidays=holidays, suspended_days=suspended_days
            )
        case Jurisdiction.CCPA:
            # Verification does not pause the CCPA period (11 CCR §7021(b)).
            return ccpa_deadline(received, extended=extended)
