import os, asyncio, httpx, pytz, logging, json
from datetime import date, datetime, timedelta
from anthropic import Anthropic
from telegram import Bot
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

# Estado do dia
multiplas_postadas  = {}   # chave -> {msg_id, picks, resultado}
analises_postadas   = set()
dia_atual           = None
resumo_dia          = {"green": 0, "red": 0, "pendente": 0}

# ── HELPERS ──────────────────────────────────────────────────────────────────
async def football_request(endpoint, params):
    headers = {"x-apisports-key": FOOTBALL_API_KEY,
                "x-rapidapi-key": FOOTBALL_API_KEY,
                "x-rapidapi-host": "v3.football.api-sports.io"}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{FOOTBALL_API_URL}/{endpoint}", headers=headers, params=params)
        r.raise_for_status()
        return r.json()

def tr(name, d):
    return d.get(name, name)

TEAMS = {"Brazil":"Brasil","Argentina":"Argentina","Germany":"Alemanha","France":"França",
         "Spain":"Espanha","Portugal":"Portugal","Italy":"Itália","Netherlands":"Holanda",
         "England":"Inglaterra","Uruguay":"Uruguai","Colombia":"Colômbia","Chile":"Chile",
         "Belgium":"Bélgica","Egypt":"Egito","USA":"EUA","Mexico":"México",
         "Japan":"Japão","South Korea":"Coreia do Sul","Paris Saint-Germain":"PSG",
         "Bayern Munich":"Bayern","FC Barcelona":"Barcelona","Turkey":"Turquia",
         "Switzerland":"Suíça","Morocco":"Marrocos","Venezuela":"Venezuela",
         "Panama":"Panamá","Bolivia":"Bolívia","Honduras":"Honduras","Qatar":"Catar",
         "Australia":"Austrália","Ecuador":"Equador","Ivory Coast":"Costa do Marfim",
         "Saudi Arabia":"Arábia Saudita","Atletico Madrid":"Atlético de Madrid"}

LEAGUES = {"International Friendlies":"Amistosos Internacionais",
           "FIFA World Cup":"Copa do Mundo","World Cup":"Copa do Mundo",
           "UEFA Champions League":"Champions League","UEFA Europa League":"Europa League",
           "Copa America":"Copa América","CONMEBOL Libertadores":"Libertadores",
           "CONMEBOL Sudamericana":"Sul-Americana","Copa Do Brasil":"Copa do Brasil",
           "Friendlies Clubs":"Amistosos de Clubes","Tournoi Maurice Revello":"Revello U20"}

def tt(n): return tr(n, TEAMS)
def tl(n): return tr(n, LEAGUES)

def bet365_link(home, away):
    q = f"{home}%20{away}"
    return f"https://www.bet365.com/#/AC/B1/C1/D1002/E^{q}/"

def parse_form(data, tid):
    res = []
    for m in (data.get("response",[]) if isinstance(data,dict) else [])[:5]:
        ih = m["teams"]["home"]["id"] == tid
        hg = m["goals"]["home"] or 0
        ag = m["goals"]["away"] or 0
        res.append("V" if (ih and hg>ag) or (not ih and ag>hg) else
                   "E" if hg==ag else "D")
    return res

async def send(texto):
    try:
        chunks = [texto[i:i+4000] for i in range(0,len(texto),4000)]
        msg_id = None
        for chunk in chunks:
            m = await bot.send_message(chat_id=CHANNEL_ID, text=chunk,
                                        parse_mode=ParseMode.MARKDOWN,
                                        disable_web_page_preview=True)
            if msg_id is None: msg_id = m.message_id
            await asyncio.sleep(0.5)
        return msg_id
    except Exception as e:
        logger.error(f"Erro send: {e}")
        try:
            m = await bot.send_message(chat_id=CHANNEL_ID, text=texto[:4096])
            return m.message_id
        except: return None

