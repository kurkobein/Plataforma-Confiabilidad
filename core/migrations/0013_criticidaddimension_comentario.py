from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0012_alter_servicio_creado_por_usuario_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='criticidaddimension',
            name='comentario',
            field=models.TextField(blank=True, verbose_name='Comentario'),
        ),
    ]
