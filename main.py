import time
import os
import random
import signal
import sys
import logging
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
import cv2
import numpy as np
from telegram import Bot
from telegram.error import TelegramError
from datetime import date

# CONFIGURAÇÕES HARDCODED - EDITE AQUI COM SEUS VALORES REAIS (SEGURANÇA: NÃO PUSH SEM ALTERAR!)
USERNAME = '931787918'  # Seu login Elephant Bet Angola
PASSWORD = '97713'  # Sua senha
TELEGRAM_TOKEN = "8344261996:AAEgDWaIb7hzknPpTQMdiYKSE3hjzP0mqFc"
CHAT_ID = "-1002783091818"
APOSTA_VALOR = 1000  # Valor fixo da aposta em KZ
MIN_SALDO = 1000  # Mínimo para apostar
DAILY_MAX = 10  # Máximo 10 apostas POR DIA (só quando detecta tendência, independente de acerto/erro)
LIMITE_PERDA = 3  # Pare se perda total > isso (em KZ)

# Verifica credenciais (agora hardcoded, sempre "existem")
if not all([USERNAME, PASSWORD, TELEGRAM_TOKEN, CHAT_ID]):
    raise ValueError("Preencha as credenciais hardcoded em main.py!")

# Inicializa Telegram
telegram_bot = Bot(token=TELEGRAM_TOKEN)

# ID da mensagem de espera
msg_espera_id = None

# Configura logging para capturar erros e enviar pro Telegram
class TelegramHandler(logging.Handler):
    def __init__(self, chat_id, bot):
        super().__init__()
        self.chat_id = chat_id
        self.bot = bot
        self.last_error_time = 0
        self.error_cooldown = 60  # 1min entre notificações do mesmo tipo para evitar spam

    def emit(self, record):
        if record.levelno >= logging.ERROR and time.time() - self.last_error_time > self.error_cooldown:
            mensagem = f"🚨 ERRO NO BOT: {record.getMessage()}\n📅 {time.strftime('%Y-%m-%d %H:%M:%S')}"
            try:
                self.bot.send_message(chat_id=self.chat_id, text=mensagem, parse_mode='HTML')
                self.last_error_time = time.time()
            except TelegramError as e:
                print(f"Falha ao enviar erro pro Telegram: {e}", file=sys.stderr)

# Configura logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
telegram_handler = TelegramHandler(CHAT_ID, telegram_bot)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
telegram_handler.setFormatter(formatter)
logger.addHandler(telegram_handler)

# Console handler (para Replit)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# Stats globais
acertos = 0
erros = 0
saldo_atual = 0.0
historico_resultados = []
apostas_feitas = 0
daily_apostas = 0
ultima_data = date.today()
patrimonio_inicial = 0.0

# Padrões (mantidos)
PADROES = [
    (['🔴', '🔴', '🔴', '🔵', '🔴', '🔴', '🔴'], '🔵'), 
    (['🔵', '🔵', '🔵', '🔴', '🔵', '🔵', '🔵'], '🔴'), 
    (['🔴', '🔴', '🔴', '🔴', '🔴', '🔴'], '🔴'),
    (['🔴', '🔴', '🔵', '🔵', '🔴'], '🔴'),
    (['🔵', '🔵', '🔴', '🔴', '🔵'], '🔵'),
    (['🔴', '🔵', '🔴', '🔵', '🔴', '🔵'], '🔴'),
    (['🔵', '🔴', '🔵', '🔴', '🔵', '🔴'], '🔵')
]

def signal_handler(sig, frame):
    logger.info("Shutdown gracioso...")
    enviar_notificacao("Bot parando graciosamente. 👋")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def enviar_notificacao(mensagem, message_id=None):
    try:
        if message_id:
            telegram_bot.edit_message_text(chat_id=CHAT_ID, message_id=message_id, text=mensagem, parse_mode='HTML')
        else:
            sent_msg = telegram_bot.send_message(chat_id=CHAT_ID, text=mensagem, parse_mode='HTML')
            return sent_msg.message_id
        logger.info(f"Notificação: {mensagem}")
    except TelegramError as e:
        logger.error(f"Erro Telegram: {e}")

