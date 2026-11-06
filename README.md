# Vérificateur RoHS 2 / RoHS 3 / REACH pour BOM

Outil qui analyse une nomenclature électronique (BOM) et détermine, ligne par ligne puis pour la carte entière,
la conformité à **RoHS 2** (2011/65/UE), **RoHS 3** (UE 2015/863) et **REACH** (CE 1907/2006),
à partir d'une base de données serveur des composants.

## Démarrage

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python cli.py seed          # charge 26 composants de démonstration
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Puis ouvrir http://localhost:8000 (documentation de l'API : http://localhost:8000/docs).

> ⚠ Les données de démonstration (`data/seed_components.csv`) servent uniquement à tester l'outil.
> Remplacez-les par vos propres données issues des déclarations fabricants.

## Fonctionnement

1. **Lecture de la BOM** : CSV/TXT (séparateur `;` `,` tab détecté, UTF-8 ou Windows-1252) ou Excel `.xlsx`.
   La ligne d'en-tête et les colonnes sont détectées automatiquement (noms français et anglais :
   *Manufacturer Part Number*, *Référence fabricant*, *MPN*, *Fabricant*, *Repères*, *Qté*, *DNP*…).
   La colonne « Reference » de KiCad est reconnue comme repère si elle contient des `R1, C2…`.
   Les colonnes peuvent être corrigées dans l'interface (bouton « Colonnes… »).
