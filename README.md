# Monitor Universal de Precios con Alertas por Telegram

Monitor automático de precios configurable mediante `config.json` para rastrear ofertas en tiendas online y recibir alertas en Telegram cuando el precio final (incluyendo gastos de envío) cumpla los objetivos configurados.

---

## 🚀 Características principales

* **Universal y configurable:** Define cualquier número de productos en `config.json` (discos duros, móviles, componentes, etc.).
* **Gastos de envío incluidos:** Calcula el coste real del pedido (`precio × cantidad + envío`) y reparte los portes entre las unidades solicitadas.
* **Filtros estrictos de envío:** Opción `exigir_envio_confirmado` para evitar falsas alertas si una tienda no informa de los gastos de envío en la web.
* **Cálculo por unidad de medida:** Soporta cálculo y alertas por €/TB, €/GB, €/kg, etc.
* **Valores opcionales (`null`):** Puedes poner `null` en cualquiera de los límites (`precio_maximo_unidad`, `precio_maximo_pedido`, `precio_maximo_por_unidad_medida`) si no deseas aplicarlo.
* **Filtro antifalsos positivos:** Descarta accesorios (cables, carcasas, fundas) y productos de segunda mano o reacondicionados.
* **Histórico y control de duplicados:** Registra cada observación en `historico.csv` y guarda el estado en `monitor_state.json` para no repetir alertas a menos que el precio baje.
* **Automatización en GitHub Actions:** Programado para ejecutarse cada hora sin agotar minutos de Actions.

---

## ⚙️ Configuración (`config.json`)

El comportamiento del monitor se controla desde `config.json`:

```json
{
  "intervalo_minutos": 60,
  "pais": "España",
  "moneda": "EUR",
  "incluir_envio": true,
  "exigir_envio_confirmado": true,
  "envio_por_defecto": null,
  "notificar_bajadas": true,
  "solo_nuevas_ofertas": true,
  "productos": [
    {
      "id": "wd-red-8tb",
      "nombre": "WD Red Plus 8TB",
      "busqueda": "WD80EFPX disco duro NAS 8TB",
      "capacidad": 8,
      "unidad_medida": "TB",
      "precio_maximo_unidad": 160,
      "precio_maximo_pedido": null,
      "precio_maximo_por_unidad_medida": 20,
      "cantidad": 2,
      "activo": true
    }
  ]
}
```

### Opciones globales:
* `incluir_envio` (bool): Si es `true`, suma los gastos de envío al coste final.
* `exigir_envio_confirmado` (bool): Si es `true` y la web no indica el envío (ni es gratis), descarta la alerta para evitar avisarte de precios que luego se encarecen en el carrito.
* `envio_por_defecto` (float o `null`): Si no se detecta el envío en la web, aplica esta cantidad estimada (ej: `5.0`) en vez de descartar la oferta.
* `intervalo_minutos` (int): Tiempo mínimo entre ejecuciones locales. En GitHub Actions se sincroniza con el cron del workflow.

### Opciones por producto:
* `precio_maximo_unidad`: Límite máximo por unidad (con su parte proporcional de envío). Pon `null` si no quieres usarlo.
* `precio_maximo_pedido`: Límite máximo para el pedido completo (`precio × cantidad + envío`). Pon `null` si no te interesa.
* `precio_maximo_por_unidad_medida`: Límite en la unidad de medida (ej. `20` para 20 €/TB). Pon `null` si no lo necesitas.
* `unidad_medida`: Medida (`TB`, `GB`, `kg`, etc.).
* `capacidad`: Capacidad unitaria (ej. `8` para 8TB). Si se omite, se intentará extraer automáticamente del nombre o búsqueda.
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
