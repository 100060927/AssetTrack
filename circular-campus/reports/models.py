from django.conf import settings
from django.db import models


class ReportDraft(models.Model):
    """AI feature 5: numbers come from the database snapshot; the AI writes
    the narrative; an administrator edits it before publishing."""

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        PUBLISHED = 'PUBLISHED', 'Published'

    period_start = models.DateField()
    period_end = models.DateField()
    snapshot = models.JSONField()          # verified figures, frozen at generation
    ai_run = models.ForeignKey('ai.AIRun', on_delete=models.PROTECT, related_name='+')
    ai_text = models.TextField()           # the original AI wording, kept for audit
    text = models.TextField()              # the wording after human review
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
