# Architecture doc exports

Compiled reference documents generated from the diagrams in `docs/architecture/`,
plus the scripts that build them so they can be regenerated after any diagram
or content change.

## What's here

| File | What it is |
| --- | --- |
| `Linux-Fleet-Automation-Architecture-Reference.pdf` | Compiled PDF: title page, overview, all four diagrams, and the operational playbook reference table. |
| `Linux-Fleet-Automation-Architecture-Overview.pptx` | Compiled slide deck covering the same material, formatted for a management/stakeholder walkthrough. |
| `render_pngs.py` | Renders the four SVG diagrams (`../01-system-overview.svg`, etc.) to high-resolution PNGs used by both builds below. |
| `build_pdf.py` | ReportLab script that builds the PDF from those PNGs. |
| `build_pptx.js` | pptxgenjs script that builds the PPTX from the same PNGs. |

## Regenerating after a diagram change

1. Edit the relevant SVG under `docs/architecture/`.
2. Re-render the PNGs:

   ```bash
   pip install cairosvg pillow
   python3 docs/architecture/export/render_pngs.py
   ```

3. Rebuild the PDF:

   ```bash
   pip install reportlab pillow
   python3 docs/architecture/export/build_pdf.py
   ```

4. Rebuild the PPTX:

   ```bash
   npm install pptxgenjs
   node docs/architecture/export/build_pptx.js
   ```

Both build scripts write their output next to themselves in this folder, so
just commit the refreshed PNGs and the regenerated `.pdf` / `.pptx` alongside
your SVG change.

## Fonts (PDF build only)

`build_pdf.py` embeds Inter and DM Sans and expects them as TTF files in a
`fonts/` folder next to the script (or wherever the `FONT_DIR` environment
variable points). They're not checked into the repo to keep it lightweight.
Both are free, OFL-licensed, and available from Google Fonts:

- Inter: Regular, SemiBold, Bold — https://fonts.google.com/specimen/Inter
- DM Sans: Bold — https://fonts.google.com/specimen/DM+Sans

Download the four `.ttf` files into `docs/architecture/export/fonts/` (or set
`FONT_DIR` to wherever you keep them) before running `build_pdf.py`. The
PPTX build has no font dependency — it uses system fonts only (Calibri,
Trebuchet MS), per the slide font-embedding constraint.
