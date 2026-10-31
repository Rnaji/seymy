"""
URL configuration for config project.

SITE-0: the Django admin is not exposed on the public site.
"""
from django.urls import include, path

urlpatterns = [
    path('', include('landing.urls')),
]
