from decimal import Decimal, InvalidOperation
from datetime import time

from django import forms
from django.utils import timezone
from django.contrib.auth.models import User
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth import get_user_model


from .models import Service, Employee, BusinessHours,BusinessHourBlock, Salon,EmployeeTimeOff

def build_time_choices(start_hour=6, end_hour=23, step_minutes=30):
    choices = [("", "Seleccionar hora")]

    current_minutes = start_hour * 60
    end_minutes = end_hour * 60

    while current_minutes <= end_minutes:
        hour = current_minutes // 60
        minute = current_minutes % 60

        value = f"{hour:02d}:{minute:02d}"
        choices.append((value, value))

        current_minutes += step_minutes

    return choices


TIME_CHOICES_30 = build_time_choices()


def parse_time_choice(value):
    if value in (None, ""):
        return None

    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def format_time_for_select(value):
    if not value:
        return ""

    return value.strftime("%H:%M")

class PanelServiceForm(forms.ModelForm):
    price = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'form-control nyx-form-input nyx-price-input',
            'placeholder': 'Ej. 50.000',
            'inputmode': 'numeric',
        })
    )

    class Meta:
        model = Service
        fields = ['name', 'price', 'duration_minutes', 'description', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'Ej. Corte premium',
            }),
            'duration_minutes': forms.NumberInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'Ej. 60',
                'min': '1',
            }),
            'description': forms.Textarea(attrs={
                'class': 'form-control nyx-form-input',
                'rows': 4,
                'placeholder': 'Descripción opcional del servicio',
            }),
            'is_active': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk and self.instance.price is not None:
            price_int = int(self.instance.price)
            self.fields['price'].initial = f"{price_int:,}".replace(",", ".")

    def clean_price(self):
        raw_price = self.cleaned_data['price'].strip()

        normalized = raw_price.replace('.', '').replace(',', '.')

        try:
            value = Decimal(normalized)
        except (InvalidOperation, ValueError):
            raise forms.ValidationError('Ingresá un precio válido.')

        if value < 0:
            raise forms.ValidationError('El precio no puede ser negativo.')

        return value
    
from .models import Service, Employee


