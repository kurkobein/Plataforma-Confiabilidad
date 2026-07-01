from django.db import migrations, models
import django.db.models.deletion


def seed_active_aca_matrix(apps, schema_editor):
    Servicio = apps.get_model('core', 'Servicio')
    MatrizRiesgo = apps.get_model('core', 'MatrizRiesgo')

    pending = []
    for service in Servicio.objects.exclude(estrategia_id__isnull=True).iterator(chunk_size=200):
        matrix_id = (
            MatrizRiesgo.objects
            .filter(estrategia_id=service.estrategia_id)
            .order_by('-fecha_creado', '-id')
            .values_list('id', flat=True)
            .first()
        )
        if matrix_id:
            service.matriz_aca_activa_id = matrix_id
            pending.append(service)
        if len(pending) >= 200:
            Servicio.objects.bulk_update(pending, ['matriz_aca_activa'], batch_size=200)
            pending = []
    if pending:
        Servicio.objects.bulk_update(pending, ['matriz_aca_activa'], batch_size=200)


def clear_active_aca_matrix(apps, schema_editor):
    Servicio = apps.get_model('core', 'Servicio')
    Servicio.objects.update(matriz_aca_activa=None)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0019_rename_matrix_axis_level_tables'),
    ]

    operations = [
        migrations.AddField(
            model_name='servicio',
            name='matriz_aca_activa',
            field=models.ForeignKey(
                blank=True,
                null=True,
                db_column='matriz_aca_activa_id',
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='servicios_aca_activa',
                to='core.matrizriesgo',
            ),
        ),
        migrations.RunPython(
            seed_active_aca_matrix,
            clear_active_aca_matrix,
        ),
    ]
