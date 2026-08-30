import os
import re
import json
import asyncio
import logging
import html
import hashlib
import aiohttp
from datetime import datetime
from aiohttp import web
import aiohttp_cors
import psycopg2
from psycopg2 import pool
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, BotCommand, WebAppInfo
)
from aiogram.enums import ParseMode
from aiogram.filters import Command
from google import genai
from google.genai import types
from dotenv import load_dotenv
import urllib.parse

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8080")

os.makedirs("templates", exist_ok=True)
os.makedirs("static", exist_ok=True)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)

logging.basicConfig(level=logging.INFO)

# --- CONNECTION POOLING PER POSTGRESQL (VELOCIZZA LE QUERY) ---
db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)

def get_db_connection():
    return db_pool.getconn()

def release_db_connection(conn):
    db_pool.putconn(conn)

def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS transazioni (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    data_ora TEXT NOT NULL,
                    tipo_movimento TEXT DEFAULT 'USCITA',
                    tipo_inserimento TEXT NOT NULL,
                    esercente TEXT,
                    categoria_esercente TEXT,
                    citta TEXT,
                    piva TEXT,
                    totale NUMERIC(10, 2) NOT NULL,
                    totale_sconti NUMERIC(10, 2) DEFAULT 0.0,
                    metodo_pagamento TEXT DEFAULT 'NON_SPECIFICATO',
                    numero_documento TEXT,
                    matricola_rt TEXT,
                    image_hash TEXT,
                    note TEXT,
                    data_creazione TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS voci_spesa (
                    id BIGSERIAL PRIMARY KEY,
                    transazione_id BIGINT REFERENCES transazioni(id) ON DELETE CASCADE,
                    nome TEXT NOT NULL,
                    quantita NUMERIC(10, 2) DEFAULT 1.0,
                    prezzo_unitario NUMERIC(10, 2),
                    prezzo_totale NUMERIC(10, 2) NOT NULL,
                    sconto NUMERIC(10, 2) DEFAULT 0.0,
                    macro_categoria TEXT NOT NULL,
                    sotto_categoria TEXT,
                    aliquota_iva NUMERIC(5, 2)
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS debiti_crediti (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    persona TEXT NOT NULL,
                    tipo TEXT NOT NULL,
                    importo NUMERIC(10, 2) NOT NULL,
                    descrizione TEXT,
                    data_ora TEXT NOT NULL,
                    data_creazione TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_transazioni_user_data ON transazioni(user_id, data_ora);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_debiti_user ON debiti_crediti(user_id);")
            conn.commit()
    finally:
        release_db_connection(conn)

init_db()

# --- PROMPT AI PER SCONTRINI ---
RECEIPT_PROMPT = """
Sei un assistente contabile personale per l'estrazione analitica di spese da scontrini commerciali.
Restituisci ESCLUSIVAMENTE un JSON valido conforme a questo schema:
{
  "is_receipt": boolean,
  "esercente": {
    "nome": "Nome negozio/insegna o 'Sconosciuto'",
    "categoria": "SUPERMERCATO" | "TABACCHERIA" | "FARMACIA" | "RISTORANTE_BAR" | "ELETTRONICA" | "ABBIGLIAMENTO" | "CASA_BRICO" | "TRASPORTI" | "ALTRO",
    "citta": "Città se visibile o null",
    "piva": "P.IVA/CF esercente se visibile o null"
  },
  "fiscale": {
    "data_ora": "YYYY-MM-DD HH:MM oppure null",
    "numero_documento": "Numero documento commerciale o null",
    "matricola_rt": "Matricola RT registratore o null"
  },
  "pagamento": {
    "totale": float,
    "totale_sconti": float,
    "metodo": "CARTA" | "CONTANTI" | "BUONI_PASTO" | "ALTRO" | "NON_SPECIFICATO"
  },
  "articoli": [
    {
      "nome": "Nome articolo normalizzato",
      "quantita": float,
      "prezzo_unitario": float,
      "prezzo_totale": float,
      "sconto": float,
      "macro_categoria": "FUMO" | "ALIMENTARI" | "CASA_PULIZIA" | "SALUTE_CURA" | "RISTORAZIONE" | "SVAGO_CULTURA" | "TECNOLOGIA" | "ABBIGLIAMENTO" | "TRASPORTI" | "ALTRO",
      "sotto_categoria": "es. Sigarette, Terea, Tabacco, Filtri, Cartine, Snus, Latticini, Carne, Bevande Alcoliche, ecc."
    }
  ]
}

Regole:
1. Se non è uno scontrino: "is_receipt": false.
2. Per FUMO: macro "FUMO", sotto-categorie (Sigarette, Terea, Tabacco, Filtri, Cartine, Snus).
3. Per BEVANDE: 'Bevande Alcoliche' o 'Bevande Analcoliche'.
4. Gestisci moltiplicatori (es. '3 x 1.50' o 'Pz. 3').
"""

NLP_EXPENSE_PROMPT = """
Analizza il messaggio dell'utente che descrive una transazione finanziaria.
Restituisci ESCLUSIVAMENTE un JSON valido conforme a questo schema:
{
  "is_valid": boolean,
  "tipo_movimento": "USCITA" | "ENTRATA" | "HO_OFFERTO" | "MI_HA_OFFERTO",
  "persona_coinvolta": "Nome della persona o null",
  "descrizione": "Nome specifico dell'articolo o movimento",
  "importo": float,
  "macro_categoria": "ENTRATE" | "FUMO" | "ALIMENTARI" | "RISTORAZIONE" | "CASA_PULIZIA" | "SALUTE_CURA" | "SVAGO_CULTURA" | "TECNOLOGIA" | "ABBIGLIAMENTO" | "TRASPORTI" | "ALTRO",
  "sotto_categoria": "Sotto-categoria precisa",
  "metodo_pagamento": "CARTA" | "CONTANTI" | "BONIFICO" | "NON_SPECIFICATO",
  "esercente": "Nome locale o fonte se specificato"
}
"""

# --- PARSER LOCALE VELOCE ---
def parse_local_text(text: str):
    text_clean = text.strip()
    match_num = re.search(r'(\d+[\.,]?\d*)', text_clean)
    if not match_num:
        return None

    importo_str = match_num.group(1).replace(',', '.')
    try:
        importo = float(importo_str)
    except ValueError:
        return None

    tokens = re.sub(r'(\d+[\.,]?\d*)', '', text_clean).strip()
    metodo = "NON_SPECIFICATO"
    if re.search(r'\b(carta|bancomat|pos)\b', tokens, re.I):
        metodo = "CARTA"
        tokens = re.sub(r'\b(carta|bancomat|pos)\b', '', tokens, flags=re.I).strip()
    elif re.search(r'\b(contanti|cash)\b', tokens, re.I):
        metodo = "CONTANTI"
        tokens = re.sub(r'\b(contanti|cash)\b', '', tokens, flags=re.I).strip()
    elif re.search(r'\b(bonifico)\b', tokens, re.I):
        metodo = "BONIFICO"
        tokens = re.sub(r'\b(bonifico)\b', '', tokens, flags=re.I).strip()

    tokens_lower = tokens.lower()

    # 1. CASO: HO OFFERTO A QUALCUNO
    offerto_match = re.search(r'(?:offert[oaei]|pagat[oaei]|offro|pago)\s+(?:a\s+)?([a-zA-Zàèéìòù]+)', tokens, re.I)
    if offerto_match:
        persona = offerto_match.group(1).capitalize()
        desc_clean = re.sub(r'(?:offert[oaei]|pagat[oaei]|offro|pago)\s+(?:a\s+)?[a-zA-Zàèéìòù]+', '', tokens, flags=re.I).strip()
        desc = desc_clean.capitalize() or "Offerta"
        return {
            "is_valid": True, "tipo_movimento": "HO_OFFERTO", "persona_coinvolta": persona,
            "descrizione": desc, "importo": importo, "macro_categoria": "RISTORAZIONE",
            "sotto_categoria": "Offerte Amici", "metodo_pagamento": metodo, "esercente": "Manuale"
        }

    # 2. CASO: QUALCUNO OFFRE A ME
    ricevuto_match = re.search(r'([a-zA-Zàèéìòù]+)\s+mi\s+(?:offre|ha offerto|ha pagato|paga|offerto|pagato)', tokens, re.I)
    if "mi offre" in tokens_lower or "mi ha pagato" in tokens_lower or "mi paga" in tokens_lower or ricevuto_match:
        persona = ricevuto_match.group(1).capitalize() if ricevuto_match else "Amico"
        desc_clean = re.sub(r'[a-zA-Zàèéìòù]+\s+mi\s+(?:offre|ha offerto|ha pagato|paga|offerto|pagato)', '', tokens, flags=re.I).strip()
        desc = desc_clean.capitalize() or "Ricevuto"
        return {
            "is_valid": True, "tipo_movimento": "MI_HA_OFFERTO", "persona_coinvolta": persona,
            "descrizione": desc, "importo": importo, "macro_categoria": "RISTORAZIONE",
            "sotto_categoria": "Ricevuti Amici", "metodo_pagamento": metodo, "esercente": "Manuale"
        }

    # 3. CASO: ENTRATE
    if any(k in tokens_lower for k in ["stipendio", "salario", "paga"]):
        return {
            "is_valid": True, "tipo_movimento": "ENTRATA", "persona_coinvolta": None,
            "descrizione": "Stipendio", "importo": importo, "macro_categoria": "ENTRATE",
            "sotto_categoria": "Stipendio", "metodo_pagamento": metodo if metodo != "NON_SPECIFICATO" else "BONIFICO",
            "esercente": "Lavoro"
        }
    if any(k in tokens_lower for k in ["mamma", "papa", "papà", "genitori", "famiglia"]):
        return {
            "is_valid": True, "tipo_movimento": "ENTRATA", "persona_coinvolta": None,
            "descrizione": "Soldi da Famiglia", "importo": importo, "macro_categoria": "ENTRATE",
            "sotto_categoria": "Famiglia", "metodo_pagamento": metodo, "esercente": "Genitori"
        }
    if "regalo" in tokens_lower:
        return {
            "is_valid": True, "tipo_movimento": "ENTRATA", "persona_coinvolta": None,
            "descrizione": "Regalo", "importo": importo, "macro_categoria": "ENTRATE",
            "sotto_categoria": "Regali", "metodo_pagamento": metodo, "esercente": "Regalo"
        }

    # 4. CASO: USCITE NORMALI
    macro, sotto = "ALTRO", "Altro"
    desc = tokens.capitalize() or "Spesa"

    if any(k in tokens_lower for k in ["snus", "velo", "zyn"]):
        macro, sotto = "FUMO", "Snus"
    elif any(k in tokens_lower for k in ["terea", "heets", "glo", "iqos"]):
        macro, sotto = "FUMO", "Terea"
    elif any(k in tokens_lower for k in ["sigarett", "marlboro", "camel", "winston"]):
        macro, sotto = "FUMO", "Sigarette"
    elif any(k in tokens_lower for k in ["cartin", "filtr", "tabacco"]):
        macro, sotto = "FUMO", "Tabacco e Accessori"
    elif any(k in tokens_lower for k in ["gin", "tonic", "peroni", "birra", "vino", "spritz", "cocktail", "amaro"]):
        macro, sotto = "RISTORAZIONE", "Bevande Alcoliche"
    elif any(k in tokens_lower for k in ["caffe", "caffè", "cappuccino", "cornetto", "pizza", "pranzo", "cena", "ristorante"]):
        macro, sotto = "RISTORAZIONE", "Pasto / Bar"
    elif any(k in tokens_lower for k in ["coca", "fanta", "acqua", "succo", "the", "tè"]):
        macro, sotto = "RISTORAZIONE", "Bevande Analcoliche"
    elif any(k in tokens_lower for k in ["benzina", "diesel", "gasolio", "rifornimento"]):
        macro, sotto = "TRASPORTI", "Carburante"

    return {
        "is_valid": True, "tipo_movimento": "USCITA", "persona_coinvolta": None,
        "descrizione": desc, "importo": importo, "macro_categoria": macro,
        "sotto_categoria": sotto, "metodo_pagamento": metodo, "esercente": "Manuale"
    }

# --- TASTIERA DI CONTROLLO (6 TASTI RAPIDI) ---
def get_main_keyboard():
    if WEBAPP_URL.startswith("https://"):
        dashboard_btn = KeyboardButton(text="📱 Apri Dashboard", web_app=WebAppInfo(url=f"{WEBAPP_URL}/"))
    else:
        dashboard_btn = KeyboardButton(text="📱 Link Dashboard")

    return ReplyKeyboardMarkup(
        keyboard=[
            [dashboard_btn, KeyboardButton(text="📊 Report Mese")],
            [KeyboardButton(text="👥 Debiti & Amici"), KeyboardButton(text="🏷️ Spese per Categoria")],
            [KeyboardButton(text="📋 Ultime Operazioni"), KeyboardButton(text="ℹ️ Guida Inserimento")]
        ],
        resize_keyboard=True,
        persistent=True
    )

# --- BOTTONE LINK DASHBOARD ---
@dp.message(Command("dashboard"))
@dp.message(F.text.in_(["📱 Link Dashboard", "📱 Apri Dashboard"]))
async def btn_link_dashboard(message: Message):
    user_id = message.from_user.id
    url = f"{WEBAPP_URL}/?user_id={user_id}"
    testo = (
        "📊 <b>Dashboard Finanziaria Interattiva</b>\n\n"
        f"Visualizza bilancio, categorie e saldi amici qui:\n"
        f"👉 <a href='{url}'>{url}</a>"
    )
    await message.reply(testo, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# --- GUIDA INSERIMENTO ---
@dp.message(Command("guida"))
@dp.message(F.text == "ℹ️ Guida Inserimento")
async def btn_guida(message: Message):
    testo = (
        "💡 <b>Guida Inserimenti Rapidi:</b>\n\n"
        "📸 <b>Scontrini:</b> Invia la foto dello scontrino per estrarre tutti gli articoli.\n\n"
        "🤝 <b>Offerte & Debiti tra Amici:</b>\n"
        "• <i>Quando offri tu:</i> <code>caffe offerto a Simone 1.50 carta</code> oppure <code>Acqua offerta a Simone 1 Arcate</code>\n"
        "• <i>Quando offrono a te:</i> <code>Simone mi offre birra 2.50</code> oppure <code>Marco mi ha pagato il pranzo 12</code>\n\n"
        "🟢 <b>Entrate & Stipendio:</b>\n"
        "• <code>Stipendio 1600 bonifico</code>\n"
        "• <code>50 mamma contanti</code>\n"
        "• <code>Regalo 100 papa</code>\n\n"
        "🔴 <b>Spese Personali:</b>\n"
        "• <code>snus 5 tabacchino</code>\n"
        "• <code>Gin tonic 6 carta</code>\n"
        "• <code>Benzina 40</code>"
    )
    await message.reply(testo, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

# --- COMANDO /START ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    testo = (
        f"👋 Ciao <b>{html.escape(message.from_user.full_name)}</b>!\n\n"
        "💡 <b>Comandi e Funzioni:</b>\n"
        "📱 Tocca <b>'Dashboard'</b> per i grafici interattivi.\n"
        "📸 Invia foto di scontrini fiscali.\n"
        "🤝 <b>Offerte ad amici:</b>\n"
        "• <code>caffe offerto a Simone 1.50 carta</code>\n"
        "• <code>Simone mi offre birra 2.50</code>\n"
        "💸 <b>Spese e Accrediti:</b> <code>snus 5</code>, <code>Stipendio 1600</code>."
    )
    await message.reply(testo, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

# --- REPORT MENSILE ---
@dp.message(Command("report"))
@dp.message(F.text == "📊 Report Mese")
async def btn_report_mese(message: Message):
    user_id = message.from_user.id
    mese_corrente = datetime.now().strftime("%Y-%m")

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT COALESCE(SUM(totale), 0.0), COUNT(id) 
                FROM transazioni 
                WHERE user_id = %s AND data_ora LIKE %s AND tipo_movimento = 'USCITA'
            """, (user_id, f"{mese_corrente}%"))
            row_uscite = cursor.fetchone()
            totale_uscite = float(row_uscite[0])
            num_uscite = row_uscite[1]

            cursor.execute("""
                SELECT COALESCE(SUM(totale), 0.0), COUNT(id) 
                FROM transazioni 
                WHERE user_id = %s AND data_ora LIKE %s AND tipo_movimento = 'ENTRATA'
            """, (user_id, f"{mese_corrente}%"))
            row_entrate = cursor.fetchone()
            totale_entrate = float(row_entrate[0])
            num_entrate = row_entrate[1]

            saldo = totale_entrate - totale_uscite

            cursor.execute("""
                SELECT v.macro_categoria, SUM(v.prezzo_totale)
                FROM voci_spesa v
                JOIN transazioni t ON v.transazione_id = t.id
                WHERE t.user_id = %s AND t.data_ora LIKE %s AND t.tipo_movimento = 'USCITA'
                GROUP BY v.macro_categoria
                ORDER BY SUM(v.prezzo_totale) DESC
            """, (user_id, f"{mese_corrente}%"))
            macro_uscite = cursor.fetchall()
    finally:
        release_db_connection(conn)

    testo = f"📊 <b>Bilancio Finanziario - {datetime.now().strftime('%m/%Y')}</b>\n\n"
    testo += f"🟢 <b>Entrate:</b> +€{totale_entrate:.2f} (<i>{num_entrate} accrediti</i>)\n"
    testo += f"🔴 <b>Uscite:</b> -€{totale_uscite:.2f} (<i>{num_uscite} spese</i>)\n"
    
    if saldo >= 0:
        testo += f"💰 <b>Saldo Netto:</b> 🟢 <b>+€{saldo:.2f}</b> (<i>In attivo</i>)\n\n"
    else:
        testo += f"💰 <b>Saldo Netto:</b> 🔴 <b>-€{-saldo:.2f}</b> (<i>In disavanzo</i>)\n\n"

    testo += "📂 <b>Ripartizione Spese per Categoria:</b>\n"
    if not macro_uscite:
        testo += "<i>Nessuna spesa registrata questo mese.</i>"
    else:
        for cat, importo in macro_uscite:
            imp = float(importo)
            perc = (imp / totale_uscite * 100) if totale_uscite > 0 else 0
            testo += f"• <b>{cat}:</b> €{imp:.2f} (<i>{perc:.1f}%</i>)\n"

    await message.reply(testo, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

# --- DEBITI & CREDITI AMICI ---
@dp.message(Command("amici"))
@dp.message(F.text == "👥 Debiti & Amici")
async def btn_amici(message: Message):
    user_id = message.from_user.id
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT persona,
                       SUM(CASE WHEN tipo = 'HO_OFFERTO' THEN importo ELSE 0 END) AS crediti,
                       SUM(CASE WHEN tipo = 'MI_HA_OFFERTO' THEN importo ELSE 0 END) AS debiti
                FROM debiti_crediti
                WHERE user_id = %s
                GROUP BY persona
            """, (user_id,))
            rows = cursor.fetchall()
    finally:
        release_db_connection(conn)

    if not rows:
        await message.reply("Nessun debito o credito registrato con gli amici.", reply_markup=get_main_keyboard())
        return

    testo = "👥 <b>Riepilogo Offerte e Saldi Amici:</b>\n\n"
    for persona, crediti, debiti in rows:
        c = float(crediti or 0.0)
        d = float(debiti or 0.0)
        saldo = c - d
        if saldo > 0:
            testo += f"• <b>{html.escape(persona)}:</b> 🟢 ti deve <b>€{saldo:.2f}</b> (Offerti: €{c:.2f})\n"
        elif saldo < 0:
            testo += f"• <b>{html.escape(persona)}:</b> 🔴 gli devi dare <b>€{-saldo:.2f}</b> (Ricevuti: €{d:.2f})\n"
        else:
            testo += f"• <b>{html.escape(persona)}:</b> ⚪ siete in pari (€{c:.2f})\n"

    await message.reply(testo, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

# --- SPESE PER CATEGORIA ---
@dp.message(Command("categorie"))
@dp.message(F.text == "🏷️ Spese per Categoria")
async def btn_spese_categoria(message: Message):
    user_id = message.from_user.id
    mese_corrente = datetime.now().strftime("%Y-%m")

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT t.tipo_movimento, v.macro_categoria, COALESCE(v.sotto_categoria, 'Altro'), SUM(v.prezzo_totale)
                FROM voci_spesa v
                JOIN transazioni t ON v.transazione_id = t.id
                WHERE t.user_id = %s AND t.data_ora LIKE %s
                GROUP BY t.tipo_movimento, v.macro_categoria, v.sotto_categoria
                ORDER BY t.tipo_movimento DESC, v.macro_categoria ASC, SUM(v.prezzo_totale) DESC
            """, (user_id, f"{mese_corrente}%"))
            righe = cursor.fetchall()
    finally:
        release_db_connection(conn)

    if not righe:
        await message.reply("Nessun movimento registrato per questo mese.", reply_markup=get_main_keyboard())
        return

    uscite_dict = {}
    entrate_dict = {}

    for tipo, macro, sotto, tot in righe:
        target_dict = entrate_dict if tipo == "ENTRATA" else uscite_dict
        if macro not in target_dict:
            target_dict[macro] = []
        target_dict[macro].append((sotto, float(tot)))

    testo = f"🏷️ <b>Dettaglio Categorie ({datetime.now().strftime('%m/%Y')}):</b>\n\n"
    if entrate_dict:
        testo += "🟢 <b>ENTRATE ACCREDITATE:</b>\n"
        for macro, sub_list in entrate_dict.items():
            for sotto, importo in sub_list:
                testo += f"   └ <i>{html.escape(sotto)}:</i> +€{importo:.2f}\n"
        testo += "\n"

    if uscite_dict:
        testo += "🔴 <b>USCITE E SPESE:</b>\n"
        for macro, sub_list in uscite_dict.items():
            tot_macro = sum(s[1] for s in sub_list)
            testo += f"📁 <b>{macro}</b> (Totale: €{tot_macro:.2f})\n"
            for sotto, importo in sub_list:
                testo += f"   └ <i>{html.escape(sotto)}:</i> €{importo:.2f}\n"
            testo += "\n"

    if len(testo) > 4000:
        testo = testo[:3950] + "\n\n<i>...altre voci salvate!</i>"

    await message.reply(testo, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

# --- ULTIME OPERAZIONI ---
@dp.message(Command("ultime"))
@dp.message(F.text == "📋 Ultime Operazioni")
async def btn_ultime_spese(message: Message):
    user_id = message.from_user.id
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, data_ora, tipo_movimento, esercente, totale, metodo_pagamento, tipo_inserimento, note
                FROM transazioni 
                WHERE user_id = %s 
                ORDER BY id DESC LIMIT 8
            """, (user_id,))
            transazioni = cursor.fetchall()
    finally:
        release_db_connection(conn)

    if not transazioni:
        await message.reply("Nessuna operazione registrata finora.", reply_markup=get_main_keyboard())
        return

    testo = "📋 <b>Ultime 8 Operazioni:</b>\n\n"
    for t_id, data, tipo_mov, es, tot, metodo, tipo_ins, note in transazioni:
        tot_val = float(tot)
        if tipo_mov == "ENTRATA":
            icona = "🟢 <b>[ENTRATA]</b>"
            segno = "+"
        else:
            icona = "📸 <b>[SCONTRINO]</b>" if tipo_ins == "FOTO" else "🔴 <b>[USCITA]</b>"
            segno = "-"

        metodo_str = f" ({metodo})" if metodo != "NON_SPECIFICATO" else ""
        nome_es = es if es and es != "Manuale" else (note or "Movimento")
        testo += f"{icona} <b>{data}</b>\n"
        testo += f"   🏷️ {html.escape(str(nome_es))}: <b>{segno}€{tot_val:.2f}</b>{metodo_str}\n\n"

    await message.reply(testo, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

# --- GESTIONE TESTO RAPIDO NLP ---
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_nlp_text(message: Message):
    testo_utente = message.text.strip()
    user_id = message.from_user.id

    dati = parse_local_text(testo_utente)

    if not dati or not dati.get("is_valid", False):
        try:
            response = ai_client.models.generate_content(
                model='gemini-3.6-flash',
                contents=[testo_utente, NLP_EXPENSE_PROMPT],
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            dati = json.loads(response.text)
        except Exception as e:
            logging.error(f"Errore NLP Gemini: {e}")
            await message.reply("❌ Errore durante l'analisi. Riprova con un formato semplice (es. <code>snus 5 tabacchino</code>).")
            return

    if not dati or not dati.get("is_valid", False) or float(dati.get("importo", 0.0)) <= 0:
        await message.reply("❓ Non ho riconosciuto una transazione valida. Esempio: <code>caffe offerto a Simone 1.50 carta</code>", parse_mode=ParseMode.HTML)
        return

    tipo_mov = dati.get("tipo_movimento", "USCITA")
    persona = dati.get("persona_coinvolta")
    descrizione = dati.get("descrizione", "Movimento")
    importo = float(dati.get("importo", 0.0))
    macro_cat = dati.get("macro_categoria", "ALTRO")
    sotto_cat = dati.get("sotto_categoria", "Altro")
    metodo = dati.get("metodo_pagamento", "NON_SPECIFICATO")
    esercente = dati.get("esercente") or "Manuale"
    data_ora = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:

            def get_friend_net_balance(cur, uid, p_name):
                cur.execute("""
                    SELECT SUM(CASE WHEN tipo = 'HO_OFFERTO' THEN importo ELSE -importo END)
                    FROM debiti_crediti
                    WHERE user_id = %s AND persona = %s
                """, (uid, p_name))
                res = cur.fetchone()[0]
                return float(res) if res is not None else 0.0

            if tipo_mov == "HO_OFFERTO":
                nome_p = persona or "Amico"
                cursor.execute("""
                    INSERT INTO transazioni (user_id, data_ora, tipo_movimento, tipo_inserimento, esercente, totale, metodo_pagamento, note)
                    VALUES (%s, %s, 'USCITA', 'MANUALE', %s, %s, %s, %s)
                    RETURNING id
                """, (user_id, data_ora, esercente, importo, metodo, f"Offerto a {nome_p}: {descrizione}"))
                t_id = cursor.fetchone()[0]
                
                cursor.execute("""
                    INSERT INTO voci_spesa (transazione_id, nome, quantita, prezzo_unitario, prezzo_totale, macro_categoria, sotto_categoria)
                    VALUES (%s, %s, 1.0, %s, %s, %s, %s)
                """, (t_id, f"{descrizione} (offerto a {nome_p})", importo, importo, macro_cat, sotto_cat))
                
                cursor.execute("""
                    INSERT INTO debiti_crediti (user_id, persona, tipo, importo, descrizione, data_ora)
                    VALUES (%s, %s, 'HO_OFFERTO', %s, %s, %s)
                """, (user_id, nome_p, importo, descrizione, data_ora))
                conn.commit()

                saldo_att = get_friend_net_balance(cursor, user_id, nome_p)
                if saldo_att > 0:
                    saldo_str = f"🟢 <b>{html.escape(nome_p)} ti deve in totale €{saldo_att:.2f}</b>"
                elif saldo_att < 0:
                    saldo_str = f"🔴 <b>Devi ancora dare a {html.escape(nome_p)} €{-saldo_att:.2f}</b>"
                else:
                    saldo_str = f"⚪ <b>Sei perfettamente in pari con {html.escape(nome_p)}!</b>"

                await message.reply(
                    f"🤝 <b>Offerta registrata!</b>\n"
                    f"👤 <b>A chi:</b> {html.escape(nome_p)}\n"
                    f"☕ <b>Cosa:</b> {html.escape(descrizione)}\n"
                    f"💰 <b>Importo:</b> €{importo:.2f} (<i>{metodo}</i>)\n\n"
                    f"📊 <b>Saldo aggiornato:</b>\n{saldo_str}",
                    parse_mode=ParseMode.HTML
                )
                return

            elif tipo_mov == "MI_HA_OFFERTO":
                nome_p = persona or "Amico"
                cursor.execute("""
                    INSERT INTO debiti_crediti (user_id, persona, tipo, importo, descrizione, data_ora)
                    VALUES (%s, %s, 'MI_HA_OFFERTO', %s, %s, %s)
                """, (user_id, nome_p, importo, descrizione, data_ora))
                conn.commit()

                saldo_att = get_friend_net_balance(cursor, user_id, nome_p)
                if saldo_att > 0:
                    saldo_str = f"🟢 <b>{html.escape(nome_p)} ti deve ancora €{saldo_att:.2f}</b>"
                elif saldo_att < 0:
                    saldo_str = f"🔴 <b>Devi dare a {html.escape(nome_p)} in totale €{-saldo_att:.2f}</b>"
                else:
                    saldo_str = f"⚪ <b>Sei perfettamente in pari con {html.escape(nome_p)}!</b>"

                await message.reply(
                    f"🤝 <b>Debito annotato!</b>\n"
                    f"👤 <b>Da chi:</b> {html.escape(nome_p)}\n"
                    f"🍺 <b>Cosa ti ha offerto:</b> {html.escape(descrizione)}\n"
                    f"💰 <b>Valore:</b> €{importo:.2f}\n\n"
                    f"📊 <b>Saldo aggiornato:</b>\n{saldo_str}",
                    parse_mode=ParseMode.HTML
                )
                return

            cursor.execute("""
                INSERT INTO transazioni (user_id, data_ora, tipo_movimento, tipo_inserimento, esercente, totale, metodo_pagamento, note)
                VALUES (%s, %s, %s, 'MANUALE', %s, %s, %s, %s)
                RETURNING id
            """, (user_id, data_ora, tipo_mov, esercente, importo, metodo, descrizione))
            t_id = cursor.fetchone()[0]

            cursor.execute("""
                INSERT INTO voci_spesa (transazione_id, nome, quantita, prezzo_unitario, prezzo_totale, macro_categoria, sotto_categoria)
                VALUES (%s, %s, 1.0, %s, %s, %s, %s)
            """, (t_id, descrizione, importo, importo, macro_cat, sotto_cat))
            conn.commit()
    finally:
        release_db_connection(conn)

    icona = "🟢 <b>Entrata registrata!</b>" if tipo_mov == "ENTRATA" else "🔴 <b>Spesa registrata!</b>"
    await message.reply(
        f"{icona}\n"
        f"📝 <b>Articolo:</b> {html.escape(descrizione)}\n"
        f"💰 <b>Importo:</b> €{importo:.2f} ({metodo})\n"
        f"🏷️ <b>Categoria:</b> {macro_cat} ➔ <i>{html.escape(sotto_cat)}</i>",
        parse_mode=ParseMode.HTML
    )

# --- GESTIONE FOTO SCONTRINI ---
@dp.message(F.photo)
async def handle_receipt(message: Message):
    status_msg = await message.reply("⏳ <i>Analisi scontrino con Gemini 3.6 Flash...</i>", parse_mode=ParseMode.HTML)
    user_id = message.from_user.id
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        photo_bytes = await bot.download_file(file_info.file_path)
        image_data = photo_bytes.read()
        image_hash = hashlib.sha256(image_data).hexdigest()

        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, data_creazione FROM transazioni WHERE user_id = %s AND image_hash = %s", (user_id, image_hash))
                dup = cursor.fetchone()
        finally:
            release_db_connection(conn)

        if dup:
            await status_msg.edit_text(f"⚠️ Scontrino già registrato il {dup[1]}.", parse_mode=ParseMode.HTML)
            return

        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=[types.Part.from_bytes(data=image_data, mime_type='image/jpeg'), RECEIPT_PROMPT],
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        dati = json.loads(response.text)
        if not dati.get("is_receipt", False):
            await status_msg.edit_text("⚠️ Nessuno scontrino valido rilevato.", parse_mode=ParseMode.HTML)
            return

        esercente = dati.get("esercente", {})
        fiscale = dati.get("fiscale", {})
        pagamento = dati.get("pagamento", {})
        articoli = dati.get("articoli", [])

        totale = float(pagamento.get("totale", 0.0))
        data_ora = fiscale.get("data_ora") or datetime.now().strftime("%Y-%m-%d %H:%M")

        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO transazioni (user_id, data_ora, tipo_movimento, tipo_inserimento, esercente, categoria_esercente, citta, piva, totale, totale_sconti, metodo_pagamento, numero_documento, matricola_rt, image_hash)
                    VALUES (%s, %s, 'USCITA', 'FOTO', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (user_id, data_ora, esercente.get("nome"), esercente.get("categoria"), esercente.get("citta"), esercente.get("piva"), totale, float(pagamento.get("totale_sconti", 0.0)), pagamento.get("metodo", "NON_SPECIFICATO"), fiscale.get("numero_documento"), fiscale.get("matricola_rt"), image_hash))
                t_id = cursor.fetchone()[0]
                for a in articoli:
                    cursor.execute("""
                        INSERT INTO voci_spesa (transazione_id, nome, quantita, prezzo_unitario, prezzo_totale, sconto, macro_categoria, sotto_categoria)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (t_id, a.get("nome"), float(a.get("quantita", 1)), float(a.get("prezzo_unitario", 0)), float(a.get("prezzo_totale", 0)), float(a.get("sconto", 0)), a.get("macro_categoria", "ALTRO"), a.get("sotto_categoria", "Altro")))
                conn.commit()
        finally:
            release_db_connection(conn)

        await status_msg.edit_text(f"🧾 <b>Scontrino Registrato!</b>\n🏬 {html.escape(str(esercente.get('nome')))}\n💰 Totale: €{totale:.2f}\n📋 Articoli: {len(articoli)} voci.", parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.error(f"Errore scontrino: {e}")
        await status_msg.edit_text("❌ Errore lettura scontrino.")

# --- ENDPOINTS API PER LA TELEGRAM WEBAPP ---
async def web_index(request):
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")

async def web_health(request):
    return web.Response(text="OK", status=200)

async def api_data(request):
    user_id = request.query.get("user_id", "0")
    init_data = request.query.get("init_data", "")

    # Se l'user_id era 0 ma abbiamo init_data inviato dalla Mini App Telegram
    if (user_id == "0" or not user_id) and init_data:
        try:
            parsed = urllib.parse.parse_qs(init_data)
            if "user" in parsed:
                user_json = json.loads(parsed["user"][0])
                user_id = str(user_json.get("id", "0"))
        except Exception as e:
            logging.warning(f"Impossibile estrarre user da init_data: {e}")

    try:
        user_id_int = int(user_id)
    except ValueError:
        user_id_int = 0

    mese_corrente = datetime.now().strftime("%Y-%m")

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT COALESCE(SUM(totale), 0.0) 
                FROM transazioni 
                WHERE user_id = %s AND data_ora LIKE %s AND tipo_movimento = 'ENTRATA'
            """, (user_id_int, f"{mese_corrente}%"))
            inc = float(cursor.fetchone()[0])

            cursor.execute("""
                SELECT COALESCE(SUM(totale), 0.0) 
                FROM transazioni 
                WHERE user_id = %s AND data_ora LIKE %s AND tipo_movimento = 'USCITA'
            """, (user_id_int, f"{mese_corrente}%"))
            exp = float(cursor.fetchone()[0])

            cursor.execute("""
                SELECT v.macro_categoria, SUM(v.prezzo_totale)
                FROM voci_spesa v 
                JOIN transazioni t ON v.transazione_id = t.id
                WHERE t.user_id = %s AND t.data_ora LIKE %s AND t.tipo_movimento = 'USCITA'
                GROUP BY v.macro_categoria
                ORDER BY SUM(v.prezzo_totale) DESC
            """, (user_id_int, f"{mese_corrente}%"))
            cats = [{"name": r[0], "value": float(r[1])} for r in cursor.fetchall()]

            cursor.execute("""
                SELECT persona,
                       SUM(CASE WHEN tipo = 'HO_OFFERTO' THEN importo ELSE -importo END) AS saldo
                FROM debiti_crediti
                WHERE user_id = %s
                GROUP BY persona
            """, (user_id_int,))
            friends = [{"name": r[0], "balance": float(r[1] or 0.0)} for r in cursor.fetchall()]
    finally:
        release_db_connection(conn)

    return web.json_response({
        "month": datetime.now().strftime("%m/%Y"),
        "income": inc,
        "expense": exp,
        "balance": inc - exp,
        "categories": cats,
        "friends": friends
    })

# --- TASK ANTI-SLEEP IN BACKGROUND (SELF-PING OGNI 10 MINUTI) ---
async def self_ping_task():
    await asyncio.sleep(30) # Attende che il server sia completamente avviato
    if not WEBAPP_URL.startswith("https://"):
        return # Non esegue il ping se si trova in ambiente locale http://

    ping_url = f"{WEBAPP_URL}/health"
    logging.info(f"🔄 Task Anti-Sleep avviato: ping attivo su {ping_url}")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(ping_url, timeout=10) as resp:
                    if resp.status == 200:
                        logging.info("💓 Anti-Sleep Self-Ping inviato con successo (200 OK).")
            except Exception as e:
                logging.warning(f"⚠️ Errore durante Self-Ping: {e}")
            
            await asyncio.sleep(600) # Ripete ogni 10 minuti esatti (600 secondi)

# --- AVVIO CONGIUNTO BOT + WEB SERVER ---
async def main():
    await bot.delete_webhook(drop_pending_updates=True)

    app = web.Application()
    cors = aiohttp_cors.setup(app, defaults={"*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")})
    
    # Routing Pagine e Statici
    app.router.add_get("/", web_index)
    app.router.add_get("/health", web_health)
    app.router.add_static("/static/", path="static", name="static")

    # Routing API con CORS
    resource = cors.add(app.router.add_resource("/api/data"))
    cors.add(resource.add_route("GET", api_data))

    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Dashboard Web Server avviato sulla porta {port}")

    asyncio.create_task(self_ping_task())

    print("🚀 Bot Finanze Personali sincronizzato e operativo!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())