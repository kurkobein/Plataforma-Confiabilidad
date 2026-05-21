from __future__ import annotations

from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from core import models


SUPPORTED_TYPES = {'rcm', 'aca', 'pautas'}


@dataclass
class BulkSummary:
    service: object
    record_type: str
    origin: str
    counts: dict[str, int]


class Command(BaseCommand):
    help = 'Borra masivamente registros importados/generados de forma controlada.'

    def add_arguments(self, parser):
        parser.add_argument('--service', required=True, help='ID, codigo o descripcion del servicio.')
        parser.add_argument('--type', required=True, dest='record_type', choices=sorted(SUPPORTED_TYPES), help='Tipo de borrado: rcm, aca o pautas.')
        parser.add_argument('--origin', help='Filtro opcional por origen, por ejemplo "RCM Excel: RCM.xlsx".')
        parser.add_argument('--dry-run', action='store_true', help='Muestra lo que se borraria sin eliminar datos.')
        parser.add_argument('--confirm', action='store_true', help='Ejecuta el borrado real.')
        parser.add_argument('--confirm-all-rcm', action='store_true', help='Permite borrar todos los RCM del servicio si no se entrega --origin.')
        parser.add_argument('--confirm-all-aca', action='store_true', help='Permite borrar todos los ACA del servicio si no se entrega --origin.')
        parser.add_argument('--confirm-all-pautas', action='store_true', help='Permite borrar todas las pautas del servicio si no se entrega --origin.')

    def handle(self, *args, **options):
        if not options['dry_run'] and not options['confirm']:
            raise CommandError('Operacion abortada: usa --dry-run para simular o --confirm para borrar realmente.')

        service = self.resolve_service(options['service'])
        record_type = options['record_type']
        origin = (options.get('origin') or '').strip()
        self.validate_scope(record_type, origin, options)

        with transaction.atomic():
            summary = self.build_summary(service, record_type, origin)
            self.print_summary(summary)

            if options['dry_run']:
                self.stdout.write(self.style.WARNING('No se borro nada porque es dry-run.'))
                transaction.set_rollback(True)
                return

            self.delete_records(service, record_type, origin)
            after_summary = self.build_summary(service, record_type, origin)
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('Borrado completado correctamente.'))
            self.stdout.write(self.style.NOTICE('Resumen posterior:'))
            self.print_summary(after_summary)

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
        service = qs.filter(Q(codigo_servicio__icontains=value) | Q(descripcion__icontains=value)).first()
        if service:
            return service
        raise CommandError(f'No se encontro servicio para: {value}')

    def validate_scope(self, record_type, origin, options):
        if origin:
            return
        required_flag = {
            'rcm': 'confirm_all_rcm',
            'aca': 'confirm_all_aca',
            'pautas': 'confirm_all_pautas',
        }[record_type]
        if not options.get(required_flag):
            flag = '--' + required_flag.replace('_', '-')
            raise CommandError(f'Operacion abortada: para borrar {record_type} sin --origin debes usar {flag}.')

    def build_summary(self, service, record_type, origin):
        builders = {
            'rcm': self.rcm_summary,
            'aca': self.aca_summary,
            'pautas': self.pautas_summary,
        }
        return BulkSummary(
            service=service,
            record_type=record_type,
            origin=origin or '(sin filtro de origen)',
            counts=builders[record_type](service, origin),
        )

    def print_summary(self, summary):
        self.stdout.write(self.style.NOTICE(f'Servicio: {summary.service}'))
        self.stdout.write(self.style.NOTICE(f'Tipo: {summary.record_type}'))
        self.stdout.write(self.style.NOTICE(f'Origen: {summary.origin}'))
        for label, value in summary.counts.items():
            self.stdout.write(self.style.NOTICE(f'{label}: {value}'))

    def rcm_cargas_qs(self, service, origin):
        qs = models.Carga.objects.filter(servicio=service, rcm__isnull=False)
        if origin:
            qs = qs.filter(origen=origin)
        return qs

    def rcm_ids(self, service, origin):
        carga_ids = list(self.rcm_cargas_qs(service, origin).values_list('id', flat=True))
        rcm_ids = list(models.RCM.objects.filter(carga_id__in=carga_ids).values_list('id', flat=True))
        fmea_ids = list(models.FMEA_FMECA.objects.filter(rcm_id__in=rcm_ids).values_list('id', flat=True))
        tarea_ids = list(models.TareaRCM.objects.filter(fmea_id__in=fmea_ids).values_list('id', flat=True))
        return {
            'carga_ids': carga_ids,
            'rcm_ids': rcm_ids,
            'fmea_ids': fmea_ids,
            'tarea_ids': tarea_ids,
        }

    def rcm_summary(self, service, origin):
        ids = self.rcm_ids(service, origin)
        return {
            'Cargas encontradas': len(ids['carga_ids']),
            'RCM encontrados': len(ids['rcm_ids']),
            'FMEA encontrados': len(ids['fmea_ids']),
            'EvaluacionFMEA': models.EvaluacionFMEA.objects.filter(fmea_id__in=ids['fmea_ids']).count(),
            'TareaRCM': len(ids['tarea_ids']),
            'ValorCampoTareaRCM': models.ValorCampoTareaRCM.objects.filter(tarea_id__in=ids['tarea_ids']).count(),
        }

    def aca_cargas_qs(self, service, origin):
        qs = models.Carga.objects.filter(servicio=service, criticidades__isnull=False).distinct()
        if origin:
            qs = qs.filter(origen=origin)
        return qs

    def aca_ids(self, service, origin):
        carga_ids = list(self.aca_cargas_qs(service, origin).values_list('id', flat=True))
        criticidad_ids = list(models.Criticidad.objects.filter(aca_carga_id__in=carga_ids).values_list('id', flat=True))
        return {
            'carga_ids': carga_ids,
            'criticidad_ids': criticidad_ids,
        }

    def aca_summary(self, service, origin):
        ids = self.aca_ids(service, origin)
        return {
            'Cargas encontradas': len(ids['carga_ids']),
            'Criticidad': len(ids['criticidad_ids']),
            'CriticidadDimension': models.CriticidadDimension.objects.filter(criticidad_id__in=ids['criticidad_ids']).count(),
        }

    def pautas_qs(self, service, origin):
        qs = models.Pauta.objects.filter(servicio=service)
        if origin:
            qs = qs.filter(origen__iexact=origin)
        return qs

    def pauta_ids(self, service, origin):
        pauta_ids = list(self.pautas_qs(service, origin).values_list('id', flat=True))
        pauta_tarea_ids = list(models.PautaTarea.objects.filter(pauta_id__in=pauta_ids).values_list('id', flat=True))
        return {
            'pauta_ids': pauta_ids,
            'pauta_tarea_ids': pauta_tarea_ids,
        }

    def pautas_summary(self, service, origin):
        ids = self.pauta_ids(service, origin)
        return {
            'Pauta': len(ids['pauta_ids']),
            'PautaTarea': len(ids['pauta_tarea_ids']),
            'PlantillaPauta': 0,
        }

    def delete_records(self, service, record_type, origin):
        deleters = {
            'rcm': self.delete_rcm,
            'aca': self.delete_aca,
            'pautas': self.delete_pautas,
        }
        deleters[record_type](service, origin)

    def delete_rcm(self, service, origin):
        ids = self.rcm_ids(service, origin)
        models.ValorCampoTareaRCM.objects.filter(tarea_id__in=ids['tarea_ids']).delete()
        models.TareaRCM.objects.filter(id__in=ids['tarea_ids']).delete()
        models.EvaluacionFMEA.objects.filter(fmea_id__in=ids['fmea_ids']).delete()
        models.FMEA_FMECA.objects.filter(id__in=ids['fmea_ids']).delete()
        models.RCM.objects.filter(id__in=ids['rcm_ids']).delete()
        models.Carga.objects.filter(id__in=ids['carga_ids']).delete()

    def delete_aca(self, service, origin):
        ids = self.aca_ids(service, origin)
        models.CriticidadDimension.objects.filter(criticidad_id__in=ids['criticidad_ids']).delete()
        models.Criticidad.objects.filter(id__in=ids['criticidad_ids']).delete()
        models.Carga.objects.filter(id__in=ids['carga_ids']).delete()

    def delete_pautas(self, service, origin):
        ids = self.pauta_ids(service, origin)
        models.PautaTarea.objects.filter(id__in=ids['pauta_tarea_ids']).delete()
        models.Pauta.objects.filter(id__in=ids['pauta_ids']).delete()
