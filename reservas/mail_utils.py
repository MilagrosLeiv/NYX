from django.conf import settings
from django.core.mail import send_mail, EmailMultiAlternatives
from django.urls import reverse
from django.template.loader import render_to_string
from collections import defaultdict


def send_booking_confirmed_email(booking, request=None):
    customer_email = (booking.customer_email or "").strip()
    from_email = (settings.DEFAULT_FROM_EMAIL or "").strip()

    if not customer_email or not from_email:
        return False

    booking_items = booking.items.select_related('service', 'employee').all()
    if not booking_items:
        return False

    services_text = ', '.join(item.service.name for item in booking_items)

    professionals_text_plain = ' | '.join(
        f'{item.service.name}: {item.employee.name}' for item in booking_items
    )

    professionals_text_html = '<br>'.join(
        f'{item.service.name}: {item.employee.name}' for item in booking_items
    )

    first_item = booking_items.order_by('start_datetime').first()
    if not first_item:
        return False

    fecha_formateada = first_item.start_datetime.strftime("%d/%m/%Y")
    hora_inicio = first_item.start_datetime.strftime("%H:%M")

    manage_url = ""

    if booking.client_manage_token:
        path = reverse('manage_booking', args=[booking.client_manage_token])

        if request:
            manage_url = request.build_absolute_uri(path)
        else:
            site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
            if site_url:
                manage_url = f"{site_url}{path}"

    manage_button_html = ""

    if manage_url:
        manage_button_html = f"""
            <div style="text-align:center; margin:28px 0 8px;">
                <a href="{manage_url}"
                   style="display:inline-block; background:#0f2d3a; color:#ffffff; text-decoration:none; padding:14px 22px; border-radius:999px; font-size:14px; font-weight:700;">
                    Gestionar mi turno
                </a>
            </div>

            <p style="margin:14px 0 0; font-size:13px; line-height:1.6; color:#6b7280; text-align:center;">
                Podés cancelar tu turno online hasta {booking.salon.cancellation_limit_hours} horas antes del horario reservado.
            </p>
        """

    payment_text_plain = ""
    payment_text_html = ""

    if booking.payment_choice == 'deposit' and booking.payment_required_amount > 0:
        amount_formatted = f"{int(booking.payment_required_amount):,}".replace(",", ".")

        payment_text_plain = f"- Seña abonada: ${amount_formatted}\n"

        payment_text_html = f"""
            <tr>
                <td style="padding:10px 0; font-size:14px; color:#6b7280;">Seña abonada</td>
                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                    ${amount_formatted}
                </td>
            </tr>
        """

    elif booking.payment_choice == 'full' and booking.payment_required_amount > 0:
        amount_formatted = f"{int(booking.payment_required_amount):,}".replace(",", ".")

        payment_text_plain = f"- Pago confirmado: ${amount_formatted}\n"

        payment_text_html = f"""
            <tr>
                <td style="padding:10px 0; font-size:14px; color:#6b7280;">Pago confirmado</td>
                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                    ${amount_formatted}
                </td>
            </tr>
        """

    if manage_url:
        manage_text_plain = (
            f"Podés gestionar tu turno desde este link:\n"
            f"{manage_url}\n\n"
            f"Podés cancelar tu turno online hasta {booking.salon.cancellation_limit_hours} horas antes del horario reservado.\n\n"
        )
    else:
        manage_text_plain = (
            f"Si necesitás modificar o cancelar tu turno, comunicate con anticipación con el salón.\n\n"
        )

    plain_message = (
        f'{booking.salon.name}\n'
        f'Turno confirmado\n\n'
        f'Hola {booking.customer_name}, tu turno quedó confirmado.\n\n'
        f'Resumen de tu reserva:\n'
        f'- Servicios: {services_text}\n'
        f'- Profesionales: {professionals_text_plain}\n'
        f'- Fecha: {fecha_formateada}\n'
        f'- Hora de inicio: {hora_inicio}\n'
        f'- Teléfono: {booking.customer_phone}\n'
        f'{payment_text_plain}\n'
        f'{manage_text_plain}'
        f'Gracias por reservar con nosotros.'
    )

    html_message = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>Turno confirmado</title>
    </head>
    <body style="margin:0; padding:0; background-color:#eef4f4; font-family:Arial, Helvetica, sans-serif; color:#1f2937;">
        <div style="width:100%; background-color:#eef4f4; padding:32px 16px;">
            <div style="max-width:620px; margin:0 auto; background-color:#ffffff; border-radius:22px; overflow:hidden; box-shadow:0 10px 30px rgba(15, 23, 42, 0.08); border:1px solid #dbe7e7;">

                <div style="background:linear-gradient(135deg, #0f2d3a 0%, #18495c 100%); padding:32px 32px 26px;">
                    <div style="font-size:28px; font-weight:700; color:#ffffff; line-height:1.2;">
                        {booking.salon.name}
                    </div>
                    <div style="margin-top:8px; font-size:14px; color:#c7d9df; letter-spacing:0.2px;">
                        Turno confirmado
                    </div>
                </div>

                <div style="padding:32px;">
                    <p style="margin:0 0 18px; font-size:17px; line-height:1.6; color:#1f2937;">
                        Hola <strong>{booking.customer_name}</strong>, tu turno quedó confirmado.
                    </p>

                    <div style="margin:0 0 24px; padding:16px 18px; background-color:#ecfeff; border:1px solid #b6ecef; border-radius:14px;">
                        <div style="font-size:14px; font-weight:700; color:#0f766e; margin-bottom:6px;">
                            Reserva confirmada
                        </div>
                        <div style="font-size:14px; line-height:1.6; color:#155e63;">
                            Guardá este correo para tener a mano los datos de tu turno.
                        </div>
                    </div>

                    <div style="background-color:#f9fbfb; border:1px solid #e3ecec; border-radius:18px; padding:22px; margin-bottom:24px;">
                        <div style="font-size:16px; font-weight:700; color:#0f2d3a; margin-bottom:16px;">
                            Resumen de tu reserva
                        </div>

                        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280; width:170px; vertical-align:top;">Servicios</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600; line-height:1.6;">
                                    {services_text}
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280; vertical-align:top;">Profesionales</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600; line-height:1.6;">
                                    {professionals_text_html}
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280;">Fecha</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                                    {fecha_formateada}
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280;">Hora de inicio</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                                    {hora_inicio}
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280;">Teléfono</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                                    {booking.customer_phone}
                                </td>
                            </tr>
                            {payment_text_html}
                        </table>
                    </div>

                    {manage_button_html}

                    <p style="margin:26px 0 0; font-size:14px; line-height:1.7; color:#6b7280; text-align:center;">
                        Gracias por reservar con nosotros.
                    </p>
                </div>

                <div style="border-top:1px solid #e5e7eb; background-color:#fafafa; padding:20px 32px; text-align:center;">
                    <div style="font-size:12px; color:#9ca3af;">
                        Este es un mensaje automático de confirmación de reserva.
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    send_mail(
        subject=f'Turno confirmado en {booking.salon.name}',
        message=plain_message,
        from_email=from_email,
        recipient_list=[customer_email],
        fail_silently=False,
        html_message=html_message,
    )

    return True


