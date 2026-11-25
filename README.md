# CheckInQR

Panel web de invitados, boletos personales y de grupo, registro de asistencia y reportes. La aplicación Flutter en `flutter/` tiene un flujo local independiente.

## Desarrollo

1. Instala Python 3.12 y las dependencias de `requirements.txt`.
2. Ejecuta `python manage.py migrate`, `python manage.py createsuperuser` y `python manage.py runserver`.
3. Para probar enlaces públicos desde otro equipo, configura `PUBLIC_BASE_URL` y agrega los hosts autorizados en `DJANGO_ALLOWED_HOSTS`. En desarrollo local usa `DJANGO_DEBUG=true`.
4. Ejecuta `python manage.py test events` y `python manage.py makemigrations --check --dry-run`.

## Render y Neon

- Usa el Blueprint `render.yaml`. Configura `DATABASE_URL` como variable privada con la cadena directa de un proyecto dedicado de Neon. Render genera `SECRET_KEY`.
- Configura temporalmente `CHECKIN_ADMIN_USERNAME` y `CHECKIN_ADMIN_PASSWORD` con los datos que eligió el administrador. El arranque crea el primer administrador, aplica migraciones y publica los archivos estáticos. Retira las dos variables de inicialización tras comprobar el acceso.
- `PUBLIC_BASE_URL` toma el origen HTTPS de Render si no hay un dominio personalizado. Con ambos dominios, el equipo de puerta debe abrir el escáner desde el mismo dominio que los boletos.
- `EVENT_NAME`, `EVENT_DATE_LABEL`, `EVENT_LOCATION_NAME`, `EVENT_LOCATION_ADDRESS` y `EVENT_LOCATION_MAP_URL` permiten publicar la información del evento.
- El plan gratuito guarda los datos en Neon. El chat de texto funciona en Render y las notas de voz se limitan al alojamiento local con almacenamiento persistente. El primer acceso puede tardar tras un periodo de inactividad.

## Compartir un boleto y registrar una entrada

Desde **Invitados**, selecciona **Enviar boleto por WhatsApp** y confirma el mensaje en WhatsApp. El invitado recibe la página del boleto, que actualiza su QR mientras siga abierta. El personal inicia sesión para registrar accesos. Las vistas previas de enlaces no confirman asistencias.

Para monitoreo, `GET /healthz` devuelve solo el estado de la conexión a PostgreSQL. Los endpoints internos requieren una sesión de personal. Conserva en privado los enlaces individuales porque quien recibe el enlace puede abrir el boleto.
