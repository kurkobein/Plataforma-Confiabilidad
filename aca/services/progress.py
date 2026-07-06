from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Prefetch

from core import models


def _is_generated_matrix_axis_dimension(estrategia_dimension):
    if not estrategia_dimension:
        return False
    name = (estrategia_dimension.dimension.nombre or '').strip().lower()
    return name.startswith((
        'eje x - ',
        'eje y - ',
        'probabilidad - ',
        'impacto - ',
        'consecuencia - ',
    ))


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
    name = node.nombre or node.codigo or 'Sin nombre'
    return f'{name} - {code}' if code else name


def get_aca_progress_dimensions(estrategia):
    if not estrategia:
        return []
    dimensions = (
        models.EstrategiaDimension.objects.filter(
            estrategia=estrategia,
            activo=True,
            considerar_avance_aca=True,
            proceso_uso__in=[
                models.EstrategiaDimension.PROCESO_ACA,
                models.EstrategiaDimension.PROCESO_AMBOS,
            ],
        )
        .select_related('dimension')
        .order_by('orden', 'id')
    )
    return [item for item in dimensions if not _is_generated_matrix_axis_dimension(item)]


def criticidad_dimension_has_value(record):
    if record is None:
        return False
    if record.valor_numerico is not None:
        return True
    if record.valor_secundario is not None:
        return True
    if record.valor_booleano is not None:
        return True
    if (record.valor_texto or '').strip():
        return True
    if record.catalogo_fila_id:
        return True
    if record.escala_valor_id:
        return True
    if record.escala_unificada_id:
        return True
    return False


def _records_by_dimension(records):
    by_strategy_dimension = {}
    by_dimension = {}
    for record in records:
        if record.estrategia_dimension_id:
            by_strategy_dimension.setdefault(record.estrategia_dimension_id, []).append(record)
        if record.dimension_id:
            by_dimension.setdefault(record.dimension_id, []).append(record)
    return by_strategy_dimension, by_dimension


def compute_criticidad_progress(criticidad, progress_dimensions):
    records = list(criticidad.dimensiones.all())
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
        if any(criticidad_dimension_has_value(record) for record in candidates):
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


def get_aca_criticidad_queryset(servicio):
    return (
        models.Criticidad.objects.filter(aca_carga__servicio=servicio)
        .select_related(
            'equipo',
            'equipo__nodo',
            'equipo__nodo__nivel',
            'aca_carga',
            'matriz',
            'matriz_celda',
            'matriz_celda__matriz',
        )
        .prefetch_related(
            Prefetch(
                'dimensiones',
                queryset=models.CriticidadDimension.objects.select_related(
                    'dimension',
                    'estrategia_dimension',
                    'catalogo_fila',
                    'escala_valor',
                    'escala_unificada',
                ),
            )
        )
    )


def _build_progress_summary_for_records(records, progress_dimensions):
    progress_by_criticidad_id = {}
    missing_counts = {dimension.pk: 0 for dimension in progress_dimensions}

    for criticidad in records:
        progress = compute_criticidad_progress(criticidad, progress_dimensions)
        progress_by_criticidad_id[criticidad.pk] = progress
        for dimension_id in progress['missing_dimension_ids']:
            missing_counts[dimension_id] = missing_counts.get(dimension_id, 0) + 1

    progress_available = bool(progress_dimensions)
    total_records = len(records)
    if progress_available and total_records:
        average = (
            sum((item['progress_percent'] or Decimal('0')) for item in progress_by_criticidad_id.values())
            / Decimal(total_records)
        ).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    else:
        average = None

    complete_records = 0
    incomplete_records = 0
    if progress_available:
        for item in progress_by_criticidad_id.values():
            if item['progress_percent'] == Decimal('100.0'):
                complete_records += 1
            else:
                incomplete_records += 1
    complete_percent = None
    incomplete_percent = None
    if total_records:
        complete_percent = (Decimal(complete_records) / Decimal(total_records) * Decimal('100')).quantize(
            Decimal('0.1'),
            rounding=ROUND_HALF_UP,
        )
        incomplete_percent = (Decimal(incomplete_records) / Decimal(total_records) * Decimal('100')).quantize(
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
        'incomplete_records_percent': incomplete_percent,
        'incomplete_records_label': _format_percent(incomplete_percent),
        'major_gap': major_gap,
        'missing_dimensions': missing_dimensions,
        'progress_by_criticidad_id': progress_by_criticidad_id,
    }


def build_aca_service_progress_summary(servicio, criticidades=None):
    progress_dimensions = get_aca_progress_dimensions(getattr(servicio, 'estrategia', None))
    records = list(criticidades) if criticidades is not None else list(get_aca_criticidad_queryset(servicio))
    return _build_progress_summary_for_records(records, progress_dimensions)


