from decimal import Decimal, InvalidOperation
import json

from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.db import models
from django.forms import formset_factory
from django.utils import timezone

from . import models as app_models
from .access import get_service_equipment, resolve_auth_user_for_email, get_users_for_service_access
from .user_sync import split_full_name, sync_auth_user_from_profile
from .models import Empresa, Servicio, Servicio, Metodologia, ServicioMetodologia

class BaseModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for _, field in self.fields.items():
            widget = field.widget
            css = 'input-textarea' if isinstance(widget, forms.Textarea) else 'input-control'
            existing = widget.attrs.get('class', '')
            widget.attrs['class'] = f'{existing} {css}'.strip()

            if isinstance(field, forms.DateField):
                widget.input_type = 'date'
            elif isinstance(field, forms.DateTimeField):
                widget.input_type = 'datetime-local'

            if isinstance(field, forms.BooleanField):
                widget.attrs['class'] = 'input-checkbox'
                widget.attrs.pop('placeholder', None)
            else:
                widget.attrs.setdefault('placeholder', field.label)


FORM_CACHE = {}

class EmpresaForm(BaseModelForm):
    logo_file = forms.ImageField(required=False, label='Logo')

    class Meta:
        model = app_models.Empresa
        fields = '__all__'
        widgets = {
            'logo': forms.HiddenInput(),
        }

    def clean_nombre(self):
        nombre = self.cleaned_data.get('nombre', '').strip()
        qs = Empresa.objects.filter(nombre__iexact=nombre)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('Ya existe una empresa con ese nombre.')
        return nombre
    
    def clean_sigla(self):
        sigla = self.cleaned_data.get("sigla", "").strip()

        qs = Empresa.objects.filter(sigla__iexact=sigla)

        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)

        if qs.exists():
            raise forms.ValidationError("Ya existe una empresa con esta sigla.")

        return sigla
    
    def save(self, commit=True):
        instance = super().save(commit=False)

        logo_file = self.cleaned_data.get('logo_file')
        if logo_file:
            instance.logo = logo_file.read()

        if commit:
            instance.save()

        return instance

def build_modelform(model_class):
    if model_class in FORM_CACHE:
        return FORM_CACHE[model_class]

    excluded = []
    for field in model_class._meta.fields:
        if field.name == 'id' or isinstance(field, models.AutoField):
            excluded.append(field.name)

    class DynamicForm(BaseModelForm):
        class Meta:
            model = model_class
            exclude = excluded

    FORM_CACHE[model_class] = DynamicForm
    return DynamicForm


class UsuarioSyncForm(BaseModelForm):
    password = forms.CharField(
        required=False,
        label='Contraseña',
        widget=forms.PasswordInput(render_value=False),
        help_text='Obligatoria al crear desde la plataforma. En edición, déjala vacía para mantener la actual.',
    )
    password_confirm = forms.CharField(
        required=False,
        label='Confirmar contraseña',
        widget=forms.PasswordInput(render_value=False),
    )

    class Meta:
        model = app_models.Usuario
        fields = ['nombre_completo', 'correo_corporativo', 'cargo', 'empresa', 'auth_user']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['auth_user'].required = False
        self.fields['auth_user'].help_text = 'Se completa y sincroniza automáticamente. Solo úsalo si quieres enlazar un usuario ya existente.'
        self.fields['correo_corporativo'].help_text = 'Se usará también para el login.'
        if self.instance and getattr(self.instance, 'pk', None):
            self.fields['password'].help_text = 'Déjala vacía para conservar la contraseña actual.'

    def clean_correo_corporativo(self):
        email = (self.cleaned_data.get('correo_corporativo') or '').strip().lower()
        qs = app_models.Usuario.objects.filter(correo_corporativo__iexact=email)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('Ya existe un perfil con ese correo.')
        return email

    def clean(self):
        cleaned = super().clean()
        email = cleaned.get('correo_corporativo')
        password = cleaned.get('password') or ''
        password_confirm = cleaned.get('password_confirm') or ''
        linked_user = cleaned.get('auth_user')

        if not self.instance.pk and not password:
            self.add_error('password', 'La contraseña es obligatoria al crear un usuario desde la plataforma.')
        if password or password_confirm:
            if password != password_confirm:
                self.add_error('password_confirm', 'Las contraseñas no coinciden.')

        if linked_user and email:
            existing = User.objects.filter(email__iexact=email).exclude(pk=linked_user.pk).first()
            if existing:
                raise forms.ValidationError('Ese correo ya está asociado a otro usuario de autenticación.')

        full_name = (cleaned.get('nombre_completo') or '').strip()
        if not full_name:
            self.add_error('nombre_completo', 'Ingresa el nombre completo.')
        return cleaned

    def save(self, commit=True):
        profile = super().save(commit=commit)
        if not commit:
            return profile

        raw_password = (self.cleaned_data.get('password') or '').strip() or None
        linked_user = self.cleaned_data.get('auth_user')
        if linked_user and profile.auth_user_id != linked_user.pk:
            profile.auth_user = linked_user
            profile.save(update_fields=['auth_user'])

        user = sync_auth_user_from_profile(profile, raw_password=raw_password)
        first_name, last_name = split_full_name(profile.nombre_completo)
        changed = []
        if user.email != profile.correo_corporativo:
            user.email = profile.correo_corporativo
            changed.append('email')
        if user.first_name != first_name:
            user.first_name = first_name
            changed.append('first_name')
        if user.last_name != last_name:
            user.last_name = last_name
            changed.append('last_name')
        if changed:
            user.save(update_fields=changed)
        return profile


