"""AI feature 3: sustainable-action advice.

The AI suggests reuse, repair, donate, recycle or dispose, with reasons. The
impact numbers beside the advice come from our own formulas, and the custodian
decides. Accepting advice records the decision; it does not change the asset.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from accounts.permissions import require
from catalog.models import Asset, AssetEvent
from core.errors import DomainError
from exchange.models import ResourceRequest
from reports.services import KG_PER_SHORT_TON

from .gateway import AIGateway
from .models import Advice

ADVISABLE_STATES = {Asset.State.RECORDED, Asset.State.AVAILABLE, Asset.State.IN_USE}
OPEN_REQUEST_STATES = {ResourceRequest.State.SUBMITTED, ResourceRequest.State.PART_FULFILLED}


class AdviceService:
    def __init__(self, gateway=None):
        self.gateway = gateway or AIGateway()

    @staticmethod
    def impact_if_kept_in_use(asset):
        """Our formulas (SRS 6.1 and 6.2). None means the data is missing."""
        cost = mass = carbon = None
        if asset.replacement_unit_cost is not None:
            cost = float(asset.quantity * asset.replacement_unit_cost)
        if asset.unit_mass_kg is not None:
            total_mass = asset.quantity * asset.unit_mass_kg
            mass = float(total_mass)
            category = asset.category
            if category.warm_source_reduction is not None and category.warm_landfill is not None:
                factor = category.warm_landfill - category.warm_source_reduction
                carbon = float((total_mass / KG_PER_SHORT_TON * factor * 1000).quantize(Decimal('0.01')))
        return {'replacement_cost_aed': cost, 'waste_diverted_kg': mass, 'co2e_kg': carbon}

    def advise(self, asset, user):
        """Ask the AI first (outside a transaction), then save in a short one."""
        require(user, 'asset.manage', asset)
        if asset.state not in ADVISABLE_STATES:
            raise DomainError('Advice is available only while the asset is in your custody.')
        demand = ResourceRequest.objects.filter(
            category=asset.category, state__in=OPEN_REQUEST_STATES
        ).exclude(department=asset.department).count()
        # SEC-7: no costs, people or locations are sent.
        facts = {
            'name': asset.name, 'description': asset.description,
            'category': asset.category.name, 'condition': asset.get_condition_display(),
            'quantity': asset.quantity, 'open_requests_in_category': demand,
        }
        run = self.gateway.advise(facts, user)
        with transaction.atomic():
            asset.advice.filter(decision=Advice.Decision.PENDING).update(
                decision=Advice.Decision.DISMISSED, decided_by=user, decided_at=timezone.now())
            return Advice.objects.create(
                asset=asset, ai_run=run, action=run.output['action'],
                reasons=run.output['reasons'], impact=self.impact_if_kept_in_use(asset))

    @staticmethod
    @transaction.atomic
    def decide(advice, user, accepted):
        require(user, 'asset.manage', advice.asset)
        updated = Advice.objects.filter(pk=advice.pk, decision=Advice.Decision.PENDING).update(
            decision=Advice.Decision.ACCEPTED if accepted else Advice.Decision.DISMISSED,
            decided_by=user, decided_at=timezone.now())
        if not updated:
            raise DomainError('This advice was already decided.')
        asset = advice.asset
        verdict = 'accepted' if accepted else 'dismissed'
        AssetEvent.objects.create(
            asset=asset, actor=user, action=f'Advice {verdict}',
            from_state=asset.state, to_state=asset.state,
            note=f'Suggested action: {advice.get_action_display()}')
