import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from core import models


RULE_OPERATORS = {'>', '>=', '<', '<=', '=', '!=', 'entre'}
RULE_ACTIONS = {
    'criticidad_minima',
    'forzar_criticidad',
    'subir_niveles',
    'forzar_valor',
}


def _decimal(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value).strip().replace(',', '.'))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _key(value):
    value = _repair_text(value)
    value = unicodedata.normalize('NFKD', value.strip().lower())
    value = ''.join(char for char in value if not unicodedata.combining(char))
    return re.sub(r'[^a-z0-9]+', '_', value).strip('_')


def _repair_text(value):
    text = str(value or '').strip()
    if not text:
        return ''
    if 'Ã' in text or 'Â' in text:
        try:
            repaired = text.encode('latin1').decode('utf-8')
            if repaired:
                return repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return text


def matrix_rule_config(matrix):
    try:
        payload = json.loads(getattr(matrix, 'leyenda_json', '') or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return payload.get('reglas_criticidad', []) if isinstance(payload, dict) else []


def normalize_rules(raw_rules):
    rules = raw_rules if isinstance(raw_rules, list) else []
    normalized = []
    errors = []
    for index, raw_rule in enumerate(rules, start=1):
        if not isinstance(raw_rule, dict):
            errors.append(f'Regla {index}: formato invalido.')
            continue
        conditions = []
        for condition_index, raw_condition in enumerate(raw_rule.get('condiciones') or [], start=1):
            if not isinstance(raw_condition, dict):
                errors.append(f'Regla {index}, condicion {condition_index}: formato invalido.')
                continue
            source = str(raw_condition.get('fuente') or '').strip()
            operator = str(raw_condition.get('operador') or '=').strip()
            value = raw_condition.get('valor')
            second_value = raw_condition.get('valor_hasta')
            if not source:
                errors.append(f'Regla {index}, condicion {condition_index}: selecciona una fuente.')
            if operator not in RULE_OPERATORS:
                errors.append(f'Regla {index}, condicion {condition_index}: operador invalido.')
            if operator == 'entre' and isinstance(value, (list, tuple)):
                value, second_value = (list(value) + [None, None])[:2]
            conditions.append({
                'fuente': source,
                'operador': operator,
                'valor': value,
                'valor_hasta': second_value,
            })

        action = raw_rule.get('accion') if isinstance(raw_rule.get('accion'), dict) else {}
        action_type = str(action.get('tipo') or raw_rule.get('tipo_accion') or '').strip()
        action_value = action.get('valor', raw_rule.get('valor_accion'))
        if action_type not in RULE_ACTIONS:
            errors.append(f'Regla {index}: accion invalida.')
        if action_value in (None, ''):
            errors.append(f'Regla {index}: indica el valor de la accion.')
        elif action_type == 'subir_niveles':
            try:
                if int(action_value) < 1:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f'Regla {index}: los niveles a subir deben ser un entero mayor a 0.')
        elif action_type == 'forzar_valor' and _decimal(action_value) is None:
            errors.append(f'Regla {index}: el valor forzado debe ser numerico.')
        if not conditions:
            errors.append(f'Regla {index}: agrega al menos una condicion.')
        try:
            priority = int(raw_rule.get('prioridad') or 0)
        except (TypeError, ValueError):
            priority = 0
        normalized.append({
            'id': str(raw_rule.get('id') or f'regla_{index}'),
            'nombre': str(raw_rule.get('nombre') or f'Regla {index}').strip(),
            'activa': raw_rule.get('activa', True) is not False,
            'prioridad': priority,
            'combinador': 'OR' if str(raw_rule.get('combinador') or 'AND').upper() == 'OR' else 'AND',
            'condiciones': conditions,
            'accion': {'tipo': action_type, 'valor': action_value},
        })
    return normalized, errors


