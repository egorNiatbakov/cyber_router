import requests
import json
import uuid
import base64
import logging
from datetime import datetime, timedelta
import csv
import os
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackGame
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters, JobQueue

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация из .env
XUI_HOST = os.getenv("XUI_HOST")
XUI_PORT = os.getenv("XUI_PORT")
XUI_BASE_PATH = os.getenv("XUI_BASE_PATH", "/admin/")
XUI_USERNAME = os.getenv("XUI_USERNAME")
XUI_PASSWORD = os.getenv("XUI_PASSWORD")

LOGIN_URL = f"http://{XUI_HOST}:{XUI_PORT}{XUI_BASE_PATH}login"
API_URL = f"http://{XUI_HOST}:{XUI_PORT}{XUI_BASE_PATH}panel/inbound/addClient"
GET_INBOUND_URL = f"http://{XUI_HOST}:{XUI_PORT}{XUI_BASE_PATH}panel/api/inbounds/get/1"
UPDATE_INBOUND_URL = f"http://{XUI_HOST}:{XUI_PORT}{XUI_BASE_PATH}panel/api/inbounds/update/1"
UPDATE_CLIENT_URL = f"http://{XUI_HOST}:{XUI_PORT}{XUI_BASE_PATH}panel/inbound/updateClient"

