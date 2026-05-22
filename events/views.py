import qrcode
import time
import hmac
import hashlib
import socket
import logging
from decimal import Decimal
from io import BytesIO
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from email.mime.image import MIMEImage
from django.conf import settings
from django.contrib import messages
from django.utils import timezone
from django.contrib.auth.models import User
from django.db.models import Count, Sum, Avg, Q
from .models import Guest, GuestRole, ChatMessage, SatisfactionQuestion, SatisfactionResponse

logger = logging.getLogger(__name__)


DEFAULT_SURVEY_QUESTIONS = [
    ("¿Cómo calificarías la organización general del evento?", "rating"),
    ("¿Cómo calificarías la atención recibida durante el registro y acceso?", "rating"),
    ("¿El servicio cumplió con tus expectativas?", "yes_no"),
    ("¿Qué mejorarías para el próximo año?", "text"),
    ("¿Recomendarías este evento o servicio?", "yes_no"),
]


def ensure_default_survey_questions():
    for index, (text, question_type) in enumerate(DEFAULT_SURVEY_QUESTIONS, start=1):
        SatisfactionQuestion.objects.get_or_create(
            text=text,
            defaults={'question_type': question_type, 'order': index}
        )


def normalize_money(value):
    try:
        return Decimal(str(value or '0').replace(',', '')).quantize(Decimal('0.01'))
    except Exception:
        return Decimal('0.00')


def get_role(role_name):
    if not role_name:
        return None
    role_obj, _ = GuestRole.objects.get_or_create(name=role_name.strip())
    return role_obj


def create_companions_for_holder(holder, request_post):
    passes = max(int(holder.passes or 1), 1)
    companions_to_create = max(passes - 1, 0)
    existing = list(Guest.objects.filter(parent=holder).order_by('id'))
    for index in range(1, companions_to_create + 1):
        prefix = f'companion_{index}_'
        name = (request_post.get(prefix + 'name') or '').strip() or f"Invitado {index} de {holder.name}"
        role = get_role(request_post.get(prefix + 'role')) or holder.role
        companion = existing[index - 1] if index <= len(existing) else Guest(parent=holder, guest_type='companion')
        companion.name = name
        companion.email = (request_post.get(prefix + 'email') or '').strip() or holder.email
        companion.phone = (request_post.get(prefix + 'phone') or '').strip() or holder.phone
        companion.passes = 1
        companion.role = role
        companion.school = (request_post.get(prefix + 'school') or '').strip() or holder.school
        companion.gender = request_post.get(prefix + 'gender') or holder.gender
        companion.amount_paid = normalize_money(request_post.get(prefix + 'amount_paid') or holder.amount_paid)
        companion.save()
    for extra in existing[companions_to_create:]:
        extra.delete()

