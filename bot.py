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
from typing import TypedDict

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
MODERATION_FILE = Path("moderation.json")  # owner_id -> moderation settings

DEFAULT_BLOCKED_WORDS = {
    "бля",
    "блядь",
    "ебать",
    "еблан",
    "заеб",
    "мразь",
    "пизд",
    "сук",
    "хуй",
    "шлюх",
}

ANONYMOUS_LABEL = "🔒 Анонимное сообщение"
REPLY_HEADER = "📩 Ответ на ваше анонимное сообщение:"


class ModerationSettings(TypedDict):
    filter_enabled: bool
    custom_words: list[str]

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

# owner_id (int) -> {filter_enabled: bool, custom_words: list[str]}
moderation_settings: dict[int, ModerationSettings] = {}


# ---------------------------------------------------------------------------
# FSM: состояние ожидания ответа от создателя
# ---------------------------------------------------------------------------

class ReplyState(StatesGroup):
    waiting_for_reply = State()


# creator_chat_id -> sender_chat_id (кому сейчас пишет создатель через кнопку)
reply_targets: dict[int, int] = {}


def _load_all() -> None:
    """Загрузить все данные с диска в глобальные переменные."""
    global links, pending_messages, sessions, moderation_settings

    raw_links = load_json(LINKS_FILE, {})
    links = {token: int(cid) for token, cid in raw_links.items()}

    raw_pending = load_json(PENDING_FILE, {})
    pending_messages = {int(mid): int(cid) for mid, cid in raw_pending.items()}

    raw_sessions = load_json(SESSIONS_FILE, {})
    sessions = {int(sid): int(cid) for sid, cid in raw_sessions.items()}

    raw_moderation = load_json(MODERATION_FILE, {})
    moderation_settings = {
        int(owner_id): {
            "filter_enabled": settings.get("filter_enabled", True),
            "custom_words": [
                str(word).casefold()
                for word in settings.get("custom_words", [])
                if str(word).strip()
            ],
        }
        for owner_id, settings in raw_moderation.items()
        if isinstance(settings, dict)
    }

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


def _save_moderation() -> None:
    save_json(MODERATION_FILE, {str(owner_id): settings for owner_id, settings in moderation_settings.items()})


def _owner_moderation(owner_id: int) -> ModerationSettings:
    settings = moderation_settings.setdefault(
        owner_id,
        {"filter_enabled": True, "custom_words": []},
    )
    return settings


def _contains_blocked_word(text: str | None, owner_id: int) -> bool:
    if not text:
        return False

    settings = _owner_moderation(owner_id)
    if not settings["filter_enabled"]:
        return False

    custom_words = settings["custom_words"]
    words = DEFAULT_BLOCKED_WORDS | set(custom_words)
    normalized_text = text.casefold()
    return any(word in normalized_text for word in words)


async def _copy_to_owner(message: Message, bot: Bot, owner_id: int) -> int:
    """Copy a message and attach the anonymous label where Telegram allows it."""
    caption = message.caption
    if caption:
        label = f"{caption}\n\n{ANONYMOUS_LABEL}"
        if len(label) > 1024:
            label = f"{caption[:1024 - len(ANONYMOUS_LABEL) - 2]}\n\n{ANONYMOUS_LABEL}"
        copied = await bot.copy_message(
            chat_id=owner_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            caption=label,
            reply_markup=_reply_keyboard(message.from_user.id),
        )
        return copied.message_id

    copied = await bot.copy_message(
        chat_id=owner_id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )
    label_message = await bot.send_message(
        chat_id=owner_id,
        text=ANONYMOUS_LABEL,
        reply_markup=_reply_keyboard(message.from_user.id),
    )
    return label_message.message_id


async def _copy_reply_to_sender(message: Message, bot: Bot, sender_id: int) -> None:
    await bot.send_message(chat_id=sender_id, text=REPLY_HEADER)
    await bot.copy_message(
        chat_id=sender_id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )


# ---------------------------------------------------------------------------
# Роутер и обработчики
# ---------------------------------------------------------------------------
router = Router()


# ── /start без параметра ────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
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


# ── Настройки фильтра владельца ─────────────────────────────────────────────

@router.message(Command("filter_on"))
async def cmd_filter_on(message: Message) -> None:
    settings = _owner_moderation(message.from_user.id)
    settings["filter_enabled"] = True
    _save_moderation()
    await message.answer("✅ Фильтр запрещённых слов включён для всех ваших ссылок.")


@router.message(Command("filter_off"))
async def cmd_filter_off(message: Message) -> None:
    settings = _owner_moderation(message.from_user.id)
    settings["filter_enabled"] = False
    _save_moderation()
    await message.answer("⚠️ Фильтр запрещённых слов выключен для всех ваших ссылок.")


@router.message(Command("add_word"))
async def cmd_add_word(message: Message) -> None:
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("Использование: /add_word <слово>")
        return

    word = args[1].strip().casefold()
    settings = _owner_moderation(message.from_user.id)
    custom_words = settings["custom_words"]
    if word not in custom_words:
        custom_words.append(word)
        _save_moderation()
    await message.answer(f"✅ Слово «{word}» добавлено в ваш список.")


@router.message(Command("remove_word"))
async def cmd_remove_word(message: Message) -> None:
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("Использование: /remove_word <слово>")
        return

    word = args[1].strip().casefold()
    settings = _owner_moderation(message.from_user.id)
    custom_words = settings["custom_words"]
    if word in custom_words:
        custom_words.remove(word)
        _save_moderation()
        await message.answer(f"✅ Слово «{word}» удалено из вашего списка.")
    else:
        await message.answer("ℹ️ Этого слова нет в вашем пользовательском списке.")


@router.message(Command("my_words"))
async def cmd_my_words(message: Message) -> None:
    settings = _owner_moderation(message.from_user.id)
    custom_words = settings["custom_words"]
    status = "включён" if settings["filter_enabled"] else "выключен"
    words = ", ".join(custom_words) if custom_words else "нет"
    await message.answer(f"Фильтр: {status}.\nВаши слова: {words}")


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

@router.message(ReplyState.waiting_for_reply)
async def fsm_reply(message: Message, state: FSMContext, bot: Bot) -> None:
    """Создатель находится в режиме ответа — копируем любой тип сообщения анониму."""
    data = await state.get_data()
    sender_id: int = data["sender_id"]

    await state.clear()

    try:
        await _copy_reply_to_sender(message, bot, sender_id)
        await message.answer("✅ Ваш ответ доставлен анонимному отправителю.")
    except Exception as exc:
        logger.error("Ошибка при отправке ответа пользователю %d: %s", sender_id, exc)
        await message.answer(
            "❌ Не удалось доставить ответ: пользователь заблокировал бота или покинул чат.",
        )


# ── Обработка входящих сообщений любых типов ────────────────────────────────

@router.message()
async def handle_message(message: Message, bot: Bot) -> None:
    """
    Центральный обработчик сообщений. Логика:
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
            await _copy_reply_to_sender(message, bot, sender_id)
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

        content = message.text or message.caption
        if _contains_blocked_word(content, creator_id):
            await message.answer(
                "Ваше сообщение не доставлено, так как содержит запрещённые слова",
            )
            return

        try:
            sent_message_id = await _copy_to_owner(message, bot, creator_id)
        except Exception as exc:
            logger.error("Ошибка при пересылке сообщения создателю %d: %s", creator_id, exc)
            await message.answer(
                "❌ Не удалось доставить сообщение: получатель заблокировал бота.",
            )
            return

        # Связываем message_id пересланного сообщения с chat_id отправителя
        pending_messages[sent_message_id] = user_id
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
