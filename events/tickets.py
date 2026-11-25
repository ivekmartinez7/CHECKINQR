import re
from urllib.parse import urlencode

from django.conf import settings
from django.urls import reverse


def public_ticket_url(guest):
    return settings.PUBLIC_BASE_URL + reverse('guest_ticket', args=[guest.uuid])


def whatsapp_phone(value):
    if not value or not re.fullmatch(r'\+?[0-9 ().-]+', value.strip()):
        return ''
    digits = re.sub(r'\D', '', value)
    if not value.strip().startswith('+') and len(digits) == 10:
        return '521' + digits if digits[0] in '23456789' else ''
    if re.fullmatch(r'52[2-9]\d{9}', digits):
        return '521' + digits[2:]
    if digits.startswith('52'):
        return digits if re.fullmatch(r'521[2-9]\d{9}', digits) else ''
    return digits if re.fullmatch(r'[1-9]\d{10,14}', digits) else ''


def whatsapp_ticket_link(guest, group_size):
    phone = whatsapp_phone(guest.phone)
    if not phone:
        return ''
    holder = guest.parent if guest.parent_id else guest
    access = f'Pases del grupo: {group_size}.' if group_size > 1 else 'Pase individual.'
    message = [f'Hola, {holder.name}. Tu registro está confirmado.', settings.EVENT_NAME, access]
    if settings.EVENT_DATE_LABEL:
        message.append(settings.EVENT_DATE_LABEL)
    message.extend(['Presenta el QR actualizado de tu boleto en la entrada:', public_ticket_url(holder)])
    return 'https://wa.me/' + phone + '?' + urlencode({'text': '\n'.join(message)})
