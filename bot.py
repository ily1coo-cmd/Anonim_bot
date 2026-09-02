"""
Анонимный Telegram-бот на aiogram 3.x
--------------------------------------
Функционал:
  - /create  — создать уникальную ссылку для приёма анонимных сообщений
  - /start <token> — открыть чат для отправки анонимных сообщений
  - Пересылка сообщений без раскрытия личности отправителя
  - Анонимные ответы создателя ссылки обратно отправителю

Переменные окружения:
  BOT_TOKEN — токен бота, полученный у @BotFather
"""

import asyncio
import json
import logging
import os
import secrets
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ---------------------------------------------------------------------------
# Настройка логирования
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Файлы для хранения данных между перезапусками
# ---------------------------------------------------------------------------
LINKS_FILE = Path("links.json")       # token -> creator_chat_id
PENDING_FILE = Path("pending.json")   # forwarded_msg_id -> sender_chat_id
SESSIONS_FILE = Path("sessions.json") # sender_chat_id -> creator_chat_id

# ---------------------------------------------------------------------------
# Вспомогательные функции для работы с JSON-хранилищем
# ---------------------------------------------------------------------------

def load_json(path: Path, default: dict) -> dict:
    """Загрузить словарь из JSON-файла; вернуть default при отсутствии файла."""
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Не удалось прочитать %s: %s. Используем пустой словарь.", path, exc)
    return default


def save_json(path: Path, data: dict) -> None:
    """Сохранить словарь в JSON-файл атомарно (через временный файл)."""
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Глобальное состояние (загружается при старте)
# ---------------------------------------------------------------------------

# token (str) -> creator_chat_id (int, хранится как str в JSON)
links: dict[str, int] = {}

# forwarded_message_id (str в JSON, int в рантайме) -> sender_chat_id (int)
# Ключ — message_id сообщения, которое бот отправил создателю ссылки
pending_messages: dict[int, int] = {}

# sender_chat_id (int) -> creator_chat_id (int)
# Активная сессия: отправитель знает, кому он пишет
sessions: dict[int, int] = {}


# ---------------------------------------------------------------------------
# FSM: состояние ожидания ответа от создателя
# ---------------------------------------------------------------------------

class ReplyState(StatesGroup):
    waiting_for_reply = State()


# creator_chat_id -> sender_chat_id (кому сейчас пишет создатель через кнопку)
reply_targets: dict[int, int] = {}


def _load_all() -> None:
    """Загрузить все данные с диска в глобальные переменные."""
    global links, pending_messages, sessions

    raw_links = load_json(LINKS_FILE, {})
    links = {token: int(cid) for token, cid in raw_links.items()}

    raw_pending = load_json(PENDING_FILE, {})
    pending_messages = {int(mid): int(cid) for mid, cid in raw_pending.items()}

    raw_sessions = load_json(SESSIONS_FILE, {})
    sessions = {int(sid): int(cid) for sid, cid in raw_sessions.items()}

    logger.info(
        "Загружено: %d токенов, %d отложенных, %d сессий",
        len(links), len(pending_messages), len(sessions),
    )


def _save_links() -> None:
    save_json(LINKS_FILE, {t: str(cid) for t, cid in links.items()})


def _save_pending() -> None:
    save_json(PENDING_FILE, {str(mid): str(cid) for mid, cid in pending_messages.items()})


def _save_sessions() -> None:
    save_json(SESSIONS_FILE, {str(sid): str(cid) for sid, cid in sessions.items()})


# ---------------------------------------------------------------------------
# Роутер и обработчики
# ---------------------------------------------------------------------------
router = Router()


# ── /start без параметра ────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    """
    Обрабатывает /start как с аргументом (deep-link), так и без.
    aiogram передаёт payload после /start в message.text, поэтому
    извлекаем его вручную.
    """
    args = message.text.split(maxsplit=1)
    token = args[1].strip() if len(args) > 1 else None

    if not token:
        # Обычный /start — показываем справку
        await message.answer(
            "👋 Привет! Это бот для анонимных сообщений.\n\n"
            "🔗 <b>Хотите получать анонимные сообщения?</b>\n"
            "Отправьте /create — бот сгенерирует вашу личную ссылку.\n\n"
            "✉️ <b>Хотите написать кому-то анонимно?</b>\n"
            "Перейдите по ссылке, которую вам прислал нужный человек.",
            parse_mode="HTML",
        )
        return

    # Deep-link: проверяем токен
    if token not in links:
        await message.answer(
            "❌ Ссылка недействительна или устарела.\n"
            "Попросите создателя выдать новую ссылку.",
        )
        return

    creator_id = links[token]

    if message.from_user.id == creator_id:
        await message.answer(
            "ℹ️ Это ваша собственная ссылка.\n"
            "Другие пользователи могут писать вам анонимно, перейдя по ней.",
        )
        return

    # Регистрируем сессию отправителя
    sessions[message.from_user.id] = creator_id
    _save_sessions()

    await message.answer(
        "✅ Теперь вы можете отправлять анонимные сообщения этому пользователю.\n"
        "Просто напишите что-нибудь — ваша личность останется скрытой.",
    )


# ── /create — создание уникальной ссылки ────────────────────────────────────

@router.message(Command("create", "newlink"))
async def cmd_create(message: Message, bot: Bot) -> None:
    """Генерирует уникальный токен и отправляет создателю его личную ссылку."""
    me = await bot.get_me()
    token = secrets.token_urlsafe(8)

    # Гарантируем уникальность токена
    while token in links:
        token = secrets.token_urlsafe(8)

    links[token] = message.from_user.id
    _save_links()

    link = f"https://t.me/{me.username}?start={token}"
    await message.answer(
        f"🔗 Ваша персональная ссылка для анонимных сообщений:\n\n"
        f"<code>{link}</code>\n\n"
        f"Поделитесь ей с теми, от кого хотите получать анонимные сообщения.",
        parse_mode="HTML",
    )


# ── Вспомогательная функция: inline-кнопка «Ответить» ───────────────────────

def _reply_keyboard(sender_chat_id: int) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру с одной кнопкой «Ответить» для конкретного отправителя."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Ответить",
                    callback_data=f"reply:{sender_chat_id}",
                )
            ]
        ]
    )


# ── Нажатие кнопки «Ответить» ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("reply:"))
async def cb_reply(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Создатель нажал «Ответить» под анонимным сообщением.
    Переводим его в режим ожидания текста ответа.
    """
    sender_id = int(callback.data.split(":")[1])
    creator_id = callback.from_user.id

    # Сохраняем цель ответа в FSM-контексте
    await state.set_state(ReplyState.waiting_for_reply)
    await state.update_data(sender_id=sender_id)

    await callback.message.answer(
        "✏️ Напишите ваш ответ — он будет доставлен анонимно.\n"
        "Отправьте /cancel, чтобы отменить.",
    )
    await callback.answer()  # убираем «часики» на кнопке


