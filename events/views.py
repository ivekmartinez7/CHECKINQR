import qrcode
import time
import hmac
import hashlib
import socket
import logging
import json
import re
from functools import wraps
from decimal import Decimal
from io import BytesIO
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_safe, require_http_methods
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from email.mime.image import MIMEImage
from html import escape
from django.conf import settings
from django.contrib import messages
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.contrib.auth.models import User
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import Count, Sum, Avg, Q
from django.urls import reverse
from uuid import UUID
from zipfile import BadZipFile
from .access import login_attempt_allowed, mobile_json
from .tickets import public_ticket_url, whatsapp_ticket_link
from .models import Guest, GuestRole, ChatMessage, SatisfactionQuestion, SatisfactionResponse

logger = logging.getLogger(__name__)

QR_WINDOW_SECONDS = 15
MAX_PASSES = 50



def normalize_money(value):
    try:
        return Decimal(str(value or '0').replace(',', '')).quantize(Decimal('0.01'))
    except Exception:
        return Decimal('0.00')


def get_role(role_name):
    if not role_name:
        return None
    if not isinstance(role_name, str) or len(role_name.strip()) > 100:
        raise ValidationError('La etiqueta debe tener hasta 100 caracteres.')
    if not role_name.strip():
        return None
    role_obj, _ = GuestRole.objects.get_or_create(name=role_name.strip())
    return role_obj


def get_group_holder(guest):
    return guest.parent if guest.parent_id else guest


def get_group_members(holder):
    return [holder, *list(holder.companions.select_related('role').order_by('id'))]


def mark_guest_checked_in(guest):
    """Only the request that changes the row may report a first arrival."""
    updated = Guest.objects.filter(pk=guest.pk, is_checked_in=False).update(
        is_checked_in=True, checked_in_at=timezone.now()
    )
    guest.refresh_from_db(fields=['is_checked_in', 'checked_in_at'])
    return not bool(updated)


def checkin_staff_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not (request.user.is_staff or request.user.is_superuser):
            if request.path.startswith('/api/'):
                return JsonResponse({'success': False, 'message': 'Solo personal autorizado.'}, status=403)
            return HttpResponseForbidden('Solo personal autorizado.')
        return view(request, *args, **kwargs)
    return wrapped


def valid_dynamic_token(uuid, token):
    return any(
        hmac.compare_digest(generate_dynamic_token(str(uuid), time_offset=offset), token)
        for offset in (0, -1)
    )


def confirmation_token(request, uuid):
    return signing.dumps({'user': request.user.pk, 'uuid': str(uuid)}, salt='checkin-confirm')


def valid_confirmation_token(request, uuid):
    try:
        payload = signing.loads(request.POST.get('confirmation_token', ''),
                                salt='checkin-confirm', max_age=300)
    except signing.BadSignature:
        return False
    return payload == {'user': request.user.pk, 'uuid': str(uuid)}


def validated_passes(value):
    try:
        passes = int(str(value or 1))
    except (TypeError, ValueError) as error:
        raise ValidationError('Escribe una cantidad válida de pases.') from error
    if not 1 <= passes <= MAX_PASSES:
        raise ValidationError(f'Puedes registrar de 1 a {MAX_PASSES} pases por grupo.')
    return passes


def save_guest_record(guest):
    guest.full_clean(exclude=['raffle_number'], validate_unique=False)
    guest.save()


def split_group_payment(total_amount, passes):
    total_amount = normalize_money(total_amount)
    if not total_amount.is_finite() or not 0 <= total_amount <= Decimal('99999999.99'):
        raise ValidationError('Revisa el monto pagado.')
    cents = int(total_amount * 100)
    base_cents, remainder = divmod(cents, passes)
    return [
        (Decimal(base_cents + (1 if index < remainder else 0)) / Decimal('100')).quantize(Decimal('0.01'))
        for index in range(passes)
    ]


def group_total_paid(holder):
    members = get_group_members(holder)
    return sum((member.amount_paid or Decimal('0.00') for member in members), Decimal('0.00')).quantize(Decimal('0.01'))


def create_companions_for_holder(holder, request_post, payment_shares=None):
    passes = max(int(holder.passes or 1), 1)
    companions_to_create = max(passes - 1, 0)
    existing = list(Guest.objects.filter(parent=holder).order_by('id'))
    for index in range(1, companions_to_create + 1):
        prefix = f'companion_{index}_'
        name = (request_post.get(prefix + 'name') or '').strip() or f"Invitado {index} de {holder.name}"
        role = get_role(request_post.get(prefix + 'role')) or holder.role
        submitted_amount = request_post.get(prefix + 'amount_paid')
        default_amount = payment_shares[index] if payment_shares and index < len(payment_shares) else Decimal('0.00')
        companion = existing[index - 1] if index <= len(existing) else Guest(parent=holder, guest_type='companion')
        companion.name = name
        companion.email = holder.email
        companion.phone = holder.phone
        companion.passes = 1
        companion.role = role
        companion.school = holder.school
        companion.gender = ''
        companion.amount_paid = normalize_money(submitted_amount) if submitted_amount not in (None, '') else default_amount
        save_guest_record(companion)
    for extra in existing[companions_to_create:]:
        extra.delete()

