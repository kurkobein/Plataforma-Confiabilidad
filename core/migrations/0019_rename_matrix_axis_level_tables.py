from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0018_rcm_campo_opcion'),
    ]

    operations = [
        migrations.AlterModelTable(
            name='nivelprobabilidad',
            table='reliability_nivelesejex',
        ),
        migrations.AlterModelTable(
            name='nivelimpacto',
            table='reliability_nivelesejey',
        ),
    ]
