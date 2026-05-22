import json
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from core import models
from core.access import get_accessible_services, get_profile_for_user, get_service_permission
from aca.forms import CriticidadDimensionFormSet, ServicioACARegistroForm
from core.views import (
    _calc_slug,
    _catalog_row_boolean,
    _catalog_row_primary_numeric,
    _catalog_row_text,
    _decimal_or_none,
    _dimension_display_value,
    _export_filename,
    _export_pdf_response,
    _export_xlsx_response,
    _format_matrix_decimal,
    _is_generated_matrix_axis_dimension,
    _json_loads_safe,
    _matrix_cell_for_axis_values,
    _matrix_resolution_mode,
    _matrix_result_from_axis_values,
    _matrix_threshold_axis_values,
    _service_equipment_browser_payload,
    _service_equipment_endpoints,
    _service_or_404,
    _strategy_dimensions,
)


# ---------------------------------------------------------------------------
# Helpers del flujo ACA
# ---------------------------------------------------------------------------
def _next_service_aca_version(servicio):
    latest = models.Carga.objects.filter(servicio=servicio).order_by('-fecha_analisis', '-id').first()
    if not latest or latest.version_carga is None:
        return Decimal('1.0')
    try:
        return Decimal(str(latest.version_carga)) + Decimal('0.1')
    except (InvalidOperation, ValueError, TypeError):
        return Decimal('1.0')

def _service_matrix_selector(servicio):
    matriz = None
    if getattr(servicio, 'estrategia_id', None):
        matriz = models.MatrizRiesgo.objects.filter(
            estrategia=servicio.estrategia
        ).select_related(
            'dimension_probabilidad',
            'dimension_probabilidad__dimension',
            'dimension_impacto',
            'dimension_impacto__dimension',
        ).order_by('-fecha_creado', '-id').first()

    if not matriz:
        return {
            'matriz': None,
            'rows': [],
            'columns': [],
            'axis': 'impacto',
            'prob_dimension_id': '',
            'impact_dimension_id': '',
            'prob_estrategia_dimension_id': '',
            'impact_estrategia_dimension_id': '',
            'modo_resolucion': models.MatrizRiesgo.RESOLUCION_EXACTA,
            'prob_axis_generated': False,
            'impact_axis_generated': False,
            'prob_axis_label': 'Probabilidad',
            'impact_axis_label': 'Impacto',
        }

    prob_levels = list(
        models.NivelProbabilidad.objects.filter(matriz=matriz).order_by('orden_visual', 'id')
    )
    impact_levels = list(
        models.NivelImpacto.objects.filter(matriz=matriz).order_by('orden_visual', 'id')
    )

    cells = {
        (cell.probabilidad_id, cell.impacto_nivel_id): cell
        for cell in models.MatrizRiesgoCelda.objects.filter(matriz=matriz)
        .select_related('probabilidad', 'impacto_nivel')
        .order_by('probabilidad__orden_visual', 'impacto_nivel__orden_visual')
    }

    rows = []
    for prob in prob_levels:
        row = {
            'id': prob.id,
            'label': prob.nombre,
            'value': prob.valor,
            'cells': [],
        }
        for impact in impact_levels:
            cell = cells.get((prob.id, impact.id))
            row['cells'].append({
                'id': cell.id if cell else '',
                'prob_id': prob.id,
                'prob_label': prob.nombre,
                'prob_value': str(prob.valor),
                'impact_id': impact.id,
                'impact_label': impact.nombre,
                'impact_value': str(impact.valor),
                'result': str(cell.resultado_num) if cell and cell.resultado_num is not None else '',
                'classification': cell.clasificacion if cell else '',
                'color': cell.color if cell and cell.color else '#ffffff',
            })
        rows.append(row)

    columns = [
        {'id': impact.id, 'label': impact.nombre, 'value': impact.valor}
        for impact in impact_levels
    ]

    return {
        'matriz': matriz,
        'rows': rows,
        'columns': columns,
        'axis': matriz.eje_horizontal,
        'prob_dimension_id': matriz.dimension_probabilidad.dimension_id if matriz.dimension_probabilidad_id else '',
        'impact_dimension_id': matriz.dimension_impacto.dimension_id if matriz.dimension_impacto_id else '',
        'prob_estrategia_dimension_id': matriz.dimension_probabilidad_id or '',
        'impact_estrategia_dimension_id': matriz.dimension_impacto_id or '',
        'modo_resolucion': _matrix_resolution_mode(matriz),
        'prob_axis_generated': _is_generated_matrix_axis_dimension(matriz.dimension_probabilidad, 'probabilidad') if matriz.dimension_probabilidad_id else False,
        'impact_axis_generated': _is_generated_matrix_axis_dimension(matriz.dimension_impacto, 'impacto') if matriz.dimension_impacto_id else False,
        'prob_axis_label': matriz.dimension_probabilidad.dimension.nombre if matriz.dimension_probabilidad_id else 'Probabilidad',
        'impact_axis_label': matriz.dimension_impacto.dimension.nombre if matriz.dimension_impacto_id else 'Impacto',
    }

