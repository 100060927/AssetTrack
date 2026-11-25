from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.permissions import require
from ai.advice import ADVISABLE_STATES, AdviceService
from ai.classification import ClassificationService
from ai.models import Advice
from core.errors import DomainError
from lifecycle.forms import DefectForm, ProposalForm
from lifecycle.models import EndOfLifeProposal
from lifecycle.services import EOL_STAGES, LifeCycleService

import csv

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.http import HttpResponse

from .forms import AllAssetsFilter, AssetEditForm, AssetForm, SearchForm
from .models import Asset, AssetEvent
from .services import CatalogService


@login_required
def marketplace(request):
    form = SearchForm(request.GET or None)
    filters = form.cleaned_data if form.is_valid() else {}
    assets = CatalogService.search(
        keyword=filters.get('q', ''), category=filters.get('category'),
        condition=filters.get('condition', ''), department=filters.get('department'))
    return render(request, 'catalog/marketplace.html', {'form': form, 'assets': assets})


@login_required
def inventory(request):
    require(request.user, 'asset.view_inventory')
    assets = Asset.objects.filter(department=request.user.department).select_related('category')
    return render(request, 'catalog/inventory.html', {'assets': assets})


@login_required
def all_assets(request):
    """Every department's assets, with a summary per department."""
    require(request.user, 'asset.view_all')
    form = AllAssetsFilter(request.GET or None)
    items = Asset.objects.select_related('category', 'department', 'owner')
    if form.is_valid():
        f = form.cleaned_data
        for word in f['q'].split():
            items = items.filter(Q(name__icontains=word) | Q(description__icontains=word)
                                 | Q(tags__icontains=word) | Q(location__icontains=word))
        for field in ('department', 'category', 'state', 'condition'):
            if f[field]:
                items = items.filter(**{field: f[field]})
    items = items.order_by('department__name', 'category__name', 'name')
    value = ExpressionWrapper(F('quantity') * F('replacement_unit_cost'),
                              output_field=DecimalField(max_digits=14, decimal_places=2))
    summary = items.values('department__name', 'department__code', 'department__id').annotate(
        records=Count('id'), units=Sum('quantity'),
        available=Sum('quantity', filter=Q(state=Asset.State.AVAILABLE)),
        in_use=Sum('quantity', filter=Q(state=Asset.State.IN_USE)),
        busy=Sum('quantity', filter=Q(state__in=[Asset.State.HELD, Asset.State.IN_TRANSIT])),
        blocked=Sum('quantity', filter=Q(state=Asset.State.BLOCKED)),
        value=Sum(value, filter=~Q(state=Asset.State.TERMINAL)),
    ).order_by('department__name')
    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="all-assets.csv"'
        writer = csv.writer(response)
        writer.writerow(['Tag', 'Name', 'Department', 'Category', 'Condition', 'Status', 'Quantity',
                         'Holder', 'Location', 'Replacement cost per unit (AED)', 'Mass per unit (kg)'])
        for a in items:
            writer.writerow([a.tag, a.name, a.department.name, a.category.name, a.get_condition_display(),
                             a.get_state_display(), a.quantity, a.owner.display_name, a.location,
                             a.replacement_unit_cost if a.replacement_unit_cost is not None else '',
                             a.unit_mass_kg if a.unit_mass_kg is not None else ''])
        return response
    return render(request, 'catalog/all_assets.html', {
        'form': form, 'items': items[:500], 'count': items.count(), 'summary': summary})


@login_required
def my_items(request):
    """Items the signed-in person holds right now (they are the owner)."""
    require(request.user, 'asset.view_mine')
    show = request.GET.get('show', 'current')
    items = Asset.objects.filter(owner=request.user).select_related('category', 'department')
    if show != 'all':
        items = items.exclude(state=Asset.State.TERMINAL)
    received = {e.asset_id: e.created_at for e in AssetEvent.objects.filter(
        asset__in=items, action__startswith='Received').order_by('created_at')}
    rows = [{'asset': a, 'received': received.get(a.pk)} for a in items]
    return render(request, 'catalog/my_items.html', {
        'rows': rows, 'show': show, 'total_units': sum(a.quantity for a in items)})


