from datetime import timedelta, datetime


from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.utils import timezone

from reservas.notifications import notify_admin_new_trial_account

from .mail_utils import send_booking_confirmed_email, send_staff_invitation_email, send_booking_cancelled_email, send_booking_payment_pending_email
from .models import (
    BookingItem,
    EmployeeTimeOff,
    Service,
    ServiceCategory,
    Employee,
    BusinessHours,
    BusinessHourBlock,
    Salon,
    SalonMembership,
    Booking,
    StaffInvitation,
    SalonPaymentSettings,
    SalonSubscription,
)
from .panel_forms import (
    PanelBusinessHoursForm,
    PanelBusinessHourBlockForm,
    PanelServiceForm,
    PanelEmployeeForm,
    EmployeeTimeOffForm,
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
    monthly_price = getattr(settings, "NYX_BASIC_MONTHLY_PRICE_ARS", 30000)

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


def get_user_employee(user):
    return getattr(user, 'employee_profile', None)



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
        'today_count': today_items.count(),
        'tomorrow_count': tomorrow_items.count(),
        'pending_count': future_items.filter(booking__status='pending').count(),
        'confirmed_count': future_items.filter(booking__status='confirmed').count(),
        'time_off_count': time_off_blocks.filter(end_datetime__gte=now).count(),
        'next_item': next_item,
        'has_dashboard_activity': has_dashboard_activity,
    }
    return render(request, 'reservas/panel/dashboard.html', context)

@login_required
@subscription_required
def panel_onboarding(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede ver la bienvenida del salón.")

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
@subscription_required
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

    context = {
        "panel_role": "owner",
        "salon": salon,
        "categories": categories,
    }

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
        form = PanelServiceCategoryForm(request.POST, request.FILES)

        if form.is_valid():
            category = form.save(commit=False)
            category.salon = salon
            category.save()

            messages.success(request, "Categoría creada correctamente.")
            return redirect("panel_service_categories")
    else:
        form = PanelServiceCategoryForm()

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
        form = PanelServiceCategoryForm(request.POST, request.FILES, instance=category)

        if form.is_valid():
            form.save()
            messages.success(request, "Categoría actualizada correctamente.")
            return redirect("panel_service_categories")
    else:
        form = PanelServiceCategoryForm(instance=category)

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
    }
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

    context = {
        'panel_role': 'owner',
        'salon': salon,
        'blocks_by_weekday': blocks_by_weekday,
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
            block = form.save(commit=False)
            block.salon = salon
            block.full_clean()
            block.save()

            messages.success(request, 'Franja horaria creada correctamente.')
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

    block = get_object_or_404(
        EmployeeTimeOff,
        pk=block_id,
        employee__salon=salon,
    )

    if not is_owner_user(request.user):
        employee = get_user_employee(request.user)

        if not employee or block.employee_id != employee.id:
            raise PermissionDenied("No podés eliminar bloqueos de otro profesional.")

    if request.method != "POST":
        return redirect("panel_bloqueos")

    block.delete()
    messages.success(request, "Bloqueo eliminado correctamente.")
    return redirect("panel_bloqueos")


@login_required
@subscription_required
def panel_settings(request):
    if request.user.is_superuser:
        return redirect('/admin/')

    salon = get_user_salon(request.user)

    if not salon or not is_owner_user(request.user):
        raise PermissionDenied("Solo la dueña puede editar la configuración.")

    payment_settings, _ = SalonPaymentSettings.objects.get_or_create(salon=salon)

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
    accepts_integrated = salon.payment_method in ["integrated", "both"]
    mercadopago_ready = payment_settings.has_valid_mercadopago_connection()
    mercadopago_visible_to_clients = accepts_integrated and mercadopago_ready

    context = {
        'payment_policy_active': payment_policy_active,
        'panel_role': 'owner',
        'salon': salon,
        'form': form,
        'payment_settings': payment_settings,
        'accepts_transfer': accepts_transfer,
        'accepts_integrated': accepts_integrated,
        'settings_saved': settings_saved,
        'mercadopago_ready': mercadopago_ready,
        'mercadopago_visible_to_clients': mercadopago_visible_to_clients,
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
            monthly_price = getattr(settings, "NYX_BASIC_MONTHLY_PRICE_ARS", 30000)
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