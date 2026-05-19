from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('guest/<int:pk>/edit/', views.edit_guest, name='edit_guest'),
    path('guest/<int:pk>/delete/', views.delete_guest, name='delete_guest'),
    path('guest/<int:pk>/resend/', views.resend_email, name='resend_email'),
    path('export/excel/', views.export_excel, name='export_excel'),
    path('export/pdf/', views.export_pdf, name='export_pdf'),
    path('import/excel/', views.import_excel, name='import_excel'),
    path('scanner/', views.scanner, name='scanner'),
    path('api/checkin/', views.api_check_in, name='api_check_in'),
    path('qr/<uuid:uuid>.png', views.qr_code_image, name='qr_code_image'),
]