def _save_matrix_dimensions(evaluacion, estrategia, selected_cell):
    matriz = models.MatrizRiesgo.objects.filter(
        estrategia=estrategia
    ).select_related(
        'dimension_probabilidad',
        'dimension_probabilidad__dimension',
        'dimension_impacto',
        'dimension_impacto__dimension',
    ).order_by('-fecha_creado', '-id').first()
    if not matriz:
        return

    prob_dim = matriz.dimension_probabilidad
    impact_dim = matriz.dimension_impacto
    existing_dimension_ids = set(
        evaluacion.dimensiones.values_list('dimension_id', flat=True)
    )

    if prob_dim and prob_dim.dimension_id not in existing_dimension_ids:
        models.CriticidadDimension.objects.create(
            criticidad=evaluacion,
            dimension=prob_dim.dimension,
            estrategia_dimension=prob_dim,
            valor_numerico=selected_cell.probabilidad.valor,
            valor_texto=selected_cell.probabilidad.nombre or selected_cell.probabilidad.descripcion or '',
        )

    if impact_dim and impact_dim.dimension_id not in existing_dimension_ids:
        models.CriticidadDimension.objects.create(
            criticidad=evaluacion,
            dimension=impact_dim.dimension,
            estrategia_dimension=impact_dim,
            valor_numerico=selected_cell.impacto_nivel.valor,
            valor_texto=selected_cell.impacto_nivel.nombre or selected_cell.impacto_nivel.descripcion or '',
        )

def _aca_excluded_dimension_ids(estrategia, matriz=None):
    excluded_ids = set()
    matrix_input_dimension_ids = set()

    if matriz:
        if (
            getattr(matriz, 'dimension_probabilidad_id', None)
            and _is_generated_matrix_axis_dimension(matriz.dimension_probabilidad, 'probabilidad')
        ):
            excluded_ids.add(matriz.dimension_probabilidad.dimension_id)
        elif getattr(matriz, 'dimension_probabilidad_id', None):
            matrix_input_dimension_ids.add(matriz.dimension_probabilidad.dimension_id)
        if (
            getattr(matriz, 'dimension_impacto_id', None)
            and _is_generated_matrix_axis_dimension(matriz.dimension_impacto, 'impacto')
        ):
            excluded_ids.add(matriz.dimension_impacto.dimension_id)
        elif getattr(matriz, 'dimension_impacto_id', None):
            matrix_input_dimension_ids.add(matriz.dimension_impacto.dimension_id)

    derived_keywords = [
        'frecuencia normalizada',
        'valor consecuencia total',
        'probabilidad -',
        'impacto -',
    ]

    dims = models.EstrategiaDimension.objects.filter(
        estrategia=estrategia,
        activo=True,
        proceso_uso__in=[
            models.EstrategiaDimension.PROCESO_ACA,
            models.EstrategiaDimension.PROCESO_AMBOS,
        ],
    ).select_related('dimension').prefetch_related('catalogo')

    for item in dims:
        nombre = (item.dimension.nombre or '').strip().lower()
        if _is_generated_matrix_axis_dimension(item):
            excluded_ids.add(item.dimension_id)
            continue
        if item.dimension_id in matrix_input_dimension_ids:
            continue
        try:
            catalogo = item.catalogo
        except models.DimensionCatalogo.DoesNotExist:
            catalogo = None
        is_user_configured = bool(
            (getattr(item.dimension, 'tipo_calculo', '') or '').strip()
            or (catalogo and catalogo.activa)
        )
        if is_user_configured:
            continue
        if any(keyword in nombre for keyword in derived_keywords):
            excluded_ids.add(item.dimension_id)

    return excluded_ids

def _dependency_ref_from_config(config):
    if isinstance(config, str):
        config = _json_loads_safe(config, {})
    if not isinstance(config, dict):
        return ''
    for key in ['dependencia', 'depende_de', 'source', 'fuente', 'campo_fuente', 'dimension_fuente']:
        value = config.get(key)
        if value not in (None, ''):
            return value if isinstance(value, dict) else str(value).strip()
    if config.get('estrategia_dimension_id') or config.get('dimension_id'):
        return config
    return ''

def _source_ref_candidates(source_ref):
    if source_ref in (None, ''):
        return []
    if isinstance(source_ref, dict):
        candidates = []
        estrategia_dimension_id = (
            source_ref.get('estrategia_dimension_id')
            or source_ref.get('estrategiaDimensionId')
            or source_ref.get('ed_id')
        )
        if estrategia_dimension_id not in (None, ''):
            candidates.append(f'ed:{estrategia_dimension_id}')
            candidates.append(f'estrategia_dimension:{estrategia_dimension_id}')
        dimension_id = source_ref.get('dimension_id') or source_ref.get('dimensionId')
        if dimension_id not in (None, ''):
            candidates.append(str(dimension_id))
            candidates.append(f'dim:{dimension_id}')
        for key in ['campo', 'nombre', 'source', 'fuente', 'dependencia', 'depende_de', 'campo_fuente', 'dimension_fuente']:
            value = source_ref.get(key)
            if value not in (None, '') and not isinstance(value, dict):
                candidates.append(str(value).strip())
        return [candidate for candidate in candidates if candidate]
    return [str(source_ref).strip()]

def _dimension_dependency_key(dimension):
    return next(iter(_source_ref_candidates(_dependency_ref_from_config(getattr(dimension, 'config_calculo', None)))), '')

def _remember_source_value(source_values, estrategia_dimension, value):
    decimal_value = _decimal_or_none(value)
    if decimal_value is None:
        return
    dimension = estrategia_dimension.dimension
    campo = _dimension_source_key(estrategia_dimension)
    source_values[f'ed:{estrategia_dimension.id}'] = decimal_value
    source_values[f'estrategia_dimension:{estrategia_dimension.id}'] = decimal_value
    source_values[str(dimension.id)] = decimal_value
    source_values[f'dim:{dimension.id}'] = decimal_value
    source_values[dimension.nombre] = decimal_value
    source_values[_calc_slug(dimension.nombre)] = decimal_value
    if campo:
        source_values[campo] = decimal_value
        source_values[_calc_slug(campo)] = decimal_value

