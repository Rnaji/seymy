"""
Builds the blank "SMS Test Recipient Consent" paper form (PDF + PNG) served at
/assets/seymy-sms-test-consent-form.{pdf,png}.

Every program, opt-in and disclosure sentence is read from the published legal
templates, never retyped, so the paper form always says exactly what /privacy
and /terms say:
  - landing/templates/landing/legal/sms_internal_testing.html (program block)
  - landing/templates/landing/legal/privacy.html (sharing sentence)
  - landing/templates/landing/legal/contact.html (support email)

Not part of the site build (reportlab and pypdfium2 are not site dependencies).
Run it from the repository root in a separate environment:
    python -m venv /tmp/formenv
    /tmp/formenv/bin/pip install reportlab==4.2.5 pypdfium2==4.30.0
    /tmp/formenv/bin/python tools/sms_test_consent_form/build.py
then update the version below, the page (sms_test_consent.html) and its tests.
"""
import html
import re
import sys
from pathlib import Path

from reportlab import rl_config

rl_config.invariant = 1  # reproducible output: no timestamp, fixed document id

from reportlab.lib.colors import Color  # noqa: E402
from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.lib.utils import simpleSplit  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

VERSION = 'Version 1.2  |  November 2026'
ROOT = Path(__file__).resolve().parents[2]
LEGAL = ROOT / 'landing' / 'templates' / 'landing' / 'legal'
OUT = ROOT / 'public_root' / 'assets'

NAVY = Color(0.078431, 0.152941, 0.262745)
MUTED = Color(0.32549, 0.384314, 0.458824)
LINE = Color(0.839216, 0.878431, 0.929412)
BOX_BG = Color(0.945098, 0.960784, 0.984314)
BLUE = Color(0.196078, 0.396078, 0.85098)
WHITE = Color(1, 1, 1)


def _strip_comments(source):
    return re.sub(r'{% comment %}.*?{% endcomment %}', '', source, flags=re.S)


def _text(fragment):
    return html.unescape(re.sub(r'<[^>]+>', '', fragment)).strip()


def legal_texts():
    """The published wording, read from the legal templates."""
    block = _strip_comments((LEGAL / 'sms_internal_testing.html').read_text())
    items = {_text(dt): _text(dd) for dt, dd in re.findall(r'<dt>(.*?)</dt><dd>(.*?)</dd>', block, flags=re.S)}
    expected = ['Program', 'How testers opt in', 'Message frequency', 'Costs', 'Opt out', 'Help', 'Delivery']
    if list(items) != expected:
        sys.exit(f'Unexpected SMS block structure: {list(items)}')

    opt_in = items['How testers opt in']
    match = re.search(r'Testers agree to this statement: “(.*?)” ', opt_in)
    if not match:
        sys.exit('Consent statement not found in the SMS block.')
    statement = match.group(1)
    opt_in_rest = opt_in.replace(match.group(0), '')

    privacy = _strip_comments((LEGAL / 'privacy.html').read_text())
    sharing = _text(re.search(r'<p class="legal-sms__sharing">(.*?)</p>', privacy, flags=re.S).group(1))
    email = re.search(r'mailto:([^"]+)"', (LEGAL / 'contact.html').read_text()).group(1)

    help_text = items['Help']
    if help_text != 'For help with the testing program, contact us using the details in the Contact section below.':
        sys.exit('The Help sentence changed: update the paper-form adaptation below.')
    return {
        'program': items['Program'],
        'opt_in': opt_in_rest,
        'statement': statement,
        'disclosures': [
            items['Message frequency'],
            items['Costs'],
            items['Opt out'],
            # Paper adaptation of the Help sentence: the form has no Contact
            # section, so it gives the address that section shows.
            f'Reply HELP for help. For help with the testing program, contact us at {email}.',
            items['Delivery'],
            sharing,
        ],
    }


def _rounded_band(c, x, y, w, h, r, fill):
    c.setFillColor(fill)
    c.roundRect(x, y, w, h, r, stroke=0, fill=1)


def _paragraph(c, text, x, y, width, font='Helvetica', size=10, leading=12.7):
    c.setFont(font, size)
    for line in simpleSplit(text, font, size, width):
        c.drawString(x, y, line)
        y -= leading
    return y


