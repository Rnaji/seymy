import secrets
import uuid

from django.db import models
from django.utils import timezone


def _public_token():
    return secrets.token_urlsafe(24)


class AcceptanceRequirement(models.TextChoices):
    """
    How the customer may accept this estimate.

    Extensible for state/job-specific rules later. Do not assume deposit
    payment alone is legally sufficient in every US jurisdiction.
    """

    PAYMENT = 'payment', 'Deposit payment'
    ELECTRONIC_SIGNATURE = 'electronic_signature', 'Electronic signature'
    PAYMENT_AND_SIGNATURE = 'payment_and_signature', 'Payment and signature'


class EstimateStatus(models.TextChoices):
    DRAFT = 'draft', 'Draft'
    SENT = 'sent', 'Sent'
    ACCEPTED = 'accepted', 'Accepted'
    DEPOSIT_PAID = 'deposit_paid', 'Deposit paid'
    CANCELLED = 'cancelled', 'Cancelled'
    SUPERSEDED = 'superseded', 'Superseded'


class AcceptanceMethod(models.TextChoices):
    DEPOSIT_PAYMENT = 'deposit_payment', 'Deposit payment'
    ELECTRONIC_SIGNATURE = 'electronic_signature', 'Electronic signature'
    PAYMENT_AND_SIGNATURE = 'payment_and_signature', 'Payment and signature'


class PaymentRequestType(models.TextChoices):
    """Whether Seymy creates an online payment request (separate from PDF terms)."""

    NONE = 'none', 'No online payment request'
    DEPOSIT = 'deposit', 'Deposit through Seymy'
    FULL = 'full', 'Full payment through Seymy'


class PaymentStatus(models.TextChoices):
    """
    Seymy-tracked payment status only.

    External / off-platform payments are never recorded here.
    """

    NONE = 'none', 'No Seymy payment'
    REQUESTED = 'requested', 'Payment requested'
    PAID = 'paid', 'Paid (Stripe confirmed)'
    FAILED = 'failed', 'Payment failed'


