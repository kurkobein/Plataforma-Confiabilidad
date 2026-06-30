import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import DataError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core import models
from evaluation_tables.forms import MatrizBuilderForm
from core.views import (
    _build_homologated_matrix_preview,
    _build_matrix_original_preview,
    _cell_data_from_request,
    _calc_slug,
    _definitions_from_request,
    _decimal_or_none,
    _default_legend_for_bounds,
    _ensure_admin_access,
    _ensure_matrix_strategy_dimensions,
    _is_generated_matrix_axis_dimension,
    _json_loads_safe,
    _json_payload,
    _json_safe,
    _legend_from_matrix,
    _level_defs_from_strategy_dimension,
    _matrix_level_dicts,
    _matrix_legend_payload,
    _matrix_preview_from_defs,
    _matrix_resolution_mode,
    _matrix_result_values,
    _matrix_stats,
    _matrix_ui_payload,
    _matrix_value_bounds,
    _persist_matrix_grid,
    _safe_legend_items,
    _service_or_404,
    _validate_legend_items,
)
from core.access import is_mindco_user
from core.services.criticality_rules import matrix_rule_config, normalize_rules


CATALOG_NUMBER_MAX = Decimal('9999999999.99')
CATALOG_NUMBER_DECIMAL_PLACES = 2
AUTO_IMPACT_TOTAL_NAME = 'Impacto total'
AUTO_IMPACT_TOTAL_FIELD = 'impacto_total'
AUTO_IMPACT_TOTAL_DESCRIPTION = 'Total automatico de las dimensiones de impacto configuradas para ACA.'
AUTO_MATRIX_MODE = 'automatica_maximo_teorico'


def _normalize_process_usage(value):
    value = str(value or models.EstrategiaDimension.PROCESO_ACA).strip()
    if value == getattr(models.EstrategiaDimension, 'PROCESO_RCM_LEGACY', 'rcm'):
        return models.EstrategiaDimension.PROCESO_FMECA
    return value


def _catalog_for_strategy_dimension(estrategia_dimension):
    try:
        return getattr(estrategia_dimension, 'catalogo', None)
    except models.DimensionCatalogo.DoesNotExist:
        return None


def _is_auto_impact_total_dimension(estrategia_dimension):
    if not estrategia_dimension:
        return False
    catalogo = _catalog_for_strategy_dimension(estrategia_dimension)
    if catalogo and (
        catalogo.campo == AUTO_IMPACT_TOTAL_FIELD
        or (catalogo.nombre or '').strip().lower() == AUTO_IMPACT_TOTAL_NAME.lower()
    ):
        return True
    dimension = getattr(estrategia_dimension, 'dimension', None)
    return bool(
        dimension
        and (dimension.nombre or '').strip().lower() == AUTO_IMPACT_TOTAL_NAME.lower()
        and (dimension.tipo_calculo or '').strip()
    )


def _impact_total_source_dimensions(estrategia, exclude_ed_id=None):
    source_dimensions = (
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
    if exclude_ed_id:
        source_dimensions = source_dimensions.exclude(pk=exclude_ed_id)

    operandos = []
    for estrategia_dimension in source_dimensions:
        if _is_auto_impact_total_dimension(estrategia_dimension):
            continue
        if _is_generated_matrix_axis_dimension(estrategia_dimension):
            continue
        if (estrategia_dimension.dimension.tipo_calculo or '').strip():
            continue
        catalogo = _catalog_for_strategy_dimension(estrategia_dimension)
        nombre = ((catalogo.nombre if catalogo else '') or estrategia_dimension.dimension.nombre or '').strip()
        campo = ((catalogo.campo if catalogo else '') or _safe_slug(estrategia_dimension.dimension.nombre)).strip()
        is_impact_source = (
            estrategia_dimension.dimension.tipo_funcional == 'impacto'
            or _safe_slug(nombre).startswith('impacto_')
            or _safe_slug(campo).startswith('impacto_')
        )
        if not is_impact_source:
            continue
        operandos.append({
            'estrategia_dimension_id': estrategia_dimension.pk,
            'dimension_id': estrategia_dimension.dimension_id,
            'campo': campo,
            'nombre': nombre,
        })
    return operandos


def _impact_total_calculation_config(estrategia, exclude_ed_id=None):
    operandos = _impact_total_source_dimensions(estrategia, exclude_ed_id=exclude_ed_id)
    return {
        'version': 1,
        'auto_impact_total': True,
        'pasos': [{'operacion': 'suma', 'operandos': operandos}],
        'operandos': operandos,
    }


def _impact_total_config_source_ids(config):
    config = config if isinstance(config, dict) else {}
    source_ids = set()
    steps = config.get('pasos') if isinstance(config.get('pasos'), list) else []
    operands = []
    if steps:
        for step in steps:
            if not isinstance(step, dict):
                continue
            operands.extend(step.get('operandos') or step.get('campos') or step.get('sources') or [])
    else:
        operands = config.get('operandos') or config.get('campos') or config.get('sources') or []
    for operand in operands:
        if not isinstance(operand, dict):
            continue
        raw_id = operand.get('estrategia_dimension_id') or operand.get('ed_id')
        if raw_id not in (None, ''):
            source_ids.add(str(raw_id))
    return source_ids


def _impact_total_config_is_weighted(config):
    config = config if isinstance(config, dict) else {}
    steps = config.get('pasos') if isinstance(config.get('pasos'), list) else []
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get('modo') == 'ponderado' or step.get('ponderado') is True:
            return True
        operands = step.get('operandos') or []
        if any(isinstance(operand, dict) and operand.get('peso') not in (None, '') for operand in operands):
            return True
    operands = config.get('operandos') if isinstance(config.get('operandos'), list) else []
    return any(isinstance(operand, dict) and operand.get('peso') not in (None, '') for operand in operands)


def _calculation_config_has_sources(config):
    config = config if isinstance(config, dict) else {}
    pasos = config.get('pasos') if isinstance(config.get('pasos'), list) else []
    if any(isinstance(step, dict) and step.get('operandos') for step in pasos):
        return True
    operandos = config.get('operandos') if isinstance(config.get('operandos'), list) else []
    return bool(operandos)


def _ensure_auto_impact_total_dimension(estrategia):
    catalogo = (
        models.DimensionCatalogo.objects.filter(
            estrategia_dimension__estrategia=estrategia,
            campo=AUTO_IMPACT_TOTAL_FIELD,
        )
        .select_related('estrategia_dimension', 'estrategia_dimension__dimension')
        .first()
    )
    if not catalogo:
        catalogo = (
            models.DimensionCatalogo.objects.filter(
                estrategia_dimension__estrategia=estrategia,
                nombre__iexact=AUTO_IMPACT_TOTAL_NAME,
            )
            .select_related('estrategia_dimension', 'estrategia_dimension__dimension')
            .first()
        )
    if catalogo:
        estrategia_dimension = catalogo.estrategia_dimension
        dimension = estrategia_dimension.dimension
    else:
        estrategia_dimension = (
            models.EstrategiaDimension.objects.filter(
                estrategia=estrategia,
                dimension__nombre__iexact=AUTO_IMPACT_TOTAL_NAME,
            )
            .select_related('dimension')
            .first()
        )
        if estrategia_dimension:
            dimension = estrategia_dimension.dimension
            catalogo, _ = models.DimensionCatalogo.objects.get_or_create(
                estrategia_dimension=estrategia_dimension,
                defaults={
                    'nombre': AUTO_IMPACT_TOTAL_NAME,
                    'campo': AUTO_IMPACT_TOTAL_FIELD,
                    'tipo': 'calculado',
                    'descripcion': AUTO_IMPACT_TOTAL_DESCRIPTION,
                    'activa': True,
                },
            )
        else:
            max_order = (
                models.EstrategiaDimension.objects.filter(estrategia=estrategia)
                .order_by('-orden')
                .values_list('orden', flat=True)
                .first()
                or 0
            )
            dimension = models.Dimension.objects.create(
                nombre=AUTO_IMPACT_TOTAL_NAME,
                descripcion=AUTO_IMPACT_TOTAL_DESCRIPTION,
                tipo_funcional='resultado',
                tipo_dato='numerico',
                tipo_calculo='suma',
                config_calculo='{}',
            )
            estrategia_dimension = models.EstrategiaDimension.objects.create(
                estrategia=estrategia,
                dimension=dimension,
                orden=max_order + 1,
                obligatorio=True,
                considerar_avance_aca=False,
                visible_en_listado_aca=True,
                considerar_avance_fmeca=False,
                visible_en_listado_fmeca=False,
                proceso_uso=models.EstrategiaDimension.PROCESO_ACA,
                activo=True,
            )
            catalogo = models.DimensionCatalogo.objects.create(
                estrategia_dimension=estrategia_dimension,
                nombre=AUTO_IMPACT_TOTAL_NAME,
                campo=AUTO_IMPACT_TOTAL_FIELD,
                tipo='calculado',
                descripcion=AUTO_IMPACT_TOTAL_DESCRIPTION,
                activa=True,
            )

    config_calculo = _json_loads_safe(dimension.config_calculo, {})
    expected_config = _impact_total_calculation_config(
        estrategia,
        exclude_ed_id=estrategia_dimension.pk,
    )
    expected_source_ids = {
        str(operand.get('estrategia_dimension_id'))
        for operand in expected_config.get('operandos', [])
        if operand.get('estrategia_dimension_id') not in (None, '')
    }
    current_source_ids = _impact_total_config_source_ids(config_calculo)
    should_rebuild_config = (
        not config_calculo.get('auto_impact_total')
        or not _calculation_config_has_sources(config_calculo)
        or _impact_total_config_is_weighted(config_calculo)
        or current_source_ids != expected_source_ids
    )
    if should_rebuild_config:
        config_calculo = _impact_total_calculation_config(
            estrategia,
            exclude_ed_id=estrategia_dimension.pk,
        )
    config_calculo['auto_impact_total'] = True
    dimension.nombre = AUTO_IMPACT_TOTAL_NAME
    dimension.descripcion = AUTO_IMPACT_TOTAL_DESCRIPTION
    dimension.tipo_funcional = 'resultado'
    dimension.tipo_dato = 'numerico'
    dimension.tipo_calculo = 'suma'
    dimension.config_calculo = json.dumps(config_calculo, ensure_ascii=False)
    dimension.save()

    estrategia_dimension.obligatorio = True
    estrategia_dimension.considerar_avance_aca = False
    estrategia_dimension.visible_en_listado_aca = True
    estrategia_dimension.considerar_avance_fmeca = False
    estrategia_dimension.visible_en_listado_fmeca = False
    estrategia_dimension.proceso_uso = models.EstrategiaDimension.PROCESO_ACA
    estrategia_dimension.activo = True
    estrategia_dimension.save()

    catalogo.nombre = AUTO_IMPACT_TOTAL_NAME
    catalogo.campo = AUTO_IMPACT_TOTAL_FIELD
    catalogo.tipo = 'calculado'
    catalogo.descripcion = AUTO_IMPACT_TOTAL_DESCRIPTION
    catalogo.activa = True
    catalogo.save()
    return estrategia_dimension


# ---------------------------------------------------------------------------
# Editor de dimensiones y tablas por estrategia
# ---------------------------------------------------------------------------
def _serialize_dimension_catalog(catalogo):
    ed = catalogo.estrategia_dimension
    dimension = ed.dimension
    protegida = _is_auto_impact_total_dimension(ed)
    columnas = list(catalogo.columnas.all().order_by('orden', 'id'))
    filas = []
    for fila in catalogo.filas.prefetch_related('celdas__columna').all().order_by('orden', 'id'):
        values = fila.values_map()
        filas.append({
            'id': fila.pk,
            'orden': fila.orden,
            'etiqueta': fila.etiqueta,
            'valores': _json_safe({col.clave_interna: values.get(col.clave_interna, '') for col in columnas}),
        })
    return {
        'id': catalogo.pk,
        'estrategia_dimension_id': ed.pk,
        'dimension_id': dimension.pk,
        'nombre': catalogo.nombre or dimension.nombre,
        'campo': catalogo.campo,
        'tipo': catalogo.tipo,
        'descripcion': catalogo.descripcion or dimension.descripcion or '',
        'tipo_funcional': dimension.tipo_funcional,
        'tipo_dato': dimension.tipo_dato,
        'tipo_calculo': dimension.tipo_calculo or '',
        'config_calculo': _json_safe(_json_loads_safe(dimension.config_calculo, {})),
        'obligatorio': ed.obligatorio,
        'considerar_avance_aca': getattr(ed, 'considerar_avance_aca', True),
        'visible_en_listado_aca': getattr(ed, 'visible_en_listado_aca', True),
        'considerar_avance_fmeca': getattr(ed, 'considerar_avance_fmeca', True),
        'visible_en_listado_fmeca': getattr(ed, 'visible_en_listado_fmeca', True),
        'proceso_uso': _normalize_process_usage(ed.proceso_uso),
        'activo': ed.activo,
        'protegida': protegida,
        'auto_impact_total': protegida,
        'columnas': [
            {
                'id': col.pk,
                'nombre_columna': col.nombre_columna,
                'clave_interna': col.clave_interna,
                'tipo_dato': col.tipo_dato,
                'visible_en_registro': getattr(col, 'visible_en_registro', True),
                'orden': col.orden,
            }
            for col in columnas
        ],
        'filas': filas,
    }


def _safe_slug(value):
    value = unicodedata.normalize('NFKD', str(value or '').strip().lower())
    value = ''.join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r'[^a-z0-9]+', '_', value, flags=re.IGNORECASE)
    return value.strip('_')[:100] or 'dimension'


