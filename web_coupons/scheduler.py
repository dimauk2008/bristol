"""Фоновое автообновление токенов Bristol.

Держит refresh-цепочку живой без кнопки «Синхронизировать»:
- каждые BRISTOL_KEEPALIVE_MIN минут принудительно обновляет refresh-токен;
- каждые BRISTOL_AUTO_SYNC_MIN минут синхронизирует купоны и коды карты.
Оба цикла переживают ошибки сети и продолжают работать.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from starlette.concurrency import run_in_threadpool

import bristol

log = logging.getLogger('auto_refresh')

_state: dict = {
    'enabled': False,
    'started_at': None,
    'last_refresh': None,
    'last_sync': None,
    'last_error': '',
    'keepalive_min': 0.0,
    'sync_min': 0.0,
}
_tasks: list[asyncio.Task] = []


def _minutes(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return default


def _enabled() -> bool:
    value = (os.environ.get('BRISTOL_AUTO_REFRESH') or '1').strip().lower()
    return value not in ('0', 'false', 'no', 'off')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _accounts():
    return [a for a in bristol.accounts_list() if a.get('has_token')]


def _refresh_all() -> None:
    errors = []
    for account in _accounts():
        aid = account['id']
        try:
            changed = bristol.keepalive(aid, interval_hours=_state['keepalive_min'] / 60.0)
            if changed:
                log.info('аккаунт %s: refresh-токен продлён', aid)
        except Exception as e:
            log.warning('аккаунт %s: не удалось обновить токен: %s', aid, e)
            try:
                bristol.set_last_error(aid, f'Автообновление токена: {e}')
            except Exception:
                pass
            errors.append(f'#{aid}: {e}')
    _state['last_refresh'] = _now()
    if errors:
        raise RuntimeError('; '.join(errors))


def _sync_all() -> None:
    errors = []
    for account in _accounts():
        aid = account['id']
        try:
            bristol.sync(aid)
            log.info('аккаунт %s: купоны синхронизированы', aid)
        except Exception as e:
            log.warning('аккаунт %s: синхронизация не удалась: %s', aid, e)
            try:
                bristol.set_last_error(aid, f'Автосинхронизация: {e}')
            except Exception:
                pass
            errors.append(f'#{aid}: {e}')
    _state['last_sync'] = _now()
    if errors:
        raise RuntimeError('; '.join(errors))


async def _loop(name: str, func, interval_sec: float, first_delay: float) -> None:
    await asyncio.sleep(first_delay)
    while True:
        try:
            await run_in_threadpool(func)
            if name == 'refresh':
                _state['last_error'] = ''
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _state['last_error'] = str(e)
            log.warning('%s: %s', name, e)
        await asyncio.sleep(interval_sec)


async def start() -> None:
    if not _enabled():
        log.info('автообновление отключено (BRISTOL_AUTO_REFRESH=0)')
        return
    keepalive_min = _minutes('BRISTOL_KEEPALIVE_MIN', 360)
    sync_min = _minutes('BRISTOL_AUTO_SYNC_MIN', 360)
    _state.update(enabled=True, started_at=_now(),
                  keepalive_min=keepalive_min, sync_min=sync_min)
    if keepalive_min > 0:
        _tasks.append(asyncio.create_task(
            _loop('refresh', _refresh_all, keepalive_min * 60, 10)))
    if sync_min > 0:
        _tasks.append(asyncio.create_task(
            _loop('sync', _sync_all, sync_min * 60, 45)))
    log.info('автообновление запущено: токен каждые %s мин, синхронизация каждые %s мин',
             keepalive_min, sync_min)


async def stop() -> None:
    for task in _tasks:
        task.cancel()
    if _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()


def status() -> dict:
    return dict(_state)
