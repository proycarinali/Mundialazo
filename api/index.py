"""
api.py — Servicio HTTP para Golazo IA
Expone:
  GET  /                                  → sirve index.html
  GET  /api/ligas                         → ligas disponibles en la BD
  GET  /api/partidos?liga_nombre=         → últimos partidos de una liga
  GET  /api/trivias?id_partido=           → preguntas de trivia de un partido (SIN la respuesta correcta)
  POST /api/trivias/verificar             → valida una respuesta contra la BD
  POST /api/salas/crear                   → crea sala (estado abierta, dura 8h)
  GET  /api/salas/<codigo>                → info de sala (auto-cierra a las 2h)
  POST /api/salas/<codigo>/resultado      → guarda puntuación de un jugador
  GET  /api/salas/<codigo>/ranking        → ranking de jugadores de la sala
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os, uuid, logging, secrets
from datetime import datetime, timezone, timedelta
import psycopg2
import psycopg2.extras

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    LIMITER_DISPONIBLE = True
except ImportError:
    LIMITER_DISPONIBLE = False

# ─────────────────────────────────────────────────────
# CONFIG BD
# ─────────────────────────────────────────────────────
DB_HOST = "aws-1-us-east-2.pooler.supabase.com"
DB_NAME = "postgres"
DB_USER = os.environ.get("BASE_USER", "") 
DB_PASS =  os.environ.get("BASE_PASS", "") 
DB_PORT = "6543"

def conectar():
    return psycopg2.connect(
        host=DB_HOST, database=DB_NAME,
        user=DB_USER, password=DB_PASS, port=DB_PORT,
        connect_timeout=10
    )

# ─────────────────────────────────────────────────────
# LOGGING (los detalles de errores van al log, NUNCA al cliente)
# ─────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("golazo-api")

def error_response(e, status=500, mensaje="Ocurrió un error interno. Intentá de nuevo más tarde."):
    """Loguea la excepción completa en el servidor y devuelve un mensaje
    genérico al cliente, para no filtrar detalles internos (esquema de BD,
    host, etc.) en las respuestas HTTP."""
    logger.exception(e)
    return jsonify({"error": mensaje}), status

# ─────────────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────────────
app = Flask(__name__, static_folder=".")

# CORS restringido: por defecto sólo permite pedidos desde el propio origen.
# Configurá la variable de entorno ALLOWED_ORIGINS con una lista separada
# por comas si necesitás servir el frontend desde otro dominio, ej:
#   ALLOWED_ORIGINS=https://golazo-ia.com,https://www.golazo-ia.com
_origenes_env = os.environ.get("ALLOWED_ORIGINS", "").strip()
if _origenes_env:
    ORIGENES_PERMITIDOS = [o.strip() for o in _origenes_env.split(",") if o.strip()]
else:
    # Sin la variable configurada, no se habilita CORS de terceros: sólo
    # funcionan los pedidos same-origin (el propio index.html servido por Flask).
    ORIGENES_PERMITIDOS = []

CORS(app, origins=ORIGENES_PERMITIDOS if ORIGENES_PERMITIDOS else None, supports_credentials=False)

# ─────────────────────────────────────────────────────
# RATE LIMITING (mitiga fuerza bruta / spam de salas y resultados)
# Requiere el paquete "flask-limiter" (agregalo a requirements.txt).
# Si no está instalado, la app sigue funcionando pero sin límites.
# ─────────────────────────────────────────────────────
if LIMITER_DISPONIBLE:
    limiter = Limiter(get_remote_address, app=app, default_limits=["200 per hour"])
else:
    class _NoOpLimiter:
        def limit(self, *a, **k):
            def deco(f):
                return f
            return deco
    limiter = _NoOpLimiter()
    logger.warning("flask-limiter no está instalado: la API queda sin rate limiting. "
                    "Instalá 'flask-limiter' para habilitarlo.")


# ─────────────────────────────────────────────────────
# HELPERS EXISTENTES
# ─────────────────────────────────────────────────────

def obtener_preguntas_partido(id_partido, conn):
    cursor = conn.cursor()
    cursor.execute('''
            SELECT p.id_pregunta, p.nro_pregunta, p.pregunta,
               r.id_respuesta, r.letra, r.texto_opcion, r.es_correcta
        FROM (
            SELECT id_pregunta, nro_pregunta, pregunta 
            FROM preguntas_partido 
            WHERE id_partido = %s 
            order by random()
            LIMIT 10
        ) p
        JOIN respuestas_preguntas r ON r.id_pregunta = p.id_pregunta
        ORDER BY p.nro_pregunta, r.letra;
    ''', (id_partido,))
    filas = cursor.fetchall()
    cursor.close()

    preguntas_dict = {}
    for (id_preg, nro, texto_preg, id_resp, letra, texto_op, correcta) in filas:
        if id_preg not in preguntas_dict:
            preguntas_dict[id_preg] = {
                "id_pregunta": id_preg,
                "nro_pregunta": nro,
                "pregunta": texto_preg,
                "opciones": [],
            }
        preguntas_dict[id_preg]["opciones"].append({
            "id_respuesta": id_resp,
            "letra": letra,
            "texto": texto_op,
            "es_correcta": correcta,
        })
    return list(preguntas_dict.values())


def get_ligas_disponibles(conn):
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.liga_nombre, COUNT(DISTINCT p.id_partido) as total
        FROM partidos p
        WHERE p.liga_nombre IS NOT NULL
        GROUP BY p.liga_nombre
        ORDER BY total DESC;
    """)
    filas = cursor.fetchall()
    cursor.close()
    return [{"liga_nombre": f[0], "total_partidos": f[1]} for f in filas]


