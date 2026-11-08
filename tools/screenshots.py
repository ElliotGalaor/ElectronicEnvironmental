"""Captures d'écran du README (docs/images/*.png).

Prérequis : `pip install playwright` et Microsoft Edge installé (aucun téléchargement de navigateur),
puis un serveur lancé sur une base de démonstration :

    set RRC_DB_PATH=%TEMP%\\demo.db
    python cli.py seed
    python -m uvicorn app.main:app --port 8002
    python tools/screenshots.py http://localhost:8002
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"


def clip_between(page: Page, top_sel: str, bottom_sel: str, pad: int = 12, max_height: int | None = None) -> dict:
    """Zone de la page (coordonnées absolues, pour full_page=True) allant de top_sel à bottom_sel."""
    box = page.evaluate(
        """([a, b]) => {
            const ra = document.querySelector(a).getBoundingClientRect();
            const rb = document.querySelector(b).getBoundingClientRect();
            return { top: ra.top + scrollY, bottom: rb.bottom + scrollY };
        }""",
        [top_sel, bottom_sel],
    )
    y = max(0, box["top"] - pad)
    height = box["bottom"] - y + pad
    if max_height:
        height = min(height, max_height)
    width = page.viewport_size["width"]
    return {"x": 0, "y": y, "width": width, "height": height}


def shot(page: Page, name: str, **kwargs) -> None:
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path), **kwargs)
    print("capture :", path.relative_to(ROOT))


def main(base: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=2, color_scheme="light",
                                locale="fr-FR")

        # 1. Analyse d'une BOM : verdicts et camemberts
        page.goto(f"{base}/#analyse")
        page.set_input_files("#bomFile", str(ROOT / "exemples" / "bom_exemple.csv"))
        page.wait_for_selector("#result:not([hidden]) #charts .chart")
        page.wait_for_timeout(400)
        shot(page, "analyse", clip=clip_between(page, ".fileline", "#counts"), full_page=True)

        # 2. Tableau des lignes
        page.locator("#filters").scroll_into_view_if_needed()
        shot(page, "lignes", clip=clip_between(page, "#filters", "#lines tbody tr:nth-child(8)"), full_page=True)

        # 3. Équivalents d'autres fabricants pour le composant non conforme
        page.locator("#lines a.mpn-link", has_text="DEMO-LEADED-DIP8").click()
        page.wait_for_selector("#altTable")
        page.wait_for_timeout(300)
        page.locator("#dlgAlt").screenshot(path=str(OUT / "equivalents.png"))
        print("capture : docs/images/equivalents.png")

        # 4. Remplacement dans la BOM : nouveau verdict
        page.locator("#altTable [data-use]").first.click()
        page.wait_for_selector("#swapBar:not([hidden])")
        page.wait_for_timeout(500)
        shot(page, "remplacement", clip=clip_between(page, "#swapBar", "#cards"), full_page=True)

        # 5. Fiche d'une exemption RoHS
        page.goto(f"{base}/#exemption/7(c)-I")
        page.wait_for_selector("#exemptInfo dt")
        page.wait_for_timeout(300)
        shot(page, "exemption", clip=clip_between(page, "#exemptDetail .back", "#exemptComps"), full_page=True)

        # 6. Fiche d'un code ECCN
        page.goto(f"{base}/#eccn/3A991.a.2")
        page.wait_for_selector("#eccnParts .part")
        page.wait_for_timeout(300)
        shot(page, "eccn", clip=clip_between(page, "#eccnDetail .back", "#eccnInfo"), full_page=True)

        # 7. Fiche fabricant
        page.goto(f"{base}/#fabricant/texas%20instruments")
        page.wait_for_selector("#mfrComps tbody tr")
        page.wait_for_timeout(300)
        shot(page, "fabricant", clip=clip_between(page, "#mfrDetail .back", "#mfrComps", max_height=1100), full_page=True)

        browser.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8002")
