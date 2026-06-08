import json
import re
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from core import models
from core.access import get_accessible_services
from rcm.forms import RCMRegistroForm, RCMTaskFormSet
from core.views import (
    _decimal_or_none,
    _equipment_items_payload,
    _export_filename,
    _export_pdf_response,
    _export_value,
    _export_xlsx_response,
    _json_loads_safe,
    _json_payload,
    _json_safe,
    _service_equipment_browser_payload,
    _service_equipment_endpoints,
    _service_or_404,
)


@login_required
def rcm_index(request):
    servicios = get_accessible_services(request.user)
    if servicios.count() == 1:
        return redirect('service_rcm_list', pk=servicios.first().pk)
    messages.info(request, 'Selecciona un servicio para ver sus registros RCM.')
    return redirect('service_list')


def _safe_task_slug(value, fallback='campo'):
    text = str(value or '').strip().lower()
    text = re.sub(r'[^a-z0-9áéíóúñ]+', '_', text, flags=re.IGNORECASE)
    return text.strip('_')[:100] or fallback


def _task_field_options_json(values):
    if isinstance(values, list):
        cleaned = [str(item).strip() for item in values if str(item).strip()]
        return json.dumps(cleaned, ensure_ascii=False) if cleaned else ''
    text = str(values or '').strip()
    if not text:
        return ''
    cleaned = [item.strip() for item in re.split(r'[\n,;]+', text) if item.strip()]
    return json.dumps(cleaned, ensure_ascii=False) if cleaned else ''


def _task_field_options_list(raw):
    parsed = _json_loads_safe(raw, [])
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []


def _service_family_payload(servicio):
    payload = []
    familias = (
        models.FamiliaEquipo.objects.filter(servicio=servicio, activa=True)
        .prefetch_related('items__equipo')
        .order_by('nombre')
    )
    for familia in familias:
        equipos = [item.equipo for item in familia.items.all()]
        payload.append({
            'id': familia.pk,
            'nombre': familia.nombre,
            'equipos': _equipment_items_payload(equipos),
        })
    return payload


def _task_config_payload(estrategia):
    tipos = (
        models.TipoTareaEstrategia.objects.filter(estrategia=estrategia, activo=True)
        .prefetch_related('campos')
        .order_by('orden', 'nombre')
    )
    payload = []
    for tipo in tipos:
        payload.append({
            'id': tipo.pk,
            'nombre': tipo.nombre,
            'codigo': tipo.codigo,
            'orden': tipo.orden,
            'activo': tipo.activo,
            'campos': [
                {
                    'id': campo.pk,
                    'nombre': campo.nombre,
                    'clave': campo.clave,
                    'tipo_dato': campo.tipo_dato,
                    'opciones': _task_field_options_list(campo.opciones_json),
                    'obligatorio': campo.obligatorio,
                    'orden': campo.orden,
                    'activo': campo.activo,
                }
                for campo in tipo.campos.all().order_by('orden', 'nombre')
                if campo.activo
            ],
        })
    return payload


def _record_attachment_payload(request):
    files = request.FILES.getlist('adjuntos')
    if not files:
        return [], []
    allowed = set(models.RECORD_ATTACHMENT_EXTENSIONS)
    invalid = []
    payload = []
    for uploaded in files:
        extension = (uploaded.name.rsplit('.', 1)[-1] if '.' in uploaded.name else '').lower()
        if extension not in allowed:
            invalid.append(uploaded.name)
            continue
        payload.append({
            'name': uploaded.name,
            'content': uploaded.read(),
        })
    return payload, invalid


def _save_rcm_attachments(rcm, attachment_payload, usuario):
    now = timezone.now()
    for item in attachment_payload:
        adjunto = models.RCMAdjunto(
            rcm=rcm,
            nombre_original=item['name'],
            creado_en=now,
            usuario=usuario,
        )
        adjunto.archivo.save(item['name'], ContentFile(item['content']), save=False)
        adjunto.save()