async def reply(reply_to_id, texto):
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=texto,
                                parse_mode=ParseMode.MARKDOWN,
                                reply_to_message_id=reply_to_id,
                                disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Erro reply: {e}")
        await send(texto)

# ── PROMPTS ──────────────────────────────────────────────────────────────────
MULTIPLA_PROMPT = """Você é tipster profissional especializado em múltiplas de alavancagem.

OBJETIVO: odd total entre 4.00 e 15.00 com 3 a 5 seleções coerentes.

Critérios:
- Cada odd individual entre 1.40 e 2.50
- Alta base estatística
- Mercados: Over/Under gols, BTTS, Chance Dupla, Handicap Asiático, Escanteios, 1T
- Retorne também JSON com as seleções para verificação posterior

Retorne EXATAMENTE neste formato (texto para Telegram + JSON no final):

🎯 *MÚLTIPLA DE ALAVANCAGEM — SEU TIPSTER* 🎯
📅 [data] | ⏰ Postar antes das [hora do 1º jogo]

——————————————————
[Para cada seleção:]
✅ *[Casa] x [Fora]*
🏆 [Liga] | 🕐 [Hora]
📌 *[Mercado]: [Palpite]*
💰 Odd: ~X.XX | 📊 [Justificativa 1 linha]

——————————————————
💰 *ODD TOTAL: ~X.XX*
📈 Exemplo: R$10 → R$XX potencial
⚡ Stake: 1-2% da banca

🎰 *Bet365:* [Abrir Bet365](https://www.bet365.com/#/AC/B1/C1/D1002/)

——————————————————
⚠️ _Aposte com responsabilidade._

###JSON###
{"selecoes":[{"fixture_id":0,"home":"","away":"","mercado":"","palpite":"","odd":0.0}]}
###ENDJSON###"""

ANALISE_PROMPT = """Tipster profissional. Analise para canal Telegram. NUNCA escreva "dado insuficiente".

✅ ANÁLISE CONCLUÍDA - SEU TIPSTER ✅

🏟️ *[Casa] x [Fora]*
🏆 [Liga] | 📅 [Data] • [Hora]

——————————————————
📊 *PROBABILIDADES*
🏠 [Casa]: X% | 🤝 Empate: X% | ✈️ [Fora]: X%

——————————————————
🏠 *[CASA]* — Forma: [V V D E V]
⚽ Gols: X marc | X sof /jogo | 🕐 Marcou 1T: X/8
🚩 Escanteios: X/jogo (X no 1T) | 👟 Chutes alvo: X/jogo
🟨 Cartões: X/jogo

✈️ *[FORA]* — Forma: [V D E V V]
⚽ Gols: X marc | X sof /jogo | 🕐 Marcou 1T: X/8
🚩 Escanteios: X/jogo (X no 1T) | 👟 Chutes alvo: X/jogo
🟨 Cartões: X/jogo

——————————————————
🔄 *H2H* — Média gols: X | BTTS: X% | Over2.5: X% | Escanteios: X | Cartões: X

——————————————————
🎯 *PALPITES* (5-7 melhores mercados)

✅ *[Mercado]* — odd mín: X.XX
📌 [dado concreto]

⚡ *[Mercado]* — odd mín: X.XX
📌 [dado]

🔥 *[Mercado]* — odd mín: X.XX
📌 [dado]

——————————————————
⚠️ *RISCO:* [1 linha]"""

