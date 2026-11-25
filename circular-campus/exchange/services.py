from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from accounts.permissions import require
from audit.services import audit
from catalog.models import Asset, AssetEvent
from catalog.services import CatalogService
from core.errors import Conflict, DomainError
from notifications.services import NotificationService

from .models import ApprovalCase, ApprovalDecision, Reservation, ResourceRequest, Transfer


def _request_url(resource_request):
    return reverse('request_detail', args=[resource_request.pk])


class ExchangeService:
    """Modules 3.0 and 4.0: requests, holds, approvals and transfers.

    Every method is one database transaction, so stock, custody, history and
    notifications change together or not at all (REL-1, REL-6).
    """

    # --- Open needs (UC-05 exception, UC-18 step 5) ---------------------------

    @staticmethod
    def open_needs():
        """Requests still waiting for an item: nothing is held for them yet."""
        candidates = ResourceRequest.objects.filter(
            state__in=ResourceRequest.OPEN_FOR_MATCHING
        ).select_related('department', 'category')
        return [r for r in candidates if r.is_open_for_matching]

    @staticmethod
    def alert_open_needs(asset):
        """When an item is listed, tell everyone whose open request it could fill."""
        for resource_request in ExchangeService.open_needs():
            if asset.eligible_for(resource_request):
                NotificationService.notify(
                    [resource_request.requester],
                    f'A newly listed item may fit {resource_request.code}: {asset.name} '
                    f'from {asset.department.name}. Open your request to find matches.',
                    _request_url(resource_request))

    # --- Requests (C4) -------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def submit(actor, **data):
        require(actor, 'request.create')
        resource_request = ResourceRequest.objects.create(
            requester=actor, department=actor.department, **data)
        audit(actor, 'Request submitted', resource_request,
              f'{resource_request.quantity} x {resource_request.title}')
        NotificationService.notify(
            [actor], f'Your request {resource_request.code} was submitted.',
            _request_url(resource_request))
        return resource_request

    @staticmethod
    @transaction.atomic
    def reserve(resource_request, asset_id, quantity, actor):
        """Hold stock for a request and open its approval case.

        The asset row is locked and then moved with a guarded update, so the
        last item cannot go to two people (C5, BR-1).
        """
        require(actor, 'request.own', resource_request)
        if not resource_request.is_open_for_matching:
            raise DomainError('This request cannot take another item right now.')
        if quantity < 1 or quantity > resource_request.remaining_quantity():
            raise DomainError('The quantity is more than this request still needs.')

        asset = Asset.objects.select_for_update().get(pk=asset_id)
        if asset.state != Asset.State.AVAILABLE or asset.quantity < quantity:
            raise Conflict(f'{asset.tag} is no longer available in that quantity.')
        if not asset.eligible_for(resource_request):
            raise DomainError(f'{asset.tag} does not meet this request\'s minimum requirements.')

        held = asset if quantity == asset.quantity else CatalogService.split(asset, quantity, actor)
        held.transition(Asset.State.HELD, actor, 'Reserved', f'For {resource_request.code}')

        reservation = Reservation.objects.create(
            request=resource_request, asset=held, quantity=quantity,
            expires_at=timezone.now() + timedelta(days=settings.RESERVATION_HOLD_DAYS))
        ApprovalCase.objects.create(reservation=reservation)
        if resource_request.state == ResourceRequest.State.SUBMITTED:
            resource_request.move_to(ResourceRequest.State.UNDER_REVIEW)

        approvers = User.objects.filter(role=User.Role.APPROVER, is_active=True).exclude(pk=actor.pk)
        NotificationService.notify(
            approvers, f'{resource_request.code} needs approval: {quantity} x {held.name}.',
            reverse('approval_queue'))
        NotificationService.notify(
            [held.owner], f'{held.tag} was reserved for {resource_request.code}.',
            reverse('asset_detail', args=[held.pk]))
        return reservation

    @staticmethod
    @transaction.atomic
    def cancel(resource_request, actor):
        require(actor, 'request.own', resource_request)
        reservation = resource_request.active_reservation
        if reservation:
            transfer = getattr(reservation, 'transfer', None)
            if transfer and transfer.state == Transfer.State.IN_TRANSIT:
                raise DomainError('Items are in transit. Confirm receipt before cancelling.')
            if transfer and transfer.state == Transfer.State.DISPUTED:
                raise DomainError('A receipt problem is open. An officer must resolve it first.')
            ExchangeService._release(reservation, actor, 'Request cancelled')
            if transfer:
                transfer.state = Transfer.State.CANCELLED
                transfer.save(update_fields=['state'])
            case = reservation.approval_case
            if case.state == ApprovalCase.State.UNDER_REVIEW:
                case.state = ApprovalCase.State.WITHDRAWN
                case.save(update_fields=['state'])
        resource_request.move_to(ResourceRequest.State.CANCELLED)
        audit(actor, 'Request cancelled', resource_request)
        return resource_request

    @staticmethod
    def _release(reservation, actor, note):
        """BR-7: release undisbursed quantity, once."""
        updated = Reservation.objects.filter(
            pk=reservation.pk, state=Reservation.State.ACTIVE
        ).update(state=Reservation.State.RELEASED)
        if not updated:
            raise Conflict('This reservation was already released.')
        reservation.state = Reservation.State.RELEASED
        asset = reservation.asset
        asset.transition(Asset.State.AVAILABLE, actor, 'Hold released', note)
        from lifecycle.services import LifeCycleService
        LifeCycleService.apply_pending_block(asset, actor)

    # --- Approvals (C5) ------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def decide(case, approver, approve, reason=''):
        decision = ApprovalDecision(
            case=case, approver=approver, reason=reason,
            result=ApprovalDecision.Result.APPROVED if approve else ApprovalDecision.Result.REJECTED)
        decision.validate_authority()
        if not approve and not reason.strip():
            raise DomainError('A rejection needs a reason.')
        new_state = ApprovalCase.State.APPROVED if approve else ApprovalCase.State.REJECTED
        updated = ApprovalCase.objects.filter(
            pk=case.pk, state=ApprovalCase.State.UNDER_REVIEW).update(state=new_state)
        if not updated:
            raise Conflict('This case was already decided.')
        case.state = new_state
        decision.save()
        audit(approver, 'Approved' if approve else 'Rejected', case.reservation.request,
              f'{case.reservation.quantity} x {case.reservation.asset.tag}'
              + (f'. Reason: {reason}' if reason else ''))

        reservation = case.reservation
        resource_request = reservation.request
        asset = reservation.asset
        if approve:
            Transfer.objects.create(
                reservation=reservation, from_department=asset.department,
                to_department=resource_request.department)
            if resource_request.state == ResourceRequest.State.UNDER_REVIEW:
                resource_request.move_to(ResourceRequest.State.APPROVED)
            NotificationService.notify(
                [resource_request.requester],
                f'{resource_request.code} was approved. {asset.name} will be dispatched.',
                _request_url(resource_request))
            NotificationService.notify(
                [asset.owner], f'{asset.tag} is approved for transfer. Please dispatch it.',
                reverse('transfer_queue'))
        else:
            ExchangeService._release(reservation, approver, f'Rejected: {reason}')
            if resource_request.state == ResourceRequest.State.UNDER_REVIEW:
                resource_request.move_to(ResourceRequest.State.REJECTED)
            NotificationService.notify(
                [resource_request.requester],
                f'{resource_request.code} was rejected: {reason}',
                _request_url(resource_request))
        return decision

    # --- Transfers (C5) ------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def dispatch(transfer, actor):
        require(actor, 'transfer.dispatch', transfer)
        if not transfer.reservation.approval_case.ready_for_execution():
            raise DomainError('This transfer is not approved.')
        from lifecycle.services import LifeCycleService
        if LifeCycleService.has_unsafe_task(transfer.reservation.asset):
            # BR-8: an item reported unsafe never leaves until maintenance clears it.
            raise DomainError('This item has been reported unsafe or judged beyond repair. It cannot be sent until maintenance clears it.')
        updated = Transfer.objects.filter(pk=transfer.pk, state=Transfer.State.READY).update(
            state=Transfer.State.IN_TRANSIT, dispatched_by=actor,
            dispatched_at=timezone.now(), dispatched_qty=transfer.reservation.quantity)
        if not updated:
            raise Conflict('This transfer was already dispatched or cancelled.')
        transfer.refresh_from_db()
        reservation = transfer.reservation
        reservation.asset.transition(
            Asset.State.IN_TRANSIT, actor, 'Dispatched', f'To {transfer.to_department.name}')
        NotificationService.notify(
            [reservation.request.requester],
            f'{reservation.asset.name} is on its way. Confirm receipt when it arrives.',
            _request_url(reservation.request))
        return transfer

    @staticmethod
    @transaction.atomic
    def receive(transfer, actor):
        """The receiver confirms; owner and department change, once (BR-6)."""
        require(actor, 'transfer.receive', transfer)
        updated = Transfer.objects.filter(pk=transfer.pk, state=Transfer.State.IN_TRANSIT).update(
            state=Transfer.State.COMPLETED, received_by=actor,
            received_at=timezone.now(), received_qty=transfer.dispatched_qty)
        transfer.refresh_from_db()
        if not updated:
            if transfer.state == Transfer.State.COMPLETED:
                return transfer  # a repeated click changes nothing
            if transfer.state == Transfer.State.DISPUTED:
                raise DomainError('A problem was reported for this delivery. An officer will resolve it.')
            raise DomainError('This transfer has not been dispatched yet.')
        ExchangeService._finish_receipt(transfer, actor, 'Received')
        return transfer

    @staticmethod
    def _finish_receipt(transfer, actor, action):
        reservation = transfer.reservation
        resource_request = reservation.request
        receiver = resource_request.requester
        asset = reservation.asset
        previous_owner = asset.owner
        asset.transition(
            Asset.State.IN_USE, actor, action,
            f'{transfer.from_department.name} -> {transfer.to_department.name}',
            department=transfer.to_department, owner=receiver)
        reservation.state = Reservation.State.FULFILLED
        reservation.save(update_fields=['state'])

        fully_met = resource_request.received_quantity >= resource_request.quantity
        target = ResourceRequest.State.FULFILLED if fully_met else ResourceRequest.State.PART_FULFILLED
        if resource_request.state != target:
            resource_request.move_to(target)
        from lifecycle.services import LifeCycleService
        LifeCycleService.apply_pending_block(asset, actor)

        approvers = User.objects.filter(role=User.Role.APPROVER, is_active=True)
        NotificationService.notify(
            [previous_owner, *approvers],
            f'{asset.tag} was received by {receiver.display_name} ({transfer.to_department.name}).',
            reverse('asset_detail', args=[asset.pk]))

    # --- UC-19: receipt disputes ---------------------------------------------

    @staticmethod
    @transaction.atomic
    def report_problem(transfer, actor, problem, note):
        """The receiver says the wrong item, a damaged item, or too few arrived."""
        require(actor, 'transfer.dispute', transfer)
        if problem not in Transfer.Problem.values:
            raise DomainError('Choose what went wrong.')
        note = (note or '').strip()
        if not note:
            raise DomainError('Describe the problem.')
        updated = Transfer.objects.filter(pk=transfer.pk, state=Transfer.State.IN_TRANSIT).update(
            state=Transfer.State.DISPUTED, problem=problem, problem_note=note[:300])
        if not updated:
            raise DomainError('Only a delivery that is on its way can be disputed.')
        transfer.refresh_from_db()
        asset = transfer.reservation.asset
        text = f'{transfer.code}: {transfer.get_problem_display()}. {note}'
        AssetEvent.objects.create(asset=asset, actor=actor, action='Receipt disputed',
                                  from_state=asset.state, to_state=asset.state, note=text[:300])
        audit(actor, 'Receipt disputed', transfer, text)
        officers = User.objects.filter(role=User.Role.APPROVER, is_active=True).exclude(pk=actor.pk)
        NotificationService.notify(
            [*officers, asset.owner, *([transfer.dispatched_by] if transfer.dispatched_by else [])],
            f'Delivery problem on {transfer.code} ({asset.tag}): {transfer.get_problem_display()}.',
            reverse('dispute_list'))
        return transfer

    @staticmethod
    @transaction.atomic
    def resolve_dispute(transfer, actor, resolution, note):
        """An independent officer closes the dispute: received, returned, or lost."""
        require(actor, 'dispute.resolve', transfer)
        if resolution not in Transfer.Resolution.values:
            raise DomainError('Choose how the problem was resolved.')
        note = (note or '').strip()
        if not note:
            raise DomainError('Write what was found.')
        final = {
            Transfer.Resolution.RECEIVED: Transfer.State.COMPLETED,
            Transfer.Resolution.RETURNED: Transfer.State.RETURNED,
            Transfer.Resolution.LOST: Transfer.State.LOST,
        }[resolution]
        changes = dict(state=final, resolution=resolution, resolution_note=note[:300],
                       resolved_by=actor, resolved_at=timezone.now())
        if resolution == Transfer.Resolution.RECEIVED:
            changes.update(received_by=transfer.reservation.request.requester,
                           received_at=timezone.now(), received_qty=transfer.dispatched_qty)
        updated = Transfer.objects.filter(pk=transfer.pk, state=Transfer.State.DISPUTED).update(**changes)
        if not updated:
            raise Conflict(f'{transfer.code} has no open dispute.')
        transfer.refresh_from_db()
        reservation = transfer.reservation
        resource_request = reservation.request
        asset = reservation.asset
        text = f'{transfer.code}: {transfer.get_resolution_display()}. {note}'

        if resolution == Transfer.Resolution.RECEIVED:
            ExchangeService._finish_receipt(transfer, actor, 'Received after dispute')
        else:
            Reservation.objects.filter(pk=reservation.pk).update(state=Reservation.State.RELEASED)
            if resolution == Transfer.Resolution.RETURNED:
                asset.transition(Asset.State.AVAILABLE, actor, 'Returned after dispute', text[:300])
                if transfer.problem == Transfer.Problem.DAMAGED:
                    # A damaged item is not relisted until maintenance has looked at it.
                    from lifecycle.services import LifeCycleService
                    LifeCycleService.report_defect(
                        asset, actor, f'Arrived damaged ({transfer.code}): {transfer.problem_note}', unsafe=True)
            else:
                asset.transition(Asset.State.TERMINAL, actor, 'Lost in transit', text[:300])
            # The request needs an item again (unless part of it already arrived).
            if resource_request.state == ResourceRequest.State.APPROVED:
                resource_request.move_to(ResourceRequest.State.SUBMITTED)
        audit(actor, 'Dispute resolved', transfer, text)
        NotificationService.notify(
            [resource_request.requester, asset.owner],
            f'Delivery problem on {transfer.code} resolved: {transfer.get_resolution_display()}.',
            _request_url(resource_request))
        return transfer

    # --- UC-18: holds that expire --------------------------------------------

    @staticmethod
    def _expiring_holds():
        return Reservation.objects.filter(state=Reservation.State.ACTIVE).exclude(
            transfer__state__in=[Transfer.State.IN_TRANSIT, Transfer.State.DISPUTED,
                                 Transfer.State.COMPLETED])

    @staticmethod
    @transaction.atomic
    def _expire_one(reservation_id, now):
        reservation = Reservation.objects.select_for_update().get(pk=reservation_id)
        if reservation.state != Reservation.State.ACTIVE or reservation.expires_at > now:
            return False
        transfer = getattr(reservation, 'transfer', None)
        if transfer and transfer.state != Transfer.State.READY:
            return False  # already moving: the hold is no longer the question
        resource_request = reservation.request
        asset = reservation.asset
        ExchangeService._release(reservation, None, f'Hold expired (for {resource_request.code})')
        if transfer:
            Transfer.objects.filter(pk=transfer.pk).update(state=Transfer.State.CANCELLED)
        case = reservation.approval_case
        if case.state == ApprovalCase.State.UNDER_REVIEW:
            ApprovalCase.objects.filter(pk=case.pk).update(state=ApprovalCase.State.WITHDRAWN)
        if resource_request.state in (ResourceRequest.State.UNDER_REVIEW, ResourceRequest.State.APPROVED):
            resource_request.move_to(ResourceRequest.State.SUBMITTED)
        audit(None, 'Hold expired', resource_request, f'{reservation.quantity} x {asset.tag} released')
        NotificationService.notify(
            [resource_request.requester],
            f'The hold on {asset.name} for {resource_request.code} expired and the item was released. '
            'Your request is open again.', _request_url(resource_request))
        NotificationService.notify(
            [asset.owner], f'The hold on {asset.tag} expired. It is listed as available again.',
            reverse('asset_detail', args=[asset.pk]))
        ExchangeService.alert_open_needs(asset)
        return True

    @staticmethod
    def process_expiry(now=None):
        """UC-18: warn before a hold ends, release expired holds, remind officers.

        Run from the Holds page or `python manage.py process_expiry` (for
        example every hour from Windows Task Scheduler or cron).
        """
        now = now or timezone.now()
        result = {'expired': 0, 'warned': 0, 'reminded': 0}
        for reservation in ExchangeService._expiring_holds().filter(expires_at__lte=now):
            if ExchangeService._expire_one(reservation.pk, now):
                result['expired'] += 1

        approvers = list(User.objects.filter(role=User.Role.APPROVER, is_active=True))
        soon = now + timedelta(hours=settings.RESERVATION_WARNING_HOURS)
        with transaction.atomic():
            for reservation in ExchangeService._expiring_holds().filter(
                    expires_at__gt=now, expires_at__lte=soon, expiry_warned=False
            ).select_related('request__requester', 'asset'):
                if Reservation.objects.filter(pk=reservation.pk, expiry_warned=False).update(expiry_warned=True):
                    NotificationService.notify(
                        [reservation.request.requester, *approvers],
                        f'The hold on {reservation.asset.tag} for {reservation.request.code} ends '
                        f'{timezone.localtime(reservation.expires_at):%d %b %H:%M}. '
                        'Decide, dispatch or extend it before then.', reverse('holds'))
                    result['warned'] += 1
            late = now - timedelta(days=settings.APPROVAL_REMINDER_DAYS)
            for case in ApprovalCase.objects.filter(
                    state=ApprovalCase.State.UNDER_REVIEW, reminded=False, created_at__lte=late
            ).select_related('reservation__request'):
                if ApprovalCase.objects.filter(pk=case.pk, reminded=False).update(reminded=True):
                    code = case.reservation.request.code
                    NotificationService.notify(
                        [a for a in approvers if a.pk != case.reservation.request.requester_id],
                        f'Reminder: {code} has been waiting for approval for more than '
                        f'{settings.APPROVAL_REMINDER_DAYS} days.', reverse('approval_queue'))
                    result['reminded'] += 1
        if any(result.values()):
            audit(None, 'Expiry check', None,
                  f"{result['expired']} released, {result['warned']} warned, {result['reminded']} reminded")
        return result

    @staticmethod
    @transaction.atomic
    def extend_hold(reservation, actor, days, reason=''):
        require(actor, 'hold.extend', reservation)
        if not 1 <= days <= 14:
            raise DomainError('Extend by 1 to 14 days.')
        if not (reason or '').strip():
            raise DomainError('Give a reason for the extension.')
        reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
        if reservation.state != Reservation.State.ACTIVE:
            raise DomainError('This hold is no longer active.')
        reservation.expires_at = max(reservation.expires_at, timezone.now()) + timedelta(days=days)
        reservation.expiry_warned = False
        reservation.save(update_fields=['expires_at', 'expiry_warned'])
        asset = reservation.asset
        note = (f'For {reservation.request.code}, until '
                f'{timezone.localtime(reservation.expires_at):%d %b %Y %H:%M}. {reason.strip()}')
        AssetEvent.objects.create(asset=asset, actor=actor, action='Hold extended',
                                  from_state=asset.state, to_state=asset.state, note=note[:300])
        NotificationService.notify(
            [reservation.request.requester, asset.owner],
            f'The hold on {asset.tag} was extended. {note}'[:300], _request_url(reservation.request))
        return reservation
