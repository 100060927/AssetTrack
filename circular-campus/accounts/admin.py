from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Department, User


@admin.register(User)
class CampusUserAdmin(UserAdmin):
    list_display = ('username', 'first_name', 'last_name', 'role', 'department', 'is_active')
    list_filter = ('role', 'department', 'is_active')
    fieldsets = UserAdmin.fieldsets + (('Campus', {'fields': ('role', 'department')}),)
    add_fieldsets = UserAdmin.add_fieldsets + (('Campus', {'fields': ('role', 'department')}),)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'campus', 'building', 'active')
