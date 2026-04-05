import asyncio
import logging
import sqlite3
import os
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiocryptopay import AioCryptoPay
from telethon import TelegramClient

# --- КОНФИГУРАЦИЯ ---
API_ID = 20652575
API_HASH = 'c0d5c94ec3c668444dca9525940d876d'
BOT_TOKEN = '8648072212:AAE-hC9VtVpHpAgdY3tgj8GNNEucu1QfRXc'
CRYPTO_PAY_TOKEN = '540011:AARTDw8jiNvxfbJNrCKkEp4l6l50XTuJOYX'
ADMIN_IDS = [7785932103]
STAR_RATE = 0.015  # 1 звезда = 0.015$

logging.basicConfig(level=logging.INFO)


# --- СОСТОЯНИЯ ---
class AdminStates(StatesGroup):
    waiting_for_acc_phone = State()
    waiting_for_price_phone = State()
    waiting_for_price_value = State()


class AddAccount(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_2fa = State()


class MailingStates(StatesGroup):
    waiting_for_text = State()
    waiting_for_photo = State()
    waiting_for_recipients = State()
    waiting_for_interval = State()


class RentProcess(StatesGroup):
    waiting_for_time = State()


class TopUpStates(StatesGroup):
    waiting_for_sum = State()
    waiting_for_stars = State()


# --- БАЗА ДАННЫХ ---
conn = sqlite3.connect('bot_v8.db', check_same_thread=False)
cur = conn.cursor()
cur.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, balance REAL DEFAULT 0)')
cur.execute('''CREATE TABLE IF NOT EXISTS accounts 
               (phone TEXT PRIMARY KEY, session_path TEXT, price REAL DEFAULT 0.01, 
               rented_by INTEGER DEFAULT NULL, rent_until TEXT DEFAULT NULL)''')
conn.commit()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
crypto = AioCryptoPay(token=CRYPTO_PAY_TOKEN)
active_tasks = {}


# --- ПРОВЕРКА АДМИНА ---
def is_admin(user_id):
    return user_id in ADMIN_IDS


# --- ОБРАБОТЧИКИ АДМИН-КОМАНД ---

# 1. Добавление аккаунта (упрощенно запись в БД)
@dp.message(Command("addacc"))
async def admin_add_acc(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await message.answer("Введите номер телефона аккаунта (с +, без пробелов):")
    await state.set_state(AdminStates.waiting_for_acc_phone)


@dp.message(AdminStates.waiting_for_acc_phone)
async def admin_save_acc(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    cur.execute("INSERT OR IGNORE INTO accounts (phone, session_path) VALUES (?, ?)", (phone, f"sessions/{phone}"))
    conn.commit()
    await message.answer(f"✅ Аккаунт {phone} добавлен в базу. Не забудьте положить .session файл в папку sessions/")
    await state.clear()


# 2. Удаление аккаунта
@dp.message(Command("delacc"))
async def admin_del_acc(message: types.Message):
    if not is_admin(message.from_user.id): return
    args = message.text.split()
    if len(args) < 2:
        return await message.answer("Использование: /delacc +79991234567")

    phone = args[1]
    cur.execute("DELETE FROM accounts WHERE phone = ?", (phone,))
    conn.commit()
    await message.answer(f"🗑 Аккаунт {phone} удален.")


# 3. Выдача баланса
@dp.message(Command("give_bal"))
async def admin_give_bal(message: types.Message):
    if not is_admin(message.from_user.id): return
    args = message.text.split()
    if len(args) < 3:
        return await message.answer("Использование: /give_bal ID_ПОЛЬЗОВАТЕЛЯ СУММА")

    uid, amount = args[1], float(args[2])
    cur.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, uid))
    conn.commit()
    await message.answer(f"💰 Пользователю {uid} начислено ${amount}")


# 4. Снятие баланса
@dp.message(Command("del_bal"))
async def admin_del_bal(message: types.Message):
    if not is_admin(message.from_user.id): return
    args = message.text.split()
    if len(args) < 3:
        return await message.answer("Использование: /del_bal ID_ПОЛЬЗОВАТЕЛЯ СУММА")

    uid, amount = args[1], float(args[2])
    cur.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (amount, uid))
    conn.commit()
    await message.answer(f"📉 У пользователя {uid} списано ${amount}")


# 5. Установка цены
@dp.message(Command("setprice"))
async def admin_set_price(message: types.Message):
    if not is_admin(message.from_user.id): return
    args = message.text.split()
    if len(args) < 3:
        return await message.answer("Использование: /setprice +79991234567 0.05")

    phone, price = args[1], float(args[2])
    cur.execute("UPDATE accounts SET price = ? WHERE phone = ?", (price, phone))
    conn.commit()
    await message.answer(f"🏷 Для {phone} установлена цена ${price}/мин")


