from datetime import date, datetime, time, timedelta
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone

from . import panel_views
from .panel_forms import ManualBookingForm
from .services.google_calendar import (
    delete_booking_item_from_google_calendar,
    sync_booking_item_to_google_calendar,
)
from .panel_views import _create_manual_booking, _mercadopago_panel_context
from .models import Booking, Employee, Salon, Service, SpecialAvailabilityBlock
from .utils import (
    get_employee_working_ranges_for_date,
    get_special_block_ranges,
)


@override_settings(
    GOOGLE_CLIENT_ID="client-id",
    GOOGLE_CLIENT_SECRET="client-secret",
)
class GoogleCalendarServiceTests(SimpleTestCase):
    def build_item(self, event_id=None):
        integration = SimpleNamespace(
            access_token="access-token",
            refresh_token="refresh-token",
            token_expiry=None,
            calendar_id="primary",
            is_active=True,
            sync_confirmed_bookings=True,
            sync_pending_bookings=False,
            is_connected=lambda: True,
            save=Mock(),
        )
        salon = SimpleNamespace(
            name="Salón NYX",
            google_calendar_integration=integration,
        )
        booking = SimpleNamespace(
            status="confirmed",
            salon=salon,
            customer_name="Ana",
            customer_phone="3415550000",
            customer_email="ana@example.com",
            get_status_display=lambda: "Confirmado",
        )
        service = SimpleNamespace(name="Corte", duration_minutes=45)
        employee = SimpleNamespace(name="Julia")
        start = timezone.now() + timedelta(days=1)

        item = SimpleNamespace(
            pk=1,
            booking=booking,
            service=service,
            employee=employee,
            start_datetime=start,
            end_datetime=None,
            google_calendar_event_id=event_id,
            google_calendar_synced_at=None,
            save=Mock(),
        )
        return item

    @patch("reservas.services.google_calendar.get_calendar_service")
    def test_sync_inserts_event_and_uses_duration_fallback(self, get_service):
        item = self.build_item()
        calendar_service = Mock()
        credentials = SimpleNamespace(token="access-token", expiry=None)
        get_service.return_value = (calendar_service, credentials)
        calendar_service.events.return_value.insert.return_value.execute.return_value = {
            "id": "google-event-1"
        }

        result = sync_booking_item_to_google_calendar(item)

        self.assertTrue(result)
        self.assertEqual(item.google_calendar_event_id, "google-event-1")
        body = calendar_service.events.return_value.insert.call_args.kwargs["body"]
        expected_end = item.start_datetime + timedelta(minutes=45)
        self.assertEqual(body["end"]["dateTime"], expected_end.isoformat())
        item.save.assert_called_once()

    @patch("reservas.services.google_calendar.get_calendar_service")
    def test_delete_clears_local_event_data(self, get_service):
        item = self.build_item(event_id="google-event-1")
        calendar_service = Mock()
        credentials = SimpleNamespace(token="access-token", expiry=None)
        get_service.return_value = (calendar_service, credentials)

        result = delete_booking_item_from_google_calendar(item)

        self.assertTrue(result)
        self.assertIsNone(item.google_calendar_event_id)
        self.assertIsNone(item.google_calendar_synced_at)
        item.save.assert_called_once()

    @patch(
        "reservas.services.google_calendar.get_calendar_service",
        side_effect=RuntimeError("Google unavailable"),
    )
    @patch("reservas.services.google_calendar.logger.exception")
    def test_sync_returns_false_when_google_fails(self, logger_exception, get_service):
        item = self.build_item()

        result = sync_booking_item_to_google_calendar(item)

        self.assertFalse(result)
        item.save.assert_not_called()
        logger_exception.assert_called_once()


class MercadoPagoPanelContextTests(SimpleTestCase):
    @patch("reservas.panel_views.SalonPaymentSettings.objects.filter")
    def test_visual_status_uses_valid_connection_without_saving(self, filter_mock):
        payment_settings = Mock()
        payment_settings.has_valid_mercadopago_connection.return_value = True
        filter_mock.return_value.first.return_value = payment_settings
        salon = SimpleNamespace(payment_method="integrated")

        context = _mercadopago_panel_context(salon)

        self.assertTrue(context["mercadopago_ready"])
        self.assertTrue(context["mercadopago_visible_to_clients"])
        payment_settings.has_valid_mercadopago_connection.assert_called_once_with()
        payment_settings.save.assert_not_called()