def _unique_column_key(base_value, used_keys, fallback='columna'):
    base = _safe_slug(base_value or fallback)
    candidate = base[:100] or fallback
    counter = 2
    while candidate in used_keys:
        suffix = f'_{counter}'
        candidate = f'{base[:100 - len(suffix)]}{suffix}' or f'{fallback}_{counter}'
        counter += 1
    used_keys.add(candidate)
    return candidate


def _default_columns_for_type(tipo):
    if tipo == 'numerico_libre':
        return []
    if tipo == 'rangos':
        return [
            {'nombre_columna': 'Etiqueta', 'clave_interna': 'etiqueta', 'tipo_dato': 'texto'},
            {'nombre_columna': 'Desde', 'clave_interna': 'limite_inferior', 'tipo_dato': 'numero'},
            {'nombre_columna': 'Hasta', 'clave_interna': 'limite_superior', 'tipo_dato': 'numero'},
            {'nombre_columna': 'Valor principal', 'clave_interna': 'valor_numerico', 'tipo_dato': 'numero'},
        ]
    return [
        {'nombre_columna': 'Etiqueta', 'clave_interna': 'etiqueta', 'tipo_dato': 'texto'},
        {'nombre_columna': 'Valor principal', 'clave_interna': 'valor_numerico', 'tipo_dato': 'numero'},
        {'nombre_columna': 'Booleano', 'clave_interna': 'valor_booleano', 'tipo_dato': 'booleano'},
    ]


DIMENSION_EDITOR_RECOMMENDATIONS = [
    {
        'nombre': 'Frecuencia falla',
        'clave': 'frecuencia_falla',
        'tipo_dato': 'tabla',
        'tipo_funcional': 'probabilidad',
        'rol_columna': 'probabilidad',
    },
    {
        'nombre': 'Impacto seguridad',
        'clave': 'impacto_seguridad',
        'tipo_dato': 'tabla',
        'tipo_funcional': 'impacto',
        'rol_columna': 'impacto',
    },
    {
        'nombre': 'Impacto ambiental',
        'clave': 'impacto_ambiental',
        'tipo_dato': 'tabla',
        'tipo_funcional': 'impacto',
        'rol_columna': 'impacto',
    },
    {
        'nombre': 'Impacto operación',
        'clave': 'impacto_operacion',
        'tipo_dato': 'tabla',
        'tipo_funcional': 'impacto',
        'rol_columna': 'impacto',
    },
    {
        'nombre': 'Impacto entorno',
        'clave': 'impacto_entorno',
        'tipo_dato': 'tabla',
        'tipo_funcional': 'impacto',
        'rol_columna': 'impacto',
    },
    {
        'nombre': 'Impacto calidad',
        'clave': 'impacto_calidad',
        'tipo_dato': 'tabla',
        'tipo_funcional': 'impacto',
        'rol_columna': 'impacto',
    },
    {
        'nombre': 'Impacto costo',
        'clave': 'impacto_costo',
        'tipo_dato': 'tabla',
        'tipo_funcional': 'impacto',
        'rol_columna': 'impacto',
    },
]


def _dimension_editor_suggestions():
    def _add(target, seen_names, suggestion):
        nombre = str(suggestion.get('nombre') or '').strip()
        if not nombre:
            return
        name_key = _safe_slug(nombre)
        if name_key in seen_names:
            current_process = _normalize_process_usage(suggestion.get('proceso_uso'))
            for existing in target:
                if _safe_slug(existing.get('nombre')) != name_key:
                    continue
                if existing.get('proceso_uso') != current_process:
                    existing['proceso_uso'] = models.EstrategiaDimension.PROCESO_AMBOS
            return
        seen_names.add(name_key)
        clave = str(suggestion.get('clave') or '').strip() or name_key
        target.append({
            'nombre': nombre,
            'clave': _safe_slug(clave),
            'tipo_dato': suggestion.get('tipo_dato') or 'tabla',
            'tipo_funcional': suggestion.get('tipo_funcional') or 'atributo',
            'rol_columna': suggestion.get('rol_columna') or '',
            'proceso_uso': _normalize_process_usage(suggestion.get('proceso_uso')),
        })

    dimension_suggestions = []
    column_suggestions = []
    seen_dimensions = set()
    seen_columns = set()

    for suggestion in DIMENSION_EDITOR_RECOMMENDATIONS:
        _add(dimension_suggestions, seen_dimensions, {
            **suggestion,
            'proceso_uso': models.EstrategiaDimension.PROCESO_AMBOS,
        })

    strategy_dimensions = (
        models.EstrategiaDimension.objects.filter(activo=True)
        .select_related('dimension')
        .order_by('dimension__nombre', 'id')
    )
    for estrategia_dimension in strategy_dimensions:
        if _hide_from_dimension_tables_editor(estrategia_dimension):
            continue
        dimension = estrategia_dimension.dimension
        _add(dimension_suggestions, seen_dimensions, {
            'nombre': dimension.nombre,
            'clave': _safe_slug(dimension.nombre),
            'tipo_dato': dimension.tipo_dato or 'tabla',
            'tipo_funcional': dimension.tipo_funcional or 'atributo',
            'proceso_uso': estrategia_dimension.proceso_uso,
        })

    for suggestion in DIMENSION_EDITOR_RECOMMENDATIONS:
        _add(column_suggestions, seen_columns, {
            'nombre': suggestion['nombre'],
            'clave': suggestion['clave'],
            'tipo_dato': 'numero',
            'tipo_funcional': suggestion['tipo_funcional'],
            'rol_columna': suggestion['rol_columna'],
        })

    for column in models.DimensionCatalogoColumna.objects.exclude(nombre_columna='').order_by(
        'nombre_columna',
        'id',
    ).values('nombre_columna', 'clave_interna', 'tipo_dato'):
        _add(column_suggestions, seen_columns, {
            'nombre': column['nombre_columna'],
            'clave': column['clave_interna'] or _safe_slug(column['nombre_columna']),
            'tipo_dato': column['tipo_dato'] or 'texto',
        })

    return dimension_suggestions, column_suggestions


def _serialize_strategy_dimension_without_catalog(ed):
    dimension = ed.dimension
    protegida = _is_auto_impact_total_dimension(ed)
    tipo = 'numerico_libre' if dimension.tipo_dato == 'numerico' and not dimension.tipo_calculo else 'opciones'
    return {
        'id': f'ed_{ed.pk}',
        'estrategia_dimension_id': ed.pk,
        'dimension_id': dimension.pk,
        'nombre': dimension.nombre,
        'campo': _safe_slug(dimension.nombre or f'dimension_{dimension.pk}'),
        'tipo': tipo,
        'descripcion': dimension.descripcion or '',
        'tipo_funcional': dimension.tipo_funcional,
        'tipo_dato': dimension.tipo_dato,
        'tipo_calculo': dimension.tipo_calculo or '',
        'config_calculo': _json_safe(_json_loads_safe(dimension.config_calculo, {})),
        'obligatorio': ed.obligatorio,
        'considerar_avance_aca': getattr(ed, 'considerar_avance_aca', True),
        'visible_en_listado_aca': getattr(ed, 'visible_en_listado_aca', True),
        'considerar_avance_fmeca': getattr(ed, 'considerar_avance_fmeca', True),
        'visible_en_listado_fmeca': getattr(ed, 'visible_en_listado_fmeca', True),
        'proceso_uso': _normalize_process_usage(ed.proceso_uso),
        'activo': ed.activo,
        'protegida': protegida,
        'auto_impact_total': protegida,
        'columnas': [] if dimension.tipo_calculo else _default_columns_for_type(tipo),
        'filas': [],
    }


def _hide_from_dimension_tables_editor(estrategia_dimension):
    if _is_auto_impact_total_dimension(estrategia_dimension):
        return False
    return _is_generated_matrix_axis_dimension(estrategia_dimension)


def _strategy_catalogs_payload(estrategia, only_active=True):
    _ensure_auto_impact_total_dimension(estrategia)
    eds = models.EstrategiaDimension.objects.filter(
        estrategia=estrategia
    ).select_related('dimension').prefetch_related(
        'catalogo__columnas',
        'catalogo__filas__celdas__columna',
    ).order_by('orden', 'id')

    if only_active:
        eds = eds.filter(activo=True)

    payload = []
    for ed in eds:
        if _hide_from_dimension_tables_editor(ed):
            continue

        try:
            catalogo = ed.catalogo
        except models.DimensionCatalogo.DoesNotExist:
            catalogo = None

        if catalogo:
            payload.append(_serialize_dimension_catalog(catalogo))
        else:
            payload.append(_serialize_strategy_dimension_without_catalog(ed))

    return payload


def _normalize_catalog_cell_value(col_type, raw):
    if col_type == 'numero':
        return {'valor_numero': _decimal_or_none(raw), 'valor_texto': '', 'valor_booleano': None}
    if col_type == 'booleano':
        bool_val = raw if raw in (True, False) else str(raw).strip().lower() in {'true', '1', 'si', 'sí'} if raw not in (None, '') else None
        return {'valor_numero': None, 'valor_texto': '', 'valor_booleano': bool_val}
    return {'valor_numero': None, 'valor_texto': '' if raw is None else str(raw), 'valor_booleano': None}


def _calculated_dimension_source_key(estrategia_dimension):
    try:
        catalogo = getattr(estrategia_dimension, 'catalogo', None)
        if catalogo and catalogo.campo:
            return catalogo.campo
    except models.DimensionCatalogo.DoesNotExist:
        pass
    return _safe_slug(estrategia_dimension.dimension.nombre)


def _calculated_dimension_source_candidates(source_ref):
    if source_ref in (None, ''):
        return []
    if isinstance(source_ref, dict):
        candidates = []
        estrategia_dimension_id = (
            source_ref.get('estrategia_dimension_id')
            or source_ref.get('ed_id')
            or source_ref.get('estrategiaDimensionId')
        )
        if estrategia_dimension_id not in (None, ''):
            candidates.extend([f'ed:{estrategia_dimension_id}', f'estrategia_dimension:{estrategia_dimension_id}'])
        dimension_id = source_ref.get('dimension_id')
        if dimension_id not in (None, ''):
            candidates.extend([str(dimension_id), f'dim:{dimension_id}'])
        for key in ['campo', 'nombre', 'source', 'fuente', 'dependencia', 'depende_de']:
            value = source_ref.get(key)
            if value not in (None, '') and not isinstance(value, dict):
                candidates.append(str(value).strip())
        return [candidate for candidate in candidates if candidate]
    return [str(source_ref).strip()]


def _remember_calculated_source_value(source_values, estrategia_dimension, value):
    decimal_value = _decimal_or_none(value)
    if decimal_value is None or not estrategia_dimension:
        return
    dimension = estrategia_dimension.dimension
    campo = _calculated_dimension_source_key(estrategia_dimension)
    keys = [
        f'ed:{estrategia_dimension.id}',
        f'estrategia_dimension:{estrategia_dimension.id}',
        str(dimension.id),
        f'dim:{dimension.id}',
        dimension.nombre,
        _calc_slug(dimension.nombre),
        campo,
        _calc_slug(campo),
    ]
    for key in keys:
        if key:
            source_values[key] = decimal_value


def _calculated_source_value(source_values, source_ref, previous_result=None):
    if isinstance(source_ref, dict):
        if source_ref.get('resultado') is True or source_ref.get('tipo') == 'resultado':
            return previous_result
    else:
        raw = str(source_ref)
        if raw in {'$resultado', '__resultado__', 'resultado_anterior'}:
            return previous_result

    for candidate in _calculated_dimension_source_candidates(source_ref):
        value = source_values.get(candidate)
        if value is None:
            value = source_values.get(_calc_slug(candidate))
        decimal_value = _decimal_or_none(value)
        if decimal_value is not None:
            return decimal_value
    return None


