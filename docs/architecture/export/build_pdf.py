#!/usr/bin/env python3
"""Build the architecture-diagrams reference PDF."""
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, PageBreak, KeepTogether
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.colors import HexColor
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from PIL import Image as PILImage
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Point FONT_DIR at a local fonts/ folder next to this script, or override
# with the FONT_DIR environment variable. See README.md for where to get
# Inter and DM Sans (both free, OFL-licensed, via Google Fonts).
FONT_DIR = os.environ.get("FONT_DIR", os.path.join(SCRIPT_DIR, "fonts"))
pdfmetrics.registerFont(TTFont("Inter", f"{FONT_DIR}/Inter-Regular.ttf"))
pdfmetrics.registerFont(TTFont("Inter-SemiBold", f"{FONT_DIR}/Inter-SemiBold.ttf"))
pdfmetrics.registerFont(TTFont("Inter-Bold", f"{FONT_DIR}/Inter-Bold.ttf"))
pdfmetrics.registerFont(TTFont("DMSans-Bold", f"{FONT_DIR}/DMSans-Bold.ttf"))

# Nexus palette
BG = HexColor("#F7F6F2")
SURFACE = HexColor("#F9F8F5")
BORDER = HexColor("#D4D1CA")
TEXT = HexColor("#28251D")
TEXT_MUTED = HexColor("#7A7974")
ACCENT = HexColor("#01696F")

IMG_DIR = SCRIPT_DIR  # diagram PNGs and the built PDF live alongside this script
PAGE_W, PAGE_H = letter
MARGIN = 0.75 * inch
CONTENT_W = PAGE_W - 2 * MARGIN

styles = {
    "DocTitle": ParagraphStyle("DocTitle", fontName="DMSans-Bold", fontSize=28, leading=34, textColor=TEXT, spaceAfter=6),
    "DocSubtitle": ParagraphStyle("DocSubtitle", fontName="Inter", fontSize=13, leading=18, textColor=TEXT_MUTED, spaceAfter=4),
    "SectionTitle": ParagraphStyle("SectionTitle", fontName="DMSans-Bold", fontSize=17, leading=21, textColor=TEXT, spaceBefore=0, spaceAfter=8),
    "Body": ParagraphStyle("Body", fontName="Inter", fontSize=10.5, leading=15, textColor=TEXT, spaceAfter=8),
    "Caption": ParagraphStyle("Caption", fontName="Inter", fontSize=9, leading=13, textColor=TEXT_MUTED, spaceAfter=4),
    "TableHeader": ParagraphStyle("TableHeader", fontName="Inter-SemiBold", fontSize=8.5, leading=11, textColor=HexColor("#FFFFFF")),
    "TableCell": ParagraphStyle("TableCell", fontName="Inter", fontSize=8.5, leading=11, textColor=TEXT),
    "TableCellMono": ParagraphStyle("TableCellMono", fontName="Inter-SemiBold", fontSize=8.5, leading=11, textColor=ACCENT),
    "Footer": ParagraphStyle("Footer", fontName="Inter", fontSize=8, textColor=TEXT_MUTED),
}


def fitted_image(path, max_w, max_h):
    im = PILImage.open(path)
    iw, ih = im.size
    ratio = min(max_w / iw, max_h / ih)
    return Image(path, width=iw * ratio, height=ih * ratio)


def diagram_page(title, intro, img_file, points, max_img_h=4.7 * inch):
    story = []
    story.append(Paragraph(title, styles["SectionTitle"]))
    story.append(Paragraph(intro, styles["Body"]))
    story.append(Spacer(1, 6))
    story.append(fitted_image(f"{IMG_DIR}/{img_file}", CONTENT_W, max_img_h))
    story.append(Spacer(1, 10))
    for p in points:
        story.append(Paragraph(f"&bull;&nbsp;&nbsp;{p}", styles["Body"]))
    return story


def header_footer(canvas_obj, doc):
    canvas_obj.saveState()
    canvas_obj.setFillColor(BG)
    canvas_obj.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    canvas_obj.setFont("Inter", 8)
    canvas_obj.setFillColor(TEXT_MUTED)
    canvas_obj.drawString(MARGIN, 0.5 * inch, "Linux Fleet Automation \u2014 Architecture Reference")
    canvas_obj.drawRightString(PAGE_W - MARGIN, 0.5 * inch, f"Page {doc.page}")
    canvas_obj.setStrokeColor(BORDER)
    canvas_obj.setLineWidth(0.5)
    canvas_obj.line(MARGIN, 0.68 * inch, PAGE_W - MARGIN, 0.68 * inch)
    canvas_obj.restoreState()


