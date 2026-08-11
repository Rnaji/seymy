from django.contrib import admin

from . import models


@admin.register(models.Estimate)
class EstimateAdmin(admin.ModelAdmin):
    list_display = (
        'estimate_number',
        'business_name',
        'customer_name',
        'status',
        'payment_status',
        'acceptance_requirement',
        'sent_at',
        'accepted_at',
        'reminder_count',
        'reminders_cancelled',
    )
    search_fields = ('estimate_number', 'public_token', 'business_name', 'customer_name')
    readonly_fields = ('public_token', 'created_at', 'updated_at', 'last_reminder_sent_at')


@admin.register(models.EstimateVersion)
class EstimateVersionAdmin(admin.ModelAdmin):
    list_display = (
        'estimate',
        'version_number',
        'deposit_amount',
        'total_amount',
        'payment_request_through_seymy',
        'payment_request_type',
        'requested_payment_amount',
        'is_accepted',
        'terms_version',
        'created_at',
    )
    readonly_fields = ('document_version', 'created_at')


@admin.register(models.EstimateAcceptance)
class EstimateAcceptanceAdmin(admin.ModelAdmin):
    list_display = (
        'estimate',
        'estimate_version_number',
        'accepted_amount',
        'acceptance_method',
        'payment_intent_id',
        'accepted_at',
    )
    readonly_fields = (
        'document_version',
        'signed_at',
        'signature_provider',
        'signature_reference',
        'signed_document_version',
    )


@admin.register(models.PendingCheckout)
class PendingCheckoutAdmin(admin.ModelAdmin):
    list_display = (
        'payment_intent_id',
        'estimate',
        'amount',
        'seymy_platform_fee',
        'payment_request_type',
        'status',
        'created_at',
    )


@admin.register(models.OutboundCustomerMessage)
class OutboundCustomerMessageAdmin(admin.ModelAdmin):
    list_display = ('estimate', 'purpose', 'channel', 'to_detail', 'created_at')
    list_filter = ('purpose', 'channel')
    search_fields = ('to_detail', 'body', 'estimate__estimate_number', 'estimate__customer_name')
    readonly_fields = ('created_at',)


admin.site.register(models.StripeWebhookEvent)
