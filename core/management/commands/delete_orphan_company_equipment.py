from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core import models
from core.views import _delete_equipment_related_data, _delete_nodes_by_depth, _delete_by_materialized_ids


class Command(BaseCommand):
    help = 'Elimina equipos asociados a empresas o nodos de UT que ya no existen.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Muestra lo que se eliminaria sin borrar nada.')
        parser.add_argument('--confirm', action='store_true', help='Ejecuta el borrado real.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        confirm = options['confirm']
        if dry_run == confirm:
            raise CommandError('Debes usar exactamente una opcion: --dry-run o --confirm.')

        valid_empresa_ids = set(models.Empresa.objects.values_list('id', flat=True))
        valid_node_ids = set(models.NodoJerarquia.objects.values_list('id', flat=True))
        node_empresa_ids = set(
            models.NodoJerarquia.objects
            .exclude(empresa_id__isnull=True)
            .values_list('empresa_id', flat=True)
        )
        orphan_empresa_ids = sorted(node_empresa_ids - valid_empresa_ids)
        orphan_node_ids = list(
            models.NodoJerarquia.objects
            .filter(empresa_id__in=orphan_empresa_ids)
            .values_list('id', flat=True)
        )
        equipment_ids = set(
            models.Equipo.objects
            .filter(nodo_id__in=orphan_node_ids)
            .values_list('id', flat=True)
        )
        equipment_ids.update(
            models.Equipo.objects
            .exclude(nodo_id__isnull=True)
            .exclude(nodo_id__in=valid_node_ids)
            .values_list('id', flat=True)
        )
        orphan_level_ids = list(
            models.NivelJerarquia.objects
            .filter(empresa_id__in=orphan_empresa_ids)
            .values_list('id', flat=True)
        )
        orphan_value_ids = list(
            models.ValorNivelJerarquia.objects
            .filter(empresa_id__in=orphan_empresa_ids)
            .values_list('id', flat=True)
        )

        self.stdout.write('Limpieza de equipos huerfanos por empresa')
        self.stdout.write(f'Modo: {"dry-run" if dry_run else "confirm"}')
        self.stdout.write(f'Empresas inexistentes referenciadas: {len(orphan_empresa_ids)}')
        if orphan_empresa_ids:
            self.stdout.write(f'IDs empresa huerfanos: {", ".join(str(item) for item in orphan_empresa_ids)}')
        self.stdout.write(f'Equipos a eliminar: {len(equipment_ids)}')
        self.stdout.write(f'Nodos UT huerfanos a eliminar: {len(orphan_node_ids)}')
        self.stdout.write(f'Niveles UT huerfanos a eliminar: {len(orphan_level_ids)}')
        self.stdout.write(f'Valores UT huerfanos a eliminar: {len(orphan_value_ids)}')

        if dry_run:
            self.stdout.write(self.style.WARNING('No se elimino nada porque es dry-run.'))
            return

        with transaction.atomic():
            counts = _delete_equipment_related_data(equipment_ids)
            counts['valores_nivel_jerarquia_huerfanos'] = _delete_by_materialized_ids(
                models.ValorNivelJerarquia,
                orphan_value_ids,
            )
            counts['nodos_jerarquia_huerfanos'] = _delete_nodes_by_depth(orphan_node_ids)
            counts['niveles_jerarquia_huerfanos'] = _delete_by_materialized_ids(
                models.NivelJerarquia,
                orphan_level_ids,
            )

        self.stdout.write(self.style.SUCCESS('Limpieza completada.'))
        for key, count in counts.items():
            if count:
                self.stdout.write(f'- {key}: {count}')
