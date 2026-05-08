import json
import mercadopago


from datetime import datetime, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.conf import settings
from django.utils import timezone
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.core.mail import send_mail
from django.db.models import Count,Q
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt


from .payment_utils import create_pending_payment_session
from .forms import AppointmentForm, PublicAppointmentForm, AppointmentConfirmForm
from .models import Appointment, Employee, Salon, Service, Booking
from .utils import get_available_slots
from .booking_utils import (
    expire_unpaid_bookings,
    build_consecutive_booking_items,
    get_consecutive_slots_for_service_assignments,
    get_auto_consecutive_slots,
    find_auto_assignment_for_start,
)
from .mail_utils import (
    send_booking_confirmed_email,
    send_booking_payment_pending_email,
    send_booking_cancelled_email,
    send_booking_rescheduled_email,
)

def get_common_employees(service_ids, salon=None):
    if not service_ids:
        return Employee.objects.none()

    employees = Employee.objects.filter(is_active=True)

    if salon is not None:
        employees = employees.filter(salon=salon)

    for service_id in service_ids:
        employees = employees.filter(services__id=service_id)

    return employees.distinct().order_by('name')


def filter_past_slots_for_today(slots, selected_date):
    today = timezone.localdate()

    if selected_date != today:
        return slots

    now = timezone.localtime().replace(second=0, microsecond=0)

    minutes = now.minute
    remainder = minutes % 15

    if remainder != 0:
        now += timedelta(minutes=(15 - remainder))

    current_time = now.time()

    return [
        slot for slot in slots
        if datetime.strptime(slot, "%H:%M").time() >= current_time
    ]

def service_list(request):
    salon_id = request.GET.get('salon')
    expire_unpaid_bookings()
    services = Service.objects.filter(is_active=True).select_related('salon')

    salon = None
    if salon_id:
        services = services.filter(salon_id=salon_id)
        salon = Salon.objects.filter(id=salon_id, is_active=True).first()
    else:
        first_service = services.first()
        salon = first_service.salon if first_service else None

    services = services.order_by('name')

    context = {
        'services': services,
        'selected_salon_id': salon_id,
        'salon': salon,
        'deposit_enabled': salon.deposit_enabled if salon else False,
        'deposit_percentage': salon.deposit_percentage if salon else 0,
        'allow_full_payment': salon.allow_full_payment if salon else False,
        'full_payment_required': salon.full_payment_required if salon else False,
        'payment_method': salon.payment_method if salon else 'transfer',
        'payment_instructions': salon.payment_instructions if salon else '',
    }

    return render(request, 'reservas/service_list.html', context)