class PanelEmployeeForm(forms.ModelForm):
    services = forms.ModelMultipleChoiceField(
        queryset=Service.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label='Servicios que realiza'
    )

    class Meta:
        model = Employee
        fields = ['name', 'phone','email','notify_by_email', 'services', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'Ej. Lara',
            }),
            'phone': forms.TextInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'Ej. 3415555555',
            }),
            'email': forms.EmailInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'Ej. lara@nyx.com',
            }),
            'notify_by_email': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
            'is_active': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
        }

    def __init__(self, *args, salon=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.salon = salon

        if salon:
            self.fields['services'].queryset = Service.objects.filter(
                salon=salon
            ).order_by('name')

    def clean_services(self):
        services = self.cleaned_data.get('services')

        if self.salon:
            for service in services:
                if service.salon_id != self.salon.id:
                    raise forms.ValidationError(
                        f'El servicio "{service.name}" no pertenece a esta peluquería.'
                    )

        return services
    
class EmployeeTimeOffForm(forms.ModelForm):
    start_date = forms.DateField(
        label="Fecha",
        widget=forms.DateInput(attrs={
            "type": "date",
            "class": "form-control nyx-input",
        })
    )

    start_time = forms.TypedChoiceField(
        label="Hora inicio",
        choices=TIME_CHOICES_30,
        coerce=parse_time_choice,
        empty_value=None,
        widget=forms.Select(attrs={
            "class": "form-select nyx-input",
        })
    )

    end_time = forms.TypedChoiceField(
        label="Hora fin",
        choices=TIME_CHOICES_30,
        coerce=parse_time_choice,
        empty_value=None,
        widget=forms.Select(attrs={
            "class": "form-select nyx-input",
        })
    )
    class Meta:
        model = EmployeeTimeOff
        fields = ["employee", "reason"]
        widgets = {
            "employee": forms.Select(attrs={
                "class": "form-select nyx-input",
            }),
            "reason": forms.TextInput(attrs={
                "class": "form-control nyx-input",
                "placeholder": "Ej: almuerzo, trámite, ausencia, descanso",
            }),
        }

    def __init__(self, *args, salon=None, employee=None, is_owner=False, **kwargs):
        super().__init__(*args, **kwargs)

        self.salon = salon
        self.fixed_employee = employee
        self.is_owner = is_owner

        if is_owner:
            self.fields["employee"].queryset = Employee.objects.filter(
                salon=salon,
                is_active=True
            ).order_by("name")
            self.fields["employee"].label = "Profesional"
        else:
            self.fields["employee"].required = False
            self.fields["employee"].widget = forms.HiddenInput()

    def clean(self):
        cleaned_data = super().clean()

        start_date = cleaned_data.get("start_date")
        start_time = cleaned_data.get("start_time")
        end_time = cleaned_data.get("end_time")

        if self.is_owner:
            employee = cleaned_data.get("employee")
        else:
            employee = self.fixed_employee
            cleaned_data["employee"] = employee

        if not employee:
            raise forms.ValidationError("No encontramos el profesional asociado a tu usuario.")

        if employee.salon_id != self.salon.id:
            raise forms.ValidationError("Ese profesional no pertenece a esta peluquería.")

        if start_date and start_time and end_time:
            start_datetime = timezone.make_aware(
                timezone.datetime.combine(start_date, start_time),
                timezone.get_current_timezone()
            )
            end_datetime = timezone.make_aware(
                timezone.datetime.combine(start_date, end_time),
                timezone.get_current_timezone()
            )

            cleaned_data["start_datetime"] = start_datetime
            cleaned_data["end_datetime"] = end_datetime

            if end_datetime <= start_datetime:
                raise forms.ValidationError("La hora de fin debe ser posterior a la hora de inicio.")

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.employee = self.cleaned_data["employee"]
        instance.start_datetime = self.cleaned_data["start_datetime"]
        instance.end_datetime = self.cleaned_data["end_datetime"]

        if commit:
            instance.full_clean()
            instance.save()

        return instance
    
class PanelEmployeeAccessForm(forms.Form):
    username = forms.CharField(
        label="Usuario",
        max_length=150,
        widget=forms.TextInput(attrs={
            "class": "form-control nyx-form-input",
            "placeholder": "Ej. camila",
        })
    )

    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={
            "class": "form-control nyx-form-input",
            "placeholder": "Email del profesional",
        })
    )

    def clean_username(self):
        username = self.cleaned_data["username"].strip()

        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Ya existe un usuario con ese nombre.")

        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()

        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Ya existe un usuario con ese email.")

        return email
    

class AcceptStaffInvitationForm(forms.Form):
    password = forms.CharField(
        label="Contraseña",
        min_length=8,
        widget=forms.PasswordInput(attrs={
            "class": "form-control nyx-form-input",
            "placeholder": "Creá una contraseña",
        })
    )

    password_confirm = forms.CharField(
        label="Confirmar contraseña",
        min_length=8,
        widget=forms.PasswordInput(attrs={
            "class": "form-control nyx-form-input",
            "placeholder": "Repetí la contraseña",
        })
    )

    def clean(self):
        cleaned_data = super().clean()

        password = cleaned_data.get("password")
        password_confirm = cleaned_data.get("password_confirm")

        if password and password_confirm and password != password_confirm:
            raise forms.ValidationError("Las contraseñas no coinciden.")

        return cleaned_data
    

class NyxPasswordResetForm(PasswordResetForm):
    def get_users(self, email):
        UserModel = get_user_model()

        users = UserModel._default_manager.filter(
            email__iexact=email,
            is_active=True,
            salon_memberships__is_active=True,
        ).distinct()

        for user in users:
            if user.has_usable_password():
                yield user
    
