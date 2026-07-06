from django import forms
from django.forms import formset_factory

from core import models as app_models

class HierarchyRouteRowForm(forms.Form):
    nodo_id = forms.CharField(required=False, max_length=60, widget=forms.HiddenInput())
    nivel_id = forms.IntegerField(required=False, widget=forms.HiddenInput())
    nivel_nombre = forms.CharField(required=False, max_length=100, label='Nivel')
    codigo = forms.CharField(required=False, max_length=50, label='Código')
    nodo_nombre = forms.CharField(required=False, max_length=200, label='Nombre')

    def __init__(self, *args, **kwargs):
        initial_order = kwargs.pop('initial_order', None)
        super().__init__(*args, **kwargs)
        self.initial_order = initial_order
        for _, field in self.fields.items():
            field.widget.attrs['class'] = 'input-control'
        self.fields['nivel_nombre'].widget.attrs['readonly'] = 'readonly'

    @property
    def has_content(self):
        return any(
            bool(self.cleaned_data.get('nodo_id')) or (self.cleaned_data.get(name) or '').strip()
            for name in ('codigo', 'nodo_nombre')
        )

    def clean(self):
        cleaned = super().clean()
        field_names = ('nivel_nombre', 'codigo', 'nodo_nombre')
        for name in field_names:
            cleaned[name] = (cleaned.get(name) or '').strip()
        if cleaned.get('nodo_id'):
            return cleaned
        if not any(cleaned.get(name) for name in ('codigo', 'nodo_nombre')):
            return cleaned
        for name in ('codigo', 'nodo_nombre'):
            if not cleaned.get(name):
                self.add_error(name, 'Completa este campo para guardar el nivel.')
        cleaned['codigo'] = cleaned.get('codigo', '').upper()
        return cleaned


HierarchyRouteFormSet = formset_factory(HierarchyRouteRowForm, extra=0, can_delete=True)


class HierarchyStructureRowForm(forms.Form):
    nivel_id = forms.IntegerField(required=False, widget=forms.HiddenInput())
    nivel_nombre = forms.CharField(required=False, max_length=100, label='Nivel')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for _, field in self.fields.items():
            field.widget.attrs['class'] = 'input-control'

    @property
    def has_content(self):
        return bool((self.cleaned_data.get('nivel_nombre') or '').strip())

    def clean_nivel_nombre(self):
        return (self.cleaned_data.get('nivel_nombre') or '').strip()


HierarchyStructureFormSet = formset_factory(HierarchyStructureRowForm, extra=0, can_delete=True)


