# Monitor Universal de Precios (España y Europa) con Alertas por Telegram

Monitor automático de precios configurable mediante `config.json` para rastrear ofertas en tiendas online de **España y de toda Europa que envíen a España** (Amazon España, Amazon Alemania, Amazon Francia, Amazon Italia, Geizhals, Idealo, PcComponentes, etc.), recibiendo alertas en Telegram cuando el precio final (**incluyendo gastos de envío a España**) cumpla tus objetivos.

---

## 🚀 Características principales

* **Rastreo en España y la Unión Europea (Sin aduanas):** Consulta automáticamente tiendas del mercado único de la UE (Amazon España, Alemania, Francia, Italia, Países Bajos, Geizhals, Idealo UE, PcComponentes, etc.). **Excluye Reino Unido (UK)**, EE.UU., Suiza y terceros países para evitar aranceles, DUA y costes de importación.
* **Soporte para Wallapop y Vinted:** Rastrea anuncios de particulares en Wallapop y Vinted, calculando el importe final sumando el envío y el seguro de protección al comprador (4.50 €). Descarta automáticamente artículos rotos, averiados o para piezas.
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
  "solo_union_europea": true,
  "incluir_wallapop_vinted": true,
  "envio_wallapop_vinted_estimado": 4.50,
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

### 1. Crear el bot
1. Abre Telegram, busca [@BotFather](https://t.me/botfather) y pulsa **/start**.
2. Envía el comando `/newbot` y sigue los pasos para asignarle un nombre y un usuario.
3. Al finalizar, BotFather te entregará el **Bot Token** (guárdalo).

### 2. Obtener el Chat ID o Group ID
Puedes recibir las alertas por mensaje privado o en un grupo:

* **Opción A: En un grupo de Telegram (Recomendado)**
  1. Crea un nuevo grupo en Telegram y añade a tu bot recién creado.
  2. Añade también al bot [@getidsbot](https://t.me/getidsbot) dentro del grupo.
  3. En cuanto entre, `@getidsbot` enviará un mensaje mostrando la información del grupo, incluyendo el **`groupId`** (suele ser un número negativo que empieza por un guion, ej: `-100xxxxxxxxxx`).
  4. Copia ese ID (incluyendo el guion `-`) y elimina a `@getidsbot` del grupo.

* **Opción B: En un chat privado con el bot**
  1. Abre una conversación privada con tu bot y pulsa **/start** (o envíale cualquier mensaje).
  2. Habla con [@userinfobot](https://t.me/userinfobot) o reenvíale un mensaje a [@getidsbot](https://t.me/getidsbot) para obtener tu `Id` numérico personal.

> [!IMPORTANT]
> **Imprescindible:** Si usas un chat privado, debes haberle enviado al menos un mensaje al bot (o pulsado `/start`). Si usas un grupo, el bot debe ser miembro del grupo. De lo contrario, Telegram rechazará el envío con un error `403 Forbidden`.

### 3. Configurar en GitHub
1. En tu repositorio de GitHub, dirígete a:
   `Settings > Secrets and variables > Actions`
2. Configura las siguientes credenciales:
   * **En Secrets (`New repository secret`):**
     * Nombre: `TELEGRAM_BOT_TOKEN` (o `BOT_TOKEN`)
     * Valor: El token que te dio BotFather.
   * **En Variables (`New repository variable`) o en Secrets:**
     * Nombre: `TELEGRAM_CHAT_ID` (o `CHAT_ID`)
     * Valor: El `groupId` (o chat id numérico) obtenido con `@getidsbot`.

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