def get_partidos_por_liga(liga_nombre, limite, conn):
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT p.id_partido, p.fecha_partido, p.liga_nombre,
               p.equipo_local_nombre, p.equipo_local_goles,
               p.equipo_visitante_nombre, p.equipo_visitante_goles,
               p.ganador, p.tanda_penales
        FROM partidos p
        join  preguntas_partido pp on pp.id_partido=p.id_partido
        WHERE p.liga_nombre ILIKE %s
        ORDER BY p.fecha_partido DESC
        LIMIT %s;
    """, (f"%{liga_nombre}%", limite))
    filas = cursor.fetchall()
    cursor.close()

    partidos = []
    for (id_p, fecha, liga, loc, g_loc, vis, g_vis, ganador, penales) in filas:
        label = f"{loc} {g_loc} - {g_vis} {vis}"
        if penales:
            label += " (pen)"
        fecha_str = fecha.strftime("%d/%m/%Y") if fecha else ""
        partidos.append({
            "id_partido": id_p,
            "label":      label,
            "fecha":      fecha_str,
            "liga":       liga,
            "ganador":    ganador or "",
        })
    return partidos


# ─────────────────────────────────────────────────────
# HELPERS MUNDIALES (tabla `mundial`)
# ─────────────────────────────────────────────────────

# Mapa año -> (país sede, bandera emoji). Fuente confiable como fallback
# cuando el texto de `detalle` no menciona el país explícitamente.
MUNDIALES_ANIO_PAIS = {
    1930: ("Uruguay", "🇺🇾"),
    1934: ("Italia", "🇮🇹"),
    1938: ("Francia", "🇫🇷"),
    1950: ("Brasil", "🇧🇷"),
    1954: ("Suiza", "🇨🇭"),
    1958: ("Suecia", "🇸🇪"),
    1962: ("Chile", "🇨🇱"),
    1966: ("Inglaterra", "🇬🇧"),
    1970: ("México", "🇲🇽"),
    1974: ("Alemania", "🇩🇪"),
    1978: ("Argentina", "🇦🇷"),
    1982: ("España", "🇪🇸"),
    1986: ("México", "🇲🇽"),
    1990: ("Italia", "🇮🇹"),
    1994: ("Estados Unidos", "🇺🇸"),
    1998: ("Francia", "🇫🇷"),
    2002: ("Corea del Sur y Japón", "🇰🇷🇯🇵"),
    2006: ("Alemania", "🇩🇪"),
    2010: ("Sudáfrica", "🇿🇦"),
    2014: ("Brasil", "🇧🇷"),
    2018: ("Rusia", "🇷🇺"),
    2022: ("Catar", "🇶🇦"),
    2026: ("Estados Unidos, México y Canadá", "🇺🇸🇲🇽🇨🇦"),
}


def detectar_pais_bandera(detalle, anio):
    """
    Primero intenta resolver el país/bandera por el año del mundial (fuente
    confiable). Si el año no está mapeado, intenta inferirlo buscando el
    nombre del país dentro del texto `detalle`.
    """
    datos_anio = MUNDIALES_ANIO_PAIS.get(anio)
    if datos_anio:
        return datos_anio

    detalle_low = (detalle or "").lower()
    for pais, bandera in MUNDIALES_ANIO_PAIS.values():
        pais_simple = pais.split("/")[0].split(" y ")[0].strip().lower()
        if pais_simple and pais_simple in detalle_low:
            return (pais, bandera)

    return ("Desconocido", "🏳️")


def get_mundiales(conn):
    """
    Levanta todos los mundiales de la tabla `mundial` y, para cada uno,
    busca en `partidos` (por id_mundial) el partido fijo que tiene trivia
    cargada (mismo criterio que get_partidos_por_liga: join con
    preguntas_partido para asegurar que haya preguntas).
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT m.id_mundial, m.detalle, m.anio,
               (SELECT p.id_partido
                  FROM partidos p
                  JOIN preguntas_partido pp ON pp.id_partido = p.id_partido
                 WHERE p.id_mundial = m.id_mundial
                 ORDER BY p.id_partido
                 LIMIT 1) AS id_partido
        FROM mundial m
        ORDER BY m.anio;
    """)
    filas = cursor.fetchall()
    cursor.close()

    mundiales = []
    for (id_mundial, detalle, anio, id_partido) in filas:
        pais, bandera = detectar_pais_bandera(detalle, anio)
        mundiales.append({
            "id_mundial": id_mundial,
            "detalle":    detalle,
            "anio":       anio,
            "pais":       pais,
            "bandera":    bandera,
            "id_partido": id_partido,
        })
    return mundiales


# ─────────────────────────────────────────────────────
# HELPERS SALAS
# ─────────────────────────────────────────────────────

def limpiar_salas_viejas(conn):
    """Elimina salas cerradas con más de 5 horas de antigüedad."""
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM salas
        WHERE estado = 'cerrada'
          AND creada_en < NOW() - INTERVAL '5 hours';
    """)
    conn.commit()
    cursor.close()