def send_booking_payment_pending_email(booking, request=None):
    customer_email = (booking.customer_email or "").strip()
    from_email = (settings.DEFAULT_FROM_EMAIL or "").strip()

    if not customer_email or not from_email:
        return False

    salon = booking.salon

    booking_items = booking.items.select_related('service', 'employee').all()
    if not booking_items:
        return False

    services_text = ', '.join(item.service.name for item in booking_items)

    professionals_text_plain = ' | '.join(
        f'{item.service.name}: {item.employee.name}' for item in booking_items
    )

    professionals_text_html = '<br>'.join(
        f'{item.service.name}: {item.employee.name}' for item in booking_items
    )

    first_item = booking_items.order_by('start_datetime').first()
    if not first_item:
        return False

    fecha_formateada = first_item.start_datetime.strftime("%d/%m/%Y")
    hora_inicio = first_item.start_datetime.strftime("%H:%M")

    manage_url = ""

    if booking.client_manage_token:
        path = reverse('manage_booking', args=[booking.client_manage_token])

        if request:
            manage_url = request.build_absolute_uri(path)
        else:
            site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
            if site_url:
                manage_url = f"{site_url}{path}"

    manage_button_html = ""

    if manage_url:
        manage_button_html = f"""
            <div style="text-align:center; margin:28px 0 8px;">
                <a href="{manage_url}"
                   style="display:inline-block; background:#0f2d3a; color:#ffffff; text-decoration:none; padding:14px 22px; border-radius:999px; font-size:14px; font-weight:700;">
                    Gestionar mi reserva
                </a>
            </div>

            <p style="margin:14px 0 0; font-size:13px; line-height:1.6; color:#6b7280; text-align:center;">
                Desde ese link podés consultar o cancelar tu reserva, siempre que esté dentro del plazo permitido por el salón.
            </p>
        """

        manage_text_plain = (
            f"Podés gestionar tu reserva desde este link:\n"
            f"{manage_url}\n\n"
        )
    else:
        manage_text_plain = ""

    payment_label = "Pago pendiente"

    if booking.payment_choice == 'deposit':
        payment_label = "Seña pendiente"
    elif booking.payment_choice == 'full':
        payment_label = "Pago pendiente"

    amount_formatted = f"{int(booking.payment_required_amount):,}".replace(",", ".")

    # =========================
    # Datos de transferencia
    # =========================
    transfer_lines_plain = []

    if getattr(salon, "transfer_account_holder", ""):
        transfer_lines_plain.append(f"Titular: {salon.transfer_account_holder}")

    if getattr(salon, "transfer_bank_name", ""):
        transfer_lines_plain.append(f"Banco/Billetera: {salon.transfer_bank_name}")

    if getattr(salon, "transfer_alias", ""):
        transfer_lines_plain.append(f"Alias: {salon.transfer_alias}")

    if getattr(salon, "transfer_cbu", ""):
        transfer_lines_plain.append(f"CBU/CVU: {salon.transfer_cbu}")

    if getattr(salon, "transfer_tax_id", ""):
        transfer_lines_plain.append(f"CUIT/CUIL: {salon.transfer_tax_id}")

    if getattr(salon, "transfer_extra_instructions", ""):
        transfer_lines_plain.append("")
        transfer_lines_plain.append(salon.transfer_extra_instructions)
    elif getattr(salon, "payment_instructions", ""):
        transfer_lines_plain.append("")
        transfer_lines_plain.append(salon.payment_instructions)

    transfer_instructions_plain = "\n".join(transfer_lines_plain).strip()

    if not transfer_instructions_plain:
        transfer_instructions_plain = (
            "El salón todavía no cargó instrucciones detalladas de transferencia. "
            "Comunicate con el salón para solicitar los datos de pago."
        )

    transfer_rows_html = ""

    if getattr(salon, "transfer_account_holder", ""):
        transfer_rows_html += f"""
            <tr>
                <td style="padding:9px 0; font-size:14px; color:#6b7280;">Titular</td>
                <td style="padding:9px 0; font-size:14px; color:#111827; font-weight:700; text-align:right;">
                    {salon.transfer_account_holder}
                </td>
            </tr>
        """

    if getattr(salon, "transfer_bank_name", ""):
        transfer_rows_html += f"""
            <tr>
                <td style="padding:9px 0; font-size:14px; color:#6b7280;">Banco/Billetera</td>
                <td style="padding:9px 0; font-size:14px; color:#111827; font-weight:700; text-align:right;">
                    {salon.transfer_bank_name}
                </td>
            </tr>
        """

    if getattr(salon, "transfer_alias", ""):
        transfer_rows_html += f"""
            <tr>
                <td style="padding:9px 0; font-size:14px; color:#6b7280;">Alias</td>
                <td style="padding:9px 0; font-size:14px; color:#111827; font-weight:700; text-align:right;">
                    {salon.transfer_alias}
                </td>
            </tr>
        """

    if getattr(salon, "transfer_cbu", ""):
        transfer_rows_html += f"""
            <tr>
                <td style="padding:9px 0; font-size:14px; color:#6b7280;">CBU/CVU</td>
                <td style="padding:9px 0; font-size:14px; color:#111827; font-weight:700; text-align:right;">
                    {salon.transfer_cbu}
                </td>
            </tr>
        """

    if getattr(salon, "transfer_tax_id", ""):
        transfer_rows_html += f"""
            <tr>
                <td style="padding:9px 0; font-size:14px; color:#6b7280;">CUIT/CUIL</td>
                <td style="padding:9px 0; font-size:14px; color:#111827; font-weight:700; text-align:right;">
                    {salon.transfer_tax_id}
                </td>
            </tr>
        """

    extra_transfer_html = ""

    if getattr(salon, "transfer_extra_instructions", ""):
        extra_transfer_html = salon.transfer_extra_instructions.replace(chr(10), "<br>")
    elif getattr(salon, "payment_instructions", ""):
        extra_transfer_html = salon.payment_instructions.replace(chr(10), "<br>")

    if not transfer_rows_html and not extra_transfer_html:
        extra_transfer_html = (
            "El salón todavía no cargó instrucciones detalladas de transferencia. "
            "Comunicate con el salón para solicitar los datos de pago."
        )

    expiration_text = ""
    expiration_html = ""

    if booking.payment_expires_at:
        expiration_local = booking.payment_expires_at.strftime("%d/%m/%Y %H:%M")

        expiration_text = f"- Vence: {expiration_local}\n"

        expiration_html = f"""
            <tr>
                <td style="padding:10px 0; font-size:14px; color:#6b7280;">Vence</td>
                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                    {expiration_local}
                </td>
            </tr>
        """

    if booking.selected_payment_method == 'transfer':
        plain_intro = (
            f'Hola {booking.customer_name}, tu turno fue reservado correctamente.\n\n'
            f'Para mantener la reserva, realizá el pago indicado por el salón. '
            f'Si el pago no se acredita, el salón puede cancelar el turno.\n\n'
        )
        plain_footer = (
            f'El turno ya quedó reservado en la agenda. '
            f'Guardá este correo para tener a mano los datos de pago.'
        )
        plain_title = 'Turno reservado - pago pendiente'
    else:
        plain_intro = (
            f'Hola {booking.customer_name}, tu reserva fue creada pero todavía no está confirmada.\n\n'
            f'Para confirmar tu turno tenés que completar el pago indicado por el salón.\n\n'
        )
        plain_footer = 'Una vez validado el pago, te llegará la confirmación final del turno.'
        plain_title = 'Reserva creada - pago pendiente'

    plain_message = (
        f'{salon.name}\n'
        f'{plain_title}\n\n'
        f'{plain_intro}'
        f'Resumen de tu reserva:\n'
        f'- Servicios: {services_text}\n'
        f'- Profesionales: {professionals_text_plain}\n'
        f'- Fecha: {fecha_formateada}\n'
        f'- Hora de inicio: {hora_inicio}\n'
        f'- Teléfono: {booking.customer_phone}\n'
        f'- {payment_label}: ${amount_formatted}\n'
        f'{expiration_text}\n'
        f'{manage_text_plain}'
        f'Datos para transferencia:\n'
        f'{transfer_instructions_plain}\n\n'
        f'{plain_footer}'
    )

    html_message = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>{plain_title}</title>
    </head>
    <body style="margin:0; padding:0; background-color:#eef4f4; font-family:Arial, Helvetica, sans-serif; color:#1f2937;">
        <div style="width:100%; background-color:#eef4f4; padding:32px 16px;">
            <div style="max-width:620px; margin:0 auto; background-color:#ffffff; border-radius:22px; overflow:hidden; box-shadow:0 10px 30px rgba(15, 23, 42, 0.08); border:1px solid #dbe7e7;">

                <div style="background:linear-gradient(135deg, #0f2d3a 0%, #18495c 100%); padding:32px 32px 26px;">
                    <div style="font-size:28px; font-weight:700; color:#ffffff; line-height:1.2;">
                        {salon.name}
                    </div>
                    <div style="margin-top:8px; font-size:14px; color:#c7d9df; letter-spacing:0.2px;">
                        {plain_title}
                    </div>
                </div>

                <div style="padding:32px;">

                    {f'''
                    <p style="margin:0 0 18px; font-size:17px; line-height:1.6; color:#1f2937;">
                        Hola <strong>{booking.customer_name}</strong>, tu turno fue reservado correctamente.
                    </p>
                    ''' if booking.selected_payment_method == 'transfer' else f'''
                    <p style="margin:0 0 18px; font-size:17px; line-height:1.6; color:#1f2937;">
                        Hola <strong>{booking.customer_name}</strong>, tu reserva fue creada pero todavía no está confirmada.
                    </p>
                    '''}

                    <div style="margin:0 0 24px; padding:22px; background-color:#f6ffff; border:1px solid #d7f3f3; border-radius:18px;">
                        <div style="font-size:16px; font-weight:700; color:#0f2d3a; margin-bottom:12px;">
                            Datos para transferencia
                        </div>

                        {f'''
                        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                            {transfer_rows_html}
                        </table>
                        ''' if transfer_rows_html else ''}

                        {f'''
                        <div style="font-size:14px; line-height:1.7; color:#315f5f; margin-top:14px;">
                            {extra_transfer_html}
                        </div>
                        ''' if extra_transfer_html else ''}

                        <div style="font-size:13px; line-height:1.6; color:#6b8484; margin-top:14px;">
                            Tu turno ya quedó reservado en la agenda. Para mantener la reserva, realizá el pago indicado.
                            Si el pago no se acredita, el salón puede cancelar el turno.
                        </div>
                    </div>

                    <div style="background-color:#f9fbfb; border:1px solid #e3ecec; border-radius:18px; padding:22px; margin-bottom:24px;">
                        <div style="font-size:16px; font-weight:700; color:#0f2d3a; margin-bottom:16px;">
                            Resumen de tu reserva
                        </div>

                        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280; width:170px; vertical-align:top;">Servicios</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600; line-height:1.6;">
                                    {services_text}
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280; vertical-align:top;">Profesionales</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600; line-height:1.6;">
                                    {professionals_text_html}
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280;">Fecha</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                                    {fecha_formateada}
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280;">Hora de inicio</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                                    {hora_inicio}
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280;">Teléfono</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                                    {booking.customer_phone}
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280;">{payment_label}</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                                    ${amount_formatted}
                                </td>
                            </tr>
                            {expiration_html}
                        </table>
                    </div>

                    {manage_button_html}

                </div>

                <div style="border-top:1px solid #e5e7eb; background-color:#fafafa; padding:20px 32px; text-align:center;">
                    <div style="font-size:12px; color:#9ca3af;">
                        Este es un mensaje automático de reserva.
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    subject = (
        f'Turno reservado en {salon.name} - {payment_label.lower()}'
        if booking.selected_payment_method == 'transfer'
        else f'Reserva creada en {salon.name} - falta completar el pago'
    )

    send_mail(
        subject=subject,
        message=plain_message,
        from_email=from_email,
        recipient_list=[customer_email],
        fail_silently=False,
        html_message=html_message,
    )

    return True

