"""README screenshots (docs/images/*.png).

Requirements: `pip install playwright` and Microsoft Edge installed (no browser download),
then a server running on a demo database:

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
    """Page area (absolute coordinates, for full_page=True) from top_sel to bottom_sel."""
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
    print("screenshot:", path.relative_to(ROOT))


def main(base: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=2, color_scheme="light",
                                locale="en-GB")

        # 0. Home page: presentation of the tool and example result
        page.goto(f"{base}/#home")
        page.wait_for_selector("#tab-home.active .sample")
        page.wait_for_timeout(300)
        shot(page, "home", clip=clip_between(page, ".top", ".hero", pad=16))

        # 1. BOM analysis: verdicts and pie charts
        page.goto(f"{base}/#analysis")
        page.set_input_files("#bomFile", str(ROOT / "examples" / "bom_example.csv"))
        page.wait_for_selector("#result:not([hidden]) #charts .chart")
        page.wait_for_timeout(400)
        shot(page, "analysis", clip=clip_between(page, ".fileline", "#counts"), full_page=True)

        # 2. Line table
        page.locator("#filters").scroll_into_view_if_needed()
        shot(page, "lines", clip=clip_between(page, "#filters", "#lines tbody tr:nth-child(8)"), full_page=True)

        # 3. "Needs review" status given by hand: dialog with the reason and the lines with the same reason
        page.locator("#lines [data-review]").first.click()
        page.wait_for_selector("#dlgClassify[open]")
        page.wait_for_timeout(300)
        page.locator("#dlgClassify").screenshot(path=str(OUT / "classify.png"))
        print("screenshot: docs/images/classify.png")
        page.locator("#btnClassifyCancel").click()

        # 4. Equivalents from other manufacturers for the non-compliant component
        page.locator("#lines tr", has_text="DEMO-LEADED-DIP8").locator("button[data-altline]").click()
        page.wait_for_selector("#altTable")
        page.wait_for_timeout(300)
        page.locator("#dlgAlt").screenshot(path=str(OUT / "equivalents.png"))
        print("screenshot: docs/images/equivalents.png")

        # 5. Replacement in the BOM: new verdict
        page.locator("#altTable [data-use]").first.click()
        page.wait_for_selector("#swapBar:not([hidden])")
        page.wait_for_timeout(500)
        shot(page, "replacement", clip=clip_between(page, "#swapBar", "#cards"), full_page=True)

        # 6. A RoHS exemption's page
        page.goto(f"{base}/#exemption/7(c)-I")
        page.wait_for_selector("#exemptInfo dt")
        page.wait_for_timeout(300)
        shot(page, "exemption", clip=clip_between(page, "#exemptDetail .back", "#exemptComps"), full_page=True)

        # 7. An ECCN code's page
        page.goto(f"{base}/#eccn/3A991.a.2")
        page.wait_for_selector("#eccnParts .part")
        page.wait_for_timeout(300)
        shot(page, "eccn", clip=clip_between(page, "#eccnDetail .back", "#eccnInfo"), full_page=True)

        # 8. Manufacturer page
        page.goto(f"{base}/#manufacturer/texas%20instruments")
        page.wait_for_selector("#mfrComps tbody tr")
        page.wait_for_timeout(300)
        shot(page, "manufacturer", clip=clip_between(page, "#mfrDetail .back", "#mfrComps", max_height=1100), full_page=True)

        # 9. Component page: regulations explained, exemption used, SVHC linked to the Candidate List
        page.goto(f"{base}/#exemption/7(c)-I")
        page.wait_for_selector("#exemptComps a.comp-link")
        page.locator("#exemptComps a.comp-link", has_text="CRCW060310K0FKEA").click()
        page.wait_for_selector("#compSheet .regcard")
        page.wait_for_timeout(300)
        shot(page, "component", clip=clip_between(page, "#tab-component .back", "#compSheet .regcards"), full_page=True)

        # 10. SVHC: a substance of the REACH Candidate List and the components that contain it
        page.goto(f"{base}/#svhc/lead")
        page.wait_for_selector("#svhcComps tbody tr a.comp-link")
        page.wait_for_timeout(300)
        shot(page, "svhc", clip=clip_between(page, "#svhcDetail .back", "#svhcComps"), full_page=True)

        # 11. Column mapping of a file in an unknown format (ERP export with a title block)
        page.set_viewport_size({"width": 1440, "height": 1500})
        page.goto(f"{base}/#analysis")
        page.set_input_files("#bomFile", str(ROOT / "examples" / "bom_erp_format.csv"))
        page.wait_for_selector("#dlgMap[open] #mapFields select")
        for field, column in (("mpn", "Supplier item code"), ("manufacturer", "Company"),
                              ("rohs3", "Directive 2015/863"), ("reach", "Regulation 1907/2006")):
            page.select_option(f"#mapFields select[name={field}]", column)
            page.wait_for_function("f => document.querySelector(`#mapFields select[name=${f}]`)?.value", arg=field)
            page.wait_for_load_state("networkidle")
        page.wait_for_selector("#mapDemo:not([hidden])")
        page.wait_for_timeout(400)
        page.locator("#dlgMap").screenshot(path=str(OUT / "column_mapping.png"))
        print("screenshot: docs/images/column_mapping.png")

        browser.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8002")
