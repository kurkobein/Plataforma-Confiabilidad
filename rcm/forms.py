import json
import unicodedata
from decimal import Decimal, InvalidOperation

from django import forms
from django.db.models import Q
from django.forms import formset_factory
from django.utils import timezone

from core import models as app_models
from core.access import get_service_equipment
from rcm.field_options import normalize_rcm_field_option


class RCMExcelBulkUploadForm(forms.Form):


    replace = forms.BooleanField(
        label='Reemplazar carga previa del mismo archivo',
        required=False,
        help_text='Elimina registros RCM/FMECA previos con el mismo origen para este servicio.',
    )
    create_task_types = forms.BooleanField(
        label='Crear tipos de tarea faltantes',
        required=False,
        help_text='Crea Actual/Primaria/Secundaria si la estrategia no los tiene configurados.',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                continue
            css = widget.attrs.get('class', '')
            widget.attrs['class'] = f'{css} input-control'.strip()


def _decimal_or_none(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value).strip().replace(',', '.'))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _catalog_primary_numeric_from_values(values):
    for key in ['valor_numerico', 'valor_principal', 'valor', 'nivel', 'puntaje']:
        value = values.get(key) if isinstance(values, dict) else None
        if value not in (None, ''):
            return _decimal_or_none(value)
    if isinstance(values, dict):
        for key, value in values.items():
            if key in {'limite_inferior', 'limite_superior', 'desde', 'hasta', 'min', 'max', 'minimo', 'm\u00ednimo', 'maximo', 'm\u00e1ximo'}:
                continue
            decimal_value = _decimal_or_none(value)
            if decimal_value is not None:
                return decimal_value
    return None

def _rcm_dimension_queryset(service):
    if not service or not service.estrategia_id:
        return app_models.EstrategiaDimension.objects.none()
    process_values = list(dict.fromkeys([
        *getattr(app_models.EstrategiaDimension, 'PROCESO_FMECA_ALIASES', ('fmeca', 'rcm')),
        app_models.EstrategiaDimension.PROCESO_AMBOS,
    ]))
    consequence_terms = [
        'impacto',
        'consecuencia',
        'operacion',
        'operación',
        'medio ambiente',
        'ambiental',
        'seguridad',
        'reputacion',
        'reputación',
    ]
    query = Q(dimension__tipo_funcional='impacto')
    for term in consequence_terms:
        query |= Q(dimension__nombre__icontains=term)
    return (
        app_models.EstrategiaDimension.objects.filter(
            estrategia=service.estrategia,
            activo=True,
            proceso_uso__in=process_values,
        )
        .select_related('dimension')
        .prefetch_related(
            'escalas_valor__escala_unificada',
            'catalogo__columnas',
            'catalogo__filas__celdas__columna',
        )
        .order_by('orden', 'id')
        .distinct()
    )


_rcm_impact_dimension_queryset = _rcm_dimension_queryset


def _rcm_catalog_row_label(row):
    values = row.values_map()
    extras = []
    for col in row.catalogo.columnas.all().order_by('orden')[:4]:
        value = values.get(col.clave_interna)
        if value not in (None, ''):
            extras.append(f'{col.nombre_columna}: {value}')
    return ' || '.join(extras) or row.etiqueta or f'Fila {row.orden}'


def _rcm_catalog_option_rows(catalog_rows):
    rows = []
    first_row = catalog_rows[0] if catalog_rows else None
    columns = list(first_row.catalogo.columnas.all().order_by('orden')) if first_row else []
    headers = [col.nombre_columna for col in columns] or ['Opción']
    for row in catalog_rows:
        values = row.values_map()
        cells = []
        if columns:
            for col in columns:
                cells.append(values.get(col.clave_interna, row.etiqueta if col.clave_interna == 'etiqueta' else ''))
        else:
            cells.append(row.etiqueta)
        numeric = _catalog_primary_numeric_from_values(values)
        rows.append({
            'pk': row.pk,
            'cells': cells,
            'value_numeric': str(numeric or ''),
        })
    return headers, rows


def _rcm_catalog_row_text_value(row):
    if row is None:
        return ''
    values = row.values_map()
    for key in ['valor_texto', 'valor', 'valor_numerico', 'indicador', 'codigo', 'etiqueta', 'nombre', 'descripcion']:
        value = values.get(key)
        if value not in (None, ''):
            return str(value)
    return str(row.etiqueta or '')


def _rcm_int_from_decimal(value):
    value = _decimal_or_none(value)
    if value is None:
        return None
    return int(value)


