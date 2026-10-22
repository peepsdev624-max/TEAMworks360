"""
Generates a Despatch Note PDF using the same TEAM-branded header/footer as
quote_pdf.py and po_pdf.py (see pdf_common.py), so every document type the
app produces looks consistent. Used by /api/despatch-notes/<id>/pdf in
app.py.

Despatch notes carry no pricing - they're a record of what physically left
the building, not a financial document - so unlike po_pdf.py this has no
VAT/subtotal/total section.
"""
import io

from PIL import Image as PILImage
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
from reportlab.lib.enums import TA_LEFT

from pdf_common import esc, fmt_date, draw_header_and_footer, MARGIN_L, MARGIN_R, PAGE_W

TOP_MARGIN = 145
BOTTOM_MARGIN = 100

# Bounding box the captured signature image is scaled to fit within,
# preserving its aspect ratio - wide enough to read clearly, short enough
# to never push the sign-off section onto a second page.
SIGNATURE_MAX_W = 160
SIGNATURE_MAX_H = 50


def build_despatch_pdf(dn, lines, signature_path=None):
    """dn: a dict-like row with despatch_notes fields + job_number (the
    linked Works Order's number) + customer_name.
    lines: list of dict-like rows with qty/part_number/description - may be
    empty, in which case a single line is built from the despatch note's
    own notes field as a fallback, so every despatch note can produce a
    PDF regardless of whether it uses itemised lines.
    signature_path: path to a captured signature PNG on disk (see
    /api/despatch-notes/<id>/signature in app.py), or None if this despatch
    note hasn't been signed for yet - in which case the sign-off section
    below falls back to the original blank boxes, meant to be filled in
    and signed by hand on a printed copy."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=TOP_MARGIN, bottomMargin=BOTTOM_MARGIN,
                             leftMargin=MARGIN_L, rightMargin=MARGIN_R,
                             title=f"Despatch Note {dn['despatch_number']}")

    body = ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=13, alignment=TA_LEFT)
    label = ParagraphStyle("label", parent=body, fontName="Helvetica-Bold", fontSize=9.5)
    title_style = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=18, leading=22,
                                  textColor=colors.HexColor("#1a2a4a"))

    story = [Paragraph("Despatch Note", title_style), Spacer(1, 10)]

    # ---- Company name (left) / Date, Despatch No., WO No. (right) ----
    left_col = [
        Paragraph("Total Engineering Asset Management Limited", body),
        Spacer(1, 4),
        Paragraph("Registered No. 11554487 - Registered in England", body),
        Paragraph("info@121team.co.uk", body),
    ]
    ref_rows = [
        ("Date:", fmt_date(dn["despatch_date"])),
        ("Despatch No.", esc(dn["despatch_number"])),
        ("Works Order No.", esc(dn["job_number"] or "")),
    ]
    right_col = [Paragraph(f"<b>{k}</b> {v}", body) for k, v in ref_rows]

    header_table = Table([[left_col, right_col]], colWidths=[(PAGE_W - MARGIN_L - MARGIN_R) * 0.55,
                                                                (PAGE_W - MARGIN_L - MARGIN_R) * 0.45])
    header_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(header_table)
    story.append(Spacer(1, 16))

    # ---- Customer (left) / Deliver to (right) ----
    customer_col = [Paragraph("<b>Customer:</b>", label)]
    if dn["customer_name"]:
        customer_col.append(Paragraph(esc(dn["customer_name"]), body))

    deliver_col = [Paragraph("<b>Deliver to:</b>", label)]
    if dn["delivery_address"]:
        for line in [l.strip() for l in dn["delivery_address"].splitlines()]:
            if line:
                deliver_col.append(Paragraph(esc(line), body))
    else:
        deliver_col.append(Paragraph("<i>No delivery address set for this despatch note</i>", body))

    addr_table = Table([[customer_col, deliver_col]], colWidths=[(PAGE_W - MARGIN_L - MARGIN_R) * 0.5] * 2)
    addr_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(addr_table)
    story.append(Spacer(1, 14))

    if dn["carrier"]:
        story.append(Paragraph(f"<b>Carrier / Method</b>  {esc(dn['carrier'])}", body))
        story.append(Spacer(1, 10))

    # ---- Line items ----
    if lines:
        line_rows = lines
    else:
        line_rows = [{"qty": 1, "part_number": "", "description": dn["notes"] or dn["despatch_number"]}]

    table_header = ["Qty", "Part No.", "Description"]
    table_data = [table_header]
    for ln in line_rows:
        qty = float(ln["qty"] or 0)
        table_data.append([
            f"{qty:g}", esc(ln.get("part_number") or ""), Paragraph(esc(ln.get("description") or ""), body),
        ])

    col_widths = [40, 100, (PAGE_W - MARGIN_L - MARGIN_R) - 140]
    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2a4a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD3DC")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 16))

    if dn["notes"]:
        story.append(Paragraph("<b>Notes</b>", label))
        story.append(Paragraph(esc(dn["notes"]), body))
        story.append(Spacer(1, 12))

    story.append(Paragraph(f"<b>Despatched by</b>  {esc(dn['despatched_by'] or '')}", body))

    # ---- Customer sign-off. When the note has been signed for on-screen
    # (a phone/tablet, at the point of delivery - see
    # /api/despatch-notes/<id>/signature in app.py), the captured name,
    # date and signature image are shown here as the audit record of what
    # was received. Otherwise this falls back to the original blank boxes,
    # meant to be filled in and signed by hand on a printed copy - kept so
    # a despatch note that's never digitally signed still produces a
    # perfectly usable paper sign-off sheet. ----
    small_label = ParagraphStyle("small_label", fontName="Helvetica", fontSize=8,
                                  textColor=colors.HexColor("#555555"))
    content_w = PAGE_W - MARGIN_L - MARGIN_R

    story.append(Spacer(1, 26))
    story.append(Paragraph("<b>Received by (Customer)</b>", label))
    story.append(Spacer(1, 16))

    if signature_path:
        name_date_table = Table(
            [[Paragraph(esc(dn["signature_name"] or ""), body), Paragraph(fmt_date(dn["signature_date"]), body)],
             [Paragraph("Print Name", small_label), Paragraph("Date", small_label)]],
            colWidths=[content_w * 0.65, content_w * 0.35], rowHeights=[18, 14],
        )
        name_date_table.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#333333")),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (1, 0), (1, -1), 0),
        ]))
        story.append(name_date_table)
        story.append(Spacer(1, 16))

        # Scales the actual captured image down to fit the bounding box
        # while preserving its aspect ratio, rather than stretching it to a
        # fixed size - a wide, short signature and a narrow, tall one both
        # end up legible instead of distorted.
        try:
            with PILImage.open(signature_path) as im:
                img_w, img_h = im.size
        except (OSError, ValueError):
            img_w, img_h = None, None

        if img_w and img_h:
            scale = min(SIGNATURE_MAX_W / img_w, SIGNATURE_MAX_H / img_h, 1)
            story.append(RLImage(signature_path, width=img_w * scale, height=img_h * scale))
        else:
            story.append(Paragraph("<i>Signature image could not be loaded</i>", small_label))
        story.append(Spacer(1, 2))
        story.append(Paragraph("Signature", small_label))
    else:
        name_date_table = Table(
            [["", ""], [Paragraph("Print Name", small_label), Paragraph("Date", small_label)]],
            colWidths=[content_w * 0.65, content_w * 0.35], rowHeights=[24, 14],
        )
        name_date_table.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#333333")),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (1, 0), (1, -1), 0),
        ]))
        story.append(name_date_table)
        story.append(Spacer(1, 16))

        signature_table = Table(
            [[""], [Paragraph("Signature", small_label)]],
            colWidths=[content_w], rowHeights=[24, 14],
        )
        signature_table.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#333333")),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(signature_table)

    doc.build(story, onFirstPage=lambda c, d: draw_header_and_footer(c, d),
               onLaterPages=lambda c, d: draw_header_and_footer(c, d))

    return buf.getvalue()
