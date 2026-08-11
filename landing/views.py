from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from .acceptance import prepare_draft_estimate_from_funnel, publish_estimate_from_funnel
from .conversation import (
    answer_retrieval_query,
    apply_updates,
    confirmation_lines,
    has_send_destination,
    missing_requirements,
    next_question,
    parse_message,
)
from .delivery import (
    customer_delivery_channel,
    record_customer_delivery,
    send_contractor_estimate_copy,
)
from .models import Estimate, EstimateAcceptance, EstimateStatus
from .numbering import ensure_estimate_number
from .stripe_payments import (
    construct_webhook_event,
    create_deposit_checkout,
    process_stripe_event,
)
from .tax import recalculate_financials

US_STATES = [
    ('AL', 'Alabama'),
    ('AK', 'Alaska'),
    ('AZ', 'Arizona'),
    ('AR', 'Arkansas'),
    ('CA', 'California'),
    ('CO', 'Colorado'),
    ('CT', 'Connecticut'),
    ('DE', 'Delaware'),
    ('DC', 'District of Columbia'),
    ('FL', 'Florida'),
    ('GA', 'Georgia'),
    ('HI', 'Hawaii'),
    ('ID', 'Idaho'),
    ('IL', 'Illinois'),
    ('IN', 'Indiana'),
    ('IA', 'Iowa'),
    ('KS', 'Kansas'),
    ('KY', 'Kentucky'),
    ('LA', 'Louisiana'),
    ('ME', 'Maine'),
    ('MD', 'Maryland'),
    ('MA', 'Massachusetts'),
    ('MI', 'Michigan'),
    ('MN', 'Minnesota'),
    ('MS', 'Mississippi'),
    ('MO', 'Missouri'),
    ('MT', 'Montana'),
    ('NE', 'Nebraska'),
    ('NV', 'Nevada'),
    ('NH', 'New Hampshire'),
    ('NJ', 'New Jersey'),
    ('NM', 'New Mexico'),
    ('NY', 'New York'),
    ('NC', 'North Carolina'),
    ('ND', 'North Dakota'),
    ('OH', 'Ohio'),
    ('OK', 'Oklahoma'),
    ('OR', 'Oregon'),
    ('PA', 'Pennsylvania'),
    ('RI', 'Rhode Island'),
    ('SC', 'South Carolina'),
    ('SD', 'South Dakota'),
    ('TN', 'Tennessee'),
    ('TX', 'Texas'),
    ('UT', 'Utah'),
    ('VT', 'Vermont'),
    ('VA', 'Virginia'),
    ('WA', 'Washington'),
    ('WV', 'West Virginia'),
    ('WI', 'Wisconsin'),
    ('WY', 'Wyoming'),
]

STATE_CODES = {code for code, _ in US_STATES}
STATE_NAMES = dict(US_STATES)

# Landing-demo / historical fixture constants — used ONLY by
# `_landing_demo_estimate_data` and tests. Never inject into the live funnel.
MOCK_LINE_ITEMS = [
    {'label': '50 gal. water heater', 'amount': Decimal('1350.00')},
    {'label': 'Labor', 'amount': Decimal('500.00')},
]
MOCK_ESTIMATE_TOTAL = Decimal('1850.00')
MOCK_CUSTOMER_NAME = 'John Anderson'
MOCK_ESTIMATE_NUMBER = 'SEY-001'
DEPOSIT_PRESETS = (25, 30, 50)


def _money(value):
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _funnel_data(request):
    return request.session.get('funnel', {})


def _save_funnel(request, **kwargs):
    data = _funnel_data(request)
    data.update(kwargs)
    request.session['funnel'] = data
    request.session.modified = True
    return data


def _business_details_complete(data):
    """Identity/contact fields collected on the business form."""
    required = (
        'business_name',
        'business_address',
        'business_city',
        'state',
        'business_zip',
        'business_phone',
        'business_email',
    )
    return all(str(data.get(key) or '').strip() for key in required)


def _business_profile_complete(data):
    """Persistent contractor identity required for a professional estimate."""
    return _business_details_complete(data)


def _require_job(request):
    data = _funnel_data(request)
    if not data.get('job_description') and not data.get('estimate_total') and not data.get('conversation_history'):
        return None, redirect('start_job')
    return data, None


def _require_estimate(request):
    data, redirect_response = _require_job(request)
    if redirect_response:
        return None, redirect_response
    if not data.get('estimate_total'):
        return None, redirect('start_job')
    return data, None


def _require_draft_ready(request):
    """Estimate draft has job + payment info; may still need business profile."""
    data, redirect_response = _require_estimate(request)
    if redirect_response:
        return None, redirect_response
    if missing_requirements(data):
        return None, redirect('start_job')
    return data, None


def _stage_context(stage):
    stages = ('TALK', 'COMPLETE', 'BUSINESS SETUP', 'REVIEW & SEND', 'DONE')
    return {
        'stages': stages,
        'current_stage': stage,
    }


def _send_recipient_context(data):
    """Discreet recipient + channel line for the Ready-to-send screen."""
    method = (data.get('send_method') or '').lower().strip()
    phone = (data.get('customer_phone') or '').strip()
    email = (data.get('customer_email') or '').strip()

    if method == 'email' and email:
        channel, detail = 'Email', email
    elif method in ('sms', 'text') and phone:
        channel, detail = 'Text message', phone
    elif phone and (not email or method in ('sms', 'text', '')):
        channel, detail = 'Text message', phone
    elif email:
        channel, detail = 'Email', email
    else:
        channel, detail = '', phone or email

    display = f'{channel} · {detail}' if channel and detail else detail or channel
    return {
        'send_recipient_name': (data.get('customer_name') or '').strip(),
        'send_channel_label': channel,
        'send_destination_detail': detail,
        'send_destination_display': display,
        'send_is_email': channel == 'Email',
        'send_is_sms': channel == 'Text message',
    }


def _customer_first_name(data) -> str:
    name = (data.get('customer_name') or '').strip()
    if not name:
        return 'there'
    return name.split()[0]


def _payment_send_mode(data) -> str:
    """
    Ready-to-send / send branching (online Seymy payment request):
    - deposit: payment_request_through_seymy + deposit terms
    - full_payment: payment_request_through_seymy + full payment
    - none: estimate only (PDF may still show deposit TERMS)
    """
    from .acceptance import resolve_payment_request

    resolved = resolve_payment_request(data)
    if not resolved.get('payment_request_through_seymy'):
        return 'none'
    prt = resolved.get('payment_request_type') or 'none'
    if prt == 'deposit':
        return 'deposit'
    if prt == 'full':
        return 'full_payment'
    return 'none'


def _has_immediate_payment_terms(data) -> bool:
    """True when deposit or full payment terms exist (payment choice may apply)."""
    deposit_enabled = bool(data.get('deposit_enabled'))
    request_full = bool(data.get('request_full_payment')) and not deposit_enabled
    return deposit_enabled or request_full