class EmployeeWorkingHoursAvailabilityTests(SimpleTestCase):
    selected_date = date(2026, 6, 15)  # Lunes

    def build_range(self, start_time, end_time):
        current_tz = timezone.get_current_timezone()
        return (
            timezone.make_aware(
                datetime.combine(self.selected_date, start_time),
                current_tz,
            ),
            timezone.make_aware(
                datetime.combine(self.selected_date, end_time),
                current_tz,
            ),
        )

    @patch("reservas.utils.EmployeeWorkingHour.objects.filter")
    @patch("reservas.utils.get_working_ranges_for_date")
    def test_employee_without_own_hours_inherits_salon_hours(
        self,
        get_salon_ranges,
        filter_mock,
    ):
        salon_ranges = [self.build_range(time(9, 0), time(20, 0))]
        get_salon_ranges.return_value = salon_ranges
        filter_mock.return_value.exists.return_value = False
        employee = SimpleNamespace(salon=SimpleNamespace())

        ranges = get_employee_working_ranges_for_date(
            employee,
            self.selected_date,
        )

        self.assertEqual(ranges, salon_ranges)

    @patch("reservas.utils.EmployeeWorkingHour.objects.filter")
    @patch("reservas.utils.get_working_ranges_for_date")
    def test_configured_employee_without_monday_block_is_unavailable(
        self,
        get_salon_ranges,
        filter_mock,
    ):
        get_salon_ranges.return_value = [
            self.build_range(time(9, 0), time(20, 0))
        ]
        employee_hours = filter_mock.return_value
        employee_hours.exists.return_value = True
        day_blocks = employee_hours.filter.return_value.order_by.return_value
        day_blocks.exists.return_value = False
        employee = SimpleNamespace(salon=SimpleNamespace())

        ranges = get_employee_working_ranges_for_date(
            employee,
            self.selected_date,
        )

        self.assertEqual(ranges, [])

    @patch("reservas.utils.EmployeeWorkingHour.objects.filter")
    @patch("reservas.utils.get_working_ranges_for_date")
    def test_split_hours_are_intersected_with_salon_hours(
        self,
        get_salon_ranges,
        filter_mock,
    ):
        get_salon_ranges.return_value = [
            self.build_range(time(10, 0), time(18, 0))
        ]
        employee_hours = filter_mock.return_value
        employee_hours.exists.return_value = True
        day_blocks = employee_hours.filter.return_value.order_by.return_value
        day_blocks.exists.return_value = True
        day_blocks.__iter__ = Mock(return_value=iter([
            SimpleNamespace(start_time=time(9, 0), end_time=time(13, 0)),
            SimpleNamespace(start_time=time(16, 0), end_time=time(20, 0)),
        ]))
        employee = SimpleNamespace(salon=SimpleNamespace())

        ranges = get_employee_working_ranges_for_date(
            employee,
            self.selected_date,
        )

        self.assertEqual(
            [(start.time(), end.time()) for start, end in ranges],
            [(time(10, 0), time(13, 0)), (time(16, 0), time(18, 0))],
        )


