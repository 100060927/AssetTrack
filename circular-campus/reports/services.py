import re
from datetime import datetime, time
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Max, Sum
from django.utils import timezone

from accounts.permissions import require
from ai.gateway import AIGateway
from core.errors import DomainError
from exchange.models import ResourceRequest, Transfer

from .models import ReportDraft

KG_PER_SHORT_TON = Decimal('907.185')

# C7: every measure shows its formula and its source.
MEASURE_INFO = {
    'items_reused': {
        'label': 'Items reused', 'unit': 'items',
        'formula': 'Sum of received quantity over completed transfers in the period.',
        'source': 'Transfer records in this system (receipt confirmed by the receiver).',
    },
    'cost_avoided_aed': {
        'label': 'Cost avoided', 'unit': 'AED',
        'formula': 'Sum of received quantity x replacement cost per unit.',
        'source': 'Replacement cost entered by the custodian at registration (SRS 6.1). '
                  'An estimate until Procurement validates that a purchase was displaced.',
    },
    'waste_diverted_kg': {
        'label': 'Waste diverted', 'unit': 'kg',
        'formula': 'Sum of received quantity x mass per unit (kg).',
        'source': 'Unit mass entered by the custodian at registration (SRS 6.1).',
    },
    'co2e_kg': {
        'label': 'Estimated carbon benefit', 'unit': 'kg CO2e',
        'formula': '(mass kg / 907.185) x (landfill factor - source-reduction factor) x 1,000.',
        'source': 'EPA WARM v16 (December 2023), Electronics, Exhibit 1-5 (SRS 6.2). '
                  'US factors used as a screening proxy, not measured campus emissions.',
    },
}


def _period_bounds(start, end):
    tz = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.combine(start, time.min), tz),
            timezone.make_aware(datetime.combine(end, time.max), tz))


class ImpactService:
    """Module 6.0: sustainability figures, calculated by the database side only."""

    @staticmethod
    def completed_transfers(start, end):
        lower, upper = _period_bounds(start, end)
        return Transfer.objects.filter(
            state=Transfer.State.COMPLETED, received_at__range=(lower, upper),
        ).select_related('reservation__asset__category', 'reservation__request',
                         'from_department', 'to_department')

    @classmethod
    def calculate(cls, start, end):
        """Returns the measures for a period.

        BR-14: a figure is shown only where its supporting data exists. Items
        without the data are counted in `missing`, never estimated.
        """
        totals = {key: Decimal('0') for key in MEASURE_INFO}
        counted = {key: 0 for key in MEASURE_INFO}
        transfers = list(cls.completed_transfers(start, end))
        items = 0
        for transfer in transfers:
            asset = transfer.reservation.asset
            quantity = transfer.received_qty
            items += quantity
            totals['items_reused'] += quantity
            counted['items_reused'] += quantity
            if asset.replacement_unit_cost is not None:
                totals['cost_avoided_aed'] += quantity * asset.replacement_unit_cost
                counted['cost_avoided_aed'] += quantity
            if asset.unit_mass_kg is not None:
                mass = quantity * asset.unit_mass_kg
                totals['waste_diverted_kg'] += mass
                counted['waste_diverted_kg'] += quantity
                category = asset.category
                if category.warm_source_reduction is not None and category.warm_landfill is not None:
                    factor = category.warm_landfill - category.warm_source_reduction
                    totals['co2e_kg'] += mass / KG_PER_SHORT_TON * factor * 1000
                    counted['co2e_kg'] += quantity

        measures = {}
        for key, info in MEASURE_INFO.items():
            has_data = key == 'items_reused' or counted[key] > 0
            if not has_data:
                value = None
            elif key == 'items_reused':
                value = int(totals[key])
            else:
                value = float(totals[key].quantize(Decimal('0.01')))
            measures[key] = {**info, 'value': value, 'missing': items - counted[key]}

        lower, upper = _period_bounds(start, end)
        return {
            'period': {'start': start.isoformat(), 'end': end.isoformat()},
            'measures': measures,
            'transfers_completed': len(transfers),
            'requests_submitted': ResourceRequest.objects.filter(
                created_at__range=(lower, upper)).count(),
        }


_NUMBER = re.compile(r'\d[\d,]*(?:\.\d+)?')


