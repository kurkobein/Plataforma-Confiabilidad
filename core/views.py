import json
import re
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
import base64
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q, TextField, Prefetch
from django.http import HttpResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from numpy import rint

from .access import get_accessible_services, get_editable_services, get_profile_for_user, get_service_equipment, get_service_permission, is_mindco_user
from .user_sync import archive_profile
from .forms import (
    ACARegistroForm,
    CriticidadDimensionFormSet,
    EmailLoginForm,
    HierarchyInsertLevelForm,
    HierarchyMoveNodeForm,
    HierarchyRouteFormSet,
    HierarchyStructureFormSet,
    HierarchyValueForm,
    MatrizBuilderForm,
    ServiceAccessGrantForm,
    ServicioACARegistroForm,
    get_form_for_key,
)
from .registry import MODEL_REGISTRY, get_registered_model
from . import models
from django.core.exceptions import PermissionDenied

MAX_LIST_COLUMNS = 6
_RANGE_RE = re.compile(r'^\s*(-?\d+)\s*-\s*(-?\d+)\s*$')




def _can_delete(request, config, obj=None):
    if config.get('delete_superuser_only', False):
        return request.user.is_superuser
    return True


def _ensure_delete_allowed(request, config, obj=None):
    if not _can_delete(request, config, obj=obj):
        raise PermissionDenied('Solo un administrador puede eliminar este registro.')


def empresa_logo(request, empresa_id):
    empresa = models.Empresa.objects.filter(id=empresa_id).first()

    if not empresa or not empresa.logo or empresa.nombre == "Mindco":
        return HttpResponse(status=204)

    return HttpResponse(empresa.logo, content_type="image/png")

def _get_config(model_key: str):
    config = get_registered_model(model_key)
    if not config:
        raise Http404('Módulo no encontrado')
    config = dict(config)
    config['db_table'] = config['model']._meta.db_table
    return config


def _ensure_admin_access(request):
    if not request.user.is_authenticated:
        raise PermissionDenied('Debes iniciar sesión.')
    if not request.user.is_superuser:
        raise PermissionDenied('Solo el superusuario puede acceder a este módulo.')


def _ensure_direct_crud_allowed(config):
    if not config.get('allow_direct_crud', True):
        raise Http404('Este módulo se administra desde un editor específico.')


FORM_TEMPLATE_BY_MODEL = {
    'empresa': 'core/forms/empresa_form.html',
    'metodologia': 'core/forms/metodologia_form.html',
    'cargo': 'core/forms/cargo_form.html',
    'usuario': 'core/forms/usuario_form.html',
    'componente': 'core/forms/componente_form.html',
    'equipo': 'core/forms/equipo_form.html',
    'dimension': 'core/forms/dimension_form.html',
    'escalaunificada': 'core/forms/escalaunificada_form.html',
    'estrategia': 'core/forms/estrategia_form.html',
    'servicio': 'core/forms/servicio_form.html',
    'accesousuario': 'core/forms/accesousuario_form.html',
    'acacarga': 'core/forms/acacarga_form.html',
    'criticidad': 'core/forms/criticidad_form.html',
    'matrizriesgo': 'core/forms/matrizriesgo_form.html',
}


def _form_template_for(model_key):
    return FORM_TEMPLATE_BY_MODEL.get(model_key, 'core/model_form.html')


def _visible_fields(model_class):
    return [f for f in model_class._meta.fields if f.name != 'id']


def _list_fields(model_class):
    base = []
    exclude = [id]
    for field in _visible_fields(model_class):
        if isinstance(field, TextField) and not field.is_relation:
            continue
        base.append(field)
    return base[:MAX_LIST_COLUMNS] or _visible_fields(model_class)[:MAX_LIST_COLUMNS]


def _detail_fields(model_class):
    return _visible_fields(model_class)


def _technical_location_rows(empresa):
    nodes = list(
        models.NodoJerarquia.objects.filter(
            empresa=empresa,
            activo=True,
        ).select_related(
            'empresa',
            'nivel',
            'parent',
        ).order_by(
            'nivel__orden',
            'orden',
            'nombre',
        )
    )
    by_parent = {}
    for node in nodes:
        by_parent.setdefault(node.parent_id, []).append(node)

    rows = []

    def walk(parent_id=None, depth=0):
        for node in by_parent.get(parent_id, []):
            rows.append({'nodo': node, 'depth': depth})
            walk(node.pk, depth + 1)

    walk()
    return rows


def _default_hierarchy_initial():
    return [
        {'nivel_nombre': 'Empresa'},
        {'nivel_nombre': 'Area de negocio'},
        {'nivel_nombre': 'Planta'},
        {'nivel_nombre': 'Area'},
        {'nivel_nombre': 'Sistema'},
        {'nivel_nombre': 'Ubicacion tecnica'},
        {'nivel_nombre': 'Equipo'},
    ]


def _hierarchy_structure_initial_rows(empresa):
    levels = list(
        models.NivelJerarquia.objects.filter(
            empresa=empresa,
            activo=True,
        ).order_by(
            'orden',
        )
    )
    if not levels:
        return _default_hierarchy_initial()
    return [
        {
            'nivel_id': level.pk,
            'nivel_nombre': level.nombre,
        }
        for level in levels
    ]


def _hierarchy_route_initial_rows(empresa):
    return [
        {
            'nodo_id': '',
            'nivel_id': level.pk,
            'nivel_nombre': level.nombre,
            'codigo': '',
            'nodo_nombre': '',
        }
        for level in models.NivelJerarquia.objects.filter(
            empresa=empresa,
            activo=True,
        ).order_by(
            'orden',
        )
    ]


def _hierarchy_options_by_order(empresa):
    options = {}
    for node in models.NodoJerarquia.objects.filter(
        empresa=empresa,
        activo=True,
    ).select_related(
        'nivel',
        'parent',
    ).order_by(
        'nivel__orden',
        'parent_id',
        'nombre',
    ):
        options.setdefault(node.nivel.orden, []).append(node)
    return options


def _hierarchy_nodes_payload(empresa):
    return [
        {
            'id': node.pk,
            'empresa_id': node.empresa_id,
            'parent_id': node.parent_id,
            'nivel_id': node.nivel_id,
            'level_order': node.nivel.orden,
            'code': node.codigo,
            'name': node.nombre,
            'label': f'{node.codigo} - {node.nombre}',
            'ut': node.ut,
            'route': node.ruta_nombre,
        }
        for node in models.NodoJerarquia.objects.filter(
            empresa=empresa,
            activo=True,
        ).select_related(
            'nivel',
            'parent',
        ).order_by(
            'nivel__orden',
            'orden',
            'nombre',
        )
    ]


def _equipment_location_context(form=None, obj=None):
    selected_node_id = None
    selected_empresa_id = None
    if form is not None and getattr(form, 'is_bound', False):
        selected_node_id = form.data.get(form.add_prefix('nodo'))
        selected_empresa_id = form.data.get(form.add_prefix('empresa'))
    if not selected_node_id and obj is not None:
        selected_node_id = getattr(obj, 'nodo_id', None)
    if not selected_empresa_id and obj is not None and getattr(obj, 'nodo_id', None):
        selected_empresa_id = obj.nodo.empresa_id
    if not selected_node_id and form is not None:
        selected_node_id = form.initial.get('nodo')
        selected_node_id = getattr(selected_node_id, 'pk', selected_node_id)
    if not selected_empresa_id and form is not None:
        selected_empresa_id = form.initial.get('empresa')
        selected_empresa_id = getattr(selected_empresa_id, 'pk', selected_empresa_id)

    levels = [
        {
            'id': level.pk,
            'empresa_id': level.empresa_id,
            'order': level.orden,
            'name': level.nombre,
        }
        for level in models.NivelJerarquia.objects.filter(
            activo=True,
        ).select_related(
            'empresa',
        ).order_by(
            'empresa__nombre',
            'orden',
        )
    ]
    nodes = [
        {
            'id': node.pk,
            'empresa_id': node.empresa_id,
            'parent_id': node.parent_id,
            'level_id': node.nivel_id,
            'level_order': node.nivel.orden,
            'code': node.codigo,
            'name': node.nombre,
            'label': f'{node.codigo} - {node.nombre}',
            'ut': node.ut,
            'route': node.ruta_nombre,
        }
        for node in models.NodoJerarquia.objects.filter(
            activo=True,
        ).select_related(
            'empresa',
            'nivel',
            'parent',
        ).order_by(
            'empresa__nombre',
            'nivel__orden',
            'orden',
            'nombre',
        )
    ]
    return {
        'tech_location_levels_json': json.dumps(levels, ensure_ascii=False),
        'tech_location_nodes_json': json.dumps(nodes, ensure_ascii=False),
        'tech_location_selected_id': str(selected_node_id or ''),
        'tech_location_empresa_id': str(selected_empresa_id or ''),
    }


def _path_ids_for_node(node_id, rows_by_id):
    path = []
    seen = set()
    current_id = node_id
    while current_id and current_id not in seen:
        row = rows_by_id.get(current_id)
        if not row:
            break
        seen.add(current_id)
        path.append(current_id)
        current_id = row.get('parent_id')
    return list(reversed(path))


def _node_ut_from_path(path_ids, rows_by_id):
    return '-'.join(
        models._technical_segment(rows_by_id[node_id]['codigo'])
        for node_id in path_ids
        if node_id in rows_by_id
    )


def _node_route_from_path(path_ids, rows_by_id):
    parts = []
    for node_id in path_ids:
        row = rows_by_id.get(node_id)
        if not row:
            continue
        parts.append(f"{row['nivel__nombre']}: {row['codigo']} - {row['nombre']}")
    return ' > '.join(parts)


def _service_equipment_browser_payload(service):
    if not service:
        return {'levels': [], 'nodes': [], 'equipment': []}

    levels = [
        {
            'id': level.pk,
            'order': level.orden,
            'name': level.nombre,
        }
        for level in models.NivelJerarquia.objects.filter(
            empresa=service.empresa,
            activo=True,
        ).order_by('orden')
    ]

    node_rows = list(
        models.NodoJerarquia.objects.filter(
            empresa=service.empresa,
        ).values(
            'id',
            'parent_id',
            'nivel_id',
            'nivel__orden',
            'nivel__nombre',
            'codigo',
            'nombre',
            'activo',
        ).order_by(
            'nivel__orden',
            'parent_id',
            'orden',
            'codigo',
            'nombre',
        )
    )
    rows_by_id = {row['id']: row for row in node_rows}

    node_payload = []
    for row in node_rows:
        if not row['activo']:
            continue
        path_ids = _path_ids_for_node(row['id'], rows_by_id)
        node_payload.append({
            'id': row['id'],
            'parent_id': row['parent_id'],
            'level_id': row['nivel_id'],
            'level_order': row['nivel__orden'],
            'code': row['codigo'],
            'name': row['nombre'],
            'label': f"{_node_ut_from_path(path_ids, rows_by_id)} - {row['nombre']}",
        })

    equipment_payload = []
    for equipo in get_service_equipment(service):
        path_ids = _path_ids_for_node(equipo.nodo_id, rows_by_id) if equipo.nodo_id else []
        equipment_payload.append({
            'id': equipo.pk,
            'ut': equipo.ut or '',
            'descripcion_ut': equipo.descripcion_ut or '',
            'equipo': equipo.nombre_equipo or '',
            'tag': equipo.tag_equipo or '',
            'node_id': equipo.nodo_id,
            'path_node_ids': path_ids,
            'path_text': _node_route_from_path(path_ids, rows_by_id),
        })

    return {
        'levels': levels,
        'nodes': node_payload,
        'equipment': equipment_payload,
    }


