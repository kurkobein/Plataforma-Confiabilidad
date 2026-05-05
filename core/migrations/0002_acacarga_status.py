from django.db import migrations, models


def mark_existing_acas_complete(apps, schema_editor):
    AcaCarga = apps.get_model('core', 'AcaCarga')
    AcaCarga.objects.update(status='Completo')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='acacarga',
            name='status',
            field=models.CharField(
                'Estado',
                choices=[
                    ('Completo', 'Completo'),
                    ('Incompleto', 'Incompleto'),
                ],
                default='Incompleto',
                max_length=20,
            ),
        ),
        migrations.RunPython(mark_existing_acas_complete, migrations.RunPython.noop),
    ]
