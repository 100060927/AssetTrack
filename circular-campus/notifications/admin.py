from django.contrib import admin

from core.admin_tools import ReadOnlyAdmin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'recipient', 'message', 'read')
    list_filter = ('read',)
