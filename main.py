import os
import json
import asyncio
import logging
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime
import pytz
from collections import Counter, defaultdict
import aiohttp
from telegram import Bot
from telegram.error import TelegramError
from dotenv import load_dotenv

load_dotenv()

# Configurações
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7758723414:AAF-Zq1QPoGy2IS-iK2Wh28PfexP0_mmHHc")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "-1003816007727")
API_URL = "https://api.signals-house.com/validate/results?tableId=1&lastResult=13412758"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}
ANGOLA_TZ = pytz.timezone('Africa/Luanda')
OUTCOME_MAP = {
    "PlayerWon": "🔵",
    "BankerWon": "🔴",
    "Tie": "🟡",
    "Player": "🔵",
    "Banker": "🔴",
    "🔵": "🔵",
    "🔴": "🔴",
    "🟡": "🟡",
}

# ─── TIMING ───
API_POLL_INTERVAL = 2.0
SIGNAL_COOLDOWN_DURATION = 5
STATE_FILE = "bot_state.json"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-5s | %(message)s'
)
logger = logging.getLogger("BacBoBot")
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# ─── STICKERS ───
GREEN_STICKER_ID = "CAACAgEAAxkBAAMGablwQw7_e6LQPpkPLMGUT_7XHlsAApECAAJAHLhE5HwaQw6L5SA6BA"
LOSS_STICKER_ID = "CAACAgEAAxkBAAMHablwRJ3yTERtooEJKzCbGMCfvv8AAucCAALGBLlEm0eHrOWqoe06BA"

state: Dict[str, Any] = {
    "history": [],
    "last_round_id": None,
    "waiting_for_result": False,
    "last_signal_color": None,
    "martingale_count": 0,
    "entrada_message_id": None,
    "martingale_message_ids": [],
    "greens_seguidos": 0,
    "total_greens": 0,
    "greens_sem_gale": 0,
    "greens_gale_1": 0,
    "greens_gale_2": 0,
    "total_empates": 0,
    "total_losses": 0,
    "last_signal_pattern": None,
    "last_signal_sequence": None,
    "last_signal_round_id": None,
    "signal_cooldown_until": 0.0,
    "analise_message_id": None,
    "last_reset_date": None,
    "last_analise_refresh": 0.0,
    "last_result_round_id": None,
    "player_score_last": None,
    "banker_score_last": None,
}

# ─── PERSISTÊNCIA DO PLACAR ───
def save_state():
    try:
        data = {
            "total_greens": state["total_greens"],
            "greens_sem_gale": state["greens_sem_gale"],
            "greens_gale_1": state["greens_gale_1"],
            "greens_gale_2": state["greens_gale_2"],
            "total_empates": state["total_empates"],
            "total_losses": state["total_losses"],
            "greens_seguidos": state["greens_seguidos"],
        }
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.debug(f"Erro ao salvar estado: {e}")

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        for k in ["total_greens", "greens_sem_gale", "greens_gale_1", "greens_gale_2",
                  "total_empates", "total_losses", "greens_seguidos"]:
            if k in data:
                state[k] = data[k]
        logger.info(f"Estado carregado: Greens={state['total_greens']} Losses={state['total_losses']}")
    except FileNotFoundError:
        logger.info("Nenhum estado anterior encontrado, começando do zero.")
    except Exception as e:
        logger.debug(f"Erro ao carregar estado: {e}")

# ─── TELEGRAM HELPERS ───
async def send_to_channel(text: str, parse_mode="HTML", disable_preview=True, reply_to_message_id=None) -> Optional[int]:
    try:
        msg = await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_preview,
            reply_to_message_id=reply_to_message_id
        )
        return msg.message_id
    except Exception as e:
        logger.error(f"Erro ao enviar texto: {e}")
        return None

async def send_sticker_to_channel(sticker_id: str) -> Optional[int]:
    try:
        msg = await bot.send_sticker(
            chat_id=TELEGRAM_CHANNEL_ID,
            sticker=sticker_id
        )
        return msg.message_id
    except Exception as e:
        logger.error(f"Erro ao enviar sticker: {e}")
        return None