class SpecialAvailabilityBlockTests(SimpleTestCase):
    selected_date = date(2026, 6, 15)

    def setUp(self):
        self.salon = Salon(
            id=1,
            name='NYX Test',
            slug='nyx-test',
        )
        self.employee_one = Employee(
            id=1,
            salon=self.salon,
            name='Ana',
        )
        self.employee_two = Employee(
            id=2,
            salon=self.salon,
            name='Julia',
        )

    def aware(self, hour, minute=0):
        return timezone.make_aware(
            datetime.combine(self.selected_date, time(hour, minute)),
            timezone.get_current_timezone(),
        )

    @patch("reservas.utils.EmployeeTimeOff.objects.filter")
    @patch("reservas.utils.SpecialAvailabilityBlock.objects.filter")
    def test_salon_block_applies_to_every_employee(
        self,
        special_filter,
        legacy_filter,
    ):
        block = SpecialAvailabilityBlock(
            salon=self.salon,
            title='Capacitación',
            block_type=SpecialAvailabilityBlock.BlockType.SPECIAL_CLOSURE,
            start_datetime=self.aware(10),
            end_datetime=self.aware(12),
        )
        scoped_blocks = Mock()
        scoped_blocks.filter.return_value = [block]
        special_filter.return_value = scoped_blocks
        legacy_filter.return_value = []

        ranges_one = get_special_block_ranges(
            self.employee_one,
            self.selected_date,
        )
        ranges_two = get_special_block_ranges(
            self.employee_two,
            self.selected_date,
        )

        self.assertEqual(ranges_one, [(self.aware(10), self.aware(12))])
        self.assertEqual(ranges_two, [(self.aware(10), self.aware(12))])

    @patch("reservas.utils.EmployeeTimeOff.objects.filter")
    @patch("reservas.utils.SpecialAvailabilityBlock.objects.filter")
    def test_employee_block_only_applies_to_selected_employee(
        self,
        special_filter,
        legacy_filter,
    ):
        block = SpecialAvailabilityBlock(
            salon=self.salon,
            employee=self.employee_one,
            title='Médico',
            block_type=SpecialAvailabilityBlock.BlockType.PERSONAL,
            start_datetime=self.aware(14),
            end_datetime=self.aware(16),
        )
        scoped_blocks = Mock()

        def blocks_for_scope(scope):
            selected_employee = dict(scope.children).get('employee')
            return [block] if selected_employee is self.employee_one else []

        scoped_blocks.filter.side_effect = blocks_for_scope
        special_filter.return_value = scoped_blocks
        legacy_filter.return_value = []

        self.assertEqual(
            get_special_block_ranges(self.employee_one, self.selected_date),
            [(self.aware(14), self.aware(16))],
        )
        self.assertEqual(
            get_special_block_ranges(self.employee_two, self.selected_date),
            [],
        )

    def test_end_must_be_after_start(self):
        block = SpecialAvailabilityBlock(
            salon=self.salon,
            title='Rango inválido',
            start_datetime=self.aware(12),
            end_datetime=self.aware(12),
        )

        with self.assertRaises(ValidationError):
            block.clean()


class ManualBookingFormTests(SimpleTestCase):
    @patch("reservas.panel_forms.Service.objects.filter")
    @patch("reservas.panel_forms.Service.objects.none")
    @patch("reservas.panel_forms.Employee.objects.filter")
    def test_services_are_filtered_by_selected_employee(
        self,
        employee_filter,
        service_none,
        service_filter,
    ):
        employee_filter.return_value.order_by.return_value = Mock()
        service_none.return_value = Mock()
        service_filter.return_value.distinct.return_value.order_by.return_value = Mock()
        salon = SimpleNamespace(id=3)

        ManualBookingForm(
            data={'employee': '8'},
            salon=salon,
        )

        service_filter.assert_called_once_with(
            salon=salon,
            is_active=True,
            employees__id='8',
            employees__salon=salon,
        )

    @patch("reservas.panel_forms.Service.objects.none")
    @patch("reservas.panel_forms.Employee.objects.filter")
    def test_services_are_empty_without_employee(
        self,
        employee_filter,
        service_none,
    ):
        employee_filter.return_value.order_by.return_value = Mock()
        empty_services = Mock()
        service_none.return_value = empty_services

        form = ManualBookingForm(salon=SimpleNamespace(id=3))

        service_none.assert_called_once_with()
        self.assertIs(form.fields['service'].queryset, empty_services.all())


