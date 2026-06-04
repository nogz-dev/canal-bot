import os, asyncio, httpx, pytz, logging
from datetime import date, datetime
from anthropic import Anthropic
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.environ.get("CANAL_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN", "")
CHANNEL_ID        = os.environ.get("CHANNEL_ID", "@seutipster")
FOOTBALL_API_KEY  = os.environ.get("FOOTBALL_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TIMEZONE          = "America/Sao_Paulo"
FOOTBALL_API_URL  = "https://v3.football.api-sports.io"

PRIORITY_LEAGUE_IDS = {
    1,2,3,9,10,11,13,29,39,61,71,72,75,76,
    78,94,128,135,140,203,262,307,612,667,848,914
}

bot       = Bot(token=TELEGRAM_TOKEN)
anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Estado
simples_postadas = {}  # fid -> {msg_id, texto_original, fixture}
multiplas_dia    = []  # [{msg_id, texto_original, fixture_ids}]
dia_atual        = None
multipla_postada = False

TEAMS = {"Brazil":"Brasil","Argentina":"Argentina","Germany":"Alemanha",
         "France":"França","Spain":"Espanha","Portugal":"Portugal",
         "Italy":"Itália","England":"Inglaterra","Netherlands":"Holanda",
         "Uruguay":"Uruguai","Colombia":"Colômbia","Chile":"Chile",
         "Belgium":"Bélgica","Egypt":"Egito","USA":"EUA","Mexico":"México",
         "Japan":"Japão","South Korea":"Coreia do Sul",
         "Paris Saint-Germain":"PSG","Bayern Munich":"Bayern",
         "FC Barcelona":"Barcelona","Turkey":"Turquia","Switzerland":"Suíça",
         "Morocco":"Marrocos","Venezuela":"Venezuela","Panama":"Panamá",
         "Bolivia":"Bolívia","Honduras":"Honduras","Qatar":"Catar",
         "Australia":"Austrália","Ecuador":"Equador",
         "Ivory Coast":"Costa do Marfim","Saudi Arabia":"Arábia Saudita",
         "Nigeria":"Nigéria","Senegal":"Senegal","Cameroon":"Camarões",
         "Algeria":"Argélia","Serbia":"Sérvia","Croatia":"Croácia",
         "Denmark":"Dinamarca","Poland":"Polônia","Romania":"Romênia",
         "Scotland":"Escócia","Wales":"País de Gales","Norway":"Noruega",
         "Atletico Madrid":"Atlético de Madrid","Ghana":"Gana"}

LEAGUES = {"International Friendlies":"Amistosos Internacionais",
           "FIFA World Cup":"Copa do Mundo","World Cup":"Copa do Mundo",
           "UEFA Champions League":"Champions League",
           "UEFA Europa League":"Europa League",
           "Copa America":"Copa América","CONMEBOL Libertadores":"Libertadores",
           "CONMEBOL Sudamericana":"Sul-Americana","Copa Do Brasil":"Copa do Brasil",
           "Friendlies Clubs":"Amistosos de Clubes",
           "Tournoi Maurice Revello":"Revello U20",
           "Serie B":"Brasileirão Série B"}

def tt(n): return TEAMS.get(n, n)
def tl(n): return LEAGUES.get(n, n)

def bet365_btn(home, away):
    url = f"https://www.bet365.com/#/AC/B1/C1/D1002/E^{home.replace(' ','%20')}%20{away.replace(' ','%20')}/"
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎰 Criar aposta na Bet365", url=url)]])

def bet365_btn_multipla():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎰 Montar múltipla na Bet365", url="https://www.bet365.com/#/AC/B1/C1/D1002/")]])

def parse_form(data, tid):
    res = []
    for m in (data.get("response",[]) if isinstance(data,dict) else [])[:5]:
        ih = m["teams"]["home"]["id"] == tid
        hg = m["goals"]["home"] or 0; ag = m["goals"]["away"] or 0
        res.append("V" if (ih and hg>ag) or (not ih and ag>hg) else "E" if hg==ag else "D")
    return res

async def football_request(endpoint, params):
    headers = {"x-apisports-key": FOOTBALL_API_KEY,
               "x-rapidapi-key": FOOTBALL_API_KEY,
               "x-rapidapi-host": "v3.football.api-sports.io"}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{FOOTBALL_API_URL}/{endpoint}", headers=headers, params=params)
        r.raise_for_status()
        return r.json()

async def send_msg(texto, keyboard=None):
    try:
        chunks = [texto[i:i+4000] for i in range(0,len(texto),4000)]
        msg_id = None
        for i, chunk in enumerate(chunks):
            kb = keyboard if i == len(chunks)-1 else None
            m = await bot.send_message(chat_id=CHANNEL_ID, text=chunk,
                parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
                reply_markup=kb)
            if msg_id is None: msg_id = m.message_id
            await asyncio.sleep(0.5)
        return msg_id
    except Exception as e:
        logger.error(f"Erro send: {e}")
        return None

async def edit_msg(msg_id, novo_texto, keyboard=None):
    """Edita mensagem existente adicionando resultado no topo"""
    try:
        await bot.edit_message_text(
            chat_id=CHANNEL_ID, message_id=msg_id,
            text=novo_texto[:4096], parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Erro edit: {e}")

async def coletar_info(f):
    hid = f["teams"]["home"]["id"]; aid = f["teams"]["away"]["id"]
    tz  = pytz.timezone(TIMEZONE)
    dt  = datetime.fromisoformat(f["fixture"]["date"]).astimezone(tz)
    try:
        hd, ad, h2h = await asyncio.gather(
            football_request("fixtures", {"team": hid, "last": 8}),
            football_request("fixtures", {"team": aid, "last": 8}),
            football_request("fixtures/headtohead", {"h2h": f"{hid}-{aid}", "last": 8}),
            return_exceptions=True)
    except:
        hd = ad = h2h = {}
    hf = ' '.join(parse_form(hd, hid)) if not isinstance(hd, Exception) else "N/A"
    af = ' '.join(parse_form(ad, aid)) if not isinstance(ad, Exception) else "N/A"
    h2h_str = []
    if isinstance(h2h, dict):
        for m in h2h.get("response",[])[:4]:
            h2h_str.append(f"{tt(m['teams']['home']['name'])} {m['goals']['home'] or 0}-{m['goals']['away'] or 0} {tt(m['teams']['away']['name'])}")
    return {"id": f["fixture"]["id"],
            "home": tt(f["teams"]["home"]["name"]),
            "away": tt(f["teams"]["away"]["name"]),
            "league": tl(f["league"]["name"]),
            "hora": dt.strftime("%H:%M"), "data": dt.strftime("%d/%m"),
            "home_form": hf, "away_form": af,
            "h2h": ' | '.join(h2h_str) if h2h_str else "Sem histórico"}

# ── PROMPTS ────────────────────────────────────────────────────────────────────
SIMPLES_PROMPT = """Tipster profissional. Bilhete SIMPLES para um jogo.
Regras: Só palpites. Sem estatísticas, sem forma dos times, sem probabilidades.
Odds entre 1.75 e 3.50 por palpite. NUNCA "dado insuficiente".
Selecione 5-7 palpites com maior base estatística.

Formato:
🎫🎫🎫 *BILHETE SIMPLES* 🎫🎫🎫
🏟️ *[Casa] x [Fora]*
🏆 [Liga] | 🕐 [Hora]
——————————————————
✅ *[Mercado]* → [Palpite] | odd: X.XX
📌 [Justificativa 1 linha]

✅ *[Mercado]* → [Palpite] | odd: X.XX
📌 [Justificativa]

⚡ *[Mercado]* → [Palpite] | odd: X.XX
📌 [Justificativa]

——————————————————
⚠️ _Aposte com responsabilidade._"""

MULTIPLA_PROMPT = """Tipster profissional. MÚLTIPLA agressiva com jogos diferentes.
Regras: Só palpites. Sem estatísticas, sem forma. Direto ao ponto.
Odds individuais: 1.60 a 3.00. Odd total alvo: 8.00 a 25.00.
4 a 6 seleções de jogos DIFERENTES. NUNCA "dado insuficiente".

Formato:
🎰🎰🎰 *MÚLTIPLA AGRESSIVA* 🎰🎰🎰
📅 [data]
——————————————————
✅ *[Casa] x [Fora]* | 🕐 [Hora]
📌 [Mercado]: *[Palpite]* | odd: ~X.XX
📊 [Justificativa 1 linha]

✅ *[Casa] x [Fora]* | 🕐 [Hora]
📌 [Mercado]: *[Palpite]* | odd: ~X.XX
📊 [Justificativa]

——————————————————
💰 *ODD TOTAL: ~X.XX*
📈 R$10 → ~R$XX | R$20 → ~R$XXX
⚡ *Stake: 1-2% da banca*
——————————————————
⚠️ _Aposte com responsabilidade._"""

async def ia_simples(info):
    ctx = (f"Jogo: {info['home']} x {info['away']}\n"
           f"Liga: {info['league']} | {info['data']} às {info['hora']}\n"
           f"Forma {info['home']}: {info['home_form']}\n"
           f"Forma {info['away']}: {info['away_form']}\n"
           f"H2H: {info['h2h']}")
    try:
        r = anthropic.messages.create(model="claude-sonnet-4-5", max_tokens=1200,
            system=SIMPLES_PROMPT, messages=[{"role":"user","content":ctx}])
        return r.content[0].text
    except Exception as e:
        logger.error(f"Erro IA simples: {e}"); return None

async def ia_multipla(jogos_info):
    ctx = "Jogos disponíveis:\n\n"
    for j in jogos_info:
        ctx += f"{j['home']} x {j['away']} ({j['league']}) às {j['hora']}\nH2H: {j['h2h']}\n\n"
    try:
        r = anthropic.messages.create(model="claude-sonnet-4-5", max_tokens=1800,
            system=MULTIPLA_PROMPT, messages=[{"role":"user","content":ctx}])
        return r.content[0].text
    except Exception as e:
        logger.error(f"Erro IA múltipla: {e}"); return None

# ── VERIFICAÇÃO DE RESULTADOS ─────────────────────────────────────────────────
async def verificar_resultados():
    tz = pytz.timezone(TIMEZONE)
    agora = datetime.now(tz)

    # Verifica simples
    for fid, info in list(simples_postadas.items()):
        if info.get("resultado"): continue
        try:
            data = await football_request("fixtures", {"id": fid})
            f = data.get("response", [{}])[0]
            status = f.get("fixture", {}).get("status", {}).get("short", "")
            if status not in ["FT","AET","PEN"]: continue

            home_score = f["goals"]["home"] or 0
            away_score = f["goals"]["away"] or 0
            home = info["home"]; away = info["away"]
            resultado_jogo = f"{home} {home_score} x {away_score} {away}"

            # Edita a mensagem original com resultado no topo
            novo_texto = (
                f"🎫 *BILHETE SIMPLES — RESULTADO*\n"
                f"📊 *{resultado_jogo}*\n"
                f"_Verifique seus palpites acima_ ✅\n\n"
                f"——————————————————\n\n"
                f"{info['texto']}"
            )
            await edit_msg(info["msg_id"], novo_texto)
            simples_postadas[fid]["resultado"] = "verificado"
            logger.info(f"✅ Resultado simples: {resultado_jogo}")
        except Exception as e:
            logger.error(f"Erro verificar simples {fid}: {e}")

    # Verifica múltiplas
    for m_info in multiplas_dia:
        if m_info.get("resultado"): continue
        fids = m_info.get("fixture_ids", [])
        if not fids: continue

        todos_terminados = True
        resultados = []
        try:
            for fid in fids:
                data = await football_request("fixtures", {"id": fid})
                f = data.get("response", [{}])[0]
                status = f.get("fixture",{}).get("status",{}).get("short","")
                if status not in ["FT","AET","PEN"]:
                    todos_terminados = False; break
                h = tt(f["teams"]["home"]["name"])
                a = tt(f["teams"]["away"]["name"])
                hg = f["goals"]["home"] or 0
                ag = f["goals"]["away"] or 0
                resultados.append(f"{h} {hg}x{ag} {a}")

            if todos_terminados and resultados:
                res_texto = "\n".join(f"• {r}" for r in resultados)
                novo_texto = (
                    f"🎰 *MÚLTIPLA — RESULTADO*\n\n"
                    f"{res_texto}\n\n"
                    f"_Verifique se seus palpites foram green_ ✅\n\n"
                    f"——————————————————\n\n"
                    f"{m_info['texto']}"
                )
                await edit_msg(m_info["msg_id"], novo_texto, bet365_btn_multipla())
                m_info["resultado"] = "verificado"
                logger.info("✅ Resultado múltipla verificado")
        except Exception as e:
            logger.error(f"Erro verificar múltipla: {e}")

# ── PROCESSAMENTO PRINCIPAL ────────────────────────────────────────────────────
async def processar():
    global multipla_postada, dia_atual
    logger.info("Verificando jogos...")

    tz    = pytz.timezone(TIMEZONE)
    agora = datetime.now(tz)
    hoje  = date.today().isoformat()

    if dia_atual != hoje:
        simples_postadas.clear()
        multiplas_dia.clear()
        multipla_postada = False
        dia_atual = hoje

    try:
        data     = await football_request("fixtures", {"date": hoje, "timezone": TIMEZONE})
        fixtures = data.get("response", [])

        priority = sorted(
            [f for f in fixtures
             if f["league"]["id"] in PRIORITY_LEAGUE_IDS
             and f["fixture"]["status"]["short"] == "NS"],
            key=lambda f: f["fixture"]["date"])

        # ── BILHETES SIMPLES ─────────────────────────────────────────────────
        for f in priority:
            fid  = f["fixture"]["id"]
            dt   = datetime.fromisoformat(f["fixture"]["date"]).astimezone(tz)
            mins = (dt - agora).total_seconds() / 60

            if 110 <= mins <= 130 and fid not in simples_postadas:
                info  = await coletar_info(f)
                texto = await ia_simples(info)
                if texto:
                    kb     = bet365_btn(info["home"], info["away"])
                    msg_id = await send_msg(texto, keyboard=kb)
                    simples_postadas[fid] = {
                        "msg_id": msg_id, "texto": texto,
                        "home": info["home"], "away": info["away"],
                        "resultado": None
                    }
                    logger.info(f"✅ Simples: {info['home']} x {info['away']}")
                    await asyncio.sleep(5)

        # ── MÚLTIPLA DO DIA ──────────────────────────────────────────────────
        if not multipla_postada and len(priority) >= 2:
            primeiro = datetime.fromisoformat(priority[0]["fixture"]["date"]).astimezone(tz)
            mins_p   = (primeiro - agora).total_seconds() / 60

            if 100 <= mins_p <= 135:
                logger.info(f"Gerando múltipla ({len(priority)} jogos)")
                infos = await asyncio.gather(*[coletar_info(f) for f in priority[:8]])
                texto = await ia_multipla(list(infos))
                if texto:
                    kb     = bet365_btn_multipla()
                    msg_id = await send_msg(texto, keyboard=kb)
                    multiplas_dia.append({
                        "msg_id": msg_id, "texto": texto,
                        "fixture_ids": [f["fixture"]["id"] for f in priority[:8]],
                        "resultado": None
                    })
                    multipla_postada = True
                    logger.info("✅ Múltipla do dia postada")

        # ── VERIFICA RESULTADOS ──────────────────────────────────────────────
        await verificar_resultados()

    except Exception as e:
        logger.error(f"Erro processar: {e}")


async def resumo_final():
    tz   = pytz.timezone(TIMEZONE)
    hoje = datetime.now(tz).strftime("%d/%m/%Y")
    texto = (
        f"📋 *RESUMO DO DIA — SEU TIPSTER*\n📅 {hoje}\n\n"
        f"——————————————————\n"
        f"⚽ Bilhetes simples: *{len(simples_postadas)}*\n"
        f"🎰 Múltipla: *{'✅ Enviada' if multipla_postada else '—'}*\n\n"
        f"📊 Resultados nas mensagens acima ⬆️\n\n"
        f"——————————————————\n_Amanhã tem mais! Boas apostas_ 🍀"
    )
    await send_msg(texto)


async def limpar():
    global multipla_postada
    simples_postadas.clear(); multiplas_dia.clear()
    multipla_postada = False


async def main():
    global dia_atual
    dia_atual = date.today().isoformat()
    me = await bot.get_me()
    logger.info(f"✅ Bot: @{me.username}")
    scheduler.add_job(processar,    "interval", minutes=5,   id="processar")
    scheduler.add_job(resumo_final, "cron", hour=23, minute=30, id="resumo")
    scheduler.add_job(limpar,       "cron", hour=0,  minute=5,  id="limpar")
    scheduler.start()
    logger.info("✅ Rodando — simples + múltipla + resultados automáticos")
    try:
        while True: await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
