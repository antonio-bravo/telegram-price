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

# 3. Función para obtener el intervalo configurado dinámicamente
# Prioridad: config.json ("intervalo_minutos") > variable CHECK_INTERVAL_MINUTES > 60 min por defecto
obtener_intervalo() {
    python -c "
import json, os
cfg_file = os.environ.get('CONFIG_PATH', '$CONFIG_TARGET')
try:
    with open(cfg_file, 'r', encoding='utf-8') as f:
        c = json.load(f)
    val = c.get('intervalo_minutos') or c.get('check_interval_minutes')
    if val and int(val) > 0:
        print(int(val))
        exit(0)
except Exception:
    pass
print(int(os.environ.get('CHECK_INTERVAL_MINUTES', 60)))
" 2>/dev/null || echo "${CHECK_INTERVAL_MINUTES:-60}"
}

INTERVAL=$(obtener_intervalo)
echo "⏱️ Frecuencia configurada: Cada ${INTERVAL} minutos"

# 4. Iniciar panel web frontend en segundo plano (muy liviano, ~10 MB RAM)
if [ "${WEB_ENABLED:-true}" = "true" ]; then
    WEB_PORT="${WEB_PORT:-9999}"
    echo "🌐 Iniciando panel web en http://0.0.0.0:$WEB_PORT ..."
    python web_app.py &
fi

# 5. Ejecución inicial opcional al arrancar el contenedor
if [ "${RUN_ON_STARTUP:-true}" = "true" ]; then
    echo ""
    echo "🚀 [$(date '+%Y-%m-%d %H:%M:%S')] Ejecutando primera ronda de comprobación al iniciar..."
    python monitor.py || echo "⚠️ Error en la ejecución (el contenedor continuará su programación)..."
fi

# 6. Bucle de ejecución periódica con detección dinámica de cambios
while true; do
    INTERVAL=$(obtener_intervalo)
    echo ""
    echo "⏳ Esperando ${INTERVAL} minutos hasta la siguiente comprobación..."

    SECS_TOTAL=$(( INTERVAL * 60 ))
    SECS_PASADOS=0
    while [ "$SECS_PASADOS" -lt "$SECS_TOTAL" ]; do
        sleep 10
        SECS_PASADOS=$(( SECS_PASADOS + 10 ))
        # Comprobar cada 30 segundos si el usuario modificó la frecuencia en el panel web
        if [ $(( SECS_PASADOS % 30 )) -eq 0 ]; then
            NUEVO_INTERVAL=$(obtener_intervalo)
            if [ "$NUEVO_INTERVAL" -ne "$INTERVAL" ]; then
                echo "⏱️ Frecuencia modificada en vivo: cambió de ${INTERVAL}m a ${NUEVO_INTERVAL}m"
                INTERVAL=$NUEVO_INTERVAL
                SECS_TOTAL=$(( INTERVAL * 60 ))
            fi
        fi
    done

    echo "🔄 [$(date '+%Y-%m-%d %H:%M:%S')] Iniciando nueva comprobación de precios..."
    python monitor.py || echo "⚠️ Error en la ejecución (el contenedor continuará su programación)..."
done