async def send_error_to_channel(error_msg: str):
    timestamp = datetime.now(ANGOLA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    text = f"⚠️ <b>ERRO DETECTADO</b> ⚠️\n<code>{timestamp}</code>\n\n{error_msg}"
    await send_to_channel(text)

async def delete_messages(message_ids: List[int]):
    if not message_ids:
        return
    for mid in message_ids[:]:
        try:
            await bot.delete_message(TELEGRAM_CHANNEL_ID, mid)
        except:
            pass

def calcular_acertividade() -> str:
    total = state["total_greens"] + state["total_losses"]
    return "0.00%" if total == 0 else f"{(state['total_greens'] / total * 100):.2f}%"

def format_placar() -> str:
    acert = calcular_acertividade()
    return (
        f"📊 Placar atual 🟢 {state['total_greens']} 🔴 {state['total_losses']}\n"
        f"✅ Assertividade {acert}\n"
        f"🏆 {state['greens_seguidos']} Greens seguidos"
    )

def format_analise_text() -> str:
    return "🎲 <b>ANALISANDO...</b> 🎲\n<i>Aguarde sinal</i>"

async def refresh_analise_message():
    await delete_analise_message()
    msg_id = await send_to_channel(format_analise_text())
    if msg_id:
        state["analise_message_id"] = msg_id

async def delete_analise_message():
    if state["analise_message_id"] is not None:
        await delete_messages([state["analise_message_id"]])
        state["analise_message_id"] = None

# ─── API ───
async def fetch_api(session: aiohttp.ClientSession) -> Optional[Dict]:
    try:
        async with session.get(API_URL, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=7)) as resp:
            if resp.status == 200:
                return await resp.json()
            return None
    except:
        return None

async def update_history_from_api(session) -> bool:
    data = await fetch_api(session)
    if not data:
        return False
    try:
        items = data.get("data", [])
        if not isinstance(items, list) or len(items) == 0:
            return False
        latest = items[0]
        round_id = latest.get("id")
        if not round_id:
            return False
        if state["last_round_id"] == round_id:
            return False
        outcome_raw = latest.get("result")
        if not outcome_raw:
            return False
        score = latest.get("score")
        outcome = OUTCOME_MAP.get(outcome_raw)
        if not outcome:
            s = str(outcome_raw or "").lower()
            if "player" in s: outcome = "🔵"
            elif "banker" in s: outcome = "🔴"
            elif any(x in s for x in ["tie", "empate", "draw"]): outcome = "🟡"
        if not outcome:
            return False
        state["last_round_id"] = round_id
        state["history"].append(outcome)
        state["player_score_last"] = None
        state["banker_score_last"] = None
        if len(state["history"]) > 200:
            state["history"].pop(0)
        logger.info(f"🔔 NOVA RODADA DETECTADA: {outcome} (round {round_id}, score={score})")
        return True
    except Exception as e:
        logger.debug(f"Erro processando API: {e}")
        return False

# ─── ESTRATÉGIAS VIP + MARKOV ───

def oposto(cor: str) -> str:
    return "🔵" if cor == "🔴" else "🔴"


def estrategia_tendencia(hist: List[str]) -> Optional[Tuple[str, str]]:
    if len(hist) < 4:
        return None
    last = hist[-4:]
    cnt = Counter(last)
    if cnt["🔵"] >= 3:
        return ("Tendência Azul", "🔵")
    if cnt["🔴"] >= 3:
        return ("Tendência Vermelho", "🔴")
    return None


def estrategia_quebra_sequencia(hist: List[str]) -> Optional[Tuple[str, str]]:
    if len(hist) < 4:
        return None
    if hist[-1] == hist[-2] == hist[-3] == hist[-4]:
        return ("Quebra 4x", oposto(hist[-1]))
    return None


def estrategia_alternancia(hist: List[str]) -> Optional[Tuple[str, str]]:
    if len(hist) < 4:
        return None
    a, b, c, d = hist[-4:]
    if a == c and b == d and a != b:
        return ("Alternância ABAB", oposto(d))
    return None


def estrategia_2x1(hist: List[str]) -> Optional[Tuple[str, str]]:
    if len(hist) < 6:
        return None
    seq = hist[-6:]
    if seq[0] == seq[1] and seq[2] != seq[1] and \
       seq[3] == seq[4] and seq[5] != seq[4]:
        if seq[3] == seq[4]:
            return ("Padrão 2x1", seq[5])
    return None


