from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.contrib.auth import get_user_model
import uuid


User = get_user_model()


class Salon(models.Model):
    PAYMENT_METHOD_CHOICES = [
        ('transfer', 'Transferencia'),
        ('integrated', 'Pago integrado'),
        ('both', 'Transferencia y pago integrado'),
    ]

    name = models.CharField('Nombre', max_length=120)
    email = models.EmailField('Email', blank=True)
    phone = models.CharField('Teléfono', max_length=30, blank=True)
    address = models.CharField('Dirección', max_length=200, blank=True)
    is_active = models.BooleanField('Activo', default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    deposit_enabled = models.BooleanField('Requerir seña', default=False)
    deposit_percentage = models.PositiveIntegerField('Porcentaje de seña', default=0)

    allow_full_payment = models.BooleanField('Permitir pago total', default=False)
    full_payment_required = models.BooleanField('Requerir pago total', default=False)

    payment_method = models.CharField(
        'Método de pago',
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        default='transfer'
    )

    payment_instructions = models.TextField('Instrucciones de pago', blank=True)

    allow_client_cancellation = models.BooleanField(
        'Permitir cancelación online por cliente',
        default=True
    )

    cancellation_limit_hours = models.PositiveIntegerField(
        'Horas límite para cancelar online',
        default=24
    )

    allow_client_reschedule = models.BooleanField(
        'Permitir modificación online por cliente',
        default=False
    )

    reschedule_limit_hours = models.PositiveIntegerField(
        'Horas límite para modificar online',
        default=24
    )
    notification_email = models.EmailField(
        blank=True,
        null=True,
        verbose_name="Email para notificaciones"
    )

    class Meta:
        verbose_name = 'Peluquería'
        verbose_name_plural = 'Peluquerías'
        ordering = ['name']

    def __str__(self):
        return self.name

    def clean(self):
        if self.deposit_percentage < 0 or self.deposit_percentage > 100:
            raise ValidationError("El porcentaje de seña debe estar entre 0 y 100.")

        if self.deposit_enabled and self.deposit_percentage <= 0:
            raise ValidationError("Si requerís seña, el porcentaje debe ser mayor que 0.")
        if self.cancellation_limit_hours < 0:
            raise ValidationError("Las horas límite para cancelar no pueden ser negativas.")

        if self.reschedule_limit_hours < 0:
            raise ValidationError("Las horas límite para modificar no pueden ser negativas.")

    
class SalonMembership(models.Model):
    ROLE_CHOICES = [
        ('owner', 'Dueña/o'),
        ('staff', 'Staff'),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='salon_memberships'
    )
    salon = models.ForeignKey(
        Salon,
        on_delete=models.CASCADE,
        related_name='memberships'
    )
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default='staff'
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Membresía de peluquería'
        verbose_name_plural = 'Membresías de peluquería'
        unique_together = ('user', 'salon')

    def __str__(self):
        return f"{self.user} - {self.salon} - {self.role}"

class Service(models.Model):
    name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    duration_minutes = models.PositiveIntegerField()
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    salon = models.ForeignKey(
        Salon,
        on_delete=models.CASCADE,
        related_name='services',
        verbose_name='Peluquería',
    )

    def __str__(self):
        return self.name


class Employee(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employee_profile',
        verbose_name='Usuario'
    )
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)
    services = models.ManyToManyField(Service, blank=True, related_name='employees')
    salon = models.ForeignKey(
        Salon,
        on_delete=models.CASCADE,
        related_name='employees',
        verbose_name='Peluquería',
    )
    email = models.EmailField(
        "Email",
        blank=True,
        null=True
    )

    notify_by_email = models.BooleanField(
        "Recibir notificaciones por email",
        default=False
    )

    def __str__(self):
        return self.name
    


