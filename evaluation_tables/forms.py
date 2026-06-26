from django import forms
from django.utils import timezone

from core import models as app_models

class MatrizBuilderForm(forms.Form):
    nombre = forms.CharField()
    fecha_creado = forms.DateField(
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date', 'readonly': 'readonly'}),
        initial=timezone.localdate,
        disabled=True,
    )
    estrategia = forms.ModelChoiceField(
        queryset=app_models.Estrategia.objects.select_related('empresa').all()
    )
    dimension_probabilidad = forms.ModelChoiceField(
        queryset=app_models.EstrategiaDimension.objects.none(),
        required=False,
        label='Dimension eje X',
        empty_label='Crear dimension automatica',
        help_text='Dimension existente que entrega el valor para el eje X.',
    )
    dimension_impacto = forms.ModelChoiceField(
        queryset=app_models.EstrategiaDimension.objects.none(),
        required=False,
        label='Dimension eje Y',
        empty_label='Crear dimension automatica',
        help_text='Dimension existente que entrega el valor para el eje Y.',
    )
    modo_resolucion = forms.ChoiceField(
        choices=app_models.MatrizRiesgo.RESOLUCION_CHOICES,
        initial=app_models.MatrizRiesgo.RESOLUCION_EXACTA,
        label='Resolucion ACA',
        help_text='Exacta mantiene el comportamiento actual. Umbral inferior usa el resultado calculado y toma la celda mas cercana por debajo.',
    )
    x_count = forms.IntegerField(min_value=2, max_value=12, initial=5, label='Columnas (eje X)')
    y_count = forms.IntegerField(min_value=2, max_value=12, initial=5, label='Filas (eje Y)')

    def __init__(self, *args, strategy=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault('fecha_creado', timezone.localdate())
        strategy_id = getattr(strategy, 'pk', strategy) if strategy is not None else None
        if self.is_bound:
            strategy_id = self.data.get(self.add_prefix('estrategia')) or strategy_id
        elif self.initial.get('estrategia'):
            strategy_id = getattr(self.initial.get('estrategia'), 'pk', self.initial.get('estrategia'))

        axis_qs = app_models.EstrategiaDimension.objects.filter(
            activo=True,
            proceso_uso__in=[
                app_models.EstrategiaDimension.PROCESO_ACA,
                app_models.EstrategiaDimension.PROCESO_AMBOS,
            ],
        ).select_related(
            'estrategia',
            'dimension',
            'catalogo',
        ).order_by(
            'estrategia__nombre',
            'orden',
            'dimension__nombre',
        )
        if strategy_id:
            axis_qs = axis_qs.filter(estrategia_id=strategy_id)
        else:
            axis_qs = app_models.EstrategiaDimension.objects.none()

        def axis_label(obj):
            try:
                catalogo = getattr(obj, 'catalogo', None)
            except app_models.DimensionCatalogo.DoesNotExist:
                catalogo = None
            return (getattr(catalogo, 'nombre', '') or obj.dimension.nombre)

        for field_name in ('dimension_probabilidad', 'dimension_impacto'):
            self.fields[field_name].queryset = axis_qs
            self.fields[field_name].label_from_instance = axis_label

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

__all__ = ['MatrizBuilderForm']