# ── COLETA DE DADOS ──────────────────────────────────────────────────────────
async def coletar_info(f):
    hid = f["teams"]["home"]["id"]
    aid = f["teams"]["away"]["id"]
    home = tt(f["teams"]["home"]["name"])
    away = tt(f["teams"]["away"]["name"])
    league = tl(f["league"]["name"])
    tz = pytz.timezone(TIMEZONE)
    dt = datetime.fromisoformat(f["fixture"]["date"]).astimezone(tz)

    try:
        hd, ad, h2h = await asyncio.gather(
            football_request("fixtures", {"team": hid, "last": 8}),
            football_request("fixtures", {"team": aid, "last": 8}),
            football_request("fixtures/headtohead", {"h2h": f"{hid}-{aid}", "last": 8}),
            return_exceptions=True
        )
    except:
        hd = ad = h2h = {}

    hf = ' '.join(parse_form(hd, hid)) if not isinstance(hd, Exception) else "N/A"
    af = ' '.join(parse_form(ad, aid)) if not isinstance(ad, Exception) else "N/A"
    h2h_str = []
    if isinstance(h2h, dict):
        for m in h2h.get("response",[])[:4]:
            h2h_str.append(f"{tt(m['teams']['home']['name'])} {m['goals']['home'] or 0}-{m['goals']['away'] or 0} {tt(m['teams']['away']['name'])}")

    return {"id": f["fixture"]["id"], "home": home, "away": away,
            "league": league, "hora": dt.strftime("%H:%M"),
            "data": dt.strftime("%d/%m"), "dt": dt,
            "home_form": hf, "away_form": af,
            "h2h": ' | '.join(h2h_str) if h2h_str else "Sem histórico"}

# ── GERAÇÃO IA ────────────────────────────────────────────────────────────────
async def ia_multipla(jogos_info, label):
    ctx = f"Grupo: {label}\n\n"
    for j in jogos_info:
        ctx += (f"ID:{j['id']} | {j['home']} x {j['away']} ({j['league']}) às {j['hora']}\n"
                f"Forma {j['home']}: {j['home_form']}\n"
                f"Forma {j['away']}: {j['away_form']}\n"
                f"H2H: {j['h2h']}\n\n")
    try:
        r = anthropic.messages.create(model="claude-sonnet-4-5", max_tokens=2000,
            system=MULTIPLA_PROMPT, messages=[{"role":"user","content":ctx}])
        return r.content[0].text
    except Exception as e:
        logger.error(f"Erro IA múltipla: {e}")
        return None

async def ia_analise(info):
    ctx = (f"Jogo: {info['home']} x {info['away']}\nLiga: {info['league']}\n"
           f"Data: {info['data']} às {info['hora']}\n"
           f"Forma {info['home']}: {info['home_form']}\n"
           f"Forma {info['away']}: {info['away_form']}\n"
           f"H2H: {info['h2h']}")
    try:
        r = anthropic.messages.create(model="claude-sonnet-4-5", max_tokens=2500,
            system=ANALISE_PROMPT, messages=[{"role":"user","content":ctx}])
        return r.content[0].text
    except Exception as e:
        logger.error(f"Erro IA análise: {e}")
        return None

def extrair_json_multipla(texto):
    try:
        start = texto.find("###JSON###") + 10
        end   = texto.find("###ENDJSON###")
        if start > 10 and end > start:
            return json.loads(texto[start:end].strip())
    except: pass
    return None

def limpar_texto(texto):
    """Remove bloco JSON do texto antes de enviar"""
    if "###JSON###" in texto:
        return texto[:texto.find("###JSON###")].strip()
    return texto

