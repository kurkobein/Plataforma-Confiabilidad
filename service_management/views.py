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
    _service_equipment_browser_payload,
    _service_equipment_count,
    _service_equipment_endpoints,
    _service_equipment_search_queryset,
    _service_or_404,
    _strategy_dimensions,
)


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
        dimension_rows.append({
            'orden': item.orden,
            'dimension': item.dimension,
            'proceso_uso': item.proceso_uso,
            'proceso_uso_display': item.get_proceso_uso_display(),
            'has_catalog': hasattr(item, 'catalogo'),
            'has_scale': item.escalas_valor.exists(),
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
