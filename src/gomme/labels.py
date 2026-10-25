"""French labels of request statuses and actions, shared by the CLI and the web UI."""

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
