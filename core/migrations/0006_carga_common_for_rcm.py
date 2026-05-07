# Generated manually to reuse the ACA load table as the common Carga table.

import django.db.models.deletion
from decimal import Decimal

from django.db import migrations, models
from django.utils import timezone


def _disable_mysql_fk_checks(apps, schema_editor):
    if schema_editor.connection.vendor == 'mysql':
        with schema_editor.connection.cursor() as cursor:
            cursor.execute('SET FOREIGN_KEY_CHECKS=0')


def _enable_mysql_fk_checks(apps, schema_editor):
    if schema_editor.connection.vendor == 'mysql':
        with schema_editor.connection.cursor() as cursor:
            cursor.execute('SET FOREIGN_KEY_CHECKS=1')


def _move_rcm_loads_to_common_carga(apps, schema_editor):
    Carga = apps.get_model('core', 'Carga')
    CargaRCM = apps.get_model('core', 'CargaRCM')
    RCM = apps.get_model('core', 'RCM')
    Usuario = apps.get_model('core', 'Usuario')

    incomplete_states = {'borrador', 'error'}

    for old_carga in CargaRCM.objects.all().order_by('id'):
        rcm_ids = list(RCM.objects.filter(carga_id=old_carga.pk).values_list('pk', flat=True))
        if not rcm_ids:
            continue

        fecha_carga = old_carga.fecha_carga or timezone.now()
        fecha_analisis = fecha_carga.date() if hasattr(fecha_carga, 'date') else timezone.localdate()
        profile = None
        if old_carga.usuario_id:
            profile = Usuario.objects.filter(auth_user_id=old_carga.usuario_id).first()

        status = 'Incompleto' if (old_carga.estado or '').lower() in incomplete_states else 'Completo'
        origen = (old_carga.origen or 'RCM Manual')[:200]
        new_carga = Carga.objects.create(
            fecha_analisis=fecha_analisis,
            version_carga=Decimal('1.0'),
            origen=origen,
            status=status,
            creado_en=fecha_carga,
            actualizado=timezone.now(),
            estrategia_id=old_carga.estrategia_id,
            servicio_id=old_carga.servicio_id,
            usuario_id=profile.pk if profile else None,
        )
        RCM.objects.filter(pk__in=rcm_ids).update(carga_id=new_carga.pk, estado=status)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('core', '0005_rcm_fmea_models'),
    ]

    operations = [
        migrations.RenameModel(
            old_name='AcaCarga',
            new_name='Carga',
        ),
        migrations.AlterField(
            model_name='carga',
            name='estrategia',
            field=models.ForeignKey(db_column='estrategia_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='cargas', to='core.estrategia'),
        ),
        migrations.AlterField(
            model_name='carga',
            name='servicio',
            field=models.ForeignKey(db_column='servicio_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='cargas', to='core.servicio'),
        ),
        migrations.AlterField(
            model_name='carga',
            name='usuario',
            field=models.ForeignKey(blank=True, db_column='usuario_id', null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='cargas', to='core.usuario'),
        ),
        migrations.RunPython(_disable_mysql_fk_checks, migrations.RunPython.noop),
        migrations.RunPython(_move_rcm_loads_to_common_carga, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='rcm',
            name='estado',
            field=models.CharField(choices=[('Completo', 'Completo'), ('Incompleto', 'Incompleto')], default='Incompleto', max_length=20),
        ),
        migrations.AlterField(
            model_name='criticidad',
            name='aca_carga',
            field=models.ForeignKey(db_column='aca_carga_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='criticidades', to='core.carga'),
        ),
        migrations.AlterField(
            model_name='rcm',
            name='carga',
            field=models.OneToOneField(db_column='carga_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='rcm', to='core.carga'),
        ),
        migrations.DeleteModel(
            name='CargaRCM',
        ),
        migrations.RunPython(_enable_mysql_fk_checks, migrations.RunPython.noop),
    ]
