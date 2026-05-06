# Generated manually after replacing Sistema with the technical location hierarchy.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_jerarquia_codigo_por_valor'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='equipo',
            name='sistema',
        ),
        migrations.DeleteModel(
            name='Sistema',
        ),
    ]
