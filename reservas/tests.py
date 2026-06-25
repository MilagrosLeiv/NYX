from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core import mail
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import panel_views
from .mail_utils import send_booking_confirmed_email
from .panel_forms import ManualBookingForm
from .services.google_calendar import (
    delete_booking_item_from_google_calendar,
    sync_booking_item_to_google_calendar,
)
from .panel_views import _create_manual_booking, _mercadopago_panel_context
from .models import (
    Booking,
    BookingItem,
    BusinessHourBlock,
    Employee,
    EmployeeWorkingHour,
    Salon,
    SalonMembership,
    SalonSubscription,
    Service,
    ServiceCategory,
    SpecialAvailabilityBlock,
)
from .utils import (
    get_employee_working_ranges_for_date,
    get_special_block_ranges,
)


@override_settings(
    DEFAULT_FROM_EMAIL="turnos@example.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    TIME_ZONE="America/Argentina/Buenos_Aires",
)
class BookingEmailTimezoneTests(TestCase):
    def test_confirmation_email_formats_booking_item_in_local_timezone(self):
        salon = Salon.objects.create(name="Salon Test", slug="salon-email-test")
        service = Service.objects.create(
            salon=salon,
            name="Corte",
            price=1000,
            duration_minutes=60,
        )
        employee = Employee.objects.create(salon=salon, name="Ana")
        booking = Booking.objects.create(
            salon=salon,
            customer_name="Cliente",
            customer_phone="3415550000",
            customer_email="cliente@example.com",
            status="confirmed",
        )
        BookingItem.objects.create(
            booking=booking,
            service=service,
            employee=employee,
            start_datetime=datetime(2026, 6, 22, 12, 0, tzinfo=datetime_timezone.utc),
            end_datetime=datetime(2026, 6, 22, 13, 0, tzinfo=datetime_timezone.utc),
        )

        sent = send_booking_confirmed_email(booking)

        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Hora de inicio: 09:00", mail.outbox[0].body)
        self.assertIn("09:00", mail.outbox[0].alternatives[0].content)
        self.assertNotIn("Hora de inicio: 12:00", mail.outbox[0].body)


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


class GuidedOnboardingDecisionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="owner",
            email="owner@example.com",
            password="pass12345",
        )
        self.salon = Salon.objects.create(
            name="Salon Test",
            slug="salon-test",
            email="salon@example.com",
        )
        SalonMembership.objects.create(
            user=self.user,
            salon=self.salon,
            role="owner",
            is_active=True,
        )
        SalonSubscription.objects.create(
            salon=self.salon,
            status=SalonSubscription.Status.TRIAL,
            plan=SalonSubscription.Plan.BASIC,
        )
        self.client.login(username="owner", password="pass12345")

    def activate_tutorial(self, step=1):
        self.salon.onboarding_current_step = step
        self.salon.onboarding_dismissed = False
        self.salon.onboarding_completed = False
        self.salon.save(update_fields=[
            "onboarding_current_step",
            "onboarding_dismissed",
            "onboarding_completed",
        ])
        session = self.client.session
        session["nyx_onboarding_active"] = True
        session.save()

    def test_new_salon_shows_initial_welcome_modal(self):
        response = self.client.get(reverse("panel_dashboard"))

        self.assertTrue(response.context["onboarding_show_prompt"])
        self.assertContains(response, "Bienvenida a NYX")
        self.assertContains(response, "Empezar tutorial")
        self.assertContains(response, "Lo hago después")

    def test_dismiss_welcome_does_not_show_it_again(self):
        self.client.post(reverse("panel_onboarding_dismiss"))

        response = self.client.get(reverse("panel_dashboard"))

        self.salon.refresh_from_db()
        self.assertTrue(self.salon.onboarding_dismissed)
        self.assertFalse(response.context["onboarding_show_prompt"])
        self.assertNotContains(response, "Bienvenida a NYX")

    def test_start_tutorial_activates_onboarding(self):
        response = self.client.post(
            reverse("panel_onboarding_start"),
            {"reset": "1"},
            follow=True,
        )

        self.salon.refresh_from_db()
        self.assertFalse(self.salon.onboarding_dismissed)
        self.assertFalse(self.salon.onboarding_completed)
        self.assertEqual(self.salon.onboarding_current_step, 1)
        self.assertTrue(self.client.session["nyx_onboarding_active"])
        self.assertIsNotNone(response.context["onboarding_modal"])

    def test_cancel_category_creation_does_not_advance_tutorial(self):
        ServiceCategory.objects.create(salon=self.salon, name="Existente")
        self.activate_tutorial(step=1)

        self.client.get(reverse("panel_service_category_create"))
        response = self.client.get(reverse("panel_service_categories"))

        self.salon.refresh_from_db()
        modal = response.context["onboarding_modal"]
        self.assertIsNotNone(modal)
        self.assertNotEqual(modal["title"], "Categoría creada correctamente")
        self.assertEqual(self.salon.onboarding_current_step, 1)
        self.assertNotIn("nyx_onboarding_completed_step", self.client.session)

    def test_create_category_shows_created_modal(self):
        self.activate_tutorial(step=1)

        response = self.client.post(
            reverse("panel_service_category_create"),
            {
                "name": "Color",
                "description": "",
                "order": "0",
                "is_active": "on",
            },
            follow=True,
        )

        modal = response.context["onboarding_modal"]
        self.assertEqual(modal["title"], "Categoría creada correctamente")
        self.assertEqual(ServiceCategory.objects.filter(salon=self.salon).count(), 1)

    def test_duplicate_category_name_shows_form_error_without_success_modal(self):
        ServiceCategory.objects.create(salon=self.salon, name="Color")
        self.activate_tutorial(step=1)

        response = self.client.post(
            reverse("panel_service_category_create"),
            {
                "name": "Color",
                "description": "",
                "order": "0",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            "name",
            "Ya existe una categoría con ese nombre en tu salón.",
        )
        self.assertEqual(ServiceCategory.objects.filter(salon=self.salon).count(), 1)
        self.assertNotIn("nyx_onboarding_completed_step", self.client.session)

    def test_add_another_category_keeps_current_step(self):
        self.activate_tutorial(step=1)
        session = self.client.session
        session["nyx_onboarding_completed_step"] = 1
        session.save()

        response = self.client.post(
            reverse("panel_onboarding_decision"),
            {
                "decision": "repeat",
                "step": "1",
                "next": reverse("panel_service_category_create"),
            },
        )

        self.salon.refresh_from_db()
        self.assertRedirects(response, reverse("panel_service_category_create"))
        self.assertEqual(self.salon.onboarding_current_step, 1)
        self.assertNotIn("nyx_onboarding_completed_step", self.client.session)

    def test_continue_with_services_advances_to_service_step(self):
        self.activate_tutorial(step=1)
        session = self.client.session
        session["nyx_onboarding_completed_step"] = 1
        session.save()

        response = self.client.post(
            reverse("panel_onboarding_decision"),
            {
                "decision": "continue",
                "step": "2",
                "next": reverse("panel_service_create"),
            },
        )

        self.salon.refresh_from_db()
        self.assertRedirects(response, reverse("panel_service_create"))
        self.assertEqual(self.salon.onboarding_current_step, 2)
        self.assertNotIn("nyx_onboarding_completed_step", self.client.session)

    def test_employee_hours_screen_does_not_show_blocking_step_modal(self):
        employee = Employee.objects.create(salon=self.salon, name="Lara")
        self.activate_tutorial(step=5)

        response = self.client.get(
            reverse("panel_employee_working_hours", args=[employee.id])
        )

        self.assertIsNone(response.context.get("onboarding_modal"))
        self.assertTrue(response.context["show_working_hours_decision"])
        self.assertContains(response, "Horarios de trabajo")
        self.assertNotIn("nyx_onboarding_completed_step", self.client.session)

    def test_use_salon_hours_completes_step_without_creating_employee_hours(self):
        employee = Employee.objects.create(salon=self.salon, name="Lara")
        BusinessHourBlock.objects.create(
            salon=self.salon,
            weekday=0,
            start_time=time(9, 0),
            end_time=time(17, 0),
            is_active=True,
        )
        self.activate_tutorial(step=5)

        response = self.client.post(
            reverse("panel_employee_working_hours_use_salon", args=[employee.id]),
            follow=True,
        )

        self.assertEqual(EmployeeWorkingHour.objects.filter(employee=employee).count(), 0)
        modal = response.context["onboarding_modal"]
        self.assertEqual(modal["title"], "Horarios configurados")
        self.assertEqual(self.client.session["nyx_onboarding_completed_step"], 5)

    def test_custom_working_hours_mark_professional_hours_resolved(self):
        employee = Employee.objects.create(salon=self.salon, name="Lara")
        self.activate_tutorial(step=5)
        session = self.client.session
        session["nyx_onboarding_pending_employee_hours"] = [employee.id]
        session.save()

        response = self.client.post(
            reverse("panel_employee_working_hour_create", args=[employee.id]),
            {
                "weekdays": ["0", "2"],
                "start_time": "09:00",
                "end_time": "13:00",
                "is_active": "on",
            },
            follow=True,
        )

        self.assertEqual(EmployeeWorkingHour.objects.filter(employee=employee).count(), 2)
        modal = response.context["onboarding_modal"]
        self.assertEqual(modal["title"], "Horarios configurados")
        self.assertNotIn(employee.id, self.client.session["nyx_onboarding_pending_employee_hours"])
        self.assertEqual(self.client.session["nyx_onboarding_completed_step"], 5)

    def test_created_professional_modal_requires_hours_decision(self):
        self.activate_tutorial(step=3)

        response = self.client.post(
            reverse("panel_employee_create"),
            {
                "name": "Lara",
                "phone": "",
                "email": "",
                "is_active": "on",
            },
            follow=True,
        )

        employee = Employee.objects.get(salon=self.salon, name="Lara")
        modal = response.context["onboarding_modal"]
        labels = [action["label"] for action in modal["actions"]]
        self.assertEqual(modal["title"], "Profesional creado correctamente")
        self.assertIn("Usar horarios del salón", labels)
        self.assertIn("Definir horarios personalizados", labels)
        self.assertIn(employee.id, self.client.session["nyx_onboarding_pending_employee_hours"])

    def test_resolved_professional_hours_asks_to_add_another_or_continue(self):
        self.activate_tutorial(step=3)
        employee = Employee.objects.create(salon=self.salon, name="Lara")
        session = self.client.session
        session["nyx_onboarding_pending_employee_hours"] = [employee.id]
        session.save()

        response = self.client.post(
            reverse("panel_employee_working_hours_use_salon", args=[employee.id]),
            follow=True,
        )

        modal = response.context["onboarding_modal"]
        labels = [action["label"] for action in modal["actions"]]
        self.assertEqual(modal["title"], "Horarios configurados")
        self.assertIn("Agregar otro profesional", labels)
        self.assertIn("Continuar con el tutorial", labels)
        self.assertNotIn(employee.id, self.client.session["nyx_onboarding_pending_employee_hours"])

    def test_pending_professional_hours_prevents_tutorial_advance(self):
        self.activate_tutorial(step=3)
        employee = Employee.objects.create(salon=self.salon, name="Lara")
        session = self.client.session
        session["nyx_onboarding_pending_employee_hours"] = [employee.id]
        session.save()

        response = self.client.post(
            reverse("panel_onboarding_decision"),
            {
                "decision": "continue",
                "step": "6",
                "next": reverse("panel_onboarding"),
            },
        )

        self.assertRedirects(
            response,
            reverse("panel_employee_working_hours", args=[employee.id]),
        )
        self.salon.refresh_from_db()
        self.assertEqual(self.salon.onboarding_current_step, 3)

    def test_public_link_step_shows_explicit_actions_without_loop_url(self):
        self.activate_tutorial(step=6)

        response = self.client.get(reverse("panel_onboarding"))

        modal = response.context["onboarding_modal"]
        labels = [action["label"] for action in modal["actions"]]
        self.assertEqual(modal["title"], "Paso 6 · Revisá tu sitio público")
        self.assertIn("Ver sitio público", labels)
        self.assertIn("Copiar link", labels)
        self.assertIn("Ya revisé, continuar", labels)
        continue_action = next(
            action for action in modal["actions"]
            if action["label"] == "Ya revisé, continuar"
        )
        self.assertEqual(continue_action["step"], 7)

    def test_public_link_continue_marks_link_reviewed_and_advances_to_finish(self):
        self.activate_tutorial(step=6)

        response = self.client.post(
            reverse("panel_onboarding_decision"),
            {
                "decision": "continue",
                "step": "7",
                "next": reverse("panel_onboarding"),
            },
        )

        self.salon.refresh_from_db()
        self.assertRedirects(response, reverse("panel_onboarding"))
        self.assertEqual(self.salon.onboarding_current_step, 7)
        self.assertTrue(self.salon.onboarding_link_shared)

    def test_complete_onboarding_from_final_step_marks_completed(self):
        self.activate_tutorial(step=7)

        response = self.client.post(
            reverse("panel_onboarding_complete"),
            {"next": reverse("panel_dashboard")},
        )

        self.salon.refresh_from_db()
        self.assertRedirects(response, reverse("panel_dashboard"))
        self.assertTrue(self.salon.onboarding_completed)
        self.assertFalse(self.client.session.get("nyx_onboarding_active", False))

    def test_completed_onboarding_does_not_show_initial_welcome_modal(self):
        self.salon.onboarding_completed = True
        self.salon.onboarding_dismissed = False
        self.salon.onboarding_current_step = 7
        self.salon.save(update_fields=[
            "onboarding_completed",
            "onboarding_dismissed",
            "onboarding_current_step",
        ])

        response = self.client.get(reverse("panel_dashboard"))

        self.assertFalse(response.context["onboarding_show_prompt"])
        self.assertNotContains(response, "Bienvenida a NYX")


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