# Configura Firefox para Replit
from webdriver_manager.firefox import GeckoDriverManager
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.firefox.options import Options as FirefoxOptions

firefox_options = FirefoxOptions()
firefox_options.add_argument('--headless')
firefox_options.add_argument('--no-sandbox')
firefox_options.add_argument('--disable-dev-shm-usage')
firefox_options.add_argument('--disable-gpu')
firefox_options.add_argument('--disable-extensions')
firefox_options.set_preference("dom.webdriver.enabled", False)
firefox_options.set_preference("useAutomationExtension", False)

# Cria driver com retry
driver = None
max_retries = 3
for retry in range(max_retries):
    try:
        service = Service(GeckoDriverManager().install())
        driver = webdriver.Firefox(service=service, options=firefox_options)
        logger.info("Firefox Driver criado!")
        break
    except WebDriverException as e:
        logger.error(f"Retry {retry+1}/{max_retries} falhou: {e}")
        if retry == max_retries - 1:
            raise Exception(f"Falha ao iniciar Firefox: {e}")
        time.sleep(2)

def reset_diario():
    global daily_apostas, ultima_data
    hoje = date.today()
    if hoje > ultima_data:
        daily_apostas = 0
        ultima_data = hoje
        logger.info("Novo dia: resetado. Apostas diárias zeradas para 0/10.")
        return True
    return False

def checar_saldo():
    global saldo_atual
    try:
        saldo_element = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, 'balance'))
        )
        texto_saldo = saldo_element.text.replace('KZ', '').replace(' ', '').replace(',', '').strip()
        saldo_atual = float(texto_saldo) if texto_saldo else 0.0
        return saldo_atual
    except (TimeoutException, NoSuchElementException, ValueError) as e:
        logger.error(f"Erro ao checar saldo: {e}. Usando saldo anterior.")
        return saldo_atual

def atualizar_historico():
    global historico_resultados
    temp_file = 'historico.png'
    try:
        history_element = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, 'roadmap'))
        )
        history_element.screenshot(temp_file)
        
        img = cv2.imread(temp_file)
        if img is None:
            raise ValueError("Falha ao carregar imagem.")
        
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        
        vermelho_lower1 = np.array([0, 50, 50])
        vermelho_upper1 = np.array([10, 255, 255])
        vermelho_lower2 = np.array([170, 50, 50])
        vermelho_upper2 = np.array([180, 255, 255])
        azul_lower = np.array([100, 50, 50])
        azul_upper = np.array([130, 255, 255])
        
        mask_vermelho = cv2.inRange(hsv, vermelho_lower1, vermelho_upper1) | cv2.inRange(hsv, vermelho_lower2, vermelho_upper2)
        mask_azul = cv2.inRange(hsv, azul_lower, azul_upper)
        
        height, width = img.shape[:2]
        crop_width = int(width * 0.7)
        crop = img[:, width - crop_width:]
        
        cell_width = crop_width // 10
        resultados = []
        for i in range(10):
            x_start = i * cell_width
            x_end = (i + 1) * cell_width
            if x_end > crop.shape[1]:
                continue
            cell = crop[:, x_start:x_end]
            if cell.size == 0:
                continue
            hsv_cell = cv2.cvtColor(cell, cv2.COLOR_BGR2HSV)
            mask_v = cv2.inRange(hsv_cell, vermelho_lower1, vermelho_upper1) | cv2.inRange(hsv_cell, vermelho_lower2, vermelho_upper2)
            mask_a = cv2.inRange(hsv_cell, azul_lower, azul_upper)
            pixels_v = cv2.countNonZero(mask_v)
            pixels_a = cv2.countNonZero(mask_a)
            if pixels_v > pixels_a:
                resultados.append('🔴')
            elif pixels_a > pixels_v:
                resultados.append('🔵')
        
        historico_resultados = resultados[-10:] if resultados else []
        logger.info(f"Histórico: {' '.join(historico_resultados)}")
        return True
    except Exception as e:
        logger.error(f"Erro ao atualizar histórico: {e}")
        return False
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)

