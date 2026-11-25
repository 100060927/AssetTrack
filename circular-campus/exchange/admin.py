from django.contrib import admin

from core.admin_tools import ReadOnlyAdmin

from .models import ApprovalCase, ApprovalDecision, Reservation, ResourceRequest, Transfer


@admin.register(ResourceRequest)
class ResourceRequestAdmin(ReadOnlyAdmin):
    list_display = ('code', 'title', 'requester', 'department', 'quantity', 'state', 'created_at')
    list_filter = ('state', 'department')


@admin.register(Reservation)
class ReservationAdmin(ReadOnlyAdmin):
    list_display = ('id', 'request', 'asset', 'quantity', 'state', 'expires_at')
    list_filter = ('state',)


@admin.register(ApprovalCase)
class ApprovalCaseAdmin(ReadOnlyAdmin):
    list_display = ('id', 'reservation', 'state', 'created_at')
    list_filter = ('state',)


@admin.register(ApprovalDecision)
class ApprovalDecisionAdmin(ReadOnlyAdmin):
    list_display = ('id', 'case', 'approver', 'result', 'reason', 'created_at')


@admin.register(Transfer)
class TransferAdmin(ReadOnlyAdmin):
    list_display = ('code', 'from_department', 'to_department', 'state', 'dispatched_at', 'received_at')
    list_filter = ('state',)