def _source_value(source_values, source_key):
    for candidate in _source_ref_candidates(source_key):
        value = source_values.get(candidate)
        if value is None:
            value = source_values.get(_calc_slug(candidate))
        decimal_value = _decimal_or_none(value)
        if decimal_value is not None:
            return decimal_value
    return None

def _catalog_bound(values, keys):
    for key in keys:
        value = values.get(key) if isinstance(values, dict) else None
        if value not in (None, ''):
            return _decimal_or_none(value)
    return None

def _match_catalog_range_row(catalogo, source_value):
    source_value = _decimal_or_none(source_value)
    if not catalogo or source_value is None:
        return None

    rows = list(catalogo.filas.prefetch_related('celdas__columna').order_by('orden', 'id'))
    row_bounds = []
    lowers = []
    for row in rows:
        values = row.values_map()
        lower = _catalog_bound(values, ['limite_inferior', 'desde', 'min', 'minimo', 'mí­nimo'])
        upper = _catalog_bound(values, ['limite_superior', 'hasta', 'max', 'maximo', 'máximo'])
        row_bounds.append((row, lower, upper))
        if lower is not None:
            lowers.append(lower)

    for row, lower, upper in row_bounds:
        if lower is not None and source_value < lower:
            continue
        if upper is not None and source_value > upper:
            continue
        if upper is not None and source_value == upper and upper in lowers:
            continue
        return row
    return None

def _catalog_primary_numeric_from_values(values):
    for key in ['valor_numerico', 'valor_principal', 'valor', 'nivel', 'puntaje']:
        value = values.get(key) if isinstance(values, dict) else None
        if value not in (None, ''):
            return _decimal_or_none(value)
    if isinstance(values, dict):
        for key, value in values.items():
            if key in {
                'limite_inferior', 'limite_superior', 'desde', 'hasta',
                'min', 'max', 'minimo', 'mí­nimo', 'maximo', 'máximo',
                'valor_secundario',
            }:
                continue
            decimal_value = _decimal_or_none(value)
            if decimal_value is not None:
                return decimal_value
    return None

def _catalog_match_value(values):
    for key in [
        'valor_dependencia',
        'dependencia',
        'depende_de',
        'valor_origen',
        'valor_fuente',
        'source_value',
        'valor_de_entrada',
        'valor_entrada',
        'entrada',
    ]:
        value = values.get(key) if isinstance(values, dict) else None
        if value not in (None, ''):
            decimal_value = _decimal_or_none(value)
            if decimal_value is not None:
                return decimal_value
    return _catalog_primary_numeric_from_values(values)

def _match_catalog_option_row(catalogo, source_value):
    source_value = _decimal_or_none(source_value)
    if not catalogo or source_value is None:
        return None

    rows = catalogo.filas.prefetch_related('celdas__columna').order_by('orden', 'id')
    for row in rows:
        match_value = _catalog_match_value(row.values_map())
        if match_value is not None and match_value == source_value:
            return row
    return None

def _match_catalog_dependency_row(catalogo, source_value):
    if not catalogo:
        return None
    if catalogo.tipo == 'rangos':
        return _match_catalog_range_row(catalogo, source_value)
    if catalogo.tipo == 'opciones':
        return _match_catalog_option_row(catalogo, source_value)
    return None

def _dimension_formset(request, estrategia, initial=None, bind_post=True, exclude_dimension_ids=None):
    initial = initial or []
    if not estrategia:
        return CriticidadDimensionFormSet(
            prefix='dims',
            form_kwargs={'estrategia': None, 'proceso': models.EstrategiaDimension.PROCESO_ACA},
        )

    if not initial:
        dims_qs = (
            models.EstrategiaDimension.objects.filter(
                estrategia=estrategia,
                activo=True,
                proceso_uso__in=[
                    models.EstrategiaDimension.PROCESO_ACA,
                    models.EstrategiaDimension.PROCESO_AMBOS,
                ],
            )
            .select_related('dimension')
            .order_by('orden', 'id')
        )
        if exclude_dimension_ids:
            dims_qs = dims_qs.exclude(dimension_id__in=exclude_dimension_ids)

        dims = list(dims_qs)
        dims.sort(key=lambda dim: (
            2 if _dimension_dependency_key(dim.dimension)
            else 1 if (getattr(dim.dimension, 'tipo_calculo', '') or '').strip()
            else 0,
            dim.orden,
            dim.id,
        ))
        initial = [{'dimension': dim.dimension} for dim in dims]

    if request.method == 'POST' and bind_post:
        return CriticidadDimensionFormSet(
            request.POST,
            prefix='dims',
            form_kwargs={'estrategia': estrategia, 'proceso': models.EstrategiaDimension.PROCESO_ACA},
        )
    return CriticidadDimensionFormSet(
        initial=initial,
        prefix='dims',
        form_kwargs={'estrategia': estrategia, 'proceso': models.EstrategiaDimension.PROCESO_ACA},
    )

def _dimension_formset_initial_from_criticidad(criticidad, estrategia, exclude_dimension_ids=None):
    if not criticidad or not estrategia:
        return []

    existing = {
        item.dimension_id: item
        for item in criticidad.dimensiones.select_related(
            'dimension',
            'catalogo_fila',
            'escala_valor',
        ).all()
    }
    dims_qs = (
        models.EstrategiaDimension.objects.filter(
            estrategia=estrategia,
            activo=True,
            proceso_uso__in=[
                models.EstrategiaDimension.PROCESO_ACA,
                models.EstrategiaDimension.PROCESO_AMBOS,
            ],
        )
        .select_related('dimension')
        .order_by('orden', 'id')
    )
    if exclude_dimension_ids:
        dims_qs = dims_qs.exclude(dimension_id__in=exclude_dimension_ids)

    dims = list(dims_qs)
    dims.sort(key=lambda dim: (
        2 if _dimension_dependency_key(dim.dimension)
        else 1 if (getattr(dim.dimension, 'tipo_calculo', '') or '').strip()
        else 0,
        dim.orden,
        dim.id,
    ))

    initial = []
    for estrategia_dimension in dims:
        item = existing.get(estrategia_dimension.dimension_id)
        payload = {'dimension': estrategia_dimension.dimension}
        if item:
            payload.update({
                'escala_valor': item.escala_valor,
                'catalogo_fila': item.catalogo_fila,
                'valor_numerico': item.valor_numerico,
                'valor_booleano': item.valor_booleano,
                'valor_texto': item.valor_texto,
            })
        initial.append(payload)
    return initial

