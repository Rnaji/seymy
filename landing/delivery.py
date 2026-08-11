"""
Customer + contractor delivery helpers for estimate send and deposit reminders.

Customer SMS is still session/demo for MVP. Contractor PDF copy uses Django's
email backend (console in local/dev) when business_email is present.

Deposit reminders reuse the same demo outbound log pattern — messages are
recorded (OutboundCustomerMessage) rather than sent through a live SMS provider.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import (
    Estimate,
    EstimateStatus,
    OutboundCustomerMessage,
    PaymentRequestType,
    PaymentStatus,
)


def customer_delivery_channel(funnel_data: dict) -> str:
    """Return 'text' or 'email' for DONE-screen copy."""
    method = (funnel_data.get('send_method') or '').lower().strip()
    phone = (funnel_data.get('customer_phone') or '').strip()
    email = (funnel_data.get('customer_email') or '').strip()
    if method == 'email' and email:
        return 'email'
    if method in ('sms', 'text') and phone:
        return 'text'
    if phone and (not email or method in ('sms', 'text', '')):
        return 'text'
    if email:
        return 'email'
    return 'text'


def record_customer_delivery(funnel_data: dict) -> dict:
    """
    Record that the customer was notified via the selected channel.

    Real SMS is not wired yet; this persists intent + channel for the session
    and DONE screen (same pattern as the existing demo send destination).
    """
    channel = customer_delivery_channel(funnel_data)
    return {
        'customer_delivery_sent': True,
        'customer_delivery_channel': channel,
        'customer_delivery_detail': (
            (funnel_data.get('customer_email') or '').strip()
            if channel == 'email'
            else (funnel_data.get('customer_phone') or '').strip()
        ),
    }


def send_contractor_estimate_copy(funnel_data: dict, *, estimate: Estimate) -> bool:
    """
    Email a plain-text copy of the estimate to the contractor's business_email.

    Uses Django's configured EMAIL_BACKEND (console by default). Returns True
    when a message was handed to the backend.
    """
    to_email = (funnel_data.get('business_email') or '').strip()
    if not to_email:
        return False

    number = estimate.estimate_number or str(estimate.pk)
    customer = estimate.customer_name or 'your customer'
    business = estimate.business_name or funnel_data.get('business_name') or 'Seymy'
    version = estimate.current_version
    total = version.total_amount if version else funnel_data.get('total') or funnel_data.get('estimate_total') or '0'
    deposit = version.deposit_amount if version else funnel_data.get('deposit_amount') or '0'
    token = estimate.public_token

    subject = f'Copy: Estimate #{number} for {customer} — {business}'
    lines = [
        f'This is your copy of estimate #{number}.',
        '',
        f'Customer: {customer}',
        f'Total: ${total}',
    ]
    if version and version.deposit_enabled:
        lines.append(f'Deposit: ${deposit}')
    if token:
        lines.append('')
        lines.append(f'Customer document token: {token}')
    lines.extend(
        [
            '',
            'Ask Seymy anytime to look up this estimate or check payment status.',
            '',
            '— Seymy',
        ]
    )
    body = '\n'.join(lines)
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@seymy.local')
    sent = send_mail(
        subject,
        body,
        from_email,
        [to_email],
        fail_silently=True,
    )
    return bool(sent)


# ---------------------------------------------------------------------------
# Deposit reminders (Seymy-collected payments only)
# ---------------------------------------------------------------------------

REMINDER_DAY2_OFFSET = timedelta(days=2)
REMINDER_DAY5_OFFSET = timedelta(days=5)


def _money(value) -> str:
    amount = Decimal(str(value or '0')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    formatted = f'{amount:,.2f}'
    if formatted.endswith('.00'):
        return formatted[:-3]
    return formatted


def customer_pay_link(estimate: Estimate) -> str:
    """Absolute-ish pay URL for SMS copy (demo-friendly when SITE_URL unset)."""
    path = f'/e/{estimate.public_token}/pay/'
    base = (getattr(settings, 'SEYMY_PUBLIC_BASE_URL', '') or '').rstrip('/')
    if base:
        return f'{base}{path}'
    return path


def _customer_contact(estimate: Estimate) -> tuple[str, str]:
    """Return (channel, to_detail) from the frozen snapshot when available."""
    version = estimate.current_version
    snapshot = (version.snapshot if version else {}) or {}
    phone = (snapshot.get('customer_phone') or '').strip()
    email = (snapshot.get('customer_email') or '').strip()
    if phone:
        return 'sms', phone
    if email:
        return 'email', email
    return 'sms', ''


def _requested_amount(estimate: Estimate) -> Decimal:
    version = estimate.current_version
    if not version:
        return Decimal('0.00')
    if version.requested_payment_amount:
        return version.requested_payment_amount
    if version.payment_request_type == PaymentRequestType.FULL:
        return version.total_amount
    return version.deposit_amount


def _payment_noun(estimate: Estimate) -> str:
    version = estimate.current_version
    if version and version.payment_request_type == PaymentRequestType.FULL:
        return 'payment'
    return 'deposit'


def build_deposit_reminder_body(estimate: Estimate, *, kind: str) -> str:
    """
    Build day+2 / day+5 / manual reminder SMS body.

    kind: 'day2' | 'day5' | 'manual'
    """
    first = (estimate.customer_name or 'there').strip().split()[0] or 'there'
    business = (estimate.business_name or 'your contractor').strip() or 'your contractor'
    number = estimate.estimate_number or str(estimate.pk)
    amount = _money(_requested_amount(estimate))
    noun = _payment_noun(estimate)
    link = customer_pay_link(estimate)
    cta = (
        f'View estimate & pay deposit:\n{link}'
        if noun == 'deposit'
        else f'View estimate & pay:\n{link}'
    )

    if kind == 'day5':
        if noun == 'deposit':
            return (
                f'Hi {first}, your ${amount} deposit for estimate #{number} is still pending.\n\n'
                f'The deposit is required to approve the estimate and schedule the work.\n\n'
                f'{cta}\n\n'
                f'— {business}'
            )
        return (
            f'Hi {first}, your ${amount} payment for estimate #{number} is still pending.\n\n'
            f'Payment is required to approve the estimate and schedule the work.\n\n'
            f'{cta}\n\n'
            f'— {business}'
        )

    # day2 + manual share the friendly reminder tone
    if noun == 'deposit':
        return (
            f'Hi {first}, just a reminder that your ${amount} deposit for estimate #{number}\n'
            f'is still pending.\n\n'
            f'{cta}\n\n'
            f'— {business}'
        )
    return (
        f'Hi {first}, just a reminder that your ${amount} payment for estimate #{number}\n'
        f'is still pending.\n\n'
        f'{cta}\n\n'
        f'— {business}'
    )


def estimate_expiration_date(estimate: Estimate):
    """Parse expiration_date from the current version snapshot, if present."""
    version = estimate.current_version
    if not version:
        return None
    raw = ((version.snapshot or {}).get('expiration_date') or '').strip()
    if not raw:
        return None
    text = str(raw)[:10]
    try:
        return datetime.strptime(text, '%Y-%m-%d').date()
    except ValueError:
        return None


def is_estimate_expired(estimate: Estimate, *, today=None) -> bool:
    expires = estimate_expiration_date(estimate)
    if not expires:
        return False
    today = today or timezone.localdate()
    return today > expires


def reminders_should_stop(estimate: Estimate, *, today=None) -> bool:
    """True when automatic (and further) reminders must not run."""
    if estimate.reminders_cancelled:
        return True
    if estimate.status in (
        EstimateStatus.CANCELLED,
        EstimateStatus.DEPOSIT_PAID,
        EstimateStatus.ACCEPTED,
        EstimateStatus.DRAFT,
        EstimateStatus.SUPERSEDED,
    ):
        return True
    if estimate.payment_status == PaymentStatus.PAID or estimate.deposit_paid_at:
        return True
    if is_estimate_expired(estimate, today=today):
        return True
    version = estimate.current_version
    if not version or not version.payment_request_through_seymy:
        return True
    if estimate.payment_status != PaymentStatus.REQUESTED:
        return True
    return False


def schedule_deposit_reminders(estimate: Estimate, *, sent_at=None) -> Estimate:
    """
    Schedule day+2 and day+5 reminders when payment is requested through Seymy.

    Clears any prior schedule for estimate-only / already-paid cases.
    """
    sent_at = sent_at or estimate.sent_at or timezone.now()
    version = estimate.current_version
    through = bool(version and version.payment_request_through_seymy)

    if not through or estimate.payment_status == PaymentStatus.PAID:
        estimate.reminder_day2_at = None
        estimate.reminder_day5_at = None
        estimate.save(
            update_fields=['reminder_day2_at', 'reminder_day5_at', 'updated_at']
        )
        return estimate

    estimate.reminders_cancelled = False
    estimate.reminder_day2_at = sent_at + REMINDER_DAY2_OFFSET
    estimate.reminder_day5_at = sent_at + REMINDER_DAY5_OFFSET
    estimate.save(
        update_fields=[
            'reminders_cancelled',
            'reminder_day2_at',
            'reminder_day5_at',
            'updated_at',
        ]
    )
    return estimate


def cancel_deposit_reminders(estimate: Estimate, *, reason: str = '') -> Estimate:
    """Cancel all future automatic deposit reminders for this estimate."""
    estimate.reminders_cancelled = True
    estimate.reminder_day2_at = None
    estimate.reminder_day5_at = None
    estimate.save(
        update_fields=[
            'reminders_cancelled',
            'reminder_day2_at',
            'reminder_day5_at',
            'updated_at',
        ]
    )
    return estimate


def cancel_estimate(estimate: Estimate, *, reason: str = '') -> Estimate:
    """Mark an estimate cancelled and stop any scheduled deposit reminders."""
    estimate.status = EstimateStatus.CANCELLED
    estimate.save(update_fields=['status', 'updated_at'])
    return cancel_deposit_reminders(estimate, reason=reason or 'cancelled')


def _purpose_for_kind(kind: str) -> str:
    if kind == 'day2':
        return OutboundCustomerMessage.PURPOSE_REMINDER_DAY2
    if kind == 'day5':
        return OutboundCustomerMessage.PURPOSE_REMINDER_DAY5
    return OutboundCustomerMessage.PURPOSE_REMINDER_MANUAL


def record_deposit_reminder(
    estimate: Estimate,
    *,
    kind: str,
    body: str | None = None,
) -> OutboundCustomerMessage:
    """Persist a demo outbound reminder (no live SMS provider)."""
    channel, to_detail = _customer_contact(estimate)
    message_body = body or build_deposit_reminder_body(estimate, kind=kind)
    msg = OutboundCustomerMessage.objects.create(
        estimate=estimate,
        purpose=_purpose_for_kind(kind),
        channel=channel if channel == 'email' else 'sms',
        to_detail=to_detail,
        body=message_body,
    )
    estimate.last_reminder_sent_at = timezone.now()
    estimate.reminder_count = (estimate.reminder_count or 0) + 1
    estimate.save(update_fields=['last_reminder_sent_at', 'reminder_count', 'updated_at'])
    return msg


def send_manual_deposit_reminder(estimate: Estimate) -> dict[str, Any]:
    """
    One-off contractor-requested reminder (through-Seymy unpaid only).

    Does not change the automatic day+2 / day+5 schedule unless already cancelled.
    """
    if reminders_should_stop(estimate):
        return {'ok': False, 'reason': 'not_eligible'}
    msg = record_deposit_reminder(estimate, kind='manual')
    return {'ok': True, 'message_id': msg.pk, 'body': msg.body}


def process_deposit_reminders(*, now=None) -> dict[str, Any]:
    """
    Send due automatic day+2 / day+5 reminders.

    Skips paid / cancelled / expired / contractor-cancelled estimates.
    After day+5 is sent (or its slot cleared), no further automatic reminders.
    """
    now = now or timezone.now()
    today = timezone.localdate() if timezone.is_aware(now) else now.date()

    # Expire through-Seymy unpaid estimates past snapshot expiration_date
    for estimate in (
        Estimate.objects.filter(payment_status=PaymentStatus.REQUESTED)
        .exclude(status=EstimateStatus.CANCELLED)
        .select_related('current_version')
        .iterator()
    ):
        if is_estimate_expired(estimate, today=today) and not estimate.reminders_cancelled:
            cancel_deposit_reminders(estimate, reason='expired')

    day2_qs = (
        Estimate.objects.filter(
            reminder_day2_at__isnull=False,
            reminder_day2_at__lte=now,
            reminders_cancelled=False,
            payment_status=PaymentStatus.REQUESTED,
        )
        .exclude(
            status__in=[
                EstimateStatus.CANCELLED,
                EstimateStatus.DEPOSIT_PAID,
                EstimateStatus.ACCEPTED,
                EstimateStatus.DRAFT,
            ]
        )
        .select_related('current_version')
    )
    day5_qs = (
        Estimate.objects.filter(
            reminder_day5_at__isnull=False,
            reminder_day5_at__lte=now,
            reminders_cancelled=False,
            payment_status=PaymentStatus.REQUESTED,
        )
        .exclude(
            status__in=[
                EstimateStatus.CANCELLED,
                EstimateStatus.DEPOSIT_PAID,
                EstimateStatus.ACCEPTED,
                EstimateStatus.DRAFT,
            ]
        )
        .select_related('current_version')
    )

    sent_day2 = 0
    sent_day5 = 0
    skipped = 0

    for estimate in day2_qs:
        if reminders_should_stop(estimate, today=today):
            estimate.reminder_day2_at = None
            estimate.save(update_fields=['reminder_day2_at', 'updated_at'])
            skipped += 1
            continue
        record_deposit_reminder(estimate, kind='day2')
        estimate.reminder_day2_at = None
        estimate.save(update_fields=['reminder_day2_at', 'updated_at'])
        sent_day2 += 1

    for estimate in day5_qs:
        if reminders_should_stop(estimate, today=today):
            estimate.reminder_day5_at = None
            estimate.save(update_fields=['reminder_day5_at', 'updated_at'])
            skipped += 1
            continue
        record_deposit_reminder(estimate, kind='day5')
        # Clear both slots — no automatic reminders after day +5
        estimate.reminder_day2_at = None
        estimate.reminder_day5_at = None
        estimate.save(update_fields=['reminder_day2_at', 'reminder_day5_at', 'updated_at'])
        sent_day5 += 1

    return {
        'sent_day2': sent_day2,
        'sent_day5': sent_day5,
        'skipped': skipped,
        'now': now.isoformat(),
    }
