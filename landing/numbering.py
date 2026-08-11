"""Estimate numbering for real funnel (separate from landing demo SEY-001)."""

from __future__ import annotations

import re

INITIAL_ESTIMATE_NUMBER = 'SEY-001'
_SEY_NUMBER_RE = re.compile(r'^SEY-(\d+)$', re.IGNORECASE)


def next_estimate_number() -> str:
    """
    Next SEY-NNN after existing Estimate rows.

    After a full test-data reset (no estimates), returns SEY-001.
    """
    from .models import Estimate

    max_n = 0
    for raw in Estimate.objects.exclude(estimate_number='').values_list('estimate_number', flat=True):
        match = _SEY_NUMBER_RE.match(str(raw).strip())
        if match:
            max_n = max(max_n, int(match.group(1)))
    return f'SEY-{max_n + 1:03d}'


def ensure_estimate_number(data: dict) -> dict:
    """Assign SEY-NNN only when missing — never invent demo customers/prices."""
    if str(data.get('estimate_number') or '').strip():
        return data
    updated = dict(data)
    updated['estimate_number'] = next_estimate_number()
    return updated