def _save_task_config(estrategia, payload):
    payload = payload if isinstance(payload, list) else []
    existing_types = {
        str(item.pk): item
        for item in models.TipoTareaEstrategia.objects.filter(estrategia=estrategia).prefetch_related('campos')
    }
    existing_types_by_code = {
        item.codigo: item
        for item in existing_types.values()
    }
    keep_type_ids = set()
    valid_field_types = dict(models.CampoTareaEstrategia.TIPO_DATO_CHOICES)

    def unique_code(base_code):
        base_code = (base_code or 'tarea')[:80]
        candidate = base_code
        stem = base_code[:74]
        suffix = 2
        while candidate in existing_types_by_code:
            candidate = f'{stem}_{suffix}'[:80]
            suffix += 1
        return candidate

    for type_idx, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            continue
        nombre = str(item.get('nombre') or '').strip() or f'Tarea {type_idx}'
        requested_code = _safe_task_slug(item.get('codigo') or nombre, f'tarea_{type_idx}')[:80]
        active = item.get('activo', True) is not False
        raw_id = str(item.get('id') or '').strip()

        tipo = existing_types.get(raw_id)
        if not tipo:
            tipo = models.TipoTareaEstrategia(estrategia=estrategia)
            tipo.codigo = unique_code(requested_code)
        tipo.nombre = nombre
        tipo.orden = type_idx
        tipo.activo = active
        tipo.save()
        existing_types_by_code[tipo.codigo] = tipo
        keep_type_ids.add(tipo.pk)

        existing_fields = {
            str(field.pk): field
            for field in tipo.campos.all()
        }
        existing_fields_by_key = {
            field.clave: field
            for field in existing_fields.values()
        }
        keep_field_ids = set()
        fields = item.get('campos') if isinstance(item.get('campos'), list) else []
        for field_idx, field_data in enumerate(fields, start=1):
            if not isinstance(field_data, dict):
                continue
            field_name = str(field_data.get('nombre') or '').strip() or f'Campo {field_idx}'
            field_key = _safe_task_slug(field_data.get('clave') or field_name, f'campo_{field_idx}')
            tipo_dato = str(field_data.get('tipo_dato') or models.CampoTareaEstrategia.TIPO_TEXTO).strip()
            if tipo_dato not in valid_field_types:
                tipo_dato = models.CampoTareaEstrategia.TIPO_TEXTO
            raw_field_id = str(field_data.get('id') or '').strip()
            campo = existing_fields.get(raw_field_id) or existing_fields_by_key.get(field_key)
            if not campo:
                campo = models.CampoTareaEstrategia(tipo_tarea_estrategia=tipo)
            campo.nombre = field_name
            campo.clave = field_key
            campo.tipo_dato = tipo_dato
            campo.opciones_json = _task_field_options_json(field_data.get('opciones'))
            campo.obligatorio = field_data.get('obligatorio', False) is True
            campo.orden = field_idx
            campo.activo = field_data.get('activo', True) is not False
            campo.save()
            keep_field_ids.add(campo.pk)

        tipo.campos.exclude(pk__in=keep_field_ids).update(activo=False)

    models.TipoTareaEstrategia.objects.filter(estrategia=estrategia).exclude(pk__in=keep_type_ids).update(activo=False)


@login_required
def service_rcm_task_config(request, pk):
    servicio, permission = _service_or_404(request, pk, edit=True)
    if not servicio.estrategia_id:
        messages.warning(request, 'El servicio debe tener una estrategia antes de configurar tareas RCM.')
        return redirect('service_detail', pk=servicio.pk)

    if request.method == 'POST':
        payload = _json_payload(request, 'payload_json', []) or []
        with transaction.atomic():
            _save_task_config(servicio.estrategia, payload)
        messages.success(request, 'La configuración de tareas RCM se guardó correctamente.')
        return redirect('service_rcm_task_config', pk=servicio.pk)

    return render(request, 'core/rcm/service_rcm_task_config.html', {
        'service': servicio,
        'permission': permission,
        'task_payload_json': json.dumps(_json_safe(_task_config_payload(servicio.estrategia)), ensure_ascii=False),
        'field_types': models.CampoTareaEstrategia.TIPO_DATO_CHOICES,
    })


