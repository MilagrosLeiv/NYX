import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Booking
from .services.google_calendar import (
    delete_booking_from_google_calendar,
    sync_booking_to_google_calendar,
)


logger = logging.getLogger(__name__)


def _sync_booking_after_commit(booking_id):
    try:
        booking = Booking.objects.prefetch_related(
            "items__service",
            "items__employee",
        ).get(pk=booking_id)

        if booking.status == "confirmed":
            sync_booking_to_google_calendar(booking)
        elif booking.status == "cancelled":
            delete_booking_from_google_calendar(booking)
    except Booking.DoesNotExist:
        return
    except Exception:
        logger.exception(
            "No se pudo procesar Google Calendar para la reserva %s.",
            booking_id,
        )


@receiver(
    post_save,
    sender=Booking,
    dispatch_uid="reservas.sync_booking_status_with_google_calendar",
)
def sync_booking_status_with_google_calendar(sender, instance, **kwargs):
    if instance.status not in {"confirmed", "cancelled"}:
        return

    transaction.on_commit(
        lambda booking_id=instance.pk: _sync_booking_after_commit(booking_id)
    )
