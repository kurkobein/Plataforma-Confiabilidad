from django.core.management.base import BaseCommand, CommandError

from core import models
from core.services.equipment_import import (
    FORMAT_AUTO,
    FORMAT_CHOICES,
    execute_equipment_import,
    preview_equipment_import,
)


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


def resolve_service(value):
    if not value:
        return None
    qs = models.Servicio.objects.select_related('empresa')
    if str(value).isdigit():
        servicio = qs.filter(pk=value).first()
        if servicio:
            return servicio
    servicio = qs.filter(codigo_servicio__iexact=value).first() or qs.filter(descripcion__iexact=value).first()
    if servicio:
        return servicio
    servicio = qs.filter(descripcion__icontains=value).first()
    if servicio:
        return servicio
    raise CommandError(f'No se encontro servicio para "{value}".')


class Command(BaseCommand):
    help = 'Importa ubicaciones tecnicas y equipos desde Excel .xlsx creando jerarquia y relaciones ServicioEquipo.'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True, help='Ruta del archivo Excel .xlsx/.xlsm.')
        parser.add_argument('--empresa', required=True, help='ID, sigla o nombre de empresa.')
        parser.add_argument('--service', help='ID, codigo o descripcion del servicio opcional.')
        parser.add_argument('--sheet', help='Nombre de hoja opcional.')
        parser.add_argument('--format', default=FORMAT_AUTO, choices=FORMAT_CHOICES, help='auto, mindco_simple o sap_uts.')
        parser.add_argument('--last-segment-is-equipment', action='store_true', help='Mindco: ultimo segmento de UT se interpreta como equipo.')
        parser.add_argument('--last-level-is-equipment', action='store_true', default=True, help='SAP: ultimo N se interpreta como equipo.')
        parser.add_argument('--no-last-level-is-equipment', action='store_false', dest='last_level_is_equipment', help='SAP: todos los N se interpretan como nodos.')
        parser.add_argument('--dry-run', action='store_true', help='Solo muestra previsualizacion.')
        parser.add_argument('--confirm', action='store_true', help='Ejecuta carga real.')
        parser.add_argument('--limit', type=int, help='Limita filas leidas para pruebas.')

    def handle(self, *args, **options):
        if not options['dry_run'] and not options['confirm']:
            raise CommandError('Debes usar --dry-run para revisar o --confirm para cargar.')
        empresa = resolve_empresa(options['empresa'])
        servicio = resolve_service(options.get('service'))

        with open(options['file'], 'rb') as file:
            preview = preview_equipment_import(
                file,
                empresa,
                servicio=servicio,
                sheet_name=options.get('sheet'),
                format=options.get('format') or FORMAT_AUTO,
                last_segment_is_equipment=True if options.get('last_segment_is_equipment') else None,
                last_level_is_equipment=options.get('last_level_is_equipment'),
                limit=options.get('limit'),
            )

        report = preview
        if options['confirm']:
            report = execute_equipment_import(preview)

        self.stdout.write('Carga masiva de ubicaciones tecnicas/equipos')
        self.stdout.write(f'Modo: {"dry-run" if options["dry_run"] else "confirm"}')
        self.stdout.write(f'Empresa: {empresa}')
        self.stdout.write(f'Servicio: {servicio.codigo_servicio if servicio else "sin servicio"}')
        self.stdout.write(f'Formato: {report.format_detected or "-"}')
        self.stdout.write('')
        self.stdout.write(f'Filas leidas: {report.rows_read}')
        self.stdout.write(f'Filas validas: {report.valid_rows}')
        self.stdout.write(f'Filas omitidas: {report.skipped}')
        self.stdout.write(f'Niveles detectados: {", ".join(report.levels_detected) or "-"}')
        self.stdout.write(f'Nodos a crear: {report.nodes_to_create}')
        self.stdout.write(f'Nodos reutilizados: {report.nodes_reused}')
        self.stdout.write(f'Nodos creados: {report.nodes_created}')
        self.stdout.write(f'Equipos a crear: {report.equipment_to_create}')
        self.stdout.write(f'Equipos a actualizar: {report.equipment_to_update}')
        self.stdout.write(f'Equipos creados: {report.equipment_created}')
        self.stdout.write(f'Equipos actualizados: {report.equipment_updated}')
        self.stdout.write(f'ServicioEquipo a crear: {report.service_equipment_to_create}')
        self.stdout.write(f'ServicioEquipo creados: {report.service_equipment_created}')

        if report.errors:
            self.stdout.write('')
            self.stdout.write('Errores:')
            for error in report.errors[:50]:
                self.stdout.write(f'- {error}')
            if len(report.errors) > 50:
                self.stdout.write(f'- ... {len(report.errors) - 50} errores adicionales')

        if report.warnings:
            self.stdout.write('')
            self.stdout.write('Warnings:')
            for warning in report.warnings[:50]:
                self.stdout.write(f'- {warning}')
            if len(report.warnings) > 50:
                self.stdout.write(f'- ... {len(report.warnings) - 50} warnings adicionales')

        if options['dry_run']:
            self.stdout.write('')
            self.stdout.write('No se guardo nada porque es dry-run.')
        else:
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('Carga completada correctamente.'))
