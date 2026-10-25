# Code et données tiers

Tout code ou toute donnée repris d'un autre projet doit figurer ici, avec le commit exact utilisé lors de l'import.

| Projet | URL | Licence | Commit | Éléments repris | Date d'import |
|---|---|---|---|---|---|
| OptOut | https://github.com/Blake104/OptOut | AGPL-3.0-only ¹ | `7a310242eca38517a483d75a6a3832bc9424c01b` | `src/optout/data/brokers/*.yml` : 5 fiches `us` converties par `scripts/import_optout.py`. Conception des étapes `web_form` (aucun code copié). | 2026-10-25 |
| Eraser | https://github.com/drumandbytes/eraser | MIT ² | `6c3099a9a4abf40ee26fb67708b3c01593aa1b92` | `data/brokers.yaml` : 622 fiches `us` et `global` converties par `scripts/import_eraser.py`, et l'adresse email de la fiche OptOut `spokeo`. | 2026-10-25 |

¹ Licence déclarée dans le `pyproject.toml` et le README d'OptOut. Au commit importé, le dépôt ne contient pas de fichier `LICENSE`.

² Notice de licence d'origine, que la licence MIT impose de conserver : [LICENSES/eraser-MIT.txt](LICENSES/eraser-MIT.txt). Eraser est le fork maintenu de [digisamroc/eraser](https://github.com/digisamroc/eraser), dont l'auteur figure aussi dans la notice.

## Mettre à jour un import

Cloner le projet amont (sans modification locale), relancer le script, puis mettre à jour le commit et la date ci-dessus :

```sh
uv run python scripts/import_optout.py --repo <clone OptOut> --eraser-repo <clone Eraser>
uv run python scripts/import_eraser.py --repo <clone Eraser>
```

L'ordre compte : les brokers couverts par OptOut sont écartés de l'import Eraser.
