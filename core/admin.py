from django.contrib import admin
from . import models
from .registry import MODEL_REGISTRY


@admin.register(models.Carga)
class CargaAdmin(admin.ModelAdmin):
    list_display = ('id', 'servicio', 'estrategia', 'usuario', 'status', 'fecha_analisis', 'version_carga', 'origen')
    search_fields = ('servicio__codigo_servicio', 'estrategia__nombre', 'usuario__nombre_completo', 'origen', 'status')
    list_filter = ('status', 'servicio', 'estrategia', 'fecha_analisis', 'origen')


@admin.register(models.RCM)
class RCMAdmin(admin.ModelAdmin):
    list_display = ('id', 'carga', 'equipo', 'tipo_analisis', 'criticidad', 'estado', 'fecha_analisis')
    search_fields = ('equipo__tag_equipo', 'equipo__nombre_equipo', 'equipo__ut', 'falla_funcional', 'modo_de_falla', 'causa', 'efecto')
    list_filter = ('estado', 'fecha_analisis', 'criticidad')


@admin.register(models.FMEA_FMECA)
class FMEAFMECAAdmin(admin.ModelAdmin):
    list_display = ('id', 'rcm', 'severidad', 'ocurrencia', 'deteccion', 'npr')
    search_fields = ('rcm__equipo__tag_equipo', 'rcm__equipo__nombre_equipo', 'rcm__equipo__ut')
    list_filter = ('severidad', 'ocurrencia', 'deteccion', 'npr')


@admin.register(models.EvaluacionFMEA)
class EvaluacionFMEAAdmin(admin.ModelAdmin):
    list_display = ('id', 'fmea', 'estrategia_dimension', 'valor_numerico')
    search_fields = ('estrategia_dimension__dimension__nombre', 'fmea__rcm__equipo__tag_equipo', 'fmea__rcm__equipo__ut')
    list_filter = ('estrategia_dimension',)


for config in MODEL_REGISTRY.values():
    try:
        admin.site.register(config['model'])
    except admin.sites.AlreadyRegistered:
        pass
