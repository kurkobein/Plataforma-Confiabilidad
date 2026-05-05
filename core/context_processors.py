from collections import OrderedDict

from .access import get_accessible_services, get_profile_for_user
from .registry import MODEL_REGISTRY


def navigation_context(request):
    groups = OrderedDict()
    service_links = []

    if getattr(request.user, 'is_authenticated', False):
        service_links = list(get_accessible_services(request.user)[:20])
        if request.user.is_superuser:
            for key, config in MODEL_REGISTRY.items():
                if not config.get('show_in_sidebar', True):
                    continue
                groups.setdefault(config['group'], []).append({
                    'key': key,
                    'label': config['label'],
                })

    return {
        'navigation_groups': groups,
        'service_links': service_links,
    }

def empresa_usuario(request):
    if not getattr(request.user, 'is_authenticated', False):
        return {'empresa_usuario': None, 'perfil_usuario': None}

    perfil = get_profile_for_user(request.user)
    empresa = perfil.empresa if perfil and perfil.empresa_id else None

    return {
        'empresa_usuario': empresa,
        'perfil_usuario': perfil,
    }