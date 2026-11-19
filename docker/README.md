# 🐳 Monitor de Precios en Docker (Edición NAS / Local)

Esta versión en contenedor Docker te permite ejecutar el monitor de precios de forma continua en tu **NAS (Synology, QNAP, TrueNAS, unRAID) o servidor local**, aprovechando tu **conexión a Internet doméstica (IP residencial)**.

---

## 🌟 ¿Por qué ejecutarlo en tu NAS en lugar de GitHub Actions?

1. **Sin bloqueos de Amazon, Wallapop ni Vinted**: GitHub Actions se ejecuta en centros de datos de Azure cuyas IPs son bloqueadas habitualmente por sistemas anti-bot (CAPTCHAs de Amazon o errores 403 de Cloudflare). Desde tu NAS, al tener una IP residencial española/europea, las consultas se realizan con total normalidad.
2. **Acceso directo en tiempo real**: Al no haber bloqueo de IP, el script verifica precios reales y comprueba la disponibilidad exacta en directo.
3. **Persistencia total**: Tanto la lista de productos (`config.json`), como las ofertas notificadas (`monitor_state.json`) y el registro de precios (`historico.csv`) se guardan directamente en el disco de tu NAS.

---

## 📁 Archivos incluidos en esta carpeta (`docker/`)

| Archivo | Descripción |
| :--- | :--- |
| `Dockerfile` | Define la imagen ligera de Python 3.11 con `curl_cffi` y dependencias |
| `docker-compose.yml` | Orquesta el servicio, volúmenes persistentes y variables de entorno |
| `entrypoint.sh` | Controla el bucle de ejecución periódica según los minutos configurados |
| `.env.example` | Plantilla de configuración para tus tokens y parámetros |

---

## 🚀 Guía de Despliegue Paso a Paso

### Paso 1: Crear la carpeta en tu NAS
En la carpeta donde guardes tus contenedores (por ejemplo, `/volume1/docker/price-monitor/` en Synology):

1. Crea una carpeta llamada `price-monitor` (o el nombre que prefieras).
2. Dentro de esa carpeta, coloca únicamente:
   - **`docker-compose.yml`**
   - **`config.json`** (tu lista de productos a rastrear)

---

### Paso 2: Inyectar tus credenciales en `docker-compose.yml`

Abre `docker-compose.yml` con el editor de texto de tu NAS o cualquier editor y pega tu Token y Chat ID directamente en la sección `environment`:

```yaml
    environment:
      # Pega aquí tus credenciales directamente:
      - TELEGRAM_BOT_TOKEN=926980000:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxqdnY
      - TELEGRAM_CHAT_ID=-5594587929

      # Frecuencia en minutos entre comprobaciones (ej. 30 o 60 min):
      - CHECK_INTERVAL_MINUTES=60
      - RUN_ON_STARTUP=true
      - MODO_RESIDENCIAL=true
      - TZ=Europe/Madrid
```

> **💡 ¡No necesitas archivos `.env` ni configurar variables en el panel del NAS!**
> Al inyectarlas directamente en el archivo `docker-compose.yml`, Docker las lee y las aplica automáticamente, dejando tu `config.json` completamente limpio de claves y seguro para git.

> **Parámetros destacados:**
> - `CHECK_INTERVAL_MINUTES`: Minutos entre cada comprobación automática (por defecto: `60`).
> - `MODO_RESIDENCIAL`: Si está en `true`, activa las consultas directas a Amazon y tiendas de segunda mano al usar tu IP doméstica.

### Paso 3: Arrancar el contenedor

#### Opción A: Mediante terminal / SSH
Dentro de la carpeta donde están `docker-compose.yml` y `.env`:

```bash
# Iniciar en segundo plano
docker compose up -d

# Ver los logs en tiempo real
docker compose logs -f
```

#### Opción B: Synology Container Manager (DSM 7.2+)
1. Abre **Container Manager** en tu Synology.
2. Ve a la pestaña **Proyecto (Project)** y pulsa **Crear**.
3. Asigna un nombre al proyecto (ej. `price-monitor`).
4. Selecciona la ruta en tu NAS donde guardaste los archivos.
5. Selecciona la opción *Crear proyecto a partir de docker-compose.yml* existente.
6. Pulsa **Siguiente** y confirma. ¡El contenedor arrancará automáticamente!

#### Opción C: Portainer
1. Abre tu panel de Portainer.
2. Ve a **Stacks** -> **Add stack**.
3. Pega el contenido de `docker-compose.yml`.
4. En la sección **Environment variables**, añade las variables de `.env`.
5. Pulsa **Deploy the stack**.

---

## 💾 Persistencia de Datos

El contenedor mapea automáticamente estos tres archivos en tu NAS:

- **`config.json`**: Puedes editar tus productos en cualquier momento; el contenedor leerá los cambios en la siguiente comprobación sin necesidad de reiniciar.
- **`monitor_state.json`**: Guarda los precios ya notificados para no repetir alertas a Telegram si el precio no ha bajado.
- **`historico.csv`**: Almacena el histórico completo de ofertas detectadas en formato CSV para que puedas abrirlo en Excel o Google Sheets.

---

## 🛠️ Comandos útiles

```bash
# Ver estado del contenedor
docker compose ps

# Ver registros de comprobación en tiempo real
docker compose logs -f --tail=100

# Detener el contenedor
docker compose down

# Forzar una comprobación manual reiniciando el servicio
docker compose restart
```
