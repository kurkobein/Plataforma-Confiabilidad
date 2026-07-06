from django.db import migrations


def drop_component_tables(apps, schema_editor):
    existing_tables = set(schema_editor.connection.introspection.table_names())
    for table_name in ('componenteequipo', 'componente'):
        if table_name not in existing_tables:
            continue
        schema_editor.execute(
            f'DROP TABLE {schema_editor.quote_name(table_name)}'
        )


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0002_criticidad_matriz_criticidad_matriz_celda'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(drop_component_tables),
            ],
            state_operations=[
                migrations.DeleteModel(name='ComponenteEquipo'),
                migrations.DeleteModel(name='Componente'),
            ],
        ),
    ]
