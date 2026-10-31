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


class ProvisionalHomeTests(SimpleTestCase):
    def setUp(self):
        self.response = self.client.get('/', secure=True)
        self.html = self.response.content.decode()

    def test_home_renders_without_database(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertTemplateUsed(self.response, 'landing/provisional_home.html')

    def test_cta_is_an_early_access_email(self):
        self.assertIn('Request early access', self.html)
        self.assertIn('href="mailto:support@seymy.com?subject=Seymy%20early%20access', self.html)
        self.assertNotIn('/start', self.html)

    def test_retired_offers_and_promises_are_gone(self):
        for text in [
            '$49',
            '$',
            'free trial',
            '14 days',
            '14-day',
            'credit card',
            'card fees',
            'Pass eligible',
            'Apple Pay',
        ]:
            with self.subTest(text=text):
                self.assertNotIn(text.lower(), self.html.lower())

    def test_text_message_payment_links_are_only_announced_as_coming_later(self):
        self.assertEqual(self.html.count('by text'), 1)
        later = self.html.index('Coming later')
        self.assertLess(later, self.html.index('Payment links by text message'))

    def test_no_form_and_no_sms_consent(self):
        self.assertNotIn('<form', self.html)
        self.assertNotIn('STOP', self.html)
        self.assertNotIn('Msg & data rates', self.html)

    def test_no_dead_legal_links(self):
        self.assertNotIn('href="#"', self.html)

    def test_contact_email_comes_from_settings(self):
        with self.settings(SITE_CONTACT_EMAIL='pilot@example.com'):
            html = self.client.get('/', secure=True).content.decode()
        self.assertIn('mailto:pilot@example.com', html)


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
        self.assertEqual(len(patterns), 3)
        for pattern in patterns:
            with self.subTest(route=str(pattern.pattern)):
                self.assertEqual(pattern.callback.__module__, 'landing.public')
