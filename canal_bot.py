import os, asyncio, httpx, pytz, logging, json
from datetime import date, datetime
from anthropic import Anthropic
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, PollAnswerHandler, ContextTypes
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
enquetes_ativas  = {}  # poll_id -> {mercados, votos, msg_id}
ultima_enquete   = None  # datetime da última enquete
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

def bet365_url(home, away):
    return "https://www.bet365.bet.br/#/AC/B1/C1/D1002/E^" + home.replace(" ","%20") + "%20" + away.replace(" ","%20") + "/"

def bet365_btn(home, away):
    url = f"https://www.bet365.bet.br/#/AC/B1/C1/D1002/E^{home.replace(' ','%20')}%20{away.replace(' ','%20')}/"
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎰 Criar aposta na Bet365", url=url)]])

def bet365_btn_multipla():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎰 Montar múltipla na Bet365", url="https://www.bet365.bet.br/#/AC/B1/C1/D1002/")]])

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


AO_VIVO_PROMPT = """Você é tipster especialista em apostas AO VIVO. Analise o estado atual do jogo e identifique oportunidades de alto valor.

Foco: encontrar mercados onde as odds estão distorcidas pelo placar atual, minuto ou dinâmica do jogo.
NUNCA escreva dado insuficiente. Seja direto e assertivo.

Mercados ao vivo disponíveis (Bet365):
Próximo Gol | Resultado Final | Ambos Marcam | Over/Under Gols restantes
Escanteios restantes | Próximo Escanteio | Próximo Cartão
Intervalo/Final | Gols no 2T | Empate Anula Aposta

Formato:
⚡⚡⚡ *APOSTA AO VIVO — SEU TIPSTER* ⚡⚡⚡
🔴 *[Casa] [Placar] [Fora]* | [Minuto]
🏆 [Liga]
——————————————————
🎯 *OPORTUNIDADE IDENTIFICADA*

✅ *[Mercado]* → [Palpite] | odd: X.XX
📌 [Justificativa baseada no estado atual do jogo]

⚡ *[Mercado]* → [Palpite] | odd: X.XX
📌 [Justificativa]

——————————————————
⏰ _Aposte AGORA — odds mudam a cada minuto!_
⚠️ _Aposte com responsabilidade._"""


async def buscar_odds(fixture_id):
    """Busca odds reais da Bet365 via API-Football"""
    try:
        data = await football_request("odds", {
            "fixture": fixture_id,
            "bookmaker": 8  # Bet365
        })
        response = data.get("response", [])
        if not response:
            return {}

        odds_dict = {}
        for item in response:
            for bookmaker in item.get("bookmakers", []):
                for bet in bookmaker.get("bets", []):
                    bet_name = bet.get("name", "")
                    values = {}
                    for v in bet.get("values", []):
                        values[v["value"]] = v["odd"]
                    odds_dict[bet_name] = values

        return odds_dict
    except Exception as e:
        logger.error("Erro buscar odds: " + str(e))
        return {}

def formatar_odds(odds_dict):
    """Formata odds para contexto do Claude"""
    if not odds_dict:
        return "Odds não disponíveis — use estimativas baseadas nas estatísticas"

    linhas = []
    mercados_priority = [
        "Match Winner", "Double Chance", "Both Teams Score",
        "Goals Over/Under", "Asian Handicap", "Exact Score",
        "First Half Winner", "Goals Over/Under First Half",
        "Corner Over/Under", "Cards Over/Under",
        "Result/Both Teams Score", "Draw No Bet"
    ]

    for mercado in mercados_priority:
        if mercado in odds_dict:
            vals = odds_dict[mercado]
            linha = mercado + ": "
            linha += " | ".join(k + "=" + str(v) for k, v in list(vals.items())[:4])
            linhas.append(linha)

    # Outros mercados
    for mercado, vals in odds_dict.items():
        if mercado not in mercados_priority:
            linha = mercado + ": "
            linha += " | ".join(k + "=" + str(v) for k, v in list(vals.items())[:3])
            linhas.append(linha)

    return "\n".join(linhas[:20])  # máx 20 mercados


