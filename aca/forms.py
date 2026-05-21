import json
from decimal import Decimal, InvalidOperation

from django import forms
from django.db.models import Q
from django.forms import formset_factory
from django.utils import timezone

from core import models as app_models
from core.access import get_service_equipment

class ACARegistroForm(forms.Form):
    servicio = forms.ModelChoiceField(queryset=app_models.Servicio.objects.select_related('empresa', 'estrategia').order_by('-creado_en', 'codigo_servicio'))
    estrategia = forms.ModelChoiceField(queryset=app_models.Estrategia.objects.select_related('empresa').order_by('empresa__nombre','nombre'))
    fecha_analisis = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date', 'readonly': 'readonly'}),
        initial=timezone.localdate,
    )
    version_carga = forms.DecimalField(max_digits=4, decimal_places=1, initial=Decimal('1.0'))
    origen = forms.CharField(initial='Manual')
    usuario = forms.ModelChoiceField(queryset=app_models.Usuario.objects.select_related('empresa', 'cargo').order_by('nombre_completo'), required=False)
    equipo = forms.ModelChoiceField(queryset=app_models.Equipo.objects.none())
    escenario_falla = forms.CharField(required=False, widget=forms.Textarea)
    frecuencia_original = forms.DecimalField(max_digits=10, decimal_places=2, required=False)
    frecuencia_normalizada = forms.DecimalField(max_digits=10, decimal_places=2, required=False)

    def __init__(self, *args, service=None, strategy=None, **kwargs):
        super().__init__(*args, **kwargs)
        for _, field in self.fields.items():
            widget = field.widget
            css = 'input-textarea' if isinstance(widget, forms.Textarea) else 'input-control'
            existing = widget.attrs.get('class', '')
            widget.attrs['class'] = f'{existing} {css}'.strip()

        service_obj = None
        strategy_obj = None

        service_id = None
        if self.is_bound:
            service_id = self.data.get('servicio') or None
        elif service is not None:
            service_id = getattr(service, 'pk', service)
        elif self.initial.get('servicio'):
            service_id = getattr(self.initial.get('servicio'), 'pk', self.initial.get('servicio'))
        if service_id:
            try:
                service_obj = app_models.Servicio.objects.select_related('empresa', 'estrategia').get(pk=service_id)
            except app_models.Servicio.DoesNotExist:
                service_obj = None

        strategy_id = None
        if self.is_bound:
            strategy_id = self.data.get('estrategia') or None
        elif strategy is not None:
            strategy_id = getattr(strategy, 'pk', strategy)
        elif self.initial.get('estrategia'):
            strategy_id = getattr(self.initial.get('estrategia'), 'pk', self.initial.get('estrategia'))
        elif service_obj and service_obj.estrategia_id:
            strategy_id = service_obj.estrategia_id

        strategy_qs = app_models.Estrategia.objects.select_related('empresa').order_by('empresa__nombre', 'nombre')
        if service_obj:
            strategy_qs = strategy_qs.filter(empresa_id=service_obj.empresa_id)
        self.fields['estrategia'].queryset = strategy_qs
        if strategy_id and not self.is_bound:
            self.initial.setdefault('estrategia', strategy_id)
        if strategy_id:
            try:
                strategy_obj = strategy_qs.get(pk=strategy_id)
            except app_models.Estrategia.DoesNotExist:
                try:
                    strategy_obj = app_models.Estrategia.objects.select_related('empresa').get(pk=strategy_id)
                except app_models.Estrategia.DoesNotExist:
                    strategy_obj = None

        empresa_id = None
        if service_obj and service_obj.empresa_id:
            empresa_id = service_obj.empresa_id
        elif strategy_obj and strategy_obj.empresa_id:
            empresa_id = strategy_obj.empresa_id

        equipos_qs = get_service_equipment(service_obj) if service_obj else app_models.Equipo.objects.none()
        if not service_obj and empresa_id:
            equipos_qs = app_models.Equipo.objects.filter(
                Q(nodo__empresa_id=empresa_id) | Q(nodo__isnull=True)
            ).select_related('nodo', 'nodo__empresa').distinct().order_by('tag_equipo', 'nombre_equipo')

        self.fields['equipo'].queryset = equipos_qs
        self.fields['equipo'].label_from_instance = lambda obj: f"{obj.tag_display} - {obj.nombre_equipo}"

        if not self.is_bound:
            self.initial.setdefault('fecha_analisis', timezone.localdate())

        self.selected_service = service_obj
        self.selected_strategy = strategy_obj

    def clean_fecha_analisis(self):
        return timezone.localdate()