class Estimate(models.Model):
    """Contractor estimate root. Versions hold frozen snapshots."""

    public_token = models.CharField(max_length=64, unique=True, default=_public_token, db_index=True)
    estimate_number = models.CharField(max_length=32, blank=True, default='')
    status = models.CharField(
        max_length=32,
        choices=EstimateStatus.choices,
        default=EstimateStatus.DRAFT,
        db_index=True,
    )
    acceptance_requirement = models.CharField(
        max_length=32,
        choices=AcceptanceRequirement.choices,
        default=AcceptanceRequirement.PAYMENT,
    )
    # Seymy payment tracking — never set from contractor "mark as paid"
    payment_status = models.CharField(
        max_length=32,
        choices=PaymentStatus.choices,
        default=PaymentStatus.NONE,
        db_index=True,
    )
    business_name = models.CharField(max_length=255, blank=True, default='')
    customer_name = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    deposit_paid_at = models.DateTimeField(null=True, blank=True)

    # Automatic deposit reminders (Seymy-collected payments only)
    reminder_day2_at = models.DateTimeField(null=True, blank=True, db_index=True)
    reminder_day5_at = models.DateTimeField(null=True, blank=True, db_index=True)
    reminders_cancelled = models.BooleanField(default=False)
    last_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    reminder_count = models.PositiveSmallIntegerField(default=0)

    # Points at the version currently offered to the customer (may differ from accepted).
    current_version = models.ForeignKey(
        'EstimateVersion',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    # Frozen pointer — never moved to a later revision silently.
    accepted_version = models.ForeignKey(
        'EstimateVersion',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Estimate #{self.estimate_number or self.pk}'


class EstimateVersion(models.Model):
    """
    Immutable snapshot of an estimate.

    Once created, snapshot / terms fields must not be overwritten.
    Material contractor changes create a new version.
    """

    estimate = models.ForeignKey(Estimate, on_delete=models.CASCADE, related_name='versions')
    version_number = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    # Opaque version id for acceptance records / document identity
    document_version = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    # Frozen payload used to render the exact PDF the customer saw
    snapshot = models.JSONField(default=dict)
    terms_version = models.CharField(max_length=64, blank=True, default='1')
    terms_text = models.TextField(blank=True, default='')

    # Money at freeze time (Decimal as string in snapshot; also denormalized)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    deposit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    balance_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    deposit_enabled = models.BooleanField(default=False)

    # Online payment request through Seymy (distinct from PDF deposit TERMS)
    payment_request_through_seymy = models.BooleanField(default=False)
    payment_request_type = models.CharField(
        max_length=16,
        choices=PaymentRequestType.choices,
        default=PaymentRequestType.NONE,
    )
    requested_payment_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Optional scheduling expectations (not guarantees); also stored in snapshot
    job_start_date = models.DateField(null=True, blank=True)
    expected_completion_date = models.DateField(null=True, blank=True)

    # Once True, this row is permanently frozen (accepted by customer).
    is_accepted = models.BooleanField(default=False)

    class Meta:
        ordering = ['estimate_id', 'version_number']
        constraints = [
            models.UniqueConstraint(
                fields=['estimate', 'version_number'],
                name='uniq_estimate_version_number',
            ),
        ]

    def __str__(self):
        return f'{self.estimate} v{self.version_number}'

    def save(self, *args, **kwargs):
        if self.pk:
            previous = EstimateVersion.objects.filter(pk=self.pk).first()
            if previous and previous.is_accepted:
                # Never silently mutate an accepted version's contractual snapshot.
                protected = (
                    'snapshot',
                    'terms_version',
                    'terms_text',
                    'total_amount',
                    'deposit_amount',
                    'balance_amount',
                    'deposit_enabled',
                    'payment_request_through_seymy',
                    'payment_request_type',
                    'requested_payment_amount',
                    'job_start_date',
                    'expected_completion_date',
                    'version_number',
                    'document_version',
                    'estimate_id',
                )
                for field in protected:
                    setattr(self, field, getattr(previous, field))
        super().save(*args, **kwargs)


class EstimateAcceptance(models.Model):
    """
    Record of customer acceptance.

    For MVP, acceptance_method is typically deposit_payment when
    acceptance_requirement is payment. Signature fields are reserved
    for future electronic-signature flows.
    """

    estimate = models.ForeignKey(Estimate, on_delete=models.CASCADE, related_name='acceptances')
    estimate_version = models.ForeignKey(
        EstimateVersion,
        on_delete=models.PROTECT,
        related_name='acceptances',
    )
    estimate_version_number = models.PositiveIntegerField()
    document_version = models.UUIDField()
    terms_version = models.CharField(max_length=64)
    terms_text_snapshot = models.TextField(blank=True, default='')

    accepted_at = models.DateTimeField(default=timezone.now)
    accepted_amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_intent_id = models.CharField(max_length=255, unique=True)
    acceptance_method = models.CharField(
        max_length=32,
        choices=AcceptanceMethod.choices,
        default=AcceptanceMethod.DEPOSIT_PAYMENT,
    )
    acceptance_requirement_at_time = models.CharField(
        max_length=32,
        choices=AcceptanceRequirement.choices,
        default=AcceptanceRequirement.PAYMENT,
    )

    # Future electronic signature — unused in MVP
    signed_at = models.DateTimeField(null=True, blank=True)
    signature_provider = models.CharField(max_length=64, blank=True, default='')
    signature_reference = models.CharField(max_length=255, blank=True, default='')
    signed_document_version = models.CharField(max_length=64, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-accepted_at']
        constraints = [
            models.UniqueConstraint(
                fields=['estimate_version'],
                name='uniq_acceptance_per_estimate_version',
            ),
        ]

    def __str__(self):
        return f'Acceptance {self.payment_intent_id}'


class StripeWebhookEvent(models.Model):
    """Idempotency ledger for Stripe webhook deliveries."""

    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=128)
    processed_at = models.DateTimeField(auto_now_add=True)
    payment_intent_id = models.CharField(max_length=255, blank=True, default='')

    def __str__(self):
        return self.event_id


class PendingCheckout(models.Model):
    """Tracks a Stripe PaymentIntent / Checkout Session awaiting confirmation."""

    estimate = models.ForeignKey(Estimate, on_delete=models.CASCADE, related_name='checkouts')
    estimate_version = models.ForeignKey(EstimateVersion, on_delete=models.CASCADE, related_name='checkouts')
    payment_intent_id = models.CharField(max_length=255, unique=True)
    checkout_session_id = models.CharField(max_length=255, blank=True, default='')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    # 1% Seymy platform fee — only on Seymy-processed payments (not card pass-through)
    seymy_platform_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_request_type = models.CharField(
        max_length=16,
        choices=PaymentRequestType.choices,
        default=PaymentRequestType.NONE,
        blank=True,
    )
    agreed_to_terms = models.BooleanField(default=False)
    status = models.CharField(max_length=32, default='pending')  # pending | succeeded | failed
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class OutboundCustomerMessage(models.Model):
    """
    Demo/MVP log of outbound customer SMS (initial send + deposit reminders).

    Real SMS providers are not wired yet — rows record intent, body, and channel
    the same way session delivery helpers do for the funnel DONE screen.
    """

    PURPOSE_INITIAL = 'initial_send'
    PURPOSE_REMINDER_DAY2 = 'deposit_reminder_day2'
    PURPOSE_REMINDER_DAY5 = 'deposit_reminder_day5'
    PURPOSE_REMINDER_MANUAL = 'deposit_reminder_manual'

    PURPOSE_CHOICES = (
        (PURPOSE_INITIAL, 'Initial send'),
        (PURPOSE_REMINDER_DAY2, 'Deposit reminder day +2'),
        (PURPOSE_REMINDER_DAY5, 'Deposit reminder day +5'),
        (PURPOSE_REMINDER_MANUAL, 'Manual deposit reminder'),
    )

    estimate = models.ForeignKey(
        Estimate, on_delete=models.CASCADE, related_name='outbound_messages'
    )
    purpose = models.CharField(max_length=32, choices=PURPOSE_CHOICES, db_index=True)
    channel = models.CharField(max_length=16, default='sms')
    to_detail = models.CharField(max_length=255, blank=True, default='')
    body = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.purpose} → estimate {self.estimate_id}'
