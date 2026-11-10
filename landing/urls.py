from django.urls import path, re_path

from . import public

# SITE-0: the MVP funnel (/start/…), customer payment and acceptance pages
# (/e/…, including test-confirm), the demo PDF and the Stripe webhook are
# retired. Their code in views.py is no longer routed.
urlpatterns = [
    path('', public.home, name='home'),
    re_path(r'^privacy/?$', public.privacy, name='privacy'),
    re_path(r'^terms/?$', public.terms, name='terms'),
    re_path(r'^sms-test-consent/?$', public.sms_test_consent, name='sms_test_consent'),
    re_path(r'^start(?:/.*)?$', public.retired_funnel, name='retired_funnel'),
    re_path(r'^e/', public.retired_customer_link, name='retired_customer_link'),
]