def generate_dynamic_token(guest_uuid, time_offset=0):
    time_window = int(time.time() // 10) + time_offset
    message = f"{guest_uuid}-{time_window}"
    return hmac.new(settings.SECRET_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()[:8]

def send_guest_email(guest):
    if not guest.email or not settings.EMAIL_HOST_USER:
        return False
    try:
        public_base_url = getattr(settings, 'PUBLIC_BASE_URL', 'https://checkinparty.systemsivek.win').rstrip('/')
        ticket_url = f"{public_base_url}/ticket/{guest.uuid}/"

        # Generate QR Image
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(ticket_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        
        # Create Email
        subject = f"Confirmación de Registro - {guest.name}"
        html_content = render_to_string('events/email_template.html', {'guest': guest, 'ticket_url': ticket_url})
        
        msg = EmailMultiAlternatives(
            subject,
            f"Tu registro ha sido confirmado. Abre tu boleto dinámico aquí: {ticket_url}",
            settings.EMAIL_HOST_USER,
            [guest.email]
        )
        msg.attach_alternative(html_content, "text/html")
        
        # Attach QR as inline image
        image = MIMEImage(buffer.getvalue())
        image.add_header('Content-ID', '<qrcode>')
        msg.attach(image)
        
        msg.send(fail_silently=False)
        logger.info("Correo de boleto enviado a %s para guest_id=%s", guest.email, guest.id)
        return True
    except Exception as e:
        logger.exception("Error enviando correo a %s para guest_id=%s: %s", guest.email, guest.id, e)
        return False

@login_required
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

@login_required
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
        amount_paid = normalize_money(request.POST.get('amount_paid'))
        
        try:
            role_obj = get_role(role_name)
            guest = Guest(
                name=name,
                email=email,
                phone=phone,
                passes=int(passes),
                role=role_obj,
                school=school,
                gender=gender,
                amount_paid=amount_paid,
            )
            guest.save()
            create_companions_for_holder(guest, request.POST)
            
            if guest.email:
                send_guest_email(guest)
                messages.success(request, f"Invitado {guest.name} agregado y correo enviado.")
            else:
                messages.success(request, f"Invitado {guest.name} agregado correctamente (sin correo).")
            
            return redirect('dashboard')
        except ValidationError as e:
            messages.error(request, "Error de validación al agregar invitado.")

    guests = Guest.objects.select_related('role', 'parent').all().order_by('-created_at')
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
    }
    return render(request, 'events/dashboard.html', context)

@login_required
def edit_guest(request, pk):
    if not request.user.is_superuser:
        return redirect('scanner')
    guest = get_object_or_404(Guest, pk=pk)
    roles = GuestRole.objects.all().order_by('name')
    
    if request.method == 'POST':
        old_email = guest.email
        guest.name = request.POST.get('name')
        guest.email = request.POST.get('email')
        guest.phone = request.POST.get('phone')
        guest.passes = int(request.POST.get('passes', 1))
        guest.gender = request.POST.get('gender', '')
        guest.amount_paid = normalize_money(request.POST.get('amount_paid'))
        
        role_name = request.POST.get('role')
        guest.role = get_role(role_name)
            
        guest.school = request.POST.get('school')
        guest.save()
        if guest.parent_id is None:
            create_companions_for_holder(guest, request.POST)
        
        # Si el correo cambió y no está vacío, reenviar confirmación automáticamente
        if guest.email and guest.email != old_email:
            send_guest_email(guest)
            messages.success(request, f"Se actualizó el correo y se envió la confirmación a {guest.email}.")
            
        return redirect('dashboard')
        
    companions = list(guest.companions.all().order_by('id')) if guest.parent_id is None else []
    return render(request, 'events/guest_form.html', {'guest': guest, 'roles': roles, 'companions': companions})

@login_required
def delete_guest(request, pk):
    if not request.user.is_superuser:
        return redirect('scanner')
    guest = get_object_or_404(Guest, pk=pk)
    guest.delete()
    return redirect('dashboard')


@login_required
def reset_checkin(request, pk):
    if not request.user.is_superuser:
        return redirect('scanner')
    guest = get_object_or_404(Guest, pk=pk)
    guest.is_checked_in = False
    guest.checked_in_at = None
    guest.save(update_fields=['is_checked_in', 'checked_in_at'])
    messages.success(request, f"El pase de {guest.name} volvió a Pendiente.")
    return redirect('dashboard')

def qr_code_image(request, uuid):
    guest = get_object_or_404(Guest, uuid=uuid)
    
    # Calculate token for the current 10-second block
    token = generate_dynamic_token(str(guest.uuid))
    
    # Generate the dynamic URL
    domain = request.build_absolute_uri('/')[:-1]
    check_in_url = f"{domain}/api/checkin-dynamic/{guest.uuid}/{token}/"
    
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(check_in_url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return HttpResponse(buffer.getvalue(), content_type="image/png")

def guest_ticket(request, uuid):
    guest = get_object_or_404(Guest, uuid=uuid)
    return render(request, 'events/ticket.html', {'guest': guest})

@login_required
def scanner(request):
    return render(request, 'events/scanner.html')

@csrf_exempt
def api_check_in(request):
    # Backward compatible static endpoint
    if request.method == 'POST':
        uuid_val = request.POST.get('uuid')
        if not uuid_val:
            return JsonResponse({'success': False, 'message': 'No UUID provided'}, status=400)
            
        try:
            guest = Guest.objects.get(uuid=uuid_val)
            already_checked = guest.is_checked_in
            if not already_checked:
                guest.is_checked_in = True
                guest.checked_in_at = timezone.now()
                guest.save()
                
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
        except Guest.DoesNotExist:
            return JsonResponse({'success': False, 'message': 'Código QR no válido. Invitado no encontrado.'}, status=404)
            
    return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

@csrf_exempt
def api_check_in_dynamic(request, uuid, token):
    # Verify token on the current window (+/- 1 time window of 10s)
    is_valid = False
    for offset in [0, -1, 1]:
        computed = generate_dynamic_token(str(uuid), time_offset=offset)
        if hmac.compare_digest(computed, token):
            is_valid = True
            break
            
    if not is_valid:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.method == 'POST':
            return JsonResponse({'success': False, 'message': 'Código QR expirado o inválido.'}, status=400)
        return render(request, 'events/scan_result.html', {'success': False, 'message': 'Código QR expirado o inválido.'})
        
    try:
        guest = Guest.objects.get(uuid=uuid)
        
        # Enforce checkin permissions if logged in
        if not request.user.is_authenticated:
            # If hit directly via browser, redirect to login or require authentication
            if request.method == 'GET':
                return redirect(f"{settings.LOGIN_URL}?next={request.path}")
                
        already_checked = guest.is_checked_in
        if not already_checked:
            guest.is_checked_in = True
            guest.checked_in_at = timezone.now()
            guest.save()
            
        guest_data = {
            'name': guest.name,
            'role': guest.role.name if guest.role else 'Sin Rol',
            'raffle_number': guest.raffle_number,
            'school': guest.school or '',
            'guest_type': guest.get_guest_type_display(),
        }
        
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.method == 'POST':
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

@login_required
@csrf_exempt
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
            
        messages = ChatMessage.objects.filter(id__gt=last_id).order_by('created_at')
        msg_list = []
        for msg in messages:
            audio_url = msg.audio_file.url if msg.audio_file else None
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
                'audio_url': msg.audio_file.url if msg.audio_file else None,
                'created_at': timezone.localtime(msg.created_at).strftime('%H:%M')
            }
        })
        
    elif request.method == 'DELETE':
        ChatMessage.objects.all().delete()
        return JsonResponse({'success': True, 'message': 'Conversación limpiada.'})
        
    return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

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

