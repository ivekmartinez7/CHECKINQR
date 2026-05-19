import os
import django
import sys
from io import BytesIO
import qrcode
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from email.mime.image import MIMEImage
from django.conf import settings
from events.models import Guest

def test_email():
    if not settings.EMAIL_HOST_USER or not settings.EMAIL_HOST_PASSWORD:
        print("ERROR: EMAIL_HOST_USER or EMAIL_HOST_PASSWORD is not set in .env")
        return

    # Create a dummy guest in memory (no need to save to DB)
    guest = Guest(name="Usuario de Prueba", email=settings.EMAIL_HOST_USER, passes=2, raffle_number=99, role="Docente", phone="5551234567")
    # For UUID, just use a dummy string
    guest.uuid = "00000000-0000-0000-0000-000000000000"

    print(f"Intentando enviar correo de prueba a: {guest.email} usando cuenta {settings.EMAIL_HOST_USER}")

    try:
        # Generate QR Image
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(str(guest.uuid))
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        
        # Create Email
        subject = f"Prueba de Sistema - Confirmación de Registro"
        html_content = render_to_string('events/email_template.html', {'guest': guest})
        
        msg = EmailMultiAlternatives(
            subject,
            "Tu registro ha sido confirmado. Abre este correo en un lector HTML para ver tu código QR.",
            settings.EMAIL_HOST_USER,
            [guest.email]
        )
        msg.attach_alternative(html_content, "text/html")
        
        # Attach QR as inline image
        image = MIMEImage(buffer.getvalue())
        image.add_header('Content-ID', '<qrcode>')
        msg.attach(image)
        
        msg.send(fail_silently=False)
        print("✅ ¡ÉXITO! El correo de prueba fue enviado correctamente.")
    except Exception as e:
        print(f"❌ ERROR al enviar correo: {e}")

if __name__ == '__main__':
    test_email()
