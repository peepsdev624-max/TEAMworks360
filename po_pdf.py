"""
Generates a Purchase Order PDF matching the layout of the business's own
real PO master template (TEAM_Purchase_Order_MASTER_Derby/Phoenix.xlsx).
Used by /api/purchase-orders/<id>/pdf in app.py.
"""
import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.enums import TA_LEFT, TA_RIGHT

from pdf_common import esc, fmt_date, fmt_money, draw_header_and_footer, MARGIN_L, MARGIN_R, PAGE_W

TOP_MARGIN = 145
BOTTOM_MARGIN = 130

VAT_RATE_DEFAULT = 20.0

# Terms & Conditions, transcribed verbatim from the "Terms & Conditions" tab
# (2nd sheet) of TEAM's own PO master templates (TEAM_Purchase_Order_MASTER
# _Derby_.xlsx / _Phoenix_.xlsx) - both offices' templates carry identical
# wording, so one copy is used regardless of which office raised the PO.
# Each section is (heading, [body lines]); a line starting with "• "
# is rendered as an indented bullet, everything else as a normal paragraph.
TERMS_TITLE = "Purchase Order Terms & Conditions"
TERMS_SUBTITLE = "(For use with suppliers; includes cascading customer obligations and liability protections)"
TERMS_SECTIONS = [
    ("1. Definitions", [
        "“Buyer” means the entity issuing the Purchase Order.",
        "“Supplier” means the entity to whom the Purchase Order is addressed.",
        "“End Customer” means the Buyer’s customer for whom the goods/services are ultimately intended.",
        "“Goods/Services” means all products, materials, equipment, software, deliverables, or services described in the Purchase Order.",
        "“PO” or “Purchase Order” means the Buyer’s written order, including these Terms & Conditions.",
    ]),
    ("2. Order Acceptance", [
        "2.1 The Supplier accepts the PO by written confirmation, commencement of work, or delivery of Goods/Services.",
        "2.2 Any terms proposed by the Supplier that conflict with or supplement these Terms are rejected unless expressly agreed in writing by the Buyer.",
        "2.3 The PO constitutes the entire agreement between the parties unless a separate, signed supply agreement exists.",
    ]),
    ("3. Cascading End Customer Requirements", [
        "3.1 The Supplier acknowledges that the Goods/Services may form part of a wider contract between the Buyer and the End Customer.",
        "3.2 All obligations, specifications, standards, warranties, service levels, compliance requirements, and delivery commitments imposed on the Buyer by the End Customer that relate to the Supplier’s scope shall automatically cascade to the Supplier.",
        "3.3 The Supplier shall review and comply with all End Customer terms provided by the Buyer. Failure to request clarification is deemed acceptance.",
        "3.4 Where the End Customer updates or modifies requirements, the Supplier shall implement such changes at no additional cost unless agreed otherwise.",
    ]),
    ("4. Price and Payment", [
        "4.1 Prices stated in the PO are fixed, firm, and inclusive of all taxes, duties, packaging, freight, and insurance unless otherwise stated.",
        "4.2 Payment terms are as stated on the PO.",
        "4.3 The Buyer may withhold payment for:",
        "• incomplete or nonconforming deliveries",
        "• disputed invoices",
        "• unresolved defects or warranty claims",
        "4.4 No price increases shall apply without the Buyer’s prior written approval.",
    ]),
    ("5. Delivery, Title, and Risk", [
        "5.1 Delivery dates are mandatory. Time is of the essence.",
        "5.2 Title passes to the Buyer upon delivery or payment, whichever occurs first.",
        "5.3 Risk passes only upon formal acceptance by the Buyer.",
        "5.4 The Supplier shall notify the Buyer immediately of any actual or anticipated delay and take all necessary steps at its own cost to mitigate such delay.",
    ]),
    ("6. Quality, Compliance, and Warranty", [
        "6.1 The Supplier warrants that all Goods/Services shall:",
        "• conform to the PO, specifications, drawings, and End Customer requirements",
        "• be free from defects in design, materials, and workmanship",
        "• be fit for purpose and safe for use",
        "• comply with all applicable laws, regulations, and industry standards",
        "6.2 Warranty period shall be the longer of:",
        "• the Supplier’s standard warranty,",
        "• the Buyer’s requirement, or",
        "• the End Customer’s requirement.",
        "6.3 The Supplier shall remedy defects at its own cost within the timeframe required by the Buyer or End Customer.",
    ]),
    ("7. Changes and Variations", [
        "7.1 The Buyer may request changes to scope, specifications, or delivery.",
        "7.2 The Supplier shall not implement changes without written approval.",
        "7.3 Any cost or schedule impact must be submitted within 5 working days; otherwise, the Supplier waives entitlement to adjustment.",
    ]),
    ("8. Liability, Indemnity & Cost Recovery", [
        "8.1 The Supplier shall indemnify and hold harmless the Buyer from all losses, damages, liabilities, penalties, claims, and costs arising from:",
        "• defective or nonconforming Goods/Services",
        "• delays or failure to meet delivery commitments",
        "• breach of warranty or contract",
        "• infringement of intellectual property rights",
        "• Supplier negligence or misconduct",
        "8.2 Cascading Liability: Where the Buyer incurs liability, penalties, liquidated damages, service credits, or cost exposure to the End Customer due to the Supplier’s acts or omissions, the Supplier shall fully reimburse the Buyer for all such amounts, including administrative and legal costs.",
        "8.3 The Supplier’s liability shall not be capped where losses arise from:",
        "• breach of confidentiality",
        "• IP infringement",
        "• fraud, wilful misconduct, or gross negligence",
        "• regulatory noncompliance",
        "• third-party claims caused by the Supplier",
        "8.4 The Buyer’s liability to the Supplier is limited to the value of the PO.",
    ]),
    ("9. Insurance", [
        "9.1 The Supplier shall maintain adequate insurance, including:",
        "• Public/Product Liability",
        "• Professional Indemnity (for services)",
        "• Employer’s Liability",
        "• Cyber Liability (where applicable)",
        "9.2 Certificates of insurance shall be provided upon request.",
    ]),
    ("10. Intellectual Property", [
        "10.1 All IP created specifically for the Buyer or End Customer shall vest in the Buyer unless otherwise agreed.",
        "10.2 The Supplier warrants that the Goods/Services do not infringe third-party IP rights.",
    ]),
    ("11. Confidentiality", [
        "11.1 All information disclosed by the Buyer or End Customer is confidential.",
        "11.2 The Supplier shall not disclose or use such information except as required to fulfil the PO.",
    ]),
    ("12. Termination", [
        "12.1 The Buyer may terminate the PO immediately for:",
        "• Supplier breach",
        "• insolvency",
        "• failure to meet delivery or quality requirements",
        "• End Customer termination of the upstream contract",
        "12.2 Upon termination, the Supplier shall:",
        "• cease work",
        "• deliver all completed and in-progress materials",
        "• refund any overpayments",
        "12.3 The Buyer shall not be liable for loss of profit, loss of opportunity, or consequential losses.",
    ]),
    ("13. Audit Rights", [
        "13.1 The Buyer and End Customer may audit the Supplier’s facilities, processes, and records relating to the PO.",
        "13.2 The Supplier shall cooperate fully and provide access to relevant personnel and documentation.",
    ]),
    ("14. Subcontracting", [
        "14.1 The Supplier shall not subcontract without the Buyer’s written approval.",
        "14.2 The Supplier remains fully liable for all subcontractor performance.",
    ]),
    ("15. Governing Law and Jurisdiction", [
        "15.1 The PO is governed by the laws of the Buyer’s registered location unless otherwise stated.",
        "15.2 Disputes shall be resolved in the courts of that jurisdiction.",
    ]),
    ("16. Order of Precedence", [
        "If there is any conflict between documents, the order of precedence is:",
        "1. Purchase Order",
        "2. End Customer terms (where applicable)",
        "3. These Terms & Conditions",
        "4. Specifications, drawings, and statements of work",
        "5. Supplier proposals or quotations",
    ]),
]


