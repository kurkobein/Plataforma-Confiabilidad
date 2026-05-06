# Generated manually for technical location codes on hierarchy values.

from django.db import migrations, models


def populate_node_codes(apps, schema_editor):
    NodoJerarquia = apps.get_model('core', 'NodoJerarquia')
    for node in NodoJerarquia.objects.all():
        if getattr(node, 'codigo', None):
            continue
        raw = (node.nombre or '').strip().upper()
        code = ''
        for char in raw:
            code += char if char.isalnum() else '-'
        code = code.strip('-') or f'NODO-{node.pk}'
        node.codigo = code[:50]
        node.save(update_fields=['codigo'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_jerarquia_ubicaciones_tecnicas'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='niveljerarquia',
            unique_together={('empresa', 'orden'), ('empresa', 'nombre')},
        ),
        migrations.RemoveField(
            model_name='niveljerarquia',
            name='abreviatura',
        ),
        migrations.AddField(
            model_name='nodojerarquia',
            name='codigo',
            field=models.CharField(default='', max_length=50),
            preserve_default=False,
        ),
        migrations.RunPython(populate_node_codes, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(
            name='nodojerarquia',
            unique_together={('empresa', 'parent', 'codigo')},
        ),
        migrations.AlterModelOptions(
            name='nodojerarquia',
            options={'ordering': ['empresa__nombre', 'nivel__orden', 'orden', 'codigo', 'nombre']},
        ),
    ]
