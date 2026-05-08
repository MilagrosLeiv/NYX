from datetime import datetime, timedelta

from django.utils import timezone

from .models import Appointment, BusinessHours, BookingItem, EmployeeTimeOff


SLOT_MINUTES = 30


def get_total_duration_minutes(services):
    return sum(service.duration_minutes for service in services)


def overlaps(start_a, end_a, start_b, end_b):
    return start_a < end_b and end_a > start_b


def get_available_slots(employee, services, selected_date):
    weekday = selected_date.weekday()
    business_hours = BusinessHours.objects.filter(
        salon=employee.salon,
        weekday=weekday
    ).first()

    if not business_hours or business_hours.is_closed:
        return []

    total_duration = get_total_duration_minutes(services)
    if total_duration <= 0:
        return []

    day_start = timezone.make_aware(
        datetime.combine(selected_date, business_hours.start_time),
        timezone.get_current_timezone()
    )
    day_end = timezone.make_aware(
        datetime.combine(selected_date, business_hours.end_time),
        timezone.get_current_timezone()
    )

    existing_appointments = Appointment.objects.filter(
        employee=employee,
        appointment_datetime__date=selected_date,
    ).exclude(status='cancelled').prefetch_related('services', 'service')

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
        existing_end = existing_start + timedelta(minutes=existing.get_total_duration_minutes())
        occupied_ranges.append((existing_start, existing_end))

    for item in existing_booking_items:
        if item.booking.is_blocking_slot():
            occupied_ranges.append((item.start_datetime, item.end_datetime))

    for block in existing_time_off_blocks:
        occupied_ranges.append((block.start_datetime, block.end_datetime))

    slots = []
    current_start = day_start

    while current_start < day_end:
        current_end = current_start + timedelta(minutes=total_duration)

        if current_end > day_end:
            break

        is_available = True

        for occupied_start, occupied_end in occupied_ranges:
            if overlaps(current_start, current_end, occupied_start, occupied_end):
                is_available = False
                break

        if is_available:
            slots.append(current_start.strftime('%H:%M'))

        current_start += timedelta(minutes=SLOT_MINUTES)

    return slots