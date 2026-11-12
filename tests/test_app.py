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
# Most tests change the database without an account; the edit access tests turn this off.
os.environ["RRC_OPEN_EDITING"] = "1"

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


def test_home_page_and_example_bom():
    page = client.get("/").text
    assert 'id="tab-home"' in page and 'id="btnExample"' in page
    r = client.get("/examples/bom_example.csv")
    assert r.status_code == 200 and r.content == (ROOT / "examples" / "bom_example.csv").read_bytes()
    # The example offered on the home page is a BOM the analysis reads without manual mapping
    report = analyze_text(r.text, name="bom_example.csv")
    assert report["summary"]["lines"] > 10 and report["summary"]["not_found"] <= 2


def test_manufacturer_normalisation():
    assert db.normalize_manufacturer("TI") == "texas instruments"
    assert db.normalize_manufacturer("Texas Instruments Inc.") == "texas instruments"
    assert db.normalize_manufacturer("ON Semiconductor") == "onsemi"
    assert db.normalize_manufacturer("Würth Elektronik") == "wurth elektronik"


def test_example_bom_verdicts():
    rep = analyze_text((ROOT / "examples" / "bom_example.csv").read_text(encoding="utf-8"))
    s = rep["summary"]
    assert s["rohs2"]["verdict"] == "non_compliant"
    assert s["rohs3"]["verdict"] == "non_compliant"
    assert s["dnp"] == 1
    by_mpn = {l["bom_mpn"]: l for l in rep["lines"]}
    assert by_mpn["STM32F103C8T6"]["match"] == "exact"
    assert by_mpn["LM1117IMPX-3.3/NOPB"]["match"] == "exact"  # "TI" alias
    assert by_mpn["RC0603FR-0710KL"]["rohs3"] == "exemption"
    assert by_mpn["RC0603FR-0710KL"]["reach"] == "svhc"
    assert by_mpn["BSS138LT1G"]["match"] == "exact"   # present in the LCSC data
    assert by_mpn["DEMO-RELAY-12V"]["rohs2"] == "compliant"
    assert by_mpn["DEMO-RELAY-12V"]["rohs3"] == "non_compliant"
    assert by_mpn["RC0603FR-07100KL"]["match"] == "exact"   # present in the LCSC data
    assert by_mpn["SRR1260-470M"]["match"] == "not_found"
    # Part number not in the database but close to a known one: suggestions are offered
    near = analyze_text("MPN,Manufacturer\nRC0603FR-0710KZ,Yageo\n")["lines"][0]
    assert near["match"] == "not_found" and near["suggestions"]


def test_partial_match_on_suffix():
    # Known base part number + unknown packaging suffix: partial match, needs review
    line = analyze_text("MPN,Manufacturer\nBSS138-ZZTEST,onsemi\n")["lines"][0]
    assert line["match"] == "partial" and line["rohs3"] == "needs_review"


def test_lcsc_components_imported():
    with db.session() as conn:
        n = conn.execute("SELECT COUNT(*) FROM components WHERE source LIKE 'LCSC C%'").fetchone()[0]
        bad = conn.execute("SELECT COUNT(*) FROM components WHERE source LIKE 'LCSC C%' AND reach != 'unknown'").fetchone()[0]
    assert n >= 100 and bad == 0   # REACH not published by LCSC: never filled in from this source


def test_fully_compliant_bom():
    rep = analyze_text("MPN,Manufacturer,Qty\nNE555P,Texas Instruments,1\nLM358DR,TI,2\n")
    assert rep["summary"]["overall"] == "compliant"


def test_kicad_headers_reference_is_a_designator():
    text = "Reference,Value,Footprint,Qty,MPN\n\"R1,R2\",10k,R_0603,2,RC0603FR-0710KL\nU1,NE555,DIP-8,1,NE555P\n"
    lines, mapping = parse_bom(text.encode(), "bom.csv")
    assert mapping.headers[mapping.columns["designator"]] == "Reference"
    assert mapping.headers[mapping.columns["mpn"]] == "MPN"
    assert lines[0].designator == "R1,R2"


def test_french_headers_and_title_row():
    # BOMs exported by French-language tools must still be recognised
    text = "Nomenclature carte X;;;\n;;;\nRepère;Quantité;Fabricant;Référence fabricant\nU1;1;ST;L7805CV\n"
    lines, mapping = parse_bom(text.encode("cp1252"), "bom.csv")
    assert mapping.header_row == 2
    assert lines[0].mpn == "L7805CV" and lines[0].manufacturer == "ST"


def test_dnp_fitted_column():
    text = "MPN,Fitted\nNE555P,Yes\nLM358DR,No\n"
    lines, _ = parse_bom(text.encode(), "bom.csv")
    assert [l.dnp for l in lines] == [False, True]


def test_excel_bom():
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


def test_column_override():
    text = "Col A,Col B\nNE555P,TI\n"
    r = client.post("/api/analyze", files={"file": ("b.csv", text.encode())})
    assert r.status_code == 422  # no recognised MPN column
    rep = analyze_text(text, mapping='{"mpn": "Col A", "manufacturer": "Col B"}')
    assert rep["lines"][0]["match"] == "exact"


def test_xlsx_and_csv_export():
    text = (ROOT / "examples" / "bom_example.csv").read_bytes()
    for fmt in ("xlsx", "csv"):
        r = client.post(f"/api/analyze?format={fmt}", files={"file": ("bom.csv", text)})
        assert r.status_code == 200 and len(r.content) > 500


def test_component_crud():
    r = client.post("/api/components", json={"mpn": "TEST-123", "manufacturer": "Acme", "rohs2": "yes", "rohs3": "Compliant", "reach": "SVHC"})
    assert r.status_code == 201, r.text
    comp = r.json()
    assert (comp["rohs2"], comp["rohs3"], comp["reach"]) == ("compliant", "compliant", "svhc")
    assert client.post("/api/components", json={"mpn": "test-123", "manufacturer": "ACME"}).status_code == 422  # duplicate
    assert client.post("/api/components", json={"mpn": "X", "rohs2": "maybe"}).status_code == 422
    r = client.put(f"/api/components/{comp['id']}", json={**comp, "reach": "compliant"})
    assert r.json()["reach"] == "compliant"
    assert client.delete(f"/api/components/{comp['id']}").status_code == 204
    assert client.get(f"/api/components/{comp['id']}").status_code == 404


def test_french_and_legacy_status_values_are_accepted():
    # Files from French-language tools, and databases exported before v2.0.0
    for raw, status in (("oui", "compliant"), ("conforme", "compliant"), ("non conforme", "non_compliant"),
                        ("inconnu", "unknown"), ("Non-compliant", "non_compliant")):
        assert db.clean_status(raw, db.ROHS_STATUSES, "rohs2") == status


def test_rohs2_inferred_from_rohs3():
    with db.session() as conn:
        db.upsert(conn, {"mpn": "INFER-1", "manufacturer": "Acme", "rohs2": "unknown", "rohs3": "compliant", "reach": "compliant"})
    rep = analyze_text("MPN,Manufacturer\nINFER-1,Acme\n")
    line = rep["lines"][0]
    assert line["rohs2"] == "compliant" and line["rohs2_inferred"]


# ---------------------------------------------------------------- PDF datasheets

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


def test_ti_datasheet(monkeypatch):
    monkeypatch.setattr(datasheet_ai, "available", lambda: False)
    r = extract_pdf(TI_PDF, "lm358b.pdf")
    assert r.status_code == 200, r.text
    d = r.json()
    f = d["fields"]
    assert d["method"] == "heuristic"
    assert f["mpn"] == "LM358B" and f["manufacturer"] == "Texas Instruments"
    assert "Operational Amplifiers" in f["description"]
    assert (f["rohs2"], f["rohs3"], f["reach"]) == ("compliant", "compliant", "compliant")
    assert "LM358BIDR" in [c["value"] for c in d["candidates"]["mpn"]]
    assert d["evidence"]["rohs3"]


