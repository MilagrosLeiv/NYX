from datetime import datetime, timedelta

from django.db.models import Q
from django.utils import timezone

from .models import (
    Appointment,
    BookingItem,
    BusinessHourBlock,
    BusinessHours,
    EmployeeTimeOff,
    EmployeeWorkingHour,
    SpecialAvailabilityBlock,
)


SLOT_MINUTES = 30


def get_total_duration_minutes(services):
    return sum(service.duration_minutes for service in services)


def overlaps(start_a, end_a, start_b, end_b):
    return start_a < end_b and end_a > start_b


def get_special_block_ranges(employee, selected_date):
    current_tz = timezone.get_current_timezone()
    day_start = timezone.make_aware(
        datetime.combine(selected_date, datetime.min.time()),
        current_tz,
    )
    day_end = day_start + timedelta(days=1)

    special_blocks = SpecialAvailabilityBlock.objects.filter(
        salon=employee.salon,
        start_datetime__lt=day_end,
        end_datetime__gt=day_start,
    ).filter(
        Q(employee__isnull=True) | Q(employee=employee)
    )
    ranges = [
        (max(block.start_datetime, day_start), min(block.end_datetime, day_end))
        for block in special_blocks
    ]

    legacy_blocks = EmployeeTimeOff.objects.filter(
        employee=employee,
        start_datetime__lt=day_end,
        end_datetime__gt=day_start,
    )
    ranges.extend(
        (max(block.start_datetime, day_start), min(block.end_datetime, day_end))
        for block in legacy_blocks
    )
    return ranges


def get_working_ranges_for_date(salon, selected_date):
    weekday = selected_date.weekday()

    print("========== DEBUG get_working_ranges_for_date ==========")
    print("salon id:", salon.id)
    print("salon name:", salon.name)
    print("selected_date:", selected_date)
    print("weekday:", weekday)

    current_tz = timezone.get_current_timezone()
    working_ranges = []

    active_blocks = list(
        BusinessHourBlock.objects.filter(
            salon=salon,
            weekday=weekday,
            is_active=True
        ).order_by("start_time")
    )

    print("active_blocks count:", len(active_blocks))
    print("active_blocks values:", list(
        BusinessHourBlock.objects.filter(
            salon=salon,
            weekday=weekday,
            is_active=True
        ).values("id", "weekday", "start_time", "end_time", "is_active")
    ))

    if active_blocks:
        print("USANDO BusinessHourBlock")

        for block in active_blocks:
            print("block:", block.id, block.start_time, block.end_time, block.is_active)

            if not block.start_time or not block.end_time:
                print("block salteado por falta de start/end")
                continue

            if block.start_time >= block.end_time:
                print("block salteado por rango invalido")
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

        print("working_ranges desde blocks:", working_ranges)
        return working_ranges

    print("NO HAY BLOQUES ACTIVOS. PROBANDO BusinessHours")

    business_hour = BusinessHours.objects.filter(
        salon=salon,
        weekday=weekday
    ).first()

    print("business_hour:", business_hour)

    print("business_hours values del salon:", list(
        BusinessHours.objects.filter(salon=salon).values(
            "id", "weekday", "start_time", "end_time", "is_closed"
        )
    ))

    if not business_hour:
        print("RETURN [] porque no existe BusinessHours para weekday:", weekday)
        return []

    if business_hour.is_closed:
        print("RETURN [] porque BusinessHours esta cerrado")
        return []

    if not business_hour.start_time or not business_hour.end_time:
        print("RETURN [] porque BusinessHours no tiene start_time/end_time")
        return []

    if business_hour.start_time >= business_hour.end_time:
        print("RETURN [] porque BusinessHours tiene rango invalido")
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

    print("working_ranges desde BusinessHours:", working_ranges)

    return working_ranges