def _address_block(company_name, line1, line2, city, postcode):
    parts = [company_name, line1, line2, city, postcode]
    return [p for p in parts if p]


def _terms_and_conditions_story():
    """Builds the Platypus flowables for the Terms & Conditions pages,
    appended after the main PO so every generated PO carries the same
    terms as the business's own master templates."""
    t_title = ParagraphStyle("t_title", fontName="Helvetica-Bold", fontSize=13, leading=16,
                              textColor=colors.HexColor("#1a2a4a"), spaceAfter=2)
    t_subtitle = ParagraphStyle("t_subtitle", fontName="Helvetica-Oblique", fontSize=8.5, leading=11,
                                 textColor=colors.HexColor("#555555"), spaceAfter=10)
    t_heading = ParagraphStyle("t_heading", fontName="Helvetica-Bold", fontSize=9.5, leading=13,
                                spaceBefore=8, spaceAfter=3)
    t_body = ParagraphStyle("t_body", fontName="Helvetica", fontSize=8, leading=11, spaceAfter=1.5)
    t_bullet = ParagraphStyle("t_bullet", parent=t_body, leftIndent=12)

    flow = [PageBreak(), Paragraph(esc(TERMS_TITLE), t_title), Paragraph(esc(TERMS_SUBTITLE), t_subtitle)]
    for heading, body_lines in TERMS_SECTIONS:
        flow.append(Paragraph(esc(heading), t_heading))
        for line in body_lines:
            if line.startswith("• "):
                flow.append(Paragraph(esc(line), t_bullet))
            else:
                flow.append(Paragraph(esc(line), t_body))
    return flow