def _dimension_source_key(estrategia_dimension):
    if not estrategia_dimension:
        return ''
    try:
        catalogo = getattr(estrategia_dimension, 'catalogo', None)
        if catalogo and catalogo.campo:
            return catalogo.campo
    except Exception:
        pass
    return _calc_slug(estrategia_dimension.dimension.nombre)

def _calculation_operation_result(tipo, values):
    if not values:
        return None

    if tipo == 'suma':
        return sum(values, Decimal('0'))
    if tipo == 'resta':
        result = values[0]
        for value in values[1:]:
            result -= value
        return result
    if tipo == 'multiplicacion':
        result = Decimal('1')
        for value in values:
            result *= value
        return result
    if tipo == 'division':
        result = values[0]
        for value in values[1:]:
            if value == 0:
                return None
            result /= value
        return result
    if tipo == 'maximo':
        return max(values)
    if tipo == 'minimo':
        return min(values)
    return None

def _calculation_operand_value(operand, source_values, previous_result=None):
    if isinstance(operand, dict):
        if operand.get('resultado') is True or operand.get('tipo') == 'resultado':
            return previous_result
    else:
        raw = str(operand)
        if raw in {'$resultado', '__resultado__', 'resultado_anterior'}:
            return previous_result

    for candidate in _source_ref_candidates(operand):
        value = source_values.get(candidate)
        if value is None:
            value = source_values.get(_calc_slug(candidate))
        decimal_value = _decimal_or_none(value)
        if decimal_value is not None:
            return decimal_value

    return None

def _calculation_steps(tipo_calculo, config_calculo):
    tipo = (tipo_calculo or '').strip().lower()
    if not tipo:
        return []

    if isinstance(config_calculo, str):
        try:
            config_calculo = json.loads(config_calculo or '{}')
        except Exception:
            config_calculo = {}
    if not isinstance(config_calculo, dict):
        config_calculo = {}

    raw_steps = config_calculo.get('pasos') or config_calculo.get('steps') or []
    if isinstance(raw_steps, list) and raw_steps:
        steps = []
        for raw_step in raw_steps:
            if not isinstance(raw_step, dict):
                continue
            operacion = str(raw_step.get('operacion') or raw_step.get('tipo_calculo') or raw_step.get('operation') or '').strip().lower()
            operandos = raw_step.get('operandos') or raw_step.get('campos') or raw_step.get('sources') or []
            if operacion and isinstance(operandos, list):
                steps.append({'operacion': operacion, 'operandos': operandos})
        return steps

    operandos = config_calculo.get('operandos') or config_calculo.get('campos') or config_calculo.get('sources') or []
    if not isinstance(operandos, list):
        operandos = []
    return [{'operacion': tipo, 'operandos': operandos}]

def _evaluate_dimension_calculation(tipo_calculo, config_calculo, source_values):
    steps = _calculation_steps(tipo_calculo, config_calculo)
    if not steps:
        return None

    result = None
    for step in steps:
        values = []
        for operand in step['operandos']:
            value = _calculation_operand_value(operand, source_values, result)
            if value is not None:
                values.append(value)

        result = _calculation_operation_result(step['operacion'], values)
        if result is None:
            return None

    return result

def _extract_dimension_form_value(data, catalogo_fila=None, escala_valor=None):
    valor_numerico = data.get('valor_numerico')
    valor_secundario = None
    valor_booleano = data.get('valor_booleano')
    valor_texto = data.get('valor_texto', '')
    escala_unificada = None

    if escala_valor:
        escala_unificada = escala_valor.escala_unificada
        if valor_numerico in (None, ''):
            valor_numerico = escala_valor.valor_numerico
        if not valor_texto:
            valor_texto = escala_valor.codigo or escala_valor.descripcion or ''

    if catalogo_fila:
        if valor_numerico in (None, ''):
            valor_numerico = _catalog_row_primary_numeric(catalogo_fila)
        if valor_booleano is None:
            valor_booleano = _catalog_row_boolean(catalogo_fila)
        if not valor_texto:
            valor_texto = _catalog_row_text(catalogo_fila)

    return valor_numerico, valor_secundario, valor_booleano, valor_texto, escala_unificada