def _level_for_order(empresa, order, nombre=None):
    if not nombre:
        level = models.NivelJerarquia.objects.filter(empresa=empresa, orden=order).first()
        if level:
            return level
    defaults = {
        'nombre': nombre or f'Nivel {order}',
        'activo': True,
    }
    level, _ = models.NivelJerarquia.objects.update_or_create(
        empresa=empresa,
        orden=order,
        defaults=defaults,
    )
    return level


def _active_hierarchy_depth(empresa):
    nodes = list(
        models.NodoJerarquia.objects.filter(
            empresa=empresa,
            activo=True,
        ).values(
            'id',
            'parent_id',
        )
    )
    parent_by_id = {row['id']: row['parent_id'] for row in nodes}
    depth_cache = {}

    def depth_for(node_id):
        if node_id in depth_cache:
            return depth_cache[node_id]
        parent_id = parent_by_id.get(node_id)
        depth = 1
        if parent_id in parent_by_id:
            depth = depth_for(parent_id) + 1
        depth_cache[node_id] = depth
        return depth

    return max((depth_for(row['id']) for row in nodes), default=0)


def _active_subtree_ids(node):
    ids = []
    frontier = [node.pk]
    while frontier:
        current_ids = list(
            models.NodoJerarquia.objects.filter(
                empresa=node.empresa,
                pk__in=frontier,
                activo=True,
            ).values_list(
                'pk',
                flat=True,
            )
        )
        if not current_ids:
            break
        ids.extend(current_ids)
        frontier = list(
            models.NodoJerarquia.objects.filter(
                empresa=node.empresa,
                parent_id__in=current_ids,
                activo=True,
            ).values_list(
                'pk',
                flat=True,
            )
        )
    return ids


def _move_levels_to_temporary_orders(empresa, levels):
    max_order = max((level.orden for level in levels), default=0)
    temp_base = max_order + len(levels) + 1000
    for index, level in enumerate(levels, start=1):
        level.orden = temp_base + index
        level.nombre = f'__tmp_nivel_{empresa.pk}_{level.pk}'
        level.save(update_fields=['orden', 'nombre'])


def _save_hierarchy_structure(empresa, formset):
    active_ids = []
    forms_with_content = [
        form for form in formset
        if not form.cleaned_data.get('DELETE') and form.has_content
    ]
    if not forms_with_content:
        raise ValueError('Completa al menos un nivel para guardar la estructura.')

    submitted_names = [form.cleaned_data['nivel_nombre'].strip().lower() for form in forms_with_content]
    if len(submitted_names) != len(set(submitted_names)):
        raise ValueError('Los nombres de nivel no se pueden repetir dentro de la misma empresa.')

    deepest_route = _active_hierarchy_depth(empresa)
    if deepest_route and len(forms_with_content) < deepest_route:
        raise ValueError(
            'No se puede reducir la estructura a '
            f'{len(forms_with_content)} niveles porque existen ubicaciones tecnicas activas '
            f'de {deepest_route} niveles. Ajusta o desactiva esos valores antes de quitar niveles.'
        )

    existing_levels = list(models.NivelJerarquia.objects.filter(empresa=empresa).order_by('pk'))
    existing = {level.pk: level for level in existing_levels}
    original_names = {level.pk: level.nombre for level in existing_levels}
    _move_levels_to_temporary_orders(empresa, existing_levels)
    reusable_by_name = {
        original_name.strip().lower(): existing[level_id]
        for level_id, original_name in original_names.items()
    }
    used_ids = set()

    for order, form in enumerate(forms_with_content, start=1):
        level_id = form.cleaned_data.get('nivel_id')
        level_name = form.cleaned_data['nivel_nombre']
        level = existing.get(level_id) if level_id else None
        if not level:
            reusable = reusable_by_name.get(level_name.lower())
            if reusable and reusable.pk not in used_ids:
                level = reusable
        if level:
            level.nombre = level_name
            level.orden = order
            level.activo = True
            level.save(update_fields=['nombre', 'orden', 'activo'])
        else:
            level = models.NivelJerarquia.objects.create(
                empresa=empresa,
                nombre=level_name,
                orden=order,
                activo=True,
            )
        active_ids.append(level.pk)
        used_ids.add(level.pk)

    models.NivelJerarquia.objects.filter(empresa=empresa).exclude(pk__in=active_ids).update(activo=False)

    active_levels_by_order = {
        level.orden: level
        for level in models.NivelJerarquia.objects.filter(
            empresa=empresa,
            pk__in=active_ids,
            activo=True,
        )
    }
    roots = models.NodoJerarquia.objects.filter(
        empresa=empresa,
        parent__isnull=True,
        activo=True,
    ).order_by('orden', 'codigo', 'nombre')
    for root in roots:
        _sync_subtree_levels(root, 1, active_levels_by_order)


def _sync_subtree_levels(node, start_order, levels_by_order=None):
    level = None
    if levels_by_order is not None:
        level = levels_by_order.get(start_order)
    if level is None:
        level = _level_for_order(node.empresa, start_order)
    node.nivel = level
    node.save(update_fields=['nivel'])
    for child in node.hijos.filter(activo=True).order_by('orden', 'codigo', 'nombre'):
        _sync_subtree_levels(child, start_order + 1, levels_by_order)


def _save_hierarchy_route(empresa, formset):
    parent = None
    last_node = None
    previous_blank = False
    for order, form in enumerate(formset, start=1):
        if form.cleaned_data.get('DELETE') or not form.has_content:
            previous_blank = True
            continue
        if previous_blank:
            raise ValueError('Completa la ruta en orden, sin saltar niveles intermedios.')
        level = models.NivelJerarquia.objects.filter(
            empresa=empresa,
            pk=form.cleaned_data.get('nivel_id'),
            activo=True,
        ).first() or _level_for_order(
            empresa,
            order,
            form.cleaned_data['nivel_nombre'],
        )
        selected_node_id = form.cleaned_data.get('nodo_id')
        if selected_node_id:
            node = models.NodoJerarquia.objects.filter(
                empresa=empresa,
                pk=selected_node_id,
                nivel=level,
                parent=parent,
                activo=True,
            ).first()
            if not node:
                raise ValueError(f'El valor seleccionado para {level.nombre} no corresponde a la ruta superior.')
            parent = node
            last_node = node
            continue
        codigo = form.cleaned_data['codigo']
        sibling_order = models.NodoJerarquia.objects.filter(
            empresa=empresa,
            parent=parent,
            nivel=level,
        ).count() + 1
        node, created = models.NodoJerarquia.objects.get_or_create(
            empresa=empresa,
            parent=parent,
            codigo=codigo,
            defaults={
                'nivel': level,
                'nombre': form.cleaned_data['nodo_nombre'],
                'orden': sibling_order,
                'activo': True,
            },
        )
        if not created:
            node.nivel = level
            node.nombre = form.cleaned_data['nodo_nombre']
            node.activo = True
            node.save(update_fields=['nivel', 'nombre', 'activo'])
        parent = node
        last_node = node
    return last_node


def _save_hierarchy_value(empresa, form):
    level = form.cleaned_data['nivel']
    parent = form.cleaned_data.get('parent')
    codigo = form.cleaned_data['codigo']
    sibling_order = models.NodoJerarquia.objects.filter(
        empresa=empresa,
        parent=parent,
        nivel=level,
    ).count() + 1
    node, created = models.NodoJerarquia.objects.update_or_create(
        empresa=empresa,
        parent=parent,
        codigo=codigo,
        defaults={
            'nivel': level,
            'nombre': form.cleaned_data['nombre'],
            'activo': True,
        },
    )
    if created:
        node.orden = sibling_order
        node.save(update_fields=['orden'])
    return node


@login_required
def dashboard(request):
    servicios = list(get_accessible_services(request.user)[:8])
    aca_total = models.Criticidad.objects.filter(aca_carga__servicio__in=servicios).count() if servicios else 0
    editable_services = [s for s in servicios if get_service_permission(request.user, s)['can_edit']]
    return render(request, 'core/dashboard.html', {
        'service_cards': servicios,
        'quick_stats': [
            {'label': 'Servicios accesibles', 'count': len(servicios)},
            {'label': 'Registros', 'count': aca_total},
            {'label': 'Servicios editables', 'count': len(editable_services)},
        ],
    })


@login_required
def technical_location_index(request):
    _ensure_admin_access(request)
    empresas = models.Empresa.objects.order_by('nombre')
    rows = []
    for empresa in empresas:
        rows.append({
            'empresa': empresa,
            'niveles_count': models.NivelJerarquia.objects.filter(empresa=empresa, activo=True).count(),
            'nodos_count': models.NodoJerarquia.objects.filter(empresa=empresa, activo=True).count(),
            'equipos_count': models.Equipo.objects.filter(nodo__empresa=empresa).count(),
        })
    return render(request, 'core/technical_location_index.html', {
        'rows': rows,
    })


@login_required
def hierarchy_tree(request, empresa_id):
    _ensure_admin_access(request)
    empresa = get_object_or_404(models.Empresa, pk=empresa_id)
    rows = _technical_location_rows(empresa)
    levels = list(models.NivelJerarquia.objects.filter(empresa=empresa, activo=True).order_by('orden'))
    return render(request, 'core/hierarchy_tree.html', {
        'empresa': empresa,
        'rows': rows,
        'levels': levels,
    })


