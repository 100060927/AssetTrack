from django.conf import settings
from django.db import models
from django.utils import timezone

from core.errors import Conflict, DomainError, IllegalTransition


class Category(models.Model):
    name = models.CharField(max_length=80, unique=True)
    # EPA WARM factors in metric tonnes CO2e per US short ton (SRS 6.2).
    # Null means no approved factor: the carbon figure is "not available".
    warm_source_reduction = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    warm_landfill = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    factor_source = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'categories'

    def __str__(self):
        return self.name


class Asset(models.Model):
    class State(models.TextChoices):
        RECORDED = 'RECORDED', 'Recorded'
        AVAILABLE = 'AVAILABLE', 'Available'
        HELD = 'HELD', 'Held'
        IN_TRANSIT = 'IN_TRANSIT', 'In transit'
        IN_USE = 'IN_USE', 'In use'
        BLOCKED = 'BLOCKED', 'Blocked unsafe'
        TERMINAL = 'TERMINAL', 'Terminal'

    class Condition(models.TextChoices):
        NEW = 'NEW', 'New'
        GOOD = 'GOOD', 'Good'
        FAIR = 'FAIR', 'Fair'
        POOR = 'POOR', 'Poor'

    CONDITION_RANK = {'POOR': 1, 'FAIR': 2, 'GOOD': 3, 'NEW': 4}

    # The only moves the life cycle allows (Phase 5, Figure 33). Anything else
    # raises IllegalTransition. In use -> Available is the "declare surplus"
    # move that closes the circle.
    ALLOWED_TRANSITIONS = {
        State.RECORDED: {State.AVAILABLE, State.BLOCKED, State.TERMINAL},
        State.AVAILABLE: {State.RECORDED, State.HELD, State.BLOCKED, State.TERMINAL},
        State.HELD: {State.AVAILABLE, State.IN_TRANSIT},
        # In transit -> Available: returned after a dispute; -> Terminal: lost (UC-19)
        State.IN_TRANSIT: {State.IN_USE, State.AVAILABLE, State.TERMINAL},
        State.IN_USE: {State.AVAILABLE, State.BLOCKED, State.TERMINAL},
        # Safety clearance restores the stage the item was in (UC-09)
        State.BLOCKED: {State.RECORDED, State.AVAILABLE, State.IN_USE, State.TERMINAL},
        State.TERMINAL: set(),
    }

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='assets')
    condition = models.CharField(max_length=5, choices=Condition.choices)
    department = models.ForeignKey('accounts.Department', on_delete=models.PROTECT, related_name='assets')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='owned_assets')
    location = models.CharField(max_length=120, blank=True)
    tags = models.CharField(max_length=200, blank=True, help_text='Comma-separated keywords')
    quantity = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=10, choices=State.choices, default=State.RECORDED)
    replacement_unit_cost = models.DecimalField(
        'Replacement cost per unit (AED)', max_digits=10, decimal_places=2, null=True, blank=True)
    unit_mass_kg = models.DecimalField('Mass per unit (kg)', max_digits=8, decimal_places=2, null=True, blank=True)
    # Set when a group was split because part of it was reserved.
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='splits')
    is_fictional = models.BooleanField(default=False)  # DB-5
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f'{self.tag} {self.name}'

    @property
    def tag(self):
        return f'AST-{self.pk:05d}' if self.pk else 'AST-new'

    def can_move_to(self, to_state):
        return to_state in self.ALLOWED_TRANSITIONS[self.state]

    def transition(self, to_state, actor, action, note='', **changes):
        """Move to `to_state`, or refuse.

        The update is a compare-and-swap on the current state, so two people
        acting on the same asset at once cannot both succeed.
        """
        if not self.can_move_to(to_state):
            raise IllegalTransition(
                f'{self.tag} cannot move from {self.get_state_display()} '
                f'to {self.State(to_state).label}.')
        updated = Asset.objects.filter(pk=self.pk, state=self.state).update(
            state=to_state, updated_at=timezone.now(), **changes)
        if not updated:
            raise Conflict(f'{self.tag} was changed by someone else. Please reload.')
        from_state = self.state
        self.state = to_state
        for field, value in changes.items():
            setattr(self, field, value)
        return AssetEvent.objects.create(
            asset=self, actor=actor, action=action,
            from_state=from_state, to_state=to_state, note=note)

    def meets_condition(self, minimum):
        return self.CONDITION_RANK[self.condition] >= self.CONDITION_RANK[minimum]

    def eligible_for(self, request):
        """Class-diagram operation Asset.eligibleFor(request): the hard rules."""
        return (
            self.state == self.State.AVAILABLE
            and self.quantity > 0
            and self.department_id != request.department_id
            and (request.category_id is None or self.category_id == request.category_id)
            and self.meets_condition(request.min_condition)
        )


class AssetEvent(models.Model):
    """Life-cycle history: who did what, and when (C6). Append-only (BR-12)."""

    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name='events')
    # Empty only for automatic system actions, such as an expired hold (UC-18).
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    action = models.CharField(max_length=40)
    from_state = models.CharField(max_length=10, blank=True)
    to_state = models.CharField(max_length=10, blank=True)
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def save(self, *args, **kwargs):
        if self.pk:
            raise DomainError('History entries are never changed.')
        super().save(*args, **kwargs)
        from audit.services import audit
        change = f'{self.from_label} → {self.to_label}' if self.from_state and self.from_state != self.to_state else ''
        audit(self.actor, self.action, self.asset, '; '.join(x for x in (change, self.note) if x))

    def delete(self, *args, **kwargs):
        raise DomainError('History entries are never deleted.')

    def _label(self, value):
        return Asset.State(value).label if value else ''

    @property
    def from_label(self):
        return self._label(self.from_state)

    @property
    def to_label(self):
        return self._label(self.to_state)
