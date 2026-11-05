"""Extraction des informations d'un composant depuis sa datasheet PDF (sans IA).

Approche heuristique :
  - texte extrait avec pypdf (les PDF scannés, sans texte, ne sont pas exploitables ici) ;
  - fabricant : nom de domaine (www.ti.com…), mention de copyright, fréquence des noms connus ;
  - référence : jetons de type « référence » (lettres + chiffres) notés selon leur fréquence,
    leur présence en page 1, dans le titre du PDF, le nom du fichier ou un tableau de commande ;
  - conformité : phrases mentionnant RoHS / 2011/65 / 2015/863 / REACH / SVHC / exemptions.

Le résultat est une proposition : l'utilisateur la relit avant enregistrement.
Une datasheet n'indique souvent pas la conformité : les statuts restent alors « inconnu ».
"""

from __future__ import annotations

import io
import re
from collections import Counter, defaultdict
from pathlib import Path

from .db import ValidationError, loose_mpn, normalize_country, normalize_eccn, normalize_manufacturer


class DatasheetError(ValueError):
    pass


# Nom affiché -> (alias recherchés dans le texte, domaines web)
MANUFACTURERS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Texas Instruments": (("texas instruments",), ("ti.com",)),
    "STMicroelectronics": (("stmicroelectronics", "st microelectronics"), ("st.com",)),
    "Analog Devices": (("analog devices", "linear technology", "maxim integrated"), ("analog.com", "maximintegrated.com", "linear.com")),
    "Microchip": (("microchip technology", "microchip", "atmel"), ("microchip.com", "atmel.com")),
    "NXP": (("nxp semiconductors", "nxp b.v", "nxp"), ("nxp.com",)),
    "onsemi": (("onsemi", "on semiconductor", "semiconductor components industries", "fairchild"), ("onsemi.com", "fairchildsemi.com")),
    "Infineon": (("infineon technologies", "infineon", "international rectifier", "cypress semiconductor"), ("infineon.com", "irf.com", "cypress.com")),
    "Renesas": (("renesas electronics", "renesas", "intersil"), ("renesas.com",)),
    "Nexperia": (("nexperia",), ("nexperia.com",)),
    "Diodes": (("diodes incorporated",), ("diodes.com",)),
    "Vishay": (("vishay intertechnology", "vishay siliconix", "vishay dale", "vishay"), ("vishay.com",)),
    "ROHM": (("rohm semiconductor", "rohm co"), ("rohm.com",)),
    "Toshiba": (("toshiba electronic", "toshiba"), ("toshiba.semicon-storage.com",)),
    "Murata": (("murata manufacturing", "murata electronics", "murata"), ("murata.com",)),
    "TDK": (("tdk corporation", "tdk electronics", "epcos"), ("tdk.com", "tdk-electronics.tdk.com")),
    "Samsung": (("samsung electro-mechanics", "samsung electro mechanics"), ("samsungsem.com",)),
    "Yageo": (("yageo corporation", "yageo"), ("yageo.com",)),
    "KEMET": (("kemet electronics", "kemet"), ("kemet.com",)),
    "Panasonic": (("panasonic industry", "panasonic"), ("panasonic.com", "industrial.panasonic.com")),
    "Wurth Elektronik": (("würth elektronik", "wurth elektronik", "wurth electronics"), ("we-online.com", "we-online.de")),
    "TE Connectivity": (("te connectivity", "tyco electronics"), ("te.com",)),
    "Molex": (("molex",), ("molex.com",)),
    "Amphenol": (("amphenol",), ("amphenol.com",)),
    "Bourns": (("bourns",), ("bourns.com",)),
    "Littelfuse": (("littelfuse",), ("littelfuse.com",)),
    "Abracon": (("abracon",), ("abracon.com",)),
    "Lite-On": (("lite-on technology", "lite-on", "liteon"), ("liteon.com",)),
    "Kingbright": (("kingbright",), ("kingbrightusa.com", "kingbright.com")),
    "Espressif": (("espressif systems", "espressif"), ("espressif.com",)),
    "Nordic Semiconductor": (("nordic semiconductor",), ("nordicsemi.com",)),
    "Silicon Labs": (("silicon laboratories", "silicon labs"), ("silabs.com",)),
    "Coilcraft": (("coilcraft",), ("coilcraft.com",)),
    "Bel Fuse": (("bel fuse",), ("belfuse.com",)),
    "Hirose": (("hirose electric", "hirose"), ("hirose.com",)),
    "JST": (("j.s.t. mfg", "jst mfg"), ("jst-mfg.com", "jst.com")),
    "Phoenix Contact": (("phoenix contact",), ("phoenixcontact.com",)),
    "Omron": (("omron",), ("omron.com",)),
}