def test_datasheet_exemption_and_svhc(monkeypatch):
    monkeypatch.setattr(datasheet_ai, "available", lambda: False)
    f = extract_pdf(YAGEO_PDF, "RC0603FR-0710KL.pdf").json()
    assert f["existing_id"]  # already in the demo database
    f = f["fields"]
    assert f["mpn"] == "RC0603FR-0710KL" and f["manufacturer"] == "Yageo"
    assert f["rohs2"] == "exemption" and f["rohs_exemptions"] == "7(c)-I"
    assert f["rohs3"] == "unknown"  # 2015/863 not mentioned
    assert f["reach"] == "svhc" and "7439-92-1" in f["svhc_list"]


def test_datasheet_without_compliance(monkeypatch):
    monkeypatch.setattr(datasheet_ai, "available", lambda: False)
    d = extract_pdf(make_pdf([["AW-2000X Precision Sensor Module", "RoHS-friendly packaging: Pb-free"]]), "s.pdf").json()
    assert d["fields"]["rohs2"] == "unknown"
    assert any("lead-free" in n for n in d["notes"])


def test_datasheet_invalid_file():
    assert extract_pdf(b"not a pdf", "x.pdf").status_code == 422


def test_datasheet_ai_unavailable(monkeypatch):
    monkeypatch.setattr(datasheet_ai, "available", lambda: False)
    assert extract_pdf(TI_PDF, "lm358b.pdf", method="ai").status_code == 400


def test_datasheet_ai_completed_by_heuristic(monkeypatch):
    def fake(data, name):
        return {
            "method": "ai", "model": "test",
            "fields": {"mpn": "LM358BIDR", "manufacturer": "", "description": "Dual op-amp", "rohs2": "compliant",
                       "rohs3": "compliant", "reach": "unknown", "rohs_exemptions": "", "svhc_list": "", "source": "x"},
            "candidates": {"mpn": [{"value": "LM358BIDR"}], "manufacturer": []},
            "evidence": {}, "notes": [], "warnings": [],
        }
    monkeypatch.setattr(datasheet_ai, "available", lambda: True)
    monkeypatch.setattr(datasheet_ai, "extract", fake)
    d = extract_pdf(TI_PDF, "lm358b.pdf").json()
    assert d["method"] == "ai"
    assert d["fields"]["mpn"] == "LM358BIDR"
    assert d["fields"]["manufacturer"] == "Texas Instruments"  # filled in by the heuristic


def test_datasheet_ai_error_falls_back(monkeypatch):
    def boom(data, name):
        raise datasheet_ai.AIExtractionError("quota")
    monkeypatch.setattr(datasheet_ai, "available", lambda: True)
    monkeypatch.setattr(datasheet_ai, "extract", boom)
    d = extract_pdf(TI_PDF, "lm358b.pdf").json()
    assert d["method"] == "heuristic" and any("quota" in w for w in d["warnings"])


# ---------------------------------------------------------------- ECCN and country of origin

import sqlite3  # noqa: E402


def test_eccn_and_country_normalisation():
    assert db.normalize_eccn("5a992.C") == "5A992.c"
    assert db.normalize_eccn("ear 99") == "EAR99"
    assert db.normalize_eccn("") == ""
    assert db.normalize_country("Malaysia") == "MY"
    assert db.normalize_country("Malaisie") == "MY"  # French name
    assert db.normalize_country("united states") == "US"
    assert db.normalize_country("tw") == "TW"
    for bad in ("5Z999", "ECCN"):
        with pytest.raises(db.ValidationError):
            db.normalize_eccn(bad)
    with pytest.raises(db.ValidationError):
        db.normalize_country("Atlantis")


def test_eccn_country_crud():
    r = client.post("/api/components", json={"mpn": "EXP-1", "manufacturer": "Acme", "eccn": "3a991.a.1", "country_of_origin": "Japan"})
    assert r.status_code == 201, r.text
    assert (r.json()["eccn"], r.json()["country_of_origin"]) == ("3A991.a.1", "JP")
    assert client.post("/api/components", json={"mpn": "EXP-2", "eccn": "wrong"}).status_code == 422
    client.delete(f"/api/components/{r.json()['id']}")


def test_import_eccn_country_columns():
    csv_text = "Part Number;Manufacturer;ECCN;Country of Origin\nIMP-ECCN-1;Acme;EAR99;Germany\nIMP-ECCN-2;Acme;5A002;Mars\n"
    with db.session() as conn:
        res = import_table(conn, csv_text.encode(), "base.csv")
        row = conn.execute("SELECT eccn, country_of_origin FROM components WHERE mpn = 'IMP-ECCN-1'").fetchone()
    assert res["created"] == 1 and res["error_count"] == 1  # "Mars" rejected
    assert tuple(row) == ("EAR99", "DE")


def test_existing_database_migration(tmp_path):
    path = tmp_path / "old.db"
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


def test_french_status_values_migrated(tmp_path):
    # Databases created up to v1.1.0 store French statuses; they are converted when opened
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO components (mpn, mpn_norm, mpn_loose, rohs2, rohs3, reach, updated_at) "
                 "VALUES ('V1', 'V1', 'V1', 'conforme', 'non_conforme', 'inconnu', 'x')")
    conn.execute("INSERT INTO users (email, password_hash, created_at) VALUES ('a@example.com', 'x', 'x')")
    conn.execute("INSERT INTO bom_history (user_id, filename, file_data, summary, created_at, updated_at) VALUES "
                 "(1, 'b.csv', x'00', '{\"overall\": \"a_verifier\", \"rohs2\": \"conforme\"}', 'x', 'x')")
    conn.commit()
    conn.close()
    from app import history as history_mod

    with db.session(path) as conn:
        row = conn.execute("SELECT rohs2, rohs3, reach FROM components WHERE mpn = 'V1'").fetchone()
        assert tuple(row) == ("compliant", "non_compliant", "unknown")
        summary = history_mod.list_for(conn, 1)[0]["summary"]
        assert (summary["overall"], summary["rohs2"]) == ("needs_review", "compliant")


def test_analysis_export_summary():
    rep = analyze_text("MPN,Manufacturer\nDEMO-LEADED-DIP8,Example Corp\nDEMO-RELAY-12V,Example Corp\nNE555P,TI\n")
    ex = rep["summary"]["export"]
    assert ex["eccn_controlled"] == 1 and ex["eccn_ear99"] == 2 and ex["eccn_unknown"] == 0
    assert ex["countries"] == {"TW": 1, "CN": 1}
    line = next(l for l in rep["lines"] if l["bom_mpn"] == "DEMO-LEADED-DIP8")
    assert any("5A992.c" in n for n in line["notes"])


def test_datasheet_eccn_country(monkeypatch):
    monkeypatch.setattr(datasheet_ai, "available", lambda: False)
    pdf = make_pdf([["XYZ123A Sensor", "www.ti.com", "ECCN: EAR99", "Country of Origin: Malaysia"]])
    f = extract_pdf(pdf, "xyz123a.pdf").json()["fields"]
    assert (f["eccn"], f["country_of_origin"]) == ("EAR99", "MY")


# ---------------------------------------------------------------- manufacturers

def test_manufacturer_list():
    mfrs = {m["key"]: m for m in client.get("/api/manufacturers").json()}
    ti = mfrs["texas instruments"]
    assert ti["name"] == "Texas Instruments" and ti["website"] == "https://www.ti.com" and ti["country"] == "US"
    assert ti["stats"]["components"] >= 4
    assert mfrs["example"]["website"] == ""  # fictitious manufacturer: no known website


