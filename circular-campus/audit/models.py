from django.conf import settings
from django.db import models

from core.errors import DomainError


class AuditEntry(models.Model):
    """UC-26: one line per change anywhere in the system. Append-only (BR-12)."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    action = models.CharField(max_length=60)
    record_type = models.CharField(max_length=40, db_index=True)
    record_label = models.CharField(max_length=160)
    details = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        verbose_name_plural = 'audit entries'

    def save(self, *args, **kwargs):
        if self.pk:
            raise DomainError('Audit entries are never changed.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise DomainError('Audit entries are never deleted.')
