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
    """L1: the landing states V1.1 pricing and makes no retired promise."""

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
    """L2: REF-1 wordmark and shared token stylesheet."""

    def test_bicolour_wordmark_and_tokens(self):
        html = self.client.get('/', secure=True).content.decode()
        self.assertIn('<span class="wordmark">Sey<span class="wordmark__miss">my.</span></span>', html)
        self.assertIn('css/site-v11.css', html)
        self.assertNotIn('#1a6dff', html.lower())


class DownloadCtaTests(SimpleTestCase):
    """L3: one configurable download channel; no URL means no link at all."""

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

    def test_blank_label_falls_back_to_a_neutral_label(self):
        html = self._home(SITE_DOWNLOAD_URL='https://downloads.example.com/x', SITE_DOWNLOAD_LABEL='')
        self.assertEqual(html.count('>Get the Seymy app</a>'), 4)


class LegalPagesTests(SimpleTestCase):
    """L4: /privacy and /terms, linked from the footer and from each other."""

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
                for item in ['STOP', 'HELP', 'fréquence variable', 'frais éventuels', 'non livrés',
                             'nom exact de la marque enregistrée']:
                    self.assertIn(item, html)
        privacy = self.client.get('/privacy', secure=True).content.decode()
        self.assertIn('non-partage des données d’opt-in SMS', privacy)

    def test_no_invented_contact_details(self):
        """Contacts stay placeholders until verified details are supplied."""
        for path in ['/', '/privacy', '/terms']:
            with self.subTest(path=path):
                html = self.client.get(path, secure=True).content.decode()
                self.assertNotRegex(html, r'[\w.+-]+@[\w-]+\.[\w.]+')
                self.assertNotIn('mailto:', html)
                self.assertNotIn('tel:', html)


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
