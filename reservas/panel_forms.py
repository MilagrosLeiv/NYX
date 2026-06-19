from decimal import Decimal, InvalidOperation
from datetime import datetime, time, timedelta

from django import forms
from django.utils import timezone
from django.contrib.auth.models import User
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth import get_user_model


from .models import (
    BusinessHourBlock,
    BusinessHours,
    Employee,
    EmployeeTimeOff,
    EmployeeWorkingHour,
    Salon,
    Service,
    ServiceCategory,
    SpecialAvailabilityBlock,
)
from .utils import get_available_slots

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
            'autocomplete': 'off',
        })
    )

    class Meta:
        model = Service
        fields = ['category', 'name', 'price', 'duration_minutes', 'description', 'is_active']
        widgets = {
            'category': forms.Select(attrs={
                'class': 'form-select nyx-form-input',
            }),
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
        salon = kwargs.pop('salon', None)
        super().__init__(*args, **kwargs)

        self.fields['category'].required = False
        self.fields['category'].empty_label = "Sin categoría"

        if salon:
            self.fields['category'].queryset = ServiceCategory.objects.filter(
                salon=salon,
                is_active=True,
            ).order_by('order', 'name')
        else:
            self.fields['category'].queryset = ServiceCategory.objects.none()

        if self.instance and self.instance.pk and self.instance.price is not None:
            price_int = int(self.instance.price)
            self.initial['price'] = f"{price_int:,}".replace(",", ".")

    def clean_price(self):
        raw_price = str(self.cleaned_data.get('price', '')).strip()

        if not raw_price:
            raise forms.ValidationError('Ingresá un precio.')

        raw_price = (
            raw_price
            .replace('$', '')
            .replace(' ', '')
        )

        try:
            # Caso argentino con coma decimal: 10.000,50
            if ',' in raw_price:
                normalized = raw_price.replace('.', '').replace(',', '.')
                value = Decimal(normalized)

            # Caso decimal que puede venir del modelo/input: 10000.00
            elif raw_price.count('.') == 1 and len(raw_price.split('.')[-1]) == 2:
                value = Decimal(raw_price)

            # Caso miles argentino: 10.000 / 100.000 / 1.000.000
            else:
                normalized = raw_price.replace('.', '')
                value = Decimal(normalized)

        except (InvalidOperation, ValueError):
            raise forms.ValidationError('Ingresá un precio válido.')

        if value < 0:
            raise forms.ValidationError('El precio no puede ser negativo.')

        return value

class PanelServiceCategoryForm(forms.ModelForm):
    def __init__(self, *args, salon=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.salon = salon

    class Meta:
        model = ServiceCategory
        fields = ["name", "description", "image", "order", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-control nyx-form-input",
                "placeholder": "Ej. Cabello, Manos, Pestañas",
            }),
            "description": forms.Textarea(attrs={
                "class": "form-control nyx-form-input",
                "rows": 3,
                "placeholder": "Descripción opcional para mostrar en la página pública",
            }),
            "image": forms.ClearableFileInput(attrs={
                "class": "form-control nyx-form-input",
            }),
            "order": forms.NumberInput(attrs={
                "class": "form-control nyx-form-input",
                "min": "0",
                "placeholder": "Ej. 1",
            }),
            "is_active": forms.CheckboxInput(attrs={
                "class": "form-check-input",
            }),
        }

    def clean_name(self):
        name = self.cleaned_data["name"].strip()

        if self.salon:
            duplicate = ServiceCategory.objects.filter(
                salon=self.salon,
                name__iexact=name,
            )
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)

            if duplicate.exists():
                raise forms.ValidationError(
                    "Ya existe una categor\u00eda con ese nombre en tu sal\u00f3n."
                )

        return name

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
    is_closed = forms.BooleanField(
        label="Día cerrado",
        required=False,
        widget=forms.CheckboxInput(attrs={
            "class": "form-check-input",
        })
    )

    start_time = forms.TypedChoiceField(
        label="Hora de inicio",
        choices=TIME_CHOICES_30,
        coerce=parse_time_choice,
        empty_value=None,
        required=False,
        widget=forms.Select(attrs={
            "class": "form-select nyx-form-input",
        })
    )

    end_time = forms.TypedChoiceField(
        label="Hora de fin",
        choices=TIME_CHOICES_30,
        coerce=parse_time_choice,
        empty_value=None,
        required=False,
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
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            self.initial["start_time"] = format_time_for_select(self.instance.start_time)
            self.initial["end_time"] = format_time_for_select(self.instance.end_time)
            self.initial["is_closed"] = self.instance.is_closed

    def clean(self):
        cleaned_data = super().clean()

        is_closed = cleaned_data.get('is_closed')
        start_time = cleaned_data.get('start_time')
        end_time = cleaned_data.get('end_time')

        if is_closed:
            # Aunque esté cerrado, mantenemos horarios válidos porque el modelo los requiere.
            if not start_time and self.instance and self.instance.pk:
                cleaned_data['start_time'] = self.instance.start_time

            if not end_time and self.instance and self.instance.pk:
                cleaned_data['end_time'] = self.instance.end_time

            if not cleaned_data.get('start_time'):
                cleaned_data['start_time'] = parse_time_choice("09:00")

            if not cleaned_data.get('end_time'):
                cleaned_data['end_time'] = parse_time_choice("20:00")

            return cleaned_data

        if not start_time or not end_time:
            raise forms.ValidationError(
                'Debés indicar horario de inicio y fin, o marcar el día como cerrado.'
            )

        if start_time >= end_time:
            raise forms.ValidationError(
                'La hora de inicio debe ser menor que la de fin.'
            )

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)

        instance.is_closed = self.cleaned_data.get('is_closed', False)
        instance.start_time = self.cleaned_data.get('start_time')
        instance.end_time = self.cleaned_data.get('end_time')

        if commit:
            instance.save()

        return instance
    
