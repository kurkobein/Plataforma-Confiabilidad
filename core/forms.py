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
        self.fields['ut'].help_text = 'Puedes pegar la UT completa; se validara contra los valores registrados.'
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
        self.fields['nodo'].label = 'Ubicacion tecnica jerarquica'
        if self.instance and getattr(self.instance, 'nodo_id', None):
            self.fields['empresa'].initial = self.instance.nodo.empresa_id

    def clean(self):
        cleaned = super().clean()
        empresa = cleaned.get('empresa')
        nodo = cleaned.get('nodo')
        ut = (cleaned.get('ut') or '').strip()
        if empresa and nodo and nodo.empresa_id != empresa.pk:
            self.add_error('nodo', 'La ubicacion tecnica debe pertenecer a la empresa seleccionada.')

        if ut:
            if not empresa:
                self.add_error('empresa', 'Selecciona una empresa para resolver la UT.')
                return cleaned
            resolved_node = self._resolve_node_from_ut(empresa, ut)
            if resolved_node:
                self._resolved_node = resolved_node
                cleaned['nodo'] = resolved_node
        elif not nodo:
            self.add_error('ut', 'Ingresa una UT o selecciona una ubicacion tecnica.')
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
            self.add_error('ut', 'La empresa no tiene estructura de ubicacion tecnica configurada.')
            return None
        if len(codes) != len(levels):
            self.add_error(
                'ut',
                f'La UT tiene {len(codes)} componentes y la estructura de {empresa} requiere {len(levels)} niveles.',
            )
            return None

        parent = None
        resolved = None
        for index, (level, code) in enumerate(zip(levels, codes), start=1):
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
                return None
            parent = node
            resolved = node
        return resolved

    def save(self, commit=True):
        instance = super().save(commit=False)
        nodo = self._resolved_node or self.cleaned_data.get('nodo')
        if nodo:
            instance.ut = nodo.ut
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class EquipoBulkUploadForm(forms.Form):
    empresa = forms.ModelChoiceField(
        queryset=app_models.Empresa.objects.order_by('nombre', 'sigla'),
        label='Empresa',
        empty_label='Selecciona una empresa',
    )
    archivo = forms.FileField(
        label='Archivo Excel',
        help_text='Columnas esperadas: TAG, Nombre, UT y Descripcion. La descripcion puede quedar vacia.',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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


class ServicioForm(BaseModelForm):
    metodologias = forms.ModelMultipleChoiceField(
        queryset=Metodologia.objects.all().order_by('nombre'),
        required=False,
        widget=forms.SelectMultiple(attrs={'class': 'input-control'}),
        label='Metodologías',
    )

    def __init__(self, *args, creador_usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.creador_usuario = creador_usuario
        self.fields['creado_en'].input_formats = ['%Y-%m-%dT%H:%M']

        if self.instance and self.instance.pk:
            self.fields['metodologias'].initial = self.instance.metodologias.all()
        else:
            self.fields['creado_en'].initial = timezone.localtime().replace(
                second=0,
                microsecond=0,
            )
            if creador_usuario:
                self.fields['creado_por_usuario'].initial = creador_usuario.pk
                self.initial['creado_por_usuario'] = creador_usuario.pk
                self.fields['creado_por_usuario'].disabled = True
                self.fields['creado_por_usuario'].help_text = 'Se asigna automáticamente al usuario de la sesión.'

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

    def save(self, commit=True):
        if not self.instance.pk and self.creador_usuario:
            self.instance.creado_por_usuario = self.creador_usuario
        instance = super().save(commit=commit)

        if commit:
            ServicioMetodologia.objects.filter(servicio=instance).delete()

            for metodologia in self.cleaned_data.get('metodologias', []):
                ServicioMetodologia.objects.create(
                    servicio=instance,
                    metodologia=metodologia,
                )

        return instance


from pautas.forms import GenerarPautasForm, MapeoPlantillaPautaForm, PlantillaPautaForm


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