def estrategia_2x2(hist: List[str]) -> Optional[Tuple[str, str]]:
    if len(hist) < 4:
        return None
    a, b, c, d = hist[-4:]
    if a == b and c == d and a != c:
        return ("Padrão 2x2", c)
    return None


def estrategia_3x3(hist: List[str]) -> Optional[Tuple[str, str]]:
    if len(hist) < 6:
        return None
    seq = hist[-6:]
    if seq[0] == seq[1] == seq[2] and seq[3] == seq[4] == seq[5] and seq[0] != seq[3]:
        return ("Padrão 3x3", seq[3])
    return None


def estrategia_maioria(hist: List[str]) -> Optional[Tuple[str, str]]:
    if len(hist) < 6:
        return None
    window = hist[-6:]
    cnt = Counter(window)
    if cnt["🔵"] >= 4:
        return ("Maioria Azul", "🔵")
    if cnt["🔴"] >= 4:
        return ("Maioria Vermelho", "🔴")
    return None


def estrategia_markov(hist: List[str], order: int = 2) -> Optional[Tuple[str, str]]:
    """
    Modelo de Markov simples de ordem 2:
    Procura a sequência dos últimos 'order' resultados e vê qual cor veio mais vezes depois dela.
    """
    if len(hist) < order + 1:
        return None

    # Construir tabela de transições (só considera 🔵 e 🔴, ignora 🟡 para simplificar)
    transitions = defaultdict(Counter)
    for i in range(order, len(hist)):
        prev = tuple(hist[i - order:i])
        next_color = hist[i]
        if next_color in ("🔵", "🔴"):
            transitions[prev][next_color] += 1

    # Sequência atual
    current_seq = tuple(hist[-order:])

    if current_seq not in transitions or sum(transitions[current_seq].values()) == 0:
        return None

    # Cor mais frequente após essa sequência
    most_common = transitions[current_seq].most_common(1)
    if not most_common:
        return None

    predicted_color, count = most_common[0]
    nome = f"Markov ordem {order} ({''.join(current_seq)} → {predicted_color})"

    return (nome, predicted_color)


# ─── MOTOR DE DECISÃO (VOTAÇÃO) ───
def gerar_sinal_estrategia(history: List[str], player_score=None, banker_score=None) -> Tuple[Optional[str], Optional[str]]:

    if len(history) < 4:
        return None, None

    votos = {"🔵": 0.0, "🔴": 0.0}
    nome = None

    estrategias = [
        (estrategia_tendencia,        3.0),
        (estrategia_quebra_sequencia, 3.0),
        (estrategia_2x1,              2.5),
        (estrategia_2x2,              2.5),
        (estrategia_3x3,              2.5),
        (estrategia_alternancia,      2.0),
        (estrategia_maioria,          2.0),
        (estrategia_markov,           2.0),
    ]

    for func, peso in estrategias:
        res = func(history)
        if res:
            n, cor = res
            votos[cor] += peso
            if nome is None:
                nome = n

    if votos["🔵"] == 0 and votos["🔴"] == 0:
        return None, None

    cor_final = "🔵" if votos["🔵"] > votos["🔴"] else "🔴"

    return nome or "Estratégia VIP + Markov", cor_final

# ─── MENSAGEM DE SINAL ───
def main_entry_text(color: str) -> str:
    return (
        f"✅ ENTRADA CONFIRMADA\n"
        f"ENTRAR NA COR {color}\n"
        f"PROTEJA O EMPATE 🟡\n"
        f"FAZER NO MÁXIMO 2 GALE🤖"
    )

async def send_gale_warning(level: int):
    text = f"🔄 <b>GALE {level}</b> 🔄\nContinuar na mesma cor!"
    msg_id = await send_to_channel(text)
    if msg_id:
        state["martingale_message_ids"].append(msg_id)

async def clear_gale_messages():
    await delete_messages(state["martingale_message_ids"])
    state["martingale_message_ids"] = []

def _reset_signal_state():
    state.update({
        "waiting_for_result": False,
        "last_signal_color": None,
        "martingale_count": 0,
        "entrada_message_id": None,
        "last_signal_pattern": None,
        "last_signal_sequence": None,
        "last_signal_round_id": None,
        "signal_cooldown_until": datetime.now().timestamp() + 2
    })

