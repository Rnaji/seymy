from datetime import date
from decimal import Decimal

from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from .acceptance import build_snapshot, publish_estimate_from_funnel
from .conversation import (
    answer_retrieval_query,
    apply_updates,
    missing_requirements,
    next_question,
    parse_message,
    parse_retrieval_intent,
    parse_schedule_updates,
)
from .tax import parse_job_tax_message, recalculate_financials
from .views import _estimate_pdf_context, _summary_context


FULL_SPOKEN = (
    'Estimate for John Anderson, 24 Main Street, Orlando, FL. '
    'Replace a 50-gallon water heater. '
    '$1,350 materials, $500 labor. '
    'Apply 6% sales tax to materials. '
    'Start the job on August 20. '
    'I want 30% upfront and the balance when the job is finished on August 24. '
    'Pass eligible card fees to the customer.'
)


class ScheduleExtractionTests(SimpleTestCase):
    def setUp(self):
        self.today = date(2026, 8, 11)

    def test_start_and_completion_from_spoken_example(self):
        updates = parse_message(FULL_SPOKEN, existing={})
        self.assertEqual(updates.get('job_start_date'), '2026-08-20')
        self.assertEqual(updates.get('expected_completion_date'), '2026-08-24')
        self.assertEqual(updates.get('balance_due_date'), '2026-08-24')
        self.assertNotEqual(updates['job_start_date'], updates['expected_completion_date'])
        self.assertNotEqual(updates['job_start_date'], updates['balance_due_date'])

    def test_start_phrases(self):
        phrases = [
            'Start the job on August 20',
            "We're starting August 20",
            'Schedule it for August 20',
            'I should start around August 20',
        ]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                updates = parse_schedule_updates(phrase, today=self.today)
                self.assertEqual(updates.get('job_start_date'), '2026-08-20')

    def test_work_starts_next_monday(self):
        # 2026-08-11 is Tuesday → next Monday is 2026-08-17
        updates = parse_schedule_updates('Work starts next Monday', today=self.today)
        self.assertEqual(updates.get('job_start_date'), '2026-08-17')

    def test_duration_three_days_with_start(self):
        updates = parse_schedule_updates(
            'Start the job on August 20. Job should take three days.',
            today=self.today,
        )
        self.assertEqual(updates.get('job_start_date'), '2026-08-20')
        self.assertEqual(updates.get('job_duration_days'), 3)
        self.assertEqual(updates.get('expected_completion_date'), '2026-08-22')

    def test_finish_by_phrases(self):
        for phrase in (
            'Finish by August 24',
            'We should be done August 24',
        ):
            with self.subTest(phrase=phrase):
                updates = parse_schedule_updates(phrase, today=self.today)
                self.assertEqual(updates.get('expected_completion_date'), '2026-08-24')

    def test_balance_when_finished_sets_both_separate_fields(self):
        updates = parse_schedule_updates(
            'I want 30% upfront and the balance when the job is finished on August 24.',
            today=self.today,
        )
        self.assertEqual(updates.get('expected_completion_date'), '2026-08-24')
        self.assertEqual(updates.get('balance_due_date'), '2026-08-24')
        self.assertIsNone(updates.get('job_start_date'))

    def test_completion_and_balance_due_are_separate(self):
        draft = apply_updates(
            {},
            parse_message(
                'Start August 20. Balance due September 1. Finish by August 24.',
                existing={},
            ),
        )
        # Balance due should not overwrite start; completion separate from balance when distinct
        self.assertEqual(draft.get('job_start_date'), '2026-08-20')
        self.assertEqual(draft.get('expected_completion_date'), '2026-08-24')
        self.assertEqual(draft.get('balance_due_date'), '2026-09-01')

    def test_not_scheduled_yet_leaves_start_null(self):
        updates = parse_message(
            'Not scheduled yet',
            existing={'pending_question': 'job_start_date', 'schedule_mentioned': True},
        )
        self.assertIsNone(updates.get('job_start_date'))
        self.assertTrue(updates.get('job_start_declined'))

    def test_schedule_fields_optional_estimate_without_dates(self):
        message = (
            'Estimate for John Anderson, 24 Main Street, Orlando, FL. '
            'Replace a 50-gallon water heater. '
            '$1,350 materials, $500 labor. Apply 6% sales tax. '
            'I want 30% upfront and the balance on August 24. '
            'Pass eligible card fees to the customer.'
        )
        draft = apply_updates({}, parse_message(message, existing={}))
        self.assertIsNone(draft.get('job_start_date'))
        self.assertFalse(draft.get('expected_completion_date'))
        missing = missing_requirements(draft)
        self.assertNotIn('job_start_date', missing)
        self.assertNotIn('expected_completion_date', missing)
        self.assertIsNone(next_question(draft))

    def test_both_dates_optional_in_missing_requirements(self):
        data = {
            'customer_name': 'John Anderson',
            'job_site_address': '24 Main Street',
            'job_site_city': 'Orlando',
            'job_site_state': 'FL',
            'estimate_total': '1850.00',
            'line_items': [{'label': 'Labor', 'amount': '500', 'type': 'labor'}],
            'deposit_enabled': True,
            'deposit_type': 'percentage',
            'deposit_value': '30',
            'balance_due_date': '2026-08-24',
            'card_fee_preference_set': True,
            'tax_preference_set': True,
            'tax_enabled': True,
            'tax_rate': '6',
        }
        self.assertEqual(missing_requirements(data), [])


class ConversationalScheduleEditTests(SimpleTestCase):
    def setUp(self):
        self.base = {
            'customer_name': 'John Anderson',
            'job_start_date': '2026-08-20',
            'expected_completion_date': '2026-08-24',
            'balance_due_date': '2026-08-24',
            'schedule_mentioned': True,
        }

    def test_move_start(self):
        draft = apply_updates(self.base, parse_message('Move the start to August 21', existing=self.base))
        self.assertEqual(draft.get('job_start_date'), '2026-08-21')
        self.assertEqual(draft.get('expected_completion_date'), '2026-08-24')

    def test_change_completion(self):
        draft = apply_updates(
            self.base,
            parse_message('Change the completion to August 28', existing=self.base),
        )
        self.assertEqual(draft.get('expected_completion_date'), '2026-08-28')
        self.assertEqual(draft.get('job_start_date'), '2026-08-20')

    def test_remove_start_date(self):
        draft = apply_updates(self.base, parse_message('Remove the start date', existing=self.base))
        self.assertIsNone(draft.get('job_start_date'))
        self.assertTrue(draft.get('job_start_declined'))

    def test_not_confirmed(self):
        draft = apply_updates(
            {'pending_question': 'job_start_date', 'schedule_mentioned': True},
            parse_message('Not confirmed', existing={'pending_question': 'job_start_date'}),
        )
        self.assertIsNone(draft.get('job_start_date'))

    def test_same_day_completion(self):
        draft = apply_updates(
            self.base,
            parse_message('Same-day completion', existing=self.base),
        )
        self.assertEqual(draft.get('expected_completion_date'), '2026-08-20')
        self.assertEqual(draft.get('job_start_date'), '2026-08-20')


