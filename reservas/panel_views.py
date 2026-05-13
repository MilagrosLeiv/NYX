from datetime import timedelta, datetime
from urllib import request

from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone

from .mail_utils import send_booking_confirmed_email, send_booking_cancelled_email, send_booking_payment_pending_email
from .models import BookingItem, EmployeeTimeOff, Service, Employee,BusinessHours, Salon, Booking
from .panel_forms import (
    PanelBusinessHoursForm,
    PanelServiceForm,
    PanelEmployeeForm,
    EmployeeTimeOffForm,
    PanelSalonSettingsForm,
)
from .booking_utils import (
    mark_completed_bookings,
    mark_completed_appointments,
    expire_unpaid_bookings,
)



def get_user_membership(user):
    if not user.is_authenticated or user.is_superuser:
        return None
    return user.salon_memberships.filter(is_active=True).select_related('salon').first()


def get_user_salon(user):
    membership = get_user_membership(user)
    return membership.salon if membership else None


def is_owner_user(user):
    membership = get_user_membership(user)
    return bool(membership and membership.role == 'owner')


def is_staff_user(user):
    membership = get_user_membership(user)
    return bool(membership and membership.role == 'staff')


def get_user_employee(user):
    return getattr(user, 'employee_profile', None)


@login_required
def panel_dashboard(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    expire_unpaid_bookings()
    mark_completed_bookings()
    mark_completed_appointments()
    expire_unpaid_bookings()
    salon = get_user_salon(request.user)
    employee = get_user_employee(request.user)
    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)
    now = timezone.localtime()

    if not salon:
        raise PermissionDenied("Tu usuario no está asociado a ninguna peluquería.")

    booking_items = BookingItem.objects.select_related(
        'booking', 'booking__salon', 'employee', 'service'
    )

    time_off_blocks = EmployeeTimeOff.objects.select_related(
        'employee', 'employee__salon'
    )

    if is_owner_user(request.user):
        booking_items = booking_items.filter(booking__salon=salon)
        time_off_blocks = time_off_blocks.filter(employee__salon=salon)
    elif is_staff_user(request.user) and employee:
        booking_items = booking_items.filter(employee=employee)
        time_off_blocks = time_off_blocks.filter(employee=employee)
    else:
        booking_items = booking_items.none()
        time_off_blocks = time_off_blocks.none()

    future_items = booking_items.filter(start_datetime__gte=now)
    today_items = booking_items.filter(start_datetime__date=today)
    tomorrow_items = booking_items.filter(start_datetime__date=tomorrow)

    next_item = future_items.order_by('start_datetime').first()

    context = {
        'panel_role': 'owner' if is_owner_user(request.user) else 'staff',
        'salon': salon,
        'today_count': today_items.count(),
        'tomorrow_count': tomorrow_items.count(),
        'pending_count': future_items.filter(booking__status='pending').count(),
        'confirmed_count': future_items.filter(booking__status='confirmed').count(),
        'time_off_count': time_off_blocks.filter(end_datetime__gte=now).count(),
        'next_item': next_item,
    }
    return render(request, 'reservas/panel/dashboard.html', context)

@login_required
def panel_agenda(request):
    if request.user.is_superuser:
        raise PermissionDenied("El superuser seguí usándolo desde Django admin.")

    expire_unpaid_bookings()
    mark_completed_bookings()
    mark_completed_appointments()
    expire_unpaid_bookings()
    salon = get_user_salon(request.user)
    employee = get_user_employee(request.user)

    if not salon:
        raise PermissionDenied("Tu usuario no está asociado a ninguna peluquería.")

    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)

    selected_date_raw = request.GET.get('date')
    quick = request.GET.get('quick')

    if quick == 'today':
        selected_date = today
    elif quick == 'tomorrow':
        selected_date = tomorrow
    elif selected_date_raw:
        try:
            selected_date = datetime.strptime(selected_date_raw, "%Y-%m-%d").date()
        except ValueError:
            selected_date = today
    else:
        selected_date = today

    items = BookingItem.objects.select_related(
        'booking', 'booking__salon', 'employee', 'service'
    )

    if is_owner_user(request.user):
        items = items.filter(booking__salon=salon)
    elif is_staff_user(request.user) and employee:
        items = items.filter(employee=employee)
    else:
        items = items.none()

    items = items.filter(start_datetime__date=selected_date).order_by('start_datetime')

    context = {
        'panel_role': 'owner' if is_owner_user(request.user) else 'staff',
        'salon': salon,
        'items': items,
        'selected_date': selected_date,
        'today': today,
        'tomorrow': tomorrow,
    }
    return render(request, 'reservas/panel/agenda.html', context)

