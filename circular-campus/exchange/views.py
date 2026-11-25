from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import require
from ai.assistant import AssistantService
from ai.matching import MatchingService
from ai.models import Recommendation
from catalog.models import Asset
from core.errors import DomainError

from .forms import RequestForm
from .models import ApprovalCase, Reservation, ResourceRequest, Transfer
from .services import ExchangeService


@login_required
def home(request):
    user = request.user
    context = {
        'my_requests': user.requests.all()[:5],
        'available_count': Asset.objects.filter(state=Asset.State.AVAILABLE).count(),
    }
    if user.can('approval.queue'):
        context['pending_approvals'] = ApprovalCase.objects.filter(
            state=ApprovalCase.State.UNDER_REVIEW).count()
    if user.can('transfer.queue'):
        context['to_dispatch'] = Transfer.objects.filter(
            state=Transfer.State.READY, from_department=user.department).count()
        context['department_assets'] = Asset.objects.filter(department=user.department).count()
    if user.can('needs.view'):
        context['open_needs'] = len(ExchangeService.open_needs())
    if user.can('maintenance.view') and user.role != user.Role.REQUESTER:
        from lifecycle.models import MaintenanceTask
        tasks = MaintenanceTask.objects.filter(state=MaintenanceTask.State.OPEN)
        if user.role == user.Role.CUSTODIAN:
            tasks = tasks.filter(asset__department=user.department)
        context['open_tasks'] = tasks.count()
    if user.role == user.Role.APPROVER:
        context['open_disputes'] = Transfer.objects.filter(state=Transfer.State.DISPUTED).count()
    context['to_receive'] = Transfer.objects.filter(
        state=Transfer.State.IN_TRANSIT, reservation__request__requester=user).count()
    return render(request, 'home.html', context)


# --- Requests ----------------------------------------------------------------

@login_required
def request_list(request):
    return render(request, 'exchange/request_list.html', {'requests': request.user.requests.all()})