def criar_card_simples(info, palpites):
    """Gera card visual do bilhete simples"""
    W, H = 600, 60 + 70 + len(palpites) * 64 + 100
    img  = Image.new("RGB", (W, H), "#0f0f0f")
    draw = ImageDraw.Draw(img)

    try:
        font_big   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_med   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        font_tiny  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
    except:
        font_big = font_med = font_small = font_tiny = ImageFont.load_default()

    GREEN  = "#39FF6B"
    WHITE  = "#ffffff"
    GRAY   = "#888888"
    CARD   = "#1a1a1a"
    BORDER = "#2a2a2a"

    # Header
    draw.rectangle([0, 0, W, 56], fill="#111111")
    draw.text((20, 10), "🎫 BILHETE SIMPLES — SEU TIPSTER", font=font_med, fill=GREEN)
    draw.text((20, 30), info["home"] + " x " + info["away"], font=font_big, fill=WHITE)
    draw.text((20, 52), info["league"] + "  |  " + info["hora"], font=font_tiny, fill=GRAY)
    draw.line([0, 70, W, 70], fill=BORDER, width=1)

    y = 80
    for p in palpites:
        draw.rectangle([16, y, W-16, y+54], fill=CARD, outline=BORDER)
        draw.text((28, y+8), p.get("mercado", ""), font=font_small, fill=GRAY)
        draw.text((28, y+26), p.get("palpite", ""), font=font_med, fill=WHITE)
        odd_str = "odd: " + str(p.get("odd", ""))
        draw.text((W-90, y+26), odd_str, font=font_med, fill=GREEN)
        y += 64

    # Footer odd total
    odd_total = 1.0
    for p in palpites:
        try: odd_total *= float(str(p.get("odd","1")).replace(",","."))
        except: pass
    odd_total = round(odd_total, 2)

    draw.rectangle([0, H-80, W, H], fill="#111111")
    draw.line([0, H-80, W, H-80], fill=BORDER, width=1)
    draw.text((20, H-62), "Odd total estimada", font=font_small, fill=GRAY)
    draw.text((20, H-40), str(odd_total), font=font_big, fill=GREEN)
    r10 = round(10 * odd_total, 0)
    r50 = round(50 * odd_total, 0)
    draw.text((W-200, H-62), "R$10 → R$" + str(int(r10)), font=font_small, fill=GRAY)
    draw.text((W-200, H-40), "R$50 → R$" + str(int(r50)), font=font_small, fill=GRAY)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def criar_card_multipla(jogos, odd_total):
    """Gera card visual da múltipla"""
    W, H = 600, 60 + 70 + len(jogos) * 72 + 100
    img  = Image.new("RGB", (W, H), "#0f0f0f")
    draw = ImageDraw.Draw(img)

    try:
        font_big   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_med   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        font_tiny  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
    except:
        font_big = font_med = font_small = font_tiny = ImageFont.load_default()

    ORANGE = "#FFA500"
    WHITE  = "#ffffff"
    GRAY   = "#888888"
    CARD   = "#1a1a1a"
    BORDER = "#2a2a2a"

    draw.rectangle([0, 0, W, 56], fill="#111111")
    draw.text((20, 10), "🎰 MULTIPLA AGRESSIVA — SEU TIPSTER", font=font_med, fill=ORANGE)
    draw.text((20, 30), str(len(jogos)) + " selecoes", font=font_big, fill=WHITE)
    draw.line([0, 70, W, 70], fill=BORDER, width=1)

    y = 80
    for j in jogos:
        draw.rectangle([16, y, W-16, y+62], fill=CARD, outline=BORDER)
        draw.text((28, y+6), j.get("home","") + " x " + j.get("away","") + "  " + j.get("hora",""), font=font_small, fill=GRAY)
        draw.text((28, y+24), j.get("mercado","") + ": " + j.get("palpite",""), font=font_med, fill=WHITE)
        odd_str = "~" + str(j.get("odd",""))
        draw.text((W-90, y+24), odd_str, font=font_med, fill=ORANGE)
        y += 72

    draw.rectangle([0, H-80, W, H], fill="#111111")
    draw.line([0, H-80, W, H-80], fill=BORDER, width=1)
    draw.text((20, H-62), "Odd total estimada", font=font_small, fill=GRAY)
    draw.text((20, H-40), str(odd_total), font=font_big, fill=ORANGE)
    r10 = round(10 * float(str(odd_total).replace(",",".")), 0)
    r20 = round(20 * float(str(odd_total).replace(",",".")), 0)
    draw.text((W-200, H-62), "R$10 -> R$" + str(int(r10)), font=font_small, fill=GRAY)
    draw.text((W-200, H-40), "R$20 -> R$" + str(int(r20)), font=font_small, fill=GRAY)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def send_card_simples(texto, palpites_parsed, info, keyboard=None):
    """Envia card visual + texto"""
    try:
        card_buf = criar_card_simples(info, palpites_parsed)
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=card_buf,
            caption=texto[:1024],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error("Erro send card: " + str(e))
        await send_msg(texto, keyboard=keyboard)


