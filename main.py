"""FELIX AUTO DONATE — один файл с логикой заказов, MySQL и Telegram.

Запуск: python3 main.py. Все настройки, цены и реквизиты находятся в config.py.
"""
from __future__ import annotations

import asyncio
import logging
import io
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import config as C
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

BASE = Path(__file__).resolve().parent
# Новые файлы заказов и SQLite WAL должны быть доступны только пользователю VPS.
try:
    os.umask(0o077)
except OSError:
    pass
BOT_TOKEN = C.BOT_TOKEN.strip()
OWNER_ID = int(C.OWNER_ID)
PAYMENT_DETAILS = C.PAYMENT_DETAILS
PREMIUM = bool(C.PREMIUM_EMOJI)
DB_PATH = str(BASE / "orders.sqlite3")
CATALOG = C.CATALOG

PRODUCTS = {}
for category_id, section in CATALOG.items():
    for item in section["products"]:
        code = item["id"]
        if code in PRODUCTS or item["kind"] not in ("level", "admin", "donate", "money"):
            raise ValueError("Ошибка config.py: повтор товара или неверный тип")
        if not (0 < int(item["price"]) < 10**8 and 0 < int(item["units"]) < 10**10):
            raise ValueError("Ошибка config.py: цена/количество")
        PRODUCTS[code] = dict(item, category=category_id)

class GrantError(Exception):
    pass


