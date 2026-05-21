from django.contrib import admin
from . import models
from .registry import MODEL_REGISTRY


@admin.register(models.Carga)
class CargaAdmin(admin.ModelAdmin):
    list_display = ('id', 'servicio', 'estrategia', 'usuario', 'status', 'fecha_analisis', 'version_carga', 'origen')
    search_fields = ('servicio__codigo_servicio', 'estrategia__nombre', 'usuario__nombre_completo', 'origen', 'status')
    list_filter = ('status', 'servicio', 'estrategia', 'fecha_analisis', 'origen')


@admin.register(models.ValorNivelJerarquia)
class ValorNivelJerarquiaAdmin(admin.ModelAdmin):
    list_display = ('id', 'empresa', 'nivel', 'codigo', 'nombre', 'activo')
    search_fields = ('empresa__nombre', 'empresa__sigla', 'nivel__nombre', 'codigo', 'nombre')
    list_filter = ('empresa', 'nivel', 'activo')


DOMAIN_ADMIN_MODEL_NAMES = {
    'RCM',
    'FMEA_FMECA',
    'EvaluacionFMEA',
    'TipoTareaEstrategia',
    'CampoTareaEstrategia',
    'TareaRCM',
    'ValorCampoTareaRCM',
    'PlantillaPauta',
    'MapeoPlantillaPauta',
    'Pauta',
    'PautaTarea',
    'ReglaGeneracionPauta',
}


for config in MODEL_REGISTRY.values():
    if config['model'].__name__ in DOMAIN_ADMIN_MODEL_NAMES:
        continue
    try:
        admin.site.register(config['model'])
    except admin.sites.AlreadyRegistered:
        pass
