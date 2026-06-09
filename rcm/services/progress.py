from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Prefetch

from core import models


def _format_percent(value):
    if value is None:
        return 'N/A'
    quantized = Decimal(value).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    if quantized == quantized.to_integral():
        return f'{int(quantized)}%'
    return f'{quantized}%'


def _node_path(node, node_by_id):
    path = []
    current = node
    seen = set()
    while current and current.pk not in seen:
        seen.add(current.pk)
        path.append(current)
        current = node_by_id.get(current.parent_id)
    return list(reversed(path))


def _node_path_code(node, node_by_id):
    return '-'.join(item.codigo for item in _node_path(node, node_by_id) if item.codigo)


def _node_option_label(node, node_by_id):
    code = _node_path_code(node, node_by_id) or node.codigo
    level_name = node.nivel.nombre if node.nivel_id else 'Sin nivel'
    return f'{level_name}: {code} - {node.nombre}'


def get_fmeca_progress_dimensions(estrategia):
    if not estrategia:
        return []
    dimensions = (
        models.EstrategiaDimension.objects.filter(
            estrategia=estrategia,
            activo=True,
            considerar_avance_fmeca=True,
            proceso_uso__in=[
                models.EstrategiaDimension.PROCESO_FMECA,
                models.EstrategiaDimension.PROCESO_AMBOS,
            ],
        )
        .select_related('dimension')
        .order_by('orden', 'id')
    )
    return list(dimensions)


def evaluacion_has_value(record):
    if record is None:
        return False
    if record.valor_numerico is not None:
        return True
    if record.valor_booleano is not None:
        return True
    if (record.valor_texto or '').strip():
        return True
    if record.catalogo_fila_id:
        return True
    if record.escala_valor_id:
        return True
    return False


def _records_by_dimension(records):
    by_strategy_dimension = {}
    by_dimension = {}

    for record in records:
        if record.estrategia_dimension_id:
            by_strategy_dimension.setdefault(record.estrategia_dimension_id, []).append(record)

        dimension_id = None
        if record.estrategia_dimension_id and record.estrategia_dimension:
            dimension_id = record.estrategia_dimension.dimension_id

        if dimension_id:
            by_dimension.setdefault(dimension_id, []).append(record)

    return by_strategy_dimension, by_dimension

def compute_fmeca_progress(fmea, progress_dimensions):
    records = list(fmea.evaluaciones.all())
    by_strategy_dimension, by_dimension = _records_by_dimension(records)
    total = len(progress_dimensions)
    if total == 0:
        return {
            'total_dimensions': 0,
            'completed_dimensions': 0,
            'missing_dimensions': [],
            'missing_dimension_ids': [],
            'progress_percent': None,
            'progress_label': 'N/A',
            'progress_degrees': 0,
        }

    completed = 0
    missing = []
    for estrategia_dimension in progress_dimensions:
        candidates = by_strategy_dimension.get(estrategia_dimension.pk) or by_dimension.get(estrategia_dimension.dimension_id) or []
        if any(evaluacion_has_value(record) for record in candidates):
            completed += 1
        else:
            missing.append(estrategia_dimension)

    percent = (Decimal(completed) / Decimal(total) * Decimal('100')).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    return {
        'total_dimensions': total,
        'completed_dimensions': completed,
        'missing_dimensions': missing,
        'missing_dimension_ids': [item.pk for item in missing],
        'progress_percent': percent,
        'progress_label': _format_percent(percent),
        'progress_degrees': float(percent * Decimal('3.6')),
    }


def get_fmeca_queryset(servicio):
    return (
        models.FMEA_FMECA.objects.filter(rcm__carga__servicio=servicio)
        .select_related('rcm', 'rcm__carga', 'rcm__equipo', 'rcm__equipo__nodo', 'rcm__equipo__nodo__nivel')
        .prefetch_related(
            Prefetch(
                'evaluaciones',
                queryset=models.EvaluacionFMEA.objects.select_related(
                    'estrategia_dimension',
                    'estrategia_dimension__dimension',
                    'catalogo_fila',
                    'escala_valor',
                ),
            ),
            'tareas_rcm__tipo_tarea_estrategia',
        )
    )


