from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Prefetch, Q

from core import models


def get_attr_any(obj: Any, names: list[str], default=''):
    if obj is None:
        return default
    if isinstance(obj, dict):
        for name in names:
            value = obj.get(name)
            if value not in (None, ''):
                return value
        return default
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value not in (None, ''):
                return value
    return default


def _decimal_or_none(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _decimal_sum(values):
    total = Decimal('0')
    found = False
    for value in values:
        decimal_value = _decimal_or_none(value)
        if decimal_value is not None:
            total += decimal_value
            found = True
    return total if found else None


def _decimal_first(values):
    for value in values:
        decimal_value = _decimal_or_none(value)
        if decimal_value is not None:
            return decimal_value
    return None


def _single_or_mixed(values, mixed='Varios'):
    cleaned = [
        str(value).strip()
        for value in values
        if value not in (None, '')
    ]
    if not cleaned:
        return ''
    unique = []
    for value in cleaned:
        if value not in unique:
            unique.append(value)
    return unique[0] if len(unique) == 1 else mixed


def _dynamic_task_values(task):
    values = {}
    cached_values = getattr(task, 'valores_campos_cache', None)
    if cached_values is None:
        cached_values = task.valores_campos.all()
    for item in cached_values:
        key = item.campo.clave
        values[key] = item.valor_display
    return values


def _task_value(task, fields, default=''):
    value = get_attr_any(task, fields, None)
    if value not in (None, ''):
        return value
    dynamic_values = _dynamic_task_values(task)
    return get_attr_any(dynamic_values, fields, default)


def _task_frequency(task):
    text = _task_value(task, ['frecuencia_texto', 'frecuencia', 'periodicidad'], '')
    if text:
        return str(text)
    value = _task_value(task, ['frecuencia_valor'], '')
    unit = _task_value(task, ['frecuencia_unidad'], '')
    return ' '.join(str(part) for part in [value, unit] if part not in (None, ''))


def _task_specialty(task):
    return str(_task_value(task, ['puesto_trabajo', 'pto_trabajo', 'especialidad'], '') or '')


def _task_activity(task):
    return str(_task_value(task, [
        'descripcion',
        'actividad',
        'actividad_mantenimiento_primaria',
        'actividad_primaria',
        'ds_descripcion_de_la_tarea',
    ], '') or '')


def _filter_text(value):
    value = unicodedata.normalize('NFKD', str(value or ''))
    value = ''.join(char for char in value if not unicodedata.combining(char))
    return ' '.join(value.lower().split())


def _contains_filter(value, query):
    query = _filter_text(query)
    if not query:
        return True
    return query in _filter_text(value)


def _task_component(task, rcm):
    value = _task_value(task, [
        'componente_involucrado',
        'componente',
        'item_mantenible',
        'items_componente',
        'equipo',
    ], '')
    if value:
        return str(value)
    return rcm.equipo.tag_display if getattr(rcm, 'equipo_id', None) else ''


def _task_type_text(task):
    tipo = task.tipo_tarea_estrategia
    return f'{tipo.codigo} {tipo.nombre}'.lower()


def _is_secondary_task(task):
    text = _task_type_text(task)
    return any(token in text for token in ['secund', 'complement'])


def _is_current_task(task):
    text = _task_type_text(task)
    return any(token in text for token in ['actual', 'existente'])


def _is_primary_task(task):
    text = _task_type_text(task)
    if _is_secondary_task(task) or _is_current_task(task):
        return False
    if any(token in text for token in ['prim', 'principal', 'tarea', 'actividad']):
        return True
    return True


def _rule_value(regla, field, default):
    if regla is None:
        return default
    return getattr(regla, field, default)


@dataclass
class RuntimeRule:
    agrupar_por_equipo: bool = True
    agrupar_por_ubicacion: bool = True
    agrupar_por_frecuencia: bool = True
    agrupar_por_especialidad: bool = True
    agrupar_por_estado_equipo: bool = True
    incluir_tareas_primarias: bool = True
    incluir_tareas_secundarias: bool = False


def build_runtime_rule(form_cleaned=None, regla=None):
    form_cleaned = form_cleaned or {}
    return RuntimeRule(
        agrupar_por_equipo=_rule_value(regla, 'agrupar_por_equipo', True),
        agrupar_por_ubicacion=_rule_value(regla, 'agrupar_por_ubicacion', True),
        agrupar_por_frecuencia=_rule_value(regla, 'agrupar_por_frecuencia', True),
        agrupar_por_especialidad=_rule_value(regla, 'agrupar_por_especialidad', True),
        agrupar_por_estado_equipo=_rule_value(regla, 'agrupar_por_estado_equipo', True),
        incluir_tareas_primarias=bool(form_cleaned.get('incluir_tareas_primarias', _rule_value(regla, 'incluir_tareas_primarias', True))),
        incluir_tareas_secundarias=bool(form_cleaned.get('incluir_tareas_secundarias', _rule_value(regla, 'incluir_tareas_secundarias', False))),
    )


def get_rcm_queryset_for_service(servicio, estrategia=None, filtros=None):
    filtros = filtros or {}
    qs = models.RCM.objects.filter(carga__servicio=servicio)
    if estrategia:
        qs = qs.filter(carga__estrategia=estrategia)

    equipment_query = (filtros.get('equipo') or '').strip()
    if equipment_query:
        qs = qs.filter(
            Q(equipo__ut__icontains=equipment_query)
            | Q(equipo__tag_equipo__icontains=equipment_query)
            | Q(equipo__nombre_equipo__icontains=equipment_query)
        )

    task_filter = Q()
    if filtros.get('frecuencia'):
        task_filter &= Q(fmea_fmeca__tareas_rcm__frecuencia_texto__icontains=filtros['frecuencia'])
    if filtros.get('especialidad'):
        task_filter &= (
            Q(fmea_fmeca__tareas_rcm__especialidad__icontains=filtros['especialidad'])
            | Q(fmea_fmeca__tareas_rcm__puesto_trabajo__icontains=filtros['especialidad'])
        )
    if filtros.get('estado_equipo'):
        task_filter &= Q(fmea_fmeca__tareas_rcm__estado_equipo__icontains=filtros['estado_equipo'])
    if task_filter:
        qs = qs.filter(task_filter)

    task_qs = models.TareaRCM.objects.filter(
        estado=models.TareaRCM.ESTADO_ACTIVO,
    ).select_related(
        'tipo_tarea_estrategia',
    ).prefetch_related(
        Prefetch(
            'valores_campos',
            queryset=models.ValorCampoTareaRCM.objects.select_related('campo'),
            to_attr='valores_campos_cache',
        )
    ).order_by('orden', 'id')

    return qs.select_related(
        'carga',
        'equipo',
        'fmea_fmeca',
    ).prefetch_related(
        Prefetch('fmea_fmeca__tareas_rcm', queryset=task_qs, to_attr='tareas_pauta_cache')
    ).distinct().order_by('equipo__ut', 'id')


def _task_allowed(task, regla):
    if _is_secondary_task(task):
        return regla.incluir_tareas_secundarias
    if _is_primary_task(task):
        return regla.incluir_tareas_primarias
    return regla.incluir_tareas_primarias


def _task_matches_filters(item, filtros):
    filtros = filtros or {}
    if not _contains_filter(item['frequency'], filtros.get('frecuencia')):
        return False
    if not _contains_filter(item['specialty'], filtros.get('especialidad')):
        return False
    if not _contains_filter(item['estado_equipo'], filtros.get('estado_equipo')):
        return False
    return True


def _iter_groupable_tasks(queryset, regla, filtros=None):
    for rcm in queryset:
        fmea = getattr(rcm, 'fmea_fmeca', None)
        if not fmea:
            continue
        tasks = getattr(fmea, 'tareas_pauta_cache', None)
        if tasks is None:
            tasks = fmea.tareas_rcm.filter(estado=models.TareaRCM.ESTADO_ACTIVO).select_related('tipo_tarea_estrategia')
        for task in tasks:
            if not _task_allowed(task, regla):
                continue
            activity = _task_activity(task)
            if not activity:
                continue
            item = {
                'rcm': rcm,
                'fmea': fmea,
                'task': task,
                'activity': activity,
                'component': _task_component(task, rcm),
                'frequency': _task_frequency(task),
                'specialty': _task_specialty(task),
                'estado_equipo': str(_task_value(task, ['estado_equipo'], '') or ''),
            }
            if not _task_matches_filters(item, filtros):
                continue
            yield item


def _group_key(item, regla):
    rcm = item['rcm']
    equipo = rcm.equipo if getattr(rcm, 'equipo_id', None) else None
    parts = []
    if regla.agrupar_por_equipo:
        parts.append(('equipo', equipo.pk if equipo else ''))
    if regla.agrupar_por_ubicacion:
        parts.append(('ut', equipo.ut if equipo else ''))
    if regla.agrupar_por_frecuencia:
        parts.append(('frecuencia', item['frequency']))
    if regla.agrupar_por_especialidad:
        parts.append(('especialidad', item['specialty']))
    if regla.agrupar_por_estado_equipo:
        parts.append(('estado_equipo', item['estado_equipo']))
    return tuple(parts) or (('servicio', rcm.carga.servicio_id),)


def agrupar_tareas_rcm(queryset, regla, filtros=None):
    groups = OrderedDict()
    for item in _iter_groupable_tasks(queryset, regla, filtros=filtros):
        key = _group_key(item, regla)
        groups.setdefault(key, []).append(item)
    return groups


def pauta_group_id(group_key):
    raw = json.dumps(group_key, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


def preview_pautas_desde_rcm(servicio, estrategia=None, filtros=None, regla=None):
    regla = regla or RuntimeRule()
    qs = get_rcm_queryset_for_service(servicio, estrategia=estrategia, filtros=filtros)
    groups = agrupar_tareas_rcm(qs, regla, filtros=filtros)
    preview = []
    for key, registros in groups.items():
        first = registros[0]
        equipo = first['rcm'].equipo if first['rcm'].equipo_id else None
        tasks = []
        for item in registros:
            task = item['task']
            tasks.append({
                'id': str(task.pk),
                'actividad': item['activity'],
                'componente': item['component'],
                'frecuencia': item['frequency'],
                'especialidad': item['specialty'],
                'estado_equipo': item['estado_equipo'],
                'tipo': task.tipo_tarea_estrategia.nombre if task.tipo_tarea_estrategia_id else '',
            })
        preview.append({
            'key': key,
            'group_id': pauta_group_id(key),
            'equipo': equipo.nombre_equipo if equipo else '',
            'ubicacion_tecnica': equipo.ut if equipo else '',
            'frecuencia': first['frequency'],
            'especialidad': first['specialty'],
            'estado_equipo': first['estado_equipo'],
            'tareas_count': len(registros),
            'tasks': tasks,
        })
    return preview, groups


def _next_pauta_code(servicio):
    prefix = f'PAUTA-{servicio.pk}-'
    last = models.Pauta.objects.filter(servicio=servicio, codigo__startswith=prefix).order_by('-id').first()
    if not last:
        return f'{prefix}1'
    try:
        next_number = int(str(last.codigo).replace(prefix, '', 1)) + 1
    except ValueError:
        next_number = models.Pauta.objects.filter(servicio=servicio).count() + 1
    return f'{prefix}{next_number}'


def crear_pauta_desde_grupo(servicio, estrategia, grupo_key, registros, plantilla=None):
    first = registros[0]
    rcm = first['rcm']
    task = first['task']
    equipo = rcm.equipo if rcm.equipo_id else None
    duracion_total = _decimal_sum(_task_value(item['task'], ['duracion_hr', 'duracion_horas', 'tiempo'], None) for item in registros)
    hh_total = _decimal_sum(_task_value(item['task'], ['hh', 'hh_sap'], None) for item in registros)
    personas = _decimal_first(_task_value(item['task'], ['cantidad_personas', 'cant_personas', 'personas'], None) for item in registros)
    name_parts = [
        first['frequency'],
        first['specialty'],
        equipo.nombre_equipo if equipo else '',
    ]
    nombre = ' - '.join(str(part) for part in name_parts if part) or f'Pauta {servicio.codigo_servicio}'
    return models.Pauta.objects.create(
        servicio=servicio,
        estrategia=estrategia or servicio.estrategia,
        equipo=equipo,
        plantilla=plantilla,
        codigo=_next_pauta_code(servicio),
        nombre=nombre[:200],
        area=str(_task_value(task, ['area', 'area_negocio', 'planta'], '') or '')[:150],
        ubicacion_tecnica=equipo.ut if equipo else '',
        frecuencia=first['frequency'][:150],
        especialidad=first['specialty'][:100],
        estado_equipo=first['estado_equipo'][:100],
        estrategia_mantenimiento=str(_task_value(task, ['tactica', 'estrategia', 'estrategia_mantenimiento'], '') or '')[:150],
        cantidad_personas=personas,
        duracion_horas=duracion_total,
        hh_total=hh_total,
        origen=models.Pauta.ORIGEN_RCM,
        estado=models.Pauta.ESTADO_GENERADA,
    )


def crear_pauta_consolidada_desde_registros(servicio, estrategia, registros, plantilla=None):
    first = registros[0]
    first_task = first['task']
    equipos = [
        item['rcm'].equipo
        for item in registros
        if getattr(item['rcm'], 'equipo_id', None)
    ]
    unique_equipment = []
    for equipo in equipos:
        if equipo.pk not in [item.pk for item in unique_equipment]:
            unique_equipment.append(equipo)
    equipo = unique_equipment[0] if len(unique_equipment) == 1 else None
    frecuencia = _single_or_mixed(item['frequency'] for item in registros)
    especialidad = _single_or_mixed(item['specialty'] for item in registros)
    estado_equipo = _single_or_mixed(item['estado_equipo'] for item in registros)
    ubicacion_tecnica = (
        equipo.ut
        if equipo
        else _single_or_mixed(
            item['rcm'].equipo.ut
            for item in registros
            if getattr(item['rcm'], 'equipo_id', None)
        )
    )
    equipo_nombre = equipo.nombre_equipo if equipo else _single_or_mixed(
        item['rcm'].equipo.nombre_equipo
        for item in registros
        if getattr(item['rcm'], 'equipo_id', None)
    )
    duracion_total = _decimal_sum(_task_value(item['task'], ['duracion_hr', 'duracion_horas', 'tiempo'], None) for item in registros)
    hh_total = _decimal_sum(_task_value(item['task'], ['hh', 'hh_sap'], None) for item in registros)
    personas = _decimal_first(_task_value(item['task'], ['cantidad_personas', 'cant_personas', 'personas'], None) for item in registros)
    name_parts = [
        'Pauta consolidada',
        frecuencia,
        especialidad,
        equipo_nombre,
    ]
    nombre = ' - '.join(str(part) for part in name_parts if part) or f'Pauta consolidada {servicio.codigo_servicio}'
    return models.Pauta.objects.create(
        servicio=servicio,
        estrategia=estrategia or servicio.estrategia,
        equipo=equipo,
        plantilla=plantilla,
        codigo=_next_pauta_code(servicio),
        nombre=nombre[:200],
        area=str(_task_value(first_task, ['area', 'area_negocio', 'planta'], '') or '')[:150],
        ubicacion_tecnica=ubicacion_tecnica[:150],
        frecuencia=frecuencia[:150],
        especialidad=especialidad[:100],
        estado_equipo=estado_equipo[:100],
        estrategia_mantenimiento=str(_task_value(first_task, ['tactica', 'estrategia', 'estrategia_mantenimiento'], '') or '')[:150],
        cantidad_personas=personas,
        duracion_horas=duracion_total,
        hh_total=hh_total,
        origen=models.Pauta.ORIGEN_RCM,
        estado=models.Pauta.ESTADO_GENERADA,
    )


def crear_tareas_pauta(pauta, registros, incluir_primarias=True, incluir_secundarias=False):
    created = []
    for order, item in enumerate(registros, start=1):
        task = item['task']
        if _is_secondary_task(task):
            tipo_tarea = models.PautaTarea.TIPO_SECUNDARIA
        else:
            tipo_tarea = models.PautaTarea.TIPO_PRIMARIA
        created.append(models.PautaTarea.objects.create(
            pauta=pauta,
            orden=order,
            componente=item['component'][:200],
            actividad=item['activity'],
            limite_aceptable=str(_task_value(task, ['limite_aceptable', 'limite', 'criterio_aceptacion'], '') or ''),
            observacion=str(_task_value(task, ['observacion', 'observaciones', 'parametros'], '') or ''),
            tipo_tarea=tipo_tarea,
            origen_modelo='TareaRCM',
            origen_id=task.pk,
            frecuencia=item['frequency'][:150],
            pto_trabajo=item['specialty'][:100],
            cantidad_personas=_decimal_or_none(_task_value(task, ['cantidad_personas', 'cant_personas', 'personas'], None)),
            duracion_horas=_decimal_or_none(_task_value(task, ['duracion_hr', 'duracion_horas', 'tiempo'], None)),
            hh=_decimal_or_none(_task_value(task, ['hh', 'hh_sap'], None)),
            estado_equipo=item['estado_equipo'][:100],
        ))
    return created


@transaction.atomic
def generar_pautas_desde_rcm(
    servicio,
    estrategia=None,
    filtros=None,
    regla=None,
    plantilla=None,
    selected_group_ids=None,
    selected_task_ids=None,
    generar_una_pauta=False,
):
    regla = regla or RuntimeRule()
    selected_group_ids = set(selected_group_ids) if selected_group_ids is not None else None
    selected_task_ids = set(str(task_id) for task_id in selected_task_ids) if selected_task_ids is not None else None
    qs = get_rcm_queryset_for_service(servicio, estrategia=estrategia, filtros=filtros)
    groups = agrupar_tareas_rcm(qs, regla, filtros=filtros)
    created = []
    consolidated_records = []
    for key, registros in groups.items():
        if selected_group_ids is not None and pauta_group_id(key) not in selected_group_ids:
            continue
        if selected_task_ids is not None:
            registros = [
                item for item in registros
                if str(item['task'].pk) in selected_task_ids
            ]
        if not registros:
            continue
        if generar_una_pauta:
            consolidated_records.extend(registros)
            continue
        pauta = crear_pauta_desde_grupo(servicio, estrategia, key, registros, plantilla=plantilla)
        crear_tareas_pauta(
            pauta,
            registros,
            incluir_primarias=regla.incluir_tareas_primarias,
            incluir_secundarias=regla.incluir_tareas_secundarias,
        )
        created.append(pauta)
    if generar_una_pauta and consolidated_records:
        pauta = crear_pauta_consolidada_desde_registros(
            servicio,
            estrategia,
            consolidated_records,
            plantilla=plantilla,
        )
        crear_tareas_pauta(
            pauta,
            consolidated_records,
            incluir_primarias=regla.incluir_tareas_primarias,
            incluir_secundarias=regla.incluir_tareas_secundarias,
        )
        created.append(pauta)
    return created