def _calculated_operation_result(operation, values):
    if not values:
        return None
    if operation == 'suma':
        return sum(values, Decimal('0'))
    if operation == 'resta':
        result = values[0]
        for value in values[1:]:
            result -= value
        return result
    if operation == 'multiplicacion':
        result = Decimal('1')
        for value in values:
            result *= value
        return result
    if operation == 'division':
        result = values[0]
        for value in values[1:]:
            if value == 0:
                return None
            result /= value
        return result
    if operation == 'maximo':
        return max(values)
    if operation == 'minimo':
        return min(values)
    return None


def _calculated_dimension_steps(tipo_calculo, config_calculo):
    operation = (tipo_calculo or '').strip().lower()
    if not operation:
        return []
    if isinstance(config_calculo, str):
        config_calculo = _json_loads_safe(config_calculo, {})
    if not isinstance(config_calculo, dict):
        config_calculo = {}
    raw_steps = config_calculo.get('pasos') or config_calculo.get('steps') or []
    if isinstance(raw_steps, list) and raw_steps:
        steps = []
        for raw_step in raw_steps:
            if not isinstance(raw_step, dict):
                continue
            step_operation = str(
                raw_step.get('operacion')
                or raw_step.get('tipo_calculo')
                or raw_step.get('operation')
                or ''
            ).strip().lower()
            operands = raw_step.get('operandos') or raw_step.get('campos') or raw_step.get('sources') or []
            if step_operation and isinstance(operands, list):
                steps.append({
                    'operacion': step_operation,
                    'operandos': operands,
                    'modo': raw_step.get('modo') or ('ponderado' if raw_step.get('ponderado') is True else ''),
                })
        return steps
    operands = config_calculo.get('operandos') or config_calculo.get('campos') or config_calculo.get('sources') or []
    return [{'operacion': operation, 'operandos': operands if isinstance(operands, list) else []}]


def _calculated_operand_weight(operand):
    if not isinstance(operand, dict):
        return None
    return _decimal_or_none(operand.get('peso', operand.get('ponderador', operand.get('weight'))))


def _weighted_calculated_values(operands, resolved_values):
    if not operands or len(operands) != len(resolved_values):
        return None
    weights = [_calculated_operand_weight(operand) for operand in operands]
    if all(weight is None for weight in weights):
        equal_weight = Decimal('1') / Decimal(len(weights))
        weights = [equal_weight] * len(weights)
    elif any(weight is None for weight in weights):
        assigned = sum((weight for weight in weights if weight is not None), Decimal('0'))
        missing_count = sum(1 for weight in weights if weight is None)
        remaining = Decimal('1') - assigned
        if remaining < 0:
            return None
        missing_weight = remaining / Decimal(missing_count)
        weights = [missing_weight if weight is None else weight for weight in weights]
    if any(weight < 0 or weight > 1 for weight in weights):
        return None
    if abs(sum(weights, Decimal('0')) - Decimal('1')) > Decimal('0.0001'):
        return None
    return [value * weight for value, weight in zip(resolved_values, weights)]


def _evaluate_calculated_dimension(tipo_calculo, config_calculo, source_values):
    result = None
    for step in _calculated_dimension_steps(tipo_calculo, config_calculo):
        values = []
        for operand in step['operandos']:
            value = _calculated_source_value(source_values, operand, previous_result=result)
            if value is None:
                return None
            values.append(value)
        if step.get('modo') == 'ponderado' and step['operacion'] == 'suma':
            values = _weighted_calculated_values(step['operandos'], values)
            if values is None:
                return None
        result = _calculated_operation_result(step['operacion'], values)
        if result is None:
            return None
    return result


def _catalog_row_primary_numeric(row):
    if not row:
        return None
    values = row.values_map()
    for key in ['valor_numerico', 'valor_principal', 'valor', 'nivel', 'puntaje']:
        value = values.get(key)
        if value not in (None, ''):
            return _decimal_or_none(value)
    for key, value in values.items():
        if key in {'limite_inferior', 'limite_superior', 'desde', 'hasta', 'min', 'max', 'minimo', 'mínimo', 'maximo', 'máximo'}:
            continue
        decimal_value = _decimal_or_none(value)
        if decimal_value is not None:
            return decimal_value
    return None


def _record_numeric_value(record):
    value = getattr(record, 'valor_numerico', None)
    if value is not None:
        return _decimal_or_none(value)
    escala_valor = getattr(record, 'escala_valor', None)
    if escala_valor and escala_valor.valor_numerico is not None:
        return _decimal_or_none(escala_valor.valor_numerico)
    return _catalog_row_primary_numeric(getattr(record, 'catalogo_fila', None))


def _auto_matrix_dimension_queryset(estrategia):
    return (
        models.EstrategiaDimension.objects.filter(
            estrategia=estrategia,
            activo=True,
        )
        .select_related('dimension')
        .prefetch_related(
            'catalogo__columnas',
            'catalogo__filas__celdas__columna',
            'escalas_valor',
        )
        .order_by('orden', 'id')
    )


def _auto_matrix_source_ref(estrategia_dimension):
    return {
        'estrategia_dimension_id': estrategia_dimension.pk,
        'dimension_id': estrategia_dimension.dimension_id,
        'campo': _calculated_dimension_source_key(estrategia_dimension),
        'nombre': estrategia_dimension.dimension.nombre,
    }


def _auto_matrix_source_index(estrategia_dimensions):
    index = {}
    for estrategia_dimension in estrategia_dimensions:
        dimension = estrategia_dimension.dimension
        campo = _calculated_dimension_source_key(estrategia_dimension)
        keys = [
            f'ed:{estrategia_dimension.pk}',
            f'estrategia_dimension:{estrategia_dimension.pk}',
            str(dimension.pk),
            f'dim:{dimension.pk}',
            dimension.nombre,
            _calc_slug(dimension.nombre),
            campo,
            _calc_slug(campo),
        ]
        for key in keys:
            if key:
                index[str(key)] = estrategia_dimension
    return index


def _auto_matrix_catalog_max(estrategia_dimension):
    escala_values = [
        _decimal_or_none(item.valor_numerico)
        for item in estrategia_dimension.escalas_valor.all()
        if item.valor_numerico is not None
    ]
    escala_values = [value for value in escala_values if value is not None]
    if escala_values:
        return max(escala_values)

    try:
        catalogo = estrategia_dimension.catalogo
    except models.DimensionCatalogo.DoesNotExist:
        catalogo = None
    if not catalogo:
        return None

    values = []
    for row in catalogo.filas.all():
        value = _catalog_row_primary_numeric(row)
        if value is not None:
            values.append(value)
    return max(values) if values else None


def _auto_matrix_operation(operation):
    operation = str(operation or '').strip().lower()
    aliases = {
        'sum': 'suma',
        'add': 'suma',
        'producto': 'multiplicacion',
        'multiplicar': 'multiplicacion',
        'multiply': 'multiplicacion',
        'multiplication': 'multiplicacion',
        'max': 'maximo',
        'maximum': 'maximo',
        'min': 'minimo',
        'minimum': 'minimo',
        'divide': 'division',
        'division': 'division',
        'división': 'division',
    }
    return aliases.get(operation, operation)


def _auto_matrix_weighted_values(operands, resolved_values):
    if not operands or len(operands) != len(resolved_values):
        return None
    weights = []
    for operand in operands:
        weight = _calculated_operand_weight(operand)
        if weight is not None and weight > 1 and weight <= 100:
            weight = weight / Decimal('100')
        weights.append(weight)
    if all(weight is None for weight in weights):
        equal_weight = Decimal('1') / Decimal(len(weights))
        weights = [equal_weight] * len(weights)
    elif any(weight is None for weight in weights):
        assigned = sum((weight for weight in weights if weight is not None), Decimal('0'))
        missing_count = sum(1 for weight in weights if weight is None)
        remaining = Decimal('1') - assigned
        if remaining < 0:
            return None
        missing_weight = remaining / Decimal(missing_count)
        weights = [missing_weight if weight is None else weight for weight in weights]
    if any(weight < 0 or weight > 1 for weight in weights):
        return None
    if abs(sum(weights, Decimal('0')) - Decimal('1')) > Decimal('0.0001'):
        return None
    return [value * weight for value, weight in zip(resolved_values, weights)]


def _auto_matrix_step_max(operation, operands, source_index, seen, previous_result=None, weighted=False):
    operation = _auto_matrix_operation(operation)
    values = []
    for operand in operands:
        value = _auto_matrix_operand_max(operand, source_index, seen, previous_result=previous_result)
        if value is None:
            return None
        values.append(value)
    if weighted and operation == 'suma':
        values = _auto_matrix_weighted_values(operands, values)
        if values is None:
            return None
    return _calculated_operation_result(operation, values)


def _auto_matrix_operand_max(operand, source_index, seen, previous_result=None):
    if isinstance(operand, dict):
        if operand.get('resultado') is True or operand.get('tipo') == 'resultado':
            return previous_result
        for key in ['valor_numerico', 'valor', 'constante', 'constant', 'numero', 'number']:
            value = _decimal_or_none(operand.get(key))
            if value is not None:
                return value
        nested_operation = (
            operand.get('operacion')
            or operand.get('tipo_calculo')
            or operand.get('operation')
        )
        nested_operands = operand.get('operandos') or operand.get('campos') or operand.get('sources')
        if nested_operation and isinstance(nested_operands, list):
            return _auto_matrix_step_max(
                nested_operation,
                nested_operands,
                source_index,
                seen,
                previous_result=previous_result,
                weighted=operand.get('modo') == 'ponderado' or operand.get('ponderado') is True,
            )
    else:
        raw = str(operand)
        if raw in {'$resultado', '__resultado__', 'resultado_anterior'}:
            return previous_result
        numeric = _decimal_or_none(raw)
        if numeric is not None:
            return numeric

    for candidate in _calculated_dimension_source_candidates(operand):
        estrategia_dimension = source_index.get(str(candidate)) or source_index.get(_calc_slug(candidate))
        if estrategia_dimension:
            return _auto_matrix_theoretical_max(estrategia_dimension, source_index, seen)
    return None


def _auto_matrix_calculation_steps(tipo_calculo, config_calculo):
    config_calculo = _json_loads_safe(config_calculo, {}) if isinstance(config_calculo, str) else config_calculo
    config_calculo = config_calculo if isinstance(config_calculo, dict) else {}
    fallback_operation = _auto_matrix_operation(tipo_calculo) or 'suma'
    raw_steps = config_calculo.get('pasos') or config_calculo.get('steps') or []
    if isinstance(raw_steps, list) and raw_steps:
        steps = []
        for raw_step in raw_steps:
            if not isinstance(raw_step, dict):
                continue
            operation = _auto_matrix_operation(
                raw_step.get('operacion')
                or raw_step.get('tipo_calculo')
                or raw_step.get('operation')
                or fallback_operation
            )
            operands = raw_step.get('operandos') or raw_step.get('campos') or raw_step.get('sources') or []
            if operation and isinstance(operands, list) and operands:
                steps.append({
                    'operacion': operation,
                    'operandos': operands,
                    'modo': raw_step.get('modo') or ('ponderado' if raw_step.get('ponderado') is True else ''),
                })
        if steps:
            return steps
    operands = config_calculo.get('operandos') or config_calculo.get('campos') or config_calculo.get('sources') or []
    if isinstance(operands, list) and operands:
        return [{'operacion': fallback_operation, 'operandos': operands}]
    return []


def _auto_matrix_theoretical_max(estrategia_dimension, source_index, seen=None):
    if not estrategia_dimension:
        return None
    if seen is None:
        seen = set()
    if estrategia_dimension.pk in seen:
        return None
    seen = set(seen)
    seen.add(estrategia_dimension.pk)

    dimension = estrategia_dimension.dimension
    tipo_calculo = (dimension.tipo_calculo or '').strip()
    config_calculo = _json_loads_safe(dimension.config_calculo, {})
    steps = _auto_matrix_calculation_steps(tipo_calculo, config_calculo)
    if not steps:
        return _auto_matrix_catalog_max(estrategia_dimension)

    result = None
    for step in steps:
        operands = step.get('operandos') or []
        result = _auto_matrix_step_max(
            step.get('operacion'),
            operands,
            source_index,
            seen,
            previous_result=result,
            weighted=step.get('modo') == 'ponderado',
        )
        if result is None:
            return None
    return result


def _auto_matrix_source_options(estrategia):
    _ensure_auto_impact_total_dimension(estrategia)
    estrategia_dimensions = list(_auto_matrix_dimension_queryset(estrategia))
    source_index = _auto_matrix_source_index(estrategia_dimensions)
    options = []
    for estrategia_dimension in estrategia_dimensions:
        if _is_generated_matrix_axis_dimension(estrategia_dimension):
            continue
        maximum = _auto_matrix_theoretical_max(estrategia_dimension, source_index)
        catalogo = _catalog_for_strategy_dimension(estrategia_dimension)
        options.append({
            'id': estrategia_dimension.pk,
            'label': (catalogo.nombre if catalogo else '') or estrategia_dimension.dimension.nombre,
            'campo': (catalogo.campo if catalogo else '') or _safe_slug(estrategia_dimension.dimension.nombre),
            'tipo_calculo': estrategia_dimension.dimension.tipo_calculo or '',
            'maximo': maximum,
            'maximo_label': _auto_matrix_decimal_label(maximum) if maximum is not None else 'requiere máximo manual',
        })
    return options, source_index


