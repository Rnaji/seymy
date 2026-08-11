"""
Estimate versioning, freeze, and deposit-payment acceptance.

Acceptance via deposit payment is the MVP default when
acceptance_requirement == "payment". Architecture supports later
electronic_signature / payment_and_signature without assuming payment
alone is legally sufficient in every US state.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.db import transaction
from django.utils import timezone

from .delivery import cancel_deposit_reminders, schedule_deposit_reminders
from .models import (
    AcceptanceMethod,
    AcceptanceRequirement,
    Estimate,
    EstimateAcceptance,
    EstimateStatus,
    EstimateVersion,
    PaymentRequestType,
    PaymentStatus,
    PendingCheckout,
)


def _money(value) -> Decimal:
    return Decimal(str(value or '0')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def platform_fee_for_amount(amount: Decimal) -> Decimal:
    """Seymy 1% platform fee — only for payments processed through Seymy."""
    return _money(_money(amount) * Decimal('0.01'))


def resolve_payment_request(data: dict) -> dict:
    """
    Normalize online payment-request fields.

    Deposit TERMS (deposit_enabled / deposit_amount) may remain on the PDF
    even when payment_request_through_seymy is False (estimate-only send).
    """
    merged = {**data}
    deposit_enabled = bool(merged.get('deposit_enabled'))
    request_full = bool(merged.get('request_full_payment')) and not deposit_enabled
    through = merged.get('payment_request_through_seymy')

    if through is False:
        merged['payment_request_through_seymy'] = False
        merged['payment_request_type'] = PaymentRequestType.NONE
        merged['requested_payment_amount'] = '0.00'
        return merged

    if deposit_enabled:
        # Default: request deposit through Seymy when terms warrant it
        merged['payment_request_through_seymy'] = True if through is None else bool(through)
        if merged['payment_request_through_seymy']:
            merged['payment_request_type'] = PaymentRequestType.DEPOSIT
            merged['requested_payment_amount'] = str(_money(merged.get('deposit_amount') or '0'))
        else:
            merged['payment_request_type'] = PaymentRequestType.NONE
            merged['requested_payment_amount'] = '0.00'
        return merged

    if request_full:
        merged['payment_request_through_seymy'] = True if through is None else bool(through)
        if merged['payment_request_through_seymy']:
            merged['payment_request_type'] = PaymentRequestType.FULL
            merged['requested_payment_amount'] = str(
                _money(merged.get('total') or merged.get('estimate_total') or '0')
            )
        else:
            merged['payment_request_type'] = PaymentRequestType.NONE
            merged['requested_payment_amount'] = '0.00'
        return merged

    merged['payment_request_through_seymy'] = False
    merged['payment_request_type'] = PaymentRequestType.NONE
    merged['requested_payment_amount'] = '0.00'
    return merged


# Fields that, if changed after send/accept, require a new estimate version.
MATERIAL_SNAPSHOT_KEYS = (
    'estimate_total',
    'total',
    'subtotal',
    'tax_amount',
    'tax_rate',
    'tax_enabled',
    'tax_applies_to',
    'deposit_enabled',
    'deposit_type',
    'deposit_value',
    'deposit_amount',
    'balance_amount',
    'balance_due_date',
    'request_full_payment',
    'payment_request_through_seymy',
    'payment_request_type',
    'requested_payment_amount',
    'job_start_date',
    'expected_completion_date',
    'job_description',
    'line_items',
    'terms_and_conditions',
    'pass_card_fees_to_customer',
    'customer_name',
    'job_site_address',
    'job_site_city',
    'job_site_state',
    'job_site_zip',
)


def build_snapshot(funnel_data: dict) -> dict:
    """Freeze a JSON-serializable copy of contractual estimate data."""
    snapshot = {}
    for key in MATERIAL_SNAPSHOT_KEYS:
        if key in funnel_data:
            snapshot[key] = deepcopy(funnel_data[key])
    # Also keep display / identity fields useful for PDF + pay page
    for key in (
        'estimate_number',
        'business_name',
        'business_address',
        'business_city',
        'state',
        'business_zip',
        'business_phone',
        'business_email',
        'license_number',
        'logo_url',
        'customer_phone',
        'customer_email',
        'issue_date',
        'expiration_date',
        'estimate_validity_days',
        'accepted_payment_methods',
        'business_credentials',
        'insured_bonded',
    ):
        if key in funnel_data and funnel_data[key] not in (None, ''):
            snapshot[key] = deepcopy(funnel_data[key])
    return snapshot


def material_differs(snapshot: dict, funnel_data: dict) -> bool:
    left = build_snapshot(snapshot if snapshot else {})
    right = build_snapshot(funnel_data)
    for key in MATERIAL_SNAPSHOT_KEYS:
        if left.get(key) != right.get(key):
            return True
    return False


def terms_version_for(funnel_data: dict) -> str:
    import hashlib

    explicit = (funnel_data.get('terms_version') or '').strip()
    if explicit:
        return explicit
    terms = (funnel_data.get('terms_and_conditions') or '').strip()
    if not terms:
        return '1'
    digest = hashlib.sha256(terms.encode('utf-8')).hexdigest()[:10]
    return f'terms-{digest}'


def _parse_optional_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, '%Y-%m-%d').date()
    except ValueError:
        return None


def _version_payload_from_funnel(funnel_data: dict) -> dict:
    funnel_data = resolve_payment_request(funnel_data)
    snapshot = build_snapshot(funnel_data)
    terms_text = (funnel_data.get('terms_and_conditions') or '').strip()
    terms_version = terms_version_for(funnel_data)
    total = _money(funnel_data.get('total') or funnel_data.get('estimate_total') or '0')
    deposit_amount = _money(funnel_data.get('deposit_amount') or '0')
    balance_amount = _money(funnel_data.get('balance_amount') or '0')
    deposit_enabled = bool(funnel_data.get('deposit_enabled'))
    payment_request_through_seymy = bool(funnel_data.get('payment_request_through_seymy'))
    payment_request_type = funnel_data.get('payment_request_type') or PaymentRequestType.NONE
    requested_payment_amount = _money(funnel_data.get('requested_payment_amount') or '0')
    job_start = _parse_optional_date(funnel_data.get('job_start_date'))
    expected_completion = _parse_optional_date(funnel_data.get('expected_completion_date'))
    return dict(
        snapshot=snapshot,
        terms_version=terms_version,
        terms_text=terms_text,
        total_amount=total,
        deposit_amount=deposit_amount,
        balance_amount=balance_amount,
        deposit_enabled=deposit_enabled,
        payment_request_through_seymy=payment_request_through_seymy,
        payment_request_type=payment_request_type,
        requested_payment_amount=requested_payment_amount,
        job_start_date=job_start,
        expected_completion_date=expected_completion,
    )


@transaction.atomic
def prepare_draft_estimate_from_funnel(funnel_data: dict) -> Estimate:
    """
    Create or refresh a DRAFT estimate so the public customer link is real
    before the contractor confirms send. Does not mark the estimate as sent.
    """
    from .numbering import ensure_estimate_number

    funnel_data = ensure_estimate_number(dict(funnel_data))
    estimate_id = funnel_data.get('persisted_estimate_id')
    estimate = Estimate.objects.filter(pk=estimate_id).first() if estimate_id else None
    version_kwargs = _version_payload_from_funnel(funnel_data)
    terms_text = version_kwargs['terms_text']

    if estimate is None:
        estimate = Estimate.objects.create(
            estimate_number=str(funnel_data.get('estimate_number') or ''),
            business_name=str(funnel_data.get('business_name') or ''),
            customer_name=str(funnel_data.get('customer_name') or ''),
            status=EstimateStatus.DRAFT,
            acceptance_requirement=AcceptanceRequirement.PAYMENT,
        )
        version = EstimateVersion.objects.create(
            estimate=estimate,
            version_number=1,
            **version_kwargs,
        )
        estimate.current_version = version
        estimate.save(update_fields=['current_version', 'updated_at'])
        return estimate

    current = estimate.current_version
    estimate.estimate_number = str(funnel_data.get('estimate_number') or estimate.estimate_number)
    estimate.business_name = str(funnel_data.get('business_name') or estimate.business_name)
    estimate.customer_name = str(funnel_data.get('customer_name') or estimate.customer_name)

    # Refresh an unsent, unaccepted draft version in place to avoid version spam.
    if (
        current is not None
        and not current.is_accepted
        and estimate.status == EstimateStatus.DRAFT
        and estimate.sent_at is None
    ):
        for field, value in version_kwargs.items():
            setattr(current, field, value)
        current.save()
        estimate.save()
        return estimate

    needs_new_version = (
        current is None
        or current.is_accepted
        or material_differs(current.snapshot, funnel_data)
        or current.terms_text != terms_text
    )
    if needs_new_version:
        next_number = (current.version_number + 1) if current else 1
        version = EstimateVersion.objects.create(
            estimate=estimate,
            version_number=next_number,
            **version_kwargs,
        )
        estimate.current_version = version

    if estimate.status != EstimateStatus.SENT or estimate.sent_at is None:
        estimate.status = EstimateStatus.DRAFT

    estimate.save()
    return estimate


@transaction.atomic
def publish_estimate_from_funnel(
    funnel_data: dict,
    *,
    acceptance_requirement: str = AcceptanceRequirement.PAYMENT,
) -> Estimate:
    """
    Create or update an Estimate and freeze the current version for sending.

    If an existing estimate was already accepted and material fields changed,
    creates a new version instead of mutating the accepted snapshot.
    """
    from .numbering import ensure_estimate_number

    funnel_data = ensure_estimate_number(dict(funnel_data))
    funnel_data = resolve_payment_request(funnel_data)
    estimate_id = funnel_data.get('persisted_estimate_id')
    estimate = None
    if estimate_id:
        estimate = Estimate.objects.filter(pk=estimate_id).first()

    version_kwargs = _version_payload_from_funnel(funnel_data)
    terms_text = version_kwargs['terms_text']
    through_seymy = bool(funnel_data.get('payment_request_through_seymy'))
    payment_status = PaymentStatus.REQUESTED if through_seymy else PaymentStatus.NONE

    if estimate is None:
        estimate = Estimate.objects.create(
            estimate_number=str(funnel_data.get('estimate_number') or ''),
            business_name=str(funnel_data.get('business_name') or ''),
            customer_name=str(funnel_data.get('customer_name') or ''),
            status=EstimateStatus.SENT,
            acceptance_requirement=acceptance_requirement,
            payment_status=payment_status,
            sent_at=timezone.now(),
        )
        version = EstimateVersion.objects.create(
            estimate=estimate,
            version_number=1,
            **version_kwargs,
        )
        estimate.current_version = version
        estimate.save(update_fields=['current_version', 'updated_at'])
        schedule_deposit_reminders(estimate)
        return estimate

    current = estimate.current_version
    needs_new_version = (
        current is None
        or current.is_accepted
        or material_differs(current.snapshot, funnel_data)
        or current.terms_text != terms_text
    )

    estimate.estimate_number = str(funnel_data.get('estimate_number') or estimate.estimate_number)
    estimate.business_name = str(funnel_data.get('business_name') or estimate.business_name)
    estimate.customer_name = str(funnel_data.get('customer_name') or estimate.customer_name)
    estimate.acceptance_requirement = acceptance_requirement
    estimate.sent_at = estimate.sent_at or timezone.now()
    # Do not overwrite paid status on re-send of same accepted flow
    if estimate.payment_status != PaymentStatus.PAID:
        estimate.payment_status = payment_status

    if needs_new_version:
        next_number = (current.version_number + 1) if current else 1
        version = EstimateVersion.objects.create(
            estimate=estimate,
            version_number=next_number,
            **version_kwargs,
        )
        estimate.current_version = version
        # If previously accepted, a material revision requires new approval
        if estimate.accepted_version_id and estimate.status in (
            EstimateStatus.ACCEPTED,
            EstimateStatus.DEPOSIT_PAID,
        ):
            estimate.status = EstimateStatus.SENT
        else:
            estimate.status = EstimateStatus.SENT
    else:
        # Same material content — keep version, just mark sent if needed
        if estimate.status == EstimateStatus.DRAFT:
            estimate.status = EstimateStatus.SENT
            estimate.sent_at = timezone.now()
        else:
            estimate.status = EstimateStatus.SENT

    estimate.save()
    schedule_deposit_reminders(estimate)
    return estimate


@transaction.atomic
def record_deposit_acceptance(
    *,
    payment_intent_id: str,
    estimate: Estimate | None = None,
    estimate_version: EstimateVersion | None = None,
    accepted_amount: Decimal | None = None,
) -> tuple[EstimateAcceptance, bool]:
    """
    Record acceptance from a verified Stripe payment.

    Idempotent on payment_intent_id. Returns (acceptance, created).
    Does not invent acceptance on failed payments — caller only invokes
    after verified success.
    """
    payment_intent_id = (payment_intent_id or '').strip()
    if not payment_intent_id:
        raise ValueError('payment_intent_id is required')

    existing = EstimateAcceptance.objects.filter(payment_intent_id=payment_intent_id).select_related(
        'estimate', 'estimate_version'
    ).first()
    if existing:
        return existing, False

    if estimate_version is None:
        checkout = (
            PendingCheckout.objects.select_related('estimate', 'estimate_version')
            .filter(payment_intent_id=payment_intent_id)
            .first()
        )
        if checkout is None:
            raise ValueError(f'No pending checkout for payment_intent_id={payment_intent_id}')
        estimate = checkout.estimate
        estimate_version = checkout.estimate_version
        if accepted_amount is None:
            accepted_amount = checkout.amount
    else:
        estimate = estimate or estimate_version.estimate

    if estimate_version.is_accepted and not EstimateAcceptance.objects.filter(
        estimate_version=estimate_version
    ).exists():
        # Defensive: version flagged accepted without record — still create if PI new
        pass

    # Do not accept a superseded version if a newer current version exists and
    # this PI was for an older unpaid version — still allow if checkout matched.
    amount = _money(accepted_amount if accepted_amount is not None else estimate_version.deposit_amount)

    acceptance = EstimateAcceptance.objects.create(
        estimate=estimate,
        estimate_version=estimate_version,
        estimate_version_number=estimate_version.version_number,
        document_version=estimate_version.document_version,
        terms_version=estimate_version.terms_version,
        terms_text_snapshot=estimate_version.terms_text,
        accepted_at=timezone.now(),
        accepted_amount=amount,
        payment_intent_id=payment_intent_id,
        acceptance_method=AcceptanceMethod.DEPOSIT_PAYMENT,
        acceptance_requirement_at_time=estimate.acceptance_requirement,
    )

    # Freeze version — save() protects snapshot thereafter
    if not estimate_version.is_accepted:
        estimate_version.is_accepted = True
        estimate_version.save(update_fields=['is_accepted'])

    estimate.accepted_version = estimate_version
    estimate.accepted_at = acceptance.accepted_at
    estimate.deposit_paid_at = acceptance.accepted_at
    estimate.status = EstimateStatus.DEPOSIT_PAID
    estimate.payment_status = PaymentStatus.PAID
    estimate.save(
        update_fields=[
            'accepted_version',
            'accepted_at',
            'deposit_paid_at',
            'status',
            'payment_status',
            'updated_at',
        ]
    )
    # Stop any pending Day +2 / Day +5 deposit reminders immediately on pay.
    cancel_deposit_reminders(estimate, reason='paid')

    PendingCheckout.objects.filter(payment_intent_id=payment_intent_id).update(status='succeeded')

    return acceptance, True


def try_overwrite_accepted_version(version: EstimateVersion, new_snapshot: dict) -> bool:
    """
    Attempt to mutate an accepted version's snapshot.

    Returns True if the snapshot changed; False if protection held.
    Used by tests to assert freeze behavior.
    """
    if not version.is_accepted:
        version.snapshot = new_snapshot
        version.save(update_fields=['snapshot'])
        version.refresh_from_db()
        return version.snapshot == new_snapshot

    original = deepcopy(version.snapshot)
    version.snapshot = new_snapshot
    version.save()
    version.refresh_from_db()
    return version.snapshot != original
