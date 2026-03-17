from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import json, os, re
from datetime import datetime

app = Flask(__name__)

# ── Configuración ─────────────────────────────────────────────────────────
TWILIO_SID    = os.environ.get("TWILIO_SID", "")
TWILIO_TOKEN  = os.environ.get("TWILIO_TOKEN", "")
TWILIO_NUMBER = os.environ.get("TWILIO_NUMBER", "whatsapp:+14155238886")  # número Twilio sandbox

# Números de WhatsApp de cada persona (formato: whatsapp:+549XXXXXXXXXX)
ADMINS = {
    os.environ.get("ADMIN_DUENO",  "whatsapp:+5491100000001"): "Vos (dueño)",
    os.environ.get("ADMIN_CINTIA", "whatsapp:+5491100000002"): "Cintia",
    os.environ.get("ADMIN_SERGIO", "whatsapp:+5491100000003"): "Sergio",
    os.environ.get("ADMIN_PATA",   "whatsapp:+5491100000004"): "Pata",
}
MOZOS = {
    os.environ.get("MOZO_DAIANA",  "whatsapp:+5491100000005"): "Daiana",
    os.environ.get("MOZO_JULIA",   "whatsapp:+5491100000006"): "Julia",
    os.environ.get("MOZO_HERNAN",  "whatsapp:+5491100000007"): "Hernan",
    os.environ.get("MOZO_LUIS",    "whatsapp:+5491100000008"): "Luis",
    os.environ.get("MOZO_BAR",     "whatsapp:+5491100000009"): "Bar",
}

# Todos los admins son también "PATA" si su key coincide
PATA_NUMBER = os.environ.get("ADMIN_PATA", "whatsapp:+5491100000004")

# ── Estado del día (en memoria, se resetea al reiniciar) ──────────────────
state = {
    "menu": {},           # key -> precio
    "stock": {},          # key -> int
    "borrados": set(),
    "agotados": set(),
    "menu_publicado": False,
    "pedidos": {},        # numero -> lista de pedidos
    "totales": {},        # numero -> total acumulado
    "reservas": [],       # lista de reservas del día
    "pending_cancel": None,  # {"numero": ..., "nombre": ..., "pedido": ..., "desc": ...}
}

def reset_state():
    state["menu"] = {}
    state["stock"] = {}
    state["borrados"] = set()
    state["agotados"] = set()
    state["menu_publicado"] = False
    state["pedidos"] = {}
    state["totales"] = {}
    state["reservas"] = []
    state["pending_cancel"] = None

def fmt(n):
    return f"${int(n):,}".replace(",", ".")

def cap(s):
    return s.capitalize()

def get_nombre(numero):
    if numero in ADMINS: return ADMINS[numero]
    if numero in MOZOS:  return MOZOS[numero]
    return numero

def is_admin(numero):
    return numero in ADMINS

def is_mozo(numero):
    return numero in MOZOS or numero in ADMINS  # admins también pueden pedir

def is_pata(numero):
    return numero == PATA_NUMBER

def send_msg(to, body):
    """Envía un mensaje proactivo (no como respuesta al webhook)."""
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(body=body, from_=TWILIO_NUMBER, to=to)
    except Exception as e:
        print(f"Error enviando a {to}: {e}")

def notify_all_admins(body, exclude=None):
    for numero in ADMINS:
        if numero != exclude:
            send_msg(numero, body)

def notify_all_mozos(body):
    for numero in MOZOS:
        send_msg(numero, body)

# ── Parser de menú libre ───────────────────────────────────────────────────
def parsear_menu_libre(text):
    lower = text.lower().strip()
    matches = list(re.finditer(r'(\d+)', lower))
    if len(matches) < 2:
        return {}
    result = {}
    for i, m in enumerate(matches):
        precio = int(m.group(1))
        if precio <= 0:
            continue
        desde = 0 if i == 0 else matches[i-1].end()
        nombre = lower[desde:m.start()].strip()
        nombre = re.sub(r'[^a-záéíóúüñ\s]', '', nombre).strip()
        if nombre:
            result[nombre] = precio
    return result