@login_required
def panel_bloqueos(request):
    if request.user.is_superuser:
        raise PermissionDenied("El superuser seguí usándolo desde Django admin.")

    salon = get_user_salon(request.user)
    employee = get_user_employee(request.user)

    if not salon:
        raise PermissionDenied("Tu usuario no está asociado a ninguna peluquería.")

    is_owner = is_owner_user(request.user)
    is_staff = is_staff_user(request.user)

    if not is_owner and not (is_staff and employee):
        raise PermissionDenied("No tenés permisos para gestionar bloqueos.")

    if request.method == "POST":
        form = EmployeeTimeOffForm(
            request.POST,
            salon=salon,
            employee=employee,
            is_owner=is_owner,
        )

        if form.is_valid():
            try:
                block = form.save(commit=False)
                block.created_by = request.user
                block.full_clean()
                block.save()

                messages.success(request, "Bloqueo cargado correctamente.")
                return redirect("panel_bloqueos")

            except ValidationError as e:
                form.add_error(
                    None,
                    e.messages[0] if getattr(e, "messages", None) else "No se pudo cargar el bloqueo."
                )
    else:
        form = EmployeeTimeOffForm(
            salon=salon,
            employee=employee,
            is_owner=is_owner,
        )

    blocks = EmployeeTimeOff.objects.select_related(
        "employee",
        "employee__salon",
        "created_by",
    )

    if is_owner:
        blocks = blocks.filter(employee__salon=salon)
    elif is_staff and employee:
        blocks = blocks.filter(employee=employee)
    else:
        blocks = blocks.none()

    blocks = blocks.order_by("start_datetime")

    context = {
        "panel_role": "owner" if is_owner else "staff",
        "salon": salon,
        "employee": employee,
        "blocks": blocks,
        "form": form,
    }

    return render(request, "reservas/panel/bloqueos.html", context)

@login_required
def panel_services(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede gestionar servicios.")

    services = Service.objects.filter(salon=salon).order_by('name')

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'services': services,
    }
    return render(request, 'reservas/panel/services.html', context)


@login_required
def panel_service_create(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede crear servicios.")

    if request.method == 'POST':
        form = PanelServiceForm(request.POST)
        if form.is_valid():
            service = form.save(commit=False)
            service.salon = salon
            service.save()
            messages.success(request, 'Servicio creado correctamente.')
            return redirect('panel_services')
    else:
        form = PanelServiceForm()

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'form': form,
        'form_title': 'Nuevo servicio',
        'submit_label': 'Crear servicio',
    }
    return render(request, 'reservas/panel/service_form.html', context)


@login_required
def panel_service_edit(request, service_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede editar servicios.")

    service = get_object_or_404(Service, pk=service_id, salon=salon)

    if request.method == 'POST':
        form = PanelServiceForm(request.POST, instance=service)
        if form.is_valid():
            form.save()
            messages.success(request, 'Servicio actualizado correctamente.')
            return redirect('panel_services')
    else:
        form = PanelServiceForm(instance=service)

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'form': form,
        'form_title': f'Editar servicio: {service.name}',
        'submit_label': 'Guardar cambios',
        'service': service,
    }
    return render(request, 'reservas/panel/service_form.html', context)