def checar_padrao_formado():
    min_len = min(len(p) for p, _ in PADROES)
    if len(historico_resultados) < min_len:
        return None
    for padrao, tendencia in PADROES:
        if len(historico_resultados) >= len(padrao) and historico_resultados[-len(padrao):] == padrao:
            return tendencia
    return None

def checar_padrao_formando():
    mensagens = []
    for padrao, _ in PADROES:
        for i in range(3, min(6, len(padrao) + 1)):
            parcial = padrao[:i]
            if len(historico_resultados) >= i and historico_resultados[-i:] == parcial:
                desc = ''.join(parcial)
                mensagens.append(f"Padrão parcial: {desc}...")
                break
    if mensagens:
        enviar_notificacao(" | ".join(mensagens))

try:
    # Login
    driver.get('https://www.elephantbet.co.ao')
    wait = WebDriverWait(driver, 10)
    
    username_field = wait.until(EC.presence_of_element_located((By.NAME, 'username')))
    password_field = driver.find_element(By.NAME, 'password')
    username_field.send_keys(USERNAME)
    password_field.send_keys(PASSWORD)
    login_button = driver.find_element(By.XPATH, '//button[@type="submit"]')
    login_button.click()
    
    time.sleep(random.uniform(3, 5))  # Delay randômico anti-bot
    if 'dashboard' not in driver.current_url.lower():
        raise Exception("Falha no login!")

    # Navega para Bac Bo
    driver.get('https://www.elephantbet.co.ao/casino/live')
    bac_bo_link = wait.until(EC.element_to_be_clickable((By.PARTIAL_LINK_TEXT, 'Bac Bo')))
    bac_bo_link.click()
    time.sleep(random.uniform(3, 5))

    saldo_atual = checar_saldo()
    patrimonio_inicial = saldo_atual
    if saldo_atual < MIN_SALDO:
        raise Exception(f"Saldo insuficiente: {saldo_atual} KZ")

    msg_inicio = f"🤖 Bot iniciado! Monitorando Bac Bo...<br>Saldo inicial: {saldo_atual} KZ<br>Limite: {DAILY_MAX} apostas/dia (só em tendências) | Perda máx: {LIMITE_PERDA} KZ"
    enviar_notificacao(msg_inicio)

    ultimo_tempo_espera = time.time()
    while driver and (patrimonio_inicial - saldo_atual) <= LIMITE_PERDA:  # Para se perda > limite
        try:  # Try extra no loop para capturar erros inesperados
            if reset_diario():
                enviar_notificacao(f"🌅 Novo dia! Apostas hoje: 0/{DAILY_MAX} (limite resetado)")

            if daily_apostas >= DAILY_MAX:
                if msg_espera_id:
                    enviar_notificacao(f"🛑 Limite diário de {DAILY_MAX} apostas atingido (independente de resultados). Monitorando padrões sem apostar... ⏰", msg_espera_id)
                time.sleep(300)  # Espera 5min, mas continua loop
                continue

            if not atualizar_historico():
                time.sleep(random.uniform(8, 12))
                continue

            if time.time() - ultimo_tempo_espera > 15:
                msg_espera = f"⏳ ESPERANDO PADRÃO... (Apostas hoje: {daily_apostas}/{DAILY_MAX})"
                if msg_espera_id:
                    enviar_notificacao(msg_espera, msg_espera_id)
                else:
                    msg_espera_id = enviar_notificacao(msg_espera)
                ultimo_tempo_espera = time.time()

            checar_padrao_formando()

            tendencia = checar_padrao_formado()
            if tendencia:
                saldo_atual = checar_saldo()
                if saldo_atual < MIN_SALDO:
                    enviar_notificacao(f"💸 Sem saldo: {saldo_atual} KZ")
                    break

                try:
                    if tendencia == '🔴':
                        botao_aposta = wait.until(EC.element_to_be_clickable((By.ID, 'bet-banker')))
                    else:
                        botao_aposta = wait.until(EC.element_to_be_clickable((By.ID, 'bet-player')))
                    botao_aposta.click()
                    
                    valor_input = driver.find_element(By.ID, 'bet-amount')
                    valor_input.clear()
                    valor_input.send_keys(str(APOSTA_VALOR))
                    confirm_button = driver.find_element(By.ID, 'confirm-bet')
                    confirm_button.click()

                    daily_apostas += 1  # Incrementa SÓ aqui: após detecção + aposta confirmada
                    apostas_feitas += 1
                    logger.info(f"Aposta {daily_apostas}/{DAILY_MAX} realizada em {tendencia}! (Total: {apostas_feitas})")

                    if daily_apostas >= DAILY_MAX:
                        enviar_notificacao(f"🛑 Última aposta do dia ({daily_apostas}/{DAILY_MAX})! Parando apostas até amanhã.")

                    if msg_espera_id:
                        enviar_notificacao(f"🎯 APOSTANDO em {tendencia}! ⏳ ({daily_apostas}/{DAILY_MAX})", msg_espera_id)

                    time.sleep(random.uniform(55, 65))  # Variação para rodada

                    # Salva histórico anterior para comparar
                    historico_anterior = historico_resultados[:]
                    atualizar_historico()
                    if len(historico_resultados) > len(historico_anterior):
                        ultimo_resultado = historico_resultados[-1]
                    else:
                        ultimo_resultado = None

                    if ultimo_resultado == tendencia:
                        acertos += 1
                        resultado = f"Green✅ ({tendencia})"
                    else:
                        erros += 1
                        resultado = f"Errei❌ ({tendencia})"

                    saldo_atual = checar_saldo()  # Sempre checa real

                    taxa = (acertos / (acertos + erros) * 100) if (acertos + erros) > 0 else 0
                    msg = f"{resultado}<br>💰 Saldo: {saldo_atual} KZ<br>📊 Acertos: {acertos} | Erros: {erros} | Taxa: {taxa:.1f}%<br>📅 Apostas hoje: {daily_apostas}/{DAILY_MAX}"
                    enviar_notificacao(msg)
                    logger.info(msg)

                except (TimeoutException, NoSuchElementException) as e:
                    logger.error(f"Erro na aposta ({tendencia}): {e}. Pulando (contador não incrementado).")
                    enviar_notificacao(f"⚠️ Erro na aposta ({tendencia}): {str(e)[:50]}... (sem contar no limite)")

                time.sleep(10)

            else:
                time.sleep(random.uniform(8, 12))
        except Exception as loop_error:  # Captura erros inesperados no loop
            logger.error(f"Erro inesperado no loop principal: {loop_error}")
            time.sleep(30)  # Pausa antes de retry

except Exception as e:
    erro_msg = f"❌ Erro crítico: {str(e)}"
    logger.error(erro_msg)
    enviar_notificacao(erro_msg)

finally:
    if msg_espera_id:
        try:
            telegram_bot.delete_message(chat_id=CHAT_ID, message_id=msg_espera_id)
        except TelegramError:
            pass
    saldo_final = checar_saldo() if 'driver' in locals() and driver else saldo_atual
    total_apostas = acertos + erros
    taxa_final = (acertos / total_apostas * 100) if total_apostas > 0 else 0
    msg_final = f"🔚 Bot finalizado.<br>💰 Saldo final: {saldo_final} KZ<br>📊 Acertos: {acertos}/{total_apostas} | Erros: {erros} | Taxa: {taxa_final:.1f}%<br>📅 Apostas hoje: {daily_apostas}/{DAILY_MAX}"
    enviar_notificacao(msg_final)
    logger.info(msg_final)
    if 'driver' in locals() and driver:
        driver.quit()
