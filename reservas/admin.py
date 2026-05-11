from django.contrib import admin
from django import forms
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.http import Http404
from django.utils import timezone
from datetime import timedelta

User = get_user_model()

from .forms import AppointmentForm
from .models import Service, Employee, Appointment, BusinessHours, Salon, Booking, BookingItem, SalonMembership, EmployeeTimeOff


def get_user_membership(user):
    if not user.is_authenticated:
        return None

    if user.is_superuser:
        return None

    return user.salon_memberships.filter(is_active=True).select_related('salon').first()


def is_owner_user(user):
    membership = get_user_membership(user)
    return bool(membership and membership.role == 'owner')


def is_staff_user(user):
    membership = get_user_membership(user)
    return bool(membership and membership.role == 'staff')


def get_user_salon(user):
    membership = get_user_membership(user)
    return membership.salon if membership else None


def get_user_employee(user):
    return getattr(user, 'employee_profile', None)

@admin.register(Salon)
class SalonAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'phone', 'is_active', 'created_at','notification_email',)
    search_fields = ('name', 'email', 'phone', 'notification_email',)
    list_filter = ('is_active',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs

        salon = get_user_salon(request.user)
        if salon:
            return qs.filter(pk=salon.pk)

        return qs.none()

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ('name', 'salon', 'price', 'duration_minutes', 'is_active')
    search_fields = ('name', 'salon__name')
    list_filter = ('salon', 'is_active')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs

        salon = get_user_salon(request.user)
        if is_owner_user(request.user) and salon:
            return qs.filter(salon=salon)

        return qs.none()

    def has_module_permission(self, request):
        return request.user.is_superuser or is_owner_user(request.user)

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if not is_owner_user(request.user):
            return False
        if obj is None:
            return True
        return obj.salon_id == getattr(get_user_salon(request.user), 'id', None)

    def has_add_permission(self, request):
        return request.user.is_superuser or is_owner_user(request.user)

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if not is_owner_user(request.user):
            return False
        if obj is None:
            return True
        return obj.salon_id == getattr(get_user_salon(request.user), 'id', None)

    def has_delete_permission(self, request, obj=None):
        return self.has_change_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser and is_owner_user(request.user):
            obj.salon = get_user_salon(request.user)
        super().save_model(request, obj, form, change)

class EmployeeAdminForm(forms.ModelForm):
    class Meta:
        model = Employee
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # En creación mostramos todos los servicios activos.
        # El JS después filtra por peluquería.
        self.fields['services'].queryset = Service.objects.filter(
            is_active=True
        ).select_related('salon').order_by('name')

        # En edición, si ya existe salón, limitamos desde backend también.
        if self.instance and self.instance.pk and self.instance.salon_id:
            self.fields['services'].queryset = Service.objects.filter(
                salon_id=self.instance.salon_id,
                is_active=True
            ).order_by('name')

    def clean(self):
        cleaned_data = super().clean()
        salon = cleaned_data.get('salon')
        services = cleaned_data.get('services')

        if salon and services:
            invalid_services = services.exclude(salon=salon)
            if invalid_services.exists():
                raise ValidationError(
                    "Hay servicios seleccionados que no pertenecen a la peluquería elegida."
                )

        return cleaned_data


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    form = EmployeeAdminForm
    list_display = ('name', 'salon', 'user', 'phone', 'is_active')
    list_filter = ('salon', 'is_active')
    search_fields = ('name', 'phone', 'salon__name', 'user__username')
    filter_horizontal = ('services',)

    class Media:
        js = ('reservas/js/employee_admin.js',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs

        salon = get_user_salon(request.user)
        if is_owner_user(request.user) and salon:
            return qs.filter(salon=salon)

        return qs.none()

    def has_module_permission(self, request):
        return request.user.is_superuser or is_owner_user(request.user)

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if not is_owner_user(request.user):
            return False
        if obj is None:
            return True
        return obj.salon_id == getattr(get_user_salon(request.user), 'id', None)

    def has_add_permission(self, request):
        return request.user.is_superuser or is_owner_user(request.user)

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if not is_owner_user(request.user):
            return False
        if obj is None:
            return True
        return obj.salon_id == getattr(get_user_salon(request.user), 'id', None)

    def has_delete_permission(self, request, obj=None):
        return self.has_change_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser and is_owner_user(request.user):
            obj.salon = get_user_salon(request.user)
        super().save_model(request, obj, form, change)


@admin.register(BusinessHours)
class BusinessHoursAdmin(admin.ModelAdmin):
    list_display = ('salon', 'weekday', 'start_time', 'end_time', 'is_closed')
    list_filter = ('salon', 'is_closed')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs

        salon = get_user_salon(request.user)
        if is_owner_user(request.user) and salon:
            return qs.filter(salon=salon)

        return qs.none()

    def has_module_permission(self, request):
        return request.user.is_superuser or is_owner_user(request.user)

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if not is_owner_user(request.user):
            return False
        if obj is None:
            return True
        return obj.salon_id == getattr(get_user_salon(request.user), 'id', None)

    def has_add_permission(self, request):
        return request.user.is_superuser or is_owner_user(request.user)

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if not is_owner_user(request.user):
            return False
        if obj is None:
            return True
        return obj.salon_id == getattr(get_user_salon(request.user), 'id', None)

    def has_delete_permission(self, request, obj=None):
        return self.has_change_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser and is_owner_user(request.user):
            obj.salon = get_user_salon(request.user)
        super().save_model(request, obj, form, change)


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    form = AppointmentForm
    list_display = ('customer_name', 'salon', 'employee', 'appointment_datetime', 'status', 'get_services_list')
    search_fields = ('customer_name', 'customer_phone', 'customer_email', 'salon__name')
    list_filter = ('status', 'employee', 'salon')

    class Media:
        js = ('reservas/js/appointment_admin.js',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs

        salon = get_user_salon(request.user)
        employee = get_user_employee(request.user)

        if is_owner_user(request.user) and salon:
            return qs.filter(salon=salon)

        if is_staff_user(request.user) and employee:
            return qs.filter(employee=employee)

        return qs.none()

    def has_module_permission(self, request):
        return request.user.is_superuser or is_owner_user(request.user)

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True

        salon = get_user_salon(request.user)

        if is_owner_user(request.user):
            if obj is None:
                return True
            return obj.salon_id == getattr(salon, 'id', None)

        return False


    def has_add_permission(self, request):
        return request.user.is_superuser or is_owner_user(request.user)

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True

        if is_owner_user(request.user):
            if obj is None:
                return True
            return obj.salon_id == getattr(get_user_salon(request.user), 'id', None)

        return False

    def has_delete_permission(self, request, obj=None):
        return self.has_change_permission(request, obj)

    def get_services_list(self, obj):
        return ", ".join(service.name for service in obj.get_selected_services())
    get_services_list.short_description = 'Servicios'


class BookingItemInline(admin.TabularInline):
    model = BookingItem
    extra = 0
    can_delete = False
    fields = ('service', 'employee', 'start_datetime', 'end_datetime')
    readonly_fields = ('service', 'employee', 'start_datetime', 'end_datetime')


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'customer_name',
        'salon',
        'customer_phone',
        'customer_email',
        'booking_mode',
        'status',
        'created_at',
        'get_services_list',
    )
    search_fields = (
        'customer_name',
        'customer_phone',
        'customer_email',
        'salon__name',
    )
    list_filter = (
        'status',
        'booking_mode',
        'salon',
    )
    inlines = [BookingItemInline]

    def get_queryset(self, request):
        qs = super().get_queryset(request).prefetch_related('items__service', 'items__employee').distinct()

        if request.user.is_superuser:
            return qs

        salon = get_user_salon(request.user)
        employee = get_user_employee(request.user)

        if is_owner_user(request.user) and salon:
            return qs.filter(salon=salon)

        if is_staff_user(request.user) and employee:
            return qs.filter(items__employee=employee).distinct()

        return qs.none()

    def has_module_permission(self, request):
        return (
            request.user.is_superuser
            or is_owner_user(request.user)
            or is_staff_user(request.user)
        )

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True

        salon = get_user_salon(request.user)
        employee = get_user_employee(request.user)

        if is_owner_user(request.user):
            if obj is None:
                return True
            return obj.salon_id == getattr(salon, 'id', None)

        if is_staff_user(request.user):
            if obj is None:
                return True
            return obj.items.filter(employee=employee).exists() if employee else False

        return False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True

        if is_owner_user(request.user):
            if obj is None:
                return True
            return obj.salon_id == getattr(get_user_salon(request.user), 'id', None)

        # staff solo lectura
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        base = ['salon', 'customer_name', 'customer_phone', 'customer_email', 'notes', 'booking_mode', 'status', 'created_at']
        if request.user.is_superuser or is_owner_user(request.user):
            return ['created_at']
        return base

    def get_services_list(self, obj):
        return ", ".join(item.service.name for item in obj.items.all())

    get_services_list.short_description = 'Servicios'



@admin.register(SalonMembership)
class SalonMembershipAdmin(admin.ModelAdmin):
    list_display = ('user', 'salon', 'role', 'is_active', 'created_at')
    list_filter = ('role', 'is_active', 'salon')
    search_fields = ('user__username', 'user__first_name', 'user__last_name', 'salon__name')

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
    

@admin.register(EmployeeTimeOff)
class EmployeeTimeOffAdmin(admin.ModelAdmin):
    list_display = ('employee', 'start_datetime', 'end_datetime', 'reason', 'created_by')
    list_filter = ('employee__salon', 'employee')
    search_fields = ('employee__name', 'reason')
    autocomplete_fields = ('employee', 'created_by')

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('employee', 'employee__salon', 'created_by')
        if request.user.is_superuser:
            return qs

        salon = get_user_salon(request.user)
        employee = get_user_employee(request.user)

        if is_owner_user(request.user) and salon:
            return qs.filter(employee__salon=salon)

        if is_staff_user(request.user) and employee:
            return qs.filter(employee=employee)

        return qs.none()

    def has_module_permission(self, request):
        return request.user.is_superuser or is_owner_user(request.user) or is_staff_user(request.user)

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True

        salon = get_user_salon(request.user)
        employee = get_user_employee(request.user)

        if is_owner_user(request.user):
            if obj is None:
                return True
            return obj.employee.salon_id == getattr(salon, 'id', None)

        if is_staff_user(request.user):
            if obj is None:
                return True
            return employee and obj.employee_id == employee.id

        return False

    def has_add_permission(self, request):
        return request.user.is_superuser or is_owner_user(request.user) or is_staff_user(request.user)

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True

        salon = get_user_salon(request.user)
        employee = get_user_employee(request.user)

        if is_owner_user(request.user):
            if obj is None:
                return True
            return obj.employee.salon_id == getattr(salon, 'id', None)

        if is_staff_user(request.user):
            if obj is None:
                return True
            return employee and obj.employee_id == employee.id

        return False

    def has_delete_permission(self, request, obj=None):
        return self.has_change_permission(request, obj)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if not request.user.is_superuser:
            salon = get_user_salon(request.user)
            employee = get_user_employee(request.user)

            if db_field.name == 'employee':
                if is_owner_user(request.user) and salon:
                    kwargs['queryset'] = Employee.objects.filter(salon=salon, is_active=True).order_by('name')
                elif is_staff_user(request.user) and employee:
                    kwargs['queryset'] = Employee.objects.filter(pk=employee.pk)
                else:
                    kwargs['queryset'] = Employee.objects.none()

            if db_field.name == 'created_by':
                kwargs['queryset'] = User.objects.filter(pk=request.user.pk)

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user

        if is_staff_user(request.user):
            employee = get_user_employee(request.user)
            if employee:
                obj.employee = employee

        super().save_model(request, obj, form, change)

class WorkDayListFilter(admin.SimpleListFilter):
    title = 'día de trabajo'
    parameter_name = 'work_day'

    def lookups(self, request, model_admin):
        return (
            ('today', 'Hoy'),
            ('tomorrow', 'Mañana'),
            ('upcoming', 'Próximos 7 días'),
            ('past', 'Pasados'),
        )

    def queryset(self, request, queryset):
        today = timezone.localdate()
        tomorrow = today + timedelta(days=1)
        next_week = today + timedelta(days=7)

        if self.value() == 'today':
            return queryset.filter(start_datetime__date=today)

        if self.value() == 'tomorrow':
            return queryset.filter(start_datetime__date=tomorrow)

        if self.value() == 'upcoming':
            return queryset.filter(
                start_datetime__date__gte=today,
                start_datetime__date__lte=next_week,
            )

        if self.value() == 'past':
            return queryset.filter(start_datetime__date__lt=today)

        return queryset

@admin.register(BookingItem)
class BookingItemAdmin(admin.ModelAdmin):
    list_display = (
        'start_datetime',
        'end_datetime',
        'customer_name',
        'customer_phone',
        'service',
        'employee',
        'booking_status',
        'salon_name',
    )
    search_fields = (
        'booking__customer_name',
        'booking__customer_phone',
        'booking__customer_email',
        'service__name',
        'employee__name',
        'booking__salon__name',
    )
    list_filter = (
        WorkDayListFilter,
        'employee',
        'service',
        'booking__status',
        'booking__salon',
    )
    autocomplete_fields = ('booking', 'service', 'employee')
    date_hierarchy = 'start_datetime'
    ordering = ('start_datetime',)

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related(
            'booking',
            'booking__salon',
            'service',
            'employee',
        )

        if request.user.is_superuser:
            return qs

        salon = get_user_salon(request.user)
        employee = get_user_employee(request.user)

        if is_owner_user(request.user) and salon:
            return qs.filter(booking__salon=salon)

        if is_staff_user(request.user) and employee:
            return qs.filter(employee=employee)

        return qs.none()

    def has_module_permission(self, request):
        return (
            request.user.is_superuser
            or is_owner_user(request.user)
            or is_staff_user(request.user)
        )

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True

        salon = get_user_salon(request.user)
        employee = get_user_employee(request.user)

        if is_owner_user(request.user):
            if obj is None:
                return True
            return obj.booking.salon_id == getattr(salon, 'id', None)

        if is_staff_user(request.user):
            if obj is None:
                return True
            return bool(employee and obj.employee_id == employee.id)

        return False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True

        if is_owner_user(request.user):
            if obj is None:
                return True
            return obj.booking.salon_id == getattr(get_user_salon(request.user), 'id', None)

        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def customer_name(self, obj):
        return obj.booking.customer_name
    customer_name.short_description = 'Cliente'

    def customer_phone(self, obj):
        return obj.booking.customer_phone
    customer_phone.short_description = 'Teléfono'

    def booking_status(self, obj):
        return obj.booking.get_status_display()
    booking_status.short_description = 'Estado'

    def salon_name(self, obj):
        return obj.booking.salon.name
    salon_name.short_description = 'Peluquería'