def send_booking_cancelled_email(booking):
    customer_email = (booking.customer_email or "").strip()
    salon_email = (booking.salon.email or "").strip()
    from_email = (settings.DEFAULT_FROM_EMAIL or "").strip()

    if not from_email:
        return False

    booking_items = booking.items.select_related('service', 'employee').all()
    if not booking_items:
        return False

    services_text = ', '.join(item.service.name for item in booking_items)

    professionals_text_plain = ' | '.join(
        f'{item.service.name}: {item.employee.name}' for item in booking_items
    )

    professionals_text_html = '<br>'.join(
        f'{item.service.name}: {item.employee.name}' for item in booking_items
    )

    first_item = booking_items.order_by('start_datetime').first()
    if not first_item:
        return False

    fecha_formateada = first_item.start_datetime.strftime("%d/%m/%Y")
    hora_inicio = first_item.start_datetime.strftime("%H:%M")

    total_formatted = f"{int(booking.get_total_price()):,}".replace(",", ".")

    if customer_email:
        customer_plain_message = (
            f'{booking.salon.name}\n'
            f'Turno cancelado\n\n'
            f'Hola {booking.customer_name}, tu turno fue cancelado correctamente.\n\n'
            f'Resumen del turno cancelado:\n'
            f'- Servicios: {services_text}\n'
            f'- Profesionales: {professionals_text_plain}\n'
            f'- Fecha: {fecha_formateada}\n'
            f'- Hora de inicio: {hora_inicio}\n'
            f'- Total: ${total_formatted}\n\n'
            f'El horario volvió a quedar disponible.\n'
            f'Si querés reservar otro turno, podés hacerlo desde la página del salón.\n'
        )

        customer_html_message = f"""
        <!DOCTYPE html>
        <html lang="es">
        <head>
            <meta charset="UTF-8">
            <title>Turno cancelado</title>
        </head>
        <body style="margin:0; padding:0; background-color:#eef4f4; font-family:Arial, Helvetica, sans-serif; color:#1f2937;">
            <div style="width:100%; background-color:#eef4f4; padding:32px 16px;">
                <div style="max-width:620px; margin:0 auto; background-color:#ffffff; border-radius:22px; overflow:hidden; box-shadow:0 10px 30px rgba(15, 23, 42, 0.08); border:1px solid #dbe7e7;">

                    <div style="background:linear-gradient(135deg, #3f1f2d 0%, #7f1d1d 100%); padding:32px 32px 26px;">
                        <div style="font-size:28px; font-weight:700; color:#ffffff; line-height:1.2;">
                            {booking.salon.name}
                        </div>
                        <div style="margin-top:8px; font-size:14px; color:#fecaca; letter-spacing:0.2px;">
                            Turno cancelado
                        </div>
                    </div>

                    <div style="padding:32px;">
                        <p style="margin:0 0 18px; font-size:17px; line-height:1.6; color:#1f2937;">
                            Hola <strong>{booking.customer_name}</strong>, tu turno fue cancelado correctamente.
                        </p>

                        <div style="margin:0 0 24px; padding:16px 18px; background-color:#fef2f2; border:1px solid #fecaca; border-radius:14px;">
                            <div style="font-size:14px; font-weight:700; color:#991b1b; margin-bottom:6px;">
                                Reserva cancelada
                            </div>
                            <div style="font-size:14px; line-height:1.6; color:#7f1d1d;">
                                El horario volvió a quedar disponible para otros clientes.
                            </div>
                        </div>

                        <div style="background-color:#f9fbfb; border:1px solid #e3ecec; border-radius:18px; padding:22px; margin-bottom:24px;">
                            <div style="font-size:16px; font-weight:700; color:#0f2d3a; margin-bottom:16px;">
                                Resumen del turno cancelado
                            </div>

                            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                                <tr>
                                    <td style="padding:10px 0; font-size:14px; color:#6b7280; width:170px; vertical-align:top;">Servicios</td>
                                    <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600; line-height:1.6;">
                                        {services_text}
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding:10px 0; font-size:14px; color:#6b7280; vertical-align:top;">Profesionales</td>
                                    <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600; line-height:1.6;">
                                        {professionals_text_html}
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding:10px 0; font-size:14px; color:#6b7280;">Fecha</td>
                                    <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                                        {fecha_formateada}
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding:10px 0; font-size:14px; color:#6b7280;">Hora de inicio</td>
                                    <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                                        {hora_inicio}
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding:10px 0; font-size:14px; color:#6b7280;">Total</td>
                                    <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                                        ${total_formatted}
                                    </td>
                                </tr>
                            </table>
                        </div>

                        <p style="margin:0; font-size:14px; line-height:1.7; color:#6b7280; text-align:center;">
                            Si querés reservar otro turno, podés hacerlo desde la página del salón.
                        </p>
                    </div>

                    <div style="border-top:1px solid #e5e7eb; background-color:#fafafa; padding:20px 32px; text-align:center;">
                        <div style="font-size:12px; color:#9ca3af;">
                            Este es un mensaje automático de cancelación de reserva.
                        </div>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """

        send_mail(
            subject=f'Turno cancelado en {booking.salon.name}',
            message=customer_plain_message,
            from_email=from_email,
            recipient_list=[customer_email],
            fail_silently=False,
            html_message=customer_html_message,
        )

    if salon_email:
        salon_plain_message = (
            f'Se canceló un turno desde NYX.\n\n'
            f'Cliente: {booking.customer_name}\n'
            f'Teléfono: {booking.customer_phone}\n'
            f'Email: {booking.customer_email or "Sin email"}\n'
            f'Servicios: {services_text}\n'
            f'Profesionales: {professionals_text_plain}\n'
            f'Fecha: {fecha_formateada}\n'
            f'Hora de inicio: {hora_inicio}\n'
            f'Total: ${total_formatted}\n\n'
            f'El horario volvió a quedar disponible.'
        )

        salon_html_message = f"""
        <!DOCTYPE html>
        <html lang="es">
        <head>
            <meta charset="UTF-8">
            <title>Turno cancelado</title>
        </head>
        <body style="margin:0; padding:0; background-color:#eef4f4; font-family:Arial, Helvetica, sans-serif; color:#1f2937;">
            <div style="width:100%; background-color:#eef4f4; padding:32px 16px;">
                <div style="max-width:620px; margin:0 auto; background-color:#ffffff; border-radius:22px; overflow:hidden; box-shadow:0 10px 30px rgba(15, 23, 42, 0.08); border:1px solid #dbe7e7;">

                    <div style="background:linear-gradient(135deg, #0f2d3a 0%, #18495c 100%); padding:32px 32px 26px;">
                        <div style="font-size:28px; font-weight:700; color:#ffffff; line-height:1.2;">
                            NYX
                        </div>
                        <div style="margin-top:8px; font-size:14px; color:#c7d9df; letter-spacing:0.2px;">
                            Aviso de cancelación
                        </div>
                    </div>

                    <div style="padding:32px;">
                        <p style="margin:0 0 18px; font-size:17px; line-height:1.6; color:#1f2937;">
                            Se canceló un turno de <strong>{booking.customer_name}</strong>.
                        </p>

                        <div style="margin:0 0 24px; padding:16px 18px; background-color:#fef2f2; border:1px solid #fecaca; border-radius:14px;">
                            <div style="font-size:14px; font-weight:700; color:#991b1b; margin-bottom:6px;">
                                Turno cancelado
                            </div>
                            <div style="font-size:14px; line-height:1.6; color:#7f1d1d;">
                                El horario volvió a quedar disponible.
                            </div>
                        </div>

                        <div style="background-color:#f9fbfb; border:1px solid #e3ecec; border-radius:18px; padding:22px;">
                            <div style="font-size:16px; font-weight:700; color:#0f2d3a; margin-bottom:16px;">
                                Detalle de la reserva cancelada
                            </div>

                            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                                <tr>
                                    <td style="padding:10px 0; font-size:14px; color:#6b7280; width:170px;">Cliente</td>
                                    <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">{booking.customer_name}</td>
                                </tr>
                                <tr>
                                    <td style="padding:10px 0; font-size:14px; color:#6b7280;">Teléfono</td>
                                    <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">{booking.customer_phone}</td>
                                </tr>
                                <tr>
                                    <td style="padding:10px 0; font-size:14px; color:#6b7280;">Email</td>
                                    <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">{booking.customer_email or "Sin email"}</td>
                                </tr>
                                <tr>
                                    <td style="padding:10px 0; font-size:14px; color:#6b7280; vertical-align:top;">Servicios</td>
                                    <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600; line-height:1.6;">{services_text}</td>
                                </tr>
                                <tr>
                                    <td style="padding:10px 0; font-size:14px; color:#6b7280; vertical-align:top;">Profesionales</td>
                                    <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600; line-height:1.6;">{professionals_text_html}</td>
                                </tr>
                                <tr>
                                    <td style="padding:10px 0; font-size:14px; color:#6b7280;">Fecha</td>
                                    <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">{fecha_formateada}</td>
                                </tr>
                                <tr>
                                    <td style="padding:10px 0; font-size:14px; color:#6b7280;">Hora de inicio</td>
                                    <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">{hora_inicio}</td>
                                </tr>
                                <tr>
                                    <td style="padding:10px 0; font-size:14px; color:#6b7280;">Total</td>
                                    <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">${total_formatted}</td>
                                </tr>
                            </table>
                        </div>
                    </div>

                    <div style="border-top:1px solid #e5e7eb; background-color:#fafafa; padding:20px 32px; text-align:center;">
                        <div style="font-size:12px; color:#9ca3af;">
                            Este es un mensaje automático de NYX.
                        </div>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """

        send_mail(
            subject=f'Turno cancelado - {booking.customer_name}',
            message=salon_plain_message,
            from_email=from_email,
            recipient_list=[salon_email],
            fail_silently=False,
            html_message=salon_html_message,
        )

    return True

