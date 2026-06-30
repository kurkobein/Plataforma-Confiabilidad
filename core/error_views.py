from django.http import HttpResponse
from django.template.loader import get_template


ERROR_CONTENT = {
    400: {
        'title': 'Solicitud no válida',
        'message': 'No fue posible procesar la solicitud enviada. Revisa los datos e intenta nuevamente.',
        'icon': 'bi-exclamation-triangle',
    },
    403: {
        'title': 'Acceso denegado',
        'message': 'No tienes permisos para acceder a esta sección o realizar esta acción.',
        'icon': 'bi-shield-lock',
    },
    404: {
        'title': 'Página no encontrada',
        'message': 'La dirección solicitada no existe, fue movida o el registro ya no está disponible.',
        'icon': 'bi-signpost-split',
    },
    500: {
        'title': 'Error interno',
        'message': 'Ocurrió un problema inesperado. Intenta nuevamente en unos minutos.',
        'icon': 'bi-tools',
    },
}


def _render_error(status_code):
    context = {
        'status_code': status_code,
        **ERROR_CONTENT[status_code],
    }
    try:
        template = get_template('error.html')
        return HttpResponse(template.render(context), status=status_code)
    except Exception:
        content = (
            '<!doctype html><html lang="es"><meta charset="utf-8">'
            f'<title>Error {status_code}</title>'
            f'<h1>{status_code}</h1><p>{context["title"]}</p>'
            '<a href="/">Volver al inicio</a></html>'
        )
        return HttpResponse(content, status=status_code)


def bad_request(request, exception=None):
    return _render_error(400)


def permission_denied(request, exception=None):
    return _render_error(403)


def page_not_found(request, exception=None):
    return _render_error(404)


def server_error(request):
    return _render_error(500)


def csrf_failure(request, reason=''):
    return _render_error(403)