class PanelBusinessHoursForm(forms.ModelForm):
    start_time = forms.TypedChoiceField(
        label="Hora de inicio",
        choices=TIME_CHOICES_30,
        coerce=parse_time_choice,
        empty_value=None,
        widget=forms.Select(attrs={
            "class": "form-select nyx-form-input",
        })
    )

    end_time = forms.TypedChoiceField(
        label="Hora de fin",
        choices=TIME_CHOICES_30,
        coerce=parse_time_choice,
        empty_value=None,
        widget=forms.Select(attrs={
            "class": "form-select nyx-form-input",
        })
    )

    class Meta:
        model = BusinessHours
        fields = ['weekday', 'start_time', 'end_time', 'is_closed']
        widgets = {
            'weekday': forms.Select(attrs={
                'class': 'form-select nyx-form-input',
            }),
            'is_closed': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            self.initial["start_time"] = format_time_for_select(self.instance.start_time)
            self.initial["end_time"] = format_time_for_select(self.instance.end_time)

    def clean(self):
        cleaned_data = super().clean()

        is_closed = cleaned_data.get('is_closed')
        start_time = cleaned_data.get('start_time')
        end_time = cleaned_data.get('end_time')

        if not is_closed:
            if not start_time or not end_time:
                raise forms.ValidationError(
                    'Debés indicar horario de inicio y fin, o marcar el día como cerrado.'
                )

            if start_time >= end_time:
                raise forms.ValidationError(
                    'La hora de inicio debe ser menor que la de fin.'
                )

        return cleaned_data
    
class PanelBusinessHourBlockForm(forms.ModelForm):
    start_time = forms.TypedChoiceField(
        label="Hora de inicio",
        choices=TIME_CHOICES_30,
        coerce=parse_time_choice,
        empty_value=None,
        widget=forms.Select(attrs={
            "class": "form-select nyx-form-input",
        })
    )

    end_time = forms.TypedChoiceField(
        label="Hora de fin",
        choices=TIME_CHOICES_30,
        coerce=parse_time_choice,
        empty_value=None,
        widget=forms.Select(attrs={
            "class": "form-select nyx-form-input",
        })
    )

    class Meta:
        model = BusinessHourBlock
        fields = ['weekday', 'start_time', 'end_time', 'is_active']
        widgets = {
            'weekday': forms.Select(attrs={
                'class': 'form-select nyx-form-input',
            }),
            'is_active': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
        }

    def __init__(self, *args, salon=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.salon = salon

        if self.instance and self.instance.pk:
            self.initial["start_time"] = format_time_for_select(self.instance.start_time)
            self.initial["end_time"] = format_time_for_select(self.instance.end_time)

    def clean(self):
        cleaned_data = super().clean()

        weekday = cleaned_data.get('weekday')
        start_time = cleaned_data.get('start_time')
        end_time = cleaned_data.get('end_time')
        is_active = cleaned_data.get('is_active')

        if not start_time or not end_time:
            raise forms.ValidationError("Debés indicar hora de inicio y hora de fin.")

        if start_time >= end_time:
            raise forms.ValidationError("La hora de inicio debe ser menor que la hora de fin.")

        if self.salon and weekday is not None and start_time and end_time and is_active:
            overlaps = BusinessHourBlock.objects.filter(
                salon=self.salon,
                weekday=weekday,
                is_active=True,
                start_time__lt=end_time,
                end_time__gt=start_time,
            )

            if self.instance and self.instance.pk:
                overlaps = overlaps.exclude(pk=self.instance.pk)

            if overlaps.exists():
                raise forms.ValidationError(
                    "Este bloque horario se superpone con otro bloque activo del mismo día."
                )

        return cleaned_data
    
class PanelSalonSettingsForm(forms.ModelForm):
    class Meta:
        model = Salon
        fields = [
            'name',
            'email',
            'notification_email',
            'notify_new_bookings_by_email',
            'phone',
            'address',
            'deposit_enabled',
            'deposit_percentage',
            'allow_full_payment',
            'full_payment_required',
            'payment_method',
            'transfer_account_holder',
            'transfer_alias',
            'transfer_cbu',
            'transfer_bank_name',
            'transfer_tax_id',
            'transfer_extra_instructions',
            'payment_instructions',
            'allow_client_cancellation',
            'cancellation_limit_hours',
            'allow_client_reschedule',
            'reschedule_limit_hours',
        ]
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'Nombre de la peluquería',
            }),
            'email': forms.EmailInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'Email de contacto',
            }),
            'notification_email': forms.EmailInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'Email donde querés recibir los avisos de nuevos turnos',
            }),
            'notify_new_bookings_by_email': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
            'phone': forms.TextInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'Teléfono',
            }),
            'address': forms.TextInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'Dirección',
            }),
            'deposit_enabled': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
            'allow_full_payment': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
            'full_payment_required': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),

            'allow_client_cancellation': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
            'allow_client_reschedule': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
            'payment_method': forms.Select(attrs={
                'class': 'form-select nyx-form-input',
            }),
            'deposit_percentage': forms.NumberInput(attrs={
                'class': 'form-control nyx-form-input',
                'min': '0',
                'max': '100',
                'placeholder': 'Ej. 30',
            }),
            'payment_instructions': forms.Textarea(attrs={
                'class': 'form-control nyx-form-input',
                'rows': 4,
                'placeholder': 'Ej. Una vez realizado el pago, enviá el comprobante por WhatsApp.',
            }),
            'cancellation_limit_hours': forms.NumberInput(attrs={
                'class': 'form-control nyx-form-input',
                'min': '0',
                'placeholder': 'Ej. 24',
            }),
            'reschedule_limit_hours': forms.NumberInput(attrs={
                'class': 'form-control nyx-form-input',
                'min': '0',
                'placeholder': 'Ej. 24',
            }),
            
            'transfer_account_holder': forms.TextInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'Ej. Lux Salon',
            }),
            'transfer_alias': forms.TextInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'Ej. lux.salon.mp',
            }),
            'transfer_cbu': forms.TextInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'CBU o CVU',
            }),
            'transfer_bank_name': forms.TextInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'Ej. Mercado Pago, Banco Galicia, etc.',
            }),
            'transfer_tax_id': forms.TextInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'CUIT/CUIL opcional',
            }),
            'transfer_extra_instructions': forms.Textarea(attrs={
                'class': 'form-control nyx-form-input',
                'rows': 3,
                'placeholder': 'Ej. Enviar comprobante por WhatsApp luego de transferir.',
            }),
        }

    def clean_deposit_percentage(self):
        value = self.cleaned_data.get('deposit_percentage', 0)

        if value < 0 or value > 100:
            raise forms.ValidationError('El porcentaje debe estar entre 0 y 100.')

        return value

    def clean(self):
        cleaned_data = super().clean()

        deposit_enabled = cleaned_data.get('deposit_enabled')
        percentage = cleaned_data.get('deposit_percentage')
        allow_full_payment = cleaned_data.get('allow_full_payment')
        full_payment_required = cleaned_data.get('full_payment_required')
        payment_method = cleaned_data.get('payment_method')

        payment_policy_active = (
            deposit_enabled
            or allow_full_payment
            or full_payment_required
        )

        if deposit_enabled and (percentage is None or percentage <= 0):
            raise forms.ValidationError(
                'Si marcás "Requerir seña", el porcentaje debe ser mayor que 0.'
            )

        if full_payment_required:
            cleaned_data['allow_full_payment'] = True
            payment_policy_active = True

        if not payment_policy_active:
            cleaned_data['payment_method'] = 'none'

        if payment_policy_active and payment_method == 'none':
            raise forms.ValidationError(
                'Si activás una política de pago, tenés que elegir Transferencia, Mercado Pago o ambas opciones.'
            )

        return cleaned_data