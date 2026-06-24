"""
api.py — Servicio HTTP para Golazo IA
Expone:
  GET /                          → sirve index.html
  GET /api/ligas                 → ligas disponibles en la BD
  GET /api/partidos?liga_nombre= → últimos partidos de una liga
  GET /api/trivias?partido_nombre= → preguntas de trivia de un partido
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os
import psycopg2

DB_HOST = "aws-1-us-east-2.pooler.supabase.com"
DB_NAME = "postgres"
DB_USER = "postgres.vlndghikrjvxmiibbqbo"
DB_PASS = "Lif#Cari.Fuk"
DB_PORT = "6543"

def conectar_supabase():
    return psycopg2.connect(
        host=DB_HOST, database=DB_NAME,
        user=DB_USER, password=DB_PASS, port=DB_PORT,
        connect_timeout=10
    )
def obtener_preguntas_partido(id_partido, conn):
    """
    Devuelve todas las preguntas con sus opciones para un partido dado.
    Formato de retorno:
    [
      {
        "id_pregunta": str,
        "nro_pregunta": int,
        "pregunta": str,
        "opciones": [
          {"id_respuesta": str, "letra": str, "texto": str, "es_correcta": bool},
          ...
        ]
      },
      ...
    ]
    Devuelve lista vacía si el partido no tiene preguntas cargadas.
    """
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

    # Agrupar por pregunta
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

app = Flask(__name__, static_folder=".")
CORS(app)


# ──────────────────────────────────────────────
# Helpers de BD
# ──────────────────────────────────────────────

def get_ligas_disponibles(conn):
    """
    Devuelve las ligas distintas que tienen partidos cargados en la BD,
    junto con la cantidad de partidos disponibles.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT liga_nombre, COUNT(*) as total
        FROM partidos
        WHERE liga_nombre IS NOT NULL
        GROUP BY liga_nombre
        ORDER BY total DESC;
    """)
    filas = cursor.fetchall()
    cursor.close()
    return [{"liga_nombre": f[0], "total_partidos": f[1]} for f in filas]


def get_partidos_por_liga(liga_nombre, limite, conn):
    """
    Devuelve los últimos `limite` partidos de una liga,
    formateados como label para el HTML.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id_partido, fecha_partido, liga_nombre,
               equipo_local_nombre, equipo_local_goles,
               equipo_visitante_nombre, equipo_visitante_goles,
               ganador, tanda_penales
        FROM partidos
        WHERE liga_nombre ILIKE %s
        ORDER BY fecha_partido DESC
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


def get_id_partido_por_nombre(partido_nombre, conn):
    """
    Busca el id_partido cuyo label coincide con el nombre dado.
    El label tiene la forma "Local G - G Visitante".
    """
    cursor = conn.cursor()
    # Intentamos extraer los equipos del label "Local G - G Visitante"
    # La estrategia más robusta: buscar por nombre de equipos en el label
    cursor.execute("""
        SELECT id_partido,
               equipo_local_nombre || ' ' || equipo_local_goles || ' - ' ||
               equipo_visitante_goles || ' ' || equipo_visitante_nombre AS label
        FROM partidos
        ORDER BY fecha_partido DESC
        LIMIT 200;
    """)
    filas = cursor.fetchall()
    cursor.close()

    nombre_lower = partido_nombre.lower().replace("(pen)", "").strip()
    for (id_p, label) in filas:
        if label and label.lower().strip() == nombre_lower:
            return id_p

    # Segunda pasada: coincidencia parcial
    for (id_p, label) in filas:
        if label and nombre_lower in label.lower():
            return id_p

    return None


# ──────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/ligas")
def api_ligas():
    """
    Devuelve las ligas que tienen partidos en la BD.
    Respuesta:
      { "ligas": [ { "liga_nombre": str, "total_partidos": int }, ... ] }
    """
    try:
        conn = conectar_supabase()
        ligas = get_ligas_disponibles(conn)
        conn.close()
        return jsonify({"ligas": ligas})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/partidos")
def api_partidos():
    """
    Devuelve los últimos partidos de una liga.
    Query params:
      liga_nombre (str, requerido)
      limit       (int, opcional, default 3)
    Respuesta:
      { "partidos": [ { "id_partido", "label", "fecha", "liga", "ganador" }, ... ] }
    """
    liga_nombre = request.args.get("liga_nombre", "").strip()
    limite      = int(request.args.get("limit", 3))

    if not liga_nombre:
        return jsonify({"error": "Falta el parámetro liga_nombre"}), 400

    try:
        conn     = conectar_supabase()
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
        conn  = conectar_supabase()
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


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
