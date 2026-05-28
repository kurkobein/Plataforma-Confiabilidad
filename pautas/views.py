import json
import re
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from core import models
from pautas.forms import GenerarPautasForm, MapeoPlantillaPautaForm, PlantillaPautaForm
from pautas.services.exporter import (
    PautaExportError,
    generar_excel_pauta,
    listar_celdas_usadas,
    listar_grid_plantilla,
    listar_hojas_plantilla,
)
from pautas.services.field_registry import (
    get_pauta_header_field_options,
    get_pauta_task_field_options,
)
from pautas.services.generator import (
    build_runtime_rule,
    generar_pautas_desde_rcm,
    preview_pautas_desde_rcm,
)
from core.views import _service_or_404


def _export_value(value):
    if value is None:
        return ''
    if isinstance(value, Decimal):
        return format(value, 'f')
    if hasattr(value, 'strftime'):
        return value.strftime('%d/%m/%Y')
    return str(value)


def _service_pauta_template_queryset(servicio, active_only=False):
    qs = models.PlantillaPauta.objects.filter(
        Q(servicio=servicio)
        | Q(servicio__isnull=True, empresa=servicio.empresa)
        | Q(servicio__isnull=True, estrategia=servicio.estrategia)
        | Q(servicio__isnull=True, empresa__isnull=True, estrategia__isnull=True)
    ).select_related('empresa', 'servicio', 'estrategia').distinct()
    if active_only:
        qs = qs.filter(activa=True)
    return qs.order_by('-activa', 'nombre')


def _service_pauta_or_404(servicio, pauta_pk):
    return get_object_or_404(
        models.Pauta.objects.select_related(
            'servicio',
            'estrategia',
            'equipo',
            'plantilla',
        ).prefetch_related('tareas'),
        pk=pauta_pk,
        servicio=servicio,
    )


def _pauta_task_detail_rows(pauta):
    pauta_tasks = list(pauta.tareas.all().order_by('orden', 'id'))
    task_ids = [
        item.origen_id
        for item in pauta_tasks
        if item.origen_modelo == 'TareaRCM' and item.origen_id
    ]
    rcm_tasks = {
        item.pk: item
        for item in models.TareaRCM.objects.filter(pk__in=task_ids)
        .select_related('tipo_tarea_estrategia', 'fmea__rcm__equipo')
    }
    dynamic_values = {}
    for value in (
        models.ValorCampoTareaRCM.objects
        .filter(tarea_id__in=task_ids)
        .select_related('campo', 'campo__tipo_tarea_estrategia')
        .order_by('campo__tipo_tarea_estrategia__orden', 'campo__orden', 'campo__nombre', 'id')
    ):
        display = _export_value(value.valor_display)
        if display == '':
            continue
        dynamic_values.setdefault(value.tarea_id, []).append({
            'campo': value.campo.nombre,
            'tipo_tarea': value.campo.tipo_tarea_estrategia.nombre if value.campo and value.campo.tipo_tarea_estrategia_id else '',
            'valor': display,
        })

    rows = []
    for pauta_task in pauta_tasks:
        rcm_task = rcm_tasks.get(pauta_task.origen_id) if pauta_task.origen_modelo == 'TareaRCM' else None
        rcm = rcm_task.fmea.rcm if rcm_task and rcm_task.fmea_id else None
        rows.append({
            'tarea': pauta_task,
            'tarea_rcm': rcm_task,
            'rcm': rcm,
            'valores_dinamicos': dynamic_values.get(getattr(rcm_task, 'pk', None), []),
        })
    return rows


def _pauta_filters_from_form(form):
    return {
        'equipo': form.cleaned_data.get('equipo') or '',
        'frecuencia': form.cleaned_data.get('frecuencia') or '',
        'especialidad': form.cleaned_data.get('especialidad') or '',
        'estado_equipo': form.cleaned_data.get('estado_equipo') or '',
    }


@login_required
def service_pautas_list(request, pk):
    servicio, permission = _service_or_404(request, pk, edit=False)
    pautas = (
        models.Pauta.objects.filter(servicio=servicio)
        .select_related('equipo', 'plantilla')
        .prefetch_related('tareas')
        .order_by('-creado_en', 'codigo')
    )
    return render(request, 'core/pautas/service_pautas_list.html', {
        'service': servicio,
        'permission': permission,
        'pautas': pautas,
    })


@login_required
def service_pauta_detail(request, service_pk, pauta_pk):
    servicio, permission = _service_or_404(request, service_pk, edit=False)
    pauta = _service_pauta_or_404(servicio, pauta_pk)
    return render(request, 'core/pautas/service_pauta_detail.html', {
        'service': servicio,
        'permission': permission,
        'pauta': pauta,
        'task_rows': _pauta_task_detail_rows(pauta),
    })


