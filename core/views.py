import json
import re
import html
from io import BytesIO
from decimal import Decimal, InvalidOperation
import base64
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, TextField, Prefetch
from django.http import HttpResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .access import get_accessible_services, get_profile_for_user, get_service_equipment, get_service_permission
from .user_sync import archive_profile, sync_profile_from_auth_user
from .forms import (
    EmailLoginForm,
    EquipoBulkUploadForm,
    get_form_for_key,
)
from .registry import MODEL_REGISTRY, get_registered_model
from . import models
from technical_locations.equipment_import import import_equipment_excel

MAX_LIST_COLUMNS = 6
_RANGE_RE = re.compile(r'^\s*(-?\d+(?:[.,]\d+)?)\s*-\s*(-?\d+(?:[.,]\d+)?)\s*$')
_MATRIX_DECIMAL_STEP = Decimal('0.01')


def _export_value(value):
    if value is None:
        return ''
    if isinstance(value, Decimal):
        return format(value, 'f')
    if hasattr(value, 'strftime'):
        return value.strftime('%d/%m/%Y')
    return str(value)


def _export_filename(prefix, service, extension):
    code = re.sub(r'[^A-Za-z0-9_-]+', '_', str(service.codigo_servicio or service.pk)).strip('_')
    date_label = timezone.localdate().strftime('%Y%m%d')
    return f'{prefix}_{code}_{date_label}.{extension}'