# Jetons qui ressemblent à des références mais n'en sont pas
_NOT_MPN = [
    re.compile(p)
    for p in (
        r"^\d+(\.\d+)?(V|MV|KV|A|MA|UA|NA|W|MW|KW|HZ|KHZ|MHZ|GHZ|F|NF|PF|UF|H|NH|UH|MH|K|M|R|Ω|NS|MS|US|PS|S|DB|DBM|PPM|MM|CM|UM|NM|MIL|C|°C|BIT|BITS|KB|MB|GB|KBPS|MBPS|V/US|%)$",
        r"^(ISO|IEC|EN|UL|CSA|JESD|JEDEC|IPC|AEC|MIL|VDE|DIN|TUV|ANSI|IEEE|CISPR|ETSI|RTCA|J-STD)[-\d]",
        r"^(SOT|SOIC|SOP|SSOP|TSSOP|MSOP|QFN|DFN|VQFN|WQFN|UQFN|TQFP|LQFP|QFP|BGA|DIP|PDIP|SO|SOD|SMA|SMB|SMC|TO|DPAK|D2PAK|SC|WLCSP|CSP|LGA|PLCC|HTSSOP|VSSOP|X2SON|SON|USON|WSON)-?\d",
        r"^\d{4}-\d{2}-\d{2}$",
        r"^(19|20)\d{2}$",
        r"^0X[0-9A-F]+$",
        r"^(REV|VER|V)\.?\d",
        r"^(RoHS|ROHS|REACH|SVHC|CAS)\d*$",
        r"^\d+(\.\d+)?[-/]\d+(\.\d+)?$",  # 3.3/5, 1-2
        r"^\d{1,3}(\.\d+)?[A-Z]$",  # 5V, 10K...
        r"^[A-Z]{1,2}\d{1,2}$",  # R1, C10, U2 (repères)
        r"^\d{2,7}-\d{2}-\d$",  # numéros CAS
        r"^PAGE\d",
        r"^(I2C|I2S|SPI|USB\d?|UART|CAN|LIN|PCIE|DDR\d|LPDDR\d|MIPI|HDMI|SATA|GPIO\d*|ADC\d*|DAC\d*|PWM\d*|ESD|HBM|CDM|MSL\d?|ARM|RISC-V|CORTEX-M\d+|X\d+R|C0G|NP0|X5R|X7R|X7S|Y5V|Z5U|AC|DC)$",
    )
]
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9\-/#.])[A-Z0-9][A-Z0-9\-/#.+]{2,34}[A-Z0-9#+](?![A-Za-z0-9])")
_ORDER_CONTEXT = re.compile(r"order|part\s*(number|no)|orderable|device\s+(name|marking)|r[ée]f[ée]rence|type\s+number|product\s+id|mpn|marking", re.I)
_COPYRIGHT_RE = re.compile(r"(?:©|\(c\)|copyright)\s*(?:\d{4}\s*[-–]?\s*\d{0,4})?\s*,?\s*([A-Za-zÀ-ÿ][\w&.,'\- ]{2,60})", re.I)

