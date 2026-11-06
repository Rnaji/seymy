"""
The landing's single download channel.

One setting pair, ``SITE_DOWNLOAD_URL`` and ``SITE_DOWNLOAD_LABEL``, drives
every call to action on the site. Without a publishable URL there is no link
at all: the CTAs become a neutral "private pilot" note, so the site can never
show a dead or provisional download link. The desktop QR code is generated
from the same URL at render time; no image is hard-coded.
"""
from functools import lru_cache
from urllib.parse import urlsplit

from django.conf import settings
from django.utils.safestring import mark_safe

DEFAULT_DOWNLOAD_LABEL = 'Get the Seymy app'
PILOT_NOTE = 'Seymy is in private pilot.'


def publishable_download_url(url: str) -> str:
    """Return ``url`` if it is a plain https link, else ``''``."""
    url = (url or '').strip()
    if not url or any(ch.isspace() for ch in url):
        return ''
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        return ''
    return url


@lru_cache(maxsize=8)
def qr_svg(url: str) -> str:
    """Inline SVG QR code for ``url`` (no file, no third-party request)."""
    import qrcode
    from qrcode.image.svg import SvgPathImage

    image = qrcode.make(url, image_factory=SvgPathImage, border=2, box_size=10)
    svg = image.to_string(encoding='unicode')
    if svg.startswith('<?xml'):
        svg = svg[svg.index('?>') + 2:].lstrip()
    return svg.replace('<svg ', '<svg role="img" aria-label="QR code to download Seymy" ', 1)


def download_cta():
    """Template context for the CTA partial: a dict, or ``None`` (pilot note)."""
    url = publishable_download_url(getattr(settings, 'SITE_DOWNLOAD_URL', ''))
    if not url:
        return None
    label = (getattr(settings, 'SITE_DOWNLOAD_LABEL', '') or '').strip() or DEFAULT_DOWNLOAD_LABEL
    return {'url': url, 'label': label, 'qr_svg': mark_safe(qr_svg(url))}
