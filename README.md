# Plataforma MindCo

## Descripción del proyecto

Plataforma web desarrollada en Django para administrar información de confiabilidad operacional. El sistema organiza empresas, servicios, ubicaciones técnicas, equipos, familias de equipos, matrices de evaluación, análisis ACA, análisis RCM/FMECA, tareas, pautas y archivos adjuntos asociados.

La aplicación funciona como una herramienta interna de gestión y análisis, con vistas tipo backoffice, formularios, tablas, carga masiva desde Excel, exportaciones y paneles de avance para seguimiento de registros.

## Objetivo del sistema

El objetivo del sistema es centralizar la información técnica usada en procesos de confiabilidad, permitiendo:

- Gestionar servicios y sus accesos.
- Estructurar ubicaciones técnicas y equipos.
- Registrar y revisar análisis ACA.
- Registrar y revisar análisis RCM/FMECA.
- Configurar dimensiones, matrices y criterios de evaluación.
- Generar y administrar pautas asociadas a tareas.
- Importar, exportar y adjuntar información de soporte.

## Tecnologías utilizadas

- Python
- Django 6.0.6
- MySQL
- HTML, CSS y JavaScript
- openpyxl para lectura y generación de archivos Excel
- Pillow para manejo de imágenes
- ReportLab para generación de documentos PDF
- mysqlclient como conector entre Django y MySQL

## Requisitos para ejecutarlo

- Python 3.12 o superior.
- MySQL instalado y accesible.
- Base de datos creada para la plataforma.
- Credenciales de conexión a MySQL.
- Entorno virtual de Python.
- Dependencias listadas en `requirements.txt`.

Variables de entorno requeridas o recomendadas:

```env
DJANGO_SECRET_KEY=clave-local-o-productiva
DJANGO_DEBUG=1
DJANGO_ALLOWED_HOSTS=*
MYSQLDATABASE=nombre_base_datos
MYSQLUSER=usuario_mysql
MYSQLPASSWORD=password_mysql
MYSQLHOST=127.0.0.1
MYSQLPORT=3306
```

El proyecto carga automáticamente un archivo `.env` ubicado en la raíz.

## Instrucciones de instalación

1. Crear y activar un entorno virtual:

```bash
python -m venv venv
venv\Scripts\activate
```

2. Instalar dependencias:

```bash
pip install -r requirements.txt
```

3. Crear el archivo `.env` en la raíz del proyecto y configurar las variables de entorno de Django y MySQL.

4. Verificar la configuración del proyecto:

```bash
python manage.py check
```

5. Aplicar migraciones si la base de datos lo requiere:

```bash
python manage.py migrate
```

6. Crear un usuario administrador si es necesario:

```bash
python manage.py createsuperuser
```

7. Ejecutar el servidor local:

```bash
python manage.py runserver
```

8. Abrir la plataforma en el navegador:

```text
http://127.0.0.1:8000/
```


## Nota de operación

Para permitir el acceso remoto, se utiliza cloudflare a traves de un tunel mediante el comando:

```bash
cloudflared tunnel --url http://localhost:8000
```
