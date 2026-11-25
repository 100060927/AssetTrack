from django.conf import settings
from django.db import models
from django.db.models import Sum

from catalog.models import Asset, Category
from core.errors import DomainError, IllegalTransition


class ResourceRequest(models.Model):
    class State(models.TextChoices):
        SUBMITTED = 'SUBMITTED', 'Submitted'
        UNDER_REVIEW = 'UNDER_REVIEW', 'Under review'
        APPROVED = 'APPROVED', 'Approved'
        PART_FULFILLED = 'PART_FULFILLED', 'Part fulfilled'
        FULFILLED = 'FULFILLED', 'Fulfilled'
        REJECTED = 'REJECTED', 'Rejected'
        CANCELLED = 'CANCELLED', 'Cancelled'

    # Phase 5, Figure 34.
    ALLOWED_TRANSITIONS = {
        State.SUBMITTED: {State.UNDER_REVIEW, State.CANCELLED},
        State.UNDER_REVIEW: {State.SUBMITTED, State.APPROVED, State.REJECTED, State.CANCELLED},
        State.APPROVED: {State.SUBMITTED, State.PART_FULFILLED, State.FULFILLED, State.CANCELLED},
        State.PART_FULFILLED: {State.FULFILLED, State.CANCELLED},
        State.FULFILLED: set(),
        State.REJECTED: set(),
        State.CANCELLED: set(),
    }
    OPEN_FOR_MATCHING = {State.SUBMITTED, State.PART_FULFILLED}

    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='requests')
    department = models.ForeignKey('accounts.Department', on_delete=models.PROTECT, related_name='requests')
    title = models.CharField('What do you need?', max_length=120)
    purpose = models.TextField('Purpose and details', blank=True)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    min_condition = models.CharField(
        'Minimum condition', max_length=5, choices=Asset.Condition.choices, default=Asset.Condition.FAIR)
    required_date = models.DateField(null=True, blank=True)
    state = models.CharField(max_length=14, choices=State.choices, default=State.SUBMITTED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f'{self.code} {self.title}'

    @property
    def code(self):
        return f'REQ-{self.pk:05d}'

    def move_to(self, to_state):
        if to_state not in self.ALLOWED_TRANSITIONS[self.state]:
            raise IllegalTransition(
                f'{self.code} cannot move from {self.get_state_display()} '
                f'to {self.State(to_state).label}.')
        self.state = to_state
        self.save(update_fields=['state', 'updated_at'])

    @property
    def received_quantity(self):
        total = Transfer.objects.filter(
            reservation__request=self, state=Transfer.State.COMPLETED
        ).aggregate(total=Sum('received_qty'))['total']
        return total or 0

    @property
    def active_reservation(self):
        return self.reservations.filter(state=Reservation.State.ACTIVE).first()

    def remaining_quantity(self):
        """Units still needed: not yet received and not currently reserved."""
        reserved = self.reservations.filter(
            state=Reservation.State.ACTIVE).aggregate(total=Sum('quantity'))['total'] or 0
        return max(self.quantity - self.received_quantity - reserved, 0)

    @property
    def is_open_for_matching(self):
        return (self.state in self.OPEN_FOR_MATCHING
                and self.active_reservation is None
                and self.remaining_quantity() > 0)


class Reservation(models.Model):
    """Joins a request to an eligible asset quantity (the hold)."""

    class State(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        RELEASED = 'RELEASED', 'Released'
        FULFILLED = 'FULFILLED', 'Fulfilled'

    request = models.ForeignKey(ResourceRequest, on_delete=models.PROTECT, related_name='reservations')
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name='reservations')
    quantity = models.PositiveIntegerField()
    state = models.CharField(max_length=10, choices=State.choices, default=State.ACTIVE)
    expires_at = models.DateTimeField()
    expiry_warned = models.BooleanField(default=False)  # UC-18: warned once
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f'Reservation {self.pk}: {self.quantity} x {self.asset}'


class ApprovalCase(models.Model):
    class State(models.TextChoices):
        UNDER_REVIEW = 'UNDER_REVIEW', 'Under review'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        WITHDRAWN = 'WITHDRAWN', 'Withdrawn'

    reservation = models.OneToOneField(Reservation, on_delete=models.PROTECT, related_name='approval_case')
    stage = models.PositiveSmallIntegerField(default=1)
    policy_version = models.CharField(max_length=20, default='single-stage-v1')
    state = models.CharField(max_length=12, choices=State.choices, default=State.UNDER_REVIEW)
    reminded = models.BooleanField(default=False)  # UC-18: officers reminded once
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def ready_for_execution(self):
        return self.state == self.State.APPROVED


class ApprovalDecision(models.Model):
    class Result(models.TextChoices):
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'

    case = models.ForeignKey(ApprovalCase, on_delete=models.PROTECT, related_name='decisions')
    approver = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='decisions')
    result = models.CharField(max_length=8, choices=Result.choices)
    reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def validate_authority(self):
        """BR-3: nobody decides on their own request."""
        if not self.approver.can('approval.decide', self.case):
            raise DomainError('You cannot decide this request. Self-approval is not allowed.')


class Transfer(models.Model):
    class State(models.TextChoices):
        READY = 'READY', 'Ready for dispatch'
        IN_TRANSIT = 'IN_TRANSIT', 'In transit'
        COMPLETED = 'COMPLETED', 'Completed'
        CANCELLED = 'CANCELLED', 'Cancelled'
        DISPUTED = 'DISPUTED', 'Disputed'      # UC-19
        RETURNED = 'RETURNED', 'Returned'
        LOST = 'LOST', 'Lost (written off)'

    class Problem(models.TextChoices):
        WRONG = 'WRONG', 'Wrong item'
        DAMAGED = 'DAMAGED', 'Arrived damaged'
        MISSING = 'MISSING', 'Missing or short quantity'

    class Resolution(models.TextChoices):
        RECEIVED = 'RECEIVED', 'Confirmed received after all'
        RETURNED = 'RETURNED', 'Returned to the sender'
        LOST = 'LOST', 'Lost: written off'

    reservation = models.OneToOneField(Reservation, on_delete=models.PROTECT, related_name='transfer')
    state = models.CharField(max_length=10, choices=State.choices, default=State.READY)
    from_department = models.ForeignKey('accounts.Department', on_delete=models.PROTECT, related_name='+')
    to_department = models.ForeignKey('accounts.Department', on_delete=models.PROTECT, related_name='+')
    dispatched_qty = models.PositiveIntegerField(default=0)
    received_qty = models.PositiveIntegerField(default=0)
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    dispatched_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name='received_transfers')
    received_at = models.DateTimeField(null=True, blank=True)
    problem = models.CharField(max_length=8, choices=Problem.choices, blank=True)
    problem_note = models.CharField(max_length=300, blank=True)
    resolution = models.CharField(max_length=8, choices=Resolution.choices, blank=True)
    resolution_note = models.CharField(max_length=300, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    @property
    def code(self):
        return f'TRF-{self.pk:05d}'
