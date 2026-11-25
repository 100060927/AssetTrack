from django.contrib import admin


class ReadOnlyAdmin(admin.ModelAdmin):
    """Lets the administrator look at a table without changing it.

    Workflow records change only through the services, so the life-cycle
    rules and the history always apply.
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
