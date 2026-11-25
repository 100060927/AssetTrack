from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from exchange.services import ExchangeService


class Command(BaseCommand):
    help = 'UC-18: release expired holds, warn about holds ending soon, remind officers.'

    def add_arguments(self, parser):
        parser.add_argument('--pretend-days', type=int, default=0,
                            help='Act as if this many days have passed (for demos and testing).')

    def handle(self, *args, **options):
        now = timezone.now() + timedelta(days=options['pretend_days'])
        result = ExchangeService.process_expiry(now)
        self.stdout.write(self.style.SUCCESS(
            f"{result['expired']} hold(s) released, {result['warned']} warning(s) sent, "
            f"{result['reminded']} approval reminder(s) sent."))
