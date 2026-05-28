import json
import re
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


CATALOG_NUMBER_MAX = Decimal('9999999999.99')
CATALOG_NUMBER_DECIMAL_PLACES = 2


def _normalize_process_usage(value):
    value = str(value or models.EstrategiaDimension.PROCESO_ACA).strip()
    if value == getattr(models.EstrategiaDimension, 'PROCESO_RCM_LEGACY', 'rcm'):
        return models.EstrategiaDimension.PROCESO_FMECA
    return value


# ---------------------------------------------------------------------------
# Editor de dimensiones y tablas por estrategia
# ---------------------------------------------------------------------------
def _serialize_dimension_catalog(catalogo):
    ed = catalogo.estrategia_dimension
    dimension = ed.dimension
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
        'proceso_uso': _normalize_process_usage(ed.proceso_uso),
        'activo': ed.activo,
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
    value = (value or '').strip().lower()
    value = re.sub(r'[^a-z0-9áéíóúñ]+', '_', value, flags=re.IGNORECASE)
    return value.strip('_')[:100] or 'dimension'


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


def _serialize_strategy_dimension_without_catalog(ed):
    dimension = ed.dimension
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
        'proceso_uso': _normalize_process_usage(ed.proceso_uso),
        'activo': ed.activo,
        'columnas': [] if dimension.tipo_calculo else _default_columns_for_type(tipo),
        'filas': [],
    }


def _hide_from_dimension_tables_editor(estrategia_dimension):
    return _is_generated_matrix_axis_dimension(estrategia_dimension)


