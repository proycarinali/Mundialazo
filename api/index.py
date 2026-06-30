"""
api.py — Servicio HTTP para Golazo IA
Expone:
  GET  /                                  → sirve index.html
  GET  /api/ligas                         → ligas disponibles en la BD
  GET  /api/partidos?liga_nombre=         → últimos partidos de una liga
  GET  /api/trivias?id_partido=           → preguntas de trivia de un partido
  POST /api/salas/crear                   → crea sala (estado abierta, dura 2h) luego de pago aprobado
  GET  /api/salas/<codigo>                → info de sala (auto-cierra a las 2h)
  POST /api/salas/<codigo>/resultado      → guarda puntuación de un jugador
  GET  /api/salas/<codigo>/ranking        → ranking de jugadores de la sala
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os, uuid, requests as http_req
from datetime import datetime, timezone, timedelta
import psycopg2
import psycopg2.extras

# ─────────────────────────────────────────────────────
# CONFIG BD
# ─────────────────────────────────────────────────────
DB_HOST = "aws-1-us-east-2.pooler.supabase.com"
DB_NAME = "postgres"
DB_USER = os.environ.get("BASE_USER", "") 
DB_PASS =  os.environ.get("BASE_PASS", "") 
DB_PORT = "6543"

# ─────────────────────────────────────────────────────
# CONFIG MERCADO PAGO
# ─────────────────────────────────────────────────────
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")   # Tu Access Token de MP
MP_PRECIO_SALA  = float(os.environ.get("MP_PRECIO_SALA", "6000"))  # ARS

# URLs que MP usa para redirigir al usuario tras el pago
# Cambiá esto por tu dominio real en Railway
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://mundialazo-production.up.railway.app/")


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
        FROM (
            SELECT id_pregunta, nro_pregunta, pregunta 
            FROM preguntas_partido 
            WHERE id_partido = %s 
            ORDER BY nro_pregunta 
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
        return jsonify({"error": str(e)}), 500


@app.route("/api/partidos")
def api_partidos():
    liga_nombre = request.args.get("liga_nombre", "").strip()
    limite      = int(request.args.get("limit", 10))

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
            return jsonify({"error": "No hay  cargadas para este partido"}), 404

        trivias = []
        for item in filas:
            opciones = [op["texto"] for op in item["opciones"]]
            correcta = next(
                (op["texto"] for op in item["opciones"] if op["es_correcta"]),
                opciones[0] if opciones else ""
            )
            trivias.append({
                "pregunta": item["pregunta"],
                "opciones": opciones,
                "correcta": correcta,
            })

        return jsonify({"preguntas": trivias})

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
    liga_partido = (data.get("ligaPartido") or "").strip()[:80]
    max_jugadores= min(int(data.get("maxJugadores") or 12), 12)
    codigo       = (data.get("codigo") or "").strip().upper()[:20]

    if not nombre or not id_partido or not codigo:
        return jsonify({"error": "Faltan campos obligatorios"}), 400

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
# ENDPOINTS MERCADO PAGO
# ─────────────────────────────────────────────────────

@app.route("/api/pagos/crear-preferencia", methods=["POST"])
def api_crear_preferencia():
    """
    Crea una preferencia de pago en Mercado Pago y devuelve la URL de checkout.
    Body JSON: { nombre, idPartido, labelPartido, maxJugadores, codigo }
    """
    if not MP_ACCESS_TOKEN:
        return jsonify({"error": "MP_ACCESS_TOKEN no configurado en el servidor"}), 500

    data         = request.get_json(force=True)
    nombre       = (data.get("nombre") or "").strip()[:40]
    id_partido   = str(data.get("idPartido") or "").strip()
    label_partido= (data.get("labelPartido") or "").strip()[:120]
    max_jugadores= min(int(data.get("maxJugadores") or 6), 12)
    codigo       = (data.get("codigo") or "").strip().upper()[:20]

    if not nombre or not id_partido or not codigo:
        return jsonify({"error": "Faltan campos obligatorios"}), 400

    # Construimos la preferencia de pago
    preferencia = {
        "items": [
            {
                "id":          f"sala_{codigo}",
                "title":       f"Sala Golazo IA — {nombre}",
                "description": f"Partido: {label_partido} | Jugadores: {max_jugadores}",
                "quantity":    1,
                "unit_price":  MP_PRECIO_SALA,
                "currency_id": "ARS",
            }
        ],
        # MP redirige al usuario a estas URLs según el resultado del pago
        "back_urls": {
            "success": f"{APP_BASE_URL}/pago/exito?codigo={codigo}",
            "failure": f"{APP_BASE_URL}/pago/fallo?codigo={codigo}",
            "pending": f"{APP_BASE_URL}/pago/pendiente?codigo={codigo}",
        },
        "auto_return": "approved",   # Redirige automáticamente solo si el pago fue aprobado
        # Metadata que va a llegar al webhook
        "metadata": {
            "codigo":        codigo,
            "nombre":        nombre,
            "id_partido":    id_partido,
            "label_partido": label_partido,
            "max_jugadores": max_jugadores,
        },
        # URL que MP llama cuando el pago cambia de estado (webhook)
        "notification_url": f"{APP_BASE_URL}/api/pagos/webhook",
        "statement_descriptor": "GOLAZO IA",
        "external_reference": codigo,   # Usamos el código de sala como referencia
    }

    try:
        resp = http_req.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers={
                "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
                "Content-Type":  "application/json",
            },
            json=preferencia,
            timeout=10,
        )
        resp.raise_for_status()
        pref_data = resp.json()

        return jsonify({
            "preference_id": pref_data["id"],
            "init_point":    pref_data["init_point"],       # URL producción
            "sandbox_init_point": pref_data.get("sandbox_init_point"),  # URL sandbox/test
        })

    except http_req.exceptions.RequestException as e:
        return jsonify({"error": f"Error al crear preferencia MP: {str(e)}"}), 502


@app.route("/api/pagos/webhook", methods=["POST"])
def api_webhook_mp():
    """
    Webhook de Mercado Pago: MP llama a este endpoint cuando el pago cambia de estado.
    Cuando el pago está 'approved', crea la sala automáticamente.
    """
    # MP puede enviar la notificación como query param o como JSON body
    topic   = request.args.get("topic") or request.args.get("type")
    data_id = request.args.get("data.id") or request.args.get("id")

    # También puede venir en el body JSON (formato IPN moderno)
    body = request.get_json(silent=True) or {}
    if not topic:
        topic   = body.get("type")
        data_id = (body.get("data") or {}).get("id")

    # Solo nos interesan las notificaciones de pagos
    if topic not in ("payment", "merchant_order"):
        return jsonify({"ok": True}), 200

    if not data_id or not MP_ACCESS_TOKEN:
        return jsonify({"ok": True}), 200

    try:
        # Consultamos el pago a la API de MP para obtener su estado real
        resp = http_req.get(
            f"https://api.mercadopago.com/v1/payments/{data_id}",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
            timeout=10,
        )
        resp.raise_for_status()
        pago = resp.json()

        status = pago.get("status")
        meta   = pago.get("metadata") or {}

        # Solo creamos la sala si el pago fue aprobado
        if status == "approved":
            codigo        = (pago.get("external_reference") or meta.get("codigo") or "").upper()
            nombre        = meta.get("nombre", "Sala Golazo")
            id_partido    = meta.get("id_partido", "")
            label_partido = meta.get("label_partido", "")
            max_jugadores = int(meta.get("max_jugadores") or 6)

            if codigo and id_partido:
                conn   = conectar()
                limpiar_salas_viejas(conn)
                cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cursor.execute("""
                    INSERT INTO salas (codigo, nombre, id_partido, label_partido, max_jugadores, estado, creada_en, abierta_en)
                    VALUES (%s, %s, %s, %s, %s, 'abierta', NOW(), NOW())
                    ON CONFLICT (codigo) DO NOTHING;
                """, (codigo, nombre, id_partido, label_partido, max_jugadores))
                conn.commit()
                cursor.close()
                conn.close()

    except Exception as e:
        # Logueamos el error pero devolvemos 200 para que MP no reintente infinitamente
        print(f"[webhook MP] Error: {e}")

    # MP espera siempre un 200 OK
    return jsonify({"ok": True}), 200


@app.route("/pago/exito")
def pago_exito():
    """MP redirige acá al usuario cuando el pago fue aprobado."""
    codigo = request.args.get("codigo", "").upper()
    # Redirigimos al frontend con el código de sala en la URL
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta http-equiv="refresh" content="2;url=/?sala={codigo}&pago=ok">
</head><body style="background:#0b0f19;color:#fff;font-family:system-ui;text-align:center;padding:60px;">
<h2>✅ ¡Pago aprobado!</h2>
<p>Redirigiendo a tu sala <strong>{codigo}</strong>...</p>
</body></html>"""


@app.route("/pago/fallo")
def pago_fallo():
    codigo = request.args.get("codigo", "").upper()
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta http-equiv="refresh" content="3;url=/">
</head><body style="background:#0b0f19;color:#fff;font-family:system-ui;text-align:center;padding:60px;">
<h2>❌ El pago no fue completado</h2>
<p>Podés intentarlo de nuevo desde la app.</p>
</body></html>"""


@app.route("/pago/pendiente")
def pago_pendiente():
    codigo = request.args.get("codigo", "").upper()
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta http-equiv="refresh" content="4;url=/">
</head><body style="background:#0b0f19;color:#fff;font-family:system-ui;text-align:center;padding:60px;">
<h2>⏳ Pago pendiente</h2>
<p>Tu pago está siendo procesado. Cuando se apruebe, tu sala <strong>{codigo}</strong> se creará automáticamente.</p>
</body></html>"""


# ─────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
