from datetime import timedelta, datetime, timezone as datetime_timezone


from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, render, redirect
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from google_auth_oauthlib.flow import Flow

from reservas.notifications import notify_admin_new_trial_account

from .mail_utils import send_booking_confirmed_email, send_staff_invitation_email, send_booking_cancelled_email, send_booking_payment_pending_email
from .models import (
    BookingItem,
    EmployeeTimeOff,
    Service,
    ServiceCategory,
    Employee,
    EmployeeWorkingHour,
    BusinessHours,
    BusinessHourBlock,
    Salon,
    SalonMembership,
    Booking,
    StaffInvitation,
    SalonPaymentSettings,
    SalonSubscription,
    GoogleCalendarIntegration,
    SpecialAvailabilityBlock,
)
from .panel_forms import (
    PanelBusinessHoursForm,
    PanelBusinessHourBlockForm,
    PanelServiceForm,
    PanelEmployeeForm,
    PanelEmployeeWorkingHourForm,
    EmployeeTimeOffForm,
    SpecialAvailabilityBlockForm,
    ManualBookingForm,
    PanelSalonSettingsForm,
    PanelEmployeeAccessForm,
    AcceptStaffInvitationForm,
    TrialSignupForm,
    PanelServiceCategoryForm,
    
)
from .booking_utils import (
    mark_completed_bookings,
    mark_completed_appointments,
    expire_unpaid_bookings,
)
from .utils import get_available_slots
from .services.google_calendar import GOOGLE_CALENDAR_SCOPES



def get_user_membership(user):
    if not user.is_authenticated or user.is_superuser:
        return None
    return user.salon_memberships.filter(is_active=True).select_related('salon').first()


def get_user_salon(user):
    membership = get_user_membership(user)
    return membership.salon if membership else None

def get_or_create_salon_subscription(salon):
    now = timezone.now()
    trial_days = getattr(settings, "NYX_TRIAL_DAYS", 15)
    monthly_price = getattr(settings, "NYX_BASIC_MONTHLY_PRICE_ARS", 25000)

    subscription, created = SalonSubscription.objects.get_or_create(
        salon=salon,
        defaults={
            "status": SalonSubscription.Status.TRIAL,
            "plan": SalonSubscription.Plan.BASIC,
            "monthly_price_ars": monthly_price,
            "trial_starts_at": now,
            "trial_ends_at": now + timedelta(days=trial_days),
        }
    )

    return subscription


def salon_has_panel_access(salon):
    if not salon or not salon.is_active:
        return False

    subscription = get_or_create_salon_subscription(salon)
    return subscription.has_access()

def subscription_required(view_func):
    def wrapper(request, *args, **kwargs):
        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)

        salon = get_user_salon(request.user)

        if not salon:
            raise PermissionDenied("Tu usuario no está asociado a ninguna peluquería.")

        subscription = get_or_create_salon_subscription(salon)

        if not subscription.has_access():
            return redirect("panel_billing_required")

        request.salon_subscription = subscription

        return view_func(request, *args, **kwargs)

    return wrapper

def is_owner_user(user):
    membership = get_user_membership(user)
    return bool(membership and membership.role == 'owner')


def is_staff_user(user):
    membership = get_user_membership(user)
    return bool(membership and membership.role == 'staff')


def get_panel_entrypoint_for_user(user):
    if user.is_superuser:
        return '/admin/'

    membership = get_user_membership(user)
    if not membership:
        return None

    if membership.role == 'staff':
        return 'panel_agenda'

    return 'panel_dashboard'


def get_user_employee(user):
    return getattr(user, 'employee_profile', None)


def is_onboarding_active(salon, request=None):
    session_active = bool(
        request
        and request.session.get("nyx_onboarding_active")
    )
    return bool(
        salon
        and not salon.onboarding_dismissed
        and not salon.onboarding_completed
        and (session_active or salon.onboarding_current_step > 1)
    )


def should_show_onboarding_welcome(request, salon):
    return bool(
        salon
        and not salon.onboarding_dismissed
        and not salon.onboarding_completed
        and not is_onboarding_active(salon, request)
    )


def get_onboarding_context(request, salon):
    business_data_completed = bool(
        salon.name and (salon.phone or salon.email or salon.address)
    )
    active_services_count = Service.objects.filter(
        salon=salon,
        is_active=True,
    ).count()
    services_count = Service.objects.filter(salon=salon).count()
    employees_count = Employee.objects.filter(
        salon=salon,
        is_active=True,
    ).count()
    business_hours_count = BusinessHourBlock.objects.filter(
        salon=salon,
        is_active=True,
    ).count()
    public_url = request.build_absolute_uri(
        reverse("service_list", kwargs={"salon_slug": salon.slug})
    )
    whatsapp_text = (
        f"Hola, reservá tu turno online en {salon.name}: {public_url}"
    )

    steps = [
        {
            "number": 1,
            "key": "business",
            "icon": "bi-shop-window",
            "title": "Configurá los datos de tu negocio",
            "text": "Agregá la información básica de tu salón para que tus clientes sepan quién sos y cómo encontrarte.",
            "button": "Configurar negocio",
            "url": reverse("panel_settings"),
            "completed": business_data_completed,
            "meta": "Datos básicos cargados" if business_data_completed else "Pendiente",
        },
        {
            "number": 2,
            "key": "services",
            "icon": "bi-scissors",
            "title": "Cargá tus servicios",
            "text": "Agregá los servicios que ofrecés, con su duración y precio.",
            "button": "Agregar servicios",
            "url": reverse("panel_services"),
            "completed": active_services_count > 0,
            "meta": (
                f"{active_services_count} servicio"
                f"{'s' if active_services_count != 1 else ''} activo"
                f"{'s' if active_services_count != 1 else ''}"
            ),
        },
        {
            "number": 3,
            "key": "employees",
            "icon": "bi-people-fill",
            "title": "Agregá profesionales",
            "text": "Cargá las personas que atienden en tu salón y asignales los servicios que realizan.",
            "button": "Agregar profesionales",
            "url": reverse("panel_employees"),
            "completed": employees_count > 0,
            "meta": (
                f"{employees_count} profesional"
                f"{'es' if employees_count != 1 else ''} activo"
                f"{'s' if employees_count != 1 else ''}"
            ),
        },
        {
            "number": 4,
            "key": "hours",
            "icon": "bi-clock-history",
            "title": "Configurá tus horarios",
            "text": "Definí los días y horarios en los que tu salón atiende.",
            "button": "Configurar horarios",
            "url": reverse("panel_business_hours"),
            "completed": business_hours_count > 0,
            "meta": (
                f"{business_hours_count} franja"
                f"{'s' if business_hours_count != 1 else ''} activa"
                f"{'s' if business_hours_count != 1 else ''}"
            ),
        },
        {
            "number": 5,
            "key": "share",
            "icon": "bi-share-fill",
            "title": "Compartí tu link de reservas",
            "text": "Cuando tu configuración esté lista, compartí este link para que tus clientes puedan reservar online.",
            "button": "Compartir link",
            "completed": salon.onboarding_link_shared,
            "meta": "Link compartido" if salon.onboarding_link_shared else "Pendiente",
            "is_share_step": True,
        },
    ]

    completed_steps_count = sum(1 for step in steps if step["completed"])
    all_steps_completed = completed_steps_count == len(steps)
    next_step = next((step for step in steps if not step["completed"]), steps[-1])

    return {
        "services_count": services_count,
        "active_services_count": active_services_count,
        "business_hours_count": business_hours_count,
        "employees_count": employees_count,
        "public_url": public_url,
        "whatsapp_text": whatsapp_text,
        "onboarding_steps": steps,
        "onboarding_next_step": next_step,
        "onboarding_completed_steps_count": completed_steps_count,
        "onboarding_total_steps_count": len(steps),
        "onboarding_progress_percent": int(
            (completed_steps_count / len(steps)) * 100
        ),
        "onboarding_all_steps_completed": all_steps_completed,
        "onboarding_show_prompt": should_show_onboarding_welcome(request, salon),
    }


