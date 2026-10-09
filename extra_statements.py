"""Strict importers for structured bank statements and the known NLB text PDF layout."""
import hashlib
import io
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from bank_import import parse_money


def entries_from_records(records, account, source, skipped=0):
    if not records: raise ValueError('В выписке не найдены операции.')
    occurrences, entries = {}, []
    account_id = source + ':' + hashlib.sha256(account.encode()).hexdigest()[:20]
    for day, cents, currency, description, unique in records:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or not cents or not re.fullmatch('[A-Z]{3}', currency):
            raise ValueError('В выписке обнаружена некорректная дата, сумма или валюта; ничего не записано.')
        identity = json.dumps([day, cents, currency, description, unique], ensure_ascii=False)
        occurrences[identity] = occurrences.get(identity, 0) + 1
        external = hashlib.sha256((identity + ':' + str(occurrences[identity])).encode()).hexdigest()
        entries.append((external, account_id, day, cents, currency, description[:240], None))
    return entries, skipped, 'валюта из выписки'


def parse_nlb_pdf(data):
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) > 50: raise ValueError('PDF длиннее 50 страниц. Выгрузи меньший период.')
            pages = [page.extract_text() or '' for page in pdf.pages]
    except ValueError: raise
    except Exception as exc: raise ValueError('PDF не читается. Для скана используй экспорт CSV/XLSX из банка.') from exc
    joined = '\n'.join(pages)
    if not re.search(r'Obvestilo\s*o\s*prometu', joined, re.I) or 'NLB' not in joined or not re.search(r'STANJE\s+PREDHODNEGA\s+IZPISKA', joined):
        raise ValueError('Этот PDF не похож на поддерживаемую текстовую выписку NLB. Выгрузи CSV/XLSX или пришли образец формата.')
    iban = re.search(r'IBAN:\s*([A-Z]{2}\d{2}(?:\s*\d+){3,})', joined)
    if not iban: raise ValueError('В PDF не найден IBAN счёта; импорт остановлен.')
    account = re.sub(r'\s+', '', iban.group(1))
    if not re.search(r'EUR\s*-\s*EVRO', joined): raise ValueError('Не найдена валюта счёта EUR; PDF не импортирован.')
    records = []
    pattern = re.compile(r'^(\d{2}\.\d{2}\.\d{2})\s+(.+?)\s+([+-]\s*[\d.]+,\d{2})\s+([\d.]+,\d{2})\s*$')
    for page in pages:
        for line in page.splitlines():
            if not re.match(r'^\d{2}\.\d{2}\.\d{2}\b', line): continue
            match = pattern.match(line.strip())
            if not match: raise ValueError('Одна строка PDF NLB не распознана; ничего не записано. Выгрузи CSV/XLSX.')
            day = datetime.strptime(match.group(1), '%d.%m.%y').date().isoformat()
            cents = parse_money(match.group(3))
            description = match.group(2).strip()
            # Running balance disambiguates transactions with the same date and label.
            records.append((day, cents, 'EUR', description, match.group(4)))
    if not records: raise ValueError('В PDF NLB не найдены строки операций.')
    # Validate the PDF subtotals before offering a preview.
    debit = sum(-r[1] for r in records if r[1] < 0)
    credit = sum(r[1] for r in records if r[1] > 0)
    credit_total = re.search(r'SKUPNI\s+PROMET\s+V\s+DOBRO\s+([\d.]+,\d{2})', joined)
    debit_total = re.search(r'SKUPNI\s+PROMET\s+V\s+BREME\s+(-?[\d.]+,\d{2})', joined)
    if credit_total and credit != abs(parse_money(credit_total.group(1))):
        raise ValueError('Сумма поступлений не совпала с итогом PDF; ничего не записано.')
    if debit_total and debit != abs(parse_money(debit_total.group(1))):
        raise ValueError('Сумма расходов не совпала с итогом PDF; ничего не записано.')
    return entries_from_records(records, account, 'nlb_pdf')


def parse_ofx(data, default_currency):
    raw = None
    for encoding in ('utf-8-sig', 'cp1250', 'cp1251', 'latin-1'):
        try:
            candidate = data.decode(encoding)
            if '<STMTTRN>' in candidate.upper(): raw = candidate; break
        except UnicodeError: pass
    if raw is None: raise ValueError('Не найдены операции OFX/QFX.')
    account = (re.search(r'<ACCTID>\s*([^<\r\n]+)',raw,re.I) or [None,'unknown'])[1].strip()
    currency = (re.search(r'<CURDEF>\s*([A-Za-z]{3})',raw,re.I) or [None,default_currency])[1].upper()
    records = []
    for item in re.split(r'<STMTTRN>', raw, flags=re.I)[1:]:
        item = re.split(r'</STMTTRN>', item, maxsplit=1, flags=re.I)[0]
        def field(key):
            match = re.search(r'<' + key + r'>\s*([^<\r\n]+)', item, re.I)
            return match.group(1).strip() if match else ''
        day, amount = field('DTPOSTED')[:8], field('TRNAMT')
        try:
            date = datetime.strptime(day, '%Y%m%d').date().isoformat()
            cents = parse_money(Decimal(amount))
        except (ValueError, InvalidOperation) as exc: raise ValueError('В OFX есть операция с неверной датой или суммой.') from exc
        records.append((date,cents,currency,field('NAME') or field('MEMO') or 'Банковская операция',field('FITID') or field('CHECKNUM')))
    return entries_from_records(records, account, 'ofx')