def dimension_source_values(records):
    values = {}
    for record in records or []:
        if isinstance(record, dict):
            ed = record.get('estrategia_dimension')
            dimension = record.get('dimension') or getattr(ed, 'dimension', None)
            numeric = record.get('valor_numerico')
            text = record.get('valor_texto')
        else:
            ed = getattr(record, 'estrategia_dimension', None)
            dimension = getattr(record, 'dimension', None) or getattr(ed, 'dimension', None)
            numeric = getattr(record, 'valor_numerico', None)
            text = getattr(record, 'valor_texto', None)
        value = numeric if numeric is not None else text
        if value in (None, '') or not ed:
            continue
        refs = [
            str(ed.pk),
            f'ed:{ed.pk}',
            f'estrategia_dimension:{ed.pk}',
        ]
        if dimension:
            refs.extend([
                str(dimension.pk),
                f'dim:{dimension.pk}',
                f'dimension:{dimension.pk}',
                dimension.nombre,
                _key(dimension.nombre),
            ])
        try:
            field_name = ed.catalogo.campo
        except models.DimensionCatalogo.DoesNotExist:
            field_name = ''
        if field_name:
            refs.extend([field_name, _key(field_name)])
        for ref in refs:
            if ref:
                values[str(ref)] = value
    return values


def _condition_value(source_values, source):
    source = str(source or '')
    if source in source_values:
        return source_values[source]
    return source_values.get(_key(source))


def _condition_matches(condition, source_values):
    actual = _condition_value(source_values, condition.get('fuente'))
    operator = condition.get('operador') or '='
    expected = condition.get('valor')
    expected_to = condition.get('valor_hasta')
    actual_number = _decimal(actual)
    expected_number = _decimal(expected)
    expected_to_number = _decimal(expected_to)

    if operator == 'entre':
        return (
            actual_number is not None
            and expected_number is not None
            and expected_to_number is not None
            and expected_number <= actual_number <= expected_to_number
        )
    if actual_number is not None and expected_number is not None:
        comparisons = {
            '>': actual_number > expected_number,
            '>=': actual_number >= expected_number,
            '<': actual_number < expected_number,
            '<=': actual_number <= expected_number,
            '=': actual_number == expected_number,
            '!=': actual_number != expected_number,
        }
        return comparisons.get(operator, False)
    actual_text = str(actual or '').strip().casefold()
    expected_text = str(expected or '').strip().casefold()
    if operator == '=':
        return actual_text == expected_text
    if operator == '!=':
        return actual_text != expected_text
    return False


def _matrix_cells(matrix):
    return list(
        matrix.celdas.select_related('probabilidad', 'impacto_nivel')
        .all()
        .order_by('resultado_num', 'id')
    ) if matrix else []


