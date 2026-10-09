import csv
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from bank_import import parse_statement
from extra_statements import parse_extra_statement
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

load_dotenv()
logging.basicConfig(level=logging.WARNING)
DB = os.getenv('DB_PATH', 'expenses.sqlite3')
CATEGORIES = ('продукты', 'кафе', 'транспорт', 'жильё', 'подписки', 'развлечения', 'здоровье', 'покупки', 'путешествия', 'доход', 'переводы', 'другое')
INCOME_CATEGORIES = ('зарплата', 'подработка', 'подарки', 'возврат', 'проценты', 'другое')
TRIP_CATEGORIES = ('дорога', 'жильё', 'еда', 'транспорт', 'развлечения', 'покупки', 'другое')
RULES = {'продукты': ('spar', 'lidl', 'hofer', 'mercator', 'tuš', 'tus', 'магазин'), 'кафе': ('coffee', 'cafe', 'кофе', 'ресторан', 'restavracija'), 'транспорт': ('petrol', 'uber', 'bolt', 'такси', 'bus'), 'подписки': ('netflix', 'spotify', 'youtube premium'), 'жильё': ('rent', 'аренда'), 'здоровье': ('pharmacy', 'аптека', 'lekarna')}
CURRENCY_SYMBOLS = {'€': 'EUR', '$': 'USD', '£': 'GBP', '₽': 'RUB', '₾': 'GEL'}
CURRENCY_MARK = r'(?:[A-Za-z]{3}|[€$£₽₾])'
LOCAL_TZ = ZoneInfo('Europe/Ljubljana')
VERSION = '2026-10-07.3'
CURRENT_USER = ContextVar('telegram_user_id', default=None)

def owner():
    value = CURRENT_USER.get()
    if value is None: raise RuntimeError('Telegram user context missing')
    return value

MONTHS = {name: i for i, forms in enumerate((('января', 'январь'), ('февраля', 'февраль'), ('марта', 'март'), ('апреля', 'апрель'), ('мая', 'май'), ('июня', 'июнь'), ('июля', 'июль'), ('августа', 'август'), ('сентября', 'сентябрь'), ('октября', 'октябрь'), ('ноября', 'ноябрь'), ('декабря', 'декабрь')), 1) for name in forms}
NUMBER_WORDS = {'ноль': 0, 'один': 1, 'одна': 1, 'два': 2, 'две': 2, 'три': 3, 'четыре': 4, 'пять': 5, 'шесть': 6, 'семь': 7, 'восемь': 8, 'девять': 9, 'десять': 10, 'полтора': 1.5}
CURRENCY_WORDS = {'евро': 'EUR', 'доллар': 'USD', 'доллара': 'USD', 'долларов': 'USD', 'рубль': 'RUB', 'рубля': 'RUB', 'рублей': 'RUB', 'фунт': 'GBP', 'фунта': 'GBP', 'фунтов': 'GBP', 'лари': 'GEL'}

def today():
    return datetime.now(LOCAL_TZ).date()

def parse_entry(value, default_currency='EUR'):
    # A friendly spoken example: «два с половиной евро кофе».
    spoken = re.fullmatch(r'\s*(два|две|три|четыре|пять|один|одна|полтора)(?:\s+с\s+половиной)?\s+(евро|доллар(?:а|ов)?|руб(?:ль|ля|лей)|фунт(?:а|ов)?|лари)\s+(.{2,120})\s*', value.casefold())
    if spoken:
        amount = NUMBER_WORDS[spoken.group(1)] + (0.5 if ' с половиной ' in value.casefold() else 0)
        value = f'{str(amount).replace(".", ",")} {CURRENCY_WORDS[spoken.group(2)]} {spoken.group(3)}'
    m = re.fullmatch(
        rf'\s*([+-]?)\s*({CURRENCY_MARK})?\s*(\d+(?:[.,]\d{{1,2}})?)\s*({CURRENCY_MARK})?\s+(.{{2,120}})\s*',
        value,
    )
    if not m:
        tail = re.fullmatch(
            rf'\s*(.{{2,120}}?)\s+([+-]?)\s*({CURRENCY_MARK})?\s*(\d+(?:[.,]\d{{1,2}})?)\s*({CURRENCY_MARK})?\s*',
            value,
        )
        if tail:
            # Put the amount first, then apply the same parsing rules.
            value = f"{tail.group(2)}{tail.group(3) or ''}{tail.group(4)}{tail.group(5) or ''} {tail.group(1)}"
            m = re.fullmatch(
                rf'\s*([+-]?)\s*({CURRENCY_MARK})?\s*(\d+(?:[.,]\d{{1,2}})?)\s*({CURRENCY_MARK})?\s+(.{{2,120}})\s*',
                value,
            )
    if not m or (m.group(2) and m.group(4)):
        return None
    marker = m.group(2) or m.group(4) or default_currency
    currency = CURRENCY_SYMBOLS.get(marker, marker.upper())
    cents = int(Decimal(m.group(3).replace(',', '.')) * 100)
    if cents <= 0:
        return None
    return (cents if m.group(1) == '+' else -cents, currency, m.group(5).strip())

def parse_date_prefix(value):
    value = value.strip()
    iso = re.match(r'^(\d{4}-\d{2}-\d{2})(?:\s+|$)', value)
    if iso:
        try: return date.fromisoformat(iso.group(1)), value[iso.end():].strip()
        except ValueError: return None
    natural = re.match(r'^(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?(?:\s+|$)', value, re.I)
    if not natural or natural.group(2).casefold() not in MONTHS: return None
    day, month = int(natural.group(1)), MONTHS[natural.group(2).casefold()]
    year = int(natural.group(3)) if natural.group(3) else today().year
    try:
        parsed = date(year, month, day)
        if not natural.group(3) and parsed > today(): parsed = date(year - 1, month, day)
        return parsed, value[natural.end():].strip()
    except ValueError: return None

def default_currency():
    with conn() as c:
        row = c.execute("SELECT value FROM user_settings WHERE user_id=? AND key='default_currency'", (owner(),)).fetchone()
    return row['value'] if row else None

def set_default_currency(value):
    with conn() as c: c.execute("INSERT OR REPLACE INTO user_settings(user_id,key,value) VALUES(?,'default_currency',?)", (owner(),value))

def currency_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton(name, callback_data=f'currency:{code}') for name, code in [('EUR €', 'EUR'), ('USD $', 'USD')]], [InlineKeyboardButton('Другая валюта', callback_data='currency:other')]])

def home_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton('📜 История расходов', callback_data='home:history'), InlineKeyboardButton('📊 Отчёт', callback_data='home:report')], [InlineKeyboardButton('💰 Доходы', callback_data='home:income'), InlineKeyboardButton('🎯 Цели', callback_data='home:goals')], [InlineKeyboardButton('✈️ Поездки', callback_data='home:trips'), InlineKeyboardButton('📥 Импорт выписки', callback_data='home:import')], [InlineKeyboardButton('💼 Мои деньги', callback_data='home:balance'), InlineKeyboardButton('💱 Валюта', callback_data='home:currency')]])

FLOW_KEYS = ('awaiting_income','income_category','awaiting_goal_new','awaiting_goal_amount','awaiting_trip_name','awaiting_trip_expense_id','trip_category','awaiting_trip_budget_id','awaiting_add_date','add_day','pending_category','awaiting_export_start','export_start')

def clear_flow(ctx):
    for key in FLOW_KEYS: ctx.user_data.pop(key,None)

def balance_values(currency):
    with conn() as c:
        opening=c.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?",(owner(),'opening:'+currency)).fetchone()
        change=c.execute("SELECT COALESCE(SUM(amount_cents),0) AS value FROM operations WHERE user_id=? AND currency=? AND category!='переводы'",(owner(),currency)).fetchone()['value']
        reserved=c.execute('SELECT COALESCE(SUM(m.amount_cents),0) AS value FROM goal_movements m JOIN savings_goals g ON g.id=m.goal_id AND g.user_id=m.user_id WHERE m.user_id=? AND g.currency=?',(owner(),currency)).fetchone()['value']
    balance=int(opening['value']) if opening else 0
    balance+=change
    return balance,reserved,balance-reserved

async def show_balance(message):
    with conn() as c:
        codes={r[0] for r in c.execute("SELECT DISTINCT currency FROM operations WHERE user_id=? AND category!='переводы'",(owner(),))}
        codes.update(r[0] for r in c.execute('SELECT DISTINCT currency FROM savings_goals WHERE user_id=?',(owner(),)))
        codes.update(r[0][8:] for r in c.execute("SELECT key FROM user_settings WHERE user_id=? AND key LIKE 'opening:%'",(owner(),)))
    codes.add(default_currency() or 'EUR')
    lines=['💼 Мои деньги · по валютам']
    for currency in sorted(codes):
        balance,reserved,available=balance_values(currency)
        lines.append(f'\n{currency}: баланс {balance/100:.2f}\n  В целях: {reserved/100:.2f}\n  Доступно: {available/100:.2f}')
    lines.append('\nБаланс = начальная сумма + доходы − расходы. Деньги в целях уже входят в баланс, поэтому вычитаются только из доступной суммы. Расходы поездок учитываются один раз. Валюты не пересчитываются.\nЕсли учёт начался с деньгами на руках, укажи /opening 500 EUR (заменяет начальную сумму этой валюты).')
    await message.reply_text('\n'.join(lines))

async def balance_cmd(u,ctx):
    if not await guard(u):return
    clear_flow(ctx)
    await show_balance(u.message)

async def opening_cmd(u,ctx):
    if not await guard(u):return
    args=ctx.args
    if len(args) not in (1,2):
        await u.message.reply_text('Начальная сумма: /opening 500 EUR. Это уже имевшиеся деньги до записей в боте; команда заменяет начальную сумму, а не добавляет доход.');return
    currency=args[1].upper() if len(args)==2 else (default_currency() or 'EUR')
    if not re.fullmatch('[A-Z]{3}',currency):
        await u.message.reply_text('Код валюты — три латинские буквы, например EUR.');return
    try:
        raw=Decimal(args[0].replace(',','.'))
        if not raw.is_finite() or raw<0 or raw>1_000_000_000 or raw*100!=int(raw*100):raise ValueError()
        cents=int(raw*100)
    except (ValueError,InvalidOperation):
        await u.message.reply_text('Укажи неотрицательную сумму с максимум двумя знаками после запятой.');return
    with conn() as c:c.execute('INSERT OR REPLACE INTO user_settings(user_id,key,value) VALUES(?,?,?)',(owner(),'opening:'+currency,str(cents)))
    await show_balance(u.message)

def period(arg):
    now = today()
    if not arg:
        return now.replace(day=1), now, now.strftime('%Y-%m')
    if arg == 'today':
        return now, now, 'Сегодня'
    if arg == 'week':
        return now - timedelta(days=now.weekday()), now, 'Эта неделя'
    if re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', arg):
        year, month = map(int, arg.split('-'))
        start = date(year, month, 1)
        end = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
        return start, end, arg
    raise ValueError('Нужен YYYY-MM, today или week')

def previous_month(value=None):
    d = value or today()
    return (d.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')

def report_keyboard(arg=None):
    start_day, end_day, _ = period(arg)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🔎 Подробные расходы', callback_data=f'reportdetail:{start_day}:{end_day}:0'), InlineKeyboardButton('📥 Скачать CSV', callback_data=f'reportexport:{start_day}:{end_day}')],
        [InlineKeyboardButton('Сегодня', callback_data='reportperiod:today'), InlineKeyboardButton('Эта неделя', callback_data='reportperiod:week')],
        [InlineKeyboardButton('Прошлый месяц', callback_data=f'reportperiod:{previous_month()}'), InlineKeyboardButton('Этот месяц', callback_data=f'reportperiod:{today():%Y-%m}')],
        [InlineKeyboardButton('📅 Выбрать другой месяц', callback_data='reportmonths:0')],
        [InlineKeyboardButton('💰 Отчёт по доходам', callback_data='income:period:this')],
    ])

def export_keyboard(last_period=None):
    buttons = [
        [InlineKeyboardButton('Сегодня', callback_data='exportperiod:today'), InlineKeyboardButton('Эта неделя', callback_data='exportperiod:week')],
        [InlineKeyboardButton('Этот месяц', callback_data=f'exportperiod:{today():%Y-%m}'), InlineKeyboardButton('Прошлый месяц', callback_data=f'exportperiod:{previous_month()}')],
        [InlineKeyboardButton('📅 Выбрать месяц', callback_data='exportmonths:0'), InlineKeyboardButton('✍️ Свой период', callback_data='exportcustom:start')],
    ]
    if last_period:
        buttons.insert(0,[InlineKeyboardButton('Период последнего отчёта', callback_data=f'exportdates:{last_period[0]}:{last_period[1]}')])
    return InlineKeyboardMarkup(buttons)

