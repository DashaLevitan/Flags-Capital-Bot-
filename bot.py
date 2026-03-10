import os
import logging
import sqlite3
import random
import datetime
from dotenv import load_dotenv
import pycountry
from babel import Locale
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

from countries import flags

DB_PATH = "scores.db"
FLAGS_DIR = "static/flags"
FLAG_CDN = "https://flagcdn.com/w320"


def _emoji_to_alpha2(emoji: str) -> str:
    """Флаг-эмодзи (🇷🇺) → ISO 3166-1 alpha-2 (RU)."""
    if len(emoji) < 2:
        return ""
    return "".join(chr(ord(c) - 0x1F1E6 + ord("A")) for c in emoji[:2]).upper()


# Словарь alpha_2 → варианты названий столицы для проверки ответа
_capitals_by_alpha2 = {
    _emoji_to_alpha2(emoji): caps
    for emoji, caps in flags.items()
    if _emoji_to_alpha2(emoji)
}

# Только страны, которые есть и в pycountry, и в нашем списке столиц
VALID_CODES = [
    c for c in _capitals_by_alpha2
    if pycountry.countries.get(alpha_2=c)
]

# Русские названия стран (babel)
_LOCALE_RU = Locale("ru")


def _country_name_ru(code: str) -> str:
    """Название страны по-русски (alpha_2 → «Беларусь» и т.д.)."""
    name = _LOCALE_RU.territories.get(code)
    if name:
        return name
    country = pycountry.countries.get(alpha_2=code)
    return country.name if country else code


def get_random_question():
    """Возвращает (alpha_2, название_страны_по_русски, список_вариантов_столицы)."""
    code = random.choice(VALID_CODES)
    name_ru = _country_name_ru(code)
    return code, name_ru, _capitals_by_alpha2[code]


def get_flag_photo(code: str):
    """Путь к файлу флага или URL CDN, если файла нет."""
    path = os.path.join(FLAGS_DIR, f"{code}.png")
    return path if os.path.exists(path) else f"{FLAG_CDN}/{code.lower()}.png"


def points_word(n: int) -> str:
    """Склонение «очко»: 1 очко, 2 очка, 5 очков."""
    if n % 100 in (11, 12, 13, 14):
        return "очков"
    if n % 10 == 1:
        return "очко"
    if n % 10 in (2, 3, 4):
        return "очка"
    return "очков"


# Все варианты тире/дефисов приводим к обычному дефису
_DASHES = (
    "\u002D",  # hyphen-minus
    "\u2010",  # hyphen
    "\u2011",  # non-breaking hyphen
    "\u2012",  # figure dash
    "\u2013",  # en dash
    "\u2014",  # em dash
    "\u2015",  # horizontal bar
    "\u2212",  # minus sign
    "\uFE58",  # small em dash
    "\uFE63",  # small hyphen-minus
    "\uFF0D",  # fullwidth hyphen-minus
)


def normalize_answer(s: str) -> str:
    """К одному виду для сравнения: нижний регистр, однотипные дефисы, без лишних пробелов."""
    if not s:
        return ""
    t = s.strip().lower()
    for d in _DASHES:
        t = t.replace(d, "-")
    # несколько дефисов/пробелов подряд — в один
    while "--" in t:
        t = t.replace("--", "-")
    while "  " in t:
        t = t.replace("  ", " ")
    return t.strip()


def init_db():
    """Создаёт таблицу очков, если её ещё нет."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scores (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 0
            )
        """)


def get_scores():
    """Возвращает словарь {user_id: {"name": ..., "points": ...}} из БД."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT user_id, name, points FROM scores")
        return {
            row["user_id"]: {"name": row["name"], "points": row["points"]}
            for row in cur.fetchall()
        }


def update_user_score(user_id: int, name: str, points_to_add: int):
    """Добавляет очки пользователю (создаёт запись, если его ещё нет)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO scores (user_id, name, points) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name = excluded.name,
                points = points + excluded.points
            """,
            (user_id, name, points_to_add),
        )

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env!")

# Таймауты для нестабильной сети (по умолчанию 5 сек — часто мало для WSL/прокси)
REQUEST = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)
PROXY_URL = os.getenv("PROXY_URL")  # при необходимости: http://host:port или socks5://...

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! \nПопробуй /game, чтобы сыграть в угадай столицу!"
    )

# Кнопки под вопросом: «Не знаю» и «Подсказка»
GIVE_UP_BUTTON = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("Подсказка", callback_data="hint"),
        InlineKeyboardButton("Не знаю", callback_data="give_up"),
    ],
])

# Кнопка «Ещё 1 игру» под результатом (правильно/неправильно)
PLAY_AGAIN_BUTTON = InlineKeyboardMarkup([
    [InlineKeyboardButton("Ещё 1 игру", callback_data="play_again")],
])


async def _send_question(message, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет новый вопрос (фото + кнопка «Не знаю»). Используется в /game и «Ещё 1 игру»."""
    code, country_name, capitals = get_random_question()
    context.user_data["correct"] = capitals
    context.user_data["attempts"] = 0
    photo = get_flag_photo(code)
    caption = f"Угадай столицу: {country_name}\n"
    await message.reply_photo(
        photo=photo,
        caption=caption,
        reply_markup=GIVE_UP_BUTTON,
    )


async def game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_question(update.message, context)


