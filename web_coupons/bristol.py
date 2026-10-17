import base64
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db import get_conn, get_active_account_id, set_active_account_id


# ---------- аккаунты ----------

def _aid(account_id=None) -> int:
    return int(account_id) if account_id is not None else get_active_account_id()


def _set_meta(account_id, key, value) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO bristol_meta (account_id, key, value) VALUES (?,?,?) '
        'ON CONFLICT(account_id, key) DO UPDATE SET value=excluded.value',
        (int(account_id), key, json.dumps(value, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def _get_meta(account_id, key):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT value FROM bristol_meta WHERE account_id=? AND key=?', (int(account_id), key))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    try:
        return json.loads(row['value'])
    except Exception:
        return row['value']


def _account_touch(account_id, **fields) -> None:
    keys = ('name', 'phone', 'card_id', 'external_id', 'refresh_token', 'token')
    sets, vals = [], []
    for k in keys:
        v = fields.get(k)
        if v is not None:
            sets.append(f'{k}=?')
            vals.append(v)
    if not sets:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT id FROM accounts WHERE id=?', (int(account_id),))
    if not cur.fetchone():
        cur.execute('INSERT INTO accounts (id) VALUES (?)', (int(account_id),))
    vals.append(int(account_id))
    cur.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    conn.close()


def _card_id_from_phone(phone: str) -> str:
    d = re.sub(r'\D', '', phone or '')
    if d.startswith('7') and len(d) == 11:
        d = d[1:]
    return d


def accounts_list() -> list:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, phone, card_id, external_id, "
        "CASE WHEN refresh_token IS NOT NULL AND refresh_token != '' THEN 1 ELSE 0 END AS has_token, "
        "created_at FROM accounts ORDER BY id"
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def account_get(account_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT * FROM accounts WHERE id=?', (int(account_id),))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def account_add(name: str = '', phone: str = '', card_id: str = '',
                external_id: str = '', refresh_token: str = '', token: str = '') -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO accounts (name, phone, card_id, external_id, refresh_token, token) '
        'VALUES (?,?,?,?,?,?)',
        (name or 'Новый аккаунт', phone, card_id, external_id, refresh_token, token),
    )
    aid = cur.lastrowid
    conn.commit()
    conn.close()
    if refresh_token or token:
        apply_account_tokens(aid, refresh_token=refresh_token, token=token, external_id=external_id)
    return aid


def apply_account_tokens(account_id, refresh_token: str = '', token: str = '',
                         external_id: str = '') -> None:
    aid = int(account_id)
    if refresh_token:
        _set_meta(aid, 'refresh_token', refresh_token)
    if token:
        _set_meta(aid, 'token', token)
    ext = (external_id or _jwt_claim_safe(token, 'external_id')
           or _jwt_claim_safe(refresh_token, 'external_id'))
    if ext:
        _set_meta(aid, 'external_id', ext)
    _account_touch(
        aid,
        refresh_token=refresh_token or None,
        token=token or None,
        external_id=ext or None,
    )


def account_activate(account_id) -> None:
    set_active_account_id(int(account_id))


def account_refresh_info(account_id) -> dict:
    aid = int(account_id)
    token = get_token(force=True, account_id=aid)
    user = _api_get('/api/v2/users/?consumer=mobile', token).get('user') or {}
    name = ' '.join(x for x in (user.get('first_name'), user.get('last_name')) if x)
    phone = user.get('phone', '')
    _account_touch(aid, name=name or None, phone=phone or None,
                   card_id=_card_id_from_phone(phone) or None)
    _set_meta(aid, 'name', name)
    _set_meta(aid, 'phone', phone)
    return {'name': name, 'phone': phone}


def _account_id_by_external_id(external_id):
    if not external_id:
        return None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT id FROM accounts WHERE external_id=? LIMIT 1', (external_id,))
    row = cur.fetchone()
    conn.close()
    return row['id'] if row else None


def account_add_from_device() -> int:
    """Добавить/обновить аккаунт из prefs приложения на эмуляторе (ADB)."""
    data = parse_prefs(pull_prefs())
    token = data['token']
    refresh = _field(data['raw'], 'flutter.refreshToken')
    ext = _jwt_claim_safe(token, 'external_id') or _jwt_claim_safe(refresh, 'external_id')
    user = data.get('user') or {}
    name = ' '.join(x for x in (user.get('first_name'), user.get('last_name')) if x)
    phone = user.get('phone', '')
    aid = _account_id_by_external_id(ext)
    if aid:
        apply_account_tokens(aid, refresh_token=refresh, token=token, external_id=ext)
        _account_touch(aid, name=name or None, phone=phone or None,
                       card_id=_card_id_from_phone(phone) or None)
        return aid
    return account_add(name=name, phone=phone, card_id=_card_id_from_phone(phone),
                       external_id=ext, refresh_token=refresh, token=token)


def account_delete(account_id) -> bool:
    aid = int(account_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) AS n FROM accounts')
    if (cur.fetchone()['n'] or 0) <= 1:
        conn.close()
        return False
    for table in ('bristol_meta', 'bristol_coupons', 'coupon_grants'):
        cur.execute(f'DELETE FROM {table} WHERE account_id=?', (aid,))
    cur.execute('DELETE FROM sessions WHERE account_id=?', (aid,))
    cur.execute('DELETE FROM accounts WHERE id=?', (aid,))
    conn.commit()
    conn.close()
    if get_active_account_id() == aid:
        rest = accounts_list()
        if rest:
            set_active_account_id(rest[0]['id'])
    return True


try:
    from PIL import Image, ImageOps
except Exception:
    Image = None

ADB = os.environ.get('BRISTOL_ADB', r'C:\adb\adb.exe')
DEVICE = os.environ.get('BRISTOL_DEVICE', '127.0.0.1:16384')
DEVICE_ID = os.environ.get('BRISTOL_DEVICE_ID', 'V417IR')
CARD_DEVICE_ID = os.environ.get('BRISTOL_CARD_DEVICE_ID', 'SP1A.210812.016')
PREFS_PATH = '/data/data/ru.bristol.bristol_app/shared_prefs/FlutterSharedPreferences.xml'
CACHE_PATH = Path(os.environ.get('BRISTOL_PREFS_CACHE') or (Path(__file__).parent / 'bristol_prefs.xml'))
BASE_DIR = Path(__file__).parent
IMG_DIR = BASE_DIR / 'static' / 'img'
API = os.environ.get('BRISTOL_API', 'https://api.mobile.bristol.ru')
X_API_KEY = os.environ.get('BRISTOL_X_API_KEY', 'e3d7d5678cd42574ce055966496b02f3')
APP_VERSION = '9.0.5+1030'
HEADERS_UA = 'Dart/3.0 (dart:io)'

_CTX = ssl.create_default_context()

_DISCOUNT_RE = re.compile(r'(\d{1,3})\s*%')
_WEIGHT_RE = re.compile(r'\d+(?:[.,]\d+)?\s*(?:МЛ|КГ|Л|Г|ШТ)\.?\b', re.IGNORECASE)
_NOISE_START = ('не более', 'акция', 'скидка', '*акция', 'не действ', 'не распрост')

# (масштаб, язык, psm) — проходы для названия и запасного чтения скидки
_OCR_CONFIG = [
    (1, 'rus', 3), (1, 'eng', 3),
    (2, 'rus', 3), (2, 'eng', 3),
    (3, 'rus', 3), (3, 'eng', 3),
]

_TESS_LANGS = None


def _tesseract_path():
    for p in (shutil.which('tesseract'), r'D:\tesseract\tesseract.exe',
              r'C:\Program Files\Tesseract-OCR\tesseract.exe'):
        if p and Path(p).exists():
            return p
    return None


TESSERACT = _tesseract_path()


def _available_langs():
    global _TESS_LANGS
    if _TESS_LANGS is not None:
        return _TESS_LANGS
    langs = []
    if TESSERACT:
        try:
            r = subprocess.run([TESSERACT, '--list-langs'], capture_output=True, timeout=20)
            out = (r.stdout + r.stderr).decode('utf-8', 'ignore')
            for ln in ('rus', 'eng'):
                if re.search(r'(?m)^' + ln + r'\s*$', out):
                    langs.append(ln)
        except Exception:
            pass
    _TESS_LANGS = langs or ['eng']
    return _TESS_LANGS


def _ocr_once(path, lang, psm, whitelist=None, timeout=40):
    cmd = [TESSERACT, str(path), 'stdout', '-l', lang, '--psm', str(psm)]
    if whitelist:
        cmd += ['-c', 'tessedit_char_whitelist=' + whitelist]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return out.stdout.decode('utf-8', 'ignore')
    except Exception:
        return ''


def _ocr_tsv(path, lang, psm, timeout=40):
    try:
        out = subprocess.run(
            [TESSERACT, str(path), 'stdout', '-l', lang, '--psm', str(psm), 'tsv'],
            capture_output=True, timeout=timeout,
        )
        rows = []
        for line in out.stdout.decode('utf-8', 'ignore').splitlines()[1:]:
            f = line.split('\t')
            if len(f) >= 12 and f[11].strip():
                try:
                    rows.append({'l': float(f[6]), 't': float(f[7]), 'w': float(f[8]),
                                 'h': float(f[9]), 'c': float(f[10]), 'txt': f[11]})
                except ValueError:
                    pass
        return rows
    except Exception:
        return []


def _pct_from_text(text):
    """Первое число-% в диапазоне [5,95] после слова «СКИДКА» (иначе — первое вообще)."""
    up = text.upper()
    i = up.find('СКИДКА')
    matches = [(m.start(), int(m.group(1))) for m in _DISCOUNT_RE.finditer(text)]

    def ok(v):
        return 5 <= v <= 95

    if i >= 0:
        for pos, v in matches:
            if pos >= i and ok(v):
                return v
    for _, v in matches:
        if ok(v):
            return v
    return None


def _first_pct(text):
    t = text.replace('O', '0').replace('o', '0').replace('З', '3').replace('Э', '3')
    for m in re.finditer(r'(\d{1,3})\s*%', t):
        v = int(m.group(1))
        if 5 <= v <= 95:
            return v
    for m in re.finditer(r'(\d{1,2})', t):
        v = int(m.group(1))
        if 5 <= v <= 95:
            return v
    return None


def _clean_title(s):
    s = re.sub(r'\s+', ' ', s).strip(' .,;:—-|«»"')
    if len(s) < 3:
        return None
    return s[0].upper() + s[1:]


def _unicode_letters(s):
    return sum(1 for ch in s if ch.isalpha())


def _title_from_text(text):
    lines = [l.strip(' \t|·') for l in text.splitlines()]
    lines = [l for l in lines if l.strip()]
    for idx, l in enumerate(lines):
        low = l.lower().lstrip('«"')
        if low.startswith('на ') and len(l) >= 7:
            name = l
            if idx + 1 < len(lines):
                nxt = lines[idx + 1].strip()
                ln = nxt.lower()
                if (nxt and not ln.startswith(_NOISE_START)
                        and 'скидка' not in ln and _WEIGHT_RE.search(nxt)):
                    name = name + ' ' + nxt
            return _clean_title(name)
    cands = [l for l in lines
             if len(l) >= 6 and not l.lower().startswith(_NOISE_START)
             and 'скидка' not in l.lower() and _unicode_letters(l) >= 4]
    if cands:
        return _clean_title(max(cands, key=len))
    return None


def _build_variants(png_path):
    """Строит увеличенные копии (1x/2x/3x серый+автоконтраст и синий канал 3x)."""
    variants = {1: Path(png_path)}
    tmp_files = []
    if Image is None:
        return variants, tmp_files
    try:
        im = Image.open(png_path).convert('RGB')
        g = ImageOps.grayscale(im)
        for sc in (2, 3):
            big = ImageOps.autocontrast(g.resize((g.width * sc, g.height * sc), Image.LANCZOS))
            tf = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            tf.close()
            big.save(tf.name)
            tmp_files.append(tf.name)
            variants[sc] = Path(tf.name)
        try:
            blue = im.split()[2]
            b3 = ImageOps.autocontrast(blue.resize((blue.width * 3, blue.height * 3), Image.LANCZOS))
            tf = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            tf.close()
            b3.save(tf.name)
            tmp_files.append(tf.name)
            variants['ch3'] = Path(tf.name)
        except Exception:
            pass
    except Exception:
        pass
    return variants, tmp_files


def _ocr_passes(variants):
    langs = _available_langs()
    passes = []
    for sc, lang, psm in _OCR_CONFIG:
        if sc not in variants:
            continue
        use = lang if lang in langs else langs[0]
        txt = _ocr_once(variants[sc], use, psm)
        if txt.strip():
            passes.append({'scale': sc, 'lang': use, 'text': txt})
    return passes


def _crop_discount(img3):
    """Читает крупное число скидки под словом «СКИДКА» по координатам."""
    if not img3 or not TESSERACT or Image is None:
        return None
    sk = num = None
    for psm in (11, 3, 6, 12):
        rows = _ocr_tsv(img3, 'rus', psm)
        s = next((r for r in rows if 'СКИДКА' in r['txt'].upper()), None)
        if not s:
            continue
        below = [r for r in rows if r['t'] > s['t'] + s['h'] * 0.4 and r['h'] > s['h'] * 1.3]
        if below:
            sk, num = s, max(below, key=lambda r: r['h'])
            break
    if not sk or not num:
        return None
    try:
        im = Image.open(img3)
    except Exception:
        return None
    W, H = im.size
    left = max(0, int(min(sk['l'], num['l']) - 80))
    top = max(0, int(num['t'] - 12))
    right = min(W, int(num['l'] + num['w'] + 160))
    bot = min(H, int(num['t'] + num['h'] + 20))
    crop = im.crop((left, top, right, bot))
    crop = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
    tf = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
    tf.close()
    crop.save(tf.name)
    try:
        for psm in (7, 8, 6, 13):
            v = _first_pct(_ocr_once(tf.name, 'eng', psm, whitelist='0123456789%'))
            if v is not None:
                return v
    finally:
        try:
            Path(tf.name).unlink()
        except Exception:
            pass
    return None


def _pick_discount(passes):
    from collections import Counter
    for group in (0, 1):
        vals = [v for p in passes if (p['scale'] == 1) == (group == 0)
                for v in [_pct_from_text(p['text'])] if v is not None]
        if vals:
            cnt = Counter(vals)
            return max(cnt.items(), key=lambda kv: (kv[1], len(str(kv[0])), kv[0]))[0]
    return None


def _pick_title(passes):
    def key(p):
        if p['lang'] == 'rus' and p['scale'] >= 2:
            return 0
        if p['lang'] == 'rus':
            return 1
        return 2

    for p in sorted(passes, key=key):
        t = _title_from_text(p['text'])
        if t and p['lang'] == 'rus':
            return t
    for p in sorted(passes, key=key):
        t = _title_from_text(p['text'])
        if t:
            return t
    return None


def ocr_coupon(png_path):
    """Возвращает (скидка_%, название_с_картинки) баннера купона."""
    if not TESSERACT:
        return None, None
    variants, tmp_files = _build_variants(png_path)
    try:
        passes = _ocr_passes(variants)
        discount = _crop_discount(variants.get(3))
        if discount is None:
            discount = _pick_discount(passes)
        title = _pick_title(passes)
        return discount, title
    finally:
        for f in tmp_files:
            try:
                Path(f).unlink()
            except Exception:
                pass


class BristolError(Exception):
    pass


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _token_expired(token: str) -> bool:
    claims = _jwt_claims(token)
    return datetime.now(timezone.utc).timestamp() > claims.get('exp', 0) - 60


def _restart_app() -> None:
    subprocess.run(
        [ADB, '-s', DEVICE, 'shell',
         'am force-stop ru.bristol.bristol_app; sleep 2; '
         'monkey -p ru.bristol.bristol_app -c android.intent.category.LAUNCHER 1'],
        capture_output=True, timeout=60,
    )


def pull_prefs(retries: int = 2) -> bytes:
    raw = _pull_prefs_once()
    try:
        data = parse_prefs(raw)
    except BristolError:
        raise
    if _token_expired(data['token']):
        for _ in range(retries):
            _restart_app()
            deadline = datetime.now(timezone.utc) + timedelta(seconds=45)
            while datetime.now(timezone.utc) < deadline:
                import time
                time.sleep(5)
                raw = _pull_prefs_once()
                try:
                    data = parse_prefs(raw)
                except BristolError:
                    continue
                if not _token_expired(data['token']):
                    return raw
        raise BristolError('Не удалось получить свежий токен Бристоля. Откройте приложение вручную.')
    return raw


def _pull_prefs_once() -> bytes:
    proc = subprocess.run(
        [ADB, '-s', DEVICE, 'shell', 'cat', PREFS_PATH],
        capture_output=True, timeout=30,
    )
    raw = proc.stdout
    if not raw or b'flutter.token' not in raw:
        raise BristolError('Не удалось прочитать данные приложения (adb). Откройте приложение Бристоль.')
    CACHE_PATH.write_bytes(raw)
    return raw


def _field(raw: str, name: str) -> str:
    m = re.search(r'<string name="' + re.escape(name) + r'">(.*?)</string>', raw, re.S)
    return html.unescape(m.group(1)) if m else ''


def parse_prefs(raw: bytes | None = None) -> dict:
    if raw is None:
        if CACHE_PATH.exists():
            raw = CACHE_PATH.read_bytes()
        else:
            raw = pull_prefs()
    text = raw.decode('utf-8', errors='replace')
    token = _field(text, 'flutter.token')
    user_raw = _field(text, 'flutter.user')
    qr_raw = _field(text, 'flutter.loyaltyQrCodes')
    user = {}
    try:
        user = json.loads(user_raw) if user_raw else {}
    except Exception:
        pass
    qrs = []
    try:
        qrs = json.loads(qr_raw) if qr_raw else []
    except Exception:
        pass
    if not token:
        raise BristolError('Токен не найден в данных приложения.')
    return {'token': token, 'user': user, 'qrs': qrs, 'raw': text}


def current_qr(qrs: list) -> str:
    if not qrs:
        raise BristolError('Нет QR-кодов в данных приложения.')
    now = datetime.now(timezone.utc)
    best = None
    for e in qrs:
        try:
            t = datetime.fromisoformat(e['time'].replace('Z', '+00:00'))
        except Exception:
            continue
        if t <= now < t + timedelta(hours=1):
            return e['data']
        if t <= now and (best is None or t > best[0]):
            best = (t, e['data'])
    if best:
        return best[1]
    return qrs[0]['data']


def _adb_available() -> bool:
    try:
        return bool(ADB) and Path(ADB).exists()
    except Exception:
        return False


def _jwt_claim_safe(tok: str, claim: str) -> str:
    try:
        return _jwt_claims(tok).get(claim, '') or ''
    except Exception:
        return ''


def ensure_tokens_from_env(account_id: int = 1) -> bool:
    """Подтянуть токены Бристоля из переменных окружения (если заданы).

    Позволяет работать автономно (Railway/Prod) без ADB и эмулятора:
    достаточно BRISTOL_REFRESH_TOKEN. Пишет в аккаунт account_id (по умолчанию 1).
    """
    env_refresh = (os.environ.get('BRISTOL_REFRESH_TOKEN') or '').strip()
    env_token = (os.environ.get('BRISTOL_TOKEN') or '').strip()
    env_ext = (os.environ.get('BRISTOL_EXTERNAL_ID') or '').strip()
    if not (env_refresh or env_token):
        return False
    ext = env_ext or _jwt_claim_safe(env_token, 'external_id') or _jwt_claim_safe(env_refresh, 'external_id')
    apply_account_tokens(account_id, refresh_token=env_refresh, token=env_token, external_id=ext)
    return True


def _load_tokens(account_id=None) -> dict:
    aid = _aid(account_id)
    out = {}
    for key in ('token', 'refresh_token', 'external_id'):
        v = _get_meta(aid, key)
        if v is not None:
            out[key] = v
    return out


def _save_tokens(token: str, refresh: str, account_id=None) -> None:
    aid = _aid(account_id)
    ext = _jwt_claims(token).get('external_id', '')
    _set_meta(aid, 'token', token)
    _set_meta(aid, 'refresh_token', refresh)
    _set_meta(aid, 'external_id', ext)
    _account_touch(aid, token=token, refresh_token=refresh, external_id=ext or None)



def _http(method: str, path: str, headers: dict, body: bytes | None = None, timeout: int = 20):
    req = urllib.request.Request(API + path, data=body, headers=headers, method=method)
    resp = urllib.request.urlopen(req, timeout=timeout, context=_CTX)
    raw = resp.read()
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    return resp.status, raw


def _do_refresh(refresh: str, account_id=None) -> dict:
    ext = _jwt_claim_safe(refresh, 'external_id') or _load_tokens(account_id).get('external_id', '')
    headers = {
        'Accept': 'application/json',
        'User-Agent': HEADERS_UA,
        'refreshtoken': refresh,
        'appversion': APP_VERSION,
        'userguid': ext,
    }
    _, raw = _http('POST', '/api/v2/users/refresh', headers)
    return json.loads(raw.decode('utf-8', 'ignore'))


def refresh_token(account_id=None) -> str:
    tokens = _load_tokens(account_id)
    refresh = tokens.get('refresh_token')
    if not refresh:
        raise BristolError('Нет refresh-токена Бристоля.')
    data = _do_refresh(refresh, account_id)
    new_access = data.get('access_token')
    if not new_access:
        raise BristolError('Бристоль не вернул access_token.')
    _save_tokens(new_access, data.get('refresh_token') or refresh, account_id)
    return new_access


def seed_from_app(account_id=None) -> str:
    data = parse_prefs(pull_prefs())
    _save_tokens(data['token'], _field(data['raw'], 'flutter.refreshToken'), account_id)
    return data['token']


def get_token(force: bool = False, account_id=None) -> str:
    tokens = _load_tokens(account_id)
    token = tokens.get('token', '')
    if token and not force and not _token_expired(token):
        return token
    if tokens.get('refresh_token'):
        try:
            return refresh_token(account_id)
        except BristolError:
            pass
    # ADB-посев доступен только локально; в проде (Railway) его нет — работаем по токенам.
    if account_id is None and _adb_available():
        try:
            return seed_from_app(account_id)
        except BristolError:
            pass
    raise BristolError(
        'Нет токена Бристоля. Укажите BRISTOL_REFRESH_TOKEN в переменных окружения '
        'или синхронизируйте локально (откройте приложение Бристоль).'
    )



def _api_get(path: str, token: str | None = None, timeout: int = 20, _retry: bool = True) -> dict:
    if token is None:
        token = get_token()
    headers = {
        'Accept': 'application/json',
        'Authorization': 'Bearer ' + token,
        'User-Agent': HEADERS_UA,
        'X-API-KEY': X_API_KEY,
    }
    try:
        _, raw = _http('GET', path, headers, timeout=timeout)
        return json.loads(raw.decode('utf-8', 'ignore'))
    except urllib.error.HTTPError as e:
        if e.code == 401 and _retry:
            return _api_get(path, get_token(force=True), timeout, _retry=False)
        body = e.read().decode('utf-8', 'ignore')
        raise BristolError(
            'Сессия Бристоля истекла. Откройте приложение Бристоль и повторите синхронизацию.'
            if e.code in (401, 403) else f'API Бристоля: HTTP {e.code} {body[:200]}'
        )
    except urllib.error.URLError as e:
        raise BristolError(f'Ошибка запроса к API: {e}')


def fetch_coupons(token: str | None = None) -> dict:
    return _api_get('/api/v2/coupons?consumer=mobile&status=3', token)


def generate_card_codes(token: str | None = None, _retry: bool = True) -> dict:
    if token is None:
        token = get_token()
    headers = {
        'Accept': 'application/json',
        'Authorization': 'Bearer ' + token,
        'User-Agent': HEADERS_UA,
        'Content-Type': 'application/json',
        'X-API-KEY': X_API_KEY,
    }
    body = json.dumps({'device_identifier': CARD_DEVICE_ID, 'with_user_external_id': True}).encode()
    try:
        _, raw = _http('POST', '/api/v3/discount_card_codes/generate?consumer=mobile', headers, body=body)
        return json.loads(raw.decode('utf-8', 'ignore'))
    except urllib.error.HTTPError as e:
        if e.code == 401 and _retry:
            return generate_card_codes(get_token(force=True), _retry=False)
        raise BristolError(f'API Бристоля (карта): HTTP {e.code}')


def fetch_image(url: str, token: str, timeout: int = 25) -> bytes:
    headers = {
        'Accept': '*/*',
        'Authorization': 'Bearer ' + token,
        'User-Agent': HEADERS_UA,
        'X-API-KEY': X_API_KEY,
    }
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=timeout, context=_CTX)
    return resp.read()