def generate_dynamic_token(guest_uuid, time_offset=0):
    time_window = int(time.time() // QR_WINDOW_SECONDS) + time_offset
    message = f"{guest_uuid}-{time_window}"
    return hmac.new(settings.SECRET_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()[:32]


def make_qr_png(data, box_size=8, border=4):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box_size, border=border)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#10233B", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def event_location_context():
    return {
        'event_name': settings.EVENT_NAME,
        'event_date_label': settings.EVENT_DATE_LABEL,
        'event_location_name': settings.EVENT_LOCATION_NAME,
        'event_location_address': settings.EVENT_LOCATION_ADDRESS,
        'event_location_map_url': settings.EVENT_LOCATION_MAP_URL,
    }


def send_guest_email(guest):
    if not guest.email or not settings.EMAIL_HOST_USER:
        return False
    try:
        ticket_url = public_ticket_url(get_group_holder(guest))

        ticket_qr_png = make_qr_png(ticket_url)
        
        # Create Email
        subject = f"Confirmación de Registro - {guest.name}"
        html_content = render_to_string('events/email_template.html', {
            'guest': guest,
            'ticket_url': ticket_url,
            **event_location_context(),
        })
        
        msg = EmailMultiAlternatives(
            subject,
            f"Tu registro ha sido confirmado. Abre tu boleto dinámico aquí: {ticket_url}",
            settings.EMAIL_HOST_USER,
            [guest.email]
        )
        msg.attach_alternative(html_content, "text/html")
        
        # Attach QR as inline image
        image = MIMEImage(ticket_qr_png)
        image.add_header('Content-ID', '<qrcode>')
        msg.attach(image)

        if settings.EVENT_LOCATION_MAP_URL:
            location_image = MIMEImage(make_qr_png(settings.EVENT_LOCATION_MAP_URL))
            location_image.add_header('Content-ID', '<locationqrcode>')
            msg.attach(location_image)
        
        msg.send(fail_silently=False)
        logger.info("Correo de boleto enviado para guest_id=%s", guest.id)
        return True
    except Exception as e:
        logger.warning("Correo de boleto falló para guest_id=%s (%s)", guest.id, type(e).__name__)
        return False

@login_required
@require_POST
def resend_email(request, pk):
    if not request.user.is_superuser:
        return redirect('scanner')
    
    guest = get_object_or_404(Guest, pk=pk)
    if guest.email:
        if send_guest_email(guest):
            messages.success(request, f"Correo reenviado exitosamente a {guest.email} ({guest.name}).")
        else:
            messages.error(request, f"No se pudo enviar el correo a {guest.email}. Revisa los logs del servidor para ver el error SMTP.")
    else:
        messages.error(request, f"{guest.name} no tiene un correo electrónico registrado.")
    
    return redirect('dashboard')

@checkin_staff_required
@never_cache
def dashboard(request):
    if request.method == 'POST':
        if not request.user.is_superuser:
            messages.error(request, "No tienes permisos para agregar invitados.")
            return redirect('dashboard')
        name = request.POST.get('name')
        email = request.POST.get('email')
        phone = request.POST.get('phone')
        passes = request.POST.get('passes', 1)
        role_name = request.POST.get('role')
        school = request.POST.get('school')
        gender = request.POST.get('gender', '')
        try:
            passes_count = validated_passes(passes)
            payment_shares = split_group_payment(request.POST.get('group_amount_paid') or request.POST.get('amount_paid'), passes_count)
            with transaction.atomic():
                role_obj = get_role(role_name)
                guest = Guest(
                    name=(name or '').strip(),
                    email=email,
                    phone=phone,
                    passes=passes_count,
                    role=role_obj,
                    school=school,
                    gender=gender,
                    amount_paid=payment_shares[0],
                )
                save_guest_record(guest)
                create_companions_for_holder(guest, request.POST, payment_shares)

            if guest.email:
                if send_guest_email(guest):
                    messages.success(request, f"Invitado {guest.name} agregado y correo enviado.")
                else:
                    messages.warning(request, f"Invitado {guest.name} guardado. Usa WhatsApp o reintenta el correo.")
            else:
                messages.success(request, f"Invitado {guest.name} agregado correctamente (sin correo).")
            
            return redirect('dashboard')
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))

    guests = Guest.objects.select_related('role', 'parent').annotate(
        companion_count=Count('companions')
    ).order_by('-created_at')
    total = guests.count()
    checked_in = guests.filter(is_checked_in=True).count()
    total_paid = guests.aggregate(total=Sum('amount_paid'))['total'] or Decimal('0.00')
    holders_count = guests.filter(parent__isnull=True).count()
    companions_count = guests.filter(parent__isnull=False).count()
    roles = GuestRole.objects.all().order_by('name')
    
    # Calculate role statistics
    roles_stats = []
    for r in roles:
        r_total = guests.filter(role=r).count()
        r_checked = guests.filter(role=r, is_checked_in=True).count()
        roles_stats.append({
            'name': r.name,
            'total': r_total,
            'checked': r_checked,
            'percentage': int((r_checked / r_total * 100) if r_total > 0 else 0)
        })
    
    context = {
        'guests': guests,
        'total': total,
        'checked_in': checked_in,
        'total_paid': total_paid,
        'holders_count': holders_count,
        'companions_count': companions_count,
        'roles': roles,
        'roles_stats': roles_stats,
        'chat_audio_enabled': settings.CHAT_AUDIO_ENABLED,
    }
    for guest in guests:
        holder = get_group_holder(guest)
        guest.ticket_url = public_ticket_url(holder)
        guest.whatsapp_link = whatsapp_ticket_link(guest, (holder.passes if guest.parent_id else 1 + guest.companion_count))
    return render(request, 'events/dashboard.html', context)

@login_required
def edit_guest(request, pk):
    if not request.user.is_superuser:
        return redirect('scanner')
    guest = get_object_or_404(Guest, pk=pk)
    roles = GuestRole.objects.all().order_by('name')
    
    if request.method == 'POST':
        old_email = guest.email
        try:
            with transaction.atomic():
                guest = get_object_or_404(Guest.objects.select_for_update(), pk=pk)
                guest.name = (request.POST.get('name') or '').strip()
                guest.email = request.POST.get('email')
                guest.phone = request.POST.get('phone')
                guest.passes = validated_passes(request.POST.get('passes')) if guest.parent_id is None else 1
                guest.gender = request.POST.get('gender', '')
                payment_shares = split_group_payment(
                    request.POST.get('group_amount_paid') or request.POST.get('amount_paid'), guest.passes
                )
                guest.amount_paid = payment_shares[0]
                guest.role = get_role(request.POST.get('role'))
                guest.school = request.POST.get('school')
                save_guest_record(guest)
                if guest.parent_id is None:
                    create_companions_for_holder(guest, request.POST, payment_shares)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
            return redirect('edit_guest', pk=pk)

        if guest.email and guest.email != old_email:
            if send_guest_email(guest):
                messages.success(request, f"Se actualizó el correo y se envió la confirmación a {guest.email}.")
            else:
                messages.warning(request, "Se guardó el correo. Puedes reenviar el boleto o compartirlo por WhatsApp.")
        return redirect('dashboard')

    companions = list(guest.companions.all().order_by('id')) if guest.parent_id is None else []
    return render(request, 'events/guest_form.html', {
        'guest': guest,
        'roles': roles,
        'companions': companions,
        'group_amount_paid': group_total_paid(guest) if guest.parent_id is None else guest.amount_paid,
    })

@login_required
@require_POST
def delete_guest(request, pk):
    if not request.user.is_superuser:
        return redirect('scanner')
    guest = get_object_or_404(Guest, pk=pk)
    guest.delete()
    return redirect('dashboard')


