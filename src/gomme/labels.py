"""French labels of request statuses and actions, shared by the CLI and the web UI."""

from gomme.brokers.fields import ProfileField
from gomme.brokers.schema import Category, Region
from gomme.legal.statutes import Jurisdiction
from gomme.tracker.state import Action, Status

# Actions the user records by hand; the others are taken by `monitor` and `escalate`.
MANUAL_ACTIONS = (
    Action.SEND,
    Action.ACKNOWLEDGE,
    Action.EXTEND,
    Action.REQUEST_ACTION,
    Action.ACTION_DONE,
    Action.COMPLETE,
    Action.REFUSE,
    Action.CONFIRM_REMOVED,
    Action.UNQUEUE,
)

STATUS_LABELS = {
    Status.DRAFT: "brouillon",
    Status.QUEUED: "en attente d'envoi",
    Status.SENT: "envoyée",
    Status.ACKNOWLEDGED: "prise en compte",
    Status.NEEDS_ACTION: "action requise",
    Status.COMPLETED: "terminée",
    Status.REFUSED: "refusée",
    Status.OVERDUE: "en retard",
    Status.ESCALATED: "plainte préparée",
    Status.RECHECK_DUE: "à re-vérifier",
}

ACTION_LABELS = {
    Action.QUEUE: "mettre en file",
    Action.UNQUEUE: "retirer de la file",
    Action.SEND: "envoyée",
    Action.ACKNOWLEDGE: "accusé de réception reçu",
    Action.EXTEND: "délai prolongé par le broker",
    Action.REQUEST_ACTION: "le broker demande une information",
    Action.ACTION_DONE: "information fournie",
    Action.COMPLETE: "suppression confirmée",
    Action.REFUSE: "refus du broker",
    Action.MARK_OVERDUE: "en retard",
    Action.ESCALATE: "plainte préparée",
    Action.RECHECK: "à re-vérifier",
    Action.CONFIRM_REMOVED: "toujours supprimé",
    Action.RESUME: "délai corrigé, pas encore dépassé",
}

# --- plain words for the web interface ------------------------------------------------

CHANNEL_LABELS = {
    "email": "email",
    "web_form": "formulaire en ligne",
    "postal": "courrier",
}

JURISDICTION_LABELS = {
    Jurisdiction.GDPR: "RGPD (Europe)",
    Jurisdiction.CCPA: "CCPA (Californie)",
}

FIELD_LABELS = {
    ProfileField.FULL_NAME: "nom",
    ProfileField.EMAIL: "email",
    ProfileField.POSTAL_ADDRESS: "adresse postale",
    ProfileField.BIRTH_DATE: "date de naissance",
    ProfileField.PHONE: "téléphone",
}

CATEGORY_LABELS = {
    Category.MARKETING: "fichiers marketing",
    Category.PEOPLE_SEARCH: "recherche de personnes",
    Category.ANNUAIRE: "annuaire",
    Category.BACKGROUND_CHECK: "vérification d'antécédents",
    Category.AUTRE: "autre",
}

REGION_LABELS = {
    Region.FR: "France",
    Region.EU: "Europe",
    Region.US: "États-Unis",
    Region.GLOBAL: "international",
}

# Buttons of the web interface, written from the user's point of view.
ACTION_BUTTONS = {
    Action.SEND: "J'ai envoyé la demande",
    Action.ACKNOWLEDGE: "Le broker a accusé réception",
    Action.EXTEND: "Le broker prolonge le délai",
    Action.REQUEST_ACTION: "Le broker me demande une information",
    Action.ACTION_DONE: "J'ai fourni l'information",
    Action.COMPLETE: "Le broker confirme la suppression",
    Action.REFUSE: "Le broker refuse",
    Action.CONFIRM_REMOVED: "Mes données n'ont pas réapparu",
    Action.UNQUEUE: "Ne pas envoyer cette demande",
}
