from django.contrib import admin

from core.admin_tools import ReadOnlyAdmin

from .models import AuditEntry


@admin.register(AuditEntry)
class AuditEntryAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'actor', 'action', 'record_type', 'record_label', 'details')
    list_filter = ('record_type', 'action')
    search_fields = ('record_label', 'details', 'action')
