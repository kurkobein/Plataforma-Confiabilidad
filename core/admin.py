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


class FamiliaEquipoItemInline(admin.TabularInline):
    model = models.FamiliaEquipoItem
    extra = 0
    raw_id_fields = ('equipo',)


@admin.register(models.FamiliaEquipo)
class FamiliaEquipoAdmin(admin.ModelAdmin):
    list_display = ('id', 'nombre', 'servicio', 'activa', 'creado_en', 'actualizado')
    search_fields = ('nombre', 'descripcion', 'servicio__codigo_servicio', 'servicio__descripcion')
    list_filter = ('activa', 'servicio')
    inlines = (FamiliaEquipoItemInline,)


@admin.register(models.FamiliaEquipoItem)
class FamiliaEquipoItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'familia', 'equipo', 'orden')
    search_fields = ('familia__nombre', 'equipo__tag_equipo', 'equipo__nombre_equipo', 'equipo__ut')
    list_filter = ('familia__servicio',)


@admin.register(models.EscenarioFalla)
class EscenarioFallaAdmin(admin.ModelAdmin):
    list_display = ('id', 'nombre', 'servicio', 'activo', 'creado_en', 'actualizado')
    search_fields = ('nombre', 'servicio__codigo_servicio', 'servicio__descripcion')
    list_filter = ('activo', 'servicio')


@admin.register(models.CriticidadAdjunto)
class CriticidadAdjuntoAdmin(admin.ModelAdmin):
    list_display = ('id', 'criticidad', 'nombre_original', 'usuario', 'creado_en')
    search_fields = ('nombre_original', 'criticidad__equipo__tag_equipo', 'criticidad__equipo__ut')
    list_filter = ('creado_en',)


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
