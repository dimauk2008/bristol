import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).parent))
sys.path.insert(0, str(_Path(__file__).parent.parent / 'capture'))

import hashlib
import hmac
import json
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Form, HTTPException, status
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from pathlib import Path

import qrcode
from db import init_db, get_conn, get_setting, set_setting, get_active_account_id
from qr import make_qr_png
from datamatrix import make_dm_png
import bristol
import scheduler
from bristol import BristolError

# Farm9 web control
from farm9_web import (
    farm9_controller,
    CAPTURE_DIR as FARM9_CAPTURE_DIR,
    CREDS_DIR as FARM9_CREDS_DIR,
    VERSIONS as FARM9_VERSIONS,
    list_creds as farm9_list_creds,
    resolve_creds_file as farm9_resolve_creds,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(title='Coupons & Card QR', lifespan=lifespan)


BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / 'templates')


class CouponStaticFiles(StaticFiles):
    """Статика + запасной источник картинок купонов из БД.

    Файловая система контейнера эфемерная: после деплоя скачанные картинки
    пропадают, хотя в БД остаются ссылки /static/img/<номер>.png. Если файла
    нет, отдаём байты из таблицы bristol_images.
    """

    async def get_response(self, path, scope):
        try:
            response = await super().get_response(path, scope)
        except Exception:
            response = None
        if response is not None and response.status_code != 404:
            return response
        norm = path.replace('\\', '/')
        if not norm.startswith('img/'):
            return response if response is not None else Response(status_code=404)
        number = Path(norm).stem
        allowed = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-')
        if not number or not set(number) <= allowed:
            return response if response is not None else Response(status_code=404)
        try:
            row = await run_in_threadpool(bristol.get_coupon_image, number)
        except Exception:
            row = None
        if not row:
            return response if response is not None else Response(status_code=404)
        data, media_type = row
        return Response(
            content=data,
            media_type=media_type,
            headers={'Cache-Control': 'public, max-age=31536000, immutable'},
        )


app.mount('/static', CouponStaticFiles(directory=BASE_DIR / 'static'), name='static')

init_db()


# ---------- автономность (Prod/Railway): токены из env + сид купонов ----------

try:
    bristol.ensure_tokens_from_env()
except Exception:
    pass


def _seed_coupons_from_json() -> int:
    """Заполнить купоны из seed_coupons.json (для чистой БД в проде).

    Существующие строки не затираются: title/discount заполняются только если пусты.
    """
    path = BASE_DIR / 'seed_coupons.json'
    if not path.exists():
        return 0
    try:
        rows = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return 0
    if not isinstance(rows, list):
        return 0
    conn = get_conn()
    cur = conn.cursor()
    n = 0
    for r in rows:
        num = r.get('number')
        if not num:
            continue
        cur.execute(
            'INSERT INTO bristol_coupons (account_id, number, universal_coupon, coupon_type, image_url, '
            'actual_start, actual_end, discount, title) VALUES (1,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(account_id, number) DO UPDATE SET '
            'title=COALESCE(bristol_coupons.title, excluded.title), '
            'discount=COALESCE(bristol_coupons.discount, excluded.discount)',
            (
                num, r.get('universal_coupon'), r.get('coupon_type'), r.get('image_url'),
                r.get('actual_start'), r.get('actual_end'), r.get('discount'), r.get('title'),
            ),
        )
        n += 1
    conn.commit()
    conn.close()
    return n


try:
    _seed_coupons_from_json()
except Exception:
    pass


def _card_schedule_exists(account_id=None) -> bool:
    aid = account_id if account_id is not None else get_active_account_id()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM bristol_meta WHERE account_id=? AND key='card_schedule'", (aid,))
    row = cur.fetchone()
    conn.close()
    if not row or not row['value']:
        return False
    try:
        return bool(json.loads(row['value']))
    except Exception:
        return False


COOKIE = 'admin'

DURATIONS = {
    '15m': 15, '30m': 30, '1h': 60, '3h': 180, '6h': 360, '12h': 720,
    '24h': 1440, '3d': 4320, '7d': 10080, '30d': 43200, 'none': 0,
}
DURATION_LABELS = {
    '15m': '15 минут', '30m': '30 минут', '1h': '1 час', '3h': '3 часа',
    '6h': '6 часов', '12h': '12 часов', '24h': '24 часа', '3d': '3 дня',
    '7d': '7 дней', '30d': '30 дней', 'none': 'Бессрочно',
}


# ---------- admin auth ----------

def _admin_secret() -> str:
    s = get_setting('admin_secret')
    if not s:
        s = secrets.token_hex(32)
        set_setting('admin_secret', s)
    return s


