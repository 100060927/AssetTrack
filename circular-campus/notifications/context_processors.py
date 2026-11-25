def unread_notifications(request):
    if not request.user.is_authenticated:
        return {}
    return {'unread_count': request.user.notifications.filter(read=False).count()}
