from django.contrib import admin

from core import models


@admin.register(models.PlantillaPauta)
class PlantillaPautaAdmin(admin.ModelAdmin):
    list_display = ('id', 'nombre', 'empresa', 'servicio', 'estrategia', 'activa', 'actualizado_en')
    search_fields = ('nombre', 'empresa__nombre', 'servicio__codigo_servicio', 'estrategia__nombre')
    list_filter = ('activa', 'empresa', 'servicio', 'estrategia')


@admin.register(models.MapeoPlantillaPauta)
class MapeoPlantillaPautaAdmin(admin.ModelAdmin):
    list_display = ('id', 'plantilla', 'hoja_principal', 'actualizado_en')
    search_fields = ('plantilla__nombre', 'hoja_principal')


@admin.register(models.Pauta)
class PautaAdmin(admin.ModelAdmin):
    list_display = ('id', 'codigo', 'nombre', 'servicio', 'equipo', 'frecuencia', 'especialidad', 'estado')
    search_fields = ('codigo', 'nombre', 'servicio__codigo_servicio', 'equipo__ut', 'equipo__nombre_equipo')
    list_filter = ('estado', 'origen', 'servicio', 'estrategia', 'plantilla')


@admin.register(models.PautaTarea)
class PautaTareaAdmin(admin.ModelAdmin):
    list_display = ('id', 'pauta', 'orden', 'tipo_tarea', 'componente', 'frecuencia', 'pto_trabajo')
    search_fields = ('pauta__codigo', 'actividad', 'componente', 'limite_aceptable')
    list_filter = ('tipo_tarea', 'frecuencia', 'pto_trabajo')