class MaterialsOnlyTaxTests(SimpleTestCase):
    def test_materials_only_tax_math(self):
        tax = parse_job_tax_message('Apply 6% sales tax to materials only.')
        self.assertIsNotNone(tax)
        self.assertEqual(tax.get('tax_applies_to'), 'materials')
        draft = apply_updates(
            {},
            {
                **tax,
                'line_items': [
                    {'label': '50 gal. water heater', 'amount': '1350.00', 'type': 'material'},
                    {'label': 'Labor', 'amount': '500.00', 'type': 'labor'},
                ],
                'deposit_enabled': True,
                'deposit_type': 'percentage',
                'deposit_value': '30',
            },
        )
        self.assertEqual(draft.get('subtotal'), '1850.00')
        self.assertEqual(draft.get('tax_amount'), '81.00')
        self.assertEqual(draft.get('estimate_total'), '1931.00')
        self.assertEqual(draft.get('deposit_amount'), '579.30')
        self.assertEqual(draft.get('balance_amount'), '1351.70')
        self.assertIn('materials', draft.get('tax_label', '').lower())

    def test_full_spoken_example_financials(self):
        draft = apply_updates({}, parse_message(FULL_SPOKEN, existing={}))
        self.assertEqual(draft.get('tax_applies_to'), 'materials')
        self.assertEqual(Decimal(draft['tax_amount']), Decimal('81.00'))
        self.assertEqual(Decimal(draft['estimate_total']), Decimal('1931.00'))
        self.assertEqual(draft.get('job_start_date'), '2026-08-20')
        self.assertEqual(draft.get('expected_completion_date'), '2026-08-24')


class SummaryAndPdfScheduleTests(SimpleTestCase):
    def _draft_with_schedule(self):
        return recalculate_financials(
            {
                'customer_name': 'John Anderson',
                'business_name': 'Miller Plumbing',
                'estimate_number': '1042',
                'line_items': [
                    {'label': '50 gal. water heater', 'amount': '1350.00', 'type': 'material'},
                    {'label': 'Labor', 'amount': '500.00', 'type': 'labor'},
                ],
                'tax_enabled': True,
                'tax_rate': '6',
                'tax_applies_to': 'materials',
                'tax_preference_set': True,
                'tax_configured': True,
                'deposit_enabled': True,
                'deposit_type': 'percentage',
                'deposit_value': '30',
                'balance_due_date': '2026-08-24',
                'job_start_date': '2026-08-20',
                'expected_completion_date': '2026-08-24',
                'pass_card_fees_to_customer': True,
                'card_fee_preference_set': True,
                'job_description': 'Replace a 50-gallon water heater.',
            }
        )

    def test_summary_shows_schedule(self):
        ctx = _summary_context(self._draft_with_schedule())
        self.assertTrue(ctx['show_schedule'])
        self.assertIn('Aug 20', ctx['job_start_date_display'])
        self.assertIn('Aug 24', ctx['expected_completion_date_display'])
        html = render_to_string('landing/start_summary.html', {**ctx, 'stages': [], 'current_stage': 'COMPLETE'})
        self.assertIn('SCHEDULE', html)
        self.assertIn('Estimated start', html)
        self.assertIn('Expected completion', html)
        self.assertIn('PAYMENT TERMS', html)
        self.assertIn('Tell Seymy what to change', html)
        self.assertNotIn('Edit manually', html)

    def test_pdf_shows_project_schedule(self):
        ctx = _estimate_pdf_context(self._draft_with_schedule())
        self.assertTrue(ctx['show_project_schedule'])
        self.assertIn('Aug 20', ctx['job_start_date_display'])
        self.assertIn('Aug 24', ctx['expected_completion_date_display'])
        self.assertEqual(ctx['estimate_schedule_range_display'], 'Aug 20 → Aug 24, 2026')
        html = render_to_string('landing/partials/estimate_document.html', ctx)
        self.assertIn('Project schedule', html)
        self.assertIn('Estimated Start', html)
        self.assertIn('Expected Completion', html)
        self.assertNotIn('None', html)
        self.assertNotIn('null', html.lower())
        self.assertIn('Acceptance &amp; payment', html)
        self.assertNotIn('Customer name:', html)
        self.assertNotIn('Signature:', html)

    def test_pdf_omits_schedule_when_null(self):
        data = self._draft_with_schedule()
        data['job_start_date'] = None
        data['expected_completion_date'] = None
        ctx = _estimate_pdf_context(data)
        self.assertFalse(ctx['show_project_schedule'])
        html = render_to_string('landing/partials/estimate_document.html', ctx)
        self.assertNotIn('Project schedule', html)
        self.assertNotIn('Not configured', html)
        bad = ('None', 'null', 'Not configured')
        for token in bad:
            self.assertNotIn(f'Estimated Start:</dt>\n            <dd>{token}', html)


