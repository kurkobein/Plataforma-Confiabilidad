from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from core import models


def resolve_empresa(value):
    if not value:
        raise CommandError('Debes indicar --empresa.')
    qs = models.Empresa.objects.all()
    if str(value).isdigit():
        empresa = qs.filter(pk=value).first()
        if empresa:
            return empresa
    empresa = qs.filter(sigla__iexact=value).first() or qs.filter(nombre__iexact=value).first()
    if empresa:
        return empresa
    empresa = qs.filter(nombre__icontains=value).first()
    if empresa:
        return empresa
    raise CommandError(f'No se encontro empresa para "{value}".')


def build_summary(empresa):
    node_ids = list(models.NodoJerarquia.objects.filter(empresa=empresa).values_list('id', flat=True))
    level_ids = list(models.NivelJerarquia.objects.filter(empresa=empresa).values_list('id', flat=True))
    value_ids = list(models.ValorNivelJerarquia.objects.filter(empresa=empresa).values_list('id', flat=True))
    equipment_ids = list(models.Equipo.objects.filter(nodo_id__in=node_ids).values_list('id', flat=True))
    criticidad_ids = list(models.Criticidad.objects.filter(equipo_id__in=equipment_ids).values_list('id', flat=True))
    rcm_ids = list(models.RCM.objects.filter(equipo_id__in=equipment_ids).values_list('id', flat=True))
    fmea_ids = list(models.FMEA_FMECA.objects.filter(rcm_id__in=rcm_ids).values_list('id', flat=True))
    tarea_ids = list(models.TareaRCM.objects.filter(fmea_id__in=fmea_ids).values_list('id', flat=True))
    pauta_ids = list(models.Pauta.objects.filter(equipo_id__in=equipment_ids).values_list('id', flat=True))
    return {
        'node_ids': node_ids,
        'level_ids': level_ids,
        'value_ids': value_ids,
        'equipment_ids': equipment_ids,
        'criticidad_ids': criticidad_ids,
        'rcm_ids': rcm_ids,
        'fmea_ids': fmea_ids,
        'tarea_ids': tarea_ids,
        'pauta_ids': pauta_ids,
        'servicio_equipo_count': models.ServicioEquipo.objects.filter(equipo_id__in=equipment_ids).count(),
        'familia_item_count': models.FamiliaEquipoItem.objects.filter(equipo_id__in=equipment_ids).count(),
        'componente_equipo_count': models.ComponenteEquipo.objects.filter(equipo_id__in=equipment_ids).count(),
        'criticidad_dimension_count': models.CriticidadDimension.objects.filter(criticidad_id__in=criticidad_ids).count(),
        'criticidad_adjunto_count': models.CriticidadAdjunto.objects.filter(criticidad_id__in=criticidad_ids).count(),
        'rcm_adjunto_count': models.RCMAdjunto.objects.filter(rcm_id__in=rcm_ids).count(),
        'evaluacion_fmea_count': models.EvaluacionFMEA.objects.filter(fmea_id__in=fmea_ids).count(),
        'valor_campo_tarea_count': models.ValorCampoTareaRCM.objects.filter(tarea_id__in=tarea_ids).count(),
        'pauta_tarea_count': models.PautaTarea.objects.filter(pauta_id__in=pauta_ids).count(),
    }


