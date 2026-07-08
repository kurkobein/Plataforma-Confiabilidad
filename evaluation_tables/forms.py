from django import forms
from django.db.models import Q
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
        required=True,
        label='Dimensión eje X',
        empty_label='Selecciona una fuente real',
        help_text='Dimensión existente de la estrategia que entrega el valor real para el eje X.',
    )
    dimension_impacto = forms.ModelChoiceField(
        queryset=app_models.EstrategiaDimension.objects.none(),
        required=True,
        label='Dimensión eje Y',
        empty_label='Selecciona una fuente real',
        help_text='Dimensión existente de la estrategia que entrega el valor real para el eje Y.',
    )
    modo_resolucion = forms.ChoiceField(
        choices=app_models.MatrizRiesgo.RESOLUCION_CHOICES,
        initial=app_models.MatrizRiesgo.RESOLUCION_EXACTA,
        label='Resolución ACA',
        help_text='Exacta mantiene el comportamiento actual. Umbral inferior usa el resultado calculado y toma la celda más cercana por debajo.',
    )
    x_count = forms.IntegerField(
        min_value=2,
        max_value=10,
        initial=5,
        label='Columnas (eje X)',
        error_messages={
            'min_value': 'La matriz debe tener al menos 2 columnas.',
            'max_value': 'La matriz admite un máximo de 10 columnas.',
        },
    )
    y_count = forms.IntegerField(
        min_value=2,
        max_value=10,
        initial=5,
        label='Filas (eje Y)',
        error_messages={
            'min_value': 'La matriz debe tener al menos 2 filas.',
            'max_value': 'La matriz admite un máximo de 10 filas.',
        },
    )

    def __init__(self, *args, strategy=None, require_axis_sources=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault('fecha_creado', timezone.localdate())
        for field_name in ('dimension_probabilidad', 'dimension_impacto'):
            self.fields[field_name].required = require_axis_sources
        strategy_id = getattr(strategy, 'pk', strategy) if strategy is not None else None
        if self.is_bound:
            strategy_id = self.data.get(self.add_prefix('estrategia')) or strategy_id
        elif self.initial.get('estrategia'):
            strategy_id = getattr(self.initial.get('estrategia'), 'pk', self.initial.get('estrategia'))

        process_values = [
            app_models.EstrategiaDimension.PROCESO_ACA,
            app_models.EstrategiaDimension.PROCESO_FMECA,
            app_models.EstrategiaDimension.PROCESO_AMBOS,
            *getattr(app_models.EstrategiaDimension, 'PROCESO_FMECA_ALIASES', ()),
        ]
        axis_qs = app_models.EstrategiaDimension.objects.filter(
            activo=True,
            proceso_uso__in=list(dict.fromkeys(process_values)),
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

        legacy_axis_ids = {
            getattr(self.initial.get(field_name), 'pk', self.initial.get(field_name))
            for field_name in ('dimension_probabilidad', 'dimension_impacto')
            if self.initial.get(field_name)
        }
        generated_axis_filter = (
            Q(dimension__nombre__istartswith='Eje X - ')
            | Q(dimension__nombre__istartswith='Eje Y - ')
            | Q(dimension__nombre__istartswith='Probabilidad - ')
            | Q(dimension__nombre__istartswith='Impacto - ')
            | Q(dimension__nombre__istartswith='Consecuencia - ')
        )
        axis_qs = axis_qs.filter(
            ~generated_axis_filter | Q(pk__in=legacy_axis_ids)
        )

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
                    'La dimensión elegida debe pertenecer a la estrategia seleccionada.',
                )
        return cleaned

__all__ = ['MatrizBuilderForm']
