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

# ─── PADRÕES DE SINAL (Longest Match First) ───

PATTERNS = [
    {"id": 13, "sequencia": ["🔵", "🔵", "🔵", "🔴", "🔴", "🔵", "🔵"], "sinal": "🔴"},
    {"id": 14, "sequencia": ["🔴", "🔴", "🔴", "🔵", "🔵", "🔴", "🔴"], "sinal": "🔵"},
    {"id": 72, "sequencia": ["🔴", "🔴", "🔴", "🔴", "🔴", "🔴"], "sinal": "🔴"},
    {"id": 87, "sequencia": ["🔵", "🔵", "🔵", "🔵", "🔵", "🔵"], "sinal": "🔵"},
    {"id": 21, "sequencia": ["🔵", "🔵", "🔵", "🔴", "🔵", "🔵"], "sinal": "🔵"},
    {"id": 22, "sequencia": ["🔴", "🔴", "🔴", "🔵", "🔴", "🔴"], "sinal": "🔴"},
    {"id": 17, "sequencia": ["🔴", "🔴", "🔵", "🔵", "🔴"], "sinal": "🔴"},
    {"id": 18, "sequencia": ["🔵", "🔵", "🔴", "🔴", "🔵"], "sinal": "🔵"},
    {"id": 23, "sequencia": ["🔵", "🔵", "🔴", "🔵", "🔵"], "sinal": "🔴"},
    {"id": 24, "sequencia": ["🔴", "🔴", "🔵", "🔴", "🔴"], "sinal": "🔵"},
    {"id": 2, "sequencia": ["🔴", "🔴", "🔴", "🔴", "🔴"], "sinal": "🔴"},
    {"id": 3, "sequencia": ["🔵", "🔵", "🔵", "🔵", "🔵"], "sinal": "🔵"},
    {"id": 6, "sequencia": ["🔴", "🔴", "🔴", "🔴", "🔵"], "sinal": "🔵"},
    {"id": 7, "sequencia": ["🔵", "🔵", "🔵", "🔵", "🔴"], "sinal": "🔴"},
    {"id": 8, "sequencia": ["🔴", "🔵", "🔴", "🔵", "🔴"], "sinal": "🔵"},
    {"id": 9, "sequencia": ["🔵", "🔴", "🔵", "🔴", "🔵"], "sinal": "🔴"},
    {"id": 160, "sequencia": ["🔵", "🔴", "🔵", "🔴", "🔴"], "sinal": "🔴"},
    {"id": 25, "sequencia": ["🔵", "🔵", "🔵", "🔵"], "sinal": "🔵"},
    {"id": 26, "sequencia": ["🔴", "🔴", "🔴", "🔴"], "sinal": "🔴"},
    {"id": 19, "sequencia": ["🔴", "🔵", "🔴", "🔴"], "sinal": "🔵"},
    {"id": 20, "sequencia": ["🔵", "🔴", "🔵", "🔵"], "sinal": "🔴"},
    {"id": 1, "sequencia": ["🔵", "🔴", "🔵", "🔴"], "sinal": "🔵"},
    {"id": 5, "sequencia": ["🔴", "🔵", "🔴", "🔵"], "sinal": "🔴"},
    {"id": 4, "sequencia": ["🔴", "🔴", "🔵", "🔵"], "sinal": "🔴"},
    {"id": 249, "sequencia": ["🔴", "🔵", "🔵", "🔴"], "sinal": "🔴"},
    {"id": 150, "sequencia": ["🔵", "🔴", "🔴", "🔵"], "sinal": "🔵"},
    {"id": 525, "sequencia": ["🔴", "🔴", "🔴", "🔵"], "sinal": "🔵"},
    {"id": 526, "sequencia": ["🔵", "🔵", "🔵", "🔴"], "sinal": "🔴"},
    {"id": 39, "sequencia": ["🔴", "🟡", "🔴", "🔵"], "sinal": "🔵"},
    {"id": 40, "sequencia": ["🔵", "🟡", "🔵", "🔴"], "sinal": "🔴"},
    {"id": 41, "sequencia": ["🔴", "🔵", "🟡", "🔴"], "sinal": "🔴"},
    {"id": 42, "sequencia": ["🔵", "🔴", "🟡", "🔵"], "sinal": "🔵"},
    {"id": 43, "sequencia": ["🔴", "🔴", "🔵", "🟡"], "sinal": "🔴"},
    {"id": 44, "sequencia": ["🔵", "🔵", "🔴", "🟡"], "sinal": "🔵"},
    {"id": 31, "sequencia": ["🔴", "🟡", "🔴", "🟡"], "sinal": "🔴"},
    {"id": 40, "sequencia": ["🟡", "🔴", "🟡", "🔴"], "sinal": "🔵"},
    {"id": 51, "sequencia": ["🔵", "🟡", "🔵", "🟡"], "sinal": "🔵"},
    {"id": 63, "sequencia": ["🟡", "🔵", "🟡", "🔵"], "sinal": "🔵"},
    {"id": 95, "sequencia": ["🟡", "🟡", "🟡", "🟡"], "sinal": "🟡"},
    {"id": 34, "sequencia": ["🔵", "🔵", "🔵"], "sinal": "🔴"},
    {"id": 15, "sequencia": ["🔴", "🔴", "🟡"], "sinal": "🔴"},
    {"id": 16, "sequencia": ["🔵", "🔵", "🟡"], "sinal": "🔵"},
    {"id": 45, "sequencia": ["🔵", "🟡", "🟡"], "sinal": "🔵"},
    {"id": 46, "sequencia": ["🔴", "🟡", "🟡"], "sinal": "🔴"},
    {"id": 420, "sequencia": ["🔴", "🟡", "🔴"], "sinal": "🔴"},
    {"id": 424, "sequencia": ["🔵", "🟡", "🔵"], "sinal": "🔵"},
    {"id": 124, "sequencia": ["🔴", "🔴", "🔵"], "sinal": "🔵"},
    {"id": 131, "sequencia": ["🔵", "🔵", "🔴"], "sinal": "🔴"},
    {"id": 142, "sequencia": ["🔵", "🔴", "🔵"], "sinal": "🔵"},
    {"id": 157, "sequencia": ["🔴", "🔵", "🔴"], "sinal": "🔴"},
    {"id": 10, "sequencia": ["🔵", "🔴"], "sinal": "🔵"},
    {"id": 11, "sequencia": ["🔴", "🔵"], "sinal": "🔴"},
]

# Pré-ordenar: padrão mais longo primeiro, depois ID maior em caso de empate
PATTERNS.sort(key=lambda p: (-len(p["sequencia"]), -p["id"]))


# ─── MOTOR DE DECISÃO (PATTERN MATCHING) ───

def gerar_sinal_estrategia(history: List[str], player_score=None, banker_score=None) -> Tuple[Optional[str], Optional[str]]:
    if len(history) < 2:
        return None, None

    for pattern in PATTERNS:
        seq = pattern["sequencia"]
        seq_len = len(seq)
        if len(history) >= seq_len and history[-seq_len:] == seq:
            nome = f"Padrão #{pattern['id']} ({''.join(seq)})"
            return nome, pattern["sinal"]

    return None, None


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

    if len(state["history"]) < 2:
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
