import os
import logging
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from countries import flags
import random
import json
import datetime

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env!")

def load_scores():
    if os.path.exists("scores.json"):
        with open("scores.json", "r") as f:
            return json.load(f)
    return {}

def save_scores(scores):
    with open("scores.json", "w") as f:
        json.dump(scores, f)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! \nПопробуй /game, чтобы сыграть в угадай столицу!"
    )

async def game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flag, capitals = random.choice(list(flags.items()))
    context.user_data["correct"] = capitals  
    await update.message.reply_text(
        f"Угадай столицу страны {flag} (англ / рус)\n",
        reply_markup=ReplyKeyboardRemove() 
    )

async def check_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    correct_list = context.user_data.get("correct")
    if not correct_list:
        return  # если вопрос ещё не задан, игнорируем

    user_answer = update.message.text.strip().lower()
    attempts = context.user_data.get("attempts", 0)  # считаем попытки

    user_id = update.message.from_user.id
    user_name = update.message.from_user.first_name

    if any(user_answer == answer.lower() for answer in correct_list):
        points = max(1, 3 - attempts)

        # Загрузка текущих баллов
        scores = load_scores()
        
        if user_id in scores:
            scores[user_id]["points"] += points
        else:
            scores[user_id] = {"name": user_name, "points": points}
        
        # Сохранение обновленных баллов в файл
        save_scores(scores)

        await update.message.reply_text(
            f"✅ Верно, {user_name}! Это {correct_list[2]}\n"
            f"Вы заработали {points} очков!",
            message_effect_id="5046509860389126442"
        )
        context.user_data["correct"] = None  # очищаем текущий вопрос
        context.user_data["attempts"] = 0
    else:
        attempts += 1
        context.user_data["attempts"] = attempts

        if attempts == 1:
            # Подсказка после первой неправильной попытки
            hint = f"{correct_list[2][0]}{'*'*(len(correct_list[2])-1)}"  # первая буква + звёздочки
            await update.message.reply_text(
                f"Чет не то :(\nПодсказка: столица начинается с {hint} "
                f"и состоит из {len(correct_list[2])} букв."
            )
        else:
            # После второй попытки сразу правильный ответ
            await update.message.reply_text(
                f"Чет не то :(\nПравильный ответ: {correct_list[2]}",
                message_effect_id=5046589136895476101
            )
            context.user_data["correct"] = None
            context.user_data["attempts"] = 0


async def rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scores = load_scores()
    if not scores:
        await update.message.reply_text("Еще нет игроков, попробуй сыграть в игру!")
        return

    sorted_scores = sorted(scores.values(), key=lambda x: x["points"], reverse=True)

    ranking = "🏆 Рейтинг игроков:\n"
    for index, player in enumerate(sorted_scores, 1):
        ranking += f"{index}. {player['name']} — {player['points']} очков\n"

    await update.message.reply_text(ranking)


def send_scheduled_question(context: ContextTypes.DEFAULT_TYPE):
    user_ids = [808572568, 752378415] 

    # Генерация случайного вопроса
    flag, capitals = random.choice(list(flags.items()))  
    
    for user_id in user_ids:
        context.bot.send_message(
            user_id,
            f"Вопрос дня: Угадай столицу страны {flag}\nНапиши ответ в сообщении (английский или русский)"
        )


def main():
    app = Application.builder().token(TOKEN).build()

    scores = load_scores()
    print("Загруженные очки:", scores)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("game", game))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_answer))
    app.add_handler(CommandHandler("rating", rating))  

    # каждый день в 20:00
    job_queue = app.job_queue
    job_queue.run_daily(send_scheduled_question, time=datetime.time(20, 0, 0)) 

    print("Бот запущен и готов к игре!")
    app.run_polling()

if __name__ == "__main__":
    main()