def _prepare_dimension_items(estrategia, formset):
    prepared = []
    source_values = {}

    for form in formset:
        data = getattr(form, 'cleaned_data', None)
        if not data:
            continue
        dimension = data.get('dimension')
        if not dimension:
            continue

        estrategia_dimension = models.EstrategiaDimension.objects.filter(
            estrategia=estrategia,
            activo=True,
            proceso_uso__in=[
                models.EstrategiaDimension.PROCESO_ACA,
                models.EstrategiaDimension.PROCESO_AMBOS,
            ],
            dimension=dimension,
        ).select_related('dimension').first()
        if not estrategia_dimension:
            continue

        catalogo_fila = data.get('catalogo_fila')
        escala_valor = data.get('escala_valor')
        valor_numerico, valor_secundario, valor_booleano, valor_texto, escala_unificada = _extract_dimension_form_value(
            data,
            catalogo_fila=catalogo_fila,
            escala_valor=escala_valor,
        )

        item = {
            'dimension': dimension,
            'estrategia_dimension': estrategia_dimension,
            'catalogo_fila': catalogo_fila,
            'escala_valor': escala_valor,
            'escala_unificada': escala_unificada,
            'valor_numerico': valor_numerico,
            'valor_secundario': valor_secundario,
            'valor_booleano': valor_booleano,
            'valor_texto': valor_texto or '',
            'is_calculated': bool((getattr(dimension, 'tipo_calculo', '') or '').strip()),
            'dependency_key': _dependency_ref_from_config(getattr(dimension, 'config_calculo', None)),
        }
        prepared.append(item)

        if not item['is_calculated'] and not item['dependency_key']:
            numeric_value = valor_numerico
            if numeric_value is not None:
                _remember_source_value(source_values, estrategia_dimension, numeric_value)

    for item in prepared:
        if item['is_calculated']:
            value = _evaluate_dimension_calculation(
                item['dimension'].tipo_calculo,
                item['dimension'].config_calculo,
                source_values,
            )
            item['valor_numerico'] = value
            item['valor_texto'] = '' if value is None else str(value)
            if value is not None:
                _remember_source_value(source_values, item['estrategia_dimension'], value)

    for item in prepared:
        if not item['dependency_key']:
            continue

        source_value = _source_value(source_values, item['dependency_key'])
        try:
            catalogo = item['estrategia_dimension'].catalogo
        except models.DimensionCatalogo.DoesNotExist:
            catalogo = None

        matched_row = _match_catalog_dependency_row(catalogo, source_value)
        if not matched_row:
            continue

        item['catalogo_fila'] = matched_row
        valor_numerico, valor_secundario, valor_booleano, valor_texto, escala_unificada = _extract_dimension_form_value(
            {},
            catalogo_fila=matched_row,
            escala_valor=None,
        )
        item['valor_numerico'] = valor_numerico
        item['valor_secundario'] = valor_secundario
        item['valor_booleano'] = valor_booleano
        item['valor_texto'] = valor_texto or ''
        item['escala_unificada'] = escala_unificada

        numeric_value = valor_numerico
        if numeric_value is not None:
            _remember_source_value(source_values, item['estrategia_dimension'], numeric_value)

    return prepared, source_values

def _create_dimension_items(evaluacion, prepared):
    created = []
    for item in prepared:
        if not any([
            item['escala_valor'],
            item['catalogo_fila'],
            item['valor_numerico'] is not None,
            item['valor_secundario'] is not None,
            item['valor_booleano'] is not None,
            item['valor_texto'],
        ]):
            continue

        created.append(models.CriticidadDimension.objects.create(
            criticidad=evaluacion,
            dimension=item['dimension'],
            estrategia_dimension=item['estrategia_dimension'],
            escala_valor=item['escala_valor'],
            escala_unificada=item['escala_unificada'],
            catalogo_fila=item['catalogo_fila'],
            valor_numerico=item['valor_numerico'],
            valor_secundario=item['valor_secundario'],
            valor_booleano=item['valor_booleano'],
            valor_texto=item['valor_texto'] or '',
        ))
    return created

def _save_dimension_formset(evaluacion, estrategia, formset):
    evaluacion.dimensiones.all().delete()
    prepared, _source_values = _prepare_dimension_items(estrategia, formset)
    return _create_dimension_items(evaluacion, prepared)

def _dimension_record_numeric_value(record):
    if isinstance(record, dict):
        value = record.get('valor_numerico')
    else:
        value = record.valor_numerico
    return _decimal_or_none(value)

def _dimension_record_ids(record):
    if isinstance(record, dict):
        estrategia_dimension = record.get('estrategia_dimension')
        dimension = record.get('dimension')
        return (
            getattr(estrategia_dimension, 'id', None),
            getattr(dimension, 'id', None),
        )
    return (
        getattr(record, 'estrategia_dimension_id', None),
        getattr(record, 'dimension_id', None),
    )

def _matrix_axis_dimension_refs(matriz):
    prob_dim_id = None
    impact_dim_id = None
    if getattr(matriz, 'dimension_probabilidad_id', None):
        prob_dim_id = matriz.dimension_probabilidad.dimension_id
    if getattr(matriz, 'dimension_impacto_id', None):
        impact_dim_id = matriz.dimension_impacto.dimension_id
    return {
        'prob_ed_id': getattr(matriz, 'dimension_probabilidad_id', None),
        'prob_dim_id': prob_dim_id,
        'impact_ed_id': getattr(matriz, 'dimension_impacto_id', None),
        'impact_dim_id': impact_dim_id,
    }

def _matrix_axis_values_from_records(matriz, records):
    refs = _matrix_axis_dimension_refs(matriz)
    prob_val = None
    impact_val = None

    for record in records:
        value = _dimension_record_numeric_value(record)
        if value is None:
            continue
        estrategia_dimension_id, dimension_id = _dimension_record_ids(record)

        if (
            refs['prob_ed_id'] and estrategia_dimension_id == refs['prob_ed_id']
        ) or (
            refs['prob_dim_id'] and dimension_id == refs['prob_dim_id']
        ):
            prob_val = value

        if (
            refs['impact_ed_id'] and estrategia_dimension_id == refs['impact_ed_id']
        ) or (
            refs['impact_dim_id'] and dimension_id == refs['impact_dim_id']
        ):
            impact_val = value

    return prob_val, impact_val

def _resolve_matrix_cell_from_dimension_records(matriz, records):
    prob_val, impact_val = _matrix_axis_values_from_records(matriz, records)
    return _matrix_cell_for_axis_values(matriz, prob_val, impact_val)