def create_appointment(request):
    available_slots = []

    if request.method == 'POST':
        employee_id = request.POST.get('employee')
        service_ids = request.POST.getlist('services')
        selected_date_raw = request.POST.get('date')

        if service_ids and not employee_id:
            selected_services = list(
                Service.objects.filter(id__in=selected_service_ids, is_active=True).select_related('salon')
            )

            salon = selected_services[0].salon if selected_services else None
            common_employees = get_common_employees(selected_service_ids, salon=salon)

            if common_employees.count() == 1:
                employee_id = str(common_employees.first().id)

        if employee_id and service_ids and selected_date_raw:
            try:
                employee = Employee.objects.get(pk=employee_id, is_active=True)
                services = list(Service.objects.filter(id__in=service_ids, is_active=True))
                selected_date = datetime.strptime(selected_date_raw, "%Y-%m-%d").date()
                available_slots = get_available_slots(employee, services, selected_date)
                available_slots = filter_past_slots_for_today(available_slots, selected_date)
            except (Employee.DoesNotExist, ValueError):
                available_slots = []

        form = PublicAppointmentForm(request.POST, available_slots=available_slots)

        if form.is_valid():
            appointment = form.save()
            salon_name = appointment.salon.name

            if appointment.customer_email:
                services_text = ', '.join(service.name for service in appointment.services.all())

                plain_message = (
                    f'{salon_name}\n'
                    f'Confirmación de turno\n\n'
                    f'Hola {appointment.customer_name}, tu turno fue reservado con éxito.\n\n'
                    f'Resumen de tu reserva:\n'
                    f'- Servicios: {services_text}\n'
                    f'- Profesional: {appointment.employee.name}\n'
                    f'- Fecha y hora: {appointment.appointment_datetime.strftime("%d/%m/%Y %H:%M")}\n'
                    f'- Teléfono: {appointment.customer_phone}\n\n'
                    f'Gracias por reservar con nosotros.\n'
                    f'Si necesitás modificar tu turno, comunicate con anticipación.'
)

                html_message = f"""
                <!DOCTYPE html>
                <html lang="es">
                <head>
                    <meta charset="UTF-8">
                    <title>Confirmación de turno</title>
                </head>
                <body style="margin:0; padding:0; background-color:#f4f4f7; font-family:Arial, Helvetica, sans-serif; color:#1f2937;">
                    <div style="width:100%; background-color:#f4f4f7; padding:32px 16px;">
                        <div style="max-width:600px; margin:0 auto; background-color:#ffffff; border-radius:16px; overflow:hidden; box-shadow:0 4px 18px rgba(0,0,0,0.08);">
                            <div style="font-size:24px; font-weight:700; color:#ffffff; letter-spacing:0.3px;">
                                {salon_name}
                            </div>
                            <div style="margin-top:8px; font-size:14px; color:#d1d5db;">
                                Confirmación de turno
                            </div>

                            <div style="padding:32px;">
                                <p style="margin:0 0 20px; font-size:16px; line-height:1.6;">
                                    Hola <strong>{appointment.customer_name}</strong>, tu turno fue reservado con éxito.
                                </p>

                                <div style="background-color:#f9fafb; border:1px solid #e5e7eb; border-radius:12px; padding:20px; margin-bottom:24px;">
                                    <div style="font-size:15px; font-weight:700; color:#111827; margin-bottom:14px;">
                                        Resumen de tu reserva
                                    </div>

                                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                                        <tr>
                                            <td style="padding:8px 0; font-size:14px; color:#6b7280; width:160px;">Servicios</td>
                                            <td style="padding:8px 0; font-size:14px; color:#111827; font-weight:600;">{services_text}</td>
                                        </tr>
                                        <tr>
                                            <td style="padding:8px 0; font-size:14px; color:#6b7280;">Profesional</td>
                                            <td style="padding:8px 0; font-size:14px; color:#111827; font-weight:600;">{appointment.employee.name}</td>
                                        </tr>
                                        <tr>
                                            <td style="padding:8px 0; font-size:14px; color:#6b7280;">Fecha y hora</td>
                                            <td style="padding:8px 0; font-size:14px; color:#111827; font-weight:600;">{appointment.appointment_datetime.strftime("%d/%m/%Y %H:%M")}</td>
                                        </tr>
                                        <tr>
                                            <td style="padding:8px 0; font-size:14px; color:#6b7280;">Teléfono</td>
                                            <td style="padding:8px 0; font-size:14px; color:#111827; font-weight:600;">{appointment.customer_phone}</td>
                                        </tr>
                                    </table>
                                </div>

                                <p style="margin:0 0 16px; font-size:14px; line-height:1.7; color:#374151;">
                                    Gracias por reservar con nosotros. Te esperamos en el horario indicado.
                                </p>

                                <p style="margin:0; font-size:14px; line-height:1.7; color:#6b7280;">
                                    Si necesitás modificar tu turno, comunicate con nosotros con anticipación.
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
                    subject=f'Confirmación de turno en {salon_name}',
                    message=plain_message,
                    from_email=None,
                    recipient_list=[appointment.customer_email],
                    fail_silently=True,
                    html_message=html_message,
                )

            return redirect('booking_success', appointment_id=appointment.id)
    else:
        selected_service_ids = request.GET.getlist('services')

        if selected_service_ids and len(selected_service_ids) > 1:
            common_employees = get_common_employees(selected_service_ids)

            if not common_employees.exists():
                query_string = urlencode([('services', service_id) for service_id in selected_service_ids])
                split_url = f"{reverse('create_split_appointments')}?{query_string}"
                return redirect(split_url)

        initial_data = {}
        if selected_service_ids:
            initial_data['services'] = Service.objects.filter(
                id__in=selected_service_ids,
                is_active=True
            ).select_related('salon')

        form = PublicAppointmentForm(initial=initial_data)

    return render(request, 'reservas/create_appointment.html', {'form': form})



def create_split_appointments(request):
    selected_service_ids = request.GET.getlist('services') if request.method == 'GET' else request.POST.getlist('services')

    services_queryset = Service.objects.filter(id__in=selected_service_ids, is_active=True)
    services_map = {str(service.id): service for service in services_queryset}
    selected_services = [services_map[service_id] for service_id in selected_service_ids if service_id in services_map]

    if not selected_services:
        return redirect('service_list')

    errors = {}
    values = {}

    if request.method == 'POST':
        customer_name = request.POST.get('customer_name', '').strip()
        customer_phone = request.POST.get('customer_phone', '').strip()

        values['customer_name'] = customer_name
        values['customer_phone'] = customer_phone

        appointments_to_create = []

        if not customer_name:
            errors['customer_name'] = 'Ingresá el nombre.'
        if not customer_phone:
            errors['customer_phone'] = 'Ingresá el teléfono.'

        for service in selected_services:
            employee_id = request.POST.get(f'employee_{service.id}', '').strip()
            selected_date = request.POST.get(f'date_{service.id}', '').strip()
            start_time = request.POST.get(f'start_time_{service.id}', '').strip()
            notes = request.POST.get(f'notes_{service.id}', '').strip()

            values[f'employee_{service.id}'] = employee_id
            values[f'date_{service.id}'] = selected_date
            values[f'start_time_{service.id}'] = start_time
            values[f'notes_{service.id}'] = notes

            if not employee_id:
                errors[f'employee_{service.id}'] = 'Seleccioná un profesional.'
                continue

            if not selected_date:
                errors[f'date_{service.id}'] = 'Seleccioná una fecha.'
                continue

            if not start_time:
                errors[f'start_time_{service.id}'] = 'Seleccioná un horario.'
                continue

            try:
                employee = Employee.objects.get(pk=employee_id, is_active=True)
            except Employee.DoesNotExist:
                errors[f'employee_{service.id}'] = 'El profesional seleccionado no es válido.'
                continue

            if not employee.services.filter(pk=service.pk).exists():
                errors[f'employee_{service.id}'] = f'{employee.name} no realiza {service.name}.'
                continue

            try:
                appointment_datetime = timezone.make_aware(
                datetime.strptime(
                    f'{selected_date} {start_time}',
                    '%Y-%m-%d %H:%M'
                ),
    timezone.get_current_timezone()
)
            except ValueError:
                errors[f'start_time_{service.id}'] = 'La fecha u hora no es válida.'
                continue

            appointment = Appointment(
                customer_name=customer_name,
                customer_phone=customer_phone,
                employee=employee,
                salon=service.salon,
                appointment_datetime=appointment_datetime,
                status='pending',
                notes=notes,
                service=service,
            )
            appointment._selected_services = [service]

            try:
                appointment.clean()
            except Exception as e:
                errors[f'general_{service.id}'] = e.messages[0] if hasattr(e, 'messages') else str(e)
                continue

            appointments_to_create.append((appointment, service))

        if not errors:
            appointments_by_service_id = {
                service.id: appointment
                for appointment, service in appointments_to_create
            }

            previous_end = None
            previous_service = None

            for service in selected_services:
                appointment = appointments_by_service_id.get(service.id)
                if not appointment:
                    continue

                current_start = appointment.appointment_datetime
                current_end = current_start + timedelta(minutes=service.duration_minutes)

                if previous_end is not None:
                    if current_start.date() == previous_end.date() and current_start < previous_end:
                        errors[f'start_time_{service.id}'] = (
                            f"Este turno se superpone con el servicio anterior ({previous_service.name}). "
                            f"Debe comenzar a las {previous_end.strftime('%H:%M')} o después."
                        )

                previous_end = current_end
                previous_service = service

        if not errors:
            with transaction.atomic():
                for appointment, service in appointments_to_create:
                    appointment.save()
                    appointment.services.set([service])

            return redirect('service_list')

    employees_by_service = {
        service.id: service.employees.filter(is_active=True).order_by('name')
        for service in selected_services
    }

    context = {
        'selected_services': selected_services,
        'employees_by_service': employees_by_service,
        'errors': errors,
        'values': values,
    }
    return render(request, 'reservas/create_split_appointments.html', context)



def employees_by_services(request):
    service_ids = request.GET.getlist('service_ids')

    employees = Employee.objects.filter(is_active=True)

    if service_ids:
        for service_id in service_ids:
            employees = employees.filter(services__id=service_id)

        employees = employees.distinct().order_by('name')
    else:
        employees = Employee.objects.none()

    data = {
        'employees': [
            {'id': employee.id, 'name': employee.name}
            for employee in employees
        ]
    }
    return JsonResponse(data)


def available_slots_api(request):
    employee_id = request.GET.get('employee_id')
    service_ids = request.GET.getlist('service_ids')
    selected_date_raw = request.GET.get('date')
    not_before = request.GET.get('not_before')

    if not employee_id or not service_ids or not selected_date_raw:
        return JsonResponse({'slots': []})

    try:
        employee = Employee.objects.select_related('salon').get(
            pk=employee_id,
            is_active=True
        )
        services = list(
            Service.objects.filter(
                id__in=service_ids,
                is_active=True,
                salon=employee.salon
            )
        )
        selected_date = datetime.strptime(selected_date_raw, "%Y-%m-%d").date()
    except (Employee.DoesNotExist, ValueError):
        return JsonResponse({'slots': []})

    if len(services) != len(service_ids):
        return JsonResponse({'slots': []})

    employee_service_ids = set(
        employee.services.filter(id__in=service_ids).values_list('id', flat=True)
    )
    requested_service_ids = {int(service_id) for service_id in service_ids}

    if employee_service_ids != requested_service_ids:
        return JsonResponse({'slots': []})

    slots = get_available_slots(employee, services, selected_date)
    slots = filter_past_slots_for_today(slots, selected_date)

    if not_before:
        try:
            min_time = datetime.strptime(not_before, "%H:%M").time()
            slots = [
                slot for slot in slots
                if datetime.strptime(slot, "%H:%M").time() >= min_time
            ]
        except ValueError:
            pass

    return JsonResponse({'slots': slots})

def booking_success(request, appointment_id):
    appointment = get_object_or_404(
        Appointment.objects.prefetch_related('services').select_related('employee', 'salon'),
        pk=appointment_id
    )
    return render(request, 'reservas/booking_success.html', {'appointment': appointment})

def services_by_salon(request):
    salon_id = request.GET.get('salon_id')
    services = []

    if salon_id:
        services = list(
            Service.objects.filter(
                salon_id=salon_id,
                is_active=True
            )
            .order_by('name')
            .values('id', 'name')
        )

    return JsonResponse({'services': services})

def employees_by_salon(request):
    salon_id = request.GET.get('salon_id')
    employees = []

    if salon_id:
        employees = list(
            Employee.objects.filter(
                salon_id=salon_id,
                is_active=True
            )
            .order_by('name')
            .values('id', 'name')
        )

    return JsonResponse({'employees': employees})

def employees_by_salon_and_services(request):
    salon_id = request.GET.get('salon_id')
    service_ids = request.GET.getlist('service_ids')

    employees = Employee.objects.filter(is_active=True)

    if salon_id:
        employees = employees.filter(salon_id=salon_id)
    else:
        employees = Employee.objects.none()

    if service_ids:
        for service_id in service_ids:
            employees = employees.filter(services__id=service_id)

    employees = employees.distinct().order_by('name')

    data = {
        'employees': [
            {'id': employee.id, 'name': employee.name}
            for employee in employees
        ]
    }
    return JsonResponse(data)


def select_professional(request):
    selected_service_ids = request.GET.getlist('services')
    expire_unpaid_bookings()
    services = list(
        Service.objects.filter(
            id__in=selected_service_ids,
            is_active=True
        ).select_related('salon')
    )

    if not services:
        return redirect('service_list')

    # Asumimos que todos los servicios elegidos son del mismo salón
    salon = services[0].salon

    # Profesionales que hacen al menos uno de los servicios elegidos
    employees = (
        Employee.objects.filter(
            salon=salon,
            is_active=True,
            services__in=services
        )
        .distinct()
        .order_by('name')
    )

    # Profesionales que hacen TODOS los servicios elegidos
    employees_for_all = (
        Employee.objects.filter(
            salon=salon,
            is_active=True,
            services__in=services
        )
        .annotate(
            matched_services=Count(
                'services',
                filter=Q(services__in=services),
                distinct=True
            )
        )
        .filter(matched_services=len(services))
        .distinct()
        .order_by('name')
    )

    employees_by_service = {
        service.id: service.employees.filter(
            salon=salon,
            is_active=True
        ).order_by('name')
        for service in services
    }

    total_price = sum(service.price for service in services)
    total_duration = sum(service.duration_minutes for service in services)

    context = {
        'salon': salon,
        'selected_services': services,
        'employees': employees,
        'employees_for_all': employees_for_all,
        'employees_by_service': employees_by_service,
        'total_price': total_price,
        'total_duration': total_duration,
        'has_single_employee_option': employees_for_all.exists(),
        'is_multi_service': len(services) > 1,
        'selected_service_ids': selected_service_ids,
        'first_employee_for_all': employees_for_all.first(),
    }

    return render(request, 'reservas/select_professional.html', context)

def select_time(request):
    selected_service_ids = request.GET.getlist('services')
    employee_id = request.GET.get('employee')
    mode = request.GET.get('mode', 'single_employee')
    selected_date_raw = request.GET.get('date')
    service_employee_map = {}
    expire_unpaid_bookings()

    services = list(
        Service.objects.filter(
            id__in=selected_service_ids,
            is_active=True
        ).select_related('salon')
    )

    if not services:
        return redirect('service_list')

    salon = services[0].salon

    if mode == 'per_service_consecutive':
        for service in services:
            employee_value = request.GET.get(f'employee_{service.id}')
            if employee_value:
                service_employee_map[str(service.id)] = employee_value

    employee = None
    selected_employees = {}
    service_employee_pairs = []
    available_slots = []
    selected_date = None

    if mode == 'per_service_consecutive':
        try:
            for service in services:
                employee_value = service_employee_map.get(str(service.id))
                if not employee_value:
                    selected_employees = {}
                    break

                selected_employee = Employee.objects.get(
                    pk=employee_value,
                    is_active=True,
                    salon=salon
                )

                if not selected_employee.services.filter(pk=service.id).exists():
                    selected_employees = {}
                    break

                selected_employees[service.id] = selected_employee
                service_employee_pairs.append((service, selected_employee))

        except (Employee.DoesNotExist, ValueError):
            selected_employees = {}
            service_employee_pairs = []

    elif employee_id:
        try:
            employee = Employee.objects.get(
                pk=employee_id,
                is_active=True,
                salon=salon
            )

            requested_service_ids = {int(service_id) for service_id in selected_service_ids}
            employee_service_ids = set(
                employee.services.filter(id__in=selected_service_ids).values_list('id', flat=True)
            )

            if employee_service_ids != requested_service_ids:
                employee = None

        except (Employee.DoesNotExist, ValueError):
            employee = None

    elif mode == 'auto':
        employee= None

    if selected_date_raw:
        try:
            selected_date = datetime.strptime(selected_date_raw, "%Y-%m-%d").date()

            if mode == 'per_service_consecutive' and service_employee_pairs:
                available_slots = get_consecutive_slots_for_service_assignments(
                    salon=salon,
                    service_employee_pairs=service_employee_pairs,
                    selected_date=selected_date,
                )
                available_slots = filter_past_slots_for_today(available_slots, selected_date)

            elif mode == 'auto':
                available_slots = get_auto_consecutive_slots(
                    salon=salon,
                    services=services,
                    selected_date=selected_date,
                )
                available_slots = filter_past_slots_for_today(available_slots, selected_date)

            elif employee:
                available_slots = get_available_slots(employee, services, selected_date)
                available_slots = filter_past_slots_for_today(available_slots, selected_date)

        except ValueError:
            selected_date = None
            available_slots = []

    total_price = sum(service.price for service in services)
    total_duration = sum(service.duration_minutes for service in services)

    has_valid_per_service_assignment = mode == 'per_service_consecutive' and len(service_employee_pairs) == len(services)

    context = {
        'selected_services': services,
        'selected_service_ids': selected_service_ids,
        'employee': employee,
        'selected_employees': selected_employees,
        'mode': mode,
        'selected_date': selected_date_raw or '',
        'available_slots': available_slots,
        'salon': salon,
        'total_price': total_price,
        'total_duration': total_duration,
        'service_employee_map': service_employee_map,
        'has_valid_per_service_assignment': has_valid_per_service_assignment,
    }

    return render(request, 'reservas/select_time.html', context)



def confirm_appointment(request):
    selected_service_ids = request.GET.getlist('services') if request.method == 'GET' else request.POST.getlist('services')
    employee_id = request.GET.get('employee') if request.method == 'GET' else request.POST.get('employee')
    selected_date_raw = request.GET.get('date') if request.method == 'GET' else request.POST.get('date')
    start_time = request.GET.get('start_time') if request.method == 'GET' else request.POST.get('start_time')

    services = list(
        Service.objects.filter(
            id__in=selected_service_ids,
            is_active=True
        ).select_related('salon')
    )

    if not services or not employee_id or not selected_date_raw or not start_time:
        return redirect('service_list')

    salon = services[0].salon

    try:
        employee = Employee.objects.get(
            pk=employee_id,
            is_active=True,
            salon=salon
        )

        requested_service_ids = {int(service_id) for service_id in selected_service_ids}
        employee_service_ids = set(
            employee.services.filter(id__in=selected_service_ids).values_list('id', flat=True)
        )

        if employee_service_ids != requested_service_ids:
            return redirect('service_list')

    except (Employee.DoesNotExist, ValueError):
        return redirect('service_list')

    try:
        appointment_datetime = timezone.make_aware(
            datetime.strptime(
                f'{selected_date_raw} {start_time}',
                '%Y-%m-%d %H:%M'
            ),
            timezone.get_current_timezone()
        )
    except ValueError:
        return redirect('service_list')

    total_price = sum(service.price for service in services)
    total_duration = sum(service.duration_minutes for service in services)

    if request.method == 'POST':
        form = AppointmentConfirmForm(request.POST)

        if form.is_valid():
            appointment = Appointment(
                customer_name=form.cleaned_data['customer_name'],
                customer_email=form.cleaned_data['customer_email'],
                customer_phone=form.cleaned_data['customer_phone'],
                employee=employee,
                salon=salon,
                appointment_datetime=appointment_datetime,
                status='pending',
                notes=form.cleaned_data['notes'],
            )
            appointment._selected_services = services

            try:
                appointment.clean()
            except ValidationError as e:
                form.add_error(
                    None,
                    e.messages[0] if getattr(e, 'messages', None) else 'No se pudo reservar el turno.'
                )
            else:
                appointment.save()
                appointment.services.set(services)

                if appointment.customer_email:
                    services_text = ', '.join(service.name for service in appointment.services.all())
                    salon_name = appointment.salon.name

                    plain_message = (
                        f'{salon_name}\n'
                        f'Confirmación de turno\n\n'
                        f'Hola {appointment.customer_name}, tu turno fue reservado con éxito.\n\n'
                        f'Resumen de tu reserva:\n'
                        f'- Servicios: {services_text}\n'
                        f'- Profesional: {appointment.employee.name}\n'
                        f'- Fecha y hora: {appointment.appointment_datetime.strftime("%d/%m/%Y %H:%M")}\n'
                        f'- Teléfono: {appointment.customer_phone}\n\n'
                        f'Gracias por reservar con nosotros.\n'
                        f'Si necesitás modificar tu turno, comunicate con anticipación.'
                    )

                    html_message = f"""
                    <!DOCTYPE html>
                    <html lang="es">
                    <head>
                        <meta charset="UTF-8">
                        <title>Confirmación de turno</title>
                    </head>
                    <body style="margin:0; padding:0; background-color:#f4f4f7; font-family:Arial, Helvetica, sans-serif; color:#1f2937;">
                        <div style="width:100%; background-color:#f4f4f7; padding:32px 16px;">
                            <div style="max-width:600px; margin:0 auto; background-color:#ffffff; border-radius:16px; overflow:hidden; box-shadow:0 4px 18px rgba(0,0,0,0.08);">
                                <div style="padding:28px 32px; background-color:#111827;">
                                    <div style="font-size:24px; font-weight:700; color:#ffffff;">
                                        {salon_name}
                                    </div>
                                    <div style="margin-top:8px; font-size:14px; color:#d1d5db;">
                                        Confirmación de turno
                                    </div>
                                </div>

                                <div style="padding:32px;">
                                    <p style="margin:0 0 20px; font-size:16px; line-height:1.6;">
                                        Hola <strong>{appointment.customer_name}</strong>, tu turno fue reservado con éxito.
                                    </p>

                                    <div style="background-color:#f9fafb; border:1px solid #e5e7eb; border-radius:12px; padding:20px; margin-bottom:24px;">
                                        <div style="font-size:15px; font-weight:700; color:#111827; margin-bottom:14px;">
                                            Resumen de tu reserva
                                        </div>

                                        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                                            <tr>
                                                <td style="padding:8px 0; font-size:14px; color:#6b7280; width:160px;">Servicios</td>
                                                <td style="padding:8px 0; font-size:14px; color:#111827; font-weight:600;">{services_text}</td>
                                            </tr>
                                            <tr>
                                                <td style="padding:8px 0; font-size:14px; color:#6b7280;">Profesional</td>
                                                <td style="padding:8px 0; font-size:14px; color:#111827; font-weight:600;">{appointment.employee.name}</td>
                                            </tr>
                                            <tr>
                                                <td style="padding:8px 0; font-size:14px; color:#6b7280;">Fecha y hora</td>
                                                <td style="padding:8px 0; font-size:14px; color:#111827; font-weight:600;">{appointment.appointment_datetime.strftime("%d/%m/%Y %H:%M")}</td>
                                            </tr>
                                            <tr>
                                                <td style="padding:8px 0; font-size:14px; color:#6b7280;">Teléfono</td>
                                                <td style="padding:8px 0; font-size:14px; color:#111827; font-weight:600;">{appointment.customer_phone}</td>
                                            </tr>
                                        </table>
                                    </div>

                                    <p style="margin:0 0 16px; font-size:14px; line-height:1.7; color:#374151;">
                                        Gracias por reservar con nosotros. Te esperamos en el horario indicado.
                                    </p>

                                    <p style="margin:0; font-size:14px; line-height:1.7; color:#6b7280;">
                                        Si necesitás modificar tu turno, comunicate con nosotros con anticipación.
                                    </p>
                                </div>
                            </div>
                        </div>
                    </body>
                    </html>
                    """

                    send_mail(
                        subject=f'Confirmación de turno en {salon_name}',
                        message=plain_message,
                        from_email=None,
                        recipient_list=[appointment.customer_email],
                        fail_silently=True,
                        html_message=html_message,
                    )

                return redirect('booking_success', appointment_id=appointment.id)
    else:
        form = AppointmentConfirmForm()

    context = {
        'form': form,
        'selected_services': services,
        'employee': employee,
        'selected_date': selected_date_raw,
        'start_time': start_time,
        'salon': salon,
        'total_price': total_price,
        'total_duration': total_duration,
        'selected_service_ids': selected_service_ids,
    }
    return render(request, 'reservas/confirm_appointment.html', context)

def confirm_booking(request):
    selected_service_ids = request.GET.getlist('services') if request.method == 'GET' else request.POST.getlist('services')
    employee_id = request.GET.get('employee') if request.method == 'GET' else request.POST.get('employee')
    selected_date_raw = request.GET.get('date') if request.method == 'GET' else request.POST.get('date')
    start_time = request.GET.get('start_time') if request.method == 'GET' else request.POST.get('start_time')
    mode = request.GET.get('mode', 'consecutive') if request.method == 'GET' else request.POST.get('mode', 'consecutive')
    expire_unpaid_bookings()
    services = list(
        Service.objects.filter(
            id__in=selected_service_ids,
            is_active=True
        ).select_related('salon')
    )

    if not services or not selected_date_raw or not start_time:
        return redirect('service_list')

    salon = services[0].salon
    employee = None
    selected_employees = {}
    service_employee_pairs = []

    if mode == 'per_service_consecutive':
        try:
            for service in services:
                employee_value = (
                    request.GET.get(f'employee_{service.id}')
                    if request.method == 'GET'
                    else request.POST.get(f'employee_{service.id}')
                )

                if not employee_value:
                    return redirect('service_list')

                selected_employee = Employee.objects.get(
                    pk=employee_value,
                    is_active=True,
                    salon=salon
                )

                if not selected_employee.services.filter(pk=service.id).exists():
                    return redirect('service_list')

                selected_employees[service.id] = selected_employee
                service_employee_pairs.append((service, selected_employee))

        except (Employee.DoesNotExist, ValueError):
            return redirect('service_list')

    elif mode == 'auto':
        employee = None

    else:
        if not employee_id:
            return redirect('service_list')

        try:
            employee = Employee.objects.get(
                pk=employee_id,
                is_active=True,
                salon=salon
            )

            requested_service_ids = {int(service_id) for service_id in selected_service_ids}
            employee_service_ids = set(
                employee.services.filter(id__in=selected_service_ids).values_list('id', flat=True)
            )

            if employee_service_ids != requested_service_ids:
                return redirect('service_list')

        except (Employee.DoesNotExist, ValueError):
            return redirect('service_list')

    total_price = sum(service.price for service in services)
    total_duration = sum(service.duration_minutes for service in services)

    deposit_enabled = salon.deposit_enabled
    deposit_percentage = salon.deposit_percentage
    allow_full_payment = salon.allow_full_payment
    full_payment_required = salon.full_payment_required
    payment_method = salon.payment_method
    payment_instructions = salon.payment_instructions

    deposit_amount = round((total_price * deposit_percentage / 100), 2) if deposit_percentage > 0 else 0
    full_amount = total_price


    selected_payment_choice = (
        request.GET.get('payment_choice', '')
        if request.method == 'GET'
        else request.POST.get('payment_choice', '')
    )

    selected_payment_method = (
        request.GET.get('selected_payment_method', '')
        if request.method == 'GET'
        else request.POST.get('selected_payment_method', '')
    )
    if request.method == 'POST':
        form = AppointmentConfirmForm(request.POST)

        if form.is_valid():
            try:
                with transaction.atomic():

                    payment_choice = 'none'
                    payment_status = 'not_required'
                    payment_required_amount = 0

                    final_payment_method = payment_method
                    if payment_method == 'both':
                        final_payment_method = selected_payment_method or 'transfer'

                    if full_payment_required:
                        payment_choice = 'full'
                        payment_status = 'pending'
                        payment_required_amount = full_amount

                    elif deposit_enabled and allow_full_payment:
                        chosen_payment = selected_payment_choice or 'deposit'

                        if chosen_payment == 'full':
                            payment_choice = 'full'
                            payment_status = 'pending'
                            payment_required_amount = full_amount
                        else:
                            payment_choice = 'deposit'
                            payment_status = 'pending'
                            payment_required_amount = deposit_amount

                    elif deposit_enabled:
                        payment_choice = 'deposit'
                        payment_status = 'pending'
                        payment_required_amount = deposit_amount

                    elif allow_full_payment:
                        if selected_payment_choice == 'full':
                            payment_choice = 'full'
                            payment_status = 'pending'
                            payment_required_amount = full_amount
                        else:
                            payment_choice = 'none'
                            payment_status = 'not_required'
                            payment_required_amount = 0

                    booking = Booking.objects.create(
                        salon=salon,
                        customer_name=form.cleaned_data['customer_name'],
                        customer_email=form.cleaned_data['customer_email'],
                        customer_phone=form.cleaned_data['customer_phone'],
                        notes=form.cleaned_data['notes'],
                        booking_mode='consecutive' if mode != 'independent' else 'independent',
                        status='pending',
                        payment_choice=payment_choice,
                        payment_status=payment_status,
                        payment_required_amount=payment_required_amount,
                        selected_payment_method=final_payment_method,
                    )

                    if mode == 'per_service_consecutive':
                        items = build_consecutive_booking_items(
                            booking=booking,
                            service_employee_pairs=service_employee_pairs,
                            selected_date=selected_date_raw,
                            start_time=start_time,
                        )

                    elif mode == 'auto':
                        start_time_obj = datetime.strptime(start_time, "%H:%M").time()

                        auto_pairs = find_auto_assignment_for_start(
                            salon=salon,
                            services=services,
                            selected_date=datetime.strptime(selected_date_raw, "%Y-%m-%d").date(),
                            start_time=start_time_obj,
                        )

                        if not auto_pairs:
                            raise ValidationError(
                                "Ya no encontramos disponibilidad automática para ese horario. Probá con otro."
                            )

                        items = build_consecutive_booking_items(
                            booking=booking,
                            service_employee_pairs=auto_pairs,
                            selected_date=selected_date_raw,
                            start_time=start_time,
                        )

                    else:
                        single_pairs = [(service, employee) for service in services]
                        items = build_consecutive_booking_items(
                            booking=booking,
                            service_employee_pairs=single_pairs,
                            selected_date=selected_date_raw,
                            start_time=start_time,
                        )

                    for item in items:
                        item.save()

                    if booking.requires_payment():
                        booking.payment_expires_at = timezone.now() + timedelta(minutes=15)
                        booking.save(update_fields=['payment_expires_at'])

                    if booking.payment_choice != 'none' and booking.payment_required_amount > 0:
                        create_pending_payment_session(booking)

            except ValidationError as e:
                form.add_error(
                    None,
                    e.messages[0] if getattr(e, 'messages', None) else 'No se pudo reservar el turno.'
                )
            else:
                if not booking.requires_payment():
                    booking.status = 'confirmed'
                    booking.save(update_fields=['status'])
                    send_booking_confirmed_email(booking, request=request)
                elif booking.selected_payment_method == 'transfer':
                    send_booking_payment_pending_email(booking, request=request )

                return redirect('booking_success_booking', booking_id=booking.id)
    else:
        form = AppointmentConfirmForm()

    context = {
        'form': form,
        'selected_services': services,
        'employee': employee,
        'selected_employees': selected_employees,
        'selected_date': selected_date_raw,
        'start_time': start_time,
        'salon': salon,
        'total_price': total_price,
        'total_duration': total_duration,
        'selected_service_ids': selected_service_ids,
        'mode': mode,
        'deposit_enabled': deposit_enabled,
        'deposit_percentage': deposit_percentage,
        'allow_full_payment': allow_full_payment,
        'full_payment_required': full_payment_required,
        'payment_method': payment_method,
        'deposit_amount': deposit_amount,
        'full_amount': full_amount,
        'payment_instructions': payment_instructions,
        'selected_payment_choice': selected_payment_choice,
    }
    return render(request, 'reservas/confirm_booking.html', context)

def select_professionals_per_service(request):
    selected_service_ids = request.GET.getlist('services')

    services_queryset = Service.objects.filter(
        id__in=selected_service_ids,
        is_active=True
    ).select_related('salon')

    services_map = {str(service.id): service for service in services_queryset}
    selected_services = [services_map[service_id] for service_id in selected_service_ids if service_id in services_map]

    if not selected_services:
        return redirect('service_list')

    salon = selected_services[0].salon

    employees_by_service = {
        service.id: service.employees.filter(
            salon=salon,
            is_active=True
        ).order_by('name')
        for service in selected_services
    }

    total_price = sum(service.price for service in selected_services)
    total_duration = sum(service.duration_minutes for service in selected_services)

    context = {
        'salon': salon,
        'selected_services': selected_services,
        'employees_by_service': employees_by_service,
        'total_price': total_price,
        'total_duration': total_duration,
    }
    return render(request, 'reservas/select_professionals_per_service.html', context)

def booking_success_booking(request, booking_id):
    expire_unpaid_bookings()

    booking = get_object_or_404(
        Booking.objects.prefetch_related('items__service', 'items__employee').select_related('salon'),
        pk=booking_id
    )
    return render(request, 'reservas/booking_success_booking.html', {
        'booking': booking,
    })

def booking_payment(request, booking_id):
    booking = get_object_or_404(
        Booking.objects.prefetch_related('items__service', 'items__employee').select_related('salon'),
        pk=booking_id
    )
    if booking.payment_status == 'verified' and booking.status == 'confirmed':
        return redirect('booking_success_booking', booking_id=booking.id)

    if booking.payment_choice == 'none' or booking.payment_required_amount <= 0:
        return redirect('booking_success_booking', booking_id=booking.id)
    
    if booking.status == 'expired' or booking.is_payment_expired():
        booking.status = 'expired'
        booking.save(update_fields=['status'])
        return render(request, 'reservas/booking_payment_expired.html', {
            'booking': booking,
        })

    context = {
        'booking': booking,
    }
    return render(request, 'reservas/booking_payment.html', context)

@csrf_exempt
def payment_webhook(request):
    if request.method != 'POST':
        return HttpResponse(status=405)

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        payload = {}

    event_type = payload.get('type')
    data = payload.get('data', {})
    payment_id = data.get('id')

    if event_type != 'payment' or not payment_id:
        return HttpResponse(status=200)

    if not settings.MERCADOPAGO_ACCESS_TOKEN:
        return HttpResponse(status=500)

    sdk = mercadopago.SDK(settings.MERCADOPAGO_ACCESS_TOKEN)
    payment_response = sdk.payment().get(payment_id)
    payment = payment_response.get('response', {})

    external_reference = payment.get('external_reference')
    status = payment.get('status')

    if not external_reference:
        return HttpResponse(status=200)

    booking = Booking.objects.filter(payment_reference=external_reference).first()
    if not booking:
        return HttpResponse(status=200)

    if status == 'approved':
        booking.payment_status = 'verified'
        booking.status = 'confirmed'
        booking.payment_verified_at = timezone.now()
        booking.save(update_fields=['payment_status', 'status', 'payment_verified_at'])
        send_booking_confirmed_email(booking, request=request)

    return HttpResponse(status=200)

def manage_booking(request, token):
    booking = get_object_or_404(
        Booking.objects.select_related('salon').prefetch_related(
            'items__service',
            'items__employee'
        ),
        client_manage_token=token
    )

    context = {
        'booking': booking,
        'can_cancel': booking.can_be_cancelled_by_client(),
        'cancel_block_reason': booking.get_client_cancellation_block_reason(),
        'cancellation_deadline': booking.get_client_cancellation_deadline(),
        'can_reschedule': booking.can_be_rescheduled_by_client(),
        'reschedule_block_reason': booking.get_client_reschedule_block_reason(),
        'reschedule_deadline': booking.get_client_reschedule_deadline(),
    }

    return render(request, 'reservas/manage_booking.html', context)


def cancel_booking(request, token):
    booking = get_object_or_404(
        Booking.objects.select_related('salon').prefetch_related(
            'items__service',
            'items__employee'
        ),
        client_manage_token=token
    )

    if request.method != 'POST':
        context = {
            'booking': booking,
            'can_cancel': booking.can_be_cancelled_by_client(),
            'cancel_block_reason': booking.get_client_cancellation_block_reason(),
            'cancellation_deadline': booking.get_client_cancellation_deadline(),
        }
        return render(request, 'reservas/cancel_booking.html', context)

    with transaction.atomic():
        booking = Booking.objects.select_for_update().select_related('salon').get(
            client_manage_token=token
        )

        if not booking.can_be_cancelled_by_client():
            messages.error(
                request,
                booking.get_client_cancellation_block_reason() or "Este turno no puede cancelarse online."
            )
            return redirect('manage_booking', token=booking.client_manage_token)

        booking.status = 'cancelled'
        booking.cancelled_at = timezone.now()
        booking.cancelled_by_client = True
        booking.save(update_fields=['status', 'cancelled_at', 'cancelled_by_client'])

    send_booking_cancelled_email(booking)

    messages.success(request, "Tu turno fue cancelado correctamente.")
    return redirect('manage_booking', token=booking.client_manage_token)

def reschedule_booking(request, token):
    booking = get_object_or_404(
        Booking.objects.select_related('salon').prefetch_related(
            'items__service',
            'items__employee'
        ),
        client_manage_token=token
    )

    if not booking.can_be_rescheduled_by_client():
        messages.error(
            request,
            booking.get_client_reschedule_block_reason() or "Este turno no puede modificarse online."
        )
        return redirect('manage_booking', token=booking.client_manage_token)

    booking_items = list(
        booking.items.select_related('service', 'employee').order_by('order', 'start_datetime')
    )

    if not booking_items:
        messages.error(request, "No encontramos los servicios de esta reserva.")
        return redirect('manage_booking', token=booking.client_manage_token)

    service_employee_pairs = [
        (item.service, item.employee)
        for item in booking_items
    ]

    selected_date_raw = request.GET.get('date') if request.method == 'GET' else request.POST.get('date')
    selected_time = request.POST.get('start_time') if request.method == 'POST' else ''
    selected_date = None
    available_slots = []

    if selected_date_raw:
        try:
            selected_date = datetime.strptime(selected_date_raw, "%Y-%m-%d").date()

            available_slots = get_consecutive_slots_for_service_assignments(
                salon=booking.salon,
                service_employee_pairs=service_employee_pairs,
                selected_date=selected_date,
                exclude_booking_id=booking.id,
            )

            available_slots = filter_past_slots_for_today(available_slots, selected_date)

        except ValueError:
            selected_date = None
            available_slots = []

    if request.method == 'POST':
        if not selected_date_raw or not selected_time:
            messages.error(request, "Seleccioná una fecha y un horario para modificar el turno.")
            return redirect(f"{reverse('reschedule_booking', args=[booking.client_manage_token])}?date={selected_date_raw or ''}")

        if selected_time not in available_slots:
            messages.error(request, "Ese horario ya no está disponible. Elegí otro.")
            return redirect(f"{reverse('reschedule_booking', args=[booking.client_manage_token])}?date={selected_date_raw}")

        try:
            with transaction.atomic():
                selected_date_obj = datetime.strptime(selected_date_raw, "%Y-%m-%d").date()
                selected_time_obj = datetime.strptime(selected_time, "%H:%M").time()

                current_start = timezone.make_aware(
                    datetime.combine(selected_date_obj, selected_time_obj),
                    timezone.get_current_timezone()
                )
                for index, item in enumerate(booking_items):
                    service = item.service
                    current_end = current_start + timedelta(minutes=service.duration_minutes)

                    item.start_datetime = current_start
                    item.end_datetime = current_end
                    item.order = index
                    item.clean()
                    item.save(update_fields=[
                        'start_datetime',
                        'end_datetime',
                        'order',
                    ])

                    current_start = current_end

        except ValidationError as e:
            messages.error(
                request,
                e.messages[0] if getattr(e, 'messages', None) else "No se pudo modificar el turno."
            )
            return redirect(f"{reverse('reschedule_booking', args=[booking.client_manage_token])}?date={selected_date_raw}")

        send_booking_rescheduled_email(booking, request=request)

        messages.success(request, "Tu turno fue modificado correctamente.")
        return redirect('manage_booking', token=booking.client_manage_token)

    context = {
        'booking': booking,
        'booking_items': booking_items,
        'selected_date': selected_date_raw or '',
        'available_slots': available_slots,
        'reschedule_deadline': booking.get_client_reschedule_deadline(),
    }

    return render(request, 'reservas/reschedule_booking.html', context)