class MatrizBuilderForm(forms.Form):
    nombre = forms.CharField()
    fecha_creado = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    estrategia = forms.ModelChoiceField(
        queryset=app_models.Estrategia.objects.select_related('empresa').all()
    )
    dimension_probabilidad = forms.ModelChoiceField(
        queryset=app_models.EstrategiaDimension.objects.none(),
        required=False,
        label='Dimension eje probabilidad',
        empty_label='Crear dimension automatica',
        help_text='Dimension existente que entrega el valor para el eje de probabilidad.',
    )
    dimension_impacto = forms.ModelChoiceField(
        queryset=app_models.EstrategiaDimension.objects.none(),
        required=False,
        label='Dimension eje impacto',
        empty_label='Crear dimension automatica',
        help_text='Dimension existente que entrega el valor para el eje de impacto.',
    )
    eje_horizontal = forms.ChoiceField(
        choices=app_models.MatrizRiesgo.EJE_HORIZONTAL_CHOICES,
        initial='impacto',
    )
    x_count = forms.IntegerField(min_value=2, max_value=12, initial=5)
    y_count = forms.IntegerField(min_value=2, max_value=12, initial=5)

    def __init__(self, *args, strategy=None, **kwargs):
        super().__init__(*args, **kwargs)
        strategy_id = getattr(strategy, 'pk', strategy) if strategy is not None else None
        if self.is_bound:
            strategy_id = self.data.get(self.add_prefix('estrategia')) or strategy_id
        elif self.initial.get('estrategia'):
            strategy_id = getattr(self.initial.get('estrategia'), 'pk', self.initial.get('estrategia'))

        axis_qs = app_models.EstrategiaDimension.objects.filter(
            activo=True,
        ).select_related(
            'estrategia',
            'dimension',
        ).order_by(
            'estrategia__nombre',
            'orden',
            'dimension__nombre',
        )
        if strategy_id:
            axis_qs = axis_qs.filter(estrategia_id=strategy_id)

        for field_name in ('dimension_probabilidad', 'dimension_impacto'):
            self.fields[field_name].queryset = axis_qs
            self.fields[field_name].label_from_instance = (
                lambda obj: f'{obj.estrategia} / {obj.dimension.nombre}'
            )

        for _, field in self.fields.items():
            existing = field.widget.attrs.get('class', '')
            field.widget.attrs['class'] = f'{existing} input-control'.strip()

    def clean(self):
        cleaned = super().clean()
        estrategia = cleaned.get('estrategia')
        for field_name in ('dimension_probabilidad', 'dimension_impacto'):
            estrategia_dimension = cleaned.get(field_name)
            if not estrategia or not estrategia_dimension:
                continue
            if estrategia_dimension.estrategia_id != estrategia.pk:
                self.add_error(
                    field_name,
                    'La dimension elegida debe pertenecer a la estrategia seleccionada.',
                )
        return cleaned


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

        equipos_qs = app_models.Equipo.objects.none()
        if service_obj:
            equipos_qs = app_models.Equipo.objects.filter(
                servicios_equipo__servicio_id=service_obj.pk
            ).select_related('sistema', 'sistema__empresa').distinct().order_by('tag_equipo', 'nombre_equipo')

        if empresa_id:
            fallback_qs = app_models.Equipo.objects.filter(
                sistema__empresa_id=empresa_id
            ).select_related('sistema', 'sistema__empresa').distinct().order_by('tag_equipo', 'nombre_equipo')
            if equipos_qs.exists():
                equipos_qs = (equipos_qs | fallback_qs).distinct().order_by('tag_equipo', 'nombre_equipo')
            else:
                equipos_qs = fallback_qs

        self.fields['equipo'].queryset = equipos_qs
        self.fields['equipo'].label_from_instance = lambda obj: f"{obj.tag_equipo} - {obj.nombre_equipo}"

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