_EXEMPTION_CODE = re.compile(r"\b(\d{1,2}\s?\(\s?[a-e]\s?\)(?:\s?[-–]?\s?(?:I{1,3}|IV|V|VI{0,3}))?)", re.I)


def _text_pages(data: bytes) -> tuple[list[str], dict]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise DatasheetError("Le module pypdf est requis pour lire les PDF (pip install pypdf)") from exc
    if not data.startswith(b"%PDF"):
        raise DatasheetError("Le fichier n'est pas un PDF")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise DatasheetError("PDF protégé par mot de passe") from exc
        pages = []
        for page in reader.pages[:60]:  # les datasheets très longues : on se limite aux 60 premières pages
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
        meta = {}
        if reader.metadata:
            for key in ("title", "author", "subject", "creator"):
                val = getattr(reader.metadata, key, None)
                if val:
                    meta[key] = str(val).strip()
        meta["page_count"] = len(reader.pages)
    except DatasheetError:
        raise
    except Exception as exc:
        raise DatasheetError(f"PDF illisible : {exc}") from exc
    return pages, meta


def _lines(pages: list[str]) -> list[tuple[int, str]]:
    out = []
    for i, text in enumerate(pages, start=1):
        for line in text.splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                out.append((i, line))
    return out


def _snippet(page: int, line: str) -> dict:
    return {"page": page, "text": line[:240]}


# ------------------------------------------------------------------ fabricant

def detect_manufacturer(pages: list[str], meta: dict, lines: list[tuple[int, str]]) -> tuple[list[dict], list[dict]]:
    full = "\n".join(pages)
    low = full.lower()
    scores: Counter = Counter()
    evidence: dict[str, dict] = {}

    for name, (aliases, domains) in MANUFACTURERS.items():
        for d in domains:
            m = re.search(r"(?:www\.|https?://|@)(?:[\w-]+\.)*" + re.escape(d) + r"\b", low)
            if m:
                scores[name] += 12
                evidence.setdefault(name, {"page": _page_of(pages, m.start()), "text": f"site web : {full[m.start():m.end()]}"})
        for a in aliases:
            hits = list(re.finditer(r"(?<![a-z])" + re.escape(a) + r"(?![a-z])", low))
            if hits:
                # On plafonne pour qu'un nom très répété ne domine pas tout
                scores[name] += min(len(hits), 10) * (2 if len(a) > 6 else 1)
                h = hits[0]
                evidence.setdefault(name, {"page": _page_of(pages, h.start()), "text": _around(full, h.start(), h.end())})

    for m in _COPYRIGHT_RE.finditer(full):
        holder = m.group(1).lower()
        for name, (aliases, _) in MANUFACTURERS.items():
            if any(holder.startswith(a) or a in holder for a in aliases):
                scores[name] += 8
                evidence[name] = {"page": _page_of(pages, m.start()), "text": _around(full, m.start(), m.end())}

    author = (meta.get("author", "") + " " + meta.get("creator", "")).lower()
    for name, (aliases, _) in MANUFACTURERS.items():
        if any(a in author for a in aliases):
            scores[name] += 6

    ranked = [{"value": n, "score": s} for n, s in scores.most_common(5) if s > 0]
    ev = [evidence[ranked[0]["value"]]] if ranked and ranked[0]["value"] in evidence else []
    return ranked, ev


def _page_of(pages: list[str], offset: int) -> int:
    pos = 0
    for i, p in enumerate(pages, start=1):
        pos += len(p) + 1
        if offset < pos:
            return i
    return len(pages)


def _around(text: str, start: int, end: int, width: int = 80) -> str:
    s = text[max(0, start - width) : end + width]
    return re.sub(r"\s+", " ", s).strip()


# ------------------------------------------------------------------ référence

