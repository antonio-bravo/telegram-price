# WD Red Plus Price Monitor

Monitor horario de precios para dos discos WD Red Plus destinados a un NAS en espejo.

Modelos:
- WD60EFPX (6 TB)
- WD80EFPX (8 TB)

## Qué hace

- Busca resultados web públicos mediante `ddgs`.
- Solo conserva resultados que mencionan el código exacto del modelo.
- Descarta resultados que parecen Pro, usados o reacondicionados.
- Extrae precios y gastos de envío cuando aparecen explícitamente en el snippet.
- Calcula el coste estimado de dos unidades y €/TB.
- Guarda observaciones en `historico_wd_red.csv`.
- Mantiene el estado de alertas en `monitor_state.json`.
- Envía alertas Telegram cuando el coste detectado está por debajo de 45 €/TB o baja respecto a la observación anterior.

## Limitaciones importantes

Este proyecto NO rastrea literalmente todas las tiendas de Internet. Usa un metabuscador y depende de los resultados que este devuelva. Los snippets pueden estar desactualizados o contener precios incompletos.

Los gastos de envío solo se consideran conocidos si el resultado de búsqueda los indica expresamente. Si no se detectan, no se calcula €/TB ni se envía una alerta de precio objetivo. El script no puede verificar el carrito, el stock, el vendedor final, la garantía o el estado del producto. Comprueba siempre la página de la tienda antes de comprar.

El envío detectado se interpreta como coste total del pedido de dos discos, no como envío por unidad. Verifica este supuesto en la tienda.

## Instalación en GitHub

1. Sube todos los archivos de este proyecto a la rama predeterminada del repositorio.
2. En GitHub, abre `Settings > Secrets and variables > Actions`.
3. Crea estos dos **Repository secrets**:
   - `TELEGRAM_BOT_TOKEN`: token de BotFather.
   - `TELEGRAM_CHAT_ID`: identificador del chat donde el bot ya ha recibido un mensaje `/start`.
4. Abre `Actions` y habilita los workflows si GitHub lo solicita.
5. Ejecuta manualmente `WD Red Plus price monitor` con `Run workflow`.
6. Comprueba el log y que se han creado `historico_wd_red.csv` y `monitor_state.json`.

No introduzcas el token en archivos del repositorio ni en el código.

## Umbrales

Por defecto:
- Vigilancia: 45 €/TB o menos.
- Muy buena oferta: 42 €/TB o menos.
- Objetivo: 40 €/TB o menos.

El cálculo es:

`(2 × precio_unitario + envío_del_pedido) / (2 × capacidad_TB)`

## Frecuencia

GitHub Actions ejecuta el workflow cada hora, al minuto 17, en la zona `Europe/Madrid`. Los horarios programados pueden sufrir retrasos ocasionales. GitHub ejecuta schedules sobre la rama predeterminada.

## Privacidad

Se recomienda mantener el repositorio privado. Aunque los secretos de Actions no se imprimen en los logs, el histórico puede contener URLs y datos de precios.

## Ejecución local (opcional)

Python 3.10+:

```bash
python -m pip install -r requirements.txt
```

Linux/macOS:

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python monitor_wd_red.py
```

Windows PowerShell:

```powershell
$env:TELEGRAM_BOT_TOKEN="..."
$env:TELEGRAM_CHAT_ID="..."
python monitor_wd_red.py
```
