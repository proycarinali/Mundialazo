"""
api.py — Servicio HTTP para Golazo IA
Expone:
  GET  /                                  → sirve index.html
  GET  /api/ligas                         → ligas disponibles en la BD
  GET  /api/partidos?liga_nombre=         → últimos partidos de una liga
  GET  /api/trivias?id_partido=           → preguntas de trivia de un partido
  POST /api/salas/crear                   → crea sala luego de pago aprobado
  GET  /api/salas/<codigo>                → info de sala
  POST /api/salas/<codigo>/abrir          → admin abre la sala
  POST /api/salas/<codigo>/cerrar         → admin cierra la sala
  POST /api/salas/<codigo>/resultado      → guarda puntuación de un jugador
  GET  /api/salas/<codigo>/ranking        → ranking de jugadores de la sala
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os, uuid
from datetime import datetime, timezone, timedelta
import psycopg2
import psycopg2.extras

# ─────────────────────────────────────────────────────
# CONFIG BD
# ─────────────────────────────────────────────────────
DB_HOST = "aws-1-us-east-2.pooler.supabase.com"
DB_NAME = "postgres"
DB_USER = "postgres.vlndghikrjvxmiibbqbo"
DB_PASS = "Lif#Cari.Fuk"
DB_PORT = "6543"

def conectar():
    return psycopg2.connect(
        host=DB_HOST, database=DB_NAME,
        user=DB_USER, password=DB_PASS, port=DB_PORT,
        connect_timeout=10
    )

# ─────────────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────────────
app = Flask(__name__, static_folder=".")
CORS(app)


# ─────────────────────────────────────────────────────
# HELPERS EXISTENTES
# ─────────────────────────────────────────────────────

def obtener_preguntas_partido(id_partido, conn):
    cursor = conn.cursor()
    cursor.execute('''
        SELECT p.id_pregunta, p.nro_pregunta, p.pregunta,
               r.id_respuesta, r.letra, r.texto_opcion, r.es_correcta
        FROM preguntas_partido p
        JOIN respuestas_preguntas r ON r.id_pregunta = p.id_pregunta
        WHERE p.id_partido = %s
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
        INNER JOIN preguntas_partido pp ON pp.id_partido = p.id_partido
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
        INNER JOIN preguntas_partido pp ON pp.id_partido = p.id_partido
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
        return jsonify({"error": str(e)}), 500


@app.route("/api/partidos")
def api_partidos():
    liga_nombre = request.args.get("liga_nombre", "").strip()
    limite      = int(request.args.get("limit", 3))

    if not liga_nombre:
        return jsonify({"error": "Falta el parámetro liga_nombre"}), 400

    try:
        conn     = conectar()
        partidos = get_partidos_por_liga(liga_nombre, limite, conn)
        conn.close()
        return jsonify({"partidos": partidos})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trivias")
def api_trivias():
    id_partido = request.args.get("id_partido", "").strip()

    if not id_partido:
        return jsonify({"error": "Falta el parámetro id_partido"}), 400

    try:
        conn  = conectar()
        filas = obtener_preguntas_partido(id_partido, conn)
        conn.close()

        if not filas:
            return jsonify({"error": "No hay preguntas cargadas para este partido"}), 404

        preguntas = []
        for item in filas:
            opciones = [op["texto"] for op in item["opciones"]]
            correcta = next(
                (op["texto"] for op in item["opciones"] if op["es_correcta"]),
                opciones[0] if opciones else ""
            )
            preguntas.append({
                "pregunta": item["pregunta"],
                "opciones": opciones,
                "correcta": correcta,
            })

        return jsonify({"preguntas": preguntas})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────
# ENDPOINTS SALAS
# ─────────────────────────────────────────────────────

@app.route("/api/salas/crear", methods=["POST"])
def api_crear_sala():
    """
    Crea una sala paga luego de recibir el pago aprobado.
    Body JSON: { nombre, idPartido, labelPartido, maxJugadores, codigo }
    """
    data = request.get_json(force=True)
    nombre       = (data.get("nombre") or "").strip()[:40]
    id_partido   = str(data.get("idPartido") or "").strip()
    label_partido= (data.get("labelPartido") or "").strip()[:120]
    max_jugadores= min(int(data.get("maxJugadores") or 6), 12)
    codigo       = (data.get("codigo") or "").strip().upper()[:20]

    if not nombre or not id_partido or not codigo:
        return jsonify({"error": "Faltan campos obligatorios"}), 400

    try:
        conn   = conectar()
        limpiar_salas_viejas(conn)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute("""
            INSERT INTO salas (codigo, nombre, id_partido, label_partido, max_jugadores, estado, creada_en)
            VALUES (%s, %s, %s, %s, %s, 'cerrada', NOW())
            ON CONFLICT (codigo) DO NOTHING
            RETURNING *;
        """, (codigo, nombre, id_partido, label_partido, max_jugadores))

        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()

        if not row:
            return jsonify({"error": "Código de sala duplicado"}), 409

        return jsonify({"sala": sala_a_dict(row)}), 201

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/salas/<codigo>")
def api_get_sala(codigo):
    """Devuelve info de la sala. Si pasaron 2h desde cierre, sigue mostrando ranking."""
    codigo = codigo.upper()
    try:
        conn   = conectar()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute("""
            SELECT s.*,
                   EXISTS(SELECT 1 FROM salas_jugador sj WHERE sj.codigo_sala = s.codigo) AS tiene_jugadas
            FROM salas s
            WHERE s.codigo = %s;
        """, (codigo,))

        row = cursor.fetchone()
        cursor.close()

        # Auto-cerrar si lleva más de 1 hora abierta
        if row and row["estado"] == "abierta" and row["abierta_en"]:
            limite = row["abierta_en"] + timedelta(hours=1)
            if datetime.now(timezone.utc) > limite.replace(tzinfo=timezone.utc) if limite.tzinfo is None else datetime.now(timezone.utc) > limite:
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
        return jsonify({"error": str(e)}), 500


@app.route("/api/salas/<codigo>/abrir", methods=["POST"])
def api_abrir_sala(codigo):
    codigo = codigo.upper()
    try:
        conn   = conectar()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            UPDATE salas SET estado='abierta', abierta_en=NOW()
            WHERE codigo=%s
            RETURNING *;
        """, (codigo,))
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()
        if not row:
            return jsonify({"error": "Sala no encontrada"}), 404
        return jsonify({"sala": sala_a_dict(row)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/salas/<codigo>/cerrar", methods=["POST"])
def api_cerrar_sala(codigo):
    codigo = codigo.upper()
    try:
        conn   = conectar()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            UPDATE salas SET estado='cerrada'
            WHERE codigo=%s
            RETURNING *;
        """, (codigo,))
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()
        if not row:
            return jsonify({"error": "Sala no encontrada"}), 404
        return jsonify({"sala": sala_a_dict(row)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/salas/<codigo>/resultado", methods=["POST"])
def api_guardar_resultado(codigo):
    """
    Guarda el resultado de un jugador en la sala.
    Body: { apodo, puntos, correctas, errores }
    """
    codigo = codigo.upper()
    data   = request.get_json(force=True)
    apodo  = (data.get("apodo") or "Jugador").strip()[:30]
    puntos = int(data.get("puntos") or 0)
    correctas = int(data.get("correctas") or 0)
    errores   = int(data.get("errores") or 0)

    try:
        conn   = conectar()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Verificar que la sala existe y está abierta
        cursor.execute("SELECT max_jugadores FROM salas WHERE codigo=%s AND estado='abierta'", (codigo,))
        sala_row = cursor.fetchone()
        if not sala_row:
            cursor.close(); conn.close()
            return jsonify({"error": "La sala no está abierta o no existe"}), 400

        # Verificar cuántos jugadores ya hay
        cursor.execute("SELECT COUNT(*) AS total FROM salas_jugador WHERE codigo_sala=%s", (codigo,))
        count_row = cursor.fetchone()
        if count_row["total"] >= sala_row["max_jugadores"]:
            cursor.close(); conn.close()
            return jsonify({"error": "La sala ya alcanzó el máximo de jugadores"}), 400

        # Insertar o actualizar resultado (upsert por apodo)
        cursor.execute("""
            INSERT INTO salas_jugador (codigo_sala, apodo, puntos, correctas, errores, jugado_en)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (codigo_sala, apodo) DO UPDATE
              SET puntos=EXCLUDED.puntos, correctas=EXCLUDED.correctas,
                  errores=EXCLUDED.errores, jugado_en=NOW();
        """, (codigo, apodo, puntos, correctas, errores))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"ok": True})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/salas/<codigo>/ranking")
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
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