def _cache_coupon_images(coupons: list, token: str) -> dict:
    """Скачивает картинки купонов локально (CDN-ссылки с подписью истекают)."""
    IMG_DIR.mkdir(parents=True, exist_ok=True)

    def _dl(c):
        n = c.get('number')
        u = c.get('image_url')
        if not n or not u:
            return n, u
        try:
            raw = fetch_image(u, token)
            if raw[:8] == b'\x89PNG\r\n\x1a\n' or raw[:2] == b'\xff\xd8':
                (IMG_DIR / (n + '.png')).write_bytes(raw)
                return n, f'/static/img/{n}.png'
        except Exception:
            pass
        return n, u

    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(ex.map(_dl, coupons))


def _ocr_coupon_meta(coupons: list, local_images: dict) -> dict:
    """OCR баннеров (с дедупликацией по содержимому): {number: (discount, title)}."""
    paths = {}
    for c in coupons:
        n = c.get('number')
        if n and str(local_images.get(n, '')).startswith('/static/img/'):
            p = IMG_DIR / (n + '.png')
            if p.exists():
                paths[n] = p
    out = {}
    if not TESSERACT or not paths:
        return out

    by_hash = {}
    hashes = {}
    for n, p in paths.items():
        try:
            h = hashlib.md5(p.read_bytes()).hexdigest()
        except Exception:
            continue
        hashes[n] = h
        by_hash.setdefault(h, p)
    uniq = list(by_hash.values())

    def _job(p):
        return p.name, ocr_coupon(p)

    try:
        workers = max(4, min(8, os.cpu_count() or 4))
    except Exception:
        workers = 6
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for name, res in ex.map(_job, uniq):
            results[name] = res
    for n, h in hashes.items():
        p = by_hash.get(h)
        if p and p.name in results:
            out[n] = results[p.name]
    return out


