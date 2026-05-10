from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import BookingItem, Booking, BusinessHours, Appointment, Employee, EmployeeTimeOff


def make_aware_datetime(selected_date, selected_time_str):
    try:
        naive_dt = datetime.strptime(
            f"{selected_date} {selected_time_str}",
            "%Y-%m-%d %H:%M"
        )
    except ValueError:
        raise ValidationError("La fecha u hora no es válida.")

    return timezone.make_aware(naive_dt, timezone.get_current_timezone())


def build_consecutive_booking_items(*, booking, service_employee_pairs, selected_date, start_time):
    """
    service_employee_pairs = [
        (service_obj, employee_obj),
        (service_obj, employee_obj),
        ...
    ]
    """
    if not service_employee_pairs:
        raise ValidationError("Debes seleccionar al menos un servicio.")

    current_start = make_aware_datetime(selected_date, start_time)
    items = []

    for index, (service, employee) in enumerate(service_employee_pairs):
        current_end = current_start + timedelta(minutes=service.duration_minutes)

        item = BookingItem(
            booking=booking,
            service=service,
            employee=employee,
            start_datetime=current_start,
            end_datetime=current_end,
            order=index,
        )
        item.clean()
        items.append(item)

        current_start = current_end

    return items


def build_independent_booking_items(*, booking, item_data_list):
    """
    item_data_list = [
        {
            'service': service_obj,
            'employee': employee_obj,
            'date': '2026-04-10',
            'start_time': '10:00',
            'order': 0,
        },
        ...
    ]
    """
    if not item_data_list:
        raise ValidationError("Debes seleccionar al menos un servicio.")

    items = []
    used_dates = set()

    for index, item_data in enumerate(item_data_list):
        service = item_data['service']
        employee = item_data['employee']
        selected_date = item_data['date']
        start_time = item_data['start_time']
        order = item_data.get('order', index)

        start_datetime = make_aware_datetime(selected_date, start_time)
        end_datetime = start_datetime + timedelta(minutes=service.duration_minutes)

        used_dates.add(start_datetime.date())

        item = BookingItem(
            booking=booking,
            service=service,
            employee=employee,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            order=order,
        )
        item.clean()
        items.append(item)

    if len(used_dates) > 1:
        raise ValidationError(
            "Todos los servicios de una misma reserva deben ser el mismo día. Si querés otro día, hacé una nueva reserva."
        )

    return items