def _sync_criticidad_resumen(evaluacion, estrategia):
    dims = list(
        evaluacion.dimensiones.select_related('dimension', 'estrategia_dimension').all()
    )

    matriz = models.MatrizRiesgo.objects.filter(
        estrategia=estrategia
    ).select_related(
        'dimension_probabilidad',
        'dimension_probabilidad__dimension',
        'dimension_impacto',
        'dimension_impacto__dimension',
    ).order_by('-fecha_creado', '-id').first()

    prob_val = None
    impact_val = None
    valor_cons_total = None
    valor_criticidad_equipo = None
    indicador = evaluacion.indicador_criticidad or ''
    criticidad_final = evaluacion.criticidad_final or ''

    # 1) Priorizar las dimensiones especí­ficas que usa la matriz
    if matriz:
        prob_val, impact_val = _matrix_axis_values_from_records(matriz, dims)

    # 2) Fallback: usar la frecuencia normalizada y/o el valor de consecuencia ya guardado
    if prob_val is None and evaluacion.frecuencia_normalizada is not None:
        prob_val = Decimal(evaluacion.frecuencia_normalizada)

    if impact_val is None and evaluacion.valor_cons_total is not None:
        impact_val = Decimal(evaluacion.valor_cons_total)

    # 3) Si existe matriz, intentar resolver la celda configurada.
    #    Por defecto es exacta; algunas matrices pueden usar umbral inferior por resultado.
    if matriz and (prob_val is not None or impact_val is not None):
        celda = _matrix_cell_for_axis_values(matriz, prob_val, impact_val)

        if celda:
            if _matrix_resolution_mode(matriz) == models.MatrizRiesgo.RESOLUCION_UMBRAL_RESULTADO:
                effective_prob_val, effective_impact_val = _matrix_threshold_axis_values(matriz, prob_val, impact_val)
                result_value = _matrix_result_from_axis_values(effective_prob_val, effective_impact_val)
                valor_cons_total = effective_impact_val if effective_impact_val is not None else result_value
                if prob_val is None:
                    prob_val = celda.probabilidad.valor
                valor_criticidad_equipo = result_value if result_value is not None else celda.resultado_num
                indicador = f'[ {_format_matrix_decimal(valor_criticidad_equipo)} -> {_format_matrix_decimal(celda.resultado_num)} ]'
            else:
                valor_cons_total = celda.impacto_nivel.valor
                prob_val = celda.probabilidad.valor
                valor_criticidad_equipo = celda.resultado_num
                indicador = f'[ {_format_matrix_decimal(valor_cons_total)} - {_format_matrix_decimal(prob_val)} ]'
            criticidad_final = celda.clasificacion or criticidad_final

    # 4) Fallback final: si no encontró celda, calcular con los niveles ya resueltos
    if valor_cons_total is None:
        valor_cons_total = impact_val

    if valor_criticidad_equipo is None and valor_cons_total is not None and prob_val is not None:
        try:
            valor_criticidad_equipo = Decimal(valor_cons_total) * Decimal(prob_val)
            indicador = f'[ {_format_matrix_decimal(valor_cons_total)} - {_format_matrix_decimal(prob_val)} ]'
        except Exception:
            valor_criticidad_equipo = evaluacion.valor_criticidad_equipo

    evaluacion.frecuencia_normalizada = prob_val
    evaluacion.valor_cons_total = valor_cons_total
    evaluacion.valor_criticidad_equipo = valor_criticidad_equipo
    evaluacion.indicador_criticidad = indicador
    evaluacion.criticidad_final = criticidad_final
    evaluacion.save(
        update_fields=[
            'frecuencia_normalizada',
            'valor_cons_total',
            'valor_criticidad_equipo',
            'indicador_criticidad',
            'criticidad_final',
        ]
    )



@login_required
def service_aca_list(request, pk):
    servicio, permission = _service_or_404(request, pk, edit=False)
    columns, rows, complete_count, incomplete_count = _service_aca_table_data(servicio, include_actions=True)

    other_aca_services = []
    if not rows:
        accessible_service_ids = [item.pk for item in get_accessible_services(request.user)]
        other_aca_services = list(
            models.Criticidad.objects.filter(
                aca_carga__servicio_id__in=accessible_service_ids,
            )
            .exclude(aca_carga__servicio=servicio)
            .values(
                'aca_carga__servicio_id',
                'aca_carga__servicio__codigo_servicio',
            )
            .annotate(total=Count('id'))
            .order_by('aca_carga__servicio__codigo_servicio')
        )

    return render(request, 'core/aca/service_aca_list.html', {
        'service': servicio,
        'permission': permission,
        'columns': columns,
        'rows': rows,
        'aca_count': len(rows),
        'complete_count': complete_count,
        'incomplete_count': incomplete_count,
        'other_aca_services': other_aca_services,
    })