def _auto_matrix_decimal_label(value):
    value = _decimal_or_none(value)
    if value is None:
        return ''
    return format(value.quantize(Decimal('0.01')), 'f').rstrip('0').rstrip('.') or '0'


def _auto_matrix_range_label(range_item):
    start = _auto_matrix_decimal_label(range_item.get('desde'))
    end = _auto_matrix_decimal_label(range_item.get('hasta'))
    return f'>{start} - {end}' if range_item.get('desde_exclusivo') else f'{start} - {end}'


def _auto_matrix_linear_ranges(maximum, count):
    maximum = _decimal_or_none(maximum)
    count = max(1, int(count or 1))
    if maximum is None or maximum <= 0:
        return []
    step = maximum / Decimal(count)
    ranges = []
    start = Decimal('0')
    for index in range(1, count + 1):
        end = maximum if index == count else (step * Decimal(index)).quantize(Decimal('0.01'))
        ranges.append({
            'nivel': index,
            'desde': _auto_matrix_decimal_label(start),
            'hasta': _auto_matrix_decimal_label(end),
            'desde_exclusivo': index > 1,
            'label': _auto_matrix_range_label({
                'desde': start,
                'hasta': end,
                'desde_exclusivo': index > 1,
            }),
        })
        start = end
    return ranges


def _auto_matrix_level_defs(ranges, prefix):
    defs = []
    for range_item in ranges:
        idx = int(range_item['nivel'])
        defs.append({
            'idx': idx,
            'nombre': f'{prefix}{idx}',
            'valor': range_item['hasta'],
            'descripcion': range_item['label'],
        })
    return defs


def _auto_matrix_cell_payload(prob_defs, impact_defs, legend_items=None):
    result_values = _matrix_result_values(prob_defs, impact_defs)
    min_value, max_value = (min(result_values), max(result_values)) if result_values else (Decimal('0'), Decimal('0'))
    legend_items = legend_items or _default_legend_for_bounds(min_value, max_value)
    payload = {}
    parsed_legend = []
    for item in legend_items:
        parts = str(item.get('range') or '').split('-', 1)
        start = _decimal_or_none(parts[0]) if parts else None
        end = _decimal_or_none(parts[1]) if len(parts) > 1 else None
        if start is not None and end is not None:
            parsed_legend.append((start, end, item.get('name') or '', item.get('color') or '#2a2a3a'))
    for prob_def in prob_defs:
        prob_value = _decimal_or_none(prob_def.get('valor')) or Decimal('0')
        for impact_def in impact_defs:
            impact_value = _decimal_or_none(impact_def.get('valor')) or Decimal('0')
            result = (prob_value * impact_value).quantize(Decimal('0.01'))
            classification = ''
            color = '#2a2a3a'
            for start, end, name, item_color in parsed_legend:
                if start <= result <= end:
                    classification = name
                    color = item_color
                    break
            payload[(prob_def['idx'], impact_def['idx'])] = {
                'resultado_num': result,
                'clasificacion': classification,
                'color': color,
                'calcular': True,
            }
    return payload, legend_items


def _auto_matrix_preview_payload(
    source_x,
    source_y,
    max_x,
    max_y,
    levels_x,
    levels_y,
    legend_items=None,
    reverse_x=False,
    reverse_y=True,
):
    ranges_x = _auto_matrix_linear_ranges(max_x, levels_x)
    ranges_y = _auto_matrix_linear_ranges(max_y, levels_y)
    prob_defs = _auto_matrix_level_defs(ranges_x, 'X')
    impact_defs = _auto_matrix_level_defs(ranges_y, 'Y')
    cell_payload, legend_items = _auto_matrix_cell_payload(prob_defs, impact_defs, legend_items=legend_items)
    grid_rows = []
    display_x_defs = list(reversed(prob_defs)) if reverse_x else list(prob_defs)
    display_y_defs = list(reversed(impact_defs)) if reverse_y else list(impact_defs)
    for impact_def in display_y_defs:
        cells = []
        for prob_def in display_x_defs:
            payload = cell_payload.get((prob_def['idx'], impact_def['idx']), {})
            cells.append({
                'x': prob_def,
                'y': impact_def,
                'resultado_num': payload.get('resultado_num'),
                'clasificacion': payload.get('clasificacion'),
                'color': payload.get('color') or '#ffffff',
            })
        grid_rows.append({
            'level': impact_def,
            'cells': cells,
        })
    return {
        'source_x': source_x,
        'source_y': source_y,
        'max_x': max_x,
        'max_y': max_y,
        'levels_x': levels_x,
        'levels_y': levels_y,
        'ranges_x': ranges_x,
        'ranges_y': ranges_y,
        'prob_defs': prob_defs,
        'impact_defs': impact_defs,
        'display_x_defs': display_x_defs,
        'reverse_x': bool(reverse_x),
        'reverse_y': bool(reverse_y),
        'cell_payload': cell_payload,
        'grid_rows': grid_rows,
        'legend_items': legend_items,
    }


def _auto_matrix_generation_config(preview):
    return {
        'modo': models.MatrizRiesgo.MODO_AUTOMATICA_MAXIMO_TEORICO,
        'distribucion': 'lineal',
        'fuente_eje_x': _auto_matrix_source_ref(preview['source_x']),
        'fuente_eje_y': _auto_matrix_source_ref(preview['source_y']),
        'maximo_teorico_eje_x': _auto_matrix_decimal_label(preview['max_x']),
        'maximo_teorico_eje_y': _auto_matrix_decimal_label(preview['max_y']),
        'niveles_eje_x': preview['levels_x'],
        'niveles_eje_y': preview['levels_y'],
        'invertir_eje_x': bool(preview.get('reverse_x')),
        'invertir_eje_y': bool(preview.get('reverse_y')),
        'rangos_eje_x': preview['ranges_x'],
        'rangos_eje_y': preview['ranges_y'],
        'fecha_generacion': timezone.now().isoformat(),
    }


@transaction.atomic
def _create_auto_matrix(nombre, estrategia, preview):
    legend_payload = _matrix_legend_payload(
        preview['legend_items'],
        models.MatrizRiesgo.RESOLUCION_EXACTA,
        [],
    )
    legend_payload['generacion'] = _auto_matrix_generation_config(preview)
    matriz = models.MatrizRiesgo.objects.create(
        nombre=nombre,
        fecha_creado=timezone.localdate(),
        estrategia=estrategia,
        eje_horizontal='probabilidad',
        dimension_probabilidad=preview['source_x'],
        dimension_impacto=preview['source_y'],
        leyenda_json=json.dumps(_json_safe(legend_payload), ensure_ascii=False),
    )
    _persist_matrix_grid(
        matriz,
        list(reversed(preview['prob_defs'])) if preview.get('reverse_x') else preview['prob_defs'],
        list(reversed(preview['impact_defs'])) if preview.get('reverse_y') else preview['impact_defs'],
        preview['cell_payload'],
    )
    return matriz


def _calculated_dimensions_for_backfill(estrategia, process_values):
    return [
        estrategia_dimension
        for estrategia_dimension in models.EstrategiaDimension.objects.filter(
            estrategia=estrategia,
            activo=True,
            proceso_uso__in=process_values,
            dimension__tipo_calculo__isnull=False,
        )
        .exclude(dimension__tipo_calculo='')
        .select_related('dimension')
        .order_by('orden', 'id')
        if not _hide_from_dimension_tables_editor(estrategia_dimension)
    ]


def _upsert_aca_calculated_record(criticidad, estrategia_dimension, value):
    existing = list(models.CriticidadDimension.objects.filter(
        criticidad=criticidad,
        estrategia_dimension=estrategia_dimension,
    ).order_by('id'))
    defaults = {
        'dimension': estrategia_dimension.dimension,
        'valor_numerico': value,
        'valor_texto': '' if value is None else str(value),
        'valor_booleano': None,
        'valor_secundario': None,
        'catalogo_fila': None,
        'escala_valor': None,
        'escala_unificada': None,
        'comentario': '',
    }
    if existing:
        record = existing[0]
        for field, field_value in defaults.items():
            setattr(record, field, field_value)
        record.save()
        if len(existing) > 1:
            models.CriticidadDimension.objects.filter(pk__in=[item.pk for item in existing[1:]]).delete()
        return False
    models.CriticidadDimension.objects.create(
        criticidad=criticidad,
        estrategia_dimension=estrategia_dimension,
        **defaults,
    )
    return True


def _backfill_aca_calculated_dimensions(estrategia, calculated_dimensions):
    if not calculated_dimensions:
        return {'created': 0, 'updated': 0}
    created = 0
    updated = 0
    criticidades = (
        models.Criticidad.objects.filter(aca_carga__estrategia=estrategia)
        .prefetch_related(
            'dimensiones__estrategia_dimension__dimension',
            'dimensiones__catalogo_fila__celdas__columna',
            'dimensiones__escala_valor',
        )
        .order_by('id')
    )
    calculated_ids = {item.pk for item in calculated_dimensions}
    for criticidad in criticidades:
        source_values = {}
        for record in criticidad.dimensiones.all():
            estrategia_dimension = record.estrategia_dimension
            if not estrategia_dimension or estrategia_dimension.pk in calculated_ids:
                continue
            value = _record_numeric_value(record)
            if value is not None:
                _remember_calculated_source_value(source_values, estrategia_dimension, value)
        for estrategia_dimension in calculated_dimensions:
            value = _evaluate_calculated_dimension(
                estrategia_dimension.dimension.tipo_calculo,
                estrategia_dimension.dimension.config_calculo,
                source_values,
            )
            if value is None:
                continue
            was_created = _upsert_aca_calculated_record(criticidad, estrategia_dimension, value)
            created += 1 if was_created else 0
            updated += 0 if was_created else 1
            _remember_calculated_source_value(source_values, estrategia_dimension, value)
    return {'created': created, 'updated': updated}


def _upsert_fmeca_calculated_record(fmea, estrategia_dimension, value):
    numeric_value = int(value) if value is not None else None
    obj, created = models.EvaluacionFMEA.objects.update_or_create(
        fmea=fmea,
        estrategia_dimension=estrategia_dimension,
        defaults={
            'valor_numerico': numeric_value,
            'valor_texto': '' if numeric_value is None else str(numeric_value),
            'catalogo_fila': None,
            'escala_valor': None,
        },
    )
    return created


def _backfill_fmeca_calculated_dimensions(estrategia, calculated_dimensions):
    if not calculated_dimensions:
        return {'created': 0, 'updated': 0}
    created = 0
    updated = 0
    fmeas = (
        models.FMEA_FMECA.objects.filter(rcm__carga__estrategia=estrategia)
        .prefetch_related(
            'evaluaciones__estrategia_dimension__dimension',
            'evaluaciones__catalogo_fila__celdas__columna',
            'evaluaciones__escala_valor',
        )
        .order_by('id')
    )
    calculated_ids = {item.pk for item in calculated_dimensions}
    for fmea in fmeas:
        source_values = {}
        for record in fmea.evaluaciones.all():
            estrategia_dimension = record.estrategia_dimension
            if not estrategia_dimension or estrategia_dimension.pk in calculated_ids:
                continue
            value = _record_numeric_value(record)
            if value is not None:
                _remember_calculated_source_value(source_values, estrategia_dimension, value)
        for estrategia_dimension in calculated_dimensions:
            value = _evaluate_calculated_dimension(
                estrategia_dimension.dimension.tipo_calculo,
                estrategia_dimension.dimension.config_calculo,
                source_values,
            )
            if value is None:
                continue
            was_created = _upsert_fmeca_calculated_record(fmea, estrategia_dimension, value)
            created += 1 if was_created else 0
            updated += 0 if was_created else 1
            _remember_calculated_source_value(source_values, estrategia_dimension, value)
    return {'created': created, 'updated': updated}


def _backfill_calculated_dimension_records(estrategia):
    aca_dimensions = _calculated_dimensions_for_backfill(
        estrategia,
        [models.EstrategiaDimension.PROCESO_ACA, models.EstrategiaDimension.PROCESO_AMBOS],
    )
    fmeca_dimensions = _calculated_dimensions_for_backfill(
        estrategia,
        [models.EstrategiaDimension.PROCESO_FMECA, models.EstrategiaDimension.PROCESO_AMBOS],
    )
    aca_result = _backfill_aca_calculated_dimensions(estrategia, aca_dimensions)
    fmeca_result = _backfill_fmeca_calculated_dimensions(estrategia, fmeca_dimensions)
    return {
        'aca_created': aca_result['created'],
        'aca_updated': aca_result['updated'],
        'fmeca_created': fmeca_result['created'],
        'fmeca_updated': fmeca_result['updated'],
    }