# ── Parser de ítems del menú en un pedido ────────────────────────────────
def extraer_items(text):
    lower = text.lower()
    items = []
    keys = sorted(state["menu"].keys(), key=len, reverse=True)
    for key in keys:
        if key in state["borrados"] or key in state["agotados"]:
            continue
        esc = re.escape(key)
        precio = state["menu"][key]
        m1 = re.search(r'(\d+)\s*(?:x\s*)?' + esc, lower)
        m2 = re.search(esc + r'\s+(\d+)', lower)
        m3 = re.search(r'\b(?:un[ao]?\s+)?' + esc + r's?\b', lower)
        qty, matched = 1, False
        if m1:   qty, matched = int(m1.group(1)), True
        elif m2: qty, matched = int(m2.group(1)), True
        elif m3: qty, matched = 1, True
        if matched and not any(i["key"] == key for i in items):
            items.append({"name": cap(key), "qty": qty, "sub": qty * precio, "key": key})
    return items

def extraer_hora(text):
    m = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(?:hs?|am|pm|h)\b|a\s+las\s+(\d{1,2})(?::(\d{2}))?', text, re.I)
    if not m: return None
    h = int(m.group(1) or m.group(3))
    mi = int(m.group(2) or m.group(4) or 0)
    if re.search(r'\d\s*pm', text, re.I) and h < 12: h += 12
    return f"{h:02d}:{mi:02d}"

def stock_msg(key):
    n = cap(key)
    if key in state["agotados"]:
        return f"Se agotó el {n} — no quedan más unidades. Avisale al cliente."
    if key in state["borrados"]:
        return f"Ya no queda {n} — fue retirado del menú hoy. Ofrecé otra cosa."
    return f"El {n} no está en el menú de hoy."

def menu_vertical():
    lines = [f"• {cap(k)}: {fmt(v)}" for k, v in state["menu"].items()]
    return "\n".join(lines)

def stock_vertical():
    if not state["menu"]:
        return "No hay menú publicado todavía."
    lines = []
    for k in state["menu"]:
        if k in state["agotados"]:
            lines.append(f"❌ {cap(k)}: agotado")
        elif k in state["borrados"]:
            lines.append(f"🚫 {cap(k)}: retirado")
        elif k in state["stock"]:
            lines.append(f"⚠️ {cap(k)}: quedan {state['stock'][k]}")
        else:
            lines.append(f"✅ {cap(k)}: disponible")
    return "\n".join(lines)

# ── Lógica principal ──────────────────────────────────────────────────────
RE_RESERVA  = re.compile(r'\b(reserv[oaeb]+|dej[aá]me\s+anotado)\b', re.I)
RE_CANCEL   = re.compile(r'\b(cancel[ao]|cancela|quiero\s+cancelar)\b', re.I)
RE_PRECIO_Q = re.compile(r'\b(cu[aá]nto\s+sale|cu[aá]nto\s+cuesta|precio\s+de|a\s+cu[aá]nto)\b', re.I)
RE_STOCK_Q  = re.compile(r'\b(queda[sn]?|hay|tiene[sn]?)\b.*\?', re.I)
RE_BORRAR   = re.compile(r'\b(borrar|borr[aá]|borren|sacar|sac[aá]|retir[aá]r?)\s+([a-záéíóúüñ][a-záéíóúüñ\s]*?)$', re.I)
RE_QUEDAN   = re.compile(r'\bquedan?\s+(\d+)\s+([a-záéíóúüñ][a-záéíóúüñ\s]*?)$', re.I)
RE_PRECIO_S = re.compile(r'^precio\s+([a-záéíóúüñ][a-záéíóúüñ\s]*?)\s+(\d+)$', re.I)

