import os
import re
import json
import asyncio
import logging
import html
import hashlib
import aiohttp
import urllib.parse
from datetime import datetime
from aiohttp import web
import aiohttp_cors
import psycopg2
from psycopg2 import pool
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
)
from aiogram.enums import ParseMode
from aiogram.filters import Command
from google import genai
from google.genai import types
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8080")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_TELEGRAM_USER_ID", "0"))

os.makedirs("templates", exist_ok=True)
os.makedirs("static", exist_ok=True)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)

logging.basicConfig(level=logging.INFO)

# --- MIDDLEWARE WHITELIST (PROTEZIONE BOT PRIVATO) ---
class WhitelistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        if ALLOWED_USER_ID and event.from_user and event.from_user.id != ALLOWED_USER_ID:
            await event.answer("⛔ Accesso negato: Questo bot è privato.")
            return
        return await handler(event, data)

dp.message.middleware(WhitelistMiddleware())

# --- CONNECTION POOLING ---
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

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS collette_regali (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    nome_regalo TEXT NOT NULL,
                    destinatario TEXT,
                    totale_anticipato NUMERIC(10, 2) NOT NULL,
                    quota_singola NUMERIC(10, 2) NOT NULL,
                    partecipanti_totali TEXT[] NOT NULL,
                    partecipanti_pagati TEXT[] DEFAULT '{}',
                    completato BOOLEAN DEFAULT FALSE,
                    data_ora TEXT NOT NULL,
                    data_creazione TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_transazioni_user_data ON transazioni(user_id, data_ora);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_debiti_user ON debiti_crediti(user_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_collette_user ON collette_regali(user_id);")
            conn.commit()
    finally:
        release_db_connection(conn)

init_db()

# --- PROMPT AI ---
NLP_EXPENSE_PROMPT = """
Analizza il messaggio contabile e restituisci ESCLUSIVAMENTE un JSON valido:
{
  "is_valid": boolean,
  "tipo_movimento": "USCITA" | "ENTRATA" | "HO_OFFERTO" | "MI_HA_OFFERTO" | "ANTICIPO_COLLETTA" | "SALDO_RICEVUTO",
  "persona_coinvolta": "Nome singolo (es. Alessio) o null",
  "partecipanti": ["Nome1", "Nome2"],
  "destinatario_regalo": "A chi è destinato il regalo o null",
  "descrizione": "Descrizione sintetica del movimento",
  "importo": float,
  "macro_categoria": "ENTRATE" | "REGALI" | "FUMO" | "ALIMENTARI" | "RISTORAZIONE" | "CASA_PULIZIA" | "SALUTE_CURA" | "SVAGO_CULTURA" | "TECNOLOGIA" | "ABBIGLIAMENTO" | "TRASPORTI" | "ALTRO",
  "sotto_categoria": "Sotto-categoria precisa",
  "metodo_pagamento": "CARTA" | "CONTANTI" | "BONIFICO" | "NON_SPECIFICATO",
  "esercente": "Nome negozio/fonte o 'Manuale'"
}

Regole fondamentali di classificazione dei regali:
1. REGALO RICEVUTO: (es. 'Alessio mi ha fatto un regalo di 50', 'Regalo ricevuto 100 nonna', 'Regalo per il mio compleanno')
   -> tipo_movimento = "ENTRATA", macro_categoria = "ENTRATE", sotto_categoria = "Regali Ricevuti"
2. REGALO FATTO DA SOLO (Nessuna colletta/anticipo): (es. 'Regalo per Alessio 40', 'Comprato regalo di laurea ad Alessio 50', 'Fatto regalo ad Alessio 30')
   -> tipo_movimento = "USCITA", macro_categoria = "REGALI", sotto_categoria = "Regali Fatti", partecipanti = []
3. ANTICIPO / COLLETTA DI GRUPPO: (es. 'Anticipo 60 per regalo Marco a Luca, Simone', 'Ho anticipato 40 per regalo Alessio a Marco')
   -> SOLO se la frase contiene esplicitamente 'Anticipo'/'Ho anticipato' oppure cita altri partecipanti da cui riscuotere soldi.
   -> tipo_movimento = "ANTICIPO_COLLETTA", macro_categoria = "REGALI", partecipanti = [elenco di chi deve ridare i soldi].
4. SALDO QUOTA RICEVUTO: (es. 'Marco mi ha dato 20 per il regalo', 'Simone mi ha ridato i soldi')
   -> tipo_movimento = "SALDO_RICEVUTO".
"""

RECEIPT_PROMPT = """
Sei un assistente per l'estrazione dati da scontrini. Restituisci ESCLUSIVAMENTE un JSON valido:
{
  "is_receipt": boolean,
  "esercente": { "nome": "Nome", "categoria": "SUPERMERCATO" | "TABACCHERIA" | "FARMACIA" | "RISTORANTE_BAR" | "ALTRO", "citta": null, "piva": null },
  "fiscale": { "data_ora": "YYYY-MM-DD HH:MM", "numero_documento": null, "matricola_rt": null },
  "pagamento": { "totale": float, "totale_sconti": float, "metodo": "CARTA" | "CONTANTI" | "NON_SPECIFICATO" },
  "articoli": [ { "nome": "Item", "quantita": float, "prezzo_unitario": float, "prezzo_totale": float, "sconto": float, "macro_categoria": "FUMO" | "ALIMENTARI" | "RISTORAZIONE" | "ALTRO", "sotto_categoria": "Sub" } ]
}
"""

# --- GESTORE AI CON RETRY E FALLBACK AUTOMATICO ---
async def generate_gemini_content(contents, schema=True):
    models_to_try = ['gemini-3.6-flash', 'gemini-2.5-flash']
    config = types.GenerateContentConfig(response_mime_type="application/json") if schema else None

    for model_name in models_to_try:
        for attempt in range(2):
            try:
                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: ai_client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=config
                    )
                )
                if response and response.text:
                    return response.text
            except Exception as e:
                err_str = str(e)
                if "503" in err_str or "UNAVAILABLE" in err_str or "429" in err_str:
                    logging.warning(f"⚠️ Modello {model_name} occupato (tentativo {attempt+1}). Ritento...")
                    await asyncio.sleep(1.5)
                else:
                    logging.error(f"Errore chiamata Gemini su {model_name}: {e}")
                    break

    raise RuntimeError("I server AI sono momentaneamente saturi. Riprova tra poco.")

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

    if any(k in tokens_lower for k in ["regalo", "anticip", "offert", "offro", "pagato a", "mi offre", "mi ha dato"]):
        return None

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

    macro, sotto = "ALTRO", "Altro"
    desc = tokens.capitalize() or "Spesa"

    if any(k in tokens_lower for k in ["snus", "velo", "zyn"]):
        macro, sotto = "FUMO", "Snus"
    elif any(k in tokens_lower for k in ["terea", "heets", "glo", "iqos"]):
        macro, sotto = "FUMO", "Terea"
    elif any(k in tokens_lower for k in ["sigarett", "marlboro", "camel"]):
        macro, sotto = "FUMO", "Sigarette"
    elif any(k in tokens_lower for k in ["gin", "tonic", "birra", "vino", "spritz", "cocktail"]):
        macro, sotto = "RISTORAZIONE", "Bevande Alcoliche"
    elif any(k in tokens_lower for k in ["caffe", "caffè", "pizza", "pranzo", "cena"]):
        macro, sotto = "RISTORAZIONE", "Pasto / Bar"
    elif any(k in tokens_lower for k in ["benzina", "diesel", "gasolio"]):
        macro, sotto = "TRASPORTI", "Carburante"
    else:
        return None

    return {
        "is_valid": True, "tipo_movimento": "USCITA", "persona_coinvolta": None,
        "descrizione": desc, "importo": importo, "macro_categoria": macro,
        "sotto_categoria": sotto, "metodo_pagamento": metodo, "esercente": "Manuale"
    }