@login_required
def panel_service_toggle_active(request, service_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede modificar servicios.")

    service = get_object_or_404(Service, pk=service_id, salon=salon)
    service.is_active = not service.is_active
    service.save(update_fields=['is_active'])

    if service.is_active:
        messages.success(request, f'“{service.name}” fue activado.')
    else:
        messages.success(request, f'“{service.name}” fue desactivado.')

    return redirect('panel_services')

@login_required
def panel_employees(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede gestionar profesionales.")

    employees = Employee.objects.filter(salon=salon).prefetch_related('services').order_by('name')

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'employees': employees,
    }
    return render(request, 'reservas/panel/employees.html', context)


@login_required
def panel_employee_create(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede crear profesionales.")

    if request.method == 'POST':
        form = PanelEmployeeForm(request.POST, salon=salon)
        if form.is_valid():
            employee = form.save(commit=False)
            employee.salon = salon
            employee.save()
            form.save_m2m()
            messages.success(request, 'Profesional creado correctamente.')
            return redirect('panel_employees')
    else:
        form = PanelEmployeeForm(salon=salon)

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'form': form,
        'form_title': 'Nuevo profesional',
        'submit_label': 'Crear profesional',
    }
    return render(request, 'reservas/panel/employee_form.html', context)


@login_required
def panel_employee_edit(request, employee_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede editar profesionales.")

    employee = get_object_or_404(Employee.objects.prefetch_related('services'), pk=employee_id, salon=salon)

    if request.method == 'POST':
        form = PanelEmployeeForm(request.POST, instance=employee, salon=salon)
        if form.is_valid():
            form.save()
            messages.success(request, 'Profesional actualizado correctamente.')
            return redirect('panel_employees')
    else:
        form = PanelEmployeeForm(instance=employee, salon=salon)

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'form': form,
        'form_title': f'Editar profesional: {employee.name}',
        'submit_label': 'Guardar cambios',
        'employee': employee,
    }
    return render(request, 'reservas/panel/employee_form.html', context)


@login_required
def panel_employee_toggle_active(request, employee_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede modificar profesionales.")

    employee = get_object_or_404(Employee, pk=employee_id, salon=salon)
    employee.is_active = not employee.is_active
    employee.save(update_fields=['is_active'])

    if employee.is_active:
        messages.success(request, f'“{employee.name}” fue activado.')
    else:
        messages.success(request, f'“{employee.name}” fue desactivado.')

    return redirect('panel_employees')

@login_required
def panel_business_hours(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede gestionar horarios.")

    hours = BusinessHours.objects.filter(salon=salon).order_by('weekday')

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'hours': hours,
    }
    return render(request, 'reservas/panel/business_hours.html', context)


@login_required
def panel_business_hours_create(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede crear horarios.")

    if request.method == 'POST':
        form = PanelBusinessHoursForm(request.POST)
        if form.is_valid():
            business_hours = form.save(commit=False)
            business_hours.salon = salon
            business_hours.save()
            messages.success(request, 'Horario creado correctamente.')
            return redirect('panel_business_hours')
    else:
        form = PanelBusinessHoursForm()

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'form': form,
        'form_title': 'Nuevo horario',
        'submit_label': 'Crear horario',
    }
    return render(request, 'reservas/panel/business_hours_form.html', context)


@login_required
def panel_business_hours_edit(request, business_hours_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede editar horarios.")

    business_hours = get_object_or_404(BusinessHours, pk=business_hours_id, salon=salon)

    if request.method == 'POST':
        form = PanelBusinessHoursForm(request.POST, instance=business_hours)
        if form.is_valid():
            form.save()
            messages.success(request, 'Horario actualizado correctamente.')
            return redirect('panel_business_hours')
    else:
        form = PanelBusinessHoursForm(instance=business_hours)

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'form': form,
        'form_title': 'Editar horario',
        'submit_label': 'Guardar cambios',
        'business_hours': business_hours,
    }
    return render(request, 'reservas/panel/business_hours_form.html', context)


@login_required
def panel_settings(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede editar la configuración.")

    payment_settings, _ = SalonPaymentSettings.objects.get_or_create(salon=salon)

    if request.method == 'POST':
        form = PanelSalonSettingsForm(request.POST, instance=salon)
        if form.is_valid():
            form.save()
            messages.success(request, 'Configuración actualizada correctamente.')
            return redirect('panel_settings')
    else:
        form = PanelSalonSettingsForm(instance=salon)

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'form': form,
        'payment_settings': payment_settings,
    }
    return render(request, 'reservas/panel/settings.html', context)

@login_required
def panel_bookings(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede ver reservas.")

    expire_unpaid_bookings()
    mark_completed_bookings()
    mark_completed_appointments()
    expire_unpaid_bookings()

    selected_status = request.GET.get('status', '')
    selected_date_raw = request.GET.get('date', '')
    selected_employee = request.GET.get('employee', '')
    selected_service = request.GET.get('service', '')

    bookings = Booking.objects.filter(salon=salon).prefetch_related(
        'items__service',
        'items__employee',
    ).order_by('-created_at')

    if selected_status:
        bookings = bookings.filter(status=selected_status)

    if selected_date_raw:
        try:
            selected_date = datetime.strptime(selected_date_raw, "%Y-%m-%d").date()
            bookings = bookings.filter(items__start_datetime__date=selected_date)
        except ValueError:
            pass

    if selected_employee:
        bookings = bookings.filter(items__employee_id=selected_employee)

    if selected_service:
        bookings = bookings.filter(items__service_id=selected_service)

    bookings = bookings.distinct()

    employees = Employee.objects.filter(salon=salon).order_by('name')
    services = Service.objects.filter(salon=salon).order_by('name')

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'bookings': bookings,
        'selected_status': selected_status,
        'selected_date': selected_date_raw,
        'selected_employee': selected_employee,
        'selected_service': selected_service,
        'employees': employees,
        'services': services,
    }
    return render(request, 'reservas/panel/bookings.html', context)


@login_required
def panel_booking_detail(request, booking_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)
    mark_completed_bookings()
    mark_completed_appointments()
    expire_unpaid_bookings()
    
    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede ver reservas.")

    booking = get_object_or_404(
        Booking.objects.select_related('salon').prefetch_related('items__service', 'items__employee'),
        pk=booking_id,
        salon=salon,
    )

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'booking': booking,
    }
    return render(request, 'reservas/panel/booking_detail.html', context)


@login_required
def panel_booking_cancel(request, booking_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede cancelar reservas.")

    booking = get_object_or_404(Booking, pk=booking_id, salon=salon)

    if booking.status != 'cancelled':
        booking.status = 'cancelled'
        booking.save(update_fields=['status'])
        messages.success(request, f'Reserva #{booking.id} cancelada correctamente.')
    else:
        messages.info(request, f'La reserva #{booking.id} ya estaba cancelada.')

    return redirect('panel_bookings')



def panel_login(request):
    if request.user.is_authenticated:
        if request.user.is_superuser:
            return redirect('/admin/')
        return redirect('panel_dashboard')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        user = authenticate(request, username=username, password=password)

        if user is None:
            messages.error(request, 'Usuario o contraseña incorrectos.')
        elif not user.is_active:
            messages.error(request, 'Tu cuenta está inactiva.')
        elif user.is_superuser:
            login(request, user)
            return redirect('/admin/')
        else:
            membership = get_user_membership(user)
            if not membership:
                messages.error(request, 'Tu usuario no está vinculado a ninguna peluquería.')
            else:
                login(request, user)
                return redirect('panel_dashboard')

    return render(request, 'reservas/panel/login.html')


@login_required
def panel_logout(request):
    logout(request)
    return redirect('panel_login')

@login_required
def panel_booking_mark_payment_verified(request, booking_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede modificar pagos.")

    booking = get_object_or_404(
        Booking.objects.select_related('salon').prefetch_related(
            'items__service',
            'items__employee',
        ),
        pk=booking_id,
        salon=salon,
    )

    if request.method != 'POST':
        return redirect('panel_booking_detail', booking_id=booking.id)

    if booking.payment_choice == 'none':
        messages.info(request, f'La reserva #{booking.id} no requiere pago.')
        return redirect('panel_booking_detail', booking_id=booking.id)

    if booking.status == 'cancelled':
        messages.error(request, f'No podés verificar el pago de una reserva cancelada.')
        return redirect('panel_booking_detail', booking_id=booking.id)

    if booking.status == 'expired':
        messages.error(request, f'No podés verificar el pago de una reserva expirada.')
        return redirect('panel_booking_detail', booking_id=booking.id)

    was_already_verified = booking.payment_status == 'verified'
    was_already_confirmed = booking.status == 'confirmed'

    booking.payment_status = 'verified'
    booking.payment_verified_at = booking.payment_verified_at or timezone.now()
    booking.status = 'confirmed'

    booking.save(update_fields=[
        'payment_status',
        'payment_verified_at',
        'status',
    ])

    if not was_already_verified or not was_already_confirmed:
        send_booking_confirmed_email(booking, request=request)

    
    if booking.payment_choice == 'deposit':
        message = f'Seña de la reserva #{booking.id} marcada como recibida.'
    else:
        message = f'Pago de la reserva #{booking.id} marcado como recibido.'

    messages.success(request, message)
    

    return redirect('panel_booking_detail', booking_id=booking.id)