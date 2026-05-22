from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from reservas.models import SalonSubscription
from reservas.notifications import notify_admin_trials_ending_soon


class Command(BaseCommand):
    help = "Envía un aviso por email con las pruebas gratuitas próximas a vencer."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=3,
            help="Cantidad de días hacia adelante para buscar pruebas por vencer.",
        )

    def handle(self, *args, **options):
        days = options["days"]

        now = timezone.now()
        limit = now + timedelta(days=days)

        subscriptions = (
            SalonSubscription.objects
            .select_related("salon")
            .filter(
                status=SalonSubscription.Status.TRIAL,
                trial_ends_at__gte=now,
                trial_ends_at__lte=limit,
            )
            .order_by("trial_ends_at")
        )

        count = subscriptions.count()

        if count == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f"No hay pruebas gratuitas que venzan en los próximos {days} días."
                )
            )
            return

        notify_admin_trials_ending_soon(subscriptions)

        self.stdout.write(
            self.style.SUCCESS(
                f"Se envió aviso de {count} prueba/s próximas a vencer."
            )
        )