from django import forms

from core import models as app_models
from core.access import get_users_for_service_access


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
        self.fields['usuario'].help_text = 'Solo los usuarios agregados aqui podran ver el servicio. Los de nivel editar tambien podran modificarlo.'
        self.fields['nivel'].help_text = 'Ver: acceso de lectura. Editar: acceso de lectura, registro y administracion del servicio.'
        for _, field in self.fields.items():
            existing = field.widget.attrs.get('class', '')
            field.widget.attrs['class'] = f'{existing} input-control'.strip()


__all__ = ['ServiceAccessGrantForm']