# --- TASTIERE ---
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Apri Dashboard"), KeyboardButton(text="📊 Report Mese")],
            [KeyboardButton(text="🎁 Regali & Collette"), KeyboardButton(text="👥 Debiti & Amici")],
            [KeyboardButton(text="🏷️ Spese per Categoria"), KeyboardButton(text="ℹ️ Guida Inserimento")]
        ],
        resize_keyboard=True,
        persistent=True
    )

def get_inline_dashboard(user_id: int):
    target_url = f"{WEBAPP_URL}/?user_id={user_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Apri Dashboard Mini App", web_app=WebAppInfo(url=target_url))]
        ]
    )

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.reply(
        f"👋 Ciao <b>{html.escape(message.from_user.full_name)}</b>!\n\n"
        "💡 <b>Il tuo assistente finanziario personale è pronto.</b>\n"
        "📱 Tocca <b>'📱 Apri Dashboard'</b> per i grafici interattivi.\n"
        "📸 Invia foto di scontrini fiscali o scrivi le tue spese in linguaggio naturale.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("dashboard"))
@dp.message(F.text == "📱 Apri Dashboard")
async def btn_link_dashboard(message: Message):
    user_id = message.from_user.id
    await message.reply(
        "📊 <b>Dashboard Finanziaria Personale</b>\n\nTocca qui sotto per aprire i grafici in tempo reale:",
        parse_mode=ParseMode.HTML,
        reply_markup=get_inline_dashboard(user_id)
    )

@dp.message(Command("regali"))
@dp.message(F.text == "🎁 Regali & Collette")
async def btn_regali(message: Message):
    user_id = message.from_user.id
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, nome_regalo, destinatario, totale_anticipato, quota_singola, partecipanti_totali, partecipanti_pagati, completato
                FROM collette_regali
                WHERE user_id = %s
                ORDER BY completato ASC, id DESC
                LIMIT 10
            """, (user_id,))
            collette = cursor.fetchall()
    finally:
        release_db_connection(conn)

    if not collette:
        await message.reply(
            "🎁 <b>Nessun regalo di gruppo / colletta attiva.</b>\n\n"
            "Per creare una colletta di gruppo scrivi:\n"
            "• <code>Anticipo 60 per regalo Marco a Luca, Simone, Giovanni</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )
        return

    testo = "🎁 <b>Riepilogo Collette di Gruppo:</b>\n\n"
    for c_id, nome, dest, tot, quota, part_tot, part_pag, comp in collette:
        tot_val = float(tot)
        quota_val = float(quota)
        dest_str = f" per <b>{dest}</b>" if dest else ""
        stato_icon = "✅ <i>(Completato)</i>" if comp else "⏳ <i>(In corso)</i>"

        testo += f"📦 <b>{html.escape(nome)}</b>{dest_str} - {stato_icon}\n"
        testo += f"💰 <b>Totale Anticipato:</b> €{tot_val:.2f} (Quota: €{quota_val:.2f})\n"
        testo += "👥 <b>Partecipanti:</b>\n"

        for p in part_tot:
            if p in part_pag:
                testo += f"   • {html.escape(p)}: 🟢 <b>PAGATO (€{quota_val:.2f})</b>\n"
            else:
                testo += f"   • {html.escape(p)}: 🔴 <b>DEVE DARE €{quota_val:.2f}</b>\n"
        testo += "\n"

    await message.reply(testo, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

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
                WHERE user_id = %s AND (data_ora::text LIKE %s OR data_creazione::text LIKE %s) AND tipo_movimento = 'USCITA'
            """, (user_id, f"{mese_corrente}%", f"{mese_corrente}%"))
            row_uscite = cursor.fetchone()
            totale_uscite = float(row_uscite[0] or 0.0)
            num_uscite = row_uscite[1]

            cursor.execute("""
                SELECT COALESCE(SUM(totale), 0.0), COUNT(id) 
                FROM transazioni 
                WHERE user_id = %s AND (data_ora::text LIKE %s OR data_creazione::text LIKE %s) AND tipo_movimento = 'ENTRATA'
            """, (user_id, f"{mese_corrente}%", f"{mese_corrente}%"))
            row_entrate = cursor.fetchone()
            totale_entrate = float(row_entrate[0] or 0.0)
            num_entrate = row_entrate[1]

            saldo = totale_entrate - totale_uscite

            cursor.execute("""
                SELECT v.macro_categoria, SUM(v.prezzo_totale)
                FROM voci_spesa v
                JOIN transazioni t ON v.transazione_id = t.id
                WHERE t.user_id = %s AND (t.data_ora::text LIKE %s OR t.data_creazione::text LIKE %s) AND t.tipo_movimento = 'USCITA'
                GROUP BY v.macro_categoria
                ORDER BY SUM(v.prezzo_totale) DESC
            """, (user_id, f"{mese_corrente}%", f"{mese_corrente}%"))
            macro_uscite = cursor.fetchall()
    finally:
        release_db_connection(conn)

    testo = f"📊 <b>Bilancio Finanziario - {datetime.now().strftime('%m/%Y')}</b>\n\n"
    testo += f"🟢 <b>Entrate:</b> +€{totale_entrate:.2f} (<i>{num_entrate} accrediti</i>)\n"
    testo += f"🔴 <b>Uscite:</b> -€{totale_uscite:.2f} (<i>{num_uscite} spese</i>)\n"
    testo += f"💰 <b>Saldo Netto:</b> {'🟢 +€' if saldo >= 0 else '🔴 -€'}{abs(saldo):.2f}\n\n"

    testo += "📂 <b>Ripartizione Spese per Categoria:</b>\n"
    if not macro_uscite:
        testo += "<i>Nessuna spesa registrata questo mese.</i>"
    else:
        for cat, importo in macro_uscite:
            imp = float(importo)
            perc = (imp / totale_uscite * 100) if totale_uscite > 0 else 0
            testo += f"• <b>{cat}:</b> €{imp:.2f} (<i>{perc:.1f}%</i>)\n"

    await message.reply(testo, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

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

    testo = "👥 <b>Riepilogo Saldi Amici:</b>\n\n"
    for persona, crediti, debiti in rows:
        c = float(crediti or 0.0)
        d = float(debiti or 0.0)
        saldo = c - d
        if saldo > 0:
            testo += f"• <b>{html.escape(persona)}:</b> 🟢 ti deve <b>€{saldo:.2f}</b>\n"
        elif saldo < 0:
            testo += f"• <b>{html.escape(persona)}:</b> 🔴 gli devi dare <b>€{-saldo:.2f}</b>\n"
        else:
            testo += f"• <b>{html.escape(persona)}:</b> ⚪ siete in pari\n"

    await message.reply(testo, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

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
                WHERE t.user_id = %s AND (t.data_ora::text LIKE %s OR t.data_creazione::text LIKE %s)
                GROUP BY t.tipo_movimento, v.macro_categoria, v.sotto_categoria
                ORDER BY t.tipo_movimento DESC, v.macro_categoria ASC, SUM(v.prezzo_totale) DESC
            """, (user_id, f"{mese_corrente}%", f"{mese_corrente}%"))
            righe = cursor.fetchall()
    finally:
        release_db_connection(conn)

    if not righe:
        await message.reply("Nessun movimento registrato per questo mese.", reply_markup=get_main_keyboard())
        return

    uscite_dict, entrate_dict = {}, {}
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

    await message.reply(testo, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

@dp.message(Command("guida"))
@dp.message(F.text == "ℹ️ Guida Inserimento")
async def btn_guida(message: Message):
    testo = (
        "💡 <b>Guida Inserimenti Rapidi:</b>\n\n"
        "🎁 <b>Regali e Collette:</b>\n"
        "• <i>Regalo fatto da solo:</i> <code>Regalo per Alessio 40 carta</code> ➔ Registra una tua spesa senza debiti.\n"
        "• <i>Regalo ricevuto:</i> <code>Alessio mi ha regalato 50</code> ➔ Registra un'entrata.\n"
        "• <i>Anticipo colletta:</i> <code>Anticipo 60 regalo Marco a Luca, Simone</code> ➔ Crea la colletta e segna i crediti.\n"
        "• <i>Quando ti rimborsano:</i> <code>Simone mi ha dato 20 per il regalo</code> ➔ Salda la quota.\n\n"
        "☕ <b>Offerte al bar:</b> <code>caffe offerto a Simone 1.50</code>\n"
        "🟢 <b>Entrate:</b> <code>Stipendio 1600 bonifico</code>\n"
        "🔴 <b>Spese personali:</b> <code>snus 5</code>, <code>Benzina 40</code>"
    )
    await message.reply(testo, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

# --- GESTORE NLP MESSAGGI TESTUALI ---
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_nlp_text(message: Message):
    testo_utente = message.text.strip()
    user_id = message.from_user.id

    dati = parse_local_text(testo_utente)

    if not dati or not dati.get("is_valid", False):
        try:
            response_text = await generate_gemini_content(
                contents=[testo_utente, NLP_EXPENSE_PROMPT],
                schema=True
            )
            dati = json.loads(response_text)
        except Exception as e:
            logging.error(f"Errore NLP Gemini: {e}")
            await message.reply("⏳ I server AI sono momentaneamente saturi. Riprova tra qualche istante.")
            return

    if not dati or not dati.get("is_valid", False) or float(dati.get("importo", 0.0)) <= 0:
        await message.reply("❓ Non ho riconosciuto una spesa valida. Consulta la sezione ℹ️ Guida per esempi.", parse_mode=ParseMode.HTML)
        return

    tipo_mov = dati.get("tipo_movimento", "USCITA")
    persona = dati.get("persona_coinvolta")
    partecipanti = [p.capitalize() for p in dati.get("partecipanti", []) if p]
    destinatario = dati.get("destinatario_regalo")
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

            # 1. ANTICIPO COLLETTA DI GRUPPO
            if tipo_mov == "ANTICIPO_COLLETTA" and partecipanti:
                quota_singola = round(importo / len(partecipanti), 2)
                nome_regalo = descrizione or f"Regalo {destinatario or 'Amico'}"

                cursor.execute("""
                    INSERT INTO transazioni (user_id, data_ora, tipo_movimento, tipo_inserimento, esercente, totale, metodo_pagamento, note)
                    VALUES (%s, %s, 'USCITA', 'MANUALE', %s, %s, %s, %s)
                    RETURNING id
                """, (user_id, data_ora, esercente, importo, metodo, f"Anticipo Colletta: {nome_regalo}"))
                t_id = cursor.fetchone()[0]

                cursor.execute("""
                    INSERT INTO voci_spesa (transazione_id, nome, quantita, prezzo_unitario, prezzo_totale, macro_categoria, sotto_categoria)
                    VALUES (%s, %s, 1.0, %s, %s, 'REGALI', 'Collette Anticipate')
                """, (t_id, nome_regalo, importo, importo))

                cursor.execute("""
                    INSERT INTO collette_regali (user_id, nome_regalo, destinatario, totale_anticipato, quota_singola, partecipanti_totali, partecipanti_pagati, data_ora)
                    VALUES (%s, %s, %s, %s, %s, %s, '{}', %s)
                """, (user_id, nome_regalo, destinatario, importo, quota_singola, partecipanti, data_ora))

                for p in partecipanti:
                    cursor.execute("""
                        INSERT INTO debiti_crediti (user_id, persona, tipo, importo, descrizione, data_ora)
                        VALUES (%s, %s, 'HO_OFFERTO', %s, %s, %s)
                    """, (user_id, p, quota_singola, f"Quota {nome_regalo}", data_ora))

                conn.commit()

                elenco_part = "\n".join([f"   • <b>{p}</b>: ti deve <b>€{quota_singola:.2f}</b>" for p in partecipanti])
                await message.reply(
                    f"🎁 <b>Colletta di Gruppo Registrata!</b>\n\n"
                    f"💸 <b>Uscita totale dal tuo conto:</b> -€{importo:.2f} ({metodo})\n"
                    f"📦 <b>Regalo:</b> {html.escape(nome_regalo)}\n\n"
                    f"👥 <b>Quote da riscuotere ({len(partecipanti)} partecipanti):</b>\n{elenco_part}",
                    parse_mode=ParseMode.HTML
                )
                return

            # 2. SALDO QUOTA RICEVUTA
            elif tipo_mov == "SALDO_RICEVUTO":
                nome_p = persona.capitalize() if persona else "Amico"

                cursor.execute("""
                    INSERT INTO transazioni (user_id, data_ora, tipo_movimento, tipo_inserimento, esercente, totale, metodo_pagamento, note)
                    VALUES (%s, %s, 'ENTRATA', 'MANUALE', 'Rimborso', %s, %s, %s)
                    RETURNING id
                """, (user_id, data_ora, importo, metodo, f"Rimborso quota da {nome_p}"))
                t_id = cursor.fetchone()[0]

                cursor.execute("""
                    INSERT INTO voci_spesa (transazione_id, nome, quantita, prezzo_unitario, prezzo_totale, macro_categoria, sotto_categoria)
                    VALUES (%s, %s, 1.0, %s, %s, 'ENTRATE', 'Rimborsi Regali')
                """, (t_id, f"Rimborso da {nome_p}", importo, importo))

                cursor.execute("""
                    INSERT INTO debiti_crediti (user_id, persona, tipo, importo, descrizione, data_ora)
                    VALUES (%s, %s, 'MI_HA_OFFERTO', %s, %s, %s)
                """, (user_id, nome_p, importo, "Rimborso quota", data_ora))

                cursor.execute("""
                    UPDATE collette_regali
                    SET partecipanti_pagati = array_append(partecipanti_pagati, %s)
                    WHERE user_id = %s 
                      AND %s = ANY(partecipanti_totali)
                      AND NOT (%s = ANY(partecipanti_pagati))
                      AND completato = FALSE
                """, (nome_p, user_id, nome_p, nome_p))

                cursor.execute("""
                    UPDATE collette_regali
                    SET completato = TRUE
                    WHERE user_id = %s 
                      AND array_length(partecipanti_totali, 1) = array_length(partecipanti_pagati, 1)
                """, (user_id,))

                conn.commit()

                await message.reply(
                    f"✅ <b>Saldo Quota Registrato!</b>\n"
                    f"👤 <b>Da:</b> {html.escape(nome_p)}\n"
                    f"🟢 <b>Importo Accreditato:</b> +€{importo:.2f}",
                    parse_mode=ParseMode.HTML
                )
                return

            # 3. OFFERTE AL BAR TRA AMICI
            elif tipo_mov == "HO_OFFERTO":
                nome_p = (persona or "Amico").capitalize()
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

                await message.reply(
                    f"🤝 <b>Offerta registrata!</b>\n👤 <b>A chi:</b> {html.escape(nome_p)}\n☕ <b>Cosa:</b> {html.escape(descrizione)}\n💰 <b>Importo:</b> €{importo:.2f} ({metodo})",
                    parse_mode=ParseMode.HTML
                )
                return

            # 4. SPESA O REGALO FATTO DA SOLO / ENTRATE STANDARD
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
        f"{icona}\n📝 <b>Articolo:</b> {html.escape(descrizione)}\n💰 <b>Importo:</b> €{importo:.2f} ({metodo})\n🏷️ <b>Categoria:</b> {macro_cat} ➔ <i>{html.escape(sotto_cat)}</i>",
        parse_mode=ParseMode.HTML
    )

# --- SCONTRINI ---
@dp.message(F.photo)
async def handle_receipt(message: Message):
    status_msg = await message.reply("⏳ <i>Analisi scontrino con Gemini AI...</i>", parse_mode=ParseMode.HTML)
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

        response_text = await generate_gemini_content(
            contents=[types.Part.from_bytes(data=image_data, mime_type='image/jpeg'), RECEIPT_PROMPT],
            schema=True
        )
        dati = json.loads(response_text)
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

# --- API ENDPOINTS ---
async def web_index(request):
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")

async def web_health(request):
    return web.Response(text="OK", status=200)

async def api_data(request):
    raw_user_id = request.query.get("user_id", "0")
    init_data = request.query.get("init_data", "")

    user_id = 0
    try:
        if raw_user_id and raw_user_id != "0":
            user_id = int(raw_user_id)
    except ValueError:
        user_id = 0

    if user_id == 0 and init_data:
        try:
            parsed = urllib.parse.parse_qs(init_data)
            if "user" in parsed:
                user_dict = json.loads(parsed["user"][0])
                user_id = int(user_dict.get("id", 0))
        except Exception:
            pass

    mese_corrente = datetime.now().strftime("%Y-%m")

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT COALESCE(SUM(totale), 0.0) 
                FROM transazioni 
                WHERE user_id = %s 
                  AND (data_ora::text LIKE %s OR data_creazione::text LIKE %s)
                  AND tipo_movimento = 'ENTRATA'
            """, (user_id, f"{mese_corrente}%", f"{mese_corrente}%"))
            inc = float(cursor.fetchone()[0] or 0.0)

            cursor.execute("""
                SELECT COALESCE(SUM(totale), 0.0) 
                FROM transazioni 
                WHERE user_id = %s 
                  AND (data_ora::text LIKE %s OR data_creazione::text LIKE %s)
                  AND tipo_movimento = 'USCITA'
            """, (user_id, f"{mese_corrente}%", f"{mese_corrente}%"))
            exp = float(cursor.fetchone()[0] or 0.0)

            cursor.execute("""
                SELECT v.macro_categoria, SUM(v.prezzo_totale)
                FROM voci_spesa v 
                JOIN transazioni t ON v.transazione_id = t.id
                WHERE t.user_id = %s 
                  AND (t.data_ora::text LIKE %s OR t.data_creazione::text LIKE %s)
                  AND t.tipo_movimento = 'USCITA'
                GROUP BY v.macro_categoria
                ORDER BY SUM(v.prezzo_totale) DESC
            """, (user_id, f"{mese_corrente}%", f"{mese_corrente}%"))
            cats = [{"name": r[0], "value": float(r[1])} for r in cursor.fetchall()]

            cursor.execute("""
                SELECT persona,
                       SUM(CASE WHEN tipo = 'HO_OFFERTO' THEN importo ELSE -importo END) AS saldo
                FROM debiti_crediti
                WHERE user_id = %s
                GROUP BY persona
            """, (user_id,))
            friends = [{"name": r[0], "balance": float(r[1] or 0.0)} for r in cursor.fetchall()]

            # STORICO 26 MESI
            cursor.execute("""
                SELECT 
                    SUBSTRING(COALESCE(NULLIF(data_ora, ''), data_creazione::text) FROM 1 FOR 7) AS mese,
                    SUM(CASE WHEN tipo_movimento = 'ENTRATA' THEN totale ELSE 0 END) -
                    SUM(CASE WHEN tipo_movimento = 'USCITA' THEN totale ELSE 0 END) AS netto
                FROM transazioni
                WHERE user_id = %s
                GROUP BY mese
                ORDER BY mese DESC
                LIMIT 26
            """, (user_id,))
            history_rows = cursor.fetchall()
            history = [{"month": r[0], "net": float(r[1] or 0.0)} for r in reversed(history_rows) if r[0]]

    finally:
        release_db_connection(conn)

    return web.json_response({
        "month": datetime.now().strftime("%m/%Y"),
        "income": inc,
        "expense": exp,
        "balance": inc - exp,
        "categories": cats,
        "friends": friends,
        "history": history
    })

# --- TASK ANTI-SLEEP ---
async def self_ping_task():
    await asyncio.sleep(30)
    if not WEBAPP_URL.startswith("https://"):
        return
    ping_url = f"{WEBAPP_URL}/health"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(ping_url, timeout=10) as resp:
                    if resp.status == 200:
                        logging.info("💓 Anti-Sleep Self-Ping inviato con successo (200 OK).")
            except Exception as e:
                logging.warning(f"⚠️ Errore Self-Ping: {e}")
            await asyncio.sleep(600)

async def send_daily_summary():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            if ALLOWED_USER_ID:
                users = [ALLOWED_USER_ID]
            else:
                cursor.execute("SELECT DISTINCT user_id FROM transazioni")
                users = [r[0] for r in cursor.fetchall()]

            tz = pytz.timezone("Europe/Rome")
            oggi_str = datetime.now(tz).strftime("%Y-%m-%d")

            for uid in users:
                cursor.execute("""
                    SELECT t.tipo_movimento, t.totale, t.metodo_pagamento, t.note, t.tipo_inserimento, t.esercente
                    FROM transazioni t
                    WHERE t.user_id = %s 
                      AND (t.data_ora::text LIKE %s OR t.data_creazione::text LIKE %s)
                    ORDER BY t.id ASC
                """, (uid, f"{oggi_str}%", f"{oggi_str}%"))
                movimenti = cursor.fetchall()

                if not movimenti:
                    testo_notifica = (
                        f"🌙 <b>Resoconto del Giorno ({datetime.now(tz).strftime('%d/%m/%Y')}):</b>\n\n"
                        "✨ Oggi <b>nessuna spesa registrata</b>... o ti sei dimenticato di inserirle? 🤔"
                    )
                else:
                    tot_uscite = sum(float(m[1]) for m in movimenti if m[0] == 'USCITA')
                    tot_entrate = sum(float(m[1]) for m in movimenti if m[0] == 'ENTRATA')

                    testo_notifica = f"🌙 <b>Resoconto Serale ({datetime.now(tz).strftime('%d/%m/%Y')}):</b>\n\n"
                    testo_notifica += "📋 <b>Movimenti di oggi:</b>\n"
                    
                    for tipo_mov, tot, metodo, note, tipo_ins, esercente in movimenti:
                        segno = "🔴 -" if tipo_mov == 'USCITA' else "🟢 +"
                        nome_voce = note or esercente or "Movimento"
                        metodo_str = f" <i>({metodo})</i>" if metodo != "NON_SPECIFICATO" else ""
                        icona_tipo = "📸 " if tipo_ins == "FOTO" else "🏷️ "
                        testo_notifica += f"• {icona_tipo}{html.escape(str(nome_voce))}: {segno}€{float(tot):.2f}{metodo_str}\n"

                    testo_notifica += f"\n🔴 <b>Totale Uscite oggi:</b> €{tot_uscite:.2f}\n"
                    if tot_entrate > 0:
                        testo_notifica += f"🟢 <b>Totale Entrate oggi:</b> €{tot_entrate:.2f}\n"

                try:
                    await bot.send_message(chat_id=uid, text=testo_notifica, parse_mode=ParseMode.HTML)
                    logging.info(f"Notifica serale 23:50 inviata a user_id: {uid}")
                except Exception as e:
                    logging.warning(f"Impossibile inviare notifica serale a {uid}: {e}")
    finally:
        release_db_connection(conn)

# --- AVVIO SERVER ---
async def main():
    await bot.delete_webhook(drop_pending_updates=True)

    app = web.Application()
    cors = aiohttp_cors.setup(app, defaults={"*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")})
    
    app.router.add_get("/", web_index)
    app.router.add_get("/health", web_health)
    app.router.add_static("/static/", path="static", name="static")

    resource = cors.add(app.router.add_resource("/api/data"))
    cors.add(resource.add_route("GET", api_data))

    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Dashboard Web Server avviato sulla porta {port}")

    asyncio.create_task(self_ping_task())

    # Scheduler Giornaliero alle 23:50
    scheduler = AsyncIOScheduler(timezone=pytz.timezone("Europe/Rome"))
    scheduler.add_job(send_daily_summary, "cron", hour=23, minute=50)
    scheduler.start()
    logging.info("⏰ Scheduler Resoconto Serale attivo (Ogni giorno alle 23:50 Europe/Rome)")
    
    print("🚀 Bot Finanze Personali Sincronizzato!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())