async def resolve_after_result():
    if not state.get("waiting_for_result") or not state.get("last_signal_color"):
        return
    if not state["history"]:
        return
    if state["last_result_round_id"] == state["last_round_id"]:
        return
    if state["last_signal_round_id"] and state["last_signal_round_id"] >= state["last_round_id"]:
        return
    last_outcome = state["history"][-1]
    state["last_result_round_id"] = state["last_round_id"]
    target = state["last_signal_color"]
    acertou = last_outcome == target
    is_tie = last_outcome == "🟡"

    if acertou or is_tie:
        # ─── ACERTO ───
        state["total_greens"] += 1
        state["greens_seguidos"] += 1
        gale = state["martingale_count"]
        if gale == 0:
            state["greens_sem_gale"] += 1
            green_text = "GREEN DE PRIMEIRA✅"
        elif gale == 1:
            state["greens_gale_1"] += 1
            green_text = "GREEN NO GALE 1✅"
        else:
            state["greens_gale_2"] += 1
            green_text = "GREEN NO GALE 2✅"

        # 1) Mensagem de acerto respondendo ao sinal original
        await send_to_channel(green_text, reply_to_message_id=state["entrada_message_id"])
        # 2) Sticker de acerto
        await send_sticker_to_channel(GREEN_STICKER_ID)
        # 3) Placar com sequência de greens
        await send_to_channel(format_placar())
        await clear_gale_messages()
        _reset_signal_state()
        save_state()
        return

    # ─── ERROU ───
    state["martingale_count"] += 1

    if state["martingale_count"] == 1:
        # Primeiro gale
        await send_gale_warning(1)
        return

    if state["martingale_count"] == 2:
        # Segundo gale
        await send_gale_warning(2)
        return

    # Já passou do gale 2 (martingale_count >= 3) → LOSS
    state["greens_seguidos"] = 0
    state["total_losses"] += 1
    await send_sticker_to_channel(LOSS_STICKER_ID)
    await send_to_channel(format_placar())
    await clear_gale_messages()
    _reset_signal_state()
    save_state()
    await refresh_analise_message()

async def try_send_signal():
    now = datetime.now().timestamp()
    if state["waiting_for_result"]:
        await delete_analise_message()
        return
    if now < state["signal_cooldown_until"]:
        return
    if len(state["history"]) < 4:
        return
    padrao, cor = gerar_sinal_estrategia(
        state["history"],
        state.get("player_score_last"),
        state.get("banker_score_last")
    )
    if not cor:
        await refresh_analise_message()
        return
    seq = "".join(state["history"][-7:])
    if state["last_signal_pattern"] == padrao and state["last_signal_sequence"] == seq:
        await refresh_analise_message()
        return
    await delete_analise_message()
    state["martingale_message_ids"] = []
    msg_id = await send_to_channel(main_entry_text(cor), disable_preview=False)
    if msg_id:
        state["entrada_message_id"] = msg_id
        state["waiting_for_result"] = True
        state["last_signal_color"] = cor
        state["martingale_count"] = 0
        state["last_signal_pattern"] = padrao
        state["last_signal_sequence"] = seq
        state["last_signal_round_id"] = state["last_round_id"]
        state["signal_cooldown_until"] = now + SIGNAL_COOLDOWN_DURATION
        logger.info(f"⚡ SINAL ENVIADO → {cor} ({padrao}) — durante betting time")

async def api_worker():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                nova_rodada = await update_history_from_api(session)
                if nova_rodada:
                    await resolve_after_result()
                    await asyncio.sleep(0.3)
                    await try_send_signal()
                await asyncio.sleep(API_POLL_INTERVAL)
            except Exception as e:
                logger.debug(f"Erro loop principal: {e}")
                await asyncio.sleep(API_POLL_INTERVAL)

async def main():
    load_state()
    logger.info("Bot iniciado...")
    await send_to_channel("🤖 BOT INICIADO 🤖")
    await refresh_analise_message()
    await api_worker()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot parado pelo usuário")
    except Exception as e:
        logger.critical("Erro fatal", exc_info=True)