def _legend_items(matrix):
    try:
        payload = json.loads(getattr(matrix, 'leyenda_json', '') or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        items = payload.get('items') or payload.get('legend') or payload.get('leyenda') or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def _range_end(range_text):
    parts = re.split(r'\s*-\s*', str(range_text or '').strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    return _decimal(parts[1])


def matrix_cell_for_value(matrix, value, cells=None):
    value = _decimal(value)
    cells = cells if cells is not None else _matrix_cells(matrix)
    if value is None or not cells:
        return None
    candidates = [cell for cell in cells if cell.resultado_num <= value]
    return candidates[-1] if candidates else cells[0]


def matrix_cell_for_axis_values(matrix, probability_value, consequence_value):
    probability_value = _decimal(probability_value)
    consequence_value = _decimal(consequence_value)
    if not matrix:
        return None
    if probability_value is not None and consequence_value is not None:
        exact = (
            matrix.celdas.select_related('probabilidad', 'impacto_nivel')
            .filter(probabilidad__valor=probability_value, impacto_nivel__valor=consequence_value)
            .order_by('id')
            .first()
        )
        if exact:
            return exact
    try:
        config = json.loads(matrix.leyenda_json or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        config = {}
    if isinstance(config, dict) and config.get('modo_resolucion') == models.MatrizRiesgo.RESOLUCION_UMBRAL_RESULTADO:
        result = None
        if probability_value is not None and consequence_value is not None:
            result = probability_value * consequence_value
        elif consequence_value is not None:
            result = consequence_value
        elif probability_value is not None:
            result = probability_value
        return matrix_cell_for_value(matrix, result)
    return None


def _classification_levels(cells, matrix=None):
    levels = []
    seen = set()
    by_key = {}
    for cell in cells:
        normalized = _key(cell.clasificacion)
        if normalized and normalized not in seen:
            seen.add(normalized)
            level = {
                'key': normalized,
                'name': _repair_text(cell.clasificacion),
                'cell': cell,
                'color': getattr(cell, 'color', ''),
            }
            levels.append(level)
            by_key[normalized] = level
        elif normalized in by_key:
            by_key[normalized]['cell'] = cell
            by_key[normalized]['name'] = _repair_text(by_key[normalized]['name'] or cell.clasificacion)
            by_key[normalized]['color'] = by_key[normalized].get('color') or getattr(cell, 'color', '')
    for item in _legend_items(matrix):
        normalized = _key(item.get('name'))
        if not normalized:
            continue
        representative = matrix_cell_for_value(matrix, _range_end(item.get('range')), cells)
        if normalized in by_key:
            by_key[normalized]['name'] = _repair_text(item.get('name') or by_key[normalized]['name'])
            by_key[normalized]['color'] = item.get('color') or by_key[normalized].get('color') or getattr(representative, 'color', '')
            if representative:
                by_key[normalized]['cell'] = representative
            continue
        level = {
            'key': normalized,
            'name': _repair_text(item.get('name')),
            'cell': representative,
            'color': item.get('color') or getattr(representative, 'color', ''),
        }
        levels.append(level)
        by_key[normalized] = level
    return levels


def _target_level(value, levels):
    normalized = _key(value)
    if normalized in {'maxima', 'maximo', 'maximum', 'mas_alta'}:
        return levels[-1] if levels else None
    return next((level for level in levels if level['key'] == normalized), None)


def apply_criticality_rules(matrix, base_cell=None, base_value=None, base_classification='', source_values=None):
    cells = _matrix_cells(matrix)
    rules, _errors = normalize_rules(matrix_rule_config(matrix))
    source_values = dict(source_values or {})
    base_value = _decimal(base_value if base_value is not None else getattr(base_cell, 'resultado_num', None))
    base_classification = base_classification or getattr(base_cell, 'clasificacion', '') or ''
    final_cell = base_cell or matrix_cell_for_value(matrix, base_value, cells)
    final_value = base_value
    final_classification = base_classification or getattr(final_cell, 'clasificacion', '') or ''
    levels = _classification_levels(cells, matrix)
    level_index = {level['key']: index for index, level in enumerate(levels)}
    applied = []

    source_values.setdefault('criticidad_base_valor', base_value)
    source_values.setdefault('resultado_matriz', base_value)
    source_values.setdefault('criticidad_base', base_classification)

    for rule in sorted((rule for rule in rules if rule.get('activa')), key=lambda item: item.get('prioridad', 0)):
        matches = [_condition_matches(condition, source_values) for condition in rule['condiciones']]
        matched = any(matches) if rule['combinador'] == 'OR' else all(matches)
        if not matched:
            continue
        action_type = rule['accion']['tipo']
        action_value = rule['accion'].get('valor')
        previous_classification = final_classification
        previous_value = final_value
        action_color = getattr(final_cell, 'color', '') or ''

        if action_type == 'forzar_valor':
            forced = _decimal(action_value)
            if forced is not None:
                final_value = forced
                final_cell = matrix_cell_for_value(matrix, forced, cells)
                final_classification = getattr(final_cell, 'clasificacion', final_classification)
                action_color = getattr(final_cell, 'color', '') or action_color
        elif action_type in {'forzar_criticidad', 'criticidad_minima'}:
            target = _target_level(action_value, levels)
            if target:
                current_index = level_index.get(_key(final_classification), -1)
                target_index = level_index[target['key']]
                if action_type == 'forzar_criticidad' or current_index < target_index:
                    final_cell = target['cell']
                    final_classification = target['name']
                    final_value = final_cell.resultado_num if final_cell else final_value
                    action_color = target.get('color') or getattr(final_cell, 'color', '') or action_color
            elif action_type == 'criticidad_minima':
                minimum = _decimal(action_value)
                if minimum is not None and (final_value is None or final_value < minimum):
                    final_value = minimum
                    final_cell = matrix_cell_for_value(matrix, minimum, cells)
                    final_classification = getattr(final_cell, 'clasificacion', final_classification)
                    action_color = getattr(final_cell, 'color', '') or action_color
        elif action_type == 'subir_niveles' and levels:
            try:
                amount = max(0, int(action_value or 0))
            except (TypeError, ValueError):
                amount = 0
            current_index = max(0, level_index.get(_key(final_classification), 0))
            target = levels[min(len(levels) - 1, current_index + amount)]
            final_cell = target['cell']
            final_classification = target['name']
            final_value = final_cell.resultado_num if final_cell else final_value
            action_color = target.get('color') or getattr(final_cell, 'color', '') or action_color

        applied.append({
            'id': rule['id'],
            'nombre': rule['nombre'],
            'prioridad': rule['prioridad'],
            'accion': action_type,
            'valor_accion': action_value,
            'antes': {'criticidad': previous_classification, 'valor': str(previous_value) if previous_value is not None else None},
            'despues': {
                'criticidad': final_classification,
                'valor': str(final_value) if final_value is not None else None,
                'color': action_color,
            },
        })

    trace = {
        'criticidad_base': base_classification,
        'valor_base': str(base_value) if base_value is not None else None,
        'reglas_aplicadas': applied,
        'regla_aplicada': applied[-1]['nombre'] if applied else '',
        'criticidad_final': final_classification,
        'valor_final': str(final_value) if final_value is not None else None,
        'color_final': applied[-1]['despues'].get('color', '') if applied else (getattr(final_cell, 'color', '') or ''),
    }
    return {
        'cell': final_cell,
        'value': final_value,
        'classification': final_classification,
        'trace': trace,
    }


def sync_fmeca_criticality_rules(rcm, fmea, service):
    matrix = (
        models.MatrizRiesgo.objects
        .filter(estrategia_id=service.estrategia_id)
        .select_related('dimension_probabilidad', 'dimension_impacto')
        .order_by('-fecha_creado', '-id')
        .first()
    )
    if not matrix:
        rcm.trazabilidad_criticidad_json = ''
        rcm.save(update_fields=['trazabilidad_criticidad_json'])
        return rcm

    evaluations = list(
        models.EvaluacionFMEA.objects
        .filter(fmea=fmea)
        .select_related('estrategia_dimension', 'estrategia_dimension__dimension')
    )
    probability_value = None
    consequence_value = None
    for evaluation in evaluations:
        if evaluation.valor_numerico is None:
            continue
        if evaluation.estrategia_dimension_id == matrix.dimension_probabilidad_id:
            probability_value = evaluation.valor_numerico
        if evaluation.estrategia_dimension_id == matrix.dimension_impacto_id:
            consequence_value = evaluation.valor_numerico

    base_cell = matrix_cell_for_axis_values(matrix, probability_value, consequence_value)
    base_value = getattr(base_cell, 'resultado_num', None)
    if base_value is None:
        base_value = rcm.criticidad
        try:
            previous_trace = json.loads(rcm.trazabilidad_criticidad_json or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            previous_trace = {}
        if (
            isinstance(previous_trace, dict)
            and previous_trace.get('reglas_aplicadas')
            and _decimal(previous_trace.get('valor_final')) == _decimal(base_value)
            and previous_trace.get('valor_base') is not None
        ):
            base_value = _decimal(previous_trace.get('valor_base'))
        base_cell = matrix_cell_for_value(matrix, base_value)
    base_classification = getattr(base_cell, 'clasificacion', '') or ''
    source_values = dimension_source_values(evaluations)
    source_values.update({
        'probabilidad': probability_value,
        'frecuencia': probability_value,
        'consecuencia': consequence_value,
        'consecuencia_total': consequence_value,
        'resultado_matriz': base_value,
    })
    if getattr(matrix, 'dimension_probabilidad_id', None):
        source_values[f'ed:{matrix.dimension_probabilidad_id}'] = probability_value
        source_values[f'estrategia_dimension:{matrix.dimension_probabilidad_id}'] = probability_value
    if getattr(matrix, 'dimension_impacto_id', None):
        source_values[f'ed:{matrix.dimension_impacto_id}'] = consequence_value
        source_values[f'estrategia_dimension:{matrix.dimension_impacto_id}'] = consequence_value
    ruled = apply_criticality_rules(
        matrix,
        base_cell=base_cell,
        base_value=base_value,
        base_classification=base_classification,
        source_values=source_values,
    )
    if ruled['value'] is not None:
        rcm.criticidad = int(Decimal(ruled['value']).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    rcm.trazabilidad_criticidad_json = json.dumps(ruled['trace'], ensure_ascii=False)
    rcm.save(update_fields=['criticidad', 'trazabilidad_criticidad_json'])
    return rcm