def _rcm_normalized_text(value):
    return unicodedata.normalize('NFD', str(value or '').lower()).encode('ascii', 'ignore').decode('ascii')


def _rcm_dimension_match_keys(estrategia_dimension):
    dimension = getattr(estrategia_dimension, 'dimension', None)
    text = _rcm_normalized_text(
        f'{getattr(dimension, "nombre", "")} {getattr(dimension, "tipo_funcional", "")}'
    )
    keys = []
    if getattr(estrategia_dimension, 'dimension_id', None):
        keys.append(f'dimension:{estrategia_dimension.dimension_id}')
    categories = {
        'operacion': ['operacion', 'operativo'],
        'seguridad': ['seguridad'],
        'ambiente': ['medio ambiente', 'ambiental', 'ambiente'],
        'reputacion': ['reputacion', 'entorno'],
    }
    for key, terms in categories.items():
        if any(term in text for term in terms):
            keys.append(f'categoria:{key}')
    return keys


def _rcm_slug(value):
    return ''.join(
        char if char.isalnum() else '_'
        for char in _rcm_normalized_text(value)
    ).strip('_')


def _rcm_source_key(estrategia_dimension):
    try:
        catalogo = getattr(estrategia_dimension, 'catalogo', None)
        if catalogo and catalogo.campo:
            return catalogo.campo
    except Exception:
        pass
    return _rcm_slug(estrategia_dimension.dimension.nombre)


def _rcm_source_candidates(source_ref):
    if source_ref in (None, ''):
        return []
    if isinstance(source_ref, dict):
        candidates = []
        ed_id = source_ref.get('estrategia_dimension_id') or source_ref.get('ed_id')
        if ed_id not in (None, ''):
            candidates.extend([f'ed:{ed_id}', f'estrategia_dimension:{ed_id}'])
        dim_id = source_ref.get('dimension_id')
        if dim_id not in (None, ''):
            candidates.extend([str(dim_id), f'dim:{dim_id}'])
        for key in ['campo', 'nombre', 'source', 'fuente', 'dependencia', 'depende_de']:
            value = source_ref.get(key)
            if value not in (None, '') and not isinstance(value, dict):
                candidates.append(str(value).strip())
        return [candidate for candidate in candidates if candidate]
    return [str(source_ref).strip()]


def _rcm_remember_source_value(source_values, estrategia_dimension, value):
    decimal_value = _decimal_or_none(value)
    if decimal_value is None:
        return
    dimension = estrategia_dimension.dimension
    campo = _rcm_source_key(estrategia_dimension)
    keys = [
        f'ed:{estrategia_dimension.id}',
        f'estrategia_dimension:{estrategia_dimension.id}',
        str(dimension.id),
        f'dim:{dimension.id}',
        dimension.nombre,
        _rcm_slug(dimension.nombre),
        campo,
        _rcm_slug(campo),
    ]
    for key in keys:
        if key:
            source_values[key] = decimal_value


def _rcm_source_value(source_values, source_ref):
    for candidate in _rcm_source_candidates(source_ref):
        value = source_values.get(candidate)
        if value is None:
            value = source_values.get(_rcm_slug(candidate))
        decimal_value = _decimal_or_none(value)
        if decimal_value is not None:
            return decimal_value
    return None


def _rcm_calculation_steps(tipo_calculo, config_calculo):
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
            operation = str(raw_step.get('operacion') or raw_step.get('tipo_calculo') or raw_step.get('operation') or '').strip().lower()
            operands = raw_step.get('operandos') or raw_step.get('campos') or raw_step.get('sources') or []
            if operation and isinstance(operands, list):
                steps.append({
                    'operacion': operation,
                    'operandos': operands,
                    'modo': raw_step.get('modo') or ('ponderado' if raw_step.get('ponderado') is True else ''),
                })
        return steps
    operands = config_calculo.get('operandos') or config_calculo.get('campos') or config_calculo.get('sources') or []
    return [{'operacion': tipo, 'operandos': operands if isinstance(operands, list) else []}]


def _rcm_operation_result(operation, values):
    if not values:
        return None
    if operation == 'suma':
        return sum(values, Decimal('0'))
    if operation == 'resta':
        result = values[0]
        for value in values[1:]:
            result -= value
        return result
    if operation == 'multiplicacion':
        result = Decimal('1')
        for value in values:
            result *= value
        return result
    if operation == 'division':
        result = values[0]
        for value in values[1:]:
            if value == 0:
                return None
            result /= value
        return result
    if operation == 'maximo':
        return max(values)
    if operation == 'minimo':
        return min(values)
    return None


