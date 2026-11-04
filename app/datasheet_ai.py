"""Extraction des informations d'une datasheet PDF par Claude (optionnelle).

Activée lorsque des identifiants Anthropic sont disponibles (variable ANTHROPIC_API_KEY,
ou ANTHROPIC_AUTH_TOKEN). Désactivable avec RRC_AI=off.
Le PDF est envoyé tel quel au modèle (texte + images des pages), ce qui permet aussi de lire
les datasheets scannées et les tableaux de commande.
"""

from __future__ import annotations

import base64
import os
from typing import Literal

from pydantic import BaseModel, Field

MODEL = os.environ.get("RRC_AI_MODEL", "claude-opus-5-5")
MAX_PDF_BYTES = 30 * 1024 * 1024  # limite de requête de l'API : 32 Mo


class Evidence(BaseModel):
    field: Literal["mpn", "manufacturer", "description", "rohs2", "rohs3", "reach", "rohs_exemptions", "svhc_list"]
    page: int = Field(description="Numéro de page (1 = première page)")
    quote: str = Field(description="Citation exacte, courte, tirée du document")


class DatasheetInfo(BaseModel):
    mpn: str = Field(description="Référence fabricant principale (la plus spécifique couverte par la datasheet)")
    other_part_numbers: list[str] = Field(description="Autres références commandables couvertes par la datasheet")
    manufacturer: str
    description: str = Field(description="Description courte en français, ex. « Double ampli-op, SOIC-8 »")
    rohs2: Literal["conforme", "exemption", "non_conforme", "inconnu"]
    rohs3: Literal["conforme", "exemption", "non_conforme", "inconnu"]
    reach: Literal["conforme", "svhc", "non_conforme", "inconnu"]
    rohs_exemptions: str = Field(description="Codes d'exemption RoHS cités, ex. « 7(c)-I », sinon chaîne vide")
    svhc_list: str = Field(description="SVHC présentes > 0,1 % citées, sinon chaîne vide")
    evidence: list[Evidence]
    notes: list[str] = Field(description="Remarques en français utiles au vérificateur (ambiguïtés, limites)")


SYSTEM = """Tu extrais les informations d'identification et de conformité environnementale d'un composant \
électronique à partir de sa datasheet, pour alimenter une base de conformité RoHS / REACH.

Règles de conformité (ne rien supposer qui ne soit pas écrit dans le document) :
- rohs2 (directive 2011/65/UE) : "conforme" si le document affirme la conformité RoHS (mention RoHS sans \
précision = RoHS 2) ; "exemption" s'il cite une exemption de l'annexe III ; "non_conforme" s'il dit le \
contraire ; sinon "inconnu". « Pb-free » ou « green » seuls ne suffisent pas.
- rohs3 (directive (UE) 2015/863, 10 substances dont 4 phtalates) : "conforme" uniquement si le document \
mentionne explicitement 2015/863, RoHS 3 ou les 10 substances / phtalates ; sinon "inconnu".
- reach (règlement 1907/2006) : "conforme" si le document indique l'absence de SVHC > 0,1 % ou la \
conformité REACH ; "svhc" s'il déclare une SVHC > 0,1 % ; "non_conforme" s'il l'indique ; sinon "inconnu".
- Pour chaque statut autre que "inconnu", et pour la référence et le fabricant, donne une citation \
exacte et la page dans evidence.
- Si la datasheet couvre une famille de références, mets la référence la plus pertinente dans mpn \
(celle du nom de fichier si elle y figure) et les autres variantes commandables dans other_part_numbers."""


def available() -> bool:
    if os.environ.get("RRC_AI", "").lower() in ("off", "0", "false", "non"):
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


class AIExtractionError(RuntimeError):
    pass


def extract(data: bytes, filename: str = "") -> dict:
    import anthropic

    if len(data) > MAX_PDF_BYTES:
        raise AIExtractionError("PDF trop volumineux pour l'analyse par IA (max 30 Mo)")

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.parse(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": "medium"},
            # Repli automatique côté serveur si le modèle décline la requête
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.standard_b64encode(data).decode("ascii"),
                        },
                        "title": filename or "datasheet.pdf",
                    },
                    {"type": "text", "text": f"Nom du fichier : {filename or 'inconnu'}. Extrais les informations du composant."},
                ],
            }],
            output_format=DatasheetInfo,
        )
    except anthropic.AuthenticationError as exc:
        raise AIExtractionError("Clé API Anthropic invalide") from exc
    except anthropic.RateLimitError as exc:
        raise AIExtractionError("Limite de requêtes de l'API atteinte, réessayez plus tard") from exc
    except anthropic.BadRequestError as exc:
        raise AIExtractionError(f"Requête refusée par l'API : {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise AIExtractionError(f"Erreur de l'API ({exc.status_code})") from exc
    except anthropic.APIConnectionError as exc:
        raise AIExtractionError("Impossible de joindre l'API Anthropic") from exc

    if response.stop_reason == "refusal":
        raise AIExtractionError("Le modèle a refusé d'analyser ce document")
    info: DatasheetInfo | None = response.parsed_output
    if info is None:
        raise AIExtractionError("Réponse de l'IA inexploitable")

    evidence: dict[str, list[dict]] = {}
    for e in info.evidence:
        evidence.setdefault(e.field, []).append({"page": e.page, "text": e.quote[:240]})

    return {
        "method": "ia",
        "model": response.model,
        "fields": {
            "mpn": info.mpn.strip(),
            "manufacturer": info.manufacturer.strip(),
            "description": info.description.strip(),
            "rohs2": info.rohs2,
            "rohs3": info.rohs3,
            "reach": info.reach,
            "rohs_exemptions": info.rohs_exemptions.strip(),
            "svhc_list": info.svhc_list.strip(),
            "source": f"Datasheet {filename} (extraction IA)".strip(),
        },
        "candidates": {
            "mpn": [{"value": info.mpn}] + [{"value": p} for p in info.other_part_numbers if p and p != info.mpn],
            "manufacturer": [{"value": info.manufacturer}] if info.manufacturer else [],
        },
        "evidence": evidence,
        "notes": list(info.notes),
        "warnings": [],
    }