def title_page_bg(canvas_obj, doc):
    canvas_obj.saveState()
    canvas_obj.setFillColor(HexColor("#171614"))
    canvas_obj.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    canvas_obj.setFillColor(ACCENT)
    canvas_obj.rect(0, PAGE_H - 0.12 * inch, PAGE_W, 0.12 * inch, fill=1, stroke=0)
    canvas_obj.restoreState()


def build():
    doc = SimpleDocTemplate(
        f"{IMG_DIR}/Linux-Fleet-Automation-Architecture-Reference.pdf",
        pagesize=letter,
        title="Linux Fleet Automation \u2014 Architecture Reference",
        author="Perplexity Computer",
        topMargin=1.0 * inch,
        bottomMargin=0.9 * inch,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
    )
    story = []

    # ---- Title page ----
    title_style = ParagraphStyle("CoverTitle", fontName="DMSans-Bold", fontSize=32, leading=38, textColor=HexColor("#F7F6F2"))
    sub_style = ParagraphStyle("CoverSub", fontName="Inter", fontSize=13, leading=19, textColor=HexColor("#B9B6AE"), spaceBefore=14)
    meta_style = ParagraphStyle("CoverMeta", fontName="Inter", fontSize=10, leading=14, textColor=HexColor("#7A7974"))

    story.append(Spacer(1, 2.6 * inch))
    story.append(Paragraph("Linux Fleet Automation", title_style))
    story.append(Paragraph("System Architecture Reference", title_style))
    story.append(Paragraph(
        "Centralized logging, host visibility, and identity-aware automation for the managed Linux estate \u2014 "
        "pipeline design, role execution order, and operational playbooks.",
        sub_style,
    ))
    story.append(Spacer(1, 1.6 * inch))
    story.append(Paragraph("September 2026", meta_style))
    story.append(PageBreak())

    # ---- Overview page ----
    story.append(Paragraph("Overview", styles["SectionTitle"]))
    story.append(Paragraph(
        "This document is a visual reference for the automation stack that provides centralized log collection, "
        "host inventory reporting, and a unified operational dashboard across the managed Red Hat / Linux fleet. "
        "It covers four views: the overall system layout, the internal pipeline design of the collector host, and the "
        "execution order of the two primary automation playbooks that build and maintain the stack.",
        styles["Body"],
    ))
    story.append(Paragraph(
        "The platform is built as reusable, idempotent configuration-management roles, organized so that each "
        "capability (log ingestion, dashboard rendering, health checks, alerting) can be deployed, tested, and rolled "
        "back independently. It is currently being validated in a controlled test environment ahead of promotion into "
        "the production estate.",
        styles["Body"],
    ))
    story.append(Spacer(1, 6))
    story.append(Paragraph("What's inside", ParagraphStyle("mini", fontName="Inter-SemiBold", fontSize=11, textColor=TEXT, spaceAfter=6)))
    for i, t in enumerate([
        "System overview \u2014 how log clients, the identity realm, the collector host, dashboards, and email alerting fit together",
        "Log collector internals \u2014 the two pipelines that run on the collector host: centralized logging and the fleet dashboard",
        "Centralized logging automation \u2014 the role execution order used to build the logging pipeline end to end",
        "Dashboard automation \u2014 the orchestration used to build the fleet inventory and reporting dashboard",
        "Operational playbook reference \u2014 the supporting playbooks used for day-to-day operations and maintenance",
    ], 1):
        story.append(Paragraph(f"{i}. {t}", styles["Body"]))
    story.append(PageBreak())

    # ---- Diagram 1 ----
    story.extend(diagram_page(
        "1. System Overview",
        "End-to-end view of how fleet hosts, the identity realm, the collector host, browser-based dashboards, and "
        "email alerting connect.",
        "01-system-overview.png",
        [
            "Every managed host forwards logs over TLS to a single collector host and authenticates against a shared identity realm.",
            "The collector host stores structured events in a database and flat files, and serves two independent web dashboards.",
            "Alert conditions (service failures, disk thresholds, sync issues) are emailed out through a relay on the collector host.",
        ],
    ))
    story.append(PageBreak())

    # ---- Diagram 2 ----
    story.extend(diagram_page(
        "2. Inside the Collector Host \u2014 Two Pipelines",
        "The collector host runs two independent pipelines side by side, sharing the same host but not the same data path.",
        "02-log-collector-pipelines.png",
        [
            "Centralized Logging: a syslog listener accepts events on a dedicated port, writes them to a database and flat "
            "files, and serves a log search interface over HTTP(S). A set of watcher roles monitor for failures and send email alerts.",
            "Fleet Dashboard: a set of local collection roles gather host inventory, report data, and configuration snapshots directly "
            "on the collector host (no database), and serve them through a second, independent web interface.",
            "Both interfaces are lightweight, dependency-free web applications; a shared authentication layer is being upgraded to "
            "identity-realm-based single sign-on with corporate-issued TLS certificates.",
        ],
    ))
    story.append(PageBreak())

    # ---- Diagram 3 ----
    story.extend(diagram_page(
        "3. Centralized Logging \u2014 Automation Role Order",
        "The centralized logging automation applies roles in a fixed, dependency-aware order across three host groups: "
        "the collector, log-forwarding clients, and the identity realm.",
        "03-centralized-logging-role-order.png",
        [
            "Collector-side roles provision the database, syslog listener, log search interface, and the alerting/watcher roles, in that order.",
            "Client-side roles configure each fleet host to forward logs securely to the collector.",
            "Identity-realm roles ensure authentication and time synchronization are consistent across the estate before the pipeline goes live.",
        ],
        max_img_h=5.0 * inch,
    ))
    story.append(PageBreak())

    # ---- Diagram 4 ----
    story.extend(diagram_page(
        "4. Fleet Dashboard \u2014 Automation Orchestration",
        "The dashboard automation gathers host inventory and reporting data locally on the collector host and renders "
        "it through a dedicated web interface \u2014 no external database required.",
        "04-linuxdb-dashboard-role-order.png",
        [
            "Host inventory, reporting, and configuration-snapshot roles run first and write structured data to the local filesystem.",
            "The dashboard role then reads that data directly to render the fleet-wide view \u2014 keeping the design simple and dependency-free.",
        ],
    ))
    story.append(PageBreak())

    # ---- Playbook table ----
    story.append(Paragraph("Operational Playbook Reference", styles["SectionTitle"]))
    story.append(Paragraph(
        "Supporting playbooks used alongside the two primary automation flows for day-to-day operations, health checks, "
        "and maintenance tasks.",
        styles["Body"],
    ))
    story.append(Spacer(1, 6))

    rows = [
        ["Playbook", "Purpose", "Target group"],
        ["idm_healthcheck", "Validates identity realm services and replication health", "idm"],
        ["system_update / update_system", "Applies patches through the package manager, with pre/post checks", "all / fleet-wide"],
        ["system_poweroff / poweroff_all", "Controlled shutdown of a single host or the full fleet", "targeted / all"],
        ["silence_setroubleshoot", "Suppresses noisy SELinux troubleshooting notifications", "log_clients"],
        ["sudo_audit_report", "Reports on sudo usage and privilege escalation events", "all"],
        ["dnf_history", "Captures package transaction history for audit purposes", "all"],
        ["gather_critical_alerts", "Collects and summarizes critical alert conditions fleet-wide", "log_collector"],
        ["collect_ilo", "Gathers out-of-band (iLO/BMC) health data from physical hosts", "physical hosts"],
        ["deploy_timesync_role", "Deploys and standardizes time synchronization configuration (redhat.rhel_system_roles.timesync)", "all"],
        ["idm_copy_gssapi_delegation", "Applies GSSAPI delegation configuration for identity-aware services", "idm"],
        ["push_script_and_run", "Ad hoc utility to push and execute a script across target hosts", "variable"],
        ["lineage", "Diagnostic playbook to identify why a service or process is active", "targeted"],
    ]

    table_data = [[Paragraph(rows[0][0], styles["TableHeader"]), Paragraph(rows[0][1], styles["TableHeader"]), Paragraph(rows[0][2], styles["TableHeader"])]]
    for r in rows[1:]:
        table_data.append([
            Paragraph(r[0], styles["TableCellMono"]),
            Paragraph(r[1], styles["TableCell"]),
            Paragraph(r[2], styles["TableCell"]),
        ])

    col_widths = [1.9 * inch, 3.4 * inch, CONTENT_W - 1.9 * inch - 3.4 * inch]
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [SURFACE, BG]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(t)
    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "Note: service_failure_alert has been retired and superseded by the watcher roles in the centralized logging pipeline.",
        styles["Caption"],
    ))

    doc.build(story, onFirstPage=title_page_bg, onLaterPages=header_footer)
    print("Built PDF")


if __name__ == "__main__":
    build()