def _rcm_operand_value(operand, source_values, previous_result=None):
    if isinstance(operand, dict) and (operand.get('resultado') is True or operand.get('tipo') == 'resultado'):
        return previous_result
    if not isinstance(operand, dict) and str(operand) in {'$resultado', '__resultado__', 'resultado_anterior'}:
        return previous_result
    return _rcm_source_value(source_values, operand)


def _rcm_operand_weight(operand):
    if not isinstance(operand, dict):
        return None
    return _decimal_or_none(operand.get('peso', operand.get('ponderador', operand.get('weight'))))


def _rcm_weighted_values(operands, resolved_values):
    if not operands or len(operands) != len(resolved_values):
        return None
    weights = [_rcm_operand_weight(operand) for operand in operands]
    if all(weight is None for weight in weights):
        equal_weight = Decimal('1') / Decimal(len(weights))
        weights = [equal_weight] * len(weights)
    elif any(weight is None for weight in weights):
        assigned = sum((weight for weight in weights if weight is not None), Decimal('0'))
        missing_count = sum(1 for weight in weights if weight is None)
        remaining = Decimal('1') - assigned
        if remaining < 0:
            return None
        missing_weight = remaining / Decimal(missing_count)
        weights = [missing_weight if weight is None else weight for weight in weights]
    if any(weight < 0 or weight > 1 for weight in weights):
        return None
    if abs(sum(weights, Decimal('0')) - Decimal('1')) > Decimal('0.0001'):
        return None
    return [value * weight for value, weight in zip(resolved_values, weights)]


def _rcm_evaluate_calculation(tipo_calculo, config_calculo, source_values):
    result = None
    for step in _rcm_calculation_steps(tipo_calculo, config_calculo):
        values = []
        for operand in step['operandos']:
            value = _rcm_operand_value(operand, source_values, result)
            if value is None:
                return None
            values.append(value)
        if step.get('modo') == 'ponderado' and step['operacion'] == 'suma':
            values = _rcm_weighted_values(step['operandos'], values)
            if values is None:
                return None
        result = _rcm_operation_result(step['operacion'], values)
        if result is None:
            return None
    return result



def _rcm_config_dict(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or '{}')
        except Exception:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def _rcm_catalog_rows_payload(catalog_rows):
    payload = []
    for row in catalog_rows:
        values = row.values_map()
        lower = _rcm_catalog_bound(values, ['limite_inferior', 'desde', 'min', 'minimo', 'mínimo'])
        upper = _rcm_catalog_bound(values, ['limite_superior', 'hasta', 'max', 'maximo', 'máximo'])
        primary = _catalog_primary_numeric_from_values(values)
        payload.append({
            'pk': row.pk,
            'lower': str(lower) if lower is not None else '',
            'upper': str(upper) if upper is not None else '',
            'primary': str(primary) if primary is not None else '',
            'text': _rcm_catalog_row_text_value(row),
        })
    return payload

def _rcm_dependency_ref(config):
    if isinstance(config, str):
        try:
            config = json.loads(config or '{}')
        except Exception:
            config = {}
    if not isinstance(config, dict):
        return ''
    for key in ['dependencia', 'depende_de', 'source', 'fuente', 'campo_fuente', 'dimension_fuente']:
        value = config.get(key)
        if value not in (None, ''):
            return value if isinstance(value, dict) else str(value).strip()
    if config.get('estrategia_dimension_id') or config.get('dimension_id'):
        return config
    return ''


def _rcm_catalog_bound(values, keys):
    for key in keys:
        value = values.get(key) if isinstance(values, dict) else None
        if value not in (None, ''):
            return _decimal_or_none(value)
    return None


def _rcm_match_range_row(catalogo, source_value):
    source_value = _decimal_or_none(source_value)
    if not catalogo or source_value is None:
        return None
    rows = list(catalogo.filas.prefetch_related('celdas__columna').order_by('orden', 'id'))
    lowers = []
    row_bounds = []
    for row in rows:
        values = row.values_map()
        lower = _rcm_catalog_bound(values, ['limite_inferior', 'desde', 'min', 'minimo', 'mínimo'])
        upper = _rcm_catalog_bound(values, ['limite_superior', 'hasta', 'max', 'maximo', 'máximo'])
        row_bounds.append((row, lower, upper))
        if lower is not None:
            lowers.append(lower)

    for row, lower, upper in row_bounds:
        if lower is not None and source_value < lower:
            continue
        if upper is not None and source_value > upper:
            continue
        if upper is not None and source_value == upper and upper in lowers:
            continue
        return row
    return None


