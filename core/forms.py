from decimal import Decimal, InvalidOperation
import json
import unicodedata

from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q
from django.forms import formset_factory
from django.utils import timezone

from . import models as app_models
from .access import get_service_equipment, resolve_auth_user_for_email, get_users_for_service_access
from .user_sync import split_full_name, sync_auth_user_from_profile
from .models import Empresa, Servicio, Servicio


EXAMPLE_PLACEHOLDERS = {
    'codigo_servicio': 'ABC-001',
    'descripcion': 'Servicio de mantenimiento ficticio',
    'nombre': 'Plan piloto Alfa',
    'sigla': 'ABC',
    'correo': 'usuario@empresa.cl',
    'correo_corporativo': 'usuario@empresa.cl',
    'nombre_completo': 'María Pérez',
    'tag_equipo': 'EQ-001',
    'nombre_equipo': 'Bomba auxiliar ficticia',
    'ut': 'PLT-AREA-SIS-EQ001',
    'descripcion_ut': 'Equipo auxiliar de proceso ficticio',
    'nivel_nombre': 'Área',
    'nodo_nombre': 'Planta piloto',
    'codigo': 'ABC',
    'campo': 'impacto_operacional',
    'valor': '10',
    'observacion': 'Observación breve ficticia',
}


def example_placeholder(field_name, field):
    widget = field.widget
    if isinstance(widget, (forms.HiddenInput, forms.CheckboxInput, forms.Select, forms.FileInput)):
        return None
    if isinstance(field, (forms.DateField, forms.DateTimeField)):
        return None
    normalized_name = (field_name or '').lower()
    if normalized_name in EXAMPLE_PLACEHOLDERS:
        return EXAMPLE_PLACEHOLDERS[normalized_name]
    label = str(field.label or '').strip().lower()
    if 'código' in label or 'codigo' in label:
        return 'ABC-001'
    if 'correo' in label or 'email' in label:
        return 'usuario@empresa.cl'
    if 'nombre' in label:
        return 'Elemento ficticio'
    if 'descripción' in label or 'descripcion' in label:
        return 'Descripción breve ficticia'
    if 'observación' in label or 'observacion' in label:
        return 'Observación breve ficticia'
    return None

class BaseModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
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
                placeholder = example_placeholder(field_name, field)
                if placeholder:
                    widget.attrs.setdefault('placeholder', placeholder)


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


class CargoForm(BaseModelForm):
    class Meta:
        model = app_models.Cargo
        fields = ['nombre_cargo', 'area', 'jefatura']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['jefatura'].required = False
        self.fields['jefatura'].help_text = 'Opcional.'


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


from evaluation_tables.forms import MatrizBuilderForm


from aca.forms import ACARegistroForm, CriticidadDimensionInputForm, CriticidadDimensionFormSet


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


from service_management.forms import ServiceAccessGrantForm


from technical_locations.forms import (
    HierarchyInsertLevelForm,
    HierarchyMoveNodeForm,
    HierarchyRouteFormSet,
    HierarchyRouteRowForm,
    HierarchyStructureFormSet,
    HierarchyStructureRowForm,
    HierarchyValueForm,
)


from rcm.forms import RCMRegistroForm, TareaRCMForm, RCMTaskFormSet
from aca.forms import ServicioACARegistroForm