@login_required
def service_rcm_list(request, pk):
    servicio, permission = _service_or_404(request, pk, edit=False)
    rows, rcm_count, fmea_count, fmeca_count = _service_rcm_rows(servicio)

    return render(request, 'core/rcm/service_rcm_list.html', {
        'service': servicio,
        'permission': permission,
        'rows': rows,
        'rcm_count': rcm_count,
        'fmea_count': fmea_count,
        'fmeca_count': fmeca_count,
    })


def _service_rcm_rows(servicio):
    registros_qs = (
        models.RCM.objects.filter(carga__servicio=servicio)
        .select_related('carga', 'equipo', 'fmea_fmeca')
        .prefetch_related(
            'fmea_fmeca__evaluaciones__estrategia_dimension__dimension',
            'fmea_fmeca__tareas_rcm__tipo_tarea_estrategia',
        )
        .order_by('-fecha_analisis', '-id')
    )
    rows = []
    for registro in registros_qs:
        try:
            fmea = registro.fmea_fmeca
        except models.FMEA_FMECA.DoesNotExist:
            fmea = None
        evaluations = []
        tasks = []
        if fmea:
            evaluations = list(
                fmea.evaluaciones.select_related('estrategia_dimension__dimension').order_by(
                    'estrategia_dimension__orden',
                    'estrategia_dimension_id',
                )
            )
            tasks = list(
                fmea.tareas_rcm.select_related(
                    'tipo_tarea_estrategia',
                ).order_by('orden', 'id')
            )
        rows.append({
            'registro': registro,
            'fmea': fmea,
            'evaluations': evaluations,
            'tasks': tasks,
        })

    return (
        rows,
        registros_qs.count(),
        registros_qs.filter(criticidad__isnull=True).count(),
        registros_qs.filter(criticidad__isnull=False).count(),
    )


def _service_rcm_export_data(servicio):
    rows, _rcm_count, _fmea_count, _fmeca_count = _service_rcm_rows(servicio)
    columns = [
        ('fecha_creacion', 'Fecha creación'),
        ('fecha_modificacion', 'Fecha modificación'),
        ('tipo', 'Tipo'),
        ('estado', 'Estado'),
        ('ubicacion_tecnica', 'U.T.'),
        ('equipo', 'Equipo'),
        ('tag', 'TAG'),
        ('componente', 'Componente'),
        ('funcion', 'Función'),
        ('falla_funcional', 'Falla funcional'),
        ('modo_de_falla', 'Modo de falla'),
        ('efecto', 'Efecto'),
        ('observacion', 'Observación'),
        ('criticidad', 'Criticidad'),
        ('evaluacion', 'Evaluación'),
        ('tareas', 'Tareas'),
    ]
    records = []
    for item in rows:
        registro = item['registro']
        evaluations = [
            f'{evaluation.estrategia_dimension.dimension.nombre}: {_export_value(evaluation.valor_display) or "-"}'
            for evaluation in item['evaluations']
        ]
        tasks = [
            f'{task.tipo_tarea_estrategia.nombre}: {task.descripcion}'
            for task in item['tasks']
        ]
        records.append({
            'fecha_creacion': registro.carga.creado_en if registro.carga_id else '',
            'fecha_modificacion': registro.carga.actualizado if registro.carga_id else '',
            'tipo': registro.tipo_analisis,
            'estado': registro.get_estado_display(),
            'ubicacion_tecnica': registro.equipo.ut_display if registro.equipo else '',
            'equipo': registro.equipo.nombre_equipo if registro.equipo else '',
            'tag': registro.equipo.tag_display if registro.equipo else '',
            'componente': registro.componente or '',
            'funcion': registro.funcion or '',
            'falla_funcional': registro.falla_funcional,
            'modo_de_falla': registro.modo_de_falla,
            'efecto': registro.efecto,
            'observacion': registro.observacion,
            'criticidad': registro.criticidad,
            'evaluacion': '\n'.join(evaluations),
            'tareas': '\n'.join(tasks),
        })
    return columns, records