@login_required
def service_pauta_templates(request, pk):
    servicio, permission = _service_or_404(request, pk, edit=True)
    if request.method == 'POST':
        form = PlantillaPautaForm(request.POST, request.FILES)
        if form.is_valid():
            plantilla = form.save(commit=False)
            plantilla.servicio = servicio
            plantilla.empresa = servicio.empresa
            plantilla.estrategia = servicio.estrategia
            plantilla.save()
            models.MapeoPlantillaPauta.objects.get_or_create(plantilla=plantilla)
            messages.success(request, 'Plantilla de pauta subida correctamente.')
            return redirect('service_pauta_template_mapping', service_pk=servicio.pk, template_pk=plantilla.pk)
    else:
        form = PlantillaPautaForm()

    return render(request, 'core/pautas/service_pauta_templates.html', {
        'service': servicio,
        'permission': permission,
        'form': form,
        'templates': _service_pauta_template_queryset(servicio),
    })


@login_required
def service_pauta_template_edit(request, service_pk, template_pk):
    servicio, permission = _service_or_404(request, service_pk, edit=True)
    plantilla = get_object_or_404(_service_pauta_template_queryset(servicio), pk=template_pk)
    if request.method == 'POST':
        form = PlantillaPautaForm(request.POST, request.FILES, instance=plantilla)
        if form.is_valid():
            plantilla = form.save(commit=False)
            if plantilla.servicio_id == servicio.pk:
                plantilla.empresa = servicio.empresa
                plantilla.estrategia = servicio.estrategia
            plantilla.save()
            messages.success(request, 'Plantilla de pauta actualizada correctamente.')
            return redirect('service_pauta_templates', pk=servicio.pk)
    else:
        form = PlantillaPautaForm(instance=plantilla)
    return render(request, 'core/pautas/service_pauta_template_form.html', {
        'service': servicio,
        'permission': permission,
        'plantilla': plantilla,
        'form': form,
    })


@login_required
def service_pauta_template_delete(request, service_pk, template_pk):
    servicio, permission = _service_or_404(request, service_pk, edit=True)
    plantilla = get_object_or_404(_service_pauta_template_queryset(servicio), pk=template_pk)
    pautas_count = models.Pauta.objects.filter(plantilla=plantilla).count()
    if request.method == 'POST':
        archivo = plantilla.archivo
        with transaction.atomic():
            models.Pauta.objects.filter(plantilla=plantilla).update(plantilla=None)
            models.MapeoPlantillaPauta.objects.filter(plantilla=plantilla).delete()
            plantilla.delete()
        if archivo:
            archivo.delete(save=False)
        messages.success(request, 'Plantilla de pauta eliminada correctamente.')
        return redirect('service_pauta_templates', pk=servicio.pk)
    return render(request, 'core/pautas/service_pauta_template_delete.html', {
        'service': servicio,
        'permission': permission,
        'plantilla': plantilla,
        'pautas_count': pautas_count,
    })


@login_required
def service_pauta_template_mapping(request, service_pk, template_pk):
    servicio, permission = _service_or_404(request, service_pk, edit=True)
    plantilla = get_object_or_404(_service_pauta_template_queryset(servicio), pk=template_pk)
    mapeo, _created = models.MapeoPlantillaPauta.objects.get_or_create(plantilla=plantilla)

    try:
        sheets = listar_hojas_plantilla(plantilla)
    except PautaExportError as exc:
        sheets = []
        messages.error(request, str(exc))

    if request.method == 'POST':
        form = MapeoPlantillaPautaForm(request.POST, instance=mapeo)
        if form.is_valid():
            form.save()
            messages.success(request, 'Mapeo de plantilla guardado correctamente.')
            return redirect('service_pauta_template_mapping', service_pk=servicio.pk, template_pk=plantilla.pk)
    else:
        form = MapeoPlantillaPautaForm(instance=mapeo)

    selected_sheet = request.GET.get('hoja') or mapeo.hoja_principal or (sheets[0] if sheets else '')
    used_cells = []
    sheet_grid = {'columns': [], 'rows': [], 'truncated_rows': False, 'truncated_cols': False}
    if selected_sheet:
        try:
            used_cells = listar_celdas_usadas(plantilla, selected_sheet)
            sheet_grid = listar_grid_plantilla(plantilla, selected_sheet)
        except PautaExportError as exc:
            messages.warning(request, str(exc))

    return render(request, 'core/pautas/service_pauta_template_mapping.html', {
        'service': servicio,
        'permission': permission,
        'plantilla': plantilla,
        'mapeo': mapeo,
        'form': form,
        'sheets': sheets,
        'selected_sheet': selected_sheet,
        'used_cells': used_cells,
        'header_field_options_json': json.dumps(
            get_pauta_header_field_options(servicio=servicio, estrategia=plantilla.estrategia or servicio.estrategia),
            ensure_ascii=False,
        ),
        'task_field_options_json': json.dumps(
            get_pauta_task_field_options(servicio=servicio, estrategia=plantilla.estrategia or servicio.estrategia),
            ensure_ascii=False,
        ),
        'sheet_grid': sheet_grid,
        'config_json': json.dumps(mapeo.config or {'celdas': [], 'tablas': []}, ensure_ascii=False),
    })


