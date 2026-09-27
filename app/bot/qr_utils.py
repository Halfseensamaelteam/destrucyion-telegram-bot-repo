"""
app.bot.qr_utils
~~~~~~~~~~~~~~~~~
Renders a Telethon qr_login().url as a PNG image suitable for sending via
the Telegram Bot API (client.send_photo expects file-like bytes).
"""

from __future__ import annotations

import io

import qrcode


def render_qr_png(url: str) -> io.BytesIO:
    """Render `url` as a PNG QR code, returned as an in-memory BytesIO."""
    img = qrcode.make(url)
    buf = io.BytesIO()
    buf.name = "connect_qr.png"
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
