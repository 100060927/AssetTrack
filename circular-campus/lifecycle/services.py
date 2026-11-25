"""Module 5.0: defects, inspection and repair (UC-21, UC-09) and end of life (UC-20).

Every public method is one database transaction: the task or proposal, the
item's stage, its history line, the audit line and the messages are saved
together or not at all.
"""
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from accounts.permissions import require
from audit.services import audit
from catalog.models import Asset, AssetEvent
from core.errors import Conflict, DomainError
from notifications.services import NotificationService

from .models import EndOfLifeProposal, MaintenanceTask

# Stages an unsafe item can be pulled out of straight away. Held and In transit
# items are part of an exchange: the block waits until the exchange ends.
BLOCKABLE = {Asset.State.RECORDED, Asset.State.AVAILABLE, Asset.State.IN_USE}
# Stages from which an item can be donated, recycled or disposed of.
EOL_STAGES = {Asset.State.RECORDED, Asset.State.AVAILABLE, Asset.State.IN_USE, Asset.State.BLOCKED}


def _active(role):
    return User.objects.filter(role=role, is_active=True)


def _keepers(asset):
    """The item's owner plus its department's custodians.

    After a hand-over the owner is the person using the item, so the
    department's custodians must be told as well.
    """
    return [asset.owner, *User.objects.filter(
        role=User.Role.CUSTODIAN, department_id=asset.department_id, is_active=True)]


def _asset_url(asset):
    return reverse('asset_detail', args=[asset.pk])