def _service_aca_table_data(servicio, include_actions=False):
    estrategia_dims = _strategy_dimensions(
        servicio.estrategia,
        proceso=models.EstrategiaDimension.PROCESO_ACA,
    )

    estrategia_dims = [
        ed for ed in estrategia_dims
        if not _is_generated_matrix_axis_dimension(ed)
    ]

    criticidades = list(
        models.Criticidad.objects.filter(aca_carga__servicio=servicio)
        .select_related('equipo', 'equipo__nodo', 'aca_carga')
        .prefetch_related(
            Prefetch(
                'dimensiones',
                queryset=models.CriticidadDimension.objects.select_related(
                    'dimension', 'catalogo_fila__catalogo', 'escala_valor', 'escala_unificada'
                ).prefetch_related('catalogo_fila__celdas__columna')
            )
        )
        .order_by('-aca_carga__fecha_analisis', 'equipo__tag_equipo', 'id')
    )

    columns = [
        ('cliente', 'Cliente'),
        ('status', 'Estado'),
        ('fecha_analisis', 'Fecha análisis'),
        ('ubicacion_tecnica', 'Ubicación Técnica'),
        ('descripcion_ut', 'Descripción U.Técnica'),
        ('equipo', 'Equipo'),
        ('tag', 'TAG'),
        ('escenario_falla', 'Escenario de Falla'),
    ]
    for ed in estrategia_dims:
        columns.append((f'dim_{ed.dimension_id}', ed.dimension.nombre))
    columns.extend([
        ('valor_cons_total', 'Valor Consecuencia Total'),
        ('valor_criticidad_equipo', 'Valor Criticidad Equipo'),
        ('criticidad_final', 'Criticidad Final'),
    ])
    if include_actions:
        columns.append(('acciones', 'Acciones'))

    rows = []
    complete_count = 0
    incomplete_count = 0
    for crit in criticidades:
        status = getattr(crit.aca_carga, 'status', '') or models.Carga.STATUS_COMPLETO
        if status == models.Carga.STATUS_INCOMPLETO:
            incomplete_count += 1
        else:
            complete_count += 1
        row = {
            'id': crit.id,
            'cliente': servicio.codigo_servicio,
            'status': status,
            'fecha_analisis': crit.aca_carga.fecha_analisis.strftime('%d/%m/%Y') if crit.aca_carga and crit.aca_carga.fecha_analisis else '',
            'ubicacion_tecnica': crit.equipo.ut if crit.equipo else '',
            'descripcion_ut': crit.equipo.descripcion_ut if crit.equipo else '',
            'equipo': crit.equipo.nombre_equipo if crit.equipo else '',
            'tag': crit.equipo.tag_display if crit.equipo else '',
            'escenario_falla': crit.escenario_falla,
            'frecuencia_original': crit.frecuencia_original,
            'frecuencia_normalizada': crit.frecuencia_normalizada,
            'valor_cons_total': crit.valor_cons_total,
            'indicador_criticidad': crit.indicador_criticidad,
            'valor_criticidad_equipo': crit.valor_criticidad_equipo,
            'criticidad_final': crit.criticidad_final,
        }
        dims_map = {item.dimension_id: item for item in crit.dimensiones.all()}
        for ed in estrategia_dims:
            item = dims_map.get(ed.dimension_id)
            row[f'dim_{ed.dimension_id}'] = _dimension_display_value(item) if item else ''
        rows.append(row)

    return columns, rows, complete_count, incomplete_count


@login_required
def service_aca_export(request, pk, formato):
    servicio, _permission = _service_or_404(request, pk, edit=False)
    columns, rows, _complete_count, _incomplete_count = _service_aca_table_data(servicio)
    formato = (formato or '').lower()
    if formato == 'excel':
        return _export_xlsx_response(
            _export_filename('ACA', servicio, 'xlsx'),
            'Registros ACA',
            columns,
            rows,
        )
    if formato == 'pdf':
        return _export_pdf_response(
            _export_filename('ACA', servicio, 'pdf'),
            f'Registros ACA - {servicio.codigo_servicio}',
            columns,
            rows,
        )
    raise Http404('Formato de exportación no soportado.')