def delete_company_data(summary):
    equipment_ids = summary['equipment_ids']
    criticidad_ids = summary['criticidad_ids']
    rcm_ids = summary['rcm_ids']
    fmea_ids = summary['fmea_ids']
    tarea_ids = summary['tarea_ids']
    pauta_ids = summary['pauta_ids']

    aca_carga_ids = list(models.Criticidad.objects.filter(id__in=criticidad_ids).values_list('aca_carga_id', flat=True))
    rcm_carga_ids = list(models.RCM.objects.filter(id__in=rcm_ids).values_list('carga_id', flat=True))

    models.PautaTarea.objects.filter(pauta_id__in=pauta_ids).delete()
    models.Pauta.objects.filter(id__in=pauta_ids).delete()

    models.ValorCampoTareaRCM.objects.filter(tarea_id__in=tarea_ids).delete()
    models.TareaRCM.objects.filter(id__in=tarea_ids).delete()
    models.EvaluacionFMEA.objects.filter(fmea_id__in=fmea_ids).delete()
    models.FMEA_FMECA.objects.filter(id__in=fmea_ids).delete()
    models.RCMAdjunto.objects.filter(rcm_id__in=rcm_ids).delete()
    models.RCM.objects.filter(id__in=rcm_ids).delete()

    models.CriticidadAdjunto.objects.filter(criticidad_id__in=criticidad_ids).delete()
    models.CriticidadDimension.objects.filter(criticidad_id__in=criticidad_ids).delete()
    models.Criticidad.objects.filter(id__in=criticidad_ids).delete()

    removable_carga_ids = []
    for carga_id in set(aca_carga_ids + rcm_carga_ids):
        if not carga_id:
            continue
        has_aca = models.Criticidad.objects.filter(aca_carga_id=carga_id).exists()
        has_rcm = models.RCM.objects.filter(carga_id=carga_id).exists()
        if not has_aca and not has_rcm:
            removable_carga_ids.append(carga_id)
    models.Carga.objects.filter(id__in=removable_carga_ids).delete()

    models.ServicioEquipo.objects.filter(equipo_id__in=equipment_ids).delete()
    models.FamiliaEquipoItem.objects.filter(equipo_id__in=equipment_ids).delete()
    models.ComponenteEquipo.objects.filter(equipo_id__in=equipment_ids).delete()
    models.Equipo.objects.filter(id__in=equipment_ids).delete()

    node_ids_by_depth = list(
        models.NodoJerarquia.objects.filter(id__in=summary['node_ids'])
        .order_by('-nivel__orden', '-id')
        .values_list('id', flat=True)
    )
    for node_id in node_ids_by_depth:
        models.NodoJerarquia.objects.filter(id=node_id).delete()
    models.ValorNivelJerarquia.objects.filter(id__in=summary['value_ids']).delete()
    models.NivelJerarquia.objects.filter(id__in=summary['level_ids']).delete()


class Command(BaseCommand):
    help = 'Elimina para pruebas toda la jerarquia UT, equipos y dependencias asociadas a una empresa.'

    def add_arguments(self, parser):
        parser.add_argument('--empresa', required=True, help='ID, sigla o nombre de empresa.')
        parser.add_argument('--dry-run', action='store_true', help='Muestra conteos sin borrar.')
        parser.add_argument('--confirm', action='store_true', help='Ejecuta el borrado real.')

    def handle(self, *args, **options):
        if not options['dry_run'] and not options['confirm']:
            raise CommandError('Debes usar --dry-run para revisar o --confirm para borrar.')

        empresa = resolve_empresa(options['empresa'])
        summary = build_summary(empresa)

        self.stdout.write(f'Empresa: {empresa}')
        self.stdout.write(f'Modo: {"dry-run" if options["dry_run"] else "confirm"}')
        self.stdout.write('')
        self.stdout.write(f'Niveles: {len(summary["level_ids"])}')
        self.stdout.write(f'Nodos: {len(summary["node_ids"])}')
        self.stdout.write(f'Valores simples: {len(summary["value_ids"])}')
        self.stdout.write(f'Equipos: {len(summary["equipment_ids"])}')
        self.stdout.write(f'ServicioEquipo: {summary["servicio_equipo_count"]}')
        self.stdout.write(f'FamiliaEquipoItem: {summary["familia_item_count"]}')
        self.stdout.write(f'ComponenteEquipo: {summary["componente_equipo_count"]}')
        self.stdout.write(f'ACA: {len(summary["criticidad_ids"])}')
        self.stdout.write(f'Dimensiones ACA: {summary["criticidad_dimension_count"]}')
        self.stdout.write(f'RCM: {len(summary["rcm_ids"])}')
        self.stdout.write(f'FMEA/FMECA: {len(summary["fmea_ids"])}')
        self.stdout.write(f'Evaluaciones FMEA: {summary["evaluacion_fmea_count"]}')
        self.stdout.write(f'Tareas RCM: {len(summary["tarea_ids"])}')
        self.stdout.write(f'Valores tarea RCM: {summary["valor_campo_tarea_count"]}')
        self.stdout.write(f'Pautas: {len(summary["pauta_ids"])}')
        self.stdout.write(f'Tareas pauta: {summary["pauta_tarea_count"]}')

        if options['dry_run']:
            self.stdout.write('')
            self.stdout.write('No se borro nada porque es dry-run.')
            return

        try:
            with transaction.atomic():
                delete_company_data(summary)
        except IntegrityError as exc:
            raise CommandError(f'No se pudo borrar por una restriccion de base de datos: {exc}') from exc

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Borrado completado correctamente.'))