def test_manufacturer_page_groups_aliases():
    client.post("/api/components", json={"mpn": "ALIAS-TEST-1", "manufacturer": "TI"})
    d = client.get("/api/manufacturers/texas instruments").json()
    assert "ALIAS-TEST-1" in [c["mpn"] for c in d["components"]]
    assert "TI" in d["aliases"]
    assert all(c["manufacturer_key"] == "texas instruments" for c in d["components"])
    assert client.get("/api/manufacturers/nonexistent-xyz").status_code == 404


def test_edit_manufacturer():
    r = client.put("/api/manufacturers/example", json={"website": "example.com", "country": "France", "notes": "fictitious"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["website"], d["country"], d["notes"]) == ("https://example.com", "FR", "fictitious")
    assert not d["website_is_default"]
    assert client.put("/api/manufacturers/example", json={"website": "not a website"}).status_code == 422
    # Emptied field: back to the known default value
    d = client.put("/api/manufacturers/murata", json={"website": ""}).json()
    assert d["website"] == "https://www.murata.com" and d["website_is_default"]


# ---------------------------------------------------------------- equivalents and replacements

from app import alternates  # noqa: E402


def comp_id(mpn: str) -> int:
    return client.get("/api/components", params={"q": mpn}).json()["items"][0]["id"]


def test_attribute_extraction():
    a = alternates.extract("Ceramic capacitor 10 µF 25 V X5R 0805")
    assert (a.category, a.package, a.dielectric) == ("capacitor", "0805", "X5R")
    assert a.primary() == pytest.approx(10e-6) and a.max("V") == 25
    m = alternates.extract("MOSFET N-channel 50 V 220 mA, SOT-23")
    assert (m.category, m.channel, m.package) == ("mosfet", "N", "SOT23")
    # Descriptions written in French are understood too
    f = alternates.extract("Condensateur céramique 10 µF 25 V X5R 0805")
    assert (f.category, f.dielectric) == ("capacitor", "X5R")


def test_equivalents_same_value_other_manufacturer():
    alts = client.get(f"/api/components/{comp_id('RC0603FR-0710KL')}/alternatives").json()["alternatives"]
    mpns = [a["component"]["mpn"] for a in alts]
    assert "CRCW060310K0FKEA" in mpns            # same value, same package, other manufacturer
    assert "RC0805FR-07100RL" not in mpns        # same manufacturer and other value


def test_no_automatic_suggestion_for_mcus():
    alts = client.get(f"/api/components/{comp_id('STM32F103C8T6')}/alternatives").json()["alternatives"]
    assert alts == []


def test_compliant_equivalent_for_non_compliant_component():
    alts = client.get(f"/api/components/{comp_id('DEMO-LEADED-DIP8')}/alternatives").json()["alternatives"]
    lm = next(a for a in alts if a["component"]["mpn"] == "LM358DR")
    assert lm["compliant"] and any("package" in d for d in lm["differences"])


def test_alternatives_for_line_not_in_database():
    r = client.get("/api/alternatives", params={"mpn": "XYZ-10K", "manufacturer": "Acme", "description": "Resistor 10 kΩ 1 % 0603"}).json()
    assert {"RC0603FR-0710KL", "CRCW060310K0FKEA"} <= {a["component"]["mpn"] for a in r["alternatives"]}


def test_confirm_equivalence():
    a, b = comp_id("NE555P"), comp_id("L7805CV")
    assert client.post(f"/api/components/{a}/equivalents", json={"other_id": b}).status_code == 204
    alts = client.get(f"/api/components/{b}/alternatives").json()["alternatives"]
    assert alts[0]["component"]["id"] == a and alts[0]["confirmed"]  # symmetric and first
    assert client.delete(f"/api/components/{a}/equivalents/{b}").status_code == 204
    assert all(x["component"]["id"] != a for x in client.get(f"/api/components/{b}/alternatives").json()["alternatives"])
    assert client.post(f"/api/components/{a}/equivalents", json={"other_id": a}).status_code == 422


BOM_SWAP = "Designator;Manufacturer;MPN\nU1;Example Corp;DEMO-LEADED-DIP8\nR1;Yageo;RC0603FR-0710KL\n"


def test_replacement_in_the_bom():
    lm = comp_id("LM358DR")
    rep = analyze_text(BOM_SWAP, substitutions=json.dumps({"2": lm}))
    line = rep["lines"][0]
    assert line["bom_mpn"] == "LM358DR" and line["original_mpn"] == "DEMO-LEADED-DIP8"
    assert line["rohs3"] == "compliant" and rep["summary"]["substituted"] == 1
    assert rep["summary"]["rohs3"]["verdict"] != "non_compliant"


def test_modified_bom_export():
    lm = comp_id("LM358DR")
    r = client.post("/api/analyze?format=bom", files={"file": ("b.csv", BOM_SWAP.encode())},
                    data={"substitutions": json.dumps({"2": lm})})
    lines = r.content.decode("utf-8-sig").splitlines()
    assert lines[0].endswith(";Replaces")
    assert lines[1] == "U1;Texas Instruments;LM358DR;DEMO-LEADED-DIP8 (Example Corp)"
    assert lines[2] == "R1;Yageo;RC0603FR-0710KL;"


def test_invalid_replacement():
    r = client.post("/api/analyze", files={"file": ("b.csv", BOM_SWAP.encode())}, data={"substitutions": '{"99": 1}'})
    assert r.status_code == 422
    r = client.post("/api/analyze", files={"file": ("b.csv", BOM_SWAP.encode())}, data={"substitutions": "not json"})
    assert r.status_code == 400


# ---------------------------------------------------------------- ECCN codes

from app import eccn as eccn_mod  # noqa: E402


def test_eccn_breakdown():
    d = eccn_mod.describe("5A992.c")
    labels = {p["label"]: p for p in d["parts"]}
    assert labels["Category"]["code"] == "5" and "Telecommunications" in labels["Category"]["name"]
    assert labels["Product group"]["code"] == "A"
    assert "unilateral" in labels["Reason for control"]["name"]
    assert labels["Sub-paragraph"]["code"] == ".c"
    assert d["official_heading"].startswith("Equipment not controlled by 5A002")
    assert d["links"][0]["url"].startswith("https://www.ecfr.gov/") and "part-774" in d["links"][0]["url"]
    assert "Wassenaar" in eccn_mod.describe("3A001")["parts"][2]["name"]


def test_ear99():
    d = eccn_mod.describe("EAR99")
    assert not d["controlled"] and "section-734.3" in d["links"][0]["url"]


def test_eccn_api():
    codes = {e["code"]: e for e in client.get("/api/eccn").json()["codes"]}
    assert codes["5A992.c"]["controlled"] and codes["5A992.c"]["components"] >= 1
    assert all(m["key"] and m["name"] for m in codes["5A992.c"]["manufacturer_links"])
    d = client.get("/api/eccn/5a992.C").json()       # normalised code
    assert d["code"] == "5A992.c" and "DEMO-LEADED-DIP8" in [c["mpn"] for c in d["components"]]
    assert sum(m["count"] for m in d["manufacturer_links"]) == len(d["components"])
    assert client.get("/api/eccn/not-a-code").status_code == 422
    r = client.put("/api/eccn/5A992.c", json={"title": "Mass-market encryption", "notes": "ENC"})
    assert r.status_code == 200 and r.json()["title"] == "Mass-market encryption"


# ---------------------------------------------------------------- accounts and history

import uuid  # noqa: E402

from app import auth as auth_mod  # noqa: E402
from app import history as history_mod  # noqa: E402


def new_client_with_account(password="password123"):
    c = TestClient(app)
    email = f"user-{uuid.uuid4().hex[:8]}@example.com"
    r = c.post("/api/auth/register", json={"email": email, "password": password, "name": "Tester"})
    assert r.status_code == 201, r.text
    return c, email


def test_password_hashing():
    h = auth_mod.hash_password("secret-123")
    assert h.startswith("scrypt$") and "secret-123" not in h
    assert auth_mod.verify_password("secret-123", h) and not auth_mod.verify_password("other", h)


def test_register_login_logout():
    c, email = new_client_with_account()
    assert c.get("/api/auth/me").json()["email"] == email          # signed in after registering
    assert c.post("/api/auth/logout").status_code == 204
    assert c.get("/api/auth/me").json() is None
    assert c.post("/api/auth/login", json={"email": email.upper(), "password": "wrong-password"}).status_code == 401
    r = c.post("/api/auth/login", json={"email": email.upper(), "password": "password123"})
    assert r.status_code == 200 and c.get("/api/auth/me").json()["email"] == email
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_invalid_registration():
    c, email = new_client_with_account()
    assert TestClient(app).post("/api/auth/register", json={"email": email, "password": "password123"}).status_code == 409
    assert TestClient(app).post("/api/auth/register", json={"email": "not-an-email", "password": "password123"}).status_code == 422
    assert TestClient(app).post("/api/auth/register", json={"email": "short@example.com", "password": "short"}).status_code == 422


def test_login_throttling():
    _, email = new_client_with_account()
    c = TestClient(app)
    for _ in range(5):
        assert c.post("/api/auth/login", json={"email": email, "password": "wrong"}).status_code == 401
    assert c.post("/api/auth/login", json={"email": email, "password": "password123"}).status_code == 429


# ---------------------------------------------------------------- sign-up confirmed by e-mail, captcha

import re  # noqa: E402
import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402
from collections import defaultdict  # noqa: E402

from app import captcha as captcha_mod  # noqa: E402
from app import mailer as mailer_mod  # noqa: E402
from app import main as main_mod  # noqa: E402

SIGNUP_ENV = ("RRC_MAIL_LOG", "RRC_BREVO_API_KEY", "RRC_MAIL_FROM", "RRC_PUBLIC_URL", "RENDER_EXTERNAL_URL",
              "RRC_TURNSTILE_SITE_KEY", "RRC_TURNSTILE_SECRET_KEY", "RRC_ADMIN_EMAIL", "RRC_DAILY_ADDITIONS")
# Cloudflare's documented test keys and the token their widget produces
TS_SITE, TS_SECRET, TS_TOKEN = "1x00000000000000000000AA", "1x0000000000000000000000000000000AA", "XXXX.DUMMY.TOKEN.XXXX"


@pytest.fixture
def clean_signup_env(monkeypatch):
    for key in SIGNUP_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(main_mod, "_signup_attempts", defaultdict(list))


@pytest.fixture
def mailbox(clean_signup_env, monkeypatch):
    """Sign-up confirmed by e-mail; the messages are captured instead of being sent."""
    sent = []
    monkeypatch.setenv("RRC_MAIL_LOG", "1")
    monkeypatch.setattr(mailer_mod, "send", lambda to, subject, html: sent.append({"to": to, "subject": subject, "html": html}))
    return sent


def link_token(mail) -> str:
    return re.search(r"#confirm/([\w-]+)", mail["html"]).group(1)


def new_email() -> str:
    return f"new-{uuid.uuid4().hex[:8]}@example.com"


def test_signup_settings_default(clean_signup_env):
    cfg = client.get("/api/auth/config").json()
    assert cfg["email_confirmation"] is False and cfg["captcha_site_key"] is None and cfg["edit_requests"] is False


def test_signup_confirmed_by_email(mailbox):
    c, email = TestClient(app), new_email()
    r = c.post("/api/auth/register", json={"email": email.upper(), "password": "password123", "name": "Ann <b>Spam</b>"})
    assert r.status_code == 202 and r.json() == {"pending": True, "email": email, "hours": 24}
    assert "set-cookie" not in r.headers and c.get("/api/auth/me").json() is None
    [mail] = mailbox
    token = link_token(mail)
    assert mail["to"] == email and "Spam" not in mail["html"]             # the typed name never reaches the e-mail
    assert mail["html"].count(f"http://testserver/#confirm/{token}") == 2  # link + address to copy

    # No sign-in before confirmation; a clear message when the password is the one of the sign-up
    assert c.post("/api/auth/login", json={"email": email, "password": "password123"}).status_code == 403
    assert c.post("/api/auth/login", json={"email": email, "password": "another-pass"}).status_code == 401

    assert c.post("/api/auth/confirm", json={"token": token, "password": "another-pass"}).status_code == 400
    r = c.post("/api/auth/confirm", json={"token": token, "password": "password123"})
    assert r.status_code == 200 and r.json()["name"] == "Ann <b>Spam</b>"
    assert c.get("/api/auth/me").json()["email"] == email

    # The link works once, and the address is now taken
    assert TestClient(app).post("/api/auth/confirm", json={"token": token, "password": "password123"}).status_code == 400
    assert TestClient(app).post("/api/auth/register", json={"email": email, "password": "password123"}).status_code == 409


def test_signup_cannot_be_taken_over(mailbox):
    """Someone who signs up first with another person's address never gets the account."""
    email = new_email()
    assert TestClient(app).post("/api/auth/register", json={"email": email, "password": "attacker-pass"}).status_code == 202
    assert TestClient(app).post("/api/auth/register", json={"email": email, "password": "owner-password"}).status_code == 202
    attacker_link, owner_link = (link_token(m) for m in mailbox)

    owner = TestClient(app)
    # The owner opens the wrong e-mail first: their password does not match that attempt
    assert owner.post("/api/auth/confirm", json={"token": attacker_link, "password": "owner-password"}).status_code == 400
    assert owner.post("/api/auth/confirm", json={"token": owner_link, "password": "owner-password"}).status_code == 200
    # The other attempts are dropped: the attacker's password opens nothing
    assert TestClient(app).post("/api/auth/confirm", json={"token": attacker_link, "password": "attacker-pass"}).status_code == 400
    assert TestClient(app).post("/api/auth/login", json={"email": email, "password": "attacker-pass"}).status_code == 401


def test_signup_link_expiry_and_limit_per_address(mailbox):
    email = new_email()
    for _ in range(3):
        assert TestClient(app).post("/api/auth/register", json={"email": email, "password": "password123"}).status_code == 202
    r = TestClient(app).post("/api/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 429 and "mailbox" in r.json()["detail"]

    with db.session() as conn:
        conn.execute("UPDATE signup_requests SET expires_at = '2000-01-01T00:00:00+00:00' WHERE email = ?", (email,))
    r = TestClient(app).post("/api/auth/confirm", json={"token": link_token(mailbox[0]), "password": "password123"})
    assert r.status_code == 400 and "expired" in r.json()["detail"]
    # Expired attempts no longer count
    assert TestClient(app).post("/api/auth/register", json={"email": email, "password": "password123"}).status_code == 202


def test_signup_limit_per_connection(mailbox):
    c = TestClient(app)
    for _ in range(10):
        assert c.post("/api/auth/register", json={"email": new_email(), "password": "password123"}).status_code == 202
    assert c.post("/api/auth/register", json={"email": new_email(), "password": "password123"}).status_code == 429


def test_signup_email_not_sent(mailbox, monkeypatch):
    def fail(*_):
        raise mailer_mod.MailError("Brevo HTTP 401")
    monkeypatch.setattr(mailer_mod, "send", fail)
    email = new_email()
    assert TestClient(app).post("/api/auth/register", json={"email": email, "password": "password123"}).status_code == 502
    with db.session() as conn:  # the attempt is not kept
        assert conn.execute("SELECT COUNT(*) FROM signup_requests WHERE email = ?", (email,)).fetchone()[0] == 0


def test_signup_link_uses_public_address(mailbox, monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://rohs-checker.onrender.com")
    TestClient(app).post("/api/auth/register", json={"email": new_email(), "password": "password123"})
    monkeypatch.setenv("RRC_PUBLIC_URL", "https://bom.example.org/")
    TestClient(app).post("/api/auth/register", json={"email": new_email(), "password": "password123"})
    assert "https://rohs-checker.onrender.com/#confirm/" in mailbox[0]["html"]
    assert "https://bom.example.org/#confirm/" in mailbox[1]["html"]


def test_captcha_on_signup(clean_signup_env, monkeypatch):
    monkeypatch.setenv("RRC_TURNSTILE_SITE_KEY", TS_SITE)
    monkeypatch.setenv("RRC_TURNSTILE_SECRET_KEY", TS_SECRET)
    calls = []

    def fake_siteverify(req, timeout):  # Cloudflare's answer, as with its test keys
        fields = urllib.parse.parse_qs(req.data.decode())
        calls.append((req.full_url, fields))
        ok = fields["response"] == [TS_TOKEN]
        return io.BytesIO(json.dumps({"success": ok, "error-codes": [] if ok else ["invalid-input-response"]}).encode())
    monkeypatch.setattr(captcha_mod.urllib.request, "urlopen", fake_siteverify)

    assert client.get("/api/auth/config").json()["captcha_site_key"] == TS_SITE
    body = {"email": new_email(), "password": "password123"}
    assert TestClient(app).post("/api/auth/register", json=body).status_code == 400            # no token: Cloudflare not called
    assert calls == []
    assert TestClient(app).post("/api/auth/register", json=body | {"captcha": "forged"}).status_code == 400
    c = TestClient(app)
    assert c.post("/api/auth/register", json=body | {"captcha": TS_TOKEN}).status_code == 201
    assert c.get("/api/auth/me").json()["email"] == body["email"]
    url, fields = calls[-1]
    assert url == captcha_mod.SITEVERIFY_URL and fields["secret"] == [TS_SECRET] and fields["remoteip"] == ["testclient"]


def test_brevo_request(clean_signup_env, monkeypatch):
    monkeypatch.setenv("RRC_BREVO_API_KEY", "xkeysib-test")
    monkeypatch.setenv("RRC_MAIL_FROM", "sender@example.org")
    requests = []

    def fake_brevo(req, timeout):
        requests.append(req)
        return io.BytesIO(b'{"messageId": "<1@relay>"}')
    monkeypatch.setattr(mailer_mod.urllib.request, "urlopen", fake_brevo)
    assert mailer_mod.available()
    mailer_mod.send("to@example.com", "Subject", "<p>Hello</p>")
    [req] = requests
    assert req.full_url == "https://api.brevo.com/v3/smtp/email" and req.get_method() == "POST"
    assert req.get_header("Api-key") == "xkeysib-test" and req.get_header("Content-type") == "application/json"
    assert json.loads(req.data) == {"sender": {"name": "RoHS / REACH Checker", "email": "sender@example.org"},
                                    "to": [{"email": "to@example.com"}], "subject": "Subject", "htmlContent": "<p>Hello</p>"}

    def refused(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None, io.BytesIO(b'{"code": "unauthorized"}'))
    monkeypatch.setattr(mailer_mod.urllib.request, "urlopen", refused)
    with pytest.raises(mailer_mod.MailError):
        mailer_mod.send("to@example.com", "Subject", "<p>Hello</p>")


# ---------------------------------------------------------------- edit access, approved by the administrator

from app import editors as editors_mod  # noqa: E402

ADMIN = "admin@example.org"


@pytest.fixture
def protected(mailbox, monkeypatch):
    """Database changes reserved for approved accounts; e-mails captured in the mailbox."""
    monkeypatch.delenv("RRC_OPEN_EDITING", raising=False)
    monkeypatch.setenv("RRC_ADMIN_EMAIL", ADMIN)
    return mailbox


def signed_in(email=None, password="password123"):
    email = email or new_email()
    with db.session() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            auth_mod.create_user(conn, email, password)
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"email": email, "password": password}).status_code == 200
    return c, email


def review_token(mail) -> str:
    return re.search(r"#review/([\w-]+)", mail["html"]).group(1)


def test_database_changes_need_an_approved_account(protected):
    anonymous = TestClient(app)
    assert anonymous.post("/api/components", json={"mpn": "EDIT-ANON"}).status_code == 401
    assert anonymous.put("/api/exemptions/7(c)-I", json={"notes": "x"}).status_code == 401
    assert anonymous.get("/api/components?q=NE555P").status_code == 200       # browsing stays open
    assert analyze_text("MPN,Manufacturer\nNE555P,TI\n")["summary"]["found"] == 1

    c, _ = signed_in()
    r = c.post("/api/components", json={"mpn": "EDIT-USER"})
    assert r.status_code == 403 and "request edit access" in r.json()["detail"]
    me = c.get("/api/auth/me").json()
    assert me["can_edit"] is False and me["edit_access"] == "" and me["is_admin"] is False
    assert c.get("/api/auth/config").json()["edit_requests"] is True


def test_edit_access_request_and_approval(protected):
    c, email = signed_in()
    r = c.post("/api/edit-access/request")
    assert r.status_code == 200 and r.json()["edit_access"] == "pending"
    [mail] = protected
    token = review_token(mail)
    assert mail["to"] == ADMIN and email in mail["html"] and f"http://testserver/#review/{token}" in mail["html"]
    assert c.post("/api/edit-access/request").status_code == 409                # one pending request at a time

    reviewer = TestClient(app)  # the link is enough: the administrator does not need to be signed in
    assert reviewer.post("/api/edit-access/lookup", json={"token": "forged"}).status_code == 404
    assert reviewer.post("/api/edit-access/lookup", json={"token": token}).json()["email"] == email
    r = reviewer.post("/api/edit-access/review", json={"token": token, "approve": True})
    assert r.json() == {"email": email, "edit_access": "approved"}
    assert protected[-1]["to"] == email and "approved" in protected[-1]["html"]  # the user is told
    assert reviewer.post("/api/edit-access/review", json={"token": token, "approve": False}).status_code == 404

    me = c.get("/api/auth/me").json()
    assert me["can_edit"] is True and me["additions_left"] == 20
    created = c.post("/api/components", json={"mpn": "EDIT-APPROVED-1", "manufacturer": "Acme"})
    assert created.status_code == 201
    assert c.put(f"/api/components/{created.json()['id']}", json={"mpn": "EDIT-APPROVED-1", "manufacturer": "Acme",
                                                                   "description": "edited"}).status_code == 200
    r = c.delete(f"/api/components/{created.json()['id']}")
    assert r.status_code == 403 and "administrator" in r.json()["detail"]


def test_edit_access_refused(protected):
    c, email = signed_in()
    c.post("/api/edit-access/request")
    TestClient(app).post("/api/edit-access/review", json={"token": review_token(protected[0]), "approve": False})
    assert "not approved" in protected[-1]["html"]
    assert c.get("/api/auth/me").json()["edit_access"] == "refused"
    assert c.post("/api/components", json={"mpn": "EDIT-REFUSED"}).status_code == 403
    r = c.post("/api/edit-access/request")
    assert r.status_code == 409 and "tomorrow" in r.json()["detail"]
    with db.session() as conn:  # one request per day
        conn.execute("UPDATE users SET edit_requested_at = '2000-01-01T00:00:00+00:00' WHERE email = ?", (email,))
    assert c.post("/api/edit-access/request").status_code == 200


def test_administrator(protected):
    c, _ = signed_in(ADMIN.upper())
    me = c.get("/api/auth/me").json()
    assert me["is_admin"] is True and me["can_edit"] is True and me["additions_left"] is None
    assert c.post("/api/edit-access/request").status_code == 409
    comp = c.post("/api/components", json={"mpn": "EDIT-ADMIN-1", "manufacturer": "Acme"}).json()
    assert c.delete(f"/api/components/{comp['id']}").status_code == 204


def test_daily_limit_on_added_components(protected, monkeypatch):
    monkeypatch.setenv("RRC_DAILY_ADDITIONS", "3")
    c, email = signed_in()
    with db.session() as conn:
        assert editors_mod.set_access(conn, email, approved=True)        # as with "python cli.py approve"
    for i in range(3):
        assert c.post("/api/components", json={"mpn": f"LIMIT-{email}-{i}"}).status_code == 201
    r = c.post("/api/components", json={"mpn": f"LIMIT-{email}-3"})
    assert r.status_code == 429 and "Daily limit" in r.json()["detail"]
    assert c.get(f"/api/components?q=LIMIT-{email}-3").json()["total"] == 0  # nothing kept

    existing = c.get(f"/api/components?q=LIMIT-{email}-0").json()["items"][0]
    assert c.put(f"/api/components/{existing['id']}", json={"mpn": existing["mpn"], "description": "edit"}).status_code == 200

    csv = f"mpn,manufacturer\nLIMIT-{email}-A,Acme\nLIMIT-{email}-B,Acme\n".encode()
    r = c.post("/api/components/import", files={"file": ("new.csv", csv)})
    assert r.status_code == 429 and "you can add 0 more" in r.json()["detail"]
    assert c.get(f"/api/components?q=LIMIT-{email}-A").json()["total"] == 0  # the whole import is cancelled

    with db.session() as conn:  # additions older than 24 hours no longer count
        conn.execute("UPDATE additions SET created_at = '2000-01-01T00:00:00+00:00'")
    r = c.post("/api/components/import", files={"file": ("new.csv", csv)})
    assert r.status_code == 200 and r.json()["created"] == 2
    assert c.get("/api/auth/me").json()["additions_left"] == 1


def test_edit_access_requests_not_set_up(clean_signup_env, monkeypatch):
    monkeypatch.delenv("RRC_OPEN_EDITING", raising=False)
    c, _ = signed_in()
    assert c.post("/api/edit-access/request").status_code == 503
    assert c.post("/api/components", json={"mpn": "EDIT-NO-ADMIN"}).status_code == 403


def test_users_of_older_databases_get_edit_columns(tmp_path):
    path = tmp_path / "old.db"
    import sqlite3
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, "
                "name TEXT NOT NULL DEFAULT '', password_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
    old.execute("INSERT INTO users (email, password_hash, created_at) VALUES ('old@example.com', 'x', '2026-01-01')")
    old.commit()
    old.close()
    with db.session(path) as conn:
        row = conn.execute("SELECT * FROM users").fetchone()
        assert row["edit_access"] == "" and row["edit_token_hash"] is None


def test_history_saved_and_isolated():
    c, _ = new_client_with_account()
    bom = "MPN,Manufacturer\nNE555P,TI\nDEMO-LEADED-DIP8,Example Corp\n"
    r = c.post("/api/analyze", files={"file": ("my_board.csv", bom.encode())}, data={"save": "1"}).json()
    hid = r["history_id"]
    items = c.get("/api/history").json()
    assert [i["filename"] for i in items] == ["my_board.csv"] and items[0]["summary"]["overall"] == "non_compliant"
    assert c.get(f"/api/history/{hid}/file").content == bom.encode()

    # The same entry is updated (replacement) rather than a new one created
    lm = comp_id("LM358DR")
    r2 = c.post("/api/analyze", files={"file": ("my_board.csv", bom.encode())},
                data={"history_id": str(hid), "substitutions": json.dumps({"3": lm})}).json()
    assert r2["history_id"] == hid
    items = c.get("/api/history").json()
    assert len(items) == 1 and items[0]["substitutions"] == {"3": lm} and items[0]["summary"]["substituted"] == 1

    # Another user does not see this BOM
    other, _ = new_client_with_account()
    assert other.get("/api/history").json() == []
    assert other.get(f"/api/history/{hid}/file").status_code == 404
    assert other.delete(f"/api/history/{hid}").status_code == 404

    assert c.delete(f"/api/history/{hid}").status_code == 204
    assert c.get("/api/history").json() == []


def test_history_without_sign_in():
    anon = TestClient(app)
    r = anon.post("/api/analyze", files={"file": ("b.csv", b"MPN\nNE555P\n")}, data={"save": "1"}).json()
    assert "history_id" not in r
    assert anon.get("/api/history").status_code == 401


def test_history_limit():
    c, _ = new_client_with_account()
    for i in range(history_mod.MAX_PER_USER + 3):
        c.post("/api/analyze", files={"file": (f"bom{i}.csv", b"MPN\nNE555P\n")}, data={"save": "1"})
    items = c.get("/api/history").json()
    assert len(items) == history_mod.MAX_PER_USER
    assert items[0]["filename"] == f"bom{history_mod.MAX_PER_USER + 2}.csv"   # most recent first


# ---------------------------------------------------------------- RoHS exemptions

from app import exemptions as ex_mod  # noqa: E402


def test_exemption_code_normalisation():
    assert ex_mod.normalize_key("7(c)-I") == ex_mod.normalize_key("7 c)-I") == ex_mod.normalize_key("7c-I") == "7C-I"
    assert ex_mod.normalize_key("Annex IV 37") == ex_mod.normalize_key("Annexe IV 37") == ex_mod.normalize_key("IV-37") == "IV-37"
    assert ex_mod.split_codes("7(a), 7(c)-I; 6(c)") == ["7(a)", "7(c)-I", "6(c)"]


def test_official_exemption_text():
    d = ex_mod.describe("7(c)-I")
    assert d["found"] and d["annex"] == "III" and d["code"] == "7(c)-I"
    assert "lead in a glass" in d["description"] and d["substances"] == ["lead"]
    assert d["dates"] and all(len(x) == 10 for x in d["dates"])
    assert d["links"][0]["url"].startswith("https://eur-lex.europa.eu/legal-content/EN/")
    assert not ex_mod.describe("99z")["found"]
    assert ex_mod.describe("IV-1")["annex"] == "IV"


def test_expiry_status(monkeypatch):
    assert ex_mod._status([])["kind"] == "no_date"
    assert ex_mod._status(["2000-01-01"])["kind"] == "expired"
    assert ex_mod._status(["2999-01-01"])["kind"] == "in_force"


def test_exemptions_api():
    rows = {e["key"]: e for e in client.get("/api/exemptions").json()["exemptions"]}
    assert rows["7C-I"]["components"] >= 2 and "Yageo" in rows["7C-I"]["manufacturers"]
    # Link to the manufacturer's page
    assert any(m["key"] == "yageo" and m["name"] == "Yageo" for m in rows["7C-I"]["manufacturer_links"])
    d = client.get("/api/exemptions/7(c)-I").json()
    assert "RC0603FR-0710KL" in [c["mpn"] for c in d["components"]]
    yageo = next(m for m in d["manufacturer_links"] if m["key"] == "yageo")
    assert yageo["name"] == "Yageo" and yageo["count"] >= 1
    r = client.put("/api/exemptions/7(c)-I", json={"notes": "Renewal requested by the manufacturer"})
    assert r.status_code == 200 and r.json()["notes"].startswith("Renewal")


# ------------------------------------------------------------------ manual column mapping (#15)

UNUSUAL_BOM = (
    "Sensor board bill of materials;;\n"
    "Revision B;;\n"
    "Supplier item code;Company;Designation\n"
    "GRM188R71H104KA93D;Murata;Capacitor 100 nF\n"
    "RC0603FR-0710KL;Yageo;Resistor 10 k\n"
)


def preview(text: str, kind="bom", mapping=None, name="file.csv"):
    data = {"mapping": json.dumps(mapping)} if mapping is not None else {}
    return client.post(f"/api/table/preview?kind={kind}", files={"file": (name, text.encode("utf-8"))}, data=data)


def test_unknown_bom_headers_report_a_column_error():
    r = client.post("/api/analyze", files={"file": ("bom.csv", UNUSUAL_BOM.encode())})
    assert r.status_code == 422
    assert r.headers["X-Error-Kind"] == "columns"  # the interface opens the column mapping dialog
    assert "column mapping" in r.json()["detail"]


def test_bom_preview_offers_fields_and_flags_missing_mpn():
    r = preview(UNUSUAL_BOM, mapping={"_header_row": 3})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["header_row"] == 3 and p["total_rows"] == 5
    assert p["headers"] == ["Supplier item code", "Company", "Designation"]
    assert p["missing"] == ["mpn"]
    assert [f["key"] for f in p["fields"] if f["required"]] == ["mpn"]
    assert p["rows"][3] == ["GRM188R71H104KA93D", "Murata", "Capacitor 100 nF"]


def test_bom_analysed_with_manual_mapping():
    mapping = {"_header_row": 3, "mpn": "Supplier item code", "manufacturer": "Company", "description": "Designation"}
    rep = analyze_text(UNUSUAL_BOM, mapping=json.dumps(mapping))
    assert rep["mapping"]["header_row"] == 3
    assert rep["mapping"]["columns"]["mpn"] == "Supplier item code"
    by_mpn = {l["bom_mpn"]: l for l in rep["lines"]}
    assert by_mpn["GRM188R71H104KA93D"]["component"]["manufacturer"] == "Murata"
    assert by_mpn["RC0603FR-0710KL"]["line"] == 5  # file row numbers kept


def test_bom_without_header_row():
    text = "GRM188R71H104KA93D;Murata;2\nRC0603FR-0710KL;Yageo;4\n"
    p = preview(text, mapping={"_header_row": 0}).json()
    assert p["header_row"] == 0 and p["headers"] == ["Column 1", "Column 2", "Column 3"]
    mapping = {"_header_row": 0, "mpn": "Column 1", "manufacturer": "Column 2", "quantity": "Column 3"}
    rep = analyze_text(text, mapping=json.dumps(mapping))
    assert [l["line"] for l in rep["lines"]] == [1, 2]
    # Modified BOM: a header row is added
    r = client.post("/api/analyze?format=bom", files={"file": ("bom.csv", text.encode())}, data={"mapping": json.dumps(mapping)})
    assert r.content.decode("utf-8-sig").splitlines()[0] == "Column 1;Column 2;Column 3;Replaces"


def test_empty_or_duplicate_columns_get_a_unique_name():
    text = "Ref;;Ref\nGRM188R71H104KA93D;x;y\n"
    p = preview(text, mapping={"_header_row": 1}).json()
    assert p["headers"] == ["Ref", "Column 2", "Ref (column 3)"]
    rep = analyze_text(text, mapping=json.dumps({"_header_row": 1, "mpn": "Ref (column 3)"}))
    assert rep["lines"][0]["bom_mpn"] == "y"


def test_invalid_mapping():
    files = {"file": ("bom.csv", UNUSUAL_BOM.encode())}
    assert client.post("/api/analyze", files=files, data={"mapping": "[1, 2]"}).status_code == 400
    r = client.post("/api/analyze", files=files, data={"mapping": json.dumps({"colour": "Company"})})
    assert r.status_code == 422 and "Unknown field" in r.json()["detail"]
    r = client.post("/api/analyze", files=files, data={"mapping": json.dumps({"_header_row": 99})})
    assert r.status_code == 422 and "outside the file" in r.json()["detail"]
    r = client.post("/api/analyze", files=files, data={"mapping": json.dumps({"_header_row": 3, "mpn": "Nonexistent"})})
    assert r.status_code == 422 and "not found" in r.json()["detail"]


def test_a_detected_field_can_be_ignored():
    text = "MPN;Manufacturer\nGRM188R71H104KA93D;Wrong Manufacturer\n"
    rep = analyze_text(text, mapping=json.dumps({"manufacturer": "__none__"}))
    assert "manufacturer" not in rep["mapping"]["columns"]
    assert rep["lines"][0]["bom_manufacturer"] == ""


def test_database_import_with_manual_mapping():
    text = "ERP export of 2026-11-08\nItem code;Company;RoHS 3 status;Classification\nMAP-IMP-1;Acme;compliant;EAR99\n"
    p = preview(text, kind="components").json()
    assert p["header_row"] == 1 and p["missing"] == ["mpn"]  # no recognised header: everything must be selected
    files = {"file": ("erp.csv", text.encode())}
    r = client.post("/api/components/import", files=files)
    assert r.status_code == 422 and r.headers["X-Error-Kind"] == "columns"
    mapping = {"_header_row": 2, "mpn": "Item code", "manufacturer": "Company", "rohs3": "RoHS 3 status", "eccn": "Classification"}
    r = client.post("/api/components/import", files=files, data={"mapping": json.dumps(mapping)})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1 and r.json()["error_count"] == 0
    comp = client.get("/api/components?q=MAP-IMP-1").json()["items"][0]
    assert (comp["manufacturer"], comp["rohs3"], comp["eccn"]) == ("Acme", "compliant", "EAR99")
    client.delete(f"/api/components/{comp['id']}")


def test_database_import_detects_header_after_a_title():
    text = "Component database\nmpn;manufacturer;rohs2\nMAP-IMP-2;Acme;compliant\n"
    p = preview(text, kind="components").json()
    assert p["header_row"] == 2 and p["columns"] == {"mpn": "mpn", "manufacturer": "manufacturer", "rohs2": "rohs2"}
    with db.session() as conn:
        assert import_table(conn, text.encode(), "base.csv")["created"] == 1
        conn.execute("DELETE FROM components WHERE mpn = 'MAP-IMP-2'")


# ------------------------------------------------------------------ preview of the result and of the changes

def test_bom_preview_shows_lines_read_and_database_presence():
    mapping = {"_header_row": 3, "mpn": "Supplier item code", "manufacturer": "Company"}
    p = preview(UNUSUAL_BOM, mapping=mapping).json()
    assert p["detected"] == {"description": "Designation"}  # proposed automatically; mpn and manufacturer chosen by hand
    r = p["result"]
    assert r["counts"] == {"lines": 2, "in_base": 2, "dnp": 0}
    assert r["lines"][0] == {"line": 4, "values": {"mpn": "GRM188R71H104KA93D", "manufacturer": "Murata", "description": "Capacitor 100 nF"}, "in_base": True}


def test_bom_preview_without_mpn_gives_no_result():
    p = preview(UNUSUAL_BOM, mapping={"_header_row": 3}).json()
    assert p["result"]["lines"] == [] and p["result"]["counts"]["lines"] == 0


def test_import_preview_simulates_creations_changes_and_errors():
    with db.session() as conn:
        import_table(conn, b"mpn;manufacturer;rohs2;rohs3;eccn\nSIM-1;Acme;compliant;compliant;EAR99\nSIM-2;Acme;compliant;compliant;\n", "base.csv")
    text = "Code;Company;RoHS3\nSIM-1;Acme;non-compliant\nSIM-2;Acme;compliant\nSIM-3;Acme;yes\nSIM-4;Acme;maybe\n"
    mapping = {"mpn": "Code", "manufacturer": "Company", "rohs3": "RoHS3"}
    p = preview(text, kind="components", mapping=mapping).json()
    r = p["result"]
    assert r["counts"] == {"create": 1, "update": 1, "unchanged": 1, "error": 1}
    by_line = {l["line"]: l for l in r["lines"]}
    assert by_line[2]["action"] == "update" and by_line[2]["changes"] == {"rohs3": ["compliant", "non_compliant"]}
    assert by_line[3]["action"] == "unchanged"
    assert by_line[4]["action"] == "create" and by_line[4]["values"]["rohs3"] == "compliant"
    assert by_line[5]["action"] == "error" and "rohs3" in by_line[5]["error"]
    with db.session() as conn:  # the preview writes nothing
        assert conn.execute("SELECT rohs3 FROM components WHERE mpn = 'SIM-1'").fetchone()[0] == "compliant"
        assert conn.execute("SELECT COUNT(*) FROM components WHERE mpn = 'SIM-3'").fetchone()[0] == 0


def test_import_only_changes_fields_mapped_to_a_column():
    text = "Code;Company;RoHS3\nSIM-1;Acme;non-compliant\nSIM-2;Acme;compliant\n"
    mapping = {"mpn": "Code", "manufacturer": "Company", "rohs3": "RoHS3"}
    r = client.post("/api/components/import", files={"file": ("update.csv", text.encode())}, data={"mapping": json.dumps(mapping)})
    assert r.status_code == 200, r.text
    assert (r.json()["updated"], r.json()["unchanged"], r.json()["created"]) == (1, 1, 0)
    with db.session() as conn:
        row = conn.execute("SELECT rohs2, rohs3, eccn FROM components WHERE mpn = 'SIM-1'").fetchone()
        assert tuple(row) == ("compliant", "non_compliant", "EAR99")  # RoHS 2 and ECCN kept
        conn.execute("DELETE FROM components WHERE mpn LIKE 'SIM-%'")


def test_import_empty_cell_does_not_replace_a_value():
    with db.session() as conn:
        import_table(conn, b"mpn;manufacturer;rohs3;eccn\nSIM-V;Acme;compliant;EAR99\n", "base.csv")
    text = "mpn;manufacturer;rohs3;eccn\nSIM-V;Acme;;5A992.c\n"
    line = preview(text, kind="components").json()["result"]["lines"][0]
    assert line["changes"] == {"eccn": ["EAR99", "5A992.c"]} and line["values"]["rohs3"] == "compliant"
    with db.session() as conn:
        import_table(conn, text.encode(), "update.csv")
        row = conn.execute("SELECT rohs3, eccn FROM components WHERE mpn = 'SIM-V'").fetchone()
        assert tuple(row) == ("compliant", "5A992.c")
        conn.execute("DELETE FROM components WHERE mpn = 'SIM-V'")


# ------------------------------------------------------------------ RoHS / REACH statuses declared in the BOM

def test_bom_rohs_and_reach_column_detection():
    from app.tables import detect_columns

    def detected(header):
        rows = [header, ["X"] * len(header)]
        return {role: header[i] for role, i in detect_columns(rows).columns.items()}

    assert detected(["MPN", "RoHS 2", "RoHS 3", "REACH"]) == {"mpn": "MPN", "rohs2": "RoHS 2", "rohs3": "RoHS 3", "reach": "REACH"}
    # "RoHS" alone means RoHS 3 (directive in force), and does not take the "RoHS 2" column
    assert detected(["Part Number", "RoHS Status", "REACH SVHC"]) == {"mpn": "Part Number", "rohs3": "RoHS Status", "reach": "REACH SVHC"}
    assert detected(["MPN", "RoHS 2"]) == {"mpn": "MPN", "rohs2": "RoHS 2"}
    fields = [f["key"] for f in preview("MPN;RoHS\nA;yes\n").json()["fields"]]
    assert {"rohs2", "rohs3", "reach"} <= set(fields)


BOM_STATUSES = (
    "MPN;Manufacturer;RoHS 2;RoHS 3;REACH\n"
    "ZZ-ABSENT-1;Acme;yes;yes;no\n"               # not in the database: statuses taken from the BOM
    "CC0603KRX7R9BB104;Yageo;;;yes\n"            # REACH unknown in the database: completed by the BOM
    "GRM188R71H104KA93D;Murata;yes;no;yes\n"     # RoHS 3: disagrees with the database
    "ZZ-ABSENT-2;Acme;maybe;;\n"                 # unrecognised value
)


def test_analysis_uses_statuses_declared_in_the_bom():
    lines = {l["bom_mpn"]: l for l in analyze_text(BOM_STATUSES)["lines"]}

    absent = lines["ZZ-ABSENT-1"]
    assert (absent["rohs2"], absent["rohs3"], absent["reach"]) == ("compliant", "compliant", "non_compliant")
    assert absent["from_bom"] == ["rohs2", "rohs3", "reach"]
    assert any("Statuses taken from the BOM" in n for n in absent["notes"])

    completed = lines["CC0603KRX7R9BB104"]
    assert completed["reach"] == "compliant" and completed["from_bom"] == ["reach"]
    assert completed["rohs3"] == "compliant"  # comes from the database

    conflict = lines["GRM188R71H104KA93D"]
    assert conflict["rohs3"] == "needs_review" and conflict["from_bom"] == []
    assert any("the BOM declares “non-compliant”, the database says “compliant”" in n for n in conflict["notes"])
    assert conflict["rohs2"] == "compliant" and conflict["reach"] == "compliant"  # BOM and database agree

    invalid = lines["ZZ-ABSENT-2"]
    assert invalid["rohs2"] == "needs_review" and invalid["from_bom"] == []
    assert any("Unrecognised" in n for n in invalid["notes"])


def test_bom_statuses_in_preview_and_report():
    p = preview(BOM_STATUSES).json()
    assert p["result"]["lines"][0]["values"]["reach"] == "non_compliant"  # normalised for display
    assert p["result"]["lines"][3]["values"]["rohs2"] == "maybe"           # unrecognised: shown as is
    r = client.post("/api/analyze?format=csv", files={"file": ("bom.csv", BOM_STATUSES.encode())})
    assert "Compliant (from the BOM)" in r.content.decode("utf-8-sig")


def test_bom_without_status_columns_unchanged():
    lines = analyze_text("MPN;Manufacturer\nZZ-ABSENT-1;Acme\n")["lines"]
    assert (lines[0]["rohs3"], lines[0]["from_bom"]) == ("needs_review", [])


def test_distributor_status_wording_is_understood():
    # Longer values written by distributors and PLM tools
    rohs, reach = db.ROHS_STATUSES, db.REACH_STATUSES
    for raw, allowed, status in (
        ("Compliant(YES)_", rohs, "compliant"), ("Compliant(YES)_", reach, "compliant"),
        ("Non-Compliant(NO)_", rohs, "non_compliant"), ("ROHS3 Compliant", rohs, "compliant"),
        ("RoHS Compliant By Exemption", rohs, "exemption"), ("Compliant, no exemption", rohs, "compliant"),
        ("Not RoHS Compliant", rohs, "non_compliant"), ("REACH Unaffected", reach, "compliant"),
        ("Contains SVHC > 0.1%", reach, "svhc"), ("SVHC free", reach, "compliant"),
        ("Vendor undefined", rohs, "unknown"), ("Not applicable", reach, "unknown"),
    ):
        assert db.clean_status(raw, allowed, "status") == status, raw
    with pytest.raises(db.ValidationError):
        db.clean_status("Pb-free", rohs, "rohs3")


def test_xlsb_bom_with_long_headers():
    data = (ROOT / "tests" / "data" / "bom_long_headers.xlsb").read_bytes()
    r = client.post("/api/analyze", files={"file": ("bom.xlsb", data)})
    assert r.status_code == 200, r.text
    xlsb_lines = r.json()["lines"]
    lines = {line["bom_mpn"]: line for line in xlsb_lines}
    assert lines["NE555P"]["match"] == "exact"
    absent = lines["XYZ-UNKNOWN-42"]  # not in the database: statuses "Compliant(YES)_" taken from the BOM
    assert (absent["rohs3"], absent["reach"], absent["from_bom"]) == ("compliant", "compliant", ["rohs3", "reach"])
    old = lines["ABC-OLD-7"]
    assert (old["rohs3"], old["reach"]) == ("non_compliant", "svhc")
    # The same table saved as CSV gives the same result
    csv_lines = analyze_text((ROOT / "tests" / "data" / "bom_long_headers.csv").read_text(encoding="utf-8"))["lines"]
    assert [(x["bom_mpn"], x["rohs3"], x["reach"]) for x in csv_lines] == \
        [(x["bom_mpn"], x["rohs3"], x["reach"]) for x in xlsb_lines]
