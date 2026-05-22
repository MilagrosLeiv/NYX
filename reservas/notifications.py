import logging

from django.conf import settings
from django.core.mail import send_mail


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