def _catalog_validation_decimal(raw):
    if raw in (None, ''):
        return None, None
    try:
        value = Decimal(str(raw).strip().replace(',', '.'))
    except (InvalidOperation, ValueError, TypeError):
        return None, 'no es un numero valido'
    if value.is_nan() or value.is_infinite():
        return None, 'no es un numero valido'
    if abs(value) > CATALOG_NUMBER_MAX:
        return None, f'excede el maximo permitido ({CATALOG_NUMBER_MAX})'
    normalized = value.normalize()
    decimal_places = max(0, -normalized.as_tuple().exponent)
    if decimal_places > CATALOG_NUMBER_DECIMAL_PLACES:
        return None, f'tiene mas de {CATALOG_NUMBER_DECIMAL_PLACES} decimales'
    return value, None


def _catalog_value_for_keys(values, keys):
    for key in keys:
        value = values.get(key) if isinstance(values, dict) else None
        if value not in (None, ''):
            return value
    return ''


def _validate_strategy_catalog_payload(payload):
    errors = []
    warnings = []
    payload = payload if isinstance(payload, list) else []
    lower_keys = ['limite_inferior', 'desde', 'min', 'minimo', 'mí­nimo']
    upper_keys = ['limite_superior', 'hasta', 'max', 'maximo', 'máximo']

    for item_index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            continue
        nombre = str(item.get('nombre') or f'Dimension {item_index}').strip()
        if item.get('tipo_calculo') or item.get('tipo') == 'numerico_libre':
            continue
        columnas = item.get('columnas') if isinstance(item.get('columnas'), list) else []
        filas = item.get('filas') if isinstance(item.get('filas'), list) else []
        numeric_columns = [
            col for col in columnas
            if isinstance(col, dict) and str(col.get('tipo_dato') or '').strip() == 'numero'
        ]
        range_bounds = []

        for row_index, row in enumerate(filas, start=1):
            values = row.get('valores') if isinstance(row, dict) and isinstance(row.get('valores'), dict) else {}
            for col in numeric_columns:
                key = str(col.get('clave_interna') or '').strip()
                raw = values.get(key)
                if raw in (None, ''):
                    continue
                _value, error = _catalog_validation_decimal(raw)
                if error:
                    label = str(col.get('nombre_columna') or key or 'valor').strip()
                    errors.append(f'{nombre}, fila {row_index}, columna "{label}": {error}.')

            lower_raw = _catalog_value_for_keys(values, lower_keys)
            upper_raw = _catalog_value_for_keys(values, upper_keys)
            lower, lower_error = _catalog_validation_decimal(lower_raw)
            upper, upper_error = _catalog_validation_decimal(upper_raw)
            if lower_error or upper_error:
                continue
            if lower is not None and upper is not None:
                if lower > upper:
                    errors.append(f'{nombre}, fila {row_index}: el limite Desde ({lower}) no puede ser mayor que Hasta ({upper}).')
                else:
                    range_bounds.append({'row': row_index, 'lower': lower, 'upper': upper})

        if str(item.get('tipo') or '') == 'rangos' and range_bounds:
            range_bounds.sort(key=lambda data: (data['lower'], data['upper'], data['row']))
            previous = None
            for current_bound in range_bounds:
                if previous and current_bound['lower'] < previous['upper']:
                    errors.append(
                        f'{nombre}: las filas {previous["row"]} y {current_bound["row"]} se solapan '
                        f'({previous["lower"]}-{previous["upper"]} con {current_bound["lower"]}-{current_bound["upper"]}).'
                    )
                elif previous and current_bound['lower'] > previous['upper']:
                    warnings.append(
                        f'{nombre}: hay un salto entre las filas {previous["row"]} y {current_bound["row"]} '
                        f'({previous["upper"]} a {current_bound["lower"]}). Si tus valores pueden ser decimales, ese tramo no resolvera ninguna fila.'
                    )
                previous = current_bound
    return errors, warnings