def _export_xlsx_response(filename, sheet_name, columns, rows):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_name[:31] or 'Registros'
    headers = [label for _key, label in columns]
    worksheet.append(headers)

    for row in rows:
        worksheet.append([_export_value(row.get(key)) for key, _label in columns])

    header_fill = PatternFill('solid', fgColor='E8EEF7')
    for cell in worksheet[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(wrap_text=True, vertical='top')

    for row_cells in worksheet.iter_rows(min_row=2):
        for cell in row_cells:
            cell.alignment = Alignment(wrap_text=True, vertical='top')

    for index, _header in enumerate(headers, start=1):
        letter = get_column_letter(index)
        max_length = max(
            len(str(worksheet.cell(row=row_idx, column=index).value or ''))
            for row_idx in range(1, worksheet.max_row + 1)
        )
        worksheet.column_dimensions[letter].width = min(max(max_length + 2, 12), 42)

    worksheet.freeze_panes = 'A2'
    output = BytesIO()
    workbook.save(output)
    output.seek(0)

    response = HttpResponse(
        output.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _export_pdf_response(filename, title, columns, rows):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle('ExportBody', parent=styles['BodyText'], fontSize=8, leading=10)
    label_style = ParagraphStyle(
        'ExportLabel',
        parent=styles['BodyText'],
        fontSize=8,
        leading=10,
        fontName='Helvetica-Bold',
    )

    story = [
        Paragraph(html.escape(title), styles['Title']),
        Paragraph(f'Total registros: {len(rows)}', styles['Normal']),
        Spacer(1, 8),
    ]

    for row_index, row in enumerate(rows, start=1):
        if row_index > 1:
            story.append(Spacer(1, 8))
        story.append(Paragraph(f'Registro {row_index}', styles['Heading3']))
        table_data = []
        for key, label in columns:
            value = html.escape(_export_value(row.get(key))).replace('\n', '<br/>')
            table_data.append([
                Paragraph(html.escape(label), label_style),
                Paragraph(value or '-', body_style),
            ])
        table = Table(table_data, colWidths=[52 * mm, 220 * mm], repeatRows=0)
        table.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#D5DBE5')),
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#F3F6FA')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        story.append(table)
        if row_index < len(rows) and row_index % 2 == 0:
            story.append(PageBreak())

    if not rows:
        story.append(Paragraph('No hay registros para exportar.', styles['Normal']))

    document.build(story)
    output.seek(0)
    response = HttpResponse(output.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response




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
        raise Http404('Este módulo se administra desde un editor especí­fico.')


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
    'carga': 'core/forms/acacarga_form.html',
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
    nodes = []
    if selected_node_id:
        try:
            selected_node_id_int = int(selected_node_id)
        except (TypeError, ValueError):
            selected_node_id_int = None
        if selected_node_id_int:
            rows_by_id = _equipment_path_rows({selected_node_id_int})
            for node_id in _path_ids_for_node(selected_node_id_int, rows_by_id):
                row = rows_by_id.get(node_id)
                if not row:
                    continue
                path_ids = _path_ids_for_node(node_id, rows_by_id)
                nodes.append({
                    'id': row['id'],
                    'empresa_id': row['empresa_id'],
                    'parent_id': row['parent_id'],
                    'level_id': row['nivel_id'],
                    'level_order': row['nivel__orden'],
                    'code': row['codigo'],
                    'name': row['nombre'],
                    'label': f"{row['codigo']} - {row['nombre']}",
                    'ut': _node_ut_from_path(path_ids, rows_by_id),
                    'route': _node_route_from_path(path_ids, rows_by_id),
                })
    return {
        'tech_location_levels_json': json.dumps(levels, ensure_ascii=False),
        'tech_location_nodes_json': json.dumps(nodes, ensure_ascii=False),
        'tech_location_selected_id': str(selected_node_id or ''),
        'tech_location_empresa_id': str(selected_empresa_id or ''),
        'tech_location_nodes_url_template': reverse(
            'hierarchy_values_nodes',
            kwargs={'empresa_id': 0},
        ).replace('/0/', '/__empresa__/'),
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

    return {
        'levels': levels,
        'nodes': [],
        'equipment': [],
    }


def _service_equipment_count(service):
    if not service:
        return 0
    return get_service_equipment(service).order_by().count()


def _service_equipment_endpoints(service):
    if not service:
        return {'nodes': '', 'search': '', 'detail_template': ''}
    return {
        'nodes': reverse('service_equipment_nodes', kwargs={'pk': service.pk}),
        'search': reverse('service_equipment_search', kwargs={'pk': service.pk}),
        'detail_template': reverse(
            'service_equipment_detail',
            kwargs={'pk': service.pk, 'equipment_pk': 0},
        ).replace('/0/', '/__id__/'),
    }


def _equipment_path_rows(node_ids):
    rows_by_id = {}
    pending = {node_id for node_id in node_ids if node_id}
    while pending:
        rows = list(
            models.NodoJerarquia.objects.filter(
                pk__in=pending,
            ).values(
                'id',
                'empresa_id',
                'parent_id',
                'nivel_id',
                'nivel__orden',
                'nivel__nombre',
                'codigo',
                'nombre',
                'activo',
            )
        )
        pending = set()
        for row in rows:
            rows_by_id[row['id']] = row
            parent_id = row.get('parent_id')
            if parent_id and parent_id not in rows_by_id:
                pending.add(parent_id)
    return rows_by_id


def _equipment_items_payload(equipment_items):
    equipment_items = list(equipment_items)
    rows_by_id = _equipment_path_rows({equipo.nodo_id for equipo in equipment_items if equipo.nodo_id})
    payload = []
    for equipo in equipment_items:
        path_ids = _path_ids_for_node(equipo.nodo_id, rows_by_id) if equipo.nodo_id else []
        payload.append({
            'id': equipo.pk,
            'ut': equipo.ut or '',
            'descripcion_ut': equipo.descripcion_ut or '',
            'equipo': equipo.nombre_equipo or '',
            'tag': equipo.tag_display or '',
            'node_id': equipo.nodo_id,
            'path_node_ids': path_ids,
            'path_text': _node_route_from_path(path_ids, rows_by_id),
        })
    return payload


def _service_equipment_search_queryset(service, node=None, query=''):
    qs = get_service_equipment(service)
    if node:
        node_ut = node.ut
        qs = qs.filter(
            Q(nodo_id=node.pk)
            | Q(ut__iexact=node_ut)
            | Q(ut__istartswith=f'{node_ut}-')
        )

    query = (query or '').strip()
    if query:
        qs = qs.filter(
            Q(ut__icontains=query)
            | Q(descripcion_ut__icontains=query)
            | Q(nombre_equipo__icontains=query)
            | Q(tag_equipo__icontains=query)
        )
    return qs.distinct().order_by('ut', 'tag_equipo', 'nombre_equipo')


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
def model_list(request, model_key):
    config = _get_config(model_key)
    _ensure_direct_crud_allowed(config)
    model = config['model']
    page_size = 50 if model_key == 'equipo' else 100
    equipment_filter_context = {}

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
        if model_key == 'equipo':
            qs = qs.only(
                'id',
                'tag_equipo',
                'nombre_equipo',
                'ut',
                'descripcion_ut',
                'nodo_id',
            )
            equipment_company_ids = (
                models.Equipo.objects
                .filter(nodo__empresa_id__isnull=False)
                .values_list('nodo__empresa_id', flat=True)
                .distinct()
            )
            equipment_companies = models.Empresa.objects.filter(id__in=equipment_company_ids).order_by('nombre', 'sigla')
            selected_empresa_id = (request.GET.get('empresa') or '').strip()
            selected_node_id = (request.GET.get('node_id') or '').strip()
            selected_node = None
            if selected_node_id:
                selected_node = models.NodoJerarquia.objects.filter(pk=selected_node_id, activo=True).select_related('empresa').first()
                if selected_node:
                    selected_empresa_id = str(selected_node.empresa_id)
            selected_empresa = None
            if selected_empresa_id:
                selected_empresa = models.Empresa.objects.filter(pk=selected_empresa_id).first()
            if selected_empresa:
                qs = qs.filter(nodo__empresa=selected_empresa)
            if selected_node:
                subtree_ids = _active_subtree_ids(selected_node)
                node_ut = selected_node.ut
                qs = qs.filter(
                    Q(nodo_id__in=subtree_ids)
                    | Q(ut__iexact=node_ut)
                    | Q(ut__istartswith=f'{node_ut}-')
                )

            selected_levels = []
            if selected_empresa:
                selected_levels = list(
                    models.NivelJerarquia.objects
                    .filter(empresa=selected_empresa, activo=True)
                    .order_by('orden')
                    .values('id', 'nombre', 'orden')
                )
            selected_nodes = []
            if selected_node:
                rows_by_id = _equipment_path_rows({selected_node.pk})
                for node_id in _path_ids_for_node(selected_node.pk, rows_by_id):
                    row = rows_by_id.get(node_id)
                    if not row:
                        continue
                    path_ids = _path_ids_for_node(node_id, rows_by_id)
                    selected_nodes.append({
                        'id': row['id'],
                        'empresa_id': row['empresa_id'],
                        'parent_id': row['parent_id'],
                        'level_id': row['nivel_id'],
                        'level_order': row['nivel__orden'],
                        'code': row['codigo'],
                        'name': row['nombre'],
                        'label': f"{row['codigo']} - {row['nombre']}",
                        'ut': _node_ut_from_path(path_ids, rows_by_id),
                        'route': _node_route_from_path(path_ids, rows_by_id),
                    })
            equipment_filter_context = {
                'equipment_filter_enabled': True,
                'equipment_companies': equipment_companies,
                'equipment_selected_empresa_id': str(selected_empresa_id or ''),
                'equipment_selected_node_id': str(selected_node_id or ''),
                'equipment_levels': selected_levels,
                'equipment_levels_json': json.dumps([
                    {'id': item['id'], 'name': item['nombre'], 'order': item['orden']}
                    for item in selected_levels
                ], ensure_ascii=False),
                'equipment_nodes_json': json.dumps(selected_nodes, ensure_ascii=False),
                'equipment_nodes_url_template': reverse(
                    'hierarchy_values_nodes',
                    kwargs={'empresa_id': 0},
                ).replace('/0/', '/__empresa__/'),
            }

    search = request.GET.get('q', '').strip()
    if search and model_key == 'equipo':
        normalized_search = search.upper()
        if '-' in normalized_search:
            qs = qs.filter(
                Q(ut__iexact=normalized_search)
                | Q(ut__istartswith=f'{normalized_search}-')
                | Q(ut__icontains=normalized_search)
                | Q(tag_equipo__icontains=search)
                | Q(nombre_equipo__icontains=search)
            )
        else:
            qs = qs.filter(
                Q(tag_equipo__icontains=search)
                | Q(nombre_equipo__icontains=search)
                | Q(ut__icontains=search)
                | Q(descripcion_ut__icontains=search)
            )
    elif search and config['search_fields']:
        clause = Q()
        for field_name in config['search_fields']:
            clause |= Q(**{f'{field_name}__icontains': search})
        qs = qs.filter(clause)

    total_count = qs.count()
    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(request.GET.get('page'))
    rows = page_obj.object_list
    equipment_hierarchy_values = {}
    if model_key == 'equipo' and equipment_filter_context.get('equipment_levels'):
        rows_by_id = _equipment_path_rows({row.nodo_id for row in rows if row.nodo_id})
        for row in rows:
            path_ids = _path_ids_for_node(row.nodo_id, rows_by_id) if row.nodo_id else []
            parts = {}
            for node_id in path_ids:
                node_row = rows_by_id.get(node_id)
                if not node_row:
                    continue
                parts[node_row['nivel_id']] = f"{node_row['codigo']} - {node_row['nombre']}"
            equipment_hierarchy_values[row.pk] = parts

    list_fields = [
        field for field in _list_fields(model)
        if field.name != 'id'
        and not (model_key == 'empresa' and field.name == 'logo')
        and not (model_key == 'equipo' and field.name == 'nodo')
    ]

    list_query = request.GET.copy()
    list_query.pop('page', None)
    context = {
        'config': config,
        'model_key': model_key,
        'rows': rows,
        'search': search,
        'list_fields': list_fields,
        'editable_service_ids': editable_service_ids,
        'total_count': total_count,
        'page_obj': page_obj,
        'paginator': paginator,
        'page_size': page_size,
        'list_query_string': list_query.urlencode(),
        'equipment_hierarchy_values': equipment_hierarchy_values,
    }
    context.update(equipment_filter_context)
    return render(request, 'core/model_list.html', context)


@login_required
def equipment_bulk_upload(request):
    _ensure_admin_access(request)
    report = None
    if request.method == 'POST':
        form = EquipoBulkUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                with transaction.atomic():
                    report = import_equipment_excel(
                        form.cleaned_data['archivo'],
                        form.cleaned_data['empresa'],
                    )
                if report.created:
                    messages.success(request, f'Carga masiva completada: {report.created} equipos creados.')
                else:
                    messages.warning(request, 'No se crearon equipos. Revisa las observaciones de la carga.')
            except ValueError as exc:
                messages.error(request, str(exc))
    else:
        form = EquipoBulkUploadForm()

    return render(request, 'core/equipment_bulk_upload.html', {
        'form': form,
        'report': report,
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
    form_kwargs = {}
    if model_key == 'servicio':
        creador_usuario = get_profile_for_user(request.user)
        if not creador_usuario:
            creador_usuario = sync_profile_from_auth_user(request.user)
        form_kwargs['creador_usuario'] = creador_usuario

    if request.method == 'POST':
        form = FormClass(request.POST, request.FILES, **form_kwargs)
        if form.is_valid():
            if model_key == 'servicio' and form_kwargs.get('creador_usuario'):
                form.instance.creado_por_usuario = form_kwargs['creador_usuario']
            obj = form.save()
            return redirect('model_detail', model_key=model_key, pk=obj.pk)
    else:
        form = FormClass(**form_kwargs)

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
        return Decimal(str(value).strip().replace(',', '.'))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _quantize_matrix_decimal(value):
    parsed = _decimal_or_none(value)
    if parsed is None:
        return None
    return parsed.quantize(_MATRIX_DECIMAL_STEP)


def _format_matrix_decimal(value):
    parsed = _quantize_matrix_decimal(value)
    if parsed is None:
        return ''
    text = format(parsed, 'f').rstrip('0').rstrip('.')
    return text or '0'


def _parse_matrix_range(range_text):
    match = _RANGE_RE.match(str(range_text or '').strip())
    if not match:
        return None
    start = _quantize_matrix_decimal(match.group(1))
    end = _quantize_matrix_decimal(match.group(2))
    if start is None or end is None or end < start:
        return None
    return start, end


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


def _strategy_dimensions(estrategia, proceso=None):
    if not estrategia:
        return []
    qs = (
        models.EstrategiaDimension.objects.filter(estrategia=estrategia, activo=True)
        .select_related('dimension')
        .prefetch_related(
            'escalas_valor__escala_unificada',
            'catalogo__columnas',
            'catalogo__filas__celdas__columna',
        )
        .order_by('orden', 'id')
    )
    if proceso:
        qs = qs.filter(proceso_uso__in=[proceso, models.EstrategiaDimension.PROCESO_AMBOS])
    return list(qs)


def _dimension_display_value(item):
    if item is None:
        return ''

    if item.valor_booleano is not None:
        return 'Sí­' if item.valor_booleano else 'No'

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
                matched = _match_legend(result_num, _legend_from_matrix(matriz))
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


# ---------------------------------------------------------------------------
# Helpers compartidos de dimensiones y matrices
# ---------------------------------------------------------------------------
def _catalog_row_primary_numeric(row):
    if row is None:
        return None
    values = row.values_map()
    for key in ['valor_numerico', 'valor_principal', 'valor', 'nivel', 'puntaje']:
        if key in values and values.get(key) not in (None, ''):
            return _decimal_or_none(values.get(key))
    for key, value in values.items():
        if key in {'limite_inferior', 'limite_superior', 'desde', 'hasta', 'min', 'max', 'minimo', 'mí­nimo', 'maximo', 'máximo'}:
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
            return str(value).strip().lower() in {'true', '1', 'si', 'sí­', 'yes'}
    return None


def _calc_slug(value):
    return re.sub(r'[^a-z0-9]+', '_', (value or '').strip().lower()).strip('_')


def _matrix_legend_config(matriz):
    payload = _json_loads_safe(getattr(matriz, 'leyenda_json', ''), [])
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {'items': payload}
    return {}


def _matrix_resolution_mode(matriz):
    mode = (_matrix_legend_config(matriz).get('modo_resolucion') or '').strip()
    valid_modes = {choice[0] for choice in models.MatrizRiesgo.RESOLUCION_CHOICES}
    return mode if mode in valid_modes else models.MatrizRiesgo.RESOLUCION_EXACTA


def _matrix_result_from_axis_values(prob_val, impact_val):
    prob_val = _decimal_or_none(prob_val)
    impact_val = _decimal_or_none(impact_val)
    if prob_val is not None and impact_val is not None:
        return (prob_val * impact_val).quantize(_MATRIX_DECIMAL_STEP)
    if impact_val is not None:
        return impact_val.quantize(_MATRIX_DECIMAL_STEP)
    if prob_val is not None:
        return prob_val.quantize(_MATRIX_DECIMAL_STEP)
    return None


def _matrix_threshold_axis_values(matriz, prob_val, impact_val):
    prob_generated = _is_generated_matrix_axis_dimension(
        getattr(matriz, 'dimension_probabilidad', None),
        'probabilidad',
    )
    impact_generated = _is_generated_matrix_axis_dimension(
        getattr(matriz, 'dimension_impacto', None),
        'impacto',
    )
    if prob_generated and not impact_generated:
        prob_val = None
    if impact_generated and not prob_generated:
        impact_val = None
    return prob_val, impact_val


def _matrix_cell_for_result_floor(matriz, result_value):
    result_value = _quantize_matrix_decimal(result_value)
    if not matriz or result_value is None:
        return None
    return models.MatrizRiesgoCelda.objects.filter(
        matriz=matriz,
        resultado_num__lte=result_value,
    ).select_related(
        'probabilidad',
        'impacto_nivel',
    ).order_by('-resultado_num', '-id').first()


def _matrix_cell_for_axis_values(matriz, prob_val, impact_val):
    if not matriz:
        return None
    if prob_val is not None and impact_val is not None:
        exact_cell = models.MatrizRiesgoCelda.objects.filter(
            matriz=matriz,
            probabilidad__valor=prob_val,
            impacto_nivel__valor=impact_val,
        ).select_related(
            'probabilidad',
            'impacto_nivel',
        ).order_by('id').first()
        if exact_cell:
            return exact_cell

    if _matrix_resolution_mode(matriz) == models.MatrizRiesgo.RESOLUCION_UMBRAL_RESULTADO:
        prob_val, impact_val = _matrix_threshold_axis_values(matriz, prob_val, impact_val)
        return _matrix_cell_for_result_floor(
            matriz,
            _matrix_result_from_axis_values(prob_val, impact_val),
        )

    return None

# ---------------------------------------------------------------------------
# Constructor visual de matrices
# ---------------------------------------------------------------------------
def _matrix_level_dicts(levels, count, prefix):
    data = []
    levels = list(levels)
    for idx in range(1, count + 1):
        obj = levels[idx - 1] if idx <= len(levels) else None
        value = getattr(obj, 'valor', None) if obj else None
        data.append({
            'idx': idx,
            'nombre': getattr(obj, 'nombre', None) or f'{prefix.upper()}{idx}',
            'valor': _json_safe(value) if value is not None else idx,
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


def _normalize_matrix_level_defs(defs, count, prefix):
    defs = defs if isinstance(defs, list) else []
    normalized = []
    for idx in range(1, count + 1):
        raw = defs[idx - 1] if idx <= len(defs) and isinstance(defs[idx - 1], dict) else {}
        value = _quantize_matrix_decimal(raw.get('valor'))
        normalized.append({
            'idx': idx,
            'nombre': str(raw.get('nombre') or f'{prefix.upper()}{idx}'),
            'valor': value if value is not None else Decimal(idx),
            'descripcion': str(raw.get('descripcion') or ''),
        })
    return normalized


def _definitions_from_request(request, prob_count, impact_count, fallback_prob, fallback_impact):
    prob_defs = _json_payload(request, 'prob_levels_json', fallback_prob) or fallback_prob
    impact_defs = _json_payload(request, 'impact_levels_json', fallback_impact) or fallback_impact
    prob_defs = _normalize_matrix_level_defs(prob_defs if isinstance(prob_defs, list) else fallback_prob, prob_count, 'p')
    impact_defs = _normalize_matrix_level_defs(impact_defs if isinstance(impact_defs, list) else fallback_impact, impact_count, 'i')

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
                'resultado_num': _quantize_matrix_decimal(cell.get('resultado_num')),
                'calcular': bool(cell.get('calcular', True)),
            }
    return result


def _matrix_result_values(prob_defs, impact_defs):
    values = []
    for prob in prob_defs:
        for impact in impact_defs:
            prob_value = _decimal_or_none(prob.get('valor'))
            impact_value = _decimal_or_none(impact.get('valor'))
            if prob_value is None or impact_value is None:
                continue
            values.append((prob_value * impact_value).quantize(_MATRIX_DECIMAL_STEP))
    return values


def _matrix_value_bounds(prob_defs, impact_defs):
    values = _matrix_result_values(prob_defs, impact_defs)
    return (min(values), max(values)) if values else (1, 1)


def _safe_legend_items(raw_items):
    if isinstance(raw_items, dict):
        raw_items = raw_items.get('items') or raw_items.get('legend') or raw_items.get('leyenda') or []
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


def _matrix_legend_payload(legend_items, modo_resolucion=None):
    return {
        'items': legend_items or [],
        'modo_resolucion': modo_resolucion or models.MatrizRiesgo.RESOLUCION_EXACTA,
    }


def _default_legend_for_bounds(min_value, max_value):
    min_value = _quantize_matrix_decimal(min_value) or Decimal('0.00')
    max_value = _quantize_matrix_decimal(max_value) or min_value
    if max_value < min_value:
        max_value = min_value
    if max_value == min_value or max_value - min_value < Decimal('0.04'):
        return [{'name': 'Bajo', 'range': f'{_format_matrix_decimal(min_value)}-{_format_matrix_decimal(max_value)}', 'color': '#2ecc71'}]
    span = max_value - min_value
    step = (span / Decimal('4')).quantize(_MATRIX_DECIMAL_STEP) if span else Decimal('1.00')
    if step <= 0:
        step = Decimal('1.00')
    labels = [('Bajo', '#2ecc71'), ('Medio', '#f1c40f'), ('Alto', '#e67e22'), ('Crí­tico', '#e74c3c')]
    items = []
    current = min_value
    for idx, (label, color) in enumerate(labels):
        end = min_value + (step * (idx + 1)) if idx < 3 else max_value
        end = _quantize_matrix_decimal(end) or max_value
        end = min(max_value, max(current, end))
        items.append({
            'name': label,
            'range': f'{_format_matrix_decimal(current)}-{_format_matrix_decimal(end)}',
            'color': color,
        })
        current = end + _MATRIX_DECIMAL_STEP
    items[-1]['range'] = f"{items[-1]['range'].split('-')[0]}-{_format_matrix_decimal(max_value)}"
    return items


def _validate_legend_items(raw_items, min_value, max_value, result_values=None):
    items = _safe_legend_items(raw_items)
    if not items:
        return None, 'Debes definir al menos un rango en la leyenda.'

    parsed = []
    for item in items:
        if not item['name']:
            return None, 'Cada rango de la leyenda debe tener un nombre.'
        range_values = _parse_matrix_range(item['range'])
        if not range_values:
            return None, f"El rango '{item['range']}' no tiene un formato valido. Usa por ejemplo 0,1-4,5."
        start, end = range_values
        if end < start:
            return None, f"El rango '{item['range']}' no es válido porque el final es menor que el inicio."
        parsed.append({'name': item['name'], 'range': f'{_format_matrix_decimal(start)}-{_format_matrix_decimal(end)}', 'color': item['color'], 'start': start, 'end': end})

    parsed.sort(key=lambda item: item['start'])
    min_value = _quantize_matrix_decimal(min_value) or Decimal('0.00')
    max_value = _quantize_matrix_decimal(max_value) or min_value
    values_to_cover = result_values if result_values is not None else [min_value, max_value]
    uncovered = []
    for value in values_to_cover:
        value = _quantize_matrix_decimal(value)
        if value is None:
            continue
        if not any(item['start'] <= value <= item['end'] for item in parsed):
            uncovered.append(value)
    if uncovered:
        samples = ', '.join(_format_matrix_decimal(value) for value in sorted(set(uncovered))[:5])
        return None, f'La leyenda no cubre los valores calculados: {samples}. Ajusta los rangos para incluirlos.'
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
    result_value = _quantize_matrix_decimal(result_value)
    if result_value is None:
        return '', '#2a2a3a'
    for item in legend_items:
        range_values = _parse_matrix_range(item['range'])
        if not range_values:
            continue
        start, end = range_values
        if start <= result_value <= end:
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
                prob_value = _decimal_or_none(prob_defs[prob_idx - 1].get('valor'))
                impact_value = _decimal_or_none(impact_defs[impact_idx - 1].get('valor'))
                result_num = ((prob_value or Decimal('0')) * (impact_value or Decimal('0'))).quantize(_MATRIX_DECIMAL_STEP)
                clasificacion, color = _match_legend(result_num, legend_items)
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
def _sync_matrix_levels(matriz, model_cls, definitions, delete_stale=True):
    existing = list(model_cls.objects.filter(matriz=matriz).order_by('orden_visual', 'id'))
    keep_ids = []
    result = []
    for idx, definition in enumerate(definitions, start=1):
        obj = existing[idx - 1] if idx <= len(existing) else model_cls(matriz=matriz)
        obj.nombre = str(definition.get('nombre') or f'N{idx}')
        value = _quantize_matrix_decimal(definition.get('valor'))
        obj.valor = value if value is not None else Decimal(idx)
        obj.descripcion = str(definition.get('descripcion') or '')
        obj.orden_visual = idx
        obj.save()
        keep_ids.append(obj.pk)
        result.append(obj)
    if delete_stale:
        model_cls.objects.filter(matriz=matriz).exclude(pk__in=keep_ids).delete()
    return result


def _next_strategy_order(estrategia):
    current = models.EstrategiaDimension.objects.filter(estrategia=estrategia, activo=True).order_by('-orden').first()
    return (current.orden if current else 0) + 1


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
    estrategia_dimension.proceso_uso = models.EstrategiaDimension.PROCESO_ACA
    estrategia_dimension.activo = True
    estrategia_dimension.save(update_fields=['obligatorio', 'proceso_uso', 'activo'])
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
        proceso_uso=models.EstrategiaDimension.PROCESO_ACA,
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
    prob_levels = _sync_matrix_levels(matriz, models.NivelProbabilidad, prob_defs, delete_stale=False)
    impact_levels = _sync_matrix_levels(matriz, models.NivelImpacto, impact_defs, delete_stale=False)
    keep_prob_level_ids = [level.pk for level in prob_levels]
    keep_impact_level_ids = [level.pk for level in impact_levels]

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
                prob_value = _decimal_or_none(prob.valor)
                impact_value = _decimal_or_none(impact.valor)
                result_num = ((prob_value or Decimal('0')) * (impact_value or Decimal('0'))).quantize(_MATRIX_DECIMAL_STEP)
            else:
                result_num = _quantize_matrix_decimal(result_num) or Decimal('0.00')
            cell.resultado_num = result_num
            cell.clasificacion = str(payload.get('clasificacion') or '')
            cell.color = str(payload.get('color') or '#2a2a3a')
            cell.calcular = bool(payload.get('calcular', True))
            cell.save()
            keep_ids.append(cell.pk)

    models.MatrizRiesgoCelda.objects.filter(matriz=matriz).exclude(pk__in=keep_ids).delete()
    models.NivelProbabilidad.objects.filter(matriz=matriz).exclude(pk__in=keep_prob_level_ids).delete()
    models.NivelImpacto.objects.filter(matriz=matriz).exclude(pk__in=keep_impact_level_ids).delete()