def parse_user_date(value):
    value = value.strip()
    dotted = re.fullmatch(r'(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?',value)
    if dotted:
        year = int(dotted.group(3)) if dotted.group(3) else today().year
        try:
            result = date(year,int(dotted.group(2)),int(dotted.group(1)))
            if not dotted.group(3) and result > today(): result = date(year-1,result.month,result.day)
            return result
        except ValueError: return None
    parsed = parse_date_prefix(value)
    return parsed[0] if parsed and not parsed[1] else None

@contextmanager
def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        with c:
            yield c
    finally:
        c.close()

def init():
    # A backup is made before the only schema change. Existing rows belong to the old allowed ID.
    legacy_owner = os.getenv('ALLOWED_TELEGRAM_ID', '').strip()
    with conn() as c:
        c.execute('CREATE TABLE IF NOT EXISTS operations(id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT, account_id TEXT, day TEXT NOT NULL, amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, description TEXT NOT NULL, category TEXT NOT NULL, UNIQUE(source, account_id, external_id))')
        c.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS overrides(needle TEXT PRIMARY KEY, category TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS budgets(category TEXT NOT NULL, currency TEXT NOT NULL, amount_cents INTEGER NOT NULL, PRIMARY KEY(category,currency))')
        migrated = 'user_id' in [r['name'] for r in c.execute('PRAGMA table_info(operations)')]
        legacy_data = any(c.execute('SELECT 1 FROM ' + name + ' LIMIT 1').fetchone() for name in ('operations','settings','overrides','budgets'))
        if not migrated and legacy_data and not legacy_owner.isdigit():
            raise RuntimeError('Для переноса старых записей сохрани прежний ALLOWED_TELEGRAM_ID в .env')
    if not migrated:
        if legacy_data:
            backup_path = DB + '.before_multiuser.sqlite3'
            if not os.path.exists(backup_path):
                with sqlite3.connect(DB) as source, sqlite3.connect(backup_path) as dest: source.backup(dest)
        with conn() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('CREATE TABLE operations_new(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, source TEXT NOT NULL, external_id TEXT, account_id TEXT, day TEXT NOT NULL, amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, description TEXT NOT NULL, category TEXT NOT NULL, UNIQUE(user_id,source,account_id,external_id))')
            c.execute('INSERT INTO operations_new(id,user_id,source,external_id,account_id,day,amount_cents,currency,description,category) SELECT id,?,source,external_id,account_id,day,amount_cents,currency,description,category FROM operations',(int(legacy_owner) if legacy_owner.isdigit() else 0,))
            c.execute('DROP TABLE operations')
            c.execute('ALTER TABLE operations_new RENAME TO operations')
            c.execute('CREATE INDEX IF NOT EXISTS operations_user_day ON operations(user_id,day)')
            c.execute('CREATE TABLE user_settings(user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(user_id,key))')
            c.execute('CREATE TABLE user_overrides(user_id INTEGER NOT NULL, needle TEXT NOT NULL, category TEXT NOT NULL, PRIMARY KEY(user_id,needle))')
            c.execute('CREATE TABLE user_budgets(user_id INTEGER NOT NULL, category TEXT NOT NULL, currency TEXT NOT NULL, amount_cents INTEGER NOT NULL, PRIMARY KEY(user_id,category,currency))')
            if legacy_owner.isdigit():
                user_id=int(legacy_owner)
                c.execute('INSERT INTO user_settings SELECT ?,key,value FROM settings',(user_id,))
                c.execute('INSERT INTO user_overrides SELECT ?,needle,category FROM overrides',(user_id,))
                c.execute('INSERT INTO user_budgets SELECT ?,category,currency,amount_cents FROM budgets',(user_id,))
    else:
        with conn() as c:
            c.execute('CREATE INDEX IF NOT EXISTS operations_user_day ON operations(user_id,day)')
            c.execute('CREATE TABLE IF NOT EXISTS user_settings(user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(user_id,key))')
            c.execute('CREATE TABLE IF NOT EXISTS user_overrides(user_id INTEGER NOT NULL, needle TEXT NOT NULL, category TEXT NOT NULL, PRIMARY KEY(user_id,needle))')
            c.execute('CREATE TABLE IF NOT EXISTS user_budgets(user_id INTEGER NOT NULL, category TEXT NOT NULL, currency TEXT NOT NULL, amount_cents INTEGER NOT NULL, PRIMARY KEY(user_id,category,currency))')
    with conn() as c:
        columns = {r['name'] for r in c.execute('PRAGMA table_info(operations)')}
    if 'income_category' not in columns or 'trip_id' not in columns or 'trip_category' not in columns:
        backup_path = DB + '.before_finance_features.sqlite3'
        if not os.path.exists(backup_path):
            with sqlite3.connect(DB) as source, sqlite3.connect(backup_path) as dest: source.backup(dest)
    with conn() as c:
        if 'income_category' not in columns:
            c.execute('ALTER TABLE operations ADD COLUMN income_category TEXT')
        if 'trip_id' not in columns:
            c.execute('ALTER TABLE operations ADD COLUMN trip_id INTEGER')
        if 'trip_category' not in columns:
            c.execute('ALTER TABLE operations ADD COLUMN trip_category TEXT')
        c.execute('CREATE TABLE IF NOT EXISTS savings_goals(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, currency TEXT NOT NULL, target_cents INTEGER NOT NULL CHECK(target_cents>0), created_day TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS goal_movements(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, goal_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents!=0), day TEXT NOT NULL, FOREIGN KEY(goal_id) REFERENCES savings_goals(id))')
        c.execute('CREATE INDEX IF NOT EXISTS goal_movements_owner ON goal_movements(user_id,goal_id)')
        c.execute('CREATE TABLE IF NOT EXISTS trips(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, currency TEXT NOT NULL, budget_cents INTEGER, active INTEGER NOT NULL DEFAULT 0, created_day TEXT NOT NULL)')
        c.execute('CREATE INDEX IF NOT EXISTS trips_owner ON trips(user_id,id)')
        c.execute('CREATE INDEX IF NOT EXISTS operations_trip ON operations(user_id,trip_id)')
        c.execute("UPDATE operations SET income_category='другое' WHERE amount_cents>0 AND income_category IS NULL")

def allowed(u):
    if not u.effective_user or not u.effective_chat or u.effective_chat.type != 'private': return False
    CURRENT_USER.set(int(u.effective_user.id))
    return True

async def guard(u):
    if not allowed(u):
        await u.effective_message.reply_text('Открой личный чат с ботом для учёта расходов.')
        return False
    return True

def category_for(description, amount):
    s = description.lower()
    with conn() as c:
        for r in c.execute('SELECT needle, category FROM user_overrides WHERE user_id=? ORDER BY length(needle) DESC', (owner(),)):
            if r['needle'] in s: return r['category']
    if amount > 0: return 'доход'
    if any(word in s for word in ('cash withdrawal', 'atm withdrawal', 'снятие наличных')): return 'переводы'
    for category, needles in RULES.items():
        if any(n in s for n in needles): return category
    return 'другое'

async def start(u, ctx):
    if not await guard(u): return
    clear_flow(ctx)
    if not default_currency():
        await u.message.reply_text('Привет! Выбери валюту для записей без обозначения валюты. Например, если выбрать EUR, «2,50 кофе» запишется как 2,50 EUR. Валюту можно поменять позже.', reply_markup=currency_keyboard())
        return
    await show_home(u.message)

async def show_home(message):
    currency = default_currency() or 'EUR'
    await message.reply_text(f'Учёт денег · версия {VERSION}\nВалюта по умолчанию: {currency}\n\nРасход: 2,50 кофе. Другая валюта: 3 USD такси.\nДоходы, накопления и поездки открываются кнопками ниже. /help — все команды.', reply_markup=home_keyboard())

async def currency_cmd(u, ctx):
    if not await guard(u): return
    if ctx.args:
        value = ctx.args[0].upper()
        if not re.fullmatch('[A-Z]{3}', value):
            await u.message.reply_text('Укажи три буквы: /currency EUR, /currency USD или /currency PLN.'); return
        set_default_currency(value)
        await u.message.reply_text(f'Валюта по умолчанию: {value}.')
        await show_home(u.message)
    else:
        await u.message.reply_text('Выбери валюту или напиши /currency PLN (три латинские буквы).', reply_markup=currency_keyboard())

async def navigation_button(u, ctx):
    q = u.callback_query
    await q.answer()
    if not allowed(u): return
    action = q.data.split(':', 1)[1]
    clear_flow(ctx)
    if action == 'history':
        rows, total = history_page()
        await q.message.reply_text(f'История · всего {total} операций. Выбери покупку или месяц.', reply_markup=history_markup(rows,total,0,'all') if rows else None)
    elif action == 'income':
        await show_income_menu(q.message,ctx)
    elif action == 'goals':
        await show_goals(q.message)
    elif action == 'trips':
        await show_trips(q.message)
    elif action == 'balance':
        await show_balance(q.message)
    elif action == 'import':
        ctx.user_data['awaiting_statement'] = True
        ctx.user_data.pop('pending_import', None)
        await q.message.reply_text('Пришли выписку файлом CSV, PDF, Excel (XLSX), OFX/QFX, QIF, CAMT XML или MT940. Перед записью я покажу предварительный итог.')
    elif action == 'report':
        ctx.user_data['report_period'] = (period(None)[0].isoformat(),period(None)[1].isoformat())
        await q.message.reply_text(report_text(), reply_markup=report_keyboard())
    else:
        await q.message.reply_text('Текущая валюта: ' + (default_currency() or 'EUR'), reply_markup=currency_keyboard())

async def currency_button(u, ctx):
    q = u.callback_query
    await q.answer()
    if not allowed(u): return
    value = q.data.split(':', 1)[1]
    if value == 'other':
        await q.message.reply_text('Напиши команду /currency PLN, заменив PLN на нужный трёхбуквенный код валюты.'); return
    set_default_currency(value)
    await q.message.reply_text(f'Валюта по умолчанию: {value}.')
    await show_home(q.message)

async def help_cmd(u, ctx):
    if not await guard(u): return
    await u.message.reply_text(f'/income — доходы по категориям; /income_report — отчёт по доходам\n/goals · /goalnew 1000 путешествие · /goaladd 50 — накопления\n/trips · /tripnew Рим · /tripadd 1,50 автобус · /tripreport — поездки\n/tripbudget 500 · /triplink ID — лимит и привязка банковского расхода к поездке\n/balance — баланс и доступные деньги; /opening 500 EUR — начальная сумма\n\n2,50 кофе — расход ({default_currency() or "EUR"} по умолчанию)\n3 USD такси — расход в другой валюте\n/add — запись за прошлую дату\n/currency — сменить валюту\n/report — отчёт с кнопками: сегодня, неделя, прошлый или другой месяц\n/history — вся история с выбором месяца и покупки\n/budget продукты 300 EUR — месячный лимит; /budgets — список\n/operations — последние 10\n/correct — исправить категорию\n/rule слово категория — правило для будущих записей\n/rules — свои правила\n/delete — удалить ручную запись\n/export — выбрать период и скачать все операции CSV\n/categories — категории\n/import — загрузить CSV, PDF, Excel (XLSX), OFX/QFX, QIF, CAMT XML или MT940')

async def cash(u, ctx):
    if not await guard(u): return
    raw = u.message.text.strip()
    if ctx.user_data.get('awaiting_income'):
        if await save_income(u,raw,ctx.user_data.get('income_category')):
            ctx.user_data.pop('awaiting_income',None)
            ctx.user_data.pop('income_category',None)
        return
    if ctx.user_data.get('awaiting_goal_new'):
        if await create_goal(u.message,raw):ctx.user_data.pop('awaiting_goal_new',None)
        return
    if ctx.user_data.get('awaiting_goal_amount'):
        goal_id,negative=ctx.user_data['awaiting_goal_amount']
        if await move_goal(u.message,goal_id,raw,negative):ctx.user_data.pop('awaiting_goal_amount',None)
        return
    if ctx.user_data.get('awaiting_trip_name'):
        if await create_trip(u.message,raw):ctx.user_data.pop('awaiting_trip_name',None)
        return
    if ctx.user_data.get('awaiting_trip_budget_id'):
        if await update_trip_budget(u.message,ctx.user_data['awaiting_trip_budget_id'],raw):ctx.user_data.pop('awaiting_trip_budget_id',None)
        return
    if ctx.user_data.get('awaiting_trip_expense_id'):
        if await add_trip_expense(u.message,ctx.user_data['awaiting_trip_expense_id'],raw,ctx.user_data.get('trip_category')):
            ctx.user_data.pop('trip_category',None)
        return
    if ctx.user_data.get('awaiting_export_start'):
        chosen = parse_user_date(raw)
        if not chosen or chosen > today():
            await u.message.reply_text('Не понял начальную дату. Пример: 1 сентября 2026 или 01.09.2026. /cancel — отменить.'); return
        ctx.user_data.pop('awaiting_export_start',None)
        ctx.user_data['export_start'] = chosen.isoformat()
        await u.message.reply_text(f'Начало: {chosen:%d.%m.%Y}. Теперь напиши конечную дату, например 03.10.2026.')
        return
    if 'export_start' in ctx.user_data:
        chosen = parse_user_date(raw)
        start_day = date.fromisoformat(ctx.user_data['export_start'])
        if not chosen or chosen < start_day or chosen > today() or (chosen-start_day).days > 3660:
            await u.message.reply_text('Конечная дата должна быть не раньше начальной и не в будущем. Пример: 03.10.2026. Максимальный период — 10 лет. /cancel — отменить.'); return
        ctx.user_data.pop('export_start',None)
        await send_period_csv(u.message,start_day.isoformat(),chosen.isoformat(),expenses_only=False)
        return
    if ctx.user_data.pop('awaiting_currency', False):
        code = raw.upper()
        if re.fullmatch('[A-Z]{3}', code):
            set_default_currency(code)
            await show_home(u.message)
        else:
            ctx.user_data['awaiting_currency'] = True
            await u.message.reply_text('Напиши код из трёх латинских букв, например PLN.')
        return
    if ctx.user_data.get('awaiting_add_date'):
        parsed = parse_date_prefix(raw)
        if not parsed or parsed[1] or parsed[0] > today():
            await u.message.reply_text('Напиши дату без суммы: 15 сентября, 15 сентября 2025 или 2025-09-15. Дата не должна быть в будущем.'); return
        ctx.user_data.pop('awaiting_add_date', None)
        ctx.user_data['add_day'] = parsed[0].isoformat()
        await u.message.reply_text(f'Дата {parsed[0].strftime("%d.%m.%Y")}. Теперь напиши сумму и покупку, например: 2,50 кофе. Если валюта другая, укажи её: 3 USD кофе.')
        return
    if 'add_day' in ctx.user_data:
        entry = parse_entry(raw, default_currency() or 'EUR')
        if not entry:
            await u.message.reply_text('Не распознал сумму и описание. Пример: 2,50 кофе или 3 USD кофе. /cancel — отменить.'); return
        day = date.fromisoformat(ctx.user_data.pop('add_day'))
        await save_entry(u, day, *entry)
        return
    if raw.casefold() == 'путешествия':
        ctx.user_data.pop('pending_category',None)
        await show_trips(u.message)
        return
    if raw.casefold() in CATEGORIES:
        ctx.user_data['pending_category'] = raw.casefold()
        await u.message.reply_text(f'Выбрана категория «{raw.casefold()}». Напиши сумму и описание, например: 2,50 кофе. Можно просто 2,50.', reply_markup=ReplyKeyboardRemove())
        return
    chosen = ctx.user_data.get('pending_category')
    entry = parse_entry(raw, default_currency() or 'EUR')
    if not entry and chosen:
        entry = parse_entry(raw + ' ' + chosen, default_currency() or 'EUR')
    if not entry:
        await u.message.reply_text('Не распознал запись. Пример: 2,50 кофе, кофе 2,50 или 3 USD такси. /help')
        return
    ctx.user_data.pop('pending_category', None)
    if chosen == 'доход':
        entry = (abs(entry[0]), entry[1], entry[2])
    await save_entry(u, today(), *entry, selected_category=chosen)

async def save_entry(u, day, cents, currency, description, selected_category=None, income_category=None):
    cat = selected_category or category_for(description, cents)
    if cents > 0 and cat == 'доход': income_category = income_category or income_category_for(description)
    with conn() as c:
        cur = c.execute('INSERT INTO operations(user_id,source,day,amount_cents,currency,description,category,income_category) VALUES(?,?,?,?,?,?,?,?)', (owner(),'cash', day.isoformat(), cents, currency, description, cat,income_category))
    suffix=f' · {income_category}' if cents>0 and cat=='доход' else ''
    await u.message.reply_text(f'Записано #{cur.lastrowid}: {day} · {cents/100:+.2f} {currency} · {cat}{suffix}\n/correct {cur.lastrowid} категория · /delete {cur.lastrowid}')

async def add(u, ctx):
    if not await guard(u): return
    ctx.user_data.pop('add_day', None)
    ctx.user_data.pop('awaiting_add_date', None)
    if not ctx.args:
        ctx.user_data['awaiting_add_date'] = True
        await u.message.reply_text('За какую дату покупка? Напиши «15 сентября», «15 сентября 2025» или «2025-09-15». Потом бот спросит сумму. /cancel — отменить.'); return
    parsed = parse_date_prefix(' '.join(ctx.args))
    if not parsed or parsed[0] > today():
        await u.message.reply_text('Укажи прошедшую дату: /add 15 сентября 2,50 кофе'); return
    day, description = parsed
    if not description:
        ctx.user_data['add_day'] = day.isoformat()
        await u.message.reply_text(f'Дата {day.strftime("%d.%m.%Y")}. Теперь напиши сумму и покупку: 2,50 кофе.'); return
    entry = parse_entry(description, default_currency() or 'EUR')
    if not entry:
        await u.message.reply_text('Пример: /add 15 сентября 2,50 кофе'); return
    await save_entry(u, day, *entry)

async def cancel_cmd(u, ctx):
    if not await guard(u): return
    clear_flow(ctx)
    for key in ('awaiting_currency','awaiting_statement','pending_import'):ctx.user_data.pop(key,None)
    await u.message.reply_text('Ввод отменён.')

REVOLUT_HEADERS = ('Тип', 'Продукт', 'Дата начала', 'Дата выполнения', 'Описание', 'Сумма', 'Комиссия', 'Валюта', 'State', 'Остаток средств')

def parse_revolut_statement(data):
    try: source = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc: raise ValueError('Нужна CSV-выписка Revolut в кодировке UTF-8.') from exc
    try:
        reader = csv.DictReader(io.StringIO(source, newline=''))
        if not reader.fieldnames or not all(key in reader.fieldnames for key in REVOLUT_HEADERS):
            raise ValueError('Это не CSV-выписка Revolut с ожидаемыми столбцами.')
        rows = list(reader)
    except csv.Error as exc: raise ValueError('Не получилось прочитать CSV-файл.') from exc
    if not rows: raise ValueError('Выписка пустая.')
    if len(rows) > 20000: raise ValueError('Слишком много строк. Выгрузи более короткий период.')
    result, skipped, occurrences = [], 0, {}
    for row in rows:
        if None in row or any(row.get(key) is None for key in REVOLUT_HEADERS):
            raise ValueError('Выписка содержит повреждённую строку.')
        state = row['State'].strip().casefold()
        if state not in ('выполнено', 'completed'):
            skipped += 1
            continue
        try:
            day = date.fromisoformat(row['Дата выполнения'].strip()[:10]).isoformat()
            amount = Decimal(row['Сумма'].strip().replace(',', '.'))
            fee = Decimal(row['Комиссия'].strip().replace(',', '.'))
            if not amount.is_finite() or not fee.is_finite(): raise ValueError()
            cents = int(amount * 100)
            fee_cents = int(abs(fee) * 100)
            if amount * 100 != cents or abs(fee) * 100 != fee_cents: raise ValueError()
            currency = row['Валюта'].strip().upper()
            if not re.fullmatch('[A-Z]{3}', currency): raise ValueError()
        except (ValueError, InvalidOperation) as exc:
            raise ValueError('В выполненной операции некорректные дата, сумма или валюта.') from exc
        if not cents and not fee_cents:
            skipped += 1
            continue
        description = row['Описание'].strip() or row['Тип'].strip() or 'Операция Revolut'
        kind = row['Тип'].strip().casefold()
        category = 'переводы' if kind in ('обмен валюты', 'переводы', 'currency exchange', 'transfer') else None
        # Balance and original fields distinguish equal-looking purchases; repeated imports retain the same ID.
        identity_fields = [row[key].strip() for key in REVOLUT_HEADERS]
        identity = json.dumps(identity_fields, ensure_ascii=False, separators=(',', ':'))
        occurrences[identity] = occurrences.get(identity, 0) + 1
        external = hashlib.sha256(f'{identity}:{occurrences[identity]}'.encode()).hexdigest()
        account_id = f"revolut_csv:{row['Продукт'].strip()}:{currency}"
        if cents:
            result.append((external, account_id, day, cents, currency, description, category))
        if fee_cents:
            result.append((external + ':fee', account_id, day, -fee_cents, currency, 'Комиссия · ' + description, 'другое'))
    return result, skipped

async def import_cmd(u, ctx):
    if not await guard(u): return
    ctx.user_data['awaiting_statement'] = True
    ctx.user_data.pop('pending_import', None)
    await u.message.reply_text('Пришли выписку файлом CSV, PDF, Excel (XLSX), OFX/QFX, QIF, CAMT XML или MT940 (до 2 МБ). Сначала покажу результат для проверки. PDF должен содержать текст; если его формат не распознан, бот попросит CSV или XLSX. Сканы и фотографии пока не читаются. /cancel — отменить.')

async def import_document(u, ctx):
    if not await guard(u): return
    if not ctx.user_data.get('awaiting_statement'):
        await u.message.reply_text('Чтобы импортировать выписку, сначала нажми /import.'); return
    doc = u.message.document
    filename = (doc.file_name or '').lower()
    if not filename.endswith(('.csv', '.xlsx', '.pdf', '.ofx', '.qfx', '.qif', '.xml', '.sta', '.mt940', '.940', '.txt')) or (doc.file_size and doc.file_size > 2_000_000):
        await u.message.reply_text('Нужен CSV, PDF, Excel (XLSX), OFX/QFX, QIF, CAMT XML или MT940 до 2 МБ. Фото и сканы не поддерживаются; незнакомый PDF бот отклонит.'); return
    try:
        file = await doc.get_file()
        data = bytes(await file.download_as_bytearray())
        if len(data) > 2_000_000: raise ValueError('Файл больше 2 МБ.')
        if filename.endswith('.csv'):
            try:
                entries, skipped = parse_revolut_statement(data)
                source, currency_note = 'revolut_csv', 'валюта из выписки Revolut'
            except ValueError as exc:
                if 'ожидаемыми столбцами' not in str(exc): raise
                entries, skipped, currency_note = parse_statement(data, filename, default_currency() or 'EUR')
                source = 'statement'
        elif filename.endswith('.xlsx'):
            entries, skipped, currency_note = parse_statement(data, filename, default_currency() or 'EUR')
            source = 'statement'
        else:
            entries, skipped, currency_note = parse_extra_statement(data, filename, default_currency() or 'EUR')
            source = {'pdf':'nlb_pdf','ofx':'ofx','qfx':'ofx','qif':'qif','xml':'camt','sta':'mt940','mt940':'mt940','940':'mt940','txt':'mt940'}[filename.rsplit('.',1)[-1]]
    except ValueError as exc:
        await u.message.reply_text(str(exc)); return
    except Exception:
        logging.exception('Failed to read bank statement')
        await u.message.reply_text('Не получилось прочитать файл. Попробуй экспортировать выписку заново.'); return
    ctx.user_data['pending_import'] = (source, entries, skipped)
    totals = {}
    for entry in entries:
        if entry[3] < 0 and entry[6] != 'переводы':
            totals[entry[4]] = totals.get(entry[4], 0) - entry[3]
    summary = '\n'.join(f'{value / 100:.2f} {currency}' for currency, value in sorted(totals.items())) or 'нет расходов'
    preview = '\n'.join(f'{day}: {cents / 100:+.2f} {currency} · {description[:50]}' for _, _, day, cents, currency, description, _ in entries[:3])
    await u.message.reply_text(
        f'Проверка выписки: распознано {len(entries)} операций; пропущено {skipped}.\nРасходы: {summary}\n{currency_note}.\n\nПримеры:\n{preview}\n\nПроверь даты, знаки и валюты. Нажми «Записать», только если всё верно. Одинаковые операции при повторном импорте будут пропущены.',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Записать', callback_data='import:confirm'), InlineKeyboardButton('Отмена', callback_data='import:cancel')]]))

async def import_button(u, ctx):
    q = u.callback_query
    if not await guard(u): return
    await q.answer()
    pending = ctx.user_data.pop('pending_import', None)
    ctx.user_data.pop('awaiting_statement', None)
    if q.data == 'import:cancel':
        await q.edit_message_text('Импорт отменён.'); return
    if not pending:
        await q.edit_message_text('Предпросмотр устарел. Начни заново: /import'); return
    source, entries, skipped = pending
    added = duplicates = 0
    with conn() as c:
        for external, account_id, day, cents, currency, description, category in entries:
            actual_category = category or category_for(description, cents)
            cursor = c.execute('INSERT OR IGNORE INTO operations(user_id,source,external_id,account_id,day,amount_cents,currency,description,category,income_category) VALUES(?,?,?,?,?,?,?,?,?,?)', (owner(),source,external,account_id,day,cents,currency,description,actual_category,income_category_for(description) if cents>0 and actual_category=='доход' else None))
            added += cursor.rowcount
            duplicates += 1 - cursor.rowcount
    await q.edit_message_text(f'Импорт готов: добавлено {added}, уже были {duplicates}, пропущено {skipped}. Посмотри /history и /report. Если какая-то строка определилась неверно, исправь категорию через /correct.')

async def categories(u, ctx):
    if not await guard(u): return
    options = [cat for cat in CATEGORIES if cat != 'доход']
    keyboard = [options[i:i+3] for i in range(0,len(options),3)]
    await u.message.reply_text('Выбери категорию и напиши расход. «Путешествия» откроет отдельные таблицы поездок. Для дохода используй /income.', reply_markup=ReplyKeyboardMarkup(keyboard,resize_keyboard=True,one_time_keyboard=True))

async def budget(u, ctx):
    if not await guard(u): return
    if len(ctx.args) not in (2,3) or ctx.args[0].lower() not in CATEGORIES or ctx.args[0].lower() in ('доход','переводы'):
        await u.message.reply_text('Пример: /budget продукты 300 EUR. Для удаления: /budget продукты 0 EUR'); return
    try:
        amount = Decimal(ctx.args[1].replace(',','.'))
        if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2: raise ValueError()
        cents = int(amount * 100)
    except (InvalidOperation,ValueError):
        await u.message.reply_text('Сумма должна быть неотрицательной, не более двух знаков после запятой.'); return
    marker = ctx.args[2] if len(ctx.args)==3 else 'EUR'
    currency = CURRENCY_SYMBOLS.get(marker,marker.upper())
    if not re.fullmatch('[A-Z]{3}',currency):
        await u.message.reply_text('Валюта: EUR, USD, RUB и т. д.'); return
    cat = ctx.args[0].lower()
    with conn() as c:
        if cents: c.execute('INSERT OR REPLACE INTO user_budgets VALUES (?,?,?,?)',(owner(),cat,currency,cents))
        else: c.execute('DELETE FROM user_budgets WHERE user_id=? AND category=? AND currency=?',(owner(),cat,currency))
    await u.message.reply_text(f'Лимит {cat}: {cents/100:.2f} {currency}' if cents else f'Лимит {cat} в {currency} удалён.')

async def budgets_cmd(u, ctx):
    if not await guard(u): return
    with conn() as c: rows=c.execute('SELECT * FROM user_budgets WHERE user_id=? ORDER BY currency,category', (owner(),)).fetchall()
    await u.message.reply_text('\n'.join(f"{r['category']}: {r['amount_cents']/100:.2f} {r['currency']} / месяц" for r in rows) or 'Лимитов пока нет. Пример: /budget продукты 300 EUR')

async def report(u, ctx):
    if not await guard(u): return
    arg = ctx.args[0] if ctx.args else None
    try:
        output = report_text(arg)
        start_day, end_day, _ = period(arg)
    except ValueError:
        await u.message.reply_text('Формат: /report 2026-09, /report week или /report today'); return
    ctx.user_data['report_period'] = (start_day.isoformat(),end_day.isoformat())
    await u.message.reply_text(output, reply_markup=report_keyboard(arg))

def report_text(arg=None):
    start_day, end_day, title = period(arg)
    with conn() as c:
        rows = c.execute('SELECT category,currency,amount_cents FROM operations WHERE user_id=? AND day BETWEEN ? AND ?', (owner(),start_day.isoformat(),end_day.isoformat())).fetchall()
        budgets = c.execute('SELECT category,currency,amount_cents FROM user_budgets WHERE user_id=?',(owner(),)).fetchall()
        month_start = end_day.replace(day=1).isoformat()
        monthly_rows = c.execute("SELECT category,currency,SUM(-amount_cents) AS spent FROM operations WHERE user_id=? AND day BETWEEN ? AND ? AND amount_cents<0 AND category!='переводы' GROUP BY category,currency", (owner(),month_start,end_day.isoformat())).fetchall()
    limit_lines = []
    if budgets:
        monthly_spent = {(r['category'],r['currency']):r['spent'] for r in monthly_rows}
        limit_lines.append(f'\nЛимиты за {end_day:%Y-%m} (с начала месяца по {end_day}):')
        for b in budgets:
            spent = monthly_spent.get((b['category'],b['currency']),0)
            amount = b['amount_cents']
            if spent > amount:
                limit_lines.append(f"⚠️ {b['category']}: {spent/100:.2f}/{amount/100:.2f} {b['currency']} · превышен на {(spent-amount)/100:.2f}")
            else:
                limit_lines.append(f"{b['category']}: {spent/100:.2f}/{amount/100:.2f} {b['currency']}")
    if not rows: return '\n'.join([f'{title}: операций пока нет. Выбери другой период кнопками ниже.']+limit_lines)
    currencies = sorted(set(r['currency'] for r in rows if r['amount_cents'] < 0 and r['category'] != 'переводы'))
    if not currencies: return '\n'.join([f'{title}: расходов пока нет. Выбери другой период кнопками ниже.']+limit_lines)
    lines = [f'{title} · {start_day}—{end_day}']
    for curr in currencies:
        group = [r for r in rows if r['currency'] == curr]
        expense = [r for r in group if r['amount_cents'] < 0 and r['category'] != 'переводы']
        totals = {}
        for r in expense: totals[r['category']] = totals.get(r['category'],0) - r['amount_cents']
        spent = sum(totals.values())
        lines.append(f'\n{curr}: расходы {spent/100:.2f}')
        lines.extend(f'  {cat}: {amount/100:.2f}' for cat, amount in sorted(totals.items(), key=lambda item: -item[1]))
        transfers = sum(r['category'] == 'переводы' for r in group)
        if transfers: lines.append(f'  Переводы: {transfers} операций (не в итогах)')
    return '\n'.join(lines+limit_lines)

async def report_button(u, ctx):
    q = u.callback_query
    await q.answer()
    if not allowed(u): return
    action, _, value = (q.data or '').partition(':')
    if action == 'reportperiod':
        try:
            output = report_text(value)
            start_day, end_day, _ = period(value)
        except ValueError:
            await q.message.reply_text('Период неверный. Нажми /report.'); return
        ctx.user_data['report_period'] = (start_day.isoformat(),end_day.isoformat())
        await q.message.reply_text(output, reply_markup=report_keyboard(value))
    elif action == 'reportmonths' and value.isdigit():
        offset = min(int(value), 100000)
        with conn() as c:
            months = c.execute('SELECT substr(day,1,7) AS month,COUNT(*) AS count FROM operations WHERE user_id=? GROUP BY substr(day,1,7) ORDER BY month DESC LIMIT 13 OFFSET ?', (owner(),offset)).fetchall()
        if not months:
            await q.message.reply_text('Других месяцев с операциями пока нет.'); return
        buttons = [[InlineKeyboardButton(f"{r['month']} · {r['count']} операций", callback_data=f"reportperiod:{r['month']}")] for r in months[:12]]
        nav = []
        if offset: nav.append(InlineKeyboardButton('⬅️ Новее', callback_data=f'reportmonths:{max(0,offset-12)}'))
        if len(months)>12: nav.append(InlineKeyboardButton('Старее ➡️', callback_data=f'reportmonths:{offset+12}'))
        if nav: buttons.append(nav)
        buttons.append([InlineKeyboardButton('⬅️ К отчёту', callback_data=f'reportperiod:{today():%Y-%m}')])
        await q.edit_message_text('Выбери месяц для отчёта:', reply_markup=InlineKeyboardMarkup(buttons))

def report_expense_page(start_day, end_day, offset=0):
    criteria = "user_id=? AND day BETWEEN ? AND ? AND amount_cents<0 AND category!='переводы'"
    with conn() as c:
        total = c.execute('SELECT COUNT(*) FROM operations WHERE ' + criteria, (owner(),start_day,end_day)).fetchone()[0]
        rows = c.execute('SELECT day,amount_cents,currency,category,description FROM operations WHERE ' + criteria + ' ORDER BY day DESC,id DESC LIMIT 8 OFFSET ?', (owner(),start_day,end_day,offset)).fetchall()
    return rows,total

async def report_action_button(u, ctx):
    q = u.callback_query
    await q.answer()
    if not allowed(u): return
    parts = (q.data or '').split(':')
    if len(parts) not in (3,4): return
    action,start_day,end_day = parts[:3]
    try:
        start, end = date.fromisoformat(start_day),date.fromisoformat(end_day)
        if start>end or (end-start).days>366: raise ValueError()
    except ValueError:
        await q.message.reply_text('Неверный период. Открой /report заново.'); return
    ctx.user_data['report_period'] = (start_day,end_day)
    if action == 'reportexport' and len(parts)==3:
        await send_expense_csv(q.message,start_day,end_day)
    elif action == 'reportdetail' and len(parts)==4 and parts[3].isdigit():
        offset = min(int(parts[3]),100000)
        rows,total = report_expense_page(start_day,end_day,offset)
        if not rows:
            await q.message.reply_text('Расходов за выбранный период нет.'); return
        lines = [f'Расходы {start_day}—{end_day} · {offset+1}–{offset+len(rows)} из {total}']
        lines.extend(f"{r['day']} · {-r['amount_cents']/100:.2f} {r['currency']} · {r['category']}\n{r['description'][:100]}" for r in rows)
        nav = []
        if offset: nav.append(InlineKeyboardButton('⬅️ Новее',callback_data=f'reportdetail:{start_day}:{end_day}:{max(0,offset-8)}'))
        if offset+8<total: nav.append(InlineKeyboardButton('Старее ➡️',callback_data=f'reportdetail:{start_day}:{end_day}:{offset+8}'))
        buttons = [nav] if nav else []
        buttons.append([InlineKeyboardButton('📥 Скачать CSV',callback_data=f'reportexport:{start_day}:{end_day}')])
        await q.message.reply_text('\n\n'.join(lines),reply_markup=InlineKeyboardMarkup(buttons))

async def operations(u, ctx):
    if not await guard(u): return
    with conn() as c: rows = c.execute('SELECT * FROM operations WHERE user_id=? ORDER BY day DESC,id DESC LIMIT 10',(owner(),)).fetchall()
    await u.message.reply_text('\n'.join(f"#{r['id']} {r['day']} {r['amount_cents']/100:+.2f} {r['currency']} {r['category']} · {r['description'][:45]} ({r['source']})" for r in rows) or 'Операций пока нет.')

HISTORY_PAGE_SIZE = 8

def history_page(offset=0, month='all'):
    where = ' WHERE user_id=?' if month == 'all' else ' WHERE user_id=? AND day LIKE ?'
    params = (owner(),) if month == 'all' else (owner(),month + '%',)
    with conn() as c:
        total = c.execute('SELECT COUNT(*) FROM operations' + where, params).fetchone()[0]
        rows = c.execute('SELECT * FROM operations' + where + ' ORDER BY day DESC,id DESC LIMIT ? OFFSET ?', params + (HISTORY_PAGE_SIZE,offset)).fetchall()
    return rows, total

def history_markup(rows, total, offset, month):
    buttons = [[InlineKeyboardButton(f"{r['day']} · {operation_label(r)}",callback_data=f"histitem:{r['id']}:{offset}:{month}")] for r in rows]
    paging = []
    if offset > 0: paging.append(InlineKeyboardButton('⬅️ Новее',callback_data=f'histpage:{max(0,offset-HISTORY_PAGE_SIZE)}:{month}'))
    if offset + HISTORY_PAGE_SIZE < total: paging.append(InlineKeyboardButton('Старее ➡️',callback_data=f'histpage:{offset+HISTORY_PAGE_SIZE}:{month}'))
    if paging: buttons.append(paging)
    buttons.append([InlineKeyboardButton('📅 Выбрать месяц',callback_data='histmonths:0')])
    if month != 'all': buttons.append([InlineKeyboardButton('Все операции',callback_data='histpage:0:all')])
    return InlineKeyboardMarkup(buttons)

async def history(u, ctx):
    if not await guard(u): return
    month = ctx.args[0] if ctx.args else 'all'
    if month != 'all' and not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',month):
        await u.message.reply_text('Используй /history или /history 2026-09.'); return
    rows,total=history_page(0,month)
    if not rows:
        await u.message.reply_text('Операций за этот период нет.'); return
    await u.message.reply_text(f'История · {month if month != "all" else "все месяцы"} · {total} операций\nНажми на покупку, чтобы посмотреть детали.',reply_markup=history_markup(rows,total,0,month))

async def history_button(u, ctx):
    q=u.callback_query
    await q.answer()
    if not allowed(u):
        await q.edit_message_text('Доступ закрыт.'); return
    parts=(q.data or '').split(':')
    action=parts[0]
    if action == 'histmonths' and len(parts)==2 and parts[1].isdigit():
        offset=min(int(parts[1]),100000)
        with conn() as c:
            months=c.execute('SELECT substr(day,1,7) AS month,COUNT(*) AS count FROM operations WHERE user_id=? GROUP BY substr(day,1,7) ORDER BY month DESC LIMIT 13 OFFSET ?',(owner(),offset)).fetchall()
        if not months:
            await q.edit_message_text('Более старых месяцев нет.'); return
        shown=months[:12]
        buttons=[[InlineKeyboardButton(f"{r['month']} · {r['count']}",callback_data=f"histpage:0:{r['month']}")] for r in shown]
        nav=[]
        if offset: nav.append(InlineKeyboardButton('⬅️ Новее',callback_data=f'histmonths:{max(0,offset-12)}'))
        if len(months)>12: nav.append(InlineKeyboardButton('Старее ➡️',callback_data=f'histmonths:{offset+12}'))
        if nav:buttons.append(nav)
        buttons.append([InlineKeyboardButton('Все операции',callback_data='histpage:0:all')])
        await q.edit_message_text('Выбери месяц:',reply_markup=InlineKeyboardMarkup(buttons))
    elif action == 'histpage' and len(parts)==3 and parts[1].isdigit():
        offset=min(int(parts[1]),100000)
        month=parts[2]
        if month != 'all' and not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',month):
            await q.edit_message_text('Неверный месяц.'); return
        rows,total=history_page(offset,month)
        if not rows:
            await q.edit_message_text('На этой странице операций нет.'); return
        await q.edit_message_text(f'История · {month if month != "all" else "все месяцы"} · {offset+1}–{offset+len(rows)} из {total}',reply_markup=history_markup(rows,total,offset,month))
    elif action == 'histitem' and len(parts)==4 and parts[1].isdigit() and parts[2].isdigit():
        operation_id,offset,month=int(parts[1]),min(int(parts[2]),100000),parts[3]
        if month != 'all' and not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',month):
            await q.edit_message_text('Неверный месяц.'); return
        with conn() as c: row=c.execute('SELECT * FROM operations WHERE user_id=? AND id=?',(owner(),operation_id)).fetchone()
        if not row:
            await q.edit_message_text('Операция уже удалена.'); return
        buttons=[[InlineKeyboardButton('✏️ Категория',callback_data=f'fixpick:{operation_id}')]]
        if row['amount_cents']>0 and row['category']=='доход':buttons.append([InlineKeyboardButton('💰 Категория дохода',callback_data=f'income:edit:{operation_id}')])
        if row['source']=='cash': buttons[0].append(InlineKeyboardButton('🗑 Удалить',callback_data=f'delpick:{operation_id}'))
        buttons.append([InlineKeyboardButton('⬅️ К списку',callback_data=f'histpage:{offset}:{month}')])
        extra=(f"\nИсточник дохода: {row['income_category'] or 'другое'}" if row['amount_cents']>0 and row['category']=='доход' else '')
        if row['trip_id']:
            trip=trip_row(row['trip_id'])
            if trip:extra+=f"\nПоездка: {trip['name']} · {row['trip_category'] or row['category']}"
        await q.edit_message_text(f"Операция #{row['id']}\nДата: {row['day']}\nСумма: {row['amount_cents']/100:+.2f} {row['currency']}\nКатегория: {row['category']}{extra}\nОписание: {row['description']}\nИсточник: {'наличные' if row['source']=='cash' else 'банк'}",reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await q.edit_message_text('Кнопка истории недействительна.')

async def correct(u, ctx):
    if not await guard(u): return
    if not ctx.args:
        await choose_operation(u, 'fixpick')
        return
    if len(ctx.args)<2 or ctx.args[1].lower() not in CATEGORIES:
        await u.message.reply_text('Используй /correct ID категория. /categories'); return
    with conn() as c:
        row = c.execute('SELECT description,amount_cents FROM operations WHERE user_id=? AND id=?', (owner(),ctx.args[0])).fetchone()
        if not row: await u.message.reply_text('Операция не найдена.'); return
        cat = ctx.args[1].lower()
        if row['amount_cents'] > 0 and cat not in ('доход','переводы','другое'):
            await u.message.reply_text('Для поступления выбери доход, переводы или другое.'); return
        c.execute('UPDATE operations SET category=? WHERE user_id=? AND id=?', (cat,owner(),ctx.args[0]))
        needle = row['description'].lower().strip()
        if len(needle)>=3: c.execute('INSERT OR REPLACE INTO user_overrides VALUES (?,?,?)', (owner(),needle,cat))
    await u.message.reply_text(f'Категория изменена на «{cat}». Правило для описания сохранено; прошлые операции не менялись.')

async def rule(u, ctx):
    if not await guard(u): return
    if len(ctx.args) < 2 or ctx.args[-1].lower() not in CATEGORIES:
        await u.message.reply_text('Пример: /rule ikea покупки (последнее слово — категория)'); return
    needle = ' '.join(ctx.args[:-1]).lower().strip()
    if len(needle) < 3:
        await u.message.reply_text('Слово для правила должно быть не короче 3 символов.'); return
    with conn() as c: c.execute('INSERT OR REPLACE INTO user_overrides VALUES(?,?,?)',(owner(),needle,ctx.args[-1].lower()))
    await u.message.reply_text(f'Запомнил: «{needle}» → {ctx.args[-1].lower()}. Действует на новые операции.')

async def rules(u, ctx):
    if not await guard(u): return
    with conn() as c: rows = c.execute('SELECT needle,category FROM user_overrides WHERE user_id=? ORDER BY needle LIMIT 50',(owner(),)).fetchall()
    await u.message.reply_text('\n'.join(f"{r['needle']} → {r['category']}" for r in rows) or 'Своих правил пока нет.')

async def export_csv(u, ctx):
    if not await guard(u): return
    ctx.user_data.pop('awaiting_export_start',None)
    ctx.user_data.pop('export_start',None)
    if not ctx.args:
        await u.message.reply_text('За какой период выгрузить все операции? Выбери кнопкой или нажми «Свой период», чтобы указать две даты.', reply_markup=export_keyboard(ctx.user_data.get('report_period')))
        return
    try:
        if len(ctx.args)==2:
            start_day, end_day = (parse_user_date(x) for x in ctx.args)
            if not start_day or not end_day or start_day>end_day or end_day>today() or (end_day-start_day).days>3660: raise ValueError()
        elif len(ctx.args)==1:
            start_day, end_day, _ = period(ctx.args[0])
        else:
            raise ValueError()
    except ValueError:
        await u.message.reply_text('Пример: /export 2026-09 или /export 01.09.2026 03.10.2026. Без дат /export показывает кнопки.'); return
    await send_period_csv(u.message,start_day.isoformat(),end_day.isoformat(),expenses_only=False)

async def send_expense_csv(message, start_day, end_day):
    await send_period_csv(message,start_day,end_day,expenses_only=True)

async def send_period_csv(message,start_day,end_day,expenses_only=False):
    condition = " AND amount_cents<0 AND category!='переводы'" if expenses_only else ''
    with conn() as c:
        rows = c.execute('SELECT id,day,amount_cents,currency,category,description,source,income_category,trip_id,trip_category FROM operations WHERE user_id=? AND day BETWEEN ? AND ?' + condition + ' ORDER BY day,id',(owner(),start_day,end_day)).fetchall()
    if not rows:
        await message.reply_text('Операций за выбранный период нет.' if not expenses_only else 'Расходов за выбранный период нет.'); return
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(('id','date','amount','currency','category','description','source','income_category','trip_id','trip_category'))
    for r in rows:
        writer.writerow((r['id'],r['day'],f"{Decimal(r['amount_cents'])/100:.2f}",r['currency'],r['category'],r['description'],r['source'],r['income_category'] or '',r['trip_id'] or '',r['trip_category'] or ''))
    data = io.BytesIO(output.getvalue().encode('utf-8-sig'))
    filename = ('expenses' if expenses_only else 'operations') + f'_{start_day}_{end_day}.csv'
    label = 'Расходы' if expenses_only else 'Все операции'
    await message.reply_document(InputFile(data, filename=filename),caption=f'{label} за {start_day}—{end_day}. Не пересылай файл посторонним.')

async def export_button(u, ctx):
    q = u.callback_query
    await q.answer()
    if not allowed(u): return
    parts = (q.data or '').split(':')
    action = parts[0]
    if action == 'exportcustom' and parts == ['exportcustom','start']:
        ctx.user_data['awaiting_export_start'] = True
        ctx.user_data.pop('export_start',None)
        await q.message.reply_text('Напиши начальную дату: 1 сентября 2026, 01.09.2026 или 2026-09-01. /cancel — отменить.')
    elif action == 'exportperiod' and len(parts)==2:
        try: start_day,end_day,_ = period(parts[1])
        except ValueError:
            await q.message.reply_text('Период неверный. Нажми /export.'); return
        await send_period_csv(q.message,start_day.isoformat(),end_day.isoformat())
    elif action == 'exportdates' and len(parts)==3:
        try:
            start_day,end_day = date.fromisoformat(parts[1]),date.fromisoformat(parts[2])
            if start_day>end_day or (end_day-start_day).days>3660: raise ValueError()
        except ValueError:
            await q.message.reply_text('Период неверный. Нажми /export.'); return
        await send_period_csv(q.message,parts[1],parts[2])
    elif action == 'exportmonths' and len(parts)==2 and parts[1].isdigit():
        offset = min(int(parts[1]),100000)
        with conn() as c:
            months = c.execute('SELECT substr(day,1,7) AS month,COUNT(*) AS count FROM operations WHERE user_id=? GROUP BY substr(day,1,7) ORDER BY month DESC LIMIT 13 OFFSET ?', (owner(),offset)).fetchall()
        if not months:
            await q.message.reply_text('Других месяцев с операциями пока нет.'); return
        buttons = [[InlineKeyboardButton(f"{r['month']} · {r['count']} операций",callback_data=f"exportperiod:{r['month']}")] for r in months[:12]]
        nav = []
        if offset: nav.append(InlineKeyboardButton('⬅️ Новее',callback_data=f'exportmonths:{max(0,offset-12)}'))
        if len(months)>12: nav.append(InlineKeyboardButton('Старее ➡️',callback_data=f'exportmonths:{offset+12}'))
        if nav:buttons.append(nav)
        buttons.append([InlineKeyboardButton('✍️ Свой период',callback_data='exportcustom:start')])
        await q.edit_message_text('Выбери месяц для выгрузки:',reply_markup=InlineKeyboardMarkup(buttons))

async def delete(u, ctx):
    if not await guard(u): return
    if not ctx.args:
        await choose_operation(u, 'delpick', manual_only=True)
        return
    if not ctx.args[0].isdigit():
        await u.message.reply_text('Используй /delete ID или выбери запись через /delete.'); return
    await show_delete_confirmation(u.message, int(ctx.args[0]))

def operation_label(row):
    return f"#{row['id']} {row['amount_cents']/100:+.2f} {row['currency']} · {row['description'][:24]}"

def operation_picker(action, offset=0):
    where = " WHERE user_id=? AND source='cash'" if action == 'delpick' else ' WHERE user_id=?'
    with conn() as c:
        total = c.execute('SELECT COUNT(*) FROM operations' + where, (owner(),)).fetchone()[0]
        rows = c.execute('SELECT * FROM operations' + where + ' ORDER BY day DESC,id DESC LIMIT 10 OFFSET ?', (owner(),offset)).fetchall()
    keyboard = [[InlineKeyboardButton(f"{r['day']} · {operation_label(r)}", callback_data=f"{action}:{r['id']}")] for r in rows]
    nav = []
    if offset: nav.append(InlineKeyboardButton('⬅️ Новее', callback_data=f'pickpage:{action}:{max(0,offset-10)}'))
    if offset + 10 < total: nav.append(InlineKeyboardButton('Старее ➡️', callback_data=f'pickpage:{action}:{offset+10}'))
    if nav: keyboard.append(nav)
    keyboard.append([InlineKeyboardButton('📅 История по месяцам', callback_data='home:history')])
    return rows, total, InlineKeyboardMarkup(keyboard)

async def choose_operation(u, action, manual_only=False):
    rows, total, markup = operation_picker(action)
    if not rows:
        await u.effective_message.reply_text('Подходящих операций пока нет.'); return
    await u.effective_message.reply_text(f'Выбери операцию · 1–{len(rows)} из {total}:', reply_markup=markup)

async def picker_button(u, ctx):
    q = u.callback_query
    await q.answer()
    if not allowed(u): return
    parts = (q.data or '').split(':')
    if len(parts) != 3 or parts[1] not in ('fixpick', 'delpick') or not parts[2].isdigit(): return
    offset = min(int(parts[2]), 1000000)
    rows, total, markup = operation_picker(parts[1], offset)
    if not rows:
        await q.edit_message_text('Более старых операций нет.'); return
    await q.edit_message_text(f'Выбери операцию · {offset+1}–{offset+len(rows)} из {total}:', reply_markup=markup)

async def show_delete_confirmation(message, operation_id):
    with conn() as c: row = c.execute("SELECT * FROM operations WHERE user_id=? AND id=? AND source='cash'",(owner(),operation_id)).fetchone()
    if not row:
        await message.reply_text('Ручная операция не найдена.'); return
    buttons = [[InlineKeyboardButton('Да, удалить',callback_data=f'delconfirm:{operation_id}'),InlineKeyboardButton('Отмена',callback_data='cancel')]]
    await message.reply_text(f'Удалить {operation_label(row)}?',reply_markup=InlineKeyboardMarkup(buttons))

async def operation_button(u, ctx):
    query = u.callback_query
    await query.answer()
    if not allowed(u):
        await query.edit_message_text('Доступ закрыт.'); return
    data = query.data or ''
    if data == 'cancel':
        await query.edit_message_text('Отменено.'); return
    parts = data.split(':')
    if len(parts) < 2 or not parts[1].isdigit():
        await query.edit_message_text('Кнопка недействительна.'); return
    action, operation_id = parts[0], int(parts[1])
    if action == 'fixpick':
        with conn() as c: row=c.execute('SELECT * FROM operations WHERE user_id=? AND id=?',(owner(),operation_id)).fetchone()
        if not row:
            await query.edit_message_text('Операция уже удалена.'); return
        options = CATEGORIES if row['amount_cents'] < 0 else ('доход','переводы','другое')
        buttons = [[InlineKeyboardButton(cat,callback_data=f'fixset:{operation_id}:{CATEGORIES.index(cat)}') for cat in options[i:i+2]] for i in range(0,len(options),2)]
        buttons.append([InlineKeyboardButton('Отмена',callback_data='cancel')])
        await query.edit_message_text(f'Выбери категорию для {operation_label(row)}:',reply_markup=InlineKeyboardMarkup(buttons))
    elif action == 'fixset' and len(parts) == 3 and parts[2].isdigit() and int(parts[2]) < len(CATEGORIES):
        cat = CATEGORIES[int(parts[2])]
        with conn() as c:
            row = c.execute('SELECT description,amount_cents FROM operations WHERE user_id=? AND id=?',(owner(),operation_id)).fetchone()
            if row and (row['amount_cents'] < 0 or cat in ('доход','переводы','другое')):
                c.execute('UPDATE operations SET category=? WHERE user_id=? AND id=?',(cat,owner(),operation_id))
                needle=row['description'].lower().strip()
                if len(needle)>=3: c.execute('INSERT OR REPLACE INTO user_overrides VALUES(?,?,?)',(owner(),needle,cat))
        await query.edit_message_text(f'Категория изменена на «{cat}».' if row else 'Операция уже удалена.')
    elif action == 'delpick':
        with conn() as c: row=c.execute("SELECT * FROM operations WHERE user_id=? AND id=? AND source='cash'",(owner(),operation_id)).fetchone()
        if not row:
            await query.edit_message_text('Ручная операция уже удалена.'); return
        buttons = [[InlineKeyboardButton('Да, удалить',callback_data=f'delconfirm:{operation_id}'),InlineKeyboardButton('Отмена',callback_data='cancel')]]
        await query.edit_message_text(f'Удалить {operation_label(row)}?',reply_markup=InlineKeyboardMarkup(buttons))
    elif action == 'delconfirm':
        with conn() as c: n=c.execute("DELETE FROM operations WHERE user_id=? AND id=? AND source='cash'",(owner(),operation_id)).rowcount
        await query.edit_message_text('Операция удалена.' if n else 'Ручная операция уже удалена.')
    else:
        await query.edit_message_text('Кнопка недействительна.')

# Personal finance flows. Each query is scoped to the current Telegram user.
def income_category_for(description):
    value = description.casefold()
    for category, words in (
        ('зарплата', ('зарплат', 'salary', 'plača', 'placa', 'payroll')),
        ('подработка', ('подработ', 'freelance', 'фриланс', 'project payment')),
        ('возврат', ('возврат', 'refund', 'reversal', 'povračilo', 'povracilo')),
        ('подарки', ('подар', 'gift', 'darilo')),
        ('проценты', ('процент', 'interest', 'obresti')),
    ):
        if any(word in value for word in words): return category
    return 'другое'


def positive_cents(value):
    amount = Decimal(str(value).strip().replace(' ', '').replace(',', '.'))
    if not amount.is_finite() or amount <= 0 or amount > 1_000_000_000 or amount * 100 != int(amount * 100):
        raise ValueError('Сумма должна быть больше нуля и иметь не больше двух знаков после запятой.')
    return int(amount * 100)


def income_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(name,callback_data=f'income:cat:{index}') for index,name in enumerate(INCOME_CATEGORIES[i:i+2],i)]
        for i in range(0,len(INCOME_CATEGORIES),2)
    ] + [[InlineKeyboardButton('📊 Отчёт по доходам',callback_data='income:period:this')]])


