import json
import os
import requests
import psycopg2
import psycopg2.extras
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from openai import OpenAI

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Automatically enforce https protocol schema if missing
if SUPABASE_URL and not SUPABASE_URL.startswith(("http://", "https://")):
    SUPABASE_URL = f"https://{SUPABASE_URL}"

# --- PostgreSQL direct connection (DB_* variables) ---
DB_HOST = "aws-1-us-east-2.pooler.supabase.com"
DB_NAME = "postgres"
DB_USER = "postgres.vlndghikrjvxmiibbqbo"
DB_PASS = "Lif#Cari.Fuk"
DB_PORT = "6543"

def get_pg_connection():
    return psycopg2.connect(
        host=DB_HOST, database=DB_NAME,
        user=DB_USER, password=DB_PASS, port=DB_PORT,
        connect_timeout=10 
    )


def ensure_trivias_table():
    """Crea la tabla trivias si no existe."""
    ddl = """
    CREATE TABLE IF NOT EXISTS trivias (
        id               SERIAL PRIMARY KEY,
        id_partido       TEXT NOT NULL,
        liga_nombre      TEXT,
        partido_label    TEXT,
        pregunta         TEXT NOT NULL,
        opciones         JSONB NOT NULL,
        correcta         TEXT NOT NULL,
        creado_en        TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_trivias_partido ON trivias(id_partido);
    CREATE INDEX IF NOT EXISTS idx_trivias_liga    ON trivias(liga_nombre);
    """
    try:
        conn = get_pg_connection()
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[ensure_trivias_table] Error: {e}")


# Garantizar tabla al arrancar
ensure_trivias_table()

openai_client = None
if OPENAI_API_KEY:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)


def supabase_get(tabla: str, params: dict) -> list:
    url = f"{SUPABASE_URL}/rest/v1/{tabla}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    res = requests.get(url, headers=headers, params=params, timeout=10)
    res.raise_for_status()
    return res.json()


def obtener_datos_partido_por_nombre(nombre_partido: str = None):
    """Busca el partido y sus jugadores/eventos usando psycopg2 directo."""
    resultado = {"detalles": {"partido": "Buscando partido reciente..."}, "jugadores": []}

    try:
        conn = get_pg_connection()
        with conn.cursor() as cur:

            if nombre_partido:
                # Extraer tokens de búsqueda descartando palabras estructurales
                palabras = [
                    p for p in nombre_partido.replace("-", " ").replace("(", " ").replace(")", " ").split()
                    if p.isalpha() and p.lower() not in ("vs", "v", "pen")
                ]
                filtradas = [w for w in palabras if w.lower() not in ("del", "real", "atletico", "manchester", "city", "united")]
                termino = filtradas[0] if filtradas else (palabras[0] if palabras else nombre_partido)

                cur.execute(
                    """
                    SELECT * FROM partidos
                    WHERE equipo_local_nombre    ILIKE %s
                       OR equipo_visitante_nombre ILIKE %s
                    ORDER BY fecha_partido DESC
                    LIMIT 1
                    """,
                    (f"%{termino}%", f"%{termino}%"),
                )
            else:
                cur.execute("SELECT * FROM partidos ORDER BY fecha_partido DESC LIMIT 1")

            partido = cur.fetchone()

            if not partido:
                resultado["detalles"]["partido"] = "No se encontraron partidos coincidentes en la Base de Datos"
                conn.close()
                return resultado

            id_partido = partido["id_partido"]
            resultado["detalles"]["id_partido"] = id_partido
            resultado["detalles"]["partido"] = (
                f"{partido['equipo_local_nombre']} {partido['equipo_local_goles']} "
                f"- {partido['equipo_visitante_goles']} {partido['equipo_visitante_nombre']}"
            )
            resultado["detalles"]["liga"]         = partido.get("liga_nombre", "N/A")
            resultado["detalles"]["fecha"]        = str(partido.get("fecha_partido", "N/A"))
            resultado["detalles"]["ganador"]      = partido.get("ganador", "N/A")
            resultado["detalles"]["tanda_penales"] = partido.get("tanda_penales", False)

            # Jugadores
            cur.execute("SELECT * FROM jugadores_partido WHERE id_partido = %s", (id_partido,))
            jugadores_rows = cur.fetchall()

            # Eventos para calcular stats
            cur.execute("SELECT * FROM eventos_partido WHERE id_partido = %s", (id_partido,))
            eventos_rows = cur.fetchall()

        conn.close()

        stats = {}
        for ev in eventos_rows:
            jid = ev.get("id_jugador")
            if not jid:
                continue
            if jid not in stats:
                stats[jid] = {"goles": 0, "asistencias": 0, "tarjetas_amarillas": 0, "tarjetas_rojas": 0}

            tipo = (ev.get("tipo_evento") or "").lower()
            if tipo in ("goal", "gol", "penalty"):
                stats[jid]["goles"] += 1
            elif tipo in ("assist", "asistencia"):
                stats[jid]["asistencias"] += 1
            elif tipo in ("yellowcard", "tarjeta amarilla", "yellow card"):
                stats[jid]["tarjetas_amarillas"] += 1
            elif tipo in ("redcard", "tarjeta roja", "red card"):
                stats[jid]["tarjetas_rojas"] += 1

            aid = ev.get("id_asistente")
            if aid and tipo in ("goal", "gol", "penalty"):
                if aid not in stats:
                    stats[aid] = {"goles": 0, "asistencias": 0, "tarjetas_amarillas": 0, "tarjetas_rojas": 0}
                stats[aid]["asistencias"] += 1

        for jug in jugadores_rows:
            jid = jug.get("id_jugador", "")
            s = stats.get(jid, {})
            resultado["jugadores"].append({
                "nombre":            jug.get("nombre_jugador", "N/A"),
                "posicion":          jug.get("posicion", "N/A"),
                "titular":           jug.get("titular", True),
                "equipo_id":         jug.get("id_equipo", "N/A"),
                "goles":             s.get("goles", 0),
                "asistencias":       s.get("asistencias", 0),
                "tarjetas_amarillas": s.get("tarjetas_amarillas", 0),
                "tarjetas_rojas":    s.get("tarjetas_rojas", 0),
            })

    except Exception as e:
        resultado["error"] = str(e)
        resultado["detalles"]["debug"] = f"Excepcion: {type(e).__name__}: {str(e)}"

    return resultado


