from datetime import datetime

from django.utils import timezone
from django import forms
from django.core.exceptions import ValidationError

from .models import Appointment, Employee, Service, Salon


class AppointmentForm(forms.ModelForm):
    services = forms.ModelMultipleChoiceField(
        queryset=Service.objects.filter(is_active=True).order_by('name'),
        widget=forms.CheckboxSelectMultiple,
        label='Servicios',
        required=True
    )

    appointment_datetime = forms.DateTimeField(
        label='Fecha y hora',
        widget=forms.DateTimeInput(attrs={
            'type': 'datetime-local',
            'class': 'form-control nyx-input'
        })
    )

    class Meta:
        model = Appointment
        fields = [
            'customer_name',
            'customer_phone',
            'customer_email',
            'salon',
            'services',
            'employee',
            'appointment_datetime',
            'status',
            'notes',
        ]
        labels = {
            'customer_name': 'Nombre',
            'customer_phone': 'Teléfono',
            'customer_email': 'Email',
            'salon': 'Peluquería',
            'employee': 'Profesional',
            'notes': 'Notas',
            'status': 'Estado',
        }
        widgets = {
            'customer_name': forms.TextInput(attrs={
                'class': 'form-control nyx-input',
                'placeholder': 'Tu nombre completo',
                'autocomplete': 'name',
            }),
            'customer_phone': forms.TextInput(attrs={
                'class': 'form-control nyx-input',
                'placeholder': 'Tu teléfono',
                'autocomplete': 'tel',
            }),
            'customer_email': forms.EmailInput(attrs={
                'class': 'form-control nyx-input',
                'placeholder': 'tuemail@ejemplo.com',
                'autocomplete': 'email',
            }),
            'salon': forms.Select(attrs={
                'class': 'form-select nyx-input',
            }),
            'employee': forms.Select(attrs={
                'class': 'form-select nyx-input',
            }),
            'appointment_datetime': forms.DateTimeInput(
                attrs={
                    'type': 'datetime-local',
                    'class': 'form-control nyx-input'
                }
            ),
            'status': forms.Select(attrs={
                'class': 'form-select nyx-input',
            }),
            'notes': forms.Textarea(attrs={
                'class': 'form-control nyx-textarea',
                'rows': 4,
                'placeholder': 'Escribí una aclaración si hace falta',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields['employee'].queryset = Employee.objects.none()
        self.fields['employee'].empty_label = 'Seleccioná un profesional'
        self.fields['salon'].queryset = Salon.objects.filter(is_active=True).order_by('name')

        selected_salon_id = None
        selected_service_ids = []

        if self.is_bound:
            selected_salon_id = self.data.get('salon')
            selected_service_ids = self.data.getlist('services')
        elif self.instance.pk:
            selected_salon_id = self.instance.salon_id
            selected_service_ids = [str(service.id) for service in self.instance.get_selected_services()]
            self.initial['services'] = [int(service_id) for service_id in selected_service_ids]

        # Filtrar servicios por salón cuando exista
        if selected_salon_id:
            self.fields['services'].queryset = Service.objects.filter(
                salon_id=selected_salon_id,
                is_active=True
            ).order_by('name')
        else:
            self.fields['services'].queryset = Service.objects.filter(
                is_active=True
            ).order_by('name')

        # Filtrar empleados por salón cuando exista
        if selected_salon_id:
            self.fields['employee'].queryset = Employee.objects.filter(
                salon_id=selected_salon_id,
                is_active=True
            ).order_by('name')

            # Si además hay servicios elegidos, reducir a empleados que hagan todos esos servicios
            if selected_service_ids:
                employees = self.fields['employee'].queryset
                for service_id in selected_service_ids:
                    employees = employees.filter(services__id=service_id)

                self.fields['employee'].queryset = employees.distinct().order_by('name')

        # Reforzar clases NYX por si algún campo viene distinto
        self.fields['customer_name'].widget.attrs.update({
            'class': 'form-control nyx-input',
            'placeholder': 'Tu nombre completo',
            'autocomplete': 'name',
        })
        self.fields['customer_phone'].widget.attrs.update({
            'class': 'form-control nyx-input',
            'placeholder': 'Tu teléfono',
            'autocomplete': 'tel',
        })
        self.fields['customer_email'].widget.attrs.update({
            'class': 'form-control nyx-input',
            'placeholder': 'tuemail@ejemplo.com',
            'autocomplete': 'email',
        })
        self.fields['salon'].widget.attrs.update({
            'class': 'form-select nyx-input',
        })
        self.fields['employee'].widget.attrs.update({
            'class': 'form-select nyx-input',
        })
        self.fields['appointment_datetime'].widget.attrs.update({
            'class': 'form-control nyx-input',
            'type': 'datetime-local',
        })
        self.fields['status'].widget.attrs.update({
            'class': 'form-select nyx-input',
        })
        self.fields['notes'].widget.attrs.update({
            'class': 'form-control nyx-textarea',
            'rows': 4,
            'placeholder': 'Escribí una aclaración si hace falta',
        })

    def clean(self):
        cleaned_data = super().clean()

        salon = cleaned_data.get('salon')
        employee = cleaned_data.get('employee')
        services = cleaned_data.get('services')
        appointment_datetime = cleaned_data.get('appointment_datetime')

        if not services:
            return cleaned_data

        if salon:
            for selected_service in services:
                if selected_service.salon_id != salon.id:
                    self.add_error('services', f"El servicio '{selected_service.name}' no pertenece a la peluquería seleccionada.")

        if employee and salon and employee.salon_id != salon.id:
            self.add_error('employee', 'El profesional no pertenece a la peluquería seleccionada.')

        if employee:
            for selected_service in services:
                if not employee.services.filter(pk=selected_service.pk).exists():
                    self.add_error('employee', f"{employee.name} no realiza el servicio '{selected_service.name}'.")

        if self.errors:
            return cleaned_data

        instance = Appointment(
            customer_name=cleaned_data.get('customer_name'),
            customer_phone=cleaned_data.get('customer_phone'),
            customer_email=cleaned_data.get('customer_email'),
            salon=salon,
            employee=employee,
            appointment_datetime=appointment_datetime,
            status=cleaned_data.get('status') or (self.instance.status if self.instance.pk else 'pending'),
            notes=cleaned_data.get('notes', ''),
        )

        if self.instance.pk:
            instance.pk = self.instance.pk

        selected_services = list(services)
        instance._selected_services = selected_services
        instance.service = selected_services[0] if selected_services else None

        try:
            instance.clean()
        except ValidationError as e:
            self.add_error(None, e)

        return cleaned_data

    def save(self, commit=True):
        appointment = super().save(commit=False)

        selected_services = list(self.cleaned_data['services'])
        appointment.service = selected_services[0] if selected_services else None

        if commit:
            appointment.save()
            self.save_m2m()

        return appointment


class PublicAppointmentForm(forms.Form):
    customer_name = forms.CharField(
        label='Nombre',
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )

    customer_phone = forms.CharField(
        label='Teléfono',
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )

    customer_email = forms.EmailField(
        label='Email',
        required=False,
        widget=forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'nombre@ejemplo.com'})
    )

    services = forms.ModelMultipleChoiceField(
        queryset=Service.objects.filter(is_active=True).order_by('name'),
        widget=forms.CheckboxSelectMultiple,
        label='Servicios',
        required=True
    )

    employee = forms.ModelChoiceField(
        queryset=Employee.objects.none(),
        label='Profesional',
        required=True,
        widget=forms.Select(attrs={'class': 'form-select'})
    )

    date = forms.DateField(
        label='Fecha',
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'})
    )

    start_time = forms.ChoiceField(
        label='Horario disponible',
        choices=[],
        required=True,
        widget=forms.Select(attrs={'class': 'form-select'})
    )

    notes = forms.CharField(
        label='Notas',
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 4})
    )

    def __init__(self, *args, available_slots=None, **kwargs):
        super().__init__(*args, **kwargs)

        today = timezone.localdate()
        self.fields['date'].widget.attrs['min'] = today.isoformat()

        self.fields['employee'].queryset = Employee.objects.none()
        self.fields['employee'].empty_label = 'Seleccioná un profesional'
        self.no_common_employee = False
        self.single_employee = None

        selected_service_ids = []
        if self.is_bound:
            selected_service_ids = self.data.getlist('services')
        else:
            initial_services = self.initial.get('services')
            if initial_services:
                selected_service_ids = [str(service.pk) for service in initial_services]

        if selected_service_ids:
            employees = Employee.objects.filter(is_active=True)
            for service_id in selected_service_ids:
                employees = employees.filter(services__id=service_id)

            employees = employees.distinct().order_by('name')
            self.fields['employee'].queryset = employees

            employee_count = employees.count()

            if employee_count == 0:
                self.no_common_employee = True
            elif employee_count == 1:
                self.single_employee = employees.first()
                self.fields['employee'].initial = self.single_employee
                self.fields['employee'].required = False
                self.fields['employee'].widget = forms.HiddenInput()

        slot_choices = [('', 'Seleccioná un horario')]
        if available_slots:
            slot_choices += [(slot, slot) for slot in available_slots]
        self.fields['start_time'].choices = slot_choices

   
    def clean(self):
        cleaned_data = super().clean()

        services = cleaned_data.get('services')
        selected_date = cleaned_data.get('date')
        start_time = cleaned_data.get('start_time')
        employee = cleaned_data.get('employee')

        if selected_date and selected_date < timezone.localdate():
            self.add_error('date', 'No podés seleccionar una fecha pasada.')

        if not services:
            return cleaned_data

        if self.no_common_employee:
            raise forms.ValidationError(
                "Los servicios seleccionados no pueden reservarse juntos con un único profesional. "
                "Reservalos por separado."
            )

        if self.single_employee:
            employee = self.single_employee
            cleaned_data['employee'] = employee

        if not employee or not selected_date or not start_time:
            return cleaned_data
        
        if selected_date == timezone.localdate():
            now = timezone.localtime().replace(second=0, microsecond=0)

            selected_datetime = timezone.make_aware(
                datetime.strptime(f"{selected_date} {start_time}", "%Y-%m-%d %H:%M"),
                timezone.get_current_timezone()
            )

            if selected_datetime <= now:
                self.add_error('start_time', 'No podés seleccionar un horario que ya pasó.')
                return cleaned_data

        for selected_service in services:
            if not employee.services.filter(pk=selected_service.pk).exists():
                self.add_error('employee', f"{employee.name} no realiza el servicio '{selected_service.name}'.")

        if self.errors:
            return cleaned_data

        appointment_datetime = timezone.make_aware(
            datetime.strptime(
                f"{selected_date} {start_time}",
                "%Y-%m-%d %H:%M"
            ),
            timezone.get_current_timezone()
        )

        instance = Appointment(
            customer_name=cleaned_data.get('customer_name'),
            customer_phone=cleaned_data.get('customer_phone'),
            employee=employee,
            appointment_datetime=appointment_datetime,
            status='pending',
            notes=cleaned_data.get('notes', ''),
        )

        selected_services = list(services)
        instance._selected_services = selected_services
        instance.service = selected_services[0] if selected_services else None

        try:
            instance.clean()
        except ValidationError as e:
            self.add_error(None, e)

        cleaned_data['appointment_datetime'] = appointment_datetime
        return cleaned_data
    
    def save(self):
        employee=self.cleaned_data['employee']

        appointment = Appointment.objects.create(
            salon=employee.salon,
            customer_name=self.cleaned_data['customer_name'],
            customer_phone=self.cleaned_data['customer_phone'],
            customer_email=self.cleaned_data.get('customer_email'),
            employee=employee,
            appointment_datetime=self.cleaned_data['appointment_datetime'],
            status='pending',
            notes=self.cleaned_data.get('notes', ''),
            service=list(self.cleaned_data['services'])[0],
        )  

        appointment.services.set(self.cleaned_data['services'])
        return appointment
    
class AppointmentConfirmForm(forms.Form):
    customer_name = forms.CharField(
        label='Nombre',
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )

    customer_email = forms.EmailField(
        label='Email',
        required=True,
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'nombre@ejemplo.com'
        })
    )

    customer_phone = forms.CharField(
        label='Teléfono',
        max_length=30,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )

    notes = forms.CharField(
        label='Notas',
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 4})
    )