def get_employee_working_ranges_for_date(employee, selected_date):
    salon_ranges = get_working_ranges_for_date(
        salon=employee.salon,
        selected_date=selected_date,
    )

    if not salon_ranges:
        return []

    employee_hours = EmployeeWorkingHour.objects.filter(employee=employee)

    # Si nunca se configuraron horarios propios, el profesional hereda el salón.
    if not employee_hours.exists():
        return salon_ranges

    employee_blocks = employee_hours.filter(
        weekday=selected_date.weekday(),
        is_active=True,
    ).order_by("start_time")

    if not employee_blocks.exists():
        return []

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
    print("========== DEBUG get_available_slots ==========")
    print("employee:", employee.id, employee)
    print("employee salon:", employee.salon_id)
    print("selected_date:", selected_date)
    print("services:", [(s.id, s.name, s.duration_minutes) for s in services])

    working_ranges = get_employee_working_ranges_for_date(
        employee=employee,
        selected_date=selected_date,
    )

    print("working_ranges count:", len(working_ranges))
    print("working_ranges:", working_ranges)

    if not working_ranges:
        print("RETURN [] PORQUE working_ranges ESTA VACIO")
        return []

    total_duration = get_total_duration_minutes(services)

    print("total_duration:", total_duration)

    if total_duration <= 0:
        print("RETURN [] PORQUE total_duration <= 0")
        return []

    step_minutes = 30
    print("step_minutes:", step_minutes)

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

    existing_time_off_blocks = get_special_block_ranges(
        employee,
        selected_date,
    )

    print("existing_appointments:", list(
        existing_appointments.values("id", "appointment_datetime", "status")
    ))

    print("existing_booking_items:", list(
        existing_booking_items.values(
            "id",
            "start_datetime",
            "end_datetime",
            "booking__status",
        )
    ))

    print("existing_time_off_blocks:", existing_time_off_blocks)

    occupied_ranges = []

    for existing in existing_appointments:
        existing_start = existing.appointment_datetime
        existing_end = existing_start + timedelta(
            minutes=existing.get_total_duration_minutes()
        )
        occupied_ranges.append((existing_start, existing_end))
        print("occupied appointment:", existing.id, existing_start, existing_end)

    for item in existing_booking_items:
        print(
            "booking item:",
            item.id,
            item.start_datetime,
            item.end_datetime,
            "booking status:",
            item.booking.status,
            "is_blocking:",
            item.booking.is_blocking_slot()
        )

        if item.booking.is_blocking_slot():
            occupied_ranges.append((item.start_datetime, item.end_datetime))

    for block_start, block_end in existing_time_off_blocks:
        occupied_ranges.append((block_start, block_end))
        print("occupied time off:", block_start, block_end)

    print("occupied_ranges count:", len(occupied_ranges))
    print("occupied_ranges:", occupied_ranges)

    slots = []

    for range_start, range_end in working_ranges:
        range_minutes = int((range_end - range_start).total_seconds() / 60)

        print("----- WORKING RANGE -----")
        print("range_start:", range_start)
        print("range_end:", range_end)
        print("range_minutes:", range_minutes)
        print("total_duration:", total_duration)

        current_start = range_start

        while current_start < range_end:
            current_end = current_start + timedelta(minutes=total_duration)

            print("probando slot:", current_start, "->", current_end)

            if current_end > range_end:
                print("BREAK porque current_end > range_end:", current_end, ">", range_end)
                break

            is_available = True

            for occupied_start, occupied_end in occupied_ranges:
                if overlaps(current_start, current_end, occupied_start, occupied_end):
                    print(
                        "NO disponible por overlap:",
                        current_start,
                        current_end,
                        "choca con",
                        occupied_start,
                        occupied_end
                    )
                    is_available = False
                    break

            if is_available:
                slot_text = current_start.strftime('%H:%M')

                if slot_text not in slots:
                    slots.append(slot_text)
                    print("SLOT AGREGADO:", slot_text)

            current_start += timedelta(minutes=step_minutes)

    print("SLOTS FINALES:", slots)

    return slots