@login_required
@require_POST
def reset_checkin(request, pk):
    if not request.user.is_superuser:
        return redirect('scanner')
    guest = get_object_or_404(Guest, pk=pk)
    guest.is_checked_in = False
    guest.checked_in_at = None
    guest.save(update_fields=['is_checked_in', 'checked_in_at'])
    messages.success(request, f"El pase de {guest.name} volvió a Pendiente.")
    return redirect('dashboard')


@checkin_staff_required
def manual_check_in(request, pk):
    if request.method != 'POST':
        return redirect('dashboard')

    guest = get_object_or_404(Guest.objects.select_related('role', 'parent'), pk=pk)
    password = request.POST.get('staff_password') or ''
    next_url = request.POST.get('next') or 'dashboard'
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()},
                                           require_https=request.is_secure()):
        next_url = 'dashboard'

    confirmed_user = authenticate(request, username=request.user.username, password=password)
    if confirmed_user is None:
        messages.error(request, "Contraseña de staff incorrecta. No se registró el check-in manual.")
        return redirect(next_url)

    if mark_guest_checked_in(guest):
        messages.warning(request, f"{guest.name} ya estaba marcado como asistente.")
        return redirect(next_url)
    messages.success(request, f"Check-in manual registrado para {guest.name} después de validar identidad.")
    return redirect(next_url)

@require_safe
def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
    except DatabaseError:
        return JsonResponse({'status': 'unavailable'}, status=503)
    return JsonResponse({'status': 'ok'})


@require_safe
def robots_txt(request):
    return HttpResponse(
        'User-agent: *\nDisallow: /admin/\nDisallow: /api/\nDisallow: /qr/\nDisallow: /survey/\n',
        content_type='text/plain',
    )


@never_cache
@require_safe
def qr_code_image(request, uuid):
    guest = get_object_or_404(Guest.objects.select_related('parent'), uuid=uuid)
    holder = get_group_holder(guest)
    group = holder.companions.exists()
    route = 'group_check_in' if group else 'api_check_in_dynamic'
    path = reverse(route, args=[holder.uuid, generate_dynamic_token(str(holder.uuid))])
    response = HttpResponse(make_qr_png(request.build_absolute_uri(path)), content_type='image/png')
    response['X-Robots-Tag'] = 'noindex, noarchive'
    return response


@require_safe
def location_qr_image(request):
    if not settings.EVENT_LOCATION_MAP_URL:
        return HttpResponse(status=404)
    return HttpResponse(make_qr_png(settings.EVENT_LOCATION_MAP_URL), content_type='image/png')


@never_cache
@require_safe
def guest_ticket(request, uuid):
    guest = get_object_or_404(Guest.objects.select_related('parent', 'role'), uuid=uuid)
    holder = get_group_holder(guest)
    group_members = get_group_members(holder)
    response = render(request, 'events/ticket.html', {
        'ticket_holder': holder,
        'group_members': group_members,
        'group_size': len(group_members),
        **event_location_context(),
    })
    response['X-Robots-Tag'] = 'noindex, noarchive'
    return response


@checkin_staff_required
@never_cache
@require_http_methods(['GET', 'POST'])
def group_check_in(request, uuid, token):
    holder = get_object_or_404(Guest.objects.select_related('role'), uuid=uuid, parent__isnull=True)
    is_valid = (valid_confirmation_token(request, uuid) if request.method == 'POST'
                else valid_dynamic_token(uuid, token))
    if not is_valid:
        return render(request, 'events/group_checkin.html', {
            'success': False,
            'message': 'Código QR de grupo expirado o inválido.',
        })

    if request.method == 'POST':
        guest_id = request.POST.get('guest_id', '')
        if not guest_id.isascii() or not guest_id.isdecimal() or len(guest_id) > 18:
            return HttpResponse('Selecciona un pase válido.', status=400)
        member = get_object_or_404(Guest, pk=guest_id)
        if member.pk == holder.pk or member.parent_id == holder.pk:
            already_checked = mark_guest_checked_in(member)
            if not already_checked:
                messages.success(request, f"Check-in registrado para {member.name}.")
            else:
                messages.warning(request, f"{member.name} ya estaba marcado como asistente.")
        else:
            messages.error(request, "Ese pase no pertenece a este grupo.")
        return redirect('group_check_in', uuid=uuid, token=generate_dynamic_token(str(holder.uuid)))

    members = get_group_members(holder)
    checked_count = sum(1 for member in members if member.is_checked_in)
    return render(request, 'events/group_checkin.html', {
        'success': True,
        'holder': holder,
        'members': members,
        'checked_count': checked_count,
        'group_size': len(members),
        'token': token,
        'confirmation_token': confirmation_token(request, uuid),
        **event_location_context(),
    })

@checkin_staff_required
@never_cache
def scanner(request):
    query = (request.GET.get('q') or '').strip()
    manual_results = []
    if query:
        manual_results = (
            Guest.objects.select_related('role', 'parent')
            .filter(
                Q(name__icontains=query)
                | Q(phone__icontains=query)
                | Q(email__icontains=query)
                | Q(school__icontains=query)
                | Q(parent__name__icontains=query)
            )
            .order_by('is_checked_in', 'name')[:12]
        )
    return render(request, 'events/scanner.html', {
        'manual_query': query,
        'manual_results': manual_results,
        'chat_audio_enabled': settings.CHAT_AUDIO_ENABLED,
    })

@checkin_staff_required
def api_check_in(request):
    # Backward compatible static endpoint
    if request.method == 'POST':
        uuid_val = request.POST.get('uuid')
        if not uuid_val:
            return JsonResponse({'success': False, 'message': 'No UUID provided'}, status=400)
            
        try:
            guest = Guest.objects.get(uuid=uuid_val)
            already_checked = mark_guest_checked_in(guest)
                
            return JsonResponse({
                'success': True,
                'already_checked': already_checked,
                'message': 'El invitado ya ha ingresado previamente.' if already_checked else 'Check-in exitoso.',
                'guest': {
                    'name': guest.name,
                    'role': guest.role.name if guest.role else 'Sin Rol',
                    'raffle_number': guest.raffle_number,
                    'school': guest.school or '',
                    'guest_type': guest.get_guest_type_display(),
                }
            })
        except (Guest.DoesNotExist, ValidationError):
            return JsonResponse({'success': False, 'message': 'Código QR no válido. Invitado no encontrado.'}, status=404)
            
    return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

