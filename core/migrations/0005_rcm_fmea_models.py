# Generated manually for the RCM/FMEA/FMECA data model.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('core', '0004_remove_sistema_from_equipo'),
    ]

    operations = [
        migrations.CreateModel(
            name='CargaRCM',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('fecha_carga', models.DateTimeField(auto_now_add=True)),
                ('origen', models.TextField(blank=True, null=True)),
                ('fecha_actualizacion', models.DateField(auto_now=True)),
                ('estado', models.CharField(choices=[('borrador', 'Borrador'), ('cargado', 'Cargado'), ('validado', 'Validado'), ('error', 'Error'), ('cerrado', 'Cerrado')], default='borrador', max_length=20)),
                ('estrategia', models.ForeignKey(db_column='estrategia_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='cargas_rcm', to='core.estrategia')),
                ('servicio', models.ForeignKey(db_column='servicio_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='cargas_rcm', to='core.servicio')),
                ('usuario', models.ForeignKey(db_column='usuario_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='cargas_rcm', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'reliability_cargarrcm',
                'ordering': ['-fecha_carga', 'id'],
                'abstract': False,
                'indexes': [
                    models.Index(fields=['servicio'], name='idx_cargarrcm_servicio'),
                    models.Index(fields=['estrategia'], name='idx_cargarrcm_estrat'),
                    models.Index(fields=['usuario'], name='idx_cargarrcm_usuario'),
                    models.Index(fields=['estado'], name='idx_cargarrcm_estado'),
                    models.Index(fields=['fecha_carga'], name='idx_cargarrcm_fecha'),
                ],
            },
        ),
        migrations.CreateModel(
            name='RCM',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('criticidad', models.IntegerField(blank=True, null=True)),
                ('fecha_analisis', models.DateField()),
                ('estado', models.CharField(choices=[('borrador', 'Borrador'), ('cargado', 'Cargado'), ('validado', 'Validado'), ('error', 'Error'), ('cerrado', 'Cerrado')], default='borrador', max_length=20)),
                ('falla_funcional', models.TextField()),
                ('modo_de_falla', models.TextField()),
                ('causa', models.TextField()),
                ('efecto', models.TextField()),
                ('carga', models.OneToOneField(db_column='carga_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='rcm', to='core.cargarcm')),
                ('equipo', models.ForeignKey(db_column='equipo_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='registros_rcm', to='core.equipo')),
            ],
            options={
                'db_table': 'reliability_rcm',
                'ordering': ['-fecha_analisis', 'id'],
                'abstract': False,
                'indexes': [
                    models.Index(fields=['equipo'], name='idx_rcm_equipo'),
                    models.Index(fields=['estado'], name='idx_rcm_estado'),
                    models.Index(fields=['fecha_analisis'], name='idx_rcm_fecha'),
                    models.Index(fields=['criticidad'], name='idx_rcm_criticidad'),
                ],
            },
        ),
        migrations.CreateModel(
            name='FMEA_FMECA',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('severidad', models.IntegerField()),
                ('ocurrencia', models.IntegerField()),
                ('deteccion', models.IntegerField()),
                ('npr', models.IntegerField()),
                ('rcm', models.OneToOneField(db_column='rcm_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='fmea_fmeca', to='core.rcm')),
            ],
            options={
                'db_table': 'reliability_fmea_fmeca',
                'ordering': ['-npr', 'id'],
                'abstract': False,
                'indexes': [
                    models.Index(fields=['severidad'], name='idx_fmea_severidad'),
                    models.Index(fields=['ocurrencia'], name='idx_fmea_ocurrencia'),
                    models.Index(fields=['deteccion'], name='idx_fmea_deteccion'),
                    models.Index(fields=['npr'], name='idx_fmea_npr'),
                ],
            },
        ),
        migrations.CreateModel(
            name='EvaluacionFMEA',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('valor_numerico', models.IntegerField()),
                ('estrategia_dimension', models.ForeignKey(db_column='estrategia_dimension_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='evaluaciones_fmea', to='core.estrategiadimension')),
                ('fmea', models.ForeignKey(db_column='fmea_id', on_delete=django.db.models.deletion.DO_NOTHING, related_name='evaluaciones', to='core.fmea_fmeca')),
            ],
            options={
                'db_table': 'reliability_evaluacionfmea',
                'ordering': ['fmea_id', 'estrategia_dimension_id'],
                'abstract': False,
                'indexes': [
                    models.Index(fields=['fmea'], name='idx_evalfmea_fmea'),
                    models.Index(fields=['estrategia_dimension'], name='idx_evalfmea_edim'),
                ],
                'constraints': [
                    models.UniqueConstraint(fields=('fmea', 'estrategia_dimension'), name='uniq_fmea_dimension'),
                ],
            },
        ),
    ]