async def send_card_multipla(texto, jogos_parsed, odd_total, keyboard=None):
    """Envia card visual da múltipla + texto"""
    try:
        card_buf = criar_card_multipla(jogos_parsed, odd_total)
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=card_buf,
            caption=texto[:1024],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error("Erro send card multipla: " + str(e))
        await send_msg(texto, keyboard=keyboard)


def parsear_palpites_simples(texto):
    """Extrai palpites do texto gerado pelo Claude"""
    palpites = []
    linhas = texto.split("\n")
    for i, linha in enumerate(linhas):
        if "odd:" in linha.lower() and ("→" in linha or "->" in linha):
            partes = linha.replace("✅","").replace("⚡","").replace("🔥","").strip()
            mercado = ""
            palpite = ""
            odd = ""
            if "→" in partes:
                left, right = partes.split("→", 1)
                mercado = left.replace("*","").strip()
                if "odd:" in right.lower():
                    palpite, odd_part = right.lower().split("odd:", 1)
                    palpite = right.split("|")[0].strip() if "|" in right else palpite.strip()
                    odd = odd_part.strip().split()[0].replace("*","")
            if mercado:
                palpites.append({"mercado": mercado[:30], "palpite": palpite[:30], "odd": odd})
    return palpites[:6]


def parsear_jogos_multipla(texto):
    """Extrai jogos/seleções da múltipla"""
    jogos = []
    linhas = texto.split("\n")
    jogo_atual = {}
    for linha in linhas:
        if "x" in linha and "|" in linha and ":" not in linha[:20]:
            partes = linha.replace("✅","").strip()
            if "x" in partes:
                times = partes.split("|")[0].strip()
                hora  = partes.split("|")[1].strip() if "|" in partes else ""
                jogo_atual = {"home": times.split("x")[0].strip(),
                              "away": times.split("x")[1].strip() if "x" in times else "",
                              "hora": hora.replace("🕐","").strip()}
        elif "📌" in linha and jogo_atual:
            mercado_palpite = linha.replace("📌","").replace("*","").strip()
            odd = ""
            if "odd:" in mercado_palpite.lower():
                parts = mercado_palpite.lower().split("odd:")
                odd = parts[1].strip().split()[0].replace("~","") if len(parts) > 1 else ""
                mercado_palpite = mercado_palpite.split("|")[0].strip()
            if ":" in mercado_palpite:
                m, p = mercado_palpite.split(":", 1)
                jogo_atual["mercado"] = m.strip()[:25]
                jogo_atual["palpite"] = p.strip()[:25]
            jogo_atual["odd"] = odd
            jogos.append(dict(jogo_atual))
            jogo_atual = {}

    # Odd total
    odd_total = 1.0
    for j in jogos:
        try: odd_total *= float(str(j.get("odd","1")).replace(",","."))
        except: pass

    return jogos[:6], round(odd_total, 2)

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


# ── ENQUETES ──────────────────────────────────────────────────────────────────
MERCADOS_ENQUETE = [
    "⚽ Over/Under Gols",
    "🎯 Ambas Marcam",
    "🚩 Escanteios",
    "🟨 Cartões",
    "🕐 Gols 1º Tempo",
    "🏅 Marcador",
    "🔀 Handicap Asiático",
    "💫 Chance Dupla",
]

