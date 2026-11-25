import csv
from datetime import datetime, time

from django import forms
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import User
from accounts.permissions import require
from core.errors import DomainError

from . import backup
from .models import AuditEntry


class AuditFilter(forms.Form):
    q = forms.CharField(label='Search', required=False)
    record_type = forms.ChoiceField(label='Record', required=False)
    actor = forms.ModelChoiceField(User.objects.order_by('username'), label='Who', required=False,
                                   empty_label='Anyone')
    start = forms.DateField(label='From', required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(label='To', required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        kinds = AuditEntry.objects.order_by('record_type').values_list('record_type', flat=True).distinct()
        self.fields['record_type'].choices = [('', 'All records')] + [(k, k) for k in kinds]


@login_required
def audit_log(request):
    """UC-26: one page to search every change in the system."""
    require(request.user, 'audit.view')
    form = AuditFilter(request.GET or None)
    entries = AuditEntry.objects.select_related('actor')
    if form.is_valid():
        f = form.cleaned_data
        for word in f['q'].split():
            entries = entries.filter(Q(record_label__icontains=word) | Q(details__icontains=word)
                                     | Q(action__icontains=word))
        if f['record_type']:
            entries = entries.filter(record_type=f['record_type'])
        if f['actor']:
            entries = entries.filter(actor=f['actor'])
        tz = timezone.get_current_timezone()
        if f['start']:
            entries = entries.filter(created_at__gte=datetime.combine(f['start'], time.min, tz))
        if f['end']:
            entries = entries.filter(created_at__lte=datetime.combine(f['end'], time.max, tz))
    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="audit-log.csv"'
        writer = csv.writer(response)
        writer.writerow(['When', 'Who', 'Action', 'Record type', 'Record', 'Details'])
        for e in entries.iterator():
            writer.writerow([timezone.localtime(e.created_at).strftime('%Y-%m-%d %H:%M:%S'),
                             e.actor.username if e.actor else 'System', e.action,
                             e.record_type, e.record_label, e.details])
        return response
    total = entries.count()
    return render(request, 'audit/log.html', {'form': form, 'entries': entries[:300], 'total': total})


@login_required
def backup_page(request):
    """UC-30: save and restore the whole database (administrator only)."""
    require(request.user, 'backup.manage')
    return render(request, 'audit/backup.html', {
        'backups': backup.list_backups(), 'folder': backup.backup_dir()})


@login_required
@require_POST
def backup_create(request):
    try:
        path, rows = backup.create_backup(request.user)
        messages.success(request, f'Backup saved: {path.name} ({rows} records).')
    except DomainError as error:
        messages.error(request, str(error))
    return redirect('backup')


@login_required
def backup_download(request, name):
    require(request.user, 'backup.manage')
    try:
        path = backup.path_for(name)
    except DomainError:
        return HttpResponse('Not found', status=404)
    return FileResponse(open(path, 'rb'), as_attachment=True, filename=path.name)


@login_required
@require_POST
def backup_restore(request):
    require(request.user, 'backup.manage')
    try:
        safety, check = backup.restore_backup(
            request.user, request.POST.get('name', ''), request.POST.get('confirm', '').strip())
    except DomainError as error:
        messages.error(request, str(error))
        return redirect('backup')
    different = [f'{label}: {a} expected, {b} found' for label, (a, b) in check.items() if a != b]
    # Sessions were emptied by the restore, so everyone signs in again.
    logout(request)
    if different:
        messages.warning(request, 'Restored, but some counts differ: ' + '; '.join(different))
    else:
        messages.success(request, f'Database restored and verified ({sum(a for a, _ in check.values())} records). '
                         f'A safety copy of the old data was saved as {safety.name}. Please sign in again.')
    return redirect('login')