def sync(account_id=None) -> dict:
    aid = _aid(account_id)
    token = get_token(account_id=aid)
    coupons = fetch_coupons(token).get('coupons') or []
    user = _api_get('/api/v2/users/?consumer=mobile', token).get('user') or {}
    try:
        codes = generate_card_codes(token).get('discount_card_codes') or []
        schedule = [{'time': c.get('time'), 'data': c.get('data')} for c in codes if c.get('data')]
    except BristolError:
        schedule = []
    try:
        card_qr = current_qr(schedule) if schedule else ''
    except BristolError:
        card_qr = ''
    now = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM bristol_coupons WHERE account_id=?', (aid,))
    local_images = _cache_coupon_images(coupons, token)
    ocr_meta = _ocr_coupon_meta(coupons, local_images)
    for c in coupons:
        num = c.get('number')
        discount, title = ocr_meta.get(num, (None, None))
        cur.execute(
            '''INSERT INTO bristol_coupons
               (account_id, number, universal_coupon, coupon_type, image_url, actual_start, actual_end, discount, title, synced_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id, number) DO UPDATE SET
                 universal_coupon=excluded.universal_coupon,
                 coupon_type=excluded.coupon_type,
                 image_url=excluded.image_url,
                 actual_start=excluded.actual_start,
                 actual_end=excluded.actual_end,
                 discount=COALESCE(excluded.discount, bristol_coupons.discount),
                 title=COALESCE(excluded.title, bristol_coupons.title),
                 synced_at=excluded.synced_at''',
            (
                aid,
                num,
                c.get('universal_coupon'),
                c.get('coupon_type'),
                local_images.get(num, c.get('image_url')),
                c.get('actual_start'),
                c.get('actual_end'),
                discount,
                title,
                now,
            ),
        )
    conn.commit()
    conn.close()
    meta = {
        'phone': user.get('phone', ''),
        'name': ' '.join(x for x in (user.get('first_name'), user.get('last_name')) if x),
        'balance': user.get('status_active_balance'),
        'loyalty_status': user.get('loyalty_status', ''),
        'synced_at': now,
        'coupon_count': len(coupons),
        'card_qr': card_qr,
        'card_schedule': schedule,
    }
    for k, v in meta.items():
        _set_meta(aid, k, v)
    _account_touch(
        aid,
        name=meta['name'] or None,
        phone=meta['phone'] or None,
        card_id=_card_id_from_phone(meta['phone']) or None,
    )
    return meta


