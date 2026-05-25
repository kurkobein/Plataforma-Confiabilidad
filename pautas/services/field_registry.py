from __future__ import annotations

from core import models


TECHNICAL_FIELD_NAMES = {
    'id',
    'password',
}


def _label(text):
    return str(text or '').replace('_', ' ').strip().capitalize()


def _field_label(prefix, field):
    return f'{prefix} > {_label(getattr(field, "verbose_name", None) or field.name)}'


def _model_field_options(model_cls, source_prefix, label_prefix, *, exclude=None, include_relations=False):
    exclude = set(exclude or ())
    fields = []
    for field in model_cls._meta.fields:
        if field.name in TECHNICAL_FIELD_NAMES or field.name in exclude:
            continue
        if field.is_relation and not include_relations:
            continue
        fields.append({
            'value': f'{source_prefix}.{field.name}',
            'label': _field_label(label_prefix, field),
        })
    return fields


def _strategy_from_context(servicio=None, estrategia=None):
    return estrategia or getattr(servicio, 'estrategia', None)


def _rcm_dimensions(estrategia):
    if not estrategia:
        return []
    process_values = [
        getattr(models.EstrategiaDimension, 'PROCESO_RCM', 'rcm'),
        getattr(models.EstrategiaDimension, 'PROCESO_AMBOS', 'ambos'),
        'rcm_fmea',
        'global',
    ]
    return (
        models.EstrategiaDimension.objects
        .filter(estrategia=estrategia, activo=True, proceso_uso__in=process_values)
        .select_related('dimension')
        .order_by('orden', 'id')
    )


def _evaluation_options(estrategia):
    fields = []
    for item in _rcm_dimensions(estrategia):
        name = getattr(item.dimension, 'nombre', '') or f'Dimension {item.pk}'
        fields.append({
            'value': f'evaluacion:{item.pk}',
            'label': f'Evaluacion RCM > {name}',
        })
    return fields


def _dynamic_task_field_options(estrategia):
    if not estrategia:
        return []
    queryset = (
        models.CampoTareaEstrategia.objects
        .filter(tipo_tarea_estrategia__estrategia=estrategia, activo=True)
        .select_related('tipo_tarea_estrategia')
        .order_by('tipo_tarea_estrategia__orden', 'orden', 'nombre', 'id')
    )
    fields = []
    for field in queryset:
        tipo = field.tipo_tarea_estrategia.nombre if field.tipo_tarea_estrategia_id else 'Tarea'
        fields.append({
            'value': f'tarea_campo:{field.pk}',
            'label': f'Campo Tarea RCM > {tipo} > {field.nombre}',
        })
    return fields


def _equipment_ut_level_options(servicio):
    empresa = getattr(servicio, 'empresa', None)
    if not empresa:
        return []
    levels = (
        models.NivelJerarquia.objects
        .filter(empresa=empresa, activo=True)
        .order_by('orden', 'id')
    )
    fields = []
    for level in levels:
        fields.extend([
            {
                'value': f'equipo_ut:{level.pk}:codigo',
                'label': f'Equipo > UT > {level.nombre} codigo',
            },
            {
                'value': f'equipo_ut:{level.pk}:nombre',
                'label': f'Equipo > UT > {level.nombre} nombre',
            },
        ])
    return fields


def _equipment_options(servicio):
    return [
        {'value': 'equipo.tag_equipo', 'label': 'Equipo > TAG'},
        {'value': 'equipo.tag_display', 'label': 'Equipo > TAG visible'},
        {'value': 'equipo.nombre_equipo', 'label': 'Equipo > Nombre'},
        {'value': 'equipo.ut', 'label': 'Equipo > Ubicacion tecnica'},
        {'value': 'equipo.descripcion_ut', 'label': 'Equipo > Descripcion UT'},
        *_equipment_ut_level_options(servicio),
    ]


def _group(name, fields):
    return {'group': name, 'fields': fields}


def get_pauta_header_field_options(servicio=None, estrategia=None):
    estrategia = _strategy_from_context(servicio, estrategia)
    return [
        _group('Equipo', _equipment_options(servicio)),
        _group('RCM', _model_field_options(models.RCM, 'rcm', 'RCM', exclude={'carga', 'equipo'})),
        _group('FMEA/FMECA', _model_field_options(models.FMEA_FMECA, 'fmea', 'FMEA/FMECA', exclude={'rcm'})),
        _group('Evaluaciones RCM', _evaluation_options(estrategia)),
        _group('Campos dinamicos tarea RCM', _dynamic_task_field_options(estrategia)),
    ]


def get_pauta_task_field_options(servicio=None, estrategia=None):
    estrategia = _strategy_from_context(servicio, estrategia)
    return [
        _group('Campos dinamicos tarea RCM', _dynamic_task_field_options(estrategia)),
        _group('RCM asociado', _model_field_options(models.RCM, 'rcm', 'RCM asociado', exclude={'carga', 'equipo'})),
        _group('Equipo', _equipment_options(servicio)),
    ]