def send_booking_rescheduled_email(booking, request=None):
    customer_email = (booking.customer_email or "").strip()
    from_email = (settings.DEFAULT_FROM_EMAIL or "").strip()

    if not from_email:
        return False

    booking_items = booking.items.select_related('service', 'employee').order_by('order', 'start_datetime')
    if not booking_items:
        return False

    services_text = ', '.join(item.service.name for item in booking_items)

    professionals_text_plain = ' | '.join(
        f'{item.service.name}: {item.employee.name}' for item in booking_items
    )

    professionals_text_html = '<br>'.join(
        f'{item.service.name}: {item.employee.name}' for item in booking_items
    )

    first_item = booking_items.first()
    fecha_formateada = first_item.start_datetime.strftime("%d/%m/%Y")
    hora_inicio = first_item.start_datetime.strftime("%H:%M")

    manage_url = ""

    if booking.client_manage_token:
        path = reverse('manage_booking', args=[booking.client_manage_token])

        if request:
            manage_url = request.build_absolute_uri(path)
        else:
            site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
            if site_url:
                manage_url = f"{site_url}{path}"

    manage_button_html = ""

    if manage_url:
        manage_button_html = f"""
            <div style="text-align:center; margin:28px 0 8px;">
                <a href="{manage_url}"
                   style="display:inline-block; background:#0f2d3a; color:#ffffff; text-decoration:none; padding:14px 22px; border-radius:999px; font-size:14px; font-weight:700;">
                    Gestionar mi turno
                </a>
            </div>
        """

    plain_message = (
        f'{booking.salon.name}\n'
        f'Turno modificado\n\n'
        f'Hola {booking.customer_name}, tu turno fue modificado correctamente.\n\n'
        f'Nuevo resumen de tu reserva:\n'
        f'- Servicios: {services_text}\n'
        f'- Profesionales: {professionals_text_plain}\n'
        f'- Nueva fecha: {fecha_formateada}\n'
        f'- Nueva hora de inicio: {hora_inicio}\n'
        f'- Teléfono: {booking.customer_phone}\n\n'
        f'Gracias por reservar con nosotros.'
    )

    html_message = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>Turno modificado</title>
    </head>
    <body style="margin:0; padding:0; background-color:#eef4f4; font-family:Arial, Helvetica, sans-serif; color:#1f2937;">
        <div style="width:100%; background-color:#eef4f4; padding:32px 16px;">
            <div style="max-width:620px; margin:0 auto; background-color:#ffffff; border-radius:22px; overflow:hidden; box-shadow:0 10px 30px rgba(15, 23, 42, 0.08); border:1px solid #dbe7e7;">

                <div style="background:linear-gradient(135deg, #0f2d3a 0%, #18495c 100%); padding:32px 32px 26px;">
                    <div style="font-size:28px; font-weight:700; color:#ffffff; line-height:1.2;">
                        {booking.salon.name}
                    </div>
                    <div style="margin-top:8px; font-size:14px; color:#c7d9df; letter-spacing:0.2px;">
                        Turno modificado
                    </div>
                </div>

                <div style="padding:32px;">
                    <p style="margin:0 0 18px; font-size:17px; line-height:1.6; color:#1f2937;">
                        Hola <strong>{booking.customer_name}</strong>, tu turno fue modificado correctamente.
                    </p>

                    <div style="margin:0 0 24px; padding:16px 18px; background-color:#ecfeff; border:1px solid #b6ecef; border-radius:14px;">
                        <div style="font-size:14px; font-weight:700; color:#0f766e; margin-bottom:6px;">
                            Nuevo horario confirmado
                        </div>
                        <div style="font-size:14px; line-height:1.6; color:#155e63;">
                            Guardá este correo para tener a mano los datos actualizados de tu turno.
                        </div>
                    </div>

                    <div style="background-color:#f9fbfb; border:1px solid #e3ecec; border-radius:18px; padding:22px; margin-bottom:24px;">
                        <div style="font-size:16px; font-weight:700; color:#0f2d3a; margin-bottom:16px;">
                            Nuevo resumen de tu reserva
                        </div>

                        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280; width:170px; vertical-align:top;">Servicios</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600; line-height:1.6;">
                                    {services_text}
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280; vertical-align:top;">Profesionales</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600; line-height:1.6;">
                                    {professionals_text_html}
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280;">Nueva fecha</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                                    {fecha_formateada}
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:10px 0; font-size:14px; color:#6b7280;">Nueva hora</td>
                                <td style="padding:10px 0; font-size:14px; color:#111827; font-weight:600;">
                                    {hora_inicio}
                                </td>
                            </tr>
                        </table>
                    </div>

                    {manage_button_html}

                    <p style="margin:26px 0 0; font-size:14px; line-height:1.7; color:#6b7280; text-align:center;">
                        Gracias por reservar con nosotros.
                    </p>
                </div>

                <div style="border-top:1px solid #e5e7eb; background-color:#fafafa; padding:20px 32px; text-align:center;">
                    <div style="font-size:12px; color:#9ca3af;">
                        Este es un mensaje automático de modificación de reserva.
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    if customer_email:
        send_mail(
            subject=f'Turno modificado en {booking.salon.name}',
            message=plain_message,
            from_email=from_email,
            recipient_list=[customer_email],
            fail_silently=False,
            html_message=html_message,
        )

    return True