@checkin_staff_required
@never_cache
def api_check_in_dynamic(request, uuid, token):
    # Verify token on the current window (+/- 1 time window of 10s)
    is_valid = (valid_dynamic_token(uuid, token) or
                (request.method == 'POST' and valid_confirmation_token(request, uuid)))
            
    if not is_valid:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.method == 'POST':
            return JsonResponse({'success': False, 'message': 'Código QR expirado o inválido.'}, status=400)
        return render(request, 'events/scan_result.html', {'success': False, 'message': 'Código QR expirado o inválido.'})
        
    try:
        guest = Guest.objects.get(uuid=uuid)
        
        if request.method == 'GET':
            return render(request, 'events/scan_result.html', {
                'success': True,
                'confirm': True,
                'guest': guest,
                'confirmation_token': confirmation_token(request, uuid),
            })
        if request.method != 'POST':
            return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)
        already_checked = mark_guest_checked_in(guest)
            
        guest_data = {
            'name': guest.name,
            'role': guest.role.name if guest.role else 'Sin Rol',
            'raffle_number': guest.raffle_number,
            'school': guest.school or '',
            'guest_type': guest.get_guest_type_display(),
        }
        
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({
                'success': True,
                'already_checked': already_checked,
                'message': 'El invitado ya ha ingresado previamente.' if already_checked else 'Check-in exitoso.',
                'guest': guest_data
            })
            
        return render(request, 'events/scan_result.html', {
            'success': True,
            'already_checked': already_checked,
            'guest': guest
        })
    except Guest.DoesNotExist:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.method == 'POST':
            return JsonResponse({'success': False, 'message': 'Invitado no encontrado.'}, status=404)
        return render(request, 'events/scan_result.html', {'success': False, 'message': 'Invitado no encontrado.'})

@checkin_staff_required
@never_cache
def api_chat_messages(request):
    if request.method == 'GET':
        last_id = request.GET.get('last_id', 0)
        try:
            last_id = int(last_id)
        except ValueError:
            last_id = 0
            
        # If client has loaded messages but db is completely empty, it means the chat was cleared
        db_empty = not ChatMessage.objects.exists()
        if last_id > 0 and db_empty:
            return JsonResponse({'success': True, 'messages': [], 'cleared': True})
            
        messages = ChatMessage.objects.filter(id__gt=last_id).select_related('sender').order_by('id')[:100]
        msg_list = []
        for msg in messages:
            audio_url = reverse('chat_audio', args=[msg.pk]) if msg.audio_file and settings.CHAT_AUDIO_ENABLED else None
            msg_list.append({
                'id': msg.id,
                'sender': msg.sender.username,
                'sender_id': msg.sender.id,
                'text': msg.text or '',
                'audio_url': audio_url,
                'created_at': timezone.localtime(msg.created_at).strftime('%H:%M')
            })
        return JsonResponse({'success': True, 'messages': msg_list})
        
    elif request.method == 'POST':
        text = request.POST.get('text', '').strip()
        audio_file = request.FILES.get('audio')
        
        if len(text) > 2000:
            return JsonResponse({'success': False, 'message': 'El mensaje supera 2,000 caracteres.'}, status=400)
        if audio_file and (not settings.CHAT_AUDIO_ENABLED or audio_file.size > 2_000_000
                           or audio_file.content_type not in ('audio/webm', 'audio/ogg')):
            return JsonResponse({'success': False, 'message': 'Audio no disponible o formato no admitido.'}, status=400)
        if not text and not audio_file:
            return JsonResponse({'success': False, 'message': 'Mensaje vacío.'}, status=400)
            
        msg = ChatMessage(sender=request.user)
        if text:
            msg.text = text
        if audio_file:
            msg.audio_file = audio_file
        msg.save()
        
        return JsonResponse({
            'success': True,
            'message': {
                'id': msg.id,
                'sender': msg.sender.username,
                'sender_id': msg.sender.id,
                'text': msg.text or '',
                'audio_url': reverse('chat_audio', args=[msg.pk]) if msg.audio_file else None,
                'created_at': timezone.localtime(msg.created_at).strftime('%H:%M')
            }
        })
        
    elif request.method == 'DELETE':
        if not request.user.is_superuser:
            return JsonResponse({'success': False, 'message': 'Solo administradores.'}, status=403)
        ChatMessage.objects.all().delete()
        return JsonResponse({'success': True, 'message': 'Conversación limpiada.'})
        
    return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

@checkin_staff_required
@never_cache
@require_safe
def chat_audio(request, pk):
    message = get_object_or_404(ChatMessage, pk=pk)
    if not settings.CHAT_AUDIO_ENABLED or not message.audio_file:
        return HttpResponse(status=404)
    from django.http import FileResponse
    try:
        audio = message.audio_file.open('rb')
    except FileNotFoundError:
        return HttpResponse(status=404)
    content_type = 'audio/ogg' if message.audio_file.name.endswith('.ogg') else 'audio/webm'
    return FileResponse(audio, content_type=content_type)


@login_required
def manage_roles(request):
    if not request.user.is_superuser:
        return redirect('scanner')
        
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        if name:
            try:
                role, created = GuestRole.objects.get_or_create(name=name)
                if created:
                    messages.success(request, f"Rol '{name}' creado exitosamente.")
                else:
                    messages.warning(request, f"El rol '{name}' ya existe.")
            except Exception as e:
                messages.error(request, f"Error al crear el rol: {e}")
        return redirect('manage_roles')
        
    roles = GuestRole.objects.all().order_by('name')
    roles_data = []
    for r in roles:
        count = Guest.objects.filter(role=r).count()
        roles_data.append({
            'role': r,
            'guest_count': count
        })
    return render(request, 'events/manage_roles.html', {'roles_data': roles_data})

@login_required
@require_POST
def delete_role(request, pk):
    if not request.user.is_superuser:
        return redirect('scanner')
    role = get_object_or_404(GuestRole, pk=pk)
    if Guest.objects.filter(role=role).exists():
        messages.error(request, f"No se puede eliminar el rol '{role.name}' porque está asignado a uno o más invitados.")
    else:
        role.delete()
        messages.success(request, f"Rol '{role.name}' eliminado exitosamente.")
    return redirect('manage_roles')

@checkin_staff_required
def raffle_view(request):
    if not request.user.is_superuser:
        return redirect('scanner')
        
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        candidates = Guest.objects.filter(is_checked_in=True).exclude(raffle_number__isnull=True)
        data = [{
            'id': c.id,
            'name': c.name,
            'role': c.role.name if c.role else 'Sin Rol',
            'school': c.school or '',
            'raffle_number': c.raffle_number
        } for c in candidates]
        return JsonResponse({'success': True, 'candidates': data})
        
    return render(request, 'events/raffle.html')

import openpyxl
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet

