from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from .models import (
    Booking,
    Employee,
    GoogleCalendarIntegration,
    Salon,
    SalonMembership,
    SalonPaymentSettings,
    SalonSubscription,
    Service,
)


User = get_user_model()


def superuser_required(view_func):
    @login_required(login_url="panel_login")
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied("Solo superusers de NYX pueden acceder al panel interno.")

        return view_func(request, *args, **kwargs)

    return wrapper


def public_salon_url(request, salon):
    return request.build_absolute_uri(reverse("service_list", args=[salon.slug]))


def salon_payment_status(salon):
    try:
        payment_settings = salon.payment_settings
    except SalonPaymentSettings.DoesNotExist:
        payment_settings = None
    connected = bool(
        payment_settings
        and payment_settings.mercadopago_connected
        and payment_settings.mercadopago_enabled
        and payment_settings.has_valid_mercadopago_connection()
    )
    return {
        "exists": bool(payment_settings),
        "connected": connected,
        "enabled": bool(payment_settings and payment_settings.mercadopago_enabled),
        "mp_user_id": payment_settings.mp_user_id if payment_settings else "",
        "token_expires_at": payment_settings.mp_token_expires_at if payment_settings else None,
    }


def salon_calendar_status(salon):
    try:
        integration = salon.google_calendar_integration
    except GoogleCalendarIntegration.DoesNotExist:
        integration = None
    connected = bool(integration and integration.is_active and integration.is_connected())
    return {
        "exists": bool(integration),
        "connected": connected,
        "is_active": bool(integration and integration.is_active),
        "calendar_id": integration.calendar_id if integration else "",
        "sync_confirmed_bookings": bool(integration and integration.sync_confirmed_bookings),
        "sync_pending_bookings": bool(integration and integration.sync_pending_bookings),
    }


def active_trial_filter(now=None):
    now = now or timezone.now()
    return Q(status=SalonSubscription.Status.TRIAL, trial_ends_at__gte=now)


@superuser_required
def dashboard(request):
    now = timezone.now()
    seven_days_ago = now - timedelta(days=7)

    recent_salons = Salon.objects.order_by("-created_at")[:6]
    recent_bookings = (
        Booking.objects
        .select_related("salon")
        .order_by("-created_at")[:8]
    )

    context = {
        "total_salons": Salon.objects.count(),
        "total_users": User.objects.count(),
        "total_bookings": Booking.objects.count(),
        "recent_bookings_count": Booking.objects.filter(created_at__gte=seven_days_ago).count(),
        "recent_salons": recent_salons,
        "recent_bookings": recent_bookings,
        "mercadopago_connected_count": SalonPaymentSettings.objects.filter(
            mercadopago_enabled=True,
            mercadopago_connected=True,
            mp_access_token__gt="",
        ).count(),
        "google_calendar_connected_count": GoogleCalendarIntegration.objects.filter(
            is_active=True,
            refresh_token__isnull=False,
        ).exclude(refresh_token="").count(),
        "active_trials_count": SalonSubscription.objects.filter(active_trial_filter(now)).count(),
        "expired_trials_count": SalonSubscription.objects.filter(
            status=SalonSubscription.Status.TRIAL,
            trial_ends_at__lt=now,
        ).count(),
    }
    return render(request, "reservas/internal_admin/dashboard.html", context)


@superuser_required
def salon_list(request):
    salons = (
        Salon.objects
        .prefetch_related("memberships__user")
        .select_related("payment_settings", "google_calendar_integration", "subscription")
        .annotate(
            employees_count=Count("employees", distinct=True),
            services_count=Count("services", distinct=True),
            bookings_count=Count("bookings", distinct=True),
        )
        .order_by("-created_at")
    )

    rows = []
    for salon in salons:
        owner_membership = next(
            (
                membership
                for membership in salon.memberships.all()
                if membership.role == "owner" and membership.is_active
            ),
            None,
        )
        rows.append({
            "salon": salon,
            "owner": owner_membership.user if owner_membership else None,
            "public_url": public_salon_url(request, salon),
            "payment": salon_payment_status(salon),
            "calendar": salon_calendar_status(salon),
        })

    return render(request, "reservas/internal_admin/salon_list.html", {
        "salon_rows": rows,
    })


@superuser_required
def salon_detail(request, salon_id):
    salon = get_object_or_404(
        Salon.objects.select_related(
            "payment_settings",
            "google_calendar_integration",
            "subscription",
        ),
        pk=salon_id,
    )
    memberships = (
        SalonMembership.objects
        .select_related("user")
        .filter(salon=salon)
        .order_by("role", "user__username")
    )
    employees = Employee.objects.filter(salon=salon).select_related("user").order_by("name")
    services = Service.objects.filter(salon=salon).select_related("category").order_by("name")
    recent_bookings = (
        Booking.objects
        .filter(salon=salon)
        .prefetch_related("items__service", "items__employee")
        .order_by("-created_at")[:12]
    )

    return render(request, "reservas/internal_admin/salon_detail.html", {
        "salon": salon,
        "public_url": public_salon_url(request, salon),
        "memberships": memberships,
        "employees": employees,
        "services": services,
        "recent_bookings": recent_bookings,
        "payment": salon_payment_status(salon),
        "calendar": salon_calendar_status(salon),
        "subscription": getattr(salon, "subscription", None),
    })


@superuser_required
def user_list(request):
    query = request.GET.get("q", "").strip()
    users = User.objects.prefetch_related("salon_memberships__salon").order_by("username")

    if query:
        users = users.filter(
            Q(username__icontains=query)
            | Q(email__icontains=query)
        )

    return render(request, "reservas/internal_admin/user_list.html", {
        "users": users[:100],
        "query": query,
    })