class BusinessHours(models.Model):
    WEEKDAY_CHOICES = [
        (0, 'Lunes'),
        (1, 'Martes'),
        (2, 'Miércoles'),
        (3, 'Jueves'),
        (4, 'Viernes'),
        (5, 'Sábado'),
        (6, 'Domingo'),
    ]

    salon = models.ForeignKey(
        Salon,
        on_delete=models.CASCADE,
        related_name='business_hours',
        verbose_name='Peluquería',
        
    )
    weekday = models.IntegerField(choices=WEEKDAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_closed = models.BooleanField(default=False)

    class Meta:
        verbose_name = 'Horario de atención'
        verbose_name_plural = 'Horarios de atención'
        ordering = ['salon', 'weekday']
        unique_together = ['salon', 'weekday']

    def clean(self):
        if not self.is_closed and self.start_time >= self.end_time:
            raise ValidationError("La hora de inicio debe ser menor que la hora de fin.")

    def __str__(self):
        day_name = dict(self.WEEKDAY_CHOICES).get(self.weekday, self.weekday)
        if self.is_closed:
            return f"{self.salon.name} - {day_name}: Cerrado"
        return f"{self.salon.name} - {day_name}: {self.start_time} - {self.end_time}"


class Appointment(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pendiente'),
        ('confirmed', 'Confirmado'),
        ('completed', 'Finalizado'),
        ('cancelled', 'Cancelado'),
    ]

    customer_name = models.CharField(max_length=100)
    customer_phone = models.CharField(max_length=30)
    customer_email= models.EmailField('Email',blank=True, null=True)

    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name='appointments',
        verbose_name='Profesional'
    )

    salon = models.ForeignKey(
        Salon,
        on_delete=models.CASCADE,
        related_name='appointments',
        verbose_name='Peluquería',
        
    )
    # Lo dejamos temporalmente por compatibilidad con tus turnos viejos
    service = models.ForeignKey(
        Service,
        on_delete=models.CASCADE,
        related_name='legacy_appointments',
        null=True,
        blank=True
    )

    services = models.ManyToManyField(Service, blank=True, related_name='appointments')

    appointment_datetime = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    notes = models.TextField(blank=True)

    def get_selected_services(self):
        if hasattr(self, '_selected_services'):
            return list(self._selected_services)

        if self.pk:
            selected = list(self.services.all())
            if selected:
                return selected

        if self.service_id:
            return [self.service]

        return []

    def get_total_duration_minutes(self):
        return sum(service.duration_minutes for service in self.get_selected_services())

    def get_total_price(self):
        return sum(service.price for service in self.get_selected_services())

    def clean(self):
        errors = []

        selected_services = self.get_selected_services()

        if not self.employee:
            errors.append("Debes seleccionar un profesional.")

        if not selected_services:
            errors.append("Debes seleccionar al menos un servicio.")

        if not self.appointment_datetime:
            errors.append("Debes seleccionar fecha y hora.")

        if not self.salon:
            errors.append("Debes seleccionar una peluquería.")

        if errors:
            raise ValidationError(errors)
        
        if self.employee and self.employee.salon_id != self.salon_id:
            errors.append("El profesional no pertenece a la peluquería seleccionada.")

        for selected_service in selected_services:
            if selected_service.salon_id != self.salon_id:
                errors.append(
                    f"El servicio '{selected_service.name}' no pertenece a la peluquería seleccionada."
                )

        weekday = self.appointment_datetime.weekday()
        appointment_start = self.appointment_datetime
        total_duration = self.get_total_duration_minutes()
        appointment_end = appointment_start + timedelta(minutes=total_duration)
        appointment_time = appointment_start.time()
        appointment_end_time = appointment_end.time()

        business_hours = BusinessHours.objects.filter(
            salon=self.salon,
            weekday=weekday
        ).first()

        if not business_hours:
            errors.append("No hay horario configurado para ese día.")
        elif business_hours.is_closed:
            errors.append("La peluquería no atiende ese día.")
        else:
            if appointment_time < business_hours.start_time or appointment_time >= business_hours.end_time:
                errors.append("El turno está fuera del horario de atención.")

            if appointment_end.date() != appointment_start.date():
                errors.append("El turno no puede terminar al día siguiente.")
            elif appointment_end_time > business_hours.end_time:
                errors.append("El turno termina fuera del horario de atención.")

        existing_appointments = Appointment.objects.filter(
            employee=self.employee,
            appointment_datetime__date=self.appointment_datetime.date(),
        ).exclude(pk=self.pk).exclude(status='cancelled').prefetch_related('services', 'service')

        for existing in existing_appointments:
            existing_start = existing.appointment_datetime
            existing_end = existing_start + timedelta(minutes=existing.get_total_duration_minutes())

            overlaps = appointment_start < existing_end and appointment_end > existing_start
            if overlaps:
                errors.append(f"{self.employee.name} ya tiene un turno que se superpone con ese horario.")
                break

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        service_names = ", ".join(service.name for service in self.get_selected_services()) or "Sin servicios"
        employee_name = self.employee.name if self.employee else "Sin profesional"
        return f"{self.customer_name} - {service_names} - {employee_name} - {self.appointment_datetime}"
    