def _strategy_catalogs_payload(estrategia, only_active=True):
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
    lower_keys = ['limite_inferior', 'desde', 'min', 'minimo', 'mÃ­nimo']
    upper_keys = ['limite_superior', 'hasta', 'max', 'maximo', 'mÃ¡ximo']

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
    tipos_catalogo_validos = {'opciones', 'rangos', 'numerico_libre'}
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

        def _clean_source_ref(value):
            if isinstance(value, dict):
                cleaned = {}
                for key in ['estrategia_dimension_id', 'dimension_id', 'campo', 'nombre', 'source']:
                    raw = value.get(key)
                    if raw not in (None, ''):
                        cleaned[key] = raw
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
                    cleaned.append({'resultado': True})
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
                    steps.append({'operacion': operacion, 'operandos': operandos})

        if steps:
            return {'pasos': steps, 'operandos': steps[0]['operandos']}

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
        if cat_id and cat_id in existing:
            catalogo = existing[cat_id]
            estrategia_dimension = catalogo.estrategia_dimension
            dimension = estrategia_dimension.dimension
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
        for col_idx, col in enumerate(columnas, start=1):
            tipo_columna = str(col.get('tipo_dato') or 'texto').strip()
            if tipo_columna not in dict(models.DimensionCatalogoColumna.TIPO_DATO_CHOICES):
                tipo_columna = 'texto'

            columna = models.DimensionCatalogoColumna.objects.create(
                catalogo=catalogo,
                nombre_columna=str(col.get('nombre_columna') or '').strip() or f'Columna {col_idx}',
                clave_interna=str(col.get('clave_interna') or '').strip() or f'col_{col_idx}',
                tipo_dato=tipo_columna,
                visible_en_registro=col.get('visible_en_registro', True) is not False,
                orden=col_idx,
            )
            columnas_creadas.append(columna)

        for row_idx, row in enumerate(filas, start=1):
            values = row.get('valores') if isinstance(row.get('valores'), dict) else {}
            fila = models.DimensionCatalogoFila.objects.create(
                catalogo=catalogo,
                etiqueta=str(row.get('etiqueta') or values.get('etiqueta') or '').strip(),
                orden=row_idx,
            )
            for col in columnas_creadas:
                raw = values.get(col.clave_interna, '')
                normalized = _normalize_catalog_cell_value(col.tipo_dato, raw)
                if (
                    normalized['valor_texto'] in ('', None)
                    and normalized['valor_numero'] is None
                    and normalized['valor_booleano'] is None
                ):
                    continue
                models.DimensionCatalogoCelda.objects.create(fila=fila, columna=col, **normalized)

    to_delete = models.DimensionCatalogo.objects.filter(
        estrategia_dimension__estrategia=estrategia,
        estrategia_dimension__activo=True,
    ).exclude(pk__in=keep_ids)

    for catalogo in to_delete:
        estrategia_dimension = catalogo.estrategia_dimension
        if _hide_from_dimension_tables_editor(estrategia_dimension):
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

    return render(request, 'core/evaluation_tables/matrix_view.html', {
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
    return {
        'estrategia': estrategia,
        'editor_payload_json': editor_payload,
        'tipos_funcionales': models.Dimension.TIPO_FUNCIONAL_CHOICES,
        'tipos_dato': models.Dimension.TIPO_DATO_CHOICES,
        'tipos_calculo': models.Dimension.TIPO_CALCULO_CHOICES,
        'procesos_uso': models.EstrategiaDimension.PROCESO_USO_CHOICES,
        'tipos_columna': models.DimensionCatalogoColumna.TIPO_DATO_CHOICES,
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
            return render(request, 'core/evaluation_tables/dimension_table_editor.html', _dimension_editor_context(estrategia, payload))
        try:
            _save_strategy_catalogs(estrategia, payload)
        except DataError as exc:
            transaction.set_rollback(True)
            messages.error(request, f'No se pudo guardar porque una celda numerica excede el formato permitido: {exc}')
            return render(request, 'core/evaluation_tables/dimension_table_editor.html', _dimension_editor_context(estrategia, payload))
        for warning in validation_warnings[:5]:
            messages.warning(request, warning)
        messages.success(request, 'Las dimensiones y catálogos se guardaron correctamente.')
        return redirect('dimension_tables_editor', pk=estrategia.pk)

    return render(request, 'core/evaluation_tables/dimension_table_editor.html', _dimension_editor_context(estrategia))


@transaction.atomic
def matriz_builder_new(request):
    _ensure_admin_access(request)
    display_legend = None
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
            selected_axis = cd['eje_horizontal']
            x_count = cd['x_count']
            y_count = cd['y_count']
            prob_count = y_count if selected_axis == 'impacto' else x_count
            impact_count = x_count if selected_axis == 'impacto' else y_count
            fallback_prob = _matrix_level_dicts([], prob_count, 'p')
            fallback_impact = _matrix_level_dicts([], impact_count, 'i')
            prob_defs, impact_defs = _definitions_from_request(request, prob_count, impact_count, fallback_prob, fallback_impact)
            cell_payload = _cell_data_from_request(request)
            min_value, max_value = _matrix_value_bounds(prob_defs, impact_defs)
            result_values = _matrix_result_values(prob_defs, impact_defs)
            raw_legend_items = _json_payload(request, 'legend_items_json', []) or []
            legend_items, legend_error = _validate_legend_items(raw_legend_items, min_value, max_value, result_values)
            display_legend = legend_items or _safe_legend_items(raw_legend_items)
            matrix_preview = _matrix_preview_from_defs(selected_axis, prob_defs, impact_defs, cell_payload, cd['estrategia'], None, None, legend_items or display_legend)
            if legend_error:
                messages.error(request, legend_error)
            if action == 'save' and not legend_error:
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
                    eje_horizontal=selected_axis,
                    dimension_probabilidad=prob_dim,
                    dimension_impacto=impact_dim,
                    leyenda_json=json.dumps(
                        _matrix_legend_payload(legend_items, cd.get('modo_resolucion')),
                        ensure_ascii=False,
                    ),
                )
                _persist_matrix_grid(matriz, prob_defs, impact_defs, cell_payload)
                messages.success(request, 'La matriz se creó correctamente y sus dimensiones se asignaron automáticamente.')
                return redirect('matriz_builder_edit', pk=matriz.pk)
        else:
            matrix_preview = _matrix_preview_from_defs('impacto', _matrix_level_dicts([], 5, 'p'), _matrix_level_dicts([], 5, 'i'))
    else:
        builder_form = MatrizBuilderForm(initial={
            'fecha_creado': timezone.localdate(),
            'eje_horizontal': 'impacto',
            'modo_resolucion': models.MatrizRiesgo.RESOLUCION_EXACTA,
            'x_count': 5,
            'y_count': 5,
        })
        matrix_preview = _matrix_preview_from_defs('impacto', _matrix_level_dicts([], 5, 'p'), _matrix_level_dicts([], 5, 'i'))

    return render(request, 'core/evaluation_tables/matrix_builder.html', {
        'is_create': True,
        'matriz': None,
        'builder_form': builder_form,
        'matrix_preview': matrix_preview,
        'matrix_ui_state': _matrix_ui_payload(matrix_preview, stored_legend=display_legend),
    })


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
            selected_axis = cd['eje_horizontal']
            x_count = cd['x_count']
            y_count = cd['y_count']
            prob_count = y_count if selected_axis == 'impacto' else x_count
            impact_count = x_count if selected_axis == 'impacto' else y_count
            fallback_prob = _level_defs_from_strategy_dimension(matriz.dimension_probabilidad, prob_count, 'p')
            fallback_impact = _level_defs_from_strategy_dimension(matriz.dimension_impacto, impact_count, 'i')
            prob_defs, impact_defs = _definitions_from_request(request, prob_count, impact_count, fallback_prob, fallback_impact)
            cell_payload = _cell_data_from_request(request)
            min_value, max_value = _matrix_value_bounds(prob_defs, impact_defs)
            result_values = _matrix_result_values(prob_defs, impact_defs)
            raw_legend_items = _json_payload(request, 'legend_items_json', []) or []
            legend_items, legend_error = _validate_legend_items(raw_legend_items, min_value, max_value, result_values)
            display_legend = legend_items or _safe_legend_items(raw_legend_items)
            matrix_preview = _matrix_preview_from_defs(selected_axis, prob_defs, impact_defs, cell_payload, cd['estrategia'], matriz.dimension_probabilidad, matriz.dimension_impacto, legend_items or display_legend)
            if legend_error:
                messages.error(request, legend_error)
            if action == 'save' and not legend_error:
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
                matriz.eje_horizontal = selected_axis
                matriz.dimension_probabilidad = prob_dim
                matriz.dimension_impacto = impact_dim
                matriz.leyenda_json = json.dumps(
                    _matrix_legend_payload(legend_items, cd.get('modo_resolucion')),
                    ensure_ascii=False,
                )
                matriz.save()
                _persist_matrix_grid(matriz, prob_defs, impact_defs, cell_payload)
                messages.success(request, 'La matriz se actualizó correctamente y sus dimensiones se ajustaron automáticamente.')
                return redirect('matriz_builder_edit', pk=matriz.pk)
        else:
            matrix_preview = _matrix_preview_from_defs(matriz.eje_horizontal or 'impacto', _matrix_level_dicts([], 5, 'p'), _matrix_level_dicts([], 5, 'i'))
    else:
        prob_levels = list(matriz.niveles_probabilidad.order_by('orden_visual', 'id'))
        impact_levels = list(matriz.niveles_impacto.order_by('orden_visual', 'id'))
        prob_defs = _matrix_level_dicts(prob_levels, len(prob_levels) or 5, 'p')
        impact_defs = _matrix_level_dicts(impact_levels, len(impact_levels) or 5, 'i')
        existing_cells = {
            (cell.probabilidad.orden_visual, cell.impacto_nivel.orden_visual): cell
            for cell in matriz.celdas.select_related('probabilidad', 'impacto_nivel').all()
        }
        display_legend = _legend_from_matrix(matriz)
        matrix_preview = _matrix_preview_from_defs(
            matriz.eje_horizontal or 'impacto',
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
            'eje_horizontal': matriz.eje_horizontal,
            'modo_resolucion': _matrix_resolution_mode(matriz),
            'x_count': len(matrix_preview['x_defs']),
            'y_count': len(matrix_preview['rows']),
        }, strategy=matriz.estrategia)

    return render(request, 'core/evaluation_tables/matrix_builder.html', {
        'is_create': False,
        'matriz': matriz,
        'builder_form': builder_form,
        'matrix_preview': matrix_preview,
        'matrix_ui_state': _matrix_ui_payload(matrix_preview, stored_legend=display_legend or []),
    })
