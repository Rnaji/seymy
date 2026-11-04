"""
Public surface of www.seymy.com.

The only views reachable on the public site. None of them touches the database,
the session store, Stripe or the retired MVP funnel in ``views.py``.
"""
from django.http import HttpResponseGone
from django.shortcuts import redirect, render
from django.template.loader import render_to_string

from .demo_estimate import TALK_ESTIMATE_CONTEXT
from .download import PILOT_NOTE, download_cta


def home(request):
    """The V1.1 landing page (static content, no database)."""
    return render(
        request,
        'landing/index.html',
        {
            'download': download_cta(),
            'pilot_note': PILOT_NOTE,
            'talk_estimate_document_html': render_to_string(
                'landing/partials/estimate_document.html',
                TALK_ESTIMATE_CONTEXT,
                request=request,
            ),
        },
    )


def retired_funnel(request):
    """Old ``/start/…`` CTA targets: send visitors back to the home page."""
    return redirect('home')


def retired_customer_link(request):
    """Old ``/e/<token>/…`` customer links: gone, without looking anything up."""
    return HttpResponseGone(render_to_string('landing/retired_link.html', request=request))