MULTIPLA_MERCADO_PROMPT = """Tipster profissional. Crie uma MÚLTIPLA focada no mercado escolhido pelos membros.

Mercado votado: {mercado}
Regras: Só palpites desse mercado específico. Odds 1.50-2.50 por seleção. Odd total 5.00-15.00.
3-5 jogos diferentes. NUNCA "dado insuficiente".

Formato:
🎰🎰🎰 *MÚLTIPLA ESPECIAL — {mercado}* 🎰🎰🎰
_Gerada pela votação dos membros_ 🗳️
📅 [data]
——————————————————
✅ *[Casa] x [Fora]* | 🕐 [Hora]
📌 *{mercado}: [Palpite específico]* | odd: ~X.XX
📊 [Justificativa 1 linha]

——————————————————
💰 *ODD TOTAL: ~X.XX*
📈 R$10 → ~R$XX | R$20 → ~R$XXX
⚡ *Stake: 1-2% da banca*
——————————————————
⚠️ _Aposte com responsabilidade._"""

async def postar_enquete():
    """Posta enquete de mercado no canal"""
    global ultima_enquete
    tz    = pytz.timezone(TIMEZONE)
    agora = datetime.now(tz)

    # Máx 2 enquetes por dia, intervalo de 4h
    if ultima_enquete:
        horas = (agora - ultima_enquete).total_seconds() / 3600
        if horas < 4:
            return

    try:
        poll = await bot.send_poll(
            chat_id=CHANNEL_ID,
            question="🗳️ Qual mercado você quer na próxima múltipla?",
            options=MERCADOS_ENQUETE,
            is_anonymous=True,
            allows_multiple_answers=False,
            open_period=3600  # 1h para votar
        )
        enquetes_ativas[poll.poll.id] = {
            "msg_id": poll.message_id,
            "mercados": MERCADOS_ENQUETE,
            "votos": {i: 0 for i in range(len(MERCADOS_ENQUETE))},
            "respondida": False
        }
        ultima_enquete = agora
        logger.info(f"✅ Enquete postada: {poll.poll.id}")
    except Exception as e:
        logger.error(f"Erro postar enquete: {e}")


async def processar_resultado_enquete(poll_id: str):
    """Gera múltipla baseada no mercado mais votado"""
    if poll_id not in enquetes_ativas:
        return
    info = enquetes_ativas[poll_id]
    if info["respondida"]:
        return

    votos = info["votos"]
    if not votos or all(v == 0 for v in votos.values()):
        return

    # Mercado mais votado
    idx_vencedor = max(votos, key=votos.get)
    mercado = MERCADOS_ENQUETE[idx_vencedor]
    total_votos = sum(votos.values())

    logger.info(f"Gerando múltipla especial: {mercado} ({votos[idx_vencedor]}/{total_votos} votos)")

    # Busca jogos do dia
    hoje = date.today().isoformat()
    tz   = pytz.timezone(TIMEZONE)
    agora = datetime.now(tz)

    try:
        data     = await football_request("fixtures", {"date": hoje, "timezone": TIMEZONE})
        fixtures = data.get("response", [])
        futuros  = [f for f in fixtures
                    if f["league"]["id"] in PRIORITY_LEAGUE_IDS
                    and f["fixture"]["status"]["short"] == "NS"
                    and (datetime.fromisoformat(f["fixture"]["date"]).astimezone(tz) - agora).total_seconds() / 60 > 40]

        if len(futuros) < 2:
            logger.info("Poucos jogos futuros para múltipla especial")
            return

        infos = await asyncio.gather(*[coletar_info(f) for f in futuros[:6]])
        ctx = f"Mercado foco: {mercado}\n\nJogos:\n"
        for j in infos:
            ctx += f"{j['home']} x {j['away']} ({j['league']}) às {j['hora']}\nH2H: {j['h2h']}\n\n"

        prompt = MULTIPLA_MERCADO_PROMPT.replace("{mercado}", mercado)
        r = anthropic.messages.create(
            model="claude-sonnet-4-5", max_tokens=1500,
            system=prompt,
            messages=[{"role":"user","content":ctx}]
        )
        texto = r.content[0].text

        # Posta resultado da votacao + multipla
        linha1 = "Resultado da Votacao: " + str(total_votos) + " votos"
        linha2 = "Vencedor: " + mercado
        intro = linha1 + "\n" + linha2 + "\n\n"
        kb = bet365_btn_multipla()
        msg_id = await send_msg(intro + texto, keyboard=kb)
        multiplas_dia.append({
            "msg_id": msg_id, "texto": texto,
            "fixture_ids": [f["fixture"]["id"] for f in futuros[:6]],
            "resultado": None
        })
        enquetes_ativas[poll_id]["respondida"] = True
        logger.info(f"✅ Múltipla especial postada: {mercado}")

    except Exception as e:
        logger.error(f"Erro múltipla especial: {e}")