class EquipoForm(BaseModelForm):
    empresa = forms.ModelChoiceField(
        queryset=app_models.Empresa.objects.none(),
        required=False,
        label='Empresa',
    )
    servicio = forms.ModelChoiceField(
        queryset=app_models.Servicio.objects.none(),
        required=False,
        label='Relacionar con servicio',
        help_text='Opcional. Si se selecciona, el equipo quedará disponible para registros del servicio.',
    )

    class Meta:
        model = app_models.Equipo
        fields = ['tag_equipo', 'nombre_equipo', 'empresa', 'nodo', 'ut', 'descripcion_ut']
        widgets = {
            'nodo': forms.HiddenInput(),
            'ut': forms.TextInput(attrs={'class': 'input-control'}),
            'descripcion_ut': forms.TextInput(attrs={'class': 'input-control'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._resolved_node = None
        self.fields['empresa'].queryset = app_models.Empresa.objects.order_by('nombre')
        self.fields['servicio'].queryset = app_models.Servicio.objects.select_related('empresa').order_by(
            'empresa__nombre',
            'codigo_servicio',
        )
        self.fields['ut'].help_text = 'Puedes pegar la UT completa; se validará contra los valores registrados.'
        self.fields['descripcion_ut'].required = False
        self.fields['nodo'].queryset = app_models.NodoJerarquia.objects.filter(
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
        self.fields['nodo'].required = False
        self.fields['nodo'].label = 'Ubicación técnica jerárquica'
        if self.instance and getattr(self.instance, 'nodo_id', None):
            self.fields['empresa'].initial = self.instance.nodo.empresa_id
        if self.instance and getattr(self.instance, 'pk', None):
            service_link = (
                app_models.ServicioEquipo.objects
                .filter(equipo=self.instance)
                .select_related('servicio')
                .order_by('servicio__codigo_servicio')
                .first()
            )
            if service_link:
                self.fields['servicio'].initial = service_link.servicio_id

    def clean(self):
        cleaned = super().clean()
        empresa = cleaned.get('empresa')
        servicio = cleaned.get('servicio')
        nodo = cleaned.get('nodo')
        ut = (cleaned.get('ut') or '').strip()
        raw_tag = (cleaned.get('tag_equipo') or '').strip()
        tag = app_models._technical_segment(raw_tag) if raw_tag else ''
        cleaned['descripcion_ut'] = (cleaned.get('descripcion_ut') or '').strip()
        if servicio and empresa and servicio.empresa_id != empresa.pk:
            self.add_error('servicio', 'El servicio seleccionado debe pertenecer a la empresa del equipo.')
        if empresa and nodo and nodo.empresa_id != empresa.pk:
            self.add_error('nodo', 'La ubicación técnica debe pertenecer a la empresa seleccionada.')

        if ut:
            if not empresa:
                self.add_error('empresa', 'Selecciona una empresa para resolver la UT.')
                return cleaned
            resolved_node, ut_tag = self._resolve_node_from_ut(empresa, ut)
            if resolved_node:
                self._resolved_node = resolved_node
                cleaned['nodo'] = resolved_node
                if ut_tag:
                    if tag and tag != ut_tag:
                        self.add_error(
                            'tag_equipo',
                            f'El tag del equipo debe coincidir con el último segmento de la UT ({ut_tag}).',
                        )
                    else:
                        cleaned['tag_equipo'] = ut_tag
        elif not nodo:
            self.add_error('ut', 'Ingresa una UT o selecciona una ubicación técnica.')
        raw_normalized_tag = (cleaned.get('tag_equipo') or '').strip()
        normalized_tag = app_models._technical_segment(raw_normalized_tag) if raw_normalized_tag else ''
        if normalized_tag:
            cleaned['tag_equipo'] = normalized_tag
        elif 'tag_equipo' not in self.errors:
            self.add_error('tag_equipo', 'Ingresa el tag del equipo.')
        return cleaned

    def _resolve_node_from_ut(self, empresa, ut):
        codes = [
            app_models._technical_segment(part)
            for part in ut.split('-')
            if part.strip()
        ]
        levels = list(app_models.NivelJerarquia.objects.filter(
            empresa=empresa,
            activo=True,
        ).order_by('orden'))

        if not levels:
            self.add_error('ut', 'La empresa no tiene estructura de ubicación técnica configurada.')
            return None, ''
        ut_tag = ''
        node_codes = codes
        if len(codes) == len(levels) + 1:
            ut_tag = codes[-1]
            node_codes = codes[:-1]
        elif len(codes) != len(levels):
            self.add_error(
                'ut',
                f'La UT tiene {len(codes)} componentes y la estructura de {empresa} requiere {len(levels)} niveles mas el tag del equipo.',
            )
            return None, ''

        parent = None
        resolved = None
        for index, (level, code) in enumerate(zip(levels, node_codes), start=1):
            candidates = app_models.NodoJerarquia.objects.filter(
                empresa=empresa,
                nivel=level,
                parent=parent,
                activo=True,
            )
            node = next(
                (
                    item for item in candidates
                    if app_models._technical_segment(item.codigo) == code
                ),
                None,
            )
            if not node:
                parent_label = f' bajo {parent.codigo} - {parent.nombre}' if parent else ''
                self.add_error(
                    'ut',
                    f'No existe el codigo "{code}" para el nivel {index} ({level.nombre}){parent_label}.',
                )
                return None, ''
            parent = node
            resolved = node
        return resolved, ut_tag

    def save(self, commit=True):
        instance = super().save(commit=False)
        nodo = self._resolved_node or self.cleaned_data.get('nodo')
        if nodo:
            tag = app_models._technical_segment(self.cleaned_data.get('tag_equipo') or instance.tag_equipo)
            instance.ut = '-'.join(part for part in [nodo.ut, tag] if part)
        if commit:
            instance.save()
            servicio = self.cleaned_data.get('servicio')
            if servicio:
                app_models.ServicioEquipo.objects.get_or_create(
                    servicio=servicio,
                    equipo=instance,
                )
            self.save_m2m()
        return instance


class EquipoBulkUploadForm(forms.Form):
    empresa = forms.ModelChoiceField(
        queryset=app_models.Empresa.objects.order_by('nombre', 'sigla'),
        label='Empresa',
        empty_label='Selecciona una empresa',
    )
    servicio = forms.ModelChoiceField(
        queryset=app_models.Servicio.objects.none(),
        label='Servicio',
        empty_label='Sin servicio',
        required=False,
        help_text='Opcional. Si se selecciona, vincula los equipos al servicio de la empresa elegida.',
    )
    archivo = forms.FileField(
        label='Archivo Excel',
        required=False,
        help_text='Archivo .xlsx con formato Mindco simple o SAP/UTS.',
    )
    hoja = forms.CharField(
        label='Hoja',
        required=False,
        help_text='Opcional. Si queda vacío se usará la hoja con encabezados reconocidos.',
    )
    formato = forms.ChoiceField(
        label='Formato',
        choices=(
            ('auto', 'Detectar automáticamente'),
            ('mindco_simple', 'Mindco simple'),
            ('sap_uts', 'SAP / UTS'),
        ),
        initial='auto',
    )
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        empresa_id = ''
        if self.is_bound:
            empresa_id = (self.data.get('empresa') or '').strip()
        elif self.initial.get('empresa'):
            empresa_id = str(getattr(self.initial.get('empresa'), 'pk', self.initial.get('empresa')) or '')
        if empresa_id:
            self.fields['servicio'].queryset = (
                app_models.Servicio.objects
                .select_related('empresa')
                .filter(empresa_id=empresa_id)
                .order_by('codigo_servicio')
            )
        for _, field in self.fields.items():
            field.widget.attrs['class'] = 'input-control'

    def clean_archivo(self):
        archivo = self.cleaned_data.get('archivo')
        if not archivo:
            return archivo
        name = (archivo.name or '').lower()
        if not name.endswith(('.xlsx', '.xlsm')):
            raise forms.ValidationError('Solo se permiten archivos Excel .xlsx o .xlsm.')
        return archivo

    def clean(self):
        cleaned = super().clean()
        empresa = cleaned.get('empresa')
        servicio = cleaned.get('servicio')
        if empresa and servicio and servicio.empresa_id != empresa.pk:
            self.add_error('servicio', 'El servicio seleccionado no pertenece a la empresa elegida.')
        return cleaned


class ServicioForm(BaseModelForm):


    def __init__(self, *args, creador_usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.creador_usuario = creador_usuario
        selected_company_id = None
        if self.is_bound:
            raw_company_id = self.data.get(self.add_prefix('empresa'))
            if str(raw_company_id or '').isdigit():
                selected_company_id = int(raw_company_id)
        elif self.instance and self.instance.pk:
            selected_company_id = self.instance.empresa_id
        else:
            initial_company = self.initial.get('empresa')
            selected_company_id = getattr(initial_company, 'pk', initial_company)

        current_strategy_id = (
            self.instance.estrategia_id
            if self.instance and self.instance.pk
            else None
        )
        available_strategies = app_models.Estrategia.objects.filter(activa=True)
        if current_strategy_id:
            available_strategies = app_models.Estrategia.objects.filter(
                Q(activa=True) | Q(pk=current_strategy_id)
            )
        available_strategies = available_strategies.select_related('empresa').order_by(
            'empresa__nombre',
            'nombre',
            'version',
        )
        self.strategy_options = [
            {
                'id': strategy.pk,
                'company_id': strategy.empresa_id,
                'label': str(strategy),
            }
            for strategy in available_strategies
        ]
        if selected_company_id:
            self.fields['estrategia'].queryset = available_strategies.filter(
                empresa_id=selected_company_id
            )
        else:
            self.fields['estrategia'].queryset = app_models.Estrategia.objects.none()
        self.fields['estrategia'].empty_label = (
            'Selecciona una estrategia'
            if selected_company_id
            else 'Selecciona primero una empresa'
        )
        self.fields['estrategia'].help_text = (
            'Solo se muestran estrategias activas pertenecientes a la empresa del servicio.'
        )
        self.fields['creado_en'].input_formats = ['%Y-%m-%dT%H:%M']
        self.fields['creado_por_usuario'].label = 'Administrador'
        self.fields['creado_por_usuario'].help_text = 'Usuario administrador del servicio.'
        self.fields['responsable_usuario'].label = 'Responsable'
        self.fields['responsable_usuario'].help_text = 'Usuario responsable operativo del servicio.'
        self.fields['creado_en'].initial = timezone.localtime().replace(
            second=0,
            microsecond=0,
        )
        if creador_usuario:
            self.fields['creado_por_usuario'].initial = creador_usuario.pk
            self.initial['creado_por_usuario'] = creador_usuario.pk

    def clean(self):
        cleaned = super().clean()
        company = cleaned.get('empresa')
        strategy = cleaned.get('estrategia')
        if company and strategy and strategy.empresa_id != company.pk:
            self.add_error(
                'estrategia',
                'La estrategia seleccionada no pertenece a la empresa del servicio.',
            )
        return cleaned

    class Meta:
        model = Servicio
        fields = '__all__'
        exclude = ['matriz_aca_activa']
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

    def save(self, commit=True):
        if (
            self.instance.matriz_aca_activa_id
            and not app_models.MatrizRiesgo.objects.filter(
                pk=self.instance.matriz_aca_activa_id,
                estrategia_id=self.instance.estrategia_id,
            ).exists()
        ):
            self.instance.matriz_aca_activa = None
        instance = super().save(commit=commit)

        return instance


from pautas.forms import GenerarPautasForm, MapeoPlantillaPautaForm, PlantillaPautaForm


def get_form_for_key(model_key: str):
    if model_key == 'usuario':
        return UsuarioSyncForm
    if model_key == 'empresa':
        return EmpresaForm
    if model_key == 'cargo':
        return CargoForm

    from .registry import MODEL_REGISTRY

    config = MODEL_REGISTRY[model_key]
    if config.get('form_class'):
        return config['form_class']
    
    return build_modelform(config['model'])