async def show_income_menu(message,ctx):
    clear_flow(ctx)
    ctx.user_data['awaiting_income']=True
    ctx.user_data.pop('income_category',None)
    await message.reply_text('Доход: напиши «1500 EUR зарплата» или выбери категорию ниже, затем введи сумму с описанием. Валюта по умолчанию применяется, если код не указан. Доход не увеличивает цели накоплений автоматически. /cancel — отменить.',reply_markup=income_keyboard())


async def income_cmd(u,ctx):
    if not await guard(u): return
    if ctx.args:
        await save_income(u,' '.join(ctx.args),None)
    else: await show_income_menu(u.message,ctx)


async def save_income(u,raw,category):
    entry=parse_entry(raw,default_currency() or 'EUR')
    if not entry and category:
        entry=parse_entry(raw+' '+category,default_currency() or 'EUR')
    if not entry:
        await u.effective_message.reply_text('Напиши сумму и описание: 1500 EUR зарплата. /cancel — отменить.')
        return False
    cents,currency,description=entry
    await save_entry(u,today(),abs(cents),currency,description,selected_category='доход',income_category=category or income_category_for(description))
    return True


def income_report_text(arg=None):
    start,end,title=period(None if arg in (None,'this') else arg)
    with conn() as c:
        rows=c.execute("SELECT income_category,currency,amount_cents FROM operations WHERE user_id=? AND day BETWEEN ? AND ? AND amount_cents>0 AND category='доход'",(owner(),start.isoformat(),end.isoformat())).fetchall()
    if not rows:return f'{title}: доходов пока нет.'
    totals={}
    for r in rows:
        key=(r['currency'],r['income_category'] or 'другое')
        totals[key]=totals.get(key,0)+r['amount_cents']
    lines=[f'Доходы · {title} · {start}—{end}']
    for currency in sorted({key[0] for key in totals}):
        lines.append(f"\n{currency}: {sum(value for (code,_),value in totals.items() if code==currency)/100:.2f}")
        lines.extend(f'  {category}: {value/100:.2f}' for (code,category),value in sorted(totals.items(),key=lambda pair:-pair[1]) if code==currency)
    return '\n'.join(lines)


