"""
Renders a QR code to PNG bytes, for the "scan instead of typing the setup
key" option on the two-factor authentication setup screen (see totp.py and
app.py's get_2fa_setup/get_2fa_setup_qr).

This deliberately does NOT add a new dependency: the QR *encoding* itself
(turning text into the grid of light/dark modules - Reed-Solomon error
correction, version selection, module placement, mask selection) is done by
reportlab.graphics.barcode.qrencoder, which is already part of this app's
pinned reportlab dependency (used elsewhere for the quote/PO/despatch PDFs).
We use that encoder directly for just the module grid and draw the PNG
ourselves with Pillow (also already pinned), rather than going through
reportlab's QrCodeWidget + renderPM, because renderPM needs an optional
native rendering backend (rlPyCairo or a compiled _rl_renderPM) that isn't
guaranteed to be installed - the plain module grid is all that's actually
needed here, and it's straightforward to turn into a PNG by hand.
"""
import io

from reportlab.graphics.barcode import qrencoder
from PIL import Image


def render_png(text, scale=8, border=4):
    """A PNG (as bytes) of a QR code encoding `text`, at error-correction
    level M (tolerates some smudging/glare when scanned off a screen,
    without needing a needlessly large/dense code). `scale` is the pixel
    size of each module; `border` is the quiet-zone width in modules on
    every side (the blank margin QR scanners expect around the code)."""
    qr = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
    qr.addData(text)
    qr.make()
    modules = qr.getModuleCount()

    size = (modules + border * 2) * scale
    img = Image.new("L", (size, size), 255)
    px = img.load()
    for row in range(modules):
        for col in range(modules):
            if not qr.modules[row][col]:
                continue
            x0 = (col + border) * scale
            y0 = (row + border) * scale
            for dy in range(scale):
                for dx in range(scale):
                    px[x0 + dx, y0 + dy] = 0

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
