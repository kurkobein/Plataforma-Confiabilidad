from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_migrate_rcm_proceso_uso_to_fmeca'),
    ]

    operations = [
        migrations.AddField(
            model_name='rcm',
            name='funcion',
            field=models.TextField(blank=True, default='', verbose_name='Función'),
        ),
    ]
