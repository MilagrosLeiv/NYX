from django.apps import AppConfig


class ReservasConfig(AppConfig):
    name = 'reservas'

    def ready(self):
        from . import signals  # noqa: F401
