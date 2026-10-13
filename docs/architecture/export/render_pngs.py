#!/usr/bin/env python3
"""
Regenerates the high-resolution PNG diagrams used by build_pdf.py and
build_pptx.js from the SVG sources in the parent docs/architecture/ folder.

Run this after editing any of the four architecture SVGs, then re-run
build_pdf.py / build_pptx.js to refresh the exported documents.

Usage (from anywhere, after `pip install cairosvg`):

    python3 docs/architecture/export/render_pngs.py
"""
import os
import cairosvg

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SVG_DIR = os.path.dirname(SCRIPT_DIR)  # docs/architecture/

DIAGRAMS = [
    "01-system-overview",
    "02-log-collector-pipelines",
    "03-centralized-logging-role-order",
    "04-linuxdb-dashboard-role-order",
]


def main():
    for name in DIAGRAMS:
        svg_path = os.path.join(SVG_DIR, f"{name}.svg")
        png_path = os.path.join(SCRIPT_DIR, f"{name}.png")
        cairosvg.svg2png(url=svg_path, write_to=png_path, scale=4.0)
        print(f"{name}.png done")


if __name__ == "__main__":
    main()
