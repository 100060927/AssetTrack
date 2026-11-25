"""AI feature 2: matching and ranking (Phase 5, section 14).

Hard rules run first and are never scored away. Eligible items get a weighted
compatibility score C = 100 x sum(w x f) / sum(w) over the factors that have
evidence. The AI only supplies the purpose-fit factor and the one-line reason.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from accounts.permissions import require
from catalog.models import Asset

from .gateway import AIGateway
from .models import Recommendation

TOP_N = 5

# Proposed design weights (Phase 5, section 14). Factors without data in this
# prototype are listed so the evidence coverage is honest.
WEIGHTS = {
    'purpose': 0.25,
    'recommended_specifications': 0.20,  # not captured yet
    'quantity_coverage': 0.10,
    'condition': 0.10,
    'date_feasibility': 0.10,            # not captured yet
    'location': 0.10,
    'transfer_and_repair_cost': 0.10,    # not captured yet
    'sustainability_benefit': 0.05,      # not captured yet
}
CONDITION_SCORE = {'NEW': 1.0, 'GOOD': 0.9, 'FAIR': 0.8, 'POOR': 0.6}


class MatchingService:
    def __init__(self, gateway=None):
        self.gateway = gateway or AIGateway()

    @staticmethod
    def eligible_assets(resource_request):
        rejected = resource_request.recommendations.filter(
            decision=Recommendation.Decision.REJECTED).values_list('asset_id', flat=True)
        candidates = Asset.objects.filter(
            state=Asset.State.AVAILABLE, quantity__gt=0
        ).exclude(department=resource_request.department).exclude(pk__in=list(rejected))
        if resource_request.category_id:
            candidates = candidates.filter(category=resource_request.category)
        candidates = candidates.select_related('category', 'department')
        return [asset for asset in candidates if asset.eligible_for(resource_request)]

    @staticmethod
    def _location_factor(asset, department):
        if asset.department.campus != department.campus:
            return 0.4
        if department.building and asset.department.building == department.building:
            return 1.0
        return 0.7

    def _score(self, asset, resource_request, needed, relevance):
        factors = {
            'purpose': relevance,
            'quantity_coverage': min(asset.quantity / needed, 1.0),
            'condition': CONDITION_SCORE[asset.condition],
            'location': self._location_factor(asset, resource_request.department),
        }
        used = sum(WEIGHTS[name] for name in factors)
        score = 100 * sum(WEIGHTS[name] * value for name, value in factors.items()) / used
        return round(score, 1), round(used / sum(WEIGHTS.values()), 2), factors

    def find_matches(self, resource_request, user):
        """Rank the top 5 surplus items for a request and store them.

        The AI is asked first, outside any database transaction, so a slow
        answer never holds the database locked for other users.
        """
        require(user, 'request.own', resource_request)
        assets = self.eligible_assets(resource_request)
        if not assets:
            resource_request.recommendations.filter(
                decision=Recommendation.Decision.PENDING).delete()
            return []
        needed = resource_request.remaining_quantity() or 1

        # SEC-7: only descriptive fields leave the application. No people, costs or locations.
        need = {
            'title': resource_request.title,
            'purpose': resource_request.purpose,
            'category': resource_request.category.name if resource_request.category else '',
            'quantity': needed,
        }
        candidates = [{
            'id': asset.pk, 'name': asset.name, 'description': asset.description,
            'category': asset.category.name, 'condition': asset.get_condition_display(),
        } for asset in assets]
        run = self.gateway.rank(need, candidates, user)

        scored = []
        for asset in assets:
            judged = run.output[asset.pk]
            score, coverage, factors = self._score(asset, resource_request, needed, judged['relevance'])
            scored.append((score, asset, coverage, factors, judged['reason']))
        # Ties: more units covered first, then the lower asset id.
        scored.sort(key=lambda row: (-row[0], -row[3]['quantity_coverage'], row[1].pk))

        with transaction.atomic():
            resource_request.recommendations.filter(
                decision=Recommendation.Decision.PENDING).delete()
            return [
                Recommendation.objects.create(
                    ai_run=run, request=resource_request, asset=asset, rank=position,
                    score=Decimal(str(score)), coverage=Decimal(str(coverage)),
                    reason=reason, factors=factors)
                for position, (score, asset, coverage, factors, reason)
                in enumerate(scored[:TOP_N], start=1)
            ]

    @staticmethod
    def record_decision(recommendation, user, accepted):
        require(user, 'request.own', recommendation.request)
        recommendation.decision = (
            Recommendation.Decision.ACCEPTED if accepted else Recommendation.Decision.REJECTED)
        recommendation.decided_at = timezone.now()
        recommendation.save(update_fields=['decision', 'decided_at'])
