from django.conf import settings
from django.db import models


class AIRun(models.Model):
    """One call through the AI gateway, kept as an immutable snapshot (AIG-5)."""

    class Task(models.TextChoices):
        CLASSIFY = 'CLASSIFY', 'Classification and tagging'
        RANK = 'RANK', 'Matching and ranking'
        ADVISE = 'ADVISE', 'Sustainable-action advice'
        CHAT = 'CHAT', 'Assistant'
        REPORT = 'REPORT', 'Report summary'

    task = models.CharField(max_length=10, choices=Task.choices)
    adapter = models.CharField(max_length=20)       # "claude" or "classic"
    model_id = models.CharField(max_length=60)
    config_version = models.CharField(max_length=20)
    input_snapshot = models.JSONField()
    output = models.JSONField()
    fallback_reason = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    @property
    def used_fallback(self):
        return bool(self.fallback_reason)


class Recommendation(models.Model):
    """One ranked surplus item offered for a request. A person decides (BR-13)."""

    class Decision(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        ACCEPTED = 'ACCEPTED', 'Accepted'
        REJECTED = 'REJECTED', 'Rejected'

    ai_run = models.ForeignKey(AIRun, on_delete=models.PROTECT, related_name='recommendations')
    request = models.ForeignKey('exchange.ResourceRequest', on_delete=models.PROTECT, related_name='recommendations')
    asset = models.ForeignKey('catalog.Asset', on_delete=models.PROTECT, related_name='recommendations')
    rank = models.PositiveSmallIntegerField()
    score = models.DecimalField(max_digits=5, decimal_places=1)
    coverage = models.DecimalField(max_digits=4, decimal_places=2)  # share of weights with evidence
    reason = models.CharField(max_length=300)
    factors = models.JSONField()
    decision = models.CharField(max_length=8, choices=Decision.choices, default=Decision.PENDING)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['rank']


class Advice(models.Model):
    """AI feature 3: a suggested sustainable action. The custodian decides."""

    class Action(models.TextChoices):
        REUSE = 'REUSE', 'Reuse'
        REPAIR = 'REPAIR', 'Repair'
        DONATE = 'DONATE', 'Donate'
        RECYCLE = 'RECYCLE', 'Recycle'
        DISPOSE = 'DISPOSE', 'Dispose'

    class Decision(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        ACCEPTED = 'ACCEPTED', 'Accepted'
        DISMISSED = 'DISMISSED', 'Dismissed'

    asset = models.ForeignKey('catalog.Asset', on_delete=models.PROTECT, related_name='advice')
    ai_run = models.ForeignKey(AIRun, on_delete=models.PROTECT, related_name='+')
    action = models.CharField(max_length=8, choices=Action.choices)
    reasons = models.JSONField()
    impact = models.JSONField()   # calculated by our own formulas, never by the AI
    decision = models.CharField(max_length=9, choices=Decision.choices, default=Decision.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
