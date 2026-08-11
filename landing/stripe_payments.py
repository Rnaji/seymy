"""
Stripe payment helpers for deposit acceptance.

Verified webhook / payment confirmation is the only path that marks
DEPOSIT PAID and ESTIMATE ACCEPTED. Frontend success alone is insufficient.
"""

from __future__ import annotations

import logging
import secrets
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import transaction
from django.urls import reverse

from .acceptance import platform_fee_for_amount, record_deposit_acceptance
from .models import Estimate, EstimateVersion, PaymentRequestType, PaymentStatus, PendingCheckout, StripeWebhookEvent

logger = logging.getLogger(__name__)


def stripe_configured() -> bool:
    return bool(getattr(settings, 'STRIPE_SECRET_KEY', '') or '')


def _stripe():
    import stripe

    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def amount_to_cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal('1')))


@transaction.atomic
def create_deposit_checkout(
    *,
    estimate: Estimate,
    version: EstimateVersion,
    agreed_to_terms: bool,
    request,
) -> dict[str, Any]:
    """
    Create a Stripe Checkout Session (or local test PI when Stripe is unset).

    Requires agreed_to_terms=True. Does not mark the estimate accepted.
    Only allowed when payment_request_through_seymy is True on the version.
    """
    if not agreed_to_terms:
        raise ValueError('Customer must agree to the Estimate and Terms & Conditions')

    if not version.payment_request_through_seymy:
        snap_through = bool((version.snapshot or {}).get('payment_request_through_seymy'))
        if not snap_through:
            raise ValueError('This estimate was sent without an online payment request')

    if estimate.acceptance_requirement not in ('payment', 'payment_and_signature'):
        raise ValueError('This estimate requires an electronic signature before payment')

    request_type = version.payment_request_type or (version.snapshot or {}).get(
        'payment_request_type'
    )
    if request_type == PaymentRequestType.FULL or (
        not version.deposit_enabled
        and bool((version.snapshot or {}).get('request_full_payment'))
    ):
        amount = version.total_amount
        product_name = f'Payment — Estimate #{estimate.estimate_number}'
        product_description = f'{estimate.business_name} payment'
        request_type = PaymentRequestType.FULL
    elif version.deposit_enabled or request_type == PaymentRequestType.DEPOSIT:
        amount = version.deposit_amount or version.requested_payment_amount
        product_name = f'Deposit — Estimate #{estimate.estimate_number}'
        product_description = f'{estimate.business_name} deposit'
        request_type = PaymentRequestType.DEPOSIT
    else:
        raise ValueError('No online payment is requested on this estimate')

    if version.is_accepted:
        raise ValueError('This estimate version was already accepted')

    platform_fee = platform_fee_for_amount(amount)
    currency = getattr(settings, 'STRIPE_CURRENCY', 'usd')

    if stripe_configured():
        stripe = _stripe()
        success_url = request.build_absolute_uri(
            reverse('customer_pay_success', kwargs={'token': estimate.public_token})
        ) + '?session_id={CHECKOUT_SESSION_ID}'
        cancel_url = request.build_absolute_uri(
            reverse('customer_pay', kwargs={'token': estimate.public_token})
        )
        session = stripe.checkout.Session.create(
            mode='payment',
            success_url=success_url,
            cancel_url=cancel_url,
            line_items=[
                {
                    'price_data': {
                        'currency': currency,
                        'unit_amount': amount_to_cents(amount),
                        'product_data': {
                            'name': product_name,
                            'description': product_description,
                        },
                    },
                    'quantity': 1,
                }
            ],
            metadata={
                'estimate_id': str(estimate.pk),
                'estimate_version_id': str(version.pk),
                'estimate_version': str(version.version_number),
                'document_version': str(version.document_version),
                'terms_version': version.terms_version,
                'acceptance_method': 'deposit_payment',
                'payment_request_type': request_type,
                'seymy_platform_fee': str(platform_fee),
                'seymy_platform_fee_note': '1_percent_platform_fee_not_card_fee',
            },
            payment_intent_data={
                'metadata': {
                    'estimate_id': str(estimate.pk),
                    'estimate_version_id': str(version.pk),
                    'seymy_platform_fee': str(platform_fee),
                }
            },
        )
        payment_intent_id = session.payment_intent
        if isinstance(payment_intent_id, dict):
            payment_intent_id = payment_intent_id.get('id')
        if not payment_intent_id:
            # Expand if needed
            retrieved = stripe.checkout.Session.retrieve(session.id, expand=['payment_intent'])
            pi = retrieved.payment_intent
            payment_intent_id = pi if isinstance(pi, str) else getattr(pi, 'id', None)
        if not payment_intent_id:
            payment_intent_id = f'pending_session_{session.id}'

        PendingCheckout.objects.update_or_create(
            payment_intent_id=str(payment_intent_id),
            defaults={
                'estimate': estimate,
                'estimate_version': version,
                'checkout_session_id': session.id,
                'amount': amount,
                'seymy_platform_fee': platform_fee,
                'payment_request_type': request_type,
                'agreed_to_terms': True,
                'status': 'pending',
            },
        )
        if estimate.payment_status != PaymentStatus.PAID:
            estimate.payment_status = PaymentStatus.REQUESTED
            estimate.save(update_fields=['payment_status', 'updated_at'])
        return {
            'mode': 'stripe',
            'checkout_url': session.url,
            'session_id': session.id,
            'payment_intent_id': str(payment_intent_id),
            'seymy_platform_fee': str(platform_fee),
        }

    # Local / test path when Stripe keys are not configured.
    # Payment is still NOT accepted until process_stripe_event runs.
    payment_intent_id = f'pi_test_{secrets.token_hex(12)}'
    PendingCheckout.objects.create(
        estimate=estimate,
        estimate_version=version,
        payment_intent_id=payment_intent_id,
        checkout_session_id='',
        amount=amount,
        seymy_platform_fee=platform_fee,
        payment_request_type=request_type,
        agreed_to_terms=True,
        status='pending',
    )
    if estimate.payment_status != PaymentStatus.PAID:
        estimate.payment_status = PaymentStatus.REQUESTED
        estimate.save(update_fields=['payment_status', 'updated_at'])
    confirm_url = request.build_absolute_uri(
        reverse('customer_pay_test_confirm', kwargs={'token': estimate.public_token})
    ) + f'?payment_intent_id={payment_intent_id}'
    return {
        'mode': 'test',
        'checkout_url': confirm_url,
        'session_id': '',
        'payment_intent_id': payment_intent_id,
        'seymy_platform_fee': str(platform_fee),
    }