def _parse_through_seymy_post(raw) -> bool:
    return str(raw or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _customer_secure_link(request, data, *, mode: str) -> str:
    token = (data.get('public_token') or '').strip()
    if token:
        if mode in ('deposit', 'full_payment'):
            path = reverse('customer_pay', kwargs={'token': token})
        else:
            path = reverse('customer_estimate_document', kwargs={'token': token})
        return request.build_absolute_uri(path)
    # Placeholder until a draft token is prepared (should be rare).
    if mode in ('deposit', 'full_payment'):
        return request.build_absolute_uri('/e/<token>/pay/')
    return request.build_absolute_uri('/e/<token>/document/')


def _customer_message_parts(request, data, *, mode: str):
    """Exact customer message body / explanation / CTA for a payment_send_mode."""
    business = (data.get('business_name') or 'Your contractor').strip()
    first = _customer_first_name(data)
    number = str(data.get('estimate_number') or '').strip() or '—'
    total = _format_money(data.get('total') or data.get('estimate_total') or '0')
    deposit = _format_money(data.get('deposit_amount') or '0')
    link = _customer_secure_link(request, data, mode=mode)
    has_terms = _has_immediate_payment_terms(data)

    if mode == 'deposit':
        body = (
            f'Hi {first}, {business} sent you estimate #{number} for ${total}. '
            f'A ${deposit} deposit is required to approve this estimate and schedule the work. '
            f'View estimate & pay deposit: {link} — {business}'
        )
        explanation = f'The estimate and a ${deposit} online deposit request will be sent.'
        cta_label = f'Send Estimate & Request ${deposit} Deposit'
        email_subject = f'Estimate #{number} from {business} — ${deposit} deposit required'
    elif mode == 'full_payment':
        body = (
            f'Hi {first}, {business} sent you estimate #{number} for ${total}. '
            f'Review your estimate and complete payment securely below. '
            f'View estimate & pay: {link} — {business}'
        )
        explanation = f'The estimate and a ${total} online payment request will be sent.'
        cta_label = f'Send Estimate & Request ${total} Payment'
        email_subject = f'Estimate #{number} from {business} — payment requested'
    else:
        body = (
            f'Hi {first}, {business} sent you estimate #{number} for ${total}. '
            f'Review your estimate below: {link} — {business}'
        )
        if has_terms:
            explanation = 'The estimate will be sent. No online payment will be requested.'
            cta_label = 'Send Estimate Only'
        else:
            explanation = 'The estimate will be sent. No payment is requested now.'
            cta_label = 'Send Estimate'
        email_subject = f'Estimate #{number} from {business}'

    return {
        'mode': mode,
        'customer_message_body': body,
        'customer_message_explanation': explanation,
        'send_cta_label': cta_label,
        'customer_secure_link': link,
        'customer_email_subject': email_subject,
        'preview_deposit_amount': deposit,
        'preview_estimate_total': total,
        'customer_first_name': first,
    }


def _customer_message_preview_context(request, data):
    """Build the exact customer SMS/email preview + CTA for Ready-to-send."""
    data = recalculate_financials(data)
    mode = _payment_send_mode(data)
    parts = _customer_message_parts(request, data, mode=mode)
    recipient = _send_recipient_context(data)
    deposit_enabled = bool(data.get('deposit_enabled'))
    request_full = bool(data.get('request_full_payment')) and not deposit_enabled
    show_payment_choice = deposit_enabled or request_full
    through_seymy = bool(data.get('payment_request_through_seymy')) if show_payment_choice else False
    request_amount = (
        parts['preview_deposit_amount'] if deposit_enabled else parts['preview_estimate_total']
    )

    # Both variants for client-side toggle (no full-page confusion).
    seymy_mode = 'deposit' if deposit_enabled else ('full_payment' if request_full else 'none')
    choice_previews = {}
    if show_payment_choice:
        seymy_parts = _customer_message_parts(request, data, mode=seymy_mode)
        only_parts = _customer_message_parts(request, data, mode='none')
        choice_previews = {
            'true': {
                'body': seymy_parts['customer_message_body'],
                'explanation': seymy_parts['customer_message_explanation'],
                'cta': seymy_parts['send_cta_label'],
                'link': seymy_parts['customer_secure_link'],
                'subject': seymy_parts['customer_email_subject'],
            },
            'false': {
                'body': only_parts['customer_message_body'],
                'explanation': only_parts['customer_message_explanation'],
                'cta': only_parts['send_cta_label'],
                'link': only_parts['customer_secure_link'],
                'subject': only_parts['customer_email_subject'],
            },
        }

    if deposit_enabled:
        choice_heading = 'HOW DO YOU WANT TO HANDLE THE DEPOSIT?'
        seymy_option_title = f'Request ${request_amount} through Seymy'
        estimate_only_detail = "You'll collect the deposit outside Seymy."
    elif request_full:
        choice_heading = 'HOW DO YOU WANT TO HANDLE THE PAYMENT?'
        seymy_option_title = f'Request ${request_amount} through Seymy'
        estimate_only_detail = "You'll handle payment outside Seymy."
    else:
        choice_heading = ''
        seymy_option_title = ''
        estimate_only_detail = ''

    return {
        **recipient,
        'payment_send_mode': mode,
        'payment_request_through_seymy': through_seymy,
        'payment_request_type': data.get('payment_request_type') or 'none',
        'requested_payment_amount': data.get('requested_payment_amount') or '0.00',
        'customer_message_body': parts['customer_message_body'],
        'customer_message_explanation': parts['customer_message_explanation'],
        'send_cta_label': parts['send_cta_label'],
        'customer_secure_link': parts['customer_secure_link'],
        'customer_email_subject': parts['customer_email_subject'],
        'customer_first_name': parts['customer_first_name'],
        'preview_deposit_amount': parts['preview_deposit_amount'],
        'preview_estimate_total': parts['preview_estimate_total'],
        'show_payment_choice': show_payment_choice,
        'payment_choice_heading': choice_heading,
        'payment_choice_amount': request_amount,
        'payment_choice_seymy_title': seymy_option_title,
        'payment_choice_estimate_only_detail': estimate_only_detail,
        'payment_choice_is_deposit': deposit_enabled,
        'payment_choice_previews': choice_previews,
    }


def _redirect_after_destination_or_ready(request, data):
    """After review/business: ask where to send, or show Ready to send."""
    if not has_send_destination(data):
        _save_funnel(request, conversation_mode='send', pending_question='send_destination')
        return redirect(reverse('start_job') + '?mode=send')
    return redirect('start_review_send')


def _default_validity_date():
    return (date.today() + timedelta(days=30)).isoformat()


# Contractor identity persists across estimates; job draft does not.
_BUSINESS_PROFILE_KEYS = (
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


def _reset_estimate_draft(request):
    """
    Start a brand-new estimate draft.

    Clears job/conversation/payment draft state.
    Preserves completed business profile for returning users.
    Does NOT evaluate missing fields — that only happens after first input.
    """
    data = _funnel_data(request)
    preserved = {key: data[key] for key in _BUSINESS_PROFILE_KEYS if data.get(key) not in (None, '')}
    request.session['funnel'] = {
        **preserved,
        'has_user_provided_job_input': False,
        'conversation_history': [],
        'conversation_mode': '',
        'pending_question': '',
        'payment_approved': False,
        'deposit_paid': False,
    }
    request.session.modified = True
    return request.session['funnel']


@require_http_methods(['GET'])
def start_new_estimate(request):
    """Landing CTA entry: always a fresh draft on TALK."""
    _reset_estimate_draft(request)
    return redirect('start_job')


def _ensure_mock_estimate(request, data):
    """
    Legacy helper — must NOT inject landing-demo customers/prices into the
    real funnel. Returns data unchanged.
    """
    return data


def _summary_context(data):
    data = recalculate_financials(data)
    subtotal = _money(data.get('subtotal') or '0')
    total = _money(data.get('estimate_total') or subtotal)
    line_items = data.get('line_items') or []
    deposit_enabled = data.get('deposit_enabled')
    deposit_amount = _money(data.get('deposit_amount') or '0')
    balance_amount = _money(data.get('balance_amount') or total)

    deposit_label = 'No deposit'
    if deposit_enabled:
        if data.get('deposit_type') == 'percentage':
            deposit_label = f"Deposit ({data.get('deposit_value')}%)"
        else:
            deposit_label = 'Deposit'

    fee_label = 'Not set'
    if data.get('card_fee_preference_set'):
        fee_label = (
            'Pass eligible fees to customer'
            if data.get('pass_card_fees_to_customer')
            else 'Covered by you'
        )

    job_site_lines = []
    if data.get('job_site_address'):
        job_site_lines.append(data['job_site_address'])
    city_state = ', '.join(
        part for part in [data.get('job_site_city'), data.get('job_site_state')] if part
    )
    if city_state:
        if data.get('job_site_zip'):
            city_state = f"{city_state} {data['job_site_zip']}"
        job_site_lines.append(city_state)

    tax_enabled = bool(data.get('tax_enabled')) and data.get('tax_configured')
    tax_amount = data.get('tax_amount')
    job_start_display = _format_date(data.get('job_start_date') or '')
    completion_display = _format_date(data.get('expected_completion_date') or '')
    show_schedule = bool(job_start_display or completion_display)

    return {
        'business_name': data.get('business_name', ''),
        'customer_name': data.get('customer_name', ''),
        'job_site_lines': job_site_lines,
        'line_items': [
            {
                'label': item.get('label', ''),
                'amount': _format_money(item.get('amount', 0)),
            }
            for item in line_items
        ],
        'subtotal': _format_money(subtotal),
        'estimate_total': _format_money(total),
        'tax_configured': tax_enabled,
        'tax_amount': _format_money(tax_amount) if tax_enabled and tax_amount is not None else '',
        'tax_label': data.get('tax_label') or 'Sales tax',
        'deposit_enabled': bool(deposit_enabled),
        'deposit_label': deposit_label,
        'deposit_amount': _format_money(deposit_amount),
        'balance_amount': _format_money(balance_amount),
        'balance_due_date_display': _format_date(data.get('balance_due_date', '')),
        'show_schedule': show_schedule,
        'job_start_date_display': job_start_display,
        'expected_completion_date_display': completion_display,
        'fee_label': fee_label,
        'pass_card_fees_to_customer': bool(data.get('pass_card_fees_to_customer')),
        'seymy_note': (confirmation_lines(data) or [''])[0],
    }


def _format_money(value):
    amount = _money(value)
    formatted = f'{amount:,.2f}'
    if formatted.endswith('.00'):
        return formatted[:-3]
    return formatted


def _format_money_fixed(value):
    """Always two decimal places — for customer-facing estimate documents."""
    return f'{_money(value):,.2f}'


def _format_date(value):
    if not value:
        return ''
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError:
            return value
    return f'{value.strftime("%b")} {value.day}, {value.year}'


def _parse_iso_date(value):
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError:
            return None
    return None


def _format_schedule_range(start_value, end_value):
    """Compact range for ready-to-send summary, e.g. Aug 20 → Aug 24, 2026."""
    start = _parse_iso_date(start_value)
    end = _parse_iso_date(end_value)
    if start and end:
        if start.year == end.year:
            return (
                f'{start.strftime("%b")} {start.day} → '
                f'{end.strftime("%b")} {end.day}, {end.year}'
            )
        return f'{_format_date(start)} → {_format_date(end)}'
    if start:
        return _format_date(start)
    if end:
        return _format_date(end)
    return ''


def _estimate_context(data):
    """Contractor funnel context — never invent demo customer/pricing."""
    raw_total = data.get('estimate_total')
    total = _money(raw_total) if raw_total not in (None, '') else _money('0')
    line_items = data.get('line_items') or []
    return {
        'business_name': data.get('business_name', ''),
        'state_name': STATE_NAMES.get(data.get('state', ''), data.get('state', '')),
        'customer_name': data.get('customer_name', ''),
        'estimate_number': data.get('estimate_number', ''),
        'job_description': data.get('job_description', ''),
        'line_items': [
            {
                'label': item['label'],
                'amount': _format_money(item['amount']),
            }
            for item in line_items
        ],
        'estimate_total': _format_money(total),
        'estimate_total_raw': str(total),
    }


def _format_tax_rate_display(rate) -> str:
    """Display configured tax rate exactly (e.g. 6.0% or 7.25%). Never invent."""
    try:
        value = Decimal(str(rate))
    except Exception:
        return f'{rate}%'
    if value == value.to_integral_value():
        return f'{value:.1f}%'
    text = f'{value:f}'.rstrip('0').rstrip('.')
    return f'{text}%'


def _landing_demo_estimate_data():
    """
    Fixed funnel snapshot matching the landing READY card demo numbers.

    Used only for the public demo PDF — not session funnel state.
    """
    return recalculate_financials(
        {
            'business_name': 'Miller Plumbing',
            'business_address': '812 Orange Avenue',
            'business_city': 'Orlando',
            'state': 'FL',
            'business_zip': '32801',
            'business_phone': '(407) 555-0148',
            'business_email': 'estimates@millerplumbing.com',
            'license_number': 'CFC1428579',
            'insured_bonded': True,
            'business_credentials': 'Insured & Bonded',
            'customer_name': 'John Anderson',
            'customer_phone': '(407) 555-0192',
            'customer_email': 'john.anderson@email.com',
            'job_site_address': '24 Main Street',
            'job_site_city': 'Orlando',
            'job_site_state': 'FL',
            'job_site_zip': '',
            'job_description': 'Replace a 50-gallon water heater.',
            'estimate_number': 'SEY-001',
            'issue_date': '2026-08-11',
            'estimate_validity_days': 30,
            'expiration_date': '2026-09-10',
            'line_items': [
                {
                    'label': '50 gal. water heater',
                    'amount': '1350.00',
                    'type': 'material',
                },
                {
                    'label': 'Labor',
                    'amount': '500.00',
                    'type': 'labor',
                },
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
            'terms_and_conditions': (
                'Valid for the period shown above. Work begins after approval and any required deposit. '
                'Changes to scope, materials, or site conditions may require a written change order '
                'and adjusted pricing before additional work proceeds.\n\n'
                'Licensed, insured, and bonded in Florida. Workmanship warranty as stated at completion; '
                'manufacturer warranties apply to equipment installed.'
            ),
        }
    )


def _estimate_pdf_context(data):
    """
    Build a structured estimate payload for the contractor-facing PDF/document.

    IMPORTANT: Do not invent missing address/contact/tax configuration.
    Only show tax when the contractor has configured a rate (or per-estimate override).
    """
    data = recalculate_financials(data)

    raw_line_items = data.get('line_items') or []

    has_qty = any('qty' in item and item.get('qty') not in (None, '') for item in raw_line_items)
    has_rate = any('rate' in item and item.get('rate') not in (None, '') for item in raw_line_items)
    show_qty_rate = has_qty or has_rate

    line_items = []
    for item in raw_line_items:
        label = (item.get('label') or '').strip()
        item_type = (item.get('type') or '').lower()
        detail = (item.get('detail') or '').strip()
        description = label
        if item_type == 'labor' or label.lower() == 'labor':
            description = 'Labor & Professional Installation'
        elif item_type == 'material' and not detail:
            detail = '(Parts & Materials)'
        elif 'water heater' in label.lower() and not detail:
            detail = '(Parts & Materials)'
        line_items.append(
            {
                'description': description,
                'detail': detail,
                'qty': item.get('qty'),
                'rate': _format_money_fixed(item['rate']) if item.get('rate') not in (None, '') else '',
                'amount': _format_money_fixed(item.get('amount', 0)),
            }
        )

    issue_date = data.get('issue_date') or date.today().isoformat()
    issue_date_display = _format_date(issue_date)
    validity_days = int(data.get('estimate_validity_days') or 30)
    expiration_date = data.get('expiration_date') or _default_validity_date()
    expiration_date_display = _format_date(expiration_date)

    subtotal = _money(data.get('subtotal') or '0')
    tax_enabled = bool(data.get('tax_enabled')) and bool(data.get('tax_configured'))
    tax_rate = data.get('tax_rate')
    tax_applies_to = (data.get('tax_applies_to') or 'all').lower()
    tax_rate_display = _format_tax_rate_display(tax_rate) if tax_enabled and tax_rate not in (None, '') else ''
    if tax_enabled and tax_applies_to == 'materials' and tax_rate_display:
        # Keep rate for Sales Tax (X) label variants; templates may use tax_label
        tax_rate_display = tax_rate_display.rstrip('%') + '% materials'
    tax_amount = _money(data['tax_amount']) if tax_enabled and data.get('tax_amount') is not None else None
    computed_total = _money(data.get('total') or data.get('estimate_total') or subtotal)

    deposit_enabled = bool(data.get('deposit_enabled'))
    deposit_type = data.get('deposit_type') or ''
    deposit_value = data.get('deposit_value') or ''
    deposit_amount = _money(data.get('deposit_amount') or '0')
    remaining_balance = _money(data.get('balance_amount') or '0')
    balance_due_date_display = _format_date(data.get('balance_due_date') or '')
    balance_due_label = balance_due_date_display or 'Upon completion'

    job_start_date_display = _format_date(data.get('job_start_date') or '')
    expected_completion_date_display = _format_date(data.get('expected_completion_date') or '')
    # Show schedule section when at least one date exists; null rows use "To be scheduled"
    show_project_schedule = bool(
        data.get('job_start_date')
        or data.get('expected_completion_date')
        or data.get('schedule_mentioned')
    )
    # Prefer showing section only when useful — dates present, or omit entirely if never discussed
    if not job_start_date_display and not expected_completion_date_display:
        show_project_schedule = False

    if deposit_enabled:
        if deposit_type == 'percentage':
            deposit_row_label = f'Required deposit ({deposit_value}%)'
            deposit_requirement_label = f'{deposit_value}% deposit'
        else:
            deposit_row_label = 'Required deposit'
            deposit_requirement_label = 'Deposit'
    else:
        deposit_row_label = 'No deposit required'
        deposit_requirement_label = 'No deposit'

    # Payment-terms prose from structured data (not invented legal language)
    if deposit_enabled:
        if balance_due_date_display and expected_completion_date_display and balance_due_date_display == expected_completion_date_display:
            balance_line = (
                f'The remaining balance (${_format_money_fixed(remaining_balance)}) will be due '
                f'when the job is finished ({balance_due_date_display}).'
            )
        elif balance_due_date_display:
            balance_line = (
                f'The remaining balance (${_format_money_fixed(remaining_balance)}) will be due '
                f'{balance_due_date_display}.'
            )
        else:
            balance_line = (
                f'The remaining balance (${_format_money_fixed(remaining_balance)}) will be due upon completion.'
            )
        payment_terms_lines = [
            (
                f'A {deposit_value}% deposit (${_format_money_fixed(deposit_amount)}) is required to approve '
                f'this estimate and schedule the work.'
                if deposit_type == 'percentage'
                else (
                    f'A deposit of ${_format_money_fixed(deposit_amount)} is required to approve '
                    f'this estimate and schedule the work.'
                )
            ),
            balance_line,
        ]
    else:
        payment_terms_lines = [
            'No deposit is required.',
            'Payment is due upon completion unless otherwise agreed.',
        ]

    business_name = data.get('business_name', '')
    business_address = data.get('business_address') or ''
    business_city = data.get('business_city') or ''
    business_state = STATE_NAMES.get(data.get('state', ''), data.get('state', '')) if data.get('state') else ''
    business_state_code = data.get('state') or ''
    business_zip = data.get('business_zip') or ''
    business_phone = data.get('business_phone') or ''
    business_email = data.get('business_email') or ''
    business_license_number = data.get('license_number') or ''
    business_logo_url = data.get('logo_url') or ''
    business_credentials = (data.get('business_credentials') or '').strip()
    if not business_credentials and data.get('insured_bonded'):
        business_credentials = 'Insured & Bonded'

    business_city_line = ''
    if business_city and (business_state_code or business_state):
        state_bit = business_state_code or business_state
        business_city_line = f'{business_city}, {state_bit}'
        if business_zip:
            business_city_line = f'{business_city_line} {business_zip}'
    elif business_city:
        business_city_line = business_city

    customer_name = data.get('customer_name') or ''
    customer_phone = data.get('customer_phone') or ''
    customer_email = data.get('customer_email') or ''

    job_site_address = data.get('job_site_address') or ''
    job_site_city = data.get('job_site_city') or ''
    job_site_state = data.get('job_site_state') or ''
    job_site_zip = data.get('job_site_zip') or ''
    job_site_city_line = ''
    if job_site_city or job_site_state or job_site_zip:
        job_site_city_line = job_site_city
        if job_site_state:
            job_site_city_line = f'{job_site_city_line}, {job_site_state}' if job_site_city_line else job_site_state
        if job_site_zip:
            job_site_city_line = f'{job_site_city_line} {job_site_zip}'.strip()

    scope_of_work = data.get('job_description') or ''
    pass_card_fees_to_customer = bool(data.get('pass_card_fees_to_customer'))
    # Only contractor-configured legal terms — never invent universal legal copy
    terms_and_conditions = (data.get('terms_and_conditions') or '').strip()

    return {
        'business_name': business_name,
        'business_logo_url': business_logo_url,
        'business_address': business_address,
        'business_city': business_city,
        'business_state': business_state,
        'business_state_code': business_state_code,
        'business_zip': business_zip,
        'business_city_line': business_city_line,
        'business_phone': business_phone,
        'business_email': business_email,
        'business_license_number': business_license_number,
        'business_credentials': business_credentials,
        'customer_name': customer_name,
        'customer_phone': customer_phone,
        'customer_email': customer_email,
        'job_site_address': job_site_address,
        'job_site_city': job_site_city,
        'job_site_state': job_site_state,
        'job_site_zip': job_site_zip,
        'job_site_city_line': job_site_city_line,
        'estimate_number': data.get('estimate_number') or '',
        'issue_date_display': issue_date_display,
        'expiration_date_display': expiration_date_display,
        'validity_days_display': f'{validity_days} Days',
        'line_items': line_items,
        'show_qty_rate': show_qty_rate,
        'subtotal': _format_money_fixed(subtotal),
        'tax_configured': tax_enabled,
        'tax_enabled': tax_enabled,
        'tax_rate_display': tax_rate_display,
        'tax_amount': _format_money_fixed(tax_amount) if tax_amount is not None else '',
        'total': _format_money_fixed(computed_total),
        'deposit_enabled': deposit_enabled,
        'deposit_requirement_label': deposit_requirement_label,
        'deposit_row_label': deposit_row_label,
        'deposit_amount': _format_money_fixed(deposit_amount),
        'remaining_balance': _format_money_fixed(remaining_balance),
        'balance_due_date_display': balance_due_date_display,
        'balance_due_label': balance_due_label,
        'show_project_schedule': show_project_schedule,
        'job_start_date_display': job_start_date_display or 'To be scheduled',
        'expected_completion_date_display': expected_completion_date_display or 'To be scheduled',
        'job_start_date_set': bool(job_start_date_display),
        'expected_completion_date_set': bool(expected_completion_date_display),
        'estimate_schedule_range_display': _format_schedule_range(
            data.get('job_start_date') or '',
            data.get('expected_completion_date') or '',
        ),
        'payment_terms_lines': payment_terms_lines,
        'scope_of_work': scope_of_work,
        'terms_and_conditions': terms_and_conditions,
        'pass_card_fees_to_customer': pass_card_fees_to_customer,
        'payment_terms': {
            'deposit_requirement_label': deposit_requirement_label,
            'balance_due_date_display': balance_due_date_display,
            'pass_card_fees_to_customer': pass_card_fees_to_customer,
        },
    }


def index(request):
    return render(request, 'landing/index.html')


@require_http_methods(['GET', 'POST'])
def start_business(request):
    """
    First-time business identity form — only after the job draft is ready.
    Skipped entirely when the contractor profile is already complete.
    Tax rate is collected conversationally after this form.
    """
    data, redirect_response = _require_draft_ready(request)
    if redirect_response:
        return redirect_response

    if _business_profile_complete(data):
        data = _save_funnel(request, **recalculate_financials(data))
        return _redirect_after_destination_or_ready(request, data)

    errors = {}

    if request.method == 'POST':
        fields = {
            'business_name': request.POST.get('business_name', '').strip(),
            'business_address': request.POST.get('business_address', '').strip(),
            'business_city': request.POST.get('business_city', '').strip(),
            'state': request.POST.get('state', '').strip(),
            'business_zip': request.POST.get('business_zip', '').strip(),
            'business_phone': request.POST.get('business_phone', '').strip(),
            'business_email': request.POST.get('business_email', '').strip(),
            'license_number': request.POST.get('license_number', '').strip(),
        }

        if not fields['business_name']:
            errors['business_name'] = 'Enter your business name.'
        if not fields['business_address']:
            errors['business_address'] = 'Enter your business address.'
        if not fields['business_city']:
            errors['business_city'] = 'Enter your city.'
        if not fields['state']:
            errors['state'] = 'Select your state.'
        elif fields['state'] not in STATE_CODES:
            errors['state'] = 'Select a valid US state.'
        if not fields['business_zip']:
            errors['business_zip'] = 'Enter your ZIP code.'
        if not fields['business_phone']:
            errors['business_phone'] = 'Enter your phone number.'
        if not fields['business_email']:
            errors['business_email'] = 'Enter your email.'

        if not errors:
            validity_days = int(data.get('estimate_validity_days') or 30)
            _save_funnel(
                request,
                **fields,
                business_profile_complete=True,
                estimate_validity_days=validity_days,
                expiration_date=data.get('expiration_date') or _default_validity_date(),
                issue_date=data.get('issue_date') or date.today().isoformat(),
            )
            data = _save_funnel(request, **recalculate_financials(_funnel_data(request)))
            return _redirect_after_destination_or_ready(request, data)

        data = {**data, **fields}

    return render(
        request,
        'landing/start_business.html',
        {
            **_stage_context('BUSINESS SETUP'),
            'states': US_STATES,
            'business_name': data.get('business_name', ''),
            'business_address': data.get('business_address', ''),
            'business_city': data.get('business_city', ''),
            'state': data.get('state', ''),
            'business_zip': data.get('business_zip', ''),
            'business_phone': data.get('business_phone', ''),
            'business_email': data.get('business_email', ''),
            'license_number': data.get('license_number', ''),
            'errors': errors,
        },
    )


@require_http_methods(['GET', 'POST'])
def start_job(request):
    """Talk / Complete — primary conversational interface for the estimate draft."""
    data = _funnel_data(request)
    errors = {}
    seymy_reply = ''
    mode = request.GET.get('mode', '').strip() or data.get('conversation_mode', '')
    for_send = mode == 'send' or data.get('conversation_mode') == 'send'
    ask_query = (request.GET.get('ask') or '').strip()

    def _append_ask_history(user_text: str, reply: str):
        nonlocal data
        history = list(data.get('conversation_history') or [])
        history.append({'role': 'contractor', 'text': user_text})
        history.append({'role': 'seymy', 'text': reply})
        data = _save_funnel(request, conversation_history=history)
        return reply

    # Conversational retrieval (estimate / payment lookup) — before draft mutations.
    if request.method == 'POST':
        message = request.POST.get('message', '').strip() or request.POST.get('job_description', '').strip()
        if message:
            retrieval = answer_retrieval_query(message, funnel=data)
            if retrieval is not None:
                seymy_reply = _append_ask_history(message, retrieval)
                return render(
                    request,
                    'landing/start_job.html',
                    {
                        **_stage_context('TALK'),
                        'business_name': data.get('business_name', ''),
                        'message': '',
                        'errors': {},
                        'question': None,
                        'seymy_reply': seymy_reply,
                        'is_followup': False,
                        'is_talk': True,
                        'is_ask': True,
                        'show_no_tax': False,
                        'example': '',
                    },
                )
    elif ask_query:
        retrieval = answer_retrieval_query(ask_query, funnel=data)
        if retrieval is not None:
            seymy_reply = _append_ask_history(ask_query, retrieval)
            return render(
                request,
                'landing/start_job.html',
                {
                    **_stage_context('TALK'),
                    'business_name': data.get('business_name', ''),
                    'message': '',
                    'errors': {},
                    'question': None,
                    'seymy_reply': seymy_reply,
                    'is_followup': False,
                    'is_talk': True,
                    'is_ask': True,
                    'show_no_tax': False,
                    'example': '',
                },
            )

    if request.method == 'POST':
        message = request.POST.get('message', '').strip() or request.POST.get('job_description', '').strip()
        if not message:
            errors['message'] = 'Tell Seymy what you need.'
        else:
            updates = parse_message(message, existing=data)
            if (
                not updates.get('job_description')
                and not data.get('job_description')
                and not for_send
                and len(message) > 12
            ):
                updates['job_description'] = message

            merged = apply_updates(data, updates)
            merged['has_user_provided_job_input'] = True
            merged = ensure_estimate_number(merged)

            data = _save_funnel(request, **merged)

            replies = confirmation_lines(data)
            if replies:
                history = list(data.get('conversation_history') or [])
                history.append({'role': 'seymy', 'text': replies[0]})
                data = _save_funnel(request, conversation_history=history)
                seymy_reply = replies[0]

            if for_send:
                if has_send_destination(data):
                    # Continue only saves destination — never sends.
                    _save_funnel(request, conversation_mode='', pending_question='')
                    return redirect('start_review_send')
                _save_funnel(request, conversation_mode='send', pending_question='send_destination')
                return redirect(reverse('start_job') + '?mode=send')

            was_edit = mode == 'edit' or data.get('conversation_mode') == 'edit'
            question = next_question(data)
            if question is None:
                _save_funnel(request, conversation_mode='', pending_question='')
                if was_edit and has_send_destination(data) and _business_profile_complete(data):
                    return redirect('start_review_send')
                return redirect('start_summary')

            _save_funnel(request, conversation_mode='followup', pending_question=question['key'])
            return redirect('start_job')

    # GET — TALK until the contractor has provided job input
    question = None
    has_input = bool(data.get('has_user_provided_job_input'))
    prefill_message = ask_query if ask_query else ''

    if mode == 'edit' and has_input:
        question = {
            'key': 'edit',
            'prompt': 'What would you like to change?',
            'hint': 'For example: “Change the deposit to 50%.” or “Change the tax to 6%.”',
            'questions': [],
            'stage': 'COMPLETE',
        }
        _save_funnel(request, conversation_mode='edit')
    elif for_send and has_input:
        question = next_question(data, for_send=True) or {
            'key': 'send_destination',
            'prompt': 'Estimate ready. Where should I send it?',
            'hint': 'For example: “Text it to John at 407-555-0182.”',
            'questions': [],
            'stage': 'REVIEW & SEND',
        }
        _save_funnel(request, conversation_mode='send', pending_question='send_destination')
    elif has_input:
        question = next_question(data)
        if question is None and data.get('estimate_total'):
            return redirect('start_summary')
        if question:
            _save_funnel(request, pending_question=question['key'], conversation_mode='followup')
    else:
        if (
            data.get('estimate_total')
            or data.get('conversation_history')
            or data.get('pending_question')
            or data.get('customer_name')
            or data.get('line_items')
            or data.get('job_description')
        ):
            _reset_estimate_draft(request)
        else:
            _save_funnel(
                request,
                conversation_mode='',
                pending_question='',
                has_user_provided_job_input=False,
            )

    is_followup = bool(question) and has_input
    stage = 'COMPLETE' if is_followup else 'TALK'
    if for_send and has_input:
        stage = 'REVIEW & SEND'

    return render(
        request,
        'landing/start_job.html',
        {
            **_stage_context(stage),
            'business_name': data.get('business_name', ''),
            'message': prefill_message,
            'errors': errors,
            'question': question,
            'seymy_reply': seymy_reply,
            'is_followup': is_followup,
            'is_talk': not is_followup,
            'is_ask': bool(prefill_message),
            'show_no_tax': False,
            'example': (
                'Estimate for John Anderson, 24 Main Street, Orlando, FL. '
                'Replace a 50-gallon water heater. '
                '$1,350 materials, $500 labor. '
                'Apply 6% sales tax to materials. '
                'Start the job on August 20. '
                'I want 30% upfront and the balance when the job is finished on August 24. '
                'Pass eligible card fees to the customer.'
            ),
        },
    )


@require_http_methods(['GET', 'POST'])
def start_summary(request):
    data, redirect_response = _require_estimate(request)
    if redirect_response:
        return redirect_response

    question = next_question(data)
    if question:
        return redirect('start_job')

    if request.method == 'POST':
        action = request.POST.get('action', '').strip()
        if action == 'continue':
            if not _business_profile_complete(data):
                return redirect('start_business')
            data = _save_funnel(request, **recalculate_financials(data))
            return _redirect_after_destination_or_ready(request, data)
        if action == 'change':
            return redirect(reverse('start_job') + '?mode=edit')
        return redirect('start_summary')

    return render(
        request,
        'landing/start_summary.html',
        {
            **_stage_context('COMPLETE'),
            **_summary_context(data),
        },
    )


@require_http_methods(['GET'])
def start_estimate_pdf(request):
    data, redirect_response = _require_estimate(request)
    if redirect_response:
        return redirect_response

    # Prefer returning to REVIEW & SEND when payment terms exist.
    data = _ensure_mock_estimate(request, data)
    return render(
        request,
        'landing/start_estimate_pdf.html',
        {
            **_estimate_pdf_context(data),
            'pdf_back_url': reverse('start_review_send'),
        },
    )


@require_http_methods(['GET'])
def landing_demo_estimate_pdf(request):
    """Public demo estimate document matching the landing READY card."""
    return render(
        request,
        'landing/start_estimate_pdf.html',
        _estimate_pdf_context(_landing_demo_estimate_data()),
    )


@require_http_methods(['GET', 'POST'])
def start_deposit(request):
    data, redirect_response = _require_estimate(request)
    if redirect_response:
        return redirect_response

    if request.method == 'POST':
        choice = request.POST.get('deposit_choice', '').strip()
        if choice == 'yes':
            _save_funnel(request, deposit_enabled=True)
            return redirect('start_deposit_configure')
        if choice == 'no':
            total = _money(data.get('estimate_total') or '0')
            _save_funnel(
                request,
                deposit_enabled=False,
                deposit_type='',
                deposit_value='',
                deposit_amount='0.00',
                balance_amount=str(total),
                balance_due_date='',
                pass_card_fees_to_customer=False,
                card_fee_preference_set=True,
            )
            return redirect('start_summary')

    return render(
        request,
        'landing/start_deposit.html',
        {
            **_stage_context('COMPLETE'),
            **_estimate_context(data),
        },
    )


@require_http_methods(['GET', 'POST'])
def start_deposit_configure(request):
    data, redirect_response = _require_estimate(request)
    if redirect_response:
        return redirect_response

    if data.get('deposit_enabled') is False:
        return redirect('start_payment_review')

    total = _money(data.get('estimate_total') or '0')
    errors = {}
    deposit_type = data.get('deposit_type') or 'percentage'
    deposit_value = data.get('deposit_value') or '30'
    balance_due_date = data.get('balance_due_date') or ''
    pass_card_fees = bool(data.get('pass_card_fees_to_customer'))

    if request.method == 'POST':
        deposit_type = request.POST.get('deposit_type', 'percentage').strip()
        balance_due_date = request.POST.get('balance_due_date', '').strip()
        pass_card_fees = request.POST.get('pass_card_fees') == 'customer'

        if deposit_type == 'percentage':
            custom = request.POST.get('deposit_percentage_custom', '').strip()
            deposit_value = custom or request.POST.get('deposit_percentage', '').strip()
            try:
                percent = Decimal(deposit_value)
                if percent <= 0 or percent >= 100:
                    raise ValueError
                deposit_amount = _money(total * percent / Decimal('100'))
            except Exception:
                errors['deposit_value'] = 'Enter a percentage between 1 and 99.'
                deposit_amount = None
        elif deposit_type == 'fixed':
            deposit_value = request.POST.get('deposit_fixed', '').strip().replace('$', '').replace(',', '')
            try:
                deposit_amount = _money(deposit_value)
                if deposit_amount <= 0 or deposit_amount >= total:
                    raise ValueError
            except Exception:
                errors['deposit_value'] = 'Enter a deposit less than the estimate total.'
                deposit_amount = None
        else:
            errors['deposit_type'] = 'Choose percentage or fixed amount.'
            deposit_amount = None

        if not balance_due_date:
            errors['balance_due_date'] = 'Choose a balance due date.'
        else:
            try:
                due = datetime.strptime(balance_due_date, '%Y-%m-%d').date()
                if due < date.today():
                    errors['balance_due_date'] = 'Choose today or a future date.'
            except ValueError:
                errors['balance_due_date'] = 'Enter a valid date.'

        if not errors and deposit_amount is not None:
            balance_amount = _money(total - deposit_amount)
            _save_funnel(
                request,
                deposit_enabled=True,
                deposit_type=deposit_type,
                deposit_value=str(deposit_value),
                deposit_amount=str(deposit_amount),
                balance_amount=str(balance_amount),
                balance_due_date=balance_due_date,
                pass_card_fees_to_customer=pass_card_fees,
                card_fee_preference_set=True,
            )
            return redirect('start_summary')

    preview_percent = Decimal(deposit_value) if deposit_type == 'percentage' and deposit_value.replace('.', '', 1).isdigit() else Decimal('30')
    try:
        preview_deposit = _money(total * preview_percent / Decimal('100')) if deposit_type == 'percentage' else _money(deposit_value or '555')
        if preview_deposit <= 0 or preview_deposit >= total:
            preview_deposit = _money(total * Decimal('0.30'))
    except Exception:
        preview_deposit = _money(total * Decimal('0.30'))

    return render(
        request,
        'landing/start_deposit_configure.html',
        {
            **_stage_context('COMPLETE'),
            **_estimate_context(data),
            'deposit_type': deposit_type,
            'deposit_value': deposit_value,
            'deposit_presets': DEPOSIT_PRESETS,
            'balance_due_date': balance_due_date,
            'pass_card_fees_to_customer': pass_card_fees,
            'preview_deposit': _format_money(preview_deposit),
            'preview_balance': _format_money(total - preview_deposit),
            'errors': errors,
            'min_due_date': date.today().isoformat(),
        },
    )


@require_http_methods(['GET', 'POST'])
def start_payment_review(request):
    data, redirect_response = _require_estimate(request)
    if redirect_response:
        return redirect_response

    if request.method == 'POST':
        return redirect('start_summary')

    total = _money(data.get('estimate_total') or '0')
    deposit_enabled = bool(data.get('deposit_enabled'))
    deposit_amount = _money(data.get('deposit_amount') or '0')
    balance_amount = _money(data.get('balance_amount') or total)
    deposit_type = data.get('deposit_type') or ''
    deposit_value = data.get('deposit_value') or ''

    deposit_label = ''
    if deposit_enabled:
        if deposit_type == 'percentage':
            deposit_label = f'Deposit ({deposit_value}%)'
        else:
            deposit_label = 'Deposit'

    return render(
        request,
        'landing/start_payment_review.html',
        {
            **_stage_context('COMPLETE'),
            **_estimate_context(data),
            'deposit_enabled': deposit_enabled,
            'deposit_label': deposit_label,
            'deposit_amount': _format_money(deposit_amount),
            'balance_amount': _format_money(balance_amount),
            'balance_due_date': data.get('balance_due_date', ''),
            'balance_due_date_display': _format_date(data.get('balance_due_date', '')),
            'pass_card_fees_to_customer': bool(data.get('pass_card_fees_to_customer')),
        },
    )


@require_http_methods(['GET', 'POST'])
def start_review_send(request):
    """Ready to send — estimate document teaser + exact customer preview. Final CTA is the only send."""
    data, redirect_response = _require_draft_ready(request)
    if redirect_response:
        return redirect_response

    if not _business_profile_complete(data):
        return redirect('start_business')

    # Ensure default validity for document
    if not data.get('expiration_date'):
        data = _save_funnel(
            request,
            expiration_date=_default_validity_date(),
            issue_date=data.get('issue_date') or date.today().isoformat(),
            estimate_validity_days=data.get('estimate_validity_days') or 30,
        )

    if not has_send_destination(data):
        _save_funnel(request, conversation_mode='send', pending_question='send_destination')
        return redirect(reverse('start_job') + '?mode=send')

    if request.method == 'POST':
        action = request.POST.get('action', '').strip() or 'send'
        if action == 'set_payment_choice':
            # No-JS / progressive enhancement: persist choice and re-render.
            if _has_immediate_payment_terms(data):
                through = _parse_through_seymy_post(
                    request.POST.get('payment_request_through_seymy')
                )
                data = _save_funnel(request, payment_request_through_seymy=through)
                data = _save_funnel(request, **recalculate_financials(data))
            return redirect('start_review_send')
        if action != 'send':
            return redirect('start_review_send')
        # Apply on-screen payment choice before publish (hidden field / radios).
        if _has_immediate_payment_terms(data) and 'payment_request_through_seymy' in request.POST:
            through = _parse_through_seymy_post(
                request.POST.get('payment_request_through_seymy')
            )
            data = _save_funnel(request, payment_request_through_seymy=through)
        data = recalculate_financials(_funnel_data(request))
        mode = _payment_send_mode(data)
        # Case C: estimate only — no Stripe PI / no platform fee (created only on pay).
        # Case A/B: publish with payment_request_through_seymy in the frozen snapshot.
        estimate = publish_estimate_from_funnel(data)
        delivery = record_customer_delivery(data)
        contractor_copy_sent = send_contractor_estimate_copy(data, estimate=estimate)
        _save_funnel(
            request,
            payment_approved=True,
            deposit_paid=False,
            persisted_estimate_id=estimate.pk,
            public_token=estimate.public_token,
            estimate_version=estimate.current_version.version_number if estimate.current_version else 1,
            payment_send_mode=mode,
            payment_request_through_seymy=bool(data.get('payment_request_through_seymy')),
            payment_request_type=data.get('payment_request_type') or 'none',
            requested_payment_amount=data.get('requested_payment_amount') or '0.00',
            awaiting_deposit=mode == 'deposit',
            awaiting_payment=mode in ('deposit', 'full_payment'),
            contractor_copy_sent=contractor_copy_sent,
            contractor_copy_intent=True,
            **delivery,
        )
        return redirect('start_sent')

    data = recalculate_financials(data)
    # Create/refresh DRAFT estimate so the preview secure link is the real URL.
    draft = prepare_draft_estimate_from_funnel(data)
    data = _save_funnel(
        request,
        persisted_estimate_id=draft.pk,
        public_token=draft.public_token,
        estimate_number=draft.estimate_number or data.get('estimate_number') or '',
        estimate_version=draft.current_version.version_number if draft.current_version else 1,
    )

    return render(
        request,
        'landing/start_review_send.html',
        {
            **_stage_context('REVIEW & SEND'),
            **_estimate_pdf_context(data),
            **_customer_message_preview_context(request, data),
        },
    )


def _sync_funnel_from_estimate(request, data):
    """Refresh session flags from persisted estimate after Stripe confirmation."""
    estimate_id = data.get('persisted_estimate_id')
    if not estimate_id:
        return data
    estimate = Estimate.objects.filter(pk=estimate_id).select_related('accepted_version').first()
    if not estimate:
        return data
    if estimate.status == EstimateStatus.DEPOSIT_PAID or estimate.accepted_version_id:
        acceptance = (
            EstimateAcceptance.objects.filter(estimate=estimate)
            .order_by('-accepted_at')
            .first()
        )
        first = (estimate.customer_name or 'Customer').split()[0]
        amount = ''
        if acceptance:
            amount = str(acceptance.accepted_amount)
        notification = (
            f'{first} paid the ${amount} deposit on estimate #{estimate.estimate_number or estimate.pk}.'
            if amount
            else f'{first} paid the deposit on estimate #{estimate.estimate_number or estimate.pk}.'
        )
        updates = {
            'deposit_paid': True,
            'estimate_accepted': True,
            'awaiting_deposit': False,
            'awaiting_payment': False,
            'public_token': estimate.public_token,
            'contractor_payment_notification': notification,
            'contractor_payment_notified': True,
        }
        if acceptance:
            updates['accepted_at'] = acceptance.accepted_at.isoformat()
            updates['accepted_amount'] = str(acceptance.accepted_amount)
            updates['acceptance_payment_intent_id'] = acceptance.payment_intent_id
            updates['estimate_version'] = acceptance.estimate_version_number
        return _save_funnel(request, **updates)
    return data


def _format_accepted_at_display(value):
    if not value:
        return ''
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    hour = value.strftime('%I').lstrip('0') or '12'
    return f'{value.strftime("%b")} {value.day}, {value.year} at {hour}:{value.strftime("%M %p")}'


@require_http_methods(['GET', 'POST'])
def start_sent(request):
    data, redirect_response = _require_estimate(request)
    if redirect_response:
        return redirect_response

    if not data.get('payment_approved'):
        return redirect('start_review_send')

    data = _sync_funnel_from_estimate(request, data)
    if data.get('deposit_paid'):
        return redirect('start_deposit_paid')

    deposit_enabled = bool(data.get('deposit_enabled'))
    request_full_payment = bool(data.get('request_full_payment')) and not deposit_enabled
    payment_mode = data.get('payment_send_mode') or _payment_send_mode(data)
    deposit_amount = _format_money(data.get('deposit_amount') or '0')
    estimate_total = _format_money(data.get('total') or data.get('estimate_total') or '0')
    customer_name = (data.get('customer_name') or '').strip() or 'your customer'
    first_name = _customer_first_name(data)
    estimate_number = data.get('estimate_number') or ''
    delivery_channel = data.get('customer_delivery_channel') or customer_delivery_channel(data)
    # Real send_mail path sets contractor_copy_sent; intent alone is not enough for the claim.
    contractor_copy_sent = bool(data.get('contractor_copy_sent'))

    ask_prompts = []
    if payment_mode == 'deposit':
        ask_prompts = [
            f"Show me {customer_name}'s estimate.",
            f'Has {first_name} paid his deposit?',
            'Show me my unpaid estimates.',
        ]
    elif payment_mode == 'full_payment':
        ask_prompts = [
            f"Show me {customer_name}'s estimate.",
            f'Has {first_name} paid?',
            'Show me my unpaid estimates.',
        ]
    else:
        ask_prompts = [
            f"Show me {customer_name}'s estimate.",
            f"Send me {customer_name}'s estimate again.",
            'Estimates sent this week',
        ]

    return render(
        request,
        'landing/start_sent.html',
        {
            **_stage_context('DONE'),
            **_estimate_context(data),
            'customer_name': customer_name,
            'customer_first_name': first_name,
            'estimate_number': estimate_number,
            'deposit_enabled': deposit_enabled,
            'request_full_payment': request_full_payment,
            'payment_send_mode': payment_mode,
            'payment_request_through_seymy': payment_mode in ('deposit', 'full_payment'),
            'deposit_amount': deposit_amount,
            'estimate_total': estimate_total,
            'delivery_channel': delivery_channel,
            'delivery_is_email': delivery_channel == 'email',
            'contractor_copy_sent': contractor_copy_sent,
            'ask_prompts': ask_prompts,
        },
    )


@require_http_methods(['GET', 'POST'])
def start_deposit_paid(request):
    data, redirect_response = _require_estimate(request)
    if redirect_response:
        return redirect_response

    data = _sync_funnel_from_estimate(request, data)

    if not data.get('deposit_paid'):
        return redirect('start_sent')

    if request.method == 'POST':
        return redirect('index')

    accepted_at_display = _format_accepted_at_display(data.get('accepted_at'))
    accepted_amount = data.get('accepted_amount') or data.get('deposit_amount') or '0'

    return render(
        request,
        'landing/start_deposit_paid.html',
        {
            **_stage_context('DONE'),
            **_estimate_context(data),
            'deposit_amount': _format_money(accepted_amount),
            'balance_amount': _format_money(data.get('balance_amount') or '0'),
            'balance_due_date': data.get('balance_due_date', ''),
            'balance_due_date_display': _format_date(data.get('balance_due_date', '')),
            'accepted_at_display': accepted_at_display,
            'estimate_accepted': True,
            'deposit_paid': True,
        },
    )


def _customer_estimate_or_404(token: str) -> Estimate:
    return get_object_or_404(
        Estimate.objects.select_related('current_version', 'accepted_version'),
        public_token=token,
    )


def _version_funnel_data(estimate: Estimate):
    version = estimate.accepted_version or estimate.current_version
    if not version:
        return {}
    data = dict(version.snapshot or {})
    data.setdefault('estimate_number', estimate.estimate_number)
    data.setdefault('business_name', estimate.business_name)
    data.setdefault('customer_name', estimate.customer_name)
    data.setdefault('terms_and_conditions', version.terms_text)
    data.setdefault('terms_version', version.terms_version)
    data.setdefault('deposit_amount', str(version.deposit_amount))
    data.setdefault('balance_amount', str(version.balance_amount))
    data.setdefault('estimate_total', str(version.total_amount))
    data.setdefault('total', str(version.total_amount))
    data.setdefault('deposit_enabled', version.deposit_enabled)
    if 'request_full_payment' not in data and version.snapshot:
        data['request_full_payment'] = bool(version.snapshot.get('request_full_payment'))
    data.setdefault(
        'payment_request_through_seymy',
        version.payment_request_through_seymy
        if hasattr(version, 'payment_request_through_seymy')
        else bool((version.snapshot or {}).get('payment_request_through_seymy')),
    )
    data.setdefault(
        'payment_request_type',
        version.payment_request_type
        if getattr(version, 'payment_request_type', None)
        else (version.snapshot or {}).get('payment_request_type') or 'none',
    )
    data.setdefault(
        'requested_payment_amount',
        str(version.requested_payment_amount)
        if getattr(version, 'requested_payment_amount', None) is not None
        else str((version.snapshot or {}).get('requested_payment_amount') or '0'),
    )
    return data


@require_http_methods(['GET', 'POST'])
def customer_pay(request, token):
    estimate = _customer_estimate_or_404(token)
    version = estimate.current_version
    if version is None:
        return HttpResponseBadRequest('Estimate has no version to pay')

    already_accepted = bool(estimate.accepted_version_id) or version.is_accepted
    funnel = _version_funnel_data(estimate)
    pdf_ctx = _estimate_pdf_context(funnel)
    error = ''
    agree_checked = False
    through_seymy = bool(
        getattr(version, 'payment_request_through_seymy', False)
        or funnel.get('payment_request_through_seymy')
    )
    request_full_payment = bool(funnel.get('request_full_payment')) and not bool(
        version.deposit_enabled
    )
    payment_request_type = (
        getattr(version, 'payment_request_type', None)
        or funnel.get('payment_request_type')
        or 'none'
    )

    # Estimate-only: no Seymy payment page — show the document instead.
    if not through_seymy or payment_request_type == 'none':
        return redirect('customer_estimate_document', token=token)

    if version.deposit_enabled and payment_request_type == 'deposit':
        pay_amount = version.deposit_amount
        pay_due_label = 'Deposit due today'
        pay_cta_verb = 'Pay'
        pay_received_label = 'deposit received'
    elif request_full_payment or payment_request_type == 'full':
        pay_amount = version.total_amount
        pay_due_label = 'Payment due today'
        pay_cta_verb = 'Pay'
        pay_received_label = 'payment received'
    else:
        pay_amount = version.requested_payment_amount or version.deposit_amount
        pay_due_label = 'Deposit due today'
        pay_cta_verb = 'Pay'
        pay_received_label = 'deposit received'

    if request.method == 'POST' and not already_accepted:
        agree_checked = request.POST.get('agree_terms') in ('1', 'on', 'true', 'yes')
        if not agree_checked:
            error = 'Please confirm you have reviewed and agree to the Estimate and Terms & Conditions.'
        else:
            try:
                result = create_deposit_checkout(
                    estimate=estimate,
                    version=version,
                    agreed_to_terms=True,
                    request=request,
                )
                return redirect(result['checkout_url'])
            except ValueError as exc:
                error = str(exc)

    return render(
        request,
        'landing/customer_pay.html',
        {
            'token': token,
            'estimate_number': estimate.estimate_number or pdf_ctx.get('estimate_number'),
            'business_name': estimate.business_name or pdf_ctx.get('business_name'),
            'total': pdf_ctx.get('total'),
            'deposit_amount': _format_money_fixed(pay_amount),
            'pay_due_label': pay_due_label,
            'pay_cta_verb': pay_cta_verb,
            'pay_received_label': pay_received_label,
            'request_full_payment': request_full_payment or payment_request_type == 'full',
            'already_accepted': already_accepted,
            'error': error,
            'agree_checked': agree_checked,
        },
    )


@require_http_methods(['GET'])
def customer_estimate_document(request, token):
    """Exact estimate PDF/version the customer is accepting."""
    estimate = _customer_estimate_or_404(token)
    version = estimate.current_version
    if estimate.accepted_version_id:
        version = estimate.accepted_version
    if version is None:
        return HttpResponseBadRequest('No estimate document')
    funnel = dict(version.snapshot or {})
    funnel.setdefault('estimate_number', estimate.estimate_number)
    funnel.setdefault('business_name', estimate.business_name)
    funnel.setdefault('customer_name', estimate.customer_name)
    funnel['terms_and_conditions'] = version.terms_text
    return render(
        request,
        'landing/start_estimate_pdf.html',
        _estimate_pdf_context(funnel),
    )


@require_http_methods(['GET', 'POST'])
def customer_pay_test_confirm(request, token):
    """
    Local-only stand-in for Stripe Checkout when keys are unset.

    Acceptance is recorded only by posting a payment_intent.succeeded
    event through the same process_stripe_event path as webhooks.
    """
    estimate = _customer_estimate_or_404(token)
    payment_intent_id = request.GET.get('payment_intent_id') or request.POST.get('payment_intent_id') or ''

    if request.method == 'POST':
        if not payment_intent_id:
            return HttpResponseBadRequest('payment_intent_id required')
        event = {
            'id': f'evt_test_{payment_intent_id}',
            'type': 'payment_intent.succeeded',
            'data': {'object': {'id': payment_intent_id}},
        }
        process_stripe_event(event)
        return redirect('customer_pay_success', token=token)

    return render(
        request,
        'landing/customer_pay_test_confirm.html',
        {
            'token': token,
            'payment_intent_id': payment_intent_id,
            'business_name': estimate.business_name,
        },
    )


@require_http_methods(['GET'])
def customer_pay_success(request, token):
    estimate = _customer_estimate_or_404(token)
    version = estimate.accepted_version or estimate.current_version
    if not estimate.accepted_version_id:
        # Stripe may redirect before webhook; wait state
        return render(
            request,
            'landing/customer_pay_success.html',
            {
                'pending_confirmation': True,
                'deposit_amount': _format_money_fixed(version.deposit_amount if version else 0),
                'balance_amount': _format_money_fixed(version.balance_amount if version else 0),
                'balance_due_date_display': _format_date(
                    (version.snapshot or {}).get('balance_due_date') if version else ''
                )
                or 'Upon completion',
            },
        )

    acceptance = (
        EstimateAcceptance.objects.filter(estimate=estimate).order_by('-accepted_at').first()
    )
    funnel = _version_funnel_data(estimate)
    return render(
        request,
        'landing/customer_pay_success.html',
        {
            'pending_confirmation': False,
            'deposit_amount': _format_money_fixed(
                acceptance.accepted_amount if acceptance else version.deposit_amount
            ),
            'balance_amount': _format_money_fixed(version.balance_amount),
            'balance_due_date_display': _format_date(funnel.get('balance_due_date') or '')
            or 'Upon completion',
        },
    )


@csrf_exempt
@require_POST
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')
    try:
        event = construct_webhook_event(payload, sig_header)
    except Exception as exc:
        return HttpResponseBadRequest(f'Invalid payload: {exc}')
    try:
        result = process_stripe_event(event)
    except Exception as exc:
        logger_msg = str(exc)
        return JsonResponse({'status': 'error', 'detail': logger_msg}, status=400)
    return JsonResponse(result)
