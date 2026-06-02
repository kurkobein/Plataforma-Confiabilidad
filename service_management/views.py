import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core import models
from core.access import get_accessible_services, get_service_equipment, is_mindco_user
from service_management.forms import FamiliaEquipoForm, ServiceAccessGrantForm
from core.views import (
    _catalog_preview,
    _equipment_items_payload,
    _is_generated_matrix_axis_dimension,
    _service_equipment_browser_payload,
    _service_equipment_count,
    _service_equipment_endpoints,
    _service_equipment_search_queryset,
    _service_or_404,
    _strategy_dimensions,
)


_CALC_OPERATION_LABELS = {
    'suma': 'Suma',
    'resta': 'Resta',
    'multiplicacion': 'Multiplicacion',
    'division': 'Division',
    'maximo': 'Maximo',
    'máximo': 'Maximo',
    'minimo': 'Minimo',
    'mínimo': 'Minimo',
}

_CALC_OPERATION_SYMBOLS = {
    'suma': ' + ',
    'resta': ' - ',
    'multiplicacion': ' x ',
    'division': ' / ',
    'maximo': ', ',
    'máximo': ', ',
    'minimo': ', ',
    'mínimo': ', ',
}


def _service_family_payload(servicio):
    familias = (
        models.FamiliaEquipo.objects.filter(servicio=servicio, activa=True)
        .prefetch_related('items__equipo')
        .order_by('nombre')
    )
    payload = []
    for familia in familias:
        equipos = [item.equipo for item in familia.items.all()]
        payload.append({
            'id': familia.pk,
            'nombre': familia.nombre,
            'descripcion': familia.descripcion,
            'equipos': _equipment_items_payload(equipos),
        })
    return payload


