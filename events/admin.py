from django.contrib import admin
from .models import Guest

@admin.register(Guest)
class GuestAdmin(admin.ModelAdmin):
    list_display = ('name', 'role', 'raffle_number', 'is_checked_in', 'created_at')
    list_filter = ('role', 'is_checked_in')
    search_fields = ('name', 'email', 'raffle_number')
