from django.test import Client, TestCase
from django.urls import reverse

from landing.models import Estimate, EstimateVersion, PendingCheckout
from landing.numbering import INITIAL_ESTIMATE_NUMBER, next_estimate_number
from landing.acceptance import publish_estimate_from_funnel
from django.core.management import call_command


class NumberingTests(TestCase):
    def test_next_number_starts_at_sey_001(self):
        self.assertEqual(next_estimate_number(), INITIAL_ESTIMATE_NUMBER)

    def test_next_number_increments(self):
        publish_estimate_from_funnel(
            {
                'business_name': 'Test Co',
                'customer_name': 'A',
                'estimate_number': 'SEY-001',
                'estimate_total': '100.00',
                'total': '100.00',
                'deposit_enabled': False,
                'payment_request_through_seymy': False,
            }
        )
        self.assertEqual(next_estimate_number(), 'SEY-002')


class ResetSeymyTestDataTests(TestCase):
    def test_reset_clears_estimates_and_sessions(self):
        publish_estimate_from_funnel(
            {
                'business_name': 'Test Co',
                'customer_name': 'John Anderson',
                'estimate_number': 'SEY-001',
                'estimate_total': '100.00',
                'total': '100.00',
                'deposit_enabled': True,
                'deposit_amount': '30.00',
                'payment_request_through_seymy': True,
                'payment_request_type': 'deposit',
                'requested_payment_amount': '30.00',
            }
        )
        client = Client()
        session = client.session
        session['funnel'] = {
            'business_name': 'Test Co',
            'business_email': 'owner@test.com',
            'business_profile_complete': True,
            'customer_name': 'John Anderson',
            'estimate_number': 'SEY-001',
            'deposit_amount': '30.00',
            'has_user_provided_job_input': True,
        }
        session.save()

        call_command('reset_seymy_test_data', '--yes')

        self.assertEqual(Estimate.objects.count(), 0)
        self.assertEqual(EstimateVersion.objects.count(), 0)
        self.assertEqual(PendingCheckout.objects.count(), 0)
        self.assertEqual(next_estimate_number(), INITIAL_ESTIMATE_NUMBER)

        # Reload session from DB
        client2 = Client()
        # Original session key may still exist with cleared funnel
        from django.contrib.sessions.models import Session

        found_preserved = False
        for row in Session.objects.all():
            from django.contrib.sessions.backends.db import SessionStore

            store = SessionStore(session_key=row.session_key)
            funnel = store.get('funnel') or {}
            if funnel.get('business_name') == 'Test Co':
                found_preserved = True
                self.assertFalse(funnel.get('customer_name'))
                self.assertFalse(funnel.get('has_user_provided_job_input'))
                self.assertFalse(funnel.get('estimate_number'))
        self.assertTrue(found_preserved)

    def test_start_new_estimate_is_blank(self):
        client = Client()
        session = client.session
        session['funnel'] = {
            'business_name': 'Kept Plumbing',
            'business_email': 'a@b.com',
            'customer_name': 'John Anderson',
            'estimate_number': '1042',
            'job_site_address': '24 Main Street',
            'deposit_value': '30',
            'has_user_provided_job_input': True,
        }
        session.save()

        response = client.get(reverse('start_new_estimate'))
        self.assertEqual(response.status_code, 302)
        funnel = client.session['funnel']
        self.assertEqual(funnel.get('business_name'), 'Kept Plumbing')
        self.assertFalse(funnel.get('customer_name'))
        self.assertFalse(funnel.get('estimate_number'))
        self.assertFalse(funnel.get('job_site_address'))
        self.assertFalse(funnel.get('deposit_value'))
        self.assertFalse(funnel.get('has_user_provided_job_input'))

        talk = client.get(reverse('start_job'))
        html = talk.content.decode()
        # Example copy may mention demo names; the draft/session must stay empty.
        self.assertIn('placeholder="Type your estimate here..."', html)
        self.assertIn('></textarea>', html.replace('\n', ''))
        # No prefilled value attribute on the textarea
        self.assertNotRegex(html, r'<textarea[^>]*value=')
        funnel_after = client.session['funnel']
        self.assertFalse(funnel_after.get('customer_name'))
        self.assertFalse(funnel_after.get('estimate_total'))
        self.assertFalse(funnel_after.get('line_items'))
        self.assertFalse(funnel_after.get('deposit_value'))
        self.assertFalse(funnel_after.get('job_site_address'))
        self.assertFalse(funnel_after.get('customer_phone'))
        self.assertFalse(funnel_after.get('payment_request_through_seymy'))
