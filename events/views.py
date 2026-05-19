import qrcode
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
from .models import Guest
import qrcode
from io import BytesIO

def send_guest_email(guest):
    if not guest.email or not settings.EMAIL_HOST_USER:
        return False
    try:
        # Generate QR Image
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(str(guest.uuid))
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        
        # Create Email
        subject = f"Confirmación de Registro - {guest.name}"
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
        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False

@login_required
def resend_email(request, pk):
    if not request.user.is_superuser:
        return redirect('scanner')
    
    guest = get_object_or_404(Guest, pk=pk)
    if guest.email:
        if send_guest_email(guest):
            messages.success(request, f"Correo reenviado exitosamente a {guest.name}.")
        else:
            messages.error(request, f"Hubo un error al intentar enviar el correo a {guest.name}.")
    else:
        messages.error(request, f"{guest.name} no tiene un correo electrónico registrado.")
    
    return redirect('dashboard')

@login_required
def dashboard(request):
    if not request.user.is_superuser:
        return redirect('scanner')

    if request.method == 'POST':
        name = request.POST.get('name')
        email = request.POST.get('email')
        phone = request.POST.get('phone')
        passes = request.POST.get('passes', 1)
        role = request.POST.get('role', 'Docente')
        
        try:
            guest = Guest(name=name, email=email, phone=phone, passes=int(passes), role=role)
            guest.save()
            
            if guest.email:
                send_guest_email(guest)
                messages.success(request, f"Invitado {guest.name} agregado y correo enviado.")
            else:
                messages.success(request, f"Invitado {guest.name} agregado correctamente (sin correo).")
            
            return redirect('dashboard')
        except ValidationError as e:
            messages.error(request, "Error de validación al agregar invitado.")

    guests = Guest.objects.all().order_by('-created_at')
    total = guests.count()
    checked_in = guests.filter(is_checked_in=True).count()
    
    context = {
        'guests': guests,
        'total': total,
        'checked_in': checked_in,
    }
    return render(request, 'events/dashboard.html', context)

@login_required
def edit_guest(request, pk):
    if not request.user.is_superuser:
        return redirect('scanner')
    guest = get_object_or_404(Guest, pk=pk)
    if request.method == 'POST':
        guest.name = request.POST.get('name')
        guest.email = request.POST.get('email')
        guest.phone = request.POST.get('phone')
        guest.passes = int(request.POST.get('passes', 1))
        guest.role = request.POST.get('role')
        guest.save()
        return redirect('dashboard')
        
    return render(request, 'events/guest_form.html', {'guest': guest})

@login_required
def delete_guest(request, pk):
    if not request.user.is_superuser:
        return redirect('scanner')
    guest = get_object_or_404(Guest, pk=pk)
    guest.delete()
    return redirect('dashboard')

@login_required
def qr_code_image(request, uuid):
    guest = get_object_or_404(Guest, uuid=uuid)
    
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(str(guest.uuid))
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return HttpResponse(buffer.getvalue(), content_type="image/png")

@login_required
def scanner(request):
    return render(request, 'events/scanner.html')

@csrf_exempt
def api_check_in(request):
    if request.method == 'POST':
        uuid_val = request.POST.get('uuid')
        if not uuid_val:
            return JsonResponse({'success': False, 'message': 'No UUID provided'}, status=400)
            
        try:
            guest = Guest.objects.get(uuid=uuid_val)
            if guest.is_checked_in:
                return JsonResponse({
                    'success': True,
                    'already_checked': True,
                    'message': 'El invitado ya ha ingresado previamente.',
                    'guest': {
                        'name': guest.name,
                        'role': guest.role,
                        'raffle_number': guest.raffle_number
                    }
                })
                
            guest.is_checked_in = True
            guest.save()
            return JsonResponse({
                'success': True,
                'already_checked': False,
                'message': 'Check-in exitoso.',
                'guest': {
                    'name': guest.name,
                    'role': guest.role,
                    'raffle_number': guest.raffle_number
                }
            })
        except Guest.DoesNotExist:
            return JsonResponse({'success': False, 'message': 'Código QR no válido. Invitado no encontrado.'}, status=404)
            
    return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

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
    headers = ['Nombre', 'Correo', 'Teléfono', 'Rol', 'No. Rifa', 'Asistió', 'Fecha Registro']
    ws.append(headers)
    
    # Data
    guests = Guest.objects.all().order_by('-created_at')
    for guest in guests:
        status = 'Sí' if guest.is_checked_in else 'No'
        date_str = guest.created_at.strftime('%Y-%m-%d %H:%M')
        ws.append([
            guest.name, 
            guest.email or '', 
            guest.phone or '', 
            guest.role, 
            guest.raffle_number, 
            status, 
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
    
    data = [['Nombre', 'Rol', 'No. Rifa', 'Asistió']]
    
    guests = Guest.objects.all().order_by('name')
    for guest in guests:
        status = 'Sí' if guest.is_checked_in else 'No'
        data.append([
            guest.name[:30], 
            guest.role, 
            str(guest.raffle_number), 
            status
        ])
        
    t = Table(data, colWidths=[250, 100, 60, 60])
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
                # Expected columns: Nombre, Correo, Teléfono, Pases, Rol
                if not row[0]: # Nombre is required
                    continue
                    
                name = str(row[0])
                email = str(row[1]) if row[1] else ''
                phone = str(row[2]) if row[2] else ''
                
                try:
                    passes = int(row[3]) if row[3] else 1
                except (ValueError, TypeError):
                    passes = 1
                    
                role = str(row[4]) if row[4] else 'Docente'
                
                guest = Guest(name=name, email=email, phone=phone, passes=passes, role=role)
                try:
                    guest.save()
                    imported_count += 1
                    
                    if guest.email:
                        if send_guest_email(guest):
                            email_count += 1
                except ValidationError:
                    continue # Skip if error saving (e.g. no more raffle numbers)
                    
            messages.success(request, f'¡Importación completada! Se registraron {imported_count} invitados y se enviaron {email_count} correos automáticos.')
            
        except Exception as e:
            messages.error(request, f'Error al procesar el archivo Excel: {str(e)}')
            
    return redirect('dashboard')