@login_required
@transaction.atomic
def service_aca_new(request, pk):
    servicio, permission = _service_or_404(request, pk, edit=True)
    if not servicio.estrategia_id:
        messages.warning(request, 'El servicio no tiene estrategia asociada. Asigna una antes de registrar ACA.')
        return redirect('service_detail', pk=servicio.pk)

    strategy = servicio.estrategia
    profile = get_profile_for_user(request.user)
    edit_crit_id = request.POST.get('crit_id') if request.method == 'POST' else request.GET.get('edit')
    edit_crit = None
    if edit_crit_id:
        edit_crit = get_object_or_404(
            models.Criticidad.objects.select_related('aca_carga', 'equipo'),
            pk=edit_crit_id,
            aca_carga__servicio=servicio,
        )

    is_draft = request.method == 'POST' and request.POST.get('save_as') == 'draft'
    existing_is_draft = bool(
        edit_crit
        and getattr(edit_crit.aca_carga, 'status', '') == models.Carga.STATUS_INCOMPLETO
    )
    allow_incomplete = is_draft or existing_is_draft
    matrix_selector = _service_matrix_selector(servicio)
    initial_base = {}
    if edit_crit:
        initial_base = {
            'fecha_analisis': edit_crit.aca_carga.fecha_analisis if edit_crit.aca_carga else timezone.localdate(),
            'version_carga': edit_crit.aca_carga.version_carga if edit_crit.aca_carga else Decimal('1.0'),
            'origen': edit_crit.aca_carga.origen if edit_crit.aca_carga else 'Manual',
            'equipo': edit_crit.equipo,
            'escenario_falla': edit_crit.escenario_falla,
            'frecuencia_normalizada': edit_crit.frecuencia_normalizada,
        }
        matriz = matrix_selector.get('matriz') if matrix_selector else None
        if matriz:
            selected_cell = _resolve_matrix_cell_from_dimension_records(
                matriz,
                edit_crit.dimensiones.select_related('dimension', 'estrategia_dimension').all(),
            )
            if not selected_cell:
                selected_cell = _matrix_cell_for_axis_values(
                    matriz,
                    edit_crit.frecuencia_normalizada,
                    edit_crit.valor_cons_total,
                )
            if selected_cell:
                initial_base['matrix_celda'] = selected_cell.pk

    base_form = ServicioACARegistroForm(
        request.POST or None,
        initial=initial_base,
        service=servicio,
        allow_incomplete=allow_incomplete,
    )
    excluded_dimension_ids = _aca_excluded_dimension_ids(
        strategy,
        matrix_selector.get('matriz') if matrix_selector else None,
    )
    dimension_initial = _dimension_formset_initial_from_criticidad(
        edit_crit,
        strategy,
        exclude_dimension_ids=excluded_dimension_ids,
    ) if edit_crit else None
    dimension_formset = _dimension_formset(
        request,
        strategy,
        initial=dimension_initial,
        exclude_dimension_ids=excluded_dimension_ids,
    )

    if request.method == 'POST' and base_form.is_valid() and dimension_formset.is_valid():
        status = models.Carga.STATUS_INCOMPLETO if is_draft else models.Carga.STATUS_COMPLETO
        now = timezone.now()

        if edit_crit:
            carga = edit_crit.aca_carga
            carga.status = status
            carga.actualizado = now
            carga.estrategia = strategy
            carga.servicio = servicio
            carga.usuario = profile
            carga.save(update_fields=['status', 'actualizado', 'estrategia', 'servicio', 'usuario'])
        else:
            version_carga = _next_service_aca_version(servicio)
            carga = models.Carga.objects.create(
                fecha_analisis=timezone.localdate(),
                version_carga=version_carga,
                origen='Manual',
                status=status,
                creado_en=now,
                actualizado=now,
                estrategia=strategy,
                servicio=servicio,
                usuario=profile,
            )

        selected_cell = base_form.cleaned_data.get('matrix_celda')
        matriz = matrix_selector.get('matriz') if matrix_selector else None
        if not selected_cell and matriz:
            prepared_items, _source_values = _prepare_dimension_items(strategy, dimension_formset)
            selected_cell = _resolve_matrix_cell_from_dimension_records(matriz, prepared_items)
        frecuencia_normalizada = selected_cell.probabilidad.valor if selected_cell else None
        valor_cons_total = selected_cell.impacto_nivel.valor if selected_cell else None

        evaluacion = edit_crit or models.Criticidad(
            creado_en=now,
            aca_carga=carga,
        )
        evaluacion.escenario_falla = base_form.cleaned_data.get('escenario_falla') or ''
        evaluacion.frecuencia_original = None
        evaluacion.frecuencia_normalizada = frecuencia_normalizada
        evaluacion.valor_cons_total = valor_cons_total
        evaluacion.indicador_criticidad = ''
        evaluacion.valor_criticidad_equipo = selected_cell.resultado_num if selected_cell else None
        evaluacion.criticidad_final = selected_cell.clasificacion if selected_cell else ''
        evaluacion.aca_carga = carga
        evaluacion.equipo = base_form.cleaned_data.get('equipo')
        evaluacion.save()

        _save_dimension_formset(evaluacion, strategy, dimension_formset)

        if selected_cell:
            _save_matrix_dimensions(evaluacion, strategy, selected_cell)

        _sync_criticidad_resumen(evaluacion, strategy)

        if is_draft:
            messages.success(request, 'Borrador ACA guardado correctamente.')
        elif edit_crit:
            messages.success(request, 'Registro ACA actualizado correctamente.')
        else:
            messages.success(request, 'Registro ACA creado correctamente.')
        return redirect('service_aca_list', pk=servicio.pk)

    return render(request, 'core/aca/aca_registro_form.html', {
        'service': servicio,
        'permission': permission,
        'base_form': base_form,
        'dimension_formset': dimension_formset,
        'selected_strategy': strategy,
        'matrix_selector': matrix_selector,
        'service_equipment_payload': _service_equipment_browser_payload(servicio),
        'service_equipment_endpoints': _service_equipment_endpoints(servicio),
        'auto_version': edit_crit.aca_carga.version_carga if edit_crit and edit_crit.aca_carga else _next_service_aca_version(servicio),
        'auto_fecha_analisis': edit_crit.aca_carga.fecha_analisis if edit_crit and edit_crit.aca_carga else timezone.localdate(),
        'editing_crit': edit_crit,
    })


@login_required
def service_aca_edit(request, service_pk, crit_pk):
    servicio, _permission = _service_or_404(request, service_pk, edit=True)
    crit = get_object_or_404(
        models.Criticidad.objects.select_related('aca_carga', 'equipo'),
        pk=crit_pk,
        aca_carga__servicio=servicio,
    )

    url = f"{reverse('service_aca_new', kwargs={'pk': servicio.pk})}?edit={crit.pk}"
    return redirect(url)


@login_required
@transaction.atomic
def service_aca_delete(request, service_pk, crit_pk):
    servicio, _permission = _service_or_404(request, service_pk, edit=True)
    crit = get_object_or_404(
        models.Criticidad.objects.select_related('aca_carga'),
        pk=crit_pk,
        aca_carga__servicio=servicio,
    )

    if request.method == 'POST':
        carga = crit.aca_carga
        crit.dimensiones.all().delete()
        crit.delete()

        if carga and not carga.criticidades.exists():
            carga.delete()

        messages.success(request, 'Registro ACA eliminado correctamente.')
        return redirect('service_aca_list', pk=servicio.pk)

    return redirect('service_aca_list', pk=servicio.pk)


def aca_registro_new(request):
    servicios_editables = [
        service
        for service in get_accessible_services(request.user)
        if get_service_permission(request.user, service)['can_edit']
    ] if request.user.is_authenticated else []
    if len(servicios_editables) == 1:
        return redirect('service_aca_new', pk=servicios_editables[0].pk)
    messages.info(request, 'Selecciona primero un servicio para registrar un ACA.')
    return redirect('service_list')