class LandingAndFunnelCopyTests(TestCase):
    def test_landing_copy_includes_schedule_and_materials_tax(self):
        client = Client()
        response = client.get(reverse('index'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('Apply 6% sales tax.', content)
        self.assertIn('Start the job on August 20', content)
        self.assertIn('✓ Schedule', content)
        self.assertIn('Tax (6% materials)', content)
        self.assertIn('$1,931.00', content)
        self.assertIn('$579.30', content)
        self.assertIn('Expected completion', content)
        self.assertNotIn('Due</dt>', content)

    def test_landing_view_pdf_links_to_demo_estimate(self):
        client = Client()
        response = client.get(reverse('index'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        demo_url = reverse('landing_demo_estimate_pdf')
        self.assertIn(demo_url, content)
        self.assertIn(f'href="{demo_url}"', content)
        self.assertIn('target="_blank"', content)
        self.assertIn('rel="noopener"', content)

    def test_funnel_example_matches_spoken_string(self):
        client = Client()
        client.get(reverse('start_new_estimate'))
        response = client.get(reverse('start_job'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('Apply 6% sales tax to materials.', content)
        self.assertIn('Start the job on August 20', content)
        self.assertIn('Create Estimate', content)
        self.assertIn('TYPE INSTEAD', content)
        self.assertNotIn('Edit manually', content)

    def test_summary_and_review_actions(self):
        summary_html = render_to_string(
            'landing/start_summary.html',
            {
                'stages': [],
                'current_stage': 'COMPLETE',
                'business_name': 'Miller Plumbing',
                'customer_name': 'John Anderson',
                'job_site_lines': ['24 Main Street'],
                'line_items': [{'label': 'Labor', 'amount': '500'}],
                'subtotal': '500',
                'estimate_total': '500',
                'tax_configured': False,
                'tax_amount': '',
                'tax_label': 'Sales tax',
                'deposit_enabled': False,
                'deposit_label': '',
                'deposit_amount': '0',
                'balance_amount': '500',
                'balance_due_date_display': '',
                'show_schedule': False,
                'job_start_date_display': '',
                'expected_completion_date_display': '',
                'fee_label': 'Not set',
                'pass_card_fees_to_customer': False,
                'seymy_note': '',
            },
        )
        self.assertIn('Continue', summary_html)
        self.assertIn('Tell Seymy what to change', summary_html)
        self.assertNotIn('Edit manually', summary_html)

        review_html = render_to_string(
            'landing/start_review_send.html',
            {
                'stages': [],
                'current_stage': 'REVIEW & SEND',
                'business_name': 'Miller Plumbing',
                'customer_name': 'John Anderson',
                'estimate_number': '1042',
                'total': '1,931.00',
                'issue_date_display': 'Aug 11, 2026',
                'expiration_date_display': 'Sep 10, 2026',
                'line_items': [],
                'show_project_schedule': False,
                'show_qty_rate': False,
                'subtotal': '1,931.00',
                'tax_configured': False,
                'deposit_enabled': False,
                'send_recipient_name': 'John Anderson',
                'send_destination_display': 'Text message · 407-555-0182',
                'send_destination_detail': '407-555-0182',
                'send_is_sms': True,
                'send_is_email': False,
                'customer_first_name': 'John',
                'customer_message_body': (
                    'Hi John, Miller Plumbing sent you estimate #1042 for $1,931. '
                    'View estimate: https://example.test/e/token/document/ — Miller Plumbing'
                ),
                'customer_message_explanation': (
                    'The estimate will be sent without an online payment request.'
                ),
                'customer_secure_link': 'https://example.test/e/token/document/',
                'customer_email_subject': 'Estimate #1042 from Miller Plumbing',
                'send_cta_label': 'Send Estimate Only',
                'payment_send_mode': 'none',
            },
        )
        self.assertIn('class="estimate-doc"', review_html)
        self.assertIn('#1042', review_html)
        self.assertIn('Miller Plumbing', review_html)
        self.assertIn('$1,931.00', review_html)
        self.assertIn('Expand estimate', review_html)
        self.assertIn('id="estimate-preview"', review_html)
        self.assertIn('estimate-preview-shell', review_html)
        self.assertNotIn('Preview estimate', review_html)
        self.assertIn('READY TO SEND', review_html)
        self.assertIn('WHAT YOUR CUSTOMER WILL RECEIVE', review_html)
        self.assertIn('Send Estimate Only', review_html)
        self.assertIn('John Anderson', review_html)
        self.assertIn('Text message · 407-555-0182', review_html)
        self.assertIn('Nothing is sent until you confirm.', review_html)
        self.assertIn('Tell Seymy what to change', review_html)
        self.assertIn('without an online payment request', review_html)
        self.assertNotIn('Approve &amp; Send', review_html)
        self.assertNotIn('Approve & Send', review_html)
        self.assertNotIn('View PDF', review_html)
        self.assertNotIn('Edit manually', review_html)
        self.assertNotIn(reverse('start_estimate_pdf'), review_html)


class ReadyToSendFunnelTests(TestCase):
    def _complete_draft(self, *, with_destination=False, deposit=True, request_full_payment=False):
        draft = apply_updates({}, parse_message(FULL_SPOKEN, existing={}))
        draft.update(
            {
                'has_user_provided_job_input': True,
                'business_name': 'Miller Plumbing',
                'business_address': '100 Trade St',
                'business_city': 'Orlando',
                'state': 'FL',
                'business_zip': '32801',
                'business_phone': '407-555-0100',
                'business_email': 'hello@millerplumbing.test',
                'business_profile_complete': True,
                'payment_approved': False,
                'deposit_paid': False,
            }
        )
        if not deposit:
            draft.update(
                {
                    'deposit_enabled': False,
                    'deposit_type': '',
                    'deposit_value': '',
                    'deposit_amount': '0.00',
                    'balance_amount': draft.get('estimate_total') or draft.get('total') or '0',
                }
            )
        if request_full_payment:
            draft['request_full_payment'] = True
            draft['deposit_enabled'] = False
            draft['deposit_amount'] = '0.00'
        if with_destination:
            draft.update(
                {
                    'customer_phone': '407-555-0182',
                    'send_method': 'sms',
                    'send_destination_set': True,
                }
            )
        return recalculate_financials(draft)

    def _session_client(self, draft):
        client = Client()
        session = client.session
        session['funnel'] = draft
        session.save()
        return client

    def test_send_destination_continue_does_not_send(self):
        client = self._session_client(self._complete_draft())
        response = client.post(
            reverse('start_job') + '?mode=send',
            {'message': 'Text it to John at 407-555-0182.'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('start_review_send'))
        funnel = client.session['funnel']
        self.assertTrue(funnel.get('send_destination_set') or funnel.get('customer_phone'))
        self.assertFalse(funnel.get('payment_approved'))
        self.assertFalse(funnel.get('deposit_paid'))
        self.assertFalse(funnel.get('persisted_estimate_id'))

    def test_ready_to_send_deposit_case_preview_and_cta(self):
        client = self._session_client(self._complete_draft(with_destination=True, deposit=True))
        response = client.get(reverse('start_review_send'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        funnel = client.session['funnel']
        # Draft token prepared early — link is real, nothing marked sent yet.
        self.assertTrue(funnel.get('public_token'))
        self.assertTrue(funnel.get('persisted_estimate_id'))
        self.assertFalse(funnel.get('payment_approved'))
        self.assertIn('HOW DO YOU WANT TO HANDLE THE DEPOSIT?', content)
        self.assertIn('Request $', content)
        self.assertIn('through Seymy', content)
        self.assertIn('Get paid faster. Lock in the job sooner.', content)
        self.assertIn('Card · Apple Pay · Google Pay · Automatic reminders', content)
        self.assertIn('Recommended', content)
        self.assertNotIn('Instant SMS Checkout', content)
        self.assertNotIn('Funds are paid out', content)
        self.assertNotIn('approves and pays in 2 taps', content)
        self.assertNotIn('Seymy automatically follows up', content)
        self.assertIn('Send estimate only', content)
        self.assertIn('collect the deposit outside Seymy.', content)
        self.assertRegex(
            content,
            r'name="payment_request_through_seymy"\s+value="true"[^>]*checked'
            r'|value="true"[^>]*name="payment_request_through_seymy"[^>]*checked',
        )
        self.assertIn('READY TO SEND', content)
        self.assertIn('WHAT YOUR CUSTOMER WILL RECEIVE', content)
        self.assertIn('John Anderson', content)
        self.assertIn('Text message · 407-555-0182', content)
        self.assertIn('SMS TO JOHN ANDERSON', content)
        self.assertIn('Miller Plumbing sent you estimate', content)
        self.assertIn('deposit is required to approve', content)
        self.assertIn('View estimate &amp; pay deposit:', content)
        self.assertIn('online deposit request will be sent.', content)
        self.assertIn('Send Estimate &amp; Request $', content)
        self.assertIn('Deposit', content)
        self.assertTrue(funnel.get('payment_request_through_seymy'))
        self.assertEqual(funnel.get('payment_request_type'), 'deposit')
        self.assertIn('Tell Seymy what to change', content)
        self.assertIn('Nothing is sent until you confirm.', content)
        self.assertIn('Expand estimate', content)
        self.assertIn('id="estimate-preview"', content)
        self.assertIn('estimate-preview-shell', content)
        self.assertIn('estimate-doc-frame', content)
        self.assertIn('class="estimate-doc"', content)
        # Real document is inline and clipped (not a separate page / hidden block).
        self.assertIn('Scope of work', content)
        self.assertIn('estimate-doc__estimate-label', content)
        self.assertNotIn('Preview estimate', content)
        self.assertNotIn('id="estimate-summary"', content)
        self.assertIn('Aug 20', content)
        self.assertIn('Aug 24', content)
        self.assertIn('$1,931.00', content)
        self.assertIn(f"/e/{funnel['public_token']}/pay/", content)
        self.assertNotIn('Approve &amp; Send', content)
        self.assertNotIn('View PDF', content)
        self.assertNotIn('Edit manually', content)

    def test_ready_to_send_switch_to_estimate_only_updates_cta(self):
        client = self._session_client(self._complete_draft(with_destination=True, deposit=True))
        client.get(reverse('start_review_send'))
        response = client.post(
            reverse('start_review_send'),
            {
                'action': 'set_payment_choice',
                'payment_request_through_seymy': 'false',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('start_review_send'))
        funnel = client.session['funnel']
        self.assertFalse(funnel.get('payment_request_through_seymy'))
        self.assertTrue(funnel.get('deposit_enabled'))
        review = client.get(reverse('start_review_send'))
        content = review.content.decode()
        self.assertIn('Send Estimate Only', content)
        self.assertIn('No online payment will be requested.', content)
        self.assertIn('Review your estimate below:', content)
        self.assertNotIn('pay deposit', content.lower().split('what your customer will receive')[1])
        self.assertIn(f"/e/{funnel['public_token']}/document/", content)
        self.assertRegex(
            content,
            r'name="payment_request_through_seymy"\s+value="false"[^>]*checked'
            r'|value="false"[^>]*name="payment_request_through_seymy"[^>]*checked',
        )

    def test_ready_to_send_no_payment_case(self):
        client = self._session_client(
            self._complete_draft(with_destination=True, deposit=False, request_full_payment=False)
        )
        response = client.get(reverse('start_review_send'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        funnel = client.session['funnel']
        self.assertNotIn('HOW DO YOU WANT TO HANDLE THE DEPOSIT?', content)
        self.assertNotIn('HOW DO YOU WANT TO HANDLE THE PAYMENT?', content)
        self.assertIn('Send Estimate', content)
        self.assertNotIn('Send Estimate Only', content)
        self.assertNotIn('Request $', content)
        self.assertNotIn('Request Payment', content)
        self.assertIn('No payment is requested now.', content)
        self.assertIn('Review your estimate below:', content)
        self.assertIn(f"/e/{funnel['public_token']}/document/", content)
        self.assertFalse(funnel.get('payment_approved'))
        self.assertFalse(funnel.get('payment_request_through_seymy'))

    def test_ready_to_send_full_payment_case(self):
        client = self._session_client(
            self._complete_draft(with_destination=True, deposit=False, request_full_payment=True)
        )
        response = client.get(reverse('start_review_send'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        funnel = client.session['funnel']
        self.assertIn('HOW DO YOU WANT TO HANDLE THE PAYMENT?', content)
        self.assertIn('through Seymy', content)
        self.assertIn('handle payment outside Seymy.', content)
        self.assertIn('Send Estimate &amp; Request $', content)
        self.assertIn('Payment', content)
        self.assertIn('online payment request will be sent.', content)
        self.assertIn('View estimate &amp; pay:', content)
        self.assertIn(f"/e/{funnel['public_token']}/pay/", content)

    def test_send_estimate_reaches_done_awaiting_deposit(self):
        client = self._session_client(self._complete_draft(with_destination=True, deposit=True))
        # Prepare draft token first (as the contractor would via GET).
        client.get(reverse('start_review_send'))
        response = client.post(reverse('start_review_send'), {'action': 'send'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('start_sent'))
        funnel = client.session['funnel']
        self.assertTrue(funnel.get('payment_approved'))
        self.assertTrue(funnel.get('persisted_estimate_id'))
        self.assertFalse(funnel.get('deposit_paid'))
        self.assertEqual(funnel.get('payment_send_mode'), 'deposit')
        self.assertTrue(funnel.get('awaiting_deposit'))
        self.assertTrue(funnel.get('customer_delivery_sent'))
        self.assertTrue(funnel.get('contractor_copy_intent'))

        done = client.get(reverse('start_sent'))
        self.assertEqual(done.status_code, 200)
        done_html = done.content.decode()
        self.assertIn('DONE', done_html)
        self.assertIn('Estimate sent', done_html)
        self.assertIn('WAITING FOR PAYMENT', done_html)
        self.assertIn('NEED IT LATER?', done_html)
        self.assertIn('deposit request', done_html.lower())
        self.assertIn('Customer notified by text', done_html)
        self.assertIn('Copy of the estimate sent to your email', done_html)
        self.assertIn(
            'Seymy will notify you when John pays and automatically follow up if needed.',
            done_html,
        )
        self.assertIn('Reminders are scheduled for day 2 and day 5.', done_html)
        self.assertIn('Show me John Anderson&#x27;s estimate.', done_html)
        self.assertIn('Has John paid his deposit?', done_html)
        self.assertIn('Show me my unpaid estimates.', done_html)
        self.assertIn('Done', done_html)
        self.assertIn(reverse('index'), done_html)
        # No contractor payment-ops UI
        self.assertNotIn('Refresh status', done_html)
        self.assertNotIn('CUSTOMER PAYMENT LINK', done_html)
        self.assertNotIn('/pay/', done_html)
        self.assertNotIn('customer_pay_url', done_html)

    def test_send_estimate_no_payment_reaches_done(self):
        client = self._session_client(
            self._complete_draft(with_destination=True, deposit=False)
        )
        client.get(reverse('start_review_send'))
        response = client.post(reverse('start_review_send'), {'action': 'send'})
        self.assertEqual(response.status_code, 302)
        funnel = client.session['funnel']
        self.assertEqual(funnel.get('payment_send_mode'), 'none')
        self.assertFalse(funnel.get('awaiting_deposit'))
        done = client.get(reverse('start_sent'))
        done_html = done.content.decode()
        self.assertIn('Estimate sent', done_html)
        self.assertIn('received estimate #', done_html)
        self.assertIn('NEED IT LATER?', done_html)
        self.assertIn('Done', done_html)
        self.assertNotIn('WAITING FOR PAYMENT', done_html)
        self.assertNotIn('automatically follow up', done_html)
        self.assertNotIn('Reminders are scheduled', done_html)
        self.assertNotIn('deposit request', done_html.lower())
        self.assertNotIn('Refresh status', done_html)
        self.assertNotIn('/pay/', done_html)

    def test_summary_continue_asks_destination_without_sending(self):
        client = self._session_client(self._complete_draft())
        response = client.post(reverse('start_summary'), {'action': 'continue'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('mode=send', response.url)
        funnel = client.session['funnel']
        self.assertFalse(funnel.get('payment_approved'))
        self.assertFalse(funnel.get('persisted_estimate_id'))

    def test_edit_from_ready_returns_to_ready_without_sending(self):
        client = self._session_client(self._complete_draft(with_destination=True))
        response = client.post(
            reverse('start_job') + '?mode=edit',
            {'message': 'Change the deposit to 50%.'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('start_review_send'))
        funnel = client.session['funnel']
        self.assertEqual(str(funnel.get('deposit_value')), '50')
        self.assertFalse(funnel.get('payment_approved'))
        # Edit path itself does not send; draft may only appear after Ready-to-send GET.
        self.assertFalse(funnel.get('payment_approved'))

    def test_edit_dont_request_deposit_yet(self):
        client = self._session_client(self._complete_draft(with_destination=True, deposit=True))
        response = client.post(
            reverse('start_job') + '?mode=edit',
            {'message': "Don't request the deposit yet."},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('start_review_send'))
        funnel = client.session['funnel']
        # Deposit TERMS remain; online Seymy payment request is disabled.
        self.assertTrue(funnel.get('deposit_enabled'))
        self.assertFalse(funnel.get('payment_request_through_seymy'))
        self.assertEqual(funnel.get('payment_request_type'), 'none')
        self.assertFalse(funnel.get('payment_approved'))
        review = client.get(reverse('start_review_send'))
        content = review.content.decode()
        self.assertIn('HOW DO YOU WANT TO HANDLE THE DEPOSIT?', content)
        self.assertIn('Send Estimate Only', content)
        self.assertIn('No online payment will be requested.', content)
        self.assertIn('through Seymy', content)
        self.assertRegex(
            content,
            r'name="payment_request_through_seymy"\s+value="false"[^>]*checked'
            r'|value="false"[^>]*name="payment_request_through_seymy"[^>]*checked',
        )
        # PDF still shows deposit terms
        self.assertIn('Required deposit', content)


class PaymentArchitectureTests(TestCase):
    """Section 18 — MVP two-path payment architecture."""

    def _draft(self, *, deposit=True, through_seymy=None, request_full=False, destination=True):
        draft = apply_updates({}, parse_message(FULL_SPOKEN, existing={}))
        draft.update(
            {
                'has_user_provided_job_input': True,
                'business_name': 'Miller Plumbing',
                'business_address': '100 Trade St',
                'business_city': 'Orlando',
                'state': 'FL',
                'business_zip': '32801',
                'business_phone': '407-555-0100',
                'business_email': 'hello@millerplumbing.test',
                'business_profile_complete': True,
                'payment_approved': False,
                'deposit_paid': False,
            }
        )
        if not deposit:
            draft.update(
                {
                    'deposit_enabled': False,
                    'deposit_type': '',
                    'deposit_value': '',
                    'deposit_amount': '0.00',
                    'balance_amount': draft.get('estimate_total') or draft.get('total') or '0',
                }
            )
        if request_full:
            draft['request_full_payment'] = True
            draft['deposit_enabled'] = False
            draft['deposit_amount'] = '0.00'
        if through_seymy is not None:
            draft['payment_request_through_seymy'] = through_seymy
        if destination:
            draft.update(
                {
                    'customer_phone': '407-555-0182',
                    'send_method': 'sms',
                    'send_destination_set': True,
                }
            )
        return recalculate_financials(draft)

    def _client(self, draft):
        client = Client()
        session = client.session
        session['funnel'] = draft
        session.save()
        return client

    def test_deposit_estimate_defaults_to_seymy_payment_request(self):
        draft = self._draft(deposit=True)
        self.assertTrue(draft.get('deposit_enabled'))
        self.assertTrue(draft.get('payment_request_through_seymy'))
        self.assertEqual(draft.get('payment_request_type'), 'deposit')
        self.assertEqual(Decimal(draft['requested_payment_amount']), Decimal(draft['deposit_amount']))

    def test_contractor_can_disable_payment_request_conversationally(self):
        phrases = [
            'Send the estimate only.',
            'Just send the estimate.',
            "Don't request the deposit online.",
            "I'll collect the deposit myself.",
            'Just send him the PDF.',
            "Don't send a payment link.",
            "Don't request payment yet.",
        ]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                base = self._draft(deposit=True)
                updated = apply_updates(base, parse_message(phrase, existing=base))
                self.assertTrue(updated.get('deposit_enabled'), phrase)
                self.assertFalse(updated.get('payment_request_through_seymy'), phrase)
                self.assertEqual(updated.get('payment_request_type'), 'none', phrase)

    def test_reenable_payment_request_conversationally(self):
        base = self._draft(deposit=True, through_seymy=False)
        self.assertFalse(base.get('payment_request_through_seymy'))
        for phrase in (
            'Request the deposit online.',
            'Request the deposit through Seymy.',
            'Send him a payment link.',
        ):
            with self.subTest(phrase=phrase):
                updated = apply_updates(base, parse_message(phrase, existing=base))
                self.assertTrue(updated.get('payment_request_through_seymy'), phrase)
                self.assertEqual(updated.get('payment_request_type'), 'deposit', phrase)

    def test_request_full_amount_sets_full_payment_mode(self):
        base = self._draft(deposit=True)
        updated = apply_updates(base, parse_message('Request the full amount.', existing=base))
        self.assertFalse(updated.get('deposit_enabled'))
        self.assertTrue(updated.get('request_full_payment'))
        self.assertTrue(updated.get('payment_request_through_seymy'))
        self.assertEqual(updated.get('payment_request_type'), 'full')

    def test_disabling_payment_request_changes_cta_to_send_estimate_only(self):
        client = self._client(self._draft(deposit=True, through_seymy=False))
        response = client.get(reverse('start_review_send'))
        content = response.content.decode()
        self.assertIn('Send Estimate Only', content)
        self.assertIn('No online payment will be requested.', content)
        self.assertIn('Review your estimate below:', content)
        funnel = client.session['funnel']
        self.assertIn(f"/e/{funnel['public_token']}/document/", content)
        self.assertNotIn('/pay/', content.split('WHAT YOUR CUSTOMER WILL RECEIVE')[1])

    def test_send_estimate_only_creates_no_stripe_pi_or_platform_fee(self):
        from .models import Estimate, PendingCheckout

        client = self._client(self._draft(deposit=True, through_seymy=False))
        client.get(reverse('start_review_send'))
        response = client.post(
            reverse('start_review_send'),
            {'action': 'send', 'payment_request_through_seymy': 'false'},
        )
        self.assertEqual(response.status_code, 302)
        funnel = client.session['funnel']
        estimate_id = funnel['persisted_estimate_id']
        self.assertEqual(funnel.get('payment_send_mode'), 'none')
        self.assertFalse(funnel.get('awaiting_deposit'))
        self.assertFalse(funnel.get('payment_request_through_seymy'))
        self.assertEqual(PendingCheckout.objects.filter(estimate_id=estimate_id).count(), 0)

        estimate = Estimate.objects.get(pk=estimate_id)
        self.assertEqual(estimate.payment_status, 'none')
        version = estimate.current_version
        self.assertTrue(version.deposit_enabled)
        self.assertFalse(version.payment_request_through_seymy)
        self.assertEqual(version.payment_request_type, 'none')
        self.assertEqual(version.requested_payment_amount, Decimal('0.00'))
        self.assertFalse(
            PendingCheckout.objects.filter(estimate=estimate, seymy_platform_fee__gt=0).exists()
        )

    def test_send_with_estimate_only_choice_from_default_seymy_creates_no_stripe(self):
        from .models import Estimate, PendingCheckout

        # Default is through Seymy; contractor flips choice on the final form.
        client = self._client(self._draft(deposit=True, through_seymy=True))
        client.get(reverse('start_review_send'))
        self.assertTrue(client.session['funnel'].get('payment_request_through_seymy'))
        response = client.post(
            reverse('start_review_send'),
            {'action': 'send', 'payment_request_through_seymy': 'false'},
        )
        self.assertEqual(response.status_code, 302)
        funnel = client.session['funnel']
        self.assertFalse(funnel.get('payment_request_through_seymy'))
        self.assertEqual(funnel.get('payment_send_mode'), 'none')
        estimate = Estimate.objects.get(pk=funnel['persisted_estimate_id'])
        self.assertEqual(estimate.payment_status, 'none')
        self.assertFalse(estimate.current_version.payment_request_through_seymy)
        self.assertEqual(PendingCheckout.objects.filter(estimate=estimate).count(), 0)

    def test_deposit_request_creates_correct_requested_amount(self):
        draft = self._draft(deposit=True)
        estimate = publish_estimate_from_funnel(draft)
        version = estimate.current_version
        self.assertTrue(version.payment_request_through_seymy)
        self.assertEqual(version.payment_request_type, 'deposit')
        self.assertEqual(version.requested_payment_amount, version.deposit_amount)
        self.assertEqual(version.deposit_amount, Decimal('579.30'))

    def test_customer_message_and_cta_change_by_payment_mode(self):
        from django.test import RequestFactory

        from .views import _customer_message_preview_context

        rf = RequestFactory()
        request = rf.get('/')

        deposit = self._draft(deposit=True)
        ctx = _customer_message_preview_context(request, deposit)
        self.assertEqual(ctx['payment_send_mode'], 'deposit')
        self.assertTrue(ctx['show_payment_choice'])
        self.assertIn('DEPOSIT', ctx['payment_choice_heading'])
        self.assertIn('online deposit request will be sent', ctx['customer_message_explanation'])
        self.assertIn('Request $', ctx['send_cta_label'])
        self.assertIn('Deposit', ctx['send_cta_label'])

        only = self._draft(deposit=True, through_seymy=False)
        ctx = _customer_message_preview_context(request, only)
        self.assertEqual(ctx['payment_send_mode'], 'none')
        self.assertTrue(ctx['show_payment_choice'])
        self.assertFalse(ctx['payment_request_through_seymy'])
        self.assertEqual(ctx['send_cta_label'], 'Send Estimate Only')
        self.assertIn('No online payment will be requested', ctx['customer_message_explanation'])

        full = self._draft(deposit=False, request_full=True)
        ctx = _customer_message_preview_context(request, full)
        self.assertEqual(ctx['payment_send_mode'], 'full_payment')
        self.assertTrue(ctx['show_payment_choice'])
        self.assertIn('PAYMENT', ctx['payment_choice_heading'])
        self.assertIn('Request $', ctx['send_cta_label'])
        self.assertIn('Payment', ctx['send_cta_label'])

        none = self._draft(deposit=False, request_full=False)
        ctx = _customer_message_preview_context(request, none)
        self.assertFalse(ctx['show_payment_choice'])
        self.assertEqual(ctx['send_cta_label'], 'Send Estimate')
        self.assertIn('No payment is requested now', ctx['customer_message_explanation'])

    def test_failed_stripe_payment_does_not_mark_deposit_paid(self):
        from .models import EstimateStatus, PendingCheckout
        from .stripe_payments import process_stripe_event

        draft = self._draft(deposit=True)
        estimate = publish_estimate_from_funnel(draft)
        version = estimate.current_version
        PendingCheckout.objects.create(
            estimate=estimate,
            estimate_version=version,
            payment_intent_id='pi_fail_test',
            amount=version.deposit_amount,
            seymy_platform_fee=Decimal('5.79'),
            payment_request_type='deposit',
            agreed_to_terms=True,
            status='pending',
        )
        result = process_stripe_event(
            {
                'id': 'evt_fail_1',
                'type': 'payment_intent.payment_failed',
                'data': {'object': {'id': 'pi_fail_test'}},
            }
        )
        self.assertEqual(result['status'], 'ignored')
        estimate.refresh_from_db()
        self.assertIsNone(estimate.deposit_paid_at)
        self.assertNotEqual(estimate.status, EstimateStatus.DEPOSIT_PAID)
        self.assertEqual(estimate.payment_status, 'failed')
        self.assertFalse(estimate.acceptances.exists())

    def test_successful_stripe_payment_marks_deposit_paid_and_acceptance(self):
        from .models import EstimateStatus, PendingCheckout
        from .stripe_payments import process_stripe_event

        draft = self._draft(deposit=True)
        estimate = publish_estimate_from_funnel(draft)
        version = estimate.current_version
        fee = Decimal('5.79')
        PendingCheckout.objects.create(
            estimate=estimate,
            estimate_version=version,
            payment_intent_id='pi_ok_test',
            amount=version.deposit_amount,
            seymy_platform_fee=fee,
            payment_request_type='deposit',
            agreed_to_terms=True,
            status='pending',
        )
        result = process_stripe_event(
            {
                'id': 'evt_ok_1',
                'type': 'payment_intent.succeeded',
                'data': {'object': {'id': 'pi_ok_test'}},
            }
        )
        self.assertEqual(result['status'], 'ok')
        estimate.refresh_from_db()
        version.refresh_from_db()
        self.assertEqual(estimate.status, EstimateStatus.DEPOSIT_PAID)
        self.assertEqual(estimate.payment_status, 'paid')
        self.assertIsNotNone(estimate.deposit_paid_at)
        self.assertTrue(version.is_accepted)
        acceptance = estimate.acceptances.get()
        self.assertEqual(acceptance.accepted_amount, version.deposit_amount)
        checkout = PendingCheckout.objects.get(payment_intent_id='pi_ok_test')
        self.assertEqual(checkout.status, 'succeeded')
        self.assertEqual(checkout.seymy_platform_fee, fee)

    def test_duplicate_stripe_webhook_is_idempotent(self):
        from .models import PendingCheckout
        from .stripe_payments import process_stripe_event

        draft = self._draft(deposit=True)
        estimate = publish_estimate_from_funnel(draft)
        version = estimate.current_version
        PendingCheckout.objects.create(
            estimate=estimate,
            estimate_version=version,
            payment_intent_id='pi_dup_test',
            amount=version.deposit_amount,
            seymy_platform_fee=Decimal('5.79'),
            payment_request_type='deposit',
            agreed_to_terms=True,
            status='pending',
        )
        event = {
            'id': 'evt_dup_1',
            'type': 'payment_intent.succeeded',
            'data': {'object': {'id': 'pi_dup_test'}},
        }
        first = process_stripe_event(event)
        second = process_stripe_event(event)
        self.assertEqual(first['status'], 'ok')
        self.assertTrue(first['created'])
        self.assertEqual(second['status'], 'duplicate')
        self.assertEqual(estimate.acceptances.count(), 1)

    def test_checkout_records_platform_fee_only_for_seymy_payments(self):
        from django.test import RequestFactory

        from .models import PendingCheckout
        from .stripe_payments import create_deposit_checkout

        draft = self._draft(deposit=True)
        estimate = publish_estimate_from_funnel(draft)
        version = estimate.current_version
        rf = RequestFactory()
        request = rf.get('/')
        result = create_deposit_checkout(
            estimate=estimate,
            version=version,
            agreed_to_terms=True,
            request=request,
        )
        self.assertEqual(result['seymy_platform_fee'], '5.79')
        checkout = PendingCheckout.objects.get(payment_intent_id=result['payment_intent_id'])
        self.assertEqual(checkout.seymy_platform_fee, Decimal('5.79'))

        only = self._draft(deposit=True, through_seymy=False)
        estimate_only = publish_estimate_from_funnel(only)
        with self.assertRaises(ValueError):
            create_deposit_checkout(
                estimate=estimate_only,
                version=estimate_only.current_version,
                agreed_to_terms=True,
                request=request,
            )

    def test_no_manual_mark_as_paid_in_mvp_flow(self):
        from pathlib import Path

        client = self._client(self._draft(deposit=True))
        client.get(reverse('start_review_send'))
        client.post(reverse('start_review_send'), {'action': 'send'})
        sent = client.get(reverse('start_sent'))
        html = sent.content.decode()
        forbidden = (
            'Mark as paid',
            'Mark deposit received',
            'Simulate deposit received',
            'Deposit received manually',
            'I received the deposit',
            'Record cash payment',
        )
        for phrase in forbidden:
            self.assertNotIn(phrase, html)
        templates_dir = Path(__file__).resolve().parent / 'templates' / 'landing'
        for name in (
            'start_review_send.html',
            'start_sent.html',
            'start_deposit_paid.html',
        ):
            source = (templates_dir / name).read_text()
            for phrase in forbidden:
                self.assertNotIn(phrase, source)


class LandingDemoEstimatePdfTests(TestCase):
    def test_demo_estimate_pdf_returns_product_template(self):
        client = Client()
        response = client.get(reverse('landing_demo_estimate_pdf'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('Miller Plumbing', content)
        self.assertIn('SEY-001', content)
        self.assertIn('John Anderson', content)
        self.assertIn('24 Main Street', content)
        self.assertIn('Replace a 50-gallon water heater.', content)
        self.assertIn('1,931.00', content)
        self.assertIn('81.00', content)
        self.assertIn('579.30', content)
        self.assertIn('1,351.70', content)
        self.assertIn('Scope of work', content)
        self.assertIn('Project schedule', content)
        self.assertIn('Payment terms', content)
        self.assertIn('Terms &amp; conditions', content)
        self.assertIn('change order', content)
        self.assertIn('Valid until:', content)
        self.assertIn('size: Letter portrait', content)
        self.assertIn('Acceptance &amp; payment', content)
        self.assertIn('Powered by Seymy', content)
        self.assertNotIn('Customer name:', content)
        self.assertNotIn('Signature:', content)
        self.assertNotIn('Back to review', content)


class SnapshotScheduleTests(TestCase):
    def test_snapshot_includes_schedule_fields(self):
        funnel = apply_updates({}, parse_message(FULL_SPOKEN, existing={}))
        funnel['business_name'] = 'Miller Plumbing'
        snap = build_snapshot(funnel)
        self.assertEqual(snap.get('job_start_date'), '2026-08-20')
        self.assertEqual(snap.get('expected_completion_date'), '2026-08-24')
        estimate = publish_estimate_from_funnel(funnel)
        version = estimate.current_version
        self.assertEqual(version.job_start_date.isoformat(), '2026-08-20')
        self.assertEqual(version.expected_completion_date.isoformat(), '2026-08-24')
        self.assertEqual(version.snapshot.get('job_start_date'), '2026-08-20')


class ConversationalRetrievalTests(TestCase):
    def _publish(self, *, deposit=True, through_seymy=True, customer='John Anderson', number='1042'):
        draft = apply_updates({}, parse_message(FULL_SPOKEN, existing={}))
        draft.update(
            {
                'business_name': 'Miller Plumbing',
                'business_email': 'hello@millerplumbing.test',
                'customer_name': customer,
                'estimate_number': number,
                'payment_request_through_seymy': through_seymy,
            }
        )
        if not deposit:
            draft.update(
                {
                    'deposit_enabled': False,
                    'deposit_amount': '0.00',
                    'payment_request_through_seymy': False,
                }
            )
        return publish_estimate_from_funnel(draft)

    def test_parse_retrieval_intents(self):
        self.assertEqual(
            parse_retrieval_intent("Show me John Anderson's estimate.")['intent'],
            'show_by_customer',
        )
        self.assertEqual(
            parse_retrieval_intent('Has John paid his deposit?')['intent'],
            'has_paid',
        )
        self.assertEqual(
            parse_retrieval_intent('Show me estimate #1042')['intent'],
            'show_by_number',
        )
        self.assertEqual(
            parse_retrieval_intent('Show me my unpaid estimates.')['intent'],
            'unpaid',
        )
        self.assertEqual(
            parse_retrieval_intent('Which customers still owe me a deposit?')['intent'],
            'unpaid',
        )
        self.assertEqual(
            parse_retrieval_intent('Estimates sent this week')['intent'],
            'sent_this_week',
        )
        self.assertIsNone(parse_retrieval_intent(FULL_SPOKEN))

    def test_unpaid_and_paid_lookups(self):
        unpaid = self._publish(number='1042')
        paid = self._publish(customer='Sarah Lee', number='1043')
        from .models import PaymentStatus

        paid.payment_status = PaymentStatus.PAID
        paid.save(update_fields=['payment_status', 'updated_at'])

        funnel = {'business_name': 'Miller Plumbing'}
        unpaid_reply = answer_retrieval_query('Show me my unpaid estimates.', funnel=funnel)
        self.assertIn('John Anderson', unpaid_reply)
        self.assertIn('1042', unpaid_reply)
        self.assertNotIn('Sarah Lee', unpaid_reply)

        paid_reply = answer_retrieval_query('Has Sarah paid her deposit?', funnel=funnel)
        self.assertIn('Yes', paid_reply)
        self.assertIn('Sarah', paid_reply)

        waiting = answer_retrieval_query('Has John paid his deposit?', funnel=funnel)
        self.assertIn('Not yet', waiting)
        self.assertTrue(unpaid.pk)

    def test_show_estimate_by_number(self):
        self._publish(number='1042')
        reply = answer_retrieval_query(
            'Show me estimate #1042',
            funnel={'business_name': 'Miller Plumbing'},
        )
        self.assertIn('#1042', reply)
        self.assertIn('John Anderson', reply)
        self.assertIn('waiting for deposit', reply.lower())

    def test_ask_deep_link_from_done(self):
        client = Client()
        estimate = self._publish(number='1042')
        session = client.session
        session['funnel'] = {
            'business_name': 'Miller Plumbing',
            'customer_name': 'John Anderson',
            'estimate_number': '1042',
            'persisted_estimate_id': estimate.pk,
            'has_user_provided_job_input': True,
            'estimate_total': '1931.00',
            'job_description': 'Replace a 50-gallon water heater.',
        }
        session.save()
        response = client.get(
            reverse('start_job'),
            {'ask': "Show me John Anderson's estimate."},
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('Here', content)
        self.assertIn('1042', content)
        self.assertIn('John Anderson', content)

    def test_remind_and_stop_reminders_commands(self):
        estimate = self._publish(number='1042')
        funnel = {'business_name': 'Miller Plumbing'}
        self.assertEqual(
            parse_retrieval_intent('Remind John about the deposit.')['intent'],
            'remind_deposit',
        )
        self.assertEqual(
            parse_retrieval_intent("Don't remind John again.")['intent'],
            'stop_reminders',
        )
        self.assertEqual(
            parse_retrieval_intent('Show me my unpaid deposits.')['intent'],
            'unpaid',
        )
        self.assertEqual(
            parse_retrieval_intent('Who still owes me a deposit?')['intent'],
            'unpaid',
        )

        remind = answer_retrieval_query('Remind John about the deposit.', funnel=funnel)
        self.assertIn('Reminder sent', remind)
        estimate.refresh_from_db()
        self.assertEqual(estimate.reminder_count, 1)
        from .models import OutboundCustomerMessage

        self.assertTrue(
            OutboundCustomerMessage.objects.filter(
                estimate=estimate,
                purpose=OutboundCustomerMessage.PURPOSE_REMINDER_MANUAL,
            ).exists()
        )

        stop = answer_retrieval_query("Don't remind John again.", funnel=funnel)
        self.assertIn("won't send", stop.lower())
        estimate.refresh_from_db()
        self.assertTrue(estimate.reminders_cancelled)
        self.assertIsNone(estimate.reminder_day2_at)
        self.assertIsNone(estimate.reminder_day5_at)

        unpaid = answer_retrieval_query('Show me my unpaid deposits.', funnel=funnel)
        self.assertIn('John Anderson', unpaid)


class DepositReminderTests(TestCase):
    def _publish(self, *, through_seymy=True, customer='John Anderson', number='1042'):
        draft = apply_updates({}, parse_message(FULL_SPOKEN, existing={}))
        draft.update(
            {
                'business_name': 'Miller Plumbing',
                'business_email': 'hello@millerplumbing.test',
                'customer_name': customer,
                'customer_phone': '407-555-0182',
                'estimate_number': number,
                'payment_request_through_seymy': through_seymy,
            }
        )
        if not through_seymy:
            draft['payment_request_through_seymy'] = False
        return publish_estimate_from_funnel(draft)

    def test_schedules_day2_and_day5_on_through_seymy_send(self):
        estimate = self._publish(through_seymy=True)
        self.assertIsNotNone(estimate.sent_at)
        self.assertFalse(estimate.reminders_cancelled)
        self.assertIsNotNone(estimate.reminder_day2_at)
        self.assertIsNotNone(estimate.reminder_day5_at)
        delta2 = estimate.reminder_day2_at - estimate.sent_at
        delta5 = estimate.reminder_day5_at - estimate.sent_at
        self.assertEqual(delta2.days, 2)
        self.assertEqual(delta5.days, 5)

    def test_no_schedule_for_estimate_only(self):
        estimate = self._publish(through_seymy=False)
        self.assertFalse(estimate.current_version.payment_request_through_seymy)
        self.assertIsNone(estimate.reminder_day2_at)
        self.assertIsNone(estimate.reminder_day5_at)

    def test_day2_and_day5_send_when_unpaid(self):
        from datetime import timedelta

        from .delivery import process_deposit_reminders
        from .models import OutboundCustomerMessage

        estimate = self._publish()
        day2 = estimate.reminder_day2_at
        day5 = estimate.reminder_day5_at

        result2 = process_deposit_reminders(now=day2 + timedelta(minutes=1))
        self.assertEqual(result2['sent_day2'], 1)
        self.assertEqual(result2['sent_day5'], 0)
        estimate.refresh_from_db()
        self.assertEqual(estimate.reminder_count, 1)
        self.assertIsNone(estimate.reminder_day2_at)
        self.assertIsNotNone(estimate.reminder_day5_at)
        msg2 = OutboundCustomerMessage.objects.get(
            estimate=estimate, purpose=OutboundCustomerMessage.PURPOSE_REMINDER_DAY2
        )
        self.assertIn('just a reminder', msg2.body.lower())
        self.assertIn('deposit', msg2.body.lower())
        self.assertIn('#1042', msg2.body)
        self.assertIn('/e/', msg2.body)

        result5 = process_deposit_reminders(now=day5 + timedelta(minutes=1))
        self.assertEqual(result5['sent_day5'], 1)
        estimate.refresh_from_db()
        self.assertEqual(estimate.reminder_count, 2)
        self.assertIsNone(estimate.reminder_day5_at)
        msg5 = OutboundCustomerMessage.objects.get(
            estimate=estimate, purpose=OutboundCustomerMessage.PURPOSE_REMINDER_DAY5
        )
        self.assertIn('still pending', msg5.body.lower())
        self.assertIn('required to approve', msg5.body.lower())

        # No more automatic reminders after day +5
        again = process_deposit_reminders(now=day5 + timedelta(days=10))
        self.assertEqual(again['sent_day2'], 0)
        self.assertEqual(again['sent_day5'], 0)
        estimate.refresh_from_db()
        self.assertEqual(estimate.reminder_count, 2)

    def test_skips_when_paid(self):
        from datetime import timedelta

        from .delivery import process_deposit_reminders
        from .models import OutboundCustomerMessage, PendingCheckout
        from .acceptance import record_deposit_acceptance

        estimate = self._publish()
        day2 = estimate.reminder_day2_at
        version = estimate.current_version
        PendingCheckout.objects.create(
            estimate=estimate,
            estimate_version=version,
            payment_intent_id='pi_reminder_paid',
            amount=version.requested_payment_amount or version.deposit_amount,
            seymy_platform_fee=Decimal('5.79'),
            payment_request_type=version.payment_request_type,
            agreed_to_terms=True,
        )
        record_deposit_acceptance(payment_intent_id='pi_reminder_paid')
        estimate.refresh_from_db()
        self.assertTrue(estimate.reminders_cancelled)
        self.assertIsNone(estimate.reminder_day2_at)
        self.assertIsNone(estimate.reminder_day5_at)

        result = process_deposit_reminders(now=day2 + timedelta(days=1))
        self.assertEqual(result['sent_day2'], 0)
        self.assertEqual(result['sent_day5'], 0)
        self.assertFalse(
            OutboundCustomerMessage.objects.filter(
                estimate=estimate,
                purpose__in=[
                    OutboundCustomerMessage.PURPOSE_REMINDER_DAY2,
                    OutboundCustomerMessage.PURPOSE_REMINDER_DAY5,
                ],
            ).exists()
        )

    def test_skips_when_cancelled_or_expired(self):
        from datetime import timedelta

        from .delivery import cancel_estimate, process_deposit_reminders
        from .models import OutboundCustomerMessage

        cancelled = self._publish(number='2001', customer='Cancel Me')
        day2 = cancelled.reminder_day2_at
        cancel_estimate(cancelled)
        cancelled.refresh_from_db()
        self.assertTrue(cancelled.reminders_cancelled)

        expired = self._publish(number='2002', customer='Expire Me')
        version = expired.current_version
        snap = dict(version.snapshot or {})
        snap['expiration_date'] = '2026-01-01'
        version.snapshot = snap
        version.save(update_fields=['snapshot'])

        result = process_deposit_reminders(now=day2 + timedelta(minutes=1))
        self.assertEqual(result['sent_day2'], 0)
        expired.refresh_from_db()
        self.assertTrue(expired.reminders_cancelled)
        self.assertFalse(
            OutboundCustomerMessage.objects.filter(
                purpose=OutboundCustomerMessage.PURPOSE_REMINDER_DAY2
            ).exists()
        )
