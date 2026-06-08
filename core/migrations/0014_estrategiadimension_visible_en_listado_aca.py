from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0013_criticidaddimension_comentario'),
    ]

    operations = [
        migrations.AddField(
            model_name='estrategiadimension',
            name='visible_en_listado_aca',
            field=models.BooleanField(default=True, verbose_name='Visible en listado ACA'),
        ),
    ]
