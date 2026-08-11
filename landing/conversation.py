"""
Conversational interface helpers for the first-estimate funnel.

The LLM/real STT layer can later replace parse_message().
This module populates/updates a persistent estimate draft —
conversation is an interface, NOT the source of truth.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .tax import parse_job_tax_message, recalculate_financials, has_tax_preference


def _money(value) -> Decimal:
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _parse_money_token(token: str) -> Decimal | None:
    cleaned = token.replace(',', '').replace('$', '').strip()
    try:
        return _money(cleaned)
    except (InvalidOperation, ValueError):
        return None


MONTHS = {
    'january': 1,
    'jan': 1,
    'february': 2,
    'feb': 2,
    'march': 3,
    'mar': 3,
    'april': 4,
    'apr': 4,
    'may': 5,
    'june': 6,
    'jun': 6,
    'july': 7,
    'jul': 7,
    'august': 8,
    'aug': 8,
    'september': 9,
    'sept': 9,
    'sep': 9,
    'october': 10,
    'oct': 10,
    'november': 11,
    'nov': 11,
    'december': 12,
    'dec': 12,
}

STATE_ABBREV = {
    'alabama': 'AL',
    'alaska': 'AK',
    'arizona': 'AZ',
    'arkansas': 'AR',
    'california': 'CA',
    'colorado': 'CO',
    'connecticut': 'CT',
    'delaware': 'DE',
    'florida': 'FL',
    'georgia': 'GA',
    'hawaii': 'HI',
    'idaho': 'ID',
    'illinois': 'IL',
    'indiana': 'IN',
    'iowa': 'IA',
    'kansas': 'KS',
    'kentucky': 'KY',
    'louisiana': 'LA',
    'maine': 'ME',
    'maryland': 'MD',
    'massachusetts': 'MA',
    'michigan': 'MI',
    'minnesota': 'MN',
    'mississippi': 'MS',
    'missouri': 'MO',
    'montana': 'MT',
    'nebraska': 'NE',
    'nevada': 'NV',
    'new hampshire': 'NH',
    'new jersey': 'NJ',
    'new mexico': 'NM',
    'new york': 'NY',
    'north carolina': 'NC',
    'north dakota': 'ND',
    'ohio': 'OH',
    'oklahoma': 'OK',
    'oregon': 'OR',
    'pennsylvania': 'PA',
    'rhode island': 'RI',
    'south carolina': 'SC',
    'south dakota': 'SD',
    'tennessee': 'TN',
    'texas': 'TX',
    'utah': 'UT',
    'vermont': 'VT',
    'virginia': 'VA',
    'washington': 'WA',
    'west virginia': 'WV',
    'wisconsin': 'WI',
    'wyoming': 'WY',
    'district of columbia': 'DC',
}

STREET_SUFFIX = (
    r'(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|boulevard|blvd|way|court|ct|circle|cir|place|pl)\.?'
)


def _next_weekday(name: str, today: date | None = None) -> date:
    today = today or date.today()
    weekdays = {
        'monday': 0,
        'tuesday': 1,
        'wednesday': 2,
        'thursday': 3,
        'friday': 4,
        'saturday': 5,
        'sunday': 6,
    }
    target = weekdays[name.lower()]
    days_ahead = (target - today.weekday() + 7) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


def parse_date_phrase(text: str, today: date | None = None) -> str | None:
    """Return ISO date string if a date phrase is found."""
    today = today or date.today()
    lower = text.lower()

    if 'tomorrow' in lower:
        return (today + timedelta(days=1)).isoformat()
    if re.search(r'\btoday\b', lower) and 'same day' not in lower and 'same-day' not in lower:
        return today.isoformat()

    for name in ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'):
        if re.search(rf'\b(next\s+)?{name}\b', lower):
            return _next_weekday(name, today).isoformat()

    m = re.search(
        r'\b('
        + '|'.join(MONTHS.keys())
        + r')\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*((?:19|20)\d{2}))?\b',
        lower,
    )
    if m:
        month = MONTHS[m.group(1)]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if not m.group(3) and candidate < today:
            candidate = date(year + 1, month, day)
        return candidate.isoformat()

    m = re.search(r'\b(\d{1,2})[/-](\d{1,2})(?:[/-]((?:19|20)\d{2}))?\b', lower)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if not m.group(3) and candidate < today:
            candidate = date(year + 1, month, day)
        return candidate.isoformat()

    m = re.search(r'\bin\s+(\d+|two|three|four)\s+weeks?\b', lower)
    if m:
        token = m.group(1)
        mapping = {'two': 2, 'three': 3, 'four': 4}
        weeks = mapping.get(token, int(token) if token.isdigit() else 0)
        if weeks:
            return (today + timedelta(weeks=weeks)).isoformat()

    return None


def _parse_duration_days(text: str) -> int | None:
    """Extract job duration in days from phrases like 'three days' / '2 days'."""
    lower = (text or '').lower()
    word_map = {
        'one': 1,
        'a': 1,
        'an': 1,
        'two': 2,
        'three': 3,
        'four': 4,
        'five': 5,
        'six': 6,
        'seven': 7,
        'eight': 8,
        'nine': 9,
        'ten': 10,
    }
    m = re.search(
        r'(?:(?:should\s+)?(?:take|last|be)\s+|job\s+(?:should\s+)?take\s+|about\s+|around\s+)?'
        r'(\d+|one|a|an|two|three|four|five|six|seven|eight|nine|ten)\s+days?\b',
        lower,
    )
    if not m:
        return None
    token = m.group(1)
    if token.isdigit():
        days = int(token)
    else:
        days = word_map.get(token)
    if days and 1 <= days <= 90:
        return days
    return None


def _iso_to_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], '%Y-%m-%d').date()
        except ValueError:
            return None
    return None


def _add_duration(start_iso: str, days: int) -> str:
    """Inclusive duration: start + (days - 1). One day => same day."""
    start = _iso_to_date(start_iso)
    if start is None:
        return start_iso
    return (start + timedelta(days=max(days, 1) - 1)).isoformat()


_UNSCHEDULED_RE = re.compile(
    r'not\s+scheduled\s+yet'
    r"|i\s+don'?t\s+know\s+yet"
    r"|don'?t\s+know\s+yet"
    r'|not\s+sure\s+yet'
    r'|unknown'
    r'|no\s+date'
    r'|not\s+confirmed'
    r'|unscheduled'
    r'|skip(?:\s+that)?'
    r'|none\s+yet',
    re.I,
)

_CLEAR_START_RE = re.compile(
    r'(?:remove|clear|delete|unset)\s+(?:the\s+)?(?:job\s+)?start(?:\s+date)?'
    r'|no\s+start\s+date'
    r'|start\s+date\s+(?:is\s+)?(?:unknown|unset|none)',
    re.I,
)

_CLEAR_COMPLETION_RE = re.compile(
    r'(?:remove|clear|delete|unset)\s+(?:the\s+)?(?:expected\s+)?completion(?:\s+date)?'
    r'|no\s+completion\s+date'
    r'|completion\s+date\s+(?:is\s+)?(?:unknown|unset|none)',
    re.I,
)

_SAME_DAY_RE = re.compile(
    r'same[-\s]?day(?:\s+completion)?'
    r'|finish(?:ed)?\s+(?:the\s+)?same\s+day'
    r'|complete(?:d)?\s+(?:the\s+)?same\s+day'
    r'|done\s+(?:the\s+)?same\s+day'
    r'|start\s+and\s+(?:finish|complete)\s+(?:the\s+)?same\s+day',
    re.I,
)

_START_CTX_RE = re.compile(
    r'(?:'
    r'start\s+(?:the\s+)?job\s+on'
    r'|we(?:\'re| are)\s+starting'
    r'|schedule\s+(?:it|the\s+job|work)?\s+for'
    r'|work\s+starts'
    r'|i\s+should\s+start\s+around'
    r'|start(?:ing)?(?:\s+(?:on|around|about))?'
    r'|move\s+(?:the\s+)?start(?:\s+date)?(?:\s+to)?'
    r'|change\s+(?:the\s+)?start(?:\s+date)?(?:\s+to)?'
    r'|estimated\s+start'
    r')\s+([^.\n]+)',
    re.I,
)

_COMPLETION_CTX_RE = re.compile(
    r'(?:'
    r'finish\s+by'
    r'|we\s+should\s+be\s+done'
    r'|done\s+(?:by|on)'
    r'|expect(?:ed)?\s+(?:to\s+)?(?:be\s+)?(?:finished|done|complete)'
    r'|expected\s+completion'
    r'|complete(?:d)?\s+(?:by|on)'
    r'|job\s+(?:should\s+be\s+)?finished\s+on'
    r'|finished\s+on'
    r'|change\s+(?:the\s+)?completion(?:\s+date)?(?:\s+to)?'
    r'|move\s+(?:the\s+)?completion(?:\s+date)?(?:\s+to)?'
    r'|completion(?:\s+date)?(?:\s+to)?'
    r')\s+([^.\n]+)',
    re.I,
)

_BALANCE_FINISH_RE = re.compile(
    r'balance\s+(?:when|due\s+when)\s+(?:the\s+)?job\s+is\s+finished\s+(?:on\s+)?([^.\n]+)'
    r'|remaining\s+(?:balance\s+)?when\s+(?:the\s+)?job\s+is\s+finished\s+(?:on\s+)?([^.\n]+)'
    r'|balance\s+(?:due\s+)?(?:on|by)\s+completion(?:\s+on)?\s+([^.\n]+)',
    re.I,
)


def parse_schedule_updates(text: str, existing: dict | None = None, today: date | None = None) -> dict:
    """
    Extract optional job_start_date / expected_completion_date from natural language.

    Scheduling expectations only — never required to generate an estimate.
    When balance is tied to a completion date, may also set balance_due_date
    (kept as a separate field).
    """
    existing = existing or {}
    today = today or date.today()
    text = (text or '').strip()
    if not text:
        return {}

    updates: dict = {}
    lower = text.lower()

    if _CLEAR_START_RE.search(lower) or (
        _UNSCHEDULED_RE.search(lower)
        and re.search(r'\bstart\b', lower)
        and not re.search(r'\bfinish|\bcomplete|\bdone\b', lower)
    ):
        updates['job_start_date'] = None
        updates['job_start_declined'] = True

    if _CLEAR_COMPLETION_RE.search(lower):
        updates['expected_completion_date'] = None
        updates['completion_declined'] = True

    if _UNSCHEDULED_RE.search(lower) and not updates:
        # Generic decline (follow-up answer): clear whichever is unset / being asked
        if not existing.get('job_start_date'):
            updates['job_start_date'] = None
            updates['job_start_declined'] = True
        if not existing.get('expected_completion_date'):
            updates['expected_completion_date'] = None
            updates['completion_declined'] = True
        if updates:
            updates['schedule_mentioned'] = True
            return updates

    # Duration: "Job should take three days"
    duration = _parse_duration_days(text)
    if duration:
        updates['job_duration_days'] = duration
        updates['schedule_mentioned'] = True

    # Start date contexts
    start_date = None
    for m in _START_CTX_RE.finditer(lower):
        start_date = parse_date_phrase(m.group(0), today=today) or parse_date_phrase(m.group(1), today=today)
        if start_date:
            break
    # "Work starts next Monday"
    if not start_date:
        m = re.search(r'work\s+starts\s+([^.\n]+)|starting\s+([^.\n]+)', lower)
        if m:
            start_date = parse_date_phrase(m.group(0), today=today)

    if start_date:
        updates['job_start_date'] = start_date
        updates['job_start_declined'] = False
        updates['schedule_mentioned'] = True

    # Completion contexts (not balance-only)
    completion_date = None
    for m in _COMPLETION_CTX_RE.finditer(lower):
        # Avoid treating "balance when finished on X" only as completion here —
        # handled below so both fields can be set.
        snippet = m.group(0)
        if 'balance' in snippet and 'finished' in snippet:
            continue
        completion_date = parse_date_phrase(snippet, today=today) or parse_date_phrase(m.group(1), today=today)
        if completion_date:
            break

    # Balance linked to completion: set BOTH fields, keep them separate
    balance_finish = None
    m = _BALANCE_FINISH_RE.search(lower)
    if m:
        phrase = next((g for g in m.groups() if g), m.group(0))
        balance_finish = parse_date_phrase(phrase, today=today) or parse_date_phrase(m.group(0), today=today)
    elif re.search(r'balance\s+when\s+(?:the\s+)?job\s+is\s+finished', lower):
        # Date may follow in the same sentence
        m2 = re.search(
            r'balance\s+when\s+(?:the\s+)?job\s+is\s+finished(?:\s+on)?\s+([^.\n]+)',
            lower,
        )
        if m2:
            balance_finish = parse_date_phrase(m2.group(0), today=today) or parse_date_phrase(
                m2.group(1), today=today
            )

    if balance_finish:
        updates['expected_completion_date'] = balance_finish
        updates['balance_due_date'] = balance_finish
        updates['completion_declined'] = False
        updates['schedule_mentioned'] = True
        completion_date = completion_date or balance_finish

    if completion_date and 'expected_completion_date' not in updates:
        updates['expected_completion_date'] = completion_date
        updates['completion_declined'] = False
        updates['schedule_mentioned'] = True

    # Same-day completion
    if _SAME_DAY_RE.search(lower):
        updates['schedule_mentioned'] = True
        start_for_same = updates.get('job_start_date') or existing.get('job_start_date')
        if start_for_same:
            updates['expected_completion_date'] = start_for_same
            updates['completion_declined'] = False
            updates['job_duration_days'] = 1
        else:
            updates['same_day_completion'] = True

    # Apply duration onto start when completion still unknown
    start_final = updates.get('job_start_date', existing.get('job_start_date'))
    duration_final = updates.get('job_duration_days', existing.get('job_duration_days'))
    if (
        start_final
        and duration_final
        and not updates.get('expected_completion_date')
        and not existing.get('expected_completion_date')
        and not updates.get('completion_declined')
    ):
        updates['expected_completion_date'] = _add_duration(start_final, int(duration_final))
        updates['completion_declined'] = False

    if updates.get('same_day_completion') and start_final and 'expected_completion_date' not in updates:
        updates['expected_completion_date'] = start_final
        updates['completion_declined'] = False

    return updates


def parse_balance_due_updates(text: str, existing: dict | None = None, today: date | None = None) -> dict:
    """
    Extract balance_due_date without confusing it with job start dates.

    Prefer explicit balance/due language. Completion-linked balance is handled
    in parse_schedule_updates (sets both fields separately).
    """
    existing = existing or {}
    today = today or date.today()
    lower = (text or '').lower()
    updates: dict = {}

    # Explicit balance move / due date (not start)
    m = re.search(
        r'(?:balance\s+due|due\s+date|move\s+(?:the\s+)?balance(?:\s+date)?(?:\s+to)?|'
        r'remaining\s+(?:balance\s+)?(?:due\s+)?(?:on|by)?)\s+([^.\n]+)',
        lower,
    )
    if m and 'start' not in m.group(0):
        d = parse_date_phrase(m.group(0), today=today) or parse_date_phrase(m.group(1), today=today)
        if d:
            updates['balance_due_date'] = d
            return updates

    # "the rest August 24" / "remaining on August 24"
    m = re.search(r'(?:the\s+)?(?:rest|remaining|balance)\s+(?:on\s+|by\s+)?([^.\n]+)', lower)
    if m and not re.search(r'start', m.group(0)):
        d = parse_date_phrase(m.group(0), today=today) or parse_date_phrase(m.group(1), today=today)
        if d:
            updates['balance_due_date'] = d
            return updates

    # Follow-up answering balance_due_date with a bare date
    if existing.get('deposit_enabled') and not existing.get('balance_due_date'):
        # Don't steal a start-only phrase
        if not re.search(r'\bstart\b|schedule|work\s+starts', lower):
            d = parse_date_phrase(lower, today=today)
            if d and re.search(r'balance|rest|remaining|due|finished|complete|august|january|february|march|april|may|june|july|september|october|november|december|\d{1,2}[/-]\d{1,2}', lower):
                updates['balance_due_date'] = d

    return updates


def _normalize_state(token: str) -> str | None:
    token = (token or '').strip()
    if not token:
        return None
    upper = token.upper()
    if len(upper) == 2 and upper.isalpha():
        return upper
    return STATE_ABBREV.get(token.lower())


def parse_job_address(text: str) -> dict:
    """Extract job site address fields from natural language."""
    updates: dict = {}
    # 24 Main Street, Orlando, FL 32801
    m = re.search(
        rf'(\d+\s+[A-Za-z0-9\'.\- ]+?{STREET_SUFFIX})\s*,\s*'
        r'([A-Za-z .]+?)\s*,\s*'
        r'([A-Za-z]{2}|[A-Za-z ]+?)(?:\s+(\d{5}(?:-\d{4})?))?\b',
        text,
        flags=re.I,
    )
    if m:
        updates['job_site_address'] = re.sub(r'\s+', ' ', m.group(1)).strip(' .,')
        updates['job_site_city'] = m.group(2).strip(' .,')
        state = _normalize_state(m.group(3))
        if state:
            updates['job_site_state'] = state
        if m.group(4):
            updates['job_site_zip'] = m.group(4)
        return updates

    # 24 Main Street, Orlando (no state)
    m = re.search(
        rf'(\d+\s+[A-Za-z0-9\'.\- ]+?{STREET_SUFFIX})\s*,\s*([A-Za-z .]+)\b',
        text,
        flags=re.I,
    )
    if m:
        updates['job_site_address'] = re.sub(r'\s+', ' ', m.group(1)).strip(' .,')
        updates['job_site_city'] = m.group(2).strip(' .,')
        return updates

    return updates


def parse_send_destination(text: str) -> dict:
    """Extract customer phone/email send destination."""
    updates: dict = {}
    lower = text.lower()

    email = re.search(r'[\w.+-]+@[\w.-]+\.\w+', text)
    if email:
        updates['customer_email'] = email.group(0)
        updates['send_method'] = 'email'
        updates['send_destination_set'] = True

    phone = re.search(
        r'(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b',
        text,
    )
    if phone:
        updates['customer_phone'] = phone.group(0)
        if 'email' not in lower or 'text' in lower or 'sms' in lower:
            updates['send_method'] = updates.get('send_method') or 'sms'
        updates['send_destination_set'] = True

    return updates


def parse_message(text: str, existing: dict | None = None) -> dict:
    """
    Extract structured updates from a natural-language message.
    Returns only fields that were detected (partial update).
    Never wipes the draft — caller merges via apply_updates().
    """
    existing = existing or {}
    text = (text or '').strip()
    if not text:
        return {}

    updates: dict = {'raw_message': text}
    lower = text.lower()

    # Send destination (when answering "where should I send it?")
    send_updates = parse_send_destination(text)
    updates.update(send_updates)

    # Customer name
    m = re.search(
        r'(?:estimate\s+for|for|customer(?:\s+is)?|client(?:\s+is)?)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)',
        text,
    )
    if m:
        name = m.group(1).strip()
        # Avoid swallowing street names as customer ("for 24 Main" won't match)
        if not re.match(r'^\d', name):
            updates['customer_name'] = name
    else:
        m = re.search(r'^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', text.strip())
        if m and 'replace' not in m.group(1).lower():
            updates['customer_name'] = m.group(1).strip()

    # Job site address
    updates.update(parse_job_address(text))

    # Job description snippet
    job_bits = []
    for pattern in (
        r'(replace[^.!\n]+)',
        r'(install[^.!\n]+)',
        r'(repair[^.!\n]+)',
        r'(water\s+heater[^.!\n]*)',
    ):
        m = re.search(pattern, text, flags=re.I)
        if m:
            job_bits.append(m.group(1).strip(' .,'))
    if job_bits:
        updates['job_description'] = job_bits[0]
    elif not existing.get('job_description') and not updates.get('deposit_enabled') and not send_updates:
        # Only treat raw text as description on first job message
        if not re.search(r'%|deposit|up\s*front|upfront|card\s+fee', lower):
            if len(text) > 12:
                updates['job_description'] = text

    # Line items: materials + labor
    line_items = []
    mat = re.search(
        r'\$?\s*([\d,]+(?:\.\d{2})?)\s*(?:in\s+)?materials?\b|\bmaterials?\s*(?:of|:)?\s*\$?\s*([\d,]+(?:\.\d{2})?)',
        lower,
    )
    labor = re.search(
        r'\$?\s*([\d,]+(?:\.\d{2})?)\s*(?:in\s+)?labor\b|\blabor\s*(?:of|:)?\s*\$?\s*([\d,]+(?:\.\d{2})?)',
        lower,
    )

    materials_amt = None
    labor_amt = None
    if mat:
        materials_amt = _parse_money_token(mat.group(1) or mat.group(2))
    if labor:
        labor_amt = _parse_money_token(labor.group(1) or labor.group(2))

    pairs = re.findall(
        r'\$\s*([\d,]+(?:\.\d{2})?)\s*(materials?|labor|disposal|parts?)',
        lower,
    )
    for amount_s, kind in pairs:
        amt = _parse_money_token(amount_s)
        if amt is None:
            continue
        if kind.startswith('material') or kind.startswith('part'):
            materials_amt = amt
        elif kind.startswith('labor'):
            labor_amt = amt
        elif kind.startswith('disposal'):
            line_items.append({'label': 'Disposal', 'amount': str(amt), 'type': 'other'})

    if materials_amt is not None:
        label = 'Materials'
        if 'water heater' in lower:
            label = '50 gal. water heater' if ('50' in lower or 'gallon' in lower) else 'Water heater'
        line_items.insert(0, {'label': label, 'amount': str(materials_amt), 'type': 'material'})
    if labor_amt is not None:
        line_items.append({'label': 'Labor', 'amount': str(labor_amt), 'type': 'labor'})

    if not line_items:
        totals = re.findall(r'\$\s*([\d,]+(?:\.\d{2})?)', text)
        deposit_like = bool(re.search(r'deposit|up\s*front|upfront|take\s+\$', lower))
        if len(totals) == 1 and not deposit_like:
            total = _parse_money_token(totals[0])
            if total is not None:
                updates['estimate_total'] = str(total)
                updates['line_items'] = [
                    {'label': 'Job total', 'amount': str(total), 'type': 'other'}
                ]
        elif len(totals) >= 2 and 'water heater' in lower and not deposit_like:
            a1 = _parse_money_token(totals[0])
            a2 = _parse_money_token(totals[1])
            if a1 and a2:
                line_items = [
                    {'label': '50 gal. water heater', 'amount': str(a1), 'type': 'material'},
                    {'label': 'Labor', 'amount': str(a2), 'type': 'labor'},
                ]

    if line_items:
        updates['line_items'] = line_items
        updates['estimate_total'] = str(sum((_money(i['amount']) for i in line_items), Decimal('0')))

    # Deposit TERMS: remove deposit requirement from the estimate
    no_deposit = bool(
        re.search(
            r'\bno\s+deposit\b|\bwithout\s+a?\s*deposit\b|\bpayment\s+when\s+the\s+job\s+is\s+done\b|'
            r'\bno\s+deposit\s+(?:yet|for\s+now)\b',
            lower,
        )
    )
    if no_deposit:
        updates['deposit_enabled'] = False
        updates['deposit_type'] = ''
        updates['deposit_value'] = ''
        updates['deposit_amount'] = '0.00'
        updates['request_full_payment'] = False
        updates['payment_request_through_seymy'] = False

    # Estimate-only / payment outside Seymy — keep deposit TERMS, disable online request
    disable_seymy_payment = bool(
        re.search(
            r'send\s+(?:the\s+|him\s+|her\s+|them\s+)?estimate\s+only|'
            r'just\s+send\s+(?:him\s+|her\s+|them\s+|the\s+)?(?:the\s+)?(?:pdf|estimate)|'
            r"don'?t\s+request\s+(?:the\s+)?deposit\s+online|"
            r"don'?t\s+request\s+(?:the\s+)?deposit(?:\s+yet)?|"
            r"don'?t\s+request\s+payment(?:\s+yet)?|"
            r"don'?t\s+send\s+a?\s*payment\s+link|"
            r"i('ll| will)\s+collect\s+(?:the\s+)?deposit\s+myself|"
            r'collect\s+(?:the\s+)?deposit\s+(?:myself|outside|offline|in\s+person)|'
            r'payment\s+outside\s+seymy|'
            r'no\s+payment\s+link|'
            r'no\s+online\s+payment',
            lower,
        )
    )
    if disable_seymy_payment and not no_deposit:
        updates['payment_request_through_seymy'] = False

    # Full payment request (no deposit) — funnel flag for Ready-to-send case B
    if re.search(
        r'request\s+(?:the\s+)?full\s+(?:payment|amount)|'
        r'pay\s+in\s+full|'
        r'collect\s+(?:the\s+)?(?:full\s+)?payment\s+now|'
        r'request\s+(?:full\s+)?payment\s+now|'
        r'request\s+(?:the\s+)?(?:full\s+)?amount\s+now',
        lower,
    ):
        updates['request_full_payment'] = True
        updates['deposit_enabled'] = False
        updates['deposit_type'] = ''
        updates['deposit_value'] = ''
        updates['deposit_amount'] = '0.00'
        updates['payment_request_through_seymy'] = True

    # Re-enable Seymy online payment request (deposit or payment link)
    # Skip when the contractor explicitly disabled the online request in this message.
    if not disable_seymy_payment and re.search(
        r'request\s+(?:the\s+)?deposit\s+online|'
        r'send\s+(?:him\s+|her\s+|them\s+)?a?\s*payment\s+link|'
        r'request\s+(?:the\s+)?deposit\s+(?:through\s+)?seymy|'
        r'use\s+seymy\s+payments?|'
        r'online\s+deposit\s+request',
        lower,
    ):
        updates['payment_request_through_seymy'] = True
        # If no deposit terms and not full payment, leave type resolution to recalculate

    pct = re.search(
        r'(\d{1,2}(?:\.\d+)?)\s*%\s*(?:up\s*front|upfront|deposit|now)\b|'
        r'(?:deposit|up\s*front|upfront)\s*(?:of\s*)?(\d{1,2}(?:\.\d+)?)\s*%',
        lower,
    )
    if pct and not no_deposit:
        percent = Decimal(pct.group(1) or pct.group(2))
        updates['deposit_enabled'] = True
        updates['deposit_type'] = 'percentage'
        updates['deposit_value'] = str(percent)
        updates['request_full_payment'] = False
        if 'payment_request_through_seymy' not in updates:
            updates['payment_request_through_seymy'] = True

    fixed = re.search(
        r'(?:take|deposit|up\s*front|upfront|collect)\s*\$\s*([\d,]+(?:\.\d{2})?)\s*(?:now|up\s*front|upfront|deposit)?|'
        r'(?:deposit|up\s*front|upfront)\s*(?:of\s*)?\$\s*([\d,]+(?:\.\d{2})?)',
        lower,
    )
    if fixed and not no_deposit and 'deposit_type' not in updates:
        if re.search(r'deposit|up\s*front|upfront|\bnow\b|take\s+\$', lower):
            amt = _parse_money_token(fixed.group(1) or fixed.group(2))
            if amt is not None:
                updates['deposit_enabled'] = True
                updates['deposit_type'] = 'fixed'
                updates['deposit_value'] = str(amt)
                updates['request_full_payment'] = False
                if 'payment_request_through_seymy' not in updates:
                    updates['payment_request_through_seymy'] = True

    # Optional project schedule (start / expected completion) — never required
    schedule_updates = parse_schedule_updates(text, existing=existing)
    updates.update(schedule_updates)

    # Bare-date answers to optional schedule follow-ups
    pending = existing.get('pending_question') or ''
    if pending in {'job_start_date', 'expected_completion_date', 'schedule'} and 'job_start_date' not in updates:
        if _UNSCHEDULED_RE.search(lower):
            if pending in {'job_start_date', 'schedule'}:
                updates['job_start_date'] = None
                updates['job_start_declined'] = True
            if pending in {'expected_completion_date', 'schedule'}:
                updates['expected_completion_date'] = None
                updates['completion_declined'] = True
            updates['schedule_mentioned'] = True
        elif _SAME_DAY_RE.search(lower) and pending in {'expected_completion_date', 'schedule'}:
            start_for_same = updates.get('job_start_date') or existing.get('job_start_date')
            if start_for_same:
                updates['expected_completion_date'] = start_for_same
                updates['completion_declined'] = False
            else:
                updates['same_day_completion'] = True
            updates['schedule_mentioned'] = True
        else:
            bare = parse_date_phrase(text)
            if bare:
                if pending == 'job_start_date':
                    updates['job_start_date'] = bare
                    updates['job_start_declined'] = False
                elif pending == 'expected_completion_date':
                    updates['expected_completion_date'] = bare
                    updates['completion_declined'] = False
                elif pending == 'schedule':
                    if not existing.get('job_start_date') and not updates.get('job_start_date'):
                        updates['job_start_date'] = bare
                        updates['job_start_declined'] = False
                    else:
                        updates['expected_completion_date'] = bare
                        updates['completion_declined'] = False
                updates['schedule_mentioned'] = True

    # Balance due date (separate from schedule; may already be set via completion link)
    if 'balance_due_date' not in updates:
        updates.update(parse_balance_due_updates(text, existing=existing))

    # Card fee preference
    if re.search(
        r'pass\s+(?:the\s+)?(?:eligible\s+)?(?:card\s+)?(?:processing\s+)?fees?'
        r'|customer\s+covers?'
        r'|customer\s+pays?'
        r'|charge\s+(?:the\s+)?customer',
        lower,
    ):
        updates['pass_card_fees_to_customer'] = True
        updates['card_fee_preference_set'] = True
    elif re.search(
        r"i('ll| will)?\s+(pay|cover|absorb)"
        r'|i\s+cover'
        r"|don'?t\s+charge\s+(?:the\s+)?customer"
        r'|no\s+card\s+fees?\s+to\s+(?:the\s+)?customer'
        r'|contractor\s+covers?',
        lower,
    ):
        updates['pass_card_fees_to_customer'] = False
        updates['card_fee_preference_set'] = True

    # Conversational edits
    m = re.search(r'(?:make\s+)?(?:the\s+)?(?:change\s+(?:the\s+)?)?deposit\s+(?:to\s+)?(\d{1,2}(?:\.\d+)?)\s*%', lower)
    if m:
        updates['deposit_enabled'] = True
        updates['deposit_type'] = 'percentage'
        updates['deposit_value'] = m.group(1)
        updates['request_full_payment'] = False
        if 'payment_request_through_seymy' not in updates:
            updates['payment_request_through_seymy'] = True

    m = re.search(r'(?:make\s+)?labor\s+\$?\s*([\d,]+(?:\.\d{2})?)', lower)
    if m:
        labor_amt = _parse_money_token(m.group(1))
        if labor_amt is not None:
            items = list(existing.get('line_items') or updates.get('line_items') or [])
            found = False
            new_items = []
            for item in items:
                if str(item.get('label', '')).lower() == 'labor' or item.get('type') == 'labor':
                    new_items.append({**item, 'label': 'Labor', 'amount': str(labor_amt), 'type': 'labor'})
                    found = True
                else:
                    new_items.append(item)
            if not found:
                new_items.append({'label': 'Labor', 'amount': str(labor_amt), 'type': 'labor'})
            updates['line_items'] = new_items
            updates['estimate_total'] = str(sum((_money(i['amount']) for i in new_items), Decimal('0')))

    m = re.search(r'add\s+\$?\s*([\d,]+(?:\.\d{2})?)\s+for\s+([a-zA-Z][a-zA-Z\s]{1,40})', lower)
    if m:
        amt = _parse_money_token(m.group(1))
        label = m.group(2).strip(' .,')
        if amt is not None and label:
            items = list(existing.get('line_items') or updates.get('line_items') or [])
            items.append({'label': label.title(), 'amount': str(amt), 'type': 'other'})
            updates['line_items'] = items
            updates['estimate_total'] = str(sum((_money(i['amount']) for i in items), Decimal('0')))

    # Job-specific sales tax (MVP: dictated in the job conversation)
    tax_update = parse_job_tax_message(text)
    if tax_update:
        updates.update(tax_update)

    return updates


def apply_updates(data: dict, updates: dict) -> dict:
    """Merge conversational updates into the estimate draft and recompute money fields."""
    merged = {**data}

    for key, value in updates.items():
        if key in {'raw_message'}:
            continue
        merged[key] = value

    history = list(merged.get('conversation_history') or [])
    if updates.get('raw_message'):
        history.append({'role': 'contractor', 'text': updates['raw_message']})
    merged['conversation_history'] = history

    if 'line_items' in updates and updates['line_items']:
        merged['line_items'] = updates['line_items']
        subtotal = sum((_money(i['amount']) for i in updates['line_items']), Decimal('0'))
        merged['subtotal'] = str(subtotal)

    # Resolve same-day / duration against possibly pre-existing start
    if merged.get('same_day_completion') and merged.get('job_start_date'):
        merged['expected_completion_date'] = merged['job_start_date']
        merged['job_duration_days'] = 1
        merged['same_day_completion'] = False
    elif (
        merged.get('job_start_date')
        and merged.get('job_duration_days')
        and not merged.get('expected_completion_date')
        and not merged.get('completion_declined')
    ):
        merged['expected_completion_date'] = _add_duration(
            merged['job_start_date'], int(merged['job_duration_days'])
        )

    # Mark optional schedule follow-up resolved after an answer/decline
    pending = data.get('pending_question') or ''
    if pending in {'job_start_date', 'expected_completion_date', 'schedule'}:
        if (
            'job_start_date' in updates
            or 'expected_completion_date' in updates
            or updates.get('job_start_declined')
            or updates.get('completion_declined')
            or updates.get('same_day_completion')
        ):
            merged['schedule_followup_done'] = True

    # Do not invent demo estimate numbers (e.g. 1042). Real SEY-NNN is
    # allocated in the funnel views / publish path when needed.

    return recalculate_financials(merged)


def has_job_site(data: dict) -> bool:
    return bool(data.get('job_site_address') or (data.get('job_site_city') and data.get('job_site_state')))


def has_send_destination(data: dict) -> bool:
    return bool(data.get('send_destination_set') or data.get('customer_phone') or data.get('customer_email'))


def soft_schedule_followups(data: dict) -> list[str]:
    """
    Optional schedule questions — only when useful (partial schedule signal).
    Never blocks estimate generation; decline leaves dates null.
    """
    if not data.get('schedule_mentioned'):
        return []
    if data.get('schedule_followup_done'):
        return []

    keys = []
    if not data.get('job_start_date') and not data.get('job_start_declined'):
        keys.append('job_start_date')
    if not data.get('expected_completion_date') and not data.get('completion_declined'):
        # Duration alone without start: ask start (completion can be derived later)
        if data.get('job_duration_days') and not data.get('job_start_date'):
            if 'job_start_date' not in keys:
                keys.append('job_start_date')
        elif not data.get('job_duration_days'):
            keys.append('expected_completion_date')
    return keys


def missing_requirements(data: dict, *, for_send: bool = False) -> list[str]:
    """Return ordered list of missing requirement keys for the estimate draft."""
    missing = []

    if not data.get('customer_name'):
        missing.append('customer_name')

    if not has_job_site(data):
        missing.append('job_site')

    if not data.get('estimate_total') and not data.get('line_items'):
        missing.append('pricing')

    if 'deposit_enabled' not in data:
        missing.append('payment_terms')
    elif data.get('deposit_enabled') and not data.get('balance_due_date'):
        missing.append('balance_due_date')

    if not data.get('card_fee_preference_set'):
        missing.append('card_fees')

    if not has_tax_preference(data):
        missing.append('sales_tax')

    if for_send and not has_send_destination(data):
        missing.append('send_destination')

    return missing


QUESTION_COPY = {
    'customer_name': {
        'prompt': 'Who is this estimate for?',
        'hint': 'For example: “John Anderson.”',
    },
    'job_site': {
        'prompt': "What's the job address?",
        'hint': 'For example: “24 Main Street, Orlando, FL.”',
    },
    'pricing': {
        'prompt': 'What are the materials and labor amounts?',
        'hint': 'For example: “$1,350 materials and $500 labor.”',
    },
    'payment_terms': {
        'prompt': 'How do you want to get paid?',
        'hint': 'For example: “30% upfront and the rest August 24.”',
    },
    'balance_due_date': {
        'prompt': 'When should the remaining balance be due?',
        'hint': 'For example: “August 24.”',
    },
    'card_fees': {
        'prompt': 'Who should cover eligible card processing fees?',
        'hint': 'For example: “Pass them to the customer” or “I’ll cover them.”',
    },
    'sales_tax': {
        'prompt': 'What sales tax rate should I apply to this job?',
        'hint': 'For example: “6 percent.” or “Don’t apply sales tax.”',
    },
    'job_start_date': {
        'prompt': 'When do you expect to start the job?',
        'hint': 'For example: “August 20.” or “Not scheduled yet.”',
    },
    'expected_completion_date': {
        'prompt': 'When do you expect the job to be finished?',
        'hint': 'For example: “August 24.” or “Same day.” or “I don’t know yet.”',
    },
    'send_destination': {
        'prompt': 'Estimate ready. Where should I send it?',
        'hint': 'For example: “Text it to John at 407-555-0182.” or “Email john@example.com.”',
    },
}


def next_question(data: dict, *, for_send: bool = False) -> dict | None:
    """
    Return the next conversational follow-up, or None if ready for summary/send.
    When multiple fields are missing, ask for them together in one turn.
    Schedule questions are optional and never block the estimate.
    """
    missing = missing_requirements(data, for_send=for_send)
    if not missing:
        soft = soft_schedule_followups(data) if not for_send else []
        if soft:
            if len(soft) == 1:
                key = soft[0]
                copy = QUESTION_COPY[key]
                return {
                    'key': key,
                    'prompt': copy['prompt'],
                    'hint': copy['hint'],
                    'questions': [],
                    'stage': 'COMPLETE',
                    'optional': True,
                }
            questions = [QUESTION_COPY[k]['prompt'] for k in soft]
            hints = [QUESTION_COPY[k]['hint'] for k in soft]
            return {
                'key': 'schedule',
                'prompt': 'Got it. Quick schedule question.',
                'hint': ' '.join(hints[:2]),
                'questions': questions,
                'missing_keys': soft,
                'stage': 'COMPLETE',
                'optional': True,
            }
        return None

    if len(missing) == 1:
        key = missing[0]
        copy = QUESTION_COPY[key]
        return {
            'key': key,
            'prompt': copy['prompt'],
            'hint': copy['hint'],
            'questions': [],
            'stage': 'COMPLETE',
        }

    word = {2: 'two', 3: 'three'}.get(len(missing), str(len(missing)))
    questions = [QUESTION_COPY[k]['prompt'] for k in missing]
    hints = [QUESTION_COPY[k]['hint'] for k in missing]
    return {
        'key': 'multi',
        'prompt': f'Got it. I just need {word} things.',
        'hint': ' '.join(hints[:2]),
        'questions': questions,
        'missing_keys': missing,
        'stage': 'COMPLETE',
    }


def confirmation_lines(data: dict) -> list[str]:
    """Short Seymy confirmation snippets after an answer."""
    lines = []
    if data.get('job_start_date') or data.get('expected_completion_date'):
        bits = []
        if data.get('job_start_date'):
            bits.append(f"start {_format_date(data.get('job_start_date'))}")
        if data.get('expected_completion_date'):
            bits.append(f"finish {_format_date(data.get('expected_completion_date'))}")
        if bits:
            lines.append('Got it. Schedule: ' + ', '.join(bits) + '.')
    if data.get('deposit_enabled') and data.get('deposit_amount'):
        if data.get('deposit_type') == 'percentage':
            lines.append(
                f"Got it. {data.get('deposit_value')}% upfront (${_format_money(data['deposit_amount'])})"
                + (
                    f", with the remaining ${_format_money(data.get('balance_amount'))} due {_format_date(data.get('balance_due_date'))}."
                    if data.get('balance_due_date')
                    else '.'
                )
            )
        else:
            lines.append(
                f"Got it. ${_format_money(data['deposit_amount'])} upfront"
                + (
                    f", remaining ${_format_money(data.get('balance_amount'))} due {_format_date(data.get('balance_due_date'))}."
                    if data.get('balance_due_date')
                    else '.'
                )
            )
    elif data.get('deposit_enabled') is False:
        lines.append('Got it. No deposit — payment when the job is done.')
    return lines


def _format_money(value) -> str:
    amount = _money(value or 0)
    formatted = f'{amount:,.2f}'
    if formatted.endswith('.00'):
        return formatted[:-3]
    return formatted


def _format_date(value) -> str:
    if not value:
        return ''
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError:
            return value
    return f'{value.strftime("%b")} {value.day}, {value.year}'


# ---------------------------------------------------------------------------
# Conversational estimate / payment retrieval (MVP intent parse)
# ---------------------------------------------------------------------------

_RETRIEVAL_PATTERNS = (
    (
        'show_by_number',
        re.compile(
            r'\b(?:show|find|open|get|pull\s+up)\s+(?:me\s+)?(?:estimate\s*#?\s*|est\.?\s*#?\s*)(\d+)\b',
            re.I,
        ),
    ),
    (
        'stop_reminders',
        re.compile(
            r"\b(?:don'?t|do\s+not|stop|cancel)\s+remind(?:ing)?\s+"
            r"([A-Za-z][A-Za-z\'\-]*(?:\s+[A-Za-z][A-Za-z\'\-]*){0,3})"
            r"(?:\s+again)?\b|"
            r"\bstop\s+(?:deposit\s+)?reminders?\s+(?:for|on)\s+"
            r"([A-Za-z][A-Za-z\'\-]*(?:\s+[A-Za-z][A-Za-z\'\-]*){0,3})\b",
            re.I,
        ),
    ),
    (
        'remind_deposit',
        re.compile(
            r'\bremind\s+([A-Za-z][A-Za-z\'\-]*(?:\s+[A-Za-z][A-Za-z\'\-]*){0,3})\s+'
            r'(?:about\s+(?:the\s+)?(?:deposit|payment)|to\s+pay(?:\s+(?:the\s+)?deposit)?)\b',
            re.I,
        ),
    ),
    (
        'unpaid',
        re.compile(
            r'\b(?:unpaid\s+(?:estimates?|deposits?)|estimates?\s+(?:that\s+are\s+)?unpaid|'
            r'which\s+customers?\s+still\s+owe|'
            r'who\s+(?:still\s+)?owes?\s+(?:me\s+)?(?:a\s+)?deposit|'
            r'still\s+owe\s+me\s+a\s+deposit|'
            r'waiting\s+(?:on|for)\s+(?:a\s+)?deposit|'
            r'show\s+(?:me\s+)?(?:my\s+)?unpaid\s+deposits?)\b',
            re.I,
        ),
    ),
    (
        'sent_this_week',
        re.compile(
            r'\bestimates?\s+sent\s+(?:this|the)\s+week\b|'
            r'\bsent\s+(?:this|the)\s+week\b',
            re.I,
        ),
    ),
    (
        'has_paid',
        re.compile(
            r'\bhas\s+([A-Za-z][A-Za-z\'\-]*(?:\s+[A-Za-z][A-Za-z\'\-]*){0,3})\s+'
            r'paid\s+(?:(?:his|her|their|the)\s+)?(?:deposit|payment)?\??',
            re.I,
        ),
    ),
    (
        'resend',
        re.compile(
            r'\b(?:send|email|text|resend|re-send)\s+(?:me\s+)?'
            r'([A-Za-z][A-Za-z\'\-]*(?:\s+[A-Za-z][A-Za-z\'\-]*){0,3})(?:\'s)?\s+'
            r'estimate\s+again\b',
            re.I,
        ),
    ),
    (
        'show_by_customer',
        re.compile(
            r'\b(?:show|find|open|get|pull\s+up)\s+(?:me\s+)?'
            r'([A-Za-z][A-Za-z\'\-]*(?:\s+[A-Za-z][A-Za-z\'\-]*){0,3})(?:\'s)?\s+estimate\b',
            re.I,
        ),
    ),
)


def parse_retrieval_intent(text: str) -> dict | None:
    """
    Detect contractor questions about existing estimates / payment status.

    Returns {'intent': str, ...} or None when the message is not a retrieval ask.
    """
    text = (text or '').strip()
    if not text:
        return None
    for intent, pattern in _RETRIEVAL_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        if intent == 'show_by_number':
            return {'intent': intent, 'estimate_number': m.group(1)}
        if intent in ('has_paid', 'resend', 'show_by_customer', 'remind_deposit'):
            name = re.sub(r'\s+', ' ', (m.group(1) or '').strip(" '\".,?"))
            name = re.sub(r"'s$", '', name, flags=re.I).strip()
            # Avoid false positives like "my unpaid" captured as a name
            if name.lower() in {'my', 'the', 'an', 'a', 'all', 'any'}:
                continue
            return {'intent': intent, 'customer_name': name}
        if intent == 'stop_reminders':
            raw = m.group(1) or m.group(2) or ''
            name = re.sub(r'\s+', ' ', raw.strip(" '\".,?"))
            name = re.sub(r"'s$", '', name, flags=re.I).strip()
            name = re.sub(r'\s+again$', '', name, flags=re.I).strip()
            if name.lower() in {'my', 'the', 'an', 'a', 'all', 'any', 'him', 'her', 'them'}:
                continue
            return {'intent': intent, 'customer_name': name}
        return {'intent': intent}
    return None


def _estimate_queryset(funnel: dict | None = None):
    from .models import Estimate, EstimateStatus

    qs = Estimate.objects.select_related('current_version', 'accepted_version').exclude(
        status=EstimateStatus.DRAFT
    )
    funnel = funnel or {}
    business = (funnel.get('business_name') or '').strip()
    if business:
        qs = qs.filter(business_name__iexact=business)
    return qs


def _payment_status_phrase(estimate) -> str:
    from .models import PaymentStatus

    version = estimate.current_version
    through = bool(version and version.payment_request_through_seymy)
    if estimate.payment_status == PaymentStatus.PAID or estimate.deposit_paid_at:
        amount = ''
        if version and version.requested_payment_amount:
            amount = f' (${_format_money(version.requested_payment_amount)})'
        elif version and version.deposit_amount:
            amount = f' (${_format_money(version.deposit_amount)})'
        return f'paid{amount}'
    if through and estimate.payment_status == PaymentStatus.REQUESTED:
        prt = (version.payment_request_type if version else '') or ''
        amount = version.requested_payment_amount if version else None
        if prt == 'full':
            return f'waiting for full payment (${_format_money(amount or (version.total_amount if version else 0))})'
        return f'waiting for deposit (${_format_money(amount or (version.deposit_amount if version else 0))})'
    if estimate.payment_status == PaymentStatus.FAILED:
        return 'payment failed — customer can try again'
    return 'no online payment requested'


def _summarize_estimate(estimate) -> str:
    version = estimate.current_version
    total = _format_money(version.total_amount if version else 0)
    number = estimate.estimate_number or str(estimate.pk)
    status = _payment_status_phrase(estimate)
    return (
        f'Estimate #{number} for {estimate.customer_name or "customer"} — '
        f'total ${total}. Status: {status}.'
    )


def _find_by_customer(qs, name: str):
    name = (name or '').strip()
    if not name:
        return qs.none()
    exact = qs.filter(customer_name__iexact=name)
    if exact.exists():
        return exact
    # First-name / partial match
    return qs.filter(customer_name__icontains=name)


def answer_retrieval_query(text: str, *, funnel: dict | None = None) -> str | None:
    """
    Answer estimate/payment lookup questions against Estimate rows (+ funnel hint).

    Returns a conversational reply string, or None if this is not a retrieval ask.
    """
    intent = parse_retrieval_intent(text)
    if not intent:
        return None

    from django.utils import timezone

    from .models import PaymentStatus

    funnel = funnel or {}
    qs = _estimate_queryset(funnel)
    kind = intent['intent']

    if kind == 'show_by_number':
        number = str(intent['estimate_number'])
        estimate = (
            qs.filter(estimate_number=number).first()
            or qs.filter(estimate_number__iendswith=number).first()
        )
        if not estimate and funnel.get('estimate_number') == number and funnel.get('persisted_estimate_id'):
            estimate = qs.filter(pk=funnel['persisted_estimate_id']).first()
        if not estimate:
            return f"I couldn't find estimate #{number}."
        return _summarize_estimate(estimate)

    if kind == 'unpaid':
        unpaid = qs.filter(payment_status=PaymentStatus.REQUESTED).order_by('-sent_at', '-created_at')
        # Also include funnel awaiting flag if published in this session but not yet paid
        rows = list(unpaid[:10])
        if not rows:
            if funnel.get('awaiting_payment') or funnel.get('awaiting_deposit'):
                name = funnel.get('customer_name') or 'your customer'
                number = funnel.get('estimate_number') or '?'
                amount = funnel.get('requested_payment_amount') or funnel.get('deposit_amount') or '0'
                return (
                    f"{name} still owes the ${_format_money(amount)} on estimate #{number}."
                )
            return 'You have no unpaid Seymy payment requests right now.'
        lines = ['These customers still owe a Seymy payment:']
        for est in rows:
            version = est.current_version
            amount = (
                version.requested_payment_amount
                if version and version.requested_payment_amount
                else (version.deposit_amount if version else 0)
            )
            lines.append(
                f'• {est.customer_name or "Customer"} — estimate #{est.estimate_number or est.pk} '
                f'(${_format_money(amount)})'
            )
        return '\n'.join(lines)

    if kind == 'sent_this_week':
        now = timezone.now()
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        sent = qs.filter(sent_at__gte=start).order_by('-sent_at')
        rows = list(sent[:15])
        if not rows:
            return "You haven't sent any estimates this week yet."
        lines = [f'Estimates sent this week ({len(rows)}):']
        for est in rows:
            when = est.sent_at.strftime('%a') if est.sent_at else ''
            lines.append(
                f'• #{est.estimate_number or est.pk} — {est.customer_name or "Customer"}'
                + (f' ({when})' if when else '')
            )
        return '\n'.join(lines)

    if kind == 'has_paid':
        name = intent['customer_name']
        matches = list(_find_by_customer(qs, name)[:5])
        if not matches and funnel.get('customer_name') and name.lower() in str(funnel.get('customer_name')).lower():
            # Fall back to current funnel payment flags
            first = name.split()[0]
            if funnel.get('deposit_paid') or funnel.get('estimate_accepted'):
                return f'Yes — {first} has paid the deposit.'
            if funnel.get('awaiting_deposit') or funnel.get('awaiting_payment'):
                return f'Not yet — still waiting for {first} to pay.'
            return f"I don't see an online payment request for {name}."
        if not matches:
            return f"I couldn't find an estimate for {name}."
        est = matches[0]
        first = (est.customer_name or name).split()[0]
        if est.payment_status == PaymentStatus.PAID or est.deposit_paid_at:
            return f'Yes — {first} has paid the deposit on estimate #{est.estimate_number or est.pk}.'
        if est.payment_status == PaymentStatus.REQUESTED:
            return f'Not yet — still waiting for {first} to pay on estimate #{est.estimate_number or est.pk}.'
        return f"Estimate #{est.estimate_number or est.pk} for {est.customer_name} has no online payment pending."

    if kind == 'remind_deposit':
        from .delivery import send_manual_deposit_reminder

        name = intent['customer_name']
        matches = list(_find_by_customer(qs, name)[:5])
        if not matches:
            return f"I couldn't find an estimate for {name}."
        est = matches[0]
        first = (est.customer_name or name).split()[0]
        version = est.current_version
        if not version or not version.payment_request_through_seymy:
            return (
                f"Estimate #{est.estimate_number or est.pk} wasn't sent with a Seymy payment request, "
                f"so I can't send deposit reminders for {first}."
            )
        if est.payment_status == PaymentStatus.PAID or est.deposit_paid_at:
            return f'{first} already paid — no reminder needed.'
        result = send_manual_deposit_reminder(est)
        if not result.get('ok'):
            return f"I can't remind {first} right now (payment may already be settled or reminders stopped)."
        return (
            f"Reminder sent to {first} about the deposit on estimate "
            f"#{est.estimate_number or est.pk}."
        )

    if kind == 'stop_reminders':
        from .delivery import cancel_deposit_reminders

        name = intent['customer_name']
        matches = list(_find_by_customer(qs, name)[:5])
        if not matches:
            return f"I couldn't find an estimate for {name}."
        est = matches[0]
        first = (est.customer_name or name).split()[0]
        cancel_deposit_reminders(est, reason='contractor_request')
        return (
            f"Got it — I won't send any more automatic deposit reminders to {first} "
            f"for estimate #{est.estimate_number or est.pk}."
        )

    if kind in ('show_by_customer', 'resend'):
        name = intent['customer_name']
        matches = list(_find_by_customer(qs, name)[:5])
        if not matches and funnel.get('customer_name') and name.lower() in str(funnel.get('customer_name')).lower():
            number = funnel.get('estimate_number') or '?'
            token = funnel.get('public_token') or ''
            status = 'waiting for payment' if funnel.get('awaiting_payment') else 'sent'
            reply = (
                f"Estimate #{number} for {funnel.get('customer_name')} — {status}."
            )
            if kind == 'resend' and token:
                reply += f' Customer link token: {token}.'
            elif kind == 'resend':
                reply += ' I can pull it up anytime — ask me again after it is sent.'
            return reply
        if not matches:
            return f"I couldn't find an estimate for {name}."
        est = matches[0]
        reply = _summarize_estimate(est)
        if kind == 'resend':
            reply += f' Public token: {est.public_token}.'
            reply += ' Say the word if you want me to resend it to the customer.'
        return reply

    return None