def get_hierarchy_filter_options(servicio):
    leaf_node_ids = set(
        models.Criticidad.objects.filter(
            aca_carga__servicio=servicio,
            equipo__nodo_id__isnull=False,
        ).values_list('equipo__nodo_id', flat=True)
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
        {
            'id': level.pk,
            'name': level.nombre,
            'order': level.orden,
        }
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
            'parent_id': node.parent_id,
            'ancestor_ids': [
                path_node.pk
                for path_node in _node_path(node, node_by_id)
                if path_node.pk != node.pk
            ],
        }
        for node in available_nodes
    ]
    nodes.sort(key=lambda item: (item['level_order'], item['path'], item['label']))
    return {
        'levels': levels,
        'nodes': nodes,
    }


def compatible_hierarchy_node_ids(hierarchy_filters, selected_node_ids):
    available_nodes = {
        int(node['id']): node
        for node in hierarchy_filters.get('nodes', [])
        if node.get('id')
    }
    requested_ids = {
        int(node_id)
        for node_id in selected_node_ids
        if str(node_id).isdigit() and int(node_id) in available_nodes
    }
    if not requested_ids:
        return set()

    valid_ids = set()
    selected_ancestor_groups = []
    for level in hierarchy_filters.get('levels', []):
        level_id = int(level['id'])
        level_ids = {
            node_id
            for node_id in requested_ids
            if int(available_nodes[node_id].get('level_id') or 0) == level_id
        }
        if not level_ids:
            continue

        compatible_ids = set()
        for node_id in level_ids:
            ancestor_ids = {
                int(ancestor_id)
                for ancestor_id in available_nodes[node_id].get('ancestor_ids', [])
            }
            if all(ancestor_ids.intersection(group) for group in selected_ancestor_groups):
                compatible_ids.add(node_id)

        if compatible_ids:
            valid_ids.update(compatible_ids)
            selected_ancestor_groups.append(compatible_ids)

    return valid_ids


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


def _normalize_node_ids(value):
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = str(value).replace(';', ',').split(',')
    node_ids = []
    for raw in raw_values:
        try:
            node_id = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if node_id not in node_ids:
            node_ids.append(node_id)
    return node_ids


def filter_criticidades_by_hierarchy(queryset, nodo_id=None):
    node_ids_input = _normalize_node_ids(nodo_id)
    if not node_ids_input:
        return queryset
    node_ids = []
    for selected_node_id in node_ids_input:
        node_ids.extend(get_descendant_node_ids(selected_node_id))
    node_ids = list(dict.fromkeys(node_ids))
    if not node_ids:
        return queryset.none()
    return queryset.filter(equipo__nodo_id__in=node_ids)


def group_progress_by_hierarchy_level(queryset, level_id, progress_dimensions):
    if not level_id:
        return []
    try:
        level_id = int(level_id)
    except (TypeError, ValueError):
        return []

    records = list(queryset)
    node_ids = {
        record.equipo.nodo_id
        for record in records
        if getattr(record, 'equipo', None) and record.equipo.nodo_id
    }
    if node_ids:
        company_ids = {
            record.equipo.nodo.empresa_id
            for record in records
            if getattr(record, 'equipo', None) and getattr(record.equipo, 'nodo', None)
        }
        all_nodes = list(
            models.NodoJerarquia.objects.filter(
                empresa_id__in=company_ids,
                activo=True,
            ).select_related('nivel')
        )
    else:
        all_nodes = []
    node_by_id = {node.pk: node for node in all_nodes}

    grouped = {}
    for record in records:
        equipo = getattr(record, 'equipo', None)
        node = node_by_id.get(getattr(equipo, 'nodo_id', None))
        group_node = None
        for path_node in _node_path(node, node_by_id):
            if path_node.nivel_id == level_id:
                group_node = path_node
                break
        if group_node:
            group_key = f'node:{group_node.pk}'
            group_label = _node_option_label(group_node, node_by_id)
            group_display_label = group_node.nombre or group_node.codigo or group_label
            group_order = (_node_path_code(group_node, node_by_id), group_node.nombre)
        else:
            group_key = 'missing'
            group_display_label = 'Sin jerarquia'
            group_label = 'Sin jerarquía'
            group_order = ('zzzz', 'Sin jerarquía')
        grouped.setdefault(group_key, {
            'key': group_key,
            'label': group_label,
            'display_label': group_display_label,
            'order': group_order,
            'records': [],
        })['records'].append(record)

    summary = []
    for group in grouped.values():
        group_summary = _build_progress_summary_for_records(group['records'], progress_dimensions)
        summary.append({
            'key': group['key'],
            'label': group['label'],
            'display_label': group.get('display_label') or group['label'],
            'total_records': group_summary['total_records'],
            'average_progress_percent': group_summary['average_progress_percent'] or Decimal('0'),
            'average_progress_label': group_summary['average_progress_label'],
            'average_progress_degrees': group_summary['average_progress_degrees'],
            'complete_records': group_summary['complete_records'],
            'complete_records_label': group_summary['complete_records_label'],
            'complete_records_degrees': group_summary['complete_records_degrees'],
            'incomplete_records': group_summary['incomplete_records'],
            'major_gap': group_summary['major_gap'],
            'order': group['order'],
        })
    summary.sort(key=lambda item: item['order'])
    return summary
