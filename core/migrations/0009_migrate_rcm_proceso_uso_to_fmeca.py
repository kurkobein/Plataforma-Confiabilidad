from django.db import migrations


def forwards(apps, schema_editor):
    EstrategiaDimension = apps.get_model('core', 'EstrategiaDimension')
    EstrategiaDimension.objects.filter(proceso_uso='rcm').update(proceso_uso='fmeca')


def backwards(apps, schema_editor):
    EstrategiaDimension = apps.get_model('core', 'EstrategiaDimension')
    EstrategiaDimension.objects.filter(proceso_uso='fmeca').update(proceso_uso='rcm')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_alter_estrategiadimension_proceso_uso'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