# --- КЛАВИАТУРЫ ---
def get_main_kb():
    buttons = [
        [types.KeyboardButton(text="💰 Баланс")],
        [types.KeyboardButton(text="📂 Каталог Аккаунтов"), types.KeyboardButton(text="📱 Моя Аренда")],
        [types.KeyboardButton(text="🆘 Support")]
    ]
    return types.ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_mailing_kb(has_photo=False):
    kb = [
        [types.InlineKeyboardButton(text="📝 Изменить текст", callback_data="set_mail_text")],
        [types.InlineKeyboardButton(text="🖼 Добавить фото", callback_data="set_mail_photo"),
         types.InlineKeyboardButton(text="❌ Удалить фото", callback_data="del_mail_photo")] if has_photo else
        [types.InlineKeyboardButton(text="🖼 Добавить фото", callback_data="set_mail_photo")],
        [types.InlineKeyboardButton(text="👥 Получатели", callback_data="set_mail_targets"),
         types.InlineKeyboardButton(text="⏱ Интервал", callback_data="set_mail_delay")],
        [types.InlineKeyboardButton(text="🔄 ЗАПУСТИТЬ РАССЫЛКУ", callback_data="start_mailing_final")],
        [types.InlineKeyboardButton(text="🚫 Прекратить аренду", callback_data="cancel_rent_confirm")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


# --- ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЕЙ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    cur.execute("INSERT OR IGNORE INTO users (id, balance) VALUES (?, ?)", (message.from_user.id, 0.0))
    conn.commit()
    await message.answer(
        "👋 Приветствуем в сервисе!\n\nИспользуйте меню для навигации.",
        reply_markup=get_main_kb()
    )


@dp.message(F.text == "💰 Баланс")
async def balance_view(message: types.Message):
    cur.execute("SELECT balance FROM users WHERE id=?", (message.from_user.id,))
    res = cur.fetchone()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="USDT (CryptoPay)", callback_data="pay_crypto_start")],
        [types.InlineKeyboardButton(text="Telegram Stars ⭐", callback_data="pay_stars_init")]
    ])
    await message.answer(
        f"💳 Ваш баланс: **${res[0]:.2f}**\n\nВыберите способ пополнения:",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "pay_stars_init")
async def pay_stars_init(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("Введите количество звезд (⭐), которое хотите внести:")
    await state.set_state(TopUpStates.waiting_for_stars)


@dp.message(TopUpStates.waiting_for_stars)
async def pay_stars_process(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("Пожалуйста, введите целое число звезд.")
    stars_amount = int(message.text)
    prices = [types.LabeledPrice(label="Пополнение баланса", amount=stars_amount)]
    await message.answer_invoice(
        title=f"Пополнение на {stars_amount} ⭐",
        description=f"Зачисление ${stars_amount * STAR_RATE:.3f} на ваш баланс",
        prices=prices,
        payload="stars_topup",
        currency="XTR"
    )
    await state.clear()


@dp.pre_checkout_query()
async def pre_checkout(query: types.PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def success_pay(message: types.Message):
    stars_received = message.successful_payment.total_amount
    amount_usd = stars_received * STAR_RATE
    cur.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount_usd, message.from_user.id))
    conn.commit()
    await message.answer(f"✅ Оплата прошла! Начислено ${amount_usd:.2f}")


# ... (Остальные функции: catalog, my_rents, setup_mail и т.д. остаются без изменений из предыдущего ответа) ...

@dp.message(F.text == "📂 Каталог Аккаунтов")
async def catalog(message: types.Message):
    cur.execute("SELECT phone, price FROM accounts WHERE rented_by IS NULL")
    rows = cur.fetchall()
    if not rows: return await message.answer("Аккаунтов нет в наличии.")
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=f"📱 {p} (${pr}/мин)", callback_data=f"rent_init_{p}")] for p, pr in rows
    ])
    await message.answer("Выберите аккаунт для аренды:", reply_markup=kb)


@dp.message(F.text == "📱 Моя Аренда")
async def my_rents(message: types.Message):
    cur.execute("SELECT phone FROM accounts WHERE rented_by = ?", (message.from_user.id,))
    rows = cur.fetchall()
    if not rows: return await message.answer("У вас нет активной аренды.")
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=f"⚙️ {p[0]}", callback_data=f"setup_mail_{p[0]}")] for p in rows
    ])
    await message.answer("Ваши активные сессии:", reply_markup=kb)


# (Функции рассылки и аренды остаются такими же, как в коде выше)

async def main():
    if not os.path.exists("sessions"): os.makedirs("sessions")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот выключен")