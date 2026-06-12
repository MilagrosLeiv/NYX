from datetime import timedelta, timezone as datetime_timezone
import logging

from django.conf import settings
from django.utils import timezone

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


logger = logging.getLogger(__name__)

GOOGLE_CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
]


def get_google_credentials(integration):
    expiry = integration.token_expiry
    if expiry and timezone.is_aware(expiry):
        expiry = expiry.astimezone(datetime_timezone.utc).replace(tzinfo=None)

    return Credentials(
        token=integration.access_token,
        refresh_token=integration.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=GOOGLE_CALENDAR_SCOPES,
        expiry=expiry,
    )


def get_calendar_service(integration):
    credentials = get_google_credentials(integration)
    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    return service, credentials


def persist_google_credentials(integration, credentials):
    update_fields = []
    credential_expiry = credentials.expiry
    if credential_expiry and timezone.is_naive(credential_expiry):
        credential_expiry = timezone.make_aware(
            credential_expiry,
            datetime_timezone.utc,
        )

    if credentials.token and credentials.token != integration.access_token:
        integration.access_token = credentials.token
        update_fields.append("access_token")

    if credential_expiry != integration.token_expiry:
        integration.token_expiry = credential_expiry
        update_fields.append("token_expiry")

    if update_fields:
        update_fields.append("updated_at")
        integration.save(update_fields=update_fields)


def build_booking_item_event(item):
    booking = item.booking
    salon = booking.salon
    service = item.service
    employee = item.employee

    start = item.start_datetime
    end = item.end_datetime
    if not end:
        duration_minutes = getattr(service, "duration_minutes", None) or 30
        end = start + timedelta(minutes=duration_minutes)

    client_name = booking.customer_name or "Cliente"
    client_phone = booking.customer_phone or ""
    client_email = booking.customer_email or ""

    service_name = service.name if service else "Servicio"
    employee_name = employee.name if employee else "Profesional"

    summary = f"{service_name} - {client_name}"

    description_parts = [
        "Reserva creada desde NYX.",
        "",
        f"Salón: {salon.name}",
        f"Cliente: {client_name}",
    ]

    if client_phone:
        description_parts.append(f"Teléfono: {client_phone}")

    if client_email:
        description_parts.append(f"Email: {client_email}")

    description_parts.extend([
        f"Servicio: {service_name}",
        f"Profesional: {employee_name}",
        f"Estado: {booking.get_status_display()}",
    ])

    return {
        "summary": summary,
        "description": "\n".join(description_parts),
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": settings.TIME_ZONE,
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": settings.TIME_ZONE,
        },
    }


def clear_google_calendar_event_data(item):
    item.google_calendar_event_id = None
    item.google_calendar_synced_at = None
    item.save(update_fields=[
        "google_calendar_event_id",
        "google_calendar_synced_at",
    ])


def sync_booking_item_to_google_calendar(item):
    try:
        booking = item.booking
        salon = booking.salon
        integration = salon.google_calendar_integration

        if not integration.is_active or not integration.is_connected():
            return False

        should_sync = False

        if booking.status == "confirmed" and integration.sync_confirmed_bookings:
            should_sync = True

        if booking.status == "pending" and integration.sync_pending_bookings:
            should_sync = True

        if not should_sync or not item.start_datetime:
            return False

        service, credentials = get_calendar_service(integration)
        event_body = build_booking_item_event(item)
        calendar_id = integration.calendar_id or "primary"
        if item.google_calendar_event_id:
            try:
                event = service.events().patch(
                    calendarId=calendar_id,
                    eventId=item.google_calendar_event_id,
                    body=event_body,
                ).execute()
            except HttpError as error:
                if error.resp.status not in {404, 410}:
                    raise
                event = service.events().insert(
                    calendarId=calendar_id,
                    body=event_body,
                ).execute()
        else:
            event = service.events().insert(
                calendarId=calendar_id,
                body=event_body,
            ).execute()

        item.google_calendar_event_id = event.get("id")
        item.google_calendar_synced_at = timezone.now()
        item.save(update_fields=[
            "google_calendar_event_id",
            "google_calendar_synced_at",
        ])

        persist_google_credentials(integration, credentials)
        return True

    except Exception:
        logger.exception(
            "No se pudo sincronizar BookingItem %s con Google Calendar.",
            getattr(item, "pk", None),
        )
        return False


def delete_booking_item_from_google_calendar(item):
    try:
        if not item.google_calendar_event_id:
            return False

        booking = item.booking
        salon = booking.salon
        integration = salon.google_calendar_integration

        if not integration.is_connected():
            return False

        service, credentials = get_calendar_service(integration)
        calendar_id = integration.calendar_id or "primary"
        try:
            service.events().delete(
                calendarId=calendar_id,
                eventId=item.google_calendar_event_id,
            ).execute()
        except HttpError as error:
            if error.resp.status not in {404, 410}:
                raise

        clear_google_calendar_event_data(item)
        persist_google_credentials(integration, credentials)
        return True

    except Exception:
        logger.exception(
            "No se pudo eliminar BookingItem %s de Google Calendar.",
            getattr(item, "pk", None),
        )
        return False


def sync_booking_to_google_calendar(booking):
    results = [
        sync_booking_item_to_google_calendar(item)
        for item in booking.items.select_related("service", "employee")
    ]
    return bool(results) and all(results)


def delete_booking_from_google_calendar(booking):
    results = [
        delete_booking_item_from_google_calendar(item)
        for item in booking.items.select_related("service", "employee")
        if item.google_calendar_event_id
    ]
    return bool(results) and all(results)
