# Bot de ventas para Facebook Messenger

Código fuente de ejemplo para un bot de atención/cotización por Messenger y
gestión de solicitudes de servicio. Esta copia pública no está conectada a
ninguna página ni incluye credenciales, datos de clientes, historial operativo
ni URLs de producción.

## Configuración

1. Copia `.env.example` a `.env`.
2. Completa tus propias credenciales de Meta: `PAGE_ACCESS_TOKEN`, `PAGE_ID`,
   `APP_SECRET` y un `VERIFY_TOKEN` aleatorio.
3. Define un `DASHBOARD_PASSWORD` largo y único antes de ejecutar el servicio.
   Si lo dejas vacío, el acceso al dashboard queda deshabilitado.
4. Configura `BUSINESS_WHATSAPP` y los valores comerciales para tu negocio.
5. Registra en Meta un callback HTTPS que apunte a `/webhook` y usa el mismo
   `VERIFY_TOKEN`.

No subas `.env` ni credenciales a GitHub. El archivo está excluido por
`.gitignore`; usa un gestor de secretos para despliegues.

## Ejecutar con Docker

```sh
cp .env.example .env
# Edita .env y configura los valores requeridos.
docker build -t messenger-sales-bot .
docker run --name messenger-sales-bot --env-file .env -p 8000:8000 messenger-sales-bot
```

La URL `/` sirve el health check; el webhook de Meta está en `/webhook`.
Para uso persistente en producción, configura almacenamiento duradero para la
base SQLite y HTTPS público en tu proveedor de hosting.

La lógica comercial, precios, disponibilidad y respuestas deben revisarse y
adaptarse antes de usarla con clientes reales.
