"""Legal frameworks a request can rely on, and the citations shown in requests.

Sources:
- GDPR: Regulation (EU) 2016/679, https://eur-lex.europa.eu/eli/reg/2016/679/oj
- CCPA: Cal. Civ. Code §1798.100 et seq., https://cppa.ca.gov/regulations/
- CCPA regulations: 11 CCR §7021 (response timing),
  https://www.law.cornell.edu/regulations/california/11-CCR-7021
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from gomme.brokers.schema import LegalBasis


class Jurisdiction(StrEnum):
    GDPR = "gdpr"
    CCPA = "ccpa"


@dataclass(frozen=True, slots=True)
class Statute:
    basis: LegalBasis
    jurisdiction: Jurisdiction
    citation: str  # as written in a request
    right_fr: str
    right_en: str


STATUTES: dict[LegalBasis, Statute] = {
    LegalBasis.GDPR_ART15: Statute(
        LegalBasis.GDPR_ART15,
        Jurisdiction.GDPR,
        "article 15 du RGPD",
        "droit d'accès, y compris à toute information sur la source des données",
        "right of access, including any available information as to the source of the data",
    ),
    LegalBasis.GDPR_ART17: Statute(
        LegalBasis.GDPR_ART17,
        Jurisdiction.GDPR,
        "article 17 du RGPD",
        "droit à l'effacement",
        "right to erasure",
    ),
    LegalBasis.GDPR_ART19: Statute(
        LegalBasis.GDPR_ART19,
        Jurisdiction.GDPR,
        "article 19 du RGPD",
        "notification de l'effacement à chaque destinataire des données",
        "notification of the erasure to each recipient of the data",
    ),
    LegalBasis.GDPR_ART21: Statute(
        LegalBasis.GDPR_ART21,
        Jurisdiction.GDPR,
        "article 21 du RGPD",
        "droit d'opposition, absolu en matière de prospection (art. 21.2 et 21.3)",
        "right to object, absolute for direct marketing (art. 21(2) and 21(3))",
    ),
    LegalBasis.CCPA_1798_105: Statute(
        LegalBasis.CCPA_1798_105,
        Jurisdiction.CCPA,
        "Cal. Civ. Code §1798.105",
        "droit à la suppression (CCPA)",
        "right to delete (CCPA)",
    ),
}


def jurisdictions(bases: Iterable[LegalBasis]) -> tuple[Jurisdiction, ...]:
    """Jurisdictions covered by `bases`, in order of first appearance."""
    found: list[Jurisdiction] = []
    for basis in bases:
        jurisdiction = STATUTES[basis].jurisdiction
        if jurisdiction not in found:
            found.append(jurisdiction)
    return tuple(found)