def obtener_eventos_con_jugador_pg(id_partido: str, limite: int = 10) -> list:
    """
    Obtiene hasta `limite` eventos del partido via psycopg2,
    enriquecidos con nombre y posición del jugador involucrado.
    """
    try:
        conn = get_pg_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ev.tipo_evento,
                    ev.minuto,
                    ev.descripcion,
                    jp.nombre_jugador,
                    jp.posicion,
                    jp.id_equipo
                FROM eventos_partido ev
                LEFT JOIN jugadores_partido jp
                       ON jp.id_jugador = ev.id_jugador
                      AND jp.id_partido = ev.id_partido
                WHERE ev.id_partido = %s
                ORDER BY ev.minuto ASC NULLS LAST
                LIMIT %s
                """,
                (id_partido, limite),
            )
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


def calcular_stats_eventos_por_jugador_pg(id_partido: str) -> list:
    """
    Calcula, vía psycopg2, la cantidad de eventos de cada tipo
    agrupados POR JUGADOR. Esto permite preguntas como:
    '¿Cuántos goles marcó X en este partido?'
    '¿Cuántas tarjetas amarillas recibió Y?'
    Devuelve solo jugadores con al menos 1 evento.
    """
    try:
        conn = get_pg_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    jp.nombre_jugador,
                    jp.posicion,
                    jp.id_equipo,
                    ev.tipo_evento,
                    COUNT(*) AS cantidad
                FROM eventos_partido ev
                JOIN jugadores_partido jp
                  ON jp.id_jugador = ev.id_jugador
                 AND jp.id_partido = ev.id_partido
                WHERE ev.id_partido = %s
                  AND ev.tipo_evento IS NOT NULL
                GROUP BY jp.nombre_jugador, jp.posicion, jp.id_equipo, ev.tipo_evento
                ORDER BY jp.nombre_jugador, ev.tipo_evento
                """,
                (id_partido,),
            )
            rows = cur.fetchall()
        conn.close()

        # Pivotear: { jugador -> { tipo_evento: cantidad, ... } }
        pivot = {}
        for r in rows:
            nombre = r["nombre_jugador"] or "N/A"
            if nombre not in pivot:
                pivot[nombre] = {
                    "nombre_jugador": nombre,
                    "posicion": r["posicion"],
                    "id_equipo": r["id_equipo"],
                    "eventos_por_tipo": {},
                }
            pivot[nombre]["eventos_por_tipo"][r["tipo_evento"]] = int(r["cantidad"])

        return list(pivot.values())
    except Exception as e:
        return [{"error": str(e)}]