def _is_mpn_like(tok: str) -> bool:
    if not (4 <= len(tok) <= 36):
        return False
    if not re.search(r"[A-Z]", tok) or not re.search(r"\d", tok):
        return False
    return not any(p.match(tok) for p in _NOT_MPN)


def detect_mpn(pages: list[str], meta: dict, lines: list[tuple[int, str]], filename: str) -> tuple[list[dict], list[dict]]:
    scores: Counter = Counter()
    first_seen: dict[str, tuple[int, str]] = {}
    title = (meta.get("title", "") + " " + meta.get("subject", "")).upper()
    stem = loose_mpn(Path(filename or "").stem)

    for page, line in lines:
        order_ctx = bool(_ORDER_CONTEXT.search(line))
        for m in _TOKEN_RE.finditer(line):
            tok = m.group(0).rstrip(".")
            if not _is_mpn_like(tok):
                continue
            scores[tok] += 1
            if page == 1:
                scores[tok] += 2
            if order_ctx:
                scores[tok] += 3
            first_seen.setdefault(tok, (page, line))

    for tok in list(scores):
        lt = loose_mpn(tok)
        if tok in title:
            scores[tok] += 10
        if stem and len(stem) >= 4:
            if lt == stem:
                scores[tok] += 25
            elif lt.startswith(stem) or stem.startswith(lt):
                scores[tok] += 8
        # Les familles (LM358) et variantes commandables (LM358DR) se renforcent mutuellement
        if len(lt) >= 5:
            family = sum(1 for o in scores if o != tok and loose_mpn(o).startswith(lt))
            scores[tok] += min(family, 5)

    ranked = [{"value": t, "score": s} for t, s in scores.most_common(8)]
    ev = []
    if ranked:
        p, l = first_seen[ranked[0]["value"]]
        ev.append(_snippet(p, l))
    return ranked, ev


# ------------------------------------------------------------------ description

_DESC_SKIP = re.compile(r"datasheet|data sheet|product specification|www\.|http|©|copyright|rev(ision)?\b|page \d|\d{4}-\d{2}|all rights|confidential|preliminary", re.I)


def detect_description(pages: list[str], meta: dict, lines: list[tuple[int, str]], mpn: str) -> tuple[str, list[dict]]:
    mpn_l = loose_mpn(mpn)

    def clean(text: str) -> str:
        t = text
        if mpn:
            t = re.sub(re.escape(mpn) + r"[A-Z0-9\-/#]*", "", t, flags=re.I)
        t = _TOKEN_RE.sub(lambda m: "" if _is_mpn_like(m.group(0)) else m.group(0), t)
        t = re.sub(r"\s{2,}", " ", t).strip(" -–—,:;|")
        return t

    for key in ("subject", "title"):
        v = meta.get(key, "")
        c = clean(v)
        if len(c.split()) >= 3 and not _DESC_SKIP.search(c) and loose_mpn(c) != mpn_l:
            return c[:160], [{"page": 0, "text": f"métadonnée PDF « {key} » : {v[:200]}"}]

    page1 = [(p, l) for p, l in lines if p == 1][:40]
    for p, l in page1:
        c = clean(l)
        words = c.split()
        letters = sum(ch.isalpha() for ch in c)
        if 3 <= len(words) <= 16 and 15 <= len(c) <= 140 and letters / max(len(c), 1) > 0.6 and not _DESC_SKIP.search(c):
            if c.endswith(".") and len(words) > 10:
                continue  # phrase de texte courant, pas un titre
            return c, [_snippet(p, l)]
    return "", []


# ------------------------------------------------------------------ conformité

