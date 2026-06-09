from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0014_estrategiadimension_visible_en_listado_aca'),
    ]

    operations = [
        migrations.AddField(
            model_name='estrategiadimension',
            name='considerar_avance_fmeca',
            field=models.BooleanField(default=True, verbose_name='Considerar en avance FMECA'),
        ),
        migrations.AddField(
            model_name='estrategiadimension',
            name='visible_en_listado_fmeca',
            field=models.BooleanField(default=True, verbose_name='Visible en listado FMECA'),
        ),
    ]