def build_pdf(texts, path):
    c = canvas.Canvas(str(path), pagesize=letter, pageCompression=0)
    c.setTitle('Seymy SMS Test Recipient Consent Form (blank)')
    c.setAuthor('Seymy, Inc.')
    c.setSubject('Seymy Internal SMS Testing: blank consent form')
    c.setCreator('Seymy, Inc.')
    left, right = 43, 569
    width = right - left

    # Header band
    _rounded_band(c, 42, 676, 528, 72, 14, NAVY)
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 22)
    c.drawString(60, 716, 'Seymy')
    c.setFont('Helvetica', 10)
    c.drawRightString(552, 718, 'INTERNAL SMS TESTING')
    c.setFont('Helvetica', 9)
    c.drawRightString(552, 700, 'CONSENT FORM')

    c.setFillColor(NAVY)
    c.setFont('Helvetica-Bold', 17)
    c.drawString(left, 647, 'SMS Test Recipient Consent')
    c.setFillColor(MUTED)
    c.setFont('Helvetica', 10.3)
    c.drawString(left, 625, 'Seymy, Inc.  |  Seymy Internal SMS Testing  |  Internal software testing only')
    c.setStrokeColor(LINE)
    c.setLineWidth(1)
    c.line(left, 610, right, 610)

    y = 590
    c.setFillColor(NAVY)
    for heading, body in [('Program', texts['program']), ('How testers opt in', texts['opt_in'])]:
        c.setFont('Helvetica-Bold', 10.5)
        c.drawString(left, y, heading)
        y = _paragraph(c, body, left, y - 15, width) - 7

    c.setFont('Helvetica-Bold', 10.5)
    c.drawString(left, y, 'Important disclosures')
    y -= 15
    for item in texts['disclosures']:
        c.setFont('Helvetica', 10)
        c.drawString(55, y, '-')
        y = _paragraph(c, item, 70, y, right - 70) - 2.5

    # Consent box
    statement_lines = simpleSplit(texts['statement'], 'Helvetica', 9.7, right - 14 - 82)
    box_top = y - 4
    box_h = 30 + 13 * len(statement_lines) + 10
    _rounded_band(c, left, box_top - box_h, width, box_h, 9, BOX_BG)
    c.setFillColor(NAVY)
    c.setFont('Helvetica-Bold', 10.5)
    c.drawString(57, box_top - 17, 'Consent - please check the box and sign')
    c.setStrokeColor(BLUE)
    c.setLineWidth(1.5)
    c.rect(57, box_top - 49, 15, 15, stroke=1, fill=0)
    ty = box_top - 38
    c.setFont('Helvetica', 9.7)
    for line in statement_lines:
        c.drawString(82, ty, line)
        ty -= 13
    y = box_top - box_h - 26

    # Fields
    c.setLineWidth(1)
    for labels in [('Tester full name', 'Mobile phone number (including country code)'),
                   ('Signature', 'Date (MM/DD/YYYY)')]:
        c.setFillColor(NAVY)
        c.setFont('Helvetica-Bold', 10)
        c.drawString(left, y, labels[0])
        c.drawString(318, y, labels[1])
        c.setStrokeColor(LINE)
        c.line(left, y - 31, 286, y - 31)
        c.line(318, y - 31, right, y - 31)
        y -= 54

    c.setFillColor(MUTED)
    c.setFont('Helvetica', 9)
    for line in ['Privacy Policy: https://www.seymy.com/privacy',
                 'Terms & Conditions: https://www.seymy.com/terms',
                 'Seymy retains the completed, signed form privately as evidence of consent.']:
        c.drawString(left, y, line)
        y -= 14
    footer_rule = 59
    if y < footer_rule + 4:
        sys.exit(f'Content overflows the page (y={y}).')
    c.setStrokeColor(LINE)
    c.line(left, footer_rule, right, footer_rule)
    c.setFont('Helvetica', 8.2)
    c.drawString(left, 44, 'Seymy, Inc.  |  Blank form for consent collection and A2P review')
    c.drawRightString(right, 44, VERSION)
    c.showPage()
    c.save()


def build_png(pdf_path, png_path):
    import pypdfium2
    from PIL import Image

    page = pypdfium2.PdfDocument(str(pdf_path))[0]
    image = page.render(scale=2).to_pil().convert('RGB')
    assert image.size == (1224, 1584), image.size
    Image.Image.save(image, png_path, format='PNG', optimize=True)  # no text chunks, no metadata


if __name__ == '__main__':
    texts = legal_texts()
    pdf = OUT / 'seymy-sms-test-consent-form.pdf'
    png = OUT / 'seymy-sms-test-consent-form.png'
    build_pdf(texts, pdf)
    build_png(pdf, png)
    print(f'wrote {pdf.relative_to(ROOT)} and {png.relative_to(ROOT)}')