@login_required
def service_pautas_generate(request, pk):
    servicio, permission = _service_or_404(request, pk, edit=True)
    preview = None
    groups = None
    preview_group_ids = []
    preview_task_ids = []
    group_task_map = {}
    selected_group_ids_for_template = []
    selected_task_ids_for_template = []
    action = request.POST.get('action') if request.method == 'POST' else ''
    selected_group_ids = None
    selected_task_ids = None
    if action == 'confirm':
        selected_payload = request.POST.get('selected_groups_payload')
        if selected_payload:
            try:
                selected_group_ids = [str(group_id) for group_id in json.loads(selected_payload) if group_id]
            except (TypeError, ValueError, json.JSONDecodeError):
                selected_group_ids = request.POST.getlist('selected_groups')
        else:
            selected_group_ids = request.POST.getlist('selected_groups')
        selected_tasks_payload = request.POST.get('selected_tasks_payload')
        if selected_tasks_payload:
            try:
                selected_task_ids = [str(task_id) for task_id in json.loads(selected_tasks_payload) if task_id]
            except (TypeError, ValueError, json.JSONDecodeError):
                selected_task_ids = request.POST.getlist('selected_tasks')
        else:
            selected_task_ids = request.POST.getlist('selected_tasks')
    form = GenerarPautasForm(request.POST or None, service=servicio)

    if request.method == 'POST' and form.is_valid():
        estrategia = form.cleaned_data.get('estrategia') or servicio.estrategia
        plantilla = form.cleaned_data.get('plantilla')
        regla = build_runtime_rule(form.cleaned_data)
        generar_una_pauta = bool(form.cleaned_data.get('generar_una_pauta'))
        filtros = _pauta_filters_from_form(form)
        preview, groups = preview_pautas_desde_rcm(
            servicio,
            estrategia=estrategia,
            filtros=filtros,
            regla=regla,
        )
        if preview:
            preview_group_ids = [row.get('group_id') for row in preview if row.get('group_id')]
            for row in preview:
                task_ids = [
                    task.get('id') for task in row.get('tasks', [])
                    if task.get('id')
                ]
                group_task_map[row.get('group_id')] = task_ids
                preview_task_ids.extend(task_ids)
            if selected_group_ids is not None:
                selected_group_ids_for_template = [
                    group_id for group_id in selected_group_ids
                    if group_id in preview_group_ids
                ]
            else:
                selected_group_ids_for_template = preview_group_ids
            selected_lookup = set(selected_group_ids_for_template)
            if selected_task_ids is not None:
                selected_task_ids_for_template = [
                    task_id for task_id in selected_task_ids
                    if task_id in preview_task_ids
                ]
            else:
                selected_task_ids_for_template = preview_task_ids
            selected_task_lookup = set(selected_task_ids_for_template)
            for row in preview:
                row['selected'] = row.get('group_id') in selected_lookup
                for task in row.get('tasks', []):
                    task['selected'] = task.get('id') in selected_task_lookup

        if action == 'confirm':
            if not preview:
                messages.warning(request, 'No se encontraron tareas RCM primarias para generar pautas.')
            elif not selected_group_ids:
                messages.warning(request, 'Selecciona al menos una pauta para generar.')
            elif not selected_task_ids:
                messages.warning(request, 'Selecciona al menos una tarea para generar pautas.')
            else:
                pautas = generar_pautas_desde_rcm(
                    servicio,
                    estrategia=estrategia,
                    filtros=filtros,
                    regla=regla,
                    plantilla=plantilla,
                    selected_group_ids=selected_group_ids,
                    selected_task_ids=selected_task_ids,
                    generar_una_pauta=generar_una_pauta,
                )
                if pautas:
                    if generar_una_pauta:
                        messages.success(request, 'Se genero 1 pauta consolidada correctamente.')
                    else:
                        messages.success(request, f'Se generaron {len(pautas)} pautas correctamente.')
                    return redirect('service_pautas_list', pk=servicio.pk)
                messages.warning(request, 'No se pudo generar ninguna pauta con la seleccion actual.')

    return render(request, 'core/pautas/service_pautas_generate.html', {
        'service': servicio,
        'permission': permission,
        'form': form,
        'preview': preview,
        'groups': groups,
        'selected_group_ids': selected_group_ids,
        'preview_group_ids': preview_group_ids,
        'preview_task_ids': preview_task_ids,
        'group_task_map': group_task_map,
        'selected_group_ids_for_template': selected_group_ids_for_template,
        'selected_task_ids_for_template': selected_task_ids_for_template,
    })


@login_required
def service_pauta_export_excel(request, service_pk, pauta_pk):
    servicio, _permission = _service_or_404(request, service_pk, edit=False)
    pauta = _service_pauta_or_404(servicio, pauta_pk)
    try:
        output = generar_excel_pauta(pauta)
    except PautaExportError as exc:
        messages.error(request, str(exc))
        return redirect('service_pauta_detail', service_pk=servicio.pk, pauta_pk=pauta.pk)

    filename = re.sub(r'[^A-Za-z0-9_-]+', '_', pauta.codigo or f'pauta_{pauta.pk}').strip('_')
    response = HttpResponse(
        output.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}.xlsx"'
    return response
