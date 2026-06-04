import os
import asyncio
import httpx
import pytz
import logging
from datetime import date, datetime, timedelta
from anthropic import Anthropic
from telegram import Bot
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("CANAL_BOT_TOKEN", "")
CHANNEL_ID       = os.environ.get("CHANNEL_ID", "@seutipster")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TIMEZONE         = "America/Sao_Paulo"
FOOTBALL_API_URL = "https://v3.football.api-sports.io"

# IDs das ligas prioritárias
PRIORITY_LEAGUE_IDS = {
    1, 2, 3, 9, 10, 11, 13, 29, 39, 61, 71, 72, 73, 75, 76,
    78, 94, 128, 135, 140, 203, 262, 307, 612, 667, 848, 914
}

bot = Bot(token=TELEGRAM_TOKEN)
anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Controle de jogos já postados
posted_2h = set()
posted_1h = set()
posted_30m = set()

# ── HELPERS ──────────────────────────────────────────────────────────────────
async def football_request(endpoint: str, params: dict) -> dict:
    headers = {
        "x-apisports-key": FOOTBALL_API_KEY,
        "x-rapidapi-key": FOOTBALL_API_KEY,
        "x-rapidapi-host": "v3.football.api-sports.io"
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{FOOTBALL_API_URL}/{endpoint}", headers=headers, params=params)
        r.raise_for_status()
        return r.json()


def translate_team(name: str) -> str:
    teams = {
        "Brazil": "Brasil", "Argentina": "Argentina", "Germany": "Alemanha",
        "France": "França", "Spain": "Espanha", "Portugal": "Portugal",
        "Italy": "Itália", "Netherlands": "Holanda", "England": "Inglaterra",
        "Uruguay": "Uruguai", "Colombia": "Colômbia", "Chile": "Chile",
        "Belgium": "Bélgica", "Egypt": "Egito", "USA": "EUA",
        "Mexico": "México", "Japan": "Japão", "South Korea": "Coreia do Sul",
        "Paris Saint-Germain": "PSG", "Bayern Munich": "Bayern",
        "Atletico Madrid": "Atlético de Madrid", "FC Barcelona": "Barcelona",
        "Ivory Coast": "Costa do Marfim", "Saudi Arabia": "Arábia Saudita",
    }
    return teams.get(name, name)


def translate_league(name: str) -> str:
    leagues = {
        "International Friendlies": "Amistosos Internacionais",
        "UEFA Champions League": "Champions League",
        "UEFA Europa League": "Europa League",
        "Copa America": "Copa América",
        "CONMEBOL Libertadores": "Libertadores",
        "CONMEBOL Sudamericana": "Sul-Americana",
        "Copa Do Brasil": "Copa do Brasil",
        "Friendlies Clubs": "Amistosos de Clubes",
        "Tournoi Maurice Revello": "Tournoi Maurice Revello",
    }
    return leagues.get(name, name)


def bet365_link(home: str, away: str) -> str:
    """Gera link de busca na Bet365"""
    query = f"{home} {away}".replace(" ", "+")
    return f"https://www.bet365.com/#/AC/B1/C1/D1002/E^{query}/"


# ── ANÁLISE ──────────────────────────────────────────────────────────────────
CANAL_PROMPT = """Você é um tipster profissional. Analise o jogo e retorne APENAS os melhores palpites para postar num canal do Telegram.

REGRA: Nunca escreva "dado insuficiente". Sempre estime com base no contexto.

Formato de resposta — texto puro para Telegram (use apenas *negrito* e emojis):

✅ ANÁLISE CONCLUÍDA - SEU TIPSTER ✅

🏟️ *[Time Casa] x [Time Fora]*
🏆 [Competição] | 📅 [Data] • [Hora]

——————————————————
📊 *PROBABILIDADES*
🏠 [Time Casa]: X%
🤝 Empate: X%
✈️ [Time Fora]: X%

——————————————————
🏠 *[TIME CASA]* — Forma: [V V D E V]
⚽ Gols: X marcados/jogo | X sofridos/jogo
🚩 Escanteios: X/jogo | 🟨 Cartões: X/jogo

✈️ *[TIME FORA]* — Forma: [V D E V V]
⚽ Gols: X marcados/jogo | X sofridos/jogo
🚩 Escanteios: X/jogo | 🟨 Cartões: X/jogo

——————————————————
🔄 *H2H* — Média gols: X | BTTS: X% | Over 2.5: X%

——————————————————
🎯 *PALPITES SELECIONADOS*

✅ *[Mercado]* — odd mín X.XX
📌 [Justificativa com número]

✅ *[Mercado]* — odd mín X.XX
📌 [Justificativa]

⚡ *[Mercado]* — odd mín X.XX
📌 [Justificativa]

⚡ *[Mercado]* — odd mín X.XX
📌 [Justificativa]

🔥 *[Mercado]* — odd mín X.XX
📌 [Justificativa]

——————————————————
⚠️ *RISCO:* [1 linha resumida]

Inclua 5-7 palpites dos melhores mercados disponíveis."""


async def gerar_analise(fixture: dict) -> str:
    home_id = fixture["teams"]["home"]["id"]
    away_id = fixture["teams"]["away"]["id"]
    home_name = translate_team(fixture["teams"]["home"]["name"])
    away_name = translate_team(fixture["teams"]["away"]["name"])
    league_name = translate_league(fixture["league"]["name"])

    tz = pytz.timezone(TIMEZONE)
    dt = datetime.fromisoformat(fixture["fixture"]["date"]).astimezone(tz)
    hora = dt.strftime("%H:%M")
    data_fmt = dt.strftime("%d/%m/%Y")

    # Busca dados
    try:
        home_data, away_data, h2h_data = await asyncio.gather(
            football_request("fixtures", {"team": home_id, "last": 8, "timezone": TIMEZONE}),
            football_request("fixtures", {"team": away_id, "last": 8, "timezone": TIMEZONE}),
            football_request("fixtures/headtohead", {"h2h": f"{home_id}-{away_id}", "last": 10}),
            return_exceptions=True
        )
    except Exception as e:
        logger.error(f"Erro ao buscar dados: {e}")
        home_data, away_data, h2h_data = {}, {}, {}

    def parse_form(data, team_id):
        results = []
        for m in (data.get("response", []) if isinstance(data, dict) else [])[:5]:
            is_home = m["teams"]["home"]["id"] == team_id
            hg = m["goals"]["home"] or 0
            ag = m["goals"]["away"] or 0
            if is_home:
                results.append("V" if hg > ag else ("E" if hg == ag else "D"))
            else:
                results.append("V" if ag > hg else ("E" if hg == ag else "D"))
        return results

    home_form = parse_form(home_data, home_id)
    away_form = parse_form(away_data, away_id)

    h2h_list = []
    for m in (h2h_data.get("response", []) if isinstance(h2h_data, dict) else [])[:5]:
        h2h_list.append({
            "home": translate_team(m["teams"]["home"]["name"]),
            "away": translate_team(m["teams"]["away"]["name"]),
            "score": f"{m['goals']['home'] or 0}-{m['goals']['away'] or 0}",
        })

    context = f"""Jogo: {home_name} x {away_name}
Liga: {league_name}
Data: {data_fmt} às {hora}
Forma {home_name}: {' '.join(home_form) if home_form else 'N/A'}
Forma {away_name}: {' '.join(away_form) if away_form else 'N/A'}
H2H recente: {h2h_list}"""

    try:
        response = anthropic.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system=CANAL_PROMPT,
            messages=[{"role": "user", "content": context}]
        )
        return response.content[0].text
    except Exception as e:
        logger.error(f"Erro Claude: {e}")
        return None