def sala_a_dict(row):
    """Convierte una fila de sala a diccionario JSON-serializable."""
    return {
        "codigo":        row["codigo"],
        "nombre":        row["nombre"],
        "idPartido":     row["id_partido"],
        "labelPartido":  row["label_partido"],
        "liga":          row.get("liga_partido") or "",
        "maxJugadores":  row["max_jugadores"],
        "estado":        row["estado"],
        "abierta_en":    row["abierta_en"].isoformat() if row["abierta_en"] else None,
        "creada_en":     row["creada_en"].isoformat() if row["creada_en"] else None,
        "tiene_jugadas": row.get("tiene_jugadas", False),
    }


# ─────────────────────────────────────────────────────
# ENDPOINTS ORIGINALES
# ─────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/ligas")
def api_ligas():
    try:
        conn  = conectar()
        ligas = get_ligas_disponibles(conn)
        conn.close()
        return jsonify({"ligas": ligas})
    except Exception as e:
        return error_response(e)


@app.route("/api/partidos")
def api_partidos():
    liga_nombre = request.args.get("liga_nombre", "").strip()

    try:
        limite = int(request.args.get("limit", 10))
    except (TypeError, ValueError):
        return jsonify({"error": "El parámetro limit debe ser numérico"}), 400
    limite = max(1, min(limite, 100))  # tope razonable

    if not liga_nombre:
        return jsonify({"error": "Falta el parámetro liga_nombre"}), 400

    try:
        conn     = conectar()
        partidos = get_partidos_por_liga(liga_nombre, limite, conn)
        conn.close()
        return jsonify({"partidos": partidos})
    except Exception as e:
        return error_response(e)


@app.route("/api/mundiales")
def api_mundiales():
    try:
        conn = conectar()
        mundiales = get_mundiales(conn)
        conn.close()
        return jsonify({"mundiales": mundiales})
    except Exception as e:
        return error_response(e)


