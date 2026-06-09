from datetime import datetime, timedelta

from django.utils import timezone

from .models import (
    Appointment,
    BookingItem,
    BusinessHourBlock,
    BusinessHours,
    EmployeeTimeOff,
    EmployeeWorkingHour,
)


SLOT_MINUTES = 30


def get_total_duration_minutes(services):
    return sum(service.duration_minutes for service in services)


def overlaps(start_a, end_a, start_b, end_b):
    return start_a < end_b and end_a > start_b


def get_working_ranges_for_date(salon, selected_date):
    """
    Devuelve rangos reales de atención para un salón en una fecha.

    Regla:
    - Si hay BusinessHourBlock activos para ese día, usa esos bloques.
    - Si no hay bloques activos, usa el horario viejo BusinessHours.
    - Si BusinessHours está cerrado o no existe, no hay horarios.
    """
    weekday = selected_date.weekday()

    current_tz = timezone.get_current_timezone()
    working_ranges = []

    active_blocks = list(
        BusinessHourBlock.objects.filter(
            salon=salon,
            weekday=weekday,
            is_active=True
        ).order_by("start_time")
    )

    # 1. Si existen bloques nuevos, usar bloques
    if active_blocks:
        for block in active_blocks:
            if not block.start_time or not block.end_time:
                continue

            if block.start_time >= block.end_time:
                continue

            start_datetime = timezone.make_aware(
                datetime.combine(selected_date, block.start_time),
                current_tz
            )

            end_datetime = timezone.make_aware(
                datetime.combine(selected_date, block.end_time),
                current_tz
            )

            working_ranges.append((start_datetime, end_datetime))

        return working_ranges

    # 2. Si NO hay bloques nuevos, usar BusinessHours viejo
    business_hour = BusinessHours.objects.filter(
        salon=salon,
        weekday=weekday
    ).first()

    if not business_hour:
        return []

    if business_hour.is_closed:
        return []

    if not business_hour.start_time or not business_hour.end_time:
        return []

    if business_hour.start_time >= business_hour.end_time:
        return []

    start_datetime = timezone.make_aware(
        datetime.combine(selected_date, business_hour.start_time),
        current_tz
    )

    end_datetime = timezone.make_aware(
        datetime.combine(selected_date, business_hour.end_time),
        current_tz
    )

    working_ranges.append((start_datetime, end_datetime))

    return working_ranges

def get_employee_working_ranges_for_date(employee, selected_date):
    salon_ranges = get_working_ranges_for_date(
        salon=employee.salon,
        selected_date=selected_date,
    )

    if not salon_ranges:
        return []

    employee_blocks = EmployeeWorkingHour.objects.filter(
        employee=employee,
        weekday=selected_date.weekday(),
        is_active=True,
    ).order_by("start_time")

    if not employee_blocks.exists():
        return salon_ranges

    current_tz = timezone.get_current_timezone()
    working_ranges = []

    for employee_block in employee_blocks:
        if (
            not employee_block.start_time
            or not employee_block.end_time
            or employee_block.start_time >= employee_block.end_time
        ):
            continue

        employee_start = timezone.make_aware(
            datetime.combine(selected_date, employee_block.start_time),
            current_tz,
        )
        employee_end = timezone.make_aware(
            datetime.combine(selected_date, employee_block.end_time),
            current_tz,
        )

        for salon_start, salon_end in salon_ranges:
            range_start = max(employee_start, salon_start)
            range_end = min(employee_end, salon_end)

            if range_start < range_end:
                working_ranges.append((range_start, range_end))

    return sorted(working_ranges, key=lambda working_range: working_range[0])


def get_available_slots(employee, services, selected_date):
    working_ranges = get_working_ranges_for_date(
        salon=employee.salon,
        selected_date=selected_date,
    )

    if not working_ranges:
        return []

    total_duration = get_total_duration_minutes(services)

    if total_duration <= 0:
        return []
    
    step_minutes = total_duration

    existing_appointments = Appointment.objects.filter(
        employee=employee,
        appointment_datetime__date=selected_date,
    ).exclude(
        status='cancelled'
    ).prefetch_related('services', 'service')

    existing_booking_items = BookingItem.objects.select_related('booking').filter(
        employee=employee,
        start_datetime__date=selected_date,
    ).exclude(
        booking__status__in=['cancelled', 'expired']
    )

    existing_time_off_blocks = EmployeeTimeOff.objects.filter(
        employee=employee,
        start_datetime__date=selected_date,
    )

    occupied_ranges = []

    for existing in existing_appointments:
        existing_start = existing.appointment_datetime
        existing_end = existing_start + timedelta(
            minutes=existing.get_total_duration_minutes()
        )
        occupied_ranges.append((existing_start, existing_end))

    for item in existing_booking_items:
        if item.booking.is_blocking_slot():
            occupied_ranges.append((item.start_datetime, item.end_datetime))

    for block in existing_time_off_blocks:
        occupied_ranges.append((block.start_datetime, block.end_datetime))

    slots = []

    for range_start, range_end in working_ranges:
        current_start = range_start

        while current_start < range_end:
            current_end = current_start + timedelta(minutes=total_duration)

            if current_end > range_end:
                break

            is_available = True

            for occupied_start, occupied_end in occupied_ranges:
                if overlaps(current_start, current_end, occupied_start, occupied_end):
                    is_available = False
                    break

            if is_available:
                slot_text = current_start.strftime('%H:%M')

                if slot_text not in slots:
                    slots.append(slot_text)

            current_start += timedelta(minutes=step_minutes)

    return slots