@login_required
def service_rcm_export(request, pk, formato):
    servicio, _permission = _service_or_404(request, pk, edit=False)
    columns, rows = _service_rcm_export_data(servicio)
    formato = (formato or '').lower()
    if formato == 'excel':
        return _export_xlsx_response(
            _export_filename('RCM', servicio, 'xlsx'),
            'Registros RCM',
            columns,
            rows,
        )
    if formato == 'pdf':
        return _export_pdf_response(
            _export_filename('RCM', servicio, 'pdf'),
            f'Registros RCM - {servicio.codigo_servicio}',
            columns,
            rows,
        )
    raise Http404('Formato de exportación no soportado.')


def _format_rcm_timestamp(value):
    if not value:
        return ''
    if hasattr(value, 'hour'):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime('%d/%m/%Y %H:%M')
    if hasattr(value, 'strftime'):
        return value.strftime('%d/%m/%Y')
    return str(value)


def _rcm_date_meta(rcm=None):
    now = timezone.localtime(timezone.now())
    if rcm and rcm.carga_id:
        return {
            'creado_en': _format_rcm_timestamp(rcm.carga.creado_en),
            'actualizado': _format_rcm_timestamp(rcm.carga.actualizado),
        }
    return {
        'creado_en': _format_rcm_timestamp(now),
        'actualizado': _format_rcm_timestamp(now),
    }


def _rcm_form_initial_from_instance(rcm):
    return {
        'equipo': rcm.equipo_id,
        'fecha_analisis': rcm.fecha_analisis,
        'estado': rcm.estado,
        'criticidad': rcm.criticidad,
        'componente': rcm.componente or '',
        'funcion': rcm.funcion or '',
        'falla_funcional': rcm.falla_funcional,
        'modo_de_falla': rcm.modo_de_falla,
        'efecto': rcm.efecto,
        'observacion': rcm.observacion,
    }


def _rcm_task_initials(rcm):
    try:
        fmea = rcm.fmea_fmeca
    except models.FMEA_FMECA.DoesNotExist:
        return [{}]
    tasks = list(
        fmea.tareas_rcm.select_related(
            'tipo_tarea_estrategia',
        ).order_by('orden', 'id')
    )
    initials = []
    for task in tasks:
        dynamic_values = {
            value.campo.clave: str(_json_safe(value.valor_display))
            for value in task.valores_campos.select_related('campo').all()
            if value.valor_display not in (None, '')
        }
        initials.append({
            'id': task.pk,
            'valores_json': json.dumps(dynamic_values, ensure_ascii=False),
            'tipo_tarea_estrategia': task.tipo_tarea_estrategia_id,
            'descripcion': task.descripcion,
            'tactica': task.tactica,
            'limite_aceptable': task.limite_aceptable,
            'parametros': task.parametros,
            'riesgo_material': task.riesgo_material,
            'especialidad': task.especialidad,
            'puesto_trabajo': task.puesto_trabajo,
            'estado_equipo': task.estado_equipo,
            'frecuencia_valor': task.frecuencia_valor,
            'frecuencia_unidad': task.frecuencia_unidad,
            'frecuencia_texto': task.frecuencia_texto,
            'duracion_min': task.duracion_min,
            'duracion_hr': task.duracion_hr,
            'cantidad_personas': task.cantidad_personas,
            'hh': task.hh,
            'plan_sap': task.plan_sap,
            'descripcion_plan': task.descripcion_plan,
            'hoja_ruta': task.hoja_ruta,
            'texto_hoja_ruta': task.texto_hoja_ruta,
            'operacion_hoja_ruta': task.operacion_hoja_ruta,
            'texto_operacion': task.texto_operacion,
            'operacion_pauta': task.operacion_pauta,
            'pauta': task.pauta,
            'titulo_pauta': task.titulo_pauta,
            'repuesto': task.repuesto,
            'componente_involucrado': task.componente_involucrado,
            'numero_parte': task.numero_parte,
            'numero_sap': task.numero_sap,
            'procedimiento_trabajo': task.procedimiento_trabajo,
            'costo_hh': task.costo_hh,
            'costo_repuestos': task.costo_repuestos,
            'tarifa_servicios': task.tarifa_servicios,
            'costo_total': task.costo_total,
            'oportunidad_mejora': task.oportunidad_mejora,
            'estado': task.estado,
        })
    initials.append({})
    return initials


