#!/usr/bin/env python3
"""
Actualiza data.json con los indicadores de Banco de México (SIE).

- Consulta las series en la API del SIE (necesita BANXICO_TOKEN).
- Toma el último dato de cada serie y el último del mes anterior para calcular la variación.
- Si existe ANTHROPIC_API_KEY, pide a Claude los subtítulos; si no, usa textos de plantilla.
- Las cifras de pantalla salen siempre de Banxico. Si Claude escribe una cifra que no
  coincide con los datos, se descarta su texto y se usa la plantilla.

Sin dependencias externas: solo la librería estándar de Python 3.9+.
"""
import json, os, re, sys, urllib.request, urllib.error
from datetime import date, datetime, timedelta, timezone

# IDs de series del SIE. Compruébalos contra los que usa tu microapp.
SERIES = {
    "usd":      "SF43718",  # Tipo de cambio FIX, pesos por dólar
    "eur":      "SF46410",  # Tipo de cambio pesos por euro
    "objetivo": "SF61745",  # Tasa objetivo
   "cetes28":  "SF60633",  # CETES 28 días (misma serie que la microapp)
   "tiie91":   "SF60649",  # TIIE 91 días (misma serie que la microapp)
}
ETIQUETAS = {"objetivo": "OBJETIVO", "cetes28": "CETES 28", "tiie91": "TIIE 91"}
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
FUENTE = ("Fuente: Banco de México, SIE (www.banxico.org.mx). Información de carácter "
          "informativo; no constituye recomendación de inversión.")
SIE = "https://www.banxico.org.mx/SieAPIRest/service/v1/series/{ids}/datos/{ini}/{fin}"
SALIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data.json")
MODELO = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")


