"""
app.bot.payment_qr_utils
~~~~~~~~~~~~~~~~~~~~~~~~
Renders a Midtrans QRIS string as a PNG image.
"""

from __future__ import annotations

import io

import qrcode


def render_payment_qr_png(qr_string: str) -> io.BytesIO:
    """Render a Midtrans QRIS string as a PNG image."""
    img = qrcode.make(qr_string)

    buf = io.BytesIO()
    buf.name = "payment_qr.png"
    img.save(buf, format="PNG")
    buf.seek(0)

    return buf
