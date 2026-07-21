from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User, VerificationOTP


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ('email',)
    list_display = (
        'email',
        'full_name',
        'phone',
        'is_verified',
        'is_profile_complete',
        'has_transaction_pin',
        'is_staff',
        'is_active',
        'date_joined',
    )
    list_filter = (
        'is_verified',
        'is_profile_complete',
        'has_transaction_pin',
        'is_staff',
        'is_active',
    )
    search_fields = ('email', 'full_name', 'phone')
    readonly_fields = ('date_joined', 'updated_at', 'last_login')

    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        (
            'Profile',
            {
                'fields': (
                    'full_name',
                    'phone',
                    'profile_picture',
                )
            },
        ),
        (
            'Registration status',
            {
                'fields': (
                    'is_verified',
                    'is_profile_complete',
                    'has_transaction_pin',
                )
            },
        ),
        (
            'Permissions',
            {
                'fields': (
                    'is_active',
                    'is_staff',
                    'is_superuser',
                    'groups',
                    'user_permissions',
                )
            },
        ),
        ('Dates', {'fields': ('last_login', 'date_joined', 'updated_at')}),
    )
    add_fieldsets = (
        (
            None,
            {
                'classes': ('wide',),
                'fields': ('email', 'password1', 'password2', 'is_staff', 'is_superuser'),
            },
        ),
    )
    filter_horizontal = ('groups', 'user_permissions')


@admin.register(VerificationOTP)
class VerificationOTPAdmin(admin.ModelAdmin):
    list_display = (
        'user',
        'purpose',
        'is_used',
        'attempts',
        'created_at',
        'expires_at',
    )
    list_filter = ('purpose', 'is_used')
    search_fields = ('user__email',)
    readonly_fields = ('code_hash', 'created_at')
