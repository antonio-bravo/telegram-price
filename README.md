# Monitor Universal de Precios (España y Europa) con Alertas por Telegram

Monitor automático de precios configurable mediante `config.json` para rastrear ofertas en tiendas online de **España y de toda Europa que envíen a España** (Amazon España, Amazon Alemania, Amazon Francia, Amazon Italia, Geizhals, Idealo, PcComponentes, etc.), recibiendo alertas en Telegram cuando el precio final (**incluyendo gastos de envío a España**) cumpla tus objetivos.

---

## 🚀 Características principales

* **Rastreo en España y Europa:** Consulta automáticamente las principales tiendas y comparadores europeos con envío a España (Amazon ES, DE, FR, IT, Geizhals, Idealo, etc.).
* **Gastos de envío a España incluidos:** 
  * Detecta envíos gratis en España e internacionales.
  * Si compras en una tienda europea (ej. Amazon Alemania), aplica el coste estimado de envío internacional a España (por defecto 6.50 €) si no viene desglosado en el texto, evitando que te sorprendan portes caros al pagar.
  * Reparte los portes entre las unidades del pedido (`(precio × cantidad + envío) / cantidad`).
* **Universal y configurable:** Define cualquier producto en `config.json` (discos duros, móviles, componentes, etc.).
* **Cálculo por unidad de medida:** Soporta cálculo y alertas por €/TB, €/GB, €/kg, etc.
* **Valores opcionales (`null`):** Puedes poner `null` en cualquiera de los límites (`precio_maximo_unidad`, `precio_maximo_pedido`, `precio_maximo_por_unidad_medida`) si no deseas aplicarlo.
* **Filtro antifalsos positivos:** Descarta accesorios (cables, carcasas, fundas) y productos de segunda mano o reacondicionados.
* **Histórico y control de duplicados:** Registra cada observación en `historico.csv` (indicando tienda y bandera del país) y guarda el estado en `monitor_state.json` para no repetir alertas a menos que el precio baje.
* **Automatización en GitHub Actions:** Programado para ejecutarse periódicamente en la nube sin agotar minutos.

---

## ⚙️ Configuración (`config.json`)

El comportamiento del monitor se controla desde `config.json`:

```json
{
  "intervalo_minutos": 60,
  "pais": "España",
  "moneda": "EUR",
  "buscar_en_europa": true,
  "envio_europa_estimado": 6.50,
  "incluir_envio": true,
  "exigir_envio_confirmado": false,
  "envio_por_defecto": 4.50,
  "notificar_bajadas": true,
  "solo_nuevas_ofertas": true,
  "productos": [
    {
      "id": "wd-red-8tb",
      "nombre": "WD Red Plus 8TB",
      "busqueda": "WD80EFPX",
      "capacidad": 8,
      "unidad_medida": "TB",
      "precio_maximo_unidad": 160,
      "precio_maximo_pedido": null,
      "precio_maximo_por_unidad_medida": 20,
      "cantidad": 2,
      "activo": true
    },
    {
      "id": "wd-red-6tb",
      "nombre": "WD Red Plus 6TB",
      "busqueda": "WD60EFPX",
      "capacidad": 6,
      "unidad_medida": "TB",
      "precio_maximo_unidad": 287,
      "precio_maximo_pedido": null,
      "precio_maximo_por_unidad_medida": 48,
      "cantidad": 2,
      "activo": true
    }
  ]
}
```

### Opciones globales:
* `buscar_en_europa` (bool): Si es `true`, rastrea tiendas europeas (Amazon DE, FR, IT, Geizhals, etc.) además de España.
* `envio_europa_estimado` (float): Coste estimado de envío a España para tiendas europeas (ej: `6.50` €) cuando no aparezca expresamente en el texto.
* `incluir_envio` (bool): Si es `true`, suma siempre los gastos de envío al coste final.
* `envio_por_defecto` (float): Coste de envío nacional estimado si la tienda española no lo especifica ni es gratis (ej: `4.50` €).
* `intervalo_minutos` (int): Tiempo mínimo entre ejecuciones locales. En GitHub Actions se sincroniza con el cron del workflow.

### Opciones por producto:
* `busqueda`: Código de modelo exacto (ej. `WD60EFPX`) o términos clave.
* `precio_maximo_unidad`: Límite máximo por unidad (con envío incluido). Pon `null` si no quieres usarlo.
* `precio_maximo_pedido`: Límite máximo para el pedido completo (`precio × cantidad + envío`). Pon `null` si no te interesa.
* `precio_maximo_por_unidad_medida`: Límite en la unidad de medida (ej. `20` para 20 €/TB). Pon `null` si no lo necesitas.
* `unidad_medida`: Medida (`TB`, `GB`, `kg`, etc.).
* `capacidad`: Capacidad unitaria (ej. `8` para 8TB).
* `cantidad`: Número de unidades para el pedido (el coste de envío se suma una sola vez al total del lote).
* `activo`: `true` para monitorizar, `false` para pausar.

---

## 🤖 Configuración del Bot de Telegram

1. Crea un bot con [@BotFather](https://t.me/botfather) en Telegram y obtén tu **Bot Token**.
2. Obtén tu **Chat ID** (por ejemplo mediante [@userinfobot](https://t.me/userinfobot)).
3. **MUY IMPORTANTE:** Abre el chat con tu bot recién creado en Telegram y pulsa el botón **/start** (o envíale un primer mensaje). Si no haces esto, Telegram no permitirá que el bot te envíe mensajes (`403 Forbidden`).
4. En tu repositorio de GitHub, dirígete a:
   `Settings > Secrets and variables > Actions > New repository secret`
5. Añade dos secretos:
   * `TELEGRAM_BOT_TOKEN`: El token de BotFather.
   * `TELEGRAM_CHAT_ID`: Tu ID de chat numérico.

---

## 💻 Ejecución local (opcional)

1. Instalar dependencias:
   ```bash
   pip install -r requirements.txt
   ```

2. Definir variables y ejecutar:
   ```bash
   export TELEGRAM_BOT_TOKEN="tu_token_aqui"
   export TELEGRAM_CHAT_ID="tu_chat_id_aqui"
   python monitor.py --force
   ```