@checkin_staff_required
def export_excel(request):
    if not request.user.is_superuser:
        return redirect('scanner')
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="invitados.xlsx"'
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Invitados'
    
    # Headers
    headers = ['Tipo', 'Titular', 'Nombre', 'Correo', 'Teléfono', 'Género', 'Pase Individual', 'Grupo Titular', 'Rol', 'Escuela/Servicio', 'Monto Pagado', 'No. Rifa', 'Asistió', 'Fecha Ingreso', 'Fecha Registro']
    ws.append(headers)
    
    # Data
    guests = Guest.objects.select_related('parent', 'role').order_by('-created_at')
    for guest in guests:
        status = 'Sí' if guest.is_checked_in else 'No'
        checked_in_str = timezone.localtime(guest.checked_in_at).strftime('%Y-%m-%d %H:%M') if guest.checked_in_at else 'N/A'
        date_str = timezone.localtime(guest.created_at).strftime('%Y-%m-%d %H:%M')
        ws.append([
            guest.get_guest_type_display(),
            guest.parent.name if guest.parent else '',
            guest.name, 
            guest.email or '', 
            guest.phone or '', 
            guest.get_gender_display() if guest.gender else '',
            1,
            guest.passes if guest.parent_id is None else '',
            guest.role.name if guest.role else '', 
            guest.school or '',
            float(guest.amount_paid),
            guest.raffle_number, 
            status, 
            checked_in_str,
            date_str
        ])
    
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if cell.data_type == 'f':
                cell.data_type = 's'
    wb.save(response)
    return response

@checkin_staff_required
def export_pdf(request):
    if not request.user.is_superuser:
        return redirect('scanner')
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="invitados.pdf"'
    
    doc = SimpleDocTemplate(
        response,
        pagesize=landscape(letter),
        leftMargin=24,
        rightMargin=24,
        topMargin=24,
        bottomMargin=24,
    )
    elements = []
    
    styles = getSampleStyleSheet()
    cell_style = styles['BodyText']
    cell_style.fontSize = 8
    cell_style.leading = 10
    elements.append(Paragraph("Lista de Invitados", styles['Title']))
    
    data = [['Tipo', 'Nombre', 'Servicio', 'Pase', 'Grupo', 'Rol', 'Monto', 'No. Rifa', 'Asistió']]
    
    guests = Guest.objects.all().order_by('name')
    for guest in guests:
        status = 'Sí' if guest.is_checked_in else 'No'
        data.append([
            guest.get_guest_type_display(),
            Paragraph(escape(guest.name or ''), cell_style),
            Paragraph(escape(guest.school or ''), cell_style),
            '1',
            str(guest.passes) if guest.parent_id is None and guest.passes > 1 else '',
            guest.role.name if guest.role else 'Sin Rol', 
            f"${guest.amount_paid}",
            str(guest.raffle_number), 
            status
        ])
        
    t = Table(data, colWidths=[55, 155, 155, 35, 40, 80, 55, 50, 50])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#6366f1')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('ALIGN', (0,1), (0,-1), 'LEFT'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 12),
        ('BOTTOMPADDING', (0,0), (-1,0), 12),
        ('BACKGROUND', (0,1), (-1,-1), colors.beige),
        ('GRID', (0,0), (-1,-1), 1, colors.black)
    ]))
    
    elements.append(t)
    doc.build(elements)
    
    return response

@checkin_staff_required
def import_excel(request):
    if not request.user.is_superuser:
        return redirect('scanner')
        
    if request.method == 'POST' and request.FILES.get('excel_file'):
        excel_file = request.FILES['excel_file']
        if not excel_file.name.lower().endswith('.xlsx') or excel_file.size > 4_000_000:
            messages.error(request, 'Selecciona un Excel .xlsx de hasta 4 MB.')
            return redirect('dashboard')
            
        try:
            wb = openpyxl.load_workbook(excel_file, read_only=True, data_only=True)
            ws = wb.active
            
            imported_count = 0
            email_count = 0
            skipped_count = 0
            
            # Skip header row
            for row in ws.iter_rows(min_row=2, max_row=min(ws.max_row or 2001, 2001), max_col=8, values_only=True):
                # Expected columns: Nombre, Correo, Teléfono, Pases, Rol, Escuela/Servicio, Género, Monto Pagado
                if not row[0]: # Nombre is required
                    continue
                    
                name = str(row[0]).strip()
                email = str(row[1]).strip() if row[1] else ''
                phone = str(row[2]).strip() if row[2] else ''
                
                try:
                    passes = validated_passes(row[3])
                    role_name = str(row[4]).strip() if row[4] else 'Docente'
                    school = str(row[5]).strip() if row[5] else ''
                    gender = str(row[6]).strip().upper()[:1] if row[6] else ''
                    payment_shares = split_group_payment(row[7], passes)
                    with transaction.atomic():
                        guest = Guest(name=name, email=email, phone=phone, passes=passes,
                                      role=get_role(role_name), school=school, gender=gender,
                                      amount_paid=payment_shares[0])
                        save_guest_record(guest)
                        create_companions_for_holder(guest, {}, payment_shares)
                    imported_count += 1
                    if guest.email and send_guest_email(guest):
                        email_count += 1
                except (ValidationError, IntegrityError):
                    skipped_count += 1
            wb.close()
                    
            messages.success(request, f'¡Importación completada! Se registraron {imported_count} invitados y se enviaron {email_count} correos. Filas omitidas: {skipped_count}.')
            
        except (OSError, ValueError, KeyError, openpyxl.utils.exceptions.InvalidFileException, BadZipFile):
            messages.error(request, 'No se pudo leer el archivo Excel.')
            
    return redirect('dashboard')


