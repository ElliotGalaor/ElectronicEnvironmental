# Technical guide — RoHS 2 / RoHS 3 / REACH Checker

A tool that analyses an electronic bill of materials (BOM) and determines, line by line and then for the whole
board, compliance with **RoHS 2** (2011/65/EU), **RoHS 3** (EU 2015/863) and **REACH** (EC 1907/2006),
based on a server-side component database.

## Getting started

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python cli.py seed          # loads 129 demo components
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000 (API documentation: http://localhost:8000/docs).

> ⚠ The demo data (`data/seed_components.csv`) is there to try the tool:
> - 26 example components (source "DEMO - to be checked", 3 of them fictitious `DEMO-…`);
> - 103 common components collected from the distributor LCSC on 6 November 2026 (source "LCSC Cxxxx"):
>   RoHS 2 / RoHS 3 from the certificate published by LCSC (RoHS 3 left "unknown" without a RoHS 3 certificate),
>   ECCN from the LCSC product page; REACH and RoHS exemptions are not published by this distributor and stay
>   "unknown" / empty.
>
> For a statement of compliance, rely on the manufacturers' declarations.

## How it works

1. **Reading the BOM**: CSV/TXT (`;` `,` tab separator detected, UTF-8 or Windows-1252) or Excel `.xlsx`.
   The header row and the columns are detected automatically (English and French names:
   *Manufacturer Part Number*, *MPN*, *Manufacturer*, *Designator*, *Qty*, *DNP*, *Référence fabricant*, *Repères*…).
   KiCad's "Reference" column is recognised as a designator if it holds `R1, C2…`.
   RoHS 2, RoHS 3 and REACH statuses declared in the BOM are also recognised ("RoHS" alone = RoHS 3).
   **Column mapping**: if the manufacturer part number is not detected, a dialog opens to choose the column
   for each field and the header row (`0` = file without a header); the "Columns…" button opens it too.
   It shows the effect of each change on the first 3 lines and the result of the reading (part number found
   in the database or not). The choice is kept in the history.
2. **Matching against the database**, from most to least reliable:
   | Match | Rule | Status used |
   |---|---|---|
   | Exact | same part number + same manufacturer (aliases handled: TI, ST, ON Semi…) | database status |
   | Part number only | same part number, no manufacturer in the BOM | database status |
   | Close | identical except for punctuation (`LM358-DR` = `LM358DR`) | database status |
   | Other manufacturer / Partial (packaging suffix) / Ambiguous | | **Needs review** |
   | Not found | + suggestions of similar part numbers | **Needs review** |

   Statuses declared in the BOM: the database is authoritative. A status that is unknown in the database is
   completed by the BOM, a disagreement gives **Needs review** (with both values in the notes), and a component
   that is missing or ambiguous takes the BOM statuses. These statuses are marked "from the BOM" on screen and
   in the exports.
3. **Verdict** per regulation: *Non-compliant* if one line is, otherwise *Needs review* if information is
   missing, otherwise *Compliant* (flagging RoHS exemptions and REACH SVHCs above 0.1 %).
   Components that are not populated (DNP) are excluded.
   Since RoHS 3 includes RoHS 2 (+ 4 phthalates), a RoHS 3 compliant component is inferred RoHS 2 compliant
   when that status is missing.

## Statuses in the database

| Field | Values |
|---|---|
| `rohs2`, `rohs3` | `compliant`, `exemption`, `non_compliant`, `unknown` |
| `reach` | `compliant` (no SVHC above 0.1 %), `svhc` (SVHC above 0.1 %: information duty under art. 33 / SCIP), `non_compliant`, `unknown` |

On import, the values `Yes/Compliant/OK` and `No/Not compliant` are also accepted, as well as their French
equivalents (`Oui`, `Conforme`, `Non`, `Non conforme`, `Inconnu`).
Other fields: `mpn`, `manufacturer`, `description`, `rohs_exemptions`, `svhc_list`, `eccn`,
`country_of_origin`, `source`, `last_checked`.

Up to v1.1.0, statuses were stored in French (`conforme`, `non_conforme`, `inconnu`…). A database created with
those versions is converted automatically the first time it is opened, including the verdicts kept in the
BOM history.

### Export and customs

| Field | Content | Examples |
|---|---|---|
| `eccn` | Export Control Classification Number (US EAR / EU dual-use list). Empty = unknown | `EAR99`, `5A992.c`, `3A991.a.1` |
| `country_of_origin` | Country of origin, stored as an ISO 3166-1 alpha-2 code. Empty = unknown | `CN`, `MY`, `DE` |

- The ECCN is normalised (`5a992.C` becomes `5A992.c`) and rejected if its format is not valid.
- The country can be entered as an ISO code or spelled out, in English or French ("Germany", "Malaisie");
  it is stored as a code.
- In the BOM analysis: "ECCN / Origin" column, "Controlled ECCN" filter, a note on every line whose ECCN is
  not EAR99, and a summary (controlled and unknown ECCNs, breakdown of countries of origin), also in the
  Excel export.
- Databases created before these fields existed get the two new columns (empty) automatically at start-up.

## Manufacturers

"Manufacturers" tab: list of the manufacturers in the database, with the number of components, the
RoHS 3 / REACH status, the controlled ECCNs, the headquarters country and the website.

Every manufacturer name (Database tab, manufacturer page, BOM analysis) links to its page:
- website and headquarters country (offered by default for known manufacturers, can be edited) and free notes
  (compliance contact, link to the RoHS/REACH declarations…);
- statistics: components in the database, share of RoHS 3 compliant ones, SVHCs, controlled or unknown ECCNs,
  countries of origin of the components;
- list of its components in the database, with direct access to their record.

Spelling variants are grouped: "TI", "Texas Instruments" and "Texas Instruments Inc." lead to the same page.
Each page has its own address (`#manufacturer/texas instruments`), usable as a direct link.

## Accounts and BOM history

"Sign in" button at the top right: sign in or create an account (e-mail, password of at least 8 characters,
optional name).

Once signed in, every analysed BOM is saved in **"My latest BOMs"** (Analysis tab): original file, chosen
columns, replacements by equivalents and verdicts. Changing the columns or replacing a line updates the same
entry. "Reopen" reruns the analysis against the current database: statuses may have changed since. The 20 most
recent analyses are kept per user; everyone only sees their own BOMs.

Security:
- passwords hashed with scrypt (random salt per account), never stored in clear text;
- 30-day session in an `HttpOnly`, `SameSite=Lax` cookie (and `Secure` over HTTPS); only the SHA-256 hash of
  the token is stored in the database;
- 5 failed sign-ins for the same address block attempts for 15 minutes;
- accounts are used for the history: changes to the database remain protected by `RRC_ADMIN_TOKEN` if set.

For a server that other people can reach, serve the application over **HTTPS** (for example behind a reverse
proxy) so that passwords and cookies are not sent in clear text. Saved BOMs are stored in the SQLite database
(`data/components.db`).

## RoHS exemptions

"RoHS exemptions" tab: exemptions used by the database components, with the substances concerned, the validity
("expires on …", "in N days") and the components / manufacturers.
Every exemption code shown in the application (Database tab, BOM analysis) links to its page:
- **official text** of the exemption and its **scope and dates**, taken from the consolidated text of
  Directive 2011/65/EU on EUR-Lex (English version, Annexes III and IV, 163 exemptions,
  `data/rohs_exemptions.json`);
- dates mentioned, highlighted once passed, and a reminder of Article 5(5) (an exemption remains valid while
  a renewal application submitted in time is being examined);
- upcoming consolidated version already published (changes planned for the exemption);
- links to EUR-Lex, components concerned (with access to equivalents to prepare for an expiry), editable notes.

Codes are recognised in several spellings (`7(c)-I`, `7 c)-I`, `7c-I`); prefix with `IV-` for Annex IV.
Updating the official text when a new consolidated version is published:

```bash
.venv\Scripts\python tools\extract_rohs_exemptions.py 20260701 20270813
```
(available versions: https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:32011L0065)

## ECCN codes

"ECCN codes" tab: list of the codes in the database (controlled ones first), with the field, the number of
components and the manufacturers. Every ECCN code shown in the application links to its page:
- **breakdown of the code**: category (e.g. 3 = Electronics), product group (A = systems, equipment and
  components), reason for control derived from the number (0xx Wassenaar, 1xx MTCR, 2xx NSG, 3xx Australia
  Group, 5xx/6xx space/military items, 9xx US unilateral controls / anti-terrorism) and sub-paragraph;
- **official heading** of the entry, taken from the Commerce Control List (`data/ccl_headings.json`,
  622 entries extracted from the eCFR, public domain);
- **links to the official definition** on the eCFR (US government website): Commerce Control List
  (15 CFR part 774, Supplement No. 1) and Commerce Country Chart (15 CFR part 738); for EAR99, 15 CFR 734.3(c);
- database components with this code, editable title and notes.

The summary by category / group / reason is indicative: only the official definition is authoritative.
To update the headings, rerun the extraction from the eCFR API
(`/api/versioner/v1/full/<date>/title-15.xml?part=774&appendix=Supplement No. 1 to Part 774`).

## Equivalents from other manufacturers

Clicking a part number in the BOM analysis (or the "Equivalents" button of a component in the Database tab
or on a manufacturer page) opens the list of similar components **from other manufacturers** in the database,
with their RoHS 2 / RoHS 3 / REACH statuses and their ECCN.

- **Similarity** computed from the descriptions: component type, value (Ω, F, H, Hz, V), package, rated
  voltage and current, dielectric, tolerance, channel (MOSFET). The common points and the differences
  ("lower voltage", "different package"…) are shown for each suggestion. A different value (10 kΩ versus
  100 Ω) excludes the candidate.
- For complex components (microcontrollers, DC-DC converters, generic transceivers), sharing the same type is
  not enough: only **confirmed equivalents** are offered.
- **Confirmed equivalences**: "Confirm the equivalence" button; they always come first, in both directions.
- **Replacement in the BOM**: "Use in the BOM" replaces the line and reruns the analysis. Replaced lines are
  highlighted; a banner lets you undo the replacements or **download the modified BOM** (CSV with all the
  original columns and a "Replaces" column). The Excel/CSV report exports also give the original part number.

Suggestions are leads: check the pinout, the footprint and the detailed characteristics on the datasheet
before confirming a replacement.

## Feeding the database

- **Web interface**: "Component database" tab (add, edit, CSV/XLSX import, export).
  From a report, the "+ Add to database" button prefills a part number that was not found.
- **Manual entry**: "+ Add" button, then fill in the fields.
- **From a PDF datasheet**: "From a PDF datasheet" button, or drop the PDF into the record.
  The fields are prefilled (and highlighted), with the datasheet excerpts and the page number that justify
  each value. **Nothing is saved without review**: correct the fields, then click "Save".
  If the component already exists, the existing record is offered.
- **Bulk import**: a CSV/XLSX file with the columns above. In the interface, the column mapping dialog opens
  before the import: columns to use, header row, and a line-by-line simulation (new, changed with the old and
  new value, unchanged, error), without writing anything.
  Uniqueness key: part number + manufacturer. On an existing component, only the fields mapped to a column
  and filled in are changed (a missing column or an empty cell does not replace the value in the database).
  ```bash
  .venv\Scripts\python cli.py import my_database.xlsx
  ```

### Datasheet extraction

Two methods, chosen automatically:

| Method | When | What it does |
|---|---|---|
| **Automatic rules** (default) | always available, offline | reads the PDF text: manufacturer (website, copyright), part number (frequency, page 1, ordering table, file name), description (title), mentions of RoHS / 2011/65 / 2015/863 / exemptions / REACH / SVHC, ECCN and country of origin ("ECCN: EAR99", "Country of Origin: Malaysia", "Made in Japan") |
| **AI (Claude)** | if `ANTHROPIC_API_KEY` is set on the server | sends the PDF to the model, which also reads tables and scanned PDFs; missing fields are completed by the rules; automatic fallback to the rules on error |

Caution rules applied in both cases:
- "Pb-free", "green" or "lead-free" alone are not enough to conclude RoHS compliance.
- RoHS 3 is only marked compliant if the datasheet cites (EU) 2015/863, RoHS 3 or the 10 substances;
  a plain "RoHS" mention only counts for RoHS 2.
- Many datasheets do not give compliance: the statuses then stay "unknown".
  The manufacturer's declaration of conformity remains the reference source.

Variables: `ANTHROPIC_API_KEY` (enables the AI), `RRC_AI=off` (disables the AI), `RRC_AI_MODEL`
(model, default `claude-opus-5-5`). With the AI, the PDF is sent to the Anthropic API.

## Command line

```bash
.venv\Scripts\python cli.py analyze examples\bom_example.csv -o report.xlsx
.venv\Scripts\python cli.py analyze bom.csv --server http://server:8000      # through the remote server
.venv\Scripts\python cli.py analyze bom.csv --mapping "{\"mpn\": \"Part Number\"}"
```
Exit code: `0` compliant, `1` needs review, `2` non-compliant (usable in continuous integration).

`mapping` format (API and command line): `{"mpn": "Item code", "manufacturer": "__none__", "_header_row": 3}`.
`__none__` ignores a wrongly detected field; `_header_row` is the number of the header row (`0` = no header).

## REST API

| Method | Route | Purpose |
|---|---|---|
| POST | `/api/analyze?format=json\|xlsx\|csv\|bom` | analyse a BOM (`file`, optional `mapping` and `substitutions`; `save=1` or `history_id` for the history when signed in); `bom` returns the modified BOM |
| GET | `/api/components?q=&status=issues&limit=&offset=` | search |
| GET/POST/PUT/DELETE | `/api/components[/{id}]` | manage a component |
| POST | `/api/components/import` | bulk import (`file`, optional `mapping`) |
| POST | `/api/table/preview?kind=bom\|components` | preview of a file: detected columns, offered fields, result of the reading (BOM) or simulation of the import (saves nothing) |
| POST | `/api/datasheet/extract?method=auto\|heuristic\|ai` | proposes a component's fields from its PDF datasheet (saves nothing) |
| GET | `/api/components/export` | CSV export of the database |
| GET | `/api/components/{id}/alternatives` | equivalents from other manufacturers for a component |
| GET | `/api/alternatives?mpn=&manufacturer=&description=` | equivalents for a part number not in the database |
| POST / DELETE | `/api/components/{id}/equivalents[/{other_id}]` | confirm / remove an equivalence |
| POST | `/api/auth/register`, `/api/auth/login`, `/api/auth/logout` | create an account, sign in, sign out |
| GET | `/api/auth/me` | signed-in user (`null` otherwise) |
| GET | `/api/history` | latest BOMs analysed by the signed-in user |
| GET / DELETE | `/api/history/{id}/file`, `/api/history/{id}` | original file of a BOM / deletion |
| GET | `/api/exemptions` | RoHS exemptions used by the database components |
| GET/PUT | `/api/exemptions/{code}` | an exemption's page (official text, dates, components) / notes |
| GET | `/api/eccn` | ECCN codes in the database, with their explanation |
| GET/PUT | `/api/eccn/{code}` | a code's page (breakdown, official heading, eCFR links, components) / title and notes |
| GET | `/api/manufacturers` | list of manufacturers and their statistics |
| GET/PUT | `/api/manufacturers/{key}` | a manufacturer's page (information + components) / edit the information |
| GET | `/api/countries` | recognised country codes and their names |
| GET | `/api/stats` | statistics |

## Server deployment

Step-by-step guide for a free Google Cloud VM with HTTPS: [DEPLOY.md](DEPLOY.md) (files in [`deploy/`](../deploy)).

- `RRC_DB_PATH`: path of the SQLite database (default `data/components.db`).
- `RRC_ADMIN_TOKEN`: if set, every change to the database requires the `X-Admin-Token` header
  (entered in the interface, Database tab). Browsing and analysis stay open.
- `FORWARDED_ALLOW_IPS`: behind a reverse proxy, the addresses whose `X-Forwarded-*` headers are trusted
  (uvicorn setting). Needed for the session cookie to be marked `Secure` over HTTPS; `deploy/docker-compose.yml`
  sets it to `*` because only the proxy can reach the application.

## Tests

```bash
.venv\Scripts\python -m pytest -q tests
```

## Structure

```
app/db.py         SQLite database, normalisation of part numbers and manufacturers, migrations
app/tables.py     CSV/XLSX reading, header and column detection, manual mapping
app/analyzer.py   matching and verdict computation
app/reports.py    Excel / CSV exports
app/importer.py   bulk import of the database (simulation, partial update)
app/datasheet.py  rule-based PDF datasheet extraction
app/datasheet_ai.py  PDF datasheet extraction with Claude (optional)
app/manufacturers.py manufacturer pages (information, statistics, components)
app/alternates.py   search for equivalents and confirmed equivalences
app/eccn.py         ECCN code pages (explanation, official definition)
app/auth.py         user accounts and sessions
app/history.py      history of analysed BOMs
app/exemptions.py   RoHS exemption pages (official EUR-Lex text)
tools/extract_rohs_exemptions.py  update of the exemption text from EUR-Lex
tools/screenshots.py  README screenshots (Playwright)
app/main.py       FastAPI API + web interface (app/static)
cli.py            command line
```