class ReportService:
    def __init__(self, gateway=None):
        self.gateway = gateway or AIGateway()

    @staticmethod
    def _plain_snapshot(snapshot):
        """Numbers only: the AI gets no people, departments or item names."""
        return {
            'period': snapshot['period'],
            'transfers_completed': snapshot['transfers_completed'],
            'requests_submitted': snapshot['requests_submitted'],
            'measures': {key: {'label': m['label'], 'unit': m['unit'], 'value': m['value'],
                               'items_without_data': m['missing']}
                         for key, m in snapshot['measures'].items()},
        }

    def generate(self, start, end, user):
        """Numbers first, then the AI (outside a transaction), then one save."""
        require(user, 'report.draft')
        if start > end:
            raise DomainError('The period start must be before its end.')
        snapshot = self._plain_snapshot(ImpactService.calculate(start, end))
        run = self.gateway.summarise_report(snapshot, user)
        text = run.output['text']
        return ReportDraft.objects.create(
            period_start=start, period_end=end, snapshot=snapshot,
            ai_run=run, ai_text=text, text=text, created_by=user)

    @staticmethod
    def unverified_numbers(draft):
        """Numbers in the narrative that are not in the snapshot (claim check)."""
        allowed = set()

        def add(value):
            if isinstance(value, bool) or value is None:
                return
            if isinstance(value, (int, float)):
                number = Decimal(str(value))
                allowed.update({f'{number:f}', f'{number:.2f}', f'{number:.1f}',
                                f'{number.to_integral_value():f}' if number == number.to_integral_value() else ''})
            elif isinstance(value, dict):
                for item in value.values():
                    add(item)
            elif isinstance(value, str):
                allowed.update(_NUMBER.findall(value))

        add(draft.snapshot)
        found = {match.replace(',', '') for match in _NUMBER.findall(draft.text)}
        return sorted(found - allowed - {''})

    @staticmethod
    @transaction.atomic
    def save_edit(draft, user, text):
        require(user, 'report.publish')
        if draft.status == ReportDraft.Status.PUBLISHED:
            raise DomainError('A published summary cannot be edited.')
        if not text.strip():
            raise DomainError('The summary cannot be empty.')
        draft.text = text.strip()
        draft.save(update_fields=['text'])
        return draft

    @staticmethod
    @transaction.atomic
    def publish(draft, user):
        require(user, 'report.publish')
        if draft.status == ReportDraft.Status.PUBLISHED:
            raise DomainError('This summary is already published.')
        draft.status = ReportDraft.Status.PUBLISHED
        draft.published_by = user
        draft.published_at = timezone.now()
        draft.save(update_fields=['status', 'published_by', 'published_at'])
        return draft


class MovementService:
    """Who took which assets: totals by receiving department and by person.

    Reads the transfer records, which already hold every completed hand-over,
    so there is no second copy of the data to keep in step.
    """

    @staticmethod
    def transfers_for(user, start=None, end=None, department=None, person=None):
        require(user, 'movements.view')
        rows = Transfer.objects.filter(state=Transfer.State.COMPLETED)
        if not user.can('movements.all_departments'):
            # A custodian sees the items that left their own department.
            rows = rows.filter(from_department=user.department)
        if start and end:
            lower, upper = _period_bounds(start, end)
            rows = rows.filter(received_at__range=(lower, upper))
        if department:
            rows = rows.filter(to_department=department)
        if person:
            rows = rows.filter(received_by=person)
        return rows.select_related(
            'reservation__asset__category', 'reservation__request',
            'from_department', 'to_department', 'received_by',
        ).order_by('-received_at')

    @staticmethod
    def _value():
        return ExpressionWrapper(
            F('received_qty') * F('reservation__asset__replacement_unit_cost'),
            output_field=DecimalField(max_digits=14, decimal_places=2))

    @classmethod
    def by_department(cls, rows):
        return list(rows.order_by().values('to_department__name').annotate(
            transfers=Count('id'), units=Sum('received_qty'),
            value=Sum(cls._value()), last=Max('received_at'),
        ).order_by('-units', 'to_department__name'))

    @classmethod
    def by_person(cls, rows):
        return list(rows.order_by().values(
            'received_by__first_name', 'received_by__last_name', 'received_by__username',
            'to_department__name',
        ).annotate(
            transfers=Count('id'), units=Sum('received_qty'),
            value=Sum(cls._value()), last=Max('received_at'),
        ).order_by('-units', 'received_by__username'))

    @classmethod
    def totals(cls, rows):
        result = rows.order_by().aggregate(
            transfers=Count('id'), units=Sum('received_qty'), value=Sum(cls._value()))
        result['departments'] = rows.order_by().values('to_department').distinct().count()
        result['people'] = rows.order_by().values('received_by').distinct().count()
        return result
