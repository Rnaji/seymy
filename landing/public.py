"""
SITE-0 public surface.

The only views reachable on the public site. None of them touches the database,
the session store, Stripe or the retired MVP funnel in ``views.py``.
"""
from urllib.parse import quote

from django.conf import settings
from django.http import HttpResponseGone
from django.shortcuts import redirect, render
from django.template.loader import render_to_string

EARLY_ACCESS_SUBJECT = 'Seymy early access'
EARLY_ACCESS_BODY = (
    'Hi Seymy team,\n\n'
    "I'd like to join the Seymy pilot.\n\n"
    'Business name:\n'
    'Trade:\n'
    'State:\n'
    'Team size:\n'
)


def _early_access_mailto(email: str) -> str:
    return (
        f'mailto:{email}'
        f'?subject={quote(EARLY_ACCESS_SUBJECT)}'
        f'&body={quote(EARLY_ACCESS_BODY)}'
    )


def home(request):
    email = settings.SITE_CONTACT_EMAIL
    return render(
        request,
        'landing/provisional_home.html',
        {
            'contact_email': email,
            'early_access_href': _early_access_mailto(email),
        },
    )


def retired_funnel(request):
    """Old ``/start/…`` CTA targets: send visitors back to the home page."""
    return redirect('home')


def retired_customer_link(request):
    """Old ``/e/<token>/…`` customer links: gone, without looking anything up."""
    return HttpResponseGone(render_to_string('landing/retired_link.html', request=request))