def _hash_password(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt.encode('utf-8'), 120000).hex()
    return f'{salt}${h}'


def _verify_password(pw: str) -> bool:
    stored = get_setting('admin_password')
    if not stored or '$' not in stored:
        return False
    salt, h = stored.split('$', 1)
    calc = _hash_password(pw, salt).split('$', 1)[1]
    return hmac.compare_digest(calc, h)


def _admin_token() -> str:
    return hmac.new(_admin_secret().encode('utf-8'), b'admin', hashlib.sha256).hexdigest()


def _is_admin(request: Request) -> bool:
    return bool(request.cookies.get(COOKIE)) and hmac.compare_digest(
        request.cookies.get(COOKIE), _admin_token()
    )


if not get_setting('admin_password'):
    set_setting('admin_password', _hash_password(os.environ.get('ADMIN_PASSWORD', 'bristol')))

_PUBLIC_EXACT = {'/login', '/logout', '/favicon.ico'}
_PUBLIC_PREFIX = ('/s/', '/g/', '/static')


@app.middleware('http')
async def admin_guard(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIX):
        return await call_next(request)
    if _is_admin(request):
        return await call_next(request)
    if path.startswith('/api/'):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    return RedirectResponse('/login', status_code=status.HTTP_303_SEE_OTHER)


@app.get('/login', response_class=HTMLResponse)
async def login_page(request: Request):
    if _is_admin(request):
        return RedirectResponse('/', status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, 'login.html', {'error': request.query_params.get('e')})


@app.post('/login')
async def login_submit(password: str = Form(...)):
    if _verify_password(password):
        resp = RedirectResponse('/', status_code=status.HTTP_303_SEE_OTHER)
        resp.set_cookie(COOKIE, _admin_token(), httponly=True, samesite='lax', max_age=60 * 60 * 24 * 30)
        return resp
    return RedirectResponse('/login?e=1', status_code=status.HTTP_303_SEE_OTHER)