# ── VERIFICAÇÃO DE RESULTADOS ─────────────────────────────────────────────────
async def verificar_resultados():
    """Verifica se jogos de múltiplas postadas já terminaram"""
    if not multiplas_postadas:
        return

    tz    = pytz.timezone(TIMEZONE)
    agora = datetime.now(tz)

    for chave, info in list(multiplas_postadas.items()):
        if info.get("resultado"):
            continue  # já verificado

        selecoes = info.get("selecoes", [])
        if not selecoes:
            continue

        # Verifica se todos os jogos já terminaram
        todos_terminados = True
        greens = 0
        reds   = 0

        for sel in selecoes:
            fid = sel.get("fixture_id", 0)
            if fid == 0:
                continue
            try:
                data = await football_request("fixtures", {"id": fid})
                fixtures = data.get("response", [])
                if not fixtures:
                    continue
                f = fixtures[0]
                status = f["fixture"]["status"]["short"]
                if status not in ["FT","AET","PEN"]:
                    todos_terminados = False
                    break
                # Simplificado: marca verde se o jogo aconteceu (verificação real dependeria das odds)
                # Por ora marca o resultado como "verificado"
                greens += 1
            except Exception as e:
                logger.error(f"Erro verificar resultado {fid}: {e}")
                todos_terminados = False
                break

        if todos_terminados and selecoes:
            # Posta resultado como reply
            total = len(selecoes)
            # Aqui uma lógica simplificada — em produção precisaria das odds reais
            resultado_texto = (
                f"📊 *RESULTADO DA MÚLTIPLA*\n\n"
                f"🔍 Todos os {total} jogos foram encerrados.\n"
                f"📋 Verifique os resultados na Bet365.\n\n"
                f"_Para verificação automática precisa de integração com API de odds._"
            )
            await reply(info["msg_id"], resultado_texto)
            multiplas_postadas[chave]["resultado"] = "verificado"
            logger.info(f"✅ Resultado verificado para múltipla {chave}")

# ── RESUMO DO DIA ─────────────────────────────────────────────────────────────
async def resumo_final():
    """Posta resumo às 23:30"""
    tz   = pytz.timezone(TIMEZONE)
    hoje = datetime.now(tz).strftime("%d/%m/%Y")
    total_multiplas = len(multiplas_postadas)
    total_analises  = len(analises_postadas)

    texto = (
        f"📋 *RESUMO DO DIA — SEU TIPSTER*\n"
        f"📅 {hoje}\n\n"
        f"——————————————————\n"
        f"🎯 Múltiplas enviadas: *{total_multiplas}*\n"
        f"📊 Análises individuais: *{total_analises}*\n\n"
        f"✅ Acompanhe os resultados nas mensagens acima ⬆️\n\n"
        f"——————————————————\n"
        f"_Amanhã tem mais! Boas apostas_ 🍀"
    )
    await send(texto)
    logger.info("✅ Resumo do dia postado")

