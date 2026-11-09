"""Command-line tool.

Examples:
  python cli.py seed                               # load the demo data
  python cli.py import my_database.xlsx            # import / update the local database
  python cli.py analyze bom.csv                    # analyse with the local database
  python cli.py analyze bom.xlsx -o report.xlsx    # + Excel report
  python cli.py analyze bom.csv --server http://srv:8000   # analyse through the remote server
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.analyzer import analyze  # noqa: E402
from app.reports import LABELS, report_csv, report_xlsx  # noqa: E402
from app.tables import TableError, parse_bom  # noqa: E402

COLORS = {"compliant": "32", "exemption": "36", "svhc": "33", "non_compliant": "31", "needs_review": "33", "dnp": "90"}


def color(status: str, text: str | None = None) -> str:
    text = text or LABELS.get(status, status)
    if not sys.stdout.isatty():
        return text
    return f"\033[{COLORS.get(status, '0')}m{text}\033[0m"


def cmd_seed(args):
    from app.importer import import_table

    data = (ROOT / "data" / "seed_components.csv").read_bytes()
    with db.session() as conn:
        res = import_table(conn, data, "seed_components.csv")
    print(f"Demo data: {res['created']} created, {res['updated']} updated")


def cmd_import(args):
    from app.importer import import_table

    path = Path(args.file)
    if args.server:
        import httpx

        headers = {"X-Admin-Token": args.token} if args.token else {}
        r = httpx.post(f"{args.server.rstrip('/')}/api/components/import", files={"file": (path.name, path.read_bytes())}, headers=headers, timeout=120)
        r.raise_for_status()
        res = r.json()
    else:
        with db.session() as conn:
            res = import_table(conn, path.read_bytes(), path.name)
    print(f"{res['created']} created, {res['updated']} updated, {res['error_count']} errors")
    for e in res["errors"]:
        print("  -", e)


def cmd_analyze(args):
    path = Path(args.file)
    data = path.read_bytes()
    mapping = json.loads(args.mapping) if args.mapping else None
    if args.server:
        import httpx

        r = httpx.post(
            f"{args.server.rstrip('/')}/api/analyze",
            files={"file": (path.name, data)},
            data={"mapping": args.mapping or ""},
            timeout=120,
        )
        if r.status_code != 200:
            sys.exit(f"Server error ({r.status_code}): {r.text}")
        report = r.json()
    else:
        try:
            lines, colmap = parse_bom(data, path.name, mapping)
        except TableError as exc:
            sys.exit(f"Error: {exc}")
        with db.session() as conn:
            report = analyze(conn, lines, colmap, path.name)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)

    if args.output:
        out = Path(args.output)
        out.write_bytes(report_xlsx(report) if out.suffix.lower() == ".xlsx" else report_csv(report))
        print(f"\nReport saved: {out}")

    # Exit code usable in continuous integration: 0 compliant, 1 needs review, 2 non-compliant
    overall = report["summary"]["overall"]
    sys.exit({"compliant": 0, "needs_review": 1}.get(overall, 2))


def print_report(report: dict):
    s = report["summary"]
    m = report.get("mapping") or {}
    print(f"BOM: {report['filename']}  ({s['lines']} lines, {s['dnp']} not populated)")
    if m:
        print("Detected columns: " + ", ".join(f"{k}={v}" for k, v in m["columns"].items()))
    print(f"Found: {s['found']}   Uncertain: {s['uncertain']}   Not found: {s['not_found']}\n")
    w = max([len(l["bom_mpn"]) for l in report["lines"]] + [11])
    print(f"{'Line':>5}  {'Part number':<{w}}  {'RoHS 2':<22}{'RoHS 3':<22}{'REACH':<16}Notes")
    for l in report["lines"]:
        cells = "".join(color(l[n]) + " " * max(1, pad - len(LABELS.get(l[n], l[n]))) for n, pad in (("rohs2", 22), ("rohs3", 22), ("reach", 16)))
        notes = list(l["notes"])
        if l["suggestions"] and l["match"] == "not_found":
            notes.append("Suggestions: " + ", ".join(x["mpn"] for x in l["suggestions"]))
        print(f"{l['line']:>5}  {l['bom_mpn']:<{w}}  {cells}{' | '.join(notes)}")
    print("\nBOM verdict:")
    for n, label in (("rohs2", "RoHS 2"), ("rohs3", "RoHS 3"), ("reach", "REACH ")):
        print(f"  {label}: {color(s[n]['verdict'])}")
    print(f"  Overall: {color(s['overall'])}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="RoHS 2 / RoHS 3 / REACH compliance checker for a BOM")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("seed", help="Load the demo data into the local database").set_defaults(func=cmd_seed)

    pi = sub.add_parser("import", help="Import a CSV/XLSX file into the database")
    pi.add_argument("file")
    pi.add_argument("--server", help="Server URL (otherwise the local database)")
    pi.add_argument("--token", help="Server admin token")
    pi.set_defaults(func=cmd_import)

    pa = sub.add_parser("analyze", help="Analyse a BOM")
    pa.add_argument("file")
    pa.add_argument("-o", "--output", help="Report file (.xlsx or .csv)")
    pa.add_argument("--server", help="Server URL (otherwise the local database)")
    pa.add_argument("--mapping", help='Forced columns, JSON: \'{"mpn": "Part Number"}\'')
    pa.add_argument("--json", action="store_true", help="Raw JSON output")
    pa.set_defaults(func=cmd_analyze)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