class Booking(models.Model):
    MODE_CHOICES = [
        ('consecutive', 'Consecutivos'),
        ('independent', 'Horarios independientes'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pendiente'),
        ('confirmed', 'Confirmado'),
        ('completed', 'Finalizado'),
        ('cancelled', 'Cancelado'),
        ('expired', 'Expirado'),
    ]

    PAYMENT_CHOICE_CHOICES = [
        ('none', 'Sin pago'),
        ('deposit', 'Seña'),
        ('full', 'Pago total'),
    ]

    PAYMENT_STATUS_CHOICES = [
        ('not_required', 'No requerido'),
        ('pending', 'Pendiente'),
        ('reported', 'Informado'),
        ('verified', 'Verificado'),
        ('rejected', 'Rechazado'),
    ]

    salon = models.ForeignKey(
        Salon,
        on_delete=models.CASCADE,
        related_name='bookings',
        verbose_name='Peluquería'
    )
    customer_name = models.CharField(max_length=100)
    customer_phone = models.CharField(max_length=30)
    customer_email = models.EmailField('Email', blank=True, null=True)
    notes = models.TextField(blank=True)
    booking_mode = models.CharField(
        max_length=20,
        choices=MODE_CHOICES,
        default='consecutive',
        verbose_name='Modo de reserva'
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    client_manage_token = models.UUIDField(
        'Token de gestión del cliente',
        unique=True,
        editable=False,
        null=True,
        blank=True
    )

    cancelled_at = models.DateTimeField(
        'Cancelado el',
        null=True,
        blank=True
    )

    cancelled_by_client = models.BooleanField(
        'Cancelado por cliente',
        default=False
    )

    payment_choice = models.CharField(
        max_length=20,
        choices=PAYMENT_CHOICE_CHOICES,
        default='none'
    )

    payment_status = models.CharField(
        max_length=20,
        choices=PAYMENT_STATUS_CHOICES,
        default='not_required'
    )

    payment_required_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    payment_provider = models.CharField(
        max_length=30,
        blank=True,
        default=''
    )

    external_payment_id = models.CharField(
        max_length=120,
        blank=True,
        default=''
    )

    payment_checkout_url = models.URLField(
        blank=True,
        default=''
    )

    payment_reference = models.CharField(
        max_length=120,
        blank=True,
        default=''
    )

    payment_expires_at = models.DateTimeField(
        null=True,
        blank=True
    )

    payment_verified_at = models.DateTimeField(
        null=True,
        blank=True
    )

    selected_payment_method = models.CharField(
        max_length=20,
        blank=True,
        default=''
    )
    

    def get_total_duration_minutes(self):
        items = self.items.select_related('service').all()
        return sum(item.service.duration_minutes for item in items)

    def get_total_price(self):
        items = self.items.select_related('service').all()
        return sum(item.service.price for item in items)
    
    def get_deposit_amount(self):
        total = self.get_total_price()
        if self.salon.deposit_percentage <= 0:
            return 0
        return (total * self.salon.deposit_percentage) / 100

    def has_deposit(self):
        return self.payment_choice == 'deposit' and self.payment_required_amount > 0

    def is_full_payment(self):
        return self.payment_choice == 'full' and self.payment_required_amount > 0
    
    def requires_payment(self):
        return self.payment_choice != 'none' and self.payment_required_amount > 0

    def is_payment_expired(self):
        if not self.requires_payment():
            return False
        if self.payment_status == 'verified':
            return False
        if not self.payment_expires_at:
            return False
        return timezone.now() > self.payment_expires_at

    def is_blocking_slot(self):
        if self.status in ['confirmed', 'completed']:
            return True

        if self.status == 'pending':
            if not self.requires_payment():
                return True
            return not self.is_payment_expired()

        return False

    def get_booking_date(self):
        first_item = self.items.order_by('start_datetime').first()
        return first_item.start_datetime.date() if first_item else None

    def __str__(self):
        booking_date = self.get_booking_date()
        booking_date_text = booking_date.strftime('%d/%m/%Y') if booking_date else 'Sin fecha'
        return f"{self.customer_name} - {self.salon.name} - {booking_date_text}"
    
    def save(self, *args, **kwargs):
        if not self.client_manage_token:
            self.client_manage_token = uuid.uuid4()
        super().save(*args, **kwargs)


    def get_first_item(self):
        return self.items.order_by('start_datetime').first()


    def get_start_datetime(self):
        first_item = self.get_first_item()
        return first_item.start_datetime if first_item else None


    def get_client_cancellation_deadline(self):
        start_datetime = self.get_start_datetime()
        if not start_datetime:
            return None

        limit_hours = self.salon.cancellation_limit_hours or 0
        return start_datetime - timedelta(hours=limit_hours)


    def can_be_cancelled_by_client(self):
        if not self.salon.allow_client_cancellation:
            return False

        if self.status not in ['pending', 'confirmed']:
            return False

        start_datetime = self.get_start_datetime()
        if not start_datetime:
            return False

        deadline = self.get_client_cancellation_deadline()
        if not deadline:
            return False

        return timezone.now() <= deadline


    def get_client_cancellation_block_reason(self):
        if not self.salon.allow_client_cancellation:
            return "Esta peluquería no permite cancelaciones online."

        if self.status == 'cancelled':
            return "Este turno ya fue cancelado."

        if self.status == 'completed':
            return "Este turno ya fue finalizado."

        if self.status == 'expired':
            return "Esta reserva expiró."

        if self.status not in ['pending', 'confirmed']:
            return "Este turno no puede cancelarse online."

        deadline = self.get_client_cancellation_deadline()
        if deadline and timezone.now() > deadline:
            hours = self.salon.cancellation_limit_hours
            return f"Este turno ya no puede cancelarse online porque superó el límite de {hours} horas antes del horario reservado."

        return ""
    
    def get_client_reschedule_deadline(self):
        start_datetime = self.get_start_datetime()
        if not start_datetime:
            return None

        limit_hours = self.salon.reschedule_limit_hours or 0
        return start_datetime - timedelta(hours=limit_hours)


    def can_be_rescheduled_by_client(self):
        if not self.salon.allow_client_reschedule:
            return False

        if self.status not in ['pending', 'confirmed']:
            return False

        start_datetime = self.get_start_datetime()
        if not start_datetime:
            return False

        deadline = self.get_client_reschedule_deadline()
        if not deadline:
            return False

        return timezone.now() <= deadline


    def get_client_reschedule_block_reason(self):
        if not self.salon.allow_client_reschedule:
            return "Esta peluquería no permite modificaciones online."

        if self.status == 'cancelled':
            return "Este turno fue cancelado y no puede modificarse."

        if self.status == 'completed':
            return "Este turno ya fue finalizado."

        if self.status == 'expired':
            return "Esta reserva expiró."

        if self.status not in ['pending', 'confirmed']:
            return "Este turno no puede modificarse online."

        deadline = self.get_client_reschedule_deadline()
        if deadline and timezone.now() > deadline:
            hours = self.salon.reschedule_limit_hours
            return f"Este turno ya no puede modificarse online porque superó el límite de {hours} horas antes del horario reservado."

        return ""
    

class BookingItem(models.Model):
    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name='Reserva'
    )
    service = models.ForeignKey(
        Service,
        on_delete=models.PROTECT,
        related_name='booking_items',
        verbose_name='Servicio'
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name='booking_items',
        verbose_name='Profesional'
    )
    start_datetime = models.DateTimeField(verbose_name='Inicio')
    end_datetime = models.DateTimeField(verbose_name='Fin')
    order = models.PositiveIntegerField(default=0, verbose_name='Orden')

    class Meta:
        ordering = ['order', 'start_datetime']
        verbose_name = 'Bloque de trabajo'
        verbose_name_plural = 'Bloques de trabajo'

    def clean(self):
        errors = []

        if not self.booking_id:
            errors.append("El bloque debe pertenecer a una reserva.")

        if not self.service_id:
            errors.append("Debes seleccionar un servicio.")

        if not self.employee_id:
            errors.append("Debes seleccionar un profesional.")

        if not self.start_datetime or not self.end_datetime:
            errors.append("Debes indicar inicio y fin del bloque.")

        if errors:
            raise ValidationError(errors)

        overlapping_appointments = Appointment.objects.filter(
            employee=self.employee,
            appointment_datetime__date=self.start_datetime.date(),
        ).exclude(status='cancelled')

        for appointment in overlapping_appointments:
            appointment_start = appointment.appointment_datetime
            appointment_end = appointment_start + timedelta(
                minutes=appointment.get_total_duration_minutes()
            )

            overlaps = (
                self.start_datetime < appointment_end and
                self.end_datetime > appointment_start
            )

            if overlaps:
                errors.append(
                    f"{self.employee.name} ya tiene otro turno que se superpone con ese horario."
                )
                break

        if self.employee.salon_id != self.booking.salon_id:
            errors.append("El profesional no pertenece a la peluquería de la reserva.")

        if self.service.salon_id != self.booking.salon_id:
            errors.append("El servicio no pertenece a la peluquería de la reserva.")

        if not self.employee.services.filter(pk=self.service_id).exists():
            errors.append(f"{self.employee.name} no realiza {self.service.name}.")

        if self.end_datetime <= self.start_datetime:
            errors.append("La hora de fin debe ser posterior a la hora de inicio.")

        start_date = timezone.localtime(self.start_datetime).date()
        end_date = timezone.localtime(self.end_datetime).date()
        if start_date != end_date:
            errors.append("Cada bloque debe comenzar y terminar el mismo día.")

        weekday = self.start_datetime.weekday()
        business_hours = BusinessHours.objects.filter(
            salon=self.booking.salon,
            weekday=weekday
        ).first()

        if not business_hours:
            errors.append("No hay horario configurado para ese día.")
        elif business_hours.is_closed:
            errors.append("La peluquería no atiende ese día.")
        else:
            start_time = timezone.localtime(self.start_datetime).time()
            end_time = timezone.localtime(self.end_datetime).time()

            if start_time < business_hours.start_time or start_time >= business_hours.end_time:
                errors.append("El bloque comienza fuera del horario de atención.")

            if end_time > business_hours.end_time:
                errors.append("El bloque termina fuera del horario de atención.")

        overlapping_items = BookingItem.objects.select_related('booking').filter(
            employee=self.employee,
            start_datetime__date=self.start_datetime.date(),
        ).exclude(pk=self.pk).exclude(
            booking__status__in=['cancelled', 'expired']
        )

        for item in overlapping_items:
            if not item.booking.is_blocking_slot():
                continue

            overlaps = (
                self.start_datetime < item.end_datetime and
                self.end_datetime > item.start_datetime
            )

            if overlaps:
                errors.append(
                    f"{self.employee.name} ya tiene otro turno que se superpone con ese horario."
                )
                break

        if self.booking_id:
            sibling_items = BookingItem.objects.filter(
                booking=self.booking
            ).exclude(pk=self.pk)

            for item in sibling_items:
                same_day = item.start_datetime.date() == self.start_datetime.date()
                if not same_day:
                    errors.append("Todos los servicios de una misma reserva deben ser el mismo día.")
                    break

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.booking.customer_name} - {self.service.name} - {self.employee.name}"
    
class EmployeeTimeOff(models.Model):
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='time_off_blocks',
        verbose_name='Profesional'
    )
    start_datetime = models.DateTimeField('Desde')
    end_datetime = models.DateTimeField('Hasta')
    reason = models.CharField('Motivo', max_length=150, blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_time_off_blocks',
        verbose_name='Creado por'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Bloqueo de horario'
        verbose_name_plural = 'Bloqueos de horario'
        ordering = ['start_datetime']

    def clean(self):
        errors = []

        if self.end_datetime and self.start_datetime and self.end_datetime <= self.start_datetime:
            errors.append("La fecha/hora de fin debe ser posterior a la de inicio.")

        if self.employee_id and self.start_datetime and self.end_datetime:
            overlaps = EmployeeTimeOff.objects.filter(
                employee=self.employee,
                start_datetime__lt=self.end_datetime,
                end_datetime__gt=self.start_datetime,
            ).exclude(pk=self.pk)

            if overlaps.exists():
                errors.append("Este bloqueo se superpone con otro bloqueo del profesional.")

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.employee.name}: {self.start_datetime} - {self.end_datetime}"