def _rcm_task_formset(request, servicio, rcm=None):
    initial = _rcm_task_initials(rcm) if rcm else [{}]
    return RCMTaskFormSet(
        request.POST or None,
        prefix='tasks',
        initial=initial,
        form_kwargs={'estrategia': servicio.estrategia},
    )


_TASK_TEXT_FIELDS = {
    'descripcion',
    'tactica',
    'limite_aceptable',
    'parametros',
    'riesgo_material',
    'especialidad',
    'puesto_trabajo',
    'estado_equipo',
    'frecuencia_unidad',
    'frecuencia_texto',
    'plan_sap',
    'descripcion_plan',
    'hoja_ruta',
    'texto_hoja_ruta',
    'operacion_hoja_ruta',
    'texto_operacion',
    'operacion_pauta',
    'pauta',
    'titulo_pauta',
    'repuesto',
    'componente_involucrado',
    'numero_parte',
    'numero_sap',
    'procedimiento_trabajo',
    'oportunidad_mejora',
}
_TASK_DECIMAL_FIELDS = {
    'frecuencia_valor',
    'duracion_min',
    'duracion_hr',
    'cantidad_personas',
    'hh',
    'costo_hh',
    'costo_repuestos',
    'tarifa_servicios',
    'costo_total',
}


def _task_config_for_type(tipo_tarea):
    if not tipo_tarea:
        return []
    return list(tipo_tarea.campos.filter(activo=True).order_by('orden', 'nombre'))


def _task_display_from_dynamic(fields, values):
    for field in fields:
        value = values.get(field.clave)
        if value not in (None, '', []):
            return str(value)
    return ''


def _apply_task_dynamic_values_to_fixed_fields(task, fields, values, cleaned):
    for name in _TASK_TEXT_FIELDS:
        setattr(task, name, cleaned.get(name) or '')
    for name in _TASK_DECIMAL_FIELDS:
        setattr(task, name, cleaned.get(name))

    for field in fields:
        target = field.clave
        if target not in _TASK_TEXT_FIELDS and target not in _TASK_DECIMAL_FIELDS:
            continue
        raw = values.get(field.clave)
        if raw in (None, '', []):
            continue
        if target in _TASK_DECIMAL_FIELDS:
            parsed = _decimal_or_none(raw)
            if parsed is not None:
                setattr(task, target, parsed)
        else:
            setattr(task, target, str(raw))

    if not task.descripcion:
        task.descripcion = _task_display_from_dynamic(fields, values) or cleaned['tipo_tarea_estrategia'].nombre


def _save_task_dynamic_values(task, fields, values):
    keep_ids = []
    for field in fields:
        raw = values.get(field.clave)
        if raw in (None, '', []):
            continue
        defaults = {
            'valor_texto': '',
            'valor_numero': None,
            'valor_booleano': None,
            'valor_fecha': None,
        }
        if field.tipo_dato in {
            models.CampoTareaEstrategia.TIPO_NUMERO,
            models.CampoTareaEstrategia.TIPO_DECIMAL,
        }:
            defaults['valor_numero'] = _decimal_or_none(raw)
            if defaults['valor_numero'] is None:
                defaults['valor_texto'] = str(raw)
        elif field.tipo_dato == models.CampoTareaEstrategia.TIPO_BOOLEANO:
            defaults['valor_booleano'] = str(raw).lower() in {'1', 'true', 'si', 'sí', 'on', 'yes'}
        elif field.tipo_dato == models.CampoTareaEstrategia.TIPO_FECHA:
            defaults['valor_fecha'] = parse_date(str(raw))
            if defaults['valor_fecha'] is None:
                defaults['valor_texto'] = str(raw)
        else:
            defaults['valor_texto'] = str(raw)

        value_obj, _ = models.ValorCampoTareaRCM.objects.update_or_create(
            tarea=task,
            campo=field,
            defaults=defaults,
        )
        keep_ids.append(value_obj.pk)

    task.valores_campos.exclude(pk__in=keep_ids).delete()


