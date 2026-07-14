from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core import mail
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from . import panel_views
from .mail_utils import send_booking_confirmed_email
from .panel_forms import ManualBookingForm, NyxPasswordResetForm, PanelEmployeeAccessForm
from .services.google_calendar import (
    delete_booking_item_from_google_calendar,
    sync_booking_item_to_google_calendar,
)
from .panel_views import _create_manual_booking, _mercadopago_panel_context
from .models import (
    Booking,
    BookingItem,
    BusinessHourBlock,
    CustomerNote,
    Employee,
    EmployeeWorkingHour,
    GoogleCalendarIntegration,
    Salon,
    SalonMembership,
    SalonPaymentSettings,
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
    DEFAULT_FROM_EMAIL="turnos@example.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    TIME_ZONE="America/Argentina/Buenos_Aires",
)
class PublicProfessionalNameTests(TestCase):
    def setUp(self):
        self.salon = Salon.objects.create(
            name="Phoenix Hair Salon",
            slug="phoenix-public-names",
            is_active=True,
        )
        self.service = Service.objects.create(
            salon=self.salon,
            name="Corte",
            price=1000,
            duration_minutes=60,
            is_active=True,
        )
        self.staff_user = User.objects.create_user(
            username="lujanleiva",
            password="pass12345",
            first_name="Luján",
            last_name="Leiva",
        )
        self.employee = Employee.objects.create(
            salon=self.salon,
            user=self.staff_user,
            name="lujanleiva",
            is_active=True,
        )
        self.employee.services.add(self.service)
        BusinessHourBlock.objects.create(
            salon=self.salon,
            weekday=0,
            start_time=time(9, 0),
            end_time=time(18, 0),
            is_active=True,
        )

    def create_confirmed_booking(self):
        booking = Booking.objects.create(
            salon=self.salon,
            customer_name="Cliente",
            customer_phone="3415550000",
            customer_email="cliente@example.com",
            status="confirmed",
            booking_mode="consecutive",
        )
        BookingItem.objects.create(
            booking=booking,
            service=self.service,
            employee=self.employee,
            start_datetime=timezone.make_aware(
                datetime(2026, 7, 6, 10, 0),
                timezone.get_current_timezone(),
            ),
            end_datetime=timezone.make_aware(
                datetime(2026, 7, 6, 11, 0),
                timezone.get_current_timezone(),
            ),
        )
        return booking

    def test_public_site_uses_salon_name_and_professional_public_name(self):
        response = self.client.get(reverse("service_list", args=[self.salon.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Phoenix Hair Salon")
        self.assertContains(response, "Luján Leiva")
        self.assertNotContains(response, "lujanleiva")

    def test_public_hours_group_multiple_blocks_by_weekday(self):
        BusinessHourBlock.objects.create(
            salon=self.salon,
            weekday=1,
            start_time=time(9, 0),
            end_time=time(12, 0),
            is_active=True,
        )
        BusinessHourBlock.objects.create(
            salon=self.salon,
            weekday=1,
            start_time=time(16, 0),
            end_time=time(20, 0),
            is_active=True,
        )

        response = self.client.get(reverse("service_list", args=[self.salon.slug]))
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Martes", count=1)
        self.assertContains(response, "09:00 - 12:00")
        self.assertContains(response, "16:00 - 20:00")
        self.assertLess(
            content.index("09:00 - 12:00"),
            content.index("16:00 - 20:00"),
        )

    def test_public_professional_selector_does_not_show_username(self):
        with patch("builtins.print"):
            response = self.client.get(
                reverse("select_professional"),
                {"services": [str(self.service.id)]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Luján Leiva")
        self.assertNotContains(response, "lujanleiva")

    def test_public_name_falls_back_to_profesional_without_username(self):
        anonymous_user = User.objects.create_user(
            username="staffinterno",
            password="pass12345",
        )
        employee = Employee.objects.create(
            salon=self.salon,
            user=anonymous_user,
            name="staffinterno",
            is_active=True,
        )

        self.assertEqual(employee.public_name, "Profesional")
        self.assertEqual(str(employee), "Profesional")

    def test_confirmation_page_and_customer_email_do_not_show_username(self):
        booking = self.create_confirmed_booking()

        response = self.client.get(reverse("booking_success_booking", args=[booking.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Luján Leiva")
        self.assertNotContains(response, "lujanleiva")

        sent = send_booking_confirmed_email(booking)

        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Luján Leiva", mail.outbox[0].body)
        self.assertNotIn("lujanleiva", mail.outbox[0].body)
        self.assertIn("Luján Leiva", mail.outbox[0].alternatives[0].content)
        self.assertNotIn("lujanleiva", mail.outbox[0].alternatives[0].content)


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


class PanelLoginAndStaffAccessTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.salon = Salon.objects.create(
            name="Lux Salon",
            slug="lux-salon-auth",
            is_active=True,
        )
        SalonSubscription.objects.create(
            salon=self.salon,
            status=SalonSubscription.Status.TRIAL,
            plan=SalonSubscription.Plan.BASIC,
        )

    def create_staff_user(self, username="manuel", email="manuel@example.com"):
        user = User.objects.create_user(
            username=username,
            email=email,
            password="pass12345",
        )
        SalonMembership.objects.create(
            user=user,
            salon=self.salon,
            role="staff",
            is_active=True,
        )
        Employee.objects.create(
            salon=self.salon,
            user=user,
            name=username.title(),
            email=email,
        )
        return user

    def test_staff_with_active_membership_logs_in_to_allowed_panel(self):
        self.create_staff_user()

        response = self.client.post(reverse("panel_login"), {
            "username": "manuel",
            "password": "pass12345",
        })

        self.assertRedirects(
            response,
            reverse("panel_agenda"),
            fetch_redirect_response=False,
        )

    def test_authenticated_login_redirects_staff_without_403(self):
        self.create_staff_user()
        self.client.login(username="manuel", password="pass12345")

        response = self.client.get(reverse("panel_login"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("panel_agenda"))

    def test_authenticated_login_logs_out_user_without_active_membership(self):
        User.objects.create_user(
            username="sin_salon",
            email="sin-salon@example.com",
            password="pass12345",
        )
        self.client.login(username="sin_salon", password="pass12345")

        response = self.client.get(reverse("panel_login"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reservas/panel/login.html")
        self.assertFalse(response.wsgi_request.user.is_authenticated)
        self.assertContains(
            response,
            "Tu usuario no tiene una membresía activa en ninguna peluquería.",
        )

    def test_staff_access_form_rejects_email_used_by_another_user(self):
        User.objects.create_user(
            username="nux",
            email="milicentral2004@gmail.com",
            password="pass12345",
        )

        form = PanelEmployeeAccessForm(data={
            "username": "manuel",
            "email": "milicentral2004@gmail.com",
        })

        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)
        self.assertIn("ya pertenece a otro usuario", form.errors["email"][0])


@override_settings(
    DEFAULT_FROM_EMAIL="turnos@example.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class PanelPasswordResetTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.salon = Salon.objects.create(
            name="Lux Salon",
            slug="lux-salon-reset",
            is_active=True,
        )

    def add_membership(self, user, role="staff"):
        SalonMembership.objects.create(
            user=user,
            salon=self.salon,
            role=role,
            is_active=True,
        )

    def test_password_reset_confirm_changes_user_that_owns_email(self):
        manuel = User.objects.create_user(
            username="Manuel",
            email="",
            password="oldpass12345",
        )
        nux = User.objects.create_user(
            username="nux",
            email="milicentral2004@gmail.com",
            password="oldpass12345",
        )
        self.add_membership(manuel)
        self.add_membership(nux)

        uid = urlsafe_base64_encode(force_bytes(nux.pk))
        token = default_token_generator.make_token(nux)
        response = self.client.get(
            reverse("password_reset_confirm", kwargs={
                "uidb64": uid,
                "token": token,
            })
        )
        self.assertEqual(response.status_code, 302)

        response = self.client.post(
            response["Location"],
            {
                "new_password1": "newpass12345",
                "new_password2": "newpass12345",
            },
        )

        self.assertEqual(response.status_code, 302)
        manuel.refresh_from_db()
        nux.refresh_from_db()
        self.assertTrue(nux.check_password("newpass12345"))
        self.assertTrue(manuel.check_password("oldpass12345"))

    def test_password_reset_form_ignores_users_without_email(self):
        manuel = User.objects.create_user(
            username="Manuel",
            email="",
            password="oldpass12345",
        )
        self.add_membership(manuel)

        form = NyxPasswordResetForm()

        self.assertEqual(list(form.get_users("")), [])

    def test_password_reset_form_does_not_choose_between_duplicate_emails(self):
        first = User.objects.create_user(
            username="staff1",
            email="duplicado@example.com",
            password="pass12345",
        )
        second = User.objects.create_user(
            username="staff2",
            email="duplicado@example.com",
            password="pass12345",
        )
        self.add_membership(first)
        self.add_membership(second)

        form = NyxPasswordResetForm()

        self.assertEqual(list(form.get_users("duplicado@example.com")), [])


class InternalAdminReadOnlyTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.superuser = User.objects.create_superuser(
            username="adminnyx",
            email="admin@nyx.test",
            password="pass12345",
        )
        self.owner = User.objects.create_user(
            username="owner-interno",
            email="owner-interno@example.com",
            password="pass12345",
        )
        self.staff_user = User.objects.create_user(
            username="staff-interno",
            email="staff-interno@example.com",
            password="pass12345",
            is_staff=True,
        )
        self.salon = Salon.objects.create(
            name="Salon Interno",
            slug="salon-interno",
            is_active=True,
        )
        SalonMembership.objects.create(
            user=self.owner,
            salon=self.salon,
            role="owner",
            is_active=True,
        )
        SalonMembership.objects.create(
            user=self.staff_user,
            salon=self.salon,
            role="staff",
            is_active=True,
        )
        SalonSubscription.objects.create(
            salon=self.salon,
            status=SalonSubscription.Status.TRIAL,
            plan=SalonSubscription.Plan.BASIC,
        )
        self.employee = Employee.objects.create(
            salon=self.salon,
            user=self.staff_user,
            name="Staff Publico",
            is_active=True,
        )
        self.service = Service.objects.create(
            salon=self.salon,
            name="Corte",
            price=1000,
            duration_minutes=30,
            is_active=True,
        )
        self.booking = Booking.objects.create(
            salon=self.salon,
            customer_name="Cliente Interno",
            customer_phone="3415550000",
            customer_email="cliente@example.com",
            status="confirmed",
            booking_mode="consecutive",
        )
        BookingItem.objects.create(
            booking=self.booking,
            service=self.service,
            employee=self.employee,
            start_datetime=timezone.make_aware(
                datetime(2026, 7, 6, 10, 0),
                timezone.get_current_timezone(),
            ),
            end_datetime=timezone.make_aware(
                datetime(2026, 7, 6, 10, 30),
                timezone.get_current_timezone(),
            ),
        )
        SalonPaymentSettings.objects.create(
            salon=self.salon,
            mercadopago_enabled=True,
            mercadopago_connected=True,
            mp_user_id="mp-user-1",
            mp_access_token="SECRET_MP_ACCESS_TOKEN",
            mp_refresh_token="SECRET_MP_REFRESH_TOKEN",
        )
        GoogleCalendarIntegration.objects.create(
            salon=self.salon,
            calendar_id="primary",
            access_token="SECRET_GOOGLE_ACCESS_TOKEN",
            refresh_token="SECRET_GOOGLE_REFRESH_TOKEN",
            is_active=True,
        )

    def test_superuser_can_access_internal_admin_dashboard(self):
        self.client.login(username="adminnyx", password="pass12345")

        response = self.client.get(reverse("internal_admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Admin NYX")
        self.assertContains(response, "Salon Interno")

    def test_owner_cannot_access_internal_admin(self):
        self.client.login(username="owner-interno", password="pass12345")

        response = self.client.get(reverse("internal_admin_dashboard"))

        self.assertEqual(response.status_code, 403)

    def test_staff_cannot_access_internal_admin(self):
        self.client.login(username="staff-interno", password="pass12345")

        response = self.client.get(reverse("internal_admin_dashboard"))

        self.assertEqual(response.status_code, 403)

    def test_anonymous_user_redirects_to_login(self):
        response = self.client.get(reverse("internal_admin_dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("panel_login"), response["Location"])

    def test_internal_admin_does_not_expose_tokens(self):
        self.client.login(username="adminnyx", password="pass12345")

        response = self.client.get(
            reverse("internal_admin_salon_detail", args=[self.salon.id])
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn("SECRET_MP_ACCESS_TOKEN", content)
        self.assertNotIn("SECRET_MP_REFRESH_TOKEN", content)
        self.assertNotIn("SECRET_GOOGLE_ACCESS_TOKEN", content)
        self.assertNotIn("SECRET_GOOGLE_REFRESH_TOKEN", content)
        self.assertNotIn("access_token", content)
        self.assertNotIn("refresh_token", content)

    def test_salon_detail_shows_memberships_and_recent_bookings(self):
        self.client.login(username="adminnyx", password="pass12345")

        response = self.client.get(
            reverse("internal_admin_salon_detail", args=[self.salon.id])
        )

        self.assertContains(response, "owner-interno")
        self.assertContains(response, "staff-interno")
        self.assertContains(response, "Cliente Interno")
        self.assertContains(response, "primary")
        self.assertContains(response, "mp-user-1")

    def test_user_list_searches_by_username_and_email(self):
        self.client.login(username="adminnyx", password="pass12345")

        username_response = self.client.get(
            reverse("internal_admin_user_list"),
            {"q": "staff-interno"},
        )
        email_response = self.client.get(
            reverse("internal_admin_user_list"),
            {"q": "owner-interno@example.com"},
        )

        self.assertContains(username_response, "staff-interno@example.com")
        self.assertContains(username_response, "Salon Interno")
        self.assertContains(email_response, "owner-interno")
        self.assertContains(email_response, "Salon Interno")


@override_settings(
    DEFAULT_FROM_EMAIL="turnos@example.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    TIME_ZONE="America/Argentina/Buenos_Aires",
)
class PanelCustomersTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(
            username="owner-clientes",
            email="owner-clientes@example.com",
            password="pass12345",
        )
        self.staff_user = User.objects.create_user(
            username="staff-clientes",
            email="staff-clientes@example.com",
            password="pass12345",
        )
        self.other_owner = User.objects.create_user(
            username="owner-otro-clientes",
            email="owner-otro-clientes@example.com",
            password="pass12345",
        )
        self.no_membership_user = User.objects.create_user(
            username="sin-membership",
            email="sin-membership@example.com",
            password="pass12345",
        )
        self.salon = Salon.objects.create(
            name="Salon Clientes",
            slug="salon-clientes",
            is_active=True,
        )
        self.other_salon = Salon.objects.create(
            name="Otro Salon Clientes",
            slug="otro-salon-clientes",
            is_active=True,
        )
        SalonMembership.objects.create(
            user=self.owner,
            salon=self.salon,
            role="owner",
            is_active=True,
        )
        SalonMembership.objects.create(
            user=self.staff_user,
            salon=self.salon,
            role="staff",
            is_active=True,
        )
        SalonMembership.objects.create(
            user=self.other_owner,
            salon=self.other_salon,
            role="owner",
            is_active=True,
        )
        SalonSubscription.objects.create(
            salon=self.salon,
            status=SalonSubscription.Status.TRIAL,
            plan=SalonSubscription.Plan.BASIC,
        )
        SalonSubscription.objects.create(
            salon=self.other_salon,
            status=SalonSubscription.Status.TRIAL,
            plan=SalonSubscription.Plan.BASIC,
        )
        self.service = Service.objects.create(
            salon=self.salon,
            name="Corte",
            price=1000,
            duration_minutes=30,
            is_active=True,
        )
        self.color_service = Service.objects.create(
            salon=self.salon,
            name="Color",
            price=3000,
            duration_minutes=60,
            is_active=True,
        )
        self.other_service = Service.objects.create(
            salon=self.other_salon,
            name="Peinado",
            price=2000,
            duration_minutes=45,
            is_active=True,
        )
        self.employee = Employee.objects.create(
            salon=self.salon,
            user=self.staff_user,
            name="Ana Profesional",
            is_active=True,
        )
        self.other_employee = Employee.objects.create(
            salon=self.other_salon,
            name="Otra Profesional",
            is_active=True,
        )
        self.booking = self.create_booking(
            salon=self.salon,
            service=self.service,
            employee=self.employee,
            name="Maria Perez",
            phone="341 555-0000",
            email="maria@example.com",
            status="confirmed",
            start=timezone.now() + timedelta(days=4),
            selected_payment_method="transfer",
            payment_choice="deposit",
            payment_required_amount=500,
        )
        self.create_booking(
            salon=self.salon,
            service=self.color_service,
            employee=self.employee,
            name="Maria Perez Completa",
            phone="+54 9 341 555 0000",
            email=" MARIA@example.com ",
            status="completed",
            start=timezone.now() - timedelta(days=10),
        )
        self.other_booking = self.create_booking(
            salon=self.other_salon,
            service=self.other_service,
            employee=self.other_employee,
            name="Cliente Otro Salon",
            phone="3419990000",
            email="otro@example.com",
            status="confirmed",
            start=timezone.now() + timedelta(days=5),
        )

    def create_booking(
        self,
        salon,
        service,
        employee,
        name,
        phone,
        email,
        status,
        start,
        selected_payment_method="",
        payment_choice="none",
        payment_required_amount=0,
    ):
        booking = Booking.objects.create(
            salon=salon,
            customer_name=name,
            customer_phone=phone,
            customer_email=email,
            status=status,
            booking_mode="consecutive",
            selected_payment_method=selected_payment_method,
            payment_choice=payment_choice,
            payment_required_amount=payment_required_amount,
        )
        BookingItem.objects.create(
            booking=booking,
            service=service,
            employee=employee,
            start_datetime=start,
            end_datetime=start + timedelta(minutes=service.duration_minutes),
        )
        return booking

    def login_owner(self):
        self.client.login(username="owner-clientes", password="pass12345")

    def test_owner_sees_customers_from_own_salon(self):
        self.login_owner()

        response = self.client.get(reverse("panel_customers"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Maria Perez Completa")
        self.assertNotContains(response, "Cliente Otro Salon")

    def test_active_staff_sees_customers_from_own_salon(self):
        self.client.login(username="staff-clientes", password="pass12345")

        response = self.client.get(reverse("panel_customers"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Maria Perez Completa")
        self.assertNotContains(response, "Cliente Otro Salon")

    def test_other_salon_user_does_not_see_foreign_customers(self):
        self.client.login(username="owner-otro-clientes", password="pass12345")

        response = self.client.get(reverse("panel_customers"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cliente Otro Salon")
        self.assertNotContains(response, "Maria Perez Completa")

    def test_anonymous_user_cannot_access_customers(self):
        response = self.client.get(reverse("panel_customers"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("panel_login"), response["Location"])

    def test_user_without_active_membership_cannot_access_customers(self):
        self.client.login(username="sin-membership", password="pass12345")

        response = self.client.get(reverse("panel_customers"))

        self.assertEqual(response.status_code, 403)

    def test_same_email_with_different_names_is_grouped(self):
        self.login_owner()

        response = self.client.get(reverse("panel_customers"))

        customers = response.context["customers"]
        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0]["total_bookings"], 2)
        self.assertEqual(customers[0]["key"], "email-maria@example.com")

    def test_grouped_customer_uses_best_available_display_name(self):
        self.login_owner()

        response = self.client.get(reverse("panel_customers"))

        self.assertEqual(response.context["customers"][0]["name"], "Maria Perez Completa")
        self.assertContains(response, "Maria Perez Completa")

    def test_email_with_spaces_and_uppercase_is_grouped(self):
        self.login_owner()

        response = self.client.get(reverse("panel_customers"))

        customer = response.context["customers"][0]
        self.assertEqual(customer["email"], "maria@example.com")
        self.assertEqual(customer["total_bookings"], 2)

    def test_without_email_groups_by_normalized_phone(self):
        first = self.create_booking(
            salon=self.salon,
            service=self.service,
            employee=self.employee,
            name="Cliente Sin Email",
            phone="(341) 222-3333",
            email="",
            status="confirmed",
            start=timezone.now() + timedelta(days=8),
        )
        self.create_booking(
            salon=self.salon,
            service=self.color_service,
            employee=self.employee,
            name="Cliente Sin Email Bis",
            phone="341 222 3333",
            email="",
            status="completed",
            start=timezone.now() - timedelta(days=8),
        )
        self.login_owner()

        response = self.client.get(
            reverse("panel_customer_detail", args=[panel_views._customer_key_for_booking(first)]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cliente Sin Email")
        self.assertContains(response, "Cliente Sin Email Bis")

    def test_search_by_name_phone_and_email(self):
        self.login_owner()

        name_response = self.client.get(reverse("panel_customers"), {"q": "maria"})
        old_name_response = self.client.get(reverse("panel_customers"), {"q": "Perez"})
        new_name_response = self.client.get(reverse("panel_customers"), {"q": "Completa"})
        phone_response = self.client.get(reverse("panel_customers"), {"q": "5550000"})
        email_response = self.client.get(reverse("panel_customers"), {"q": "maria@example.com"})

        self.assertEqual(len(name_response.context["customers"]), 1)
        self.assertEqual(len(old_name_response.context["customers"]), 1)
        self.assertEqual(len(new_name_response.context["customers"]), 1)
        self.assertEqual(len(phone_response.context["customers"]), 1)
        self.assertEqual(len(email_response.context["customers"]), 1)

    def test_customer_detail_shows_history(self):
        self.login_owner()
        customer_key = panel_views._customer_key_for_booking(self.booking)

        response = self.client.get(reverse("panel_customer_detail", args=[customer_key]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Historial de turnos")
        self.assertContains(response, "Corte")
        self.assertContains(response, "Color")
        self.assertEqual(len(response.context["history_entries"]), 2)
        self.assertContains(response, "Ana Profesional")
        self.assertContains(response, "Transferencia")

    def test_internal_notes_can_be_created_and_seen_in_panel(self):
        self.login_owner()
        customer_key = panel_views._customer_key_for_booking(self.booking)
        legacy_phone_key = f"phone-{panel_views._normalize_customer_phone(self.booking.customer_phone)}"
        CustomerNote.objects.create(
            salon=self.salon,
            customer_key=legacy_phone_key,
            author=self.owner,
            note="Nota previa por telefono",
        )

        response = self.client.post(
            reverse("panel_customer_detail", args=[customer_key]),
            {"note": "Prefiere turno por la manana"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nota previa por telefono")
        self.assertContains(response, "Prefiere turno por la manana")
        self.assertTrue(
            CustomerNote.objects.filter(
                salon=self.salon,
                customer_key=customer_key,
                note="Prefiere turno por la manana",
            ).exists()
        )

    def test_internal_notes_do_not_appear_on_public_site_or_customer_email(self):
        customer_key = panel_views._customer_key_for_booking(self.booking)
        CustomerNote.objects.create(
            salon=self.salon,
            customer_key=customer_key,
            author=self.owner,
            note="Usa tintura X",
        )

        public_response = self.client.get(reverse("service_list", args=[self.salon.slug]))
        sent = send_booking_confirmed_email(self.booking)

        self.assertEqual(public_response.status_code, 200)
        self.assertNotContains(public_response, "Usa tintura X")
        self.assertTrue(sent)
        self.assertNotIn("Usa tintura X", mail.outbox[0].body)
        self.assertNotIn("Usa tintura X", mail.outbox[0].alternatives[0].content)

    def test_notes_and_customers_do_not_mix_between_salons(self):
        self.login_owner()
        own_key = panel_views._customer_key_for_booking(self.booking)
        other_key = panel_views._customer_key_for_booking(self.other_booking)
        CustomerNote.objects.create(
            salon=self.other_salon,
            customer_key=other_key,
            author=self.other_owner,
            note="Nota de otro salon",
        )

        list_response = self.client.get(reverse("panel_customers"))
        detail_response = self.client.get(reverse("panel_customer_detail", args=[own_key]))
        forbidden_response = self.client.get(reverse("panel_customer_detail", args=[other_key]))

        self.assertNotContains(list_response, "Cliente Otro Salon")
        self.assertNotContains(detail_response, "Nota de otro salon")
        self.assertEqual(forbidden_response.status_code, 403)


@override_settings(
    DEFAULT_FROM_EMAIL="turnos@example.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    TIME_ZONE="America/Argentina/Buenos_Aires",
)
class PanelMetricsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(
            username="owner-metricas",
            email="owner-metricas@example.com",
            password="pass12345",
        )
        self.staff_user = User.objects.create_user(
            username="staff-metricas",
            email="staff-metricas@example.com",
            password="pass12345",
        )
        self.other_owner = User.objects.create_user(
            username="owner-otras-metricas",
            email="owner-otras-metricas@example.com",
            password="pass12345",
        )
        self.salon = Salon.objects.create(
            name="Salon Metricas",
            slug="salon-metricas",
            is_active=True,
        )
        self.other_salon = Salon.objects.create(
            name="Otro Salon Metricas",
            slug="otro-salon-metricas",
            is_active=True,
        )
        SalonMembership.objects.create(
            user=self.owner,
            salon=self.salon,
            role="owner",
            is_active=True,
        )
        SalonMembership.objects.create(
            user=self.staff_user,
            salon=self.salon,
            role="staff",
            is_active=True,
        )
        SalonMembership.objects.create(
            user=self.other_owner,
            salon=self.other_salon,
            role="owner",
            is_active=True,
        )
        SalonSubscription.objects.create(
            salon=self.salon,
            status=SalonSubscription.Status.TRIAL,
            plan=SalonSubscription.Plan.BASIC,
        )
        SalonSubscription.objects.create(
            salon=self.other_salon,
            status=SalonSubscription.Status.TRIAL,
            plan=SalonSubscription.Plan.BASIC,
        )
        self.corte = Service.objects.create(
            salon=self.salon,
            name="Corte",
            price=1000,
            duration_minutes=30,
            is_active=True,
        )
        self.color = Service.objects.create(
            salon=self.salon,
            name="Color",
            price=2500,
            duration_minutes=60,
            is_active=True,
        )
        self.other_service = Service.objects.create(
            salon=self.other_salon,
            name="Servicio Ajeno",
            price=3000,
            duration_minutes=45,
            is_active=True,
        )
        self.visible_user = User.objects.create_user(
            username="usuariointerno",
            password="pass12345",
            first_name="Lara",
            last_name="Visible",
        )
        self.employee = Employee.objects.create(
            salon=self.salon,
            user=self.visible_user,
            name="usuariointerno",
            is_active=True,
        )
        self.other_employee = Employee.objects.create(
            salon=self.salon,
            name="Ana Profesional",
            is_active=True,
        )
        self.foreign_employee = Employee.objects.create(
            salon=self.other_salon,
            name="Profesional Ajeno",
            is_active=True,
        )
        self.create_booking(
            salon=self.salon,
            service=self.corte,
            employee=self.employee,
            name="Mili",
            phone="3415550000",
            email=" MiliCentral2004@gmail.com ",
            status="confirmed",
            days_offset=0,
        )
        self.create_booking(
            salon=self.salon,
            service=self.color,
            employee=self.employee,
            name="mili central",
            phone="341 555 0000",
            email="milicentral2004@gmail.com",
            status="completed",
            days_offset=-1,
        )
        self.create_booking(
            salon=self.salon,
            service=self.corte,
            employee=self.other_employee,
            name="Ana",
            phone="(341) 222-3333",
            email="",
            status="cancelled",
            days_offset=-2,
        )
        self.create_booking(
            salon=self.salon,
            service=self.corte,
            employee=self.other_employee,
            name="Carla",
            phone="3414445555",
            email="",
            status="completed",
            days_offset=-10,
        )
        self.create_booking(
            salon=self.other_salon,
            service=self.other_service,
            employee=self.foreign_employee,
            name="Mili Ajena",
            phone="3419990000",
            email="milicentral2004@gmail.com",
            status="confirmed",
            days_offset=0,
        )

    def metric_datetime(self, days_offset):
        selected_date = timezone.localdate() + timedelta(days=days_offset)
        selected_time = time(23, 45) if days_offset == 0 else time(12, 0)
        return timezone.make_aware(
            datetime.combine(selected_date, selected_time),
            timezone.get_current_timezone(),
        )

    def create_booking(
        self,
        salon,
        service,
        employee,
        name,
        phone,
        email,
        status,
        days_offset,
    ):
        booking = Booking.objects.create(
            salon=salon,
            customer_name=name,
            customer_phone=phone,
            customer_email=email,
            status=status,
            booking_mode="consecutive",
        )
        start_datetime = self.metric_datetime(days_offset)
        BookingItem.objects.create(
            booking=booking,
            service=service,
            employee=employee,
            start_datetime=start_datetime,
            end_datetime=start_datetime + timedelta(minutes=service.duration_minutes),
        )
        return booking

    def login_owner(self):
        self.client.login(username="owner-metricas", password="pass12345")

    def get_metrics(self, params=None):
        self.login_owner()
        return self.client.get(reverse("panel_metrics"), params or {})

    def test_owner_sees_metrics_from_own_salon(self):
        response = self.get_metrics({"period": "last_7"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Metricas")
        self.assertContains(response, "Corte")
        self.assertNotContains(response, "Servicio Ajeno")

    def test_active_staff_sees_metrics_from_own_salon(self):
        self.client.login(username="staff-metricas", password="pass12345")

        response = self.client.get(reverse("panel_metrics"), {"period": "last_7"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Corte")
        self.assertNotContains(response, "Servicio Ajeno")

    def test_other_salon_user_does_not_see_foreign_metrics(self):
        self.client.login(username="owner-otras-metricas", password="pass12345")

        response = self.client.get(reverse("panel_metrics"), {"period": "last_7"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Servicio Ajeno")
        self.assertNotContains(response, "Ana Profesional")

    def test_anonymous_user_cannot_access_metrics(self):
        response = self.client.get(reverse("panel_metrics"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("panel_login"), response["Location"])

    def test_total_statuses_and_cancellation_rate_are_calculated(self):
        response = self.get_metrics({"period": "last_7"})
        metrics = response.context["metrics"]

        self.assertEqual(metrics["total_turns"], 3)
        self.assertEqual(metrics["confirmed_count"], 1)
        self.assertEqual(metrics["completed_count"], 1)
        self.assertEqual(metrics["cancelled_count"], 1)
        self.assertEqual(metrics["cancellation_rate"], 33.3)

    def test_unique_customers_use_email_and_phone_grouping(self):
        response = self.get_metrics({"period": "last_7"})
        metrics = response.context["metrics"]

        self.assertEqual(metrics["unique_customers_count"], 2)
        self.assertEqual(metrics["new_customers_count"], 2)
        self.assertEqual(metrics["recurrent_customers_count"], 1)

    def test_service_ranking_is_ordered(self):
        response = self.get_metrics({"period": "last_7"})
        ranking = response.context["metrics"]["service_ranking"]

        self.assertEqual(ranking[0]["name"], "Corte")
        self.assertEqual(ranking[0]["count"], 2)
        self.assertEqual(ranking[1]["name"], "Color")
        self.assertEqual(ranking[1]["count"], 1)

    def test_employee_ranking_uses_public_name_not_username(self):
        response = self.get_metrics({"period": "last_7"})
        ranking = response.context["metrics"]["employee_ranking"]
        ranking_names = [row["name"] for row in ranking]

        self.assertIn("Lara Visible", ranking_names)
        self.assertNotIn("usuariointerno", response.content.decode())

    def test_last_7_and_last_30_filters_work(self):
        last_7 = self.get_metrics({"period": "last_7"})
        last_30 = self.get_metrics({"period": "last_30"})

        self.assertEqual(last_7.context["metrics"]["total_turns"], 3)
        self.assertEqual(last_30.context["metrics"]["total_turns"], 4)


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

    def test_normal_panel_pages_do_not_show_fixed_next_step_cards(self):
        category = ServiceCategory.objects.create(salon=self.salon, name="Color")
        Service.objects.create(
            salon=self.salon,
            category=category,
            name="Corte",
            price=1000,
            duration_minutes=30,
            is_active=True,
        )
        Employee.objects.create(salon=self.salon, name="Lara", is_active=True)
        BusinessHourBlock.objects.create(
            salon=self.salon,
            weekday=0,
            start_time=time(9, 0),
            end_time=time(17, 0),
            is_active=True,
        )

        for url_name in [
            "panel_service_categories",
            "panel_services",
            "panel_employees",
            "panel_business_hours",
        ]:
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))

                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context["show_next_step"])
                self.assertNotContains(response, "Siguiente paso")


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


class ManualBookingGoogleCalendarTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(
            username="owner-calendar",
            email="owner-calendar@example.com",
            password="pass12345",
        )
        self.salon = Salon.objects.create(
            name="Phoenix Hair Salon",
            slug="phoenix-calendar",
            is_active=True,
        )
        SalonMembership.objects.create(
            user=self.owner,
            salon=self.salon,
            role="owner",
            is_active=True,
        )
        SalonSubscription.objects.create(
            salon=self.salon,
            status=SalonSubscription.Status.ACTIVE,
            plan=SalonSubscription.Plan.BASIC,
        )
        self.employee = Employee.objects.create(
            salon=self.salon,
            name="Lara",
            is_active=True,
        )
        self.service = Service.objects.create(
            salon=self.salon,
            name="Corte",
            price=1000,
            duration_minutes=60,
            is_active=True,
        )
        self.employee.services.add(self.service)
        BusinessHourBlock.objects.create(
            salon=self.salon,
            weekday=0,
            start_time=time(9, 0),
            end_time=time(18, 0),
            is_active=True,
        )
        self.client.login(username="owner-calendar", password="pass12345")
        days_until_monday = (7 - timezone.localdate().weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        appointment_date = timezone.localdate() + timedelta(days=days_until_monday)
        self.post_data = {
            "customer_name": "Ana",
            "customer_phone": "3415550000",
            "customer_email": "",
            "employee": str(self.employee.id),
            "service": str(self.service.id),
            "appointment_date": appointment_date.isoformat(),
            "appointment_time": "10:00",
            "notes": "Cliente habitual",
        }

    def connect_google_calendar(self):
        return GoogleCalendarIntegration.objects.create(
            salon=self.salon,
            calendar_id="primary",
            access_token="access-token",
            refresh_token="refresh-token",
            is_active=True,
            sync_confirmed_bookings=True,
        )

    def google_service_mock(self, event_id="google-event-1"):
        calendar_service = Mock()
        calendar_service.events.return_value.insert.return_value.execute.return_value = {
            "id": event_id,
        }
        credentials = SimpleNamespace(token="access-token", expiry=None)
        return calendar_service, credentials

    def post_manual_booking(self):
        return self.client.post(reverse("panel_manual_booking_create"), self.post_data)

    @patch("reservas.signals.sync_booking_to_google_calendar", return_value=True)
    @patch("reservas.panel_views.get_available_slots", return_value=["10:00"])
    @patch("reservas.panel_forms.get_available_slots", return_value=["10:00"])
    @patch("reservas.services.google_calendar.get_calendar_service")
    def test_confirmed_manual_booking_syncs_to_google_calendar(
        self,
        get_calendar_service,
        _form_slots,
        _view_slots,
        _signal_sync,
    ):
        self.connect_google_calendar()
        calendar_service, credentials = self.google_service_mock()
        get_calendar_service.return_value = (calendar_service, credentials)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.post_manual_booking()

        self.assertEqual(response.status_code, 302)
        booking = Booking.objects.get(customer_name="Ana")
        item = booking.items.get()
        self.assertEqual(booking.status, "confirmed")
        self.assertEqual(item.google_calendar_event_id, "google-event-1")
        self.assertIsNotNone(item.google_calendar_synced_at)
        calendar_service.events.return_value.insert.assert_called_once()

    @patch("reservas.signals.sync_booking_to_google_calendar", return_value=True)
    @patch("reservas.panel_views.logger.warning")
    @patch("reservas.services.google_calendar.logger.exception")
    @patch("reservas.panel_views.get_available_slots", return_value=["10:00"])
    @patch("reservas.panel_forms.get_available_slots", return_value=["10:00"])
    @patch(
        "reservas.services.google_calendar.get_calendar_service",
        side_effect=RuntimeError("Google unavailable"),
    )
    def test_manual_booking_is_created_when_google_calendar_sync_fails(
        self,
        _get_calendar_service,
        _form_slots,
        _view_slots,
        _logger_exception,
        logger_warning,
        _signal_sync,
    ):
        self.connect_google_calendar()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.post_manual_booking()

        self.assertEqual(response.status_code, 302)
        booking = Booking.objects.get(customer_name="Ana")
        item = booking.items.get()
        self.assertEqual(booking.status, "confirmed")
        self.assertIsNone(item.google_calendar_event_id)
        logger_warning.assert_called_once()

    @patch("reservas.signals.sync_booking_to_google_calendar", return_value=True)
    @patch("reservas.panel_views.sync_booking_item_to_google_calendar")
    @patch("reservas.panel_views.get_available_slots", return_value=["10:00"])
    @patch("reservas.panel_forms.get_available_slots", return_value=["10:00"])
    def test_manual_booking_without_google_calendar_connection_does_not_fail(
        self,
        _form_slots,
        _view_slots,
        sync_item,
        _signal_sync,
    ):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post_manual_booking()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Booking.objects.filter(customer_name="Ana").exists())
        sync_item.assert_not_called()

    @patch("reservas.signals.delete_booking_from_google_calendar", return_value=True)
    @patch("reservas.panel_views.delete_booking_item_from_google_calendar", return_value=True)
    def test_cancel_manual_booking_deletes_google_calendar_event(
        self,
        delete_item,
        _signal_delete,
    ):
        booking = Booking.objects.create(
            salon=self.salon,
            customer_name="Ana",
            customer_phone="3415550000",
            booking_mode="consecutive",
            status="confirmed",
            payment_choice="none",
            payment_status="not_required",
            payment_required_amount=0,
            selected_payment_method="none",
        )
        item = BookingItem.objects.create(
            booking=booking,
            service=self.service,
            employee=self.employee,
            start_datetime=timezone.make_aware(
                datetime(2026, 7, 6, 10, 0),
                timezone.get_current_timezone(),
            ),
            end_datetime=timezone.make_aware(
                datetime(2026, 7, 6, 11, 0),
                timezone.get_current_timezone(),
            ),
            google_calendar_event_id="google-event-1",
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("panel_booking_cancel", args=[booking.id]))

        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        delete_item.assert_called_once()
        self.assertEqual(delete_item.call_args.args[0].id, item.id)

    @patch("reservas.panel_views.sync_booking_item_to_google_calendar")
    def test_manual_booking_sync_skips_items_that_already_have_google_event(
        self,
        sync_item,
    ):
        self.connect_google_calendar()
        booking = Booking.objects.create(
            salon=self.salon,
            customer_name="Ana",
            customer_phone="3415550000",
            booking_mode="consecutive",
            status="confirmed",
            payment_choice="none",
            payment_status="not_required",
            payment_required_amount=0,
            selected_payment_method="none",
        )
        BookingItem.objects.create(
            booking=booking,
            service=self.service,
            employee=self.employee,
            start_datetime=timezone.make_aware(
                datetime(2026, 7, 6, 10, 0),
                timezone.get_current_timezone(),
            ),
            end_datetime=timezone.make_aware(
                datetime(2026, 7, 6, 11, 0),
                timezone.get_current_timezone(),
            ),
            google_calendar_event_id="google-event-1",
            google_calendar_synced_at=timezone.now(),
        )

        result = panel_views.sync_manual_booking_to_google_calendar(booking.id)

        self.assertFalse(result)
        sync_item.assert_not_called()


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
