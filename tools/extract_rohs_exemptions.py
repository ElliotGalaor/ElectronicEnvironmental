"""Extract the RoHS exemptions (Annexes III and IV of Directive 2011/65/EU) from EUR-Lex.

Usage:
    python tools/extract_rohs_exemptions.py <current version YYYYMMDD> [<future version YYYYMMDD>]

The available consolidated versions are listed on
https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:32011L0065
The result is written to data/rohs_exemptions.json (official English text).
"""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.request
from pathlib import Path

URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:02011L0065-{v}"
MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
          "september": 9, "october": 10, "november": 11, "december": 12}
DATE_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", re.I)
CODE_RE = re.compile(r"^(\d{1,2})(?:\s?([a-z])\))?(?:\s?\(?([a-z])\))?(?:-([IVX]+))?$")


def fetch(version: str) -> str:
    req = urllib.request.Request(URL.format(v=version), headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read().decode("utf-8")


def clean(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text).replace(" ", " ")  # non-breaking spaces
    text = re.sub(r"▼\w+|►\w+|◄", " ", text)      # EUR-Lex amendment markers
    text = re.sub(r"\(\s\d{1,3}\s\)", " ", text)   # footnote references "( 4 )"
    return re.sub(r"\s+", " ", text).strip()


def key_of(code: str, annex: str) -> str:
    k = re.sub(r"[\s().]", "", code).upper()
    return ("IV-" + k) if annex == "IV" else k


def dates_in(text: str) -> list[str]:
    return sorted({f"{int(y):04d}-{MONTHS[m.lower()]:02d}-{int(d):02d}" for d, m, y in DATE_RE.findall(text)})


def parse_annex_iii(seg: str) -> dict:
    out, parent = {}, None
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S):
        cells = [clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 2:
            continue
        # The English text writes some codes as "4(f) -I" or "9(b)-(I)": bring them back to "4(f)-I".
        code, desc = re.sub(r"\s*-\s*\(?([IVX]+)\)?$", r"-\1", cells[0]), cells[1]
        scope = cells[2] if len(cells) > 2 else ""
        if code == "Exemption" or not CODE_RE.match(code):
            continue
        entry = {"code": code, "annex": "III", "description": desc, "scope": scope, "dates": dates_in(scope)}
        if re.fullmatch(r"\d{1,2}", code):
            parent = entry if desc.endswith(":") else None
        elif parent and code.startswith(parent["code"]):
            entry["parent"] = parent["description"]
        out[key_of(code, "III")] = entry
    return out


def parse_annex_iv(seg: str) -> dict:
    out, group, last = {}, "", None
    tokens = re.findall(r'<p class="(title-gr-seq-level-2|norm)"[^>]*>(.*?)</p>|<tr[^>]*>(.*?)</tr>', seg, re.S)
    for cls, body, tr in tokens:
        if tr:
            cells = [clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(cells) >= 2 and re.match(r"^\d{1,2}[a-z]?\.?$", cells[0]):
                code = cells[0].rstrip(".")
                scope = cells[2] if len(cells) > 2 else ""
                last = out[key_of(code, "IV")] = {"code": code, "annex": "IV", "group": group, "description": cells[1],
                                                  "scope": scope, "dates": dates_in(cells[1] + " " + scope)}
            continue
        text = clean(body)
        if cls == "title-gr-seq-level-2":
            group, last = text, None
            continue
        m = re.match(r"^(\d{1,2}[a-z]?)\.\s+(.*)$", text)
        if m:
            code, desc = m.group(1), m.group(2)
            last = out[key_of(code, "IV")] = {"code": code, "annex": "IV", "group": group, "description": desc,
                                              "scope": "", "dates": dates_in(desc)}
        elif last and text:
            last["description"] += " " + text
            last["dates"] = dates_in(last["description"])
    return out


def parse(doc: str) -> dict:
    a3 = doc.find(">ANNEX III<")
    a4 = doc.find(">ANNEX IV<", a3)
    a5 = doc.find(">ANNEX V<", a4)
    if min(a3, a4, a5) < 0:
        raise SystemExit("Annexes III / IV not found in the document")
    return parse_annex_iii(doc[a3:a4]) | parse_annex_iv(doc[a4:a5])


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    current = sys.argv[1]
    entries = parse(fetch(current))
    result = {
        "source": "Directive 2011/65/EU (RoHS), Annexes III and IV, EUR-Lex consolidated text",
        "version": f"{current[:4]}-{current[4:6]}-{current[6:]}",
        "url": URL.format(v=current),
        "exemptions": entries,
    }
    if len(sys.argv) > 2:
        upcoming = sys.argv[2]
        future = parse(fetch(upcoming))
        changes = {}
        for k in sorted(set(entries) | set(future)):
            old, new = entries.get(k), future.get(k)
            if old and not new:
                changes[k] = {"type": "removed"}
            elif new and not old:
                changes[k] = {"type": "added", "description": new["description"], "scope": new["scope"]}
            elif (old["description"], old["scope"]) != (new["description"], new["scope"]):
                changes[k] = {"type": "modified", "description": new["description"], "scope": new["scope"], "dates": new["dates"]}
        result["upcoming"] = {"version": f"{upcoming[:4]}-{upcoming[4:6]}-{upcoming[6:]}", "url": URL.format(v=upcoming),
                              "changes": changes}
    out = Path(__file__).resolve().parent.parent / "data" / "rohs_exemptions.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    n3 = sum(1 for e in entries.values() if e["annex"] == "III")
    print(f"{n3} Annex III exemptions, {len(entries) - n3} Annex IV; "
          f"{len(result.get('upcoming', {}).get('changes', {}))} upcoming changes -> {out}")


if __name__ == "__main__":
    main()