@login_required
def hierarchy_structure(request, empresa_id):
    _ensure_admin_access(request)
    empresa = get_object_or_404(models.Empresa, pk=empresa_id)
    initial = _hierarchy_structure_initial_rows(empresa)

    if request.method == 'POST':
        formset = HierarchyStructureFormSet(request.POST)
        if formset.is_valid():
            try:
                with transaction.atomic():
                    _save_hierarchy_structure(empresa, formset)
                messages.success(request, 'Estructura base de ubicacion tecnica guardada.')
                return redirect('hierarchy_tree', empresa_id=empresa.pk)
            except ValueError as exc:
                messages.error(request, str(exc))
            except IntegrityError:
                messages.error(
                    request,
                    'No se pudo guardar la estructura porque hay niveles duplicados o una '
                    'colision de orden. Revisa los nombres de los niveles e intenta nuevamente.',
                )
    else:
        formset = HierarchyStructureFormSet(initial=initial)

    form_rows = [
        {
            'orden': index + 1,
            'form': form,
        }
        for index, form in enumerate(formset.forms)
    ]
    return render(request, 'core/hierarchy_structure_form.html', {
        'empresa': empresa,
        'formset': formset,
        'form_rows': form_rows,
    })


@login_required
def hierarchy_values(request, empresa_id):
    _ensure_admin_access(request)
    empresa = get_object_or_404(models.Empresa, pk=empresa_id)
    levels = list(models.NivelJerarquia.objects.filter(empresa=empresa, activo=True).order_by('orden'))

    if request.method == 'POST':
        form = HierarchyValueForm(request.POST, empresa=empresa)
        if not levels:
            messages.error(request, 'Primero define la estructura base de la ubicacion tecnica para esta empresa.')
        elif form.is_valid():
            node = _save_hierarchy_value(empresa, form)
            messages.success(request, f'Valor guardado: {node.codigo} - {node.nombre}')
            return redirect('hierarchy_values', empresa_id=empresa.pk)
    else:
        form = HierarchyValueForm(empresa=empresa)

    rows = _technical_location_rows(empresa)
    return render(request, 'core/hierarchy_values.html', {
        'empresa': empresa,
        'form': form,
        'levels': levels,
        'rows': rows,
        'hierarchy_nodes_json': json.dumps(_hierarchy_nodes_payload(empresa), ensure_ascii=False),
    })


@login_required
def hierarchy_create_route(request, empresa_id):
    _ensure_admin_access(request)
    empresa = get_object_or_404(models.Empresa, pk=empresa_id)
    initial = _hierarchy_route_initial_rows(empresa)
    if not initial:
        messages.info(request, 'Primero define la estructura base de la ubicacion tecnica para esta empresa.')
        return redirect('hierarchy_structure', empresa_id=empresa.pk)
    if request.method == 'POST':
        formset = HierarchyRouteFormSet(request.POST)
        if formset.is_valid():
            try:
                with transaction.atomic():
                    node = _save_hierarchy_route(empresa, formset)
                if node:
                    messages.success(request, f'Ubicacion tecnica guardada: {node.ut}')
                    return redirect('hierarchy_tree', empresa_id=empresa.pk)
                messages.error(request, 'Completa al menos un nivel para guardar la ubicacion tecnica.')
            except ValueError as exc:
                messages.error(request, str(exc))
    else:
        formset = HierarchyRouteFormSet(initial=initial)

    options = _hierarchy_options_by_order(empresa)
    form_rows = [
        {
            'orden': index + 1,
            'form': form,
            'opciones': options.get(index + 1, []),
        }
        for index, form in enumerate(formset.forms)
    ]
    return render(request, 'core/hierarchy_route_form.html', {
        'empresa': empresa,
        'formset': formset,
        'form_rows': form_rows,
        'rutas_existentes': [row['nodo'].ut for row in _technical_location_rows(empresa) if not row['nodo'].hijos.filter(activo=True).exists()],
        'hierarchy_nodes_json': json.dumps(_hierarchy_nodes_payload(empresa), ensure_ascii=False),
    })


@login_required
def hierarchy_move_node(request, pk):
    _ensure_admin_access(request)
    node = get_object_or_404(
        models.NodoJerarquia.objects.select_related('empresa', 'nivel', 'parent'),
        pk=pk,
    )
    if request.method == 'POST':
        form = HierarchyMoveNodeForm(request.POST, node=node)
        if form.is_valid():
            parent = form.cleaned_data['parent']
            with transaction.atomic():
                node.parent = parent
                node.save(update_fields=['parent'])
                start_order = parent.nivel.orden + 1 if parent else 1
                _sync_subtree_levels(node, start_order)
            messages.success(request, 'Nodo movido y niveles ajustados por posicion.')
            return redirect('hierarchy_tree', empresa_id=node.empresa_id)
    else:
        form = HierarchyMoveNodeForm(node=node, initial={'parent': node.parent_id})
    return render(request, 'core/hierarchy_move_node.html', {
        'node': node,
        'form': form,
    })


@login_required
def hierarchy_insert_between(request, pk):
    _ensure_admin_access(request)
    child = get_object_or_404(
        models.NodoJerarquia.objects.select_related('empresa', 'nivel', 'parent'),
        pk=pk,
    )
    parent = child.parent
    if request.method == 'POST':
        form = HierarchyInsertLevelForm(request.POST)
        if form.is_valid():
            insert_order = child.nivel.orden
            with transaction.atomic():
                for level in models.NivelJerarquia.objects.filter(
                    empresa=child.empresa,
                    orden__gte=insert_order,
                ).order_by('-orden'):
                    level.orden += 1
                    level.save(update_fields=['orden'])
                new_level = models.NivelJerarquia.objects.create(
                    empresa=child.empresa,
                    nombre=form.cleaned_data['nivel_nombre'],
                    orden=insert_order,
                    activo=True,
                )
                new_node = models.NodoJerarquia.objects.create(
                    empresa=child.empresa,
                    nivel=new_level,
                    parent=parent,
                    codigo=form.cleaned_data['codigo'],
                    nombre=form.cleaned_data['nodo_nombre'],
                    orden=child.orden,
                    activo=True,
                )
                child.parent = new_node
                child.save(update_fields=['parent'])
            messages.success(request, 'Nivel intermedio insertado.')
            return redirect('hierarchy_tree', empresa_id=child.empresa_id)
    else:
        form = HierarchyInsertLevelForm()
    return render(request, 'core/hierarchy_insert_between.html', {
        'child': child,
        'parent': parent,
        'form': form,
    })


@login_required
def hierarchy_delete_node(request, pk):
    _ensure_admin_access(request)
    node = get_object_or_404(
        models.NodoJerarquia.objects.select_related('empresa', 'nivel', 'parent'),
        pk=pk,
        activo=True,
    )
    if request.method != 'POST':
        return redirect('hierarchy_tree', empresa_id=node.empresa_id)

    with transaction.atomic():
        subtree_ids = _active_subtree_ids(node)
        equipment_count = models.Equipo.objects.filter(nodo_id__in=subtree_ids).count()
        deleted_count = models.NodoJerarquia.objects.filter(
            pk__in=subtree_ids,
            activo=True,
        ).update(activo=False)

    messages.success(
        request,
        f'Ubicacion tecnica desactivada: {node.ut}. Se desactivaron {deleted_count} valores de la rama.',
    )
    if equipment_count:
        messages.warning(
            request,
            f'{equipment_count} equipos siguen asociados a esa ubicacion historica. '
            'Puedes reasignarlos editando cada equipo.',
        )
    return redirect('hierarchy_tree', empresa_id=node.empresa_id)


@login_required
def model_list(request, model_key):
    config = _get_config(model_key)
    _ensure_direct_crud_allowed(config)
    model = config['model']

    editable_service_ids = set()
    if model_key == 'servicio':
        qs = get_accessible_services(request.user)
        if request.user.is_superuser:
            editable_service_ids = set(qs.values_list('id', flat=True))
        else:
            editable_service_ids = {
                service.id
                for service in qs
                if get_service_permission(request.user, service)['can_edit']
            }
    else:
        qs = model.objects.all()

    search = request.GET.get('q', '').strip()
    if search and config['search_fields']:
        clause = Q()
        for field_name in config['search_fields']:
            clause |= Q(**{f'{field_name}__icontains': search})
        qs = qs.filter(clause)

    rows = qs
    total_count = qs.count()

    list_fields = [
        field for field in _list_fields(model)
        if field.name != 'id' and not (model_key == 'empresa' and field.name == 'logo')
    ]

    return render(request, 'core/model_list.html', {
        'config': config,
        'model_key': model_key,
        'rows': rows,
        'search': search,
        'list_fields': list_fields,
        'editable_service_ids': editable_service_ids,
        'total_count': total_count,
    })


@login_required
def model_detail(request, model_key, pk):
    config = _get_config(model_key)
    _ensure_direct_crud_allowed(config)

    if model_key == 'servicio':
        obj, permission = _service_or_404(request, pk, edit=False)
    else:
        _ensure_admin_access(request)
        obj = get_object_or_404(config['model'], pk=pk)
        permission = None

    logo_base64 = None
    if model_key == 'empresa' and obj.logo:
        logo_base64 = base64.b64encode(obj.logo).decode('utf-8')

    return render(request, 'core/model_detail.html', {
        'logo_base64': logo_base64,
        'config': config,
        'model_key': model_key,
        'object': obj,
        'detail_fields': _detail_fields(config['model']),
        'permission': permission,
    })


@login_required
def model_create(request, model_key):
    _ensure_admin_access(request)
    if model_key == 'matrizriesgo':
        return redirect('matriz_builder_new')
    config = _get_config(model_key)
    _ensure_direct_crud_allowed(config)
    FormClass = get_form_for_key(model_key)

    if request.method == 'POST':
        form = FormClass(request.POST, request.FILES)
        if form.is_valid():
            obj = form.save()
            return redirect('model_detail', model_key=model_key, pk=obj.pk)
    else:
        form = FormClass()

    context = {
        'config': config,
        'model_key': model_key,
        'form': form,
        'mode': 'create',
    }
    if model_key == 'equipo':
        context.update(_equipment_location_context(form=form))
    return render(request, _form_template_for(model_key), context)


@login_required
def model_update(request, model_key, pk):
    config = _get_config(model_key)
    _ensure_direct_crud_allowed(config)

    if model_key == 'servicio':
        obj, permission = _service_or_404(request, pk, edit=True)
    else:
        _ensure_admin_access(request)
        obj = get_object_or_404(config['model'], pk=pk)
        permission = None

    FormClass = get_form_for_key(model_key)

    if request.method == 'POST':
        form = FormClass(request.POST, request.FILES, instance=obj)
        if form.is_valid():
            obj = form.save()
            if model_key == 'servicio':
                return redirect('service_detail', pk=obj.pk)
            return redirect('model_detail', model_key=model_key, pk=obj.pk)
    else:
        form = FormClass(instance=obj)

    context = {
        'config': config,
        'model_key': model_key,
        'form': form,
        'object': obj,
        'mode': 'update',
        'permission': permission,
    }
    if model_key == 'equipo':
        context.update(_equipment_location_context(form=form, obj=obj))
    return render(request, _form_template_for(model_key), context)


