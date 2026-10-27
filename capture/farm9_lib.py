"""Общая логика экспериментальных farm9-версий (охота за купонами)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import play

HERE = Path(__file__).parent

VERSIONS = {
    'baseline': {
        'title': 'v1 baseline: каждая сессия до безопасного максимума, gift_min=150, пауза 5с',
        'strategy': 'baseline',
        'delay': 5.0,
        'gift_min': 150,
    },
    'max': {
        'title': 'v2 max: каждая сессия до второго проигрыша (бустер), пауза 0с',
        'strategy': 'max',
        'delay': 0.0,
        'gift_min': 150,
    },
    'promo': {
        'title': 'v3 promo: пропуск безнадёжных сессий, стоп ровно на промо-клике, пауза 0с',
        'strategy': 'promo',
        'delay': 0.0,
        'gift_min': 150,
    },
}


def log(msg: str, logfile: Path | None = None) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    if logfile:
        with open(logfile, 'a', encoding='utf-8') as f:
            f.write(line + '\n')


def get_balance(token: str) -> int:
    return play.api('GET', '/users/profile', token).json().get('balance') or 0


def stop_active_sessions(token: str) -> int:
    try:
        data = play.api('GET', '/game/sessions', token).json()
    except Exception:
        return 0
    sessions = data if isinstance(data, list) else (data.get('sessions') or [])
    stopped = 0
    for s in sessions:
        sid = s.get('session_id') or s.get('id')
        if not sid:
            continue
        try:
            click_count = max(1, int(s.get('click_count') or 1))
            play.api('POST', f'/game/sessions/{sid}/stop', token,
                     json={'click_count': click_count})
            stopped += 1
        except Exception:
            pass
    return stopped


def seconds_until_next_utc_day() -> float:
    now = datetime.now(timezone.utc)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(30.0, (nxt - now).total_seconds())


def wait_for_free_attempt(token: str, creds: dict, logfile: Path | None = None) -> str:
    wait = seconds_until_next_utc_day()
    log(f'бесплатных попыток нет — ждём сброса ~{int(wait // 60)} мин (до нового дня UTC)', logfile)
    while True:
        chunk = min(600.0, wait)
        time.sleep(chunk)
        wait -= chunk
        try:
            att = play.api('GET', '/users/daily-attempt', token).json()
        except Exception:
            token = play.get_token(creds)
            continue
        if att.get('free_attempt_available') and (att.get('free_attempts_remaining') or 0) != 0:
            log(f"бесплатные попытки вернулись: {att.get('free_attempts_remaining')}", logfile)
            return token
        if wait <= 0:
            wait = 60.0


def start_attempt(token: str, attempt_type: str) -> dict:
    try:
        return play.api('POST', '/game/attempts', token, json={'attempt_type': attempt_type}).json()
    except Exception as e:
        if 'active_session_exists' in str(e) or '409' in str(e):
            stop_active_sessions(token)
            return play.api('POST', '/game/attempts', token, json={'attempt_type': attempt_type}).json()
        raise


def collect_new_rewards(token: str, known: set[str]) -> tuple[int, set[str]]:
    try:
        pending = play.api('GET', '/game/rewards?status=pending', token).json()
    except Exception:
        return 0, known
    coins = 0
    for r in pending:
        rid = r.get('reward_id')
        if not rid or rid in known:
            continue
        try:
            res = play.api('POST', '/game/rewards/collect', token,
                           params={'reward_id': rid}).json()
            coins += res.get('coins') or r.get('coins') or 0
        except Exception:
            pass
        known.add(rid)
    return coins, known


def _score(cfg: dict, attempt_type: str):
    return play.score_fn_for(cfg, attempt_type)


def decide(strategy: str, sess: dict, cfg: dict, attempt_type: str,
           gift_min: int, booster_price: int, balance: int) -> dict:
    losses = sorted(sess.get('loss_click_numbers') or [])
    promos = sorted(sess.get('promo_click_numbers') or [])
    promo = promos[0] if promos else None
    max_taps = int(sess.get('max_taps') or 120)
    first = losses[0] if losses else None
    second = losses[1] if len(losses) > 1 else None
    safe0 = min(max_taps, first - 1) if first is not None else max_taps
    safe1 = min(max_taps, second - 1) if second is not None else max_taps
    booster_ok = (int(sess.get('booster_remaining') or 0) > 0
                  and balance >= booster_price)
    score = _score(cfg, attempt_type)

    def result(target: int | None, use_booster: bool = False, boost_at: int | None = None):
        if not target or target < 1:
            return {'skip': True, 'promo': promo, 'target': 0,
                    'use_booster': False, 'boost_at': None, 'coupon': False}
        return {
            'skip': False,
            'promo': promo,
            'target': int(target),
            'use_booster': bool(use_booster and boost_at),
            'boost_at': boost_at,
            'coupon': promo is not None and target >= promo,
            'reachable': promo is not None and target >= promo,
        }

    if strategy == 'promo':
        if promo is None:
            return result(None)
        if first is None and promo <= max_taps:
            return result(promo)
        if first is not None and promo < first:
            return result(promo)
        if booster_ok and first is not None and first < promo < safe1 + 1:
            return result(promo, True, first)
        return result(None)

    if strategy == 'max':
        if first is None:
            return result(max_taps, False, None)
        if booster_ok and safe1 > safe0:
            return result(safe1, True, first)
        return result(safe0)

    def gift_ok(t: int) -> bool:
        return t > 0 and (score(t) >= gift_min or (promo is not None and t >= promo))

    if first is None:
        return result(max_taps, False, None)
    t0, t1 = safe0, (safe1 if booster_ok else -1)
    if t1 > 0 and (gift_ok(t1) and not gift_ok(t0) or score(t1) - booster_price > score(t0)):
        return result(t1, True, first)
    return result(t0)


def execute_round(token: str, sid: str, decision: dict) -> dict:
    target = 1 if decision['skip'] else decision['target']
    boost_at = decision.get('boost_at')
    pre_limit = (boost_at - 1) if boost_at else target
    pre_limit = max(1, pre_limit)
    steps = [s for s in (1, max(1, target // 2), pre_limit) if 1 <= s <= pre_limit]
    for step in dict.fromkeys(steps):
        play.api('PATCH', f'/game/sessions/{sid}', token, json={'click_count': step})
    if boost_at:
        play.api('POST', f'/game/sessions/{sid}/booster', token,
                 json={'click_count': boost_at})
    if target > pre_limit:
        play.api('PATCH', f'/game/sessions/{sid}', token, json={'click_count': target})
    return play.api('POST', f'/game/sessions/{sid}/stop', token,
                    json={'click_count': max(1, target)}).json()


def main(version: str, default_creds: Path | str | None = None,
         default_log: Path | str | None = None) -> None:
    defaults = VERSIONS[version]
    parser = argparse.ArgumentParser(description=defaults['title'])
    parser.add_argument('--creds', default=str(default_creds or (HERE / 'credentials.account5.json')))
    parser.add_argument('--minutes', type=float, default=0, help='остановиться через N минут')
    parser.add_argument('--target', type=int, default=0, help='целевой баланс')
    parser.add_argument('--status', action='store_true', help='показать баланс и выйти')
    parser.add_argument('--attempt-type', choices=('free', 'paid'), default='paid')
    parser.add_argument('--delay', type=float, default=None)
    parser.add_argument('--gift-min', type=int, default=None)
    parser.add_argument('--booster-price', type=int, default=None)
    parser.add_argument('--json-out', default='')
    parser.add_argument('--log', default='')
    args = parser.parse_args()

    delay = defaults['delay'] if args.delay is None else args.delay
    gift_min = defaults['gift_min'] if args.gift_min is None else args.gift_min
    logfile = Path(args.log) if args.log else Path(default_log or (HERE / f'farm9_{version}.log'))

    creds_path = Path(args.creds)
    if not creds_path.is_absolute():
        creds_path = HERE / creds_path
    creds = json.loads(creds_path.read_text(encoding='utf-8'))

    token = play.get_token(creds)
    cfg = play.get_config(token)
    booster_price = int(args.booster_price or cfg.get('booster_price') or 100)
    balance = get_balance(token)
    start_balance = balance
    if args.status:
        log(f'баланс={balance}', logfile)
        return

    known: set[str] = set()
    try:
        known = {r['reward_id'] for r in play.api('GET', '/game/rewards?status=pending', token).json()
                 if r.get('reward_id')}
    except Exception:
        pass

    stats = {
        'version': version,
        'account': creds.get('user_id'),
        'attempt_type': args.attempt_type,
        'strategy': defaults['strategy'],
        'delay': delay,
        'gift_min': gift_min,
        'rounds': 0,
        'played': 0,
        'skipped': 0,
        'wins': 0,
        'promo_seen': 0,
        'promo_reachable': 0,
        'coupons': 0,
        'coins': 0,
        'balance_start': start_balance,
        'balance': balance,
        'errors': 0,
        'duration_sec': 0.0,
    }

    log(f"старт {defaults['title']}, аккаунт {creds.get('user_id')}, "
        f"баланс={balance}, режим={args.attempt_type}", logfile)
    stop_active_sessions(token)
    deadline = time.monotonic() + args.minutes * 60 if args.minutes else None
    started = time.monotonic()

    while True:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if args.target and balance >= args.target:
            log(f'ЦЕЛЬ ДОСТИГНУТА: баланс {balance} >= {args.target}', logfile)
            break

        try:
            att = play.api('GET', '/users/daily-attempt', token).json()
            if args.attempt_type == 'free':
                available = att.get('free_attempt_available')
                remaining = att.get('free_attempts_remaining', 0)
            else:
                available = att.get('paid_attempt_available')
                remaining = att.get('paid_attempts_remaining')
            if not available or remaining == 0:
                if args.attempt_type == 'free':
                    token = wait_for_free_attempt(token, creds, logfile)
                    continue
                log('платные попытки недоступны — стоп', logfile)
                break

            sess = start_attempt(token, args.attempt_type)
        except Exception as e:
            stats['errors'] += 1
            log(f'ошибка старта: {e}, перелогин', logfile)
            try:
                token = play.get_token(creds)
                cfg = play.get_config(token)
                balance = get_balance(token)
            except Exception as e2:
                log(f'перелогин не удался: {e2}', logfile)
                time.sleep(5)
            continue

        sid = sess['session_id']
        decision = decide(defaults['strategy'], sess, cfg, args.attempt_type,
                          gift_min, booster_price, balance)
        stats['rounds'] += 1
        if decision['promo'] is not None:
            stats['promo_seen'] += 1
        if decision.get('reachable'):
            stats['promo_reachable'] += 1
        if decision['skip']:
            stats['skipped'] += 1
        else:
            stats['played'] += 1

        try:
            stop = execute_round(token, sid, decision)
        except Exception as e:
            stats['errors'] += 1
            log(f'ошибка раунда: {e}, перелогин', logfile)
            try:
                token = play.get_token(creds)
            except Exception:
                pass
            continue

        if stop.get('status') == 'win_finished':
            stats['wins'] += 1
            coins, known = collect_new_rewards(token, known)
            stats['coins'] += coins
            balance += coins
        if decision['coupon'] and stop.get('status') == 'win_finished':
            stats['coupons'] += 1

        if stats['rounds'] % 10 == 0:
            log(f"раундов={stats['rounds']} played={stats['played']} skipped={stats['skipped']} "
                f"купонов={stats['coupons']} монет={stats['coins']} баланс={balance}", logfile)

        if delay:
            time.sleep(delay)

    stats['balance'] = get_balance(token)
    stats['duration_sec'] = round(time.monotonic() - started, 1)
    stats['coupon_per_min'] = round(stats['coupons'] / (stats['duration_sec'] / 60), 3) if stats['duration_sec'] else 0
    stats['round_per_min'] = round(stats['rounds'] / (stats['duration_sec'] / 60), 2) if stats['duration_sec'] else 0
    log(f"ИТОГО v={version}: раундов={stats['rounds']} played={stats['played']} "
        f"skipped={stats['skipped']} промо={stats['promo_seen']} "
        f"купонов={stats['coupons']} ({stats['coupon_per_min']}/мин) "
        f"монет={stats['coins']} баланс={start_balance}->{stats['balance']} "
        f"за {stats['duration_sec']}с", logfile)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(stats, ensure_ascii=False, indent=1) + '\n',
                                       encoding='utf-8')
    print('STATS ' + json.dumps(stats, ensure_ascii=False))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in VERSIONS else 'baseline')