async def verificar_enquetes_expiradas():
    """Verifica enquetes com 1h+ e gera múltipla do vencedor"""
    tz    = pytz.timezone(TIMEZONE)
    agora = datetime.now(tz)

    for poll_id, info in list(enquetes_ativas.items()):
        if info["respondida"]:
            continue
        if not ultima_enquete:
            continue
        mins = (agora - ultima_enquete).total_seconds() / 60
        if mins >= 60:  # 1h após a enquete
            await processar_resultado_enquete(poll_id)


async def ia_ao_vivo(fixture_ao_vivo):
    """Gera palpite ao vivo baseado no estado atual do jogo"""
    home  = fixture_ao_vivo["home"]
    away  = fixture_ao_vivo["away"]
    placar = fixture_ao_vivo["placar"]
    minuto = fixture_ao_vivo["minuto"]
    liga   = fixture_ao_vivo["league"]
    stats  = fixture_ao_vivo.get("stats", "")

    ctx = (
        "Jogo ao vivo: " + home + " " + placar + " " + away +
        " | Minuto: " + str(minuto) +
        " | Liga: " + liga +
        ("\nEstatísticas: " + stats if stats else "")
    )
    try:
        r = anthropic.messages.create(
            model="claude-sonnet-4-5", max_tokens=800,
            system=AO_VIVO_PROMPT,
            messages=[{"role":"user","content":ctx}])
        return r.content[0].text
    except Exception as e:
        logger.error("Erro IA ao vivo: " + str(e))
        return None

# Controle ao vivo
ao_vivo_postados = set()  # fixture_id + minuto

