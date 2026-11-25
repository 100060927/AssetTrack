from django.contrib import admin

from core.admin_tools import ReadOnlyAdmin

from .models import Asset, AssetEvent, Category


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'warm_source_reduction', 'warm_landfill', 'factor_source')


@admin.register(Asset)
class AssetAdmin(ReadOnlyAdmin):
    # Items change only through the services, so every change is in the history.
    list_display = ('tag', 'name', 'category', 'condition', 'department', 'owner', 'state', 'quantity')
    list_filter = ('state', 'category', 'department')
    search_fields = ('name', 'description', 'tags')


@admin.register(AssetEvent)
class AssetEventAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'asset', 'actor', 'action', 'from_state', 'to_state', 'note')
    list_filter = ('action',)
