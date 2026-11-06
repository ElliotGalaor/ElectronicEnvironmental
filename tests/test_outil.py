import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
os.environ["RRC_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.importer import import_table  # noqa: E402
from app.main import app  # noqa: E402
from app.tables import parse_bom  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True, scope="module")
def seeded():
    with db.session() as conn:
        conn.execute("DELETE FROM components")
        import_table(conn, (ROOT / "data" / "seed_components.csv").read_bytes(), "seed.csv")


def analyze_text(text: str, name="bom.csv", **params):
    r = client.post("/api/analyze", files={"file": (name, text.encode("utf-8"))}, data=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_normalisation_fabricant():
    assert db.normalize_manufacturer("TI") == "texas instruments"
    assert db.normalize_manufacturer("Texas Instruments Inc.") == "texas instruments"
    assert db.normalize_manufacturer("ON Semiconductor") == "onsemi"
    assert db.normalize_manufacturer("Würth Elektronik") == "wurth elektronik"


def test_bom_exemple_verdicts():
    rep = analyze_text((ROOT / "exemples" / "bom_exemple.csv").read_text(encoding="utf-8"))
    s = rep["summary"]
    assert s["rohs2"]["verdict"] == "non_conforme"
    assert s["rohs3"]["verdict"] == "non_conforme"
    assert s["dnp"] == 1
    by_mpn = {l["bom_mpn"]: l for l in rep["lines"]}
    assert by_mpn["STM32F103C8T6"]["match"] == "exact"
    assert by_mpn["LM1117IMPX-3.3/NOPB"]["match"] == "exact"  # alias "TI"
    assert by_mpn["RC0603FR-0710KL"]["rohs3"] == "exemption"
    assert by_mpn["RC0603FR-0710KL"]["reach"] == "svhc"
    assert by_mpn["BSS138LT1G"]["match"] == "partiel"
    assert by_mpn["BSS138LT1G"]["rohs3"] == "a_verifier"
    assert by_mpn["DEMO-RELAY-12V"]["rohs2"] == "conforme"
    assert by_mpn["DEMO-RELAY-12V"]["rohs3"] == "non_conforme"
    assert by_mpn["RC0603FR-07100KL"]["match"] == "introuvable"
    assert by_mpn["RC0603FR-07100KL"]["suggestions"]


def test_bom_entierement_conforme():
    rep = analyze_text("MPN,Manufacturer,Qty\nNE555P,Texas Instruments,1\nLM358DR,TI,2\n")
    assert rep["summary"]["overall"] == "conforme"


def test_entetes_kicad_reference_est_un_repere():
    text = "Reference,Value,Footprint,Qty,MPN\n\"R1,R2\",10k,R_0603,2,RC0603FR-0710KL\nU1,NE555,DIP-8,1,NE555P\n"
    lines, mapping = parse_bom(text.encode(), "bom.csv")
    assert mapping.headers[mapping.columns["designator"]] == "Reference"
    assert mapping.headers[mapping.columns["mpn"]] == "MPN"
    assert lines[0].designator == "R1,R2"


def test_entetes_francais_et_ligne_titre():
    text = "Nomenclature carte X;;;\n;;;\nRepère;Quantité;Fabricant;Référence fabricant\nU1;1;ST;L7805CV\n"
    lines, mapping = parse_bom(text.encode("cp1252"), "bom.csv")
    assert mapping.header_row == 2
    assert lines[0].mpn == "L7805CV" and lines[0].manufacturer == "ST"


def test_dnp_colonne_fitted():
    text = "MPN,Fitted\nNE555P,Yes\nLM358DR,No\n"
    lines, _ = parse_bom(text.encode(), "bom.csv")
    assert [l.dnp for l in lines] == [False, True]


def test_bom_excel():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Designator", "Manufacturer Part Number", "Manufacturer"])
    ws.append(["U1", "ATMEGA328P-AU", "Microchip Technology"])
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post("/api/analyze", files={"file": ("bom.xlsx", buf.getvalue())})
    assert r.status_code == 200
    assert r.json()["lines"][0]["match"] == "exact"


def test_surcharge_colonnes():
    text = "Col A,Col B\nNE555P,TI\n"
    r = client.post("/api/analyze", files={"file": ("b.csv", text.encode())})
    assert r.status_code == 422  # pas de colonne MPN reconnue
    rep = analyze_text(text, mapping='{"mpn": "Col A", "manufacturer": "Col B"}')
    assert rep["lines"][0]["match"] == "exact"


def test_export_xlsx_et_csv():
    text = (ROOT / "exemples" / "bom_exemple.csv").read_bytes()
    for fmt in ("xlsx", "csv"):
        r = client.post(f"/api/analyze?format={fmt}", files={"file": ("bom.csv", text)})
        assert r.status_code == 200 and len(r.content) > 500


def test_crud_composant():
    r = client.post("/api/components", json={"mpn": "TEST-123", "manufacturer": "Acme", "rohs2": "oui", "rohs3": "Compliant", "reach": "SVHC"})
    assert r.status_code == 201, r.text
    comp = r.json()
    assert (comp["rohs2"], comp["rohs3"], comp["reach"]) == ("conforme", "conforme", "svhc")
    assert client.post("/api/components", json={"mpn": "test-123", "manufacturer": "ACME"}).status_code == 422  # doublon
    assert client.post("/api/components", json={"mpn": "X", "rohs2": "peut-être"}).status_code == 422
    r = client.put(f"/api/components/{comp['id']}", json={**comp, "reach": "conforme"})
    assert r.json()["reach"] == "conforme"
    assert client.delete(f"/api/components/{comp['id']}").status_code == 204
    assert client.get(f"/api/components/{comp['id']}").status_code == 404


def test_deduction_rohs2_depuis_rohs3():
    with db.session() as conn:
        db.upsert(conn, {"mpn": "INFER-1", "manufacturer": "Acme", "rohs2": "inconnu", "rohs3": "conforme", "reach": "conforme"})
    rep = analyze_text("MPN,Manufacturer\nINFER-1,Acme\n")
    line = rep["lines"][0]
    assert line["rohs2"] == "conforme" and line["rohs2_inferred"]


# ---------------------------------------------------------------- datasheets PDF

from pdfgen import make_pdf  # noqa: E402

from app import datasheet_ai  # noqa: E402

TI_PDF = make_pdf(
    [
        ["LM358B, LM2904B", "Industry-Standard Dual Operational Amplifiers", "1 Features", "- Supply 3 V to 36 V", "www.ti.com"],
        [
            "Orderable Device  Status  Package  Eco Plan",
            "LM358BIDR  ACTIVE  SOIC  RoHS & Green",
            "LM358BIPWR  ACTIVE  TSSOP  RoHS & Green",
            "RoHS: compliant with the current EU RoHS requirements for all 10 RoHS",
            "substances. REACH: no SVHC above 0.1% by weight.",
            "Copyright (c) 2023, Texas Instruments Incorporated",
        ],
    ],
    title="LM358B Dual Operational Amplifiers datasheet",
)
YAGEO_PDF = make_pdf([[
    "Chip Resistor Surface Mount RC Series 0603", "Example: RC0603FR-0710KL",
    "- RoHS compliant with exemption 7(c)-I",
    "- Contains lead (CAS 7439-92-1), an SVHC under REACH, above 0.1% w/w", "www.yageo.com",
]])


def extract_pdf(data: bytes, name: str, **params):
    return client.post("/api/datasheet/extract", files={"file": (name, data, "application/pdf")}, params=params)


def test_datasheet_ti(monkeypatch):
    monkeypatch.setattr(datasheet_ai, "available", lambda: False)
    r = extract_pdf(TI_PDF, "lm358b.pdf")
    assert r.status_code == 200, r.text
    d = r.json()
    f = d["fields"]
    assert d["method"] == "heuristique"
    assert f["mpn"] == "LM358B" and f["manufacturer"] == "Texas Instruments"
    assert "Operational Amplifiers" in f["description"]
    assert (f["rohs2"], f["rohs3"], f["reach"]) == ("conforme", "conforme", "conforme")
    assert "LM358BIDR" in [c["value"] for c in d["candidates"]["mpn"]]
    assert d["evidence"]["rohs3"]


def test_datasheet_exemption_et_svhc(monkeypatch):
    monkeypatch.setattr(datasheet_ai, "available", lambda: False)
    f = extract_pdf(YAGEO_PDF, "RC0603FR-0710KL.pdf").json()
    assert f["existing_id"]  # déjà dans la base de démonstration
    f = f["fields"]
    assert f["mpn"] == "RC0603FR-0710KL" and f["manufacturer"] == "Yageo"
    assert f["rohs2"] == "exemption" and f["rohs_exemptions"] == "7(c)-I"
    assert f["rohs3"] == "inconnu"  # 2015/863 non mentionnée
    assert f["reach"] == "svhc" and "7439-92-1" in f["svhc_list"]


def test_datasheet_sans_conformite(monkeypatch):
    monkeypatch.setattr(datasheet_ai, "available", lambda: False)
    d = extract_pdf(make_pdf([["AW-2000X Precision Sensor Module", "RoHS-friendly packaging: Pb-free"]]), "s.pdf").json()
    assert d["fields"]["rohs2"] == "inconnu"
    assert any("sans plomb" in n for n in d["notes"])


def test_datasheet_fichier_invalide():
    assert extract_pdf(b"pas un pdf", "x.pdf").status_code == 422


def test_datasheet_ia_indisponible(monkeypatch):
    monkeypatch.setattr(datasheet_ai, "available", lambda: False)
    assert extract_pdf(TI_PDF, "lm358b.pdf", method="ia").status_code == 400


def test_datasheet_ia_complete_par_heuristique(monkeypatch):
    def fake(data, name):
        return {
            "method": "ia", "model": "test",
            "fields": {"mpn": "LM358BIDR", "manufacturer": "", "description": "Double ampli-op", "rohs2": "conforme",
                       "rohs3": "conforme", "reach": "inconnu", "rohs_exemptions": "", "svhc_list": "", "source": "x"},
            "candidates": {"mpn": [{"value": "LM358BIDR"}], "manufacturer": []},
            "evidence": {}, "notes": [], "warnings": [],
        }
    monkeypatch.setattr(datasheet_ai, "available", lambda: True)
    monkeypatch.setattr(datasheet_ai, "extract", fake)
    d = extract_pdf(TI_PDF, "lm358b.pdf").json()
    assert d["method"] == "ia"
    assert d["fields"]["mpn"] == "LM358BIDR"
    assert d["fields"]["manufacturer"] == "Texas Instruments"  # complété par l'heuristique


def test_datasheet_ia_en_erreur_repli(monkeypatch):
    def boom(data, name):
        raise datasheet_ai.AIExtractionError("quota")
    monkeypatch.setattr(datasheet_ai, "available", lambda: True)
    monkeypatch.setattr(datasheet_ai, "extract", boom)
    d = extract_pdf(TI_PDF, "lm358b.pdf").json()
    assert d["method"] == "heuristique" and any("quota" in w for w in d["warnings"])


# ---------------------------------------------------------------- ECCN et pays d'origine

import sqlite3  # noqa: E402


def test_normalisation_eccn_et_pays():
    assert db.normalize_eccn("5a992.C") == "5A992.c"
    assert db.normalize_eccn("ear 99") == "EAR99"
    assert db.normalize_eccn("") == ""
    assert db.normalize_country("Malaisie") == "MY"
    assert db.normalize_country("united states") == "US"
    assert db.normalize_country("tw") == "TW"
    for bad in ("5Z999", "ECCN"):
        with pytest.raises(db.ValidationError):
            db.normalize_eccn(bad)
    with pytest.raises(db.ValidationError):
        db.normalize_country("Atlantide")


def test_crud_eccn_pays():
    r = client.post("/api/components", json={"mpn": "EXP-1", "manufacturer": "Acme", "eccn": "3a991.a.1", "country_of_origin": "Japon"})
    assert r.status_code == 201, r.text
    assert (r.json()["eccn"], r.json()["country_of_origin"]) == ("3A991.a.1", "JP")
    assert client.post("/api/components", json={"mpn": "EXP-2", "eccn": "faux"}).status_code == 422
    client.delete(f"/api/components/{r.json()['id']}")


def test_import_colonnes_eccn_pays():
    csv_text = "Part Number;Fabricant;ECCN;Country of Origin\nIMP-ECCN-1;Acme;EAR99;Germany\nIMP-ECCN-2;Acme;5A002;Mars\n"
    with db.session() as conn:
        res = import_table(conn, csv_text.encode(), "base.csv")
        row = conn.execute("SELECT eccn, country_of_origin FROM components WHERE mpn = 'IMP-ECCN-1'").fetchone()
    assert res["created"] == 1 and res["error_count"] == 1  # "Mars" refusé
    assert tuple(row) == ("EAR99", "DE")


def test_migration_base_existante(tmp_path):
    path = tmp_path / "ancienne.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA.replace("    eccn              TEXT NOT NULL DEFAULT '',\n", "").replace(
        "    country_of_origin TEXT NOT NULL DEFAULT '',\n", ""))
    conn.execute("INSERT INTO components (mpn, mpn_norm, mpn_loose, updated_at) VALUES ('OLD1', 'OLD1', 'OLD1', 'x')")
    conn.commit()
    conn.close()
    with db.session(path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(components)")}
        assert {"eccn", "country_of_origin"} <= cols
        assert conn.execute("SELECT mpn, eccn FROM components").fetchone()["mpn"] == "OLD1"


def test_analyse_resume_export():
    rep = analyze_text("MPN,Manufacturer\nDEMO-LEADED-DIP8,Exemple Corp\nDEMO-RELAY-12V,Exemple Corp\nNE555P,TI\n")
    ex = rep["summary"]["export"]
    assert ex["eccn_controlled"] == 1 and ex["eccn_ear99"] == 2 and ex["eccn_unknown"] == 0
    assert ex["countries"] == {"TW": 1, "CN": 1}
    line = next(l for l in rep["lines"] if l["bom_mpn"] == "DEMO-LEADED-DIP8")
    assert any("5A992.c" in n for n in line["notes"])


def test_datasheet_eccn_pays(monkeypatch):
    monkeypatch.setattr(datasheet_ai, "available", lambda: False)
    pdf = make_pdf([["XYZ123A Sensor", "www.ti.com", "ECCN: EAR99", "Country of Origin: Malaysia"]])
    f = extract_pdf(pdf, "xyz123a.pdf").json()["fields"]
    assert (f["eccn"], f["country_of_origin"]) == ("EAR99", "MY")


# ---------------------------------------------------------------- fabricants

def test_liste_fabricants():
    mfrs = {m["key"]: m for m in client.get("/api/manufacturers").json()}
    ti = mfrs["texas instruments"]
    assert ti["name"] == "Texas Instruments" and ti["website"] == "https://www.ti.com" and ti["country"] == "US"
    assert ti["stats"]["components"] >= 4
    assert mfrs["exemple"]["website"] == ""  # fabricant fictif : pas de site connu


def test_fiche_fabricant_regroupe_les_alias():
    client.post("/api/components", json={"mpn": "ALIAS-TEST-1", "manufacturer": "TI"})
    d = client.get("/api/manufacturers/texas instruments").json()
    assert "ALIAS-TEST-1" in [c["mpn"] for c in d["components"]]
    assert "TI" in d["aliases"]
    assert all(c["manufacturer_key"] == "texas instruments" for c in d["components"])
    assert client.get("/api/manufacturers/inexistant-xyz").status_code == 404


def test_modifier_fabricant():
    r = client.put("/api/manufacturers/exemple", json={"website": "exemple.com", "country": "France", "notes": "fictif"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["website"], d["country"], d["notes"]) == ("https://exemple.com", "FR", "fictif")
    assert not d["website_is_default"]
    assert client.put("/api/manufacturers/exemple", json={"website": "pas un site"}).status_code == 422
    # Champ vidé : retour à la valeur par défaut connue
    d = client.put("/api/manufacturers/murata", json={"website": ""}).json()
    assert d["website"] == "https://www.murata.com" and d["website_is_default"]


# ---------------------------------------------------------------- équivalents et remplacements

from app import alternates  # noqa: E402


def comp_id(mpn: str) -> int:
    return client.get("/api/components", params={"q": mpn}).json()["items"][0]["id"]


def test_extraction_attributs():
    a = alternates.extract("Condensateur céramique 10 µF 25 V X5R 0805")
    assert (a.category, a.package, a.dielectric) == ("capacitor", "0805", "X5R")
    assert a.primary() == pytest.approx(10e-6) and a.max("V") == 25
    m = alternates.extract("MOSFET canal N 50 V 220 mA, SOT-23")
    assert (m.category, m.channel, m.package) == ("mosfet", "N", "SOT23")


def test_equivalents_meme_valeur_autre_fabricant():
    alts = client.get(f"/api/components/{comp_id('RC0603FR-0710KL')}/alternatives").json()["alternatives"]
    mpns = [a["component"]["mpn"] for a in alts]
    assert "CRCW060310K0FKEA" in mpns            # même valeur, même boîtier, autre fabricant
    assert "RC0805FR-07100RL" not in mpns        # même fabricant et autre valeur


def test_pas_de_suggestion_automatique_pour_les_mcu():
    alts = client.get(f"/api/components/{comp_id('STM32F103C8T6')}/alternatives").json()["alternatives"]
    assert alts == []


def test_equivalent_conforme_pour_composant_non_conforme():
    alts = client.get(f"/api/components/{comp_id('DEMO-LEADED-DIP8')}/alternatives").json()["alternatives"]
    lm = next(a for a in alts if a["component"]["mpn"] == "LM358DR")
    assert lm["compliant"] and any("boîtier" in d for d in lm["differences"])


def test_alternatives_pour_ligne_hors_base():
    r = client.get("/api/alternatives", params={"mpn": "XYZ-10K", "manufacturer": "Acme", "description": "Résistance 10 kΩ 1 % 0603"}).json()
    assert {"RC0603FR-0710KL", "CRCW060310K0FKEA"} <= {a["component"]["mpn"] for a in r["alternatives"]}


def test_confirmer_equivalence():
    a, b = comp_id("NE555P"), comp_id("L7805CV")
    assert client.post(f"/api/components/{a}/equivalents", json={"other_id": b}).status_code == 204
    alts = client.get(f"/api/components/{b}/alternatives").json()["alternatives"]
    assert alts[0]["component"]["id"] == a and alts[0]["confirmed"]  # symétrique et en tête
    assert client.delete(f"/api/components/{a}/equivalents/{b}").status_code == 204
    assert all(x["component"]["id"] != a for x in client.get(f"/api/components/{b}/alternatives").json()["alternatives"])
    assert client.post(f"/api/components/{a}/equivalents", json={"other_id": a}).status_code == 422


BOM_SWAP = "Designator;Manufacturer;MPN\nU1;Exemple Corp;DEMO-LEADED-DIP8\nR1;Yageo;RC0603FR-0710KL\n"


def test_remplacement_dans_la_bom():
    lm = comp_id("LM358DR")
    rep = analyze_text(BOM_SWAP, substitutions=json.dumps({"2": lm}))
    line = rep["lines"][0]
    assert line["bom_mpn"] == "LM358DR" and line["original_mpn"] == "DEMO-LEADED-DIP8"
    assert line["rohs3"] == "conforme" and rep["summary"]["substituted"] == 1
    assert rep["summary"]["rohs3"]["verdict"] != "non_conforme"


def test_bom_modifiee_export():
    lm = comp_id("LM358DR")
    r = client.post("/api/analyze?format=bom", files={"file": ("b.csv", BOM_SWAP.encode())},
                    data={"substitutions": json.dumps({"2": lm})})
    lines = r.content.decode("utf-8-sig").splitlines()
    assert lines[0].endswith(";Remplace")
    assert lines[1] == "U1;Texas Instruments;LM358DR;DEMO-LEADED-DIP8 (Exemple Corp)"
    assert lines[2] == "R1;Yageo;RC0603FR-0710KL;"


def test_remplacement_invalide():
    r = client.post("/api/analyze", files={"file": ("b.csv", BOM_SWAP.encode())}, data={"substitutions": '{"99": 1}'})
    assert r.status_code == 422
    r = client.post("/api/analyze", files={"file": ("b.csv", BOM_SWAP.encode())}, data={"substitutions": "pas du json"})
    assert r.status_code == 400
