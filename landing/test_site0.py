"""
SITE-0 containment tests.

SimpleTestCase forbids every database query, so each test also proves the
public surface works without the (dead) Render Postgres.
"""
from django.test import SimpleTestCase
from django.urls import NoReverseMatch, URLPattern, URLResolver, get_resolver, reverse

TOKEN = 'abcDEF123_-xyz'

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
                             'Msg &amp; data rates may apply.', 'Reply STOP', 'Reply HELP',
                             '“Seymy test program: for help contact',
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
                     'carry no Seymy fee', 'does not book appointments']:
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
                for item in ['entité légale', 'adresse légale', 'e-mail d’assistance']:
                    self.assertIn(item, html)

    def test_no_invented_contact_details(self):
        """Contacts stay placeholders until verified details are supplied."""
        for path in ['/', '/privacy', '/terms']:
            with self.subTest(path=path):
                html = self.client.get(path, secure=True).content.decode()
                self.assertNotRegex(html, r'[\w.+-]+@[\w-]+\.[\w.]+')
                self.assertNotIn('mailto:', html)
                self.assertNotIn('tel:', html)


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
        for path in ['/', '/privacy', '/terms']:
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


class FictionalBrandTests(SimpleTestCase):
    """The hero shirt shows a fictional "SUNSHINE Services" logo: no page may
    name it or present it as a Seymy customer (caption, alt text, testimonial)."""

    def test_no_page_mentions_the_shirt_brand(self):
        for path in ['/', '/privacy', '/terms']:
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
        for path in ['/', '/privacy', '/terms']:
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
        self.assertEqual(len(patterns), 5)
        for pattern in patterns:
            with self.subTest(route=str(pattern.pattern)):
                self.assertEqual(pattern.callback.__module__, 'landing.public')