@transaction.atomic
def _save_strategy_catalogs(estrategia, payload):
    payload = payload if isinstance(payload, list) else []
    auto_impact_ed = _ensure_auto_impact_total_dimension(estrategia)
    existing = {
        str(obj.pk): obj
        for obj in models.DimensionCatalogo.objects.filter(
            estrategia_dimension__estrategia=estrategia,
            estrategia_dimension__activo=True,
        ).select_related(
            'estrategia_dimension',
            'estrategia_dimension__dimension',
        )
    }
    existing_eds = {
        str(obj.pk): obj
        for obj in models.EstrategiaDimension.objects.filter(estrategia=estrategia).select_related('dimension')
    }
    keep_ids = set()
    tipos_calculo_validos = dict(models.Dimension.TIPO_CALCULO_CHOICES)
    procesos_validos = dict(models.EstrategiaDimension.PROCESO_USO_CHOICES)
    tipos_catalogo_validos = {'opciones', 'rangos', 'numerico_libre', 'calculado'}
    source_index = {}

    def _index_source_ref(ref, *values):
        for value in values:
            if value in (None, ''):
                continue
            raw = str(value).strip()
            if not raw:
                continue
            source_index[raw] = ref
            source_index[_calc_slug(raw)] = ref

    for ed in existing_eds.values():
        dimension = ed.dimension
        try:
            catalogo = getattr(ed, 'catalogo', None)
        except Exception:
            catalogo = None
        ref = {
            'estrategia_dimension_id': ed.pk,
            'dimension_id': dimension.pk,
            'campo': getattr(catalogo, 'campo', '') or _safe_slug(dimension.nombre),
            'nombre': getattr(catalogo, 'nombre', '') or dimension.nombre,
        }
        _index_source_ref(
            ref,
            f'ed:{ed.pk}',
            f'estrategia_dimension:{ed.pk}',
            f'dim:{dimension.pk}',
            str(dimension.pk),
            ref['campo'],
            ref['nombre'],
            dimension.nombre,
        )

    def _payload_item_is_auto_impact_total(item, catalogo=None, estrategia_dimension=None):
        if item.get('auto_impact_total') is True or item.get('protegida') is True:
            return True
        if str(item.get('campo') or '').strip() == AUTO_IMPACT_TOTAL_FIELD:
            return True
        if str(item.get('nombre') or '').strip().lower() == AUTO_IMPACT_TOTAL_NAME.lower():
            return True
        if catalogo and catalogo.campo == AUTO_IMPACT_TOTAL_FIELD:
            return True
        if estrategia_dimension and _is_auto_impact_total_dimension(estrategia_dimension):
            return True
        return False

    def _source_ref_for_value(value):
        if value in (None, ''):
            return None
        raw = str(value).strip()
        if not raw:
            return None
        if raw.startswith('campo:'):
            raw = raw.split(':', 1)[1].strip()
        if raw.startswith('ed:') and raw[3:] in existing_eds:
            ed = existing_eds[raw[3:]]
            return dict(source_index.get(raw) or {
                'estrategia_dimension_id': ed.pk,
                'dimension_id': ed.dimension_id,
                'campo': _safe_slug(ed.dimension.nombre),
                'nombre': ed.dimension.nombre,
            })
        if raw.startswith('estrategia_dimension:') and raw.split(':', 1)[1] in existing_eds:
            ed = existing_eds[raw.split(':', 1)[1]]
            return dict(source_index.get(f'ed:{ed.pk}') or {
                'estrategia_dimension_id': ed.pk,
                'dimension_id': ed.dimension_id,
                'campo': _safe_slug(ed.dimension.nombre),
                'nombre': ed.dimension.nombre,
            })
        ref = source_index.get(raw) or source_index.get(_calc_slug(raw))
        return dict(ref) if ref else None

    def _normalize_calculation_config(config_raw, tipo_calculo):
        config_raw = config_raw if isinstance(config_raw, dict) else {}
        valid_ops = set(tipos_calculo_validos.keys()) - {''}

        def _clean_weight(value):
            if value in (None, ''):
                return None
            try:
                weight = Decimal(str(value).replace(',', '.'))
            except (InvalidOperation, TypeError, ValueError):
                return None
            if weight < 0 or weight > 1:
                return None
            return format(weight.normalize(), 'f')

        def _clean_source_ref(value):
            if isinstance(value, dict):
                cleaned = {}
                for key in ['estrategia_dimension_id', 'dimension_id', 'campo', 'nombre', 'source']:
                    raw = value.get(key)
                    if raw not in (None, ''):
                        cleaned[key] = raw
                weight = _clean_weight(value.get('peso', value.get('ponderador', value.get('weight'))))
                if weight is not None:
                    cleaned['peso'] = weight
                return cleaned or None
            if value not in (None, ''):
                return _source_ref_for_value(value) or str(value)
            return None

        def _clean_operandos(value):
            if not isinstance(value, list):
                return []
            cleaned = []
            for operand in value:
                if isinstance(operand, dict) and (operand.get('resultado') is True or operand.get('tipo') == 'resultado'):
                    result_ref = {'resultado': True}
                    weight = _clean_weight(operand.get('peso', operand.get('ponderador', operand.get('weight'))))
                    if weight is not None:
                        result_ref['peso'] = weight
                    cleaned.append(result_ref)
                    continue
                if not isinstance(operand, dict) and str(operand) in {'$resultado', '__resultado__', 'resultado_anterior'}:
                    cleaned.append('$resultado')
                    continue
                source_ref = _clean_source_ref(operand)
                if source_ref:
                    cleaned.append(source_ref)
            return cleaned

        raw_steps = config_raw.get('pasos') or config_raw.get('steps') or []
        steps = []
        if isinstance(raw_steps, list):
            for raw_step in raw_steps:
                if not isinstance(raw_step, dict):
                    continue
                operacion = str(raw_step.get('operacion') or raw_step.get('tipo_calculo') or raw_step.get('operation') or '').strip()
                if operacion not in valid_ops:
                    operacion = tipo_calculo
                operandos = _clean_operandos(
                    raw_step.get('operandos') or raw_step.get('campos') or raw_step.get('sources') or []
                )
                if operacion and operandos:
                    step = {'operacion': operacion, 'operandos': operandos}
                    weighted = raw_step.get('modo') == 'ponderado' or raw_step.get('ponderado') is True
                    if weighted and operacion == 'suma':
                        step['modo'] = 'ponderado'
                        step['ponderacion_manual'] = raw_step.get('ponderacion_manual') is True
                        if not any(isinstance(operand, dict) and operand.get('peso') not in (None, '') for operand in operandos):
                            equal_weight = Decimal('1') / Decimal(len(operandos))
                            assigned = Decimal('0')
                            weighted_operands = []
                            for operand_index, operand in enumerate(operandos):
                                weight = Decimal('1') - assigned if operand_index == len(operandos) - 1 else equal_weight
                                assigned += weight
                                if isinstance(operand, dict):
                                    weighted_operand = dict(operand)
                                elif str(operand) in {'$resultado', '__resultado__', 'resultado_anterior'}:
                                    weighted_operand = {'resultado': True}
                                else:
                                    weighted_operand = {'source': str(operand)}
                                weighted_operand['peso'] = format(weight.normalize(), 'f')
                                weighted_operands.append(weighted_operand)
                            step['operandos'] = weighted_operands
                    steps.append(step)

        if steps:
            return {
                'version': 2 if any(step.get('modo') == 'ponderado' for step in steps) else 1,
                'pasos': steps,
                'operandos': steps[0]['operandos'],
            }

        operandos = _clean_operandos(
            config_raw.get('operandos') or config_raw.get('campos') or config_raw.get('sources') or []
        )
        return {'operandos': operandos}

    def _dependency_payload(config_raw):
        config_raw = config_raw if isinstance(config_raw, dict) else {}
        for key in ['dependencia', 'depende_de', 'source', 'fuente', 'campo_fuente', 'dimension_fuente']:
            value = config_raw.get(key)
            if value in (None, ''):
                continue
            if isinstance(value, dict):
                cleaned = {}
                for ref_key in ['estrategia_dimension_id', 'dimension_id', 'campo', 'nombre', 'source']:
                    raw = value.get(ref_key)
                    if raw not in (None, ''):
                        cleaned[ref_key] = raw
                return cleaned or ''
            return _source_ref_for_value(value) or str(value).strip()
        return ''

    def _clean_catalogo(catalogo):
        models.CriticidadDimension.objects.filter(catalogo_fila__catalogo=catalogo).update(catalogo_fila=None)
        models.EvaluacionFMEA.objects.filter(catalogo_fila__catalogo=catalogo).update(catalogo_fila=None)
        models.DimensionCatalogoCelda.objects.filter(fila__catalogo=catalogo).delete()
        models.DimensionCatalogoFila.objects.filter(catalogo=catalogo).delete()
        models.DimensionCatalogoColumna.objects.filter(catalogo=catalogo).delete()

    for idx, item in enumerate(payload, start=1):
        cat_id = str(item.get('id') or '').strip()
        nombre = str(item.get('nombre') or '').strip() or f'Dimension {idx}'
        campo = str(item.get('campo') or '').strip()
        tipo = str(item.get('tipo') or 'opciones').strip()
        if tipo not in tipos_catalogo_validos:
            tipo = 'opciones'
        descripcion = str(item.get('descripcion') or '').strip()
        tipo_funcional = str(item.get('tipo_funcional') or 'atributo').strip()
        tipo_dato = str(item.get('tipo_dato') or 'tabla').strip()
        tipo_calculo = str(item.get('tipo_calculo') or '').strip()
        if tipo == 'calculado' and not tipo_calculo:
            tipo_calculo = 'suma'
        if tipo_calculo not in tipos_calculo_validos:
            tipo_calculo = ''
        proceso_uso = _normalize_process_usage(item.get('proceso_uso'))
        if proceso_uso not in procesos_validos:
            proceso_uso = models.EstrategiaDimension.PROCESO_ACA
        es_calculada = bool(tipo_calculo)

        config_calculo_raw = item.get('config_calculo') if isinstance(item.get('config_calculo'), dict) else None
        if es_calculada:
            config_calculo_raw = _normalize_calculation_config(config_calculo_raw, tipo_calculo)
            config_calculo = json.dumps(config_calculo_raw, ensure_ascii=False)
            columnas = []
            filas = []
            if tipo_funcional not in dict(models.Dimension.TIPO_FUNCIONAL_CHOICES):
                tipo_funcional = 'resultado'
            if tipo_dato not in dict(models.Dimension.TIPO_DATO_CHOICES):
                tipo_dato = 'numerico'
        elif tipo == 'numerico_libre':
            config_calculo = None
            columnas = []
            filas = []
            tipo_dato = 'numerico'
        else:
            dependencia = ''
            if tipo in {'rangos', 'opciones'} and isinstance(config_calculo_raw, dict):
                dependencia = _dependency_payload(config_calculo_raw)
            config_calculo = json.dumps({'dependencia': dependencia}, ensure_ascii=False) if dependencia else None
            columnas = item.get('columnas') if isinstance(item.get('columnas'), list) else []
            columnas = [
                col for col in columnas
                if isinstance(col, dict) and str(col.get('clave_interna') or '').strip() != 'valor_secundario'
            ]
            filas = item.get('filas') if isinstance(item.get('filas'), list) else []
            for row in filas:
                values = row.get('valores') if isinstance(row, dict) and isinstance(row.get('valores'), dict) else None
                if values is not None:
                    values.pop('valor_secundario', None)
            if not columnas:
                columnas = _default_columns_for_type(tipo)

        obligatorio = item.get('obligatorio', True) is not False
        considerar_avance_aca = item.get('considerar_avance_aca', True) is not False
        visible_en_listado_aca = item.get('visible_en_listado_aca', True) is not False
        considerar_avance_fmeca = item.get('considerar_avance_fmeca', True) is not False
        visible_en_listado_fmeca = item.get('visible_en_listado_fmeca', True) is not False
        if proceso_uso == models.EstrategiaDimension.PROCESO_ACA:
            considerar_avance_fmeca = False
            visible_en_listado_fmeca = False
        if proceso_uso == models.EstrategiaDimension.PROCESO_FMECA:
            considerar_avance_aca = False
            visible_en_listado_aca = False

        auto_impact_item = _payload_item_is_auto_impact_total(item, existing.get(cat_id))
        if auto_impact_item:
            nombre = AUTO_IMPACT_TOTAL_NAME
            campo = AUTO_IMPACT_TOTAL_FIELD
            tipo = 'calculado'
            descripcion = AUTO_IMPACT_TOTAL_DESCRIPTION
            tipo_funcional = 'resultado'
            tipo_dato = 'numerico'
            tipo_calculo = 'suma'
            proceso_uso = models.EstrategiaDimension.PROCESO_ACA
            obligatorio = True
            considerar_avance_aca = False
            visible_en_listado_aca = True
            considerar_avance_fmeca = False
            visible_en_listado_fmeca = False
            es_calculada = True
            config_calculo_raw = item.get('config_calculo') if isinstance(item.get('config_calculo'), dict) else None
            if not _calculation_config_has_sources(config_calculo_raw):
                config_calculo_raw = _json_loads_safe(auto_impact_ed.dimension.config_calculo, {})
            if not _calculation_config_has_sources(config_calculo_raw):
                config_calculo_raw = _impact_total_calculation_config(
                    estrategia,
                    exclude_ed_id=auto_impact_ed.pk,
                )
            config_calculo_raw = _normalize_calculation_config(config_calculo_raw, tipo_calculo)
            config_calculo_raw['auto_impact_total'] = True
            config_calculo = json.dumps(config_calculo_raw, ensure_ascii=False)
            columnas = []
            filas = []

        if cat_id and cat_id in existing:
            catalogo = existing[cat_id]
            estrategia_dimension = catalogo.estrategia_dimension
            dimension = estrategia_dimension.dimension
            keep_ids.add(catalogo.pk)
        elif auto_impact_item:
            estrategia_dimension = auto_impact_ed
            dimension = estrategia_dimension.dimension
            catalogo = estrategia_dimension.catalogo
            keep_ids.add(catalogo.pk)
        elif cat_id.startswith('ed_') and cat_id[3:] in existing_eds:
            estrategia_dimension = existing_eds[cat_id[3:]]
            dimension = estrategia_dimension.dimension
            catalogo, _ = models.DimensionCatalogo.objects.get_or_create(
                estrategia_dimension=estrategia_dimension,
                defaults={
                    'nombre': nombre,
                    'campo': campo or _safe_slug(nombre),
                    'tipo': tipo,
                    'descripcion': descripcion,
                    'activa': True,
                },
            )
            keep_ids.add(catalogo.pk)
        else:
            dimension = models.Dimension.objects.create(
                nombre=nombre,
                descripcion=descripcion,
                tipo_funcional=tipo_funcional if tipo_funcional in dict(models.Dimension.TIPO_FUNCIONAL_CHOICES) else 'atributo',
                tipo_dato=tipo_dato if tipo_dato in dict(models.Dimension.TIPO_DATO_CHOICES) else 'tabla',
                tipo_calculo=tipo_calculo or None,
                config_calculo=config_calculo,
            )
            estrategia_dimension = models.EstrategiaDimension.objects.create(
                estrategia=estrategia,
                dimension=dimension,
                orden=idx,
                obligatorio=obligatorio,
                considerar_avance_aca=considerar_avance_aca,
                visible_en_listado_aca=visible_en_listado_aca,
                considerar_avance_fmeca=considerar_avance_fmeca,
                visible_en_listado_fmeca=visible_en_listado_fmeca,
                proceso_uso=proceso_uso,
                activo=True,
            )
            catalogo = models.DimensionCatalogo.objects.create(
                estrategia_dimension=estrategia_dimension,
                nombre=nombre,
                campo=campo or '',
                tipo=tipo,
                descripcion=descripcion,
                activa=True,
            )
            keep_ids.add(catalogo.pk)

        dimension.nombre = nombre
        dimension.descripcion = descripcion
        dimension.tipo_funcional = tipo_funcional if tipo_funcional in dict(models.Dimension.TIPO_FUNCIONAL_CHOICES) else 'atributo'
        dimension.tipo_dato = tipo_dato if tipo_dato in dict(models.Dimension.TIPO_DATO_CHOICES) else 'tabla'
        dimension.tipo_calculo = tipo_calculo or None
        dimension.config_calculo = config_calculo
        dimension.save()

        estrategia_dimension.orden = idx
        estrategia_dimension.obligatorio = obligatorio
        estrategia_dimension.considerar_avance_aca = considerar_avance_aca
        estrategia_dimension.visible_en_listado_aca = visible_en_listado_aca
        estrategia_dimension.considerar_avance_fmeca = considerar_avance_fmeca
        estrategia_dimension.visible_en_listado_fmeca = visible_en_listado_fmeca
        estrategia_dimension.proceso_uso = proceso_uso
        estrategia_dimension.activo = True
        estrategia_dimension.save()

        catalogo.nombre = nombre
        catalogo.campo = campo
        catalogo.tipo = tipo
        catalogo.descripcion = descripcion
        catalogo.activa = True
        catalogo.save()

        _clean_catalogo(catalogo)

        if es_calculada:
            continue

        columnas_creadas = []
        used_column_keys = set()
        for col_idx, col in enumerate(columnas, start=1):
            tipo_columna = str(col.get('tipo_dato') or 'texto').strip()
            if tipo_columna not in dict(models.DimensionCatalogoColumna.TIPO_DATO_CHOICES):
                tipo_columna = 'texto'

            nombre_columna = str(col.get('nombre_columna') or '').strip() or f'Columna {col_idx}'
            raw_key = str(col.get('clave_interna') or '').strip()
            clave_interna = _unique_column_key(raw_key or nombre_columna, used_column_keys, f'columna_{col_idx}')
            columna = models.DimensionCatalogoColumna.objects.create(
                catalogo=catalogo,
                nombre_columna=nombre_columna,
                clave_interna=clave_interna,
                tipo_dato=tipo_columna,
                visible_en_registro=col.get('visible_en_registro', True) is not False,
                orden=col_idx,
            )
            columnas_creadas.append(columna)

        for row_idx, row in enumerate(filas, start=1):
            values = row.get('valores') if isinstance(row.get('valores'), dict) else {}
            values_by_column = row.get('valores_por_columna') if isinstance(row.get('valores_por_columna'), dict) else {}
            fila = models.DimensionCatalogoFila.objects.create(
                catalogo=catalogo,
                etiqueta=str(row.get('etiqueta') or values.get('etiqueta') or '').strip(),
                orden=row_idx,
            )
            for col_idx, col in enumerate(columnas_creadas, start=1):
                source_col = columnas[col_idx - 1] if col_idx - 1 < len(columnas) else {}
                source_col_id = str(source_col.get('id') or '')
                raw = values_by_column.get(source_col_id, values.get(col.clave_interna, ''))
                normalized = _normalize_catalog_cell_value(col.tipo_dato, raw)
                if (
                    normalized['valor_texto'] in ('', None)
                    and normalized['valor_numero'] is None
                    and normalized['valor_booleano'] is None
                ):
                    continue
                models.DimensionCatalogoCelda.objects.create(fila=fila, columna=col, **normalized)

    auto_impact_ed = _ensure_auto_impact_total_dimension(estrategia)
    auto_catalogo = _catalog_for_strategy_dimension(auto_impact_ed)
    if auto_catalogo:
        keep_ids.add(auto_catalogo.pk)

    to_delete = models.DimensionCatalogo.objects.filter(
        estrategia_dimension__estrategia=estrategia,
        estrategia_dimension__activo=True,
    ).exclude(pk__in=keep_ids)

    for catalogo in to_delete:
        estrategia_dimension = catalogo.estrategia_dimension
        if _hide_from_dimension_tables_editor(estrategia_dimension) or _is_auto_impact_total_dimension(estrategia_dimension):
            continue

        dimension = estrategia_dimension.dimension
        _clean_catalogo(catalogo)
        catalogo.delete()

        tiene_uso_historico = models.CriticidadDimension.objects.filter(
            estrategia_dimension_id=estrategia_dimension.id
        ).exists() or models.EvaluacionFMEA.objects.filter(
            estrategia_dimension_id=estrategia_dimension.id
        ).exists()
        if tiene_uso_historico:
            estrategia_dimension.activo = False
            estrategia_dimension.save(update_fields=['activo'])
            continue

        estrategia_dimension.delete()
        if not dimension.estrategias_dimension.exists() and not dimension.criticidades_dimension.exists():
            dimension.delete()


def service_matrix_view(request, service_pk, matrix_pk):
    servicio, permission = _service_or_404(request, service_pk, edit=False)
    matriz = get_object_or_404(
        models.MatrizRiesgo.objects.select_related(
            'estrategia', 'estrategia__empresa', 'dimension_probabilidad__dimension', 'dimension_impacto__dimension'
        ).prefetch_related('niveles_probabilidad', 'niveles_impacto', 'celdas__probabilidad', 'celdas__impacto_nivel'),
        pk=matrix_pk,
        estrategia_id=servicio.estrategia_id,
    )

    original_preview = _build_matrix_original_preview(matriz)
    original_stats = _matrix_stats(original_preview)
    mindco_viewer = is_mindco_user(request.user)
    homologation = _build_homologated_matrix_preview(matriz) if mindco_viewer else None

    return render(request, 'matrix_view.html', {
        'service': servicio,
        'permission': permission,
        'matriz': matriz,
        'mindco_viewer': mindco_viewer,
        'original_preview': original_preview,
        'original_stats': original_stats,
        'homologation': homologation,
        'homologated_stats': _matrix_stats(homologation['preview']) if homologation else None,
    })


