"""
Modular sales-tax helpers for job-specific contractor-stated rates.

MVP: tax is extracted from the job conversation (or a COMPLETE follow-up).
This is NOT a jurisdiction tax engine — rates are explicit contractor input.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def _money(value) -> Decimal:
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _format_rate(rate: Decimal) -> str:
    text = f'{rate:f}'.rstrip('0').rstrip('.')
    return text or '0'


def _job_tax_fields(*, enabled: bool, rate: str = '0', applies_to: str = 'all') -> dict:
    fields = {
        'tax_enabled': enabled,
        'tax_rate': rate if enabled else '0',
        'tax_configured': True,
        'tax_preference_set': True,
        'estimate_tax_override': True,
    }
    if enabled:
        fields['tax_applies_to'] = applies_to if applies_to in {'all', 'materials'} else 'all'
    else:
        fields['tax_applies_to'] = 'all'
    return fields


def _detect_tax_applies_to(lower: str) -> str:
    """Return 'materials' when contractor scopes tax to materials only."""
    if re.search(
        r'(?:sales\s+)?tax\s+to\s+materials?(?:\s+only)?'
        r'|materials?\s+only'
        r'|only\s+(?:on\s+)?materials?'
        r'|tax\s+(?:on|for)\s+materials?(?:\s+only)?'
        r'|materials?\s+(?:are|is)\s+taxable',
        lower,
    ):
        return 'materials'
    return 'all'


def parse_job_tax_message(text: str) -> dict | None:
    """
    Parse job-specific sales tax from natural language.

    Examples:
    - Apply 6% sales tax.
    - Apply 6% sales tax to materials only.
    - Sales tax is 7.25%.
    - Add 8 percent tax.
    - 6 percent tax.
    - No sales tax. / Don't apply sales tax.
    """
    text = (text or '').strip()
    if not text:
        return None
    lower = text.lower()
    applies_to = _detect_tax_applies_to(lower)

    if re.search(
        r"don'?t\s+apply\s+(?:sales\s+)?tax"
        r"|no\s+(?:sales\s+)?tax"
        r"|don'?t\s+charge\s+(?:any\s+)?(?:sales\s+)?tax"
        r"|without\s+(?:sales\s+)?tax"
        r"|tax\s*[- ]?exempt"
        r"|zero\s+(?:sales\s+)?tax",
        lower,
    ):
        return _job_tax_fields(enabled=False)

    # Require explicit tax language so "30% upfront" is never treated as tax
    if not re.search(r'\btax\b', lower):
        # Allow short follow-up answers: "6 percent." / "6%"
        m = re.fullmatch(r'\s*(\d{1,2}(?:\.\d{1,4})?)\s*(%|percent|pct)\.?\s*', lower)
        if not m:
            return None
        try:
            rate = Decimal(m.group(1))
        except InvalidOperation:
            return None
        if rate < 0 or rate > 100:
            return None
        rate_s = _format_rate(rate)
        if rate == 0:
            return _job_tax_fields(enabled=False)
        return _job_tax_fields(enabled=True, rate=rate_s, applies_to=applies_to)

    patterns = (
        r'(?:apply|add|use)\s+(\d{1,2}(?:\.\d{1,4})?)\s*(%|percent|pct)?\s*(?:sales\s+)?tax',
        r'(?:sales\s+)?tax(?:\s+rate)?\s*(?:is|of|at|:)?\s*(\d{1,2}(?:\.\d{1,4})?)\s*(%|percent|pct)?',
        r'(\d{1,2}(?:\.\d{1,4})?)\s*(%|percent|pct)\s*(?:sales\s+)?tax',
        r'change\s+(?:the\s+)?(?:sales\s+)?tax\s+(?:to\s+)?(\d{1,2}(?:\.\d{1,4})?)\s*(%|percent|pct)?',
    )
    rate = None
    for pattern in patterns:
        m = re.search(pattern, lower)
        if m:
            try:
                rate = Decimal(m.group(1))
            except InvalidOperation:
                rate = None
            if rate is not None:
                break

    if rate is None:
        # Scope-only edit: "tax materials only" after a rate already exists
        if applies_to == 'materials' and re.search(r'\btax\b', lower):
            return {
                'tax_applies_to': 'materials',
                'tax_preference_set': True,
                'estimate_tax_override': True,
            }
        return None
    if rate < 0 or rate > 100:
        return None

    rate_s = _format_rate(rate)
    if rate == 0:
        return _job_tax_fields(enabled=False)
    return _job_tax_fields(enabled=True, rate=rate_s, applies_to=applies_to)


# Backward-compatible aliases used by older call sites
def parse_tax_setup_answer(text: str) -> dict | None:
    return parse_job_tax_message(text)


def parse_tax_override_message(text: str) -> dict | None:
    return parse_job_tax_message(text)


def resolve_tax(data: dict) -> tuple[bool, Decimal | None]:
    """Resolve effective tax for the current estimate: (enabled, rate)."""
    # Job-specific tax (MVP primary path)
    if data.get('tax_preference_set') or data.get('estimate_tax_override'):
        if not data.get('tax_enabled'):
            return False, None
        try:
            rate = Decimal(str(data.get('tax_rate') or '0'))
        except InvalidOperation:
            return False, None
        if rate <= 0:
            return False, None
        return True, rate

    # Optional profile default (retained for returning users / future settings)
    if not data.get('sales_tax_preference_set'):
        return False, None
    if not data.get('sales_tax_enabled'):
        return False, None
    try:
        rate = Decimal(str(data.get('default_sales_tax_rate') or data.get('tax_rate') or '0'))
    except InvalidOperation:
        return False, None
    if rate <= 0:
        return False, None
    return True, rate


def has_tax_preference(data: dict) -> bool:
    return bool(
        data.get('tax_preference_set')
        or data.get('estimate_tax_override')
        or data.get('sales_tax_preference_set')
    )


def line_items_subtotal(data: dict) -> Decimal:
    items = data.get('line_items') or []
    if items:
        return sum((_money(i.get('amount') or 0) for i in items), Decimal('0.00'))
    if data.get('subtotal') not in (None, ''):
        return _money(data['subtotal'])
    return Decimal('0.00')


def materials_subtotal(data: dict) -> Decimal:
    """Sum line items typed as materials/parts (for materials-only tax)."""
    items = data.get('line_items') or []
    total = Decimal('0.00')
    for item in items:
        kind = str(item.get('type') or '').lower()
        label = str(item.get('label') or '').lower()
        if kind in {'material', 'materials', 'part', 'parts'} or 'material' in label or 'water heater' in label:
            total += _money(item.get('amount') or 0)
    return total


def tax_base_amount(data: dict, subtotal: Decimal) -> tuple[Decimal, str]:
    """
    Return (taxable_amount, applies_to).
    applies_to is 'materials' or 'all'.
    """
    applies_to = (data.get('tax_applies_to') or 'all').lower()
    if applies_to == 'materials':
        base = materials_subtotal(data)
        # If line items lack material typing, fall back to full subtotal
        if base == 0 and subtotal > 0 and not (data.get('line_items') or []):
            return subtotal, 'all'
        return base, 'materials'
    return subtotal, 'all'


def recalculate_financials(data: dict) -> dict:
    """
    Recompute subtotal, tax, grand total, and deposit/balance.

    Deposit percentages apply to the tax-inclusive total.
    When tax_applies_to == 'materials', tax is computed on materials only.
    """
    merged = {**data}
    subtotal = line_items_subtotal(merged)
    if subtotal == 0 and merged.get('estimate_total') and not merged.get('line_items'):
        if not merged.get('tax_configured'):
            subtotal = _money(merged['estimate_total'])

    merged['subtotal'] = str(subtotal)

    enabled, rate = resolve_tax(merged)
    if enabled and rate is not None:
        taxable, applies_to = tax_base_amount(merged, subtotal)
        merged['tax_applies_to'] = applies_to
        tax_amount = _money(taxable * rate / Decimal('100'))
        total = _money(subtotal + tax_amount)
        rate_s = _format_rate(rate)
        merged['tax_configured'] = True
        merged['tax_enabled'] = True
        merged['tax_rate'] = rate_s
        merged['tax_amount'] = str(tax_amount)
        if applies_to == 'materials':
            merged['tax_label'] = f'Tax ({rate_s}% materials)'
        else:
            merged['tax_label'] = f'Sales Tax ({rate_s}%)'
        merged['total'] = str(total)
        merged['estimate_total'] = str(total)
    else:
        merged['tax_configured'] = has_tax_preference(merged)
        merged['tax_enabled'] = False
        merged['tax_amount'] = '0.00'
        merged['tax_label'] = 'Sales Tax'
        merged['total'] = str(subtotal)
        merged['estimate_total'] = str(subtotal)

    total = _money(merged['estimate_total'])
    if merged.get('deposit_enabled') is False:
        merged['deposit_amount'] = '0.00'
        merged['balance_amount'] = str(total)
    elif merged.get('deposit_type') == 'percentage' and merged.get('deposit_value'):
        try:
            percent = Decimal(str(merged['deposit_value']))
            deposit_amount = _money(total * percent / Decimal('100'))
            merged['deposit_amount'] = str(deposit_amount)
            merged['balance_amount'] = str(_money(total - deposit_amount))
        except (InvalidOperation, ValueError):
            pass
    elif merged.get('deposit_type') == 'fixed' and merged.get('deposit_value'):
        try:
            deposit_amount = _money(merged['deposit_value'])
            if deposit_amount > 0:
                merged['deposit_amount'] = str(min(deposit_amount, total))
                merged['balance_amount'] = str(_money(total - _money(merged['deposit_amount'])))
        except (InvalidOperation, ValueError):
            pass

    # Online payment request through Seymy (distinct from deposit TERMS on the PDF)
    from .acceptance import resolve_payment_request

    return resolve_payment_request(merged)