# ── ENVIO DE MENSAGENS ────────────────────────────────────────────────────────
async def post_analise_completa(fixture: dict):
    """Posta análise completa 2h antes"""
    home = translate_team(fixture["teams"]["home"]["name"])
    away = translate_team(fixture["teams"]["away"]["name"])
    league = translate_league(fixture["league"]["name"])
    fid = fixture["fixture"]["id"]

    tz = pytz.timezone(TIMEZONE)
    dt = datetime.fromisoformat(fixture["fixture"]["date"]).astimezone(tz)
    hora = dt.strftime("%H:%M")

    logger.info(f"Gerando análise 2h: {home} x {away}")
    analise = await gerar_analise(fixture)

    if not analise:
        return

    link = bet365_link(home, away)
    mensagem = f"{analise}\n\n🎰 *Aposte na Bet365:*\n[Clique aqui para apostar]({link})"

    try:
        # Divide se necessário
        if len(mensagem) > 4096:
            parte1 = mensagem[:4000]
            parte2 = mensagem[4000:]
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=parte1,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=parte2,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=mensagem,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
        posted_2h.add(fid)
        logger.info(f"✅ Análise 2h postada: {home} x {away}")
    except Exception as e:
        logger.error(f"Erro ao postar: {e}")


async def post_lembrete_1h(fixture: dict):
    """Posta lembrete 1h antes"""
    home = translate_team(fixture["teams"]["home"]["name"])
    away = translate_team(fixture["teams"]["away"]["name"])
    league = translate_league(fixture["league"]["name"])
    fid = fixture["fixture"]["id"]

    tz = pytz.timezone(TIMEZONE)
    dt = datetime.fromisoformat(fixture["fixture"]["date"]).astimezone(tz)
    hora = dt.strftime("%H:%M")
    link = bet365_link(home, away)

    mensagem = (
        f"⏰ *1 HORA PARA O JOGO!*\n\n"
        f"🏟️ *{home} x {away}*\n"
        f"🏆 {league} | 🕐 {hora}\n\n"
        f"Já analisamos este jogo — veja os palpites acima ⬆️\n\n"
        f"🎰 [Aposte agora na Bet365]({link})"
    )

    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=mensagem,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        posted_1h.add(fid)
        logger.info(f"✅ Lembrete 1h postado: {home} x {away}")
    except Exception as e:
        logger.error(f"Erro ao postar lembrete 1h: {e}")