class LifeCycleService:

    # --- UC-21: report a defect ----------------------------------------------

    @staticmethod
    def open_task(asset):
        return asset.maintenance_tasks.filter(state=MaintenanceTask.State.OPEN).first()

    @staticmethod
    def has_unsafe_task(asset):
        """Unsafe and not cleared yet, or judged beyond repair (BR-8)."""
        return (asset.maintenance_tasks.filter(state=MaintenanceTask.State.OPEN, unsafe=True).exists()
                or LifeCycleService._beyond_repair(asset) is not None)

    @staticmethod
    def _beyond_repair(asset):
        """The latest closed inspection, if its verdict was beyond repair."""
        last = asset.maintenance_tasks.filter(
            state=MaintenanceTask.State.CLOSED).exclude(outcome='').order_by('-closed_at', '-id').first()
        return last if last and last.outcome == MaintenanceTask.Outcome.BEYOND else None

    @staticmethod
    def _block(asset, task, actor):
        """Pull an unsafe item out of use and remember where to put it back."""
        task.blocked_from = asset.state
        task.save(update_fields=['blocked_from'])
        asset.transition(Asset.State.BLOCKED, actor, 'Blocked unsafe', f'{task.code}: {task.description[:200]}')

    @staticmethod
    @transaction.atomic
    def report_defect(asset, actor, description, unsafe=False):
        require(actor, 'defect.report', asset)
        description = (description or '').strip()
        if not description:
            raise DomainError('Describe the problem first.')
        asset = Asset.objects.select_for_update().get(pk=asset.pk)
        if asset.state == Asset.State.TERMINAL:
            raise DomainError(f'{asset.tag} has left the campus. Nothing to repair.')

        task = LifeCycleService.open_task(asset)
        if task:
            # A second report on the same item joins the open task (no duplicates).
            task.description += f'\n\nAlso reported by {actor.display_name}: {description}'
            became_unsafe = unsafe and not task.unsafe
            task.unsafe = task.unsafe or unsafe
            task.save(update_fields=['description', 'unsafe'])
            if not became_unsafe:
                AssetEvent.objects.create(
                    asset=asset, actor=actor, action='Defect reported again',
                    from_state=asset.state, to_state=asset.state, note=f'{task.code}: {description[:200]}')
                return task
        else:
            task = MaintenanceTask.objects.create(
                asset=asset, reported_by=actor, description=description, unsafe=unsafe)
            audit(actor, 'Defect reported', task, f'{asset.tag}: {description}')

        if unsafe and asset.state in BLOCKABLE:
            LifeCycleService._block(asset, task, actor)
        else:
            AssetEvent.objects.create(
                asset=asset, actor=actor, action='Defect reported',
                from_state=asset.state, to_state=asset.state,
                note=f'{task.code}{" (unsafe)" if unsafe else ""}: {description[:200]}')

        url = reverse('maintenance_detail', args=[task.pk])
        safety = 'UNSAFE: ' if unsafe else ''
        NotificationService.notify(
            [*_keepers(asset), *_active(User.Role.MAINTENANCE)],
            f'{safety}{task.code} on {asset.tag} {asset.name}: {description[:120]}', url)
        if unsafe and asset.state in (Asset.State.HELD, Asset.State.IN_TRANSIT):
            # BR-8: the exchange is stopped where it can be; officers decide the rest.
            NotificationService.notify(
                [*_active(User.Role.APPROVER)],
                f'{asset.tag} is reported unsafe while it is {asset.get_state_display().lower()}. '
                'Dispatch is blocked until maintenance clears it.', url)
        return task

    @staticmethod
    def apply_pending_block(asset, actor):
        """Called when an exchange ends: block the item now if it was reported
        unsafe or judged beyond repair while it was reserved or on its way."""
        task = asset.maintenance_tasks.filter(
            state=MaintenanceTask.State.OPEN, unsafe=True, blocked_from='').first()
        if task is None:
            beyond = LifeCycleService._beyond_repair(asset)
            task = beyond if beyond and not beyond.blocked_from else None
        if task and asset.state in BLOCKABLE:
            LifeCycleService._block(asset, task, actor)

    # --- UC-09: inspection and repair ----------------------------------------

    @staticmethod
    @transaction.atomic
    def record_inspection(task, actor, outcome, findings, repair_done='', repair_cost=None, condition=''):
        require(actor, 'maintenance.work', task)
        if outcome not in MaintenanceTask.Outcome.values:
            raise DomainError('Choose an outcome.')
        if not (findings or '').strip():
            raise DomainError('Write what you found.')
        if repair_cost is not None and repair_cost < 0:
            raise DomainError('Repair cost cannot be negative.')
        updated = MaintenanceTask.objects.filter(pk=task.pk, state=MaintenanceTask.State.OPEN).update(
            state=MaintenanceTask.State.CLOSED, outcome=outcome, findings=findings.strip(),
            repair_done=(repair_done or '').strip(), repair_cost=repair_cost,
            inspected_by=actor, closed_at=timezone.now())
        if not updated:
            raise Conflict(f'{task.code} was already closed.')
        task.refresh_from_db()
        asset = Asset.objects.select_for_update().get(pk=task.asset_id)

        if condition and condition != asset.condition:
            old = asset.get_condition_display()
            Asset.objects.filter(pk=asset.pk).update(condition=condition, updated_at=timezone.now())
            asset.condition = condition
            AssetEvent.objects.create(
                asset=asset, actor=actor, action='Condition updated',
                from_state=asset.state, to_state=asset.state,
                note=f'{old} → {asset.get_condition_display()} after inspection {task.code}')

        cost = f', cost AED {repair_cost}' if repair_cost is not None else ''
        note = f'{task.code}: {task.get_outcome_display()}. {findings.strip()[:200]}'
        if outcome in (MaintenanceTask.Outcome.SAFE, MaintenanceTask.Outcome.NO_FAULT):
            if asset.state == Asset.State.BLOCKED:
                back_to = task.blocked_from or Asset.State.RECORDED
                asset.transition(back_to, actor, 'Cleared by maintenance', note)
                if back_to == Asset.State.AVAILABLE:
                    from exchange.services import ExchangeService
                    ExchangeService.alert_open_needs(asset)
            else:
                AssetEvent.objects.create(asset=asset, actor=actor, action='Inspected',
                                          from_state=asset.state, to_state=asset.state, note=note)
        else:  # beyond repair: keep it out of use and ask for an end-of-life decision
            if asset.state in BLOCKABLE:
                task.blocked_from = asset.state
                task.save(update_fields=['blocked_from'])
                asset.transition(Asset.State.BLOCKED, actor, 'Beyond repair', note)
            else:
                AssetEvent.objects.create(asset=asset, actor=actor, action='Beyond repair',
                                          from_state=asset.state, to_state=asset.state, note=note)
            NotificationService.notify(
                [*_keepers(asset), *_active(User.Role.APPROVER)],
                f'{asset.tag} {asset.name} is beyond repair. Please propose donation, recycling or disposal.',
                _asset_url(asset))
        audit(actor, 'Inspection recorded', task, note + cost)
        NotificationService.notify(
            [task.reported_by, *_keepers(asset)],
            f'{task.code} on {asset.tag} closed: {task.get_outcome_display()}.',
            reverse('maintenance_detail', args=[task.pk]))
        return task

    # --- UC-20: end of life ----------------------------------------------------

    @staticmethod
    def open_proposal(asset):
        return asset.eol_proposals.filter(
            state__in=[EndOfLifeProposal.State.PROPOSED, EndOfLifeProposal.State.APPROVED]).first()

    @staticmethod
    @transaction.atomic
    def propose(asset, actor, action, reason):
        require(actor, 'eol.propose', asset)
        if action not in EndOfLifeProposal.Action.values:
            raise DomainError('Choose donate, recycle or dispose.')
        if not (reason or '').strip():
            raise DomainError('Give a reason, including why the item cannot be reused or repaired.')
        asset = Asset.objects.select_for_update().get(pk=asset.pk)
        if asset.state not in EOL_STAGES:
            raise DomainError(
                f'{asset.tag} is {asset.get_state_display().lower()}. Finish or cancel the exchange first.')
        if LifeCycleService.open_proposal(asset):
            raise DomainError(f'{asset.tag} already has an open end-of-life proposal.')
        proposal = EndOfLifeProposal.objects.create(
            asset=asset, action=action, reason=reason.strip(), proposed_by=actor)
        note = f'{proposal.code}: {proposal.get_action_display()}. {reason.strip()[:200]}'
        if asset.state == Asset.State.AVAILABLE:
            # Take it off the marketplace so nobody reserves it during the review.
            asset.transition(Asset.State.RECORDED, actor, 'End of life proposed', note)
        else:
            AssetEvent.objects.create(asset=asset, actor=actor, action='End of life proposed',
                                      from_state=asset.state, to_state=asset.state, note=note)
        audit(actor, 'End of life proposed', proposal, note)
        NotificationService.notify(
            _active(User.Role.APPROVER).exclude(pk=actor.pk),
            f'{proposal.code}: {proposal.get_action_display()} {asset.tag} {asset.name} needs a decision.',
            reverse('eol_list'))
        return proposal

    @staticmethod
    @transaction.atomic
    def decide(proposal, actor, approve, note=''):
        require(actor, 'eol.decide', proposal)  # BR-5: never the proposer
        if not approve and not (note or '').strip():
            raise DomainError('A rejection needs a reason.')
        state = EndOfLifeProposal.State.APPROVED if approve else EndOfLifeProposal.State.REJECTED
        updated = EndOfLifeProposal.objects.filter(
            pk=proposal.pk, state=EndOfLifeProposal.State.PROPOSED
        ).update(state=state, decided_by=actor, decision_note=(note or '').strip(), decided_at=timezone.now())
        if not updated:
            raise Conflict(f'{proposal.code} was already decided.')
        proposal.refresh_from_db()
        asset = proposal.asset
        verb = 'approved' if approve else 'rejected'
        text = f'{proposal.code} {verb}' + (f': {note.strip()}' if note and note.strip() else '')
        AssetEvent.objects.create(asset=asset, actor=actor, action=f'End of life {verb}',
                                  from_state=asset.state, to_state=asset.state, note=text[:300])
        audit(actor, f'End of life {verb}', proposal, text)
        NotificationService.notify(
            [proposal.proposed_by, *_keepers(asset)],
            f'{text}.' + (' Record the hand-over when it is done.' if approve else ''),
            reverse('eol_list'))
        return proposal

    @staticmethod
    @transaction.atomic
    def complete(proposal, actor, recipient, evidence):
        require(actor, 'eol.complete', proposal)
        recipient, evidence = (recipient or '').strip(), (evidence or '').strip()
        if not evidence:
            raise DomainError('Enter the evidence, for example a receipt or certificate number.')
        if proposal.action == EndOfLifeProposal.Action.DONATE and not recipient:
            raise DomainError('A donation needs the name of the receiving organization.')
        updated = EndOfLifeProposal.objects.filter(
            pk=proposal.pk, state=EndOfLifeProposal.State.APPROVED
        ).update(state=EndOfLifeProposal.State.COMPLETED, recipient=recipient, evidence=evidence,
                 completed_by=actor, completed_at=timezone.now())
        if not updated:
            raise DomainError(f'{proposal.code} is not approved, or was already completed.')
        proposal.refresh_from_db()
        asset = Asset.objects.select_for_update().get(pk=proposal.asset_id)
        to = f' to {recipient}' if recipient else ''
        note = f'{proposal.code}: {proposal.get_action_display()}{to}. Evidence: {evidence}'
        asset.transition(Asset.State.TERMINAL, actor, {'DONATE': 'Donated', 'RECYCLE': 'Recycled', 'DISPOSE': 'Disposed'}[proposal.action], note[:300])
        # An open repair task has nothing left to repair.
        asset.maintenance_tasks.filter(state=MaintenanceTask.State.OPEN).update(
            state=MaintenanceTask.State.CLOSED, closed_at=timezone.now(),
            findings=f'Closed: item left campus under {proposal.code}.')
        audit(actor, 'End of life completed', proposal, note)
        NotificationService.notify(
            [proposal.proposed_by, *_keepers(asset), *_active(User.Role.APPROVER)],
            f'{asset.tag} {asset.name}: {note}'[:300], _asset_url(asset))
        return proposal