async def monitorar_ao_vivo():
    """Monitora jogos ao vivo e posta oportunidades"""
    tz    = pytz.timezone(TIMEZONE)
    agora = datetime.now(tz)
    hoje  = date.today().isoformat()

    try:
        data     = await football_request("fixtures", {"date": hoje, "timezone": TIMEZONE})
        fixtures = data.get("response", [])

        ao_vivo = [f for f in fixtures
                   if f["league"]["id"] in PRIORITY_LEAGUE_IDS
                   and f["fixture"]["status"]["short"] in ["1H","2H","HT","ET","BT","LIVE"]]

        for f in ao_vivo:
            fid    = f["fixture"]["id"]
            minuto = f["fixture"]["status"].get("elapsed") or 0
            home   = tt(f["teams"]["home"]["name"])
            away   = tt(f["teams"]["away"]["name"])
            hg     = f["goals"]["home"] or 0
            ag     = f["goals"]["away"] or 0
            placar = str(hg) + "x" + str(ag)
            status = f["fixture"]["status"]["short"]
            liga   = tl(f["league"]["name"])

            # Janelas estratégicas para postar ao vivo:
            # - Aos 20-25 min (tendência do 1T estabelecida)
            # - Aos 55-60 min (início do 2T)
            # - Aos 70-75 min (pressão final)
            janelas = [(20,25), (55,60), (70,75)]
            chave = str(fid) + "_" + str(minuto // 5)  # agrupa por bloco de 5 min

            em_janela = any(j[0] <= minuto <= j[1] for j in janelas)

            if em_janela and chave not in ao_vivo_postados:
                # Busca estatísticas do jogo
                stats_str = ""
                try:
                    stats_data = await football_request("fixtures/statistics", {"fixture": fid})
                    stats_list = stats_data.get("response", [])
                    if stats_list:
                        for team_stats in stats_list[:2]:
                            t_name = tt(team_stats["team"]["name"])
                            for s in team_stats.get("statistics", []):
                                if s["type"] in ["Shots on Goal","Corner Kicks","Yellow Cards","Ball Possession"]:
                                    val = s["value"] or 0
                                    stats_str += t_name + " " + s["type"] + ": " + str(val) + " | "
                except:
                    pass

                fixture_info = {
                    "home": home, "away": away, "placar": placar,
                    "minuto": minuto, "league": liga, "stats": stats_str
                }

                texto = await ia_ao_vivo(fixture_info)
                if texto:
                    link = bet365_url(home, away)
                    kb   = InlineKeyboardMarkup([[
                        InlineKeyboardButton("⚡ Apostar AO VIVO na Bet365", url=link)
                    ]])
                    await send_msg(texto, keyboard=kb)
                    ao_vivo_postados.add(chave)
                    logger.info("Ao vivo postado: " + home + " x " + away + " " + str(minuto) + "min")
                    await asyncio.sleep(3)

    except Exception as e:
        logger.error("Erro monitorar ao vivo: " + str(e))

# ── PROCESSAMENTO PRINCIPAL ────────────────────────────────────────────────────
async def processar():
    global multipla_postada, dia_atual
    tz    = pytz.timezone(TIMEZONE)
    agora = datetime.now(tz)
    hoje  = date.today().isoformat()

    # Não verifica entre 2h e 8h — sem jogos nesse horário
    if 2 <= agora.hour < 8:
        return

    logger.info("Verificando jogos...")

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

            if 60 <= mins <= 180 and fid not in simples_postadas:
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

        # ── MÚLTIPLA ─────────────────────────────────────────────────────────
        # Jogos com mais de 40 min de antecedência (ainda dá tempo de apostar)
        futuros = [f for f in priority
                   if (datetime.fromisoformat(f["fixture"]["date"]).astimezone(tz) - agora).total_seconds() / 60 > 40]

        if not multipla_postada and len(futuros) >= 2:
            # Posta múltipla 100-135 min antes do PRÓXIMO jogo que ainda vai acontecer
            proximo = datetime.fromisoformat(futuros[0]["fixture"]["date"]).astimezone(tz)
            mins_p  = (proximo - agora).total_seconds() / 60

            if 60 <= mins_p <= 180:
                logger.info(f"Gerando múltipla ({len(futuros)} jogos futuros)")
                infos = await asyncio.gather(*[coletar_info(f) for f in futuros[:8]])
                texto = await ia_multipla(list(infos))
                if texto:
                    kb     = bet365_btn_multipla()
                    msg_id = await send_msg(texto, keyboard=kb)
                    multiplas_dia.append({
                        "msg_id": msg_id, "texto": texto,
                        "fixture_ids": [f["fixture"]["id"] for f in futuros[:8]],
                        "resultado": None
                    })
                    multipla_postada = True
                    logger.info("✅ Múltipla postada")

        # Se a múltipla já foi postada mas ainda surgiu um novo bloco de jogos
        # distantes mais de 3h do último grupo — posta uma segunda múltipla
        elif multipla_postada and len(futuros) >= 2:
            ultima_multipla_ids = set(multiplas_dia[-1]["fixture_ids"]) if multiplas_dia else set()
            novos = [f for f in futuros if f["fixture"]["id"] not in ultima_multipla_ids]
            if len(novos) >= 2:
                proximo_novo = datetime.fromisoformat(novos[0]["fixture"]["date"]).astimezone(tz)
                mins_novo = (proximo_novo - agora).total_seconds() / 60
                if 60 <= mins_novo <= 180:
                    logger.info(f"Gerando múltipla extra ({len(novos)} novos jogos)")
                    infos = await asyncio.gather(*[coletar_info(f) for f in novos[:8]])
                    texto = await ia_multipla(list(infos))
                    if texto:
                        kb     = bet365_btn_multipla()
                        msg_id = await send_msg(texto, keyboard=kb)
                        multiplas_dia.append({
                            "msg_id": msg_id, "texto": texto,
                            "fixture_ids": [f["fixture"]["id"] for f in novos[:8]],
                            "resultado": None
                        })
                        logger.info("✅ Múltipla extra postada")

        # ── VERIFICA RESULTADOS ──────────────────────────────────────────────
        await verificar_resultados()

        # ── ENQUETES ─────────────────────────────────────────────────────────
        # Posta enquete entre 12h e 20h, se tiver jogos futuros
        hora_atual = agora.hour
        if 12 <= hora_atual <= 20 and len([f for f in priority if (datetime.fromisoformat(f["fixture"]["date"]).astimezone(tz) - agora).total_seconds()/60 > 60]) >= 2:
            await postar_enquete()
        # Verifica enquetes expiradas
        await verificar_enquetes_expiradas()

    except Exception as e:
        logger.error(f"Erro processar: {e}")



async def relatorio_semanal():
    texto = gerar_relatorio("semana")
    if texto:
        await send_msg(texto)
        logger.info("Relatório semanal postado")

async def relatorio_mensal():
    texto = gerar_relatorio("mes")
    if texto:
        await send_msg(texto)
        logger.info("Relatório mensal postado")

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
    ao_vivo_postados.clear()
    multipla_postada = False



async def cmd_link(update, context):
    """
    Comando para atualizar o link do botão da última mensagem postada.
    Uso: /link https://www.bet365.bet.br/...
    Só funciona para o admin (CHAT_ID)
    """
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    user_id  = update.effective_user.id

    if admin_id and user_id != admin_id:
        await update.message.reply_text("Sem permissao.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Uso: /link [simples|multipla] https://link-da-bet365\n\n"
            "Exemplo:\n/link simples https://www.bet365.bet.br/..."
        )
        return

    if len(args) < 2:
        await update.message.reply_text("Informe o tipo e o link. Ex: /link simples https://...")
        return

    tipo = args[0].lower()
    novo_link = args[1]

    # Pega a última mensagem do tipo correto
    msg_id = None
    texto_original = None

    if tipo == "simples" and simples_postadas:
        ultimo = list(simples_postadas.values())[-1]
        msg_id = ultimo.get("msg_id")
        texto_original = ultimo.get("texto", "")
    elif tipo == "multipla" and multiplas_dia:
        ultimo = multiplas_dia[-1]
        msg_id = ultimo.get("msg_id")
        texto_original = ultimo.get("texto", "")

    if not msg_id:
        await update.message.reply_text("Nenhuma mensagem " + tipo + " encontrada para atualizar.")
        return

    # Novo botão com link personalizado
    novo_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎰 Apostar na Bet365", url=novo_link)
    ]])

    try:
        await context.bot.edit_message_reply_markup(
            chat_id=CHANNEL_ID,
            message_id=msg_id,
            reply_markup=novo_kb
        )
        await update.message.reply_text("Botao atualizado com sucesso!")
        logger.info("Link atualizado: " + tipo + " -> " + novo_link)
    except Exception as e:
        await update.message.reply_text("Erro ao atualizar: " + str(e))