def guardar_trivias_pg(id_partido: str, liga_nombre: str, partido_label: str, preguntas: list):
    """Inserta las preguntas generadas en la tabla trivias."""
    try:
        conn = get_pg_connection()
        with conn.cursor() as cur:
            for p in preguntas:
                cur.execute(
                    """
                    INSERT INTO trivias (id_partido, liga_nombre, partido_label, pregunta, opciones, correcta)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        id_partido,
                        liga_nombre,
                        partido_label,
                        p.get("pregunta", ""),
                        json.dumps(p.get("opciones", []), ensure_ascii=False),
                        p.get("correcta", ""),
                    ),
                )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[guardar_trivias_pg] Error: {e}")


@app.get("/", response_class=HTMLResponse)
async def root():
    ruta_html = os.path.join(os.path.dirname(__file__), "index.html")
    if not os.path.exists(ruta_html):
        return HTMLResponse(
            content="<h2>Error: No se encontró el archivo index.html en el servidor.</h2>",
            status_code=404
        )
    with open(ruta_html, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/test")
async def probar_apis(partido_nombre: str = None):
    datos = obtener_datos_partido_por_nombre(partido_nombre)
    openai_res = {"status": "No configurado"}

    if openai_client:
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "Hola"}]
            )
            openai_res = {"status": 200, "body": response.choices[0].message.content}
        except Exception as e:
            openai_res = {"error": str(e)}

    return {"openai": openai_res, "datos_futbol": datos}


@app.get("/api/partidos")
async def listar_partidos(liga_nombre: str = None, limit: int = 3):
    """
    Devuelve los últimos partidos de la BD, opcionalmente filtrados por liga.
    El frontend lo usa para poblar la pantalla de selección de partido.
    """
    try:
        conn = get_pg_connection()
        with conn.cursor() as cur:
            if liga_nombre:
                cur.execute(
                    """
                    SELECT * FROM partidos
                    WHERE liga_nombre ILIKE %s
                    ORDER BY fecha_partido DESC
                    LIMIT %s
                    """,
                    (f"%{liga_nombre}%", limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM partidos ORDER BY fecha_partido DESC LIMIT %s",
                    (limit,),
                )
            rows = cur.fetchall()
        conn.close()

        resultado = []
        for p in rows:
            resultado.append({
                "id_partido": p.get("id_partido"),
                "label": (
                    f"{p['equipo_local_nombre']} {p['equipo_local_goles']} "
                    f"- {p['equipo_visitante_goles']} {p['equipo_visitante_nombre']}"
                ),
                "fecha":   str(p.get("fecha_partido", "")),
                "liga":    p.get("liga_nombre", ""),
                "ganador": p.get("ganador", ""),
            })
        return {"partidos": resultado}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/trivias")
async def obtener_trivias(partido_nombre: str = None):
    if not openai_client:
        return {"error": "OPENAI_API_KEY no configurada"}

    datos = obtener_datos_partido_por_nombre(partido_nombre)

    if "error" in datos:
        return {"error": f"Error conectando a las tablas de Supabase: {datos['error']}"}

    info_jugadores = datos.get("jugadores", [])[:15]
    detalles       = datos.get("detalles", {})
    id_partido     = detalles.get("id_partido", partido_nombre or "desconocido")
    liga_nombre    = detalles.get("liga", "N/A")
    partido_label  = detalles.get("partido", partido_nombre or "")

    # 1) Hasta 10 eventos con jugador (cronológicos, para contexto narrativo)
    eventos_con_jugador = obtener_eventos_con_jugador_pg(id_partido, limite=10)

    # 2) Conteo de eventos POR TIPO POR JUGADOR (base de preguntas de cantidad)
    stats_eventos_jugador = calcular_stats_eventos_por_jugador_pg(id_partido)

    if not info_jugadores:
        if not partido_nombre:
            return {"error": "No se encontraron datos en Supabase y no se especificó un partido_nombre."}

        prompt_contenido = (
            f"Eres un experto en fútbol. Basándote en tus conocimientos históricos reales del partido '{partido_nombre}', "
            f"crea exactamente 10 preguntas de trivia variadas y desafiantes. "
            f"Incluye preguntas sobre cuántos eventos de un tipo tuvo cada jugador (goles, asistencias, tarjetas). "
            f"IMPORTANTE: todas las respuestas correctas deben ser 100% verídicas de la realidad de ese partido. "
            f"Formato de salida: Devuelve SOLO un objeto JSON con este formato exacto, sin texto adicional: "
            f'{{\"preguntas\": [{{\"pregunta\": \"...\", \"opciones\": [\"A\",\"B\",\"C\"], \"correcta\": \"...\"}}]}}'
        )
    else:
        prompt_contenido = (
            f"Eres un generador de trivia de fútbol. Crea exactamente 10 preguntas basándote ESTRICTAMENTE "
            f"en los datos reales de la base de datos que se te proveen. NO inventes datos.\n\n"
            f"PARTIDO: {json.dumps(detalles, ensure_ascii=False)}\n\n"
            f"JUGADORES Y ESTADÍSTICAS GLOBALES DEL PARTIDO:\n"
            f"{json.dumps(info_jugadores, ensure_ascii=False)}\n\n"
            f"EVENTOS CRONOLÓGICOS (con jugador involucrado, hasta 10):\n"
            f"{json.dumps(eventos_con_jugador, ensure_ascii=False)}\n\n"
            f"CONTEO DE EVENTOS POR TIPO POR JUGADOR (usa esto para preguntas de cantidad, "
            f"ej: '¿Cuántos goles marcó X?', '¿Cuántas tarjetas amarillas recibió Y?', "
            f"'¿Qué jugador tuvo más eventos de tipo Z?'):\n"
            f"{json.dumps(stats_eventos_jugador, ensure_ascii=False)}\n\n"
            f"INSTRUCCIONES:\n"
            f"- Incluí al menos 3 preguntas sobre cantidades de eventos por jugador (goles, asistencias, tarjetas, etc.).\n"
            f"- Variá los tipos de pregunta: resultado, minutos, posiciones, comparaciones entre jugadores.\n"
            f"- Cada pregunta tiene exactamente 3 opciones y una correcta.\n"
            f"- Formato de salida: SOLO un objeto JSON, sin texto adicional:\n"
            f'{{\"preguntas\": [{{\"pregunta\": \"...\", \"opciones\": [\"A\",\"B\",\"C\"], \"correcta\": \"...\"}}]}}'
        )

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt_contenido}],
            max_tokens=2500,
        )
        resultado = json.loads(response.choices[0].message.content)
        preguntas = resultado.get("preguntas", [])

        # Guardar en tabla trivias
        if preguntas:
            guardar_trivias_pg(id_partido, liga_nombre, partido_label, preguntas)

        return resultado
    except Exception as e:
        return {"error": f"Error procesando la respuesta o estructura JSON de OpenAI: {str(e)}"}


@app.get("/api/trivias/partido")
async def trivias_por_partido(partido_nombre: str = None, liga_nombre: str = None):
    """Devuelve las trivias guardadas en la BD filtradas por partido y/o liga."""
    try:
        conn = get_pg_connection()
        with conn.cursor() as cur:
            condiciones = []
            valores     = []

            if partido_nombre:
                condiciones.append("partido_label ILIKE %s")
                valores.append(f"%{partido_nombre}%")
            if liga_nombre:
                condiciones.append("liga_nombre ILIKE %s")
                valores.append(f"%{liga_nombre}%")

            where = ("WHERE " + " AND ".join(condiciones)) if condiciones else ""
            cur.execute(
                f"""
                SELECT id, id_partido, liga_nombre, partido_label,
                       pregunta, opciones, correcta, creado_en
                FROM trivias
                {where}
                ORDER BY creado_en DESC
                """,
                valores,
            )
            rows = cur.fetchall()
        conn.close()

        preguntas = [
            {
                "id":            r["id"],
                "id_partido":    r["id_partido"],
                "liga_nombre":   r["liga_nombre"],
                "partido_label": r["partido_label"],
                "pregunta":      r["pregunta"],
                "opciones":      r["opciones"] if isinstance(r["opciones"], list) else json.loads(r["opciones"]),
                "correcta":      r["correcta"],
                "creado_en":     str(r["creado_en"]),
            }
            for r in rows
        ]
        return {"preguntas": preguntas, "total": len(preguntas)}
    except Exception as e:
        return {"error": f"Error al consultar trivias: {str(e)}"}
