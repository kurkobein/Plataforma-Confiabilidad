from core import models


def service_aca_matrix_queryset(service):
    if not service or not getattr(service, 'estrategia_id', None):
        return models.MatrizRiesgo.objects.none()
    return (
        models.MatrizRiesgo.objects
        .filter(estrategia_id=service.estrategia_id)
        .select_related(
            'dimension_probabilidad',
            'dimension_probabilidad__dimension',
            'dimension_impacto',
            'dimension_impacto__dimension',
        )
    )


def get_service_aca_matrix(service):
    queryset = service_aca_matrix_queryset(service)
    active_id = getattr(service, 'matriz_aca_activa_id', None)
    if active_id:
        active = queryset.filter(pk=active_id).first()
        if active:
            return active
    return queryset.order_by('-fecha_creado', '-id').first()


def set_service_aca_matrix(service, matrix):
    if not service or not matrix:
        raise ValueError('El servicio y la matriz son obligatorios.')
    if not service.estrategia_id or matrix.estrategia_id != service.estrategia_id:
        raise ValueError('La matriz debe pertenecer a la estrategia del servicio.')
    if service.matriz_aca_activa_id != matrix.pk:
        service.matriz_aca_activa = matrix
        service.save(update_fields=['matriz_aca_activa'])
    return service
