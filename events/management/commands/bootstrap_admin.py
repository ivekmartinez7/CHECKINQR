import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = 'Crea exclusivamente el primer administrador con variables privadas del servicio.'

    def handle(self, *args, **options):
        username = os.getenv('CHECKIN_ADMIN_USERNAME')
        password = os.getenv('CHECKIN_ADMIN_PASSWORD')
        if not username and not password:
            return
        if not username or not password:
            raise CommandError('Configura las dos variables CHECKIN_ADMIN o elimina ambas.')

        users = get_user_model().objects
        with transaction.atomic():
            if users.filter(is_superuser=True).exists():
                return
            user = get_user_model()(username=username, is_staff=True, is_superuser=True)
            try:
                user.full_clean(exclude=['password'])
                validate_password(password, user)
            except ValidationError:
                raise CommandError('Revisa los requisitos del usuario y la contraseña inicial.') from None
            user.set_password(password)
            user.save()
        self.stdout.write(self.style.SUCCESS('Primer administrador creado. Retira CHECKIN_ADMIN_PASSWORD del servicio.'))