def _catalog_dependency_from_config(config):
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
            return str(value).strip()
    return ''


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

    def __init__(self, *args, estrategia=None, **kwargs):
        super().__init__(*args, **kwargs)
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
            ).distinct().order_by('estrategias_dimension__orden', 'nombre')
            qs_scale = app_models.EscalaValor.objects.filter(
                estrategia_dimension__estrategia=estrategia,
                estrategia_dimension__activo=True,
            ).select_related('estrategia_dimension', 'estrategia_dimension__dimension', 'escala_unificada').order_by(
                'estrategia_dimension__orden', 'nivel_ordinal'
            )
            qs_catalog_rows = app_models.DimensionCatalogoFila.objects.filter(
                catalogo__estrategia_dimension__estrategia=estrategia,
                catalogo__estrategia_dimension__activo=True,
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
        self.catalog_type = ''
        self.is_dependent_catalog = False
        catalogo_obj = None
        self.estrategia_dimension_obj = None
        if dimension_obj:
            try:
                estrategia_dimension_obj = app_models.EstrategiaDimension.objects.select_related('dimension').filter(estrategia=estrategia, dimension=dimension_obj, activo=True).first()
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
            self.catalog_dependency = _catalog_dependency_from_config(self.config_calculo)

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


class EmailLoginForm(forms.Form):
    correo = forms.EmailField(label='Correo corporativo')
    password = forms.CharField(label='Contraseña', widget=forms.PasswordInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for _, field in self.fields.items():
            existing = field.widget.attrs.get('class', '')
            field.widget.attrs['class'] = f'{existing} input-control'.strip()

    def clean(self):
        cleaned = super().clean()
        correo = cleaned.get('correo')
        password = cleaned.get('password')
        if not correo or not password:
            return cleaned
        user, profile = resolve_auth_user_for_email(correo)
        if not user:
            raise forms.ValidationError('No existe un usuario asociado a ese correo.')
        auth_user = authenticate(username=user.username, password=password)
        if not auth_user:
            raise forms.ValidationError('Correo o contraseña inválidos.')
        cleaned['auth_user'] = auth_user
        cleaned['perfil_usuario'] = profile
        return cleaned


class ServiceAccessGrantForm(forms.Form):
    usuario = forms.ModelChoiceField(queryset=app_models.Usuario.objects.none(), label='Agregar usuario')
    nivel = forms.ChoiceField(
        label='Permiso',
        choices=(('view', 'Ver'), ('edit', 'Editar')),
        initial='view',
    )

    def __init__(self, *args, service=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.service = service
        self.fields['usuario'].queryset = get_users_for_service_access(service) if service else app_models.Usuario.objects.none()
        self.fields['usuario'].empty_label = 'Selecciona un usuario'
        self.fields['usuario'].help_text = 'Solo los usuarios agregados aquí podrán ver el servicio. Los de nivel editar también podrán modificarlo.'
        self.fields['nivel'].help_text = 'Ver: acceso de lectura. Editar: acceso de lectura, registro y administración del servicio.'
        for _, field in self.fields.items():
            existing = field.widget.attrs.get('class', '')
            field.widget.attrs['class'] = f'{existing} input-control'.strip()


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
        self.fields['equipo'].label_from_instance = lambda obj: f"{obj.tag_equipo} - {obj.nombre_equipo}"
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

class ServicioForm(BaseModelForm):
    metodologias = forms.ModelMultipleChoiceField(
        queryset=Metodologia.objects.all().order_by('nombre'),
        required=False,
        widget=forms.SelectMultiple(attrs={'class': 'input-control'}),
        label='Metodologías',
    )

    class Meta:
        model = Servicio
        fields = '__all__'
        exclude = ['metodologias']
        widgets = {
            'codigo_servicio': forms.TextInput(attrs={'class': 'input-control'}),
            'descripcion': forms.Textarea(attrs={'class': 'input-textarea', 'rows': 3}),
            'fecha_inicio': forms.DateInput(attrs={'class': 'input-control', 'type': 'date'}),
            'fecha_fin': forms.DateInput(attrs={'class': 'input-control', 'type': 'date'}),
            'status': forms.Select(attrs={'class': 'input-control'}),
            'creado_en': forms.DateTimeInput(
                attrs={
                    'class': 'input-control',
                    'type': 'datetime-local',
                    'readonly': 'readonly',
                },
                format='%Y-%m-%dT%H:%M',
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields['creado_en'].input_formats = ['%Y-%m-%dT%H:%M']

        if self.instance and self.instance.pk:
            self.fields['metodologias'].initial = self.instance.metodologias.all()
        else:
            self.fields['creado_en'].initial = timezone.localtime().replace(
                second=0,
                microsecond=0,
            )

    def save(self, commit=True):
        instance = super().save(commit=commit)

        if commit:
            ServicioMetodologia.objects.filter(servicio=instance).delete()

            for metodologia in self.cleaned_data.get('metodologias', []):
                ServicioMetodologia.objects.create(
                    servicio=instance,
                    metodologia=metodologia,
                )

        return instance

def get_form_for_key(model_key: str):
    if model_key == 'usuario':
        return UsuarioSyncForm
    if model_key == 'empresa':
        return EmpresaForm

    from .registry import MODEL_REGISTRY

    config = MODEL_REGISTRY[model_key]
    if config.get('form_class'):
        return config['form_class']
    
    return build_modelform(config['model'])
