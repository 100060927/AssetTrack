from django.contrib.auth.models import AbstractUser
from django.db import models


class Department(models.Model):
    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=100)
    campus = models.CharField(max_length=60, default='Main Campus')
    building = models.CharField(max_length=60, blank=True)
    active = models.BooleanField(default=True)  # DB-3: deactivate, never delete

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class User(AbstractUser):
    class Role(models.TextChoices):
        REQUESTER = 'REQUESTER', 'Requester'
        CUSTODIAN = 'CUSTODIAN', 'Asset custodian'
        APPROVER = 'APPROVER', 'Approver (asset officer)'
        MAINTENANCE = 'MAINTENANCE', 'Maintenance staff'
        ADMIN = 'ADMIN', 'System administrator'

    role = models.CharField(max_length=12, choices=Role.choices, default=Role.REQUESTER)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.PROTECT, related_name='users'
    )

    def can(self, action, record=None):
        """Class-diagram operation User.can(action, record)."""
        from .permissions import can
        return can(self, action, record)

    @property
    def display_name(self):
        return self.get_full_name() or self.username

    def __str__(self):
        return f'{self.display_name} ({self.get_role_display()})'