@login_required
def request_create(request):
    require(request.user, 'request.create')
    asset = None
    if request.GET.get('asset'):
        asset = get_object_or_404(Asset, pk=request.GET['asset'])
        if asset.state != Asset.State.AVAILABLE:
            # Someone else took it, or it was withdrawn, since the page was opened.
            messages.error(request, f'{asset.tag} {asset.name} is no longer available. '
                                    'Search again, or describe what you need in a new request.')
            return redirect('marketplace')
    initial = {'title': asset.name, 'category': asset.category, 'min_condition': asset.condition} if asset else None
    form = RequestForm(request.POST or None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        resource_request = ExchangeService.submit(request.user, **form.cleaned_data)
        if not asset:
            messages.success(request, f'{resource_request.code} submitted. Find matching surplus items below.')
            return redirect('request_detail', pk=resource_request.pk)
        try:
            quantity = min(resource_request.quantity, asset.quantity)
            ExchangeService.reserve(resource_request, asset.pk, quantity, request.user)
            messages.success(request, f'{resource_request.code} submitted and {asset.name} reserved for approval.')
        except DomainError as error:
            messages.error(request, f'{error} Your request {resource_request.code} is saved and stays open.')
        return redirect('request_detail', pk=resource_request.pk)
    return render(request, 'exchange/request_form.html', {'form': form, 'asset': asset})


@login_required
def request_detail(request, pk):
    resource_request = get_object_or_404(ResourceRequest, pk=pk)
    require(request.user, 'request.view', resource_request)
    is_owner = request.user.can('request.own', resource_request)
    recommendations = []
    if is_owner and resource_request.is_open_for_matching:
        recommendations = [
            r for r in resource_request.recommendations.filter(
                decision=Recommendation.Decision.PENDING
            ).select_related('asset__department', 'asset__category', 'ai_run')
            if r.asset.state == Asset.State.AVAILABLE
        ]
    reservations = resource_request.reservations.select_related(
        'asset', 'approval_case').prefetch_related('approval_case__decisions__approver')
    return render(request, 'exchange/request_detail.html', {
        'req': resource_request,
        'is_owner': is_owner,
        'recommendations': recommendations,
        'reservations': reservations,
        'problems': Transfer.Problem.choices,
        'can_cancel': is_owner and ResourceRequest.State.CANCELLED
        in ResourceRequest.ALLOWED_TRANSITIONS[resource_request.state],
    })


@login_required
@require_POST
def request_find_matches(request, pk):
    resource_request = get_object_or_404(ResourceRequest, pk=pk)
    require(request.user, 'request.own', resource_request)
    if not resource_request.is_open_for_matching:
        messages.error(request, 'This request is not open for matching.')
    elif not MatchingService().find_matches(resource_request, request.user):
        messages.info(request, 'Nothing listed fits yet. Your request stays open: custodians can see it '
                               'on the Wanted list, and you will get a message when a matching item is listed.')
    return redirect('request_detail', pk=pk)


@login_required
@require_POST
def recommendation_decide(request, pk):
    recommendation = get_object_or_404(Recommendation, pk=pk, decision=Recommendation.Decision.PENDING)
    resource_request = recommendation.request
    require(request.user, 'request.own', resource_request)
    try:
        if request.POST.get('decision') == 'accept':
            quantity = min(resource_request.remaining_quantity(), recommendation.asset.quantity)
            ExchangeService.reserve(resource_request, recommendation.asset_id, quantity, request.user)
            MatchingService.record_decision(recommendation, request.user, accepted=True)
            messages.success(request, 'Item reserved. It now waits for approval.')
        else:
            MatchingService.record_decision(recommendation, request.user, accepted=False)
            messages.info(request, 'Suggestion dismissed.')
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('request_detail', pk=resource_request.pk)


@login_required
@require_POST
def request_cancel(request, pk):
    resource_request = get_object_or_404(ResourceRequest, pk=pk)
    try:
        ExchangeService.cancel(resource_request, request.user)
        messages.success(request, f'{resource_request.code} cancelled.')
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('request_detail', pk=pk)


@login_required
def wanted(request):
    """Open requests with no item reserved yet, for custodians and officers."""
    require(request.user, 'needs.view')
    needs = ExchangeService.open_needs()
    return render(request, 'exchange/wanted.html', {'needs': needs})


# --- Approvals ---------------------------------------------------------------

@login_required
def approval_queue(request):
    require(request.user, 'approval.queue')
    cases = ApprovalCase.objects.filter(state=ApprovalCase.State.UNDER_REVIEW).select_related(
        'reservation__request__requester', 'reservation__request__department',
        'reservation__asset__department')
    rows = [{'case': case, 'can_decide': request.user.can('approval.decide', case)} for case in cases]
    return render(request, 'exchange/approval_queue.html', {'rows': rows})


@login_required
@require_POST
def approval_decide(request, pk):
    case = get_object_or_404(ApprovalCase, pk=pk)
    require(request.user, 'approval.decide', case)
    try:
        approve = request.POST.get('decision') == 'approve'
        ExchangeService.decide(case, request.user, approve, request.POST.get('reason', ''))
        messages.success(request, 'Approved.' if approve else 'Rejected and the hold was released.')
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('approval_queue')


# --- Transfers ---------------------------------------------------------------

@login_required
def transfer_queue(request):
    require(request.user, 'transfer.queue')
    transfers = Transfer.objects.filter(
        from_department=request.user.department,
        state__in=[Transfer.State.READY, Transfer.State.IN_TRANSIT],
    ).select_related('reservation__asset', 'reservation__request__requester', 'to_department')
    return render(request, 'exchange/transfer_queue.html', {'transfers': transfers})


@login_required
@require_POST
def transfer_dispatch(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    try:
        ExchangeService.dispatch(transfer, request.user)
        messages.success(request, f'{transfer.code} dispatched.')
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('transfer_queue')


@login_required
@require_POST
def transfer_receive(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    try:
        ExchangeService.receive(transfer, request.user)
        messages.success(request, 'Receipt confirmed. The item now belongs to your department.')
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('request_detail', pk=transfer.reservation.request_id)


# --- Assistant (AI feature 4) -------------------------------------------------

@login_required
def assistant(request):
    require(request.user, 'assistant.use')
    history = request.session.get('assistant_history', [])
    if request.method == 'POST':
        if request.POST.get('action') == 'clear':
            history = []
        elif request.POST.get('question', '').strip():
            history.append(AssistantService().ask(request.user, request.POST['question']))
            history = history[-8:]
        request.session['assistant_history'] = history
        return redirect('assistant')
    # Items are loaded fresh, so one that was taken since the answer is not offered.
    wanted = {pk for turn in history for pk in turn['item_ids']}
    assets = {a.pk: a for a in Asset.objects.filter(
        pk__in=wanted, state=Asset.State.AVAILABLE).select_related('category', 'department')}
    turns = [{**turn, 'items': [assets[pk] for pk in turn['item_ids'] if pk in assets]}
             for turn in history]
    return render(request, 'exchange/assistant.html', {
        'turns': turns,
        'can_request': request.user.can('request.create'),
    })


# --- UC-18: holds that expire --------------------------------------------------

@login_required
def holds(request):
    require(request.user, 'expiry.run')
    active = ExchangeService._expiring_holds().select_related(
        'request__requester', 'request__department', 'asset__department', 'approval_case'
    ).order_by('expires_at')
    return render(request, 'exchange/holds.html', {
        'holds': active, 'now': timezone.now(), 'can_extend': request.user.can('hold.extend'),
        'demo': settings.DEBUG})


@login_required
@require_POST
def holds_run_expiry(request):
    require(request.user, 'expiry.run')
    now = timezone.now()
    # Demo mode only: act as if days have passed, so expiry can be shown live.
    if settings.DEBUG and request.POST.get('pretend_days'):
        try:
            now += timedelta(days=max(0, min(30, int(request.POST['pretend_days']))))
        except ValueError:
            pass
    result = ExchangeService.process_expiry(now)
    messages.success(request, f"Expiry check done: {result['expired']} hold(s) released, "
                     f"{result['warned']} warning(s), {result['reminded']} approval reminder(s).")
    return redirect('holds')


@login_required
@require_POST
def hold_extend(request, pk):
    reservation = get_object_or_404(Reservation, pk=pk)
    try:
        days = int(request.POST.get('days') or 0)
        ExchangeService.extend_hold(reservation, request.user, days, request.POST.get('reason', ''))
        messages.success(request, f'Hold on {reservation.asset.tag} extended by {days} day(s).')
    except ValueError:
        messages.error(request, 'Enter a number of days.')
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('holds')


# --- UC-19: receipt disputes ---------------------------------------------------

@login_required
@require_POST
def transfer_problem(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    try:
        ExchangeService.report_problem(
            transfer, request.user, request.POST.get('problem', ''), request.POST.get('note', ''))
        messages.success(request, 'Problem reported. An asset officer will resolve it; the item stays '
                         'with the sending department until then.')
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('request_detail', pk=transfer.reservation.request_id)


@login_required
def dispute_list(request):
    require(request.user, 'movements.view')
    disputes = Transfer.objects.filter(Q(state=Transfer.State.DISPUTED) | ~Q(resolution='')).select_related(
        'reservation__asset', 'reservation__request__requester', 'from_department', 'to_department',
        'resolved_by').order_by('-created_at')
    if not request.user.can('movements.all_departments'):
        disputes = disputes.filter(from_department=request.user.department)
    rows = [{'t': t, 'can_resolve': t.state == Transfer.State.DISPUTED
             and request.user.can('dispute.resolve', t)} for t in disputes[:200]]
    return render(request, 'exchange/disputes.html', {
        'rows': rows, 'resolutions': Transfer.Resolution.choices})


@login_required
@require_POST
def dispute_resolve(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    try:
        ExchangeService.resolve_dispute(
            transfer, request.user, request.POST.get('resolution', ''), request.POST.get('note', ''))
        messages.success(request, f'{transfer.code} resolved.')
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('dispute_list')