def identifier(s: str) -> str:
    """MySQL identifiers cannot be passed as SQL query parameters."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s or ""):
        raise GrantError(f"Недопустимое имя таблицы или поля: {s!r}")
    return f"`{s}`"


@dataclass(frozen=True)
class GameConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    table: str
    nickname_col: str
    level_col: str
    admin_col: str
    donate_col: str
    money_col: str

    def check(self):
        for x in (self.table, self.nickname_col, self.level_col, self.admin_col,
                  self.donate_col, self.money_col):
            identifier(x)
        if not self.host or not self.user or not self.database:
            raise GrantError("Не настроены MySQL хост, пользователь или имя базы (админ-панель или config.py)")


def grant_sql(order: dict, cfg: GameConfig):
    """Return a parameterized query; all product quantities are taken from catalog snapshots."""
    col = {
        "level": cfg.level_col, "admin": cfg.admin_col,
        "donate": cfg.donate_col, "money": cfg.money_col,
    }.get(order['kind'])
    if not col:
        raise GrantError("Неизвестный тип покупки")
    amount = int(order['units'])
    if not (1 <= amount <= 10_000_000_000):
        raise GrantError("Недопустимое количество")
    table, nick, val = map(identifier, (cfg.table, cfg.nickname_col, col))
    if order['kind'] == 'admin':
        return f"UPDATE {table} SET {val}=GREATEST({val}, %s) WHERE {nick}=%s", (amount, order['nickname'])
    return f"UPDATE {table} SET {val}={val}+%s WHERE {nick}=%s", (amount, order['nickname'])


def grant_to_game(order: dict, cfg: GameConfig) -> str:
    """MySQL transaction: one update per Telegram order, even on retries.

    bot_donation_grants must live in the SAME MySQL schema as the accounts table.
    """
    cfg.check()
    try:
        import mysql.connector
    except ImportError as exc:
        raise GrantError("Установи mysql-connector-python") from exc
    connection = None
    try:
        connection = mysql.connector.connect(
            host=cfg.host, port=cfg.port, user=cfg.user,
            password=cfg.password, database=cfg.database, connection_timeout=8,
            allow_local_infile=False,
            autocommit=False,
        )
        cur = connection.cursor()
        # Idempotency ledger is separate from game account data.
        cur.execute('''CREATE TABLE IF NOT EXISTS bot_donation_grants (
          order_id BIGINT NOT NULL PRIMARY KEY,
          account_name VARCHAR(64) NOT NULL,
          kind VARCHAR(16) NOT NULL,
          units BIGINT NOT NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
        connection.commit()
        connection.start_transaction()
        # Guarantee the account exists before attempting to grant anything.
        table, nick = identifier(cfg.table), identifier(cfg.nickname_col)
        cur.execute(f"SELECT {nick} FROM {table} WHERE {nick}=%s FOR UPDATE", (order['nickname'],))
        first = cur.fetchone()
        if first is None:
            raise GrantError(f"Аккаунт {order['nickname']} не найден в игровой базе")
        if cur.fetchone() is not None:
            raise GrantError("В базе несколько аккаунтов с таким ником — выдача остановлена")
        cur.execute("SELECT order_id FROM bot_donation_grants WHERE order_id=%s FOR UPDATE", (order['id'],))
        if cur.fetchone():
            connection.commit()
            return "already"
        query, args = grant_sql(order, cfg)
        cur.execute(query, args)
        if cur.rowcount != 1:
            raise GrantError("Не удалось однозначно изменить аккаунт")
        cur.execute('''INSERT INTO bot_donation_grants(order_id,account_name,kind,units)
                       VALUES(%s,%s,%s,%s)''',
                    (order['id'], order['nickname'], order['kind'], order['units']))
        connection.commit()
        return "applied"
    except GrantError:
        if connection:
            connection.rollback()
        raise
    except Exception as exc:
        if connection:
            connection.rollback()
        raise GrantError(f"Ошибка MySQL: {exc}") from exc
    finally:
        if connection:
            connection.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OrderStorage:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout = 15000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            nickname TEXT NOT NULL,
            item_code TEXT NOT NULL,
            item_title TEXT NOT NULL,
            kind TEXT NOT NULL,
            units INTEGER NOT NULL,
            price INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'awaiting_receipt',
            receipt_file_id TEXT,
            receipt_unique_id TEXT,
            receipt_kind TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error_text TEXT
        );
        CREATE INDEX IF NOT EXISTS order_owner ON orders (telegram_id, id);
        CREATE INDEX IF NOT EXISTS order_status ON orders (status, id);
        CREATE UNIQUE INDEX IF NOT EXISTS receipt_dedupe
          ON orders (receipt_unique_id) WHERE receipt_unique_id IS NOT NULL;
        ''')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS category_photos (
            category TEXT PRIMARY KEY, photo_file_id TEXT NOT NULL
        );
        ''')
        self.db.commit()
        # Локальный SQLite содержит пароль MySQL: доступ разрешён только владельцу файла.
        for private_path in (path, path + '-wal', path + '-shm'):
            if os.path.exists(private_path):
                try:
                    os.chmod(private_path, 0o600)
                except OSError:
                    logging.warning('Не удалось закрыть доступ к файлу: %s', private_path)

    def setting(self, key: str, default: str = '') -> str:
        row = self.db.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else str(default)

    def set_settings(self, values: dict[str, str]) -> None:
        with self.db:
            self.db.executemany(
                'INSERT INTO app_settings(key,value) VALUES(?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                [(k, str(v)) for k, v in values.items()]
            )

    def photo(self, category: str) -> str | None:
        row = self.db.execute('SELECT photo_file_id FROM category_photos WHERE category=?', (category,)).fetchone()
        return row['photo_file_id'] if row else None

    def set_photo(self, category: str, file_id: str) -> None:
        with self.db:
            self.db.execute(
                'INSERT INTO category_photos(category,photo_file_id) VALUES(?,?) '
                'ON CONFLICT(category) DO UPDATE SET photo_file_id=excluded.photo_file_id',
                (category, file_id)
            )

    def remove_photo(self, category: str) -> None:
        with self.db:
            self.db.execute('DELETE FROM category_photos WHERE category=?', (category,))

    @staticmethod
    def _row(cur):
        row = cur.fetchone()
        return dict(row) if row else None

    def get(self, order_id: int):
        return self._row(self.db.execute("SELECT * FROM orders WHERE id=?", (order_id,)))

    def active(self, user_id: int):
        return self._row(self.db.execute(
            "SELECT * FROM orders WHERE telegram_id=? AND status IN ('awaiting_receipt','review','processing') ORDER BY id DESC LIMIT 1",
            (user_id,)))

    def latest(self, user_id: int, limit=8):
        return [dict(x) for x in self.db.execute(
            "SELECT * FROM orders WHERE telegram_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))]

    def create(self, telegram_id: int, nickname: str, item_code: str, item: dict):
        if self.active(telegram_id):
            raise ValueError("Есть незавершённый заказ")
        t = now()
        cur = self.db.execute(
            "INSERT INTO orders (telegram_id,nickname,item_code,item_title,kind,units,price,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (telegram_id, nickname, item_code, item['title'], item['kind'], int(item['units']), int(item['price']), t, t))
        self.db.commit()
        return self.get(cur.lastrowid)

    def submit_receipt(self, order_id: int, user_id: int, file_id: str, unique_id: str, kind: str):
        try:
            cur = self.db.execute('''UPDATE orders SET receipt_file_id=?,receipt_unique_id=?,receipt_kind=?,
                 status='review',updated_at=? WHERE id=? AND telegram_id=? AND status='awaiting_receipt' ''',
                 (file_id, unique_id, kind, now(), order_id, user_id))
            self.db.commit()
            return cur.rowcount == 1
        except sqlite3.IntegrityError as exc:
            self.db.rollback()
            raise ValueError("Этот чек уже используется в другом заказе") from exc

    def cancel(self, order_id: int, user_id: int):
        cur = self.db.execute("UPDATE orders SET status='cancelled',updated_at=? WHERE id=? AND telegram_id=? AND status='awaiting_receipt'", (now(), order_id, user_id))
        self.db.commit()
        return cur.rowcount == 1

    def claim(self, order_id: int):
        cur = self.db.execute("UPDATE orders SET status='processing',updated_at=?,error_text=NULL WHERE id=? AND status='review'", (now(), order_id))
        self.db.commit()
        return cur.rowcount == 1

    def delivered(self, order_id: int):
        self.db.execute("UPDATE orders SET status='delivered',updated_at=?,error_text=NULL WHERE id=? AND status='processing'", (now(), order_id))
        self.db.commit()

    def retry(self, order_id: int, error: str):
        self.db.execute("UPDATE orders SET status='review',updated_at=?,error_text=? WHERE id=? AND status='processing'", (now(), error[:500], order_id))
        self.db.commit()

    def reject(self, order_id: int):
        cur = self.db.execute("UPDATE orders SET status='rejected',updated_at=? WHERE id=? AND status='review'", (now(), order_id))
        self.db.commit()
        return cur.rowcount == 1

    def recover_stuck(self):
        # If the program was interrupted after MySQL commit, grant_store idempotency avoids a second grant.
        self.db.execute("UPDATE orders SET status='review',updated_at=?,error_text='Повтор после перезапуска' WHERE status='processing'", (now(),))
        self.db.commit()

    def pending(self, limit=15):
        return [dict(x) for x in self.db.execute("SELECT * FROM orders WHERE status='review' ORDER BY id LIMIT ?", (limit,))]

    def close(self):
        self.db.close()


EMOJIS = {
    'fire': ('5424972470023104089', '🔥'),
    'star': ('5438496463044752972', '⭐'),
    'money': ('5409048419211682843', '💰'),
    'lock': ('5296369303661067030', '🔒'),
    'check': ('5206607081334906820', '✅'),
    'warning': ('5420323339723881652', '⚠️'),
    'card': ('5769403330761593044', '💳'),
    'home': ('5416044402781229623', '🏠'),
    'support': ('5884510167986343350', '💬'),
    'paperclip': ('5305265301917549162', '📎'),
    'settings': ('5341715473882955310', '⚙️'),
    'photo': ('5305265301917549162', '🖼'),
}


def em(key: str) -> str:
    eid, symbol = EMOJIS[key]
    return f'<tg-emoji emoji-id="{eid}">{symbol}</tg-emoji>' if PREMIUM else symbol


def button(label: str, data: str, emoji: str | None = None, style: str | None = None):
    kw = {'text': label, 'callback_data': data}
    if PREMIUM and emoji in EMOJIS:
        kw['icon_custom_emoji_id'] = EMOJIS[emoji][0]
    if style:
        kw['style'] = style
    return InlineKeyboardButton(**kw)


def keyboard(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows])


def home_kb(owner: bool = False):
    rows = [
        (button('Приобрести ЛВЛ', 'cat:level', 'star'), button('Приобрести админку', 'cat:admin', 'lock')),
        (button('Приобрести донат', 'cat:donate', 'fire'), button('Приобрести валюту', 'cat:money', 'money')),
        (button('Мои покупки', 'my', 'paperclip'),),
    ]
    if owner:
        rows.append((button('Админ-панель', 'adm:home', 'settings'),))
    return keyboard(*rows)


def section_kb(category: str):
    return keyboard(*[(button(f"{p['title']} — {p['price']} ₽", f"buy:{p['id']}", 'card'),)
                      for p in CATALOG[category]['products']],
                    (button('Назад', 'home', 'home'),))


def payment_kb(order_id: int):
    return keyboard((button('Отменить заказ', f'cancel:{order_id}', 'warning', 'danger'),),
                    (button('Меню', 'home', 'home'),))


def review_kb(order_id: int):
    return keyboard((button('Подтвердить и выдать', f'ok:{order_id}', 'check', 'success'),),
                    (button('Отклонить чек', f'no:{order_id}', 'warning', 'danger'),))


def game_config() -> GameConfig:
    """Сначала настройки из админ-панели, иначе значения из config.py."""
    return GameConfig(
        host=store.setting('mysql_host', C.GAME_DB_HOST),
        port=int(store.setting('mysql_port', C.GAME_DB_PORT)),
        user=store.setting('mysql_user', C.GAME_DB_USER),
        password=store.setting('mysql_password', C.GAME_DB_PASSWORD),
        database=store.setting('mysql_database', C.GAME_DB_NAME),
        table=store.setting('mysql_table', C.GAME_ACCOUNT_TABLE),
        nickname_col=store.setting('mysql_nickname_col', C.GAME_NICK_COLUMN),
        level_col=store.setting('mysql_level_col', C.GAME_LEVEL_COLUMN),
        admin_col=store.setting('mysql_admin_col', C.GAME_ADMIN_COLUMN),
        donate_col=store.setting('mysql_donate_col', C.GAME_DONATE_COLUMN),
        money_col=store.setting('mysql_money_col', C.GAME_MONEY_COLUMN),
    )


def mysql_probe(cfg: GameConfig, check_schema: bool = False) -> str:
    """Без изменения игровых аккаунтов проверяет доступ и при запросе схему."""
    if not cfg.host or not cfg.user or not cfg.database:
        raise GrantError('Укажи хост, логин и имя базы данных')
    if not 1 <= cfg.port <= 65535:
        raise GrantError('Неверный MySQL-порт')
    try:
        import mysql.connector
    except ImportError as exc:
        raise GrantError('Нужна библиотека mysql-connector-python') from exc
    try:
        connection = mysql.connector.connect(
            host=cfg.host, port=cfg.port, user=cfg.user,
            password=cfg.password, database=cfg.database,
            allow_local_infile=False, connection_timeout=7, autocommit=True,
        )
        try:
            cur = connection.cursor()
            cur.execute('SELECT 1')
            cur.fetchone()
            if check_schema:
                # Игровые таблицы/столбцы могут называться иначе — их нужно настроить отдельно.
                for x in (cfg.table, cfg.nickname_col, cfg.level_col,
                          cfg.admin_col, cfg.donate_col, cfg.money_col):
                    identifier(x)
                cur.execute('SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS '
                            'WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s',
                            (cfg.database, cfg.table))
                found = {row[0].lower() for row in cur.fetchall()}
                if not found:
                    raise GrantError(f'В базе не найдена игровая таблица {cfg.table}')
                wanted = (cfg.nickname_col, cfg.level_col, cfg.admin_col,
                          cfg.donate_col, cfg.money_col)
                missing = [v for v in wanted if v.lower() not in found]
                if missing:
                    raise GrantError('Нет столбцов в игровой таблице: ' + ', '.join(missing))
            cur.close()
        finally:
            connection.close()
    except GrantError:
        raise
    except Exception as exc:
        raise GrantError(f'Не удалось подключиться к MySQL: {exc}') from exc
    return 'connected_and_schema_ok' if check_schema else 'connected'


store = OrderStorage(DB_PATH)
store.recover_stuck()
dp = Dispatcher()
# Transient input stage only. Created orders/receipts never rely on this dict.
waiting_for_nickname: dict[int, str] = {}
# Временные этапы ввода администратора, никогда не отправляются другим пользователям.
admin_input: dict[int, dict] = {}
# У каждого владельца проекта — свои MySQL-хост и панель phpMyAdmin.
# Бот никогда не формирует URL с паролем в параметрах (такие URL попадают в историю/логи).
# Админ отправляет данные MySQL ОДНИМ сообщением, как в старом боте:
#   Логин Пароль Имя_базы
# Хост/порт меняются отдельно и никогда не угадываются по логину.
def parse_mysql_credentials(value: str) -> tuple[str, str, str]:
    parts = value.strip().split()
    if len(parts) != 3:
        raise ValueError('Формат: Логин Пароль Имя_базы. Пример: gs123456 MyPass gs123456')
    username, password, database = parts
    if not re.fullmatch(r'[A-Za-z0-9_.$-]{1,100}', username):
        raise ValueError('Неверный логин MySQL. Пример: gs123456')
    if not 1 <= len(password) <= 250:
        raise ValueError('Пароль должен содержать от 1 до 250 символов без пробелов.')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,64}', database):
        raise ValueError('Имя базы: только латинские буквы, цифры и подчёркивание.')
    return username, password, database


def normalize_panel_url(value: str) -> str:
    """Accept a public https phpMyAdmin URL, without storing credential query parameters."""
    if value == '-':
        return ''
    raw = value.strip()
    if not raw.startswith(('https://', 'http://')):
        raw = 'https://' + raw
    parts = urlsplit(raw)
    hostname = parts.hostname or ''
    if (parts.scheme != 'https' or not hostname or parts.username or parts.password
            or not re.fullmatch(r'[A-Za-z0-9.-]+', hostname)
            or hostname.lower() in ('localhost',) or '.' not in hostname):
        raise ValueError('Пришли HTTPS-ссылку на панель без логина и пароля в адресе.')
    path = parts.path or '/index.php'
    return urlunsplit(('https', parts.netloc, path, '', ''))


def parse_mysql_endpoint(raw: str) -> tuple[str, int]:
    """Allow hostname or IPv4 plus optional custom port, but no web URL."""
    m = re.fullmatch(r'([a-zA-Z0-9._-]{1,253})(?::(\d{1,5}))?', raw.strip())
    if not m or '..' in raw or '/' in raw:
        raise ValueError('Пришли MySQL-хост вида 164.132.206.179:3306 (не HTTPS-ссылку).')
    host, port = m.group(1), int(m.group(2) or 3306)
    if not 1 <= port <= 65535:
        raise ValueError('Порт должен быть от 1 до 65535.')
    return host, port


def safe_panel_link(base: str, database: str) -> str:
    """Create a phpMyAdmin page URL WITHOUT secrets or autologin parameters."""
    if not base:
        return ''
    url = urlsplit(normalize_panel_url(base))
    return urlunsplit((url.scheme, url.netloc, url.path, urlencode({'db': database}), ''))


FIELD_STEPS = [
    ('mysql_table', 'Таблица аккаунтов'),
    ('mysql_nickname_col', 'Столбец с игровым ником'),
    ('mysql_level_col', 'Столбец уровня'),
    ('mysql_admin_col', 'Столбец админки'),
    ('mysql_donate_col', 'Столбец доната'),
    ('mysql_money_col', 'Столбец игровой валюты'),
]


def owner_only(uid: int, chat_id: int) -> bool:
    return uid == OWNER_ID and chat_id == OWNER_ID


def admin_panel_kb():
    return keyboard(
        (button('Подключить MySQL', 'adm:db', 'settings'),),
        (button('Сменить MySQL-хост', 'adm:host', 'settings'),),
        (button('Проверить MySQL', 'adm:test', 'check'),),
        (button('Ссылка phpMyAdmin', 'adm:pma', 'paperclip'),),
        (button('Настроить phpMyAdmin', 'adm:pma_set', 'settings'),),
        (button('Поля игрового мода', 'adm:fields', 'paperclip'),),
        (button('Фото категорий', 'adm:photos', 'photo'),),
        (button('Чеки на проверке', 'adm:orders', 'card'),),
        (button('Главное меню', 'home', 'home'),),
    )


def photo_admin_kb():
    return keyboard(*[(button(f"{CATALOG[k]['title']} {'✅' if store.photo(k) else '➕'}", f'adm:photo:{k}', 'photo'),)
                      for k in CATALOG],
                    (button('В админ-панель', 'adm:home', 'settings'),))


def photo_remove_kb(category: str):
    return keyboard((button('Убрать фото', f'adm:photo_del:{category}', 'warning', 'danger'),),
                    (button('Назад', 'adm:photos', 'home'),))


async def show_admin(message: Message):
    cfg = game_config()
    admin_input.pop(OWNER_ID, None)
    await message.answer(
        f"{em('settings')} <b>FELIX — Админ-панель</b>\n\n"
        f"База: <code>{escape(cfg.database or 'не указана')}</code>\n"
        f"Хост: <code>{escape(cfg.host or 'не указан')}</code>\n"
        f"Пользователь: <code>{escape(cfg.user or 'не указан')}</code>\n"
        "Пароль: <b>••••••</b> (не показывается)\n\n"
        "Для подключения отправь одной строкой: Логин Пароль Имя_базы. "
        "MySQL-хост и ссылку phpMyAdmin можно указать отдельно. Затем проверь игровые поля.",
        reply_markup=admin_panel_kb())


async def show_admin_orders(message: Message, bot: Bot):
    orders = store.pending()
    if not orders:
        await message.answer('Новых чеков на проверке нет.', reply_markup=admin_panel_kb())
        return
    await message.answer(f"{em('card')} <b>Чеки на проверке: {len(orders)}</b>")
    for order in orders:
        # Чек, сведения о заказе и кнопки подтверждения — одним сообщением.
        try:
            caption = order_description(order)
            markup = review_kb(order['id'])
            if order['receipt_kind'] == 'photo':
                await bot.send_photo(OWNER_ID, order['receipt_file_id'],
                                     caption=caption, reply_markup=markup)
            elif order['receipt_kind'] == 'document':
                await bot.send_document(OWNER_ID, order['receipt_file_id'],
                                        caption=caption, reply_markup=markup)
            else:
                # Только на случай повреждённой или старой записи без чека.
                await message.answer(caption, reply_markup=markup)
        except Exception:
            logging.exception('Cannot resend receipt of order %s', order['id'])
            await message.answer(order_description(order), reply_markup=review_kb(order['id']))


async def admin_prompt(message: Message, stage: dict):
    if stage['kind'] == 'db':
        cfg = game_config()
        await message.answer(
            f"{em('settings')} <b>Подключение MySQL</b>\n\n"
            f"{em('check')} <b>Формат:</b> <code>Логин Пароль Имя_базы</code>\n"
            f"{em('check')} <b>Пример:</b> <code>gs123456 MyPass gs123456</code>\n\n"
            f"Текущий MySQL-хост: <code>{escape(cfg.host)}:{cfg.port}</code>\n"
            "Отправь все три значения <b>одним сообщением</b> в личном чате бота. "
            "Бот попытается удалить сообщение с паролем после получения.\n\n"
            "Если сервер использует другой MySQL-хост, сначала нажми «Сменить MySQL-хост».\n"
            "Для отмены — /admin",
            reply_markup=keyboard(
                (button('Сменить MySQL-хост', 'adm:host', 'settings'),),
                (button('Назад', 'adm:home', 'home'),)))
    elif stage['kind'] == 'fields':
        i = stage['idx']
        field, label = FIELD_STEPS[i]
        old = getattr(game_config(), field.removeprefix('mysql_'), '')
        await message.answer(
            f"{em('settings')} <b>Поля игры — шаг {i + 1}/{len(FIELD_STEPS)}</b>\n"
            f"<b>{escape(label)}</b>\n"
            f"Текущее значение: <code>{escape(str(store.setting(field, old)))}</code>\n"
            "Пришли название из структуры твоей MySQL. Для отмены — /admin")


async def home(message: Message):
    await message.answer(
        f"{em('fire')} <b>FELIX | АВТОДОНАТ</b>\n\n"
        "Выбери, что хочешь приобрести. После оплаты отправь чек. "
        "Владелец проверит перевод, и бот выдаст покупку игровому аккаунту.\n\n"
        f"{em('check')} <b>4 категории покупок</b>",
        reply_markup=home_kb(owner=message.chat.id == OWNER_ID))


@dp.message(CommandStart())
@dp.message(Command('menu'))
async def start(message: Message):
    if message.chat.type != 'private':
        return
    waiting_for_nickname.pop(message.from_user.id, None)
    admin_input.pop(message.from_user.id, None)
    await home(message)


@dp.message(Command('cancel'))
async def cancel_cmd(message: Message):
    if message.chat.type != 'private':
        return
    waiting_for_nickname.pop(message.from_user.id, None)
    if message.from_user.id == OWNER_ID and admin_input.pop(OWNER_ID, None):
        await message.answer('Действие администратора отменено.', reply_markup=admin_panel_kb())
        return
    active = store.active(message.from_user.id)
    if active and active['status'] == 'awaiting_receipt':
        store.cancel(active['id'], message.from_user.id)
        await message.answer(f"Заказ #{active['id']} отменён.", reply_markup=home_kb())
    else:
        await message.answer('Выбор товара отменён. Проверяемый чек отменить нельзя.', reply_markup=home_kb())


@dp.message(Command('myorders'))
async def myorders_cmd(message: Message):
    if message.chat.type != 'private':
        return
    await show_orders(message, message.from_user.id)


async def show_orders(message: Message, uid: int):
    orders = store.latest(uid)
    if not orders:
        await message.answer('У тебя пока нет покупок.', reply_markup=home_kb())
        return
    statuses = {'awaiting_receipt': 'ожидает чек', 'review': 'на проверке',
                'processing': 'выдаётся', 'delivered': 'выдано',
                'rejected': 'отклонено', 'cancelled': 'отменено'}
    lines = [f"#{o['id']} — {escape(o['item_title'])} / <b>{statuses.get(o['status'], o['status'])}</b>"
             for o in orders]
    await message.answer(f"{em('paperclip')} <b>Мои покупки</b>\n\n" + '\n'.join(lines), reply_markup=home_kb())


@dp.message(Command('admin'))
async def admin_cmd(message: Message):
    if message.chat.type != 'private' or message.from_user.id != OWNER_ID:
        return
    await show_admin(message)


@dp.callback_query(F.data.startswith('adm:'))
async def admin_callbacks(call: CallbackQuery, bot: Bot):
    if not owner_only(call.from_user.id, call.message.chat.id):
        await call.answer('Только владелец', show_alert=True)
        return
    command = call.data.split(':')
    action = command[1]
    await call.answer()
    if action == 'home':
        await show_admin(call.message)
    elif action == 'orders':
        await show_admin_orders(call.message, bot)
    elif action == 'db':
        admin_input[OWNER_ID] = {'kind': 'db'}
        await admin_prompt(call.message, admin_input[OWNER_ID])
    elif action == 'host':
        admin_input[OWNER_ID] = {'kind': 'host'}
        await call.message.answer(
            f"{em('settings')} <b>MySQL-хост</b>\n\n"
            "Пришли адрес игрового MySQL-сервера, например:\n"
            "<code>164.132.206.179:3306</code>\n\n"
            "Это не ссылка на phpMyAdmin. Укажи настоящий адрес MySQL из панели хостинга.\n"
            "Для отмены — /admin")
    elif action == 'pma_set':
        admin_input[OWNER_ID] = {'kind': 'pma_set'}
        await call.message.answer(
            f"{em('settings')} <b>Адрес панели phpMyAdmin</b>\n\n"
            "Пришли HTTPS-адрес страницы входа, например:\n"
            "<code>https://node1.maze-host.ru/index.php</code>\n\n"
            "Не присылай ссылки с паролями. Если панель не нужна, отправь <code>-</code>.\n"
            "Для отмены — /admin")
    elif action == 'fields':
        admin_input[OWNER_ID] = {'kind': 'fields', 'idx': 0, 'draft': {}}
        await admin_prompt(call.message, admin_input[OWNER_ID])
    elif action == 'pma':
        cfg = game_config()
        link = safe_panel_link(store.setting('pma_url', ''), cfg.database)
        if not link:
            await call.message.answer('Адрес phpMyAdmin ещё не указан. Выбери «Настроить phpMyAdmin».',
                                      reply_markup=admin_panel_kb())
        else:
            await call.message.answer(
                f"{em('paperclip')} <b>Вход в phpMyAdmin</b>\n\n"
                "Ссылка не содержит пароля. Войди через страницу хостинга вручную.\n\n"
                "Для автоматической выдачи бот использует отдельное соединение MySQL.",
                reply_markup=keyboard((InlineKeyboardButton(text='Открыть phpMyAdmin', url=link),),
                                      (button('Админ-панель', 'adm:home', 'settings'),)))
    elif action == 'test':
        cfg = game_config()
        try:
            await asyncio.to_thread(mysql_probe, cfg, True)
        except GrantError as exc:
            await call.message.answer(f"{em('warning')} <b>Проверка не пройдена</b>\n"
                                      f"{escape(str(exc))}\n\n"
                                      "Если соединение доступно, но таблицы другие — выбери «Поля игрового мода».",
                                      reply_markup=admin_panel_kb())
        else:
            await call.message.answer(f"{em('check')} <b>MySQL подключена, таблица и все 5 полей найдены!</b>\n"
                                      "Это проверка чтения, не тест начисления.",
                                      reply_markup=admin_panel_kb())
    elif action == 'photos':
        admin_input.pop(OWNER_ID, None)
        await call.message.answer('Выбери категорию, для которой хочешь установить фото:',
                                  reply_markup=photo_admin_kb())
    elif action == 'photo' and len(command) == 3 and command[2] in CATALOG:
        category = command[2]
        admin_input[OWNER_ID] = {'kind': 'photo', 'category': category}
        await call.message.answer(f"{em('photo')} <b>{escape(CATALOG[category]['title'])}</b>\n\n"
                                  "Пришли фотографию или изображение-файл JPG/PNG/WEBP. "
                                  "Оно появится перед товарами выбранной категории.",
                                  reply_markup=photo_remove_kb(category))
    elif action == 'photo_del' and len(command) == 3 and command[2] in CATALOG:
        category = command[2]
        store.remove_photo(category)
        admin_input.pop(OWNER_ID, None)
        await call.message.answer('Фото категории удалено.', reply_markup=photo_admin_kb())
    else:
        await call.message.answer('Неизвестная команда администратора.', reply_markup=admin_panel_kb())


async def admin_text(message: Message, stage: dict):
    kind = stage['kind']
    value = (message.text or '').strip()
    if kind == 'db':
        # Пароль не должен оставаться в чате: удаляем сообщение даже при неверном формате.
        try:
            await message.delete()
        except Exception:
            pass
        try:
            user, password, database = parse_mysql_credentials(value)
        except ValueError as exc:
            await message.answer(f"{em('warning')} {escape(str(exc))}\nОтправь три значения одним сообщением.")
            return
        current = game_config()
        candidate = GameConfig(
            host=current.host, port=current.port,
            user=user, password=password, database=database,
            table=current.table, nickname_col=current.nickname_col,
            level_col=current.level_col, admin_col=current.admin_col,
            donate_col=current.donate_col, money_col=current.money_col)
        try:
            await asyncio.to_thread(mysql_probe, candidate)
        except GrantError as exc:
            # Никогда не отражаем присланный пароль обратно в чат.
            detail = str(exc).replace(password, '[скрыто]') if password else str(exc)
            await message.answer(
                f"{em('warning')} <b>Не удалось подключиться</b>\n"
                f"{escape(detail)}\n\n"
                "Проверь логин, пароль, базу и MySQL-хост. При необходимости выбери «Сменить MySQL-хост».\n"
                "Затем повторно пришли данные в формате: <code>Логин Пароль Имя_базы</code>.",
                reply_markup=keyboard(
                    (button('Сменить MySQL-хост', 'adm:host', 'settings'),),
                    (button('Админ-панель', 'adm:home', 'home'),)))
            return
        store.set_settings({'mysql_user': user, 'mysql_password': password, 'mysql_database': database})
        admin_input.pop(OWNER_ID, None)
        link = safe_panel_link(store.setting('pma_url', ''), database)
        response = (
            f"{em('check')} <b>Подключение MySQL успешно!</b>\n"
            f"Хост: <code>{escape(current.host)}:{current.port}</code>\n"
            f"Логин: <code>{escape(user)}</code>\n"
            f"База: <code>{escape(database)}</code>\n\n"
            "Пароль сохранён и не показывается. Проверь поля игрового мода перед автовыдачей.\n"
            "Ссылка phpMyAdmin не содержит пароль.")
        if link:
            await message.answer(response, reply_markup=keyboard(
                (InlineKeyboardButton(text='Открыть phpMyAdmin', url=link),),
                (button('Админ-панель', 'adm:home', 'settings'),)))
        else:
            await message.answer(response, reply_markup=admin_panel_kb())
        return
    if kind == 'host':
        try:
            host, port = parse_mysql_endpoint(value)
        except ValueError as exc:
            await message.answer(escape(str(exc)))
            return
        store.set_settings({'mysql_host': host, 'mysql_port': str(port)})
        admin_input[OWNER_ID] = {'kind': 'db'}
        await message.answer(f"{em('check')} MySQL-хост сохранён: <code>{escape(host)}:{port}</code>.")
        await admin_prompt(message, admin_input[OWNER_ID])
        return
    if kind == 'pma_set':
        try:
            panel = normalize_panel_url(value)
        except ValueError as exc:
            await message.answer(escape(str(exc)))
            return
        if any(k in value.lower() for k in ('pma_password=', 'password=')):
            try:
                await message.delete()
            except Exception:
                pass
        store.set_settings({'pma_url': panel})
        admin_input.pop(OWNER_ID, None)
        await message.answer(
            f"{em('check')} Адрес phpMyAdmin {'сохранён' if panel else 'очищен'}.\n"
            "Пароли в ссылках не сохраняются.", reply_markup=admin_panel_kb())
        return
    if kind == 'fields':
        idx = stage['idx']
        field, label = FIELD_STEPS[idx]
        try:
            identifier(value)
        except GrantError:
            await message.answer('Имя таблицы/столбца: только латинские буквы, цифры и _. Первым символом — буква или _.')
            return
        stage['draft'][field] = value
        stage['idx'] += 1
        if stage['idx'] < len(FIELD_STEPS):
            await admin_prompt(message, stage)
            return
        store.set_settings(stage['draft'])
        admin_input.pop(OWNER_ID, None)
        cfg = game_config()
        try:
            await asyncio.to_thread(mysql_probe, cfg, True)
        except GrantError as exc:
            await message.answer(f"{em('warning')} <b>Поля сохранены, но проверка не пройдена</b>\n"
                                 f"{escape(str(exc))}\n\n"
                                 "Сверь названия с реальной игровой базой.", reply_markup=admin_panel_kb())
        else:
            await message.answer(f"{em('check')} <b>Игровые поля сохранены и найдены в MySQL.</b>\n"
                                 "Для выдачи у пользователя MySQL также нужны права UPDATE и CREATE TABLE.",
                                 reply_markup=admin_panel_kb())
        return


async def admin_photo(message: Message, bot: Bot, stage: dict):
    category = stage['category']
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and message.document.mime_type and message.document.mime_type.startswith('image/'):
        if (message.document.file_size or 0) > 10 * 1024 * 1024:
            await message.answer('Изображение слишком большое (максимум 10 МБ).')
            return
        try:
            from PIL import Image, ImageOps
            Image.MAX_IMAGE_PIXELS = 35_000_000
            fobj = io.BytesIO()
            await bot.download(message.document, destination=fobj)
            fobj.seek(0)
            with Image.open(fobj) as im:
                im = ImageOps.exif_transpose(im).convert('RGB')
                im.thumbnail((1600, 1600))
                out = io.BytesIO()
                im.save(out, format='JPEG', quality=91)
            posted = await bot.send_photo(OWNER_ID, BufferedInputFile(out.getvalue(), filename='category.jpg'),
                                          caption='Предпросмотр фото категории')
            file_id = posted.photo[-1].file_id
        except Exception:
            logging.exception('Cannot convert admin photo')
            await message.answer('Не удалось прочитать изображение. Пришли обычное фото или JPG/PNG/WEBP.')
            return
    else:
        await message.answer('Пришли фотографию или изображение-файл JPG/PNG/WEBP.')
        return
    store.set_photo(category, file_id)
    admin_input.pop(OWNER_ID, None)
    await message.answer(f"{em('check')} <b>Фото для «{escape(CATALOG[category]['title'])}» сохранено.</b>",
                         reply_markup=photo_admin_kb())


def order_description(o: dict):
    return (f"{em('card')} <b>Заказ #{o['id']}</b>\n"
            f"Товар: <b>{escape(o['item_title'])}</b>\n"
            f"Количество: <code>{o['units']}</code>\n"
            f"Ник: <code>{escape(o['nickname'])}</code>\n"
            f"Telegram ID: <code>{o['telegram_id']}</code>\n"
            f"Сумма: <b>{o['price']} ₽</b>\n"
            f"Статус: <code>{escape(o['status'])}</code>")


@dp.callback_query(F.data == 'home')
async def callback_home(call: CallbackQuery):
    await call.answer()
    await home(call.message)


@dp.callback_query(F.data == 'my')
async def callback_my(call: CallbackQuery):
    await call.answer()
    await show_orders(call.message, call.from_user.id)


@dp.callback_query(F.data.startswith('cat:'))
async def callback_category(call: CallbackQuery):
    category = call.data.partition(':')[2]
    if category not in CATALOG:
        await call.answer('Категория не найдена', show_alert=True)
        return
    await call.answer()
    caption = (f"{em('star')} <b>{escape(CATALOG[category]['title'])}</b>\n\n"
               "Выбери пакет. Цены указаны в рублях.")
    photo_id = store.photo(category)
    if photo_id:
        try:
            await call.message.answer_photo(photo_id, caption=caption, reply_markup=section_kb(category))
            return
        except Exception:
            logging.exception('Cannot show photo for category %s', category)
    await call.message.answer(caption, reply_markup=section_kb(category))


@dp.callback_query(F.data.startswith('buy:'))
async def callback_buy(call: CallbackQuery):
    code = call.data.partition(':')[2]
    if code not in PRODUCTS:
        await call.answer('Товар не найден', show_alert=True)
        return
    active = store.active(call.from_user.id)
    if active:
        await call.answer(f"У тебя уже есть заказ #{active['id']} ({active['status']})", show_alert=True)
        return
    waiting_for_nickname[call.from_user.id] = code
    item = PRODUCTS[code]
    await call.answer()
    await call.message.answer(
        f"{em('check')} <b>{escape(item['title'])}</b>\n"
        f"Стоимость: <b>{item['price']} ₽</b>\n\n"
        "<b>Шаг 1/3 — игровой ник</b>\n"
        "Отправь ник своего аккаунта на SA:MP сервере.\n"
        "Например: <code>Ivan_Ivanov</code>\n\n"
        "Не отправляй пароль от игрового аккаунта.")


@dp.callback_query(F.data.startswith('cancel:'))
async def callback_cancel(call: CallbackQuery):
    try:
        order_id = int(call.data.partition(':')[2])
    except ValueError:
        await call.answer('Неверный номер', show_alert=True)
        return
    if store.cancel(order_id, call.from_user.id):
        await call.message.answer(f"{em('check')} Заказ #{order_id} отменён.", reply_markup=home_kb())
        await call.answer('Отменено')
    else:
        await call.answer('Заказ уже нельзя отменить этой кнопкой', show_alert=True)


async def report_receipt(bot: Bot, order: dict, receipt_message: Message):
    # copy_message сохраняет изображение или PDF, но заменяет подпись на
    # детали покупки и прикрепляет кнопки к ЭТОМУ ЖЕ сообщению с чеком.
    await bot.copy_message(
        chat_id=OWNER_ID,
        from_chat_id=receipt_message.chat.id,
        message_id=receipt_message.message_id,
        caption=order_description(order),
        reply_markup=review_kb(order['id']),
    )


@dp.message(F.photo | F.document)
async def receipt(message: Message, bot: Bot):
    if message.chat.type != 'private':
        return
    uid = message.from_user.id
    if owner_only(uid, message.chat.id) and admin_input.get(uid):
        state = admin_input[uid]
        if state['kind'] == 'photo':
            await admin_photo(message, bot, state)
        else:
            await message.answer('Сейчас ожидается текстовый ответ. Введи значение или нажми /admin для отмены.')
        return
    order = store.active(uid)
    if not order or order['status'] != 'awaiting_receipt':
        await message.answer('Сначала выбери товар через /start. Фото пока не привязано к заказу.')
        return
    if message.photo:
        file = message.photo[-1]
        kind = 'photo'
    else:
        file = message.document
        if file.mime_type not in ('image/jpeg', 'image/png', 'image/webp', 'application/pdf'):
            await message.answer('Пришли чек фотографией, JPG, PNG, WEBP или PDF.')
            return
        kind = 'document'
    try:
        success = store.submit_receipt(order['id'], uid, file.file_id, file.file_unique_id, kind)
    except ValueError as exc:
        await message.answer(f"{em('warning')} {escape(str(exc))}")
        return
    if not success:
        await message.answer('Заказ уже изменился, напиши /myorders.')
        return
    order = store.get(order['id'])
    await message.answer(
        f"{em('check')} <b>Чек для заказа #{order['id']} принят!</b>\n\n"
        "Ожидай решения владельца. После подтверждения бот попробует сразу выдать покупку в игровую базу.",
        reply_markup=home_kb())
    try:
        await report_receipt(bot, order, message)
    except Exception:
        logging.exception('Owner notification failed for order %s', order['id'])
        await message.answer('Чек сохранён, но уведомить владельца не удалось. Он увидит заказ через /admin.')


@dp.message(F.text)
async def nickname_or_default(message: Message):
    if message.chat.type != 'private':
        return
    uid = message.from_user.id
    if owner_only(uid, message.chat.id) and admin_input.get(uid):
        stage = admin_input[uid]
        if stage['kind'] == 'photo':
            await message.answer('Пришли фотографию для выбранной категории или нажми /admin для отмены.')
        else:
            await admin_text(message, stage)
        return
    code = waiting_for_nickname.get(uid)
    if not code:
        active = store.active(uid)
        if active and active['status'] == 'awaiting_receipt':
            await message.answer(f"{em('paperclip')} <b>Шаг 3/3 — чек</b>\n\n"
                                 f"Отправь фото или PDF чека за заказ #{active['id']}.",
                                 reply_markup=payment_kb(active['id']))
        else:
            await home(message)
        return
    nickname = (message.text or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_]{3,24}', nickname):
        await message.answer('Укажи игровой ник от 3 до 24 символов: латинские буквы, цифры, _. Пример: Ivan_Ivanov')
        return
    if store.active(uid):
        waiting_for_nickname.pop(uid, None)
        await message.answer('У тебя уже есть активный заказ. Проверь /myorders.')
        return
    order = store.create(uid, nickname, code, PRODUCTS[code])
    waiting_for_nickname.pop(uid, None)
    await message.answer(
        f"{em('card')} <b>Заказ #{order['id']}</b>\n\n"
        f"<b>Шаг 2/3 — оплата</b>\n"
        f"Товар: <b>{escape(order['item_title'])}</b>\n"
        f"Ник: <code>{escape(nickname)}</code>\n"
        f"К оплате: <b>{order['price']} ₽</b>\n\n"
        f"Реквизиты: <code>{escape(PAYMENT_DETAILS)}</code>\n\n"
        f"{em('paperclip')} <b>Шаг 3/3 — отправь фото или PDF чека</b>\n"
        "После проверки владельцем награда будет выдана автоматически.",
        reply_markup=payment_kb(order['id']))


async def dismiss_review_buttons(call: CallbackQuery):
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data.startswith('ok:'))
async def approve(call: CallbackQuery, bot: Bot):
    if call.from_user.id != OWNER_ID or call.message.chat.id != OWNER_ID:
        await call.answer('Только владелец', show_alert=True)
        return
    try:
        order_id = int(call.data.partition(':')[2])
    except ValueError:
        await call.answer('Неверный номер', show_alert=True)
        return
    if not store.claim(order_id):
        await call.answer('Заказ уже обработан или недоступен', show_alert=True)
        return
    await call.answer('Проверяю аккаунт и выдаю товар…')
    order = store.get(order_id)
    try:
        result = await asyncio.to_thread(grant_to_game, order, game_config())
    except GrantError as exc:
        store.retry(order_id, str(exc))
        await call.message.answer(
            f"{em('warning')} <b>Заказ #{order_id} не выдан</b>\n"
            f"{escape(str(exc))}\n\nИсправь настройки/аккаунт и повтори подтверждение.",
            reply_markup=review_kb(order_id))
        return
    except Exception:
        logging.exception('Unexpected grant error for #%s', order_id)
        store.retry(order_id, 'Неожиданная ошибка выдачи')
        await call.message.answer(f"Ошибка выдачи #{order_id}; повтори после исправления.",
                                  reply_markup=review_kb(order_id))
        return
    store.delivered(order_id)
    await dismiss_review_buttons(call)
    await call.message.answer(f"{em('check')} <b>Заказ #{order_id} выдан!</b>\n"
                              f"Игрок: <code>{escape(order['nickname'])}</code>\n"
                              f"Результат: <code>{result}</code>")
    try:
        await bot.send_message(order['telegram_id'],
            f"{em('check')} <b>Покупка #{order_id} подтверждена!</b>\n"
            f"Выдано: <b>{escape(order['item_title'])}</b>\n"
            f"Ник: <code>{escape(order['nickname'])}</code>\n\n"
            "Данные сохранены в игровой базе. Если ты сейчас в игре и сервер держит статистику в памяти, "
            "понадобится повторный вход либо интеграция онлайн-обновления.")
    except Exception:
        logging.exception('Cannot notify buyer #%s', order_id)


@dp.callback_query(F.data.startswith('no:'))
async def decline(call: CallbackQuery, bot: Bot):
    if call.from_user.id != OWNER_ID or call.message.chat.id != OWNER_ID:
        await call.answer('Только владелец', show_alert=True)
        return
    try:
        order_id = int(call.data.partition(':')[2])
    except ValueError:
        await call.answer('Неверный номер', show_alert=True)
        return
    if not store.reject(order_id):
        await call.answer('Этот заказ уже обработан', show_alert=True)
        return
    await call.answer('Чек отклонён')
    await dismiss_review_buttons(call)
    await call.message.answer(f"{em('warning')} Заказ #{order_id} отклонён.")
    order = store.get(order_id)
    try:
        await bot.send_message(order['telegram_id'],
                               f"{em('warning')} <b>Чек по заказу #{order_id} не принят.</b>\n"
                               "Уточни причину у владельца перед новой покупкой.")
    except Exception:
        logging.exception('Cannot notify buyer about rejected #%s', order_id)


async def main():
    if not BOT_TOKEN or BOT_TOKEN == 'ВСТАВЬ_НОВЫЙ_ТОКЕН':
        raise SystemExit('Укажи BOT_TOKEN в config.py (новый токен из @BotFather)')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        store.close()
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
