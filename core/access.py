from django.contrib.auth.models import User
from django.db.models import Q

from . import models


def get_profile_for_user(user):
    if not getattr(user, 'is_authenticated', False):
        return None
    if getattr(user, 'is_superuser', False):
        return models.Usuario.objects.filter(auth_user_id=user.id).first() or models.Usuario.objects.filter(correo_corporativo__iexact=(user.email or '')).first()
    profile = getattr(user, 'perfil_reliability', None)
    if profile:
        return profile
    profile = models.Usuario.objects.filter(auth_user_id=user.id).first()
    if profile:
        return profile
    email = (user.email or '').strip()
    if email:
        return models.Usuario.objects.filter(correo_corporativo__iexact=email).first()
    return None


def resolve_auth_user_for_email(email):
    email = (email or '').strip()
    if not email:
        return None, None
    profile = models.Usuario.objects.select_related('auth_user').filter(correo_corporativo__iexact=email).first()
    if profile and profile.auth_user_id:
        return profile.auth_user, profile
    user = User.objects.filter(email__iexact=email).first()
    if user:
        if profile and not profile.auth_user_id:
            profile.auth_user = user
            profile.save(update_fields=['auth_user'])
        return user, profile
    if profile and profile.auth_user_id:
        return profile.auth_user, profile
    return None, profile


def _seed_owner_access(profile, servicio):
    if not profile or not servicio:
        return None
    if profile.id not in {servicio.creado_por_usuario_id, servicio.responsable_usuario_id}:
        return None
    acceso, _ = models.AccesoUsuario.objects.get_or_create(
        servicio=servicio,
        usuario=profile,
        defaults={
            'creado_en': servicio.creado_en,
            'empresa': servicio.empresa,
            'estrategia': servicio.estrategia,
            'puede_ver': True,
            'puede_editar': True,
            'puede_ver_todo': False,
        },
    )
    updates = []
    if acceso.empresa_id != servicio.empresa_id:
        acceso.empresa = servicio.empresa
        updates.append('empresa')
    if acceso.estrategia_id != servicio.estrategia_id:
        acceso.estrategia = servicio.estrategia
        updates.append('estrategia')
    if not acceso.puede_ver:
        acceso.puede_ver = True
        updates.append('puede_ver')
    if not acceso.puede_editar:
        acceso.puede_editar = True
        updates.append('puede_editar')
    if acceso.puede_ver_todo:
        acceso.puede_ver_todo = False
        updates.append('puede_ver_todo')
    if updates:
        acceso.save(update_fields=updates)
    return acceso


def _service_access_rows(servicio):
    return models.AccesoUsuario.objects.filter(servicio=servicio).select_related('usuario', 'usuario__cargo', 'usuario__empresa').order_by('usuario__nombre_completo')


def get_accessible_services(user):
    qs = models.Servicio.objects.select_related(
        'empresa', 'estrategia', 'creado_por_usuario', 'responsable_usuario'
    ).order_by('-creado_en', 'codigo_servicio')
    if not getattr(user, 'is_authenticated', False):
        return qs.none()
    if getattr(user, 'is_superuser', False):
        return qs

    profile = get_profile_for_user(user)
    if not profile:
        return qs.none()

    owned_ids = list(
        qs.filter(Q(creado_por_usuario_id=profile.id) | Q(responsable_usuario_id=profile.id)).values_list('id', flat=True)
    )
    if owned_ids:
        owned_services = list(qs.filter(id__in=owned_ids))
        for servicio in owned_services:
            _seed_owner_access(profile, servicio)

    servicio_ids = list(
        models.AccesoUsuario.objects.filter(usuario=profile, servicio_id__isnull=False)
        .filter(Q(puede_ver=True) | Q(puede_editar=True))
        .values_list('servicio_id', flat=True)
    )
    return qs.filter(id__in=servicio_ids).distinct()



def get_editable_services(user):
    qs = models.Servicio.objects.select_related(
        'empresa', 'estrategia', 'creado_por_usuario', 'responsable_usuario'
    ).order_by('-creado_en', 'codigo_servicio')
    if not getattr(user, 'is_authenticated', False):
        return qs.none()
    if getattr(user, 'is_superuser', False):
        return qs

    profile = get_profile_for_user(user)
    if not profile:
        return qs.none()

    owned_ids = list(
        qs.filter(Q(creado_por_usuario_id=profile.id) | Q(responsable_usuario_id=profile.id)).values_list('id', flat=True)
    )
    if owned_ids:
        owned_services = list(qs.filter(id__in=owned_ids))
        for servicio in owned_services:
            _seed_owner_access(profile, servicio)

    servicio_ids = list(
        models.AccesoUsuario.objects.filter(
            usuario=profile,
            servicio_id__isnull=False,
            puede_editar=True,
        ).values_list('servicio_id', flat=True)
    )
    return qs.filter(id__in=servicio_ids).distinct()

def get_service_permission(user, servicio):
    if not getattr(user, 'is_authenticated', False):
        return {
            'can_view': False,
            'can_edit': False,
            'can_manage_access': False,
            'profile': None,
            'access_rows': models.AccesoUsuario.objects.none(),
        }
    if getattr(user, 'is_superuser', False):
        return {
            'can_view': True,
            'can_edit': True,
            'can_manage_access': True,
            'profile': get_profile_for_user(user),
            'access_rows': _service_access_rows(servicio),
        }

    profile = get_profile_for_user(user)
    if not profile:
        return {
            'can_view': False,
            'can_edit': False,
            'can_manage_access': False,
            'profile': None,
            'access_rows': models.AccesoUsuario.objects.none(),
        }

    _seed_owner_access(profile, servicio)
    row = models.AccesoUsuario.objects.filter(usuario=profile, servicio=servicio).first()
    can_view = bool(row and (row.puede_ver or row.puede_editar))
    can_edit = bool(row and row.puede_editar)

    return {
        'can_view': can_view,
        'can_edit': can_edit,
        'can_manage_access': can_edit,
        'profile': profile,
        'access_rows': _service_access_rows(servicio),
    }


def get_service_equipment(servicio):
    base_qs = models.Equipo.objects.select_related('nodo', 'nodo__empresa').order_by('tag_equipo', 'nombre_equipo')
    if not servicio:
        return base_qs.none()

    return base_qs.filter(
        Q(servicios_equipo__servicio_id=servicio.pk) | Q(nodo__empresa_id=servicio.empresa_id)
    ).distinct()


def get_users_for_service_access(servicio):
    return models.Usuario.objects.select_related('cargo', 'empresa').order_by('empresa__nombre', 'nombre_completo')


def is_mindco_user(user):
    if not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False):
        return True

    profile = get_profile_for_user(user)
    empresa = getattr(profile, 'empresa', None)
    if not empresa:
        return False

    candidates = [
        (empresa.nombre or '').strip().lower(),
        (empresa.sigla or '').strip().lower(),
    ]
    return any('mindco' in value for value in candidates if value)
