from django.urls import path
from django.contrib.auth import views as auth_views
from .panel_forms import NyxPasswordResetForm
from . import views, panel_views
from .views import mercadopago_oauth_connect, mercadopago_oauth_callback
urlpatterns = [
    path('', views.service_list, name='service_list'),

    path('reservar/profesional/', views.select_professional, name='select_professional'),
    path('reservar/horario/', views.select_time, name='select_time'),
    path('reservar/confirmar/', views.confirm_appointment, name='confirm_appointment'),
    path('turno-reservado/<int:appointment_id>/', views.booking_success, name='booking_success'),
    path('reservar/final/', views.confirm_booking, name='confirm_booking'),
    path('reserva/<int:booking_id>/pago/', views.booking_payment, name='booking_payment'),
    path('reserva-confirmada/<int:booking_id>/', views.booking_success_booking, name='booking_success_booking'),
    path('reservar/profesionales-por-servicio/', views.select_professionals_per_service, name='select_professionals_per_service'),
    path('webhooks/payments/', views.payment_webhook, name='payment_webhook'),
    path(
        'reserva/gestionar/<uuid:token>/',
        views.manage_booking,
        name='manage_booking'
    ),

    path(
        'reserva/cancelar/<uuid:token>/',
        views.cancel_booking,
        name='cancel_booking'
    ),
    path(
        'reserva/modificar/<uuid:token>/',
        views.reschedule_booking,
        name='reschedule_booking'
    ),

    # legado / transición
    path('reservar/', views.create_appointment, name='create_appointment'),
    path('reservar-por-separado/', views.create_split_appointments, name='create_split_appointments'),

    # APIs
    path('api/employees-by-services/', views.employees_by_services, name='employees_by_services'),
    path('api/employees-by-salon/', views.employees_by_salon, name='employees_by_salon'),
    path('api/services-by-salon/', views.services_by_salon, name='services_by_salon'),
    path('api/employees-by-salon-and-services/', views.employees_by_salon_and_services, name='employees_by_salon_and_services'),
    path('api/available-slots/', views.available_slots_api, name='available_slots_api'),

    # Panel de gestión
    path('panel/', panel_views.panel_dashboard, name='panel_dashboard'),
    path('panel/agenda/', panel_views.panel_agenda, name='panel_agenda'),
    path('panel/bloqueos/', panel_views.panel_bloqueos, name='panel_bloqueos'),
    path('login/', panel_views.panel_login, name='panel_login'),
    path('logout/', panel_views.panel_logout, name='panel_logout'),
    path(
        'password-reset/',
        auth_views.PasswordResetView.as_view(
            form_class=NyxPasswordResetForm,
            template_name='reservas/panel/password_reset_form.html',
            email_template_name='reservas/panel/password_reset_email.txt',
            subject_template_name='reservas/panel/password_reset_subject.txt',
            success_url='/password-reset/done/',
        ),
        name='password_reset'
    ),
    path(
        'password-reset/done/',
        auth_views.PasswordResetDoneView.as_view(
            template_name='reservas/panel/password_reset_done.html'
        ),
        name='password_reset_done'
    ),
    path(
        'reset/<uidb64>/<token>/',
        auth_views.PasswordResetConfirmView.as_view(
            template_name='reservas/panel/password_reset_confirm.html',
            success_url='/reset/done/',
        ),
        name='password_reset_confirm'
    ),
    path(
        'reset/done/',
        auth_views.PasswordResetCompleteView.as_view(
            template_name='reservas/panel/password_reset_complete.html'
        ),
        name='password_reset_complete'
    ),
    path('panel/servicios/', panel_views.panel_services, name='panel_services'),
    path('panel/servicios/nuevo/', panel_views.panel_service_create, name='panel_service_create'),
    path('panel/servicios/<int:service_id>/editar/', panel_views.panel_service_edit, name='panel_service_edit'),
    path('panel/servicios/<int:service_id>/toggle-activo/', panel_views.panel_service_toggle_active, name='panel_service_toggle_active'),
    path('panel/profesionales/', panel_views.panel_employees, name='panel_employees'),
    path('panel/profesionales/nuevo/', panel_views.panel_employee_create, name='panel_employee_create'),
    path('panel/profesionales/<int:employee_id>/editar/', panel_views.panel_employee_edit, name='panel_employee_edit'),
    path('panel/profesionales/<int:employee_id>/toggle-activo/', panel_views.panel_employee_toggle_active, name='panel_employee_toggle_active'),
    path(
        "panel/profesionales/<int:employee_id>/crear-acceso/",
        panel_views.panel_employee_create_access,
        name="panel_employee_create_access"
    ),
    path('panel/horarios/', panel_views.panel_business_hours, name='panel_business_hours'),
    path('panel/horarios/nuevo/', panel_views.panel_business_hours_create, name='panel_business_hours_create'),
    path(
        'panel/horarios/bloques/nuevo/',
        panel_views.panel_business_hour_block_create,
        name='panel_business_hour_block_create'
    ),
    path(
        'panel/horarios/bloques/<int:block_id>/editar/',
        panel_views.panel_business_hour_block_edit,
        name='panel_business_hour_block_edit'
    ),
    path(
        'panel/horarios/bloques/<int:block_id>/toggle-activo/',
        panel_views.panel_business_hour_block_toggle_active,
        name='panel_business_hour_block_toggle_active'
    ),
    path('panel/horarios/<int:business_hours_id>/editar/', panel_views.panel_business_hours_edit, name='panel_business_hours_edit'),
    path('panel/configuracion/', panel_views.panel_settings, name='panel_settings'),
    path('panel/reservas/', panel_views.panel_bookings, name='panel_bookings'),
    path('panel/reservas/<int:booking_id>/', panel_views.panel_booking_detail, name='panel_booking_detail'),
    path('panel/reservas/<int:booking_id>/cancelar/', panel_views.panel_booking_cancel, name='panel_booking_cancel'),
    path(
        'panel/reservas/<int:booking_id>/marcar-pago-verificado/',
        panel_views.panel_booking_mark_payment_verified,
        name='panel_booking_mark_payment_verified'
    ),
    path(
        "panel/invitacion/<uuid:token>/",
        panel_views.accept_staff_invitation,
        name="accept_staff_invitation"
    ),

    #PAGOS INTEGRADOS - MERCADOPAGO
    path(
        "pagos/mercadopago/oauth/connect/<int:salon_id>/",
        views.mercadopago_oauth_connect,
        name="mercadopago_oauth_connect"
    ),
    path(
        "pagos/mercadopago/oauth/callback/",
        views.mercadopago_oauth_callback,
        name="mercadopago_oauth_callback"
    ),
    path(
        "pagos/mercadopago/oauth/disconnect/<int:salon_id>/",
        views.mercadopago_oauth_disconnect,
        name="mercadopago_oauth_disconnect"
    ),
]