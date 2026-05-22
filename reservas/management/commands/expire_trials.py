from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from reservas.models import SalonSubscription


class Command(BaseCommand):
    help = "Suspende automáticamente las pruebas gratuitas vencidas."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Muestra qué suscripciones se suspenderían sin modificar la base de datos.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        now = timezone.now()

        expired_trials = (
            SalonSubscription.objects
            .select_related("salon")
            .filter(
                status=SalonSubscription.Status.TRIAL,
                trial_ends_at__lt=now,
            )
            .order_by("trial_ends_at")
        )

        count = expired_trials.count()

        if count == 0:
            self.stdout.write(
                self.style.SUCCESS("No hay pruebas gratuitas vencidas para suspender.")
            )
            return

        self.stdout.write(
            self.style.WARNING(f"Se encontraron {count} prueba/s vencida/s.")
        )

        for subscription in expired_trials:
            self.stdout.write(
                f"- {subscription.salon.name} | venció: {timezone.localtime(subscription.trial_ends_at).strftime('%d/%m/%Y %H:%M')}"
            )

        if dry_run:
            self.stdout.write(
                self.style.WARNING("Modo dry-run activo. No se modificó ninguna suscripción.")
            )
            return

        with transaction.atomic():
            updated = expired_trials.update(
                status=SalonSubscription.Status.SUSPENDED,
                updated_at=now,
            )

        self.stdout.write(
            self.style.SUCCESS(f"Se suspendieron {updated} prueba/s vencida/s.")
        )