async def cmd_link_all(update, context):
    """
    /linkall https://... — atualiza TODOS os botoes do dia com o mesmo link
    """
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if admin_id and update.effective_user.id != admin_id:
        return

    args = context.args
    if not args:
        await update.message.reply_text("Uso: /linkall https://link-da-bet365")
        return

    novo_link = args[0]
    novo_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎰 Apostar na Bet365", url=novo_link)
    ]])

    atualizados = 0

    # Atualiza simples
    for fid, info in simples_postadas.items():
        mid = info.get("msg_id")
        if mid:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=CHANNEL_ID, message_id=mid, reply_markup=novo_kb)
                atualizados += 1
                await asyncio.sleep(0.3)
            except: pass

    # Atualiza múltiplas
    for m in multiplas_dia:
        mid = m.get("msg_id")
        if mid:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=CHANNEL_ID, message_id=mid, reply_markup=novo_kb)
                atualizados += 1
                await asyncio.sleep(0.3)
            except: pass

    await update.message.reply_text(str(atualizados) + " botoes atualizados!")

async def main():
    global dia_atual
    dia_atual = date.today().isoformat()
    me = await bot.get_me()
    logger.info(f"✅ Bot: @{me.username}")
    scheduler.add_job(processar,    "interval", minutes=5,   id="processar")
    scheduler.add_job(resumo_final, "cron", hour=23, minute=30, id="resumo")
    scheduler.add_job(relatorio_semanal, "cron", day_of_week="sun", hour=20, minute=0, id="relatorio_semanal")
    scheduler.add_job(relatorio_mensal,  "cron", day=1, hour=10, minute=0, id="relatorio_mensal")
    scheduler.add_job(verificar_enquetes_expiradas, "interval", minutes=15, id="enquetes")
    scheduler.add_job(limpar,       "cron", hour=0,  minute=5,  id="limpar")
    scheduler.start()
    logger.info("✅ Rodando — simples + múltipla + resultados automáticos")
    try:
        while True: await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
