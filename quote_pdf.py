"""
Generates a customer-facing quote letter PDF, matching the layout of the
business's own real quote template (TMQ-2704_4_Off_Porta_Chuck_Services...).
Used by /api/quotes/<id>/pdf in app.py.
"""
import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.enums import TA_LEFT

from pdf_common import esc, fmt_date, fmt_money, draw_header_and_footer, MARGIN_L, MARGIN_R

TOP_MARGIN = 150
BOTTOM_MARGIN = 130

# Standard terms appended to the foot of every quote letter.
QUOTE_TERMS_TITLE = "Terms and Conditions of Quotation"
QUOTE_TERMS = [
    ("Validity", "This quotation is valid for 30 days from the date of issue."),
    ("Pricing Basis", "All prices contained within this quotation are based on material costs provided to "
                       "Total Engineering Asset Management (TEAM) with a validity period of 24 hours from the "
                       "date of this quotation."),
    ("Price Adjustments", "TEAM reserves the right to adjust the prices stated in this quotation to reflect any "
                           "subsequent increases in material costs occurring after the date of issue."),
    ("Right to Re-Quote", "In the event that an order is placed after material prices have increased, TEAM "
                           "reserves the right to issue a revised quotation to reflect the updated costs."),
]
QUOTE_TERMS_GENERAL_STATEMENT = (
    "We trust that the above meets with your requirements and is to your satisfaction. We look forward to the "
    "opportunity of working with you. Should you wish to discuss any aspect of this quotation further, please "
    "do not hesitate to contact us."
)


def build_quote_pdf(quote):
    """quote: a dict-like row with quote fields + customer_name/customer_address/customer_contact_name."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=TOP_MARGIN, bottomMargin=BOTTOM_MARGIN,
                             leftMargin=MARGIN_L, rightMargin=MARGIN_R, title=f"Quote {quote['quote_number']}")

    body = ParagraphStyle("body", fontName="Helvetica", fontSize=10.5, leading=15, spaceAfter=0, alignment=TA_LEFT)
    bold = ParagraphStyle("bold", parent=body, fontName="Helvetica-Bold")
    underline_label = ParagraphStyle("underline", parent=body)

    story = []

    contact_name = quote["contact_name"] or quote["customer_contact_name"] or ""
    customer_name = quote["customer_name"] or ""
    address = quote["customer_address"] or ""

    # ---- Recipient block ----
    if contact_name:
        story.append(Paragraph(esc(contact_name), body))
    if customer_name:
        story.append(Paragraph(esc(customer_name), body))
    for line in [l.strip() for l in address.split(",")]:
        if line:
            story.append(Paragraph(esc(line), body))
    story.append(Spacer(1, 12))

    # ---- Reference block ----
    story.append(Paragraph(f"Your Ref: {esc(quote['your_ref']) if quote['your_ref'] else 'TBC'}", body))
    quote_ref = f"Quote Ref: {esc(quote['quote_number'])}"
    if quote["works_order_ref"]:
        quote_ref += f" \u2013 {esc(quote['works_order_ref'])}"
    story.append(Paragraph(quote_ref, body))
    story.append(Paragraph(f"Date: {fmt_date(quote['quote_date']) or fmt_date(quote['created_date'])}", body))
    if quote["description"]:
        story.append(Paragraph(f"<b>{esc(quote['description'])} - {esc(quote['quote_number'])}</b>", bold))
    story.append(Spacer(1, 10))

    if contact_name:
        story.append(Paragraph(f"<u>For the attention of {esc(contact_name)}</u>", body))
        story.append(Spacer(1, 10))
        first_name = contact_name.split()[0]
        story.append(Paragraph(f"Dear {esc(first_name)},", body))
        story.append(Spacer(1, 10))

    story.append(Paragraph("Further to your recent enquiry, we are pleased to offer the following: -", body))
    story.append(Spacer(1, 8))

    # ---- Scope of work ----
    bullet_style = ParagraphStyle("bullet", parent=body, leftIndent=18, bulletIndent=6)
    sub_bullet_style = ParagraphStyle("subbullet", parent=body, leftIndent=36, bulletIndent=24)
    main_line = quote["description"] or quote["quote_number"]
    items = [l.strip() for l in (quote["scope_items"] or "").split("\n") if l.strip()]
    story.append(Paragraph(f"<b>{esc(main_line)}</b>", bullet_style, bulletText="\u2022"))
    for i in items:
        story.append(Paragraph(f"<b>{esc(i)}</b>", sub_bullet_style, bulletText="\u2022"))
    story.append(Spacer(1, 10))

    story.append(Paragraph(f"<u>Price</u>: {fmt_money(quote['estimated_value'])}", body))
    story.append(Paragraph(f"<u>Delivery</u>: {esc(quote['delivery_terms']) if quote['delivery_terms'] else 'Invoiced in Arrears.'}", body))
    story.append(Spacer(1, 12))

    story.append(Paragraph("If you require any further information, please do not hesitate to contact me.", body))
    story.append(Spacer(1, 12))
    story.append(Paragraph("Best regards", body))
    story.append(Spacer(1, 12))
    story.append(Paragraph(esc(quote["owner"]) if quote["owner"] else "", body))

    # ---- Terms and conditions ----
    terms_title_style = ParagraphStyle("terms_title", parent=bold, fontSize=10.5, spaceBefore=18, spaceAfter=6,
                                        alignment=1)
    terms_label_style = ParagraphStyle("terms_label", parent=bold, spaceBefore=6)
    terms_body_style = ParagraphStyle("terms_body", parent=body, spaceAfter=0)

    story.append(Paragraph(esc(QUOTE_TERMS_TITLE), terms_title_style))
    for label, text in QUOTE_TERMS:
        story.append(Paragraph(f"{esc(label)}:", terms_label_style))
        story.append(Paragraph(esc(text), terms_body_style))
    story.append(Paragraph("General Statement", terms_label_style))
    story.append(Paragraph(esc(QUOTE_TERMS_GENERAL_STATEMENT), terms_body_style))

    doc.build(story, onFirstPage=lambda c, d: draw_header_and_footer(c, d),
               onLaterPages=lambda c, d: draw_header_and_footer(c, d))

    return buf.getvalue()