@login_required
@transaction.atomic
def model_delete(request, model_key, pk):
    _ensure_admin_access(request)
    config = _get_config(model_key)
    _ensure_direct_crud_allowed(config)

    lookup_qs = config['model']
    if model_key == 'usuario' and hasattr(config['model'], 'all_objects'):
        lookup_qs = config['model'].all_objects

    obj = get_object_or_404(lookup_qs, pk=pk)

    _ensure_delete_allowed(request, config, obj)

    if request.method == 'POST':
        if model_key == 'usuario':
            if getattr(obj, 'is_deleted', False):
                messages.info(request, 'El usuario ya estaba archivado.')
            else:
                archive_profile(
                    obj,
                    deleted_by=request.user,
                    reason='Eliminado desde la plataforma',
                )
                messages.success(
                    request,
                    'Usuario archivado correctamente. Ya no aparecerá en listados ni opciones.'
                )
            return redirect('model_list', model_key=model_key)
        if model_key == 'matrizriesgo':
            models.MatrizRiesgoCelda.objects.filter(matriz=obj).delete()
            models.NivelImpacto.objects.filter(matriz=obj).delete()
            models.NivelProbabilidad.objects.filter(matriz=obj).delete()

            obj.delete()

            messages.success(request, 'Matriz eliminada correctamente.')
            return redirect('model_list', model_key=model_key)
        
        obj.delete()
        messages.success(request, 'Registro eliminado correctamente.')
        return redirect('model_list', model_key=model_key)
    return render(request, 'core/model_delete.html', {
        'config': config,
        'model_key': model_key,
        'object': obj,
        'can_delete': _can_delete(request, config, obj),
    })

