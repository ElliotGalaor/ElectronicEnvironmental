import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
