import hashlib
import re
import unicodedata

from django.db import migrations, models
import django.db.models.deletion


OPTION_FIELDS = ('falla_funcional', 'modo_de_falla', 'efecto')


def _normalized_key(value):
    text = unicodedata.normalize('NFKD', str(value or '').strip().casefold())
    text = ''.join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r'\s+', ' ', text)
    return hashlib.sha256(text.encode('utf-8')).hexdigest() if text else ''


def seed_rcm_field_options(apps, schema_editor):
    RCM = apps.get_model('core', 'RCM')
    RCMCampoOpcion = apps.get_model('core', 'RCMCampoOpcion')
    seen = set()
    pending = []

    records = RCM.objects.select_related('carga').iterator(chunk_size=500)
    for record in records:
        service_id = getattr(record.carga, 'servicio_id', None)
        if not service_id:
            continue
        for field_name in OPTION_FIELDS:
            value = str(getattr(record, field_name, '') or '').strip()
            key = _normalized_key(value)
            unique_key = (service_id, field_name, key)
            if not key or unique_key in seen:
                continue
            seen.add(unique_key)
            pending.append(RCMCampoOpcion(
                servicio_id=service_id,
                campo=field_name,
                valor=value,
                clave_normalizada=key,
                activo=True,
            ))
            if len(pending) >= 500:
                RCMCampoOpcion.objects.bulk_create(
                    pending,
                    batch_size=500,
                    ignore_conflicts=True,
                )
                pending = []

    if pending:
        RCMCampoOpcion.objects.bulk_create(
            pending,
            batch_size=500,
            ignore_conflicts=True,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_criticality_rule_trace'),
    ]

    operations = [
        migrations.CreateModel(
            name='RCMCampoOpcion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('campo', models.CharField(choices=[
                    ('falla_funcional', 'Falla funcional'),
                    ('modo_de_falla', 'Modo de falla'),
                    ('efecto', 'Efecto'),
                ], max_length=32)),
                ('valor', models.TextField()),
                ('clave_normalizada', models.CharField(max_length=64)),
                ('activo', models.BooleanField(default=True)),
                ('servicio', models.ForeignKey(
                    db_column='servicio_id',
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='opciones_campos_rcm',
                    to='core.servicio',
                )),
            ],
            options={
                'db_table': 'reliability_rcmcampoopcion',
                'ordering': ['campo', 'valor'],
            },
        ),
        migrations.AddConstraint(
            model_name='rcmcampoopcion',
            constraint=models.UniqueConstraint(
                fields=('servicio', 'campo', 'clave_normalizada'),
                name='uq_rcm_opcion_servicio_campo_clave',
            ),
        ),
        migrations.RunPython(seed_rcm_field_options, migrations.RunPython.noop),
    ]
