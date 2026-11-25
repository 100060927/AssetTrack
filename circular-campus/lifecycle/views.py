from django.contrib import messages
from django.db.models import Q
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import User
from accounts.permissions import require
from catalog.models import Asset
from core.errors import DomainError

from .forms import DefectForm, InspectionForm, ProposalForm
from .models import EndOfLifeProposal, MaintenanceTask
from .services import LifeCycleService


def _department_only(user):
    """Custodians see their own department's records; officers see all."""
    return user.role == User.Role.CUSTODIAN


# --- UC-21 / UC-09 -------------------------------------------------------------

@login_required
@require_POST
def defect_report(request, pk):
    asset = get_object_or_404(Asset, pk=pk)
    form = DefectForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Describe the problem first.')
        return redirect('asset_detail', pk=pk)
    try:
        task = LifeCycleService.report_defect(
            asset, request.user, form.cleaned_data['description'], form.cleaned_data['unsafe'])
        messages.success(request, f'Thank you. {task.code} was sent to maintenance.'
                         + (' The item is blocked from use.' if form.cleaned_data['unsafe'] else ''))
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('asset_detail', pk=pk)


@login_required
def maintenance_list(request):
    require(request.user, 'maintenance.view')
    tasks = MaintenanceTask.objects.select_related('asset__department', 'reported_by', 'inspected_by')
    if request.user.role == User.Role.REQUESTER:
        tasks = tasks.filter(reported_by=request.user)
    elif _department_only(request.user):
        # Their department's items, plus anything they reported themselves.
        tasks = tasks.filter(Q(asset__department=request.user.department) | Q(reported_by=request.user))
    # Maintenance staff start with their to-do list; everyone else sees
    # every job, so a finished repair never seems to vanish.
    default = 'open' if request.user.role == User.Role.MAINTENANCE else 'all'
    show = request.GET.get('show', default)
    if show == 'open':
        tasks = tasks.filter(state=MaintenanceTask.State.OPEN)
    tasks = tasks.order_by('-state', '-created_at')  # OPEN before CLOSED, newest first
    return render(request, 'lifecycle/maintenance_list.html', {'tasks': tasks[:200], 'show': show})


@login_required
def maintenance_detail(request, pk):
    task = get_object_or_404(MaintenanceTask.objects.select_related('asset'), pk=pk)
    require(request.user, 'maintenance.view')
    if ((request.user.role == User.Role.REQUESTER and task.reported_by_id != request.user.id)
            or (_department_only(request.user) and task.asset.department_id != request.user.department_id
                and task.reported_by_id != request.user.id)):
        require(request.user, 'maintenance.work')  # always refused for these roles
    can_work = request.user.can('maintenance.work') and task.state == MaintenanceTask.State.OPEN
    form = InspectionForm(request.POST or None)
    if request.method == 'POST':
        require(request.user, 'maintenance.work')
        if form.is_valid():
            data = form.cleaned_data
            try:
                LifeCycleService.record_inspection(
                    task, request.user, data['outcome'], data['findings'], data['repair_done'],
                    data['repair_cost'], data['condition'])
                messages.success(request, f'{task.code} closed and the item history updated.')
                return redirect('maintenance_detail', pk=pk)
            except DomainError as error:
                messages.error(request, str(error))
    return render(request, 'lifecycle/maintenance_detail.html', {
        'task': task, 'form': form, 'can_work': can_work,
        'show_repair_cost': request.user.can('repair.view_cost')})


# --- UC-20 ---------------------------------------------------------------------

@login_required
@require_POST
def eol_propose(request, pk):
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, 'eol.propose', asset)
    form = ProposalForm(request.POST)
    try:
        if not form.is_valid():
            raise DomainError('Choose an action and give a reason.')
        proposal = LifeCycleService.propose(
            asset, request.user, form.cleaned_data['action'], form.cleaned_data['reason'])
        messages.success(request, f'{proposal.code} sent to an asset officer for approval.')
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('asset_detail', pk=pk)


@login_required
def eol_list(request):
    require(request.user, 'eol.view')
    proposals = EndOfLifeProposal.objects.select_related(
        'asset__department', 'proposed_by', 'decided_by', 'completed_by')
    if _department_only(request.user):
        proposals = proposals.filter(asset__department=request.user.department)
    rows = [{'p': p,
             'can_decide': p.state == EndOfLifeProposal.State.PROPOSED and request.user.can('eol.decide', p),
             'own': p.proposed_by_id == request.user.id,
             'can_complete': p.state == EndOfLifeProposal.State.APPROVED and request.user.can('eol.complete', p)}
            for p in proposals[:200]]
    return render(request, 'lifecycle/eol_list.html', {'rows': rows})


@login_required
@require_POST
def eol_decide(request, pk):
    proposal = get_object_or_404(EndOfLifeProposal, pk=pk)
    try:
        approve = request.POST.get('decision') == 'approve'
        LifeCycleService.decide(proposal, request.user, approve, request.POST.get('note', ''))
        messages.success(request, f'{proposal.code} {"approved" if approve else "rejected"}.')
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('eol_list')


@login_required
@require_POST
def eol_complete(request, pk):
    proposal = get_object_or_404(EndOfLifeProposal, pk=pk)
    try:
        LifeCycleService.complete(
            proposal, request.user, request.POST.get('recipient', ''), request.POST.get('evidence', ''))
        messages.success(request, f'{proposal.code} completed. {proposal.asset.tag} is now closed (Terminal).')
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('eol_list')