class PanelBusinessHourBlockForm(forms.ModelForm):
    weekdays = forms.MultipleChoiceField(
        label="Días",
        choices=BusinessHourBlock.WEEKDAY_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

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
        self.is_multiple_create = not (self.instance and self.instance.pk)

        if self.is_multiple_create:
            self.fields.pop('weekday')
            self.fields['weekdays'].required = True
        else:
            self.fields.pop('weekdays')
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

        if self.is_multiple_create:
            weekdays = [int(value) for value in cleaned_data.get('weekdays', [])]
        else:
            weekdays = [weekday] if weekday is not None else []

        duplicate_days = []
        conflict_days = []
        weekday_names = dict(BusinessHourBlock.WEEKDAY_CHOICES)

        for selected_weekday in weekdays:
            blocks = BusinessHourBlock.objects.filter(
                salon=self.salon,
                weekday=selected_weekday,
            )

            if self.instance and self.instance.pk:
                blocks = blocks.exclude(pk=self.instance.pk)

            if blocks.filter(
                start_time=start_time,
                end_time=end_time,
            ).exists():
                duplicate_days.append(weekday_names[selected_weekday])
                continue

            if blocks.filter(
                is_active=True,
                start_time__lt=end_time,
                end_time__gt=start_time,
            ).exists():
                conflict_days.append(weekday_names[selected_weekday])

        validation_errors = []
        if duplicate_days:
            validation_errors.append(
                f"Ya existe una franja idéntica para: {', '.join(duplicate_days)}."
            )
        if conflict_days:
            validation_errors.append(
                "La franja se superpone con horarios activos para: "
                f"{', '.join(conflict_days)}."
            )
        if validation_errors:
            raise forms.ValidationError(validation_errors)

        cleaned_data['weekdays'] = weekdays
        return cleaned_data


class PanelEmployeeWorkingHourForm(forms.ModelForm):
    weekdays = forms.MultipleChoiceField(
        label="Días",
        choices=EmployeeWorkingHour.WEEKDAY_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

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
        model = EmployeeWorkingHour
        fields = ['weekday', 'start_time', 'end_time', 'is_active']
        widgets = {
            'weekday': forms.Select(attrs={
                'class': 'form-select nyx-form-input',
            }),
            'is_active': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
        }

    def __init__(self, *args, employee, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee
        self.is_multiple_create = not (self.instance and self.instance.pk)

        if self.is_multiple_create:
            self.fields.pop('weekday')
            self.fields['weekdays'].required = True
        else:
            self.fields.pop('weekdays')
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

        if self.is_multiple_create:
            weekdays = [int(value) for value in cleaned_data.get('weekdays', [])]
        else:
            weekdays = [weekday] if weekday is not None else []

        duplicate_days = []
        conflict_days = []
        weekday_names = dict(EmployeeWorkingHour.WEEKDAY_CHOICES)

        for selected_weekday in weekdays:
            blocks = EmployeeWorkingHour.objects.filter(
                employee=self.employee,
                weekday=selected_weekday,
            )

            if self.instance and self.instance.pk:
                blocks = blocks.exclude(pk=self.instance.pk)

            if blocks.filter(
                start_time=start_time,
                end_time=end_time,
            ).exists():
                duplicate_days.append(weekday_names[selected_weekday])
                continue

            if is_active and blocks.filter(
                is_active=True,
                start_time__lt=end_time,
                end_time__gt=start_time,
            ).exists():
                conflict_days.append(weekday_names[selected_weekday])

        validation_errors = []
        if duplicate_days:
            validation_errors.append(
                f"Ya existe una franja idéntica para: {', '.join(duplicate_days)}."
            )
        if conflict_days:
            validation_errors.append(
                "La franja se superpone con horarios activos para: "
                f"{', '.join(conflict_days)}."
            )
        if validation_errors:
            raise forms.ValidationError(validation_errors)

        cleaned_data['weekdays'] = weekdays
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.employee = self.employee

        if commit:
            instance.full_clean()
            instance.save()

        return instance


class SpecialAvailabilityBlockForm(forms.ModelForm):
    scope = forms.ChoiceField(
        label='¿A quién afecta?',
        choices=[
            ('salon', 'Todo el salón'),
            ('employee', 'Un profesional específico'),
        ],
        widget=forms.RadioSelect(attrs={
            'class': 'form-check-input',
            'data-block-scope': 'true',
        }),
        initial='salon',
    )
    reason = forms.ChoiceField(
        label='Motivo',
        choices=[
            ('holiday', 'Feriado'),
            ('vacation', 'Vacaciones'),
            ('personal', 'Ausencia'),
            ('training', 'Capacitación'),
            ('special_closure', 'Cierre especial'),
            ('other', 'Otro'),
        ],
        widget=forms.Select(attrs={
            'class': 'form-select nyx-input',
        }),
    )
    duration_type = forms.ChoiceField(
        label='Duración del bloqueo',
        choices=[
            ('all_day', 'Todo el día'),
            ('timed', 'Solo un horario'),
        ],
        widget=forms.RadioSelect(attrs={
            'class': 'form-check-input',
            'data-duration-type': 'true',
        }),
        initial='all_day',
    )

    start_date = forms.DateField(
        label='Fecha de inicio',
        widget=forms.DateInput(attrs={
            'type': 'date',
            'class': 'form-control nyx-input',
        }),
    )
    start_time = forms.TimeField(
        label='Hora de inicio',
        required=False,
        widget=forms.TimeInput(attrs={
            'type': 'time',
            'class': 'form-control nyx-input',
        }),
    )
    end_date = forms.DateField(
        label='Fecha de fin',
        required=False,
        widget=forms.DateInput(attrs={
            'type': 'date',
            'class': 'form-control nyx-input',
        }),
    )
    end_time = forms.TimeField(
        label='Hora de fin',
        required=False,
        widget=forms.TimeInput(attrs={
            'type': 'time',
            'class': 'form-control nyx-input',
        }),
    )

    class Meta:
        model = SpecialAvailabilityBlock
        fields = [
            'employee',
            'title',
            'show_in_agenda',
            'notes',
        ]
        widgets = {
            'title': forms.TextInput(attrs={
                'class': 'form-control nyx-input',
                'placeholder': 'Ej. Feriado, vacaciones o capacitación',
            }),
            'employee': forms.Select(attrs={
                'class': 'form-select nyx-input',
            }),
            'show_in_agenda': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
            'notes': forms.Textarea(attrs={
                'class': 'form-control nyx-input',
                'rows': 3,
                'placeholder': 'Información opcional para el equipo',
            }),
        }

    def __init__(self, *args, salon=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.salon = salon
        self.fields['title'].required = False
        self.fields['title'].label = 'Título personalizado (opcional)'
        self.fields['title'].widget.attrs['placeholder'] = (
            'Ej. Capacitación del equipo'
        )
        self.fields['employee'].required = False
        self.fields['employee'].label = 'Profesional'
        self.fields['employee'].empty_label = 'Seleccioná un profesional'
        self.fields['employee'].queryset = Employee.objects.filter(
            salon=salon,
            is_active=True,
        ).order_by('name')

        if self.instance and self.instance.pk:
            self.initial.setdefault(
                'scope',
                'employee' if self.instance.employee_id else 'salon',
            )
            self.initial.setdefault(
                'duration_type',
                'all_day' if self.instance.all_day else 'timed',
            )
            reason = self.instance.block_type
            if (
                reason == SpecialAvailabilityBlock.BlockType.SPECIAL_CLOSURE
                and self.instance.title.lower().startswith('capacit')
            ):
                reason = 'training'
            self.initial.setdefault('reason', reason)
            local_start = timezone.localtime(self.instance.start_datetime)
            local_end = timezone.localtime(self.instance.end_datetime)
            self.initial.setdefault('start_date', local_start.date())
            self.initial.setdefault('start_time', local_start.time())

            if self.instance.all_day:
                self.initial.setdefault(
                    'end_date',
                    (local_end - timedelta(days=1)).date(),
                )
            else:
                self.initial.setdefault('end_time', local_end.time())

    def clean_employee(self):
        employee = self.cleaned_data.get('employee')

        if employee and employee.salon_id != self.salon.id:
            raise forms.ValidationError(
                'Ese profesional no pertenece a este salón.'
            )
        return employee

    def clean(self):
        cleaned_data = super().clean()
        scope = cleaned_data.get('scope')
        employee = cleaned_data.get('employee')
        reason = cleaned_data.get('reason')
        duration_type = cleaned_data.get('duration_type')
        title = (cleaned_data.get('title') or '').strip()
        start_date = cleaned_data.get('start_date')
        end_date = cleaned_data.get('end_date')
        all_day = duration_type == 'all_day'
        cleaned_data['all_day'] = all_day

        if scope == 'employee' and not employee:
            self.add_error(
                'employee',
                'Seleccioná el profesional al que afecta el bloqueo.',
            )
        elif scope == 'salon':
            cleaned_data['employee'] = None

        if not title and reason:
            cleaned_data['title'] = dict(
                self.fields['reason'].choices
            ).get(reason, 'Bloqueo')

        if not start_date:
            return cleaned_data

        current_tz = timezone.get_current_timezone()

        if all_day:
            if not end_date:
                self.add_error('end_date', 'Indicá la fecha de fin.')
                return cleaned_data
            start_datetime = timezone.make_aware(
                datetime.combine(start_date, time.min),
                current_tz,
            )
            end_datetime = timezone.make_aware(
                datetime.combine(end_date + timedelta(days=1), time.min),
                current_tz,
            )
        else:
            start_time = cleaned_data.get('start_time')
            end_time = cleaned_data.get('end_time')

            if not start_time:
                self.add_error('start_time', 'Indicá la hora de inicio.')
            if not end_time:
                self.add_error('end_time', 'Indicá la hora de fin.')
            if not start_time or not end_time:
                return cleaned_data

            start_datetime = timezone.make_aware(
                datetime.combine(start_date, start_time),
                current_tz,
            )
            end_datetime = timezone.make_aware(
                datetime.combine(start_date, end_time),
                current_tz,
            )

        if end_datetime <= start_datetime:
            raise forms.ValidationError(
                'La fecha/hora de fin debe ser posterior a la de inicio.'
            )

        cleaned_data['start_datetime'] = start_datetime
        cleaned_data['end_datetime'] = end_datetime
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.salon = self.salon
        instance.employee = self.cleaned_data.get('employee')
        instance.all_day = self.cleaned_data['all_day']
        reason = self.cleaned_data['reason']
        instance.block_type = (
            SpecialAvailabilityBlock.BlockType.SPECIAL_CLOSURE
            if reason == 'training'
            else reason
        )
        instance.title = self.cleaned_data['title']
        instance.start_datetime = self.cleaned_data['start_datetime']
        instance.end_datetime = self.cleaned_data['end_datetime']

        if commit:
            instance.full_clean()
            instance.save()
        return instance


class ManualBookingForm(forms.Form):
    customer_name = forms.CharField(
        label='Nombre del cliente',
        max_length=100,
        widget=forms.TextInput(attrs={
            'class': 'form-control nyx-form-input',
            'placeholder': 'Ej. María López',
            'autocomplete': 'name',
        }),
    )
    customer_phone = forms.CharField(
        label='Teléfono',
        max_length=30,
        widget=forms.TextInput(attrs={
            'class': 'form-control nyx-form-input',
            'placeholder': 'Ej. 3415555555',
            'autocomplete': 'tel',
        }),
    )
    customer_email = forms.EmailField(
        label='Email (opcional)',
        required=False,
        widget=forms.EmailInput(attrs={
            'class': 'form-control nyx-form-input',
            'placeholder': 'cliente@email.com',
            'autocomplete': 'email',
        }),
    )
    employee = forms.ModelChoiceField(
        label='Profesional',
        queryset=Employee.objects.none(),
        widget=forms.Select(attrs={
            'class': 'form-select nyx-form-input',
        }),
    )
    service = forms.ModelChoiceField(
        label='Servicio',
        queryset=Service.objects.none(),
        error_messages={
            'invalid_choice': (
                'Ese servicio no está asignado al profesional seleccionado.'
            ),
        },
        widget=forms.Select(attrs={
            'class': 'form-select nyx-form-input',
        }),
    )
    appointment_date = forms.DateField(
        label='Fecha',
        widget=forms.DateInput(attrs={
            'type': 'date',
            'class': 'form-control nyx-form-input',
        }),
    )
    appointment_time = forms.TypedChoiceField(
        label='Hora',
        choices=TIME_CHOICES_30,
        coerce=parse_time_choice,
        empty_value=None,
        widget=forms.Select(attrs={
            'class': 'form-select nyx-form-input',
        }),
    )
    notes = forms.CharField(
        label='Notas internas (opcional)',
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-control nyx-form-input',
            'rows': 3,
            'placeholder': 'Información útil para el equipo',
        }),
    )

    def __init__(self, *args, salon=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.salon = salon
        self.fields['employee'].queryset = Employee.objects.filter(
            salon=salon,
            is_active=True,
        ).order_by('name')
        self.fields['service'].queryset = Service.objects.none()

        employee_id = None
        if self.is_bound:
            employee_id = self.data.get('employee')
        elif self.initial.get('employee'):
            employee_id = getattr(
                self.initial['employee'],
                'pk',
                self.initial['employee'],
            )

        if employee_id:
            self.fields['service'].queryset = Service.objects.filter(
                salon=salon,
                is_active=True,
                employees__id=employee_id,
                employees__salon=salon,
            ).distinct().order_by('name')

    def clean(self):
        cleaned_data = super().clean()
        employee = cleaned_data.get('employee')
        service = cleaned_data.get('service')
        appointment_date = cleaned_data.get('appointment_date')
        appointment_time = cleaned_data.get('appointment_time')

        if not all([employee, service, appointment_date, appointment_time]):
            return cleaned_data

        if employee.salon_id != self.salon.id:
            self.add_error('employee', 'Ese profesional no pertenece al salón.')
        if service.salon_id != self.salon.id:
            self.add_error('service', 'Ese servicio no pertenece al salón.')
        if not employee.services.filter(pk=service.pk).exists():
            self.add_error(
                'service',
                f'{employee.name} no tiene asignado ese servicio.',
            )

        start_datetime = timezone.make_aware(
            datetime.combine(appointment_date, appointment_time),
            timezone.get_current_timezone(),
        )
        if start_datetime < timezone.now():
            self.add_error(
                'appointment_time',
                'No podés cargar un turno en una fecha u hora pasada.',
            )

        if self.errors:
            return cleaned_data

        available_slots = get_available_slots(
            employee,
            [service],
            appointment_date,
        )
        selected_slot = appointment_time.strftime('%H:%M')

        if selected_slot not in available_slots:
            raise forms.ValidationError(
                'Ese horario no está disponible. Revisá los horarios, '
                'turnos existentes o bloqueos del profesional.'
            )

        cleaned_data['start_datetime'] = start_datetime
        cleaned_data['end_datetime'] = start_datetime + timedelta(
            minutes=service.duration_minutes,
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
            'public_description',
            'instagram_url',
            'logo',
            'cover_image',
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
                'placeholder': 'Nombre del negocio',
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

            'public_description': forms.Textarea(attrs={
                'class': 'form-control nyx-form-input',
                'rows': 4,
                'placeholder': 'Contá brevemente qué ofrece tu negocio, tu estilo de atención o qué te diferencia.',
            }),
            'instagram_url': forms.URLInput(attrs={
                'class': 'form-control nyx-form-input',
                'placeholder': 'https://www.instagram.com/tuusuario',
            }),
            'logo': forms.ClearableFileInput(attrs={
                'class': 'form-control nyx-form-input',
            }),
            'cover_image': forms.ClearableFileInput(attrs={
                'class': 'form-control nyx-form-input',
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
                'placeholder': 'Ej. Nombre del negocio o titular',
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
    
class TrialSignupForm(forms.Form):
    salon_name = forms.CharField(
        label="Nombre del negocio",
        max_length=120,
        widget=forms.TextInput(attrs={
            "class": "form-control nyx-input",
            "placeholder": "Ej. Lux Salon",
        })
    )

    owner_name = forms.CharField(
        label="Tu nombre",
        max_length=120,
        widget=forms.TextInput(attrs={
            "class": "form-control nyx-input",
            "placeholder": "Ej. Camila",
        })
    )

    owner_works = forms.BooleanField(
        label="Yo también atiendo turnos en el negocio",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={
            "class": "form-check-input",
        })
    )

    phone = forms.CharField(
        label="WhatsApp",
        max_length=30,
        widget=forms.TextInput(attrs={
            "class": "form-control nyx-input",
            "placeholder": "Ej. 3364123456",
        })
    )

    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={
            "class": "form-control nyx-input",
            "placeholder": "tu@email.com",
        })
    )

    username = forms.CharField(
        label="Usuario",
        max_length=150,
        widget=forms.TextInput(attrs={
            "class": "form-control nyx-input",
            "placeholder": "Ej. luxsalon",
        })
    )

    password = forms.CharField(
        label="Contraseña",
        min_length=8,
        widget=forms.PasswordInput(attrs={
            "class": "form-control nyx-input",
            "placeholder": "Mínimo 8 caracteres",
        })
    )

    password_confirm = forms.CharField(
        label="Confirmar contraseña",
        min_length=8,
        widget=forms.PasswordInput(attrs={
            "class": "form-control nyx-input",
            "placeholder": "Repetí tu contraseña",
        })
    )

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()

        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Ya existe una cuenta con ese email.")

        return email

    def clean_username(self):
        username = self.cleaned_data["username"].strip()

        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Ya existe una cuenta con ese usuario.")

        return username

    def clean_salon_name(self):
        salon_name = self.cleaned_data["salon_name"].strip()

        if Salon.objects.filter(name__iexact=salon_name).exists():
            raise forms.ValidationError("Ya existe un negocio registrado con ese nombre.")

        return salon_name

    def clean(self):
        cleaned_data = super().clean()

        password = cleaned_data.get("password")
        password_confirm = cleaned_data.get("password_confirm")

        if password and password_confirm and password != password_confirm:
            raise forms.ValidationError("Las contraseñas no coinciden.")

        return cleaned_data
