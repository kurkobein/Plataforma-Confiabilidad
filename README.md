# Mindco v1

Base Django mínima para navegar y editar la estructura del modelo de confiabilidad usando las tablas ya creadas en MySQL.

## Enfoque

Esta versión no intenta rehacer la plataforma anterior. Hace algo más simple:

- toma como base el SQL ya definido
- se conecta a las tablas existentes
- expone CRUDs genéricos
- usa una interfaz mínima, tipo backoffice
- evita lógica visual o UX compleja

## Qué incluye

- dashboard inicial con conteos
- navegación lateral por módulos
- listado con búsqueda y paginación
- detalle de registro
- crear / editar / eliminar
- registro también en Django admin

## Modelos cubiertos

- Empresa
- Metodologia
- Estrategia
- Cargo
- Usuario
- Servicio
- AccesoUsuario
- Sistema
- Equipo
- Componente
- ServicioEquipo
- ComponenteEquipo
- AcaCarga
- Criticidad
- Dimension
- EstrategiaDimension
- DimensionCatalogo
- DimensionCatalogoFila
- DimensionCatalogoColumna
- DimensionCatalogoCelda
- EscalaUnificada
- EscalaValor
- CriticidadDimension
- InicioSesion
- MatrizRiesgo
- NivelImpacto
- NivelProbabilidad
- MatrizRiesgoCelda

## Importante

Los modelos están con `managed = False` porque esta base asume que la estructura ya existe en MySQL y fue creada por tu SQL.

No debes usar `makemigrations` para intentar recrear este esquema.

## Puesta en marcha

1. Crear base de datos y ejecutar tu script SQL.
2. Crear entorno virtual.
3. Instalar dependencias.
4. Definir variables de entorno MySQL.
5. Levantar servidor Django.

## Instalación rápida

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

En Windows PowerShell:

```powershell
$env:MYSQL_DB='mindcodb11'
$env:MYSQL_USER='root'
$env:MYSQL_PASSWORD=''
$env:MYSQL_HOST='127.0.0.1'
$env:MYSQL_PORT='3306'
python manage.py runserver
```

## Nota de operación

Si una eliminación falla, normalmente será por llaves foráneas ya definidas en MySQL.



#COMANDO PARA ELIMINAR:

python manage.py delete_bulk_records --service ERB-001 --type rcm --origin "RCM Excel: RCM.xlsx" --confirm


cloudflared tunnel --url http://localhost:8000