def _catalog_primary_numeric_from_values(values):
    for key in ['valor_numerico', 'valor_principal', 'valor', 'nivel', 'puntaje']:
        value = values.get(key) if isinstance(values, dict) else None
        if value not in (None, ''):
            return _decimal_or_none(value)
    if isinstance(values, dict):
        for key, value in values.items():
            if key in {'limite_inferior', 'limite_superior', 'desde', 'hasta', 'min', 'max', 'minimo', 'mínimo', 'maximo', 'máximo'}:
                continue
            decimal_value = _decimal_or_none(value)
            if decimal_value is not None:
                return decimal_value
    return None


def _catalog_dependency_match_from_values(values):
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
            return str(value)
    primary = _catalog_primary_numeric_from_values(values)
    return str(primary) if primary is not None else ''


def _catalog_dependency_ref_from_config(config):
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


def _catalog_dependency_from_config(config):
    ref = _catalog_dependency_ref_from_config(config)
    if isinstance(ref, dict):
        return str(
            ref.get('nombre')
            or ref.get('campo')
            or ref.get('source')
            or ref.get('estrategia_dimension_id')
            or ref.get('dimension_id')
            or ''
        ).strip()
    return str(ref or '').strip()


def _catalog_bound_from_values(values, keys):
    if not isinstance(values, dict):
        return ''
    for key in keys:
        value = values.get(key)
        if value not in (None, ''):
            return str(value)
    return ''