def parse_qif(data, default_currency):
    raw = data.decode('utf-8-sig')
    if not raw.lstrip().startswith('!Type:'): raise ValueError('Файл не похож на QIF.')
    records=[]
    for block in raw.split('^'):
        fields={line[:1]:line[1:].strip() for line in block.splitlines() if line and line[:1] in 'DTPLN'}
        if not fields.get('D') or not fields.get('T'): continue
        date_text=fields['D'].replace("'",'/')
        parsed=None
        if '/' in date_text:
            parts=date_text.split('/')
            if len(parts)==3 and parts[0].isdigit() and parts[1].isdigit() and int(parts[0])<=12 and int(parts[1])<=12:
                raise ValueError('Дата QIF неоднозначна: ДД/ММ или ММ/ДД. Нужен CSV/XLSX или дата с точками.')
        for fmt in ('%d.%m.%Y','%d/%m/%Y','%m/%d/%Y','%d/%m/%y','%m/%d/%y'):
            try:
                parsed=datetime.strptime(date_text,fmt).date().isoformat();break
            except ValueError:pass
        if parsed is None: raise ValueError('Не могу определить дату QIF; ничего не записано.')
        cents=parse_money(fields['T'])
        records.append((parsed,cents,default_currency,fields.get('P') or fields.get('L') or 'Операция QIF',fields.get('N','')))
    return entries_from_records(records, 'qif', 'qif')


def parse_camt(data):
    try: root=ET.fromstring(data)
    except ET.ParseError as exc: raise ValueError('Неверный XML файл.') from exc
    local=lambda tag: tag.rsplit('}',1)[-1]
    entries=[e for e in root.iter() if local(e.tag)=='Ntry']
    if not entries: raise ValueError('XML не содержит операций CAMT.053.')
    account='camt'; currency=None; records=[]
    for element in root.iter():
        if local(element.tag)=='IBAN' and element.text:
            account=element.text.strip();break
    for entry in entries:
        def descendant(name):
            return next((e for e in entry.iter() if local(e.tag)==name and e.text),None)
        amount=descendant('Amt'); indicator=descendant('CdtDbtInd'); booking=next((e for e in entry.iter() if local(e.tag)=='BookgDt'),None)
        if amount is None or indicator is None or booking is None: raise ValueError('Неполная операция CAMT.053.')
        d=next((e.text for e in booking.iter() if local(e.tag) in ('Dt','DtTm') and e.text),None)
        if not d: raise ValueError('В CAMT.053 отсутствует дата.')
        try: day=datetime.fromisoformat(d.replace('Z','+00:00')).date().isoformat()
        except ValueError as exc: raise ValueError('Неверная дата CAMT.053.') from exc
        code=amount.attrib.get('Ccy','').upper()
        if indicator.text.strip() not in ('DBIT','CRDT') or not re.fullmatch('[A-Z]{3}',code): raise ValueError('Неизвестное направление или валюта CAMT.053.')
        try: cents=abs(parse_money(Decimal(amount.text))) * (-1 if indicator.text.strip()=='DBIT' else 1)
        except (InvalidOperation, ValueError) as exc: raise ValueError('Неверная сумма CAMT.053.') from exc
        desc=descendant('Ustrd') or descendant('AddtlNtryInf')
        ref=descendant('AcctSvcrRef') or descendant('NtryRef')
        records.append((day,cents,code,desc.text.strip() if desc is not None else 'Операция CAMT',ref.text.strip() if ref is not None else ''))
    return entries_from_records(records, account, 'camt')


def parse_mt940(data, default_currency):
    raw = None
    for encoding in ('utf-8-sig', 'cp1250', 'cp1251'):
        try:
            candidate = data.decode(encoding)
            if ':61:' in candidate and ':20:' in candidate: raw = candidate; break
        except UnicodeError: pass
    if raw is None: raise ValueError('Это не банковский файл MT940.')
    match = re.search(r':60[FM]:[CD]\d{6}([A-Z]{3})', raw)
    currency = match.group(1) if match else default_currency
    account_match = re.search(r':25:([^\r\n]+)', raw)
    account = account_match.group(1).strip() if account_match else 'mt940'
    records = []
    blocks = re.split(r'(?=^:61:)', raw, flags=re.M)
    for block in blocks:
        if not block.startswith(':61:'): continue
        line = block.splitlines()[0][4:]
        item = re.match(r'^(\d{6})(?:\d{4})?(RC|RD|C|D)([\d.,]+)[A-Z]([^\r\n]*)', line)
        if not item or item.group(2) in ('RC','RD'):
            raise ValueError('Формат операции MT940 неизвестен; ничего не записано.')
        day = datetime.strptime(item.group(1), '%y%m%d').date().isoformat()
        amount = abs(parse_money(item.group(3)))
        signed = amount if item.group(2)=='C' else -amount
        desc_match = re.search(r'^:86:(.*?)(?=^:\d{2}[A-Z]?:|\Z)', block, flags=re.M | re.S)
        desc = ' '.join(desc_match.group(1).split()) if desc_match else 'Операция MT940'
        records.append((day, signed, currency, desc[:240], item.group(4).strip()))
    return entries_from_records(records, account, 'mt940')

def parse_extra_statement(data, filename, default_currency):
    name=filename.lower()
    if name.endswith('.pdf'): return parse_nlb_pdf(data)
    if name.endswith(('.ofx','.qfx')): return parse_ofx(data,default_currency)
    if name.endswith('.qif'): return parse_qif(data,default_currency)
    if name.endswith('.xml'): return parse_camt(data)
    if name.endswith(('.sta','.mt940','.940','.txt')): return parse_mt940(data,default_currency)
    raise ValueError('Неизвестный формат выписки.')
