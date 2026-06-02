from django.contrib import admin

from core import models


class RCMAdjuntoInline(admin.TabularInline):
    model = models.RCMAdjunto
    extra = 0
    readonly_fields = ('creado_en',)


@admin.register(models.RCM)
class RCMAdmin(admin.ModelAdmin):
    list_display = ('id', 'carga', 'equipo', 'componente', 'funcion', 'tipo_analisis', 'criticidad', 'estado', 'fecha_analisis')
    search_fields = ('equipo__tag_equipo', 'equipo__nombre_equipo', 'equipo__ut', 'componente', 'funcion', 'falla_funcional', 'modo_de_falla', 'efecto', 'observacion')
    list_filter = ('estado', 'fecha_analisis', 'criticidad')
    inlines = (RCMAdjuntoInline,)


@admin.register(models.RCMAdjunto)
class RCMAdjuntoAdmin(admin.ModelAdmin):
    list_display = ('id', 'rcm', 'nombre_original', 'usuario', 'creado_en')
    search_fields = ('nombre_original', 'rcm__equipo__tag_equipo', 'rcm__equipo__ut')
    list_filter = ('creado_en',)


@admin.register(models.FMEA_FMECA)
class FMEAFMECAAdmin(admin.ModelAdmin):
    list_display = ('id', 'rcm')
    search_fields = ('rcm__equipo__tag_equipo', 'rcm__equipo__nombre_equipo', 'rcm__equipo__ut')


@admin.register(models.EvaluacionFMEA)
class EvaluacionFMEAAdmin(admin.ModelAdmin):
    list_display = ('id', 'fmea', 'estrategia_dimension', 'valor_numerico', 'valor_texto', 'catalogo_fila', 'escala_valor')
    search_fields = ('estrategia_dimension__dimension__nombre', 'fmea__rcm__equipo__tag_equipo', 'fmea__rcm__equipo__ut')
    list_filter = ('estrategia_dimension',)


@admin.register(models.TipoTareaEstrategia)
class TipoTareaEstrategiaAdmin(admin.ModelAdmin):
    list_display = ('id', 'estrategia', 'nombre', 'codigo', 'orden', 'activo')
    search_fields = ('estrategia__nombre', 'nombre', 'codigo')
    list_filter = ('estrategia', 'activo')
    ordering = ('estrategia', 'orden', 'nombre')


@admin.register(models.CampoTareaEstrategia)
class CampoTareaEstrategiaAdmin(admin.ModelAdmin):
    list_display = ('id', 'tipo_tarea_estrategia', 'nombre', 'clave', 'tipo_dato', 'obligatorio', 'orden', 'activo')
    search_fields = ('tipo_tarea_estrategia__nombre', 'nombre', 'clave')
    list_filter = ('activo', 'tipo_dato', 'obligatorio', 'tipo_tarea_estrategia__estrategia')
    ordering = ('tipo_tarea_estrategia', 'orden', 'nombre')


@admin.register(models.TareaRCM)
class TareaRCMAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'fmea',
        'tipo_tarea_estrategia',
        'estado',
        'frecuencia_texto',
        'plan_sap',
        'hoja_ruta',
    )
    search_fields = (
        'descripcion',
        'tactica',
        'plan_sap',
        'hoja_ruta',
        'pauta',
        'numero_sap',
        'numero_parte',
        'fmea__rcm__equipo__tag_equipo',
        'fmea__rcm__equipo__ut',
    )
    list_filter = ('estado', 'tipo_tarea_estrategia', 'especialidad')
    ordering = ('fmea', 'orden', 'id')


@admin.register(models.ValorCampoTareaRCM)
class ValorCampoTareaRCMAdmin(admin.ModelAdmin):
    list_display = ('id', 'tarea', 'campo', 'valor_display')
    search_fields = ('tarea__descripcion', 'campo__nombre', 'campo__clave', 'valor_texto')
    list_filter = ('campo__tipo_tarea_estrategia', 'campo__tipo_dato')