class CriticidadDimensionInputForm(forms.Form):
    dimension = forms.ModelChoiceField(queryset=app_models.Dimension.objects.none(), widget=forms.HiddenInput())
    escala_valor = forms.ModelChoiceField(queryset=app_models.EscalaValor.objects.none(), required=False)
    catalogo_fila = forms.ModelChoiceField(queryset=app_models.DimensionCatalogoFila.objects.none(), required=False)
    valor_numerico = forms.DecimalField(required=False, max_digits=12, decimal_places=2)
    valor_secundario = forms.DecimalField(required=False, max_digits=12, decimal_places=2, widget=forms.HiddenInput())
    valor_booleano = forms.TypedChoiceField(
        required=False,
        choices=(('', '—'), ('true', 'Sí'), ('false', 'No')),
        coerce=lambda v: None if v == '' else v == 'true',
    )
    valor_texto = forms.CharField(required=False, widget=forms.Textarea)

    def __init__(self, *args, estrategia=None, proceso=None, **kwargs):
        super().__init__(*args, **kwargs)
        proceso = proceso or app_models.EstrategiaDimension.PROCESO_ACA
        for name, field in self.fields.items():
            widget = field.widget
            css = 'input-textarea' if isinstance(widget, forms.Textarea) else 'input-control'
            existing = widget.attrs.get('class', '')
            widget.attrs['class'] = f'{existing} {css}'.strip()
            if name == 'dimension':
                continue

        qs_dim = app_models.Dimension.objects.none()
        qs_scale = app_models.EscalaValor.objects.none()
        qs_catalog_rows = app_models.DimensionCatalogoFila.objects.none()
        if estrategia is not None:
            qs_dim = app_models.Dimension.objects.filter(
                estrategias_dimension__estrategia=estrategia,
                estrategias_dimension__activo=True,
                estrategias_dimension__proceso_uso__in=[
                    proceso,
                    app_models.EstrategiaDimension.PROCESO_AMBOS,
                ],
            ).distinct().order_by('estrategias_dimension__orden', 'nombre')
            qs_scale = app_models.EscalaValor.objects.filter(
                estrategia_dimension__estrategia=estrategia,
                estrategia_dimension__activo=True,
                estrategia_dimension__proceso_uso__in=[
                    proceso,
                    app_models.EstrategiaDimension.PROCESO_AMBOS,
                ],
            ).select_related('estrategia_dimension', 'estrategia_dimension__dimension', 'escala_unificada').order_by(
                'estrategia_dimension__orden', 'nivel_ordinal'
            )
            qs_catalog_rows = app_models.DimensionCatalogoFila.objects.filter(
                catalogo__estrategia_dimension__estrategia=estrategia,
                catalogo__estrategia_dimension__activo=True,
                catalogo__estrategia_dimension__proceso_uso__in=[
                    proceso,
                    app_models.EstrategiaDimension.PROCESO_AMBOS,
                ],
            ).select_related(
                'catalogo', 'catalogo__estrategia_dimension', 'catalogo__estrategia_dimension__dimension'
            ).prefetch_related('celdas__columna', 'catalogo__columnas').order_by(
                'catalogo__estrategia_dimension__orden', 'orden'
            )

        current_dimension_id = None
        dimension_obj = None
        if self.is_bound:
            current_dimension_id = self.data.get(self.add_prefix('dimension')) or None
        else:
            dim_initial = self.initial.get('dimension') if hasattr(self, 'initial') else None
            current_dimension_id = getattr(dim_initial, 'pk', dim_initial) if dim_initial else None
            dimension_obj = dim_initial if hasattr(dim_initial, 'nombre') else None
        self.fields['dimension'].queryset = qs_dim
        if not dimension_obj and current_dimension_id:
            try:
                dimension_obj = qs_dim.get(pk=current_dimension_id)
            except Exception:
                dimension_obj = None
        self.dimension_obj = dimension_obj
        self.dimension_campo = ''
        self.tipo_calculo = (getattr(dimension_obj, 'tipo_calculo', '') or '').strip() if dimension_obj else ''
        self.config_calculo = {}
        self.config_calculo_json = '{}'
        self.catalog_dependency = ''
        self.catalog_dependency_estrategia_dimension_id = ''
        self.catalog_dependency_dimension_id = ''
        self.catalog_dependency_campo = ''
        self.catalog_type = ''
        self.is_dependent_catalog = False
        catalogo_obj = None
        self.estrategia_dimension_obj = None
        if dimension_obj:
            try:
                estrategia_dimension_obj = app_models.EstrategiaDimension.objects.select_related('dimension').filter(
                    estrategia=estrategia,
                    dimension=dimension_obj,
                    activo=True,
                    proceso_uso__in=[
                        proceso,
                        app_models.EstrategiaDimension.PROCESO_AMBOS,
                    ],
                ).first()
                self.estrategia_dimension_obj = estrategia_dimension_obj
                catalogo_obj = getattr(estrategia_dimension_obj, 'catalogo', None) if estrategia_dimension_obj else None
                self.catalog_type = getattr(catalogo_obj, 'tipo', '') if catalogo_obj else ''
                self.dimension_campo = getattr(catalogo_obj, 'campo', '') or ''
            except Exception:
                self.dimension_campo = ''
            raw_config = getattr(dimension_obj, 'config_calculo', '') or ''
            if raw_config:
                try:
                    self.config_calculo = json.loads(raw_config)
                except Exception:
                    self.config_calculo = {}
            self.config_calculo_json = json.dumps(self.config_calculo, ensure_ascii=False)
            dependency_ref = _catalog_dependency_ref_from_config(self.config_calculo)
            self.catalog_dependency = _catalog_dependency_from_config(self.config_calculo)
            if isinstance(dependency_ref, dict):
                self.catalog_dependency_estrategia_dimension_id = dependency_ref.get('estrategia_dimension_id') or ''
                self.catalog_dependency_dimension_id = dependency_ref.get('dimension_id') or ''
                self.catalog_dependency_campo = dependency_ref.get('campo') or dependency_ref.get('source') or ''

        if current_dimension_id:
            qs_scale = qs_scale.filter(estrategia_dimension__dimension_id=current_dimension_id)
            qs_catalog_rows = qs_catalog_rows.filter(catalogo__estrategia_dimension__dimension_id=current_dimension_id)

        self.fields['escala_valor'].queryset = qs_scale
        self.fields['catalogo_fila'].queryset = qs_catalog_rows
        self.fields['escala_valor'].label_from_instance = lambda obj: f"{obj.codigo or obj.descripcion} ({obj.valor_numerico})"

        def _catalog_label(obj):
            values = obj.values_map()
            columns = list(obj.catalogo.columnas.all().order_by('orden'))
            is_dependent = bool(_catalog_dependency_from_config(obj.catalogo.estrategia_dimension.dimension.config_calculo))
            extras = []
            for col in columns[:3]:
                val = values.get(col.clave_interna)
                if val not in (None, '') and col.clave_interna not in {'etiqueta', 'nombre', 'descripcion'}:
                    extras.append(f"{col.nombre_columna}: {val}")

            if is_dependent:
                for value_key in ['valor_numerico', 'valor_principal', 'valor', 'nivel', 'puntaje']:
                    val = values.get(value_key)
                    if val in (None, ''):
                        continue
                    column = next((col for col in columns if col.clave_interna == value_key), None)
                    label = column.nombre_columna if column else 'Valor'
                    display = f"{label}: {val}"
                    if display not in extras:
                        extras.append(display)
                    break

            return f"{' || '.join(extras)}" if extras else ''

        self.fields['catalogo_fila'].label_from_instance = _catalog_label

        self.has_scale = qs_scale.exists()
        self.has_catalog = qs_catalog_rows.exists()
        mode = 'texto'
        if self.tipo_calculo:
            mode = 'calculado'
        elif self.has_scale:
            mode = 'escala'
        elif self.has_catalog:
            mode = 'catalogo'
        elif dimension_obj and dimension_obj.tipo_dato == 'booleano':
            mode = 'booleano'
        elif dimension_obj and dimension_obj.tipo_dato == 'numerico':
            mode = 'numerico'
        self.input_mode = mode
        if mode == 'calculado':
            self.fields['valor_numerico'].widget = forms.HiddenInput()
        self.is_dependent_catalog = bool(
            mode == 'catalogo'
            and self.catalog_dependency
            and catalogo_obj
            and catalogo_obj.tipo in {'rangos', 'opciones'}
        )

        self.option_headers = []
        self.option_rows = []

        if self.has_scale:
            self.option_headers = ['Código', 'Descripción', 'Valor', 'Escala unificada']
            for option in qs_scale:
                self.option_rows.append({
                    'pk': option.pk,
                    'cells': [
                        option.codigo or '',
                        option.descripcion or '',
                        option.valor_numerico,
                        str(option.escala_unificada) if option.escala_unificada_id else '',
                    ],
                    'value_numeric': str(option.valor_numerico) if option.valor_numerico is not None else '',
                })

        elif self.has_catalog:
            catalogo = qs_catalog_rows.first().catalogo if qs_catalog_rows.exists() else None
            columnas = list(catalogo.columnas.all().order_by('orden')) if catalogo else []
            self.option_headers = [col.nombre_columna for col in columnas] or ['Etiqueta']
            for fila in qs_catalog_rows:
                values = fila.values_map()
                cells = []
                if columnas:
                    for col in columnas:
                        default_value = fila.etiqueta if col.clave_interna == 'etiqueta' else ''
                        cells.append(values.get(col.clave_interna, default_value))
                else:
                    cells.append(fila.etiqueta)
                self.option_rows.append({
                    'pk': fila.pk,
                    'cells': cells,
                    'value_numeric': str(_catalog_primary_numeric_from_values(values) or ''),
                    'match_value': _catalog_dependency_match_from_values(values),
                    'range_min': _catalog_bound_from_values(values, ['limite_inferior', 'desde', 'min', 'minimo', 'mínimo']),
                    'range_max': _catalog_bound_from_values(values, ['limite_superior', 'hasta', 'max', 'maximo', 'máximo']),
                })


