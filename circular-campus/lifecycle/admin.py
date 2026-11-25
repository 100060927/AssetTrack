from django.contrib import admin

from core.admin_tools import ReadOnlyAdmin

from .models import EndOfLifeProposal, MaintenanceTask


@admin.register(MaintenanceTask)
class MaintenanceTaskAdmin(ReadOnlyAdmin):
    list_display = ('code', 'asset', 'reported_by', 'unsafe', 'state', 'outcome', 'repair_cost', 'created_at')
    list_filter = ('state', 'unsafe', 'outcome')


@admin.register(EndOfLifeProposal)
class EndOfLifeProposalAdmin(ReadOnlyAdmin):
    list_display = ('code', 'asset', 'action', 'state', 'proposed_by', 'decided_by', 'recipient', 'evidence')
    list_filter = ('state', 'action')