def _dimension_editor_context(estrategia, payload=None):
    editor_payload = json.dumps(
        _json_safe(payload if payload is not None else _strategy_catalogs_payload(estrategia, only_active=True)),
        ensure_ascii=False,
    )
    dimension_suggestions, column_suggestions = _dimension_editor_suggestions()
    return {
        'estrategia': estrategia,
        'editor_payload_json': editor_payload,
        'dimension_suggestions_json': json.dumps(_json_safe(dimension_suggestions), ensure_ascii=False),
        'column_suggestions_json': json.dumps(_json_safe(column_suggestions), ensure_ascii=False),
        'tipos_funcionales': models.Dimension.TIPO_FUNCIONAL_CHOICES,
        'tipos_dato': models.Dimension.TIPO_DATO_CHOICES,
        'tipos_calculo': models.Dimension.TIPO_CALCULO_CHOICES,
        'procesos_uso': models.EstrategiaDimension.PROCESO_USO_CHOICES,
        'tipos_columna': models.DimensionCatalogoColumna.TIPO_DATO_CHOICES,
        'dimension_name_suggestions': [item['nombre'] for item in dimension_suggestions],
        'column_name_suggestions': [item['nombre'] for item in column_suggestions],
    }


@transaction.atomic
def dimension_tables_editor(request, pk):
    _ensure_admin_access(request)
    estrategia = get_object_or_404(models.Estrategia.objects.select_related('empresa'), pk=pk)
    if request.method == 'POST':
        payload = _json_payload(request, 'payload_json', []) or []
        validation_errors, validation_warnings = _validate_strategy_catalog_payload(payload)
        if validation_errors:
            for error in validation_errors[:10]:
                messages.error(request, error)
            if len(validation_errors) > 10:
                messages.error(request, f'Hay {len(validation_errors) - 10} errores adicionales. Corrige la tabla antes de guardar.')
            return render(request, 'dimension_table_editor.html', _dimension_editor_context(estrategia, payload))
        try:
            _save_strategy_catalogs(estrategia, payload)
            backfill_result = _backfill_calculated_dimension_records(estrategia)
        except DataError as exc:
            transaction.set_rollback(True)
            messages.error(request, f'No se pudo guardar porque una celda numerica excede el formato permitido: {exc}')
            return render(request, 'dimension_table_editor.html', _dimension_editor_context(estrategia, payload))
        for warning in validation_warnings[:5]:
            messages.warning(request, warning)
        calculated_total = sum(backfill_result.values())
        if calculated_total:
            messages.info(
                request,
                (
                    'Se sincronizaron dimensiones calculadas en registros existentes: '
                    f'{backfill_result["aca_created"]} ACA creadas, '
                    f'{backfill_result["aca_updated"]} ACA actualizadas, '
                    f'{backfill_result["fmeca_created"]} FMECA creadas, '
                    f'{backfill_result["fmeca_updated"]} FMECA actualizadas.'
                ),
            )
        messages.success(request, 'Las dimensiones y catálogos se guardaron correctamente.')
        return redirect('dimension_tables_editor', pk=estrategia.pk)

    return render(request, 'dimension_table_editor.html', _dimension_editor_context(estrategia))


def _axis_dimension_level_count(estrategia_dimension):
    if not estrategia_dimension:
        return 0
    scale_count = estrategia_dimension.escalas_valor.count()
    if scale_count:
        return scale_count
    try:
        return estrategia_dimension.catalogo.filas.count()
    except models.DimensionCatalogo.DoesNotExist:
        return 0


def _axis_dimension_requires_threshold(estrategia_dimension):
    if not estrategia_dimension:
        return False
    dimension = estrategia_dimension.dimension
    is_calculated = bool((getattr(dimension, 'tipo_calculo', '') or '').strip())
    return is_calculated and _axis_dimension_level_count(estrategia_dimension) == 0


def _matrix_mode_for_selected_axes(mode, prob_dimension, impact_dimension, request=None):
    mode = mode or models.MatrizRiesgo.RESOLUCION_EXACTA
    if (
        mode == models.MatrizRiesgo.RESOLUCION_EXACTA
        and (
            _axis_dimension_requires_threshold(prob_dimension)
            or _axis_dimension_requires_threshold(impact_dimension)
        )
    ):
        if request is not None:
            messages.warning(
                request,
                'La matriz usa una dimension calculada como eje. Se guardara en modo "Umbral inferior por resultado" para poder resolver valores calculados que no coincidan exactamente con un nivel.',
            )
        return models.MatrizRiesgo.RESOLUCION_UMBRAL_RESULTADO
    return mode


def _matrix_axis_dimension_options(builder_form):
    dimension_ids = set()
    for field_name in ('dimension_probabilidad', 'dimension_impacto'):
        queryset = builder_form.fields[field_name].queryset
        dimension_ids.update(queryset.values_list('id', flat=True))

    if not dimension_ids:
        return {}

    dimensions = (
        models.EstrategiaDimension.objects.filter(pk__in=dimension_ids)
        .select_related('dimension')
        .prefetch_related(
            'escalas_valor',
            'catalogo__filas__celdas__columna',
            'catalogo__columnas',
        )
    )

    payload = {}
    for estrategia_dimension in dimensions:
        source_count = _axis_dimension_level_count(estrategia_dimension)
        level_count = max(2, source_count or 5)
        try:
            catalogo = estrategia_dimension.catalogo
        except models.DimensionCatalogo.DoesNotExist:
            catalogo = None
        is_calculated = bool((estrategia_dimension.dimension.tipo_calculo or '').strip())
        has_scale = estrategia_dimension.escalas_valor.exists()
        has_catalog_levels = bool(catalogo and source_count)
        if has_scale:
            source_type = 'escala'
        elif has_catalog_levels:
            source_type = 'catalogo'
        elif is_calculated:
            source_type = 'calculada'
        else:
            source_type = 'manual'

        payload[str(estrategia_dimension.pk)] = {
            'id': estrategia_dimension.pk,
            'label': estrategia_dimension.dimension.nombre,
            'source_type': source_type,
            'has_levels': has_scale or has_catalog_levels,
            'is_calculated': is_calculated,
            'requires_threshold': _axis_dimension_requires_threshold(estrategia_dimension),
            'level_count': level_count,
            'prob_levels': _json_safe(
                _level_defs_from_strategy_dimension(estrategia_dimension, level_count, 'p')
            ),
            'impact_levels': _json_safe(
                _level_defs_from_strategy_dimension(estrategia_dimension, level_count, 'c')
            ),
        }
    return payload


def _criticality_rule_sources(builder_form, matriz=None):
    strategy_id = None
    if builder_form.is_bound:
        strategy_id = builder_form.data.get(builder_form.add_prefix('estrategia'))
    if not strategy_id:
        initial = builder_form.initial.get('estrategia')
        strategy_id = getattr(initial, 'pk', initial)
    if not strategy_id and matriz:
        strategy_id = matriz.estrategia_id
    dimensions = models.EstrategiaDimension.objects.filter(
        estrategia_id=strategy_id,
        activo=True,
        proceso_uso__in=[
            getattr(models.EstrategiaDimension, 'PROCESO_ACA', 'aca'),
            getattr(models.EstrategiaDimension, 'PROCESO_AMBOS', 'ambos'),
        ],
    ).select_related('dimension').order_by('orden', 'id') if strategy_id else []
    sources = []
    generated_prefixes = (
        'eje x -',
        'eje y -',
        'probabilidad -',
        'impacto -',
        'consecuencia -',
    )
    for item in dimensions:
        name = (item.dimension.nombre or '').strip()
        if not name or name.lower().startswith(generated_prefixes):
            continue
        sources.append({
            'value': f'ed:{item.pk}',
            'label': name,
        })
    return sources


def _auto_matrix_positive_int(value, default=5):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(2, min(parsed, 20))


def _auto_matrix_form_values(request):
    data = request.POST if request.method == 'POST' else request.GET
    if request.method == 'POST':
        reverse_y = '1' if data.get('reverse_y') == '1' else ''
    else:
        reverse_y = '' if data.get('reverse_y') == '0' else '1'
    return {
        'estrategia': data.get('estrategia') or '',
        'nombre': data.get('nombre') or '',
        'levels_x': data.get('levels_x') or '5',
        'levels_y': data.get('levels_y') or '5',
        'source_x': data.get('source_x') or '',
        'source_y': data.get('source_y') or '',
        'maximo_x_manual': data.get('maximo_x_manual') or '',
        'maximo_y_manual': data.get('maximo_y_manual') or '',
        'distribucion': 'lineal',
        'reverse_x': '1' if data.get('reverse_x') == '1' else '',
        'reverse_y': reverse_y,
    }


def _auto_matrix_legend_for_request(request, prob_defs, impact_defs):
    result_values = _matrix_result_values(prob_defs, impact_defs)
    min_value, max_value = (min(result_values), max(result_values)) if result_values else (Decimal('0'), Decimal('0'))
    raw_legend = _json_payload(request, 'legend_items_json', []) if request.method == 'POST' else []
    if not raw_legend:
        return _default_legend_for_bounds(min_value, max_value), None
    legend_items, legend_error = _validate_legend_items(raw_legend, min_value, max_value, result_values)
    return legend_items or _safe_legend_items(raw_legend), legend_error


def matriz_auto_generate(request):
    _ensure_admin_access(request)
    form_values = _auto_matrix_form_values(request)
    strategies = models.Estrategia.objects.select_related('empresa').order_by('nombre', 'id')
    selected_strategy = None
    if str(form_values['estrategia']).isdigit():
        selected_strategy = strategies.filter(pk=form_values['estrategia']).first()

    source_options = []
    source_index = {}
    if selected_strategy:
        source_options, source_index = _auto_matrix_source_options(selected_strategy)
    source_by_id = {str(item['id']): item for item in source_options}
    source_ed_by_id = {str(item['id']): source_index.get(f"ed:{item['id']}") for item in source_options}

    preview = None
    if request.method == 'POST':
        action = request.POST.get('action') or 'preview'
        errors = []
        nombre = (form_values['nombre'] or '').strip()
        if not selected_strategy:
            errors.append('Selecciona una estrategia.')
        if not nombre:
            errors.append('Ingresa un nombre para la matriz.')

        source_x_option = source_by_id.get(form_values['source_x'])
        source_y_option = source_by_id.get(form_values['source_y'])
        source_x = source_ed_by_id.get(form_values['source_x'])
        source_y = source_ed_by_id.get(form_values['source_y'])
        if not source_x_option or not source_x:
            errors.append('Selecciona la fuente del eje X.')
        if not source_y_option or not source_y:
            errors.append('Selecciona la fuente del eje Y.')

        max_x = source_x_option.get('maximo') if source_x_option else None
        max_y = source_y_option.get('maximo') if source_y_option else None
        if max_x is None:
            max_x = _decimal_or_none(form_values['maximo_x_manual'])
        if max_y is None:
            max_y = _decimal_or_none(form_values['maximo_y_manual'])
        if max_x is None or max_x <= 0:
            errors.append('El eje X necesita un máximo teórico mayor a 0.')
        if max_y is None or max_y <= 0:
            errors.append('El eje Y necesita un máximo teórico mayor a 0.')

        levels_x = _auto_matrix_positive_int(form_values['levels_x'])
        levels_y = _auto_matrix_positive_int(form_values['levels_y'])
        form_values['levels_x'] = str(levels_x)
        form_values['levels_y'] = str(levels_y)

        if not errors:
            ranges_x = _auto_matrix_linear_ranges(max_x, levels_x)
            ranges_y = _auto_matrix_linear_ranges(max_y, levels_y)
            prob_defs = _auto_matrix_level_defs(ranges_x, 'X')
            impact_defs = _auto_matrix_level_defs(ranges_y, 'Y')
            legend_items, legend_error = _auto_matrix_legend_for_request(request, prob_defs, impact_defs)
            preview = _auto_matrix_preview_payload(
                source_x,
                source_y,
                max_x,
                max_y,
                levels_x,
                levels_y,
                legend_items=legend_items,
                reverse_x=form_values['reverse_x'] == '1',
                reverse_y=form_values['reverse_y'] == '1',
            )
            if legend_error:
                messages.error(request, legend_error)
            if action == 'generate' and not legend_error:
                matriz = _create_auto_matrix(nombre, selected_strategy, preview)
                messages.success(request, 'La matriz automática se generó correctamente. Puedes ajustar colores y clasificaciones en el editor.')
                return redirect('matriz_builder_edit', pk=matriz.pk)

        for error in errors:
            messages.error(request, error)

    return render(request, 'matrix_auto_generate.html', {
        'strategies': strategies,
        'selected_strategy': selected_strategy,
        'source_options': source_options,
        'form_values': form_values,
        'preview': preview,
        'preview_legend_json': json.dumps(_json_safe(preview['legend_items'] if preview else []), ensure_ascii=False),
    })