class HierarchyValueForm(forms.Form):
    nivel = forms.ModelChoiceField(
        queryset=app_models.NivelJerarquia.objects.none(),
        label='Nivel',
        empty_label='Selecciona un nivel',
    )
    parent = forms.ModelChoiceField(
        queryset=app_models.NodoJerarquia.objects.none(),
        required=False,
        label='Nodo superior',
        empty_label='Sin nodo superior',
    )
    codigo = forms.CharField(max_length=50, label='Código')
    nombre = forms.CharField(max_length=200, label='Nombre')

    def __init__(self, *args, empresa=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.empresa = empresa
        if empresa:
            self.fields['nivel'].queryset = app_models.NivelJerarquia.objects.filter(
                empresa=empresa,
                activo=True,
            ).order_by('orden')
            parent_id = None
            if self.is_bound:
                parent_id = self.data.get(self.add_prefix('parent')) or None
            elif self.initial.get('parent'):
                parent_id = getattr(self.initial.get('parent'), 'pk', self.initial.get('parent'))
            if parent_id:
                self.fields['parent'].queryset = app_models.NodoJerarquia.objects.filter(
                    empresa=empresa,
                    activo=True,
                    pk=parent_id,
                ).select_related('nivel', 'parent')
            else:
                self.fields['parent'].queryset = app_models.NodoJerarquia.objects.none()
        self.fields['parent'].label_from_instance = lambda obj: f'{obj.ut} - {obj.ruta_nombre}'
        for _, field in self.fields.items():
            field.widget.attrs['class'] = 'input-control'

    def clean_codigo(self):
        return (self.cleaned_data.get('codigo') or '').strip().upper()

    def clean_nombre(self):
        return (self.cleaned_data.get('nombre') or '').strip()

    def clean(self):
        cleaned = super().clean()
        level = cleaned.get('nivel')
        parent = cleaned.get('parent')
        if not self.empresa or not level:
            return cleaned
        if level.empresa_id != self.empresa.pk:
            self.add_error('nivel', 'El nivel debe pertenecer a la empresa seleccionada.')
        if parent and parent.empresa_id != self.empresa.pk:
            self.add_error('parent', 'El nodo superior debe pertenecer a la empresa seleccionada.')
        if level.orden == 1 and parent:
            self.add_error('parent', 'El primer nivel no debe tener nodo superior.')
        if level.orden > 1:
            if not parent:
                self.add_error('parent', 'Selecciona el nodo superior del nivel anterior.')
            elif parent.nivel.orden != level.orden - 1:
                self.add_error('parent', 'El nodo superior debe pertenecer al nivel inmediatamente anterior.')
        return cleaned


class HierarchyBulkValueForm(forms.Form):
    nivel = forms.ModelChoiceField(
        queryset=app_models.NivelJerarquia.objects.none(),
        label='Nivel',
        empty_label='Selecciona un nivel',
    )
    parent = forms.ModelChoiceField(
        queryset=app_models.NodoJerarquia.objects.none(),
        required=False,
        label='Nodo superior',
        empty_label='Sin nodo superior',
    )
    sin_nodo_superior = forms.BooleanField(
        required=False,
        initial=False,
        label='Cargar solo al nivel, sin nodo superior',
        help_text='Guarda los valores como catalogo simple del nivel y no modifica el arbol de UT existente.',
    )
    bulk_values = forms.CharField(
        label='Valores',
        widget=forms.Textarea(attrs={
            'rows': 7,
            'placeholder': 'Pega una fila por valor. Ejemplo:\nHDK\tHidrocracking\nCOK\tCoker',
        }),
        help_text='Acepta Codigo + Nombre separados por tab, punto y coma, coma o espacio. Si pegas una sola columna, se usara como nombre y se generara el codigo desde ese texto.',
    )

    def __init__(self, *args, empresa=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.empresa = empresa
        self.duplicate_rows = 0
        if empresa:
            self.fields['nivel'].queryset = app_models.NivelJerarquia.objects.filter(
                empresa=empresa,
                activo=True,
            ).order_by('orden')
            parent_id = None
            if self.is_bound:
                parent_id = self.data.get(self.add_prefix('parent')) or None
            elif self.initial.get('parent'):
                parent_id = getattr(self.initial.get('parent'), 'pk', self.initial.get('parent'))
            if parent_id:
                self.fields['parent'].queryset = app_models.NodoJerarquia.objects.filter(
                    empresa=empresa,
                    activo=True,
                    pk=parent_id,
                ).select_related('nivel', 'parent')
            else:
                self.fields['parent'].queryset = app_models.NodoJerarquia.objects.none()
        self.fields['parent'].label_from_instance = lambda obj: f'{obj.ut} - {obj.ruta_nombre}'
        for _, field in self.fields.items():
            field.widget.attrs['class'] = 'input-control'
        self.fields['sin_nodo_superior'].widget = forms.HiddenInput()

    @staticmethod
    def _split_bulk_line(line):
        line = (line or '').strip()
        for separator in ('\t', ';', '|', ','):
            if separator in line:
                left, right = line.split(separator, 1)
                return left.strip(), right.strip()
        parts = line.split(None, 1)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
        return '', line

    @staticmethod
    def _codigo_from_nombre(nombre):
        return app_models._technical_segment(nombre)[:50]

    def clean(self):
        cleaned = super().clean()
        level = cleaned.get('nivel')
        parent = cleaned.get('parent')
        simple_load = bool(cleaned.get('sin_nodo_superior'))
        if simple_load:
            cleaned['parent'] = None
            parent = None
        if self.empresa and level:
            if level.empresa_id != self.empresa.pk:
                self.add_error('nivel', 'El nivel debe pertenecer a la empresa seleccionada.')
            if parent and parent.empresa_id != self.empresa.pk:
                self.add_error('parent', 'El nodo superior debe pertenecer a la empresa seleccionada.')
            if not simple_load and level.orden == 1 and parent:
                self.add_error('parent', 'El primer nivel no debe tener nodo superior.')
            if not simple_load and level.orden > 1:
                if not parent:
                    self.add_error('parent', 'Selecciona el nodo superior del nivel anterior.')
                elif parent.nivel.orden != level.orden - 1:
                    self.add_error('parent', 'El nodo superior debe pertenecer al nivel inmediatamente anterior.')

        rows = []
        seen_codes = set()
        errors = []
        raw_values = cleaned.get('bulk_values') or ''
        for line_number, line in enumerate(raw_values.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            codigo, nombre = self._split_bulk_line(line)
            if not nombre and codigo:
                nombre = codigo
            if not codigo and nombre:
                codigo = self._codigo_from_nombre(nombre)
            codigo = (codigo or '').strip().upper()[:50]
            nombre = (nombre or '').strip()
            if not codigo or not nombre:
                errors.append(f'Fila {line_number}: completa codigo y nombre.')
                continue
            if codigo in seen_codes:
                self.duplicate_rows += 1
                continue
            seen_codes.add(codigo)
            rows.append({'codigo': codigo, 'nombre': nombre})

        if errors:
            self.add_error('bulk_values', ' '.join(errors[:5]))
        if not rows:
            self.add_error('bulk_values', 'Pega al menos una fila valida para cargar.')
        cleaned['bulk_rows'] = rows
        return cleaned


class HierarchyMoveNodeForm(forms.Form):
    parent = forms.ModelChoiceField(
        queryset=app_models.NodoJerarquia.objects.none(),
        required=False,
        label='Nodo superior',
        empty_label='Dejar como raiz',
    )

    def __init__(self, *args, node=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.node = node
        queryset = app_models.NodoJerarquia.objects.none()
        if node:
            excluded = {node.pk}
            stack = list(node.hijos.values_list('pk', flat=True))
            while stack:
                child_id = stack.pop()
                if child_id in excluded:
                    continue
                excluded.add(child_id)
                stack.extend(app_models.NodoJerarquia.objects.filter(parent_id=child_id).values_list('pk', flat=True))
            queryset = app_models.NodoJerarquia.objects.filter(
                empresa=node.empresa,
                activo=True,
            ).exclude(
                pk__in=excluded,
            ).select_related(
                'nivel',
                'parent',
            ).order_by(
                'nivel__orden',
                'orden',
                'nombre',
            )
        self.fields['parent'].queryset = queryset
        self.fields['parent'].label_from_instance = lambda obj: f'{obj.ut} - {obj.ruta_nombre}'
        self.fields['parent'].widget.attrs['class'] = 'input-control'

    def clean_parent(self):
        parent = self.cleaned_data.get('parent')
        if not parent or not self.node:
            return parent
        if parent.empresa_id != self.node.empresa_id:
            raise forms.ValidationError('El nodo superior debe pertenecer a la misma empresa.')
        return parent


class HierarchyInsertLevelForm(forms.Form):
    nivel_nombre = forms.CharField(max_length=100, label='Nombre del nivel')
    codigo = forms.CharField(max_length=50, label='Codigo del valor')
    nodo_nombre = forms.CharField(max_length=200, label='Nombre del valor')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for _, field in self.fields.items():
            field.widget.attrs['class'] = 'input-control'

    def clean_codigo(self):
        return (self.cleaned_data.get('codigo') or '').strip().upper()

    def clean_nodo_nombre(self):
        return (self.cleaned_data.get('nodo_nombre') or '').strip()

__all__ = ['HierarchyBulkValueForm', 'HierarchyInsertLevelForm', 'HierarchyMoveNodeForm', 'HierarchyRouteFormSet', 'HierarchyRouteRowForm', 'HierarchyStructureFormSet', 'HierarchyStructureRowForm', 'HierarchyValueForm']
