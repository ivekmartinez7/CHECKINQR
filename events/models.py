import uuid
import random
from django.db import models
from django.core.exceptions import ValidationError

class Guest(models.Model):
    ROLE_CHOICES = [
        ('Docente', 'Docente'),
        ('Acompañante', 'Acompañante'),
    ]

    name = models.CharField(max_length=255, verbose_name="Nombre")
    email = models.EmailField(blank=True, null=True, verbose_name="Correo Electrónico")
    phone = models.CharField(max_length=20, blank=True, null=True, verbose_name="Teléfono")
    passes = models.IntegerField(default=1, verbose_name="Cantidad de Pases")
    role = models.CharField(max_length=50, choices=ROLE_CHOICES, default='Docente', verbose_name="Rol")
    raffle_number = models.IntegerField(unique=True, blank=True, null=True, verbose_name="Número de Rifa")
    
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    is_checked_in = models.BooleanField(default=False, verbose_name="¿Asistió?")
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.raffle_number:
            assigned_numbers = set(Guest.objects.values_list('raffle_number', flat=True))
            available_numbers = list(set(range(1, 101)) - assigned_numbers)
            if not available_numbers:
                raise ValidationError("Ya no hay números de rifa disponibles del 1 al 100.")
            self.raffle_number = random.choice(available_numbers)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} - {self.role} (#{self.raffle_number})"