# ---------------------------------------------------------------------------
# Helpers generales
# ---------------------------------------------------------------------------
def _json_payload(request, key, default=None):
    raw = request.POST.get(key, '')
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _decimal_or_none(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _int_or_default(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json_loads_safe(value, default=None):
    if default is None:
        default = {}
    if not value:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value




def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form = EmailLoginForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        login(request, form.cleaned_data['auth_user'])
        messages.success(request, 'Sesión iniciada correctamente.')
        return redirect(request.GET.get('next') or 'dashboard')
    return render(request, 'core/login.html', {'form': form})


def logout_view(request):
    logout(request)
    return redirect('login')


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
    return render(request, 'core/service_list.html', {
        'page_title': 'Servicios',
        'services': servicios,
        'search': search,
    })


def _service_or_404(request, pk, edit=False):
    servicio = get_object_or_404(
        models.Servicio.objects.select_related('empresa', 'estrategia', 'responsable_usuario', 'creado_por_usuario'),
        pk=pk,
    )
    permission = get_service_permission(request.user, servicio)
    if edit and not permission['can_edit']:
        raise PermissionDenied('No tienes permiso para editar este servicio.')
    if not edit and not permission['can_view']:
        raise PermissionDenied('No tienes permiso para ver este servicio.')
    return servicio, permission


def _strategy_dimensions(estrategia):
    if not estrategia:
        return []
    return list(
        models.EstrategiaDimension.objects.filter(estrategia=estrategia, activo=True)
        .select_related('dimension')
        .prefetch_related(
            'escalas_valor__escala_unificada',
            'catalogo__columnas',
            'catalogo__filas__celdas__columna',
        )
        .order_by('orden', 'id')
    )


def _dimension_display_value(item):
    if item is None:
        return ''

    if item.valor_booleano is not None:
        return 'Sí' if item.valor_booleano else 'No'

    if item.valor_numerico is not None:
        if item.valor_numerico == int(item.valor_numerico):
            return str(int(item.valor_numerico))
        return str(item.valor_numerico)

    if item.escala_valor_id:
        if item.escala_valor.valor_numerico is not None:
            if item.escala_valor.valor_numerico == int(item.escala_valor.valor_numerico):
                return str(int(item.escala_valor.valor_numerico))
            return str(item.escala_valor.valor_numerico)
        return item.escala_valor.codigo or item.escala_valor.descripcion or ''

    if item.valor_texto:
        return item.valor_texto

    if item.catalogo_fila_id:
        return item.catalogo_fila.etiqueta or ''

    return ''


def _catalog_preview(estrategia_dimension):
    catalogo = getattr(estrategia_dimension, 'catalogo', None)
    if not catalogo:
        return None
    columnas = list(catalogo.columnas.all().order_by('orden'))
    filas = []
    for fila in catalogo.filas.all().order_by('orden'):
        value_map = fila.values_map()
        cells = []
        for col in columnas:
            cells.append(value_map.get(col.clave_interna, fila.etiqueta if col.clave_interna == 'etiqueta' else ''))
        filas.append({'pk': fila.pk, 'cells': cells, 'label': fila.etiqueta or f'Fila {fila.orden}'})
    return {
        'nombre': catalogo.nombre,
        'columnas': columnas,
        'filas': filas,
    }


def _quantize_decimal(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _matrix_axis_level_info(levels, estrategia_dimension):
    levels = list(levels)
    if not levels:
        return {'mode': 'sin_niveles', 'levels': []}

    scale_levels = []
    if estrategia_dimension is not None:
        try:
            scale_levels = list(estrategia_dimension.escalas_valor.select_related('escala_unificada').order_by('nivel_ordinal', 'id'))
        except Exception:
            scale_levels = []

    use_unified = len(scale_levels) >= len(levels) and all(
        getattr(scale_levels[idx], 'escala_unificada_id', None) and 1 <= int(scale_levels[idx].escala_unificada.nivel) <= 5
        for idx in range(len(levels))
    )

    info = []
    if use_unified:
        mode = 'escala_unificada'
        for idx, level in enumerate(levels, start=1):
            unified = int(scale_levels[idx - 1].escala_unificada.nivel)
            position = 0 if unified <= 1 else (unified - 1) / 4
            info.append({
                'idx': idx,
                'obj': level,
                'nombre': level.nombre,
                'valor': _quantize_decimal(level.valor),
                'descripcion': level.descripcion,
                'position': position,
                'canonical_slot': unified,
                'source': 'Escala unificada',
                'unified_level': unified,
            })
        return {'mode': mode, 'levels': info}

    numeric_values = [_quantize_decimal(level.valor) for level in levels]
    unique_values = {value for value in numeric_values if value is not None}
    if len(levels) > 1 and len(unique_values) > 1:
        mode = 'valor_relativo'
        min_value = min(unique_values)
        max_value = max(unique_values)
        span = max_value - min_value
        for idx, level in enumerate(levels, start=1):
            current = _quantize_decimal(level.valor)
            position = float((current - min_value) / span) if current is not None and span not in (None, 0) else (idx - 1) / (len(levels) - 1)
            canonical = max(1, min(5, int(round(1 + (position * 4)))))
            info.append({
                'idx': idx,
                'obj': level,
                'nombre': level.nombre,
                'valor': current,
                'descripcion': level.descripcion,
                'position': position,
                'canonical_slot': canonical,
                'source': 'Valor relativo',
                'unified_level': None,
            })
        return {'mode': mode, 'levels': info}

    mode = 'orden_relativo'
    denominator = max(len(levels) - 1, 1)
    for idx, level in enumerate(levels, start=1):
        position = (idx - 1) / denominator
        canonical = max(1, min(5, int(round(1 + (position * 4)))))
        info.append({
            'idx': idx,
            'obj': level,
            'nombre': level.nombre,
            'valor': _quantize_decimal(level.valor),
            'descripcion': level.descripcion,
            'position': position,
            'canonical_slot': canonical,
            'source': 'Orden relativo',
            'unified_level': None,
        })
    return {'mode': mode, 'levels': info}


def _closest_original_level(level_info, canonical_slot):
    target_position = 0 if canonical_slot <= 1 else (canonical_slot - 1) / 4
    levels = level_info.get('levels', [])
    if not levels:
        return None
    return min(
        levels,
        key=lambda item: (abs(item['position'] - target_position), abs(item['idx'] - canonical_slot), item['idx'])
    )


def _homologation_method_label(level_info):
    mode = level_info.get('mode')
    return {
        'escala_unificada': 'Exacta por escala unificada',
        'valor_relativo': 'Proyección por valor relativo',
        'orden_relativo': 'Proyección por orden relativo',
        'sin_niveles': 'Sin homologación',
    }.get(mode, 'Proyección')


def _build_homologated_matrix_preview(matriz):
    prob_levels = list(matriz.niveles_probabilidad.order_by('orden_visual', 'id'))
    impact_levels = list(matriz.niveles_impacto.order_by('orden_visual', 'id'))
    cell_map = {
        (cell.probabilidad.orden_visual, cell.impacto_nivel.orden_visual): cell
        for cell in matriz.celdas.select_related('probabilidad', 'impacto_nivel').all()
    }

    prob_info = _matrix_axis_level_info(prob_levels, matriz.dimension_probabilidad)
    impact_info = _matrix_axis_level_info(impact_levels, matriz.dimension_impacto)

    prob_defs = []
    impact_defs = []
    cell_payload = {}

    for slot in range(1, 6):
        source_prob = _closest_original_level(prob_info, slot)
        source_impact = _closest_original_level(impact_info, slot)
        prob_defs.append({
            'idx': slot,
            'nombre': f'P{slot}',
            'valor': slot,
            'descripcion': f"Ref. {source_prob['nombre']}" if source_prob else 'Sin referencia',
        })
        impact_defs.append({
            'idx': slot,
            'nombre': f'I{slot}',
            'valor': slot,
            'descripcion': f"Ref. {source_impact['nombre']}" if source_impact else 'Sin referencia',
        })

    for prob_slot in range(1, 6):
        src_prob = _closest_original_level(prob_info, prob_slot)
        if not src_prob:
            continue
        for impact_slot in range(1, 6):
            src_impact = _closest_original_level(impact_info, impact_slot)
            if not src_impact:
                continue
            original_cell = cell_map.get((src_prob['idx'], src_impact['idx']))
            if original_cell:
                result_num = original_cell.resultado_num
                clasificacion = original_cell.clasificacion
                color = original_cell.color
            else:
                prob_value = _quantize_decimal(src_prob.get('valor')) or Decimal(prob_slot)
                impact_value = _quantize_decimal(src_impact.get('valor')) or Decimal(impact_slot)
                result_num = prob_value * impact_value
                matched = _match_legend(int(result_num), _legend_from_matrix(matriz))
                clasificacion, color = matched
            cell_payload[(prob_slot, impact_slot)] = {
                'prob_idx': prob_slot,
                'impact_idx': impact_slot,
                'resultado_num': result_num,
                'clasificacion': clasificacion,
                'color': color,
                'calcular': True,
            }

    preview = _matrix_preview_from_defs(
        'impacto',
        prob_defs,
        impact_defs,
        cell_payload,
        matriz.estrategia,
        matriz.dimension_probabilidad,
        matriz.dimension_impacto,
        _legend_from_matrix(matriz),
    )

    return {
        'preview': preview,
        'prob_mapping': prob_info['levels'],
        'impact_mapping': impact_info['levels'],
        'prob_method': _homologation_method_label(prob_info),
        'impact_method': _homologation_method_label(impact_info),
        'source_shape': f"{len(prob_levels)}x{len(impact_levels)}",
        'target_shape': '5x5',
        'is_exact': prob_info.get('mode') == 'escala_unificada' and impact_info.get('mode') == 'escala_unificada',
    }


def _build_matrix_original_preview(matriz):
    prob_levels = list(matriz.niveles_probabilidad.order_by('orden_visual', 'id'))
    impact_levels = list(matriz.niveles_impacto.order_by('orden_visual', 'id'))
    prob_defs = _matrix_level_dicts(prob_levels, len(prob_levels) or 5, 'p')
    impact_defs = _matrix_level_dicts(impact_levels, len(impact_levels) or 5, 'i')
    existing_cells = {
        (cell.probabilidad.orden_visual, cell.impacto_nivel.orden_visual): cell
        for cell in matriz.celdas.select_related('probabilidad', 'impacto_nivel').all()
    }
    return _matrix_preview_from_defs(
        matriz.eje_horizontal or 'impacto',
        prob_defs,
        impact_defs,
        existing_cells,
        matriz.estrategia,
        matriz.dimension_probabilidad,
        matriz.dimension_impacto,
        _legend_from_matrix(matriz),
    )


def _matrix_stats(preview):
    rows = preview.get('rows', [])
    cols = preview.get('x_defs', [])
    values = []
    for row in rows:
        for cell in row.get('cells', []):
            value = _quantize_decimal(cell.get('resultado_num'))
            if value is not None:
                values.append(value)
    return {
        'rows': len(rows),
        'cols': len(cols),
        'min_value': min(values) if values else None,
        'max_value': max(values) if values else None,
    }


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

    return render(request, 'core/matrix_view.html', {
        'service': servicio,
        'permission': permission,
        'matriz': matriz,
        'mindco_viewer': mindco_viewer,
        'original_preview': original_preview,
        'original_stats': original_stats,
        'homologation': homologation,
        'homologated_stats': _matrix_stats(homologation['preview']) if homologation else None,
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
            'has_catalog': hasattr(item, 'catalogo'),
            'has_scale': item.escalas_valor.exists(),
        })
    aca_count = models.Criticidad.objects.filter(aca_carga__servicio=servicio).count()
    matrices = models.MatrizRiesgo.objects.filter(estrategia=servicio.estrategia).order_by('-fecha_creado', 'nombre') if servicio.estrategia_id else []
    access_form = ServiceAccessGrantForm(service=servicio)
    access_rows = list(permission['access_rows'])
    service_equipment_payload = _service_equipment_browser_payload(servicio)
    return render(request, 'core/service_detail.html', {
        'service': servicio,
        'permission': permission,
        'aca_count': aca_count,
        'equipment_count': len(service_equipment_payload['equipment']),
        'dimension_rows': dimension_rows,
        'matrices': matrices,
        'access_form': access_form,
        'access_rows': access_rows,
        'dimension_count': len(estrategia_dims),
        'mindco_viewer': is_mindco_user(request.user),
        'service_equipment_payload': service_equipment_payload,
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
    return render(request, 'core/service_dimensions.html', {
        'service': servicio,
        'permission': permission,
        'dim_cards': dim_cards,
    })


@login_required
def service_aca_list(request, pk):
    servicio, permission = _service_or_404(request, pk, edit=False)
    estrategia_dims = _strategy_dimensions(servicio.estrategia)

    excluded_dimension_names = {
        'Probabilidad - Enap',
        'Impacto - Enap',
        'Probabilidad - matri4x4',
        'Impacto - matri4x4',
    }

    estrategia_dims = [
        ed for ed in estrategia_dims
        if ed.dimension.nombre not in excluded_dimension_names
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
        ('indicador_criticidad', 'Indicador de Criticidad'),
        ('valor_criticidad_equipo', 'Valor Criticidad Equipo'),
        ('criticidad_final', 'Criticidad Final'),
        ('acciones', 'Acciones'),
    ])

    rows = []
    complete_count = 0
    incomplete_count = 0
    for crit in criticidades:
        status = getattr(crit.aca_carga, 'status', '') or models.AcaCarga.STATUS_COMPLETO
        if status == models.AcaCarga.STATUS_INCOMPLETO:
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
            'tag': crit.equipo.tag_equipo if crit.equipo else '',
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

    return render(request, 'core/service_aca_list.html', {
        'service': servicio,
        'permission': permission,
        'columns': columns,
        'rows': rows,
        'aca_count': len(rows),
        'complete_count': complete_count,
        'incomplete_count': incomplete_count,
    })


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
        and getattr(edit_crit.aca_carga, 'status', '') == models.AcaCarga.STATUS_INCOMPLETO
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
        status = models.AcaCarga.STATUS_INCOMPLETO if is_draft else models.AcaCarga.STATUS_COMPLETO
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
            carga = models.AcaCarga.objects.create(
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

    return render(request, 'core/aca_registro_form.html', {
        'service': servicio,
        'permission': permission,
        'base_form': base_form,
        'dimension_formset': dimension_formset,
        'selected_strategy': strategy,
        'matrix_selector': matrix_selector,
        'service_equipment_payload': _service_equipment_browser_payload(servicio),
        'auto_version': edit_crit.aca_carga.version_carga if edit_crit and edit_crit.aca_carga else _next_service_aca_version(servicio),
        'auto_fecha_analisis': edit_crit.aca_carga.fecha_analisis if edit_crit and edit_crit.aca_carga else timezone.localdate(),
        'editing_crit': edit_crit,
    })


@login_required
def service_aca_edit(request, service_pk, crit_pk):
    servicio, permission = _service_or_404(request, service_pk, edit=True)
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
    servicio, permission = _service_or_404(request, service_pk, edit=True)
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
    servicios_editables = [s for s in get_accessible_services(request.user) if get_service_permission(request.user, s)['can_edit']] if request.user.is_authenticated else []
    if len(servicios_editables) == 1:
        return redirect('service_aca_new', pk=servicios_editables[0].pk)
    messages.info(request, 'Selecciona primero un servicio para registrar un ACA.')
    return redirect('service_list')


# ---------------------------------------------------------------------------
# Registro ACA práctico
# ---------------------------------------------------------------------------





def _next_service_aca_version(servicio):
    latest = models.AcaCarga.objects.filter(servicio=servicio).order_by('-fecha_analisis', '-id').first()
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
        activo=True
    ).select_related('dimension').prefetch_related('catalogo')

    for item in dims:
        if item.dimension_id in matrix_input_dimension_ids:
            continue
        nombre = (item.dimension.nombre or '').strip().lower()
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


def _dimension_dependency_key(dimension):
    config = _json_loads_safe(getattr(dimension, 'config_calculo', None), {})
    if not isinstance(config, dict):
        return ''
    for key in ['dependencia', 'depende_de', 'source', 'fuente', 'campo_fuente', 'dimension_fuente']:
        value = config.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def _remember_source_value(source_values, estrategia_dimension, value):
    decimal_value = _decimal_or_none(value)
    if decimal_value is None:
        return
    dimension = estrategia_dimension.dimension
    campo = _dimension_source_key(estrategia_dimension)
    source_values[str(dimension.id)] = decimal_value
    source_values[dimension.nombre] = decimal_value
    source_values[_calc_slug(dimension.nombre)] = decimal_value
    if campo:
        source_values[campo] = decimal_value
        source_values[_calc_slug(campo)] = decimal_value


def _source_value(source_values, source_key):
    if not source_key:
        return None
    value = source_values.get(str(source_key))
    if value is None:
        value = source_values.get(_calc_slug(str(source_key)))
    return _decimal_or_none(value)


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

    rows = catalogo.filas.prefetch_related('celdas__columna').order_by('orden', 'id')
    for row in rows:
        values = row.values_map()
        lower = _catalog_bound(values, ['limite_inferior', 'desde', 'min', 'minimo', 'mínimo'])
        upper = _catalog_bound(values, ['limite_superior', 'hasta', 'max', 'maximo', 'máximo'])

        if lower is not None and source_value < lower:
            continue
        if upper is not None and source_value >= upper:
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
                'min', 'max', 'minimo', 'mínimo', 'maximo', 'máximo',
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
        return CriticidadDimensionFormSet(prefix='dims', form_kwargs={'estrategia': None})

    if not initial:
        dims_qs = (
            models.EstrategiaDimension.objects.filter(estrategia=estrategia, activo=True)
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
            form_kwargs={'estrategia': estrategia},
        )
    return CriticidadDimensionFormSet(
        initial=initial,
        prefix='dims',
        form_kwargs={'estrategia': estrategia},
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
        models.EstrategiaDimension.objects.filter(estrategia=estrategia, activo=True)
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


def _catalog_row_primary_numeric(row):
    if row is None:
        return None
    values = row.values_map()
    for key in ['valor_numerico', 'valor_principal', 'valor', 'nivel', 'puntaje']:
        if key in values and values.get(key) not in (None, ''):
            return _decimal_or_none(values.get(key))
    for key, value in values.items():
        if key in {'limite_inferior', 'limite_superior', 'desde', 'hasta', 'min', 'max', 'minimo', 'mínimo', 'maximo', 'máximo'}:
            continue
        decimal_value = _decimal_or_none(value)
        if decimal_value is not None:
            return decimal_value
    return None


def _catalog_row_text(row):
    if row is None:
        return ''
    values = row.values_map()
    for key in ['etiqueta', 'nombre', 'descripcion', 'texto', 'codigo']:
        value = values.get(key)
        if value not in (None, ''):
            return str(value)
    return str(row.etiqueta or '')


def _catalog_row_boolean(row):
    if row is None:
        return None
    values = row.values_map()
    for key in ['valor_booleano', 'booleano', 'flag']:
        value = values.get(key)
        if value in (True, False):
            return value
        if value not in (None, ''):
            return str(value).strip().lower() in {'true', '1', 'si', 'sí', 'yes'}
    return None


def _calc_slug(value):
    return re.sub(r'[^a-z0-9]+', '_', (value or '').strip().lower()).strip('_')


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
    candidates = []
    if isinstance(operand, dict):
        if operand.get('resultado') is True or operand.get('tipo') == 'resultado':
            return previous_result
        for key in ['campo', 'dimension_id', 'nombre', 'source']:
            value = operand.get(key)
            if value not in (None, ''):
                candidates.append(str(value))
    else:
        raw = str(operand)
        if raw in {'$resultado', '__resultado__', 'resultado_anterior'}:
            return previous_result
        candidates.append(raw)

    for candidate in candidates:
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
            'dependency_key': _dimension_dependency_key(dimension),
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


def _matrix_cell_for_axis_values(matriz, prob_val, impact_val):
    if not matriz or prob_val is None or impact_val is None:
        return None
    return models.MatrizRiesgoCelda.objects.filter(
        matriz=matriz,
        probabilidad__valor=prob_val,
        impacto_nivel__valor=impact_val,
    ).select_related(
        'probabilidad',
        'impacto_nivel',
    ).order_by('id').first()


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

    # 1) Priorizar las dimensiones específicas que usa la matriz
    if matriz:
        prob_val, impact_val = _matrix_axis_values_from_records(matriz, dims)

    # 2) Fallback: usar la frecuencia normalizada y/o el valor de consecuencia ya guardado
    if prob_val is None and evaluacion.frecuencia_normalizada is not None:
        prob_val = Decimal(evaluacion.frecuencia_normalizada)

    if impact_val is None and evaluacion.valor_cons_total is not None:
        impact_val = Decimal(evaluacion.valor_cons_total)

    # 3) Si existe matriz, intentar resolver la celda exacta por probabilidad + impacto
    if matriz and prob_val is not None and impact_val is not None:
        celda = _matrix_cell_for_axis_values(matriz, prob_val, impact_val)

        if celda:
            valor_cons_total = celda.impacto_nivel.valor
            prob_val = celda.probabilidad.valor
            valor_criticidad_equipo = celda.resultado_num
            indicador = f'[ {int(Decimal(valor_cons_total))} - {int(Decimal(prob_val))} ]'
            criticidad_final = celda.clasificacion or criticidad_final

    # 4) Fallback final: si no encontró celda, calcular con los niveles ya resueltos
    if valor_cons_total is None:
        valor_cons_total = impact_val

    if valor_criticidad_equipo is None and valor_cons_total is not None and prob_val is not None:
        try:
            valor_criticidad_equipo = Decimal(valor_cons_total) * Decimal(prob_val)
            indicador = f'[ {int(Decimal(valor_cons_total))} - {int(Decimal(prob_val))} ]'
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

def aca_registro_new_legacy(request):
    selected_service = None
    selected_strategy = None

    if request.method == 'POST':
        service_id = request.POST.get('servicio') or None
        strategy_id = request.POST.get('estrategia') or None
    else:
        service_id = request.GET.get('servicio') or None
        strategy_id = request.GET.get('estrategia') or None

    if service_id:
        selected_service = models.Servicio.objects.select_related('empresa', 'estrategia').filter(pk=service_id).first()
    if strategy_id:
        selected_strategy = models.Estrategia.objects.select_related('empresa').filter(pk=strategy_id).first()
    elif selected_service and selected_service.estrategia_id:
        selected_strategy = selected_service.estrategia

    if request.method == 'POST':
        base_form = ACARegistroForm(request.POST, service=selected_service, strategy=selected_strategy)
        selected_service = base_form.selected_service
        selected_strategy = base_form.selected_strategy
        is_preview = bool(request.POST.get('preview'))
        bind_dimensions = 'dims-TOTAL_FORMS' in request.POST and not is_preview
        dimension_formset = _dimension_formset(request, selected_strategy, bind_post=bind_dimensions)
        if bind_dimensions and base_form.is_valid() and dimension_formset.is_valid():
            cd = base_form.cleaned_data
            now = timezone.now()
            carga = models.AcaCarga.objects.filter(
                servicio=cd['servicio'],
                estrategia=cd['estrategia'],
                fecha_analisis=cd['fecha_analisis'],
                version_carga=cd['version_carga'],
            ).order_by('-id').first()
            if not carga:
                carga = models.AcaCarga.objects.create(
                    servicio=cd['servicio'],
                    estrategia=cd['estrategia'],
                    fecha_analisis=cd['fecha_analisis'],
                    version_carga=cd['version_carga'],
                    origen=cd['origen'],
                    status=models.AcaCarga.STATUS_COMPLETO,
                    usuario=cd['usuario'],
                    creado_en=now,
                    actualizado=now,
                )
            else:
                carga.actualizado = now
                carga.origen = cd['origen']
                carga.status = models.AcaCarga.STATUS_COMPLETO
                carga.usuario = cd['usuario']
                carga.save(update_fields=['actualizado', 'origen', 'status', 'usuario'])

            criticidad = models.Criticidad.objects.create(
                aca_carga=carga,
                equipo=cd['equipo'],
                escenario_falla=cd['escenario_falla'],
                frecuencia_original=cd['frecuencia_original'],
                frecuencia_normalizada=cd['frecuencia_normalizada'],
                valor_cons_total=None,
                indicador_criticidad='',
                valor_criticidad_equipo=None,
                criticidad_final='',
                creado_en=now,
            )
            _save_dimension_formset(criticidad, cd['estrategia'], dimension_formset)
            _sync_criticidad_resumen(criticidad, cd['estrategia'])
            messages.success(request, 'Registro ACA creado correctamente.')
            return redirect('model_detail', model_key='criticidad', pk=criticidad.pk)
    else:
        initial = {}
        if selected_service:
            initial['servicio'] = selected_service.pk
        if selected_strategy:
            initial['estrategia'] = selected_strategy.pk
        base_form = ACARegistroForm(initial=initial, service=selected_service, strategy=selected_strategy)
        selected_service = base_form.selected_service or selected_service
        selected_strategy = base_form.selected_strategy or selected_strategy
        dimension_formset = _dimension_formset(request, selected_strategy)

    return render(request, 'core/aca_registro_form.html', {
        'base_form': base_form,
        'dimension_formset': dimension_formset,
        'selected_service': selected_service,
        'selected_strategy': selected_strategy,
        'service_equipment_payload': _service_equipment_browser_payload(selected_service),
    })


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
        'activo': ed.activo,
        'columnas': [
            {
                'id': col.pk,
                'nombre_columna': col.nombre_columna,
                'clave_interna': col.clave_interna,
                'tipo_dato': col.tipo_dato,
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


def _serialize_strategy_dimension_without_catalog(ed):
    dimension = ed.dimension
    tipo = 'numerico_libre' if dimension.tipo_dato == 'numerico' and not dimension.tipo_calculo else 'opciones'
    return {
        # Si el catálogo fue eliminado por soft-delete anterior y luego reactivas
        # EstrategiaDimension.activo=1, esta entrada permite que vuelva a aparecer
        # en el editor sin duplicar la dimensión.
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
        'activo': ed.activo,
        'columnas': [] if dimension.tipo_calculo else _default_columns_for_type(tipo),
        'filas': [],
    }


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
    existing_eds = {str(obj.pk): obj for obj in models.EstrategiaDimension.objects.filter(estrategia=estrategia).select_related('dimension')}
    keep_ids = set()
    tipos_calculo_validos = dict(models.Dimension.TIPO_CALCULO_CHOICES)
    tipos_catalogo_validos = {'opciones', 'rangos', 'numerico_libre'}

    def _normalize_calculation_config(config_raw, tipo_calculo):
        config_raw = config_raw if isinstance(config_raw, dict) else {}
        valid_ops = set(tipos_calculo_validos.keys()) - {''}

        def _clean_operandos(value):
            if not isinstance(value, list):
                return []
            cleaned = []
            for operand in value:
                if isinstance(operand, dict):
                    cleaned.append(operand)
                elif operand not in (None, ''):
                    cleaned.append(str(operand))
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
            return {
                'pasos': steps,
                'operandos': steps[0]['operandos'],
            }

        operandos = _clean_operandos(
            config_raw.get('operandos') or config_raw.get('campos') or config_raw.get('sources') or []
        )
        return {'operandos': operandos}

    def _clean_catalogo(catalogo):
        models.CriticidadDimension.objects.filter(
            catalogo_fila__catalogo=catalogo
        ).update(catalogo_fila=None)

        models.DimensionCatalogoCelda.objects.filter(
            fila__catalogo=catalogo
        ).delete()

        models.DimensionCatalogoFila.objects.filter(
            catalogo=catalogo
        ).delete()

        models.DimensionCatalogoColumna.objects.filter(
            catalogo=catalogo
        ).delete()

    for idx, item in enumerate(payload, start=1):
        cat_id = str(item.get('id') or '').strip()
        nombre = str(item.get('nombre') or '').strip() or f'Dimensión {idx}'
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
                for key in ['dependencia', 'depende_de', 'source', 'fuente', 'campo_fuente', 'dimension_fuente']:
                    value = config_calculo_raw.get(key)
                    if value not in (None, ''):
                        dependencia = str(value).strip()
                        break
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
                tipo_funcional=(
                    tipo_funcional
                    if tipo_funcional in dict(models.Dimension.TIPO_FUNCIONAL_CHOICES)
                    else 'atributo'
                ),
                tipo_dato=(
                    tipo_dato
                    if tipo_dato in dict(models.Dimension.TIPO_DATO_CHOICES)
                    else 'tabla'
                ),
                tipo_calculo=tipo_calculo or None,
                config_calculo=config_calculo,
            )
            estrategia_dimension = models.EstrategiaDimension.objects.create(
                estrategia=estrategia,
                dimension=dimension,
                orden=idx,
                obligatorio=obligatorio,
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
            keep_ids.add(catalogo.pk)

        dimension.nombre = nombre
        dimension.descripcion = descripcion
        dimension.tipo_funcional = (
            tipo_funcional
            if tipo_funcional in dict(models.Dimension.TIPO_FUNCIONAL_CHOICES)
            else 'atributo'
        )
        dimension.tipo_dato = (
            tipo_dato
            if tipo_dato in dict(models.Dimension.TIPO_DATO_CHOICES)
            else 'tabla'
        )
        dimension.tipo_calculo = tipo_calculo or None
        dimension.config_calculo = config_calculo
        dimension.save()

        estrategia_dimension.orden = idx
        estrategia_dimension.obligatorio = obligatorio
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

                models.DimensionCatalogoCelda.objects.create(
                    fila=fila,
                    columna=col,
                    **normalized,
                )

    to_delete = models.DimensionCatalogo.objects.filter(
        estrategia_dimension__estrategia=estrategia,
        estrategia_dimension__activo=True,
    ).exclude(pk__in=keep_ids)

    for catalogo in to_delete:
        estrategia_dimension = catalogo.estrategia_dimension
        dimension = estrategia_dimension.dimension

        _clean_catalogo(catalogo)
        catalogo.delete()

        tiene_uso_historico = models.CriticidadDimension.objects.filter(
            estrategia_dimension_id=estrategia_dimension.id
        ).exists()

        if tiene_uso_historico:
            estrategia_dimension.activo = False
            estrategia_dimension.save(update_fields=['activo'])
            continue

        estrategia_dimension.delete()

        if not dimension.estrategias_dimension.exists() and not dimension.criticidades_dimension.exists():
            dimension.delete()


@transaction.atomic
def dimension_tables_editor(request, pk):
    _ensure_admin_access(request)
    estrategia = get_object_or_404(models.Estrategia.objects.select_related('empresa'), pk=pk)
    if request.method == 'POST':
        payload = _json_payload(request, 'payload_json', []) or []
        _save_strategy_catalogs(estrategia, payload)
        messages.success(request, 'Las dimensiones y catálogos se guardaron correctamente.')
        return redirect('dimension_tables_editor', pk=estrategia.pk)

    editor_payload = json.dumps(_json_safe(_strategy_catalogs_payload(estrategia, only_active=True)), ensure_ascii=False)
    return render(request, 'core/dimension_table_editor.html', {
        'estrategia': estrategia,
        'editor_payload_json': editor_payload,
        'tipos_funcionales': models.Dimension.TIPO_FUNCIONAL_CHOICES,
        'tipos_dato': models.Dimension.TIPO_DATO_CHOICES,
        'tipos_calculo': models.Dimension.TIPO_CALCULO_CHOICES,
        'tipos_columna': models.DimensionCatalogoColumna.TIPO_DATO_CHOICES,
    })


# ---------------------------------------------------------------------------
# Constructor visual de matrices
# ---------------------------------------------------------------------------
def _matrix_level_dicts(levels, count, prefix):
    data = []
    levels = list(levels)
    for idx in range(1, count + 1):
        obj = levels[idx - 1] if idx <= len(levels) else None
        data.append({
            'idx': idx,
            'nombre': getattr(obj, 'nombre', None) or f'{prefix.upper()}{idx}',
            'valor': _json_safe(getattr(obj, 'valor', None)) or idx,
            'descripcion': getattr(obj, 'descripcion', None) or '',
        })
    return data


def _level_defs_from_strategy_dimension(estrategia_dimension, count, prefix):
    if not estrategia_dimension:
        return _matrix_level_dicts([], count, prefix)

    escalas = list(estrategia_dimension.escalas_valor.order_by('nivel_ordinal', 'id'))
    if not escalas:
        try:
            catalogo = estrategia_dimension.catalogo
            filas_catalogo = list(
                catalogo.filas.prefetch_related('celdas__columna').order_by('orden', 'id')
            )
        except models.DimensionCatalogo.DoesNotExist:
            filas_catalogo = []
        if filas_catalogo:
            data = []
            for idx in range(1, count + 1):
                fila = filas_catalogo[idx - 1] if idx <= len(filas_catalogo) else None
                numeric_value = _catalog_row_primary_numeric(fila) if fila else None
                data.append({
                    'idx': idx,
                    'nombre': _catalog_row_text(fila) if fila else f'{prefix.upper()}{idx}',
                    'valor': _json_safe(numeric_value) if numeric_value is not None else idx,
                    'descripcion': fila.etiqueta if fila else '',
                })
            return data

    data = []
    for idx in range(1, count + 1):
        escala = escalas[idx - 1] if idx <= len(escalas) else None
        data.append({
            'idx': idx,
            'nombre': (escala.descripcion or escala.codigo) if escala else f'{prefix.upper()}{idx}',
            'valor': _json_safe(escala.valor_numerico) if escala else idx,
            'descripcion': escala.codigo if escala and escala.codigo else '',
        })
    return data


def _definitions_from_request(request, prob_count, impact_count, fallback_prob, fallback_impact):
    prob_defs = _json_payload(request, 'prob_levels_json', fallback_prob) or fallback_prob
    impact_defs = _json_payload(request, 'impact_levels_json', fallback_impact) or fallback_impact
    prob_defs = (prob_defs if isinstance(prob_defs, list) else fallback_prob)[:prob_count]
    impact_defs = (impact_defs if isinstance(impact_defs, list) else fallback_impact)[:impact_count]

    while len(prob_defs) < prob_count:
        idx = len(prob_defs) + 1
        prob_defs.append({'idx': idx, 'nombre': f'P{idx}', 'valor': idx, 'descripcion': ''})
    while len(impact_defs) < impact_count:
        idx = len(impact_defs) + 1
        impact_defs.append({'idx': idx, 'nombre': f'I{idx}', 'valor': idx, 'descripcion': ''})

    return prob_defs, impact_defs


def _cell_data_from_request(request):
    payload = _json_payload(request, 'matrix_cells_json', []) or []
    result = {}
    rows = payload if isinstance(payload, list) else []
    for row in rows:
        if not isinstance(row, list):
            continue
        for cell in row:
            if not isinstance(cell, dict):
                continue
            prob_idx = _int_or_default(cell.get('prob_idx'), None)
            impact_idx = _int_or_default(cell.get('impact_idx'), None)
            if not prob_idx or not impact_idx:
                continue
            result[(prob_idx, impact_idx)] = {
                'prob_idx': prob_idx,
                'impact_idx': impact_idx,
                'clasificacion': str(cell.get('clasificacion') or ''),
                'color': str(cell.get('color') or '#2a2a3a'),
                'resultado_num': _decimal_or_none(cell.get('resultado_num')),
                'calcular': bool(cell.get('calcular', True)),
            }
    return result


def _matrix_value_bounds(prob_defs, impact_defs):
    values = []
    for prob in prob_defs:
        for impact in impact_defs:
            try:
                values.append(int(Decimal(str(prob['valor'])) * Decimal(str(impact['valor']))))
            except (InvalidOperation, ValueError, TypeError):
                continue
    return (min(values), max(values)) if values else (1, 1)


def _safe_legend_items(raw_items):
    if not isinstance(raw_items, list):
        return []
    cleaned = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        range_text = str(item.get('range') or '').strip()
        color = str(item.get('color') or '#6c63ff').strip() or '#6c63ff'
        if not name and not range_text:
            continue
        cleaned.append({'name': name, 'range': range_text, 'color': color})
    return cleaned


def _default_legend_for_bounds(min_value, max_value):
    min_value = int(min_value)
    max_value = int(max_value)
    span = max(max_value - min_value + 1, 1)
    step = max(span // 4, 1)
    cuts = [min_value, min_value + step, min_value + (step * 2), min_value + (step * 3), max_value + 1]
    labels = [('Bajo', '#2ecc71'), ('Medio', '#f1c40f'), ('Alto', '#e67e22'), ('Crítico', '#e74c3c')]
    items = []
    current = min_value
    for idx, (label, color) in enumerate(labels):
        end = cuts[idx + 1] - 1 if idx < 3 else max_value
        end = min(max_value, max(current, end))
        items.append({'name': label, 'range': f'{current}-{end}', 'color': color})
        current = end + 1
    items[-1]['range'] = f"{items[-1]['range'].split('-')[0]}-{max_value}"
    return items


def _validate_legend_items(raw_items, min_value, max_value):
    items = _safe_legend_items(raw_items)
    if not items:
        return None, 'Debes definir al menos un rango en la leyenda.'

    parsed = []
    for item in items:
        if not item['name']:
            return None, 'Cada rango de la leyenda debe tener un nombre.'
        match = _RANGE_RE.match(item['range'])
        if not match:
            return None, f"El rango '{item['range']}' no tiene un formato válido. Usa por ejemplo 1-4."
        start, end = int(match.group(1)), int(match.group(2))
        if end < start:
            return None, f"El rango '{item['range']}' no es válido porque el final es menor que el inicio."
        parsed.append({'name': item['name'], 'range': f'{start}-{end}', 'color': item['color'], 'start': start, 'end': end})

    parsed.sort(key=lambda item: item['start'])
    expected = int(min_value)
    if parsed[0]['start'] != expected:
        return None, f'La leyenda debe comenzar exactamente en {expected}.'
    for item in parsed:
        if item['start'] != expected:
            return None, f'Los rangos no pueden saltarse valores. Después de {expected - 1} debe venir {expected}.'
        expected = item['end'] + 1
    if expected - 1 != int(max_value):
        return None, f'La leyenda debe terminar exactamente en {int(max_value)}.'
    return [{'name': item['name'], 'range': item['range'], 'color': item['color']} for item in parsed], None


def _legend_from_matrix(matriz, min_value=None, max_value=None):
    try:
        legend = json.loads(matriz.leyenda_json or '[]')
    except json.JSONDecodeError:
        legend = []
    legend = _safe_legend_items(legend)
    if legend:
        return legend
    if min_value is None or max_value is None:
        prob_defs = _matrix_level_dicts(matriz.niveles_probabilidad.order_by('orden_visual', 'id'), matriz.niveles_probabilidad.count() or 5, 'prob')
        impact_defs = _matrix_level_dicts(matriz.niveles_impacto.order_by('orden_visual', 'id'), matriz.niveles_impacto.count() or 5, 'impact')
        min_value, max_value = _matrix_value_bounds(prob_defs, impact_defs)
    return _default_legend_for_bounds(min_value, max_value)


def _match_legend(result_value, legend_items):
    for item in legend_items:
        match = _RANGE_RE.match(item['range'])
        if not match:
            continue
        start, end = int(match.group(1)), int(match.group(2))
        if start <= int(result_value) <= end:
            return item['name'], item['color']
    return '', '#2a2a3a'


def _matrix_preview_from_defs(selected_axis, prob_defs, impact_defs, existing_cells=None, strategy=None, prob_dim=None, impact_dim=None, legend_items=None):
    existing_cells = existing_cells or {}
    legend_items = legend_items or _default_legend_for_bounds(*_matrix_value_bounds(prob_defs, impact_defs))
    x_defs = impact_defs if selected_axis == 'impacto' else prob_defs
    y_defs = prob_defs if selected_axis == 'impacto' else impact_defs
    rows = []
    for y_idx, y_def in enumerate(y_defs, start=1):
        row = {'header': y_def, 'cells': []}
        for x_idx, x_def in enumerate(x_defs, start=1):
            prob_idx = y_idx if selected_axis == 'impacto' else x_idx
            impact_idx = x_idx if selected_axis == 'impacto' else y_idx
            existing = existing_cells.get((prob_idx, impact_idx))
            if existing and hasattr(existing, 'resultado_num'):
                result_num = existing.resultado_num
                clasificacion = existing.clasificacion
                color = existing.color
                calcular = existing.calcular
            elif isinstance(existing, dict):
                result_num = existing.get('resultado_num')
                clasificacion = existing.get('clasificacion') or ''
                color = existing.get('color') or '#2a2a3a'
                calcular = existing.get('calcular', True)
            else:
                try:
                    result_num = Decimal(str(prob_defs[prob_idx - 1]['valor'])) * Decimal(str(impact_defs[impact_idx - 1]['valor']))
                except (InvalidOperation, ValueError, TypeError):
                    result_num = Decimal('0')
                clasificacion, color = _match_legend(int(result_num), legend_items)
                calcular = True
            row['cells'].append({
                'prob_idx': prob_idx,
                'impact_idx': impact_idx,
                'resultado_num': _json_safe(result_num),
                'clasificacion': clasificacion,
                'color': color,
                'calcular': calcular,
            })
        rows.append(row)
    return {
        'x_axis': selected_axis,
        'x_defs': x_defs,
        'rows': rows,
        'prob_defs': prob_defs,
        'impact_defs': impact_defs,
        'strategy': strategy,
        'prob_dim': prob_dim,
        'impact_dim': impact_dim,
    }


def _matrix_ui_payload(matrix_preview, stored_legend=None):
    return {
        'rows_count': len(matrix_preview['rows']),
        'cols_count': len(matrix_preview['x_defs']),
        'prob_defs': _json_safe(matrix_preview['prob_defs']),
        'impact_defs': _json_safe(matrix_preview['impact_defs']),
        'legend': stored_legend or [],
    }


@transaction.atomic
def _sync_matrix_levels(matriz, model_cls, definitions):
    existing = list(model_cls.objects.filter(matriz=matriz).order_by('orden_visual', 'id'))
    keep_ids = []
    result = []
    for idx, definition in enumerate(definitions, start=1):
        obj = existing[idx - 1] if idx <= len(existing) else model_cls(matriz=matriz)
        obj.nombre = str(definition.get('nombre') or f'N{idx}')
        obj.valor = _decimal_or_none(definition.get('valor')) or Decimal(idx)
        obj.descripcion = str(definition.get('descripcion') or '')
        obj.orden_visual = idx
        obj.save()
        keep_ids.append(obj.pk)
        result.append(obj)
    model_cls.objects.filter(matriz=matriz).exclude(pk__in=keep_ids).delete()
    return result


def _next_strategy_order(estrategia):
    current = models.EstrategiaDimension.objects.filter(estrategia=estrategia, activo=True).order_by('-orden').first()
    return (current.orden if current else 0) + 1


def _ensure_matrix_strategy_dimensions_legacy(estrategia, matrix_name, prob_defs, impact_defs, existing_prob=None, existing_impact=None):
    prob_dim = existing_prob if existing_prob and existing_prob.estrategia_id == estrategia.pk else None
    impact_dim = existing_impact if existing_impact and existing_impact.estrategia_id == estrategia.pk else None
    next_order = _next_strategy_order(estrategia)
    if not prob_dim:
        prob_dimension = models.Dimension.objects.create(
            nombre=f'Probabilidad - {matrix_name}',
            descripcion=f'Dimensión creada automáticamente para la matriz {matrix_name}',
            tipo_funcional='probabilidad',
            tipo_dato='numerico',
        )
        prob_dim = models.EstrategiaDimension.objects.create(
            estrategia=estrategia,
            dimension=prob_dimension,
            orden=next_order,
            obligatorio=True,
            activo=True,
        )
        next_order += 1
    else:
        prob_dimension = prob_dim.dimension
        prob_dimension.nombre = f'Probabilidad - {matrix_name}'
        prob_dimension.descripcion = f'Dimensión creada automáticamente para la matriz {matrix_name}'
        prob_dimension.tipo_funcional = 'probabilidad'
        prob_dimension.tipo_dato = 'numerico'
        prob_dimension.save()
        prob_dim.obligatorio = True
        prob_dim.activo = True
        prob_dim.save(update_fields=['obligatorio', 'activo'])

    if not impact_dim:
        impact_dimension = models.Dimension.objects.create(
            nombre=f'Impacto - {matrix_name}',
            descripcion=f'Dimensión creada automáticamente para la matriz {matrix_name}',
            tipo_funcional='impacto',
            tipo_dato='numerico',
        )
        impact_dim = models.EstrategiaDimension.objects.create(
            estrategia=estrategia,
            dimension=impact_dimension,
            orden=next_order,
            obligatorio=True,
            activo=True,
        )
    else:
        impact_dimension = impact_dim.dimension
        impact_dimension.nombre = f'Impacto - {matrix_name}'
        impact_dimension.descripcion = f'Dimensión creada automáticamente para la matriz {matrix_name}'
        impact_dimension.tipo_funcional = 'impacto'
        impact_dimension.tipo_dato = 'numerico'
        impact_dimension.save()
        impact_dim.obligatorio = True
        impact_dim.activo = True
        impact_dim.save(update_fields=['obligatorio', 'activo'])

    return prob_dim, impact_dim


def _matrix_axis_prefix(axis):
    return 'Probabilidad' if axis == 'probabilidad' else 'Impacto'


def _is_generated_matrix_axis_dimension(estrategia_dimension, axis=None):
    if not estrategia_dimension:
        return False
    name = (estrategia_dimension.dimension.nombre or '').strip().lower()
    prefixes = []
    if axis in (None, 'probabilidad'):
        prefixes.append('probabilidad - ')
    if axis in (None, 'impacto'):
        prefixes.append('impacto - ')
    return any(name.startswith(prefix) for prefix in prefixes)


def _update_generated_matrix_axis_dimension(estrategia_dimension, axis, matrix_name):
    prefix = _matrix_axis_prefix(axis)
    dimension = estrategia_dimension.dimension
    dimension.nombre = f'{prefix} - {matrix_name}'
    dimension.descripcion = f'Dimension creada automaticamente para la matriz {matrix_name}'
    dimension.tipo_funcional = axis
    dimension.tipo_dato = 'numerico'
    dimension.save()
    estrategia_dimension.obligatorio = True
    estrategia_dimension.activo = True
    estrategia_dimension.save(update_fields=['obligatorio', 'activo'])
    return estrategia_dimension


def _create_generated_matrix_axis_dimension(estrategia, axis, matrix_name, order):
    prefix = _matrix_axis_prefix(axis)
    dimension = models.Dimension.objects.create(
        nombre=f'{prefix} - {matrix_name}',
        descripcion=f'Dimension creada automaticamente para la matriz {matrix_name}',
        tipo_funcional=axis,
        tipo_dato='numerico',
    )
    return models.EstrategiaDimension.objects.create(
        estrategia=estrategia,
        dimension=dimension,
        orden=order,
        obligatorio=True,
        activo=True,
    )


def _resolve_matrix_axis_dimension(estrategia, axis, matrix_name, next_order, selected=None, existing=None):
    if selected and selected.estrategia_id == estrategia.pk:
        return selected, next_order

    if existing and existing.estrategia_id == estrategia.pk and _is_generated_matrix_axis_dimension(existing, axis):
        return _update_generated_matrix_axis_dimension(existing, axis, matrix_name), next_order

    created = _create_generated_matrix_axis_dimension(estrategia, axis, matrix_name, next_order)
    return created, next_order + 1


def _ensure_matrix_strategy_dimensions(
    estrategia,
    matrix_name,
    prob_defs,
    impact_defs,
    existing_prob=None,
    existing_impact=None,
    selected_prob=None,
    selected_impact=None,
):
    next_order = _next_strategy_order(estrategia)
    prob_dim, next_order = _resolve_matrix_axis_dimension(
        estrategia,
        'probabilidad',
        matrix_name,
        next_order,
        selected=selected_prob,
        existing=existing_prob,
    )
    impact_dim, next_order = _resolve_matrix_axis_dimension(
        estrategia,
        'impacto',
        matrix_name,
        next_order,
        selected=selected_impact,
        existing=existing_impact,
    )
    return prob_dim, impact_dim


@transaction.atomic
def _persist_matrix_grid(matriz, prob_defs, impact_defs, cell_payload):
    prob_levels = _sync_matrix_levels(matriz, models.NivelProbabilidad, prob_defs)
    impact_levels = _sync_matrix_levels(matriz, models.NivelImpacto, impact_defs)

    prob_by_idx = {idx + 1: level for idx, level in enumerate(prob_levels)}
    impact_by_idx = {idx + 1: level for idx, level in enumerate(impact_levels)}

    keep_ids = []
    existing = {
        (cell.probabilidad.orden_visual, cell.impacto_nivel.orden_visual): cell
        for cell in models.MatrizRiesgoCelda.objects.filter(matriz=matriz).select_related('probabilidad', 'impacto_nivel')
    }

    for prob_idx, prob in prob_by_idx.items():
        for impact_idx, impact in impact_by_idx.items():
            payload = cell_payload.get((prob_idx, impact_idx), {})
            cell = existing.get((prob_idx, impact_idx)) or models.MatrizRiesgoCelda(matriz=matriz, probabilidad=prob, impacto_nivel=impact)
            result_num = payload.get('resultado_num')
            if result_num is None:
                try:
                    result_num = Decimal(str(prob.valor)) * Decimal(str(impact.valor))
                except (InvalidOperation, ValueError, TypeError):
                    result_num = Decimal('0')
            cell.resultado_num = result_num
            cell.clasificacion = str(payload.get('clasificacion') or '')
            cell.color = str(payload.get('color') or '#2a2a3a')
            cell.calcular = bool(payload.get('calcular', True))
            cell.save()
            keep_ids.append(cell.pk)

    models.MatrizRiesgoCelda.objects.filter(matriz=matriz).exclude(pk__in=keep_ids).delete()


@transaction.atomic
def matriz_builder_new(request):
    _ensure_admin_access(request)
    display_legend = None
    if request.method == 'POST':
        strategy = request.POST.get('estrategia') or None
        builder_form = MatrizBuilderForm(request.POST, strategy=strategy)
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
            raw_legend_items = _json_payload(request, 'legend_items_json', []) or []
            legend_items, legend_error = _validate_legend_items(raw_legend_items, min_value, max_value)
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
                    leyenda_json=json.dumps(legend_items, ensure_ascii=False),
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
            'x_count': 5,
            'y_count': 5,
        })
        matrix_preview = _matrix_preview_from_defs('impacto', _matrix_level_dicts([], 5, 'p'), _matrix_level_dicts([], 5, 'i'))

    return render(request, 'core/matrix_builder.html', {
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
        builder_form = MatrizBuilderForm(request.POST, strategy=strategy)
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
            raw_legend_items = _json_payload(request, 'legend_items_json', []) or []
            legend_items, legend_error = _validate_legend_items(raw_legend_items, min_value, max_value)
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
                matriz.leyenda_json = json.dumps(legend_items, ensure_ascii=False)
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
            'x_count': len(matrix_preview['x_defs']),
            'y_count': len(matrix_preview['rows']),
        }, strategy=matriz.estrategia)

    return render(request, 'core/matrix_builder.html', {
        'is_create': False,
        'matriz': matriz,
        'builder_form': builder_form,
        'matrix_preview': matrix_preview,
        'matrix_ui_state': _matrix_ui_payload(matrix_preview, stored_legend=display_legend or []),
    })