def _save_rcm_tasks(fmea, task_formset):
    saved_ids = []
    existing_tasks = {
        item.pk: item
        for item in fmea.tareas_rcm.all()
    }

    active_forms = []
    for form_idx, task_form in enumerate(task_formset.forms, start=1):
        if not hasattr(task_form, 'cleaned_data'):
            continue
        cleaned = task_form.cleaned_data
        task_id = cleaned.get('id')
        if cleaned.get('DELETE'):
            if task_id and task_id in existing_tasks:
                models.ValorCampoTareaRCM.objects.filter(tarea=existing_tasks[task_id]).delete()
                existing_tasks[task_id].delete()
            continue
        if getattr(task_form, 'empty_task', False):
            continue
        sort_order = cleaned['tipo_tarea_estrategia'].orden or form_idx
        active_forms.append((sort_order, form_idx, task_form))

    active_forms.sort(key=lambda item: (item[0], item[1]))
    for order, (_sort_order, _form_idx, task_form) in enumerate(active_forms, start=1):
        cleaned = task_form.cleaned_data
        task_id = cleaned.get('id')
        configured_fields = _task_config_for_type(cleaned['tipo_tarea_estrategia'])
        dynamic_values = cleaned.get('dynamic_values') or {}
        task = existing_tasks.get(task_id) if task_id else models.TareaRCM(fmea=fmea)
        task.fmea = fmea
        task.tipo_tarea_estrategia = cleaned['tipo_tarea_estrategia']
        _apply_task_dynamic_values_to_fixed_fields(task, configured_fields, dynamic_values, cleaned)
        task.orden = order
        task.estado = cleaned.get('estado') or models.TareaRCM.ESTADO_ACTIVO
        task.save()
        _save_task_dynamic_values(task, configured_fields, dynamic_values)
        saved_ids.append(task.pk)

    stale_tasks = fmea.tareas_rcm.exclude(pk__in=saved_ids)
    models.ValorCampoTareaRCM.objects.filter(tarea__in=stale_tasks).delete()
    stale_tasks.delete()