2. **Rapprochement avec la base**, par ordre de fiabilité :
   | Correspondance | Règle | Statut retenu |
   |---|---|---|
   | Exacte | même référence + même fabricant (alias gérés : TI, ST, ON Semi…) | statut de la base |
   | Référence seule | même référence, pas de fabricant dans la BOM | statut de la base |
   | Approchante | identique hors ponctuation (`LM358-DR` = `LM358DR`) | statut de la base |
   | Autre fabricant / Partielle (suffixe d'emballage) / Ambiguë | | **À vérifier** |
   | Introuvable | + suggestions de références proches | **À vérifier** |
3. **Verdict** par norme : *Non conforme* si une ligne l'est, sinon *À vérifier* s'il manque une information,
   sinon *Conforme* (en signalant les exemptions RoHS et les SVHC REACH > 0,1 %).
   Les composants non montés (DNP) sont exclus.
   RoHS 3 incluant RoHS 2 (+ 4 phtalates), un composant RoHS 3 conforme est déduit conforme RoHS 2 si ce statut manque.

## Statuts en base

| Champ | Valeurs |
|---|---|
| `rohs2`, `rohs3` | `conforme`, `exemption`, `non_conforme`, `inconnu` |
| `reach` | `conforme` (pas de SVHC > 0,1 %), `svhc` (SVHC > 0,1 % : obligation d'info art. 33 / SCIP), `non_conforme`, `inconnu` |

À l'import, les valeurs `Oui/Yes/Compliant/OK` et `Non/No/Not compliant` sont aussi acceptées.
Autres champs : `mpn`, `manufacturer`, `description`, `rohs_exemptions`, `svhc_list`, `eccn`,
`country_of_origin`, `source`, `last_checked`.

### Export et douane

| Champ | Contenu | Exemples |
|---|---|---|
| `eccn` | Export Control Classification Number (EAR américain / liste UE des biens à double usage). Vide = inconnu | `EAR99`, `5A992.c`, `3A991.a.1` |
| `country_of_origin` | Pays d'origine, stocké en code ISO 3166-1 alpha-2. Vide = inconnu | `CN`, `MY`, `DE` |

- L'ECCN est normalisé (`5a992.C` devient `5A992.c`) et refusé s'il n'a pas un format valide.
- Le pays peut être saisi en code ISO ou en toutes lettres, en français ou en anglais (« Malaisie », « Germany ») ;
  il est enregistré sous forme de code.
- Dans l'analyse de BOM : colonne « ECCN / Origine », filtre « ECCN contrôlé », remarque sur chaque ligne
  dont l'ECCN n'est pas EAR99, et synthèse (ECCN contrôlés, inconnus, répartition des pays d'origine),
  reprise dans l'export Excel.
- Les bases créées avant cette version reçoivent automatiquement les deux nouvelles colonnes (vides) au démarrage.

## Fabricants

Onglet « Fabricants » : liste des fabricants présents en base, avec le nombre de composants, l'état
RoHS 3 / REACH, les ECCN contrôlés, le pays du siège et le site web.

Chaque nom de fabricant (onglet Base, fiche fabricant, analyse de BOM) est un lien vers sa fiche :
- site web et pays du siège (proposés par défaut pour les fabricants connus, modifiables) et notes libres
  (contact conformité, lien vers les déclarations RoHS/REACH…) ;
- statistiques : composants en base, part RoHS 3 conforme, SVHC, ECCN contrôlés ou inconnus,
  pays d'origine des composants ;
- liste de ses composants en base, avec accès direct à leur fiche.

Les variantes d'écriture sont regroupées : « TI », « Texas Instruments » et « Texas Instruments Inc. »
mènent à la même fiche. Chaque page a sa propre adresse (`#fabricant/texas instruments`), utilisable
comme lien direct.

## Équivalents d'autres fabricants

Cliquer sur une référence dans l'analyse de BOM (ou sur le bouton « Équivalents » d'un composant dans
l'onglet Base ou une fiche fabricant) ouvre la liste des composants similaires **d'autres fabricants**
présents en base, avec leurs statuts RoHS 2 / RoHS 3 / REACH et leur ECCN.

- **Similarité** calculée à partir des descriptions : type de composant, valeur (Ω, F, H, Hz, V),
  boîtier, tension et courant nominaux, diélectrique, tolérance, canal (MOSFET). Les points communs et
  les différences (« tension inférieure », « boîtier différent »…) sont affichés pour chaque suggestion.
  Une valeur différente (10 kΩ contre 100 Ω) exclut le candidat.
- Pour les composants complexes (microcontrôleurs, convertisseurs DC-DC, transceivers génériques),
  partager le même type ne suffit pas : seuls les **équivalents confirmés** sont proposés.
- **Équivalences confirmées** : bouton « Confirmer l'équivalence » ; elles apparaissent toujours en tête,
  dans les deux sens.
- **Remplacement dans la BOM** : « Utiliser dans la BOM » remplace la ligne et relance l'analyse.
  Les lignes remplacées sont surlignées ; un bandeau permet d'annuler les remplacements ou de
  **télécharger la BOM modifiée** (CSV avec toutes les colonnes d'origine et une colonne « Remplace »).
  Les exports Excel/CSV du rapport indiquent aussi la référence d'origine.

Les suggestions sont des pistes : vérifiez le brochage, l'encombrement et les caractéristiques
détaillées sur la datasheet avant de valider un remplacement.

## Alimenter la base

- **Interface web** : onglet « Base de composants » (ajout, modification, import CSV/XLSX, export).
  Depuis un rapport, le bouton « + Ajouter à la base » pré-remplit une référence introuvable.
- **Ajout manuel** : bouton « + Ajouter », puis saisie des champs.
- **Ajout depuis une datasheet PDF** : bouton « Depuis une datasheet PDF », ou dépôt du PDF dans la fiche.
  Les champs sont pré-remplis (et surlignés), avec les extraits de la datasheet et le numéro de page
  qui justifient chaque valeur. **Rien n'est enregistré sans relecture** : on corrige puis on clique sur « Enregistrer ».
  Si le composant existe déjà, la fiche existante est proposée.

### Extraction des datasheets

Deux méthodes, choisies automatiquement :

| Méthode | Quand | Ce qu'elle fait |
|---|---|---|
| **Règles automatiques** (par défaut) | toujours disponible, hors ligne | lit le texte du PDF : fabricant (site web, copyright), référence (fréquence, page 1, tableau de commande, nom du fichier), description (titre), mentions RoHS / 2011/65 / 2015/863 / exemptions / REACH / SVHC, ECCN et pays d'origine (« ECCN: EAR99 », « Country of Origin: Malaysia », « Made in Japan ») |
| **IA (Claude)** | si `ANTHROPIC_API_KEY` est défini sur le serveur | envoie le PDF au modèle, qui lit aussi les tableaux et les PDF scannés ; les champs manquants sont complétés par les règles ; repli automatique sur les règles en cas d'erreur |

Règles de prudence appliquées dans les deux cas :
- « Pb-free », « green » ou « lead-free » seuls ne suffisent pas pour conclure à la conformité RoHS.
- RoHS 3 n'est marqué conforme que si la datasheet cite (UE) 2015/863, RoHS 3 ou les 10 substances ;
  une simple mention « RoHS » ne vaut que pour RoHS 2.
- Beaucoup de datasheets ne donnent pas la conformité : les statuts restent alors « inconnu ».
  La déclaration de conformité du fabricant reste la source de référence.

Variables : `ANTHROPIC_API_KEY` (active l'IA), `RRC_AI=off` (désactive l'IA), `RRC_AI_MODEL`
(modèle, défaut `claude-opus-5-5`). Avec l'IA, le PDF est envoyé à l'API Anthropic.
- **Import en masse** : un CSV/XLSX avec les colonnes ci-dessus (première ligne = en-têtes). Clé d'unicité :
  référence + fabricant (mise à jour si existe déjà).
  ```bash
  .venv\Scripts\python cli.py import ma_base.xlsx
  ```

## Ligne de commande

```bash
.venv\Scripts\python cli.py analyze exemples\bom_exemple.csv -o rapport.xlsx
.venv\Scripts\python cli.py analyze bom.csv --server http://serveur:8000      # via le serveur distant
.venv\Scripts\python cli.py analyze bom.csv --mapping "{\"mpn\": \"Part Number\"}"
```
Code de sortie : `0` conforme, `1` à vérifier, `2` non conforme (utilisable en intégration continue).

## API REST

| Méthode | Route | Rôle |
|---|---|---|
| POST | `/api/analyze?format=json\|xlsx\|csv\|bom` | analyse d'une BOM (`file`, `mapping` et `substitutions` optionnels) ; `bom` renvoie la BOM modifiée |
| GET | `/api/components?q=&status=probleme&limit=&offset=` | recherche |
| GET/POST/PUT/DELETE | `/api/components[/{id}]` | gestion d'un composant |
| POST | `/api/components/import` | import en masse |
| POST | `/api/datasheet/extract?method=auto\|heuristique\|ia` | propose les champs d'un composant depuis sa datasheet PDF (n'enregistre rien) |
| GET | `/api/components/export` | export CSV de la base |
| GET | `/api/components/{id}/alternatives` | équivalents d'autres fabricants pour un composant |
| GET | `/api/alternatives?mpn=&manufacturer=&description=` | équivalents pour une référence absente de la base |
| POST / DELETE | `/api/components/{id}/equivalents[/{autre_id}]` | confirmer / retirer une équivalence |
| GET | `/api/manufacturers` | liste des fabricants et leurs statistiques |
| GET/PUT | `/api/manufacturers/{clé}` | fiche d'un fabricant (infos + composants) / modification des infos |
| GET | `/api/countries` | codes pays reconnus et leur nom |
| GET | `/api/stats` | statistiques |

## Déploiement serveur

- `RRC_DB_PATH` : chemin de la base SQLite (défaut `data/components.db`).
- `RRC_ADMIN_TOKEN` : si défini, toute modification de la base exige l'en-tête `X-Admin-Token`
  (saisi dans l'interface, onglet Base). La consultation et l'analyse restent libres.

## Tests

```bash
.venv\Scripts\python -m pytest -q tests
```

## Structure

```
app/db.py         base SQLite, normalisation des références et fabricants
app/tables.py     lecture CSV/XLSX, détection de l'en-tête et des colonnes
app/analyzer.py   rapprochement et calcul des verdicts
app/reports.py    exports Excel / CSV
app/importer.py   import en masse de la base
app/datasheet.py  extraction des datasheets PDF par règles
app/datasheet_ai.py  extraction des datasheets PDF par Claude (optionnelle)
app/manufacturers.py fiches fabricants (infos, statistiques, composants)
app/alternates.py   recherche d'équivalents et équivalences confirmées
app/main.py       API FastAPI + interface web (app/static)
cli.py            ligne de commande
```