# ── LOOP PRINCIPAL ────────────────────────────────────────────────────────────
async def processar():
    global dia_atual
    logger.info("Processando jogos...")

    tz    = pytz.timezone(TIMEZONE)
    agora = datetime.now(tz)
    hoje  = date.today().isoformat()

    # Reset diário
    if dia_atual != hoje:
        multiplas_postadas.clear()
        analises_postadas.clear()
        dia_atual = hoje
        logger.info(f"Novo dia: {hoje}")

    try:
        data     = await football_request("fixtures", {"date": hoje, "timezone": TIMEZONE})
        fixtures = data.get("response", [])

        priority = sorted(
            [f for f in fixtures
             if f["league"]["id"] in PRIORITY_LEAGUE_IDS
             and f["fixture"]["status"]["short"] == "NS"],
            key=lambda f: f["fixture"]["date"]
        )

        if not priority:
            logger.info("Sem jogos prioritários pendentes")
            return

        # ── MÚLTIPLAS ────────────────────────────────────────────────────────
        # Múltipla 1: TODOS os jogos do dia (postada 2h antes do primeiro)
        chave_total = "multipla_total_" + hoje
        if chave_total not in multiplas_postadas:
            primeiro_jogo = datetime.fromisoformat(priority[0]["fixture"]["date"]).astimezone(tz)
            mins_primeiro = (primeiro_jogo - agora).total_seconds() / 60
            if 100 <= mins_primeiro <= 130:
                logger.info(f"Gerando múltipla total ({len(priority)} jogos)")
                infos = await asyncio.gather(*[coletar_info(f) for f in priority[:8]])
                texto = await ia_multipla(list(infos), "Múltipla do dia — todos os jogos")
                if texto:
                    json_data = extrair_json_multipla(texto)
                    texto_limpo = limpar_texto(texto)
                    msg_id = await send(texto_limpo)
                    multiplas_postadas[chave_total] = {
                        "msg_id": msg_id,
                        "selecoes": json_data["selecoes"] if json_data else [],
                        "resultado": None
                    }
                    logger.info("✅ Múltipla total postada")
                    await asyncio.sleep(5)

        # Múltiplas por janela de horário (grupos de 2h)
        grupos = []
        grupo_atual = []
        hora_ref = None
        for f in priority:
            dt = datetime.fromisoformat(f["fixture"]["date"]).astimezone(tz)
            if hora_ref is None:
                hora_ref = dt
                grupo_atual.append(f)
            elif (dt - hora_ref).total_seconds() <= 7200:
                grupo_atual.append(f)
            else:
                grupos.append((hora_ref, grupo_atual))
                hora_ref = dt
                grupo_atual = [f]
        if grupo_atual:
            grupos.append((hora_ref, grupo_atual))

        multiplas_enviadas_hoje = sum(1 for k in multiplas_postadas if k != chave_total)

        for hora_grupo, jogos_grupo in grupos:
            if len(jogos_grupo) < 2:
                continue  # múltipla precisa de pelo menos 2 jogos
            chave_grupo = f"grupo_{hora_grupo.strftime('%H%M')}_{hoje}"
            mins = (hora_grupo - agora).total_seconds() / 60

            if 55 <= mins <= 75 and chave_grupo not in multiplas_postadas and multiplas_enviadas_hoje < 4:
                label = f"Jogos das {hora_grupo.strftime('%H:%M')} ({len(jogos_grupo)} jogos)"
                logger.info(f"Gerando múltipla: {label}")
                infos = await asyncio.gather(*[coletar_info(f) for f in jogos_grupo[:6]])
                texto = await ia_multipla(list(infos), label)
                if texto:
                    json_data = extrair_json_multipla(texto)
                    texto_limpo = limpar_texto(texto)
                    msg_id = await send(texto_limpo)
                    multiplas_postadas[chave_grupo] = {
                        "msg_id": msg_id,
                        "selecoes": json_data["selecoes"] if json_data else [],
                        "resultado": None
                    }
                    multiplas_enviadas_hoje += 1
                    logger.info(f"✅ Múltipla grupo postada: {label}")
                    await asyncio.sleep(5)

        # ── ANÁLISES INDIVIDUAIS ─────────────────────────────────────────────
        for f in priority:
            fid = f["fixture"]["id"]
            dt  = datetime.fromisoformat(f["fixture"]["date"]).astimezone(tz)
            mins = (dt - agora).total_seconds() / 60

            if 110 <= mins <= 130 and fid not in analises_postadas:
                info = await coletar_info(f)
                analise = await ia_analise(info)
                if analise:
                    link = bet365_link(info["home"], info["away"])
                    texto = f"{analise}\n\n🎰 *Bet365:* [Aposte aqui]({link})"
                    await send(texto)
                    analises_postadas.add(fid)
                    logger.info(f"✅ Análise: {info['home']} x {info['away']}")
                    await asyncio.sleep(5)

        # ── VERIFICAÇÃO DE RESULTADOS ────────────────────────────────────────
        await verificar_resultados()

    except Exception as e:
        logger.error(f"Erro processar: {e}")


async def limpar():
    multiplas_postadas.clear()
    analises_postadas.clear()
    logger.info("Cache limpo")


async def main():
    global dia_atual
    dia_atual = date.today().isoformat()
    logger.info("🚀 Canal Bot iniciando...")
    me = await bot.get_me()
    logger.info(f"✅ Bot: @{me.username}")

    scheduler.add_job(processar, "interval", minutes=5, id="processar")
    scheduler.add_job(resumo_final, "cron", hour=23, minute=30, id="resumo")
    scheduler.add_job(limpar, "cron", hour=0, minute=5, id="limpar")
    scheduler.start()
    logger.info("✅ Rodando — múltiplas automáticas + análises + resumo diário")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
