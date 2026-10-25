# Contribuer

## Ajouter une fiche broker

Ajouter un courtier ne demande **aucun code Python** : il suffit d'un fichier YAML.

### 1. Où placer le fichier

```
src/gomme/data/brokers/<région>/<slug>.yml
```

- `<région>` vaut `fr`, `eu`, `us` ou `global`, et doit être identique au champ `region` de la fiche.
- Le nom du fichier est le `slug` suivi de `.yml` (pas `.yaml`).
- Une seule fiche par fichier.

### 2. Contenu

```yaml
slug: exemple-broker              # kebab-case, unique : minuscules, chiffres et tirets
name: Exemple Broker
domain: exemple.fr                # nom de domaine seul, sans https:// ni chemin
country: FR                       # code pays ISO 3166-1 alpha-2, en majuscules (obligatoire si source: manual)
region: fr                        # fr | eu | us | global
category: marketing               # marketing | people-search | annuaire | background-check | autre
channels:                         # au moins un, par ordre de préférence
  - type: email
    address: dpo@exemple.fr
  - type: web_form
    url: https://exemple.fr/rgpd
    steps: []                     # facultatif, voir « Formulaires web » ci-dessous
  - type: postal
    address: "DPO, 1 rue X, 75000 Paris"
legal_basis: [gdpr_art17, gdpr_art21]   # gdpr_art15 | gdpr_art17 | gdpr_art19 | gdpr_art21 | ccpa_1798_105
required_fields: [full_name, email]     # full_name | email | postal_address | birth_date | phone
fields_why:                       # facultatif mais conseillé : pourquoi chaque champ est demandé
  full_name: "Estimation : nécessaire pour toute demande."
  email: "La page demande « votre adresse email » pour traiter la demande (https://exemple.fr/rgpd)."
requires_id: false                # true seulement si le broker exige une pièce d'identité
language: fr                      # code ISO 639-1 de la langue de la demande
source: manual                    # manual | eraser | optout | badbool | optery
source_ref: https://exemple.fr/politique-de-confidentialite
last_verified: 2026-10-24         # obligatoire si source: manual
notes: ""                         # facultatif
```

Tout champ inconnu est refusé, pour attraper les fautes de frappe.

### 3. Formulaires web

Un canal `web_form` **sans étapes** est un formulaire à remplir à la main : Gomme ouvre la page et vous la remplissez vous-même. La page peut alors être hébergée ailleurs que chez le broker (OneTrust, Google Forms…).

Avec des **étapes**, Gomme pilote le navigateur, toujours en mode visible. Chaque étape a un `id` (minuscules, chiffres et `_`), un `type`, et peut avoir un `label` et `optional: true` (un échec est signalé sans arrêter la suite).

| Type | Paramètres | Effet |
|---|---|---|
| `navigate` | `url` (par défaut : l'URL du canal) | ouvre une page |
| `search` | `url`, `result_selector` | cherche **votre propre** fiche ; vous cliquez dessus, son URL devient `{listing_url}` |
| `form_fill` | `fields`, `frame` | remplit des champs (voir ci-dessous) |
| `click` | `selector`, `frame` | clique |
| `wait_for` | `selector` **ou** `js_condition`, `timeout_ms` (30 000 par défaut), `message`, `frame` | attend un élément ou une condition |
| `prompt_user` | `message` | met en pause jusqu'à ce que vous ayez agi (CAPTCHA, lien de confirmation…) |
| `prompt_user_if_present` | `selector`, `message`, `frame` | idem, seulement si l'élément est présent |
| `assert` | au moins un de `selector`, `text_contains`, `url_matches` (regex), `js_condition` | vérifie que la page est dans l'état attendu |
| `capture` | aucun | capture d'écran, conservée comme preuve |

`frame` est le sélecteur de l'iframe qui contient l'élément. Chaque champ de `form_fill` a un `selector`, une `value`, et éventuellement `input: type` (frappe touche par touche) ou `input: js` (champs React), et `force: true` (champ masqué).

Les messages s'écrivent dans les deux langues, le français d'abord :

```yaml
- id: solve_captcha
  type: prompt_user
  message:
    fr: Résolvez le CAPTCHA, puis appuyez sur Entrée.
    en: Solve the CAPTCHA, then press Enter.
```

**Variables.** Les données du profil ne peuvent entrer dans une étape que par ces variables :

| Variable | Champ du profil |
|---|---|
| `{full_name}`, `{first_name}`, `{last_name}`, `{name_slug}` (jane-doe), `{name_title_slug}` (Jane-Doe) | `full_name` |
| `{email}` | `email` |
| `{street}`, `{city}`, `{postal_code}`, `{state}` (NY), `{state_name}` (New York), `{state_slug}` (New-York) | `postal_address` |
| `{birth_date}`, `{birth_year}`, `{age}` | `birth_date` |
| `{phone}` | `phone` |
| `{listing_url}` | aucun : l'URL choisie lors d'une étape `search` précédente |

Pour une accolade littérale, écrire `{{` ou `}}`. `js_condition` et `url_matches` ne sont pas interprétés.

Le chargement refuse une fiche si :

- une étape utilise un champ absent de `required_fields` ;
- le formulaire ou une étape ouvre une page qui n'est pas en https sur le domaine du broker (ou un sous-domaine) ;
- `{listing_url}` est utilisée avant une étape `search`.

Il n'existe pas d'étape de résolution de CAPTCHA, et il n'y en aura pas : c'est toujours vous qui le résolvez.

### 4. Règles

- **Fiches importées.** Les fiches `source: eraser` ou `source: optout` sont générées par `scripts/` : ne les modifiez pas à la main, elles seraient écrasées au prochain import. Pour corriger un broker importé, créez une fiche `manual` sur le même domaine et supprimez la fiche générée : les imports suivants l'écarteront.
- **Citer ses sources.** `source_ref` pointe vers la page où vous avez trouvé les coordonnées (politique de confidentialité, mentions légales…). N'inventez rien : en cas de doute, ajoutez un commentaire `# TODO(verify): …`.
- **Dater la vérification.** `last_verified` est la date à laquelle vous avez vérifié la fiche vous-même.
- **Minimisation.** Dans `required_fields`, ne listez que les champs dont le broker a réellement besoin pour vous identifier.
- **Justifier.** `fields_why` explique chaque champ de `required_fields`, en une phrase affichée à l'utilisateur : soit une citation de la page du broker, entre « », suivie de son URL ; soit une phrase commençant par `Estimation :` quand la page ne dit rien. Jamais de citation inventée.
- **Aucune donnée personnelle** dans les fiches, les exemples ou les commits.

### 5. Vérifier

```sh
uv run gomme brokers list --region fr
uv run pytest
```

`gomme brokers list` charge et valide toutes les fiches. En cas d'erreur, il indique le fichier et le champ en cause.

Pour un formulaire automatisé, vérifiez aussi qu'il répond encore, sans aucune donnée personnelle :

```sh
uv sync --extra web && uv run playwright install chromium
uv run gomme verify --broker <slug> --headed
```

### 6. Commit

Un commit par fiche ou par petit lot, au format [Conventional Commits](https://www.conventionalcommits.org/fr/), avec le préfixe `data:` :

```
data: add exemple-broker (fr)
```

Tout code ou toute donnée repris d'un autre projet doit être attribué dans [THIRD_PARTY.md](THIRD_PARTY.md).

## Avant d'ouvrir une PR

```sh
uv run ruff check . && uv run ruff format --check . && uv run mypy src scripts && uv run pytest
```

Toute nouvelle dépendance doit être justifiée dans la description de la PR.
