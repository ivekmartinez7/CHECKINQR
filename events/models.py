import uuid
import random
from django.db import models
from django.core.exceptions import ValidationError
from django.conf import settings

class GuestRole(models.Model):
    name = models.CharField(max_length=100, unique=True, verbose_name="Nombre del Rol")

    def __str__(self):
        return self.name

class ChatMessage(models.Model):
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, verbose_name="Remitente")
    text = models.TextField(blank=True, null=True, verbose_name="Texto del Mensaje")
    audio_file = models.FileField(upload_to='chat_audios/', blank=True, null=True, verbose_name="Archivo de Audio")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Enviado el")

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.sender.username}: {self.text[:20] if self.text else 'Nota de Audio'}"

class Guest(models.Model):
    name = models.CharField(max_length=255, verbose_name="Nombre")
    email = models.EmailField(blank=True, null=True, verbose_name="Correo Electrónico")
    phone = models.CharField(max_length=20, blank=True, null=True, verbose_name="Teléfono")
    passes = models.IntegerField(default=1, verbose_name="Cantidad de Pases")
    role = models.ForeignKey(GuestRole, on_delete=models.PROTECT, null=True, blank=True, verbose_name="Rol/Etiqueta")
    school = models.CharField(max_length=255, blank=True, null=True, verbose_name="Escuela/Servicio")
    raffle_number = models.IntegerField(unique=True, blank=True, null=True, verbose_name="Número de Rifa")
    
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    is_checked_in = models.BooleanField(default=False, verbose_name="¿Asistió?")
    checked_in_at = models.DateTimeField(blank=True, null=True, verbose_name="Fecha de Ingreso")
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
        role_name = self.role.name if self.role else "Sin Rol"
        return f"{self.name} - {role_name} (#{self.raffle_number})"
