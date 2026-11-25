import csv
from datetime import date, timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import require
from core.errors import DomainError
from exchange.models import Reservation, ResourceRequest

from .models import ReportDraft
from accounts.models import Department, User

from .services import ImpactService, MovementService, ReportService


class PeriodForm(forms.Form):
    start = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))


def _period(request):
    today = timezone.localdate()
    form = PeriodForm(request.GET or None)
    if form.is_valid() and form.cleaned_data['start'] <= form.cleaned_data['end']:
        return form, form.cleaned_data['start'], form.cleaned_data['end']
    start, end = today - timedelta(days=30), today
    return PeriodForm(initial={'start': start, 'end': end}), start, end


@login_required
def dashboard(request):
    """C7: open to every signed-in user; cost is limited by BR-11."""
    form, start, end = _period(request)
    impact = ImpactService.calculate(start, end)
    if not request.user.can('asset.view_cost'):
        impact['measures'].pop('cost_avoided_aed')
    return render(request, 'reports/dashboard.html', {
        'form': form, 'impact': impact, 'start': start, 'end': end,
        'published': ReportDraft.objects.filter(status=ReportDraft.Status.PUBLISHED).first(),
    })


def _report_rows():
    """C9: every request, with one row per item reserved for it.

    A request still waiting for an item gets one row with the item and transfer
    columns empty, so the table lists all requests, not only those with transfers.
    """
    requests = ResourceRequest.objects.select_related(
        'requester', 'department').prefetch_related(
        'reservations__asset__department', 'reservations__transfer__from_department')
    for req in requests:
        base = {
            'request': req.code, 'title': req.title,
            'requester': req.requester.display_name,
            'to_department': req.department.name,
            'request_state': req.get_state_display(),
            'requested_qty': req.quantity,
        }
        reservations = list(req.reservations.all())
        if not reservations:
            yield {**base, 'asset': '', 'item': '', 'quantity': '', 'from_department': '',
                   'transfer': '', 'transfer_state': 'No item reserved yet',
                   'dispatched_at': None, 'received_at': None}
        for r in reservations:
            transfer = getattr(r, 'transfer', None)
            yield {
                **base,
                'asset': r.asset.tag, 'item': r.asset.name, 'quantity': r.quantity,
                'from_department': transfer.from_department.name if transfer else r.asset.department.name,
                'transfer': transfer.code if transfer else '',
                'transfer_state': transfer.get_state_display() if transfer else (
                    'Hold released' if r.state == Reservation.State.RELEASED else 'Waiting for approval'),
                'dispatched_at': transfer.dispatched_at if transfer else None,
                'received_at': transfer.received_at if transfer else None,
            }


@login_required
def report(request):
    """C9: requests and transfers, with CSV export."""
    require(request.user, 'report.view')
    rows = list(_report_rows())
    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="requests-transfers-{date.today()}.csv"'
        response.write('﻿')  # so Excel reads UTF-8 (Arabic text) correctly
        writer = csv.writer(response)
        if rows:
            writer.writerow(rows[0].keys())
        for row in rows:
            writer.writerow([
                timezone.localtime(v).strftime('%Y-%m-%d %H:%M') if hasattr(v, 'tzinfo') and v else
                ('' if v is None else v) for v in row.values()])
        return response
    return render(request, 'reports/report.html', {'rows': rows})


@login_required
def summaries(request):
    require(request.user, 'report.view')
    form = PeriodForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            draft = ReportService().generate(form.cleaned_data['start'], form.cleaned_data['end'], request.user)
            return redirect('summary_detail', pk=draft.pk)
        except DomainError as error:
            messages.error(request, str(error))
    elif request.method != 'POST':
        today = timezone.localdate()
        form = PeriodForm(initial={'start': today - timedelta(days=30), 'end': today})
    return render(request, 'reports/summaries.html', {'form': form, 'drafts': ReportDraft.objects.all()})


@login_required
def summary_detail(request, pk):
    require(request.user, 'report.view')
    draft = get_object_or_404(ReportDraft, pk=pk)
    return render(request, 'reports/summary_detail.html', {
        'draft': draft,
        'unverified': ReportService.unverified_numbers(draft),
        'can_publish': request.user.can('report.publish') and draft.status == ReportDraft.Status.DRAFT,
    })


@login_required
@require_POST
def summary_update(request, pk):
    draft = get_object_or_404(ReportDraft, pk=pk)
    try:
        ReportService.save_edit(draft, request.user, request.POST.get('text', ''))
        if request.POST.get('action') == 'publish':
            ReportService.publish(draft, request.user)
            messages.success(request, 'Summary published to the dashboard.')
        else:
            messages.success(request, 'Edits saved.')
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('summary_detail', pk=pk)


class MovementFilterForm(forms.Form):
    start = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    department = forms.ModelChoiceField(
        Department.objects.all(), required=False, empty_label='Any department', label='Taken by department')
    person = forms.ModelChoiceField(
        User.objects.none(), required=False, empty_label='Anyone', label='Taken by person')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['person'].queryset = User.objects.filter(
            received_transfers__isnull=False).distinct().order_by('first_name', 'username')
        self.fields['person'].label_from_instance = lambda u: f'{u.display_name} ({u.department.code if u.department else "-"})'


@login_required
def movements(request):
    """Who took which assets, by department and by person, with CSV export."""
    require(request.user, 'movements.view')
    form = MovementFilterForm(request.GET or None)
    filters = form.cleaned_data if form.is_valid() else {}
    start, end = filters.get('start'), filters.get('end')
    if start and end and start > end:
        start, end = end, start
    rows = MovementService.transfers_for(
        request.user, start if end else None, end if start else None,
        filters.get('department'), filters.get('person'))

    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="asset-movements-{date.today()}.csv"'
        response.write('\ufeff')
        writer = csv.writer(response)
        writer.writerow(['received', 'asset', 'item', 'category', 'quantity', 'value_aed',
                         'from_department', 'to_department', 'taken_by', 'request', 'transfer'])
        for t in rows:
            asset = t.reservation.asset
            value = t.received_qty * asset.replacement_unit_cost if asset.replacement_unit_cost is not None else ''
            writer.writerow([
                timezone.localtime(t.received_at).strftime('%Y-%m-%d %H:%M'), asset.tag, asset.name,
                asset.category.name, t.received_qty, value, t.from_department.name,
                t.to_department.name, t.received_by.display_name, t.reservation.request.code, t.code])
        return response

    return render(request, 'reports/movements.html', {
        'form': form,
        'rows': rows,
        'totals': MovementService.totals(rows),
        'by_department': MovementService.by_department(rows),
        'by_person': MovementService.by_person(rows),
        'all_departments': request.user.can('movements.all_departments'),
    })
