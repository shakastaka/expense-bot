"""Normalize currency names and parse money without changing account preferences."""
import re
from decimal import Decimal, InvalidOperation

CODES = set('AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VED VES VND VUV WST XAF XCD XCG XOF XPF YER ZAR ZMW ZWG'.split())
SYMBOLS = {'€':'EUR','$':'USD','£':'GBP','₽':'RUB','₾':'GEL','¥':'JPY','￥':'JPY','₴':'UAH','₸':'KZT','₺':'TRY','₹':'INR','₩':'KRW','₫':'VND','฿':'THB','₪':'ILS','₱':'PHP','₦':'NGN','₼':'AZN','₿':'BTC','US$':'USD','CA$':'CAD','C$':'CAD','AU$':'AUD','A$':'AUD','NZ$':'NZD','HK$':'HKD','SG$':'SGD','S$':'SGD','NT$':'TWD','R$':'BRL','MX$':'MXN','CN¥':'CNY','JP¥':'JPY','₡':'CRC','₲':'PYG','₭':'LAK','₮':'MNT','؋':'AFN','৳':'BDT','៛':'KHR','₵':'GHS','₣':'CHF'}
ALIASES = {'евро':'EUR','euro':'EUR','euros':'EUR','доллар':'USD','доллара':'USD','долларов':'USD','рубль':'RUB','рубля':'RUB','рублей':'RUB','р':'RUB','руб':'RUB','руб.':'RUB','р.':'RUB','rur':'RUB','фунт':'GBP','фунта':'GBP','фунтов':'GBP','лари':'GEL','гривна':'UAH','гривны':'UAH','гривен':'UAH','тенге':'KZT','злотый':'PLN','злотых':'PLN','zł':'PLN','zl':'PLN','юань':'CNY','юаня':'CNY','юаней':'CNY','иена':'JPY','иены':'JPY','йена':'JPY','йены':'JPY','франк':'CHF','франков':'CHF','fr.':'CHF','fr':'CHF'}
CODES.update(('BTC','ETH','USDT','USDC'))


def normalize_currency(value):
    raw = str(value).strip()
    code = SYMBOLS.get(raw) or {k.casefold():v for k,v in SYMBOLS.items()}.get(raw.casefold()) or ALIASES.get(raw.casefold()) or raw.upper()
    return code if code in CODES else None


def currency_pattern():
    # Word boundaries prevent a currency code from eating the beginning of a description.
    symbols = '|'.join(re.escape(s) for s in sorted(SYMBOLS,key=len,reverse=True))
    names = '|'.join(re.escape(s) for s in sorted(CODES | set(ALIASES),key=len,reverse=True))
    return rf'(?:{symbols}|(?:{names})(?![A-Za-zА-Яа-яЁё]))'

MARK = currency_pattern()
NUMBER = r'(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?'
PREFIX = re.compile(rf'^\s*([+-]?)\s*({MARK})?\s*({NUMBER})\s*({MARK})?(?:\s+(.{{1,120}}))?\s*$', re.I)
SUFFIX = re.compile(rf'^\s*(.{{1,120}}?)\s+([+-]?)\s*({MARK})?\s*({NUMBER})\s*({MARK})?\s*$', re.I)
WORDS = {'ноль':0,'один':1,'одна':1,'два':2,'две':2,'три':3,'четыре':4,'пять':5,'шесть':6,'семь':7,'восемь':8,'девять':9,'десять':10,'полтора':Decimal('1.5')}


def parse_money(value, default='EUR', allow_zero=False):
    value = str(value).strip()
    spoken = re.match(r'^(ноль|один|одна|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|полтора)(\s+с\s+половиной)?(?=\s|$)',value,re.I)
    if spoken:
        amount = Decimal(WORDS[spoken.group(1).casefold()]) + (Decimal('0.5') if spoken.group(2) else 0)
        value = str(amount) + value[spoken.end():]
    match = PREFIX.fullmatch(value)
    if match:
        sign, before, number, after, description = match.groups()
    else:
        match = SUFFIX.fullmatch(value)
        if not match: return None
        description, sign, before, number, after = match.groups()
    if before and after: return None
    currency = normalize_currency(before or after or default)
    if not currency: return None
    # A leading isolated currency symbol/code left in the description is a malformed amount.
    if description and re.match(rf'^(?:{MARK})(?:\s|$)', description, re.I): return None
    try:
        amount = Decimal(re.sub(r'[ \u00a0\u202f]', '',number).replace(',','.'))
        if not amount.is_finite() or amount > 1_000_000_000 or amount < 0 or (amount == 0 and not allow_zero): return None
        cents = int(amount * 100)
    except (ValueError, InvalidOperation): return None
    return (cents if sign == '+' else -cents, currency, (description or '').strip())