def _safe_calc_config(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _calc_steps(tipo_calculo, config):
    config = _safe_calc_config(config)
    raw_steps = config.get('pasos') or config.get('steps')
    if isinstance(raw_steps, list) and raw_steps:
        steps = []
        for step in raw_steps:
            if not isinstance(step, dict):
                continue
            operation = (
                step.get('operacion')
                or step.get('tipo_calculo')
                or step.get('operation')
                or tipo_calculo
                or ''
            )
            operands = step.get('operandos') or step.get('campos') or step.get('sources') or []
            if operation and isinstance(operands, list):
                steps.append({'operation': str(operation).strip().lower(), 'operands': operands})
        if steps:
            return steps
    operands = config.get('operandos') or config.get('campos') or config.get('sources') or []
    return [{'operation': str(tipo_calculo or '').strip().lower(), 'operands': operands if isinstance(operands, list) else []}]


def _calc_operand_label(operand, estrategia):
    if isinstance(operand, dict):
        estrategia_dimension_id = operand.get('estrategia_dimension_id') or operand.get('estrategiaDimensionId')
        dimension_id = operand.get('dimension_id') or operand.get('dimensionId')
        if estrategia_dimension_id:
            item = (
                models.EstrategiaDimension.objects.filter(
                    pk=estrategia_dimension_id,
                    estrategia=estrategia,
                )
                .select_related('dimension')
                .first()
            )
            if item:
                return item.dimension.nombre
        if dimension_id:
            item = (
                models.EstrategiaDimension.objects.filter(
                    dimension_id=dimension_id,
                    estrategia=estrategia,
                )
                .select_related('dimension')
                .first()
            )
            if item:
                return item.dimension.nombre
        for key in ('nombre', 'campo', 'source', 'fuente', 'dependencia', 'depende_de'):
            if operand.get(key):
                return str(operand.get(key))
        return 'Valor'
    operand_text = str(operand or '').strip()
    if operand_text in {'$resultado', '__resultado__', 'resultado_anterior'}:
        return 'Resultado anterior'
    return operand_text or 'Valor'


def _calculation_preview(estrategia_dimension):
    dimension = estrategia_dimension.dimension
    tipo_calculo = (getattr(dimension, 'tipo_calculo', '') or '').strip().lower()
    if not tipo_calculo:
        return None
    steps = _calc_steps(tipo_calculo, getattr(dimension, 'config_calculo', None))
    preview_steps = []
    for idx, step in enumerate(steps, start=1):
        operation = step.get('operation') or tipo_calculo
        operands = [
            _calc_operand_label(operand, estrategia_dimension.estrategia)
            for operand in step.get('operands', [])
        ]
        if operation in {'maximo', 'máximo'}:
            expression = f"Maximo({', '.join(operands)})"
        elif operation in {'minimo', 'mínimo'}:
            expression = f"Minimo({', '.join(operands)})"
        else:
            expression = _CALC_OPERATION_SYMBOLS.get(operation, ' ? ').join(operands)
        preview_steps.append({
            'order': idx,
            'operation': _CALC_OPERATION_LABELS.get(operation, operation or 'Calculo'),
            'expression': expression or 'Sin operandos configurados',
        })
    return {
        'operation': _CALC_OPERATION_LABELS.get(tipo_calculo, tipo_calculo),
        'steps': preview_steps,
    }


def _catalog_table_preview(catalogo):
    if not catalogo:
        return None
    columns = list(catalogo.columnas.all().order_by('orden', 'id'))
    rows = []
    for row in catalogo.filas.prefetch_related('celdas__columna').all().order_by('orden', 'id'):
        values = row.values_map()
        rows.append({
            'label': row.etiqueta,
            'cells': [
                values.get(column.clave_interna, row.etiqueta if column.clave_interna == 'etiqueta' else '')
                for column in columns
            ],
        })
    return {
        'columns': [column.nombre_columna for column in columns],
        'rows': rows,
        'extra_rows': 0,
    }


def _scale_table_preview(estrategia_dimension):
    rows = []
    valores = estrategia_dimension.escalas_valor.select_related('escala_unificada').order_by('nivel_ordinal', 'id')
    for item in valores:
        rows.append({
            'cells': [
                item.nivel_ordinal,
                item.codigo or '',
                item.descripcion or '',
                item.valor_numerico,
                str(item.escala_unificada) if item.escala_unificada_id else '',
            ],
        })
    return {
        'columns': ['Nivel', 'Codigo', 'Descripcion', 'Valor', 'Escala unificada'],
        'rows': rows,
        'extra_rows': 0,
    }


def _dimension_origin_info(estrategia_dimension):
    dimension = estrategia_dimension.dimension
    tipo_calculo = (getattr(dimension, 'tipo_calculo', '') or '').strip()
    try:
        catalogo = estrategia_dimension.catalogo
    except models.DimensionCatalogo.DoesNotExist:
        catalogo = None

    scale_count = estrategia_dimension.escalas_valor.count()
    calculation = _calculation_preview(estrategia_dimension)
    if tipo_calculo:
        return {
            'label': 'Calculo',
            'title': dimension.nombre,
            'kind': 'Dimension calculada',
            'field': catalogo.campo if catalogo else '',
            'rows': catalogo.filas.count() if catalogo else 0,
            'columns': catalogo.columnas.count() if catalogo else 0,
            'description': dimension.descripcion or (catalogo.descripcion if catalogo else ''),
            'calculation': calculation,
            'table_preview': _catalog_table_preview(catalogo) if catalogo else None,
        }
    if catalogo:
        return {
            'label': 'Tabla',
            'title': catalogo.nombre or dimension.nombre,
            'kind': catalogo.get_tipo_display() if hasattr(catalogo, 'get_tipo_display') else catalogo.tipo,
            'field': catalogo.campo or '',
            'rows': catalogo.filas.count(),
            'columns': catalogo.columnas.count(),
            'description': catalogo.descripcion or dimension.descripcion or '',
            'calculation': None,
            'table_preview': _catalog_table_preview(catalogo),
        }
    if scale_count:
        return {
            'label': 'Escala',
            'title': dimension.nombre,
            'kind': 'Escala',
            'field': '',
            'rows': scale_count,
            'columns': 0,
            'description': dimension.descripcion or '',
            'calculation': None,
            'table_preview': _scale_table_preview(estrategia_dimension),
        }
    return {
        'label': 'Directo',
        'title': dimension.nombre,
        'kind': 'Ingreso directo',
        'field': '',
        'rows': 0,
        'columns': 0,
        'description': dimension.descripcion or '',
        'calculation': None,
        'table_preview': None,
    }


@login_required
def service_list(request):
    servicios = get_accessible_services(request.user)
    search = request.GET.get('q', '').strip()
    if search:
        servicios = servicios.filter(
            Q(codigo_servicio__icontains=search)
            | Q(descripcion__icontains=search)
            | Q(empresa__nombre__icontains=search)
            | Q(estrategia__nombre__icontains=search)
            | Q(metodologias__abreviatura__icontains=search)
            | Q(metodologias__nombre__icontains=search)
        ).distinct()
    servicios = servicios.order_by('-creado_en', 'codigo_servicio')
    return render(request, 'core/service_management/service_list.html', {
        'page_title': 'Servicios',
        'services': servicios,
        'search': search,
    })


@login_required
def service_detail(request, pk):
    servicio, permission = _service_or_404(request, pk, edit=False)
    estrategia_dims = _strategy_dimensions(servicio.estrategia)
    dimension_rows = []
    for item in estrategia_dims:
        if _is_generated_matrix_axis_dimension(item):
            continue
        origin = _dimension_origin_info(item)
        if origin.get('label') == 'Directo':
            continue
        dimension_rows.append({
            'orden': item.orden,
            'dimension': item.dimension,
            'proceso_uso': item.proceso_uso,
            'proceso_uso_display': item.get_proceso_uso_display(),
            'has_catalog': hasattr(item, 'catalogo'),
            'has_scale': item.escalas_valor.exists(),
            'origin': origin,
        })
    aca_count = models.Criticidad.objects.filter(aca_carga__servicio=servicio).count()
    rcm_count = models.RCM.objects.filter(carga__servicio=servicio).count()
    pauta_count = models.Pauta.objects.filter(servicio=servicio).count()
    family_count = models.FamiliaEquipo.objects.filter(servicio=servicio, activa=True).count()
    total_count = aca_count + rcm_count
    matrices = (
        models.MatrizRiesgo.objects.filter(estrategia=servicio.estrategia).order_by('-fecha_creado', 'nombre')
        if servicio.estrategia_id
        else []
    )
    access_form = ServiceAccessGrantForm(service=servicio)
    access_rows = list(permission['access_rows'])
    return render(request, 'core/service_management/service_detail.html', {
        'service': servicio,
        'permission': permission,
        'aca_count': aca_count,
        'rcm_count': rcm_count,
        'pauta_count': pauta_count,
        'family_count': family_count,
        'total_count': total_count,
        'equipment_count': _service_equipment_count(servicio),
        'dimension_rows': dimension_rows,
        'matrices': matrices,
        'access_form': access_form,
        'access_rows': access_rows,
        'dimension_count': len(estrategia_dims),
        'mindco_viewer': is_mindco_user(request.user),
        'service_equipment_payload': _service_equipment_browser_payload(servicio),
        'service_family_payload': _service_family_payload(servicio),
    })


def _save_family_items(familia, equipos):
    current_ids = set(familia.items.values_list('equipo_id', flat=True))
    selected_ids = [equipo.pk for equipo in equipos]
    selected_set = set(selected_ids)
    if current_ids - selected_set:
        models.FamiliaEquipoItem.objects.filter(
            familia=familia,
            equipo_id__in=current_ids - selected_set,
        ).delete()
    for order, equipo_id in enumerate(selected_ids, start=1):
        models.FamiliaEquipoItem.objects.update_or_create(
            familia=familia,
            equipo_id=equipo_id,
            defaults={'orden': order},
        )


@login_required
def service_equipment_families(request, pk):
    servicio, permission = _service_or_404(request, pk, edit=False)
    familias = (
        models.FamiliaEquipo.objects.filter(servicio=servicio)
        .prefetch_related('items__equipo')
        .order_by('-activa', 'nombre')
    )
    return render(request, 'core/service_management/service_equipment_families.html', {
        'service': servicio,
        'permission': permission,
        'familias': familias,
    })


@login_required
@transaction.atomic
def service_equipment_family_form(request, pk, family_pk=None):
    servicio, permission = _service_or_404(request, pk, edit=True)
    familia = None
    if family_pk:
        familia = get_object_or_404(models.FamiliaEquipo, pk=family_pk, servicio=servicio)
    form = FamiliaEquipoForm(request.POST or None, instance=familia, service=servicio)
    if request.method == 'POST' and form.is_valid():
        now = timezone.now()
        familia = form.save(commit=False)
        familia.servicio = servicio
        familia.actualizado = now
        if not familia.pk:
            familia.creado_en = now
            familia.usuario = permission.get('profile')
        familia.save()
        _save_family_items(familia, form.cleaned_equipment)
        messages.success(request, 'Familia de equipos guardada correctamente.')
        return redirect('service_equipment_families', pk=servicio.pk)
    return render(request, 'core/service_management/service_equipment_family_form.html', {
        'service': servicio,
        'permission': permission,
        'form': form,
        'familia': familia,
        'service_equipment_payload': _service_equipment_browser_payload(servicio),
        'service_equipment_endpoints': _service_equipment_endpoints(servicio),
        'selected_equipment_payload': _equipment_items_payload(form.cleaned_equipment if request.method == 'POST' and form.is_valid() else (
            [item.equipo for item in familia.items.select_related('equipo').order_by('orden', 'id')] if familia else []
        )),
    })


@login_required
@transaction.atomic
def service_equipment_family_delete(request, pk, family_pk):
    servicio, permission = _service_or_404(request, pk, edit=True)
    familia = get_object_or_404(models.FamiliaEquipo, pk=family_pk, servicio=servicio)
    if request.method == 'POST':
        models.FamiliaEquipoItem.objects.filter(familia=familia).delete()
        familia.delete()
        messages.success(request, 'Familia de equipos eliminada.')
        return redirect('service_equipment_families', pk=servicio.pk)
    return render(request, 'core/service_management/service_equipment_family_delete.html', {
        'service': servicio,
        'permission': permission,
        'familia': familia,
    })


@login_required
@transaction.atomic
def service_access_manage(request, pk):
    servicio, permission = _service_or_404(request, pk, edit=True)
    if not permission['can_manage_access']:
        raise PermissionDenied('No puedes administrar accesos en este servicio.')
    if request.method != 'POST':
        return redirect('service_detail', pk=servicio.pk)

    if request.POST.get('action') == 'remove':
        target_id = request.POST.get('access_id')
        models.AccesoUsuario.objects.filter(pk=target_id, servicio=servicio).delete()
        messages.success(request, 'Acceso eliminado.')
        return redirect('service_detail', pk=servicio.pk)

    form = ServiceAccessGrantForm(request.POST or None, service=servicio)
    if request.method == 'POST' and form.is_valid():
        usuario = form.cleaned_data['usuario']
        nivel = form.cleaned_data['nivel']
        acceso = models.AccesoUsuario.objects.filter(servicio=servicio, usuario=usuario).first()
        if not acceso:
            acceso = models.AccesoUsuario(
                creado_en=timezone.now(),
                empresa=servicio.empresa,
                estrategia=servicio.estrategia,
                servicio=servicio,
                usuario=usuario,
            )
        acceso.empresa = servicio.empresa
        acceso.estrategia = servicio.estrategia
        acceso.servicio = servicio
        acceso.puede_ver = True
        acceso.puede_editar = nivel == 'edit'
        acceso.puede_ver_todo = False
        acceso.creado_en = getattr(acceso, 'creado_en', None) or timezone.now()
        acceso.save()
        messages.success(request, f'Permiso actualizado para {usuario.nombre_completo}.')
    else:
        messages.error(request, 'No se pudo guardar el permiso.')
    return redirect('service_detail', pk=servicio.pk)


@login_required
def service_dimensions(request, pk):
    servicio, permission = _service_or_404(request, pk, edit=False)
    estrategia_dims = _strategy_dimensions(servicio.estrategia)
    dim_cards = []
    for ed in estrategia_dims:
        dim_cards.append({
            'estrategia_dimension': ed,
            'dimension': ed.dimension,
            'scales': list(ed.escalas_valor.all().order_by('nivel_ordinal', 'id')),
            'catalog': _catalog_preview(ed),
        })
    return render(request, 'core/service_management/service_dimensions.html', {
        'service': servicio,
        'permission': permission,
        'dim_cards': dim_cards,
    })


@login_required
def service_equipment_levels(request, pk):
    servicio, _permission = _service_or_404(request, pk, edit=False)
    return JsonResponse(_service_equipment_browser_payload(servicio))


@login_required
def service_equipment_nodes(request, pk):
    servicio, _permission = _service_or_404(request, pk, edit=False)
    parent_id = (request.GET.get('parent_id') or '').strip()
    parent = None
    if parent_id:
        parent = get_object_or_404(
            models.NodoJerarquia,
            pk=parent_id,
            empresa=servicio.empresa,
            activo=True,
        )

    nodes = models.NodoJerarquia.objects.filter(
        empresa=servicio.empresa,
        activo=True,
        parent=parent,
    ).select_related(
        'nivel',
    ).order_by(
        'nivel__orden',
        'orden',
        'codigo',
        'nombre',
    )

    payload = []
    for node in nodes:
        payload.append({
            'id': node.pk,
            'parent_id': node.parent_id,
            'level_id': node.nivel_id,
            'level_order': node.nivel.orden,
            'code': node.codigo,
            'name': node.nombre,
            'label': f'{node.codigo} - {node.nombre}',
        })
    return JsonResponse({'nodes': payload})


@login_required
def service_equipment_search(request, pk):
    servicio, _permission = _service_or_404(request, pk, edit=False)
    query = (request.GET.get('q') or '').strip()
    node_id = (request.GET.get('node_id') or '').strip()
    max_limit = 1000 if (request.GET.get('mode') or '').strip() == 'family' else 200
    try:
        limit = min(max(int(request.GET.get('limit') or 50), 1), max_limit)
    except (TypeError, ValueError):
        limit = 50

    node = None
    if node_id:
        node = get_object_or_404(
            models.NodoJerarquia,
            pk=node_id,
            empresa=servicio.empresa,
            activo=True,
        )

    if not node and len(query) < 2:
        return JsonResponse({
            'items': [],
            'count': 0,
            'requires_filter': True,
            'message': 'Selecciona un nivel de la U.T. o escribe al menos 2 caracteres.',
        })

    qs = _service_equipment_search_queryset(servicio, node=node, query=query)
    total = qs.count()
    items = list(qs[:limit])
    return JsonResponse({
        'items': _equipment_items_payload(items),
        'count': total,
        'limit': limit,
        'truncated': total > limit,
    })


@login_required
def service_equipment_detail(request, pk, equipment_pk):
    servicio, _permission = _service_or_404(request, pk, edit=False)
    equipo = get_object_or_404(
        get_service_equipment(servicio),
        pk=equipment_pk,
    )
    payload = _equipment_items_payload([equipo])
    return JsonResponse({'item': payload[0] if payload else None})
