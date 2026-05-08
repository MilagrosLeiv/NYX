from uuid import uuid4

import mercadopago
from django.conf import settings
from django.urls import reverse


def build_payment_reference(booking):
    return f"NYX-{booking.salon_id}-{booking.id}-{uuid4().hex[:8].upper()}"


def build_absolute_url(path: str) -> str:
    # Ajustalo cuando pases a producción
    base_url = "http://127.0.0.1:8000"
    return f"{base_url}{path}"


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
        if not settings.MERCADOPAGO_ACCESS_TOKEN:
            raise ValueError("Falta configurar MERCADOPAGO_ACCESS_TOKEN.")

        sdk = mercadopago.SDK(settings.MERCADOPAGO_ACCESS_TOKEN)

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
            #"notification_url": build_absolute_url(reverse('payment_webhook')),
            #"back_urls": {
            #    "success": build_absolute_url(reverse('booking_success_booking', args=[booking.id])),
            #    "pending": build_absolute_url(reverse('booking_success_booking', args=[booking.id])),
            #    "failure": build_absolute_url(reverse('booking_success_booking', args=[booking.id])),
            #},
            #"auto_return": "approved",
        }

        response = sdk.preference().create(preference_data)

        status = response.get("status")
        preference = response.get("response", {})

        print("MERCADO PAGO RESPONSE:", response)

        if status not in [200, 201]:
            raise ValueError(f"Mercado Pago no pudo crear la preferencia: {preference}")

        checkout_url = preference.get("sandbox_init_point") or preference.get("init_point")
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