_ROHS_NEG = re.compile(r"\b(not|non)[\s-]*rohs|rohs[\s-]*non[\s-]*compliant|not\s+(be\s+)?(rohs[\s-]*)?compliant\s+(with|to)\s+rohs|non[\s-]*conforme?\s+(à\s+la\s+)?rohs", re.I)
_ROHS3 = re.compile(r"2015\s*/\s*863|rohs\s*[-]?\s*(3|iii)\b|\b10\s+(eu\s+)?(rohs\s+)?(restricted\s+)?substances|four\s+phthalates|4\s+phtalates", re.I)
_ROHS2 = re.compile(r"2011\s*/\s*65|rohs\s*[-]?\s*(2|ii)\b", re.I)
_ROHS_POS = re.compile(r"rohs[\s-]*(compliant|conforme?|conformity|compliance|certified|:?\s*(yes|y)\b)|rohs\s*(&|and)\s*green|compl(y|ies|iant)\s+(with\s+)?(the\s+)?(eu\s+)?rohs|meets?\s+(the\s+)?(eu\s+)?rohs|conforme?s?\s+(à|a)\s+(la\s+)?(directive\s+)?rohs|rohs\s+directive", re.I)
_ROHS_BARE = re.compile(r"^\W*rohs\W*$", re.I)  # puce « • RoHS » seule dans une liste de caractéristiques
_EXEMPT = re.compile(r"exempt", re.I)
_LEAD_FREE = re.compile(r"pb[\s-]*free|lead[\s-]*free|sans\s+plomb|green\s+(product|package)|halogen[\s-]*free", re.I)
_REACH = re.compile(r"\bREACH\b|1907\s*/\s*2006|\bSVHC\b")
_REACH_FREE = re.compile(r"svhc[\s-]*free|\bno\s+svhc|free\s+(of|from)\s+svhc|does\s+not\s+contain\s+(any\s+)?(substances?\s+of\s+very\s+high\s+concern|svhc)|(below|less\s+than|<|under|not\s+exceed(ing)?|n'exc[eè]de\s+pas)\s*0[.,]1\s*%|aucune\s+svhc|sans\s+svhc", re.I)
_REACH_POS = re.compile(r"reach[\s-]*(compliant|conforme?|:?\s*yes\b)|compl(y|ies|iant)\s+with\s+(the\s+)?reach|conforme?s?\s+(au\s+r[èe]glement\s+)?reach", re.I)
_REACH_SVHC = re.compile(r"contains?\s+(the\s+following\s+)?(svhc|substances?\s+of\s+very\s+high\s+concern)|svhc.{0,60}(above|exceed(s|ing)?|greater\s+than|>)\s*0[.,]1|an\s+svhc|contient.{0,40}svhc", re.I)
_REACH_NEG = re.compile(r"not\s+reach[\s-]*compliant|non[\s-]*reach[\s-]*compliant|reach\s*:?\s*(no|non)\s*$", re.I)


