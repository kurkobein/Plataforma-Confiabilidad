from __future__ import annotations

from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from core import models


@dataclass
class DeletePlan:
    service: object | None
    estado: str
    origen: str
    pauta_ids: list[int]
    pauta_tarea_ids: list[int]
    plantilla_ids: list[int]
    mapeo_ids: list[int]


class Command(BaseCommand):
    help = 'Borra masivamente pautas generadas de forma segura.'

    def add_arguments(self, parser):
        parser.add_argument('--service', help='ID, codigo o descripcion del servicio.')
        parser.add_argument('--estado', help='Estado de pauta: borrador, generada, revisada, aprobada, etc.')
        parser.add_argument('--origen', help='Origen de pauta: rcm, fmea, manual, etc.')
        parser.add_argument('--dry-run', action='store_true', help='Muestra lo que se borraria sin eliminar datos.')
        parser.add_argument('--confirm', action='store_true', help='Ejecuta el borrado real.')
        parser.add_argument('--all', action='store_true', dest='all_records', help='Permite borrar sin filtro de servicio/estado/origen.')
        parser.add_argument('--include-templates', action='store_true', help='Tambien borra MapeoPlantillaPauta y PlantillaPauta.')

    def handle(self, *args, **options):
        self.validate_options(options)
        service = self.resolve_service(options['service']) if options.get('service') else None

        with transaction.atomic():
            plan = self.build_plan(service, options)
            self.print_plan(plan, options, before_delete=True)

            if options['dry_run']:
                transaction.set_rollback(True)
                if not plan.pauta_ids:
                    self.stdout.write(self.style.WARNING('No se encontraron pautas con los filtros indicados.'))
                self.stdout.write(self.style.WARNING('No se borro nada porque es dry-run.'))
                return

            self.delete_from_plan(plan, options)
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('Borrado completado correctamente.'))
            self.stdout.write(self.style.NOTICE(f'- Pautas eliminadas: {len(plan.pauta_ids)}'))
            self.stdout.write(self.style.NOTICE(f'- Tareas de pauta eliminadas: {len(plan.pauta_tarea_ids)}'))
            if options['include_templates']:
                self.stdout.write(self.style.NOTICE(f'- Mapeos de plantilla eliminados: {len(plan.mapeo_ids)}'))
                self.stdout.write(self.style.NOTICE(f'- Plantillas eliminadas: {len(plan.plantilla_ids)}'))

    def validate_options(self, options):
        if not options['dry_run'] and not options['confirm']:
            raise CommandError('Debes usar --dry-run para revisar o --confirm para borrar.')
        if options['dry_run'] and options['confirm']:
            raise CommandError('Usa solo una opcion: --dry-run o --confirm.')

        has_filter = any(options.get(key) for key in ['service', 'estado', 'origen'])
        if options['confirm'] and not has_filter and not options['all_records']:
            raise CommandError('Para borrar todas las pautas sin filtros debes usar --all explicitamente.')

        if options['include_templates'] and not options.get('service') and not options['all_records']:
            raise CommandError('Para borrar plantillas sin filtro de servicio debes usar --all explicitamente.')

        self.validate_choice('estado', options.get('estado'), models.Pauta.ESTADO_CHOICES)
        self.validate_choice('origen', options.get('origen'), models.Pauta.ORIGEN_CHOICES)

    def validate_choice(self, label, value, choices):
        if not value:
            return
        valid = {choice_value for choice_value, _choice_label in choices}
        if value not in valid:
            valid_label = ', '.join(sorted(valid))
            raise CommandError(f'Valor invalido para --{label}: {value}. Valores validos: {valid_label}.')

    def resolve_service(self, value):
        qs = models.Servicio.objects.select_related('empresa', 'estrategia')
        value = (value or '').strip()
        if value.isdigit():
            service = qs.filter(pk=int(value)).first()
            if service:
                return service
        service = qs.filter(codigo_servicio__iexact=value).first()
        if service:
            return service
        service = qs.filter(descripcion__iexact=value).first()
        if service:
            return service
        service = qs.filter(Q(codigo_servicio__icontains=value) | Q(descripcion__icontains=value)).first()
        if service:
            return service
        raise CommandError(f'No se encontro servicio para: {value}')

    def build_plan(self, service, options):
        pautas = models.Pauta.objects.all()
        if service:
            pautas = pautas.filter(servicio=service)
        if options.get('estado'):
            pautas = pautas.filter(estado=options['estado'])
        if options.get('origen'):
            pautas = pautas.filter(origen=options['origen'])

        pauta_ids = list(pautas.values_list('id', flat=True))
        pauta_tarea_ids = list(
            models.PautaTarea.objects
            .filter(pauta_id__in=pauta_ids)
            .values_list('id', flat=True)
        )

        plantilla_ids = []
        mapeo_ids = []
        if options['include_templates']:
            plantilla_qs = models.PlantillaPauta.objects.all()
            if service:
                plantilla_qs = plantilla_qs.filter(servicio=service)
            plantilla_ids = list(plantilla_qs.values_list('id', flat=True))
            mapeo_ids = list(
                models.MapeoPlantillaPauta.objects
                .filter(plantilla_id__in=plantilla_ids)
                .values_list('id', flat=True)
            )

        return DeletePlan(
            service=service,
            estado=options.get('estado') or '',
            origen=options.get('origen') or '',
            pauta_ids=pauta_ids,
            pauta_tarea_ids=pauta_tarea_ids,
            plantilla_ids=plantilla_ids,
            mapeo_ids=mapeo_ids,
        )

    def print_plan(self, plan, options, before_delete=False):
        mode = 'dry-run' if options['dry_run'] else 'confirm'
        self.stdout.write(self.style.NOTICE('Borrado masivo de pautas'))
        self.stdout.write(self.style.NOTICE(f'Modo: {mode}'))
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE('Filtros:'))
        self.stdout.write(self.style.NOTICE(f'- Servicio: {plan.service or "todos"}'))
        self.stdout.write(self.style.NOTICE(f'- Estado: {plan.estado or "todos"}'))
        self.stdout.write(self.style.NOTICE(f'- Origen: {plan.origen or "todos"}'))
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE('Registros encontrados:' if before_delete else 'Registros:'))
        self.stdout.write(self.style.NOTICE(f'- Pautas: {len(plan.pauta_ids)}'))
        self.stdout.write(self.style.NOTICE(f'- PautaTarea: {len(plan.pauta_tarea_ids)}'))
        if options['include_templates']:
            self.stdout.write(self.style.NOTICE(f'- Plantillas: {len(plan.plantilla_ids)}'))
            self.stdout.write(self.style.NOTICE(f'- Mapeos de plantilla: {len(plan.mapeo_ids)}'))

    def delete_from_plan(self, plan, options):
        if plan.pauta_tarea_ids:
            models.PautaTarea.objects.filter(id__in=plan.pauta_tarea_ids).delete()
        if plan.pauta_ids:
            models.Pauta.objects.filter(id__in=plan.pauta_ids).delete()

        if options['include_templates']:
            if plan.mapeo_ids:
                models.MapeoPlantillaPauta.objects.filter(id__in=plan.mapeo_ids).delete()
            if plan.plantilla_ids:
                models.PlantillaPauta.objects.filter(id__in=plan.plantilla_ids).delete()
