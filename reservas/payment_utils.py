from uuid import uuid4

import mercadopago
from django.conf import settings
from django.urls import reverse


def build_payment_reference(booking):
    return f"NYX-{booking.salon_id}-{booking.id}-{uuid4().hex[:8].upper()}"


def build_absolute_url(path: str) -> str:
    base = settings.SITE_URL.rstrip("/")
    path = path if path.startswith("/") else f"/{path}"
    return f"{base}{path}"

def create_pending_payment_session(booking):
    reference = build_payment_reference(booking)

    # Transferencia manual
    if booking.selected_payment_method == 'transfer':
        booking.payment_provider = 'transfer'
        booking.external_payment_id = reference
        booking.payment_reference = reference
        booking.payment_checkout_url = build_absolute_url(
            reverse('booking_payment', args=[booking.id])
        )
        booking.save(update_fields=[
            'payment_provider',
            'external_payment_id',
            'payment_reference',
            'payment_checkout_url',
        ])
        return {
            'provider': booking.payment_provider,
            'external_payment_id': booking.external_payment_id,
            'reference': booking.payment_reference,
            'checkout_url': booking.payment_checkout_url,
        }

    # Pago integrado con Mercado Pago
    if booking.selected_payment_method == 'integrated':
        payment_settings = getattr(booking.salon, "payment_settings", None)

        if not payment_settings:
            raise ValueError("El salón no tiene configuración de pagos.")

        if not payment_settings.has_valid_mercadopago_connection():
            raise ValueError("El salón no tiene Mercado Pago conectado correctamente.")

        sdk = mercadopago.SDK(payment_settings.mp_access_token)

        reference = build_payment_reference(booking)

        preference_data = {
            "items": [
                {
                    "title": f"Reserva #{booking.id} - {booking.salon.name}",
                    "quantity": 1,
                    "currency_id": "ARS",
                    "unit_price": float(booking.payment_required_amount),
                }
            ],
            "external_reference": reference,
            "notification_url": (
                build_absolute_url(reverse("payment_webhook"))
                + f"?booking_id={booking.id}"
            ),
            "back_urls": {
                "success": build_absolute_url(reverse('booking_success_booking', args=[booking.id])),
                "pending": build_absolute_url(reverse('booking_success_booking', args=[booking.id])),
                "failure": build_absolute_url(reverse('booking_success_booking', args=[booking.id])),
            },
            "auto_return": "approved",
        }
        print("MP NOTIFICATION URL:", preference_data.get("notification_url"))
        response = sdk.preference().create(preference_data)

        status = response.get("status")
        preference = response.get("response", {})

        print("MERCADO PAGO RESPONSE:", response)

        if status not in [200, 201]:
            raise ValueError(f"Mercado Pago no pudo crear la preferencia: {preference}")

        if settings.MERCADOPAGO_USE_SANDBOX:
            checkout_url = preference.get("sandbox_init_point")
        else:
            checkout_url = preference.get("init_point")
        preference_id = preference.get("id", "")

        if not checkout_url:
            raise ValueError(f"Mercado Pago no devolvió checkout_url. Respuesta: {preference}")

        if not preference_id:
            raise ValueError(f"Mercado Pago no devolvió preference_id. Respuesta: {preference}")

        booking.payment_provider = 'mercadopago'
        booking.external_payment_id = preference_id
        booking.payment_reference = reference
        booking.payment_checkout_url = checkout_url

        booking.save(update_fields=[
            'payment_provider',
            'external_payment_id',
            'payment_reference',
            'payment_checkout_url',
        ])

        return {
            'provider': booking.payment_provider,
            'external_payment_id': booking.external_payment_id,
            'reference': booking.payment_reference,
            'checkout_url': booking.payment_checkout_url,
        }

    # Fallback
    booking.payment_provider = ''
    booking.external_payment_id = reference
    booking.payment_reference = reference
    booking.payment_checkout_url = build_absolute_url(
        reverse('booking_payment', args=[booking.id])
    )
    booking.save(update_fields=[
        'payment_provider',
        'external_payment_id',
        'payment_reference',
        'payment_checkout_url',
    ])

    return {
        'provider': booking.payment_provider,
        'external_payment_id': booking.external_payment_id,
        'reference': booking.payment_reference,
        'checkout_url': booking.payment_checkout_url,
    }