@app.route("/api/trivias")
def api_trivias():
    """
    Devuelve las preguntas de un partido SIN la respuesta correcta.
    El cliente valida cada respuesta contra /api/trivias/verificar,
    para que la respuesta correcta nunca viaje al navegador antes de
    que el jugador responda (evita que se pueda ver en las DevTools).
    """
    id_partido = request.args.get("id_partido", "").strip()

    if not id_partido:
        return jsonify({"error": "Falta el parámetro id_partido"}), 400

    try:
        conn  = conectar()
        filas = obtener_preguntas_partido(id_partido, conn)
        conn.close()

        if not filas:
            return jsonify({"error": "No hay  cargadas para este partido"}), 404

        trivias = []
        for item in filas:
            trivias.append({
                "id_pregunta": item["id_pregunta"],
                "pregunta": item["pregunta"],
                "opciones": [
                    {"id_respuesta": op["id_respuesta"], "texto": op["texto"]}
                    for op in item["opciones"]
                ],
            })

        return jsonify({"preguntas": trivias})

    except Exception as e:
        return error_response(e)


@app.route("/api/trivias/verificar", methods=["POST"])
@limiter.limit("120 per minute")
def api_verificar_respuesta():
    """
    Valida la respuesta elegida por el jugador contra la base de datos.
    Body: { id_pregunta, id_respuesta }  (id_respuesta puede ser null si
    se agotó el tiempo, en cuyo caso sólo se informa cuál era la correcta).
    Devuelve: { correcta, id_respuesta_correcta, texto_correcta }
    """
    data = request.get_json(silent=True) or {}
    id_pregunta  = data.get("id_pregunta")
    id_respuesta = data.get("id_respuesta")

    if id_pregunta is None:
        return jsonify({"error": "Falta id_pregunta"}), 400

    try:
        conn = conectar()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id_respuesta, texto_opcion, es_correcta
            FROM respuestas_preguntas
            WHERE id_pregunta = %s
        """, (id_pregunta,))
        filas = cursor.fetchall()
        cursor.close()
        conn.close()

        if not filas:
            return jsonify({"error": "Pregunta no encontrada"}), 404

        correcta_row = next((f for f in filas if f[2]), None)
        if not correcta_row:
            return jsonify({"error": "Pregunta sin respuesta correcta cargada"}), 500

        es_correcta = (
            id_respuesta is not None and
            any(f[0] == id_respuesta and f[2] for f in filas)
        )

        return jsonify({
            "correcta": bool(es_correcta),
            "id_respuesta_correcta": correcta_row[0],
            "texto_correcta": correcta_row[1],
        })

    except Exception as e:
        return error_response(e)


# ─────────────────────────────────────────────────────
# ENDPOINTS SALAS
# ─────────────────────────────────────────────────────

@app.route("/api/salas/crear", methods=["POST"])
@limiter.limit("10 per minute")
def api_crear_sala():
    """
    Crea una sala nueva.
    Body JSON: { nombre, idPartido, labelPartido, maxJugadores, codigo }
    """
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "Body inválido, se esperaba JSON"}), 400

    nombre        = (data.get("nombre") or "").strip()[:40]
    id_partido    = str(data.get("idPartido") or "").strip()
    label_partido = (data.get("labelPartido") or "").strip()[:120]
    liga_partido  = (data.get("ligaPartido") or "").strip()[:80]
    codigo        = (data.get("codigo") or "").strip().upper()[:20]

    try:
        max_jugadores = min(int(data.get("maxJugadores") or 12), 12)
        if max_jugadores < 1:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "maxJugadores inválido"}), 400

    if not nombre or not id_partido or not codigo:
        return jsonify({"error": "Faltan campos obligatorios"}), 400

    # El código de sala lo genera el cliente, pero validamos su formato
    # para evitar valores raros (whitespace, símbolos de control, etc.)
    if not codigo.replace("-", "").isalnum():
        return jsonify({"error": "Código de sala inválido"}), 400

    try:
        conn   = conectar()
        limpiar_salas_viejas(conn)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute("""
            INSERT INTO salas (codigo, nombre, id_partido, label_partido, max_jugadores, estado, creada_en, abierta_en)
            VALUES (%s, %s, %s, %s, %s, 'abierta', NOW(), NOW())
            ON CONFLICT (codigo) DO NOTHING
            RETURNING *;
        """, (codigo, nombre, id_partido, label_partido, max_jugadores))

        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()

        if not row:
            return jsonify({"error": "Código de sala duplicado"}), 409

        row["liga_partido"] = liga_partido
        return jsonify({"sala": sala_a_dict(row)}), 201

    except Exception as e:
        return error_response(e)


@app.route("/api/salas/<codigo>")
@limiter.limit("60 per minute")
def api_get_sala(codigo):
    """Devuelve info de la sala. Si pasaron 2h desde cierre, sigue mostrando ranking."""
    codigo = codigo.upper()
    try:
        conn   = conectar()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute("""
            SELECT s.*,
                   EXISTS(SELECT 1 FROM salas_jugador sj WHERE sj.codigo_sala = s.codigo) AS tiene_jugadas,
                   p.liga_nombre AS liga_partido
            FROM salas s
            LEFT JOIN partidos p ON p.id_partido = s.id_partido
            WHERE s.codigo = %s
            LIMIT 1;
        """, (codigo,))

        row = cursor.fetchone()
        cursor.close()

        # Auto-cerrar si lleva más de 2 horas desde creación
        if row and row["estado"] == "abierta" and row["creada_en"]:
            limite = row["creada_en"] + timedelta(hours=8)
            if datetime.now(timezone.utc) > (limite.replace(tzinfo=timezone.utc) if limite.tzinfo is None else limite):
                cur2 = conn.cursor()
                cur2.execute("UPDATE salas SET estado='cerrada' WHERE codigo=%s", (codigo,))
                conn.commit()
                cur2.close()
                row["estado"] = "cerrada"

        conn.close()

        if not row:
            return jsonify({"error": "Sala no encontrada"}), 404

        return jsonify({"sala": sala_a_dict(row)})

    except Exception as e:
        return error_response(e)


def _calcular_puntaje_server(conn, id_partido, respuestas):
    """
    Recalcula puntos/correctas/errores en el servidor a partir de las
    respuestas elegidas por el jugador, sin confiar en ningún número
    que venga del cliente. `respuestas` es una lista de dicts:
        { id_pregunta, id_respuesta (o None si no contestó), tiempo_restante }
    Sólo se aceptan preguntas que realmente pertenezcan al id_partido
    de la sala, para evitar que se "cuelen" preguntas de otro partido.
    """
    cursor = conn.cursor()
    puntos = correctas = errores = 0

    for r in respuestas:
        id_pregunta = r.get("id_pregunta")
        id_respuesta = r.get("id_respuesta")
        try:
            tiempo_restante = int(r.get("tiempo_restante") or 0)
        except (TypeError, ValueError):
            tiempo_restante = 0
        tiempo_restante = max(0, min(tiempo_restante, 30))  # mismo rango que el timer del quiz

        if id_pregunta is None:
            continue

        # La pregunta tiene que pertenecer al partido de esta sala
        cursor.execute("""
            SELECT 1 FROM preguntas_partido
            WHERE id_pregunta = %s AND id_partido = %s
        """, (id_pregunta, id_partido))
        if not cursor.fetchone():
            continue  # pregunta ajena a este partido: se ignora

        cursor.execute("""
            SELECT id_respuesta FROM respuestas_preguntas
            WHERE id_pregunta = %s AND es_correcta = TRUE
        """, (id_pregunta,))
        fila_correcta = cursor.fetchone()
        es_correcta = bool(fila_correcta and id_respuesta is not None and fila_correcta[0] == id_respuesta)

        if es_correcta:
            puntos += 10 + tiempo_restante
            correctas += 1
        else:
            errores += 1

    cursor.close()
    return puntos, correctas, errores


@app.route("/api/salas/<codigo>/resultado", methods=["POST"])
@limiter.limit("30 per minute")
def api_guardar_resultado(codigo):
    """
    Guarda el resultado de un jugador en la sala.
    Body: { apodo, token, respuestas: [{id_pregunta, id_respuesta, tiempo_restante}, ...] }

    El puntaje NO se recibe del cliente: se recalcula acá mismo contra la
    base de datos a partir de las respuestas elegidas, para que no se
    pueda falsear el ranking mandando un POST manual con puntos inventados.

    `token` es un identificador aleatorio que genera el navegador la
    primera vez que el jugador entra a la sala (ver golazo_token_<codigo>
    en localStorage). Sirve para que otra persona no pueda pisar el
    resultado de alguien más usando el mismo apodo.
    """
    codigo = codigo.upper()
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "Body inválido, se esperaba JSON"}), 400

    apodo = (data.get("apodo") or "Jugador").strip()[:30]
    token = (data.get("token") or "").strip()[:64]
    respuestas = data.get("respuestas")
    if not apodo:
        return jsonify({"error": "Falta el apodo"}), 400
    if not token:
        return jsonify({"error": "Falta el token de jugador"}), 400
    if not isinstance(respuestas, list):
        return jsonify({"error": "Formato de respuestas inválido"}), 400

    try:
        conn   = conectar()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Verificar que la sala existe y está abierta
        cursor.execute("SELECT id_partido, max_jugadores FROM salas WHERE codigo=%s AND estado='abierta'", (codigo,))
        sala_row = cursor.fetchone()
        if not sala_row:
            cursor.close(); conn.close()
            return jsonify({"error": "La sala no está abierta o no existe"}), 400

        # Si ya existe una jugada con este apodo en la sala, sólo se puede
        # actualizar si el token coincide (evita que alguien pise el
        # resultado de otro jugador usando el mismo apodo).
        cursor.execute("""
            SELECT jugador_token FROM salas_jugador
            WHERE codigo_sala=%s AND apodo=%s
        """, (codigo, apodo))
        existente = cursor.fetchone()
        if existente and existente["jugador_token"] and existente["jugador_token"] != token:
            cursor.close(); conn.close()
            return jsonify({"error": "Ese apodo ya está en uso en esta sala por otro jugador"}), 409

        # Verificar cuántos jugadores ya hay (sólo aplica a jugadores nuevos)
        if not existente:
            cursor.execute("SELECT COUNT(*) AS total FROM salas_jugador WHERE codigo_sala=%s", (codigo,))
            count_row = cursor.fetchone()
            if count_row["total"] >= sala_row["max_jugadores"]:
                cursor.close(); conn.close()
                return jsonify({"error": "La sala ya alcanzó el máximo de jugadores"}), 400

        # Recalcular el puntaje en el servidor (nunca confiar en el cliente)
        puntos, correctas, errores = _calcular_puntaje_server(conn, sala_row["id_partido"], respuestas)

        cursor.execute("""
            INSERT INTO salas_jugador (codigo_sala, apodo, puntos, correctas, errores, jugador_token, jugado_en)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (codigo_sala, apodo) DO UPDATE
              SET puntos=EXCLUDED.puntos, correctas=EXCLUDED.correctas,
                  errores=EXCLUDED.errores, jugador_token=EXCLUDED.jugador_token,
                  jugado_en=NOW();
        """, (codigo, apodo, puntos, correctas, errores, token))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"ok": True, "puntos": puntos, "correctas": correctas, "errores": errores})

    except Exception as e:
        return error_response(e)


@app.route("/api/salas/<codigo>/ranking")
@limiter.limit("60 per minute")
def api_ranking(codigo):
    """Devuelve el ranking de jugadores de la sala, solo si está cerrada."""
    codigo = codigo.upper()
    try:
        conn   = conectar()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Verificar sala
        cursor.execute("SELECT estado FROM salas WHERE codigo=%s", (codigo,))
        sala = cursor.fetchone()
        if not sala:
            cursor.close(); conn.close()
            return jsonify({"error": "Sala no encontrada"}), 404

        # Ranking sin restricción de estado (se puede ver siempre si hay jugadas)
        cursor.execute("""
            SELECT apodo, puntos, correctas, errores, jugado_en
            FROM salas_jugador
            WHERE codigo_sala = %s
            ORDER BY puntos DESC, correctas DESC, jugado_en ASC;
        """, (codigo,))
        filas = cursor.fetchall()
        cursor.close()
        conn.close()

        ranking = [
            {
                "apodo":    f["apodo"],
                "puntos":   f["puntos"],
                "correctas":f["correctas"],
                "errores":  f["errores"],
            }
            for f in filas
        ]
        return jsonify({"ranking": ranking})

    except Exception as e:
        return error_response(e)


# ─────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