@login_required
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
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet

@login_required
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
    guests = Guest.objects.all().order_by('-created_at')
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
    
    wb.save(response)
    return response

@login_required
def export_pdf(request):
    if not request.user.is_superuser:
        return redirect('scanner')
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="invitados.pdf"'
    
    doc = SimpleDocTemplate(response, pagesize=letter)
    elements = []
    
    styles = getSampleStyleSheet()
    elements.append(Paragraph("Lista de Invitados", styles['Title']))
    
    data = [['Tipo', 'Nombre', 'Pase', 'Grupo', 'Rol', 'Monto', 'No. Rifa', 'Asistió']]
    
    guests = Guest.objects.all().order_by('name')
    for guest in guests:
        status = 'Sí' if guest.is_checked_in else 'No'
        data.append([
            guest.get_guest_type_display(),
            guest.name[:30], 
            '1',
            str(guest.passes) if guest.parent_id is None and guest.passes > 1 else '',
            guest.role.name if guest.role else 'Sin Rol', 
            f"${guest.amount_paid}",
            str(guest.raffle_number), 
            status
        ])
        
    t = Table(data, colWidths=[55, 140, 35, 40, 70, 55, 50, 50])
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

@login_required
def import_excel(request):
    if not request.user.is_superuser:
        return redirect('scanner')
        
    if request.method == 'POST' and request.FILES.get('excel_file'):
        excel_file = request.FILES['excel_file']
        if not excel_file.name.endswith(('.xlsx', '.xls')):
            messages.error(request, 'El archivo debe ser un Excel válido (.xlsx o .xls).')
            return redirect('dashboard')
            
        try:
            wb = openpyxl.load_workbook(excel_file)
            ws = wb.active
            
            imported_count = 0
            email_count = 0
            
            # Skip header row
            for row in ws.iter_rows(min_row=2, values_only=True):
                # Expected columns: Nombre, Correo, Teléfono, Pases, Rol, Escuela/Servicio, Género, Monto Pagado
                if not row[0]: # Nombre is required
                    continue
                    
                name = str(row[0]).strip()
                email = str(row[1]).strip() if row[1] else ''
                phone = str(row[2]).strip() if row[2] else ''
                
                try:
                    passes = int(row[3]) if row[3] else 1
                except (ValueError, TypeError):
                    passes = 1
                    
                role_name = str(row[4]).strip() if row[4] else 'Docente'
                school = str(row[5]).strip() if len(row) > 5 and row[5] else ''
                gender = str(row[6]).strip().upper()[:1] if len(row) > 6 and row[6] else ''
                amount_paid = normalize_money(row[7] if len(row) > 7 else 0)
                
                role_obj = get_role(role_name)
                    
                guest = Guest(name=name, email=email, phone=phone, passes=passes, role=role_obj, school=school, gender=gender, amount_paid=amount_paid)
                try:
                    guest.save()
                    for companion_index in range(1, max(passes - 1, 0) + 1):
                        companion = Guest(
                            parent=guest,
                            guest_type='companion',
                            name=f"Invitado {companion_index} de {guest.name}",
                            email=guest.email,
                            phone=guest.phone,
                            passes=1,
                            role=guest.role,
                            school=guest.school,
                            gender=guest.gender,
                            amount_paid=guest.amount_paid,
                        )
                        companion.save()
                    imported_count += 1
                    
                    if guest.email:
                        if send_guest_email(guest):
                            email_count += 1
                except ValidationError:
                    continue # Skip if error saving
                    
            messages.success(request, f'¡Importación completada! Se registraron {imported_count} invitados y se enviaron {email_count} correos automáticos.')
            
        except Exception as e:
            messages.error(request, f'Error al procesar el archivo Excel: {str(e)}')
            
    return redirect('dashboard')


def build_executive_report_data(gender_filter=''):
    ensure_default_survey_questions()
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


@login_required
def executive_report(request):
    if not request.user.is_superuser:
        return redirect('scanner')
    gender_filter = request.GET.get('gender', '')
    data = build_executive_report_data(gender_filter)
    return render(request, 'events/executive_report.html', data)


@login_required
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
    ensure_default_survey_questions()
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


def survey_view(request, uuid):
    guest = get_object_or_404(Guest, uuid=uuid)
    ensure_default_survey_questions()
    questions = SatisfactionQuestion.objects.filter(is_active=True)
    if request.method == 'POST':
        for question in questions:
            value = request.POST.get(f'question_{question.id}', '').strip()
            response, _ = SatisfactionResponse.objects.get_or_create(guest=guest, question=question)
            if question.question_type == 'rating':
                response.rating = int(value or 0) or None
                response.answer_text = ''
            else:
                response.rating = None
                response.answer_text = value
            response.save()
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
                user = User.objects.create_user(username=username, password=password)
                user.is_staff = True
                user.is_superuser = is_admin
                user.save()
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

@login_required
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
