from django.contrib import admin

from core.admin_tools import ReadOnlyAdmin

from .models import Advice, AIRun, Recommendation


@admin.register(AIRun)
class AIRunAdmin(ReadOnlyAdmin):
    list_display = ('id', 'task', 'adapter', 'model_id', 'created_by', 'created_at', 'fallback_reason')
    list_filter = ('task', 'adapter')


@admin.register(Recommendation)
class RecommendationAdmin(ReadOnlyAdmin):
    list_display = ('id', 'request', 'asset', 'rank', 'score', 'reason', 'decision')


@admin.register(Advice)
class AdviceAdmin(ReadOnlyAdmin):
    list_display = ('id', 'asset', 'action', 'decision', 'decided_by', 'created_at')
