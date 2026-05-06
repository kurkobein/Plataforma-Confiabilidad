# Generated manually for the technical location hierarchy.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='NivelJerarquia',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('nombre', models.CharField(max_length=100)),
                ('abreviatura', models.CharField(max_length=20)),
                ('orden', models.PositiveIntegerField(default=0)),
                ('activo', models.BooleanField(default=True)),
                ('empresa', models.ForeignKey(db_column='empresa_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='niveles_jerarquia', to='core.empresa')),
            ],
            options={
                'db_table': 'reliability_niveljerarquia',
                'ordering': ['empresa__nombre', 'orden', 'nombre'],
                'unique_together': {('empresa', 'orden'), ('empresa', 'nombre'), ('empresa', 'abreviatura')},
            },
        ),
        migrations.CreateModel(
            name='NodoJerarquia',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('nombre', models.CharField(max_length=200)),
                ('orden', models.PositiveIntegerField(default=0)),
                ('activo', models.BooleanField(default=True)),
                ('empresa', models.ForeignKey(db_column='empresa_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='nodos_jerarquia', to='core.empresa')),
                ('nivel', models.ForeignKey(db_column='nivel_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='nodos', to='core.niveljerarquia')),
                ('parent', models.ForeignKey(blank=True, db_column='parent_id', null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='hijos', to='core.nodojerarquia')),
            ],
            options={
                'db_table': 'reliability_nodojerarquia',
                'ordering': ['empresa__nombre', 'nivel__orden', 'orden', 'nombre'],
                'unique_together': {('empresa', 'parent', 'nombre')},
            },
        ),
        migrations.AddField(
            model_name='equipo',
            name='nodo',
            field=models.ForeignKey(blank=True, db_column='nodo_id', null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='equipos', to='core.nodojerarquia'),
        ),
    ]
