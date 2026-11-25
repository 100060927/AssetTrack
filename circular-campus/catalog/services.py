from django.db import transaction
from django.db.models import F, Q

from accounts.permissions import require
from core.errors import Conflict, DomainError

from .models import Asset, AssetEvent


class CatalogService:
    """Module 2.0: asset registration, publication and search."""

    @staticmethod
    @transaction.atomic
    def register(actor, history_note='', **data):
        require(actor, 'asset.register')
        # BR-10: the department always comes from the custodian, never the form.
        asset = Asset.objects.create(
            department=actor.department, owner=actor, state=Asset.State.RECORDED, **data)
        AssetEvent.objects.create(
            asset=asset, actor=actor, action='Registered', to_state=asset.state,
            note=history_note)
        return asset

    @staticmethod
    @transaction.atomic
    def publish(asset, actor):
        require(actor, 'asset.manage', asset)
        action = 'Declared surplus' if asset.state == Asset.State.IN_USE else 'Published'
        event = asset.transition(Asset.State.AVAILABLE, actor, action)
        # Tell requesters whose open need this item could fill (UC-18 step 5).
        from exchange.services import ExchangeService
        ExchangeService.alert_open_needs(asset)
        return event

    @staticmethod
    @transaction.atomic
    def withdraw(asset, actor, note=''):
        require(actor, 'asset.manage', asset)
        return asset.transition(Asset.State.RECORDED, actor, 'Withdrawn', note)

    EDITABLE = ['name', 'description', 'category', 'condition', 'tags', 'location',
                'quantity', 'replacement_unit_cost', 'unit_mass_kg']

    @staticmethod
    @transaction.atomic
    def edit(asset, actor, changes, reason=''):
        """UC-15: correct an item's details. Old and new values go into the history."""
        require(actor, 'asset.edit', asset)
        asset = Asset.objects.select_for_update().get(pk=asset.pk)
        if asset.state in (Asset.State.HELD, Asset.State.IN_TRANSIT):
            raise DomainError(f'{asset.tag} is part of an exchange right now. Edit it after the hand-over.')
        if asset.state == Asset.State.TERMINAL:
            raise DomainError(f'{asset.tag} has left the campus. Its record is closed.')
        diffs = []
        for field in CatalogService.EDITABLE:
            if field not in changes:
                continue
            old, new = getattr(asset, field), changes[field]
            if old != new:
                label = Asset._meta.get_field(field).verbose_name
                show = lambda v: '-' if v in (None, '') else str(v)
                if field == 'condition':
                    old, new_shown = asset.get_condition_display(), Asset.Condition(new).label
                    diffs.append(f'{label}: {old} → {new_shown}')
                elif field == 'description':
                    diffs.append('description changed')
                else:
                    diffs.append(f'{label}: {show(old)} → {show(new)}')
                setattr(asset, field, new)
        if not diffs:
            raise DomainError('Nothing was changed.')
        reason = (reason or '').strip()
        if 'quantity' in changes and any(d.startswith('quantity') for d in diffs) and not reason:
            raise DomainError('Changing the quantity needs a reason (for example, a recount).')
        if asset.quantity < 1:
            raise DomainError('Quantity must be at least 1.')
        asset.save()
        note = '; '.join(diffs) + (f' (reason: {reason})' if reason else '')
        AssetEvent.objects.create(asset=asset, actor=actor, action='Edited',
                                  from_state=asset.state, to_state=asset.state, note=note[:300])
        return asset

    @staticmethod
    @transaction.atomic
    def offer_back(asset, actor, note=''):
        """The person using an item says they no longer need it.

        Nothing moves yet: the department's custodian decides whether to
        declare it surplus (In use -> Available), so the circle can close.
        """
        require(actor, 'asset.offer_back', asset)
        if asset.state != Asset.State.IN_USE:
            raise DomainError('Only an item you are using can be offered back.')
        from accounts.models import User
        from django.urls import reverse
        from notifications.services import NotificationService
        note = (note or '').strip()
        AssetEvent.objects.create(asset=asset, actor=actor, action='Offered back',
                                  from_state=asset.state, to_state=asset.state,
                                  note=(note or 'No longer needed')[:300])
        custodians = User.objects.filter(role=User.Role.CUSTODIAN, department_id=asset.department_id,
                                         is_active=True)
        NotificationService.notify(
            custodians, f'{actor.display_name} no longer needs {asset.tag} {asset.name}. '
            'Declare it surplus to list it on the marketplace.' + (f' Note: {note}' if note else ''),
            reverse('asset_detail', args=[asset.pk]))

    @staticmethod
    def split(asset, quantity, actor):
        """Take `quantity` units out of a group as a new asset record.

        Groups are split when custody differs (Phase 5, section 9). The
        guarded update means the same units can never be taken twice.
        """
        if quantity <= 0 or quantity >= asset.quantity:
            raise DomainError('Split quantity must be less than the group size.')
        updated = Asset.objects.filter(
            pk=asset.pk, state=asset.state, quantity__gt=quantity
        ).update(quantity=F('quantity') - quantity)
        if not updated:
            raise Conflict(f'{asset.tag} no longer has {quantity} units available.')
        asset.refresh_from_db()
        part = Asset.objects.create(
            name=asset.name, description=asset.description, category=asset.category,
            condition=asset.condition, department=asset.department, owner=asset.owner,
            location=asset.location, tags=asset.tags, quantity=quantity, state=asset.state,
            replacement_unit_cost=asset.replacement_unit_cost,
            unit_mass_kg=asset.unit_mass_kg, parent=asset, is_fictional=asset.is_fictional)
        AssetEvent.objects.create(
            asset=asset, actor=actor, action='Split',
            from_state=asset.state, to_state=asset.state,
            note=f'{quantity} unit(s) moved to {part.tag}')
        AssetEvent.objects.create(
            asset=part, actor=actor, action='Split from group',
            to_state=part.state, note=f'{quantity} unit(s) taken from {asset.tag}')
        return part

    @staticmethod
    def search(keyword='', category=None, condition='', department=None):
        """C3: published surplus items, filtered."""
        items = Asset.objects.filter(state=Asset.State.AVAILABLE, quantity__gt=0)
        if keyword:
            for word in keyword.split():
                items = items.filter(
                    Q(name__icontains=word) | Q(description__icontains=word)
                    | Q(category__name__icontains=word) | Q(location__icontains=word)
                    | Q(tags__icontains=word))
        if category:
            items = items.filter(category=category)
        if condition:
            items = items.filter(condition=condition)
        if department:
            items = items.filter(department=department)
        return items.select_related('category', 'department')