def build_executive_report_data(gender_filter=''):
    guests = Guest.objects.select_related('role', 'parent').all()
    if gender_filter:
        guests = guests.filter(gender=gender_filter)

    total = guests.count()
    checked_in = guests.filter(is_checked_in=True).count()
    total_paid = guests.aggregate(total=Sum('amount_paid'))['total'] or Decimal('0.00')
    holders_paid = guests.filter(parent__isnull=True).aggregate(total=Sum('amount_paid'))['total'] or Decimal('0.00')
    companions_paid = guests.filter(parent__isnull=False).aggregate(total=Sum('amount_paid'))['total'] or Decimal('0.00')
    holders_count = guests.filter(parent__isnull=True).count()
    companions_count = guests.filter(parent__isnull=False).count()

    role_rows = list(
        guests.values('role__name')
        .annotate(total=Count('id'), checked_in=Count('id', filter=Q(is_checked_in=True)), amount=Sum('amount_paid'))
        .order_by('role__name')
    )
    school_rows = list(
        guests.values('school')
        .annotate(total=Count('id'), checked_in=Count('id', filter=Q(is_checked_in=True)), amount=Sum('amount_paid'))
        .order_by('-total')[:12]
    )
    gender_rows = list(
        guests.values('gender')
        .annotate(total=Count('id'), checked_in=Count('id', filter=Q(is_checked_in=True)), amount=Sum('amount_paid'))
        .order_by('gender')
    )
    survey_rows = list(
        SatisfactionQuestion.objects.filter(is_active=True)
        .annotate(avg_rating=Avg('responses__rating'), responses_count=Count('responses'))
        .values('id', 'text', 'question_type', 'avg_rating', 'responses_count')
    )
    for rows in (role_rows, school_rows, gender_rows):
        for row in rows:
            row['amount'] = float(row['amount'] or 0)
    for row in survey_rows:
        row['avg_rating'] = round(float(row['avg_rating'] or 0), 2)

    attendance_rate = round((checked_in / total * 100), 1) if total else 0
    analysis = [
        f"Asistencia general: {attendance_rate}% ({checked_in} de {total} registros).",
        f"Corte de caja total: ${total_paid:.2f}; titulares ${holders_paid:.2f} e invitados ${companions_paid:.2f}.",
        f"Distribución: {holders_count} titulares y {companions_count} invitados/acompañantes.",
    ]
    if school_rows:
        top_school = school_rows[0]['school'] or 'Sin escuela/servicio'
        analysis.append(f"Mayor participación por escuela/servicio: {top_school} con {school_rows[0]['total']} registros.")

    return {
        'filters': {'gender': gender_filter},
        'summary': {
            'total': total,
            'checked_in': checked_in,
            'pending': total - checked_in,
            'attendance_rate': attendance_rate,
            'holders_count': holders_count,
            'companions_count': companions_count,
            'total_paid': float(total_paid),
            'holders_paid': float(holders_paid),
            'companions_paid': float(companions_paid),
        },
        'roles': role_rows,
        'schools': school_rows,
        'genders': gender_rows,
        'survey': survey_rows,
        'analysis': analysis,
    }


@checkin_staff_required
@never_cache
def executive_report(request):
    if not request.user.is_superuser:
        return redirect('scanner')
    gender_filter = request.GET.get('gender', '')
    data = build_executive_report_data(gender_filter)
    return render(request, 'events/executive_report.html', data)


@checkin_staff_required
@never_cache
def executive_report_json(request):
    if not request.user.is_superuser:
        return redirect('scanner')
    gender_filter = request.GET.get('gender', '')
    data = build_executive_report_data(gender_filter)
    return JsonResponse(data, json_dumps_params={'indent': 2})


@login_required
def survey_crud(request):
    if not request.user.is_superuser:
        return redirect('scanner')
    if request.method == 'POST':
        action = request.POST.get('action')
        question_id = request.POST.get('question_id')
        if action == 'delete' and question_id:
            SatisfactionQuestion.objects.filter(pk=question_id).delete()
            messages.success(request, "Pregunta eliminada.")
        else:
            text = request.POST.get('text', '').strip()
            if text:
                question_type = request.POST.get('question_type', 'rating')
                order = int(request.POST.get('order') or 0)
                is_active = request.POST.get('is_active') == '1'
                if question_id:
                    question = get_object_or_404(SatisfactionQuestion, pk=question_id)
                    question.text = text
                    question.question_type = question_type
                    question.order = order
                    question.is_active = is_active
                    question.save()
                    messages.success(request, "Pregunta actualizada.")
                else:
                    SatisfactionQuestion.objects.create(text=text, question_type=question_type, order=order, is_active=is_active)
                    messages.success(request, "Pregunta agregada.")
        return redirect('survey_crud')
    questions = SatisfactionQuestion.objects.all()
    return render(request, 'events/survey_crud.html', {'questions': questions})


@never_cache
@require_http_methods(['GET', 'POST'])
def survey_view(request, uuid):
    guest = get_object_or_404(Guest, uuid=uuid)
    questions = list(SatisfactionQuestion.objects.filter(is_active=True))
    if request.method == 'POST':
        answers = []
        for question in questions:
            value = request.POST.get(f'question_{question.id}', '').strip()
            if not value:
                continue
            if (question.question_type == 'rating' and value not in ('1', '2', '3', '4', '5')
                    or question.question_type == 'yes_no' and value not in ('Sí', 'No')
                    or len(value) > 2000):
                return render(request, 'events/survey.html', {
                    'guest': guest, 'questions': questions, 'survey_error': 'Revisa tus respuestas.'
                }, status=400)
            answers.append((question, value))
        with transaction.atomic():
            for question, value in answers:
                rating = question.question_type == 'rating'
                SatisfactionResponse.objects.update_or_create(
                    guest=guest, question=question,
                    defaults={'rating': int(value) if rating else None, 'answer_text': '' if rating else value},
                )
        return render(request, 'events/survey_thanks.html', {'guest': guest})
    return render(request, 'events/survey.html', {'guest': guest, 'questions': questions})

@login_required
def manage_staff(request):
    if not request.user.is_superuser:
        return redirect('scanner')
        
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create_staff':
            username = request.POST.get('username', '').strip()
            password = request.POST.get('password', '').strip()
            is_admin = request.POST.get('is_admin') == '1'
            
            if not username or not password:
                messages.error(request, "El nombre de usuario y contraseña son requeridos.")
            elif User.objects.filter(username=username).exists():
                messages.error(request, "El nombre de usuario ya está registrado.")
            else:
                user = User(username=username, is_staff=True, is_superuser=is_admin)
                try:
                    user.full_clean(exclude=['password'], validate_unique=False)
                    validate_password(password, user)
                    user.set_password(password)
                    user.save()
                except ValidationError as error:
                    messages.error(request, ' '.join(error.messages))
                except IntegrityError:
                    messages.error(request, 'El nombre de usuario ya está registrado.')
                else:
                    messages.success(request, f"Usuario de staff '{username}' creado exitosamente.")
                
        return redirect('manage_staff')
        
    # Get local IPs
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(('10.254.254.254', 1))
        IP = s.getsockname()[0]
        if IP and IP not in ips:
            ips.append(IP)
        s.close()
    except Exception:
        pass
        
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if ip not in ips and not ip.startswith('127.'):
                ips.append(ip)
    except Exception:
        pass
        
    if not ips:
        ips.append("192.168.1.X")
        
    # Get list of staff users (excluding current logged-in user to avoid self-deletion)
    staff_users = User.objects.exclude(id=request.user.id).order_by('username')
    
    return render(request, 'events/manage_staff.html', {
        'ips': ips,
        'staff_users': staff_users,
    })

