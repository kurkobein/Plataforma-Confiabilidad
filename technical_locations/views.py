import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from core import models
from technical_locations.forms import (
    HierarchyBulkValueForm,
    HierarchyInsertLevelForm,
    HierarchyMoveNodeForm,
    HierarchyRouteFormSet,
    HierarchyStructureFormSet,
    HierarchyValueForm,
)
from core.views import (
    _active_subtree_ids,
    _ensure_admin_access,
    _equipment_path_rows,
    _node_route_from_path,
    _node_ut_from_path,
    _path_ids_for_node,
)


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
    items = [
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
    items.extend(
        {
            'id': f'simple-{value.pk}',
            'empresa_id': value.empresa_id,
            'parent_id': None,
            'nivel_id': value.nivel_id,
            'level_order': value.nivel.orden,
            'code': value.codigo,
            'name': value.nombre,
            'label': f'{value.codigo} - {value.nombre} (valor simple)',
            'ut': value.codigo,
            'route': value.nombre,
            'is_simple': True,
        }
        for value in models.ValorNivelJerarquia.objects.filter(
            empresa=empresa,
            activo=True,
        ).select_related(
            'nivel',
        ).order_by(
            'nivel__orden',
            'orden',
            'nombre',
        )
    )
    return items


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
    return (
        models.NodoJerarquia.objects.filter(
            empresa=empresa,
            activo=True,
        ).aggregate(max_depth=Max('nivel__orden')).get('max_depth')
        or 0
    )


def _move_levels_to_temporary_orders(empresa, levels):
    max_order = max((level.orden for level in levels), default=0)
    temp_base = max_order + len(levels) + 1000
    for index, level in enumerate(levels, start=1):
        level.orden = temp_base + index
        level.nombre = f'__tmp_nivel_{empresa.pk}_{level.pk}'
        level.save(update_fields=['orden', 'nombre'])


def _node_depth_groups(empresa):
    rows = list(
        models.NodoJerarquia.objects.filter(
            empresa=empresa,
            activo=True,
        ).values(
            'id',
            'parent_id',
            'nivel_id',
        )
    )
    parent_by_id = {row['id']: row['parent_id'] for row in rows}
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

    groups = {}
    for row in rows:
        depth = depth_for(row['id'])
        groups.setdefault(depth, []).append(row)
    return groups


def _sync_hierarchy_node_levels(empresa, levels_by_order):
    chunk_size = 1000
    for depth, rows in _node_depth_groups(empresa).items():
        level = levels_by_order.get(depth)
        if not level:
            continue
        stale_ids = [row['id'] for row in rows if row['nivel_id'] != level.pk]
        for index in range(0, len(stale_ids), chunk_size):
            models.NodoJerarquia.objects.filter(pk__in=stale_ids[index:index + chunk_size]).update(nivel=level)


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
    original_active_by_order = {
        level.orden: level.pk
        for level in existing_levels
        if level.activo
    }
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
    new_active_by_order = {
        order: level.pk
        for order, level in active_levels_by_order.items()
    }
    needs_node_sync = any(
        original_active_by_order.get(order) != new_active_by_order.get(order)
        for order in range(1, deepest_route + 1)
    )
    if needs_node_sync:
        _sync_hierarchy_node_levels(empresa, active_levels_by_order)


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
            selected_node_id = str(selected_node_id)
            if selected_node_id.startswith('simple-'):
                simple_value = models.ValorNivelJerarquia.objects.filter(
                    empresa=empresa,
                    pk=selected_node_id.removeprefix('simple-'),
                    nivel=level,
                    activo=True,
                ).first()
                if not simple_value:
                    raise ValueError(f'El valor simple seleccionado para {level.nombre} ya no esta disponible.')
                sibling_order = models.NodoJerarquia.objects.filter(
                    empresa=empresa,
                    parent=parent,
                    nivel=level,
                ).count() + 1
                node, created = models.NodoJerarquia.objects.get_or_create(
                    empresa=empresa,
                    parent=parent,
                    codigo=simple_value.codigo,
                    defaults={
                        'nivel': level,
                        'nombre': simple_value.nombre,
                        'orden': sibling_order,
                        'activo': True,
                    },
                )
                if not created:
                    node.nivel = level
                    node.nombre = simple_value.nombre
                    node.activo = True
                    node.save(update_fields=['nivel', 'nombre', 'activo'])
                parent = node
                last_node = node
                continue
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


def _save_hierarchy_values_bulk(empresa, form):
    level = form.cleaned_data['nivel']
    parent = form.cleaned_data.get('parent')
    rows = form.cleaned_data.get('bulk_rows') or []
    if form.cleaned_data.get('sin_nodo_superior'):
        next_order = models.ValorNivelJerarquia.objects.filter(
            empresa=empresa,
            nivel=level,
        ).aggregate(
            max_order=Max('orden'),
        ).get('max_order') or 0
        created = 0
        updated = 0
        for row in rows:
            value = models.ValorNivelJerarquia.objects.filter(
                empresa=empresa,
                nivel=level,
                codigo=row['codigo'],
            ).first()
            if value:
                changed_fields = []
                if value.nombre != row['nombre']:
                    value.nombre = row['nombre']
                    changed_fields.append('nombre')
                if not value.activo:
                    value.activo = True
                    changed_fields.append('activo')
                if changed_fields:
                    value.save(update_fields=changed_fields)
                updated += 1
                continue
            next_order += 1
            models.ValorNivelJerarquia.objects.create(
                empresa=empresa,
                nivel=level,
                codigo=row['codigo'],
                nombre=row['nombre'],
                orden=next_order,
                activo=True,
            )
            created += 1
        return {
            'mode': 'simple',
            'created': created,
            'updated': updated,
            'duplicates': getattr(form, 'duplicate_rows', 0),
            'total': len(rows),
        }

    next_order = models.NodoJerarquia.objects.filter(
        empresa=empresa,
        parent=parent,
        nivel=level,
    ).aggregate(
        max_order=Max('orden'),
    ).get('max_order') or 0
    created = 0
    updated = 0
    for row in rows:
        node = models.NodoJerarquia.objects.filter(
            empresa=empresa,
            parent=parent,
            codigo=row['codigo'],
        ).first()
        if node:
            changed_fields = []
            if node.nivel_id != level.pk:
                node.nivel = level
                changed_fields.append('nivel')
            if node.nombre != row['nombre']:
                node.nombre = row['nombre']
                changed_fields.append('nombre')
            if not node.activo:
                node.activo = True
                changed_fields.append('activo')
            if changed_fields:
                node.save(update_fields=changed_fields)
            updated += 1
            continue
        next_order += 1
        models.NodoJerarquia.objects.create(
            empresa=empresa,
            nivel=level,
            parent=parent,
            codigo=row['codigo'],
            nombre=row['nombre'],
            orden=next_order,
            activo=True,
        )
        created += 1
    return {
        'mode': 'hierarchy',
        'created': created,
        'updated': updated,
        'duplicates': getattr(form, 'duplicate_rows', 0),
        'total': len(rows),
    }


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
    return render(request, 'technical_location_index.html', {
        'rows': rows,
    })


@login_required
def hierarchy_tree(request, empresa_id):
    _ensure_admin_access(request)
    empresa = get_object_or_404(models.Empresa, pk=empresa_id)
    levels = list(models.NivelJerarquia.objects.filter(empresa=empresa, activo=True).order_by('orden'))
    return render(request, 'hierarchy_tree.html', {
        'empresa': empresa,
        'levels': levels,
        'nodes_url': reverse('hierarchy_values_nodes', kwargs={'empresa_id': empresa.pk}),
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
    return render(request, 'hierarchy_structure_form.html', {
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
        action = request.POST.get('action') or 'single'
        if action == 'bulk_create':
            form = HierarchyValueForm(empresa=empresa)
            bulk_form = HierarchyBulkValueForm(request.POST, empresa=empresa, prefix='bulk')
            if not levels:
                messages.error(request, 'Primero define la estructura base de la ubicacion tecnica para esta empresa.')
            elif bulk_form.is_valid():
                with transaction.atomic():
                    result = _save_hierarchy_values_bulk(empresa, bulk_form)
                message = (
                    f'Carga rapida completada: {result["created"]} creados, '
                    f'{result["updated"]} actualizados.'
                )
                if result.get('mode') == 'simple':
                    message += ' Se guardaron como valores simples del nivel, sin modificar rutas UT existentes.'
                if result['duplicates']:
                    message += f' {result["duplicates"]} filas duplicadas dentro del pegado fueron omitidas.'
                messages.success(request, message)
                return redirect('hierarchy_values', empresa_id=empresa.pk)
        else:
            form = HierarchyValueForm(request.POST, empresa=empresa)
            bulk_form = HierarchyBulkValueForm(empresa=empresa, prefix='bulk')
            if not levels:
                messages.error(request, 'Primero define la estructura base de la ubicacion tecnica para esta empresa.')
            elif form.is_valid():
                node = _save_hierarchy_value(empresa, form)
                messages.success(request, f'Valor guardado: {node.codigo} - {node.nombre}')
                return redirect('hierarchy_values', empresa_id=empresa.pk)
    else:
        form = HierarchyValueForm(empresa=empresa)
        bulk_form = HierarchyBulkValueForm(empresa=empresa, prefix='bulk')

    return render(request, 'hierarchy_values.html', {
        'empresa': empresa,
        'form': form,
        'bulk_form': bulk_form,
        'levels': levels,
        'hierarchy_levels_json': json.dumps([
            {'id': level.pk, 'order': level.orden, 'name': level.nombre}
            for level in levels
        ], ensure_ascii=False),
    })


def _hierarchy_value_node_payload(node, rows_by_id=None):
    if rows_by_id:
        path_ids = _path_ids_for_node(node.pk, rows_by_id)
        ut = _node_ut_from_path(path_ids, rows_by_id)
        route = _node_route_from_path(path_ids, rows_by_id)
        parent_label = _node_ut_from_path(_path_ids_for_node(node.parent_id, rows_by_id), rows_by_id) if node.parent_id else 'Raiz'
    else:
        ut = node.ut
        route = node.ruta_nombre
        parent_label = node.parent.ut if node.parent_id and node.parent else 'Raiz'

    return {
        'id': node.pk,
        'empresa_id': node.empresa_id,
        'parent_id': node.parent_id,
        'level_id': node.nivel_id,
        'level_order': node.nivel.orden,
        'level_name': node.nivel.nombre,
        'code': node.codigo,
        'name': node.nombre,
        'ut': ut,
        'route': route,
        'parent_label': parent_label,
        'label': f'{node.codigo} - {node.nombre}',
        'equipment_count': getattr(node, 'equipment_count', None) if getattr(node, 'equipment_count', None) is not None else node.equipos.count(),
        'children_count': getattr(node, 'children_count', None) if getattr(node, 'children_count', None) is not None else node.hijos.filter(activo=True).count(),
        'edit_url': reverse('model_update', kwargs={'model_key': 'nodojerarquia', 'pk': node.pk}),
        'move_url': reverse('hierarchy_move_node', kwargs={'pk': node.pk}),
        'insert_url': reverse('hierarchy_insert_between', kwargs={'pk': node.pk}),
        'delete_url': reverse('hierarchy_delete_node', kwargs={'pk': node.pk}),
    }


def _hierarchy_simple_value_payload(value):
    return {
        'id': f'simple-{value.pk}',
        'empresa_id': value.empresa_id,
        'parent_id': None,
        'level_id': value.nivel_id,
        'level_order': value.nivel.orden,
        'level_name': value.nivel.nombre,
        'code': value.codigo,
        'name': value.nombre,
        'ut': value.codigo,
        'route': value.nombre,
        'parent_label': 'Catalogo simple',
        'label': f'{value.codigo} - {value.nombre}',
        'equipment_count': 0,
        'children_count': 0,
        'is_simple': True,
    }


def _hierarchy_values_base_queryset(empresa):
    return models.NodoJerarquia.objects.filter(
        empresa=empresa,
        activo=True,
    ).select_related(
        'nivel',
        'parent',
    ).annotate(
        equipment_count=Count('equipos', distinct=True),
        children_count=Count('hijos', filter=Q(hijos__activo=True), distinct=True),
    ).order_by(
        'nivel__orden',
        'orden',
        'codigo',
        'nombre',
    )


@login_required
def hierarchy_values_nodes(request, empresa_id):
    _ensure_admin_access(request)
    empresa = get_object_or_404(models.Empresa, pk=empresa_id)
    parent_id = (request.GET.get('parent_id') or '').strip()
    level_id = (request.GET.get('level_id') or '').strip()
    query = (request.GET.get('q') or '').strip()
    try:
        limit = min(max(int(request.GET.get('limit') or 500), 1), 1000)
    except (TypeError, ValueError):
        limit = 500
    try:
        offset = max(int(request.GET.get('offset') or 0), 0)
    except (TypeError, ValueError):
        offset = 0

    qs = _hierarchy_values_base_queryset(empresa)
    if parent_id and parent_id != 'root':
        parent = get_object_or_404(qs, pk=parent_id)
        qs = qs.filter(parent=parent)
    elif parent_id == 'root' or (not parent_id and not query and not level_id):
        qs = qs.filter(parent__isnull=True)

    if level_id:
        qs = qs.filter(nivel_id=level_id)

    if query:
        query_clause = Q(codigo__icontains=query) | Q(nombre__icontains=query)
        if '-' in query:
            query_clause |= Q(codigo__icontains=models._technical_segment(query.split('-')[-1]))
        qs = qs.filter(query_clause)

    total = qs.count()
    nodes = list(qs[offset:offset + limit])
    rows_by_id = _equipment_path_rows({node.pk for node in nodes} | {node.parent_id for node in nodes if node.parent_id})
    next_offset = offset + len(nodes)
    return JsonResponse({
        'nodes': [_hierarchy_value_node_payload(node, rows_by_id=rows_by_id) for node in nodes],
        'count': total,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_more': next_offset < total,
        'truncated': total > limit,
    })


@login_required
def hierarchy_values_search(request, empresa_id):
    _ensure_admin_access(request)
    empresa = get_object_or_404(models.Empresa, pk=empresa_id)
    parent_id = (request.GET.get('parent_id') or '').strip()
    level_id = (request.GET.get('level_id') or '').strip()
    query = (request.GET.get('q') or '').strip()
    try:
        limit = min(max(int(request.GET.get('limit') or 200), 1), 500)
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(int(request.GET.get('offset') or 0), 0)
    except (TypeError, ValueError):
        offset = 0

    if not parent_id and not level_id and len(query) < 2:
        return JsonResponse({
            'items': [],
            'count': 0,
            'requires_filter': True,
            'message': 'Selecciona un nodo superior o escribe al menos 2 caracteres para buscar.',
        })

    qs = _hierarchy_values_base_queryset(empresa)
    if parent_id == 'root':
        qs = qs.filter(parent__isnull=True)
    elif parent_id:
        parent = get_object_or_404(_hierarchy_values_base_queryset(empresa), pk=parent_id)
        qs = qs.filter(parent=parent)
    if level_id:
        qs = qs.filter(nivel_id=level_id)
    if query:
        query_clause = Q(codigo__icontains=query) | Q(nombre__icontains=query)
        if '-' in query:
            query_clause |= Q(codigo__icontains=models._technical_segment(query.split('-')[-1]))
        qs = qs.filter(query_clause)

    total = qs.count()
    nodes = list(qs[offset:offset + limit])
    rows_by_id = _equipment_path_rows({node.pk for node in nodes} | {node.parent_id for node in nodes if node.parent_id})
    node_items = [_hierarchy_value_node_payload(node, rows_by_id=rows_by_id) for node in nodes]
    simple_items = []
    simple_total = 0
    simple_level_id = level_id or None
    if parent_id and parent_id != 'root' and not simple_level_id:
        parent_for_simple = models.NodoJerarquia.objects.filter(
            empresa=empresa,
            pk=parent_id,
            activo=True,
        ).select_related('nivel').first()
        if parent_for_simple:
            next_level = models.NivelJerarquia.objects.filter(
                empresa=empresa,
                activo=True,
                orden=parent_for_simple.nivel.orden + 1,
            ).first()
            simple_level_id = next_level.pk if next_level else None
    elif parent_id == 'root' and not simple_level_id:
        first_level = models.NivelJerarquia.objects.filter(
            empresa=empresa,
            activo=True,
            orden=1,
        ).first()
        simple_level_id = first_level.pk if first_level else None

    if simple_level_id or not parent_id:
        simple_qs = models.ValorNivelJerarquia.objects.filter(
            empresa=empresa,
            activo=True,
        ).select_related(
            'nivel',
        )
        if simple_level_id:
            simple_qs = simple_qs.filter(nivel_id=simple_level_id)
        if query:
            simple_qs = simple_qs.filter(Q(codigo__icontains=query) | Q(nombre__icontains=query))
        simple_total = simple_qs.count()
        simple_values = list(simple_qs.order_by('nivel__orden', 'orden', 'codigo', 'nombre')[:max(limit - len(node_items), 0)])
        simple_items = [_hierarchy_simple_value_payload(value) for value in simple_values]
    next_offset = offset + len(nodes)
    return JsonResponse({
        'items': node_items + simple_items,
        'count': total + simple_total,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_more': next_offset < total or simple_total > len(simple_items),
        'truncated': total + simple_total > len(node_items) + len(simple_items),
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
    return render(request, 'hierarchy_route_form.html', {
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
    return render(request, 'hierarchy_move_node.html', {
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
    return render(request, 'hierarchy_insert_between.html', {
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