class ManualBookingCreationTests(SimpleTestCase):
    def setUp(self):
        self.salon = SimpleNamespace(id=1)
        self.employee_input = SimpleNamespace(pk=7)
        self.service_input = SimpleNamespace(pk=9)
        self.employee = SimpleNamespace(pk=7, name='Luana')
        self.service = SimpleNamespace(
            pk=9,
            name='Corte',
            duration_minutes=60,
        )
        self.start = timezone.make_aware(
            datetime(2026, 7, 6, 10, 0),
            timezone.get_current_timezone(),
        )
        self.cleaned_data = {
            'customer_name': 'Ana',
            'customer_phone': '3415550000',
            'customer_email': '',
            'notes': 'Cliente habitual',
            'employee': self.employee_input,
            'service': self.service_input,
            'appointment_date': self.start.date(),
            'appointment_time': self.start.time(),
            'start_datetime': self.start,
            'end_datetime': self.start + timedelta(hours=1),
        }

    def run_create(self, available_slots):
        booking = Mock()
        item = Mock()

        with (
            patch(
                "reservas.panel_views.transaction.atomic",
                return_value=nullcontext(),
            ),
            patch(
                "reservas.panel_views.Employee.objects.select_for_update"
            ) as select_for_update,
            patch(
                "reservas.panel_views.Service.objects.get",
                return_value=self.service,
            ),
            patch(
                "reservas.panel_views.get_available_slots",
                return_value=available_slots,
            ),
            patch(
                "reservas.panel_views.Booking.objects.create",
                return_value=booking,
            ) as booking_create,
            patch(
                "reservas.panel_views.BookingItem",
                return_value=item,
            ),
        ):
            select_for_update.return_value.get.return_value = self.employee
            result = _create_manual_booking(self.salon, self.cleaned_data)

        return result, booking, item, booking_create

    def test_creates_valid_manual_booking(self):
        result, booking, item, booking_create = self.run_create(['10:00'])

        self.assertIs(result, booking)
        item.full_clean.assert_called_once_with()
        item.save.assert_called_once_with()
        booking_create.assert_called_once()

    def test_created_booking_blocks_the_slot(self):
        booking = Booking(
            status='confirmed',
            payment_choice='none',
            payment_required_amount=0,
        )

        self.assertTrue(booking.is_blocking_slot())

    def test_does_not_create_payment_or_mercadopago_data(self):
        _result, _booking, _item, booking_create = self.run_create(['10:00'])
        kwargs = booking_create.call_args.kwargs

        self.assertEqual(kwargs['payment_choice'], 'none')
        self.assertEqual(kwargs['payment_status'], 'not_required')
        self.assertEqual(kwargs['payment_required_amount'], 0)
        self.assertEqual(kwargs['selected_payment_method'], 'none')
        self.assertNotIn('payment_provider', kwargs)
        self.assertNotIn('payment_checkout_url', kwargs)

    def test_rejects_service_not_assigned_to_employee(self):
        with (
            patch(
                "reservas.panel_views.transaction.atomic",
                return_value=nullcontext(),
            ),
            patch(
                "reservas.panel_views.Employee.objects.select_for_update"
            ) as select_for_update,
            patch(
                "reservas.panel_views.Service.objects.get",
                side_effect=Service.DoesNotExist,
            ),
            patch("reservas.panel_views.Booking.objects.create") as booking_create,
        ):
            select_for_update.return_value.get.return_value = self.employee

            with self.assertRaisesMessage(
                ValidationError,
                'El profesional o el servicio ya no están disponibles.',
            ):
                _create_manual_booking(self.salon, self.cleaned_data)

        booking_create.assert_not_called()

    def test_rejects_slot_outside_employee_hours(self):
        with self.assertRaisesMessage(
            ValidationError,
            'Ese horario ya no está disponible. Elegí otro horario.',
        ):
            self.run_create([])

    def test_rejects_slot_during_special_block(self):
        with self.assertRaisesMessage(
            ValidationError,
            'Ese horario ya no está disponible. Elegí otro horario.',
        ):
            self.run_create([])


class ManualBookingPermissionTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.view = panel_views.panel_manual_booking_create

    @patch("reservas.panel_views.get_or_create_salon_subscription")
    @patch("reservas.panel_views.get_user_salon")
    @patch("reservas.panel_views.is_owner_user", return_value=False)
    def test_staff_cannot_create_manual_booking(
        self,
        _is_owner,
        get_salon,
        get_subscription,
    ):
        request = self.factory.get('/panel/agenda/cargar-turno/')
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
        )
        get_salon.return_value = SimpleNamespace(id=1)
        get_subscription.return_value.has_access.return_value = True

        with self.assertRaises(PermissionDenied):
            self.view(request)

    @patch("reservas.panel_views.get_or_create_salon_subscription")
    @patch("reservas.panel_views.render", return_value=Mock())
    @patch("reservas.panel_views.ManualBookingForm")
    @patch("reservas.panel_views.get_user_salon")
    @patch("reservas.panel_views.is_owner_user", return_value=True)
    def test_owner_can_open_manual_booking_form(
        self,
        _is_owner,
        get_salon,
        form_class,
        render_mock,
        get_subscription,
    ):
        request = self.factory.get('/panel/agenda/cargar-turno/')
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
        )
        salon = SimpleNamespace(id=1)
        get_salon.return_value = salon
        get_subscription.return_value.has_access.return_value = True

        self.view(request)

        form_class.assert_called_once_with(salon=salon, initial={})
        render_mock.assert_called_once()
