from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0016_remove_serviciometodologia_metodologia_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='criticidad',
            name='trazabilidad_criticidad_json',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='rcm',
            name='trazabilidad_criticidad_json',
            field=models.TextField(blank=True, default=''),
        ),
    ]
