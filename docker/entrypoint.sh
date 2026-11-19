#!/bin/sh
set -e

echo "============================================================"
echo "  MONITOR DE PRECIOS - DOCKER CONTAINER (NAS / LOCAL)"
echo "============================================================"

DATA_DIRECTORY="${DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIRECTORY"

# 1. Detección automática de rutas para persistencia
# Si el usuario mapeó archivos individuales en /app o en /app/data:
if [ ! -f "${CONFIG_PATH:-$DATA_DIRECTORY/config.json}" ] && [ -f "/app/config.json" ]; then
    export CONFIG_PATH="/app/config.json"
fi

if [ ! -f "${STATE_PATH:-$DATA_DIRECTORY/monitor_state.json}" ] && [ -f "/app/monitor_state.json" ]; then
    export STATE_PATH="/app/monitor_state.json"
fi

if [ ! -f "${HISTORY_PATH:-$DATA_DIRECTORY/historico.csv}" ] && [ -f "/app/historico.csv" ]; then
    export HISTORY_PATH="/app/historico.csv"
fi

CONFIG_TARGET="${CONFIG_PATH:-$DATA_DIRECTORY/config.json}"
STATE_TARGET="${STATE_PATH:-$DATA_DIRECTORY/monitor_state.json}"
HISTORY_TARGET="${HISTORY_PATH:-$DATA_DIRECTORY/historico.csv}"

# 2. Inicialización de archivos si no existen aún (evita errores en Docker)
if [ ! -f "$STATE_TARGET" ]; then
    echo "ℹ️ Inicializando archivo de estado vacío: $STATE_TARGET"
    echo '{"ofertas": {}}' > "$STATE_TARGET"
fi

if [ ! -f "$HISTORY_TARGET" ]; then
    echo "ℹ️ Inicializando archivo de histórico vacío: $HISTORY_TARGET"
    touch "$HISTORY_TARGET"
fi

echo "📁 Archivo de configuración: $CONFIG_TARGET"
echo "📁 Archivo de estado:        $STATE_TARGET"
echo "📁 Archivo de histórico:     $HISTORY_TARGET"

if [ ! -f "$CONFIG_TARGET" ]; then
    echo "⚠️ AVISO: No se encontró el archivo de configuración en '$CONFIG_TARGET'."
    echo "   Asegúrate de mapear tu archivo 'config.json' en los volúmenes de Docker."
fi

# 3. Configuración de frecuencia de rastreo
INTERVAL="${CHECK_INTERVAL_MINUTES:-60}"
echo "⏱️ Frecuencia configurada: Cada ${INTERVAL} minutos"

# 4. Ejecución inicial opcional al arrancar el contenedor
if [ "${RUN_ON_STARTUP:-true}" = "true" ]; then
    echo ""
    echo "🚀 [$(date '+%Y-%m-%d %H:%M:%S')] Ejecutando primera ronda de comprobación al iniciar..."
    python monitor.py || echo "⚠️ Error en la ejecución (el contenedor continuará su programación)..."
fi

# 5. Bucle de ejecución periódica
while true; do
    echo ""
    echo "⏳ Esperando ${INTERVAL} minutos hasta la siguiente comprobación..."
    sleep $(( INTERVAL * 60 ))
    echo "🔄 [$(date '+%Y-%m-%d %H:%M:%S')] Iniciando nueva comprobación de precios..."
    python monitor.py || echo "⚠️ Error en la ejecución (el contenedor continuará su programación)..."
done
