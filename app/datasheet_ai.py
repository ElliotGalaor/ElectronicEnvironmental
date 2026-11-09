"""Extracting a PDF datasheet's information with Claude (optional).

Enabled when Anthropic credentials are available (ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN
environment variable). Can be disabled with RRC_AI=off.
The PDF is sent as is to the model (text + page images), so scanned datasheets and ordering
tables can be read too.
"""

from __future__ import annotations

import base64
import os
from typing import Literal

from pydantic import BaseModel, Field

from .db import ValidationError, normalize_country, normalize_eccn

MODEL = os.environ.get("RRC_AI_MODEL", "claude-opus-5-5")
MAX_PDF_BYTES = 30 * 1024 * 1024  # API request limit: 32 MB


class Evidence(BaseModel):
    field: Literal["mpn", "manufacturer", "description", "rohs2", "rohs3", "reach", "rohs_exemptions", "svhc_list", "eccn", "country_of_origin"]
    page: int = Field(description="Page number (1 = first page)")
    quote: str = Field(description="Short, exact quote taken from the document")


class DatasheetInfo(BaseModel):
    mpn: str = Field(description="Main manufacturer part number (the most specific one covered by the datasheet)")
    other_part_numbers: list[str] = Field(description="Other orderable part numbers covered by the datasheet")
    manufacturer: str
    description: str = Field(description="Short description in English, e.g. “Dual op-amp, SOIC-8”")
    rohs2: Literal["compliant", "exemption", "non_compliant", "unknown"]
    rohs3: Literal["compliant", "exemption", "non_compliant", "unknown"]
    reach: Literal["compliant", "svhc", "non_compliant", "unknown"]
    rohs_exemptions: str = Field(description="RoHS exemption codes cited, e.g. “7(c)-I”, otherwise an empty string")
    svhc_list: str = Field(description="SVHCs present above 0.1 % cited, otherwise an empty string")
    eccn: str = Field(description="ECCN cited in the document (e.g. EAR99, 5A992.c), otherwise an empty string")
    country_of_origin: str = Field(description="Country of origin cited, ISO 3166-1 alpha-2 code (e.g. CN, MY), otherwise an empty string")
    evidence: list[Evidence]
    notes: list[str] = Field(description="Remarks in English useful to the reviewer (ambiguities, limits)")


SYSTEM = """You extract the identification and environmental compliance information of an electronic \
component from its datasheet, to feed a RoHS / REACH compliance database.

Compliance rules (do not assume anything that is not written in the document):
- rohs2 (Directive 2011/65/EU): "compliant" if the document states RoHS compliance (RoHS mentioned \
without further detail = RoHS 2); "exemption" if it cites an Annex III exemption; "non_compliant" if it \
says otherwise; otherwise "unknown". "Pb-free" or "green" alone are not enough.
- rohs3 (Directive (EU) 2015/863, 10 substances including 4 phthalates): "compliant" only if the document \
explicitly mentions 2015/863, RoHS 3 or the 10 substances / phthalates; otherwise "unknown".
- reach (Regulation 1907/2006): "compliant" if the document states there is no SVHC above 0.1 % or states \
REACH compliance; "svhc" if it declares an SVHC above 0.1 %; "non_compliant" if it says so; otherwise "unknown".
- eccn and country_of_origin: only if they are written in the document (ECCN, Export Control Classification \
Number, Country of Origin, Made in...); otherwise an empty string. Never infer an ECCN.
- For every status other than "unknown", and for the part number and the manufacturer, give an exact \
quote and the page in evidence.
- If the datasheet covers a family of part numbers, put the most relevant one in mpn (the one in the file \
name if it appears there) and the other orderable variants in other_part_numbers."""


def available() -> bool:
    if os.environ.get("RRC_AI", "").lower() in ("off", "0", "false", "no"):
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


class AIExtractionError(RuntimeError):
    pass


def _safe(normalize, value: str) -> str:
    """Normalise a value returned by the AI; an unrecognised value is ignored."""
    try:
        return normalize(value)
    except ValidationError:
        return ""


def extract(data: bytes, filename: str = "") -> dict:
    import anthropic

    if len(data) > MAX_PDF_BYTES:
        raise AIExtractionError("PDF too large for AI analysis (max 30 MB)")

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.parse(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": "medium"},
            # Automatic server-side fallback if the model declines the request
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
                    {"type": "text", "text": f"File name: {filename or 'unknown'}. Extract the component information."},
                ],
            }],
            output_format=DatasheetInfo,
        )
    except anthropic.AuthenticationError as exc:
        raise AIExtractionError("Invalid Anthropic API key") from exc
    except anthropic.RateLimitError as exc:
        raise AIExtractionError("API rate limit reached, try again later") from exc
    except anthropic.BadRequestError as exc:
        raise AIExtractionError(f"Request rejected by the API: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise AIExtractionError(f"API error ({exc.status_code})") from exc
    except anthropic.APIConnectionError as exc:
        raise AIExtractionError("Could not reach the Anthropic API") from exc

    if response.stop_reason == "refusal":
        raise AIExtractionError("The model declined to analyse this document")
    info: DatasheetInfo | None = response.parsed_output
    if info is None:
        raise AIExtractionError("Unusable AI response")

    evidence: dict[str, list[dict]] = {}
    for e in info.evidence:
        evidence.setdefault(e.field, []).append({"page": e.page, "text": e.quote[:240]})

    return {
        "method": "ai",
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
            "eccn": _safe(normalize_eccn, info.eccn),
            "country_of_origin": _safe(normalize_country, info.country_of_origin),
            "source": f"Datasheet {filename} (AI extraction)".strip(),
        },
        "candidates": {
            "mpn": [{"value": info.mpn}] + [{"value": p} for p in info.other_part_numbers if p and p != info.mpn],
            "manufacturer": [{"value": info.manufacturer}] if info.manufacturer else [],
        },
        "evidence": evidence,
        "notes": list(info.notes),
        "warnings": [],
    }
