from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Notification


@login_required
def notification_list(request):
    items = request.user.notifications.all()[:100]
    return render(request, 'notifications/list.html', {'items': items})


@login_required
@require_POST
def notification_open(request, pk):
    item = get_object_or_404(Notification, pk=pk, recipient=request.user)
    item.read = True
    item.save(update_fields=['read'])
    return redirect(item.url or 'notifications')


@login_required
@require_POST
def notification_read_all(request):
    request.user.notifications.filter(read=False).update(read=True)
    return redirect('notifications')
