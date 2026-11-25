from django.contrib import admin

from core.admin_tools import ReadOnlyAdmin

from .models import ReportDraft


@admin.register(ReportDraft)
class ReportDraftAdmin(ReadOnlyAdmin):
    list_display = ('id', 'period_start', 'period_end', 'status', 'created_by', 'published_by')
