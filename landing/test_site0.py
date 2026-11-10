"""
SITE-0 containment tests.

SimpleTestCase forbids every database query, so each test also proves the
public surface works without the (dead) Render Postgres.
"""
from django.test import SimpleTestCase
from django.urls import NoReverseMatch, URLPattern, URLResolver, get_resolver, reverse

TOKEN = 'abcDEF123_-xyz'

PUBLIC_PAGES = ['/', '/privacy', '/terms', '/sms-test-consent']

RETIRED_FUNNEL_PATHS = [
    '/start',
    '/start/',
    '/start/new/',
    '/start/summary/',
    '/start/business/',
    '/start/estimate/pdf/',
    '/start/deposit/',
    '/start/deposit/configure/',
    '/start/payment/',
    '/start/review/',
    '/start/sent/',
    '/start/deposit/paid/',
]

RETIRED_CUSTOMER_PATHS = [
    f'/e/{TOKEN}/pay/',
    f'/e/{TOKEN}/document/',
    f'/e/{TOKEN}/pay/success/',
    f'/e/{TOKEN}/pay/test-confirm/',
]

REMOVED_PATHS = [
    '/webhooks/stripe/',
    '/admin/',
    '/admin/login/',
    '/demo/estimate/',
]

RETIRED_ROUTE_NAMES = [
    'index',
    'landing_demo_estimate_pdf',
    'start_new_estimate',
    'start_job',
    'customer_pay',
    'customer_pay_test_confirm',
    'stripe_webhook',
    'admin:index',
]


def _all_patterns(resolver=None):
    resolver = resolver or get_resolver()
    for entry in resolver.url_patterns:
        if isinstance(entry, URLResolver):
            yield from _all_patterns(entry)
        elif isinstance(entry, URLPattern):
            yield entry