def send_salon_new_booking_email(booking):
    salon = booking.salon

    print("=== MAIL SALON DEBUG ===")
    print("Booking ID:", booking.id)
    print("Salon:", salon.name)
    print("Notification email:", salon.notification_email)

    if not salon.notification_email:
        print("NO SE ENVIA: salon.notification_email está vacío")
        return

    items = booking.items.select_related("service", "employee").order_by(
        "start_datetime"
    )

    is_integrated_verified = (
        booking.selected_payment_method == "integrated"
        and booking.payment_status == "verified"
    )

    is_transfer_pending = (
        booking.selected_payment_method == "transfer"
        and booking.requires_payment()
    )

    is_payment_pending = (
        booking.requires_payment()
        and booking.payment_status != "verified"
    )

    context = {
        "booking": booking,
        "salon": salon,
        "items": items,
        "total_price": booking.get_total_price(),
        "total_duration": booking.get_total_duration_minutes(),
        "payment_required_amount": booking.payment_required_amount,
        "remaining_amount": booking.get_total_price() - booking.payment_required_amount,
        "is_integrated_verified": is_integrated_verified,
        "is_transfer_pending": is_transfer_pending,
        "is_payment_pending": is_payment_pending,
    }

    if booking.selected_payment_method == "transfer" and booking.requires_payment():
        if booking.payment_choice == "deposit":
            subject = f"Nuevo turno reservado · seña pendiente - {salon.name}"
        elif booking.payment_choice == "full":
            subject = f"Nuevo turno reservado · pago pendiente - {salon.name}"
        else:
            subject = f"Nuevo turno reservado · pago pendiente - {salon.name}"
    elif booking.status == "confirmed":
        subject = f"Nuevo turno confirmado - {salon.name}"
    elif booking.payment_choice == "deposit":
        subject = f"Nueva reserva pendiente de seña - {salon.name}"
    elif booking.payment_choice == "full":
        subject = f"Nueva reserva pendiente de pago - {salon.name}"
    else:
        subject = f"Nueva reserva pendiente - {salon.name}"

    text_body = render_to_string(
        "reservas/emails/salon_new_booking.txt",
        context
    )

    html_body = render_to_string(
        "reservas/emails/salon_new_booking.html",
        context
    )

    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=[salon.notification_email],
    )

    email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)