def detect_compliance(lines: list[tuple[int, str]]) -> tuple[dict, dict, list[str]]:
    # On travaille sur des fenêtres de 2 lignes : les phrases sont souvent coupées.
    windows = []
    for i, (p, l) in enumerate(lines):
        nxt = lines[i + 1][1] if i + 1 < len(lines) and lines[i + 1][0] == p else ""
        windows.append((p, l, f"{l} {nxt}"))

    ev: dict[str, list[dict]] = defaultdict(list)
    res = {"rohs2": "inconnu", "rohs3": "inconnu", "reach": "inconnu", "rohs_exemptions": "", "svhc_list": ""}
    notes: list[str] = []
    flags = Counter()
    exemptions: list[str] = []
    svhc_names: list[str] = []

    def add(field: str, p: int, l: str):
        if len(ev[field]) < 3 and all(e["text"] != l[:240] for e in ev[field]):
            ev[field].append(_snippet(p, l))

    for p, l, w in windows:
        has_rohs = re.search(r"rohs|2011\s*/\s*65|2015\s*/\s*863", w, re.I)
        if has_rohs:
            if _ROHS_NEG.search(w):
                flags["rohs_neg"] += 1
                add("rohs2", p, l)
            else:
                if _ROHS3.search(w):
                    flags["rohs3"] += 1
                    add("rohs3", p, l)
                if _ROHS2.search(w):
                    flags["rohs2"] += 1
                    add("rohs2", p, l)
                if _ROHS_POS.search(w) or _ROHS_BARE.match(l):
                    flags["rohs_pos"] += 1
                    add("rohs2", p, l)
            if _EXEMPT.search(w):
                codes = [re.sub(r"\s+", "", c) for c in _EXEMPTION_CODE.findall(w)]
                if codes:
                    flags["exempt"] += 1
                    exemptions += [c for c in codes if c not in exemptions]
                    add("rohs_exemptions", p, l)
        elif _EXEMPT.search(w) and re.search(r"annex\s+iii|annexe\s+iii|exemption\s+\d", w, re.I):
            codes = [re.sub(r"\s+", "", c) for c in _EXEMPTION_CODE.findall(w)]
            if codes:
                flags["exempt"] += 1
                exemptions += [c for c in codes if c not in exemptions]
                add("rohs_exemptions", p, l)
        if _LEAD_FREE.search(w):
            flags["lead_free"] += 1
        if _REACH.search(w):
            if _REACH_NEG.search(l):
                flags["reach_neg"] += 1
                add("reach", p, l)
            elif _REACH_FREE.search(w):
                flags["reach_pos"] += 1
                add("reach", p, l)
            elif _REACH_SVHC.search(w):
                flags["reach_svhc"] += 1
                add("reach", p, l)
                for name in re.findall(r"(lead|plomb|cadmium|DEHP|DBP|BBP|DIBP|boric acid|diboron trioxide|lead titanate|lead monoxide|silicon dioxide|CAS\s*(?:No\.?)?\s*\d{2,7}-\d{2}-\d)", w, re.I):
                    if name.lower() not in [s.lower() for s in svhc_names]:
                        svhc_names.append(name)
            elif _REACH_POS.search(w):
                flags["reach_pos"] += 1
                add("reach", p, l)
            else:
                flags["reach_mention"] += 1
                add("reach", p, l)

    # --- RoHS
    if flags["rohs_neg"]:
        res["rohs2"] = res["rohs3"] = "non_conforme"
        notes.append("La datasheet indique une non-conformité RoHS")
    elif flags["rohs3"] or flags["rohs2"] or flags["rohs_pos"]:
        res["rohs2"] = "conforme"
        if flags["rohs3"]:
            res["rohs3"] = "conforme"
        else:
            notes.append("RoHS mentionné sans préciser la directive (UE) 2015/863 : RoHS 3 laissé « inconnu », à confirmer")
        if flags["exempt"]:
            res["rohs2"] = "exemption"
            if res["rohs3"] == "conforme":
                res["rohs3"] = "exemption"
    elif flags["lead_free"]:
        notes.append("Seules des mentions « sans plomb / Pb-free / green » ont été trouvées : insuffisant pour conclure sur RoHS")
    if exemptions:
        res["rohs_exemptions"] = ", ".join(exemptions)

    # --- REACH
    if flags["reach_neg"]:
        res["reach"] = "non_conforme"
    elif flags["reach_svhc"]:
        res["reach"] = "svhc"
        if svhc_names:
            res["svhc_list"] = ", ".join(svhc_names)
    elif flags["reach_pos"]:
        res["reach"] = "conforme"
    elif flags["reach_mention"]:
        notes.append("REACH est mentionné sans statut clair : à vérifier dans la déclaration du fabricant")

    if all(res[k] == "inconnu" for k in ("rohs2", "rohs3", "reach")):
        notes.append("Aucune information de conformité trouvée : consultez la déclaration RoHS/REACH du fabricant")
    return res, dict(ev), notes


# ------------------------------------------------------------------ export / douane