def procesar_mozo(numero, raw):
    nombre = get_nombre(numero)
    lower = raw.lower().strip()

    if not state["menu_publicado"]:
        return "Todavía no hay menú para hoy. Esperá que un admin lo publique."

    # Stock general
    if lower == "stock":
        return f"📋 *Stock actual*\n{stock_vertical()}"

    # Consulta stock puntual
    if RE_STOCK_Q.search(lower):
        all_keys = list(state["menu"].keys()) + list(state["borrados"]) + list(state["agotados"])
        key = next((k for k in sorted(all_keys, key=len, reverse=True) if k in lower), None)
        if not key: return "No reconocí el producto."
        if key in state["agotados"]:  return f"No, se agotó el {cap(key)}."
        if key in state["borrados"]:  return f"No, el {cap(key)} fue retirado del menú hoy."
        if key in state["stock"]:     return f"Sí, quedan *{state['stock'][key]}* {cap(key)}."
        return f"Sí, hay {cap(key)}."

    # Precio
    if RE_PRECIO_Q.search(lower) or lower.startswith("precio"):
        key = next((k for k in sorted(state["menu"].keys(), key=len, reverse=True) if k in lower), None)
        if key:
            if key in state["borrados"] or key in state["agotados"]: return stock_msg(key)
            return f"{cap(key)}: *{fmt(state['menu'][key])}*"
        return "Ese producto no está en el menú de hoy."

    # Cancelar
    if RE_CANCEL.search(lower):
        pedidos_activos = [p for p in state["pedidos"].get(numero, []) if not p.get("cancelado")]
        if not pedidos_activos:
            return "No tenés pedidos activos para cancelar."
        target = pedidos_activos[-1]
        desc = ", ".join(f"{i['qty']}x {i['name']}" for i in target["items"])
        state["pending_cancel"] = {"numero": numero, "nombre": nombre, "pedido": target, "desc": desc}
        # Notificar a todos los admins
        msg_admins = (f"⚠️ *Solicitud de cancelación*\n"
                      f"*{nombre}* quiere cancelar: {desc}\n"
                      f"Respondé *confirmo* o *rechazo*")
        notify_all_admins(msg_admins)
        return "Solicitud enviada a los admins. Esperando autorización..."

    # Bloquear cambio de precio
    if RE_PRECIO_S.match(lower):
        return "No tenés permiso para modificar precios."

    # Producto fuera de menú
    for k in list(state["borrados"]) + list(state["agotados"]):
        if k in lower:
            return stock_msg(k)

    # Parsear pedido
    items = extraer_items(raw)
    if not items:
        return 'No reconocí ningún producto del menú. Escribí "stock" para ver qué hay disponible.'

    # Verificar stock
    for it in items:
        k = it["key"]
        if k in state["stock"]:
            if it["qty"] > state["stock"][k]:
                if state["stock"][k] == 0:
                    state["agotados"].add(k)
                    return stock_msg(k)
                return f"Solo quedan *{state['stock'][k]}* {it['name']}. Pedí menos."
            state["stock"][k] -= it["qty"]
            if state["stock"][k] <= 0:
                state["agotados"].add(k)
                notify_all_admins(f"Stock agotado: *{it['name']}*. Los mozos ya no pueden pedirlo.")
                notify_all_mozos(f"⚠️ *Ya no hay {it['name']} disponible hoy.* Avisale a los clientes.")

    es_reserva = bool(RE_RESERVA.search(lower))
    hora = extraer_hora(raw) if es_reserva else None
    tipo = "reserva" if es_reserva else "confirmado"
    total = sum(i["sub"] for i in items)

    if numero not in state["pedidos"]:  state["pedidos"][numero] = []
    if numero not in state["totales"]:  state["totales"][numero] = 0
    pedido = {"items": items, "total": total, "tipo": tipo, "hora": hora, "cancelado": False}
    state["pedidos"][numero].append(pedido)
    state["totales"][numero] += total

    desc2 = " · ".join(f"{i['qty']}x {i['name']} {fmt(i['sub'])}" for i in items)
    hora_str = f" 🕐 {hora}hs" if hora else ""
    tipo_str = "Reserva" if es_reserva else "Pedido"
    acum = state["totales"][numero]

    if es_reserva and hora:
        state["reservas"].append({"nombre": nombre, "hora": hora, "desc": ", ".join(f"{i['qty']}x {i['name']}" for i in items), "total": total})
        state["reservas"].sort(key=lambda r: r["hora"])

    # Notificar a admins
    tag = "🕐 RESERVA" if es_reserva else "✅ PEDIDO"
    notify_all_admins(
        f"{tag} — *{nombre}*{hora_str}\n{desc2}\nTotal: *{fmt(total)}* · Acumulado {nombre}: *{fmt(acum)}*"
    )

    return (f"*{tipo_str} registrado{hora_str}*\n{desc2}\n"
            f"Total: *{fmt(total)}* · Acumulado tuyo: *{fmt(acum)}*")