def _build_progress_summary_for_records(records, progress_dimensions):
    progress_by_fmea_id = {}
    missing_counts = {dimension.pk: 0 for dimension in progress_dimensions}

    for fmea in records:
        progress = compute_fmeca_progress(fmea, progress_dimensions)
        progress_by_fmea_id[fmea.pk] = progress
        for dimension_id in progress['missing_dimension_ids']:
            missing_counts[dimension_id] = missing_counts.get(dimension_id, 0) + 1

    progress_available = bool(progress_dimensions)
    total_records = len(records)
    if progress_available and total_records:
        average = (
            sum((item['progress_percent'] or Decimal('0')) for item in progress_by_fmea_id.values())
            / Decimal(total_records)
        ).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    else:
        average = None

    complete_records = 0
    incomplete_records = 0
    if progress_available:
        for item in progress_by_fmea_id.values():
            if item['progress_percent'] == Decimal('100.0'):
                complete_records += 1
            else:
                incomplete_records += 1

    complete_percent = None
    if total_records:
        complete_percent = (Decimal(complete_records) / Decimal(total_records) * Decimal('100')).quantize(
            Decimal('0.1'),
            rounding=ROUND_HALF_UP,
        )

    missing_dimensions = []
    dimension_by_id = {dimension.pk: dimension for dimension in progress_dimensions}
    for dimension_id, count in missing_counts.items():
        dimension = dimension_by_id.get(dimension_id)
        if not dimension:
            continue
        missing_percent = None
        if total_records:
            missing_percent = (Decimal(count) / Decimal(total_records) * Decimal('100')).quantize(
                Decimal('0.1'),
                rounding=ROUND_HALF_UP,
            )
        missing_dimensions.append({
            'id': dimension_id,
            'name': dimension.dimension.nombre,
            'missing_count': count,
            'missing_percent': missing_percent,
            'missing_percent_label': _format_percent(missing_percent) if total_records else '0%',
        })

    missing_dimensions.sort(key=lambda item: (-item['missing_count'], item['name']))
    major_gap = next((item for item in missing_dimensions if item['missing_count'] > 0), None)

    return {
        'progress_available': progress_available,
        'dimension_count': len(progress_dimensions),
        'total_records': total_records,
        'average_progress_percent': average,
        'average_progress_label': _format_percent(average),
        'average_progress_degrees': float((average or Decimal('0')) * Decimal('3.6')),
        'complete_records': complete_records,
        'complete_records_percent': complete_percent,
        'complete_records_label': _format_percent(complete_percent),
        'complete_records_degrees': float((complete_percent or Decimal('0')) * Decimal('3.6')),
        'incomplete_records': incomplete_records,
        'major_gap': major_gap,
        'missing_dimensions': missing_dimensions,
        'progress_by_fmea_id': progress_by_fmea_id,
    }


def build_fmeca_service_progress_summary(servicio, fmeas=None):
    progress_dimensions = get_fmeca_progress_dimensions(getattr(servicio, 'estrategia', None))
    records = list(fmeas) if fmeas is not None else list(get_fmeca_queryset(servicio))
    return _build_progress_summary_for_records(records, progress_dimensions)


def get_hierarchy_filter_options(servicio):
    leaf_node_ids = set(
        models.FMEA_FMECA.objects.filter(
            rcm__carga__servicio=servicio,
            rcm__equipo__nodo_id__isnull=False,
        ).values_list('rcm__equipo__nodo_id', flat=True)
    )
    all_nodes = list(
        models.NodoJerarquia.objects.filter(
            empresa=servicio.empresa,
            activo=True,
        ).select_related('nivel').order_by('nivel__orden', 'orden', 'codigo', 'nombre')
    )
    node_by_id = {node.pk: node for node in all_nodes}
    available_node_ids = set()
    for node_id in leaf_node_ids:
        node = node_by_id.get(node_id)
        for path_node in _node_path(node, node_by_id):
            available_node_ids.add(path_node.pk)

    available_nodes = [node for node in all_nodes if node.pk in available_node_ids]
    level_ids = {node.nivel_id for node in available_nodes if node.nivel_id}
    levels = [
        {'id': level.pk, 'name': level.nombre, 'order': level.orden}
        for level in models.NivelJerarquia.objects.filter(
            empresa=servicio.empresa,
            activo=True,
            pk__in=level_ids,
        ).order_by('orden', 'nombre')
    ]
    nodes = [
        {
            'id': node.pk,
            'level_id': node.nivel_id,
            'level_name': node.nivel.nombre if node.nivel_id else 'Sin nivel',
            'level_order': node.nivel.orden if node.nivel_id else 9999,
            'label': _node_option_label(node, node_by_id),
            'path': _node_path_code(node, node_by_id) or node.codigo,
        }
        for node in available_nodes
    ]
    nodes.sort(key=lambda item: (item['level_order'], item['path'], item['label']))
    return {'levels': levels, 'nodes': nodes}


def get_descendant_node_ids(nodo):
    if not nodo:
        return []
    if not isinstance(nodo, models.NodoJerarquia):
        try:
            nodo = int(nodo)
        except (TypeError, ValueError):
            return []
        nodo = models.NodoJerarquia.objects.filter(pk=nodo).select_related('empresa').first()
    if not nodo:
        return []

    rows = list(
        models.NodoJerarquia.objects.filter(
            empresa=nodo.empresa,
            activo=True,
        ).values('id', 'parent_id')
    )
    children_by_parent = {}
    for row in rows:
        children_by_parent.setdefault(row['parent_id'], []).append(row['id'])

    descendant_ids = []
    stack = [nodo.pk]
    seen = set()
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        descendant_ids.append(node_id)
        stack.extend(children_by_parent.get(node_id, []))
    return descendant_ids


def filter_fmeca_by_hierarchy(queryset, nodo_id=None):
    if not nodo_id:
        return queryset
    node_ids = get_descendant_node_ids(nodo_id)
    if not node_ids:
        return queryset.none()
    return queryset.filter(rcm__equipo__nodo_id__in=node_ids)