def http_json(url, headers=None, body=None, timeout=60):
    req = urllib.request.Request(url, data=body, headers=headers or {},
                                 method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def num(s):
    s = (s or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None  # "N/E" u otro valor no disponible


def fecha(s):
    return datetime.strptime(s, "%d/%m/%Y").date()


def consultar_sie(token, hoy):
    ini = (hoy.replace(day=1) - timedelta(days=45)).isoformat()
    url = SIE.format(ids=",".join(SERIES.values()), ini=ini, fin=hoy.isoformat())
    return http_json(url, {"Bmx-Token": token, "Accept": "application/json"})


def procesar(respuesta, hoy):
    por_id = {s["idSerie"]: s for s in respuesta["bmx"]["series"]}
    inicio_mes = hoy.replace(day=1)
    out, titulos = {}, {}
    for clave, sid in SERIES.items():
        serie = por_id.get(sid)
        if not serie:
            raise SystemExit(f"La serie {sid} ({clave}) no vino en la respuesta del SIE")
        titulos[clave] = serie.get("titulo", "")
        datos = [(fecha(d["fecha"]), num(d["dato"])) for d in serie.get("datos", [])]
        datos = sorted([d for d in datos if d[1] is not None])
        if not datos:
            raise SystemExit(f"La serie {sid} ({clave}) no tiene datos en el periodo")
        ultimo = datos[-1]
        previos = [d for d in datos if d[0] < inicio_mes]
        # Si el último dato ya es del mes anterior (primeros días del mes), compara con el mes previo a ese.
        if ultimo[0] < inicio_mes:
            ref_mes = ultimo[0].replace(day=1)
            previos = [d for d in datos if d[0] < ref_mes]
        prev = previos[-1] if previos else None
        out[clave] = {"v": ultimo[1], "fecha": ultimo[0].strftime("%d/%m/%Y"),
                      "prev": prev[1] if prev else None,
                      "prev_fecha": prev[0].strftime("%d/%m/%Y") if prev else None}
    return out, titulos


def f4(n): return f"{n:,.4f}"
def f2(n): return f"{n:,.2f}"


def texto_cambio(v, p, p_fecha, dec, unidad):
    if p is None:
        return ""
    mes = MESES[fecha(p_fecha).month - 1]
    d = round(v - p, dec)
    if d == 0:
        return f" Sin cambio frente al cierre de {mes}."
    verbo = "más" if d > 0 else "menos"
    return f" Son {abs(d):.{dec}f} {unidad} {verbo} que al cierre de {mes}."


def subtitulos_plantilla(d):
    u, e = d["usd"], d["eur"]
    o, c, t = d["objetivo"], d["cetes28"], d["tiie91"]
    return [
        f"Estos son los indicadores de Banco de México al {u['fecha']}.",
        f"El dólar FIX se ubicó en ${f4(u['v'])} pesos." + texto_cambio(u["v"], u["prev"], u["prev_fecha"], 4, "pesos"),
        f"El euro cotizó en ${f4(e['v'])} pesos." + texto_cambio(e["v"], e["prev"], e["prev_fecha"], 4, "pesos"),
        f"La tasa objetivo está en {f2(o['v'])} %; CETES a 28 días en {f2(c['v'])} % "
        f"y la TIIE a 91 días en {f2(t['v'])} %.",
        "Resumen del día. Fuente: Banxico, Sistema de Información Económica.",
    ]


def cifras_permitidas(d):
    ok = set()
    for k in d.values():
        for x in (k["v"], k["prev"]):
            if x is None:
                continue
            for dec in (2, 4):
                ok.add(f"{x:.{dec}f}")
        if k["prev"] is not None:
            for dec in (2, 4):
                ok.add(f"{abs(k['v'] - k['prev']):.{dec}f}")
    for k in d.values():  # fechas como números sueltos (día, año)
        for f in (k["fecha"], k["prev_fecha"]):
            if f:
                ok.update(f.split("/"))
                ok.add(f)
    return ok


def validar(textos, d):
    ok = cifras_permitidas(d) | {"28", "91"}
    for t in textos:
        for n in re.findall(r"\d+(?:[.,]\d+)?(?:/\d+/\d+)?", t):
            if n.replace(",", "") not in ok:
                return False, n
    return True, None


def subtitulos_claude(d, api_key):
    datos = {k: {"valor": v["v"], "fecha": v["fecha"], "cierre_mes_anterior": v["prev"],
                 "fecha_cierre_mes_anterior": v["prev_fecha"]} for k, v in d.items()}
    prompt = (
        "Eres redactor de un widget de intranet para el IMEF (Instituto Mexicano de Ejecutivos de "
        "Finanzas). Redacta 5 subtítulos breves en español de México, uno por escena:\n"
        "1) introducción con la fecha del dato, 2) dólar FIX, 3) euro, 4) las tres tasas, 5) cierre.\n"
        "Reglas: máximo 160 caracteres cada uno; tono informativo y neutral; usa solo cifras que "
        "aparecen en los datos, copiadas tal cual (4 decimales para tipos de cambio, 2 para tasas); "
        "puedes decir si algo subió o bajó frente al cierre del mes anterior; no hagas predicciones "
        "ni recomendaciones de inversión.\n"
        f"Datos (Banxico SIE):\n{json.dumps(datos, ensure_ascii=False)}\n\n"
        'Responde solo con JSON: {"subtitulos": ["...", "...", "...", "...", "..."]}'
    )
    body = json.dumps({"model": MODELO, "max_tokens": 800,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    r = http_json("https://api.anthropic.com/v1/messages",
                  {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}, body)
    texto = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", texto, re.S)
    subs = json.loads(m.group(0))["subtitulos"]
    if len(subs) != 5 or not all(isinstance(s, str) and s.strip() for s in subs):
        raise ValueError("formato inesperado")
    return [s.strip() for s in subs]


def main():
    token = os.environ.get("BANXICO_TOKEN")
    if not token:
        sys.exit("Falta BANXICO_TOKEN")
    hoy = datetime.now(timezone(timedelta(hours=-6))).date()  # hora de CDMX
    d, titulos = procesar(consultar_sie(token, hoy), hoy)
    for k, t in titulos.items():
        print(f"{SERIES[k]} {k}: {t} -> {d[k]['v']} ({d[k]['fecha']}), mes anterior {d[k]['prev']}")

    subs, origen = subtitulos_plantilla(d), "plantilla"
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            propuesta = subtitulos_claude(d, api_key)
            valido, cifra = validar(propuesta, d)
            if valido:
                subs, origen = propuesta, "claude"
            else:
                print(f"Claude mencionó una cifra que no está en los datos ({cifra}); uso la plantilla")
        except Exception as ex:  # nunca dejamos la página sin actualizar por un fallo de redacción
            print(f"No se pudo usar Claude ({ex}); uso la plantilla")

    salida = {
        "fecha": d["usd"]["fecha"],
        "usd": d["usd"]["v"], "eur": d["eur"]["v"],
        "prev": {"usd": d["usd"]["prev"], "eur": d["eur"]["prev"],
                 "fecha": d["usd"]["prev_fecha"]},
        "tasas": [{"k": ETIQUETAS[k], "v": d[k]["v"], "fecha": d[k]["fecha"]}
                  for k in ("objetivo", "cetes28", "tiie91")],
        "subtitulos": subs,
        "origen_subtitulos": origen,
        "fuente": FUENTE,
        "generado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=2)
    print(f"data.json actualizado ({origen})")


if __name__ == "__main__":
    main()