def _rcm_match_option_row(catalogo, source_value):
    source_value = _decimal_or_none(source_value)
    if not catalogo or source_value is None:
        return None
    for row in catalogo.filas.prefetch_related('celdas__columna').order_by('orden', 'id'):
        row_value = _catalog_primary_numeric_from_values(row.values_map())
        if row_value is not None and row_value == source_value:
            return row
    return None


def _rcm_match_dependency_row(catalogo, source_value):
    if not catalogo:
        return None
    if catalogo.tipo == 'rangos':
        return _rcm_match_range_row(catalogo, source_value)
    if catalogo.tipo == 'opciones':
        return _rcm_match_option_row(catalogo, source_value)
    return None


class RCMRegistroForm(forms.Form):
    equipo = forms.ModelChoiceField(
        queryset=app_models.Equipo.objects.none(),
        widget=forms.HiddenInput(),
        required=False,
        label='Equipo',
    )
    familia_equipo = forms.ModelChoiceField(
        queryset=app_models.FamiliaEquipo.objects.none(),
        required=False,
        label='Familia de equipos',
        help_text='Opcional. Si seleccionas una familia, se creara un registro RCM/FMEA para cada equipo activo de esa familia.',
    )
    fecha_analisis = forms.DateField(
        label='Fecha de análisis',
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    estado = forms.ChoiceField(
        choices=app_models.Carga.STATUS_CHOICES,
        initial=app_models.Carga.STATUS_COMPLETO,
    )
    criticidad = forms.IntegerField(
        required=False,
        min_value=0,
        label='Criticidad',
        
    )
    componente = forms.CharField(required=False, max_length=255, label='Componente')
    funcion = forms.CharField(label='Función', required=False, widget=forms.Textarea(attrs={'rows': 3}))
    falla_funcional = forms.ChoiceField(label='Falla funcional', choices=())
    modo_de_falla = forms.ChoiceField(label='Modo de falla', choices=())
    efecto = forms.ChoiceField(label='Efecto', choices=())
    observacion = forms.CharField(
        required=False,
        label='Observación',
        widget=forms.Textarea(attrs={'rows': 3}),
    )

    def __init__(self, *args, service=None, rcm=None, allow_incomplete=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.service = service
        self.allow_incomplete = allow_incomplete
        self.impact_dimensions = []
        self.impact_value_map = {}
        self.evaluation_dimensions = self.impact_dimensions
        self.dimension_value_map = self.impact_value_map
        self.dimension_runtime_payload = []
        impact_initial_values = {}
        impact_initial_records = {}
        impact_initial_by_key = {}
        if rcm:
            try:
                fmea = rcm.fmea_fmeca
            except app_models.FMEA_FMECA.DoesNotExist:
                fmea = None
            if fmea:
                evaluations = fmea.evaluaciones.select_related('estrategia_dimension__dimension')
                impact_initial_records = {
                    evaluation.estrategia_dimension_id: evaluation
                    for evaluation in evaluations
                }
                impact_initial_values = {
                    evaluation.estrategia_dimension_id: evaluation.valor_numerico
                    for evaluation in impact_initial_records.values()
                    if evaluation.valor_numerico is not None
                }
                for evaluation in impact_initial_records.values():
                    for key in _rcm_dimension_match_keys(evaluation.estrategia_dimension):
                        if evaluation.valor_numerico is not None:
                            impact_initial_by_key.setdefault(key, evaluation.valor_numerico)

        if service:
            self.fields['equipo'].queryset = get_service_equipment(service)
            self.fields['familia_equipo'].queryset = app_models.FamiliaEquipo.objects.filter(
                servicio=service,
                activa=True,
            ).order_by('nombre')
            for field_name in ('falla_funcional', 'modo_de_falla', 'efecto'):
                option_values = list(
                    app_models.RCMCampoOpcion.objects.filter(
                        servicio=service,
                        campo=field_name,
                        activo=True,
                    )
                    .order_by('valor')
                    .values_list('valor', flat=True)
                )
                preserved_values = [
                    self.data.get(self.add_prefix(field_name)) if self.is_bound else '',
                    self.initial.get(field_name),
                ]
                known_keys = {
                    normalize_rcm_field_option(value)
                    for value in option_values
                    if normalize_rcm_field_option(value)
                }
                for value in preserved_values:
                    normalized = normalize_rcm_field_option(value)
                    if normalized and normalized not in known_keys:
                        option_values.append(str(value))
                        known_keys.add(normalized)
                self.fields[field_name].choices = [
                    ('', f'Selecciona {self.fields[field_name].label.lower()}'),
                    *((value, value) for value in option_values),
                ]
        self.fields['familia_equipo'].empty_label = 'Sin familia'
        if self.allow_incomplete:
            for field_name in ('falla_funcional', 'modo_de_falla', 'efecto'):
                self.fields[field_name].required = False

        for estrategia_dimension in _rcm_impact_dimension_queryset(service):
            dimension = estrategia_dimension.dimension
            field_name = f'impact_{estrategia_dimension.pk}'
            scales = list(estrategia_dimension.escalas_valor.all().order_by('nivel_ordinal', 'id'))
            catalog_rows = []
            catalogo = getattr(estrategia_dimension, 'catalogo', None)
            if catalogo:
                catalog_rows = list(catalogo.filas.all().prefetch_related('celdas__columna').order_by('orden', 'id'))

            mode = 'manual'
            is_calculated = bool((dimension.tipo_calculo or '').strip())
            option_headers = []
            option_rows = []
            value_map = {}
            if is_calculated:
                mode = 'calculado'
                field = forms.IntegerField(required=False, label=dimension.nombre, disabled=True)
            elif scales:
                mode = 'escala'
                field = forms.ModelChoiceField(
                    queryset=app_models.EscalaValor.objects.filter(pk__in=[item.pk for item in scales]).select_related('escala_unificada').order_by('nivel_ordinal', 'id'),
                    required=False,
                    label=dimension.nombre,
                    empty_label='Selecciona un valor',
                )
                field.label_from_instance = lambda obj: f"{obj.codigo or obj.descripcion} ({obj.valor_numerico})"
                option_headers = ['Código', 'Descripción', 'Valor', 'Escala unificada']
                for scale in scales:
                    value_map[str(scale.pk)] = _rcm_int_from_decimal(scale.valor_numerico)
                    option_rows.append({
                        'pk': scale.pk,
                        'cells': [
                            scale.codigo or '',
                            scale.descripcion or '',
                            scale.valor_numerico,
                            str(scale.escala_unificada) if scale.escala_unificada_id else '',
                        ],
                        'value_numeric': str(scale.valor_numerico or ''),
                    })
            elif catalog_rows:
                mode = 'catalogo'
                field = forms.ModelChoiceField(
                    queryset=app_models.DimensionCatalogoFila.objects.filter(pk__in=[item.pk for item in catalog_rows]).prefetch_related('celdas__columna', 'catalogo__columnas').order_by('orden', 'id'),
                    required=False,
                    label=dimension.nombre,
                    empty_label='Selecciona un valor',
                )
                field.label_from_instance = _rcm_catalog_row_label
                option_headers, option_rows = _rcm_catalog_option_rows(catalog_rows)
                for row in catalog_rows:
                    value_map[str(row.pk)] = _rcm_int_from_decimal(_catalog_primary_numeric_from_values(row.values_map()))
            else:
                field = forms.IntegerField(required=False, min_value=1, label=dimension.nombre)
                value_map['__manual__'] = True

            self.fields[field_name] = field
            initial_record = impact_initial_records.get(estrategia_dimension.pk)
            if initial_record:
                if mode == 'catalogo' and initial_record.catalogo_fila_id:
                    self.initial[field_name] = initial_record.catalogo_fila_id
                    initial_value = None
                elif mode == 'escala' and initial_record.escala_valor_id:
                    self.initial[field_name] = initial_record.escala_valor_id
                    initial_value = None
                else:
                    initial_value = initial_record.valor_numerico
            else:
                initial_value = impact_initial_values.get(estrategia_dimension.pk)
            if initial_value in (None, ''):
                for key in _rcm_dimension_match_keys(estrategia_dimension):
                    if key in impact_initial_by_key:
                        initial_value = impact_initial_by_key[key]
                        break
            if initial_value not in (None, ''):
                if mode in {'manual', 'calculado'}:
                    self.initial[field_name] = initial_value
                else:
                    for option_value, numeric_value in value_map.items():
                        if option_value == '__manual__':
                            continue
                        if numeric_value == initial_value:
                            self.initial[field_name] = option_value
                            break

            config_dict = _rcm_config_dict(dimension.config_calculo)
            dependency_ref = _rcm_dependency_ref(config_dict)
            self.dimension_runtime_payload.append({
                'field_name': field_name,
                'field_id': self[field_name].id_for_label,
                'mode': mode,
                'estrategia_dimension_id': estrategia_dimension.pk,
                'dimension_id': dimension.pk,
                'nombre': dimension.nombre,
                'campo': _rcm_source_key(estrategia_dimension),
                'tipo_calculo': dimension.tipo_calculo or '',
                'config_calculo': config_dict,
                'dependency': dependency_ref,
                'value_map': {str(key): (str(value) if value is not None else '') for key, value in value_map.items()},
                'catalog_tipo': getattr(catalogo, 'tipo', '') if catalogo else '',
                'catalog_rows': _rcm_catalog_rows_payload(catalog_rows),
            })

            self.impact_dimensions.append({
                'estrategia_dimension': estrategia_dimension,
                'dimension': dimension,
                'field_name': field_name,
                'field': self[field_name],
                'mode': mode,
                'is_calculated': is_calculated,
                'option_headers': option_headers,
                'option_rows': option_rows,
            })
            self.impact_value_map[field_name] = value_map

        for _, field in self.fields.items():
            widget = field.widget
            css = 'input-textarea' if isinstance(widget, forms.Textarea) else 'input-control'
            if isinstance(widget, forms.HiddenInput):
                css = ''
            existing = widget.attrs.get('class', '')
            widget.attrs['class'] = f'{existing} {css}'.strip()
            if not isinstance(widget, forms.HiddenInput):
                widget.attrs.setdefault('placeholder', field.label)

    def _clean_dynamic_rcm_evaluations(self):
        cleaned = super().clean()
        if self.service and not self.service.estrategia_id:
            raise forms.ValidationError('El servicio debe tener una estrategia asociada para registrar RCM.')

        self.cleaned_dimension_evaluations = []
        self.cleaned_impact_evaluations = self.cleaned_dimension_evaluations
        source_values = {}
        pending_items = []
        resolved_dimension_ids = set()

        def remember_evaluation(item, value, text_value='', catalogo_fila=None, escala_valor=None):
            estrategia_dimension = item['estrategia_dimension']
            resolved_dimension_ids.add(estrategia_dimension.pk)
            cleaned[item['field_name']] = value
            self.cleaned_dimension_evaluations.append({
                'estrategia_dimension': estrategia_dimension,
                'valor_numerico': value,
                'valor_texto': text_value or ('' if value is None else str(value)),
                'catalogo_fila': catalogo_fila,
                'escala_valor': escala_valor,
            })
            if value is not None:
                _rcm_remember_source_value(source_values, estrategia_dimension, value)

        def selected_value_payload(item, selected):
            if item['mode'] == 'escala':
                return {
                    'value': _rcm_int_from_decimal(selected.valor_numerico),
                    'text': selected.codigo or selected.descripcion or str(selected.valor_numerico or ''),
                    'catalogo_fila': None,
                    'escala_valor': selected,
                }
            if item['mode'] == 'catalogo':
                return {
                    'value': _rcm_int_from_decimal(_catalog_primary_numeric_from_values(selected.values_map())),
                    'text': _rcm_catalog_row_text_value(selected),
                    'catalogo_fila': selected,
                    'escala_valor': None,
                }
            value = _rcm_int_from_decimal(selected)
            return {
                'value': value,
                'text': '' if value is None else str(value),
                'catalogo_fila': None,
                'escala_valor': None,
            }

        for item in self.impact_dimensions:
            field_name = item['field_name']
            if item.get('is_calculated'):
                pending_items.append({'item': item, 'kind': 'calculated'})
                continue

            selected = cleaned.get(field_name)
            if selected in (None, ''):
                dependency = _rcm_dependency_ref(item['dimension'].config_calculo)
                if dependency:
                    pending_items.append({'item': item, 'kind': 'dependent', 'dependency': dependency})
                    continue
                if item['estrategia_dimension'].obligatorio and not self.allow_incomplete:
                    self.add_error(field_name, 'Selecciona un valor para esta dimensión.')
                continue

            payload = selected_value_payload(item, selected)
            if payload['value'] is None and not payload['text']:
                self.add_error(field_name, 'La opción seleccionada no tiene un valor numérico válido.')
                continue
            remember_evaluation(
                item,
                payload['value'],
                payload['text'],
                payload['catalogo_fila'],
                payload['escala_valor'],
            )

        while pending_items:
            progressed = False
            remaining = []
            for pending in pending_items:
                item = pending['item']
                field_name = item['field_name']
                if item['estrategia_dimension'].pk in resolved_dimension_ids:
                    progressed = True
                    continue

                if pending['kind'] == 'calculated':
                    value = _rcm_evaluate_calculation(
                        item['dimension'].tipo_calculo,
                        item['dimension'].config_calculo,
                        source_values,
                    )
                    value = _rcm_int_from_decimal(value)
                    if value is None:
                        remaining.append(pending)
                        continue
                    remember_evaluation(item, value)
                    progressed = True
                    continue

                source_value = _rcm_source_value(source_values, pending.get('dependency'))
                if source_value is None:
                    remaining.append(pending)
                    continue
                try:
                    catalogo = item['estrategia_dimension'].catalogo
                except app_models.DimensionCatalogo.DoesNotExist:
                    catalogo = None
                matched_row = _rcm_match_dependency_row(catalogo, source_value)
                if not matched_row:
                    remaining.append(pending)
                    continue
                payload = selected_value_payload(item, matched_row)
                if payload['value'] is None and not payload['text']:
                    self.add_error(field_name, 'La fila resuelta no tiene un valor numérico ni texto válido.')
                    progressed = True
                    continue
                cleaned[field_name] = matched_row
                remember_evaluation(
                    item,
                    payload['value'],
                    payload['text'],
                    payload['catalogo_fila'],
                    payload['escala_valor'],
                )
                progressed = True

            if not progressed:
                pending_items = remaining
                break
            pending_items = remaining

        for pending in pending_items:
            item = pending['item']
            if not item['estrategia_dimension'].obligatorio or self.allow_incomplete:
                continue
            if pending['kind'] == 'calculated':
                self.add_error(item['field_name'], 'No se pudo calcular esta dimensión con los valores seleccionados.')
            else:
                self.add_error(item['field_name'], 'No se pudo resolver esta dimensión con los valores seleccionados.')

        return cleaned

    def clean(self):
        cleaned = self._clean_dynamic_rcm_evaluations()
        familia = cleaned.get('familia_equipo')
        equipo = cleaned.get('equipo')
        if familia and equipo:
            self.add_error('familia_equipo', 'Elige una familia o un equipo individual, no ambos.')
        if familia and not familia.items.exists():
            self.add_error('familia_equipo', 'La familia seleccionada no tiene equipos.')
        if not familia and not equipo:
            self.add_error('equipo', 'Selecciona un equipo o una familia de equipos.')
        return cleaned


class TareaRCMForm(forms.Form):
    id = forms.IntegerField(required=False, widget=forms.HiddenInput())
    valores_json = forms.CharField(required=False, widget=forms.HiddenInput())
    tipo_tarea_estrategia = forms.ModelChoiceField(
        queryset=app_models.TipoTareaEstrategia.objects.none(),
        required=False,
        label='Tipo de tarea',
        empty_label='Selecciona un tipo',
    )
    descripcion = forms.CharField(required=False, label='Descripción de tarea', widget=forms.Textarea(attrs={'rows': 2}))
    tactica = forms.CharField(required=False, label='Táctica', max_length=100)
    limite_aceptable = forms.CharField(required=False, label='Límite aceptable', widget=forms.Textarea(attrs={'rows': 2}))
    parametros = forms.CharField(required=False, label='Parámetros', widget=forms.Textarea(attrs={'rows': 2}))
    riesgo_material = forms.CharField(required=False, label='Riesgo material', widget=forms.Textarea(attrs={'rows': 2}))
    especialidad = forms.CharField(required=False, label='Especialidad', max_length=100)
    puesto_trabajo = forms.CharField(required=False, label='Puesto de trabajo', max_length=100)
    estado_equipo = forms.CharField(required=False, label='Estado equipo', max_length=100)
    frecuencia_valor = forms.DecimalField(required=False, label='Frecuencia valor', max_digits=10, decimal_places=2)
    frecuencia_unidad = forms.CharField(required=False, label='Unidad frecuencia', max_length=50)
    frecuencia_texto = forms.CharField(required=False, label='Frecuencia texto', max_length=150)
    duracion_min = forms.DecimalField(required=False, label='Duración min', max_digits=10, decimal_places=2)
    duracion_hr = forms.DecimalField(required=False, label='Duración hr', max_digits=10, decimal_places=2)
    cantidad_personas = forms.DecimalField(required=False, label='Personas', max_digits=10, decimal_places=2)
    hh = forms.DecimalField(required=False, label='HH', max_digits=10, decimal_places=2)
    plan_sap = forms.CharField(required=False, label='Plan SAP', max_length=100)
    descripcion_plan = forms.CharField(required=False, label='Descripción plan', widget=forms.Textarea(attrs={'rows': 2}))
    hoja_ruta = forms.CharField(required=False, label='Hoja ruta', max_length=100)
    texto_hoja_ruta = forms.CharField(required=False, label='Texto hoja ruta', widget=forms.Textarea(attrs={'rows': 2}))
    operacion_hoja_ruta = forms.CharField(required=False, label='Operación hoja ruta', max_length=100)
    texto_operacion = forms.CharField(required=False, label='Texto operación', widget=forms.Textarea(attrs={'rows': 2}))
    operacion_pauta = forms.CharField(required=False, label='Operación pauta', max_length=100)
    pauta = forms.CharField(required=False, label='Pauta', max_length=150)
    titulo_pauta = forms.CharField(required=False, label='Título pauta', widget=forms.Textarea(attrs={'rows': 2}))
    repuesto = forms.CharField(required=False, label='Repuesto', widget=forms.Textarea(attrs={'rows': 2}))
    componente_involucrado = forms.CharField(required=False, label='Componente involucrado', widget=forms.Textarea(attrs={'rows': 2}))
    numero_parte = forms.CharField(required=False, label='Número parte', max_length=100)
    numero_sap = forms.CharField(required=False, label='Número SAP', max_length=100)
    procedimiento_trabajo = forms.CharField(required=False, label='Procedimiento trabajo', widget=forms.Textarea(attrs={'rows': 2}))
    costo_hh = forms.DecimalField(required=False, label='Costo HH', max_digits=12, decimal_places=2)
    costo_repuestos = forms.DecimalField(required=False, label='Costo repuestos', max_digits=12, decimal_places=2)
    tarifa_servicios = forms.DecimalField(required=False, label='Tarifa servicios', max_digits=12, decimal_places=2)
    costo_total = forms.DecimalField(required=False, label='Costo total', max_digits=12, decimal_places=2)
    oportunidad_mejora = forms.CharField(required=False, label='Oportunidad mejora', widget=forms.Textarea(attrs={'rows': 2}))
    estado = forms.ChoiceField(
        required=False,
        label='Estado',
        choices=app_models.TareaRCM.ESTADO_CHOICES,
        initial=app_models.TareaRCM.ESTADO_ACTIVO,
    )

    empty_permitted_fields = {'id', 'DELETE', 'estado', 'valores_json', 'dynamic_values', 'tipo_tarea_estrategia'}

    def __init__(self, *args, estrategia=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.estrategia = estrategia
        self.empty_task = False
        tipos_qs = app_models.TipoTareaEstrategia.objects.none()
        if estrategia:
            tipos_qs = app_models.TipoTareaEstrategia.objects.filter(
                estrategia=estrategia,
                activo=True,
            ).order_by('orden', 'nombre')
        self.fields['tipo_tarea_estrategia'].queryset = tipos_qs

        for _, field in self.fields.items():
            widget = field.widget
            css = 'input-textarea' if isinstance(widget, forms.Textarea) else 'input-control'
            if isinstance(widget, forms.HiddenInput) or isinstance(widget, forms.CheckboxInput):
                css = ''
            existing = widget.attrs.get('class', '')
            widget.attrs['class'] = f'{existing} {css}'.strip()
            if not isinstance(widget, forms.HiddenInput):
                widget.attrs.setdefault('placeholder', field.label)

    def _has_task_content(self):
        data = self.cleaned_data
        if data.get('dynamic_values'):
            return True
        for key, value in data.items():
            if key in self.empty_permitted_fields:
                continue
            if value not in (None, ''):
                return True
        return False

    def clean(self):
        cleaned = super().clean()
        raw_values = cleaned.get('valores_json') or '{}'
        try:
            dynamic_values = json.loads(raw_values)
        except (TypeError, json.JSONDecodeError):
            dynamic_values = {}
        if not isinstance(dynamic_values, dict):
            dynamic_values = {}
        dynamic_values = {
            str(key): value
            for key, value in dynamic_values.items()
            if value not in (None, '', [])
        }
        cleaned['dynamic_values'] = dynamic_values

        if cleaned.get('DELETE'):
            return cleaned
        has_content = self._has_task_content()
        self.empty_task = not has_content
        if not has_content:
            return cleaned
        if not cleaned.get('tipo_tarea_estrategia'):
            self.add_error('tipo_tarea_estrategia', 'Selecciona el tipo de tarea.')
            return cleaned

        configured_fields = cleaned['tipo_tarea_estrategia'].campos.filter(activo=True).order_by('orden', 'nombre')
        if configured_fields.exists():
            for field in configured_fields:
                value = dynamic_values.get(field.clave)
                if field.obligatorio and value in (None, '', []):
                    self.add_error(None, f'Completa el campo obligatorio "{field.nombre}".')
        return cleaned


RCMTaskFormSet = formset_factory(TareaRCMForm, extra=0, can_delete=True)

__all__ = ['RCMRegistroForm', 'TareaRCMForm', 'RCMTaskFormSet']
