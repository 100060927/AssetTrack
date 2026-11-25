from django.conf import settings
from django.db import models


class MaintenanceTask(models.Model):
    """UC-21 report a defect, UC-09 inspection and repair."""

    class State(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        CLOSED = 'CLOSED', 'Closed'

    class Outcome(models.TextChoices):
        SAFE = 'SAFE', 'Safe: returned to service'
        BEYOND = 'BEYOND', 'Beyond repair'
        NO_FAULT = 'NO_FAULT', 'No fault found'

    asset = models.ForeignKey('catalog.Asset', on_delete=models.PROTECT, related_name='maintenance_tasks')
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    description = models.TextField()
    unsafe = models.BooleanField(default=False)
    blocked_from = models.CharField(max_length=10, blank=True)  # stage to restore on clearance
    state = models.CharField(max_length=6, choices=State.choices, default=State.OPEN)
    findings = models.TextField(blank=True)
    repair_done = models.TextField(blank=True)
    repair_cost = models.DecimalField('Repair cost (AED)', max_digits=10, decimal_places=2, null=True, blank=True)
    outcome = models.CharField(max_length=8, choices=Outcome.choices, blank=True)
    inspected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    @property
    def code(self):
        return f'MNT-{self.pk:05d}'


class EndOfLifeProposal(models.Model):
    """UC-20: propose, independently approve, then record completion with evidence."""

    class Action(models.TextChoices):
        DONATE = 'DONATE', 'Donate'
        RECYCLE = 'RECYCLE', 'Recycle'
        DISPOSE = 'DISPOSE', 'Dispose'

    class State(models.TextChoices):
        PROPOSED = 'PROPOSED', 'Proposed'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        WITHDRAWN = 'WITHDRAWN', 'Withdrawn'
        COMPLETED = 'COMPLETED', 'Completed'

    asset = models.ForeignKey('catalog.Asset', on_delete=models.PROTECT, related_name='eol_proposals')
    action = models.CharField(max_length=8, choices=Action.choices)
    reason = models.TextField('Why this action, and why not reuse or repair')
    state = models.CharField(max_length=10, choices=State.choices, default=State.PROPOSED)
    proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    decision_note = models.CharField(max_length=300, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    recipient = models.CharField(max_length=120, blank=True)
    evidence = models.CharField('Completion evidence', max_length=300, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    @property
    def code(self):
        return f'EOL-{self.pk:05d}'