@login_required
@require_POST
def delete_staff(request, pk):
    if not request.user.is_superuser:
        return redirect('scanner')
        
    user_to_delete = get_object_or_404(User, pk=pk)
    if user_to_delete == request.user:
        messages.error(request, "No puedes eliminar tu propio usuario actual.")
    else:
        username = user_to_delete.username
        user_to_delete.delete()
        messages.success(request, f"Usuario '{username}' eliminado con éxito.")
        
    return redirect('manage_staff')

@checkin_staff_required
def connection_qr_image(request):
    ip = request.GET.get('ip', '')
    protocol = request.GET.get('protocol', 'http')
    
    if not ip:
        ip = request.get_host()
        
    # Check if user typed the protocol manually
    if ip.startswith('http://') or ip.startswith('https://'):
        connection_url = ip
    else:
        # Build connection URL
        connection_url = f"{protocol}://{ip}"
        if ':' not in ip and not ip.endswith('.win') and not ip.endswith('.com') and not ip.endswith('.net') and not ip.endswith('.org'):
            # If no port is specified and not a regular domain, assume default port 8000
            connection_url = f"{protocol}://{ip}:8000/"
        
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(connection_url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return HttpResponse(buffer.getvalue(), content_type="image/png")


def guest_to_mobile_dict(guest):
    return {
        'id': guest.id,
        'uuid': str(guest.uuid),
        'parent_id': guest.parent_id,
        'guest_type': guest.guest_type,
        'guest_type_label': guest.get_guest_type_display(),
        'name': guest.name,
        'email': guest.email or '',
        'phone': guest.phone or '',
        'passes': guest.passes,
        'role': guest.role.name if guest.role else '',
        'school': guest.school or '',
        'gender': guest.gender or '',
        'gender_label': guest.get_gender_display() if guest.gender else '',
        'amount_paid': float(guest.amount_paid or 0),
        'raffle_number': guest.raffle_number,
        'is_checked_in': guest.is_checked_in,
        'checked_in_at': guest.checked_in_at.isoformat() if guest.checked_in_at else None,
        'created_at': guest.created_at.isoformat() if guest.created_at else None,
        'qr_code': str(guest.uuid),
    }


def mobile_summary_data():
    guests = Guest.objects.select_related('role', 'parent').all()
    total = guests.count()
    checked_in = guests.filter(is_checked_in=True).count()
    total_paid = guests.aggregate(total=Sum('amount_paid'))['total'] or Decimal('0.00')
    return {
        'id': 1,
        'name': 'CheckinParty',
        'venue': 'Evento activo',
        'total_guests': total,
        'checked_in': checked_in,
        'pending': total - checked_in,
        'total_paid': float(total_paid),
        'holders_count': guests.filter(parent__isnull=True).count(),
        'companions_count': guests.filter(parent__isnull=False).count(),
    }


def role_to_mobile_dict(role):
    return {
        'id': role.id,
        'name': role.name,
        'guest_count': Guest.objects.filter(role=role).count(),
    }


def parse_json_body(request):
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
        return payload if isinstance(payload, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def mobile_text(data, name, default=''):
    value = data.get(name, default)
    if not isinstance(value, str):
        raise ValidationError('Revisa los campos de texto.')
    return value.strip()


def extract_uuid_from_qr(raw_code):
    code = raw_code.strip() if isinstance(raw_code, str) and len(raw_code) <= 1024 else ''
    if not code:
        return '', ''
    group_match = re.search(r'/group-checkin/([0-9a-fA-F-]{36})/([A-Za-z0-9_-]+)/?', code)
    if group_match:
        return group_match.group(1), group_match.group(2)
    dynamic_match = re.search(r'/api/checkin-dynamic/([0-9a-fA-F-]{36})/([A-Za-z0-9_-]+)/?', code)
    if dynamic_match:
        return dynamic_match.group(1), dynamic_match.group(2)
    uuid_match = re.search(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', code)
    if uuid_match:
        return uuid_match.group(1), ''
    return code, ''


@mobile_json
def mobile_login(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)
    data = parse_json_body(request)
    username = data.get('username') or ''
    password = data.get('password') or ''
    if not isinstance(username, str) or not isinstance(password, str) or len(password) > 4096:
        return JsonResponse({'success': False, 'message': 'Credenciales inválidas.'}, status=400)
    username = username.strip()[:254]
    if not login_attempt_allowed(request, username):
        return JsonResponse({'success': False, 'message': 'Espera unos minutos para volver a intentar.'}, status=429)
    login_username = username
    if '@' in username:
        user_match = User.objects.filter(email__iexact=username).first()
        if user_match:
            login_username = user_match.username
    user = authenticate(request, username=login_username, password=password)
    if not user or not (user.is_staff or user.is_superuser):
        return JsonResponse({'success': False, 'message': 'Usuario o contraseña incorrectos.'}, status=401)
    if not user.is_active:
        return JsonResponse({'success': False, 'message': 'Usuario inactivo.'}, status=403)
    login(request, user)
    return JsonResponse({
        'success': True,
        'user': {
            'id': user.id,
            'username': user.username,
            'is_superuser': user.is_superuser,
            'is_staff': user.is_staff,
        },
        'summary': mobile_summary_data(),
    })


@mobile_json
@require_POST
def mobile_logout(request):
    logout(request)
    return JsonResponse({'success': True})


@checkin_staff_required
@never_cache
def mobile_sync(request):
    guests = Guest.objects.select_related('role', 'parent').all().order_by('name')
    roles = [role_to_mobile_dict(role) for role in GuestRole.objects.order_by('name')]
    return JsonResponse({
        'success': True,
        'user': {
            'id': request.user.id,
            'username': request.user.username,
            'is_superuser': request.user.is_superuser,
            'is_staff': request.user.is_staff,
        },
        'summary': mobile_summary_data(),
        'roles': roles,
        'guests': [guest_to_mobile_dict(guest) for guest in guests],
    })


@mobile_json
@checkin_staff_required
def mobile_check_in(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)
    data = parse_json_body(request)
    raw_qr_code = data.get('qr_code') or data.get('uuid')
    guest_uuid, token = extract_uuid_from_qr(raw_qr_code)
    try:
        guest_uuid = str(UUID(guest_uuid))
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'message': 'Código QR inválido.'}, status=400)
    if token:
        if not valid_dynamic_token(guest_uuid, token):
            return JsonResponse({'success': False, 'message': 'Código QR expirado o inválido.'}, status=400)
    try:
        guest = Guest.objects.select_related('role', 'parent').get(uuid=guest_uuid)
    except Guest.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Invitado no encontrado.'}, status=404)

    if token and '/group-checkin/' in raw_qr_code:
        holder = get_group_holder(guest)
        members = get_group_members(holder)
        return JsonResponse({
            'success': True,
            'group_checkin': True,
            'message': 'QR de grupo detectado. Selecciona el pase que llegó.',
            'holder': guest_to_mobile_dict(holder),
            'members': [guest_to_mobile_dict(member) for member in members],
            'summary': mobile_summary_data(),
        })

    already_checked = mark_guest_checked_in(guest)
    return JsonResponse({
        'success': True,
        'already_checked': already_checked,
        'message': 'El invitado ya había ingresado.' if already_checked else 'Check-in sincronizado.',
        'guest': guest_to_mobile_dict(guest),
        'summary': mobile_summary_data(),
    })


@mobile_json
@login_required
def mobile_guest_create(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)
    if not request.user.is_superuser:
        return JsonResponse({'success': False, 'message': 'Solo administradores pueden crear invitados.'}, status=403)
    data = parse_json_body(request)
    try:
        passes = validated_passes(data.get('passes'))
        payment_shares = split_group_payment(data.get('amount_paid'), passes)
        with transaction.atomic():
            guest = Guest(
                name=mobile_text(data, 'name'),
                email=mobile_text(data, 'email'),
                phone=mobile_text(data, 'phone'),
                passes=passes,
                role=get_role(data.get('role')),
                school=mobile_text(data, 'school'),
                gender=mobile_text(data, 'gender').upper(),
                amount_paid=payment_shares[0],
            )
            save_guest_record(guest)
            create_companions_for_holder(guest, {}, payment_shares)
    except ValidationError as error:
        return JsonResponse({'success': False, 'message': ' '.join(error.messages)}, status=400)
    if guest.email:
        send_guest_email(guest)
    return JsonResponse({'success': True, 'guest': guest_to_mobile_dict(guest), 'summary': mobile_summary_data()})


@mobile_json
@login_required
def mobile_guest_detail(request, pk):
    if not request.user.is_superuser:
        return JsonResponse({'success': False, 'message': 'Solo administradores.'}, status=403)
    guest = get_object_or_404(Guest, pk=pk)
    if request.method == 'GET':
        return JsonResponse({'success': True, 'guest': guest_to_mobile_dict(guest)})
    if request.method == 'DELETE':
        guest.delete()
        return JsonResponse({'success': True, 'summary': mobile_summary_data()})
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)
    data = parse_json_body(request)
    try:
        guest.name = mobile_text(data, 'name', guest.name)
        guest.email = mobile_text(data, 'email', guest.email or '')
        guest.phone = mobile_text(data, 'phone', guest.phone or '')
        passes = validated_passes(data.get('passes', guest.passes))
        if passes != guest.passes:
            raise ValidationError('Edita la cantidad de pases desde el panel web para actualizar el grupo.')
        if 'role' in data:
            guest.role = get_role(data['role'])
        guest.school = mobile_text(data, 'school', guest.school or '')
        guest.gender = mobile_text(data, 'gender', guest.gender).upper()
        guest.amount_paid = normalize_money(data.get('amount_paid', guest.amount_paid))
        with transaction.atomic():
            save_guest_record(guest)
    except ValidationError as error:
        return JsonResponse({'success': False, 'message': ' '.join(error.messages)}, status=400)
    return JsonResponse({'success': True, 'guest': guest_to_mobile_dict(guest), 'summary': mobile_summary_data()})