async def hint_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """По нажатию «Подсказка» — показываем первую букву и длину, без штрафа."""
    await update.callback_query.answer()
    correct_list = context.user_data.get("correct")
    if not correct_list:
        return
    capital_ru = correct_list[2]
    hint = f"{capital_ru[0]}{'*' * (len(capital_ru) - 1)}"
    await update.callback_query.message.reply_text(
        f"Подсказка: столица начинается с {hint} и состоит из {len(capital_ru)} букв."
    )


async def give_up_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия «Не знаю» (inline-кнопка под сообщением)."""
    await update.callback_query.answer()
    correct_list = context.user_data.get("correct")
    if not correct_list:
        return
    user_id = update.callback_query.from_user.id
    user_name = update.callback_query.from_user.first_name
    update_user_score(user_id, user_name, -1)
    await update.callback_query.message.reply_text(
        f"Правильный ответ: {correct_list[2]}\n−1 {points_word(1)}.",
        reply_markup=PLAY_AGAIN_BUTTON,
    )
    context.user_data["correct"] = None
    context.user_data["attempts"] = 0


async def play_again_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """По нажатию «Ещё 1 игру» — отправляем новый вопрос."""
    await update.callback_query.answer()
    await _send_question(update.callback_query.message, context)


async def check_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    correct_list = context.user_data.get("correct")
    if not correct_list:
        return  # если вопрос ещё не задан, игнорируем

    raw = update.message.text.strip()
    user_answer = normalize_answer(raw)
    attempts = context.user_data.get("attempts", 0)

    user_id = update.message.from_user.id
    user_name = update.message.from_user.first_name

    if user_answer == "не знаю":
        update_user_score(user_id, user_name, -1)
        await update.message.reply_text(
            f"Правильный ответ: {correct_list[2]}\n−1 {points_word(1)}.",
            reply_markup=PLAY_AGAIN_BUTTON,
        )
        context.user_data["correct"] = None
        context.user_data["attempts"] = 0
        return

    if any(user_answer == normalize_answer(answer) for answer in correct_list):
        points = max(1, 3 - attempts)
        update_user_score(user_id, user_name, points)
        await update.message.reply_text(
            f"✅ Верно, {user_name}! Это {correct_list[2]}\n"
            f"Вы заработали {points} {points_word(points)}!",
            message_effect_id="5046509860389126442",
            reply_markup=PLAY_AGAIN_BUTTON,
        )
        context.user_data["correct"] = None
        context.user_data["attempts"] = 0
    else:
        attempts += 1
        context.user_data["attempts"] = attempts

        if attempts == 1:
            hint = f"{correct_list[2][0]}{'*'*(len(correct_list[2])-1)}"
            await update.message.reply_text(
                f"Чет не то :(\nПодсказка: столица начинается с {hint} "
                f"и состоит из {len(correct_list[2])} букв."
            )
        else:
            update_user_score(user_id, user_name, -1)
            await update.message.reply_text(
                f"Чет не то :(\nПравильный ответ: {correct_list[2]}\n−1 {points_word(1)}.",
                message_effect_id=5046589136895476101,
                reply_markup=PLAY_AGAIN_BUTTON,
            )
            context.user_data["correct"] = None
            context.user_data["attempts"] = 0


async def rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scores = get_scores()
    if not scores:
        await update.message.reply_text("Еще нет игроков, попробуй сыграть в игру!")
        return

    sorted_scores = sorted(scores.values(), key=lambda x: x["points"], reverse=True)

    ranking = "🏆 Рейтинг игроков:\n"
    for index, player in enumerate(sorted_scores, 1):
        p = player["points"]
        ranking += f"{index}. {player['name']} — {p} {points_word(p)}\n"

    await update.message.reply_text(ranking)


def send_scheduled_question(context: ContextTypes.DEFAULT_TYPE):
    """Джоба выполняется в отдельном потоке — шлём фото через HTTP API синхронно."""
    import requests
    user_ids = [808572568, 752378415]
    code, country_name, _ = get_random_question()
    photo = get_flag_photo(code)
    caption = "Вопрос дня: Угадай столицу этой страны. Напиши ответ в сообщении (англ / рус)."
    api_url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    for user_id in user_ids:
        try:
            if os.path.isfile(photo):
                with open(photo, "rb") as f:
                    requests.post(
                        api_url,
                        data={"chat_id": user_id, "caption": caption},
                        files={"photo": ("flag.png", f, "image/png")},
                        timeout=30,
                    )
            else:
                requests.post(
                    api_url,
                    data={"chat_id": user_id, "photo": photo, "caption": caption},
                    timeout=30,
                )
        except Exception as e:
            logging.exception("Ошибка отправки вопроса дня: %s", e)


def main():
    init_db()
    builder = Application.builder().token(TOKEN).request(REQUEST)
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
    app = builder.build()

    scores = get_scores()
    print("Загруженные очки:", scores)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("game", game))
    app.add_handler(CallbackQueryHandler(hint_callback, pattern="^hint$"))
    app.add_handler(CallbackQueryHandler(give_up_callback, pattern="^give_up$"))
    app.add_handler(CallbackQueryHandler(play_again_callback, pattern="^play_again$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_answer))
    app.add_handler(CommandHandler("rating", rating))  

    # каждый день в 20:00
    job_queue = app.job_queue
    job_queue.run_daily(send_scheduled_question, time=datetime.time(20, 0, 0)) 

    print("Бот запущен и готов к игре!")
    app.run_polling(bootstrap_retries=5)

if __name__ == "__main__":
    main()