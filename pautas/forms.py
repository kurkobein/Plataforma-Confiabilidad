import json

from django import forms
from django.db.models import Q

from core import models as app_models


class PautaBaseModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for _, field in self.fields.items():
            widget = field.widget
            css = 'input-textarea' if isinstance(widget, forms.Textarea) else 'input-control'
            existing = widget.attrs.get('class', '')
            widget.attrs['class'] = f'{existing} {css}'.strip()
            widget.attrs.setdefault('placeholder', field.label)


class PlantillaPautaForm(PautaBaseModelForm):
    class Meta:
        model = app_models.PlantillaPauta
        fields = ['nombre', 'archivo', 'activa']
        widgets = {
            'archivo': forms.FileInput(attrs={'accept': '.xlsx,.xlsm'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields['archivo'].required = False

    def clean_archivo(self):
        archivo = self.cleaned_data.get('archivo')
        if not archivo:
            return archivo
        name = (archivo.name or '').lower()
        if not name.endswith(('.xlsx', '.xlsm')):
            raise forms.ValidationError('Solo se permiten archivos Excel .xlsx o .xlsm.')
        return archivo


class MapeoPlantillaPautaForm(PautaBaseModelForm):
    config_json = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = app_models.MapeoPlantillaPauta
        fields = ['hoja_principal']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        config = self.instance.config if self.instance and self.instance.pk else {}
        self.fields['config_json'].initial = json.dumps(config or {}, ensure_ascii=False)

    def clean_config_json(self):
        raw = self.cleaned_data.get('config_json') or '{}'
        try:
            config = json.loads(raw)
        except json.JSONDecodeError:
            raise forms.ValidationError('El mapeo no tiene un JSON valido.')
        if not isinstance(config, dict):
            raise forms.ValidationError('El mapeo debe ser un objeto JSON.')
        config.setdefault('celdas', [])
        config.setdefault('tablas', [])
        return config

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.config = self.cleaned_data.get('config_json') or {}
        if commit:
            instance.save()
        return instance


class GenerarPautasForm(forms.Form):
    estrategia = forms.ModelChoiceField(
        queryset=app_models.Estrategia.objects.none(),
        required=False,
        label='Estrategia',
    )
    plantilla = forms.ModelChoiceField(
        queryset=app_models.PlantillaPauta.objects.none(),
        required=False,
        label='Plantilla Excel',
        empty_label='Seleccionar plantilla',
    )
    incluir_tareas_primarias = forms.BooleanField(
        required=False,
        initial=True,
        label='Incluir tareas primarias',
    )
    incluir_tareas_secundarias = forms.BooleanField(
        required=False,
        initial=False,
        label='Incluir tareas secundarias',
    )
    generar_una_pauta = forms.BooleanField(
        required=False,
        initial=False,
        label='Crear una unica pauta con los registros seleccionados',
    )
    equipo = forms.CharField(required=False, label='Equipo / UT / TAG')
    frecuencia = forms.CharField(required=False, label='Frecuencia')
    especialidad = forms.CharField(required=False, label='Especialidad / puesto de trabajo')
    estado_equipo = forms.CharField(required=False, label='Estado equipo')

    def __init__(self, *args, service=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.service = service
        if service:
            if service.estrategia_id:
                self.fields['estrategia'].queryset = app_models.Estrategia.objects.filter(
                    Q(pk=service.estrategia_id) | Q(empresa=service.empresa, activa=True)
                ).distinct().order_by('nombre')
                self.fields['estrategia'].initial = service.estrategia_id
            else:
                self.fields['estrategia'].queryset = app_models.Estrategia.objects.filter(
                    empresa=service.empresa,
                    activa=True,
                ).order_by('nombre')
            self.fields['plantilla'].queryset = app_models.PlantillaPauta.objects.filter(
                activa=True,
            ).filter(
                Q(servicio=service)
                | Q(servicio__isnull=True, empresa=service.empresa)
                | Q(servicio__isnull=True, estrategia=service.estrategia)
                | Q(servicio__isnull=True, empresa__isnull=True, estrategia__isnull=True)
            ).distinct().order_by('nombre')
        for _, field in self.fields.items():
            widget = field.widget
            if isinstance(field, forms.BooleanField):
                widget.attrs['class'] = 'input-checkbox'
            else:
                existing = widget.attrs.get('class', '')
                widget.attrs['class'] = f'{existing} input-control'.strip()


__all__ = ['GenerarPautasForm', 'MapeoPlantillaPautaForm', 'PlantillaPautaForm']
