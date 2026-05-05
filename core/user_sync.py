from __future__ import annotations

from typing import Tuple

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from . import models


AUTO_PASSWORD_PREFIX = 'auto::'


def split_full_name(full_name: str) -> Tuple[str, str]:
    parts = [p for p in (full_name or '').strip().split() if p]
    if not parts:
        return '', ''
    if len(parts) == 1:
        return parts[0], ''
    return parts[0], ' '.join(parts[1:])


def normalize_email(value: str) -> str:
    return (value or '').strip().lower()


def preferred_profile_email(profile: models.Usuario) -> str:
    return normalize_email(profile.correo_corporativo or (profile.auth_user.email if profile.auth_user_id else ''))


def preferred_user_email(user: User) -> str:
    if user.email:
        return normalize_email(user.email)
    if '@' in (user.username or ''):
        return normalize_email(user.username)
    return ''


def build_username_from_email(email: str, *, fallback: str = 'usuario') -> str:
    email = normalize_email(email)
    return email or fallback


def ensure_unique_username(base_username: str, *, exclude_user_id: int | None = None) -> str:
    base = (base_username or 'usuario').strip() or 'usuario'
    qs = User.objects.all()
    if exclude_user_id:
        qs = qs.exclude(pk=exclude_user_id)
    if not qs.filter(username__iexact=base).exists():
        return base
    suffix = 2
    while True:
        candidate = f'{base}__{suffix}'
        if not qs.filter(username__iexact=candidate).exists():
            return candidate
        suffix += 1


@transaction.atomic

def sync_profile_from_auth_user(user: User) -> models.Usuario:
    email = preferred_user_email(user)
    if email and user.email != email:
        user.email = email
        user.save(update_fields=['email'])
    full_name = f'{(user.first_name or "").strip()} {(user.last_name or "").strip()}'.strip() or user.username or email
    profile = None

    if hasattr(user, 'perfil_reliability'):
        try:
            profile = user.perfil_reliability
        except models.Usuario.DoesNotExist:
            profile = None

    if profile is not None and getattr(profile, 'is_deleted', False):
        profile = None

    if profile is None:
        profile = models.Usuario.objects.filter(auth_user_id=user.id).first()
    if profile is None and email:
        profile = models.Usuario.objects.filter(correo_corporativo__iexact=email).first()

    deleted_profile = None
    if profile is None:
        deleted_profile = models.Usuario.all_objects.filter(auth_user_id=user.id).exclude(archivo_eliminacion__isnull=True).first()
    if deleted_profile is None and email:
        deleted_profile = models.Usuario.all_objects.filter(correo_corporativo__iexact=email).exclude(archivo_eliminacion__isnull=True).first()
    if deleted_profile is not None:
        return deleted_profile

    defaults = {
        'nombre_completo': full_name,
        'correo_corporativo': email or user.username,
    }

    if profile is None:
        profile = models.Usuario.objects.create(auth_user=user, **defaults)
        return profile

    changed = []
    if profile.auth_user_id != user.id:
        profile.auth_user = user
        changed.append('auth_user')
    if defaults['nombre_completo'] and profile.nombre_completo != defaults['nombre_completo']:
        profile.nombre_completo = defaults['nombre_completo']
        changed.append('nombre_completo')
    if defaults['correo_corporativo'] and profile.correo_corporativo != defaults['correo_corporativo']:
        profile.correo_corporativo = defaults['correo_corporativo']
        changed.append('correo_corporativo')
    if changed:
        profile.save(update_fields=changed)
    return profile


@transaction.atomic

def sync_auth_user_from_profile(profile: models.Usuario, *, raw_password: str | None = None) -> User:
    email = preferred_profile_email(profile)
    first_name, last_name = split_full_name(profile.nombre_completo)

    user = None
    if getattr(profile, 'is_deleted', False):
        return profile.auth_user if profile.auth_user_id else None

    if profile.auth_user_id:
        user = profile.auth_user
    if user is None and email:
        user = User.objects.filter(email__iexact=email).first() or User.objects.filter(username__iexact=email).first()

    if user is None:
        username = ensure_unique_username(build_username_from_email(email, fallback=f'usuario_{profile.pk or "nuevo"}'))
        user = User(
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
            is_active=True,
        )
        if raw_password:
            user.set_password(raw_password)
        else:
            user.set_unusable_password()
        user.save()
    else:
        changed = []
        desired_email = email or user.email
        desired_username = build_username_from_email(desired_email, fallback=user.username or f'usuario_{user.pk}')
        desired_username = ensure_unique_username(desired_username, exclude_user_id=user.id)
        if user.username != desired_username:
            user.username = desired_username
            changed.append('username')
        if user.email != desired_email:
            user.email = desired_email
            changed.append('email')
        if user.first_name != first_name:
            user.first_name = first_name
            changed.append('first_name')
        if user.last_name != last_name:
            user.last_name = last_name
            changed.append('last_name')
        if raw_password:
            user.set_password(raw_password)
            changed.append('password')
        if changed:
            user.save(update_fields=changed)

    if profile.auth_user_id != user.id:
        profile.auth_user = user
        profile.save(update_fields=['auth_user'])
    return user


@transaction.atomic
def archive_profile(profile: models.Usuario, *, deleted_by: User | None = None, reason: str = ''):
    archived, _ = models.UsuarioEliminado.objects.update_or_create(
        usuario=profile,
        defaults={
            'auth_user': profile.auth_user,
            'nombre_completo': profile.nombre_completo,
            'correo_corporativo': profile.correo_corporativo or '',
            'cargo': profile.cargo,
            'empresa': profile.empresa,
            'eliminado_en': timezone.now(),
            'eliminado_por': deleted_by,
            'motivo': (reason or '')[:255],
        }
    )

    models.AccesoUsuario.objects.filter(usuario=profile).delete()

    if profile.auth_user_id:
        user = profile.auth_user
        changes = []

        archived_username = f'archivado_{user.username or "usuario"}'
        if user.username != archived_username:
            user.username = ensure_unique_username(archived_username, exclude_user_id=user.id)
            changes.append('username')

        if user.email:
            user.email = f'archived+{profile.pk}@disabled.local'
            changes.append('email')

        if user.is_active:
            user.is_active = False
            changes.append('is_active')

        if user.is_staff:
            user.is_staff = False
            changes.append('is_staff')

        if user.is_superuser:
            user.is_superuser = False
            changes.append('is_superuser')

        if changes:
            user.save(update_fields=list(dict.fromkeys(changes)))

    return archived