async def post_alerta_30m(fixture: dict):
    """Posta alerta final 30min antes"""
    home = translate_team(fixture["teams"]["home"]["name"])
    away = translate_team(fixture["teams"]["away"]["name"])
    league = translate_league(fixture["league"]["name"])
    fid = fixture["fixture"]["id"]

    tz = pytz.timezone(TIMEZONE)
    dt = datetime.fromisoformat(fixture["fixture"]["date"]).astimezone(tz)
    hora = dt.strftime("%H:%M")
    link = bet365_link(home, away)

    mensagem = (
        f"🚨 *ÚLTIMA CHANCE — 30 MINUTOS!*\n\n"
        f"🏟️ *{home} x {away}*\n"
        f"🏆 {league} | 🕐 {hora}\n\n"
        f"⚡ As odds estão fechando — hora de apostar!\n\n"
        f"🎰 [Clique aqui para apostar na Bet365]({link})\n\n"
        f"📊 _Análise completa postada acima ⬆️_"
    )

    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=mensagem,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        posted_30m.add(fid)
        logger.info(f"✅ Alerta 30min postado: {home} x {away}")
    except Exception as e:
        logger.error(f"Erro ao postar alerta 30min: {e}")


# ── VERIFICAÇÃO PERIÓDICA ─────────────────────────────────────────────────────
async def verificar_jogos():
    """Verifica jogos próximos e posta quando necessário"""
    logger.info("Verificando jogos próximos...")
    try:
        today = date.today().isoformat()
        data = await football_request("fixtures", {"date": today, "timezone": TIMEZONE})
        fixtures = data.get("response", [])

        tz = pytz.timezone(TIMEZONE)
        agora = datetime.now(tz)

        for f in fixtures:
            # Filtra apenas ligas prioritárias
            if f["league"]["id"] not in PRIORITY_LEAGUE_IDS:
                continue

            # Só jogos não iniciados
            if f["fixture"]["status"]["short"] != "NS":
                continue

            fid = f["fixture"]["id"]
            dt_jogo = datetime.fromisoformat(f["fixture"]["date"]).astimezone(tz)
            minutos_restantes = (dt_jogo - agora).total_seconds() / 60

            home = translate_team(f["teams"]["home"]["name"])
            away = translate_team(f["teams"]["away"]["name"])

            # 2h antes (entre 130 e 110 minutos)
            if 110 <= minutos_restantes <= 130 and fid not in posted_2h:
                logger.info(f"Agendando análise 2h: {home} x {away} ({minutos_restantes:.0f}min)")
                asyncio.create_task(post_analise_completa(f))

            # 1h antes (entre 65 e 55 minutos)
            elif 55 <= minutos_restantes <= 65 and fid not in posted_1h:
                logger.info(f"Agendando lembrete 1h: {home} x {away}")
                asyncio.create_task(post_lembrete_1h(f))

            # 30min antes (entre 35 e 25 minutos)
            elif 25 <= minutos_restantes <= 35 and fid not in posted_30m:
                logger.info(f"Agendando alerta 30min: {home} x {away}")
                asyncio.create_task(post_alerta_30m(f))

    except Exception as e:
        logger.error(f"Erro ao verificar jogos: {e}")


async def limpar_cache():
    """Limpa cache diariamente à meia-noite"""
    posted_2h.clear()
    posted_1h.clear()
    posted_30m.clear()
    logger.info("Cache limpo")


# ── MAIN ──────────────────────────────────────────────────────────────────────
async def main():
    logger.info("🚀 Canal Bot iniciando...")

    # Verifica conexão
    me = await bot.get_me()
    logger.info(f"✅ Bot conectado: @{me.username}")

    # Agenda verificação a cada 5 minutos
    scheduler.add_job(verificar_jogos, "interval", minutes=5, id="verificar")
    scheduler.add_job(limpar_cache, "cron", hour=0, minute=0, id="limpar")
    scheduler.start()

    logger.info("✅ Canal Bot rodando — verificando jogos a cada 5 minutos")

    # Roda indefinidamente
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
