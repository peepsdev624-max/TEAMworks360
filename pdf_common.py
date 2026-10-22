"""
Shared utilities for PDF generation - the TEAM logo, official footer
(addresses, contact details, certification badges), and common formatting
helpers. Used by both quote_pdf.py and po_pdf.py so every document type
shares exactly the same branding, and any future change to it (a new
certification badge, an address change) only ever needs to happen here,
not separately in each document generator.
"""
import os
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "pdf_assets")
PAGE_W, PAGE_H = A4
MARGIN_L = 56
MARGIN_R = 56


def esc(s):
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_date(iso_date):
    if not iso_date:
        return ""
    try:
        return datetime.strptime(iso_date[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return iso_date


def fmt_money(value):
    try:
        return f"£{float(value):,.2f}"
    except (TypeError, ValueError):
        return "£0.00"


def draw_logo(canvas):
    """TEAM logo, centered at the top of the page."""
    logo_path = os.path.join(ASSETS_DIR, "logo.png")
    if os.path.exists(logo_path):
        logo_w, logo_h = 46 * mm, 22.4 * mm
        canvas.drawImage(logo_path, (PAGE_W - logo_w) / 2, PAGE_H - 30 * mm, width=logo_w, height=logo_h,
                          preserveAspectRatio=True, mask='auto')


def draw_footer(canvas):
    """The standard TEAM document footer: both office addresses, contact
    details, company registration, and the four certification badges."""
    footer_style = ParagraphStyle("footer", fontName="Helvetica", fontSize=7.6, leading=10,
                                   alignment=1, textColor=colors.HexColor("#333333"))
    footer_lines = [
        "Midlands Region &amp; Head Office \u2013 10 Enterprise Way, Jubilee Business Park, Derby, DE21 4BB.",
        "North Region - Unit 5, Phoenix Road, Crowther Industrial Estate, Washington Tyne &amp; Wear, NE38 0AD",
        "0333 3355 351&nbsp;&nbsp;&nbsp;info@121team.co.uk&nbsp;&nbsp;www.121team.co.uk",
        "Total Engineering Asset Management Limited \u2013 Company Registration No. 11554487 \u2013 Registered in England",
    ]
    y = 42 * mm
    for line in footer_lines:
        para = Paragraph(line, footer_style)
        w, h = para.wrap(PAGE_W - MARGIN_L - MARGIN_R, 20)
        para.drawOn(canvas, MARGIN_L, y)
        y -= (h + 1)

    badges = [
        ("badge_iso_ukas.jpg", 15 * mm),
        ("badge_safecontractor.png", 10 * mm),
        ("badge_safepqq.png", 10 * mm),
        ("badge_cemarking.png", 9 * mm),
    ]
    from PIL import Image as PILImage
    widths = []
    for filename, h in badges:
        path = os.path.join(ASSETS_DIR, filename)
        iw, ih = PILImage.open(path).size
        widths.append(h * (iw / ih))
    total_w = sum(widths) + 7 * mm * (len(badges) - 1)
    x = (PAGE_W - total_w) / 2
    badge_y = 8 * mm
    for (filename, h), w in zip(badges, widths):
        path = os.path.join(ASSETS_DIR, filename)
        if os.path.exists(path):
            canvas.drawImage(path, x, badge_y, width=w, height=h, preserveAspectRatio=True, mask='auto')
            x += w + 7 * mm


def draw_header_and_footer(canvas, doc=None):
    canvas.saveState()
    draw_logo(canvas)
    draw_footer(canvas)
    canvas.restoreState()