def income_period_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('Сегодня',callback_data='income:period:today'),InlineKeyboardButton('Эта неделя',callback_data='income:period:week')],
        [InlineKeyboardButton('Этот месяц',callback_data='income:period:this'),InlineKeyboardButton('Прошлый месяц',callback_data=f'income:period:{previous_month()}')],
        [InlineKeyboardButton('Выбрать месяц',callback_data='income:months:0')],
    ])


async def income_report(u,ctx):
    if not await guard(u): return
    arg=ctx.args[0] if ctx.args else None
    try: result=income_report_text(arg)
    except ValueError:
        await u.message.reply_text('Пример: /income_report 2026-09, /income_report today или /income_report week');return
    await u.message.reply_text(result,reply_markup=income_period_keyboard())


async def income_button(u,ctx):
    q=u.callback_query
    await q.answer()
    if not allowed(u): return
    parts=q.data.split(':')
    if parts[1]=='edit' and len(parts)==3 and parts[2].isdigit():
        await show_income_category_picker(q.message,int(parts[2]));return
    if parts[1]=='set' and len(parts)==4 and parts[2].isdigit() and parts[3].isdigit():
        await set_income_category(q.message,int(parts[2]),int(parts[3]));return
    if len(parts)!=3:return
    if parts[1]=='cat' and parts[2].isdigit() and int(parts[2])<len(INCOME_CATEGORIES):
        category=INCOME_CATEGORIES[int(parts[2])]
        ctx.user_data['awaiting_income']=True
        ctx.user_data['income_category']=category
        await q.message.reply_text(f'Категория «{category}». Напиши сумму с описанием, например: 1500 EUR зарплата. Можно просто 1500. /cancel — отменить.')
    elif parts[1]=='period':
        try: result=income_report_text(parts[2])
        except ValueError:
            await q.message.reply_text('Выбери период заново: /income_report');return
        await q.message.reply_text(result,reply_markup=income_period_keyboard())
    elif parts[1]=='months' and parts[2].isdigit():
        offset=min(int(parts[2]),100000)
        with conn() as c:
            rows=c.execute("SELECT substr(day,1,7) AS month FROM operations WHERE user_id=? AND amount_cents>0 AND category='доход' GROUP BY substr(day,1,7) ORDER BY month DESC LIMIT 13 OFFSET ?",(owner(),offset)).fetchall()
        if not rows:
            await q.message.reply_text('Других месяцев с доходами нет.');return
        buttons=[[InlineKeyboardButton(row['month'],callback_data=f"income:period:{row['month']}")] for row in rows[:12]]
        nav=[]
        if offset:nav.append(InlineKeyboardButton('Новее',callback_data=f'income:months:{max(0,offset-12)}'))
        if len(rows)>12:nav.append(InlineKeyboardButton('Старее',callback_data=f'income:months:{offset+12}'))
        if nav:buttons.append(nav)
        await q.message.reply_text('Выбери месяц доходов:',reply_markup=InlineKeyboardMarkup(buttons))


