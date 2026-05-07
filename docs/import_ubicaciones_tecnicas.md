# Importacion de ubicaciones tecnicas

Comando:

```powershell
python manage.py import_ubicaciones_tecnicas --empresa "ENAP" --archivo "C:\ruta\UTS.xlsx"
```

Formato recomendado:

| Nivel | UT del nivel | Nombre | UT padre | UT completa | TAG | Equipo |
| --- | --- | --- | --- | --- | --- | --- |
| Empresa | E | Empresa ejemplo | | E | | |
| Area de negocio | DS | Downstream | E | E-DS | | |
| Planta | ERB | Refineria Bio Bio | E-DS | E-DS-ERB | | |
| Area | FCCU | Unidad FCCU | E-DS-ERB | E-DS-ERB-FCCU | | |
| Sistema | INST | Instrumentacion | E-DS-ERB-FCCU | E-DS-ERB-FCCU-INST | | |
| Ubicacion tecnica | 0000001FC86 | Lazo de control | E-DS-ERB-FCCU-INST | E-DS-ERB-FCCU-INST-0000001FC86 | | |
| Equipo | EQ001 | Bomba principal | E-DS-ERB-FCCU-INST-0000001FC86 | E-DS-ERB-FCCU-INST-0000001FC86-EQ001 | TAG-001 | Bomba principal |

Reglas:

- `Nivel` define el tipo de nivel, por ejemplo Empresa, Area de negocio, Planta, Area, Sistema, Ubicacion tecnica, Equipo.
- `UT del nivel` es el codigo del valor en ese nivel, no la UT completa.
- `Nombre` es la descripcion legible del valor.
- `UT padre` es opcional si el archivo viene ordenado de arriba hacia abajo, pero se recomienda incluirlo para cargas masivas.
- `UT completa` es opcional, pero si existe se usa como ruta principal y evita ambiguedades.
- `TAG` y `Equipo` son opcionales. Si `TAG` viene informado, el comando crea o actualiza el equipo y lo enlaza al nodo final.

Tambien se soporta el formato ancho del archivo `UTS.xlsx`:

| Ubicacion tecnica | Denominacion de la ubicacion tecnica | Level | Parent_UT_ID | N0 | N0_nombre | N1 | N1_nombre | ... | N6 | N6_nombre |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E-BB-B0COK-0COK-CACO-0BH1401-1 | VENTILADOR TIRO FORZADO BH-1401-1 | 6 | E-BB-B0COK-0COK-CACO-0BH1401 | E | ENAP | E-BB | REFINERIA BIO BIO | ... | E-BB-B0COK-0COK-CACO-0BH1401-1 | VENTILADOR TIRO FORZADO BH-1401-1 |

En ese formato:

- Las columnas `N0`, `N1`, ..., `N6` se interpretan como la UT acumulada de cada nivel.
- Las columnas `N0_nombre`, `N1_nombre`, ..., `N6_nombre` se usan como nombre/descripcion del nodo.
- El codigo guardado en cada nivel es el ultimo segmento de la UT acumulada. Por ejemplo, para `E-BB-B0COK`, el codigo del nivel es `B0COK`.
- Si no existe columna `TAG`, los equipos se crean automaticamente para hojas finales usando la UT completa como identificador.
- Por defecto solo se crean equipos automaticos desde nivel 5 en adelante. Puedes cambiarlo con `--equipo-min-level`.

Opciones utiles:

```powershell
python manage.py import_ubicaciones_tecnicas --empresa "ENAP" --archivo "C:\ruta\UTS.xlsx" --dry-run
python manage.py import_ubicaciones_tecnicas --empresa "ENAP" --archivo "C:\ruta\UTS.xlsx" --sheet "Hoja1"
python manage.py import_ubicaciones_tecnicas --empresa "ENAP" --archivo "C:\ruta\UTS.xlsx" --estructura "Empresa,Area de negocio,Planta,Area,Sistema,Ubicacion tecnica,Equipo"
python manage.py import_ubicaciones_tecnicas --empresa "ENAP" --archivo "C:\ruta\UTS.xlsx" --estructura "Empresa,Refineria,Planta,Unidad,Area,Sistema,Equipo" --equipo-min-level 5 --dry-run
python manage.py import_ubicaciones_tecnicas --empresa "ENAP" --archivo "C:\ruta\UTS.xlsx" --deactivate-missing
python manage.py import_ubicaciones_tecnicas --crear-plantilla "C:\ruta\plantilla_ubicaciones_tecnicas.csv"
```
