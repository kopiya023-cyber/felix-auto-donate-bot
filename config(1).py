"""Настройки FELIX AUTO DONATE BOT. Измени значения и сохрани файл."""

# 1. Новый токен от @BotFather (старый токен, отправленный в чат, перевыпусти)
BOT_TOKEN = ""
OWNER_ID = 0  # Telegram ID владельца бота

# 2. Реквизиты для оплаты, которые бот покажет покупателю
PAYMENT_DETAILS = "+77777777777"

# 3. Показывать премиум-эмодзи Telegram (нужна поддержка со стороны Telegram)
PREMIUM_EMOJI = True

# 4. Подключение к MySQL ИГРОВОГО СЕРВЕРА
# Данные должны соответствовать реальной таблице аккаунтов в твоём игровом моде.
# До настройки этих полей выдача не заработает.
GAME_DB_HOST = "127.0.0.1"
GAME_DB_PORT = 3306
GAME_DB_USER = ""
GAME_DB_PASSWORD = ""
GAME_DB_NAME = ""

# Названия таблицы и столбцов базы игрового сервера.
# Это ПРИМЕРЫ: если в моде другие названия, замени их здесь.
GAME_ACCOUNT_TABLE = "accounts"
GAME_NICK_COLUMN = "Name"
GAME_LEVEL_COLUMN = "Level"
GAME_ADMIN_COLUMN = "Admin"
GAME_DONATE_COLUMN = "Donate"
GAME_MONEY_COLUMN = "Money"

# 5. Прайс-лист: price — цена в рублях, units — сколько выдать в игре.
# Для админки units — установленный уровень админки (не прибавляется).
# Изменяй цены как хочешь, идентификаторы id лучше не менять.
CATALOG = {
    "level": {
        "title": "Приобрести ЛВЛ",
        "icon": "star",
        "products": [
            {
                "id": "lvl1",
                "title": "+1 уровень",
                "kind": "level",
                "units": 1,
                "price": 50
            },
            {
                "id": "lvl5",
                "title": "+5 уровней",
                "kind": "level",
                "units": 5,
                "price": 200
            },
            {
                "id": "lvl10",
                "title": "+10 уровней",
                "kind": "level",
                "units": 10,
                "price": 350
            }
        ]
    },
    "admin": {
        "title": "Приобрести админку",
        "icon": "lock",
        "products": [
            {
                "id": "adm1",
                "title": "Админка 1 уровня",
                "kind": "admin",
                "units": 1,
                "price": 100
            },
            {
                "id": "adm2",
                "title": "Админка 2 уровня",
                "kind": "admin",
                "units": 2,
                "price": 250
            },
            {
                "id": "adm3",
                "title": "Админка 3 уровня",
                "kind": "admin",
                "units": 3,
                "price": 500
            }
        ]
    },
    "donate": {
        "title": "Приобрести донат",
        "icon": "fire",
        "products": [
            {
                "id": "don100",
                "title": "100 донат-очков",
                "kind": "donate",
                "units": 100,
                "price": 100
            },
            {
                "id": "don500",
                "title": "500 донат-очков",
                "kind": "donate",
                "units": 500,
                "price": 400
            },
            {
                "id": "don1000",
                "title": "1 000 донат-очков",
                "kind": "donate",
                "units": 1000,
                "price": 700
            }
        ]
    },
    "money": {
        "title": "Приобрести валюту",
        "icon": "money",
        "products": [
            {
                "id": "cash100k",
                "title": "100 000 игровой валюты",
                "kind": "money",
                "units": 100000,
                "price": 100
            },
            {
                "id": "cash500k",
                "title": "500 000 игровой валюты",
                "kind": "money",
                "units": 500000,
                "price": 400
            },
            {
                "id": "cash1m",
                "title": "1 000 000 игровой валюты",
                "kind": "money",
                "units": 1000000,
                "price": 700
            }
        ]
    }
}