async def set_income_category(message,operation_id,index):
    if index >= len(INCOME_CATEGORIES):
        await message.reply_text('Неизвестная категория дохода.');return
    with conn() as c:
        changed=c.execute("UPDATE operations SET income_category=? WHERE user_id=? AND id=? AND amount_cents>0 AND category='доход'",(INCOME_CATEGORIES[index],owner(),operation_id)).rowcount
    await message.reply_text(f'Доход #{operation_id}: категория «{INCOME_CATEGORIES[index]}».' if changed else 'Доход не найден.')


async def show_income_category_picker(message,operation_id):
    with conn() as c:
        row=c.execute("SELECT id FROM operations WHERE user_id=? AND id=? AND amount_cents>0 AND category='доход'",(owner(),operation_id)).fetchone()
    if not row:
        await message.reply_text('Доход не найден.');return
    buttons=[[InlineKeyboardButton(category,callback_data=f'income:set:{operation_id}:{index}') for index,category in enumerate(INCOME_CATEGORIES[i:i+2],i)] for i in range(0,len(INCOME_CATEGORIES),2)]
    await message.reply_text('Выбери категорию дохода:',reply_markup=InlineKeyboardMarkup(buttons))


async def incomecat(u,ctx):
    if not await guard(u):return
    if not ctx.args or not ctx.args[0].isdigit():
        await u.message.reply_text('Открой доход в /history и нажми «Категория дохода» или введи /incomecat ID зарплата.');return
    operation_id=int(ctx.args[0])
    if len(ctx.args)==1:
        await show_income_category_picker(u.message,operation_id);return
    category=ctx.args[1].casefold()
    if category not in INCOME_CATEGORIES:
        await u.message.reply_text('Категории доходов: '+', '.join(INCOME_CATEGORIES));return
    await set_income_category(u.message,operation_id,INCOME_CATEGORIES.index(category))