@login_required
@subscription_required
@require_POST
def panel_onboarding_start(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede gestionar el tutorial.")

    salon.onboarding_dismissed = False
    salon.onboarding_completed = False
    update_fields = [
        "onboarding_dismissed",
        "onboarding_completed",
    ]
    if request.POST.get("reset") == "1":
        salon.onboarding_current_step = 1
        update_fields.append("onboarding_current_step")
    salon.save(update_fields=update_fields)
    request.session["nyx_onboarding_active"] = True
    clear_onboarding_step_event(request)
    request.session.pop(ONBOARDING_PENDING_EMPLOYEE_HOURS_SESSION_KEY, None)
    request.session.pop(ONBOARDING_HOURS_RESOLVED_SESSION_KEY, None)
    return redirect("panel_onboarding")


@login_required
@subscription_required
@require_POST
def panel_onboarding_dismiss(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede gestionar el tutorial.")

    salon.onboarding_dismissed = True
    salon.save(update_fields=["onboarding_dismissed"])
    request.session["nyx_onboarding_active"] = False
    clear_onboarding_step_event(request)
    request.session.pop(ONBOARDING_HOURS_RESOLVED_SESSION_KEY, None)
    messages.info(request, "Podés retomar el tutorial cuando quieras desde el panel.")
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("panel_dashboard")


@login_required
@subscription_required
@require_POST
def panel_onboarding_complete(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede gestionar el tutorial.")

    salon.onboarding_completed = True
    salon.onboarding_dismissed = False
    salon.onboarding_current_step = 7
    salon.save(update_fields=[
        "onboarding_completed",
        "onboarding_dismissed",
        "onboarding_current_step",
    ])
    request.session["nyx_onboarding_active"] = False
    clear_onboarding_step_event(request)
    request.session.pop(ONBOARDING_PENDING_EMPLOYEE_HOURS_SESSION_KEY, None)
    request.session.pop(ONBOARDING_HOURS_RESOLVED_SESSION_KEY, None)
    messages.success(request, "Tutorial finalizado. Tu guía queda disponible en Primeros pasos.")
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("panel_dashboard")


@login_required
@subscription_required
@require_POST
def panel_onboarding_mark_link_shared(request):
    if request.user.is_superuser:
        return JsonResponse({"ok": False}, status=403)

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        return JsonResponse({"ok": False}, status=403)

    salon.onboarding_link_shared = True
    if salon.onboarding_current_step < 7:
        salon.onboarding_current_step = 7
        salon.save(update_fields=["onboarding_link_shared", "onboarding_current_step"])
    else:
        salon.save(update_fields=["onboarding_link_shared"])
    return JsonResponse({"ok": True})


ONBOARDING_EVENT_STEP_SESSION_KEY = "nyx_onboarding_completed_step"
ONBOARDING_EVENT_META_SESSION_KEY = "nyx_onboarding_completed_step_meta"
ONBOARDING_PENDING_EMPLOYEE_HOURS_SESSION_KEY = "nyx_onboarding_pending_employee_hours"
ONBOARDING_HOURS_RESOLVED_SESSION_KEY = "nyx_onboarding_hours_resolved_employee"


def should_track_onboarding_step(request, salon, step_number):
    return (
        is_onboarding_active(salon, request)
        and salon.onboarding_current_step == step_number
    )


def set_onboarding_step_completed(request, salon, step_number, **metadata):
    if not should_track_onboarding_step(request, salon, step_number):
        return

    request.session["nyx_onboarding_active"] = True
    request.session[ONBOARDING_EVENT_STEP_SESSION_KEY] = step_number
    request.session[ONBOARDING_EVENT_META_SESSION_KEY] = metadata


def clear_onboarding_step_event(request):
    request.session.pop(ONBOARDING_EVENT_STEP_SESSION_KEY, None)
    request.session.pop(ONBOARDING_EVENT_META_SESSION_KEY, None)


def get_pending_onboarding_employee_hours(request):
    return [
        int(employee_id)
        for employee_id in request.session.get(
            ONBOARDING_PENDING_EMPLOYEE_HOURS_SESSION_KEY,
            [],
        )
    ]


def add_pending_onboarding_employee_hours(request, employee):
    pending = get_pending_onboarding_employee_hours(request)
    if employee.id not in pending:
        pending.append(employee.id)
        request.session[ONBOARDING_PENDING_EMPLOYEE_HOURS_SESSION_KEY] = pending


def resolve_pending_onboarding_employee_hours(request, employee):
    pending = [
        employee_id
        for employee_id in get_pending_onboarding_employee_hours(request)
        if employee_id != employee.id
    ]
    request.session[ONBOARDING_PENDING_EMPLOYEE_HOURS_SESSION_KEY] = pending
    request.session[ONBOARDING_HOURS_RESOLVED_SESSION_KEY] = employee.id


def mark_onboarding_employee_hours_resolved(request, salon, employee):
    resolve_pending_onboarding_employee_hours(request, employee)
    if (
        is_onboarding_active(salon, request)
        and not salon.onboarding_dismissed
        and not salon.onboarding_completed
        and salon.onboarding_current_step in {3, 5}
    ):
        request.session["nyx_onboarding_active"] = True
        request.session[ONBOARDING_EVENT_STEP_SESSION_KEY] = 5
        request.session[ONBOARDING_EVENT_META_SESSION_KEY] = {
            "employee_id": employee.id,
            "employee_name": employee.name,
        }


def safe_panel_redirect(request, fallback_name):
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect(fallback_name)


@login_required
@subscription_required
@require_POST
def panel_onboarding_decision(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueÃ±a puede gestionar el tutorial.")

    action = request.POST.get("decision")
    try:
        step_number = int(request.POST.get("step", salon.onboarding_current_step))
    except (TypeError, ValueError):
        step_number = salon.onboarding_current_step

    pending_employee_ids = get_pending_onboarding_employee_hours(request)
    if action == "continue" and step_number > 3 and pending_employee_ids:
        pending_employee = Employee.objects.filter(
            salon=salon,
            pk=pending_employee_ids[0],
        ).first()
        if pending_employee:
            clear_onboarding_step_event(request)
            request.session["nyx_onboarding_active"] = True
            return redirect(
                "panel_employee_working_hours",
                employee_id=pending_employee.id,
            )

    if action in {"repeat", "continue"}:
        salon.onboarding_current_step = max(1, min(step_number, 7))
        salon.onboarding_dismissed = False
        salon.onboarding_completed = False
        update_fields = [
            "onboarding_current_step",
            "onboarding_dismissed",
            "onboarding_completed",
        ]
        if action == "continue" and salon.onboarding_current_step >= 7:
            salon.onboarding_link_shared = True
            update_fields.append("onboarding_link_shared")
        salon.save(update_fields=update_fields)
        request.session["nyx_onboarding_active"] = True
        clear_onboarding_step_event(request)
        if action in {"repeat", "continue"}:
            request.session.pop(ONBOARDING_HOURS_RESOLVED_SESSION_KEY, None)
        return safe_panel_redirect(request, "panel_onboarding")

    return redirect("panel_onboarding")


def get_onboarding_context(request, salon):
    categories_count = ServiceCategory.objects.filter(
        salon=salon,
        is_active=True,
    ).count()
    active_services_count = Service.objects.filter(
        salon=salon,
        is_active=True,
    ).count()
    active_categorized_services_count = Service.objects.filter(
        salon=salon,
        is_active=True,
        category__isnull=False,
    ).count()
    employees = Employee.objects.filter(
        salon=salon,
        is_active=True,
    ).order_by("id")
    employees_count = employees.count()
    first_employee = employees.first()
    assigned_services_count = Employee.objects.filter(
        salon=salon,
        is_active=True,
        services__is_active=True,
    ).distinct().count()
    employee_working_hours_count = EmployeeWorkingHour.objects.filter(
        employee__salon=salon,
        is_active=True,
    ).count()
    employee_hours_step_completed = (
        employee_working_hours_count > 0
        or salon.onboarding_current_step > 5
        or salon.onboarding_completed
    )
    public_url = request.build_absolute_uri(
        reverse("service_list", kwargs={"salon_slug": salon.slug})
    )
    whatsapp_text = f"Hola, reservá tu turno online en {salon.name}: {public_url}"

    employee_edit_url = (
        reverse("panel_employee_edit", args=[first_employee.id])
        if first_employee else reverse("panel_employees")
    )
    employee_hours_url = (
        reverse("panel_employee_working_hours", args=[first_employee.id])
        if first_employee else reverse("panel_employees")
    )

    steps = [
        {
            "number": 1,
            "key": "category",
            "icon": "bi-grid-3x3-gap-fill",
            "title": "Paso 1 · Creá tu primera categoría",
            "text": "Las categorías te ayudan a ordenar tus servicios, por ejemplo Peluquería, Color, Uñas o Tratamientos.",
            "button": "Crear categoría",
            "url": reverse("panel_service_category_create"),
            "completed": categories_count > 0,
            "meta": f"{categories_count} categoría{'s' if categories_count != 1 else ''} activa{'s' if categories_count != 1 else ''}",
            "completion_title": "Categoría creada correctamente",
            "completion_text": "Ahora cargá tu primer servicio dentro de esa categoría.",
        },
        {
            "number": 2,
            "key": "service",
            "icon": "bi-scissors",
            "title": "Paso 2 · Cargá tu primer servicio",
            "text": "Agregá un servicio dentro de una categoría, con su duración y precio.",
            "button": "Crear servicio",
            "url": reverse("panel_service_create"),
            "completed": active_categorized_services_count > 0,
            "meta": f"{active_categorized_services_count} servicio{'s' if active_categorized_services_count != 1 else ''} con categoría",
            "completion_title": "Servicio creado correctamente",
            "completion_text": "Ahora agregá un profesional para que pueda atender ese servicio.",
        },
        {
            "number": 3,
            "key": "employee",
            "icon": "bi-person-plus-fill",
            "title": "Paso 3 · Agregá un profesional",
            "text": "Cargá quién realiza los servicios. Podés agregarte a vos misma o sumar a alguien del equipo.",
            "button": "Crear profesional",
            "url": reverse("panel_employee_create"),
            "completed": employees_count > 0,
            "meta": f"{employees_count} profesional{'es' if employees_count != 1 else ''} activo{'s' if employees_count != 1 else ''}",
            "completion_title": "Profesional creado correctamente",
            "completion_text": "Ahora asigná los servicios que realiza este profesional.",
        },
        {
            "number": 4,
            "key": "employee_services",
            "icon": "bi-ui-checks",
            "title": "Paso 4 · Asigná servicios al profesional",
            "text": "Indicá qué servicios realiza cada profesional para que el sistema muestre turnos correctos.",
            "button": "Asignar servicios",
            "url": employee_edit_url,
            "completed": assigned_services_count > 0,
            "meta": f"{assigned_services_count} profesional{'es' if assigned_services_count != 1 else ''} con servicios",
            "completion_title": "Servicios asignados correctamente",
            "completion_text": "Ahora configurá sus horarios de trabajo.",
        },
        {
            "number": 5,
            "key": "employee_hours",
            "icon": "bi-clock-history",
            "title": "Paso 5 · Configurá sus horarios",
            "text": "Definí cuándo trabaja para que NYX no ofrezca turnos fuera de su horario.",
            "button": "Configurar horarios",
            "url": employee_hours_url,
            "completed": employee_hours_step_completed,
            "meta": (
                f"{employee_working_hours_count} franja{'s' if employee_working_hours_count != 1 else ''} activa{'s' if employee_working_hours_count != 1 else ''}"
                if employee_working_hours_count > 0
                else "Usa horarios del salÃ³n" if employee_hours_step_completed else "Pendiente"
            ),
            "completion_title": "Horarios configurados correctamente",
            "completion_text": "Ahora revisá tu sitio público y copiá el link para compartirlo.",
        },
        {
            "number": 6,
            "key": "public_link",
            "icon": "bi-link-45deg",
            "title": "Paso 6 · Revisá tu sitio público",
            "text": "Este es el link que podés compartir con tus clientas para que reserven online.",
            "button": "Revisar link",
            "completed": salon.onboarding_link_shared,
            "meta": "Link revisado" if salon.onboarding_link_shared else "Pendiente",
            "is_share_step": True,
            "completion_title": "Link revisado correctamente",
            "completion_text": "Ya podés finalizar el tutorial.",
        },
        {
            "number": 7,
            "key": "finish",
            "icon": "bi-check2-circle",
            "title": "Tu peluquería ya está lista",
            "text": "Ya podés recibir reservas online. Después podés configurar pagos integrados, Google Calendar y bloqueos especiales.",
            "button": "Finalizar tutorial",
            "url": reverse("panel_onboarding"),
            "completed": salon.onboarding_completed,
            "meta": "Tutorial finalizado" if salon.onboarding_completed else "Pendiente",
            "is_finish_step": True,
        },
    ]

    completed_steps_count = sum(1 for step in steps if step["completed"])
    next_step = next((step for step in steps if not step["completed"]), steps[-1])

    return {
        "categories_count": categories_count,
        "active_services_count": active_services_count,
        "active_categorized_services_count": active_categorized_services_count,
        "employees_count": employees_count,
        "assigned_services_count": assigned_services_count,
        "employee_working_hours_count": employee_working_hours_count,
        "public_url": public_url,
        "whatsapp_text": whatsapp_text,
        "onboarding_steps": steps,
        "onboarding_next_step": next_step,
        "onboarding_current_step": salon.onboarding_current_step,
        "onboarding_completed_steps_count": completed_steps_count,
        "onboarding_total_steps_count": len(steps),
        "onboarding_progress_percent": int(
            (completed_steps_count / len(steps)) * 100
        ),
        "onboarding_all_steps_completed": completed_steps_count == len(steps),
        "onboarding_show_prompt": should_show_onboarding_welcome(request, salon),
    }


def get_onboarding_guidance_context(request, salon):
    context = get_onboarding_context(request, salon)
    context["onboarding_modal"] = None

    if not is_onboarding_active(salon, request):
        return context

    steps = context["onboarding_steps"]
    completed_step_number = request.session.get(ONBOARDING_EVENT_STEP_SESSION_KEY)
    if not completed_step_number:
        current_step = next(
            (
                step for step in steps
                if step["number"] == salon.onboarding_current_step
            ),
            context["onboarding_next_step"],
        )
        if current_step.get("is_finish_step"):
            return context

        if current_step.get("is_share_step"):
            context["onboarding_modal"] = {
                "title": current_step["title"],
                "text": current_step["text"],
                "actions": [
                    {
                        "kind": "link",
                        "label": "Ver sitio p\u00fablico",
                        "url": reverse("service_list", kwargs={"salon_slug": salon.slug}),
                        "target": "_blank",
                        "style": "secondary",
                        "icon": "bi-box-arrow-up-right",
                    },
                    {
                        "kind": "copy",
                        "label": "Copiar link",
                        "public_url": context["public_url"],
                        "style": "secondary",
                    },
                    {
                        "label": "Ya revis\u00e9, continuar",
                        "decision": "continue",
                        "step": 7,
                        "next": reverse("panel_onboarding"),
                        "style": "primary",
                    },
                ],
                "dismiss_url": reverse("panel_onboarding_dismiss"),
                "dismiss_next": request.get_full_path(),
            }
            context["show_next_step"] = False
            context["show_working_hours_decision"] = False
            context["onboarding_current_step"] = salon.onboarding_current_step
            return context

        context["onboarding_modal"] = {
            "title": current_step["title"],
            "text": current_step["text"],
            "actions": [
                {
                    "label": current_step["button"],
                    "decision": "continue",
                    "step": current_step["number"],
                    "next": current_step.get("url") or reverse("panel_onboarding"),
                    "style": "primary",
                },
            ],
            "dismiss_url": reverse("panel_onboarding_dismiss"),
            "dismiss_next": request.get_full_path(),
        }
        context["show_next_step"] = False
        context["show_working_hours_decision"] = False
        context["onboarding_current_step"] = salon.onboarding_current_step
        return context

    completed_step = next(
        (
            step for step in steps
            if step["number"] == completed_step_number
        ),
        None,
    )

    if not completed_step or completed_step.get("is_finish_step"):
        return context

    metadata = request.session.get(ONBOARDING_EVENT_META_SESSION_KEY) or {}
    employee_id = metadata.get("employee_id")
    employee_hours_url = (
        reverse("panel_employee_working_hours", args=[employee_id])
        if employee_id else completed_step.get("url") or reverse("panel_employees")
    )
    employee_name = metadata.get("employee_name") or "este profesional"
    modal_by_step = {
        1: {
            "title": "CategorÃ­a creada correctamente",
            "text": "PodÃ©s cargar mÃ¡s categorÃ­as para ordenar mejor tus servicios, o seguir y cargar tu primer servicio.",
            "actions": [
                {
                    "label": "Agregar otra categorÃ­a",
                    "decision": "repeat",
                    "step": 1,
                    "next": reverse("panel_service_category_create"),
                    "style": "secondary",
                },
                {
                    "label": "Continuar con servicios",
                    "decision": "continue",
                    "step": 2,
                    "next": reverse("panel_service_create"),
                    "style": "primary",
                },
            ],
        },
        2: {
            "title": "Servicio creado correctamente",
            "text": "PodÃ©s cargar otro servicio o seguir con los profesionales que lo realizan.",
            "actions": [
                {
                    "label": "Agregar otro servicio",
                    "decision": "repeat",
                    "step": 2,
                    "next": reverse("panel_service_create"),
                    "style": "secondary",
                },
                {
                    "label": "Continuar con profesionales",
                    "decision": "continue",
                    "step": 3,
                    "next": reverse("panel_employee_create"),
                    "style": "primary",
                },
            ],
        },
        3: {
            "title": "Profesional creado correctamente",
            "text": "PodÃ©s cargar otro profesional o configurar sus horarios de trabajo.",
            "actions": [
                {
                    "label": "Agregar otro profesional",
                    "decision": "repeat",
                    "step": 3,
                    "next": reverse("panel_employee_create"),
                    "style": "secondary",
                },
                {
                    "label": "Configurar horarios",
                    "decision": "continue",
                    "step": 5,
                    "next": employee_hours_url,
                    "style": "primary",
                },
            ],
        },
        5: {
            "title": "Horarios configurados correctamente",
            "text": "Ahora revisÃ¡ tu sitio pÃºblico y copiÃ¡ el link para compartirlo.",
            "actions": [
                {
                    "label": "Continuar",
                    "decision": "continue",
                    "step": 6,
                    "next": reverse("panel_onboarding"),
                    "style": "primary",
                },
            ],
        },
    }
    modal_by_step[1]["title"] = "Categor\u00eda creada correctamente"
    modal_by_step[1]["text"] = (
        "Pod\u00e9s cargar m\u00e1s categor\u00edas para ordenar mejor tus "
        "servicios, o seguir y cargar tu primer servicio."
    )
    modal_by_step[1]["actions"][0]["label"] = "Agregar otra categor\u00eda"
    modal_by_step[2]["text"] = (
        "Pod\u00e9s cargar otro servicio o seguir con los profesionales que lo realizan."
    )
    modal_by_step[3]["text"] = (
        f"Antes de seguir, indic\u00e1 c\u00f3mo trabaja {employee_name} para que NYX sepa cu\u00e1ndo ofrecer turnos."
    )
    modal_by_step[3]["actions"] = [
        {
            "kind": "post",
            "label": "Usar horarios del sal\u00f3n",
            "url": (
                reverse("panel_employee_working_hours_use_salon", args=[employee_id])
                if employee_id else reverse("panel_employees")
            ),
            "style": "primary",
        },
        {
            "kind": "link",
            "label": "Definir horarios personalizados",
            "url": employee_hours_url,
            "style": "secondary",
        },
        {
            "label": "Agregar otro profesional despu\u00e9s",
            "decision": "repeat",
            "step": 3,
            "next": employee_hours_url,
            "style": "secondary",
        },
    ]
    modal_by_step[5]["text"] = (
        "Ahora revis\u00e1 tu sitio p\u00fablico y copi\u00e1 el link para compartirlo."
    )
    modal_by_step[5]["title"] = "Horarios configurados"
    modal_by_step[5]["text"] = "\u00bfQuer\u00e9s agregar otro profesional?"
    modal_by_step[5]["actions"] = [
        {
            "label": "Agregar otro profesional",
            "decision": "repeat",
            "step": 3,
            "next": reverse("panel_employee_create"),
            "style": "secondary",
        },
        {
            "label": "Continuar con el tutorial",
            "decision": "continue",
            "step": 6,
            "next": reverse("panel_onboarding"),
            "style": "primary",
        },
    ]

    modal = modal_by_step.get(completed_step_number)
    if not modal:
        return context

    modal["dismiss_url"] = reverse("panel_onboarding_dismiss")
    modal["dismiss_next"] = request.get_full_path()
    context["onboarding_modal"] = modal
    context["show_next_step"] = False
    context["show_working_hours_decision"] = False
    context["onboarding_current_step"] = salon.onboarding_current_step
    return context



@login_required
@subscription_required
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

    subscription = get_or_create_salon_subscription(salon)

    if not subscription.has_access():
        return redirect("panel_billing_required")

    onboarding_context = {}
    if is_owner_user(request.user):
        onboarding_context = get_onboarding_context(request, salon)

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

    visible_dashboard_statuses = ['pending', 'confirmed']

    visible_booking_items = booking_items.filter(
        booking__status__in=visible_dashboard_statuses
    )

    future_items = visible_booking_items.filter(start_datetime__gte=now)
    today_items = visible_booking_items.filter(start_datetime__date=today)
    tomorrow_items = visible_booking_items.filter(start_datetime__date=tomorrow)

    next_item = future_items.order_by('start_datetime').first()

    today_count = today_items.count()
    tomorrow_count = tomorrow_items.count()
    pending_count = future_items.filter(booking__status='pending').count()
    confirmed_count = future_items.filter(booking__status='confirmed').count()
    time_off_count = time_off_blocks.filter(end_datetime__gte=now).count()

    has_dashboard_activity = any([
        today_count,
        tomorrow_count,
        pending_count,
        confirmed_count,
        time_off_count,
        next_item,
    ])

    context = {
        'panel_role': 'owner' if is_owner_user(request.user) else 'staff',
        'salon': salon,
        'subscription': subscription,
        'today_count': today_count,
        'tomorrow_count': tomorrow_count,
        'pending_count': pending_count,
        'confirmed_count': confirmed_count,
        'time_off_count': time_off_count,
        'next_item': next_item,
        'has_dashboard_activity': has_dashboard_activity,
    }
    context.update(onboarding_context)
    return render(request, 'reservas/panel/dashboard.html', context)

@login_required
@subscription_required
def panel_onboarding(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede ver la bienvenida del salón.")

    context = {
        "panel_role": "owner",
        "salon": salon,
    }
    context.update(get_onboarding_guidance_context(request, salon))
    return render(request, "reservas/panel/onboarding.html", context)

    service_categories_count = ServiceCategory.objects.filter(
        salon=salon,
        is_active=True,
    ).count()

    has_service_categories = service_categories_count > 0
    services_count = Service.objects.filter(salon=salon).count()
    active_services_count = Service.objects.filter(salon=salon, is_active=True).count()

    business_hours_count = BusinessHourBlock.objects.filter(
        salon=salon,
        is_active=True,
    ).count()

    employees_count = Employee.objects.filter(
        salon=salon,
        is_active=True,
    ).count()

    public_url = request.build_absolute_uri(
        reverse("service_list", kwargs={"salon_slug": salon.slug})
    )

    if not has_service_categories:
        next_step = {
            "title": "Creá categorías",
            "description": "Organizá tus servicios en secciones para que tu catálogo sea fácil de recorrer.",
            "url": reverse("panel_service_categories"),
            "button_label": "Ir a categorías",
        }
    elif active_services_count == 0:
        next_step = {
            "title": "Cargá tus servicios",
            "description": "Agregá lo que ofrecés, con precio, duración y categoría.",
            "url": reverse("panel_services"),
            "button_label": "Ir a servicios",
        }
    elif employees_count == 0:
        next_step = {
            "title": "Agregá tu equipo",
            "description": "Cargá las personas que atienden y asignales los servicios que realizan.",
            "url": reverse("panel_employees"),
            "button_label": "Ir a profesionales",
        }
    elif business_hours_count == 0:
        next_step = {
            "title": "Configurá tus horarios",
            "description": "Definí los días y franjas en los que tu negocio recibe reservas.",
            "url": reverse("panel_business_hours"),
            "button_label": "Ir a horarios",
        }
    else:
        next_step = {
            "title": "Compartí tu link de reservas",
            "description": "Tu configuración principal está lista. Compartí el link para empezar a recibir turnos.",
            "is_share_step": True,
        }

    context = {
        "panel_role": "owner",
        "salon": salon,
        "services_count": services_count,
        "service_categories_count": service_categories_count,
        "active_services_count": active_services_count,
        "business_hours_count": business_hours_count,
        "employees_count": employees_count,
        "public_url": public_url,
        "has_services": active_services_count > 0,
        "has_business_hours": business_hours_count > 0,
        "has_employees": employees_count > 0,
        "has_service_categories": has_service_categories,
        "next_step": next_step,
    }

    return render(request, "reservas/panel/onboarding.html", context)

@login_required
@subscription_required
def panel_agenda(request):
    if request.user.is_superuser:
        raise PermissionDenied("El superuser seguí usándolo desde Django admin.")

    expire_unpaid_bookings()
    mark_completed_bookings()
    mark_completed_appointments()

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
        'booking',
        'booking__salon',
        'employee',
        'service',
    )

    if is_owner_user(request.user):
        items = items.filter(booking__salon=salon)
    elif is_staff_user(request.user) and employee:
        items = items.filter(employee=employee)
    else:
        items = items.none()

    visible_agenda_statuses = ['confirmed', 'pending', 'completed']

    items = list(items.filter(
        start_datetime__date=selected_date,
        booking__status__in=visible_agenda_statuses,
    ).order_by('start_datetime'))

    current_tz = timezone.get_current_timezone()
    day_start = timezone.make_aware(
        datetime.combine(selected_date, datetime.min.time()),
        current_tz,
    )
    day_end = day_start + timedelta(days=1)
    previous_date = selected_date - timedelta(days=1)
    next_date = selected_date + timedelta(days=1)

    special_blocks = SpecialAvailabilityBlock.objects.select_related(
        'employee',
    ).filter(
        salon=salon,
        show_in_agenda=True,
        start_datetime__lt=day_end,
        end_datetime__gt=day_start,
    )

    if is_owner_user(request.user):
        pass
    elif is_staff_user(request.user) and employee:
        special_blocks = special_blocks.filter(
            Q(employee__isnull=True) | Q(employee=employee)
        )
    else:
        special_blocks = special_blocks.none()

    agenda_entries = [
        {
            'kind': 'booking',
            'sort_datetime': item.start_datetime,
            'item': item,
            'phone_digits': ''.join(
                character
                for character in (item.booking.customer_phone or '')
                if character.isdigit()
            ),
        }
        for item in items
    ]
    agenda_entries.extend({
        'kind': 'block',
        'sort_datetime': max(block.start_datetime, day_start),
        'block': block,
        'display_start': max(block.start_datetime, day_start),
        'display_end': min(block.end_datetime, day_end),
    } for block in special_blocks)
    agenda_entries.sort(
        key=lambda entry: (
            entry['sort_datetime'],
            0 if entry['kind'] == 'block' else 1,
        )
    )

    context = {
        'panel_role': 'owner' if is_owner_user(request.user) else 'staff',
        'salon': salon,
        'items': items,
        'agenda_entries': agenda_entries,
        'booking_count': len(items),
        'block_count': len(agenda_entries) - len(items),
        'selected_date': selected_date,
        'today': today,
        'tomorrow': tomorrow,
        'previous_date': previous_date,
        'next_date': next_date,
    }

    return render(request, 'reservas/panel/agenda.html', context)


@login_required
@subscription_required
def panel_manual_booking_create(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)
    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede cargar turnos manuales.")

    initial = {}
    selected_date_raw = request.GET.get('date')
    if selected_date_raw:
        try:
            initial['appointment_date'] = datetime.strptime(
                selected_date_raw,
                '%Y-%m-%d',
            ).date()
        except ValueError:
            pass

    if request.method == 'POST':
        form = ManualBookingForm(request.POST, salon=salon)
        if form.is_valid():
            try:
                _create_manual_booking(salon, form.cleaned_data)
            except ValidationError as error:
                form.add_error(
                    None,
                    error.messages[0] if error.messages else (
                        'Ese horario ya no está disponible. Elegí otro horario.'
                    ),
                )
            else:
                messages.success(
                    request,
                    'Turno manual cargado correctamente.',
                )
                agenda_url = reverse('panel_agenda')
                selected_date = form.cleaned_data['appointment_date']
                return redirect(
                    f'{agenda_url}?date={selected_date.isoformat()}'
                )
    else:
        form = ManualBookingForm(salon=salon, initial=initial)

    return render(request, 'reservas/panel/manual_booking_form.html', {
        'panel_role': 'owner',
        'salon': salon,
        'form': form,
    })


def _create_manual_booking(salon, cleaned_data):
    with transaction.atomic():
        try:
            employee = Employee.objects.select_for_update().get(
                pk=cleaned_data['employee'].pk,
                salon=salon,
                is_active=True,
            )
            service = Service.objects.get(
                pk=cleaned_data['service'].pk,
                salon=salon,
                is_active=True,
                employees=employee,
            )
        except (Employee.DoesNotExist, Service.DoesNotExist):
            raise ValidationError(
                'El profesional o el servicio ya no están disponibles.'
            )

        available_slots = get_available_slots(
            employee,
            [service],
            cleaned_data['appointment_date'],
        )
        selected_slot = cleaned_data['appointment_time'].strftime('%H:%M')
        if selected_slot not in available_slots:
            raise ValidationError(
                'Ese horario ya no está disponible. Elegí otro horario.'
            )

        booking = Booking.objects.create(
            salon=salon,
            customer_name=cleaned_data['customer_name'],
            customer_phone=cleaned_data['customer_phone'],
            customer_email=cleaned_data['customer_email'] or None,
            notes=cleaned_data['notes'],
            booking_mode='consecutive',
            status='confirmed',
            payment_choice='none',
            payment_status='not_required',
            payment_required_amount=0,
            selected_payment_method='none',
        )
        item = BookingItem(
            booking=booking,
            service=service,
            employee=employee,
            start_datetime=cleaned_data['start_datetime'],
            end_datetime=cleaned_data['end_datetime'],
            order=0,
        )
        item.full_clean()
        item.save()
        return booking


@login_required
@subscription_required
def panel_manual_booking_services(request):
    if request.user.is_superuser:
        return JsonResponse({'services': []}, status=403)

    salon = get_user_salon(request.user)
    if not salon or not is_owner_user(request.user):
        return JsonResponse({'services': []}, status=403)

    employee_id = request.GET.get('employee')
    if not employee_id:
        return JsonResponse({'services': []})

    employee = Employee.objects.filter(
        pk=employee_id,
        salon=salon,
        is_active=True,
    ).first()
    if not employee:
        return JsonResponse({'services': []}, status=404)

    services = employee.services.filter(
        salon=salon,
        is_active=True,
    ).order_by('name').values('id', 'name')
    return JsonResponse({'services': list(services)})


@login_required
@subscription_required
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

    if request.method == "POST" and not is_owner:
        raise PermissionDenied("Solo la dueña puede administrar bloqueos.")

    if request.method == "POST":
        form = SpecialAvailabilityBlockForm(request.POST, salon=salon)
        if form.is_valid():
            block = form.save(commit=False)
            block.created_by = request.user
            block.full_clean()
            block.save()
            messages.success(request, "Bloqueo especial creado correctamente.")
            return redirect("panel_bloqueos")
    else:
        form = SpecialAvailabilityBlockForm(salon=salon) if is_owner else None

    blocks = SpecialAvailabilityBlock.objects.select_related(
        "salon",
        "employee",
        "created_by",
    )
    legacy_blocks = EmployeeTimeOff.objects.select_related(
        "employee",
        "created_by",
    )

    if is_owner:
        blocks = blocks.filter(salon=salon)
        legacy_blocks = legacy_blocks.filter(employee__salon=salon)
    elif is_staff and employee:
        blocks = blocks.filter(
            Q(employee__isnull=True) | Q(employee=employee)
        )
        legacy_blocks = legacy_blocks.filter(employee=employee)
    else:
        blocks = blocks.none()
        legacy_blocks = legacy_blocks.none()

    blocks = blocks.order_by("start_datetime")
    legacy_blocks = legacy_blocks.order_by("start_datetime")

    context = {
        "panel_role": "owner" if is_owner else "staff",
        "salon": salon,
        "employee": employee,
        "blocks": blocks,
        "legacy_blocks": legacy_blocks,
        "form": form,
        "editing_block": None,
    }

    return render(request, "reservas/panel/bloqueos.html", context)


@login_required
@subscription_required
def panel_bloqueo_edit(request, block_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)
    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede editar bloqueos.")

    block = get_object_or_404(
        SpecialAvailabilityBlock,
        pk=block_id,
        salon=salon,
    )

    if request.method == "POST":
        form = SpecialAvailabilityBlockForm(
            request.POST,
            instance=block,
            salon=salon,
        )
        if form.is_valid():
            form.save()
            messages.success(request, "Bloqueo especial actualizado.")
            return redirect("panel_bloqueos")
    else:
        form = SpecialAvailabilityBlockForm(instance=block, salon=salon)

    blocks = SpecialAvailabilityBlock.objects.filter(
        salon=salon,
    ).select_related("employee", "created_by").order_by("start_datetime")
    legacy_blocks = EmployeeTimeOff.objects.filter(
        employee__salon=salon,
    ).select_related("employee", "created_by").order_by("start_datetime")

    return render(request, "reservas/panel/bloqueos.html", {
        "panel_role": "owner",
        "salon": salon,
        "employee": None,
        "blocks": blocks,
        "legacy_blocks": legacy_blocks,
        "form": form,
        "editing_block": block,
    })


@login_required
@subscription_required
def panel_services(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede gestionar servicios.")

    services = Service.objects.filter(salon=salon).order_by('name')
    has_active_services = services.filter(is_active=True).exists()

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'services': services,
        'show_next_step': has_active_services,
    }
    context.update(get_onboarding_guidance_context(request, salon))
    return render(request, 'reservas/panel/services.html', context)



@login_required
@subscription_required
def panel_service_create(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede crear servicios.")

    if request.method == 'POST':
        form = PanelServiceForm(request.POST, salon=salon)
        if form.is_valid():
            service = form.save(commit=False)
            service.salon = salon
            service.save()
            set_onboarding_step_completed(request, salon, 2)
            active_employees = list(
                Employee.objects.filter(
                    salon=salon,
                    is_active=True
                )[:2]
            )

            if len(active_employees) == 1:
                active_employees[0].services.add(service)
                messages.success(
                    request,
                    f"Servicio creado y asignado automáticamente a {active_employees[0].name}."
                )
            else:
                messages.success(request, "Servicio creado correctamente.")
            return redirect('panel_services')
    else:
        form = PanelServiceForm(salon=salon)

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'form': form,
        'form_title': 'Nuevo servicio',
        'submit_label': 'Crear servicio',
    }
    return render(request, 'reservas/panel/service_form.html', context)


@login_required
@subscription_required
def panel_service_edit(request, service_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede editar servicios.")

    service = get_object_or_404(Service, pk=service_id, salon=salon)

    if request.method == 'POST':
        form = PanelServiceForm(request.POST, instance=service, salon=salon)
        if form.is_valid():
            form.save()
            messages.success(request, 'Servicio actualizado correctamente.')
            return redirect('panel_services')
    else:
        form = PanelServiceForm(instance=service, salon=salon)

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
@subscription_required
def panel_service_toggle_active(request, service_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede modificar servicios.")

    if request.method != "POST":
        return redirect('panel_services')

    service = get_object_or_404(Service, pk=service_id, salon=salon)
    service.is_active = not service.is_active
    service.save(update_fields=['is_active'])

    if service.is_active:
        messages.success(request, f'“{service.name}” fue activado.')
    else:
        messages.success(request, f'“{service.name}” fue desactivado.')

    return redirect('panel_services')


@login_required
@subscription_required
def panel_service_categories(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede gestionar categorías.")

    categories = (
        ServiceCategory.objects
        .filter(salon=salon)
        .order_by("order", "name")
    )
    has_categories = categories.exists()

    context = {
        "panel_role": "owner",
        "salon": salon,
        "categories": categories,
        "show_next_step": has_categories,
    }
    context.update(get_onboarding_guidance_context(request, salon))

    return render(request, "reservas/panel/service_categories.html", context)


@login_required
@subscription_required
def panel_service_category_create(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede crear categorías.")

    if request.method == "POST":
        form = PanelServiceCategoryForm(request.POST, request.FILES, salon=salon)

        if form.is_valid():
            category = form.save(commit=False)
            category.salon = salon
            category.save()
            set_onboarding_step_completed(request, salon, 1)

            messages.success(request, "Categoría creada correctamente.")
            return redirect("panel_service_categories")
    else:
        form = PanelServiceCategoryForm(salon=salon)

    context = {
        "panel_role": "owner",
        "salon": salon,
        "form": form,
        "form_title": "Nueva categoría",
        "submit_label": "Crear categoría",
    }

    return render(request, "reservas/panel/service_category_form.html", context)


@login_required
@subscription_required
def panel_service_category_edit(request, category_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede editar categorías.")

    category = get_object_or_404(
        ServiceCategory,
        pk=category_id,
        salon=salon,
    )

    if request.method == "POST":
        form = PanelServiceCategoryForm(
            request.POST,
            request.FILES,
            instance=category,
            salon=salon,
        )

        if form.is_valid():
            form.save()
            messages.success(request, "Categoría actualizada correctamente.")
            return redirect("panel_service_categories")
    else:
        form = PanelServiceCategoryForm(instance=category, salon=salon)

    context = {
        "panel_role": "owner",
        "salon": salon,
        "form": form,
        "form_title": f"Editar categoría: {category.name}",
        "submit_label": "Guardar cambios",
        "category": category,
    }

    return render(request, "reservas/panel/service_category_form.html", context)


@login_required
@subscription_required
def panel_service_category_toggle_active(request, category_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede modificar categorías.")

    if request.method != "POST":
        return redirect("panel_service_categories")

    category = get_object_or_404(
        ServiceCategory,
        pk=category_id,
        salon=salon,
    )

    category.is_active = not category.is_active
    category.save(update_fields=["is_active"])

    if category.is_active:
        messages.success(request, f'La categoría “{category.name}” fue activada.')
    else:
        messages.success(request, f'La categoría “{category.name}” fue desactivada.')

    return redirect("panel_service_categories")


@login_required
@subscription_required
def panel_employees(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede gestionar profesionales.")

    employees = (
        Employee.objects
        .filter(salon=salon)
        .select_related("user")
        .prefetch_related("services", "user__salon_memberships")
        .order_by("name")
    )

    for employee in employees:
        employee.is_owner_professional = False

        if employee.user_id:
            employee.is_owner_professional = employee.user.salon_memberships.filter(
                salon=salon,
                role="owner",
                is_active=True
            ).exists()

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'employees': employees,
        'show_next_step': any(employee.is_active for employee in employees),
    }
    context.update(get_onboarding_guidance_context(request, salon))
    return render(request, 'reservas/panel/employees.html', context)



@login_required
@subscription_required
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
            add_pending_onboarding_employee_hours(request, employee)
            set_onboarding_step_completed(
                request,
                salon,
                3,
                employee_id=employee.id,
                employee_name=employee.name,
            )
            messages.success(
                request,
                'Profesional creado correctamente. Ahora podés definir sus horarios de trabajo.',
            )
            if not employee.services.exists():
                return redirect('panel_employees')

            working_hours_url = reverse(
                'panel_employee_working_hours',
                args=[employee.id],
            )
            return redirect(f'{working_hours_url}?created=1')
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
@subscription_required
def panel_employee_create_access(request, employee_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede crear accesos staff.")

    employee = get_object_or_404(
        Employee,
        pk=employee_id,
        salon=salon,
    )

    if employee.user:
        messages.info(request, f"{employee.name} ya tiene acceso al panel.")
        return redirect("panel_employees")

    if request.method == "POST":
        form = PanelEmployeeAccessForm(request.POST)

        if form.is_valid():
            access_email = form.cleaned_data["email"]

            user = User.objects.create(
                username=form.cleaned_data["username"],
                email=access_email,
                first_name=employee.name,
                is_active=False,
            )
            user.set_unusable_password()
            user.save(update_fields=["password", "is_active"])

            SalonMembership.objects.create(
                user=user,
                salon=salon,
                role="staff",
                is_active=True,
            )

            employee.user = user

            if not employee.email:
                employee.email = access_email

            employee.save(update_fields=["user", "email"])

            invitation = StaffInvitation.objects.create(
                salon=salon,
                employee=employee,
                user=user,
                email=access_email,
                invited_by=request.user,
                expires_at=timezone.now() + timedelta(days=3),
            )

            email_sent = False

            try:
                email_sent = send_staff_invitation_email(
                    invitation=invitation,
                    request=request,
                )
            except Exception as exc:
                print(
                    f"ERROR enviando invitación staff. "
                    f"Invitation ID: {invitation.id}. User ID: {user.id}. Error: {exc}"
                )

            if email_sent:
                messages.success(
                    request,
                    f"Invitación enviada correctamente a {access_email}."
                )
            else:
                messages.warning(
                    request,
                    f"El acceso fue creado, pero no se pudo enviar la invitación. Revisá el email o reenviá la invitación."
                )

            return redirect("panel_employees")
    else:
        initial_username = (
            employee.name
            .strip()
            .lower()
            .replace(" ", "_")
        )

        form = PanelEmployeeAccessForm(initial={
            "username": initial_username,
            "email": employee.email or "",
        })

    context = {
        "panel_role": "owner",
        "salon": salon,
        "employee": employee,
        "form": form,
    }

    return render(
        request,
        "reservas/panel/employee_access_form.html",
        context
    )


def accept_staff_invitation(request, token):
    invitation = get_object_or_404(
        StaffInvitation.objects.select_related("salon", "employee", "user"),
        token=token,
    )

    if not invitation.is_valid():
        return render(request, "reservas/panel/staff_invitation_invalid.html", {
            "invitation": invitation,
        })

    # Si alguien abre la invitación estando logueado con otra cuenta,
    # cerramos esa sesión para evitar que termine en el panel equivocado.
    if request.user.is_authenticated and request.user != invitation.user:
        logout(request)

    if request.method == "POST":
        form = AcceptStaffInvitationForm(request.POST)

        if form.is_valid():
            user = invitation.user

            user.set_password(form.cleaned_data["password"])
            user.is_active = True
            user.save(update_fields=["password", "is_active"])

            invitation.accepted_at = timezone.now()
            invitation.save(update_fields=["accepted_at"])

            # Cerramos cualquier sesión residual y logueamos al staff correcto.
            if request.user.is_authenticated:
                logout(request)

            login(request, user)

            messages.success(
                request,
                "Tu acceso fue activado correctamente. Ya podés usar el panel."
            )

            return redirect("panel_dashboard")
    else:
        form = AcceptStaffInvitationForm()

    context = {
        "invitation": invitation,
        "salon": invitation.salon,
        "employee": invitation.employee,
        "form": form,
    }

    return render(request, "reservas/panel/accept_staff_invitation.html", context)


@login_required
@subscription_required
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

    working_hour_blocks = list(
        EmployeeWorkingHour.objects
        .filter(employee=employee, is_active=True)
        .order_by('weekday', 'start_time')
    )
    working_hours_summary = []
    for weekday_value, weekday_name in EmployeeWorkingHour.WEEKDAY_CHOICES:
        day_blocks = [
            block for block in working_hour_blocks
            if block.weekday == weekday_value
        ]
        if day_blocks:
            working_hours_summary.append({
                'weekday_name': weekday_name,
                'blocks': day_blocks,
            })

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'form': form,
        'form_title': f'Editar profesional: {employee.name}',
        'submit_label': 'Guardar cambios',
        'employee': employee,
        'has_own_working_hours': bool(working_hour_blocks),
        'working_hours_summary': working_hours_summary,
    }
    return render(request, 'reservas/panel/employee_form.html', context)


@login_required
@subscription_required
def panel_employee_toggle_active(request, employee_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede modificar profesionales.")

    employee = get_object_or_404(
        Employee.objects.select_related("user"),
        pk=employee_id,
        salon=salon
    )

    if request.method != "POST":
        return redirect("panel_employees")

    should_activate = not employee.is_active

    # Protección: no permitir que una dueña se desactive a sí misma.
    if not should_activate and employee.user_id == request.user.id:
        messages.error(
            request,
            "No podés desactivar tu propio acceso al panel."
        )
        return redirect("panel_employees")

    # Protección: no permitir desactivar a la única dueña activa del salón.
    if not should_activate and employee.user:
        target_membership = SalonMembership.objects.filter(
            user=employee.user,
            salon=salon,
            role="owner",
            is_active=True
        ).first()

        if target_membership:
            active_owners_count = SalonMembership.objects.filter(
                salon=salon,
                role="owner",
                is_active=True
            ).count()

            if active_owners_count <= 1:
                messages.error(
                    request,
                    "No podés desactivar a la única dueña activa del salón."
                )
                return redirect("panel_employees")

    employee.is_active = should_activate
    employee.save(update_fields=["is_active"])

    if employee.user:
        employee.user.is_active = should_activate
        employee.user.save(update_fields=["is_active"])

        SalonMembership.objects.filter(
            user=employee.user,
            salon=salon
        ).update(is_active=should_activate)

    if should_activate:
        messages.success(
            request,
            f'“{employee.name}” fue activado. Su acceso al panel también fue habilitado.'
        )
    else:
        messages.success(
            request,
            f'“{employee.name}” fue desactivado. Su acceso al panel también fue bloqueado.'
        )

    return redirect('panel_employees')


@login_required
@subscription_required
def panel_employee_working_hours(request, employee_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede gestionar horarios de profesionales.")

    employee = get_object_or_404(Employee, pk=employee_id, salon=salon)
    event_metadata = request.session.get(ONBOARDING_EVENT_META_SESSION_KEY) or {}
    if (
        request.session.get(ONBOARDING_EVENT_STEP_SESSION_KEY) == 3
        and event_metadata.get("employee_id") == employee.id
    ):
        clear_onboarding_step_event(request)

    blocks = list(
        EmployeeWorkingHour.objects
        .filter(employee=employee)
        .order_by('weekday', 'start_time')
    )
    has_own_working_hours = bool(blocks)
    show_working_hours_decision = (
        not has_own_working_hours
        and is_onboarding_active(salon, request)
        and (
            salon.onboarding_current_step == 5
            or employee.id in get_pending_onboarding_employee_hours(request)
        )
    )

    blocks_by_weekday = []
    for weekday_value, weekday_name in EmployeeWorkingHour.WEEKDAY_CHOICES:
        day_blocks = [
            block for block in blocks
            if block.weekday == weekday_value
        ]
        blocks_by_weekday.append({
            'weekday': weekday_value,
            'weekday_name': weekday_name,
            'blocks': day_blocks,
            'active_blocks_count': sum(
                1 for block in day_blocks if block.is_active
            ),
        })

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'employee': employee,
        'blocks_by_weekday': blocks_by_weekday,
        'has_own_working_hours': has_own_working_hours,
        'show_working_hours_decision': show_working_hours_decision,
    }
    if request.session.get(ONBOARDING_EVENT_STEP_SESSION_KEY):
        context.update(get_onboarding_guidance_context(request, salon))
    else:
        context.update(get_onboarding_context(request, salon))
        context['show_working_hours_decision'] = show_working_hours_decision
    return render(request, 'reservas/panel/employee_working_hours.html', context)


@login_required
@subscription_required
def panel_employee_working_hour_create(request, employee_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede crear horarios de profesionales.")

    employee = get_object_or_404(Employee, pk=employee_id, salon=salon)

    if request.method == 'POST':
        form = PanelEmployeeWorkingHourForm(
            request.POST,
            employee=employee,
        )
        if form.is_valid():
            with transaction.atomic():
                for weekday in form.cleaned_data['weekdays']:
                    block = EmployeeWorkingHour(
                        employee=employee,
                        weekday=weekday,
                        start_time=form.cleaned_data['start_time'],
                        end_time=form.cleaned_data['end_time'],
                        is_active=form.cleaned_data['is_active'],
                    )
                    block.full_clean()
                    block.save()

            created_count = len(form.cleaned_data['weekdays'])
            mark_onboarding_employee_hours_resolved(request, salon, employee)
            messages.success(
                request,
                f"Se crearon {created_count} franja"
                f"{'s' if created_count != 1 else ''} de trabajo.",
            )
            return redirect('panel_employee_working_hours', employee_id=employee.id)
    else:
        form = PanelEmployeeWorkingHourForm(employee=employee)

    return render(request, 'reservas/panel/employee_working_hour_form.html', {
        'panel_role': 'owner',
        'salon': salon,
        'employee': employee,
        'form': form,
        'form_title': f'Agregar horario: {employee.name}',
        'submit_label': 'Guardar franja',
    })


@login_required
@subscription_required
def panel_employee_working_hour_edit(request, employee_id, block_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede editar horarios de profesionales.")

    employee = get_object_or_404(Employee, pk=employee_id, salon=salon)
    block = get_object_or_404(
        EmployeeWorkingHour,
        pk=block_id,
        employee=employee,
    )

    if request.method == 'POST':
        form = PanelEmployeeWorkingHourForm(
            request.POST,
            instance=block,
            employee=employee,
        )
        if form.is_valid():
            form.save()
            messages.success(request, 'Franja de trabajo actualizada correctamente.')
            return redirect('panel_employee_working_hours', employee_id=employee.id)
    else:
        form = PanelEmployeeWorkingHourForm(
            instance=block,
            employee=employee,
        )

    return render(request, 'reservas/panel/employee_working_hour_form.html', {
        'panel_role': 'owner',
        'salon': salon,
        'employee': employee,
        'block': block,
        'form': form,
        'form_title': f'Editar horario: {employee.name}',
        'submit_label': 'Guardar cambios',
    })


@login_required
@subscription_required
def panel_employee_working_hour_toggle_active(request, employee_id, block_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede modificar horarios de profesionales.")

    employee = get_object_or_404(Employee, pk=employee_id, salon=salon)
    block = get_object_or_404(
        EmployeeWorkingHour,
        pk=block_id,
        employee=employee,
    )

    if request.method != 'POST':
        return redirect('panel_employee_working_hours', employee_id=employee.id)

    block.is_active = not block.is_active

    try:
        block.full_clean()
    except ValidationError as error:
        messages.error(
            request,
            error.messages[0] if error.messages else 'No se pudo modificar la franja.',
        )
    else:
        block.save(update_fields=['is_active'])
        status = 'activada' if block.is_active else 'desactivada'
        messages.success(request, f'Franja de trabajo {status} correctamente.')

    return redirect('panel_employee_working_hours', employee_id=employee.id)


@login_required
@subscription_required
@require_POST
def panel_employee_working_hours_use_salon(request, employee_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueÃ±a puede gestionar horarios de profesionales.")

    employee = get_object_or_404(Employee, pk=employee_id, salon=salon)
    mark_onboarding_employee_hours_resolved(request, salon, employee)
    messages.success(
        request,
        f"{employee.name} usarÃ¡ los horarios generales del salÃ³n.",
    )
    return redirect('panel_employee_working_hours', employee_id=employee.id)


@login_required
@subscription_required
def panel_employee_working_hours_copy_from_salon(request, employee_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede copiar horarios del salón.")

    employee = get_object_or_404(Employee, pk=employee_id, salon=salon)

    if request.method != 'POST':
        return redirect('panel_employee_working_hours', employee_id=employee.id)

    salon_blocks = BusinessHourBlock.objects.filter(
        salon=salon,
        is_active=True,
    ).order_by('weekday', 'start_time')

    created_count = 0
    duplicate_count = 0
    conflicts = []

    for salon_block in salon_blocks:
        exact_block_exists = EmployeeWorkingHour.objects.filter(
            employee=employee,
            weekday=salon_block.weekday,
            start_time=salon_block.start_time,
            end_time=salon_block.end_time,
        ).exists()

        if exact_block_exists:
            duplicate_count += 1
            continue

        employee_block = EmployeeWorkingHour(
            employee=employee,
            weekday=salon_block.weekday,
            start_time=salon_block.start_time,
            end_time=salon_block.end_time,
            is_active=True,
        )

        try:
            employee_block.full_clean()
        except ValidationError:
            conflicts.append(
                f"{salon_block.get_weekday_display()} "
                f"{salon_block.start_time.strftime('%H:%M')} a "
                f"{salon_block.end_time.strftime('%H:%M')}"
            )
            continue

        employee_block.save()
        created_count += 1

    if created_count:
        messages.success(
            request,
            f"Se copiaron {created_count} franja"
            f"{'s' if created_count != 1 else ''} del salón.",
        )

    if created_count:
        mark_onboarding_employee_hours_resolved(request, salon, employee)

    if duplicate_count:
        messages.info(
            request,
            f"Se omitieron {duplicate_count} franja"
            f"{'s' if duplicate_count != 1 else ''} idéntica"
            f"{'s' if duplicate_count != 1 else ''} que ya existían.",
        )

    if duplicate_count:
        mark_onboarding_employee_hours_resolved(request, salon, employee)

    if conflicts:
        messages.warning(
            request,
            "No se copiaron las franjas que se superponen con horarios activos "
            f"de {employee.name}: {', '.join(conflicts)}.",
        )

    if not salon_blocks.exists():
        messages.info(
            request,
            "El salón no tiene franjas activas para copiar.",
        )

    return redirect('panel_employee_working_hours', employee_id=employee.id)


@login_required
@subscription_required
def panel_business_hours(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede gestionar horarios.")

    blocks = (
        BusinessHourBlock.objects
        .filter(salon=salon)
        .order_by('weekday', 'start_time')
    )

    weekdays = BusinessHourBlock.WEEKDAY_CHOICES

    blocks_by_weekday = []

    for weekday_value, weekday_name in weekdays:
        day_blocks = [block for block in blocks if block.weekday == weekday_value]

        active_blocks = [
            block for block in day_blocks
            if block.is_active
        ]

        blocks_by_weekday.append({
            'weekday': weekday_value,
            'weekday_name': weekday_name,
            'blocks': day_blocks,
            'active_blocks_count': len(active_blocks),
        })

    has_active_blocks = any(
        day['active_blocks_count'] > 0
        for day in blocks_by_weekday
    )
    public_url = request.build_absolute_uri(
        reverse("service_list", kwargs={"salon_slug": salon.slug})
    )

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'blocks_by_weekday': blocks_by_weekday,
        'show_next_step': has_active_blocks,
        'public_url': public_url,
    }

    return render(request, 'reservas/panel/business_hours.html', context)


@login_required
@subscription_required
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
@subscription_required
def panel_business_hours_edit(request, business_hours_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede editar horarios.")

    business_hours = get_object_or_404(
        BusinessHours,
        pk=business_hours_id,
        salon=salon
    )

    if request.method == 'POST':
        print("POST HORARIO:", request.POST)

        form = PanelBusinessHoursForm(request.POST, instance=business_hours)

        if form.is_valid():
            business_hours = form.save(commit=False)

            # Forzamos el valor real del checkbox.
            business_hours.is_closed = 'is_closed' in request.POST

            # Si está cerrado, mantenemos horarios válidos para no romper campos obligatorios.
            if business_hours.is_closed:
                if not business_hours.start_time:
                    business_hours.start_time = form.cleaned_data.get('start_time') or business_hours.start_time
                if not business_hours.end_time:
                    business_hours.end_time = form.cleaned_data.get('end_time') or business_hours.end_time

            business_hours.save()

            print(
                "GUARDADO HORARIO:",
                business_hours.get_weekday_display(),
                "is_closed:",
                business_hours.is_closed,
                "start:",
                business_hours.start_time,
                "end:",
                business_hours.end_time,
            )

            messages.success(request, 'Horario actualizado correctamente.')
            return redirect('panel_business_hours')

        print("ERRORES HORARIO:", form.errors, form.non_field_errors())

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
@subscription_required
def panel_business_hour_block_create(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede crear horarios.")

    if request.method == 'POST':
        form = PanelBusinessHourBlockForm(request.POST, salon=salon)

        if form.is_valid():
            with transaction.atomic():
                for weekday in form.cleaned_data['weekdays']:
                    block = BusinessHourBlock(
                        salon=salon,
                        weekday=weekday,
                        start_time=form.cleaned_data['start_time'],
                        end_time=form.cleaned_data['end_time'],
                        is_active=form.cleaned_data['is_active'],
                    )
                    block.full_clean()
                    block.save()

            created_count = len(form.cleaned_data['weekdays'])
            messages.success(
                request,
                f"Se crearon {created_count} franja"
                f"{'s' if created_count != 1 else ''} horaria"
                f"{'s' if created_count != 1 else ''}.",
            )
            return redirect('panel_business_hours')
    else:
        form = PanelBusinessHourBlockForm(salon=salon)

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'form': form,
        'form_title': 'Agregar franja horarria',
        'submit_label': 'Guardar franja',
    }

    return render(request, 'reservas/panel/business_hour_block_form.html', context)


@login_required
@subscription_required
def panel_business_hour_block_edit(request, block_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede editar horarios.")

    block = get_object_or_404(
        BusinessHourBlock,
        pk=block_id,
        salon=salon,
    )

    if request.method == 'POST':
        form = PanelBusinessHourBlockForm(request.POST, instance=block, salon=salon)

        if form.is_valid():
            block = form.save(commit=False)
            block.salon = salon
            block.full_clean()
            block.save()

            messages.success(request, 'Bloque horario actualizado correctamente.')
            return redirect('panel_business_hours')
    else:
        form = PanelBusinessHourBlockForm(instance=block, salon=salon)

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'form': form,
        'form_title': 'Editar franja horarria',
        'submit_label': 'Guardar cambios',
        'block': block,
    }

    return render(request, 'reservas/panel/business_hour_block_form.html', context)


@login_required
@subscription_required
def panel_business_hour_block_toggle_active(request, block_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede modificar horarios.")

    if request.method != "POST":
        return redirect('panel_business_hours')

    block = get_object_or_404(
        BusinessHourBlock,
        pk=block_id,
        salon=salon,
    )

    block.is_active = not block.is_active
    block.save(update_fields=['is_active'])

    if block.is_active:
        messages.success(request, 'Franja horaria activada correctamente.')
    else:
        messages.success(request, 'Franja horaria desactivada correctamente.')

    return redirect('panel_business_hours')

@login_required
@subscription_required
def panel_bloqueo_delete(request, block_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon:
        raise PermissionDenied("No encontramos el negocio asociado.")

    if not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede eliminar bloqueos.")

    if request.method != "POST":
        return redirect("panel_bloqueos")

    block = get_object_or_404(
        EmployeeTimeOff,
        pk=block_id,
        employee__salon=salon,
    )

    block.delete()
    messages.success(request, "Bloqueo eliminado correctamente.")
    return redirect("panel_bloqueos")


@login_required
@subscription_required
def panel_special_bloqueo_delete(request, block_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)
    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede eliminar bloqueos.")

    if request.method != "POST":
        return redirect("panel_bloqueos")

    block = get_object_or_404(
        SpecialAvailabilityBlock,
        pk=block_id,
        salon=salon,
    )
    block.delete()
    messages.success(request, "Bloqueo especial eliminado correctamente.")
    return redirect("panel_bloqueos")


@login_required
@subscription_required
def panel_integrations(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon:
        raise PermissionDenied("Tu usuario no está asociado a ninguna peluquería.")

    if not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede ver las integraciones.")

    google_calendar_connected = GoogleCalendarIntegration.objects.filter(
        salon=salon,
        refresh_token__isnull=False,
    ).exclude(refresh_token="").exists()
    payment_settings = SalonPaymentSettings.objects.filter(salon=salon).first()
    mercadopago_ready = bool(
        payment_settings
        and payment_settings.has_valid_mercadopago_connection()
    )

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'google_calendar_connected': google_calendar_connected,
        'mercadopago_ready': mercadopago_ready,
    }
    return render(request, 'reservas/panel/integrations.html', context)


def _mercadopago_panel_context(salon):
    payment_settings = SalonPaymentSettings.objects.filter(salon=salon).first()
    accepts_integrated = salon.payment_method in ["integrated", "both"]
    mercadopago_ready = bool(
        payment_settings
        and payment_settings.has_valid_mercadopago_connection()
    )

    return {
        'panel_role': 'owner',
        'salon': salon,
        'payment_settings': payment_settings,
        'mercadopago_ready': mercadopago_ready,
        'accepts_integrated': accepts_integrated,
        'mercadopago_visible_to_clients': accepts_integrated and mercadopago_ready,
    }


@login_required
@subscription_required
def panel_mercado_pago_settings(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede configurar Mercado Pago.")

    return render(
        request,
        'reservas/panel/mercado_pago_settings.html',
        _mercadopago_panel_context(salon),
    )


def _google_calendar_oauth_flow(state=None):
    client_config = {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [
                settings.GOOGLE_CALENDAR_REDIRECT_URI,
            ],
        }
    }

    flow = Flow.from_client_config(
        client_config,
        scopes=GOOGLE_CALENDAR_SCOPES,
        state=state,
        autogenerate_code_verifier=False,
    )

    flow.redirect_uri = settings.GOOGLE_CALENDAR_REDIRECT_URI
    return flow

@login_required
@subscription_required
def panel_google_calendar_settings(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede configurar Google Calendar.")

    integration, _ = GoogleCalendarIntegration.objects.get_or_create(salon=salon)

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'integration': integration,
        'google_calendar_configured': all([
            settings.GOOGLE_CLIENT_ID,
            settings.GOOGLE_CLIENT_SECRET,
            settings.GOOGLE_CALENDAR_REDIRECT_URI,
        ]),
    }
    return render(request, 'reservas/panel/google_calendar_settings.html', context)


@login_required
@subscription_required
def panel_google_calendar_connect(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede conectar Google Calendar.")

    if not all([
        settings.GOOGLE_CLIENT_ID,
        settings.GOOGLE_CLIENT_SECRET,
        settings.GOOGLE_CALENDAR_REDIRECT_URI,
    ]):
        messages.error(request, "Faltan configurar las credenciales de Google Calendar.")
        return redirect("panel_google_calendar_settings")

    flow = _google_calendar_oauth_flow()
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    request.session["google_calendar_oauth_state"] = state
    request.session["google_calendar_oauth_salon_id"] = salon.id
    return redirect(authorization_url)


@login_required
@subscription_required
def panel_google_calendar_callback(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)
    expected_state = request.session.get("google_calendar_oauth_state")
    oauth_salon_id = request.session.get("google_calendar_oauth_salon_id")
    state = request.GET.get("state")
    code = request.GET.get("code")

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede conectar Google Calendar.")

    if request.GET.get("error"):
        messages.error(request, "Google Calendar no autorizó la conexión.")
        return redirect("panel_google_calendar_settings")

    if not expected_state or not state or state != expected_state:
        messages.error(request, "No se pudo validar la conexión con Google Calendar.")
        return redirect("panel_google_calendar_settings")

    if not oauth_salon_id or salon.id != oauth_salon_id:
        raise PermissionDenied("La autorización no corresponde a este salón.")

    if not code:
        messages.error(request, "Google Calendar no devolvió un código de autorización.")
        return redirect("panel_google_calendar_settings")

    try:
        flow = _google_calendar_oauth_flow(state=state)
        flow.fetch_token(code=code)
        credentials = flow.credentials

        integration, _ = GoogleCalendarIntegration.objects.get_or_create(salon=salon)
        integration.access_token = credentials.token
        integration.refresh_token = (
            credentials.refresh_token or integration.refresh_token
        )
        integration.token_expiry = credentials.expiry
        if integration.token_expiry and timezone.is_naive(integration.token_expiry):
            integration.token_expiry = timezone.make_aware(
                integration.token_expiry,
                datetime_timezone.utc,
            )
        integration.is_active = True
        integration.save(update_fields=[
            "access_token",
            "refresh_token",
            "token_expiry",
            "is_active",
            "updated_at",
        ])
    except Exception as error:
        print("GOOGLE CALENDAR CALLBACK ERROR:", repr(error))
        messages.error(request, "No se pudo conectar Google Calendar. Intentá nuevamente.")
        return redirect("panel_google_calendar_settings")
    finally:
        request.session.pop("google_calendar_oauth_state", None)
        request.session.pop("google_calendar_oauth_salon_id", None)

    if not integration.is_connected():
        messages.error(
            request,
            "Google no devolvió acceso permanente. Intentá conectar la cuenta nuevamente.",
        )
        return redirect("panel_google_calendar_settings")

    messages.success(request, "Google Calendar conectado correctamente.")
    return redirect("panel_google_calendar_settings")


@login_required
@subscription_required
def panel_google_calendar_disconnect(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede desconectar Google Calendar.")

    if request.method != "POST":
        return redirect("panel_google_calendar_settings")

    integration = GoogleCalendarIntegration.objects.filter(salon=salon).first()
    if integration:
        integration.access_token = None
        integration.refresh_token = None
        integration.token_expiry = None
        integration.is_active = False
        integration.save(update_fields=[
            "access_token",
            "refresh_token",
            "token_expiry",
            "is_active",
            "updated_at",
        ])

    messages.success(request, "Google Calendar fue desconectado.")
    return redirect("panel_google_calendar_settings")


@login_required
@subscription_required
def panel_settings(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede editar la configuración.")

    settings_saved = False

    if request.method == 'POST':
        form = PanelSalonSettingsForm(request.POST, request.FILES, instance=salon)

        if form.is_valid():
            form.save()
            settings_saved = True
    else:
        form = PanelSalonSettingsForm(instance=salon)

    # Recalcular después del form.save(), porque estos valores dependen del salón actualizado.
    payment_policy_active = (
        salon.deposit_enabled
        or salon.allow_full_payment
        or salon.full_payment_required
    )
    accepts_transfer = salon.payment_method in ["transfer", "both"]
    context = {
        'payment_policy_active': payment_policy_active,
        'panel_role': 'owner',
        'salon': salon,
        'form': form,
        'accepts_transfer': accepts_transfer,
        'settings_saved': settings_saved,
    }

    return render(request, 'reservas/panel/settings.html', context)


@login_required
@subscription_required
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
@subscription_required
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
@subscription_required
def panel_booking_cancel(request, booking_id):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede cancelar reservas.")

    if request.method != "POST":
        return redirect('panel_bookings')

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
        entrypoint = get_panel_entrypoint_for_user(request.user)
        if entrypoint:
            return redirect(entrypoint)

        messages.error(
            request,
            'Tu usuario no tiene una membresía activa en ninguna peluquería.'
        )
        logout(request)
        return redirect('panel_login')

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
                return redirect(get_panel_entrypoint_for_user(user))

    return render(request, 'reservas/panel/login.html')


def trial_signup(request):
    if request.user.is_authenticated:
        if request.user.is_superuser:
            return redirect("/admin/")
        return redirect("panel_dashboard")

    if request.method == "POST":
        form = TrialSignupForm(request.POST)

        if form.is_valid():
            salon_name = form.cleaned_data["salon_name"]
            owner_name = form.cleaned_data["owner_name"]
            phone = form.cleaned_data["phone"]
            email = form.cleaned_data["email"]
            username = form.cleaned_data["username"]
            password = form.cleaned_data["password"]

            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                first_name=owner_name,
            )

            salon = Salon.objects.create(
                name=salon_name,
                email=email,
                phone=phone,
                notification_email=email,
                notify_new_bookings_by_email=True,
                is_active=True,
            )

            SalonMembership.objects.create(
                user=user,
                salon=salon,
                role="owner",
                is_active=True,
            )

            now = timezone.now()
            trial_days = getattr(settings, "NYX_TRIAL_DAYS", 15)
            monthly_price = getattr(settings, "NYX_BASIC_MONTHLY_PRICE_ARS", 25000)
            notify_admin_new_trial_account(user=user, salon=salon)
            
            SalonSubscription.objects.create(
                salon=salon,
                status=SalonSubscription.Status.TRIAL,
                plan=SalonSubscription.Plan.BASIC,
                monthly_price_ars=monthly_price,
                trial_starts_at=now,
                trial_ends_at=now + timedelta(days=trial_days),
            )

            if form.cleaned_data.get("owner_works"):
                Employee.objects.create(
                    salon=salon,
                    user=user,
                    name=form.cleaned_data["owner_name"],
                    phone=form.cleaned_data["phone"],
                    email=form.cleaned_data["email"],
                    is_active=True,
                    notify_by_email=True,
                )

            login(request, user)

            messages.success(
                request,
                f"Tu prueba gratuita de {trial_days} días fue creada correctamente."
            )

            return redirect("panel_onboarding")
    else:
        form = TrialSignupForm()

    return render(request, "reservas/panel/trial_signup.html", {
        "form": form,
    })

@login_required
def panel_logout(request):
    logout(request)
    return redirect('panel_login')

@login_required
def panel_billing_required(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon:
        raise PermissionDenied("Tu usuario no está asociado a ninguna peluquería.")

    subscription = get_or_create_salon_subscription(salon)

    context = {
        "salon": salon,
        "subscription": subscription,
        "panel_role": "owner" if is_owner_user(request.user) else "staff",
        "hide_panel_nav": True,
    }

    return render(request, "reservas/panel/billing_required.html", context)

@login_required
@subscription_required
def panel_plan(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña o dueño puede ver el plan.")

    subscription = get_or_create_salon_subscription(salon)

    support_whatsapp = getattr(settings, "NYX_SUPPORT_WHATSAPP", "5493416959852")
    mercadopago_subscription_url = getattr(settings, "NYX_MERCADOPAGO_SUBSCRIPTION_URL", "")

    whatsapp_text = (
        f"Hola, necesito ayuda con mi plan de NYX. "
        f"Mi negocio es {salon.name}."
    )

    context = {
        "panel_role": "owner",
        "salon": salon,
        "subscription": subscription,
        "support_whatsapp": support_whatsapp,
        "mercadopago_subscription_url": mercadopago_subscription_url,
        "whatsapp_text": whatsapp_text,
    }

    return render(request, "reservas/panel/plan.html", context)

@login_required
@subscription_required
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