def _matrix_builder_context(is_create, matriz, builder_form, matrix_preview, display_legend, criticality_rules=None):
    return {
        'is_create': is_create,
        'matriz': matriz,
        'builder_form': builder_form,
        'matrix_preview': matrix_preview,
        'matrix_ui_state': _matrix_ui_payload(matrix_preview, stored_legend=display_legend or []),
        'matrix_axis_options': _matrix_axis_dimension_options(builder_form),
        'criticality_rules': criticality_rules if criticality_rules is not None else (matrix_rule_config(matriz) if matriz else []),
        'criticality_rule_sources': _criticality_rule_sources(builder_form, matriz),
    }


def _matrix_builder_bound_post_state(request, builder_form, matriz=None):
    def posted_count(name, default):
        try:
            value = int(request.POST.get(name, default))
        except (TypeError, ValueError):
            return default
        # Preserve a moderately oversized submitted grid without allowing an
        # arbitrary POST to force an excessively large preview.
        return max(2, min(value, 20))

    cleaned = getattr(builder_form, 'cleaned_data', {}) or {}
    strategy = cleaned.get('estrategia')
    if not strategy:
        strategy_id = request.POST.get('estrategia')
        if str(strategy_id or '').isdigit():
            strategy = models.Estrategia.objects.filter(pk=strategy_id).first()
    if not strategy and matriz:
        strategy = matriz.estrategia

    def selected_dimension(field_name, existing=None):
        selected = cleaned.get(field_name)
        if selected:
            return selected
        selected_id = request.POST.get(field_name)
        queryset = models.EstrategiaDimension.objects.select_related('dimension')
        if strategy:
            queryset = queryset.filter(estrategia=strategy)
        if str(selected_id or '').isdigit():
            return queryset.filter(pk=selected_id).first() or existing
        return existing

    selected_prob = selected_dimension(
        'dimension_probabilidad',
        matriz.dimension_probabilidad if matriz else None,
    )
    selected_impact = selected_dimension(
        'dimension_impacto',
        matriz.dimension_impacto if matriz else None,
    )
    prob_count = posted_count('x_count', 5)
    impact_count = posted_count('y_count', 5)
    fallback_prob = _level_defs_from_strategy_dimension(selected_prob, prob_count, 'p')
    fallback_impact = _level_defs_from_strategy_dimension(selected_impact, impact_count, 'c')
    prob_defs, impact_defs = _definitions_from_request(
        request,
        prob_count,
        impact_count,
        fallback_prob,
        fallback_impact,
    )
    display_legend = _safe_legend_items(
        _json_payload(request, 'legend_items_json', []) or []
    )
    display_rules, _rule_errors = normalize_rules(
        _json_payload(request, 'criticality_rules_json', []) or []
    )
    matrix_preview = _matrix_preview_from_defs(
        'probabilidad',
        prob_defs,
        impact_defs,
        _cell_data_from_request(request),
        strategy,
        selected_prob,
        selected_impact,
        display_legend,
    )
    return matrix_preview, display_legend, display_rules


@transaction.atomic
def matriz_builder_new(request):
    _ensure_admin_access(request)
    display_legend = None
    display_rules = []
    if request.method == 'POST':
        strategy = request.POST.get('estrategia') or None
        builder_form = MatrizBuilderForm(
            request.POST,
            strategy=strategy,
            initial={'fecha_creado': timezone.localdate()},
        )
        if builder_form.is_valid():
            action = request.POST.get('action', 'preview')
            cd = builder_form.cleaned_data
            selected_axis = 'probabilidad'
            x_count = cd['x_count']
            y_count = cd['y_count']
            prob_count = x_count
            impact_count = y_count
            selected_prob = cd.get('dimension_probabilidad')
            selected_impact = cd.get('dimension_impacto')
            mode = _matrix_mode_for_selected_axes(
                cd.get('modo_resolucion'),
                selected_prob,
                selected_impact,
                request if action == 'save' else None,
            )
            fallback_prob = _level_defs_from_strategy_dimension(selected_prob, prob_count, 'p')
            fallback_impact = _level_defs_from_strategy_dimension(selected_impact, impact_count, 'c')
            prob_defs, impact_defs = _definitions_from_request(request, prob_count, impact_count, fallback_prob, fallback_impact)
            cell_payload = _cell_data_from_request(request)
            min_value, max_value = _matrix_value_bounds(prob_defs, impact_defs)
            result_values = _matrix_result_values(prob_defs, impact_defs)
            raw_legend_items = _json_payload(request, 'legend_items_json', []) or []
            legend_items, legend_error = _validate_legend_items(raw_legend_items, min_value, max_value, result_values)
            display_legend = legend_items or _safe_legend_items(raw_legend_items)
            display_rules, rule_errors = normalize_rules(_json_payload(request, 'criticality_rules_json', []) or [])
            matrix_preview = _matrix_preview_from_defs(selected_axis, prob_defs, impact_defs, cell_payload, cd['estrategia'], None, None, legend_items or display_legend)
            if legend_error:
                messages.error(request, legend_error)
            for rule_error in rule_errors[:8]:
                messages.error(request, rule_error)
            if action == 'save' and not legend_error and not rule_errors:
                prob_dim, impact_dim = _ensure_matrix_strategy_dimensions(
                    cd['estrategia'],
                    cd['nombre'],
                    prob_defs,
                    impact_defs,
                    selected_prob=cd.get('dimension_probabilidad'),
                    selected_impact=cd.get('dimension_impacto'),
                )
                matriz = models.MatrizRiesgo.objects.create(
                    nombre=cd['nombre'],
                    fecha_creado=cd['fecha_creado'],
                    estrategia=cd['estrategia'],
                    eje_horizontal='probabilidad',
                    dimension_probabilidad=prob_dim,
                    dimension_impacto=impact_dim,
                    leyenda_json=json.dumps(
                        _matrix_legend_payload(legend_items, mode, display_rules),
                        ensure_ascii=False,
                    ),
                )
                _persist_matrix_grid(matriz, prob_defs, impact_defs, cell_payload)
                messages.success(request, 'La matriz se creó correctamente y sus dimensiones se asignaron automáticamente.')
                return redirect('matriz_builder_edit', pk=matriz.pk)
        else:
            matrix_preview, display_legend, display_rules = _matrix_builder_bound_post_state(
                request,
                builder_form,
            )
    else:
        initial_strategy = request.GET.get('estrategia') or None
        builder_form = MatrizBuilderForm(initial={
            'fecha_creado': timezone.localdate(),
            'estrategia': initial_strategy,
            'modo_resolucion': models.MatrizRiesgo.RESOLUCION_EXACTA,
            'x_count': 5,
            'y_count': 5,
        }, strategy=initial_strategy)
        matrix_preview = _matrix_preview_from_defs('probabilidad', _matrix_level_dicts([], 5, 'p'), _matrix_level_dicts([], 5, 'c'))

    return render(
        request,
        'matrix_builder.html',
        _matrix_builder_context(True, None, builder_form, matrix_preview, display_legend, display_rules),
    )


@transaction.atomic
def matriz_builder_edit(request, pk):
    _ensure_admin_access(request)
    matriz = get_object_or_404(
        models.MatrizRiesgo.objects.select_related(
            'estrategia', 'estrategia__empresa', 'dimension_probabilidad__dimension', 'dimension_impacto__dimension'
        ).prefetch_related('niveles_probabilidad', 'niveles_impacto', 'celdas__probabilidad', 'celdas__impacto_nivel'),
        pk=pk,
    )

    display_legend = None
    display_rules = matrix_rule_config(matriz)
    if request.method == 'POST':
        strategy = request.POST.get('estrategia') or matriz.estrategia_id
        builder_form = MatrizBuilderForm(
            request.POST,
            strategy=strategy,
            initial={'fecha_creado': matriz.fecha_creado},
        )
        if builder_form.is_valid():
            action = request.POST.get('action', 'preview')
            cd = builder_form.cleaned_data
            selected_axis = 'probabilidad'
            x_count = cd['x_count']
            y_count = cd['y_count']
            prob_count = x_count
            impact_count = y_count
            selected_prob = cd.get('dimension_probabilidad') or matriz.dimension_probabilidad
            selected_impact = cd.get('dimension_impacto') or matriz.dimension_impacto
            mode = _matrix_mode_for_selected_axes(
                cd.get('modo_resolucion'),
                selected_prob,
                selected_impact,
                request if action == 'save' else None,
            )
            fallback_prob = _level_defs_from_strategy_dimension(selected_prob, prob_count, 'p')
            fallback_impact = _level_defs_from_strategy_dimension(selected_impact, impact_count, 'c')
            prob_defs, impact_defs = _definitions_from_request(request, prob_count, impact_count, fallback_prob, fallback_impact)
            cell_payload = _cell_data_from_request(request)
            min_value, max_value = _matrix_value_bounds(prob_defs, impact_defs)
            result_values = _matrix_result_values(prob_defs, impact_defs)
            raw_legend_items = _json_payload(request, 'legend_items_json', []) or []
            legend_items, legend_error = _validate_legend_items(raw_legend_items, min_value, max_value, result_values)
            display_legend = legend_items or _safe_legend_items(raw_legend_items)
            display_rules, rule_errors = normalize_rules(_json_payload(request, 'criticality_rules_json', []) or [])
            matrix_preview = _matrix_preview_from_defs(selected_axis, prob_defs, impact_defs, cell_payload, cd['estrategia'], selected_prob, selected_impact, legend_items or display_legend)
            if legend_error:
                messages.error(request, legend_error)
            for rule_error in rule_errors[:8]:
                messages.error(request, rule_error)
            if action == 'save' and not legend_error and not rule_errors:
                prob_dim, impact_dim = _ensure_matrix_strategy_dimensions(
                    cd['estrategia'],
                    cd['nombre'],
                    prob_defs,
                    impact_defs,
                    existing_prob=matriz.dimension_probabilidad,
                    existing_impact=matriz.dimension_impacto,
                    selected_prob=cd.get('dimension_probabilidad'),
                    selected_impact=cd.get('dimension_impacto'),
                )
                matriz.nombre = cd['nombre']
                matriz.fecha_creado = cd['fecha_creado']
                matriz.estrategia = cd['estrategia']
                matriz.eje_horizontal = 'probabilidad'
                matriz.dimension_probabilidad = prob_dim
                matriz.dimension_impacto = impact_dim
                matriz.leyenda_json = json.dumps(
                    _matrix_legend_payload(legend_items, mode, display_rules),
                    ensure_ascii=False,
                )
                matriz.save()
                _persist_matrix_grid(matriz, prob_defs, impact_defs, cell_payload)
                messages.success(request, 'La matriz se actualizó correctamente y sus dimensiones se ajustaron automáticamente.')
                return redirect('matriz_builder_edit', pk=matriz.pk)
        else:
            matrix_preview, display_legend, display_rules = _matrix_builder_bound_post_state(
                request,
                builder_form,
                matriz,
            )
    else:
        prob_levels = list(matriz.niveles_probabilidad.order_by('orden_visual', 'id'))
        impact_levels = list(matriz.niveles_impacto.order_by('orden_visual', 'id'))
        prob_defs = _matrix_level_dicts(prob_levels, len(prob_levels) or 5, 'p')
        impact_defs = _matrix_level_dicts(impact_levels, len(impact_levels) or 5, 'c')
        existing_cells = {
            (cell.probabilidad.orden_visual, cell.impacto_nivel.orden_visual): cell
            for cell in matriz.celdas.select_related('probabilidad', 'impacto_nivel').all()
        }
        display_legend = _legend_from_matrix(matriz)
        display_rules = matrix_rule_config(matriz)
        matrix_preview = _matrix_preview_from_defs(
            'probabilidad',
            prob_defs,
            impact_defs,
            existing_cells,
            matriz.estrategia,
            matriz.dimension_probabilidad,
            matriz.dimension_impacto,
            display_legend,
        )
        builder_form = MatrizBuilderForm(initial={
            'nombre': matriz.nombre,
            'fecha_creado': matriz.fecha_creado,
            'estrategia': matriz.estrategia,
            'dimension_probabilidad': matriz.dimension_probabilidad,
            'dimension_impacto': matriz.dimension_impacto,
            'modo_resolucion': _matrix_resolution_mode(matriz),
            'x_count': len(matrix_preview['x_defs']),
            'y_count': len(matrix_preview['rows']),
        }, strategy=matriz.estrategia)

    return render(
        request,
        'matrix_builder.html',
        _matrix_builder_context(False, matriz, builder_form, matrix_preview, display_legend, display_rules),
    )