def send_salon_booking_rescheduled_email(booking):
    salon = booking.salon

    if not salon.notification_email:
        return

    items = booking.items.select_related("service", "employee").order_by(
        "start_datetime"
    )

    context = {
        "booking": booking,
        "salon": salon,
        "items": items,
        "total_price": booking.get_total_price(),
        "total_duration": booking.get_total_duration_minutes(),
    }

    subject = f"Turno modificado - {booking.customer_name}"

    text_body = render_to_string(
        "reservas/emails/salon_booking_rescheduled.txt",
        context
    )

    html_body = render_to_string(
        "reservas/emails/salon_booking_rescheduled.html",
        context
    )

    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=[salon.notification_email],
    )

    email.attach_alternative(html_body, "text/html")

    try:
        sent_count = email.send(fail_silently=False)
        print(f"Mail salón turno modificado enviado. Booking ID: {booking.id}. sent_count: {sent_count}")
    except Exception as exc:
        print(f"ERROR enviando mail de turno modificado al salón. Booking ID: {booking.id}. Error: {exc}")


def send_staff_new_booking_emails(booking):
    items = booking.items.select_related(
        "service",
        "employee",
        "booking",
        "booking__salon",
    ).order_by("start_datetime")

    items_by_employee = defaultdict(list)

    for item in items:
        employee = item.employee

        if not employee.email:
            continue

        if not getattr(employee, "notify_by_email", False):
            continue

        items_by_employee[employee].append(item)

    for employee, employee_items in items_by_employee.items():
        
        is_integrated_verified = (
            booking.selected_payment_method == "integrated"
            and booking.payment_status == "verified"
        )

        is_transfer_pending = (
            booking.selected_payment_method == "transfer"
            and booking.requires_payment()
        )

        is_payment_pending = (
            booking.requires_payment()
            and booking.payment_status != "verified"
        )

        context = {
            "booking": booking,
            "salon": booking.salon,
            "employee": employee,
            "items": employee_items,
            "total_duration": sum(
                item.service.duration_minutes for item in employee_items
            ),
            "is_integrated_verified": is_integrated_verified,
            "is_transfer_pending": is_transfer_pending,
            "is_payment_pending": is_payment_pending,
        }

        if booking.selected_payment_method == "transfer" and booking.requires_payment():
            subject = f"Nuevo turno asignado - {booking.salon.name}"
        elif booking.status == "confirmed":
            subject = f"Nuevo turno asignado - {booking.salon.name}"
        elif booking.payment_choice == "deposit":
            subject = f"Turno pendiente de seña asignado - {booking.salon.name}"
        elif booking.payment_choice == "full":
            subject = f"Turno pendiente de pago asignado - {booking.salon.name}"
        else:
            subject = f"Nueva reserva asignada - {booking.salon.name}"

        text_body = render_to_string(
            "reservas/emails/staff_new_booking.txt",
            context
        )

        html_body = render_to_string(
            "reservas/emails/staff_new_booking.html",
            context
        )

        email = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            to=[employee.email],
        )

        email.attach_alternative(html_body, "text/html")

        try:
            sent_count = email.send(fail_silently=False)
            print(
                f"Mail staff enviado a {employee.email}. "
                f"Booking ID: {booking.id}. sent_count: {sent_count}"
            )
        except Exception as exc:
            print(
                f"ERROR enviando mail al staff {employee.email}. "
                f"Booking ID: {booking.id}. Error: {exc}"
            )

