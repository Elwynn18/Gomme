# Gomme

> **Statut : pré-alpha.** Le cycle complet fonctionne (profil, demandes par email ou courrier, suivi des échéances, plainte CNIL), mais rien n'a encore été éprouvé avec de vrais brokers. Relisez chaque demande avant de l'envoyer.

Gomme (nom provisoire) est un outil **gratuit, open source et auto-hébergé** qui envoie des demandes de suppression de données personnelles aux courtiers en données (*data brokers*), suit leurs réponses et relance automatiquement.

C'est l'équivalent personnel de services comme Incogni ou DeleteMe, **sans service central** : chaque personne fait tourner sa propre copie, avec ses propres données, depuis sa propre boîte mail.

La priorité est l'**Europe et la France** (RGPD), mal couvertes par les outils existants. La base compte 32 fiches françaises et européennes (annuaires, données d'entreprises, courtiers marketing, bureaux de crédit), chacune relevée sur la page officielle de l'organisme. Le volet États-Unis (CCPA) reprend les fiches des projets [Eraser](https://github.com/drumandbytes/eraser) et [OptOut](https://github.com/Blake104/OptOut) : 627 brokers américains ou internationaux, sans vérification manuelle de notre part.

## Principes

- **Usage personnel uniquement** : l'outil agit au nom de la personne qui l'exécute, et de personne d'autre.
- **Aucune donnée personnelle ne quitte votre machine**, sauf vers le courtier destinataire et votre propre messagerie (serveur mail, ou Gmail quand vous ouvrez une demande dans Gmail pour l'envoyer). Ni télémétrie, ni statistiques d'usage.
- **Minimisation** : chaque courtier ne reçoit que les champs nécessaires pour vous identifier. Aucune pièce d'identité n'est jamais envoyée automatiquement.
- **Pas de résolution automatique de CAPTCHA** : le navigateur s'ouvre et vous le résolvez vous-même.
- **Pas de recherche de personnes** : l'outil ne vérifie que votre propre présence chez un courtier.

## Utilisation

### Dans le navigateur (le plus simple)

```sh
uv sync --extra ui
uv run gomme ui
```

Une page s'ouvre dans votre navigateur. Elle tourne sur votre ordinateur seulement, protégée par un jeton. Un assistant vous demande votre profil, y compris vos anciennes coordonnées (anciennes adresses email ou postales, anciens numéros), que la demande d'effacement couvrira aussi. Gomme prépare ensuite une demande par broker, que vous envoyez :

- **automatiquement**, depuis votre adresse, après confirmation : avec Gmail, il faut un [mot de passe d'application](https://myaccount.google.com/apppasswords), gardé dans le trousseau sécurisé du système (jamais dans un fichier) ;
- ou **une par une**, ouverte en un clic dans Gmail ou dans votre logiciel de messagerie, déjà remplie.

Gomme suit ensuite les délais légaux, prépare les courriers PDF et vous dit quoi faire à chaque réponse.

### En ligne de commande

```sh
uv run gomme init              # votre profil ; les emails partent par défaut en mode manuel (.eml)
uv run gomme queue             # une demande par broker de votre région
uv run gomme send --dry-run    # vérifier ce qui partirait, sans rien envoyer
uv run gomme send              # écrire les .eml (ou envoyer via votre SMTP, après confirmation)
uv run gomme mark 1 send       # noter un envoi fait à la main
uv run gomme status            # où en sont vos demandes et leurs échéances légales
uv run gomme monitor           # à lancer régulièrement : retards et re-vérifications
uv run gomme letters           # courriers PDF pour les brokers qui n'acceptent que la poste
uv run gomme submit            # formulaires web, dans un navigateur visible (extra « web »)
uv run gomme escalate 1        # brouillon de plainte CNIL si un broker ne répond pas
```

Pour le démarchage téléphonique (fin de Bloctel en août 2026) et les listes rouge et anti-prospection des annuaires, voir [docs/opposition-fr.md](docs/opposition-fr.md).

Les formulaires web demandent Playwright : `uv sync --extra web`, puis `uv run playwright install chromium`. Gomme ne résout jamais de CAPTCHA : il vous rend la main.

Le mot de passe SMTP n'est jamais enregistré : il se lit dans la variable `GOMME_SMTP_PASSWORD` (ou dans le trousseau du système si le paquet `keyring` est installé).

## Développement

Prérequis : [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run gomme --version
uv run pytest
```

Pour ajouter un courtier, voir [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

[AGPL-3.0-only](LICENSE). Le code et les données repris d'autres projets sont attribués dans [THIRD_PARTY.md](THIRD_PARTY.md).