def _save_rcm_form(servicio, permission, form, task_formset=None, rcm=None, equipo=None, attachment_payload=None):
    cleaned = form.cleaned_data
    now = timezone.now()
    selected_equipment = equipo or cleaned['equipo']
    attachment_payload = attachment_payload or []

    if rcm:
        carga = rcm.carga
        carga.fecha_analisis = cleaned['fecha_analisis']
        carga.status = cleaned['estado']
        carga.actualizado = now
        carga.estrategia = servicio.estrategia
        carga.servicio = servicio
        if permission.get('profile'):
            carga.usuario = permission.get('profile')
        carga.save(update_fields=['fecha_analisis', 'status', 'actualizado', 'estrategia', 'servicio', 'usuario'])

        rcm.equipo = selected_equipment
        rcm.criticidad = cleaned.get('criticidad')
        rcm.fecha_analisis = cleaned['fecha_analisis']
        rcm.estado = cleaned['estado']
        rcm.componente = cleaned.get('componente') or ''
        rcm.funcion = cleaned.get('funcion') or ''
        rcm.falla_funcional = cleaned['falla_funcional']
        rcm.modo_de_falla = cleaned['modo_de_falla']
        rcm.causa = ''
        rcm.efecto = cleaned['efecto']
        rcm.observacion = cleaned.get('observacion') or ''
        rcm.save(update_fields=[
            'equipo',
            'criticidad',
            'fecha_analisis',
            'estado',
            'componente',
            'funcion',
            'falla_funcional',
            'modo_de_falla',
            'causa',
            'efecto',
            'observacion',
        ])
    else:
        carga = models.Carga.objects.create(
            fecha_analisis=cleaned['fecha_analisis'],
            version_carga=Decimal('1.0'),
            usuario=permission.get('profile'),
            servicio=servicio,
            estrategia=servicio.estrategia,
            origen='RCM Manual',
            status=cleaned['estado'],
            creado_en=now,
            actualizado=now,
        )
        rcm = models.RCM.objects.create(
            carga=carga,
            equipo=selected_equipment,
            criticidad=cleaned.get('criticidad'),
            fecha_analisis=cleaned['fecha_analisis'],
            estado=cleaned['estado'],
            componente=cleaned.get('componente') or '',
            funcion=cleaned.get('funcion') or '',
            falla_funcional=cleaned['falla_funcional'],
            modo_de_falla=cleaned['modo_de_falla'],
            causa='',
            efecto=cleaned['efecto'],
            observacion=cleaned.get('observacion') or '',
        )

    fmea, _created = models.FMEA_FMECA.objects.get_or_create(rcm=rcm)

    saved_dimension_ids = []
    for evaluation in getattr(form, 'cleaned_dimension_evaluations', []):
        estrategia_dimension = evaluation['estrategia_dimension']
        saved_dimension_ids.append(estrategia_dimension.pk)
        models.EvaluacionFMEA.objects.update_or_create(
            fmea=fmea,
            estrategia_dimension=estrategia_dimension,
            defaults={
                'valor_numerico': evaluation.get('valor_numerico'),
                'valor_texto': evaluation.get('valor_texto') or '',
                'catalogo_fila': evaluation.get('catalogo_fila'),
                'escala_valor': evaluation.get('escala_valor'),
            },
        )
    models.EvaluacionFMEA.objects.filter(fmea=fmea).exclude(
        estrategia_dimension_id__in=saved_dimension_ids,
    ).delete()
    if task_formset is not None:
        _save_rcm_tasks(fmea, task_formset)
    _save_rcm_attachments(rcm, attachment_payload, permission.get('profile'))
    return rcm


@login_required
def service_rcm_new(request, pk):
    servicio, permission = _service_or_404(request, pk, edit=True)
    form = RCMRegistroForm(
        request.POST or None,
        service=servicio,
        initial={
            'fecha_analisis': timezone.localdate(),
            'estado': models.Carga.STATUS_COMPLETO,
        },
    )
    task_formset = _rcm_task_formset(request, servicio)

    if request.method == 'POST' and form.is_valid() and task_formset.is_valid():
        attachment_payload, invalid_attachments = _record_attachment_payload(request)
        if invalid_attachments:
            form.add_error(
                None,
                'Formato de archivo no permitido: '
                + ', '.join(invalid_attachments)
                + '. Usa PDF, Word, Excel, PowerPoint, CSV, TXT, imagen o ZIP.',
            )
        else:
            with transaction.atomic():
                familia = form.cleaned_data.get('familia_equipo')
                if familia:
                    equipos = [item.equipo for item in familia.items.select_related('equipo').order_by('orden', 'id')]
                    for equipo in equipos:
                        _save_rcm_form(
                            servicio,
                            permission,
                            form,
                            task_formset=task_formset,
                            equipo=equipo,
                            attachment_payload=attachment_payload,
                        )
                    messages.success(request, f'Se crearon {len(equipos)} registros RCM para la familia {familia.nombre}.')
                else:
                    _save_rcm_form(servicio, permission, form, task_formset=task_formset, attachment_payload=attachment_payload)
                    messages.success(request, 'Registro RCM creado correctamente.')
            return redirect('service_rcm_list', pk=servicio.pk)

    return render(request, 'core/rcm/service_rcm_form.html', {
        'service': servicio,
        'permission': permission,
        'form': form,
        'task_formset': task_formset,
        'task_types_available': models.TipoTareaEstrategia.objects.filter(estrategia=servicio.estrategia, activo=True).exists(),
        'task_field_config_payload': json.dumps(_json_safe(_task_config_payload(servicio.estrategia)), ensure_ascii=False) if servicio.estrategia_id else '[]',
        'editing_rcm': None,
        'rcm_date_meta': _rcm_date_meta(),
        'form_title': 'Nuevo registro RCM',
        'submit_label': 'Guardar registro RCM',
        'service_equipment_payload': _service_equipment_browser_payload(servicio),
        'service_equipment_endpoints': _service_equipment_endpoints(servicio),
        'service_family_payload': _service_family_payload(servicio),
        'existing_attachments': [],
    })