def process_stripe_event(event: dict) -> dict[str, Any]:
    """
    Process a Stripe event payload idempotently.

    Only payment success paths create acceptance / deposit-paid status.
    """
    event_id = event.get('id') or ''
    event_type = event.get('type') or ''
    if not event_id:
        raise ValueError('Stripe event missing id')

    if StripeWebhookEvent.objects.filter(event_id=event_id).exists():
        return {'status': 'duplicate', 'event_id': event_id}

    data_object = (event.get('data') or {}).get('object') or {}
    payment_intent_id = ''

    if event_type == 'checkout.session.completed':
        if data_object.get('payment_status') != 'paid' and data_object.get('status') != 'complete':
            StripeWebhookEvent.objects.create(
                event_id=event_id,
                event_type=event_type,
                payment_intent_id='',
            )
            return {'status': 'ignored', 'reason': 'checkout_not_paid'}
        pi = data_object.get('payment_intent')
        payment_intent_id = pi if isinstance(pi, str) else (pi or {}).get('id', '')
        session_id = data_object.get('id') or ''
        if not payment_intent_id and session_id:
            checkout = PendingCheckout.objects.filter(checkout_session_id=session_id).first()
            if checkout:
                payment_intent_id = checkout.payment_intent_id
        if session_id and payment_intent_id:
            PendingCheckout.objects.filter(checkout_session_id=session_id).exclude(
                payment_intent_id=payment_intent_id
            ).update(payment_intent_id=payment_intent_id)

    elif event_type in ('payment_intent.succeeded', 'payment_intent.payment_failed'):
        payment_intent_id = data_object.get('id') or ''
        if event_type == 'payment_intent.payment_failed':
            if payment_intent_id:
                checkout = PendingCheckout.objects.filter(payment_intent_id=payment_intent_id).first()
                PendingCheckout.objects.filter(payment_intent_id=payment_intent_id).update(
                    status='failed'
                )
                if checkout and checkout.estimate.payment_status != PaymentStatus.PAID:
                    checkout.estimate.payment_status = PaymentStatus.FAILED
                    checkout.estimate.save(update_fields=['payment_status', 'updated_at'])
            StripeWebhookEvent.objects.create(
                event_id=event_id,
                event_type=event_type,
                payment_intent_id=payment_intent_id or '',
            )
            return {'status': 'ignored', 'reason': 'payment_failed'}

    else:
        StripeWebhookEvent.objects.create(
            event_id=event_id,
            event_type=event_type,
            payment_intent_id='',
        )
        return {'status': 'ignored', 'reason': 'unhandled_type', 'event_type': event_type}

    if not payment_intent_id:
        StripeWebhookEvent.objects.create(
            event_id=event_id,
            event_type=event_type,
            payment_intent_id='',
        )
        return {'status': 'ignored', 'reason': 'missing_payment_intent'}

    acceptance, created = record_deposit_acceptance(payment_intent_id=payment_intent_id)
    StripeWebhookEvent.objects.create(
        event_id=event_id,
        event_type=event_type,
        payment_intent_id=payment_intent_id,
    )
    return {
        'status': 'ok',
        'created': created,
        'acceptance_id': acceptance.pk,
        'payment_intent_id': payment_intent_id,
        'estimate_id': acceptance.estimate_id,
        'estimate_version': acceptance.estimate_version_number,
    }


def construct_webhook_event(payload: bytes, sig_header: str | None) -> dict:
    """Verify Stripe signature when webhook secret is configured."""
    secret = getattr(settings, 'STRIPE_WEBHOOK_SECRET', '') or ''
    if secret and stripe_configured():
        stripe = _stripe()
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
        return dict(event)
    # Tests / local without signing secret: parse JSON payload directly
    import json

    return json.loads(payload.decode('utf-8'))
