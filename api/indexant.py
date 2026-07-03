from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os, html
from datetime import datetime, timezone, timedelta
import psycopg2
from psycopg2 import pool
import psycopg2.extras

DB_HOST = "://supabase.com"
DB_NAME = "postgres"
DB_USER = os.environ.get("BASE_USER", "") 
DB_PASS = os.environ.get("BASE_PASS", "") 
DB_PORT = "6543"

app = Flask(__name__, static_folder=".")
CORS(app)

# 1. SOLUCIÓN DOS: Implementación de Connection Pool
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(
        1, 20, # Mínimo y máximo de conexiones simultáneas
        host=DB_HOST, database=DB_NAME,
        user=DB_USER, password=DB_PASS, port=DB_PORT,
        connect_timeout=10
    )
except Exception as e:
    print(f"Error al crear el pool de conexiones: {e}")
    db_pool = None

def obtener_conexion():
    if db_pool:
        return db_pool.getconn()
    raise Exception("Pool de conexiones no disponible")

def liberar_conexion(conn):
    if db_pool and conn:
        db_pool.putconn(conn)

# ─────────────────────────────────────────────────────
# HELPERS MODIFICADOS POR SEGURIDAD
# ─────────────────────────────────────────────────────

def obtener_preguntas_partido(id_partido, conn):
    cursor = conn.cursor()
    # Consulta parametrizada segura
    cursor.execute('''
        SELECT p.id_pregunta, p.nro_pregunta, p.pregunta,
               r.id_respuesta, r.letra, r.texto_opcion, r.es_correcta
        FROM (
            SELECT id_pregunta, nro_pregunta, pregunta 
            FROM preguntas_partido 
            WHERE id_partido = %s 
            ORDER BY random()
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

# (Se mantienen get_ligas_disponibles y get_partidos_por_liga usando el pool)

# ─────────────────────────────────────────────────────
# ENDPOINTS OPTIMIZADOS
# ─────────────────────────────────────────────────────

@app.route("/api/trivias")
def api_trivias():
    id_partido = request.args.get("id_partido", "").strip()
    if not id_partido:
        return jsonify({"error": "Falta el parámetro id_partido"}), 400

    conn = None
    try:
        conn = obtener_conexion()
        filas = obtener_preguntas_partido(id_partido, conn)
        
        if not filas:
            return jsonify({"error": "No hay preguntas cargadas"}), 404

        trivias = []
        for item in filas:
            # 3. SOLUCIÓN CHEATING: No enviamos la respuesta correcta ni el flag 'es_correcta' al frontend
            opciones = [{"id_respuesta": op["id_respuesta"], "texto": op["texto"]} for op in item["opciones"]]
            trivias.append({
                "id_pregunta": item["id_pregunta"],
                "pregunta": item["pregunta"],
                "opciones": opciones
            })

        return jsonify({"preguntas": trivias})
    except Exception as e:
        return jsonify({"error": "Error interno del servidor"}), 500
    finally:
        if conn: liberar_conexion(conn)


@app.route("/api/salas/crear", methods=["POST"])
def api_crear_sala():
    data = request.get_json(force=True)
    
    # 5. SOLUCIÓN XSS: Sanitizar cadenas de texto contra inyección HTML/JS
    nombre = html.escape((data.get("nombre") or "").strip())[:40]
    id_partido = str(data.get("idPartido") or "").strip()
    label_partido = html.escape((data.get("labelPartido") or "").strip())[:120]
    liga_partido = html.escape((data.get("ligaPartido") or "").strip())[:80]
    
    try:
        max_jugadores = min(int(data.get("maxJugadores") or 12), 12)
    except ValueError:
        max_jugadores = 12
        
    codigo = (data.get("codigo") or "").strip().upper()[:20]

    if not nombre or not id_partido or not codigo or not codigo.isalnum():
        return jsonify({"error": "Campos inválidos o código no alfanumérico"}), 400

    conn = None
    try:
        conn = obtener_conexion()
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

        if not row:
            return jsonify({"error": "Código de sala duplicado"}), 409

        return jsonify({"sala": {
            "codigo": row["codigo"],
            "nombre": row["nombre"],
            "idPartido": row["id_partido"]
        }}), 201
    except Exception as e:
        return jsonify({"error": "Error al crear sala"}), 500
    finally:
        if conn: liberar_conexion(conn)

# Nota: Deberás implementar un endpoint alternativo como `/api/trivias/verificar` 
# que reciba {"id_pregunta": X, "id_respuesta": Y} para procesar los puntos de forma segura en el servidor.