def goals_keyboard(rows):
    buttons=[[InlineKeyboardButton(f"🎯 {r['name'][:35]}",callback_data=f"goal:view:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton('➕ Новая цель',callback_data='goal:new:0')])
    return InlineKeyboardMarkup(buttons)


async def show_goals(message):
    with conn() as c:
        rows=c.execute('SELECT g.id,g.name,g.currency,g.target_cents,COALESCE(SUM(m.amount_cents),0) AS saved FROM savings_goals g LEFT JOIN goal_movements m ON m.goal_id=g.id AND m.user_id=g.user_id WHERE g.user_id=? GROUP BY g.id ORDER BY g.id DESC LIMIT 30',(owner(),)).fetchall()
    lines=['🎯 Цели накоплений (отложенные суммы вводятся вручную):']
    lines.extend(f"{r['name']}: {r['saved']/100:.2f}/{r['target_cents']/100:.2f} {r['currency']} · {min(100,r['saved']*100//r['target_cents'])}%" for r in rows)
    if not rows:lines.append('Целей пока нет.')
    lines.append('Создать: /goalnew 1000 путешествие (валюта по умолчанию)')
    await message.reply_text('\n'.join(lines),reply_markup=goals_keyboard(rows))


async def goals_cmd(u,ctx):
    if not await guard(u):return
    clear_flow(ctx)
    await show_goals(u.message)


async def create_goal(message,text):
    m=re.fullmatch(r'\s*([€$£₽₾])?\s*([\d.,]+)\s*(?:([A-Za-z]{3}|[€$£₽₾]|евро)\s+|\s+)(.{2,60})\s*',text,re.I)
    if not m:
        await message.reply_text('Напиши сумму и название: 1000 путешествие, 1000 € путешествие или 1000 EUR путешествие.');return False
    try: cents=positive_cents(m.group(2))
    except (ValueError,InvalidOperation):
        await message.reply_text('Укажи сумму больше нуля и максимум две цифры после запятой.');return False
    marker=(m.group(1) or m.group(3) or default_currency() or 'EUR').upper()
    currency='EUR' if marker=='ЕВРО' else CURRENCY_SYMBOLS.get(marker,marker)
    if m.group(1) and m.group(3):
        await message.reply_text('Укажи валюту один раз, например: 1000 € путешествие.');return False
    with conn() as c:
        cur=c.execute('INSERT INTO savings_goals(user_id,name,currency,target_cents,created_day) VALUES(?,?,?,?,?)',(owner(),m.group(4).strip(),currency,cents,today().isoformat()))
    await show_goal(message,cur.lastrowid)
    return True


async def goalnew(u,ctx):
    if not await guard(u):return
    clear_flow(ctx)
    if ctx.args:await create_goal(u.message,' '.join(ctx.args))
    else:
        ctx.user_data['awaiting_goal_new']=True
        await u.message.reply_text('Напиши сумму и название цели: 1000 путешествие или 1000 € путешествие. Без валюты используется валюта по умолчанию. /cancel — отменить.')


def goal_row(goal_id):
    with conn() as c:
        return c.execute('SELECT g.*,COALESCE((SELECT SUM(amount_cents) FROM goal_movements WHERE user_id=g.user_id AND goal_id=g.id),0) AS saved FROM savings_goals g WHERE g.user_id=? AND g.id=?',(owner(),goal_id)).fetchone()


async def show_goal(message,goal_id):
    r=goal_row(goal_id)
    if not r:
        await message.reply_text('Цель не найдена.');return
    remain=max(0,r['target_cents']-r['saved'])
    balance,reserved,available=balance_values(r['currency'])
    await message.reply_text(f"🎯 {r['name']}\nОтложено: {r['saved']/100:.2f}/{r['target_cents']/100:.2f} {r['currency']}\nОсталось: {remain/100:.2f} {r['currency']}\nПрогресс: {min(100,r['saved']*100//r['target_cents'])}%\nДоступно вне целей: {available/100:.2f} {r['currency']}\n\nОтложенная сумма входит в общий баланс; это не расход и не перевод в банке.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('➕ Отложил',callback_data=f'goal:add:{goal_id}'),InlineKeyboardButton('➖ Исправить',callback_data=f'goal:remove:{goal_id}')],[InlineKeyboardButton('Все цели',callback_data='goal:list:0')]]))


async def move_goal(message,goal_id,value,negative=False):
    r=goal_row(goal_id)
    if not r:
        await message.reply_text('Цель не найдена.');return False
    try:
        raw=value.strip().upper()
        if raw.endswith(' '+r['currency']):raw=raw[:-(len(r['currency'])+1)]
        cents=positive_cents(raw)
    except (ValueError,InvalidOperation):
        await message.reply_text(f"Напиши положительную сумму в {r['currency']}: 50 или 50,25. /cancel — отменить.");return False
    if negative and cents>r['saved']:
        await message.reply_text('Нельзя снять больше, чем уже отмечено для цели.');return False
    available=balance_values(r['currency'])[2]
    with conn() as c:
        c.execute('INSERT INTO goal_movements(user_id,goal_id,amount_cents,day) VALUES(?,?,?,?)',(owner(),goal_id,-cents if negative else cents,today().isoformat()))
    await show_goal(message,goal_id)
    if not negative and cents>available:
        await message.reply_text('Доступная сумма стала отрицательной. Если деньги были до начала учёта, укажи их через /opening 500 '+r['currency']+'. Это не создаёт расход.')
    return True


async def goaladd(u,ctx):
    if not await guard(u):return
    if len(ctx.args)>=2 and ctx.args[0].isdigit():
        await move_goal(u.message,int(ctx.args[0]),' '.join(ctx.args[1:]));return
    with conn() as c: rows=c.execute('SELECT id FROM savings_goals WHERE user_id=? ORDER BY id DESC LIMIT 2',(owner(),)).fetchall()
    if len(rows)==1 and ctx.args:
        await move_goal(u.message,rows[0]['id'],' '.join(ctx.args));return
    await u.message.reply_text('Выбери цель через /goals и нажми «Отложил». Если цель одна: /goaladd 50. Если несколько: /goaladd ID 50.')


async def goal_button(u,ctx):
    q=u.callback_query
    await q.answer()
    if not allowed(u):return
    parts=q.data.split(':')
    if len(parts)!=3:return
    action,identifier=parts[1:]
    clear_flow(ctx)
    if action=='new':
        ctx.user_data['awaiting_goal_new']=True
        await q.message.reply_text('Напиши сумму и название цели: 1000 путешествие или 1000 € путешествие. Без валюты используется валюта по умолчанию. /cancel — отменить.')
    elif action=='list':await show_goals(q.message)
    elif identifier.isdigit() and action=='view':await show_goal(q.message,int(identifier))
    elif identifier.isdigit() and action in ('add','remove'):
        if not goal_row(int(identifier)):
            await q.message.reply_text('Цель не найдена.');return
        ctx.user_data['awaiting_goal_amount']=(int(identifier),action=='remove')
        await q.message.reply_text('Напиши сумму, например 50 или 50,25. /cancel — отменить.')


def trip_category_for(description):
    value=description.casefold()
    for category,words in (('жильё',('hotel','hostel','отель','ночёв','airbnb')),
                           ('дорога',('flight','авиабил','поезд','train','билет')),
                           ('еда',('food','кафе','coffee','еда','ресторан')),
                           ('транспорт',('metro','такси','taxi','метро','автобус')),
                           ('развлечения',('museum','музей','экскурс','билет в музей'))):
        if any(word in value for word in words):return category
    return 'другое'


def trips_keyboard(rows):
    buttons=[[InlineKeyboardButton(f"{'✅ ' if r['active'] else ''}{r['name'][:38]}",callback_data=f"trip:view:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton('➕ Новая поездка',callback_data='trip:new:0')])
    return InlineKeyboardMarkup(buttons)


async def show_trips(message):
    with conn() as c: rows=c.execute('SELECT * FROM trips WHERE user_id=? ORDER BY id DESC LIMIT 30',(owner(),)).fetchall()
    text='✈️ Поездки. Открой поездку, чтобы вести её отдельную таблицу расходов. Записи поездки также входят в обычный отчёт один раз.\nСоздать: /tripnew Рим'
    if rows:text+='\nАктивная поездка отмечена ✅. Для быстрого добавления: /tripadd 1,50 автобус.'
    await message.reply_text(text,reply_markup=trips_keyboard(rows))


async def trips_cmd(u,ctx):
    if not await guard(u):return
    clear_flow(ctx)
    await show_trips(u.message)


def trip_row(trip_id=None):
    with conn() as c:
        if trip_id is None:return c.execute('SELECT * FROM trips WHERE user_id=? AND active=1 ORDER BY id DESC LIMIT 1',(owner(),)).fetchone()
        return c.execute('SELECT * FROM trips WHERE user_id=? AND id=?',(owner(),trip_id)).fetchone()


async def create_trip(message,name):
    name=name.strip()
    if not 2<=len(name)<=60:
        await message.reply_text('Название поездки: от 2 до 60 символов. Пример: Рим.');return False
    with conn() as c:
        c.execute('UPDATE trips SET active=0 WHERE user_id=?',(owner(),))
        cur=c.execute('INSERT INTO trips(user_id,name,currency,active,created_day) VALUES(?,?,?,1,?)',(owner(),name,default_currency() or 'EUR',today().isoformat()))
    await show_trip(message,cur.lastrowid)
    await message.reply_text(f'Поездка «{name}» активна. Для расходов нажми «➕ Расход» или напиши /tripadd 1,50 автобус.')
    return True


async def tripnew(u,ctx):
    if not await guard(u):return
    clear_flow(ctx)
    if ctx.args:await create_trip(u.message,' '.join(ctx.args))
    else:
        ctx.user_data['awaiting_trip_name']=True
        await u.message.reply_text('Как назовём поездку? Например: Италия октябрь. /cancel — отменить.')


async def add_trip_expense(message,trip_id,raw,chosen_category=None):
    trip=trip_row(trip_id)
    if not trip:
        await message.reply_text('Поездка не найдена. Открой /trips.');return False
    parsed=parse_date_prefix(raw)
    if parsed and parsed[1]:
        day,raw=parsed
    else:day=today()
    if day>today():
        await message.reply_text('Дата расхода не может быть в будущем.');return False
    entry=parse_entry(raw,trip['currency'])
    if not entry and chosen_category:
        entry=parse_entry(raw+' '+chosen_category,trip['currency'])
    if not entry:
        await message.reply_text('Напиши сумму с описанием: 1,50 EUR автобус. Для прошлой даты: 15 сентября 2 EUR автобус. /cancel — отменить.');return False
    cents,currency,description=entry
    category=chosen_category or trip_category_for(description)
    with conn() as c:
        cur=c.execute("INSERT INTO operations(user_id,source,day,amount_cents,currency,description,category,trip_id,trip_category) VALUES(?,'cash',?,?,?,?,'путешествия',?,?)",(owner(),day.isoformat(),-abs(cents),currency,description,trip_id,category))
    await message.reply_text(f"Поездка «{trip['name']}»: записано #{cur.lastrowid} · {day} · {abs(cents)/100:.2f} {currency} · {category}. Этот расход уменьшил баланс и учтён в общем отчёте один раз. Можно написать следующий расход; /cancel — выйти из ввода. /delete {cur.lastrowid} — удалить.")
    return True


async def tripadd(u,ctx):
    if not await guard(u):return
    clear_flow(ctx)
    trip=trip_row()
    if not trip:
        await u.message.reply_text('Сначала создай поездку через /trips или /tripnew Рим.');return
    if ctx.args:await add_trip_expense(u.message,trip['id'],' '.join(ctx.args))
    else:
        ctx.user_data['awaiting_trip_expense_id']=trip['id']
        await u.message.reply_text('Выбери категорию поездки или сразу напиши расход: 1,50 EUR автобус. Можно указать дату: 15 сентября 2 EUR автобус.',reply_markup=trip_category_keyboard(trip['id']))


def trip_category_keyboard(trip_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton(category,callback_data=f'trip:category:{trip_id}:{index}') for index,category in enumerate(TRIP_CATEGORIES[i:i+2],i)] for i in range(0,len(TRIP_CATEGORIES),2)])


def trip_report_text(trip_id):
    trip=trip_row(trip_id)
    if not trip:return None
    with conn() as c:
        rows=c.execute('SELECT id,day,description,amount_cents,currency,COALESCE(trip_category,category) AS travel_category FROM operations WHERE user_id=? AND trip_id=? AND amount_cents<0 ORDER BY day DESC,id DESC',(owner(),trip_id)).fetchall()
    totals={}
    for row in rows:
        key=(row['currency'],row['travel_category'])
        totals[key]=totals.get(key,0)-row['amount_cents']
    lines=[f"✈️ {trip['name']} · {len(rows)} расходов"]
    for currency in sorted({code for code,_ in totals}):
        spent=sum(amount for (code,_),amount in totals.items() if code==currency)
        lines.append(f'\n{currency}: {spent/100:.2f}')
        lines.extend(f'  {category}: {amount/100:.2f}' for (code,category),amount in sorted(totals.items(),key=lambda pair:-pair[1]) if code==currency)
        if trip['budget_cents'] is not None and currency==trip['currency']:
            difference=trip['budget_cents']-spent
            lines.append(f"  Лимит: {trip['budget_cents']/100:.2f} · {'остаток' if difference>=0 else 'превышение'} {abs(difference)/100:.2f} {currency}")
    if not rows:lines.append('Расходов пока нет.')
    if trip['budget_cents'] is not None and trip['currency'] not in {code for code,_ in totals}:
        lines.append(f"Лимит: {trip['budget_cents']/100:.2f} {trip['currency']}")
    lines.append('\nПоследние операции:')
    lines.extend(f"{r['day']} · {-r['amount_cents']/100:.2f} {r['currency']} · {r['travel_category']} · {r['description'][:45]}" for r in rows[:8])
    if len(rows)>8:lines.append(f'Ещё {len(rows)-8}: скачай CSV поездки.')
    return '\n'.join(lines)


async def show_trip(message,trip_id):
    result=trip_report_text(trip_id)
    if result is None:
        await message.reply_text('Поездка не найдена.');return
    await message.reply_text(result,reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton('➕ Расход',callback_data=f'trip:add:{trip_id}'),InlineKeyboardButton('📥 CSV поездки',callback_data=f'trip:export:{trip_id}')],
        [InlineKeyboardButton('🎯 Лимит поездки',callback_data=f'trip:budget:{trip_id}'),InlineKeyboardButton('✅ Сделать активной',callback_data=f'trip:select:{trip_id}')],
        [InlineKeyboardButton('Все поездки',callback_data='trip:list:0')],
    ]))


async def tripreport(u,ctx):
    if not await guard(u):return
    trip=trip_row(int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else None)
    if not trip:
        await u.message.reply_text('Поездка не найдена. Открой /trips.');return
    await show_trip(u.message,trip['id'])


async def update_trip_budget(message,trip_id,raw):
    trip=trip_row(trip_id)
    if not trip:
        await message.reply_text('Поездка не найдена.');return False
    parts=raw.strip().split()
    if len(parts) not in (1,2) or (len(parts)==2 and parts[1].upper()!=trip['currency']):
        await message.reply_text(f"Напиши сумму в {trip['currency']}, например 500 или 500 {trip['currency']}. Напиши 0, чтобы снять лимит.");return False
    try:
        amount=None if Decimal(parts[0].replace(',','.'))==0 else positive_cents(parts[0])
    except (ValueError,InvalidOperation):
        await message.reply_text('Лимит должен быть положительным числом, максимум два знака после запятой.');return False
    with conn() as c:c.execute('UPDATE trips SET budget_cents=? WHERE user_id=? AND id=?',(amount,owner(),trip_id))
    await show_trip(message,trip_id)
    return True


async def tripbudget(u,ctx):
    if not await guard(u):return
    trip=trip_row()
    if not trip:
        await u.message.reply_text('Сначала выбери поездку в /trips.');return
    if ctx.args:await update_trip_budget(u.message,trip['id'],' '.join(ctx.args))
    else:
        ctx.user_data['awaiting_trip_budget_id']=trip['id']
        await u.message.reply_text(f"Лимит для поездки «{trip['name']}»: напиши сумму в {trip['currency']}, например 500. /cancel — отменить.")


async def send_trip_csv(message,trip_id):
    trip=trip_row(trip_id)
    if not trip:
        await message.reply_text('Поездка не найдена.');return
    with conn() as c:rows=c.execute('SELECT id,day,amount_cents,currency,description,COALESCE(trip_category,category) AS travel_category FROM operations WHERE user_id=? AND trip_id=? AND amount_cents<0 ORDER BY day,id',(owner(),trip_id)).fetchall()
    if not rows:
        await message.reply_text('В поездке пока нет расходов.');return
    output=io.StringIO();writer=csv.writer(output)
    writer.writerow(('id','date','amount','currency','category','description'))
    for r in rows:writer.writerow((r['id'],r['day'],f"{Decimal(r['amount_cents'])/100:.2f}",r['currency'],r['travel_category'],r['description']))
    await message.reply_document(InputFile(io.BytesIO(output.getvalue().encode('utf-8-sig')),filename=f'trip_{trip_id}.csv'),caption=f"Расходы поездки «{trip['name']}»")


async def triplink(u,ctx):
    if not await guard(u):return
    trip=trip_row()
    if not trip or not ctx.args or not ctx.args[0].isdigit():
        await u.message.reply_text('Выбери поездку через /trips, затем /triplink ID. ID операции видно в /history.');return
    category=ctx.args[1].casefold() if len(ctx.args)>1 else None
    if category and category not in TRIP_CATEGORIES:
        await u.message.reply_text('Категория поездки: '+', '.join(TRIP_CATEGORIES));return
    with conn() as c:
        row=c.execute('SELECT description,category FROM operations WHERE user_id=? AND id=? AND amount_cents<0',(owner(),int(ctx.args[0]))).fetchone()
        if not row:
            await u.message.reply_text('Расход не найден.');return
        c.execute('UPDATE operations SET trip_id=?,trip_category=? WHERE user_id=? AND id=?',(trip['id'],category or trip_category_for(row['description']),owner(),int(ctx.args[0])))
    await u.message.reply_text(f"Расход добавлен в поездку «{trip['name']}». В обычном отчёте он остаётся одной операцией. /tripreport")


async def trip_button(u,ctx):
    q=u.callback_query
    await q.answer()
    if not allowed(u):return
    parts=q.data.split(':');action=parts[1]
    clear_flow(ctx)
    if action=='new':
        ctx.user_data['awaiting_trip_name']=True
        await q.message.reply_text('Название поездки: например Рим октябрь. /cancel — отменить.');return
    if action=='list':await show_trips(q.message);return
    if len(parts)<3 or not parts[2].isdigit():return
    trip_id=int(parts[2]);trip=trip_row(trip_id)
    if not trip:
        await q.message.reply_text('Поездка не найдена.');return
    if action=='view':
        with conn() as c:
            c.execute('UPDATE trips SET active=0 WHERE user_id=?',(owner(),))
            c.execute('UPDATE trips SET active=1 WHERE user_id=? AND id=?',(owner(),trip_id))
        ctx.user_data['awaiting_trip_expense_id']=trip_id
        await show_trip(q.message,trip_id)
        await q.message.reply_text(f"Поездка «{trip['name']}» выбрана. Напиши расход: 1,50 автобус. Каждый следующий расход тоже попадёт в неё. /cancel — закончить ввод.",reply_markup=trip_category_keyboard(trip_id))
    elif action=='select':
        with conn() as c:
            c.execute('UPDATE trips SET active=0 WHERE user_id=?',(owner(),))
            c.execute('UPDATE trips SET active=1 WHERE user_id=? AND id=?',(owner(),trip_id))
        ctx.user_data['awaiting_trip_expense_id']=trip_id
        await show_trip(q.message,trip_id)
        await q.message.reply_text('Теперь напиши расход, например 1,50 автобус. /cancel — закончить ввод.')
    elif action=='add':
        ctx.user_data['awaiting_trip_expense_id']=trip_id
        ctx.user_data.pop('trip_category',None)
        await q.message.reply_text('Выбери категорию или напиши расход, например 1,50 EUR автобус.',reply_markup=trip_category_keyboard(trip_id))
    elif action=='category' and len(parts)==4 and parts[3].isdigit() and int(parts[3])<len(TRIP_CATEGORIES):
        ctx.user_data['awaiting_trip_expense_id']=trip_id
        ctx.user_data['trip_category']=TRIP_CATEGORIES[int(parts[3])]
        await q.message.reply_text(f"Категория «{ctx.user_data['trip_category']}». Напиши 1,50 EUR автобус или 1,50. /cancel — отменить.")
    elif action=='budget':
        ctx.user_data['awaiting_trip_budget_id']=trip_id
        await q.message.reply_text(f"Лимит поездки в {trip['currency']}: напиши сумму, например 500. Ноль снимает лимит. /cancel — отменить.")
    elif action=='export':await send_trip_csv(q.message,trip_id)


async def setup_commands(app):
    await app.bot.set_my_commands([
        BotCommand('start', 'Начать'),
        BotCommand('help', 'Все команды'),
        BotCommand('report', 'Отчёт за месяц'),
        BotCommand('history', 'Вся история расходов'),
        BotCommand('operations', 'Последние операции'),
        BotCommand('categories', 'Категории'),
        BotCommand('budget', 'Установить месячный лимит'),
        BotCommand('budgets', 'Месячные лимиты'),
        BotCommand('add', 'Добавить операцию за дату'),
        BotCommand('currency', 'Валюта по умолчанию'),
        BotCommand('cancel', 'Отменить ввод'),
        BotCommand('correct', 'Исправить категорию'),
        BotCommand('delete', 'Удалить ручную операцию'),
        BotCommand('export', 'Скачать операции в CSV'),
        BotCommand('import', 'Импортировать выписку банка'),
        BotCommand('income','Записать доход'),
        BotCommand('income_report','Отчёт по доходам'),
        BotCommand('incomecat','Категория дохода'),
        BotCommand('goals','Цели накоплений'),
        BotCommand('goalnew','Создать цель'),
        BotCommand('goaladd','Отложить сумму'),
        BotCommand('balance','Баланс и доступные деньги'),
        BotCommand('opening','Задать начальную сумму'),
        BotCommand('trips','Поездки'),
        BotCommand('tripnew','Новая поездка'),
        BotCommand('tripadd','Расход в поездке'),
        BotCommand('tripreport','Таблица поездки'),
        BotCommand('tripbudget','Лимит поездки'),
        BotCommand('triplink','Привязать расход к поездке'),
    ])

def main():
    if not os.getenv('TELEGRAM_BOT_TOKEN'): raise SystemExit('Set TELEGRAM_BOT_TOKEN in .env')
    init()
    app=Application.builder().token(os.environ['TELEGRAM_BOT_TOKEN']).post_init(setup_commands).build()
    for name,fn in [('start',start),('help',help_cmd),('add',add),('currency',currency_cmd),('cancel',cancel_cmd),('categories',categories),('budget',budget),('budgets',budgets_cmd),('report',report),('history',history),('operations',operations),('correct',correct),('rule',rule),('rules',rules),('export',export_csv),('delete',delete),('import',import_cmd),('income',income_cmd),('income_report',income_report),('incomecat',incomecat),('goals',goals_cmd),('goalnew',goalnew),('goaladd',goaladd),('balance',balance_cmd),('opening',opening_cmd),('trips',trips_cmd),('tripnew',tripnew),('tripadd',tripadd),('tripreport',tripreport),('tripbudget',tripbudget),('triplink',triplink)]: app.add_handler(CommandHandler(name,fn))
    app.add_handler(CallbackQueryHandler(operation_button,pattern=r'^(fixpick|fixset|delpick|delconfirm|cancel)(:|$)'))
    app.add_handler(CallbackQueryHandler(history_button,pattern=r'^(histpage|histmonths|histitem):'))
    app.add_handler(CallbackQueryHandler(picker_button,pattern=r'^pickpage:'))
    app.add_handler(CallbackQueryHandler(navigation_button,pattern=r'^home:'))
    app.add_handler(CallbackQueryHandler(currency_button,pattern=r'^currency:'))
    app.add_handler(CallbackQueryHandler(report_button,pattern=r'^(reportperiod|reportmonths):'))
    app.add_handler(CallbackQueryHandler(report_action_button,pattern=r'^(reportdetail|reportexport):'))
    app.add_handler(CallbackQueryHandler(export_button,pattern=r'^(exportperiod|exportmonths|exportcustom|exportdates):'))
    app.add_handler(CallbackQueryHandler(import_button,pattern=r'^import:(confirm|cancel)$'))
    app.add_handler(CallbackQueryHandler(income_button,pattern=r'^income:(cat|period|months|edit|set):'))
    app.add_handler(CallbackQueryHandler(goal_button,pattern=r'^goal:(new|list|view|add|remove):'))
    app.add_handler(CallbackQueryHandler(trip_button,pattern=r'^trip:(new|list|view|select|add|category|budget|export):'))
    app.add_handler(MessageHandler(filters.Document.ALL,import_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,cash))
    app.run_polling()

if __name__=='__main__': main()
