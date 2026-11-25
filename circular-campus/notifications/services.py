from .models import Notification


class NotificationService:
    """C8: in-app messages. Written in the caller's transaction, so a message
    is never sent for a change that rolled back."""

    @staticmethod
    def notify(recipients, message, url=''):
        unique = {user.pk: user for user in recipients}.values()
        Notification.objects.bulk_create(
            [Notification(recipient=user, message=message[:300], url=url) for user in unique])