@mobile_json
@login_required
@require_POST
def mobile_guest_reset(request, pk):
    if not request.user.is_superuser:
        return JsonResponse({'success': False, 'message': 'Solo administradores.'}, status=403)
    guest = get_object_or_404(Guest, pk=pk)
    guest.is_checked_in = False
    guest.checked_in_at = None
    guest.save(update_fields=['is_checked_in', 'checked_in_at'])
    return JsonResponse({'success': True, 'guest': guest_to_mobile_dict(guest), 'summary': mobile_summary_data()})


@mobile_json
@checkin_staff_required
def mobile_roles(request):
    if request.method == 'GET':
        return JsonResponse({'success': True, 'roles': [role_to_mobile_dict(role) for role in GuestRole.objects.order_by('name')]})
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)
    if not request.user.is_superuser:
        return JsonResponse({'success': False, 'message': 'Solo administradores.'}, status=403)
    try:
        role = get_role(parse_json_body(request).get('name'))
    except ValidationError as error:
        return JsonResponse({'success': False, 'message': ' '.join(error.messages)}, status=400)
    if role is None:
        return JsonResponse({'success': False, 'message': 'El nombre del rol es requerido.'}, status=400)
    return JsonResponse({'success': True, 'role': role_to_mobile_dict(role)})


@mobile_json
@login_required
def mobile_role_detail(request, pk):
    if request.method != 'DELETE':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)
    if not request.user.is_superuser:
        return JsonResponse({'success': False, 'message': 'Solo administradores.'}, status=403)
    role = get_object_or_404(GuestRole, pk=pk)
    if Guest.objects.filter(role=role).exists():
        return JsonResponse({'success': False, 'message': 'No se puede borrar un rol asignado.'}, status=400)
    role.delete()
    return JsonResponse({'success': True})


@checkin_staff_required
@never_cache
def mobile_report(request):
    if not request.user.is_superuser:
        return JsonResponse({'success': False, 'message': 'Solo administradores.'}, status=403)
    data = build_executive_report_data(request.GET.get('gender', ''))
    data['success'] = True
    return JsonResponse(data)


@checkin_staff_required
@never_cache
def mobile_raffle(request):
    if not request.user.is_superuser:
        return JsonResponse({'success': False, 'message': 'Solo administradores.'}, status=403)
    candidates = Guest.objects.filter(is_checked_in=True).exclude(raffle_number__isnull=True).select_related('role').order_by('raffle_number')
    return JsonResponse({
        'success': True,
        'candidates': [{
            'id': guest.id,
            'name': guest.name,
            'role': guest.role.name if guest.role else 'Sin Rol',
            'school': guest.school or '',
            'raffle_number': guest.raffle_number,
        } for guest in candidates],
    })