credentials = {
    "username": XUI_USERNAME,
    "password": XUI_PASSWORD
}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME")
DONATE_URL = os.getenv("DONATE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
BOT_USERNAME = os.getenv("BOT_USERNAME")
THANK_IMAGE_PATH = os.getenv("THANK_IMAGE_PATH", "thank.jpg")

# Файлы
USERS_FILE = "users.csv"
MESSAGES_FILE = "messages.csv"
NEARLY_EXPIRED_FILE = "notified_nearly_expired.txt"
EXPIRED_FILE = "notified_expired.txt"

# Заголовки
USER_HEADERS = [
    "timestamp", "user_id", "username", "first_name", "xui_email",
    "referrals", "renew_count", "ref_by", "last_renewal", "invited_users", "donor_status"
]
MESSAGE_HEADERS = ["timestamp", "username", "xui_email", "first_name", "message_text"]

# Функции для загрузки и сохранения уведомлений
def load_notified_set(filename):
    if not os.path.exists(filename):
        return set()
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    except Exception as e:
        logger.error(f"Ошибка при загрузке {filename}: {e}")
        return set()

def save_notified_set(notified_set, filename):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            for email in sorted(notified_set):
                f.write(email + "\n")
    except Exception as e:
        logger.error(f"Ошибка при сохранении {filename}: {e}")

# Загружаем состояния уведомлений
NOTIFIED_NEARLY_EXPIRED = load_notified_set(NEARLY_EXPIRED_FILE)
NOTIFIED_EXPIRED = load_notified_set(EXPIRED_FILE)

# Глобальная переменная для бота
bot = None


# --- Сброс уведомлений ---
def reset_user_notifications(email: str):
    """Сбрасывает уведомления для пользователя после продления"""
    if email in NOTIFIED_NEARLY_EXPIRED:
        NOTIFIED_NEARLY_EXPIRED.remove(email)
        save_notified_set(NOTIFIED_NEARLY_EXPIRED, NEARLY_EXPIRED_FILE)
    if email in NOTIFIED_EXPIRED:
        NOTIFIED_EXPIRED.remove(email)
        save_notified_set(NOTIFIED_EXPIRED, EXPIRED_FILE)


# --- Автоочистка users.csv ---
def fix_users_csv():
    if not os.path.exists(USERS_FILE):
        return
    rows = []
    try:
        with open(USERS_FILE, newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader)
            if header != USER_HEADERS:
                rows.append(USER_HEADERS)
            else:
                rows.append(header)
            for row in reader:
                if len(row) == len(USER_HEADERS):
                    row = [cell if cell.strip() not in ['', 'None', ' '] else '' for cell in row]
                    rows.append(row)
        with open(USERS_FILE, 'w', newline='\n', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerows(rows)
        logger.info("users.csv очищен.")
    except Exception as e:
        logger.error(f"Ошибка при автоочистке users.csv: {e}")


# --- Безопасный парсинг даты ---
def safe_parse_datetime(date_str):
    if not date_str or not isinstance(date_str, str):
        return None
    date_str = date_str.strip()
    if not date_str or date_str in ['None', '']:
        return None
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        logger.warning(f"Некорректный формат даты: {date_str}")
        return None


# --- Функции работы с файлами ---
def user_exists(email: str) -> bool:
    if not os.path.exists(USERS_FILE):
        return False
    with open(USERS_FILE, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["xui_email"] == email:
                return True
    return False


def log_user(user, xui_email: str, ref_by: str = None):
    try:
        user_id = str(user.id)
        username = user.username or ""
        first_name = user.first_name or ""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        referrals = "0"
        renew_count = "0"
        ref_by = ref_by or ""
        last_renewal = ""
        invited_users = ""
        donor_status = ""

        file_exists = os.path.exists(USERS_FILE)
        if file_exists:
            with open(USERS_FILE, newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row["xui_email"] == xui_email:
                        return

        with open(USERS_FILE, 'a', newline='\n', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(USER_HEADERS)
            writer.writerow([
                timestamp, user_id, username, first_name, xui_email,
                referrals, renew_count, ref_by, last_renewal, invited_users
            ])
    except Exception as e:
        logger.error(f"Ошибка при записи пользователя: {e}")


def log_message(user, message_text: str, xui_email: str = ""):
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username = user.username or ""
        first_name = user.first_name or ""
        with open(MESSAGES_FILE, "a+", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([timestamp, username, xui_email, first_name, message_text])
    except Exception as e:
        logger.error(f"Ошибка при записи сообщения: {e}")


def get_user_data(email: str):
    if not os.path.exists(USERS_FILE):
        return None
    with open(USERS_FILE, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["xui_email"] == email:
                return row
    return None


def update_user_field(email: str, field: str, value: str):
    if not os.path.exists(USERS_FILE):
        return
    rows = []
    try:
        with open(USERS_FILE, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        for row in rows:
            if row["xui_email"] == email:
                row[field] = str(value) if value is not None else ""

        with open(USERS_FILE, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=USER_HEADERS)
            writer.writeheader()
            for row in rows:
                if len(row) == len(USER_HEADERS):
                    writer.writerow(row)
    except Exception as e:
        logger.error(f"Ошибка при обновлении поля {field} для {email}: {e}")


# --- Генерация пароля ---
def generate_password():
    import random
    import string
    chars = string.ascii_letters + string.digits + "+/"
    return ''.join(random.choice(chars) for _ in range(16))


# --- Получение клиента ---
async def get_existing_client(email: str):
    try:
        s = requests.Session()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": f"http://{XUI_HOST}:{XUI_PORT}{XUI_BASE_PATH}",
            "X-Requested-With": "XMLHttpRequest"
        }
        login_response = s.post(LOGIN_URL, json=credentials, headers=headers)
        if not login_response.json().get("success"):
            return None
        get_response = s.get(GET_INBOUND_URL, headers=headers)
        if not get_response.ok or not get_response.json().get("success"):
            return None
        data = get_response.json()["obj"]
        settings = json.loads(data["settings"])
        method = settings.get("method", "aes-256-gcm")
        for client in settings.get("clients", []):
            if client.get("email") == email:
                return {
                    "password": client["password"],
                    "method": method,
                    "expiryTime": client["expiryTime"],
                    "enable": client["enable"]
                }
        return None
    except Exception as e:
        logger.error(f"Error getting client: {e}")
        return None


# --- Обновление срока действия ---
async def update_client_expiry(email: str, additional_hours: int = 0):
    try:
        s = requests.Session()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": f"http://{XUI_HOST}:{XUI_PORT}{XUI_BASE_PATH}",
            "X-Requested-With": "XMLHttpRequest"
        }
        login_response = s.post(LOGIN_URL, json=credentials, headers=headers)
        if not login_response.json().get("success"):
            return False, None
        get_response = s.get(GET_INBOUND_URL, headers=headers)
        if not get_response.ok or not get_response.json().get("success"):
            return False, None
        data = get_response.json()["obj"]
        settings = json.loads(data["settings"])
        method = settings.get("method", "aes-256-gcm")
        client_data = None
        for client in settings["clients"]:
            if client.get("email") == email:
                new_expiry = int((datetime.now() + timedelta(hours=24 + additional_hours)).timestamp() * 1000)
                client_data = {
                    "method": method,
                    "password": client["password"],
                    "email": client["email"],
                    "limitIp": client.get("limitIp", 0),
                    "totalGB": 0,
                    "expiryTime": new_expiry,
                    "enable": True,
                    "tgId": client.get("tgId", ""),
                    "subId": client.get("subId", ""),
                    "comment": client.get("comment", ""),
                    "reset": 0
                }
                break
        if not client_data:
            return False, None
        payload = {"id": 1, "settings": json.dumps({"clients": [client_data]}, ensure_ascii=False)}
        update_url = f"{UPDATE_CLIENT_URL}/{email}"
        update_response = s.post(update_url, json=payload, headers=headers)
        try:
            result = update_response.json()
            if result.get("success"):
                return True, client_data["expiryTime"]
            return False, None
        except Exception:
            return False, None
    except Exception as e:
        logger.error(f"Error updating expiry: {e}")
        return False, None


# --- Создание ссылки ---
async def create_shadowsocks_link(update: Update):
    try:
        user = update.effective_user
        email = f"tg_{user.username}" if user.username else f"tg_{user.id}"
        remark = f"Telegram: {user.username}" if user.username else f"Telegram ID: {user.id}"
        expiry_time = int((datetime.now() + timedelta(hours=24)).timestamp() * 1000)
        password = generate_password()
        method = "aes-256-gcm"
        s = requests.Session()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": f"http://{XUI_HOST}:{XUI_PORT}{XUI_BASE_PATH}",
            "X-Requested-With": "XMLHttpRequest"
        }
        login_response = s.post(LOGIN_URL, json=credentials, headers=headers)
        if not login_response.json().get("success"):
            return None, None, "login_error"
        client_data = {
            "id": 1,
            "settings": json.dumps({
                "clients": [{
                    "id": str(uuid.uuid4()),
                    "email": email,
                    "enable": True,
                    "expiryTime": expiry_time,
                    "totalGB": 0,
                    "password": password,
                    "method": method,
                    "flow": "",
                    "limitIp": 0
                }],
                "decryption": "none",
                "fallbacks": [],
                "method": method,
                "password": password,
                "network": "tcp",
                "header": {"type": "none"}
            }),
            "remark": remark
        }
        response = s.post(API_URL, json=client_data, headers=headers)
        result = response.json()
        if not result.get("success"):
            return None, None, "exists"
        ss_config = f"{method}:{password}"
        encoded_config = base64.urlsafe_b64encode(ss_config.encode()).decode().rstrip("=")
        ss_url = f"ss://{encoded_config}@{XUI_HOST}:46424?type=tcp#VP-ONE-{email}"
        log_user(user, email)
        return ss_url, expiry_time, None
    except Exception as e:
        logger.error(f"Error creating link: {e}")
        return None, None, "error"


# --- Ранги ---
def get_user_rank(renew_count: int, expiry_time: int) -> tuple:
    now_ms = int(datetime.now().timestamp() * 1000)
    time_left = expiry_time - now_ms
    days_left = time_left / (24 * 60 * 60 * 1000)

    if days_left > 10:
        return "💎 Поддержавший", "Спасибо за Вашу щедрость! 💙"
    elif renew_count >= 15:
        return "🌳 Постоянный", "Вы — наш постоянный пользователь, спасибо Вам за это!"
    elif renew_count >= 5:
        return "🌿 Активный", "Продолжайте в том же духе!"
    else:
        return "🌱 Новичок", "Начните продлевать доступ или приглашать друзей — и получите больше возможностей!"


def get_bonus_hours(rank: str) -> int:
    bonuses = {
        "🌱 Новичок": 0,
        "🌿 Активный": 0,
        "🌳 Постоянный": 0,
        "💎 Поддерживший": 0
    }
    return bonuses.get(rank, 0)


# --- Реферальная система ---
async def process_referral(ref_email: str, new_user_id: int, new_user_name: str):
    try:
        if not ref_email or ref_email == f"tg_{new_user_name}" or ref_email == f"tg_{new_user_id}":
            return

        ref_user_data = get_user_data(ref_email)
        if not ref_user_data:
            return

        # Проверка на уникальность приглашения
        invited_users_str = ref_user_data.get("invited_users", "")
        invited_users = set(invited_users_str.split(",")) if invited_users_str else set()
        new_user_id_str = str(new_user_id)
        if new_user_id_str in invited_users:
            return

        # Добавляем в список приглашённых
        invited_users.add(new_user_id_str)
        updated_invited_users = ",".join(sorted(invited_users))
        update_user_field(ref_email, "invited_users", updated_invited_users)

        # Увеличиваем счётчик приглашений
        referrals = int(ref_user_data.get("referrals", "0")) + 1
        update_user_field(ref_email, "referrals", str(referrals))

        # --- Ключевое исправление: прибавляем 72 часа к текущему сроку ---
        existing_client = await get_existing_client(ref_email)
        if not existing_client:
            return

        current_expiry = existing_client["expiryTime"]
        now_ms = int(datetime.now().timestamp() * 1000)

        # Если срок уже истёк — считаем от текущего времени
        if current_expiry < now_ms:
            new_expiry = int((datetime.now() + timedelta(hours=72)).timestamp() * 1000)
        else:
            new_expiry = current_expiry + (72 * 60 * 60 * 1000)  # +72 часа к текущей дате

        # Обновляем срок
        s = requests.Session()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": f"http://{XUI_HOST}:{XUI_PORT}{XUI_BASE_PATH}",
            "X-Requested-With": "XMLHttpRequest"
        }

        login_response = s.post(LOGIN_URL, json=credentials, headers=headers)
        if not login_response.json().get("success"):
            return

        get_response = s.get(GET_INBOUND_URL, headers=headers)
        if not get_response.ok or not get_response.json().get("success"):
            return

        data = get_response.json()["obj"]
        settings = json.loads(data["settings"])
        method = settings.get("method", "aes-256-gcm")
        client_data = None

        for client in settings["clients"]:
            if client.get("email") == ref_email:
                client_data = {
                    "method": method,
                    "password": client["password"],
                    "email": client["email"],
                    "limitIp": client.get("limitIp", 0),
                    "totalGB": 0,
                    "expiryTime": new_expiry,
                    "enable": True,
                    "tgId": client.get("tgId", ""),
                    "subId": client.get("subId", ""),
                    "comment": client.get("comment", ""),
                    "reset": 0
                }
                break

        if not client_data:
            return

        payload = {"id": 1, "settings": json.dumps({"clients": [client_data]}, ensure_ascii=False)}
        update_url = f"{UPDATE_CLIENT_URL}/{ref_email}"
        update_response = s.post(update_url, json=payload, headers=headers)

        if not update_response.ok:
            return

        try:
            result = update_response.json()
            if not result.get("success"):
                return
        except Exception:
            return
        # --- Конец обновления срока ---

        try:
            ref_user_id = int(ref_user_data["user_id"])
            expiry_date = datetime.fromtimestamp(new_expiry / 1000).strftime('%d-%m-%Y %H:%M')
            display_name = new_user_name or str(new_user_id)

            message = (
                f"🎉 Ура! По вашей ссылке зарегистрировался новый пользователь: <b>{display_name}</b>!\n\n"
                f"Ваш доступ продлён на <b>+72 часа</b>!\n"
                f"Теперь он действует до <b>{expiry_date}</b>.\n\n"
                f"Спасибо, что делитесь VPOne! 💙"
            )

            keyboard = [[InlineKeyboardButton("🔙 Вернуться в главное меню", callback_data='back_to_main_from_referral')]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if os.path.exists(THANK_IMAGE_PATH):
                with open(THANK_IMAGE_PATH, 'rb') as photo:
                    await bot.send_photo(
                        chat_id=ref_user_id,
                        photo=photo,
                        caption=message,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup
                    )
            else:
                await bot.send_message(
                    chat_id=ref_user_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )

        except Exception as e:
            logger.error(f"Failed to send referral notification: {e}")

    except Exception as e:
        logger.error(f"Error processing referral: {e}")


# --- Главное меню ---
async def show_main_menu(context_or_query, email, ss_url, expiry_time):
    now_ms = int(datetime.now().timestamp() * 1000)
    time_left_ms = expiry_time - now_ms

    if time_left_ms <= 0:
        status = "🔴 <b>Доступ заблокирован. Но вы всегда можете нажать внизу кнопку ПРОДЛИТЬ и бесплатно продолжать наслаждаться VPN ещё 24 часа 😉</b>"
    else:
        time_left = timedelta(milliseconds=time_left_ms)
        hours = int(time_left.total_seconds() // 3600)
        minutes = int((time_left.total_seconds() % 3600) // 60)
        status = f"🟢 <b>Доступ активен ещё {hours} ч {minutes} мин</b>"

    message = (
        f"{status}\n\n- - - - - - - - - - -\n\n\n"
        f"🔗 <b>Ваша ссылка для подключения (никогда не меняется, даже после отключения):</b>\n"
        f"   • <code>{ss_url}</code>\n\n\n"
        f"- - - - - - - - - - -\n"
        f"... 📘 инструкция как подключиться 👇\n\n"
        f"__"
    )

    keyboard = [
        [InlineKeyboardButton("🔄 ПРОДЛИТЬ на 24 часа (free) 🔄", callback_data='renew_ss')],
        [InlineKeyboardButton("🔍 Мой статус", callback_data='check_status'),
         InlineKeyboardButton("🏆 Мой ранг", callback_data='my_rank')],
        [InlineKeyboardButton("📘 ИНСТРУКЦИЯ КАК ПОДКЛЮЧИТЬСЯ", callback_data='how_to_connect')],
        [InlineKeyboardButton("☕ Надоело продлевать?", callback_data='donate_info')],
        [InlineKeyboardButton("📤 Поделиться VPN", callback_data='share_bot')],
        #InlineKeyboardButton("📩 Поддержка", url=f'https://t.me/{SUPPORT_USERNAME}'),
    ]

    user_id = None
    if hasattr(context_or_query, 'from_user') and context_or_query.from_user:
        user_id = context_or_query.from_user.id
    elif hasattr(context_or_query, 'message') and context_or_query.message.from_user:
        user_id = context_or_query.message.from_user.id

    if user_id == ADMIN_ID:
        keyboard.append([
            InlineKeyboardButton("📄 Пользователи", callback_data='admin_users'),
            InlineKeyboardButton("💬 Сообщения", callback_data='admin_msg')
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if hasattr(context_or_query, 'message') and hasattr(context_or_query, 'data'):
        try:
            messages = await context_or_query.message.reply_text("test")
            await messages.delete()
            await context_or_query.message.reply_text(
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup
            )
            await context_or_query.delete_message()
        except:
            try:
                await context_or_query.edit_message_text(
                    text=message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=reply_markup
                )
            except Exception as e:
                if "Message is not modified" not in str(e):
                    logger.error(f"Error editing message: {e}")
    else:
        try:
            await context_or_query.message.reply_text(
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Error sending new message: {e}")

async def sps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text("❌ Использование: /sps <user_id> [дни]\nПример: /sps 123456789 90")
        return

    try:
        donor_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Первый аргумент (ID) должен быть числом.")
        return

    # Количество дней (по умолчанию 90, если не указано)
    try:
        days = int(context.args[1]) if len(context.args) > 1 else 90
    except ValueError:
        await update.message.reply_text("❌ Количество дней должно быть числом.")
        return

    if days <= 0:
        await update.message.reply_text("❌ Количество дней должно быть больше 0.")
        return

    # Ищем пользователя по user_id в users.csv
    donor_email = None
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["user_id"] == str(donor_id):
                    donor_email = row["xui_email"]
                    break

    if not donor_email:
        await update.message.reply_text(f"❌ Пользователь с ID {donor_id} не найден.")
        return

    # Получаем текущий клиент
    existing_client = await get_existing_client(donor_email)
    if not existing_client:
        await update.message.reply_text(f"❌ У пользователя {donor_id} нет активного подключения.")
        return

    # Вычисляем новую дату окончания: +N дней
    current_expiry = existing_client["expiryTime"]
    now_ms = int(datetime.now().timestamp() * 1000)

    if current_expiry < now_ms:
        new_expiry = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)
    else:
        new_expiry = current_expiry + (days * 24 * 60 * 60 * 1000)  # +N дней

    # Обновляем срок (копия из process_referral)
    s = requests.Session()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Referer": f"http://{XUI_HOST}:{XUI_PORT}{XUI_BASE_PATH}",
        "X-Requested-With": "XMLHttpRequest"
    }

    login_response = s.post(LOGIN_URL, json=credentials, headers=headers)
    if not login_response.json().get("success"):
        await update.message.reply_text("❌ Не удалось войти в X-UI.")
        return

    get_response = s.get(GET_INBOUND_URL, headers=headers)
    if not get_response.ok or not get_response.json().get("success"):
        await update.message.reply_text("❌ Не удалось получить данные.")
        return

    data = get_response.json()["obj"]
    settings = json.loads(data["settings"])
    method = settings.get("method", "aes-256-gcm")
    client_data = None

    for client in settings["clients"]:
        if client.get("email") == donor_email:
            client_data = {
                "method": method,
                "password": client["password"],
                "email": client["email"],
                "limitIp": client.get("limitIp", 0),
                "totalGB": 0,
                "expiryTime": new_expiry,
                "enable": True,
                "tgId": client.get("tgId", ""),
                "subId": client.get("subId", ""),
                "comment": client.get("comment", ""),
                "reset": 0
            }
            break

    if not client_data:
        await update.message.reply_text("❌ Не удалось найти клиента для обновления.")
        return

    payload = {"id": 1, "settings": json.dumps({"clients": [client_data]}, ensure_ascii=False)}
    update_url = f"{UPDATE_CLIENT_URL}/{donor_email}"
    update_response = s.post(update_url, json=payload, headers=headers)

    if not update_response.ok:
        await update.message.reply_text("❌ Ошибка при обновлении.")
        return

    try:
        result = update_response.json()
        if not result.get("success"):
            await update.message.reply_text(f"❌ Ошибка X-UI: {result}")
            return
    except Exception:
        await update.message.reply_text("❌ Не удалось распарсить ответ X-UI.")
        return

    # Обновляем поле ref_by как метку донора (опционально)
    update_user_field(donor_email, "donor_status", "true")

    # Формируем сообщение
    expiry_date = datetime.fromtimestamp(new_expiry / 1000).strftime('%d-%m-%Y %H:%M')
    message = (
        "💌 <b>Дорогой друг!</b>\n\n"
        "💙 Это не просто сообщение — это <b>искреннее признание в благодарности</b>!\n"
        "💙 Благодаря Вам VPOne остаётся <b>бесплатным*, стабильным* и быстрым*</b>.\n"
        "💙 Спасибо, что вы с нами!\n\n"
        f"✅ Ваш доступ продлён на <b>{days} дней</b> — теперь он действует до <b>{expiry_date}</b>.\n"
        "✅ Теперь вам не нужно продлевать его каждые 24 часа — просто наслаждайтесь!\n\n\n___"
    )

    # Кнопки
    keyboard = [
        [InlineKeyboardButton("📊 Мой статус", callback_data='check_status')],
        [InlineKeyboardButton("🔙 Вернуться на главную", callback_data='back_to_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Отправляем фото с подписью
    if os.path.exists("sps.jpg"):
        with open("sps.jpg", 'rb') as photo:
            try:
                await context.bot.send_photo(
                    chat_id=donor_id,
                    photo=photo,
                    caption=message,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )
                # Получаем username из users.csv для ссылки
                donor_username = None
                donor_first_name = None
                if os.path.exists(USERS_FILE):
                    with open(USERS_FILE, newline='', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            if row["user_id"] == str(donor_id):
                                donor_username = row["username"]
                                donor_first_name = row["first_name"]
                                break

                # Формируем username как ссылку
                username_link = f"@{donor_username}" if donor_username else "нет"
                telegram_link = f'<a href="https://t.me/{donor_username}">{username_link}</a>' if donor_username else "нет"

                # Отправляем админу подтверждение с деталями
                await update.message.reply_text(
                    f"✅ <b>Благодарность отправлена!</b>\n\n"
                    f"👤 <b>User ID:</b> <code>{donor_id}</code>\n"
                    f"📧 <b>Email (X-UI):</b> <code>{donor_email}</code>\n"
                    f"💬 <b>Telegram:</b> {telegram_link}\n"
                    f"🏷️ <b>Имя:</b> {donor_first_name or 'Не указано'}\n"
                    f"🎁 <b>Награда:</b> +{days} дней\n"
                    f"___",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
)
            except Exception as e:
                await update.message.reply_text(f"❌ Не удалось отправить фото: {e}")
    else:
        await context.bot.send_message(
            chat_id=donor_id,
            text=message,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
        await update.message.reply_text(f"✅ Благодарность отправлена (без фото) пользователю {donor_id} (+{days} дней)")


async def find_id_by_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text("❌ Использование: /id <xui_email>\nПример: /id tg_niatbakov_egor")
        return

    search_email = context.args[0].strip()
    if not search_email.startswith("tg_"):
        await update.message.reply_text("❌ Email должен начинаться с tg_")
        return

    if not os.path.exists(USERS_FILE):
        await update.message.reply_text("❌ Файл users.csv не найден.")
        return

    found = None
    with open(USERS_FILE, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["xui_email"] == search_email:
                found = row
                break

    if not found:
        await update.message.reply_text(f"❌ Пользователь с email <code>{search_email}</code> не найден.", parse_mode=ParseMode.HTML)
        return

    username_link = f'<a href="https://t.me/{found["username"]}">@{found["username"]}</a>' if found["username"] else "нет"
    first_name = found["first_name"] or "Не указано"

    await update.message.reply_text(
        f"🔍 <b>Найден пользователь:</b>\n\n"
        f"👤 <b>User ID:</b> <code>{found['user_id']}</code>\n"
        f"📧 <b>Email (X-UI):</b> <code>{found['xui_email']}</code>\n"
        f"💬 <b>Telegram:</b> {username_link}\n"
        f"🏷️ <b>Имя:</b> {first_name}\n"
        f"📈 <b>Продлений:</b> {found['renew_count']}\n"
        f"👥 <b>Приглашено:</b> {found['referrals']}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


async def notify_admin_new_user(user, ref_by: str = None):
    """
    Отправляет администратору уведомление о новом пользователе.
    Работает даже если у пользователя нет username.
    """
    user_id = user.id
    username = user.username
    first_name = user.first_name or "Не указано"
    last_name = user.last_name or ""
    full_name = f"{first_name} {last_name}".strip() if last_name else first_name

    # Генерируем email как в log_user
    email = f"tg_{username}" if username else f"tg_{user_id}"

    # Ссылка на профиль: только если есть username
    tg_link = f'<a href="https://t.me/{username}">@{username}</a>' if username else "нет (@ отсутствует)"

    # Кто пригласил
    ref_user_link = ref_by or "не был приглашён"

    message = (
        "🆕 <b>Новый пользователь!</b>\n\n"
        f"👤 <b>ID:</b> <code>{user_id}</code>\n"
        f"📧 <b>Email (X-UI):</b> <code>{email}</code>\n"
        f"💬 <b>Telegram:</b> {tg_link}\n"
        f"🏷️ <b>Имя:</b> {full_name}\n"
        f"👥 <b>Пришёл от:</b> <code>{ref_user_link}</code>\n"
        f"⏰ <b>Время:</b> {datetime.now().strftime('%d-%m-%Y %H:%M')}"
    )

    # Кнопка "Написать" через t.me только если есть username
    keyboard = []
    if username:
        keyboard.append([InlineKeyboardButton("📩 Написать пользователю", url=f"https://t.me/{username}")])
        
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=message,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
        logger.info(f"Уведомление о новом пользователе {user_id} отправлено администратору.")
    except Exception as e:
        logger.error(f"❌ Не удалось отправить уведомление о новом пользователе {user_id}: {e}")
        
 
async def notify_admin_donor_interest(query):
    """
    Отправляет администратору, что пользователь заинтересован в поддержке.
    Работает даже если у пользователя нет username.
    """
    user = query.from_user
    user_id = user.id
    username = user.username
    first_name = user.first_name or "Не указано"
    last_name = user.last_name or ""
    full_name = f"{first_name} {last_name}".strip() if last_name else first_name
    email = f"tg_{username}" if username else f"tg_{user_id}"

    # Получаем реферера из users.csv
    user_data = get_user_data(email)
    ref_user_link = "неизвестно"
    if user_data and user_data.get("ref_by"):
        ref_user_link = f"<code>{user_data['ref_by']}</code>"

    # Формируем ссылку только если есть username
    if username:
        tg_link = f'<a href="https://t.me/{username}">@{username}</a>'
    else:
        tg_link = f"нет (@ отсутствует, ID: <code>{user_id}</code>)"

    message = (
        "💛 <b>Пользователь заинтересован в поддержке!</b>\n\n"
        "Нажал: <b>«Надоело продлевать?»</b>\n\n"
        f"👤 <b>ID:</b> <code>{user_id}</code>\n"
        f"📧 <b>Email (X-UI):</b> <code>{email}</code>\n"
        f"💬 <b>Telegram:</b> {tg_link}\n"
        f"🏷️ <b>Имя:</b> {full_name}\n"
        f"👥 <b>Пришёл от:</b> {ref_user_link}\n"
        f"⏰ <b>Время:</b> {datetime.now().strftime('%d-%m-%Y %H:%M')}\n\n"
        "Возможно, готов оставить чаевые 💸"
    )

    # Кнопка "Написать" через t.me только если есть username
    keyboard = []
    if username:
        keyboard.append([InlineKeyboardButton("📩 Написать пользователю", url=f"https://t.me/{username}")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=message,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
        logger.info(f"Уведомление о интересе к донорству от {user_id} отправлено администратору.")
    except Exception as e:
        logger.error(f"❌ Не удалось отправить уведомление о интересе к донорству от {user_id}: {e}")

        

# --- Уведомления о скором и просроченном доступе ---
async def check_expiring_clients(context: ContextTypes.DEFAULT_TYPE):
    try:
        global bot
        bot = context.bot

        s = requests.Session()
        headers = {
            "Accept": "application/json",
            "Referer": f"http://{XUI_HOST}:{XUI_PORT}{XUI_BASE_PATH}",
            "X-Requested-With": "XMLHttpRequest"
        }

        login_response = s.post(LOGIN_URL, json=credentials, headers=headers)
        if not login_response.json().get("success"):
            logger.error("Не удалось войти в X-UI")
            return

        get_response = s.get(GET_INBOUND_URL, headers=headers)
        if not get_response.ok or not get_response.json().get("success"):
            logger.error("Не удалось получить данные инбаунда")
            return

        data = get_response.json()["obj"]
        settings = json.loads(data["settings"])
        clients = settings.get("clients", [])

        now_ms = int(datetime.now().timestamp() * 1000)

        user_mapping = {}
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row["xui_email"] and row["user_id"]:
                        user_mapping[row["xui_email"]] = int(row["user_id"])

        keyboard = [
            [InlineKeyboardButton("🔄 ПРОДЛИТЬ на 24 часа (free) 🔄", callback_data='renew_ss')],
            [InlineKeyboardButton("☕ Надоело продлевать?", callback_data='donate_info')],
            [InlineKeyboardButton("🔙 Вернуться в главное меню", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        for client in clients:
            email = client.get("email")
            expiry_time = client.get("expiryTime", 0)
            if not email or expiry_time == 0:
                continue

            user_id = user_mapping.get(email)
            if not user_id:
                continue

            time_left_ms = expiry_time - now_ms

            if time_left_ms <= 2 * 60 * 60 * 1000 and time_left_ms > 0:
                if email not in NOTIFIED_NEARLY_EXPIRED:
                    caption = "😏 <b>Продлевать будете?)))))</b>\n\nВ течении 2х часов VPN перестанет работать. Но это не беда, просто нажмите внизу кнопку ПРОДЛИТЬ и продолжайте наслаждаться бесплатным VPN 😇\n\n\n___"
                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text=caption,
                            reply_markup=reply_markup,
                            parse_mode=ParseMode.HTML
                        )
                        NOTIFIED_NEARLY_EXPIRED.add(email)
                        save_notified_set(NOTIFIED_NEARLY_EXPIRED, NEARLY_EXPIRED_FILE)
                        logger.info(f"Уведомление о скором окончании отправлено: {email}")
                    except Exception as e:
                        logger.error(f"Ошибка отправки 'nearly expired' для {email}: {e}")

            elif time_left_ms < 0:
                if email not in NOTIFIED_EXPIRED:
                    caption = "🔒 <b>Доступ закончился!</b>\n\nВаше подключение больше не работает. Но это тоже не беда, просто нажмите внизу кнопку ПРОДЛИТЬ, чтобы продолжить пользоваться VPN бесплатно.\n\n\n___"
                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text=caption,
                            reply_markup=reply_markup,
                            parse_mode=ParseMode.HTML
                        )
                        NOTIFIED_EXPIRED.add(email)
                        save_notified_set(NOTIFIED_EXPIRED, EXPIRED_FILE)
                        logger.info(f"Уведомление об окончании отправлено: {email}")
                    except Exception as e:
                        logger.error(f"Ошибка отправки 'expired' для {email}: {e}")

    except Exception as e:
        logger.error(f"Ошибка в check_expiring_clients: {e}")


# --- Остальные функции ---
async def show_rank(query, email: str, existing_client):
    if not existing_client:
        await query.edit_message_text("❌ У вас нет активного подключения.")
        return

    user_data = get_user_data(email)
    if not user_data:
        log_user(query.from_user, email)
        user_data = get_user_data(email)
        if not user_data:
            await query.edit_message_text("❌ Ошибка: не удалось загрузить данные.")
            return

    renew_count = int(user_data.get("renew_count", "0"))
    referrals = int(user_data.get("referrals", "0"))
    rank, description = get_user_rank(renew_count, existing_client['expiryTime'])
    expiry_date = datetime.fromtimestamp(existing_client['expiryTime'] / 1000).strftime('%d-%m-%Y %H:%M')

    message = (
        f"🏆 <b>Ваш ранг: {rank}</b>\n\n"
        f"🔁 Продлений: <b>{renew_count}</b>\n"
        f"👥 Приглашено: <b>{referrals}</b>\n"
        f"⏳ Доступ до: <b>{expiry_date}</b>\n\n"
        f"🎯 {description}\n"
        #"🎮 Нажмите кнопку ниже, чтобы сыграть и получить приз!\n\n"
        "Спасибо, что остаётесь с нами! 💙\n\n\n___"
    )

    # Используем URL, а не callback_game 
    keyboard = [
        [InlineKeyboardButton("🌐 Играть Router", url="t.me/secret_ok_bot/cyber_vpone")],
        [InlineKeyboardButton("🦖 Играть Cyber Rex", url="t.me/secret_ok_bot/vpone_rex")],
        [InlineKeyboardButton("🐦 Играть Flappy Birds", url="t.me/secret_ok_bot/vpone_flappybird")],
        [InlineKeyboardButton("🗼 Играть Towers SkyNet", url="t.me/secret_ok_bot/vpongame_tower")],
        [InlineKeyboardButton("🔙 Вернуться в главное меню", callback_data='back_to_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(
            text=message,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения: {e}")
        # Если не получилось — отправь новое
        await query.message.reply_text(message, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        await query.delete_message()


async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рассылка от админа: /all или /all user_id"""
    user = update.effective_user
    if user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("❌ Использование:\n"
                                       "/all — всем\n"
                                       "/all user_id — одному")
        return

    # Определяем цель
    if context.args[0].isdigit():
        target_id = int(context.args[0])
        await _send_to_single_user(update, context, target_id)
    else:
        await _send_to_all_users(update, context)


async def _send_to_single_user(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    keyboard = [[InlineKeyboardButton("🔙 Вернуться в главное меню", callback_data='back_to_main')]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        # Если команда отправлена в ответ – берём ОТВЕЧЕННОЕ сообщение, иначе само командное
        src_msg = update.message.reply_to_message or update.message

        # Копируем сообщение «как есть» (тип контента определяется автоматически)
        await src_msg.copy(
            chat_id=user_id,
            reply_markup=reply_markup
        )

        await update.message.reply_text(f"✅ Сообщение отправлено пользователю {user_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке пользователю {user_id}: {e}")
        await update.message.reply_text(f"❌ Не удалось отправить: {e}")

        

async def _send_to_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рассылка всем пользователям"""
    if not os.path.exists(USERS_FILE):
        await update.message.reply_text("❌ Файл users.csv не найден.")
        return

    sent_count = 0
    failed_count = 0

    # Берём исходник: отвеченное сообщение (предпочтительно) или саму команду
    src_msg = update.message.reply_to_message or update.message

    with open(USERS_FILE, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        user_ids = [row["user_id"] for row in reader if row["user_id"]]

    for user_id_str in user_ids:
        try:
            user_id = int(user_id_str)
            await src_msg.copy(
                chat_id=user_id,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Вернуться в главное меню", callback_data='back_to_main')
                ]])
            )
            sent_count += 1
        except Exception as e:
            logger.error(f"❌ Ошибка при отправке пользователю {user_id}: {e}")
            failed_count += 1

    await update.message.reply_text(
        f"✅ Рассылка завершена\n"
        f"📨 Отправлено: {sent_count}\n"
        f"❌ Ошибок: {failed_count}"
    )

        

async def show_how_to_connect(query, ss_url: str):
    message = (
        "📘 <b>Как подключиться к VPOne? 🌍</b>\n\n"
        "<b>🔗 Ваша ссылка:</b>\n"
        f"<code>{ss_url}</code>\n\n\n"
        "<b>1.</b> Скопируйте ссылку выше 👆 \n"
        "<b>2.</b> Откройте приложение Outline:\n"
        "   • <a href='https://apps.apple.com/app/outline-app/id1356177741'>Скачать для iOS</a>\n"
        "   • <a href='https://play.google.com/store/apps/details?id=org.outline.android.client'>Скачать для Android</a>\n"
        "   • <a href='https://outline-vpn.com/download.php?os=c_windows'>Скачать для PC</a>\n"
        "<b>3.</b> Вставьте ссылку в приложение\n"
        "<b>4.</b> Готово — наслаждайтесь интернетом!\n\n\n"
        "__"
    )
    keyboard = [[InlineKeyboardButton("🔙 Вернуться на главную", callback_data='back_to_main')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=message, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=reply_markup)


async def show_donate_info(query, email: str):
    message = (
        "💙 <b>Поддержать проект</b>\n\n"
        "-- Наш VPN остается бесплатным благодаря вам! Но если хотите сказать «спасибо» и получить расширенные возможности — можете оставить чаевые ☕️\n\n"
        "✨ <b>Что даёт поддержка?</b>\n"
        "✔ Доступ без ежедневного обновления\n"
        "✔ Приоритетная обработка запросов\n"
        "✔ Наша бесконечная благодарность!\n\n"
        "👉 <b>Как поддержать?</b>\n"
        "1. Нажмите «Оставить на чай» ниже.\n"
        f"2. Укажите ваш ID: <code>{email}</code> в комментарии.\n\n"
        "🙏 Спасибо, что остаётесь с нами!\n"
        "Каждая поддержка помогает делать VPOne лучше! 💙\n\n\n"
        "__"
    )
    keyboard = [
        [InlineKeyboardButton("🎁 Оставить на чай", url=DONATE_URL)],
        [InlineKeyboardButton("🔙 Вернуться на главную", callback_data='back_to_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=message, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


async def admin_users(query):
    if not os.path.exists(USERS_FILE):
        await query.message.reply_text("Файл users.csv не найден.")
        return
    with open(USERS_FILE, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        rows = list(reader)
    html = """<meta charset="utf-8"><table border='1' cellpadding='5'>""" + "".join(
        f"<tr>{''.join(f'<td>{cell}</td>' for cell in row)}</tr>" for row in rows
    ) + "</table>"
    with open("users.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open("users.html", "rb") as f:
        await query.message.reply_document(document=f, filename="users.html")


async def admin_msg(query):
    if not os.path.exists(MESSAGES_FILE):
        await query.message.reply_text("Файл messages.csv не найден.")
        return
    with open(MESSAGES_FILE, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        rows = list(reader)
    html = """<meta charset="utf-8"><table border='1' cellpadding='5'>""" + "".join(
        f"<tr>{''.join(f'<td>{cell}</td>' for cell in row)}</tr>" for row in rows
    ) + "</table>"
    with open("messages.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open("messages.html", "rb") as f:
        await query.message.reply_document(document=f, filename="messages.html")


async def share_bot(query, email: str):
    ref_link = f"https://t.me/{BOT_USERNAME}?start={email}"
    message = (
        "📤 <b>Поделитесь ботом с друзьями!</b>\n\n"
        "Чем больше людей узнают о VPOne — тем стабильнее будет сервер.\n\n"
        "🔗 Ваша реферальная ссылка:\n"
        f"   • <a href='{ref_link}'>{ref_link}</a>\n\n"
        "За каждого приглашённого вы получите <b>+72 часа</b> к текущему сроку доступа! 🎁\n\n\n"
        "__"
    )
    keyboard = [[InlineKeyboardButton("🔙 Вернуться на главную", callback_data='back_to_main')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=message, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Добавь логирование, чтобы видеть, что функция вызвана
        logger.info("🎮 handle_web_app_data вызван")
        data = json.loads(update.message.web_app_data.data)
        score = data.get("score", 0)
        user = update.effective_user
        email = f"tg_{user.username}" if user.username else f"tg_{user.id}"

        logger.info(f"🎮 Пользователь {email} набрал {score} очков")

        # Начисляем призы
        if score >= 50:
            success, new_expiry = await extend_client_expiry(email, 24)
            if success:
                expiry_date = datetime.fromtimestamp(new_expiry / 1000).strftime('%d-%m-%Y %H:%M')
                await update.message.reply_text(
                    f"🎉 Отлично! Ты набрал {score} очков!\n"
                    f"🎁 Доступ продлён на 24 часа!\n"
                    f"До: {expiry_date}"
                )
            else:
                await update.message.reply_text("❌ Не удалось продлить доступ.")
        elif score >= 20:
            success, new_expiry = await extend_client_expiry(email, 6)
            await update.message.reply_text(f"💥 {score} очков! +6 часов к доступу!")
        else:
            await update.message.reply_text(f"🔥 {score} очков! Почти получилось — попробуй ещё!")

    except Exception as e:
        logger.error(f"Ошибка обработки результата игры: {e}")
        await update.message.reply_text("Произошла ошибка. Попробуйте снова.")

# --- Обработчик кнопок ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global bot
    bot = context.bot
    query = update.callback_query
    user = query.from_user
    email = f"tg_{user.username}" if user.username else f"tg_{user.id}"

    await query.answer()

    existing_client = await get_existing_client(email)
    ss_url = None
    if existing_client:
        ss_config = f"{existing_client['method']}:{existing_client['password']}"
        encoded_config = base64.urlsafe_b64encode(ss_config.encode()).decode().rstrip("=")
        ss_url = f"ss://{encoded_config}@{XUI_HOST}:46424?type=tcp#VP-ONE-{email}"

    user_data = get_user_data(email)

    last_renewal = None
    if user_data:
        last_renewal_str = user_data.get("last_renewal")
        last_renewal = safe_parse_datetime(last_renewal_str)

    # === Обработка кнопок ===
    if query.data == 'create_ss':
        existing_client = await get_existing_client(email)
        if existing_client:
            ss_config = f"{existing_client['method']}:{existing_client['password']}"
            encoded_config = base64.urlsafe_b64encode(ss_config.encode()).decode().rstrip("=")
            ss_url = f"ss://{encoded_config}@{XUI_HOST}:46424?type=tcp#VP-ONE-{email}"
            await show_main_menu(query, email, ss_url, existing_client['expiryTime'])
            return

        ss_url, expiry_time, error = await create_shadowsocks_link(update)
        if error:
            keyboard = [
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data='create_ss')],
                [InlineKeyboardButton("📩 Написать в поддержку", url=f'https://t.me/{SUPPORT_USERNAME}')]
            ]
            await query.edit_message_text(
                text="❌ Не удалось создать ссылку. Попробуйте позже или обратитесь в поддержку.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
            return

        await show_main_menu(query, email, ss_url, expiry_time)
        reset_user_notifications(email)  # Сброс при первом создании

    elif query.data == 'renew_ss':
        if not existing_client:
            keyboard = [
                [InlineKeyboardButton("🔄 ПРОДЛИТЬ на 24 часа (free) 🔄", callback_data='renew_ss')],
                [InlineKeyboardButton("☕ Надоело продлевать?", callback_data='donate_info')],
                [InlineKeyboardButton("🔙 Вернуться на главную", callback_data='back_to_main')]
            ]
            await query.edit_message_text(
                text="❌ У вас нет активного подключения.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        if not user_data:
            log_user(user, email)
            user_data = get_user_data(email)
            if not user_data:
                await query.edit_message_text("❌ Ошибка: не удалось создать запись.")
                return

        now = datetime.now()
        if last_renewal and (now - last_renewal).total_seconds() < 5 * 3600:
            time_left = timedelta(seconds=5*3600 - (now - last_renewal).total_seconds())
            hours = int(time_left.total_seconds() // 3600)
            minutes = int((time_left.total_seconds() % 3600) // 60)
            message = (
                f"⏳ Подождите ещё {hours} ч {minutes} мин до следующего продления.\n\n"
                "Частое нажатие кнопки не влияет на ваш ранг и не ускоряет процесс. "
                "Пожалуйста, подождите — и тогда сможете продлить снова.\n\n\n___"
            )
            keyboard = [[InlineKeyboardButton("🔙 Вернуться в главное меню", callback_data='back_to_main')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text=message, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            return

        now_ms = int(now.timestamp() * 1000)
        time_left_ms = existing_client['expiryTime'] - now_ms
        hours_left = time_left_ms / (60 * 60 * 1000)

        if hours_left > 24:
            expiry_date = datetime.fromtimestamp(existing_client['expiryTime'] / 1000).strftime('%d-%m-%Y %H:%M')
            message = (
                f"...а продлевать тут нечего 🤩\n\n"
                f"Ваш доступ и так активен до <b>{expiry_date}</b> — вы получили расширенный период не просто так 💙\n"
                "🙏 <b>Большое спасибо за вашу поддержку! </b>\n\n"
                "Наслаждайтесь стабильным подключением без необходимости продлевать его каждые 24 часа!\n\n\n"
                "__"
            )
            keyboard = [[InlineKeyboardButton("🔙 Вернуться в главное меню", callback_data='back_to_main')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                text=message,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
        else:
            rank, _ = get_user_rank(int(user_data.get("renew_count", "0")), existing_client['expiryTime'])
            bonus_hours = get_bonus_hours(rank)
            success, new_expiry = await update_client_expiry(email, bonus_hours)
            if success:
                total_hours = 24 + bonus_hours
                expiry_date = datetime.fromtimestamp(new_expiry / 1000).strftime('%d-%m-%Y %H:%M')
                message = (
                    f"✅ Успех! Доступ продлён на <b>{total_hours} часа</b>!\n"
                    f"... до {expiry_date} ... Just Enjoy 😉\n\n\n___"
                )
                keyboard = [
                    [InlineKeyboardButton("☕ Надоело продлевать?", callback_data='donate_info')],
                    [InlineKeyboardButton("🔙 Вернуться на главную", callback_data='back_to_main')]
                ]
                await query.edit_message_text(
                    text=message,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                renew_count = int(user_data.get("renew_count", "0")) + 1
                update_user_field(email, "renew_count", str(renew_count))
                update_user_field(email, "last_renewal", datetime.now().isoformat())
                reset_user_notifications(email)  # 🔥 Сброс уведомлений после продления
            else:
                keyboard = [
                    [InlineKeyboardButton("🔄 ПРОДЛИТЬ на 24 часа (free) 🔄", callback_data='renew_ss')],
                    [InlineKeyboardButton("☕ Надоело продлевать?", callback_data='donate_info')],
                    [InlineKeyboardButton("🔙 Вернуться на главную", callback_data='back_to_main')]
                ]
                await query.edit_message_text(
                    text="❌ Не удалось продлить срок действия подключения",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

    elif query.data == 'back_to_main_from_referral':
        user = query.from_user
        email = f"tg_{user.username}" if user.username else f"tg_{user.id}"
        existing_client = await get_existing_client(email)
        ss_url = None
        if existing_client:
            ss_config = f"{existing_client['method']}:{existing_client['password']}"
            encoded_config = base64.urlsafe_b64encode(ss_config.encode()).decode().rstrip("=")
            ss_url = f"ss://{encoded_config}@{XUI_HOST}:46424?type=tcp#VP-ONE-{email}"
        await show_main_menu(query, email, ss_url, existing_client['expiryTime'] if existing_client else 0)

    elif query.data == 'check_status':
        if existing_client and ss_url:
            await show_main_menu(query, email, ss_url, existing_client['expiryTime'])
        else:
            keyboard = [[InlineKeyboardButton("🔙 Вернуться на главную", callback_data='back_to_main')]]
            await query.edit_message_text(
                text="❌ У вас нет активного подключения.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    elif query.data == 'how_to_connect':
        if not ss_url:
            keyboard = [[InlineKeyboardButton("🔙 Вернуться на главную", callback_data='back_to_main')]]
            await query.edit_message_text(
                text="❌ Сначала создайте подключение.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        await show_how_to_connect(query, ss_url)

    elif query.data == 'donate_info':
        await show_donate_info(query, email)
        await notify_admin_donor_interest(query)
        
    elif query.data == 'share_bot':
        await share_bot(query, email)
        
    elif query.data == 'play_game':
        try:
            game_url = "https://t.me/secret_ok_bot/test"
            if not game_url.startswith("https://"):
                logger.error("❌ URL не по HTTPS")
                return
            await context.bot.answer_callback_query(
                callback_query_id=query.id,
                url=game_url
            )
            # ✅ Успешно — ничего больше не делаем
        except Exception as e:
            logger.error(f"❌ Ошибка при запуске игры: {e}")
            # ❌ Не редактируем сообщение!
            # Можно отправить новое:
            await query.message.reply_text("❌ Не удалось запустить игру. Попробуйте позже.")
            
    elif query.data == 'my_rank':
        await show_rank(query, email, existing_client)

    elif query.data == 'back_to_main':
        if existing_client and ss_url:
            await show_main_menu(query, email, ss_url, existing_client['expiryTime'])
        else:
            await start(update, context)

    elif query.data == 'admin_users':
        if user.id == ADMIN_ID:
            await admin_users(query)

    elif query.data == 'admin_msg':
        if user.id == ADMIN_ID:
            await admin_msg(query)

    else:
        logger.warning(f"Unknown callback: {query.data}")


# --- Команда /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global bot
    bot = context.bot
    user = update.effective_user
    ref_by = context.args[0] if context.args and context.args[0].startswith("tg_") else None
    email = f"tg_{user.username}" if user.username else f"tg_{user.id}"
    
    # Проверяем, существует ли пользователь
    is_new_user = not user_exists(email)

    # Всегда логируем (создаёт запись, если её нет)
    log_user(user, email, ref_by)

    # Обработка реферала — только если пользователь новый
    if ref_by and is_new_user:
        await process_referral(ref_by, user.id, user.first_name or user.username or "Пользователь")

    # Отправляем уведомление администратору — ТОЛЬКО если пользователь новый
    if is_new_user:
        await notify_admin_new_user(user, ref_by)

    # Показ главного меню
    existing_client = await get_existing_client(email)
    if existing_client:
        ss_config = f"{existing_client['method']}:{existing_client['password']}"
        encoded_config = base64.urlsafe_b64encode(ss_config.encode()).decode().rstrip("=")
        ss_url = f"ss://{encoded_config}@{XUI_HOST}:46424?type=tcp#VP-ONE-{email}"
        await show_main_menu(update, email, ss_url, existing_client['expiryTime'])
    else:
        message = (
            "✌️ Добро пожаловать в VPOne! Я помогу Вам подключиться к VPN абсолютно бесплатно!\n"
            "🤘 ... помимо того, что VPN бесплатный - он ещё: быстрый, стабильный и безопасный!\n\n"
            "⏺️ Нажмите кнопку ниже, чтобы создать VPOne-подключение 👇"
        )
        keyboard = [[InlineKeyboardButton("🛡️ Создать ссылку VPOne", callback_data='create_ss')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(text=message, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


# --- Запуск бота ---
def main():
    global bot
    fix_users_csv()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    # 🔥 Сначала — WebAppData
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("sps", sps_command))
    application.add_handler(CommandHandler("id", find_id_by_email))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler("all", broadcast_message))

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        message_text = update.message.text
        email = f"tg_{user.username}" if user.username else f"tg_{user.id}"
        log_message(user, message_text, email)

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Проверка каждую минуту
    application.job_queue.run_repeating(check_expiring_clients, interval=60, first=10)

    logger.info("Бот запущен...")
    application.run_polling()


if __name__ == '__main__':
    main()