# ── /cancel — выход из режима ответа ─────────────────────────────────────────

@router.message(Command("cancel"), ReplyState.waiting_for_reply)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Ответ отменён.")


# ── Получение текста ответа от создателя (FSM) ───────────────────────────────

@router.message(F.text, ReplyState.waiting_for_reply)
async def fsm_reply_text(message: Message, state: FSMContext, bot: Bot) -> None:
    """Создатель находится в режиме ответа — отправляем его текст анонимному пользователю."""
    data = await state.get_data()
    sender_id: int = data["sender_id"]

    await state.clear()

    try:
        await bot.send_message(
            chat_id=sender_id,
            text=(
                "📩 <b>Ответ на ваше анонимное сообщение:</b>\n\n"
                f"{message.text}"
            ),
            parse_mode="HTML",
        )
        await message.answer("✅ Ваш ответ доставлен анонимному отправителю.")
    except Exception as exc:
        logger.error("Ошибка при отправке ответа пользователю %d: %s", sender_id, exc)
        await message.answer(
            "❌ Не удалось доставить ответ: пользователь заблокировал бота или покинул чат.",
        )


# ── Обработка входящих текстовых сообщений ──────────────────────────────────

@router.message(F.text)
async def handle_text(message: Message, bot: Bot) -> None:
    """
    Центральный обработчик текста. Логика:
    1. Если сообщение — ответ (reply) через Telegram-reply на пересланное сообщение
       → отправить ответ обратно анонимному отправителю.
    2. Если пользователь состоит в активной сессии (перешёл по ссылке)
       → переслать сообщение создателю анонимно + кнопка «Ответить».
    3. Иначе — подсказка.
    """
    user_id = message.from_user.id

    # ── Ветка 1: Telegram-reply от создателя ────────────────────────────────
    if message.reply_to_message is not None:
        replied_mid = message.reply_to_message.message_id
        sender_id = pending_messages.get(replied_mid)

        if sender_id is None:
            await message.answer(
                "⚠️ Не удалось определить отправителя.\n"
                "Возможно, переписка была очищена или сообщение слишком старое.",
            )
            return

        try:
            await bot.send_message(
                chat_id=sender_id,
                text=(
                    "📩 <b>Ответ на ваше анонимное сообщение:</b>\n\n"
                    f"{message.text}"
                ),
                parse_mode="HTML",
            )
            await message.answer("✅ Ваш ответ доставлен анонимному отправителю.")
        except Exception as exc:
            logger.error("Ошибка при отправке ответа пользователю %d: %s", sender_id, exc)
            await message.answer(
                "❌ Не удалось доставить ответ: пользователь заблокировал бота или покинул чат.",
            )
        return

    # ── Ветка 2: анонимное сообщение от отправителя ─────────────────────────
    if user_id in sessions:
        creator_id = sessions[user_id]

        try:
            sent = await bot.send_message(
                chat_id=creator_id,
                text=(
                    "🔒 <b>Анонимное сообщение:</b>\n\n"
                    f"{message.text}"
                ),
                parse_mode="HTML",
                reply_markup=_reply_keyboard(user_id),  # ← кнопка «Ответить»
            )
        except Exception as exc:
            logger.error("Ошибка при пересылке сообщения создателю %d: %s", creator_id, exc)
            await message.answer(
                "❌ Не удалось доставить сообщение: получатель заблокировал бота.",
            )
            return

        # Связываем message_id пересланного сообщения с chat_id отправителя
        pending_messages[sent.message_id] = user_id
        _save_pending()

        await message.answer("✅ Ваше анонимное сообщение отправлено.")
        return

    # ── Ветка 3: пользователь не в сессии ───────────────────────────────────
    await message.answer(
        "ℹ️ Чтобы отправить анонимное сообщение, перейдите по ссылке от нужного человека.\n"
        "Чтобы создать свою ссылку, отправьте /create.",
    )


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Переменная окружения BOT_TOKEN не задана. "
            "Получите токен у @BotFather и экспортируйте его:\n"
            "  export BOT_TOKEN='8674751262:AAHOfEqXQjSpunfquJbC9hl7GIJ-RfPeSIg'",
        )

    _load_all()

    bot = Bot(token=token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот запущен. Ожидание сообщений...")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())
