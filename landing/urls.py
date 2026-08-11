from django.urls import path

from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('demo/estimate/', views.landing_demo_estimate_pdf, name='landing_demo_estimate_pdf'),
    path('start/new/', views.start_new_estimate, name='start_new_estimate'),
    path('start/', views.start_job, name='start_job'),
    path('start/summary/', views.start_summary, name='start_summary'),
    path('start/business/', views.start_business, name='start_business'),
    path('start/estimate/pdf/', views.start_estimate_pdf, name='start_estimate_pdf'),
    path('start/deposit/', views.start_deposit, name='start_deposit'),
    path('start/deposit/configure/', views.start_deposit_configure, name='start_deposit_configure'),
    path('start/payment/', views.start_payment_review, name='start_payment_review'),
    path('start/review/', views.start_review_send, name='start_review_send'),
    path('start/sent/', views.start_sent, name='start_sent'),
    path('start/deposit/paid/', views.start_deposit_paid, name='start_deposit_paid'),
    # Customer acceptance + deposit payment
    path('e/<str:token>/pay/', views.customer_pay, name='customer_pay'),
    path('e/<str:token>/document/', views.customer_estimate_document, name='customer_estimate_document'),
    path('e/<str:token>/pay/success/', views.customer_pay_success, name='customer_pay_success'),
    path(
        'e/<str:token>/pay/test-confirm/',
        views.customer_pay_test_confirm,
        name='customer_pay_test_confirm',
    ),
    path('webhooks/stripe/', views.stripe_webhook, name='stripe_webhook'),
]
