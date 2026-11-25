from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.db import transaction
from django.shortcuts import redirect, render

from audit.services import audit
from notifications.services import NotificationService


@login_required
def password_change(request):
    """Every signed-in user can change their own password.

    The old password must be typed first. The new one is checked by the
    password rules in settings (length, not common, not all numbers, not like
    the username) and stored only as a salted PBKDF2-SHA256 hash.
    """
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            user = form.save()
            audit(user, 'Password changed', user, 'Changed by the user')
            NotificationService.notify(
                [user], 'Your password was changed. If this was not you, contact the administrator.')
        update_session_auth_hash(request, user)  # stay signed in on this browser
        messages.success(request, 'Your password was changed.')
        return redirect('home')
    return render(request, 'registration/password_change.html', {'form': form})