def build_po_pdf(po, lines):
    """po: a dict-like row with PO fields + supplier_name/supplier_address +
    ship_to fields (ship_to_company_name/address_line1/address_line2/city/
    postcode) + job_number (the linked Works Order's number).
    lines: list of dict-like rows with qty/part_number/item_number/
    description/unit_price - may be empty, in which case a single line is
    built from the PO's own Description and po_value as a fallback, so
    every PO can produce a PDF regardless of whether it uses itemised
    lines."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=TOP_MARGIN, bottomMargin=BOTTOM_MARGIN,
                             leftMargin=MARGIN_L, rightMargin=MARGIN_R, title=f"Purchase Order {po['po_number']}")

    body = ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=13, alignment=TA_LEFT)
    bold = ParagraphStyle("bold", parent=body, fontName="Helvetica-Bold")
    label = ParagraphStyle("label", parent=body, fontName="Helvetica-Bold", fontSize=9.5)
    title_style = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=colors.HexColor("#1a2a4a"))
    right = ParagraphStyle("right", parent=body, alignment=TA_RIGHT)
    right_bold = ParagraphStyle("right_bold", parent=bold, alignment=TA_RIGHT)

    story = [Paragraph("Purchase Order", title_style), Spacer(1, 10)]

    # ---- Company address (left) / Date, PO No., WO No., Account No. (right) ----
    company_addr = _address_block("Total Engineering Asset Management Limited",
                                   po["ship_to_address_line1"], po["ship_to_address_line2"],
                                   po["ship_to_city"], po["ship_to_postcode"])
    left_col = [Paragraph(esc(l), body) for l in company_addr]
    left_col.append(Spacer(1, 4))
    left_col.append(Paragraph("Registered No. 11554487 - Registered in England", body))
    left_col.append(Paragraph("accounts@121team.co.uk", body))

    ref_rows = [
        ("Date:", fmt_date(po["order_date"])),
        ("P.O. No.", esc(po["po_number"])),
        ("Works Order No.", esc(po["job_number"] or "")),
        ("Account No.", esc(po["account_number"] or "")),
    ]
    right_col = [Paragraph(f"<b>{k}</b> {v}", body) for k, v in ref_rows]

    header_table = Table([[left_col, right_col]], colWidths=[(PAGE_W - MARGIN_L - MARGIN_R) * 0.55,
                                                                (PAGE_W - MARGIN_L - MARGIN_R) * 0.45])
    header_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(header_table)
    story.append(Spacer(1, 16))

    # ---- Supplier (left) / Deliver to (right) ----
    supplier_col = [Paragraph("<b>Supplier:</b>", label)]
    if po["supplier_name"]:
        supplier_col.append(Paragraph(esc(po["supplier_name"]), body))
    if po["supplier_address"]:
        for line in [l.strip() for l in po["supplier_address"].split(",")]:
            if line:
                supplier_col.append(Paragraph(esc(line), body))

    deliver_addr = _address_block(po["ship_to_company_name"] or "Total Engineering Asset Management Limited",
                                   po["ship_to_address_line1"], po["ship_to_address_line2"],
                                   po["ship_to_city"], po["ship_to_postcode"])
    deliver_col = [Paragraph("<b>Deliver to:</b>", label)]
    if deliver_addr:
        for l in deliver_addr:
            deliver_col.append(Paragraph(esc(l), body))
    else:
        deliver_col.append(Paragraph("<i>No Ship To address set for this PO</i>", body))

    supplier_table = Table([[supplier_col, deliver_col]], colWidths=[(PAGE_W - MARGIN_L - MARGIN_R) * 0.5] * 2)
    supplier_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(supplier_table)
    story.append(Spacer(1, 14))

    if po["date_required"]:
        story.append(Paragraph(f"<b>Date Required</b>  {fmt_date(po['date_required'])}", body))
        story.append(Spacer(1, 12))

    # ---- Line items ----
    if lines:
        line_rows = lines
    else:
        line_rows = [{
            "qty": 1, "part_number": "", "item_number": "",
            "description": po["description"] or po["po_number"],
            "unit_price": po["po_value"],
        }]

    table_header = ["Qty", "Part No.", "Item #", "Description", "Works Order No.", "Unit Price", "Line Total"]
    table_data = [table_header]
    subtotal = 0.0
    for ln in line_rows:
        qty = float(ln["qty"] or 0)
        unit_price = float(ln["unit_price"] or 0)
        line_total = qty * unit_price
        subtotal += line_total
        table_data.append([
            f"{qty:g}", esc(ln.get("part_number") or ""), esc(ln.get("item_number") or ""),
            Paragraph(esc(ln.get("description") or ""), body), esc(po["job_number"] or ""),
            fmt_money(unit_price), fmt_money(line_total),
        ])

    col_widths = [30, 60, 50, 165, 75, 60, 65]
    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2a4a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (5, 0), (6, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD3DC")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 10))

    # ---- Invoice to (left) / Subtotal, VAT, Total (right) ----
    invoice_col = [Paragraph("<b>Invoice to:</b>", label)]
    for l in deliver_addr:
        invoice_col.append(Paragraph(esc(l), body))

    vat_rate = float(po["vat_rate"] if po["vat_rate"] is not None else VAT_RATE_DEFAULT)
    vat_amount = subtotal * (vat_rate / 100.0)
    total = subtotal + vat_amount
    totals_rows = [
        ("Subtotal", fmt_money(subtotal)),
        (f"VAT ({vat_rate:g}%)", fmt_money(vat_amount)),
        ("Total", fmt_money(total)),
    ]
    totals_table = Table([[Paragraph(k, body), Paragraph(v, right)] for k, v in totals_rows[:-1]] +
                          [[Paragraph("<b>Total</b>", bold), Paragraph(f"<b>{totals_rows[-1][1]}</b>", right_bold)]],
                          colWidths=[70, 70])
    totals_table.setStyle(TableStyle([
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.HexColor("#1a2a4a")),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    bottom_table = Table([[invoice_col, totals_table]], colWidths=[(PAGE_W - MARGIN_L - MARGIN_R) * 0.55,
                                                                     (PAGE_W - MARGIN_L - MARGIN_R) * 0.45])
    bottom_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    story.append(bottom_table)
    story.append(Spacer(1, 16))

    story.append(Paragraph(f"<b>Raised by</b>  {esc(po['raised_by'] or '')}", body))

    story.extend(_terms_and_conditions_story())

    doc.build(story, onFirstPage=lambda c, d: draw_header_and_footer(c, d),
               onLaterPages=lambda c, d: draw_header_and_footer(c, d))

    return buf.getvalue()