def procesar_admin(numero, raw):
    nombre = get_nombre(numero)
    lower = raw.lower().strip()

    # Confirmar/rechazar cancelación
    if "confirmo" in lower and state["pending_cancel"]:
        pc = state["pending_cancel"]
        pc["pedido"]["cancelado"] = True
        state["totales"][pc["numero"]] = state["totales"].get(pc["numero"], 0) - pc["pedido"]["total"]
        state["pending_cancel"] = None
        send_msg(pc["numero"], f"✅ *Cancelación aprobada* por {nombre}\n{pc['desc']}")
        notify_all_admins(f"✅ Cancelación aprobada por {nombre}\n{pc['nombre']}: {pc['desc']}", exclude=numero)
        return f"Cancelación aprobada. {pc['nombre']}: {pc['desc']}"

    if "rechazo" in lower and state["pending_cancel"]:
        pc = state["pending_cancel"]
        state["pending_cancel"] = None
        send_msg(pc["numero"], f"❌ Cancelación rechazada por {nombre}. El pedido sigue activo.")
        notify_all_admins(f"❌ Cancelación rechazada por {nombre}", exclude=numero)
        return "Cancelación rechazada."

    # quedan N producto
    m = RE_QUEDAN.search(lower)
    if m:
        qty, prod = int(m.group(1)), m.group(2).strip()
        key = next((k for k in sorted(state["menu"].keys(), key=len, reverse=True) if prod in k or k in prod), None)
        if not key: return f'No encontré "{prod}" en el menú.'
        state["stock"][key] = qty
        state["agotados"].discard(key)
        state["borrados"].discard(key)
        return f"Stock actualizado: *{cap(key)} — {qty} unidades* ⚠️"

    # borrar producto → aviso en mozos
    m = RE_BORRAR.search(lower)
    if m:
        if not state["menu_publicado"]: return "No hay menú activo todavía."
        prod = m.group(2).strip()
        key = next((k for k in sorted(state["menu"].keys(), key=len, reverse=True) if prod in k or k in prod), None)
        if not key: return f'No encontré "{prod}" en el menú de hoy.'
        state["borrados"].add(key)
        notify_all_mozos(f"⚠️ *Ya no hay {cap(key)} disponible hoy.* Avisale a los clientes.")
        notify_all_admins(f"🗑️ {cap(key)} retirado del menú por {nombre}", exclude=numero)
        return f"*{cap(key)} retirado del menú.* Mozos notificados."

    # cambiar precio
    m = RE_PRECIO_S.match(raw)
    if m:
        prod, nuevo = m.group(1).strip().lower(), int(m.group(2))
        key = next((k for k in sorted(state["menu"].keys(), key=len, reverse=True) if prod in k or k in prod), None)
        if not key: return f'No encontré "{prod}" en el menú.'
        viejo = state["menu"][key]
        state["menu"][key] = nuevo
        return f"*Precio actualizado* por {nombre}\n{cap(key)}: {fmt(viejo)} → {fmt(nuevo)}"

    # ver precios
    if re.match(r'^precios?$', lower):
        if not state["menu_publicado"]: return "No hay menú publicado todavía."
        return f"*Precios actuales:*\n{menu_vertical()}"

    # resumen
    if "resumen" in lower:
        mozo_nombre = None
        for n, nom in {**ADMINS, **MOZOS}.items():
            if nom.lower() in lower:
                mozo_nombre = (n, nom)
                break
        if mozo_nombre:
            num, nom = mozo_nombre
            pedidos = [p for p in state["pedidos"].get(num, []) if not p.get("cancelado")]
            desc = " | ".join(", ".join(f"{i['qty']}x {i['name']}" for i in p["items"]) for p in pedidos) or "Sin pedidos"
            return f"*Resumen — {nom}*\n{desc}\nTotal: *{fmt(state['totales'].get(num, 0))}*"
        else:
            lines = []
            all_people = {**ADMINS, **MOZOS}
            for num, nom in all_people.items():
                t = state["totales"].get(num, 0)
                if t > 0: lines.append(f"• {nom}: {fmt(t)}")
            total_dia = sum(state["totales"].values())
            pedidos_activos = sum(len([p for p in ps if not p.get("cancelado")]) for ps in state["pedidos"].values())
            resumen = "\n".join(lines) if lines else "Sin ventas todavía"
            return f"*Resumen del día*\n{resumen}\n\n*Total: {fmt(total_dia)}* · {pedidos_activos} pedidos"

    # reservas
    if re.match(r'^reservas?$', lower):
        if not state["reservas"]: return "No hay reservas registradas."
        lines = [f"🕐 {r['hora']}hs — {r['nombre']}: {r['desc']} ({fmt(r['total'])})" for r in state["reservas"]]
        return "*Reservas del día*\n" + "\n".join(lines)

    # cierre
    if "cierre" in lower:
        total_dia = sum(state["totales"].values())
        cancelados = sum(1 for ps in state["pedidos"].values() for p in ps if p.get("cancelado"))
        all_people = {**ADMINS, **MOZOS}
        lines = [f"• {nom}: {fmt(state['totales'].get(num, 0))}" for num, nom in all_people.items() if state["totales"].get(num, 0) > 0]
        return (f"*Cierre del día*\n" + "\n".join(lines) +
                f"\n\n*Total: {fmt(total_dia)}*\nCancelados: {cancelados}")

    # reset (solo para emergencias)
    if lower == "reset dia":
        reset_state()
        return "✅ Día reiniciado. Esperando nuevo menú."

    # DETECTAR MENÚ LIBRE (2+ pares texto-número)
    parsed = parsear_menu_libre(raw)
    if len(parsed) >= 2:
        state["menu"] = parsed
        state["borrados"] = set()
        state["agotados"] = set()
        state["stock"] = {}
        state["menu_publicado"] = True
        menu_txt = menu_vertical()
        notify_all_mozos(f"📋 *Menú del día*\n{menu_txt}")
        notify_all_admins(f"✅ Menú publicado por {nombre}\n{menu_txt}", exclude=numero)
        return f"*Menú del día publicado* ✅\n{menu_txt}\nEnviado al grupo de mozos."

    return ("Comandos disponibles:\n"
            "• *resumen* · *resumen [nombre]*\n"
            "• *precio [prod] [valor]*\n"
            "• *borrar [prod]*\n"
            "• *quedan N [prod]*\n"
            "• *precios* · *reservas* · *cierre*\n"
            "• *reset dia* (reinicia todo)\n"
            "• Para publicar menú escribí: *bife 500 pizza 300 coca 200*")


# ── Webhook principal ─────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    numero = request.form.get("From", "")
    raw    = request.form.get("Body", "").strip()
    print(f">>> NUMERO: {numero} | MENSAJE: {raw}")

    if not raw:
        resp = MessagingResponse()
        return str(resp)

    if numero not in ADMINS and numero not in MOZOS:
        ADMINS[numero] = "Admin temporal"

    if is_admin(numero):
        respuesta = procesar_admin(numero, raw)
        nombre = get_nombre(numero)
        notify_all_admins(
            f"👤 *{nombre}* escribió: {raw}\n\n🤖 Bot: {respuesta}",
            exclude=numero
        )
    else:
        respuesta = procesar_mozo(numero, raw)

    resp = MessagingResponse()
    resp.message(respuesta)
    return str(resp)

@app.route("/", methods=["GET"])
def health():
    return "Bot Bar El Mostacho — activo ✅", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
