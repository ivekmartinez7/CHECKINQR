#!/bin/bash

# Esperar a que PostgreSQL esté listo
if [ "$DB_ENGINE" = "postgresql" ]
then
    echo "Esperando a PostgreSQL..."
    while ! nc -z $DB_HOST $DB_PORT; do
      sleep 0.1
    done
    echo "PostgreSQL iniciado"
fi

# Aplicar migraciones
echo "Aplicando migraciones..."
python manage.py migrate

# Cargar respaldo si existe y no se ha cargado antes
if [ -f "datadump.json" ] && [ ! -f ".data_loaded" ]; then
    echo "Cargando datos de respaldo (datadump.json)..."
    python manage.py loaddata datadump.json
    touch .data_loaded
    echo "Datos cargados exitosamente."
fi

# Iniciar Gunicorn
echo "Iniciando Gunicorn..."
exec gunicorn checkin_project.wsgi:application --bind 0.0.0.0:8000 --workers 3
