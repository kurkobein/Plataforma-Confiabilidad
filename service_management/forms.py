from django import forms

from core import models as app_models
from core.access import get_service_equipment, get_users_for_service_access


class ServiceAccessGrantForm(forms.Form):
    usuario = forms.ModelChoiceField(queryset=app_models.Usuario.objects.none(), label='Agregar usuario')
    nivel = forms.ChoiceField(
        label='Permiso',
        choices=(('view', 'Ver'), ('edit', 'Editar')),
        initial='view',
    )

    def __init__(self, *args, service=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.service = service
        self.user = user
        self.fields['usuario'].queryset = get_users_for_service_access(service, user=user) if service else app_models.Usuario.objects.none()
        self.fields['usuario'].empty_label = 'Selecciona un usuario'
        self.fields['usuario'].help_text = 'Solo los usuarios agregados aqui podran ver el servicio. Los de nivel editar tambien podran modificarlo.'
        self.fields['nivel'].help_text = 'Ver: acceso de lectura. Editar: acceso de lectura, registro y administracion del servicio.'
        for _, field in self.fields.items():
            existing = field.widget.attrs.get('class', '')
            field.widget.attrs['class'] = f'{existing} input-control'.strip()


class FamiliaEquipoForm(forms.ModelForm):
    equipos_ids = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={'id': 'family-equipment-selected-ids'}),
    )

    class Meta:
        model = app_models.FamiliaEquipo
        fields = ['nombre', 'descripcion', 'activa']
        widgets = {
            'nombre': forms.TextInput(attrs={'class': 'input-control'}),
            'descripcion': forms.Textarea(attrs={'class': 'input-textarea', 'rows': 3}),
            'activa': forms.CheckboxInput(attrs={'class': 'input-checkbox'}),
        }

    def __init__(self, *args, service=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.service = service
        if self.instance and self.instance.pk and not self.is_bound:
            selected_ids = list(
                self.instance.items.order_by('orden', 'id').values_list('equipo_id', flat=True)
            )
            self.initial['equipos_ids'] = ','.join(str(item) for item in selected_ids)

    def clean_nombre(self):
        nombre = (self.cleaned_data.get('nombre') or '').strip()
        qs = app_models.FamiliaEquipo.objects.filter(servicio=self.service, nombre__iexact=nombre)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('Ya existe una familia de equipos con ese nombre en este servicio.')
        return nombre

    def clean_equipos_ids(self):
        raw = (self.cleaned_data.get('equipos_ids') or '').strip()
        ids = []
        seen = set()
        for part in raw.replace(';', ',').split(','):
            part = part.strip()
            if not part:
                continue
            try:
                equipment_id = int(part)
            except (TypeError, ValueError):
                raise forms.ValidationError('La selección de equipos contiene un valor inválido.')
            if equipment_id not in seen:
                seen.add(equipment_id)
                ids.append(equipment_id)
        if not ids:
            raise forms.ValidationError('Selecciona al menos un equipo.')
        qs = get_service_equipment(self.service).filter(pk__in=ids) if self.service else app_models.Equipo.objects.none()
        found = {item.pk: item for item in qs}
        missing = [item for item in ids if item not in found]
        if missing:
            raise forms.ValidationError('Hay equipos seleccionados que no pertenecen al servicio.')
        self.cleaned_equipment = [found[item] for item in ids]
        return raw


__all__ = ['ServiceAccessGrantForm', 'FamiliaEquipoForm']