@login_required
def service_rcm_edit(request, service_pk, rcm_pk):
    servicio, permission = _service_or_404(request, service_pk, edit=True)
    rcm = get_object_or_404(
        models.RCM.objects.select_related('carga', 'equipo', 'fmea_fmeca').prefetch_related('fmea_fmeca__evaluaciones'),
        pk=rcm_pk,
        carga__servicio=servicio,
    )
    form = RCMRegistroForm(
        request.POST or None,
        service=servicio,
        rcm=rcm,
        initial=_rcm_form_initial_from_instance(rcm),
    )
    task_formset = _rcm_task_formset(request, servicio, rcm=rcm)

    if request.method == 'POST' and form.is_valid() and task_formset.is_valid():
        attachment_payload, invalid_attachments = _record_attachment_payload(request)
        if invalid_attachments:
            form.add_error(
                None,
                'Formato de archivo no permitido: '
                + ', '.join(invalid_attachments)
                + '. Usa PDF, Word, Excel, PowerPoint, CSV, TXT, imagen o ZIP.',
            )
        else:
            with transaction.atomic():
                _save_rcm_form(
                    servicio,
                    permission,
                    form,
                    task_formset=task_formset,
                    rcm=rcm,
                    attachment_payload=attachment_payload,
                )
            messages.success(request, 'Registro RCM actualizado correctamente.')
            return redirect('service_rcm_list', pk=servicio.pk)

    return render(request, 'core/rcm/service_rcm_form.html', {
        'service': servicio,
        'permission': permission,
        'form': form,
        'task_formset': task_formset,
        'task_types_available': models.TipoTareaEstrategia.objects.filter(estrategia=servicio.estrategia, activo=True).exists(),
        'task_field_config_payload': json.dumps(_json_safe(_task_config_payload(servicio.estrategia)), ensure_ascii=False) if servicio.estrategia_id else '[]',
        'editing_rcm': rcm,
        'rcm_date_meta': _rcm_date_meta(rcm),
        'form_title': 'Editar registro RCM',
        'submit_label': 'Actualizar registro RCM',
        'service_equipment_payload': _service_equipment_browser_payload(servicio),
        'service_equipment_endpoints': _service_equipment_endpoints(servicio),
        'service_family_payload': _service_family_payload(servicio),
        'existing_attachments': rcm.adjuntos.all(),
    })


@login_required
@transaction.atomic
def service_rcm_delete(request, service_pk, rcm_pk):
    servicio, permission = _service_or_404(request, service_pk, edit=True)
    rcm = get_object_or_404(
        models.RCM.objects.select_related('carga'),
        pk=rcm_pk,
        carga__servicio=servicio,
    )

    if request.method == 'POST':
        carga = rcm.carga
        models.ValorCampoTareaRCM.objects.filter(tarea__fmea__rcm=rcm).delete()
        models.TareaRCM.objects.filter(fmea__rcm=rcm).delete()
        models.EvaluacionFMEA.objects.filter(fmea__rcm=rcm).delete()
        models.FMEA_FMECA.objects.filter(rcm=rcm).delete()
        rcm.delete()
        if carga and not carga.criticidades.exists() and not models.RCM.objects.filter(carga=carga).exists():
            carga.delete()
        messages.success(request, 'Registro RCM eliminado correctamente.')
        return redirect('service_rcm_list', pk=servicio.pk)

    return redirect('service_rcm_list', pk=servicio.pk)