class HomeTests(SimpleTestCase):
    def setUp(self):
        self.response = self.client.get('/', secure=True)
        self.html = self.response.content.decode()

    def test_home_renders_the_landing_without_database(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertTemplateUsed(self.response, 'landing/index.html')
        self.assertIn('When the job ends,', self.html)

    def test_home_never_links_to_the_retired_funnel(self):
        self.assertNotIn('Start with Seymy', self.html)
        self.assertNotIn('/start', self.html)
        self.assertNotIn('/e/', self.html)

    def test_sample_estimate_preview_is_rendered(self):
        self.assertIn('Estimate #1042', self.html)
        self.assertIn('Miller Plumbing', self.html)


class V11ContentTests(SimpleTestCase):
    """The landing states V1.1 pricing and makes no retired promise."""

    def setUp(self):
        self.html = self.client.get('/', secure=True).content.decode()
        self.lower = self.html.lower()

    def test_v11_prices_are_shown(self):
        for text in ['$99', '$990', '$25/month or $250/year', 'call minutes each month']:
            with self.subTest(text=text):
                self.assertIn(text, self.html)

    def test_platform_fee_is_limited_to_payments_collected_through_seymy(self):
        self.assertIn('1% only on payments collected through Seymy', self.html)
        self.assertIn('carry no Seymy fee', self.html)
        self.assertNotIn('Card, cash, check, or bank transfer', self.html)

    def test_price_microcopy_says_only_the_owner_is_included(self):
        self.assertEqual(self.html.count('Owner included · Extra users billed separately.'), 3)
        self.assertNotIn('Everything included', self.html)
        self.assertIn('Simple, transparent pricing.', self.html)

    def test_retired_offers_and_promises_are_gone(self):
        for text in [
            'trial',
            'free',
            '$49',
            '499',
            'no subscription',
            'by text',
            'text message',
            'customer notified',
            'books the appointment',
            'credit card',
            'card fees',
            'apple pay',
        ]:
            with self.subTest(text=text):
                self.assertNotIn(text, self.lower)

    def test_no_store_or_platform_mentions(self):
        for text in ['app store', 'google play', 'ios', 'android', 'testflight', 'apk']:
            with self.subTest(text=text):
                self.assertNotRegex(self.lower, rf'\b{text}\b')


class IdentityTests(SimpleTestCase):
    """Brand wordmark and shared token stylesheet."""

    def test_bicolour_wordmark_and_tokens(self):
        html = self.client.get('/', secure=True).content.decode()
        self.assertIn('<span class="wordmark">Sey<span class="wordmark__miss">my.</span></span>', html)
        self.assertIn('css/site-v11.css', html)
        self.assertNotIn('#1a6dff', html.lower())


class DownloadCtaTests(SimpleTestCase):
    """One configurable download channel; no URL means no link at all."""

    def _home(self, **overrides):
        with self.settings(**overrides):
            return self.client.get('/', secure=True).content.decode()

    def test_without_url_there_is_no_download_link_and_no_qr(self):
        for url in ['', '   ', 'http://example.com/app', 'javascript:alert(1)', '#', '/download']:
            with self.subTest(url=url):
                html = self._home(SITE_DOWNLOAD_URL=url)
                self.assertNotIn('data-download-cta', html)
                self.assertNotIn('download-qr', html)
                self.assertEqual(html.count('Seymy is in private pilot.'), 4)
                self.assertNotIn('Start with Seymy', html)

    def test_default_settings_have_no_download_link(self):
        html = self._home()
        self.assertNotIn('data-download-cta', html)
        self.assertIn('Seymy is in private pilot.', html)

    def test_configured_url_drives_every_cta_and_the_qr(self):
        url = 'https://downloads.example.com/seymy'
        html = self._home(SITE_DOWNLOAD_URL=url, SITE_DOWNLOAD_LABEL='Get Seymy')
        self.assertEqual(html.count(f'href="{url}"'), 4)
        self.assertEqual(html.count('>Get Seymy</a>'), 4)
        self.assertNotIn('private pilot', html)
        self.assertIn('class="download-qr"', html)
        self.assertIn('aria-label="QR code to download Seymy"', html)
        self.assertNotIn('<?xml', html)

    def test_label_and_url_are_escaped(self):
        html = self._home(
            SITE_DOWNLOAD_URL='https://downloads.example.com/a"onmouseover="x',
            SITE_DOWNLOAD_LABEL='<script>alert(1)</script>',
        )
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('"onmouseover="', html)

    def test_blank_label_falls_back_to_a_neutral_label(self):
        html = self._home(SITE_DOWNLOAD_URL='https://downloads.example.com/x', SITE_DOWNLOAD_LABEL='')
        self.assertEqual(html.count('>Get the Seymy app</a>'), 4)


class LegalPagesTests(SimpleTestCase):
    """/privacy and /terms, linked from the footer and from each other."""

    PAGES = {'/privacy': 'Privacy Policy', '/terms': 'Terms &amp; Conditions'}

    def test_pages_render_without_database(self):
        for path, title in self.PAGES.items():
            for variant in (path, path + '/'):
                with self.subTest(path=variant):
                    response = self.client.get(variant, secure=True)
                    self.assertEqual(response.status_code, 200)
                    html = response.content.decode()
                    self.assertIn(f'<h1 class="legal-title">{title}</h1>', html)
                    self.assertIn('<html lang="en">', html)

    def test_footer_links_reach_both_pages_from_every_page(self):
        for path in ['/', '/privacy', '/terms']:
            with self.subTest(path=path):
                html = self.client.get(path, secure=True).content.decode()
                self.assertIn('href="/privacy"', html)
                self.assertIn('href="/terms"', html)
                self.assertNotIn('href="#"', html)

    def test_current_page_is_marked(self):
        html = self.client.get('/terms', secure=True).content.decode()
        self.assertIn('href="/terms" aria-current="page"', html)
        self.assertNotIn('href="/privacy" aria-current="page"', html)

    def test_a2p_sections_are_present(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                html = self.client.get(path, secure=True).content.decode()
                self.assertIn('id="sms"', html)
                self.assertIn('id="contact"', html)
                self.assertIn('is the brand of', html)
                for item in ['Seymy Internal SMS Testing', 'only to internal testers who expressly consent in advance',
                             'A Seymy representative obtains consent from each tester before their phone number is enrolled.',
                             'Message frequency varies', 'Message and data rates may apply.',
                             'Msg &amp; data rates may apply.', 'Reply STOP', 'HELP for help.',
                             'For help with the testing program, contact us using the details in the Contact section below.',
                             'does not send promotional messages',
                             'Carriers are not liable for any delayed or undelivered messages.']:
                    self.assertIn(item, html)
        privacy = self.client.get('/privacy', secure=True).content.decode()
        self.assertIn(
            'Mobile information will not be shared with third parties or affiliates for marketing '
            'or promotional purposes. Text messaging originator opt-in data and consent will not be '
            'shared with any third parties.',
            privacy,
        )

    def test_v11_billing_terms(self):
        html = self.client.get('/terms', secure=True).content.decode()
        for text in ['$99 per month', '$990 per year', 'There is no free trial.', '$25 per month',
                     '$250 per year', '500 Miss Seymy call minutes', '1% platform fee',
                     'carry no Seymy fee', 'does not book appointments',
                     'No Seymy platform fee applies to payments made outside Seymy.']:
            with self.subTest(text=text):
                self.assertIn(text, html)

    def test_calls_are_transcribed_not_recorded(self):
        html = self.client.get('/privacy', secure=True).content.decode()
        self.assertIn('they are not recorded as audio', html)
        self.assertIn('transcribed through our telephony provider, Twilio', html)
        self.assertNotIn('OpenAI</strong>: transcription', html)

    def test_only_verified_active_providers_are_named(self):
        html = self.client.get('/privacy', secure=True).content.decode()
        for name in ['Heroku', 'Render', 'Cloudflare', 'Stripe', 'Twilio', 'Deepgram', 'ElevenLabs',
                     'OpenAI', 'Resend', 'Amazon SES', 'Firebase', 'Expo', 'Google Workspace']:
            with self.subTest(provider=name):
                self.assertIn(name, html)
        for name in ['Sentry', 'S3', 'Apple']:
            with self.subTest(inactive=name):
                self.assertNotIn(name, html)

    def test_effective_date_is_one_setting(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                html = self.client.get(path, secure=True).content.decode()
                with self.settings(SITE_LEGAL_EFFECTIVE_DATE=''):
                    html = self.client.get(path, secure=True).content.decode()
                self.assertIn('date d’entrée en vigueur', html)
                with self.settings(SITE_LEGAL_EFFECTIVE_DATE='November 10, 2026'):
                    html = self.client.get(path, secure=True).content.decode()
                self.assertIn('Effective date: November 10, 2026', html)
                self.assertNotIn('date d’entrée en vigueur', html)

    def test_unsupplied_items_are_visible_markers(self):
        """Every missing legal item is a visible marker, never silent text."""
        for path in self.PAGES:
            with self.subTest(path=path):
                html = self.client.get(path, secure=True).content.decode()
                self.assertEqual(html.count('data-todo'), html.count('{{À FOURNIR : '))
                self.assertNotIn('&amp;amp;', html)
                for item in ['Seymy, Inc., a Delaware corporation',
                             '131 Continental Dr, Suite 305, Newark, DE 19713, USA',
                             'rachid@seymy.com']:
                    self.assertIn(item, html)
                # No unverified automatic reply is quoted (Twilio HELP not verified).
                self.assertNotIn('Seymy test program: for help contact', html)

    def test_no_deletion_capability_is_promised(self):
        """No account deletion or purge exists: the policy must say so and
        never promise deletion it cannot perform."""
        html = self.client.get('/privacy', secure=True).content.decode()
        self.assertIn('It is not deleted automatically', html)
        self.assertIn('We will respond as required by applicable law.', html)
        for promise in ['we will delete', 'You may request access to, correction of, or deletion',
                        'deleted on request', 'export your data']:
            self.assertNotIn(promise.lower(), html.lower())

    def test_no_invented_contact_details(self):
        """Only the contact Rachid confirmed (2026-11-06) appears: one email
        address, no other email, no phone number."""
        import re
        for path in ['/', '/privacy', '/terms']:
            with self.subTest(path=path):
                html = self.client.get(path, secure=True).content.decode()
                emails = {e.rstrip('.') for e in re.findall(r'[\w.+-]+@[\w-]+\.[\w.]+', html)}
                self.assertLessEqual(emails, {'rachid@seymy.com'})
                self.assertNotIn('tel:', html)
                for mailto in re.findall(r'mailto:([^"]*)', html):
                    self.assertEqual(mailto, 'rachid@seymy.com')


class SmsHonestyTests(SimpleTestCase):
    """Only the internal testing program exists: no page may present texts
    between contractors and their customers as available."""

    FORBIDDEN = [
        'text your customers',
        'text your customer',
        'texting your customers',
        'customers receive texts',
        'customers receive text messages',
        'customer receives a text',
        'text messages to your customers',
        'texts to your customers',
        'send texts',
        'by text',
        'by sms',
        'via sms',
        'sms to customers',
        'two-way texting',
        'two-way sms',
        'text message to customers',
        'text messages to customers',
        'texting',
        'text them',
        'text reminder',
        'sms reminder',
        'reminder by text',
    ]

    def test_no_page_offers_customer_texting(self):
        for path in PUBLIC_PAGES:
            html = self.client.get(path, secure=True).content.decode().lower()
            for phrase in self.FORBIDDEN:
                with self.subTest(path=path, phrase=phrase):
                    self.assertNotIn(phrase, html)

    def test_contractor_sms_program_is_not_displayed(self):
        for path in ['/privacy', '/terms']:
            with self.subTest(path=path):
                html = self.client.get(path, secure=True).content.decode()
                self.assertNotIn('Contractor business SMS program', html)
                self.assertNotIn('per-business A2P', html)
                self.assertIn('does not', html)

    def test_contractor_sms_block_is_not_included(self):
        from pathlib import Path
        import re
        legal = Path(__file__).parent / 'templates' / 'landing' / 'legal'
        for name in ['privacy.html', 'terms.html', 'sms_internal_testing.html']:
            source = (legal / name).read_text()
            visible = re.sub(r'{% comment %}.*?{% endcomment %}', '', source, flags=re.S)
            with self.subTest(template=name):
                self.assertNotIn('sms_contractor_business', visible)


class SmsTestConsentPageTests(SimpleTestCase):
    """/sms-test-consent: the blank paper form of the internal SMS testing
    program, public for the A2P review. Read-only: no data is collected."""

    PNG = '/assets/seymy-sms-test-consent-form.png'
    PDF = '/assets/seymy-sms-test-consent-form.pdf'

    def setUp(self):
        self.response = self.client.get('/sms-test-consent', secure=True)
        self.html = self.response.content.decode()

    def test_page_renders_without_login_or_database(self):
        for variant in ('/sms-test-consent', '/sms-test-consent/'):
            with self.subTest(path=variant):
                response = self.client.get(variant, secure=True)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, 'landing/legal/sms_test_consent.html')
        self.assertIn('<h1 class="legal-title">SMS Test Consent Form</h1>', self.html)
        self.assertIn('<title>SMS Test Consent Form — Seymy</title>', self.html)
        self.assertIn('<meta name="description" content="The blank paper form', self.html)
        self.assertNotIn('noindex', self.html)

    def test_page_collects_no_data(self):
        for tag in ['<form', '<input', '<textarea', '<select', '<button', '<script', '<iframe', 'csrfmiddlewaretoken']:
            with self.subTest(tag=tag):
                self.assertNotIn(tag, self.html.lower())
        self.assertIn('this website has no sign-up form and does not collect phone numbers.', self.html)
        self.assertEqual(len(self.response.cookies), 0)

    def test_page_shows_and_links_both_files(self):
        self.assertIn(f'<img src="{self.PNG}" width="1224" height="1584"', self.html)
        self.assertIn(f'href="{self.PDF}"', self.html)
        self.assertIn(f'href="{self.PNG}"', self.html)

    @staticmethod
    def _visible_text(html_source):
        import html as html_lib
        import re
        return re.sub(r'\s+', ' ', html_lib.unescape(re.sub(r'<[^>]+>', ' ', html_source)))

    def test_form_text_matches_the_paper_form(self):
        text = self._visible_text(self.html)
        for item in [
            'Seymy, Inc. | Seymy Internal SMS Testing | Internal software testing only',
            'Reply HELP for help. For help with the testing program, contact us at rachid@seymy.com.',
            'Consent - please check the box and sign',
            'Fields completed by the tester: tester full name; mobile phone number (including country code); '
            'signature; date (MM/DD/YYYY).',
            'Seymy retains the completed, signed form privately as evidence of consent.',
            'Form version 1.1 · November 2026',
        ]:
            with self.subTest(item=item):
                self.assertIn(item, text)
        for retired in ['Form version 1.0', 'I agree to receive SMS test messages from Seymy, Inc.',
                        'reply HELP for assistance', 'based on testing activity']:
            with self.subTest(retired=retired):
                self.assertNotIn(retired, text)

    def test_form_text_is_the_published_legal_text(self):
        """Program, opt-in, disclosures and consent statement are those of the
        SMS block shown on /privacy and /terms, word for word (A2P reference)."""
        import re
        page = self._visible_text(self.html)
        for path in ['/privacy', '/terms']:
            legal_html = self.client.get(path, secure=True).content.decode()
            block = re.search(r'<dl class="legal-sms">(.*?)</dl>', legal_html, flags=re.S).group(1)
            items = {self._visible_text(dt).strip(): self._visible_text(dd).strip()
                     for dt, dd in re.findall(r'<dt>(.*?)</dt><dd>(.*?)</dd>', block, flags=re.S)}
            opt_in = items['How testers opt in']
            statement = re.search(r'Testers agree to this statement: “(.*?)”', opt_in).group(1)
            expected = [
                items['Program'],
                opt_in.split(' Testers agree to this statement:')[0],
                opt_in.split('” ')[1],
                statement,
                items['Message frequency'],
                items['Costs'],
                items['Opt out'],
                items['Delivery'],
            ]
            for sentence in expected:
                with self.subTest(path=path, sentence=sentence[:60]):
                    self.assertIn(sentence, page)
            self.assertIn(f'☐ {statement}', page)
        sharing = re.search(r'<p class="legal-sms__sharing">(.*?)</p>',
                            self.client.get('/privacy', secure=True).content.decode(), flags=re.S).group(1)
        self.assertIn(self._visible_text(sharing).strip(), page)

    def test_published_files_are_version_1_1(self):
        from pathlib import Path
        pdf = (Path(__file__).resolve().parent.parent / 'public_root' / self.PDF.lstrip('/')).read_bytes()
        self.assertIn(b'(Version 1.1  |  November 2026)', pdf)
        self.assertIn(b'/Title (Seymy SMS Test Recipient Consent Form \\(blank\\))', pdf)
        self.assertNotIn(b'Version 1.0', pdf)

    def test_links_to_privacy_terms_and_contact(self):
        self.assertIn('href="/privacy"', self.html)
        self.assertIn('href="/terms"', self.html)
        self.assertIn('href="/privacy#sms"', self.html)
        self.assertIn('id="contact"', self.html)

    def test_no_personal_data_is_published(self):
        import re
        self.assertNotIn('tel:', self.html.lower())
        text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', self.html))
        self.assertNotRegex(text, r'\+?\(?\d[\d .()-]{8,}\d')

    def test_legal_pages_keep_their_effective_date_line(self):
        for path in ['/privacy', '/terms']:
            with self.subTest(path=path):
                html = self.client.get(path, secure=True).content.decode()
                self.assertIn('<p class="legal-updated">Effective date: ', html)
                self.assertNotIn('Form version', html)

    def test_files_are_served_publicly_with_the_right_type(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / 'public_root'
        for path, kind, magic in [(self.PNG, 'image/png', b'\x89PNG'), (self.PDF, 'application/pdf', b'%PDF-')]:
            with self.subTest(path=path):
                response = self.client.get(path, secure=True)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Content-Type'], kind)
                body = b''.join(response.streaming_content) if response.streaming else response.content
                self.assertTrue(body.startswith(magic))
                self.assertEqual(body, (root / path.lstrip('/')).read_bytes())

    def test_public_root_holds_only_the_two_blank_files(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / 'public_root'
        files = sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file())
        self.assertEqual(files, ['assets/seymy-sms-test-consent-form.pdf', 'assets/seymy-sms-test-consent-form.png'])

    def test_pdf_has_no_active_content(self):
        from pathlib import Path
        pdf = (Path(__file__).resolve().parent.parent / 'public_root' / self.PDF.lstrip('/')).read_bytes()
        for marker in [b'/JavaScript', b'/JS', b'/OpenAction', b'/Launch', b'/EmbeddedFile', b'/AcroForm', b'/URI']:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, pdf)


class FictionalBrandTests(SimpleTestCase):
    """The hero shirt shows a fictional "SUNSHINE Services" logo: no page may
    name it or present it as a Seymy customer (caption, alt text, testimonial)."""

    def test_no_page_mentions_the_shirt_brand(self):
        for path in PUBLIC_PAGES:
            with self.subTest(path=path):
                html = self.client.get(path, secure=True).content.decode().lower()
                self.assertNotIn('sunshine', html)

    def test_hero_alt_text_describes_a_contractor_only(self):
        html = self.client.get('/', secure=True).content.decode()
        self.assertIn(
            'alt="Contractor in his work truck talking to Seymy on a dashboard-mounted phone"', html
        )
        for text in ['testimonial', 'trusted by', 'our customers', 'our clients', 'case study']:
            with self.subTest(text=text):
                self.assertNotRegex(html.lower(), rf'\b{text}\b')


class PublicationGateCommandTests(SimpleTestCase):
    """build.sh runs check_public_pages: the build fails while a marker remains."""

    def _run(self, pages):
        from unittest import mock
        from django.core.management import call_command
        from django.http import HttpResponse
        from landing.management.commands import check_public_pages as cmd
        views = {path: (lambda html: (lambda request: HttpResponse(html)))(html) for path, html in pages.items()}
        with mock.patch.object(cmd, 'PAGES', views):
            call_command('check_public_pages', stdout=__import__('io').StringIO())

    def test_gate_fails_on_a_placeholder(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self._run({'/': '<p>ok</p>', '/privacy': '<mark data-todo>{{À FOURNIR : x}}</mark>'})

    def test_gate_passes_on_finished_pages(self):
        self._run({'/': '<p>ok</p>', '/privacy': '<p>done</p>', '/terms': '<p>done</p>'})

    def test_build_script_runs_the_gate(self):
        from pathlib import Path
        build = (Path(__file__).resolve().parent.parent / 'build.sh').read_text()
        self.assertIn('python manage.py check_public_pages', build)


class PublicationReadinessTests(SimpleTestCase):
    """BLOCKS PUBLICATION. Fails while any {{À FOURNIR}} marker or the
    effective-date placeholder remains. Do not skip or relax it: supply the
    missing information instead (and set SITE_LEGAL_EFFECTIVE_DATE)."""

    def test_no_placeholder_left_on_public_pages(self):
        for path in PUBLIC_PAGES:
            with self.subTest(path=path):
                html = self.client.get(path, secure=True).content.decode()
                self.assertNotIn('data-todo', html)
                self.assertNotIn('À FOURNIR', html)


class RetiredRouteTests(SimpleTestCase):
    def test_old_funnel_redirects_home(self):
        for path in RETIRED_FUNNEL_PATHS:
            with self.subTest(path=path):
                response = self.client.get(path, secure=True)
                self.assertRedirects(response, '/', fetch_redirect_response=False)

    def test_old_customer_links_are_gone_for_every_method(self):
        for path in RETIRED_CUSTOMER_PATHS:
            for method in ('get', 'post'):
                with self.subTest(path=path, method=method):
                    response = getattr(self.client, method)(path, secure=True)
                    self.assertEqual(response.status_code, 410)
                    self.assertContains(response, 'no longer active', status_code=410)

    def test_webhook_admin_and_demo_are_not_routed(self):
        for path in REMOVED_PATHS:
            with self.subTest(path=path, method='get'):
                self.assertEqual(self.client.get(path, secure=True).status_code, 404)
            with self.subTest(path=path, method='post'):
                response = self.client.post(
                    path,
                    data=b'{"type": "checkout.session.completed"}',
                    content_type='application/json',
                    secure=True,
                )
                self.assertEqual(response.status_code, 404)

    def test_retired_route_names_no_longer_reverse(self):
        for name in RETIRED_ROUTE_NAMES:
            with self.subTest(name=name):
                with self.assertRaises(NoReverseMatch):
                    reverse(name)


class RoutingSurfaceTests(SimpleTestCase):
    def test_every_route_is_served_by_the_public_module(self):
        patterns = list(_all_patterns())
        self.assertEqual(len(patterns), 6)
        for pattern in patterns:
            with self.subTest(route=str(pattern.pattern)):
                self.assertEqual(pattern.callback.__module__, 'landing.public')
