from django.contrib import admin
from .models import Guest, GuestRole, SatisfactionQuestion, SatisfactionResponse


@admin.register(GuestRole)
class GuestRoleAdmin(admin.ModelAdmin):
    search_fields = ('name',)

@admin.register(Guest)
class GuestAdmin(admin.ModelAdmin):
    list_display = ('name', 'guest_type', 'parent', 'role', 'school', 'gender', 'amount_paid', 'raffle_number', 'is_checked_in', 'created_at')
    list_filter = ('guest_type', 'role', 'school', 'gender', 'is_checked_in')
    search_fields = ('name', 'email', 'phone', 'school', 'raffle_number')


@admin.register(SatisfactionQuestion)
class SatisfactionQuestionAdmin(admin.ModelAdmin):
    list_display = ('text', 'question_type', 'is_active', 'order')
    list_editable = ('question_type', 'is_active', 'order')
    search_fields = ('text',)


@admin.register(SatisfactionResponse)
class SatisfactionResponseAdmin(admin.ModelAdmin):
    list_display = ('guest', 'question', 'rating', 'answer_text', 'created_at')
    list_filter = ('question', 'rating')
    search_fields = ('guest__name', 'answer_text')