_ECCN_LINE = re.compile(r"\b(?:US\s*|EU\s*)?ECCN\b\s*(?:code|number|no\.?|n°)?\s*[:=\-]?\s*(EAR\s?99|\d[A-E]\d{3}(?:\.[a-z0-9]+)*)", re.I)
_COO_LINE = re.compile(
    r"(?:country\s+of\s+origin|countries\s+of\s+origin|\bCOO\b|pays\s+d['’]origine|origin)\s*[:=\-]\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'\-]{1,40}?)(?=\s*(?:[,;|/()]|$|\s{2,}))"
    r"|\bmade\s+in\s+([A-Z][A-Za-z .\-]{1,30}?)(?=\s*(?:[,;|/().]|$))"
    r"|\bfabriqu[ée]\s+en\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .\-]{1,30}?)(?=\s*(?:[,;|/().]|$))",
    re.I,
)


def detect_export(lines: list[tuple[int, str]]) -> tuple[dict, dict, list[str]]:
    res = {"eccn": "", "country_of_origin": ""}
    ev: dict[str, list[dict]] = {}
    notes: list[str] = []
    eccns: list[str] = []
    countries: list[str] = []
    for p, l in lines:
        for m in _ECCN_LINE.finditer(l):
            try:
                code = normalize_eccn(m.group(1))
            except ValidationError:
                continue
            if code and code not in eccns:
                eccns.append(code)
                ev.setdefault("eccn", []).append(_snippet(p, l))
        for m in _COO_LINE.finditer(l):
            raw = next(g for g in m.groups() if g)
            try:
                code = normalize_country(raw.strip(" .-"))
            except ValidationError:
                continue
            if code and code not in countries:
                countries.append(code)
                ev.setdefault("country_of_origin", []).append(_snippet(p, l))
    if eccns:
        res["eccn"] = eccns[0]
        if len(eccns) > 1:
            notes.append(f"Plusieurs ECCN cités ({', '.join(eccns)}) : vérifiez celui de la référence exacte")
    if countries:
        res["country_of_origin"] = countries[0]
        if len(countries) > 1:
            notes.append(f"Plusieurs pays d'origine cités ({', '.join(countries)}) : dépend du site de fabrication")
    return res, ev, notes


# ------------------------------------------------------------------ point d'entrée

def extract(data: bytes, filename: str = "") -> dict:
    pages, meta = _text_pages(data)
    lines = _lines(pages)
    text_chars = sum(len(p) for p in pages)
    warnings: list[str] = []
    if text_chars < 200:
        warnings.append("Le PDF contient très peu de texte (document scanné ?) : l'extraction automatique est limitée")

    mpn_cands, mpn_ev = detect_mpn(pages, meta, lines, filename)
    mfr_cands, mfr_ev = detect_manufacturer(pages, meta, lines)
    mpn = mpn_cands[0]["value"] if mpn_cands else ""
    manufacturer = mfr_cands[0]["value"] if mfr_cands else ""
    description, desc_ev = detect_description(pages, meta, lines, mpn)
    compliance, comp_ev, notes = detect_compliance(lines)
    export, export_ev, export_notes = detect_export(lines)
    notes += export_notes

    if len(mpn_cands) > 1 and mpn_cands[1]["score"] >= mpn_cands[0]["score"] * 0.8:
        notes.insert(0, "Plusieurs références possibles : vérifiez la référence exacte (variante commandable)")

    evidence = {"mpn": mpn_ev, "manufacturer": mfr_ev, "description": desc_ev, **comp_ev, **export_ev}
    return {
        "method": "heuristique",
        "fields": {
            "mpn": mpn,
            "manufacturer": manufacturer,
            "description": description,
            **compliance,
            **export,
            "source": f"Datasheet {filename}".strip(),
        },
        "candidates": {"mpn": mpn_cands, "manufacturer": mfr_cands},
        "evidence": evidence,
        "notes": notes,
        "warnings": warnings,
        "pages": meta.get("page_count", len(pages)),
        "text_chars": text_chars,
        "manufacturer_norm": normalize_manufacturer(manufacturer),
    }