def _decimal_or_none(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value).strip().replace(',', '.'))
    except (InvalidOperation, ValueError, TypeError):
        return None


CriticidadDimensionFormSet = formset_factory(CriticidadDimensionInputForm, extra=0, can_delete=False)


class ServicioACARegistroForm(forms.Form):
    fecha_analisis = forms.DateField(
        required=False,
        label='Fecha de análisis',
        widget=forms.HiddenInput(),
        initial=timezone.localdate,
    )
    version_carga = forms.DecimalField(
        max_digits=4,
        decimal_places=1,
        initial=Decimal('1.0'),
        label='Versión carga',
        widget=forms.HiddenInput(),
    )
    origen = forms.CharField(
        initial='Manual',
        label='Origen',
        widget=forms.HiddenInput(),
    )
    equipo = forms.ModelChoiceField(
        queryset=app_models.Equipo.objects.none(),
        label='Equipo',
    )
    escenario_falla = forms.CharField(
        required=False,
        label='Escenario de falla',
        widget=forms.Textarea,
    )
    frecuencia_original = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        label='Frecuencia falla',
    )
    frecuencia_normalizada = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        label='Frecuencia normalizada',
        widget=forms.HiddenInput(),
    )
    matrix_celda = forms.ModelChoiceField(
        queryset=app_models.MatrizRiesgoCelda.objects.none(),
        required=False,
        widget=forms.HiddenInput(),
    )

    def __init__(self, *args, service=None, allow_incomplete=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.service = service
        self.matriz = None
        self.allow_incomplete = allow_incomplete

        equipos_qs = get_service_equipment(service) if service else app_models.Equipo.objects.none()
        self.fields['equipo'].queryset = equipos_qs
        self.fields['equipo'].label_from_instance = lambda obj: f"{obj.tag_display} - {obj.nombre_equipo}"
        self.fields['equipo'].empty_label = 'Selecciona un equipo'
        self.fields['equipo'].required = not allow_incomplete

        if service and getattr(service, 'estrategia_id', None):
            self.matriz = app_models.MatrizRiesgo.objects.filter(
                estrategia=service.estrategia
            ).order_by('-fecha_creado', '-id').first()

            if self.matriz:
                self.fields['matrix_celda'].queryset = app_models.MatrizRiesgoCelda.objects.filter(
                    matriz=self.matriz
                ).select_related(
                    'probabilidad',
                    'impacto_nivel',
                ).order_by(
                    'probabilidad__orden_visual',
                    'impacto_nivel__orden_visual',
                )

        if not self.is_bound:
            self.initial.setdefault('fecha_analisis', timezone.localdate())

        for _, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.HiddenInput):
                continue
            css = 'input-textarea' if isinstance(widget, forms.Textarea) else 'input-control'
            existing = widget.attrs.get('class', '')
            widget.attrs['class'] = f'{existing} {css}'.strip()

    def clean_fecha_analisis(self):
        return timezone.localdate()

    def clean(self):
        cleaned = super().clean()
        return cleaned

__all__ = ['ACARegistroForm', 'CriticidadDimensionInputForm', 'CriticidadDimensionFormSet', 'ServicioACARegistroForm']