def get_meta(account_id=None) -> dict:
    aid = _aid(account_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT key, value FROM bristol_meta WHERE account_id=?', (aid,))
    out = {}
    for r in cur.fetchall():
        try:
            out[r['key']] = json.loads(r['value'])
        except Exception:
            out[r['key']] = r['value']
    conn.close()
    return out


def get_stored_coupons(account_id=None) -> list:
    aid = _aid(account_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT number, universal_coupon, coupon_type, image_url, actual_start, actual_end, discount, title '
        'FROM bristol_coupons WHERE account_id=? '
        'ORDER BY (discount IS NULL), discount DESC, actual_start DESC',
        (aid,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_coupon(number: str, account_id=None):
    aid = _aid(account_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT number, universal_coupon, coupon_type, image_url, actual_start, actual_end, discount, title '
        'FROM bristol_coupons WHERE account_id=? AND number=?',
        (aid, number),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def available_bristol_coupons(account_id=None) -> list:
    now = datetime.now(timezone.utc)
    out = []
    for c in get_stored_coupons(account_id):
        end = c.get('actual_end')
        if end:
            try:
                if datetime.fromisoformat(end) < now:
                    continue
            except Exception:
                pass
        out.append(c)
    return out


def get_card_qr(account_id=None) -> str:
    aid = _aid(account_id)
    meta = get_meta(aid)
    schedule = meta.get('card_schedule')
    if schedule:
        try:
            return current_qr(schedule)
        except BristolError:
            pass
    data = meta.get('card_qr') or ''
    if data:
        return data
    if aid == get_active_account_id():
        try:
            return current_qr(parse_prefs()['qrs'])
        except BristolError:
            return ''
    return ''



def _card_ts(code: str) -> str:
    m = re.search(r't(\d{9,12})', code)
    return m.group(1) if m else str(int(datetime.now(timezone.utc).timestamp()))


def wrap_card_payload(code: str) -> str:
    """Обёртка кода карты как в приложении: '[B+]' + base64(<ts> + код)."""
    if not code:
        return ''
    inner = _card_ts(code) + code
    return '[B+]' + base64.b64encode(inner.encode('utf-8')).decode('ascii')


def get_card_payload(account_id=None) -> str:
    return wrap_card_payload(get_card_qr(account_id))


def sync_card_codes(account_id=None) -> dict:
    """Перегенерировать расписание кодов карты с сервера и сохранить."""
    aid = _aid(account_id)
    token = get_token(account_id=aid)
    d = generate_card_codes(token)
    codes = d.get('discount_card_codes') or []
    schedule = [{'time': c.get('time'), 'data': c.get('data')} for c in codes if c.get('data')]
    card_qr = ''
    try:
        card_qr = current_qr(schedule)
    except BristolError:
        pass
    _set_meta(aid, 'card_schedule', schedule)
    _set_meta(aid, 'card_qr', card_qr)
    return {'codes': len(schedule), 'card_qr': card_qr}


def qr_png_response(account_id=None):
    from datamatrix import make_dm_png
    data = get_card_payload(account_id)
    if not data:
        raise BristolError('Код карты не получен. Нажмите «Синхронизировать».')
    return make_dm_png(data, box_size=5, border=4)