@app.get('/logout')
async def logout():
    resp = RedirectResponse('/login', status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(COOKIE)
    return resp


@app.post('/admin/password')
async def change_password(current: str = Form(...), new: str = Form(...)):
    if not _verify_password(current):
        return RedirectResponse('/?perr=1', status_code=status.HTTP_303_SEE_OTHER)
    if len(new) < 4:
        return RedirectResponse('/?perr=2', status_code=status.HTTP_303_SEE_OTHER)
    set_setting('admin_password', _hash_password(new))
    return RedirectResponse('/?pok=1', status_code=status.HTTP_303_SEE_OTHER)


# ---------- helpers ----------

def _load_session(session_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT id, name, active, expires_at, created_at, account_id, show_coupons FROM sessions WHERE id=?', (session_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def _session_valid(s: dict | None) -> bool:
    if not s or s['active'] != 1:
        return False
    exp = s.get('expires_at')
    if exp:
        try:
            if datetime.fromisoformat(exp) <= datetime.now(timezone.utc):
                return False
        except Exception:
            pass
    return True


def _remaining(expires_at: str | None):
    if not expires_at:
        return None
    try:
        return int((datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def _stub_page(request: Request, status_code: int = status.HTTP_404_NOT_FOUND):
    return templates.TemplateResponse(request, 'stub.html', {}, status_code=status_code)


def _expired_page(request: Request, session_id: str):
    return _stub_page(request, status.HTTP_410_GONE)


def _load_grant(token: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT token, number, active, expires_at, created_at, account_id FROM coupon_grants WHERE token=?', (token,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def _grant_valid(g: dict | None) -> bool:
    if not g or g['active'] != 1:
        return False
    exp = g.get('expires_at')
    if exp:
        try:
            if datetime.fromisoformat(exp) <= datetime.now(timezone.utc):
                return False
        except Exception:
            pass
    return True


def _active_grants(account_id=None):
    aid = account_id if account_id is not None else get_active_account_id()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT token, number, active, expires_at, created_at, account_id FROM coupon_grants '
                'WHERE account_id=? ORDER BY created_at DESC', (aid,))
    out = []
    for r in cur.fetchall():
        g = dict(r)
        g['remaining'] = _remaining(g.get('expires_at'))
        g['valid'] = _grant_valid(g)
        out.append(g)
    conn.close()
    return out


def _discount_value(value):
    try:
        return int(float(value))
    except Exception:
        return None


def _discount_counts(account_id=None) -> list:
    aid = account_id if account_id is not None else get_active_account_id()
    try:
        coupons = bristol.available_bristol_coupons(aid)
    except Exception:
        coupons = []
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT number FROM coupon_grants WHERE account_id=?', (aid,))
    issued = {r['number'] for r in cur.fetchall()}
    conn.close()
    counts: dict[int, int] = {}
    for c in coupons:
        value = _discount_value(c.get('discount'))
        if value is None or c.get('number') in issued:
            continue
        counts[value] = counts.get(value, 0) + 1
    return [{'value': k, 'count': counts[k]} for k in sorted(counts, reverse=True)]


# ---------- admin panel ----------

def _purchases_by_account(accounts: list, active_id) -> list:
    """История покупок по каждой карте/аккаунту для вкладки «История покупок»."""
    out = []
    for a in accounts or []:
        aid = a.get('id')
        try:
            items = bristol.get_stored_purchases(aid, limit=50)
            meta = bristol.get_meta(aid)
        except Exception:
            items, meta = [], {}
        total = sum(p.get('total') or 0 for p in items)
        out.append({
            'account': a,
            'purchases': items,
            'is_active': aid == active_id,
            'name': meta.get('name') or a.get('name') or f'Аккаунт {aid}',
            'phone': meta.get('phone') or a.get('phone') or '',
            'count': len(items),
            'total': total,
            'total_display': f'{total:.2f}',
            'synced_at': (meta.get('purchases_synced_at') or '')[:16].replace('T', ' '),
            'error': meta.get('last_purchases_error') or '',
        })
    return out


@app.get('/', response_class=HTMLResponse)
async def index(request: Request):
    aid = get_active_account_id()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT id, name, active, expires_at, created_at, account_id, show_coupons FROM sessions '
                'WHERE account_id=? ORDER BY created_at DESC', (aid,))
    sessions = [dict(r) for r in cur.fetchall()]
    for s in sessions:
        s['remaining'] = _remaining(s.get('expires_at'))
    cur.execute('SELECT id, code, value, status, note, created_at, used_at, used_by_session_id FROM coupons ORDER BY id DESC')
    coupons = [dict(r) for r in cur.fetchall()]
    conn.close()
    try:
        bristol_meta = bristol.get_meta(aid)
    except Exception:
        bristol_meta = {}
    bristol_coupons = bristol.get_stored_coupons(aid)
    accounts = bristol.accounts_list()
    purchases_by_account = _purchases_by_account(accounts, aid)
    return templates.TemplateResponse(request, 'index.html', {
        'sessions': sessions,
        'coupons': coupons,
        'bristol_meta': bristol_meta,
        'bristol_coupons': bristol_coupons,
        'discounts': _discount_counts(aid),
        'grants': _active_grants(aid),
        'accounts': accounts,
        'purchases_by_account': purchases_by_account,
        'purchases_total': sum(x['count'] for x in purchases_by_account),
        'active_account_id': aid,
        'auto': scheduler.status(),
        'durations': DURATION_LABELS,
        'perr': request.query_params.get('perr'),
        'pok': request.query_params.get('pok'),
        'aerr': request.query_params.get('aerr'),
        'aok': request.query_params.get('aok'),
    })


@app.get('/ticker', response_class=HTMLResponse)
async def ticker_page(request: Request):
    return templates.TemplateResponse(request, 'ticker.html', {})


@app.get('/api/admin/data')
async def api_admin_data():
    aid = get_active_account_id()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT id, name, active, expires_at FROM sessions WHERE account_id=? ORDER BY created_at DESC', (aid,))
    sessions = []
    for r in cur.fetchall():
        s = dict(r)
        s['remaining'] = _remaining(s.get('expires_at'))
        sessions.append(s)
    cur.execute('SELECT count(*) c FROM coupons')
    manual = cur.fetchone()['c']
    cur.execute('SELECT count(*) c FROM bristol_coupons WHERE account_id=?', (aid,))
    bcount = cur.fetchone()['c']
    conn.close()
    meta = bristol.get_meta(aid)
    safe = {k: meta.get(k) for k in ('balance', 'coupon_count', 'phone', 'name', 'loyalty_status', 'synced_at')}
    return JSONResponse({
        'meta': safe,
        'sessions': sessions,
        'grants': _active_grants(aid),
        'counts': {'bristol': bcount, 'manual': manual},
        'last_error': meta.get('last_error'),
        'auto': scheduler.status(),
        'active_account_id': aid,
    })


@app.get('/api/auto/status')
async def api_auto_status():
    return JSONResponse(scheduler.status())


@app.get('/api/admin/coupons')
async def api_admin_coupons():
    return JSONResponse({'coupons': bristol.get_stored_coupons()})


@app.post('/bristol/sync')
async def bristol_sync():
    aid = get_active_account_id()
    try:
        meta = await run_in_threadpool(bristol.sync, aid)
        msg = (meta or {}).get('last_error') or ''
    except BristolError as e:
        msg = str(e)
    except Exception as e:
        msg = f'Ошибка: {e}'
    conn = get_conn()
    cur = conn.cursor()
    if msg:
        cur.execute(
            'INSERT INTO bristol_meta (account_id, key, value) VALUES (?,?,?) '
            'ON CONFLICT(account_id, key) DO UPDATE SET value=excluded.value',
            (aid, 'last_error', json.dumps(msg, ensure_ascii=False)),
        )
    else:
        cur.execute("DELETE FROM bristol_meta WHERE account_id=? AND key='last_error'", (aid,))
    conn.commit()
    conn.close()
    return RedirectResponse(url='/', status_code=status.HTTP_303_SEE_OTHER)


@app.get('/bristol/qr.png')
async def bristol_qr():
    try:
        return await run_in_threadpool(bristol.qr_png_response)
    except BristolError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------- аккаунты ----------

@app.post('/accounts/add')
async def account_add_route(refresh_token: str = Form(''), name: str = Form(''),
                            phone: str = Form(''), token: str = Form('')):
    rt = refresh_token.strip()
    tk = token.strip()
    if not rt and not tk:
        return RedirectResponse('/?aerr=1', status_code=status.HTTP_303_SEE_OTHER)
    aid = bristol.account_add(name=name.strip(), phone=phone.strip(),
                              refresh_token=rt, token=tk)
    try:
        bristol.account_refresh_info(aid)
    except Exception:
        pass
    return RedirectResponse('/?aok=1', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/accounts/from-device')
async def account_from_device_route():
    try:
        await run_in_threadpool(bristol.account_add_from_device)
    except Exception:
        return RedirectResponse('/?aerr=1', status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse('/?aok=1', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/accounts/{account_id}/activate')
async def account_activate_route(account_id: int):
    if bristol.account_get(account_id):
        bristol.account_activate(account_id)
    return RedirectResponse('/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/accounts/{account_id}/refresh')
async def account_refresh_route(account_id: int):
    try:
        bristol.account_refresh_info(account_id)
    except Exception:
        pass
    return RedirectResponse('/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/accounts/{account_id}/delete')
async def account_delete_route(account_id: int):
    bristol.account_delete(account_id)
    return RedirectResponse('/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/sessions/create')
async def create_session(name: str = Form(...), duration: str = Form('none'), show_coupons: int = Form(0)):
    minutes = DURATIONS.get(duration, 0)
    expires_at = None
    if minutes:
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    sid = str(uuid.uuid4())
    aid = get_active_account_id()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('INSERT INTO sessions (id,name,active,expires_at,account_id,show_coupons) VALUES (?,?,1,?,?,?)',
                (sid, name, expires_at, aid, 1 if show_coupons else 0))
    conn.commit()
    conn.close()
    return RedirectResponse(url='/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/sessions/{session_id}/update')
async def update_session(session_id: str, name: str = Form(...), duration: str = Form(''), show_coupons: int = Form(0)):
    conn = get_conn()
    cur = conn.cursor()
    if duration in DURATIONS:
        minutes = DURATIONS[duration]
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat() if minutes else None
        cur.execute('UPDATE sessions SET name=?, expires_at=?, show_coupons=? WHERE id=?',
                    (name, expires_at, 1 if show_coupons else 0, session_id))
    else:
        cur.execute('UPDATE sessions SET name=?, show_coupons=? WHERE id=?',
                    (name, 1 if show_coupons else 0, session_id))
    conn.commit()
    conn.close()
    return RedirectResponse(url='/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/sessions/{session_id}/extend')
async def extend_session(session_id: str, duration: str = Form('1h')):
    minutes = DURATIONS.get(duration, 0)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat() if minutes else None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('UPDATE sessions SET expires_at=? WHERE id=?', (expires_at, session_id))
    conn.commit()
    conn.close()
    return RedirectResponse(url='/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/sessions/{session_id}/revoke')
async def revoke_session(session_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('UPDATE sessions SET active=0 WHERE id=?', (session_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url='/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/sessions/{session_id}/restore')
async def restore_session(session_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('UPDATE sessions SET active=1 WHERE id=?', (session_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url='/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/sessions/{session_id}/toggle')
async def toggle_session(session_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('UPDATE sessions SET active = 1 - active WHERE id=?', (session_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url='/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/sessions/{session_id}/delete')
async def delete_session(session_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM sessions WHERE id=?', (session_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url='/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/coupons/add')
async def add_coupon(code: str = Form(...), value: int = Form(0), note: str = Form('')):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO coupons (code,value,note,status) VALUES (?,?,?,'available')", (code.strip(), value, note))
        conn.commit()
    except Exception:
        pass
    conn.close()
    return RedirectResponse(url='/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/coupons/{cid}/use')
async def use_coupon(cid: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE coupons SET status='used', used_at=?, used_by_session_id=NULL WHERE id=? AND status='available'", (datetime.now(timezone.utc).isoformat(), cid))
    conn.commit()
    conn.close()
    return RedirectResponse(url='/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/coupons/{cid}/unuse')
async def unuse_coupon(cid: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE coupons SET status='available', used_at=NULL, used_by_session_id=NULL WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return RedirectResponse(url='/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/coupons/{cid}/delete')
async def delete_coupon(cid: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM coupons WHERE id=?', (cid,))
    conn.commit()
    conn.close()
    return RedirectResponse(url='/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/coupons/import')
async def import_coupons(text: str = Form(...)):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    conn = get_conn()
    cur = conn.cursor()
    for l in lines:
        try:
            code = l.split(';')[0].split(',')[0].strip()
            if not code:
                continue
            cur.execute("INSERT INTO coupons (code,status) VALUES (?, 'available') ON CONFLICT DO NOTHING", (code,))
        except Exception:
            continue
    conn.commit()
    conn.close()
    return RedirectResponse(url='/', status_code=status.HTTP_303_SEE_OTHER)


# ---------- разовая выдача купонов (TTL + отзыв) ----------

@app.post('/grants/create')
async def create_grant(request: Request):
    body = await request.json()
    number = (body.get('number') or '').strip()
    duration = body.get('duration') or 'none'
    aid = get_active_account_id()
    if not number or not bristol.get_coupon(number, aid):
        return JSONResponse({'error': 'Купон не найден'}, status_code=404)
    minutes = DURATIONS.get(duration, 0)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat() if minutes else None
    token = secrets.token_urlsafe(16)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('INSERT INTO coupon_grants (token, number, active, expires_at, account_id) VALUES (?,?,1,?,?)',
                (token, number, expires_at, aid))
    conn.commit()
    conn.close()
    return JSONResponse({'token': token, 'number': number, 'url': f'/g/{token}', 'expires_at': expires_at})


@app.post('/grants/bulk')
async def create_grants_bulk(request: Request):
    """Выдать по одной ссылке на каждый действующий купон выбранной скидки.

    Уже выданные купоны (по этому аккаунту) пропускаются.
    """
    body = await request.json()
    discount = _discount_value(body.get('discount'))
    if discount is None:
        return JSONResponse({'error': 'Укажите скидку'}, status_code=400)
    duration = body.get('duration') or 'none'
    minutes = DURATIONS.get(duration, 0)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat() if minutes else None
    aid = get_active_account_id()
    coupons = await run_in_threadpool(bristol.available_bristol_coupons, aid)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT number FROM coupon_grants WHERE account_id=?', (aid,))
    issued_before = {r['number'] for r in cur.fetchall()}

    matching = [c for c in coupons
                if _discount_value(c.get('discount')) == discount and c.get('number')]
    fresh = [c for c in matching if c['number'] not in issued_before]
    out = []
    for c in fresh:
        token = secrets.token_urlsafe(16)
        cur.execute('INSERT INTO coupon_grants (token, number, active, expires_at, account_id) VALUES (?,?,1,?,?)',
                    (token, c['number'], expires_at, aid))
        out.append({
            'number': c['number'],
            'title': c.get('title') or c.get('universal_coupon') or c['number'],
            'discount': discount,
            'url': f'/g/{token}',
            'expires_at': expires_at,
        })
    conn.commit()
    conn.close()
    return JSONResponse({
        'discount': discount,
        'duration': duration,
        'issued': out,
        'skipped': len(matching) - len(out),
        'active_total': len(coupons),
    })


@app.post('/grants/{token}/revoke')
async def revoke_grant(token: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('UPDATE coupon_grants SET active=0 WHERE token=?', (token,))
    conn.commit()
    conn.close()
    return JSONResponse({'ok': True})


@app.get('/g/{token}', response_class=HTMLResponse)
async def public_grant(request: Request, token: str):
    g = _load_grant(token)
    if not g:
        return _stub_page(request)
    if not _grant_valid(g):
        return _stub_page(request, status.HTTP_410_GONE)
    c = bristol.get_coupon(g['number'], g.get('account_id'))
    if not c:
        return _stub_page(request)
    return templates.TemplateResponse(request, 'coupon_grant.html', {
        'coupon': c,
        'token': token,
        'remaining': _remaining(g.get('expires_at')),
    })


@app.get('/g/{token}/qr.png')
async def public_grant_qr(token: str):
    g = _load_grant(token)
    if not g or not _grant_valid(g):
        raise HTTPException(status_code=404)
    if not bristol.get_coupon(g['number'], g.get('account_id')):
        raise HTTPException(status_code=404, detail='Coupon not found')
    return make_dm_png(g['number'], box_size=8, border=4)


# ---------- public pages ----------

@app.get('/s/{session_id}/card', response_class=HTMLResponse)
async def public_card(request: Request, session_id: str):
    s = _load_session(session_id)
    if not s:
        return _stub_page(request)
    if not _session_valid(s):
        return _expired_page(request, session_id)
    aid = s.get('account_id')
    card_data = bristol.get_card_qr(aid)
    try:
        bristol_coupons = bristol.available_bristol_coupons(aid)
    except Exception:
        bristol_coupons = []
    coupons_count = len(bristol_coupons)

    access_total = None
    exp, created = s.get('expires_at'), s.get('created_at')
    if exp and created:
        try:
            access_total = max(0, int(
                (datetime.fromisoformat(exp) - datetime.fromisoformat(created)).total_seconds()
            ))
        except Exception:
            access_total = None

    return templates.TemplateResponse(request, 'card-ses.html', {
        'session': s,
        'card_data': card_data,
        'remaining': _remaining(s.get('expires_at')),
        'access_total': access_total,
        'bristol_meta': bristol.get_meta(aid),
        'bristol_coupons': bristol_coupons[:3],
        'coupons_count': coupons_count,
        'show_coupons': s.get('show_coupons', 1) == 1,
    })


@app.get('/s/{session_id}/card/qr.png')
async def public_card_qr(session_id: str, refresh: bool = False):
    s = _load_session(session_id)
    if not s or not _session_valid(s):
        raise HTTPException(status_code=404)
    aid = s.get('account_id')
    if refresh or not _card_schedule_exists(aid):
        try:
            await run_in_threadpool(bristol.sync_card_codes, aid)
        except Exception:
            pass
    qr_data = bristol.get_card_payload(aid)
    if not qr_data:
        raise HTTPException(status_code=404, detail='QR data is empty')
    return make_dm_png(qr_data, box_size=5, border=4)


@app.get('/s/{session_id}/coupons', response_class=HTMLResponse)
async def public_coupons(request: Request, session_id: str):
    s = _load_session(session_id)
    if not s:
        return _stub_page(request)
    if not _session_valid(s):
        return _expired_page(request, session_id)
    if s.get('show_coupons', 1) != 1:
        raise HTTPException(status_code=404)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT code,value,status,note FROM coupons WHERE status='available' ORDER BY id DESC")
    coupons = [dict(r) for r in cur.fetchall()]
    conn.close()
    try:
        bristol_coupons = bristol.available_bristol_coupons(s.get('account_id'))
    except Exception:
        bristol_coupons = []
    return templates.TemplateResponse(request, 'coupons_public.html', {
        'session': s,
        'coupons': coupons,
        'bristol_coupons': bristol_coupons,
        'remaining': _remaining(s.get('expires_at')),
    })


@app.get('/s/{session_id}/b/{number}', response_class=HTMLResponse)
async def public_bristol_coupon(request: Request, session_id: str, number: str):
    s = _load_session(session_id)
    if not s:
        return _stub_page(request)
    if not _session_valid(s):
        return _expired_page(request, session_id)
    c = bristol.get_coupon(number, s.get('account_id'))
    if not c:
        return _stub_page(request)
    return templates.TemplateResponse(request, 'bristol_coupon.html', {
        'session': s,
        'coupon': c,
        'remaining': _remaining(s.get('expires_at')),
    })


@app.get('/s/{session_id}/b/{number}/qr.png')
async def public_bristol_coupon_qr(session_id: str, number: str):
    s = _load_session(session_id)
    if not s or not _session_valid(s):
        raise HTTPException(status_code=404)
    if not bristol.get_coupon(number, s.get('account_id')):
        raise HTTPException(status_code=404, detail='Coupon not found')
    return make_dm_png(number, box_size=8, border=4)


@app.get('/s/{session_id}/coupon/{code}', response_class=HTMLResponse)
async def public_get_coupon(request: Request, session_id: str, code: str):
    s = _load_session(session_id)
    if not s:
        return _stub_page(request)
    if not _session_valid(s):
        return _expired_page(request, session_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id,code,value,status,used_by_session_id FROM coupons WHERE code=?", (code,))
    c = cur.fetchone()
    if not c:
        conn.close()
        return _stub_page(request)
    if c['status'] != 'available':
        conn.close()
        return _stub_page(request)
    cur.execute("UPDATE coupons SET status='used', used_at=?, used_by_session_id=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), session_id, c['id']))
    conn.commit()
    cur.execute("SELECT id,code,value,status,note,used_by_session_id FROM coupons WHERE id=?", (c['id'],))
    c2 = cur.fetchone()
    conn.close()
    return templates.TemplateResponse(request, 'coupon_given.html', {'session': s, 'coupon': dict(c2)})


@app.post('/s/{session_id}/coupon/{code}/claim')
async def public_claim_coupon(session_id: str, code: str):
    s = _load_session(session_id)
    if not s or not _session_valid(s):
        raise HTTPException(status_code=404)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id,status FROM coupons WHERE code=?", (code,))
    c = cur.fetchone()
    if not c or c['status'] != 'available':
        conn.close()
        raise HTTPException(status_code=404)
    cur.execute("UPDATE coupons SET status='used', used_at=?, used_by_session_id=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), session_id, c['id']))
    conn.commit()
    conn.close()
    return RedirectResponse(url=f'/s/{session_id}/coupon/{code}', status_code=status.HTTP_303_SEE_OTHER)


# ============================================================
# FARM9 BOT CONTROL
# ============================================================

@app.get('/farm9', response_class=HTMLResponse)
async def farm9_page(request: Request):
    """Farm9 bot control page."""
    if not _is_admin(request):
        return RedirectResponse('/login', status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, 'farm9.html', {
        'stats': farm9_controller.get_stats(),
    })


@app.post('/api/farm9/start')
async def farm9_start(request: Request):
    """Start the farm9 bot."""
    if not _is_admin(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    def num(key: str, default, cast, minimum: float = 0):
        """Parse a numeric field, return (value, error)."""
        raw = body.get(key, default)
        if raw is None or raw == '':
            return default, None
        try:
            value = cast(float(raw))
        except (TypeError, ValueError):
            return None, f'Поле {key}: ожидается число'
        if value < minimum:
            return None, f'Поле {key}: значение не может быть меньше {minimum:g}'
        return value, None

    minutes, err = num('minutes', 0.0, float)
    if err:
        return JSONResponse({'error': err}, status_code=400)
    target, err = num('target', 0, int)
    if err:
        return JSONResponse({'error': err}, status_code=400)
    delay, err = num('delay', None, float)
    if err:
        return JSONResponse({'error': err}, status_code=400)
    gift_min, err = num('gift_min', None, int)
    if err:
        return JSONResponse({'error': err}, status_code=400)

    attempt_type = str(body.get('attempt_type') or 'paid')
    if attempt_type not in ('paid', 'free'):
        return JSONResponse({'error': 'Неизвестный тип попытки'}, status_code=400)

    version = str(body.get('version') or 'promo')
    if version not in FARM9_VERSIONS:
        return JSONResponse({'error': f'Неизвестная стратегия: {version}'}, status_code=400)

    raw_accounts = body.get('accounts')
    if raw_accounts is None:
        raw_accounts = [body.get('creds_file') or 'credentials.account5.json']
    if not isinstance(raw_accounts, list):
        return JSONResponse({'error': 'Поле accounts должно быть списком'}, status_code=400)

    accounts: list[str] = []
    for raw in raw_accounts:
        name = str(raw or '').strip()
        if name and name not in accounts:
            accounts.append(name)
    if not accounts:
        return JSONResponse({'error': 'Не выбран ни один аккаунт'}, status_code=400)
    if len(accounts) > 10:
        return JSONResponse({'error': 'Максимум 10 аккаунтов за один запуск'}, status_code=400)

    missing = [name for name in accounts if farm9_resolve_creds(name) is None]
    if missing:
        return JSONResponse(
            {'error': 'Файлы кредов не найдены: ' + ', '.join(missing) +
                      f'. Загрузите JSON в панели (сохраняется в {FARM9_CREDS_DIR}).'},
            status_code=400,
        )

    results = await farm9_controller.start_many(
        accounts,
        minutes=minutes,
        target=target,
        attempt_type=attempt_type,
        version=version,
        delay=delay,
        gift_min=gift_min,
    )
    started = [item for item in results if item.get('started')]
    if not started:
        return JSONResponse(
            {'error': 'Все выбранные аккаунты уже запущены', 'results': results},
            status_code=400,
        )
    return JSONResponse({'ok': True, 'results': results, 'started': len(started)})


@app.post('/api/farm9/stop')
async def farm9_stop(request: Request):
    """Stop one account (creds_file/account in body) or all running accounts."""
    if not _is_admin(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    account = None
    if isinstance(body, dict):
        account = body.get('creds_file') or body.get('account')
    account = str(account).strip() if account else None

    if isinstance(body, dict) and isinstance(body.get('accounts'), list) and body['accounts']:
        names = {str(item).strip() for item in body['accounts'] if str(item).strip()}
        for name in names:
            await farm9_controller.stop(name)
        return JSONResponse({'ok': True, 'stopped': sorted(names)})

    await farm9_controller.stop(account)
    return JSONResponse({'ok': True, 'stopped': account or 'all'})


def _safe_creds_name(raw) -> str | None:
    """Validate a credentials file name (no paths, .json only)."""
    name = str(raw or '').strip()
    if not name or not name.endswith('.json'):
        return None
    if Path(name).name != name or '/' in name or '\\' in name:
        return None
    return name


@app.get('/api/farm9/creds')
async def farm9_creds(request: Request):
    """List credential files for the start form."""
    if not _is_admin(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    creds = farm9_list_creds()
    names = [c['name'] for c in creds]
    default = 'credentials.account5.json' if 'credentials.account5.json' in names else (names[0] if names else '')
    return JSONResponse({
        'creds': creds,
        'default': default,
        'capture_dir': str(FARM9_CAPTURE_DIR),
        'creds_dir': str(FARM9_CREDS_DIR),
    })


@app.post('/api/farm9/creds')
async def farm9_creds_upload(request: Request):
    """Upload a credentials JSON file into the writable credentials folder."""
    if not _is_admin(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'Ожидается JSON'}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({'error': 'Ожидается JSON-объект'}, status_code=400)

    name = _safe_creds_name(body.get('name'))
    if name is None:
        return JSONResponse({'error': 'Некорректное имя файла (нужно *.json без пути)'}, status_code=400)

    content = body.get('content')
    if not isinstance(content, str) or not content.strip():
        return JSONResponse({'error': 'Пустое содержимое файла'}, status_code=400)
    if len(content) > 200_000:
        return JSONResponse({'error': 'Файл слишком большой (лимит 200 КБ)'}, status_code=400)
    try:
        data = json.loads(content)
    except ValueError:
        return JSONResponse({'error': 'Содержимое не является корректным JSON'}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({'error': 'В JSON должен быть объект'}, status_code=400)
    if not data.get('user_id') or not (data.get('hash') or data.get('token')):
        return JSONResponse({'error': 'В файле должны быть поля user_id и hash'}, status_code=400)

    try:
        FARM9_CREDS_DIR.mkdir(parents=True, exist_ok=True)
        target = FARM9_CREDS_DIR / name
        tmp = target.with_name(target.name + '.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
        os.replace(tmp, target)
    except OSError as exc:
        return JSONResponse({'error': f'Не удалось сохранить файл: {exc}'}, status_code=500)

    return JSONResponse({'ok': True, 'name': name})


@app.post('/api/farm9/creds/delete')
async def farm9_creds_delete(request: Request):
    """Delete an uploaded credentials file (files from capture/ are read-only)."""
    if not _is_admin(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = _safe_creds_name((body or {}).get('name'))
    if name is None:
        return JSONResponse({'error': 'Некорректное имя файла'}, status_code=400)
    target = FARM9_CREDS_DIR / name
    if not target.is_file():
        return JSONResponse({'error': f'Файл не найден: {name}'}, status_code=404)
    try:
        target.unlink()
    except OSError as exc:
        return JSONResponse({'error': f'Не удалось удалить файл: {exc}'}, status_code=500)
    return JSONResponse({'ok': True})


@app.get('/api/farm9/stats')
async def farm9_stats(request: Request, account: str | None = None):
    """Get aggregate stats for all accounts (or one account)."""
    if not _is_admin(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    stats = farm9_controller.get_stats(account) if account else farm9_controller.get_stats()
    if stats is None:
        return JSONResponse({'error': f'Аккаунт не запускался: {account}'}, status_code=404)
    return JSONResponse(stats)


@app.get('/api/farm9/logs')
async def farm9_logs(request: Request, since: int = 0, account: str | None = None):
    """Get farm9 logs since index, optionally filtered by account."""
    if not _is_admin(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    logs = farm9_controller.get_logs(since, account)
    return JSONResponse({'logs': logs, 'total': farm9_controller.log_total})
