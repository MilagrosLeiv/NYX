import logging
import math


from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone


logger = logging.getLogger(__name__)


def notify_admin_new_trial_account(user, salon=None):
    """
    Envía un aviso interno cuando se registra una nueva cuenta de prueba.
    No debe bloquear el flujo de registro si el email falla.
    """

    admin_email = getattr(settings, "ADMIN_NOTIFICATION_EMAIL", None)

    if not admin_email:
        logger.warning("ADMIN_NOTIFICATION_EMAIL no está configurado.")
        return

    salon_name = (
        getattr(salon, "name", None)
        or getattr(salon, "nombre", None)
        or getattr(salon, "salon_name", None)
        or "No informado"
    )

    username = getattr(user, "username", "No informado")
    email = getattr(user, "email", "") or "No informado"

    subject = "Nueva cuenta de prueba registrada en NYX"

    message = f"""
Se registró una nueva cuenta de prueba en NYX.

Datos de la cuenta:
- Usuario: {username}
- Email: {email}
- Peluquería / salón: {salon_name}

Acción sugerida:
Revisar el alta en el panel de administración y hacer seguimiento comercial.
""".strip()

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[admin_email],
            fail_silently=False,
        )
    except Exception:
        logger.exception(
            "Error enviando notificación de nueva cuenta de prueba para usuario %s",
            username,
        )

logger = logging.getLogger(__name__)


def notify_admin_trials_ending_soon(subscriptions):
    """
    Envía un resumen al admin con las pruebas gratuitas próximas a vencer.
    No debe romper el proceso si falla el envío.
    """

    admin_email = getattr(settings, "ADMIN_NOTIFICATION_EMAIL", None)

    if not admin_email:
        logger.warning("ADMIN_NOTIFICATION_EMAIL no está configurado.")
        return

    subscriptions = list(subscriptions)

    if not subscriptions:
        logger.info("No hay pruebas próximas a vencer.")
        return

    lines = [
        "Hay cuentas de prueba próximas a vencer en NYX.",
        "",
        "Detalle:",
        "",
    ]

    now = timezone.now()

    for sub in subscriptions:
        salon = sub.salon
        trial_ends_at = timezone.localtime(sub.trial_ends_at)

        remaining_seconds = max(0, (sub.trial_ends_at - now).total_seconds())
        days_left = math.ceil(remaining_seconds / 86400)

        lines.append(f"- Salón: {salon.name}")
        lines.append(f"  Estado: {sub.get_status_display()}")
        lines.append(f"  Plan: {sub.get_plan_display()}")
        lines.append(f"  Vence: {trial_ends_at.strftime('%d/%m/%Y %H:%M')}")
        lines.append(f"  Días restantes: {days_left}")
        lines.append("")

    lines.extend([
        "Acción sugerida:",
        "Contactar a estos salones para ofrecer la activación del plan mensual.",
    ])

    subject = "Pruebas gratuitas próximas a vencer en NYX"
    message = "\n".join(lines)

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[admin_email],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Error enviando aviso de pruebas próximas a vencer.")