def get_consecutive_slots_for_service_assignments(
    *,
    salon,
    service_employee_pairs,
    selected_date,
    slot_minutes=15,
    exclude_booking_id=None
):
    """
    service_employee_pairs = [
        (service_obj, employee_obj),
        (service_obj, employee_obj),
        ...
    ]

    Devuelve horas de inicio válidas para ejecutar toda la secuencia de forma consecutiva.
    """
    if not service_employee_pairs:
        return []

    weekday = selected_date.weekday()

    business_hours = BusinessHours.objects.filter(
        salon=salon,
        weekday=weekday
    ).first()

    if not business_hours or business_hours.is_closed:
        return []

    current_tz = timezone.get_current_timezone()

    day_start = timezone.make_aware(
        datetime.combine(selected_date, business_hours.start_time),
        current_tz
    )

    day_end = timezone.make_aware(
        datetime.combine(selected_date, business_hours.end_time),
        current_tz
    )

    total_duration = sum(
        service.duration_minutes
        for service, _employee in service_employee_pairs
    )

    if total_duration <= 0:
        return []

    employees = {
        employee.id: employee
        for _service, employee in service_employee_pairs
    }

    existing_items_by_employee = {}

    for employee in employees.values():
        items = BookingItem.objects.select_related("booking").filter(
            employee=employee,
            start_datetime__date=selected_date
        ).exclude(
            booking__status__in=["cancelled", "expired"]
        )

        if exclude_booking_id:
            items = items.exclude(booking_id=exclude_booking_id)

        existing_items_by_employee[employee.id] = [
            item for item in items
            if item.booking.is_blocking_slot()
        ]

    existing_appointments_by_employee = {}

    for employee in employees.values():
        existing_appointments_by_employee[employee.id] = list(
            Appointment.objects.filter(
                employee=employee,
                appointment_datetime__date=selected_date
            ).exclude(
                status="cancelled"
            ).prefetch_related("services", "service")
        )

    existing_time_off_by_employee = {}

    for employee in employees.values():
        existing_time_off_by_employee[employee.id] = list(
            EmployeeTimeOff.objects.filter(
                employee=employee,
                start_datetime__date=selected_date
            )
        )

    slots = []
    current_start = day_start

    while current_start < day_end:
        sequence_start = current_start
        sequence_end = sequence_start + timedelta(minutes=total_duration)

        if sequence_end > day_end:
            break

        is_valid = True
        block_start = sequence_start

        for service, employee in service_employee_pairs:
            block_end = block_start + timedelta(minutes=service.duration_minutes)

            # Validar superposición con BookingItem nuevo
            for item in existing_items_by_employee.get(employee.id, []):
                overlaps = (
                    block_start < item.end_datetime
                    and block_end > item.start_datetime
                )

                if overlaps:
                    is_valid = False
                    break

            if not is_valid:
                break

            # Validar superposición con Appointment viejo
            for appointment in existing_appointments_by_employee.get(employee.id, []):
                appointment_start = appointment.appointment_datetime
                appointment_end = appointment_start + timedelta(
                    minutes=appointment.get_total_duration_minutes()
                )

                overlaps = (
                    block_start < appointment_end
                    and block_end > appointment_start
                )

                if overlaps:
                    is_valid = False
                    break

            if not is_valid:
                break

            # Validar superposición con bloqueos del profesional
            for block in existing_time_off_by_employee.get(employee.id, []):
                overlaps = (
                    block_start < block.end_datetime
                    and block_end > block.start_datetime
                )

                if overlaps:
                    is_valid = False
                    break

            if not is_valid:
                break

            block_start = block_end

        if is_valid:
            slots.append(sequence_start.strftime("%H:%M"))

        current_start += timedelta(minutes=slot_minutes)

    return slots

def find_auto_assignment_for_start(*, salon, services, selected_date, start_time, slot_minutes=15):
    """
    Devuelve una lista de pares (service, employee) si encuentra una combinación
    válida y consecutiva para ese horario de inicio. Si no, devuelve [].
    """
    if not services:
        return []

    weekday = selected_date.weekday()
    business_hours = BusinessHours.objects.filter(
        salon=salon,
        weekday=weekday
    ).first()

    if not business_hours or business_hours.is_closed:
        return []

    day_start = timezone.make_aware(
        datetime.combine(selected_date, business_hours.start_time),
        timezone.get_current_timezone()
    )
    day_end = timezone.make_aware(
        datetime.combine(selected_date, business_hours.end_time),
        timezone.get_current_timezone()
    )

    sequence_start = timezone.make_aware(
        datetime.combine(selected_date, start_time),
        timezone.get_current_timezone()
    )

    total_duration = sum(service.duration_minutes for service in services)
    if total_duration <= 0:
        return []

    sequence_end = sequence_start + timedelta(minutes=total_duration)
    if sequence_start < day_start or sequence_end > day_end:
        return []

    employees_by_service = {}
    all_employee_ids = set()

    for service in services:
        employees = list(
            Employee.objects.filter(
                salon=salon,
                is_active=True,
                services=service
            ).distinct().order_by('name')
        )
        if not employees:
            return []

        employees_by_service[service.id] = employees
        all_employee_ids.update(employee.id for employee in employees)

    existing_items_by_employee = {}
    for employee_id in all_employee_ids:
        existing_items_by_employee[employee_id] = list(
            BookingItem.objects.select_related('booking').filter(
                employee_id=employee_id,
                start_datetime__date=selected_date
            ).exclude(booking__status__in=['cancelled', 'expired'])
        )

    existing_appointments_by_employee = {}
    for employee_id in all_employee_ids:
        existing_appointments_by_employee[employee_id] = list(
            Appointment.objects.filter(
                employee_id=employee_id,
                appointment_datetime__date=selected_date
            ).exclude(status='cancelled').prefetch_related('services', 'service')
        )

    existing_time_off_by_employee = {}
    for employee_id in all_employee_ids:
        existing_time_off_by_employee[employee_id] = list(
            EmployeeTimeOff.objects.filter(
                employee_id=employee_id,
                start_datetime__date=selected_date
            )
        )

    def employee_is_available(employee, block_start, block_end):
        for item in existing_items_by_employee[employee.id]:
            if not item.booking.is_blocking_slot():
                continue

            overlaps = block_start < item.end_datetime and block_end > item.start_datetime
            if overlaps:
                return False

        for appointment in existing_appointments_by_employee[employee.id]:
            appointment_start = appointment.appointment_datetime
            appointment_end = appointment_start + timedelta(minutes=appointment.get_total_duration_minutes())

            overlaps = block_start < appointment_end and block_end > appointment_start
            if overlaps:
                return False

        for block in existing_time_off_by_employee[employee.id]:
            overlaps = block_start < block.end_datetime and block_end > block.start_datetime
            if overlaps:
                return False

        return True

    def backtrack(service_index, block_start, current_pairs):
        if service_index >= len(services):
            return current_pairs

        service = services[service_index]
        block_end = block_start + timedelta(minutes=service.duration_minutes)

        for employee in employees_by_service[service.id]:
            if employee_is_available(employee, block_start, block_end):
                result = backtrack(
                    service_index + 1,
                    block_end,
                    current_pairs + [(service, employee)]
                )
                if result:
                    return result

        return []

    return backtrack(0, sequence_start, [])