@login_required
def asset_create(request):
    require(request.user, 'asset.register')
    suggestion = None
    if request.method == 'POST' and request.POST.get('action') == 'suggest':
        # AI feature 1: fill the form with a suggestion. Nothing is saved yet.
        data = request.POST.copy()
        if not data.get('name', '').strip():
            messages.error(request, 'Type a name or description first.')
        else:
            suggestion = ClassificationService().suggest(
                request.user, data.get('name', ''), data.get('description', ''))
            if suggestion['category']:
                data['category'] = str(suggestion['category'].pk)
            data['condition'] = suggestion['condition']
            data['tags'] = suggestion['tags']
        form = AssetForm(initial=data.dict())
        return render(request, 'catalog/asset_form.html', {'form': form, 'suggestion': suggestion})

    form = AssetForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        note = ClassificationService.review_note(
            request.POST.get('ai_run'), request.user,
            form.cleaned_data['category'], form.cleaned_data['condition'])
        asset = CatalogService.register(request.user, history_note=note, **form.cleaned_data)
        messages.success(request, f'{asset.tag} registered. Publish it to list it as surplus.')
        return redirect('asset_detail', pk=asset.pk)
    return render(request, 'catalog/asset_form.html', {
        'form': form, 'ai_run': request.POST.get('ai_run', '')})


@login_required
def asset_edit(request, pk):
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, 'asset.edit', asset)
    form = AssetEditForm(request.POST or None, instance=asset)
    if request.method == 'POST' and form.is_valid():
        changes = {f: form.cleaned_data[f] for f in CatalogService.EDITABLE}
        try:
            CatalogService.edit(Asset.objects.get(pk=pk), request.user, changes, form.cleaned_data['reason'])
            messages.success(request, f'{asset.tag} updated. The old and new values are in its history.')
            return redirect('asset_detail', pk=pk)
        except DomainError as error:
            messages.error(request, str(error))
    return render(request, 'catalog/asset_edit.html', {'form': form, 'asset': Asset.objects.get(pk=pk)})


@login_required
def asset_detail(request, pk):
    asset = get_object_or_404(Asset.objects.select_related('category', 'department', 'owner'), pk=pk)
    user = request.user
    visible = (asset.state == Asset.State.AVAILABLE
               or asset.owner_id == user.id
               or asset.department_id == user.department_id
               or user.can('report.view')
               or user.can('maintenance.work'))
    if not visible:
        # Requesters may still see an asset they asked for or reported a problem on.
        visible = (asset.reservations.filter(request__requester=user).exists()
                   or asset.maintenance_tasks.filter(reported_by=user).exists())
    if not visible:
        require(user, 'asset.manage', asset)
    return render(request, 'catalog/asset_detail.html', {
        'asset': asset,
        'events': asset.events.select_related('actor'),
        'can_manage': user.can('asset.manage', asset),
        'can_offer_back': user.can('asset.offer_back', asset) and asset.state == Asset.State.IN_USE,
        'can_request': (asset.state == Asset.State.AVAILABLE
                        and user.can('request.create')
                        and asset.department_id != user.department_id),
        'show_cost': user.can('asset.view_cost'),
        'show_repair_cost': user.can('repair.view_cost'),
        'can_advise': user.can('asset.manage', asset) and asset.state in ADVISABLE_STATES,
        'advice': asset.advice.select_related('ai_run').first()
        if user.can('asset.manage', asset) else None,
        # UC-15, UC-21, UC-20
        'can_edit': user.can('asset.edit', asset) and asset.state not in (
            Asset.State.HELD, Asset.State.IN_TRANSIT, Asset.State.TERMINAL),
        'can_report_defect': asset.state != Asset.State.TERMINAL,
        'open_task': LifeCycleService.open_task(asset),
        'tasks': asset.maintenance_tasks.all()[:5],
        'proposal': LifeCycleService.open_proposal(asset) or asset.eol_proposals.filter(
            state=EndOfLifeProposal.State.COMPLETED).first(),
        'can_propose': (user.can('eol.propose', asset) and asset.state in EOL_STAGES
                        and LifeCycleService.open_proposal(asset) is None),
        'defect_form': DefectForm(), 'proposal_form': ProposalForm(),
    })


@login_required
@require_POST
def asset_action(request, pk, action):
    asset = get_object_or_404(Asset, pk=pk)
    try:
        if action == 'publish':
            CatalogService.publish(asset, request.user)
            messages.success(request, f'{asset.tag} is now listed as available.')
        elif action == 'advise':
            AdviceService().advise(asset, request.user)
        elif action in ('advice-accept', 'advice-dismiss'):
            advice = get_object_or_404(Advice, asset=asset, decision=Advice.Decision.PENDING)
            AdviceService.decide(advice, request.user, accepted=action == 'advice-accept')
            messages.success(request, 'Your decision was recorded in the history.')
        elif action == 'offer-back':
            CatalogService.offer_back(asset, request.user, request.POST.get('note', ''))
            messages.success(request, 'Your department custodian was told. They can list it on the marketplace.')
        elif action == 'withdraw':
            CatalogService.withdraw(asset, request.user)
            messages.success(request, f'{asset.tag} was withdrawn from the listing.')
        else:
            raise DomainError('Unknown action.')
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('asset_detail', pk=asset.pk)
