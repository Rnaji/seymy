"""
Development-only reset of Seymy test / funnel business data.

Preserves Django auth users. Clears estimate DB rows and server sessions'
funnel drafts. Optional flag also clears business-profile keys from sessions
so first-ever onboarding can be retested.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from landing.models import (
    Estimate,
    EstimateAcceptance,
    EstimateVersion,
    OutboundCustomerMessage,
    PendingCheckout,
    StripeWebhookEvent,
)
from landing.numbering import INITIAL_ESTIMATE_NUMBER

# Mirrored from landing.views._BUSINESS_PROFILE_KEYS — persistent contractor identity.
BUSINESS_PROFILE_KEYS = (
    'business_name',
    'business_address',
    'business_city',
    'state',
    'business_zip',
    'business_phone',
    'business_email',
    'license_number',
    'logo_url',
    'business_profile_complete',
    'estimate_validity_days',
    'terms_and_conditions',
    'accepted_payment_methods',
    'sales_tax_enabled',
    'default_sales_tax_rate',
    'sales_tax_preference_set',
)


def _empty_draft(*, preserved: dict | None = None) -> dict:
    return {
        **(preserved or {}),
        'has_user_provided_job_input': False,
        'conversation_history': [],
        'conversation_mode': '',
        'pending_question': '',
        'payment_approved': False,
        'deposit_paid': False,
    }


class Command(BaseCommand):
    help = (
        'DEV ONLY: delete all estimates and related test records, reset '
        'server-side funnel session drafts, and reset numbering to SEY-001. '
        'Auth users are preserved. Use --clear-business-profile to also wipe '
        'onboarding business fields from sessions.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear-business-profile',
            action='store_true',
            help=(
                'Also clear contractor business profile keys from sessions '
                '(re-test first-ever onboarding). Does not delete auth users.'
            ),
        )
        parser.add_argument(
            '--flush-sessions',
            action='store_true',
            help=(
                'Delete all Django session rows entirely (forces every browser '
                'to get a new empty session). Stronger than rewriting funnel keys.'
            ),
        )
        parser.add_argument(
            '--yes',
            action='store_true',
            help='Skip interactive confirmation.',
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options['yes']:
            raise CommandError(
                'Refusing to run outside DEBUG without --yes. '
                'This command deletes estimate data.'
            )

        clear_business = options['clear_business_profile']
        flush_sessions = options['flush_sessions']

        if not options['yes']:
            self.stdout.write(self.style.WARNING(
                'This will DELETE all estimates, payments, acceptances, '
                'reminders, outbound messages, and reset funnel sessions.'
            ))
            if clear_business:
                self.stdout.write(self.style.WARNING(
                    'Also clearing business profile fields from sessions.'
                ))
            confirm = input('Type RESET to continue: ').strip()
            if confirm != 'RESET':
                raise CommandError('Aborted.')

        with transaction.atomic():
            counts = self._delete_business_data()

        session_stats = self._reset_sessions(
            clear_business=clear_business,
            flush_all=flush_sessions,
        )

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Seymy test data reset complete.'))
        self.stdout.write(f"  Estimates deleted:              {counts['estimates']}")
        self.stdout.write(f"  Estimate versions deleted:      {counts['versions']}")
        self.stdout.write(f"  Acceptances deleted:            {counts['acceptances']}")
        self.stdout.write(f"  Pending checkouts deleted:      {counts['checkouts']}")
        self.stdout.write(f"  Stripe webhook events deleted:  {counts['webhooks']}")
        self.stdout.write(f"  Outbound messages deleted:      {counts['outbound']}")
        self.stdout.write(f"  Sessions flushed:               {session_stats['flushed']}")
        self.stdout.write(f"  Sessions funnel-cleared:        {session_stats['cleared']}")
        self.stdout.write(f"  Business profile preserved:     {not clear_business and not flush_sessions}")
        self.stdout.write(f"  Next estimate number:           {INITIAL_ESTIMATE_NUMBER}")
        self.stdout.write('')
        self.stdout.write(
            'Browser: no localStorage/sessionStorage keys are used. '
            'If the old draft still appears, hard-refresh or clear the '
            'sessionid cookie, then open Create My First Estimate again.'
        )
        self._verify()

    def _delete_business_data(self) -> dict:
        # Break self-FKs on Estimate before cascade-friendly deletes.
        Estimate.objects.all().update(current_version=None, accepted_version=None)

        acceptances = EstimateAcceptance.objects.all().delete()[0]
        checkouts = PendingCheckout.objects.all().delete()[0]
        outbound = OutboundCustomerMessage.objects.all().delete()[0]
        webhooks = StripeWebhookEvent.objects.all().delete()[0]
        versions = EstimateVersion.objects.all().delete()[0]
        estimates = Estimate.objects.all().delete()[0]

        return {
            'acceptances': acceptances,
            'checkouts': checkouts,
            'outbound': outbound,
            'webhooks': webhooks,
            'versions': versions,
            'estimates': estimates,
        }

    def _reset_sessions(self, *, clear_business: bool, flush_all: bool) -> dict:
        if flush_all:
            deleted, _ = Session.objects.all().delete()
            return {'flushed': deleted, 'cleared': 0}

        cleared = 0
        for row in Session.objects.all():
            store = SessionStore(session_key=row.session_key)
            try:
                # Load existing data; expired keys raise / return empty.
                _ = store.load()
            except Exception:
                continue
            funnel = store.get('funnel')
            if funnel is None and 'payment_notification' not in store:
                continue

            if clear_business:
                store['funnel'] = _empty_draft()
            elif funnel is not None:
                preserved = {
                    key: funnel[key]
                    for key in BUSINESS_PROFILE_KEYS
                    if funnel.get(key) not in (None, '')
                }
                store['funnel'] = _empty_draft(preserved=preserved)
            else:
                store['funnel'] = _empty_draft()

            for key in ('payment_notification', 'payment_notification_message'):
                if key in store:
                    del store[key]

            store.save()
            cleared += 1

        return {'flushed': 0, 'cleared': cleared}

    def _verify(self):
        from landing.numbering import next_estimate_number

        estimates = Estimate.objects.count()
        drafts = Estimate.objects.filter(status='draft').count()
        pending = PendingCheckout.objects.filter(status='pending').count()
        reminders = Estimate.objects.exclude(
            reminder_day2_at=None, reminder_day5_at=None
        ).count()
        next_num = next_estimate_number()

        self.stdout.write('')
        self.stdout.write('Verification:')
        self.stdout.write(f'  Customers (estimate customer rows): n/a — customers live on estimates')
        self.stdout.write(f'  Estimates: {estimates}')
        self.stdout.write(f'  Active estimate drafts: {drafts}')
        self.stdout.write(f'  Pending payment requests: {pending}')
        self.stdout.write(f'  Scheduled reminders: {reminders}')
        self.stdout.write(f'  Next allocate number: {next_num}')

        if estimates or drafts or pending or reminders or next_num != INITIAL_ESTIMATE_NUMBER:
            self.stdout.write(self.style.ERROR('Verification found leftover state.'))
        else:
            self.stdout.write(self.style.SUCCESS('Verification OK — clean first-use DB state.'))