def get_auto_consecutive_slots(*, salon, services, selected_date, slot_minutes=15):
    """
    Devuelve horas de inicio válidas para ejecutar toda la secuencia de forma
    consecutiva, asignando automáticamente el profesional más conveniente para
    cada servicio.
    """
    if not services:
        return []

    weekday = selected_date.weekday()
    business_hours = BusinessHours.objects.filter(
        salon=salon,
        weekday=weekday
    ).first()

    if not business_hours or business_hours.is_closed:
        return []

    day_start = timezone.make_aware(
        datetime.combine(selected_date, business_hours.start_time),
        timezone.get_current_timezone()
    )
    day_end = timezone.make_aware(
        datetime.combine(selected_date, business_hours.end_time),
        timezone.get_current_timezone()
    )

    total_duration = sum(service.duration_minutes for service in services)
    if total_duration <= 0:
        return []

    slots = []
    current_start = day_start

    while current_start < day_end:
        sequence_end = current_start + timedelta(minutes=total_duration)
        if sequence_end > day_end:
            break

        assignment = find_auto_assignment_for_start(
            salon=salon,
            services=services,
            selected_date=selected_date,
            start_time=current_start.time(),
            slot_minutes=slot_minutes,
        )

        if assignment:
            slots.append(current_start.strftime('%H:%M'))

        current_start += timedelta(minutes=slot_minutes)

    return slots

def mark_completed_bookings():
    now = timezone.localtime()

    bookings = Booking.objects.exclude(
        status__in=['completed', 'cancelled']
    ).prefetch_related('items')

    updated_ids = []

    for booking in bookings:
        last_item = booking.items.order_by('end_datetime').last()

        if last_item and last_item.end_datetime < now:
            booking.status = 'completed'
            booking.save(update_fields=['status'])
            updated_ids.append(booking.id)

    return updated_ids


def mark_completed_appointments():
    now = timezone.localtime()

    appointments = Appointment.objects.exclude(
        status__in=['completed', 'cancelled']
    ).prefetch_related('services')

    updated_ids = []

    for appointment in appointments:
        end_datetime = appointment.appointment_datetime + timedelta(
            minutes=appointment.get_total_duration_minutes()
        )

        if end_datetime < now:
            appointment.status = 'completed'
            appointment.save(update_fields=['status'])
            updated_ids.append(appointment.id)

    return updated_ids

def expire_unpaid_bookings():
    now = timezone.now()

    bookings = Booking.objects.filter(
        status='pending',
        payment_status='pending',
        payment_expires_at__isnull=False,
        payment_expires_at__lt=now,
    ).exclude(payment_choice='none')

    updated_ids = []

    for booking in bookings:
        booking.status = 'expired'
        booking.save(update_fields=['status'])
        updated_ids.append(booking.id)

    return updated_ids