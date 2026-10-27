"""Farm9 bot control module for web interface."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool

HERE = Path(__file__).parent
CAPTURE_DIR = HERE.parent / 'capture'

# Add capture to path for imports
sys.path.insert(0, str(CAPTURE_DIR))

import farm9_lib
from farm9_lib import VERSIONS, log as farm9_log, main as farm9_main


class Farm9Controller:
    """Controls the farm9 bot with real-time logging and stats."""
    
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._log_queue: asyncio.Queue[str] = asyncio.Queue()
        self._stats: dict[str, Any] = {
            'running': False,
            'started_at': None,
            'duration_min': 0,
            'target_balance': 0,
            'rounds': 0,
            'played': 0,
            'skipped': 0,
            'coupons': 0,
            'coins': 0,
            'balance_start': 0,
            'balance': 0,
            'coupon_stats': {},  # discount -> count
            'last_10min_stats': {},
            'errors': 0,
            'wins': 0,
        }
        self._log_buffer: list[str] = []
        self._max_log_lines = 500
        # Monotonic counters so log polling survives buffer trimming/clearing
        self._log_total = 0
        self._log_dropped = 0
    
    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()
    
    def _add_log(self, msg: str) -> None:
        """Add log message to buffer and queue."""
        timestamp = datetime.now().strftime('%H:%M:%S')
        line = f"[{timestamp}] {msg}"
        self._log_buffer.append(line)
        self._log_total += 1
        if len(self._log_buffer) > self._max_log_lines:
            drop = len(self._log_buffer) - self._max_log_lines
            self._log_buffer = self._log_buffer[drop:]
            self._log_dropped += drop
        try:
            self._log_queue.put_nowait(line)
        except asyncio.QueueFull:
            pass
    
    def _update_stats(self, key: str, value: Any) -> None:
        self._stats[key] = value
    
    def _update_coupon_stats(self, discount: int) -> None:
        """Track coupon by discount percentage."""
        self._stats['coupon_stats'][discount] = self._stats['coupon_stats'].get(discount, 0) + 1
        self._stats['last_10min_stats'][discount] = self._stats['last_10min_stats'].get(discount, 0) + 1
    
    def _format_10min_stats(self) -> str:
        """Format coupon stats as '70% - 2 шт, 50% - 5 шт'."""
        if not self._stats['last_10min_stats']:
            return "нет данных"
        parts = []
        for disc in sorted(self._stats['last_10min_stats'].keys(), reverse=True):
            count = self._stats['last_10min_stats'][disc]
            parts.append(f"{disc}% - {count} шт")
        return ", ".join(parts)
    
    def _reset_10min_stats(self) -> None:
        self._stats['last_10min_stats'] = {}
    
    async def start(self, minutes: float = 0, target: int = 0, attempt_type: str = 'paid', 
                    version: str = 'promo', creds_file: str = 'credentials.account5.json',
                    delay: float | None = None, gift_min: int | None = None) -> None:
        """Start the farm9 bot."""
        if self.is_running:
            return
        
        self._stop_event.clear()
        self._log_buffer.clear()
        # Keep counters monotonic: everything logged so far counts as dropped
        self._log_dropped = self._log_total
        self._stats = {
            'running': True,
            'started_at': datetime.now().isoformat(),
            'duration_min': minutes,
            'target_balance': target,
            'rounds': 0,
            'played': 0,
            'skipped': 0,
            'coupons': 0,
            'coins': 0,
            'wins': 0,
            'balance_start': 0,
            'balance': 0,
            'coupon_stats': {},
            'last_10min_stats': {},
            'errors': 0,
        }
        
        self._task = asyncio.create_task(
            self._run_bot(minutes, target, attempt_type, version, creds_file, delay, gift_min)
        )
    
    async def stop(self) -> None:
        """Stop the farm9 bot."""
        if not self.is_running:
            return
        self._stop_event.set()
        self._add_log("Получена команда остановки...")
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._stats['running'] = False
        self._add_log("Бот остановлен")
    
    async def _run_bot(self, minutes: float, target: int, attempt_type: str, 
                       version: str, creds_file: str, delay: float | None, gift_min: int | None) -> None:
        """Run the farm9 bot in a thread pool with custom logging."""
        
        # We need to monkey-patch the farm9_lib.log function to capture logs
        original_log = farm9_lib.log
        logfile = CAPTURE_DIR / f'farm9_{version}.log'
        
        def web_log(msg: str, logfile_param: Path | None = None) -> None:
            # Call original to write to file
            original_log(msg, logfile_param)
            # Also send to web
            self._add_log(msg)
        
        farm9_lib.log = web_log
        
        # Also patch collect_new_rewards to track coupons by discount
        original_collect = farm9_lib.collect_new_rewards
        
        def web_collect(token: str, known: set[str]) -> tuple[int, set[str]]:
            coins, new_known = original_collect(token, known)
            # Note: The actual coupon discount info would come from the reward data
            # For now, we track when coupons are collected
            return coins, new_known
        
        farm9_lib.collect_new_rewards = web_collect
        
        # Patch decide to track coupon stats
        original_decide = farm9_lib.decide
        
        def web_decide(strategy: str, sess: dict, cfg: dict, attempt_type: str,
                       gift_min: int, booster_price: int, balance: int) -> dict:
            result = original_decide(strategy, sess, cfg, attempt_type, gift_min, booster_price, balance)
            # Track promo coupons seen
            if result.get('promo') is not None:
                self._stats['promo_seen'] = self._stats.get('promo_seen', 0) + 1
            if result.get('coupon'):
                # We'll track when actually collected
                pass
            return result
        
        farm9_lib.decide = web_decide
        
        try:
            # Run the main function with our parameters
            # We need to simulate command line args
            class Args:
                def __init__(self):
                    self.creds = str(CAPTURE_DIR / creds_file)
                    self.minutes = minutes
                    self.target = target
                    self.status = False
                    self.attempt_type = attempt_type
                    self.delay = delay
                    self.gift_min = gift_min
                    self.booster_price = None
                    self.json_out = ''
                    self.log = str(logfile)
            
            args = Args()
            
            # We'll run a modified version of main that checks _stop_event
            await self._run_main_loop(args, version)
            
        except Exception as e:
            self._add_log(f"Ошибка: {e}")
        finally:
            farm9_lib.log = original_log
            farm9_lib.collect_new_rewards = original_collect
            farm9_lib.decide = original_decide
            self._stats['running'] = False
    
    async def _run_main_loop(self, args, version: str) -> None:
        """Modified main loop that respects stop event and provides 10-min stats."""
        import play
        
        defaults = VERSIONS[version]
        delay = defaults['delay'] if args.delay is None else args.delay
        gift_min = defaults['gift_min'] if args.gift_min is None else args.gift_min
        
        creds_path = Path(args.creds)
        if not creds_path.is_absolute():
            creds_path = CAPTURE_DIR / creds_path
        creds = json.loads(creds_path.read_text(encoding='utf-8'))
        
        token = await run_in_threadpool(play.get_token, creds)
        cfg = await run_in_threadpool(play.get_config, token)
        booster_price = int(args.booster_price or cfg.get('booster_price') or 100)
        balance = await run_in_threadpool(farm9_lib.get_balance, token)
        start_balance = balance
        
        self._stats['balance_start'] = start_balance
        self._stats['balance'] = balance
        
        self._add_log(f"Старт {defaults['title']}, аккаунт {creds.get('user_id')}, баланс={balance}, режим={args.attempt_type}")
        
        await run_in_threadpool(farm9_lib.stop_active_sessions, token)
        
        known: set[str] = set()
        try:
            known = {r['reward_id'] for r in (await run_in_threadpool(
                lambda: play.api('GET', '/game/rewards?status=pending', token).json()
            )) if r.get('reward_id')}
        except Exception:
            pass
        
        deadline = time.monotonic() + args.minutes * 60 if args.minutes else None
        started = time.monotonic()
        last_10min_log = time.monotonic()
        
        while not self._stop_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                self._add_log("Время вышло")
                break
            if args.target and balance >= args.target:
                self._add_log(f"ЦЕЛЬ ДОСТИГНУТА: баланс {balance} >= {args.target}")
                break
            
            # Every 10 minutes, log coupon stats
            if time.monotonic() - last_10min_log >= 600:  # 10 minutes
                stats_msg = self._format_10min_stats()
                if stats_msg != "нет данных":
                    self._add_log(f"📊 За 10 мин: {stats_msg}")
                self._reset_10min_stats()
                last_10min_log = time.monotonic()
            
            try:
                att = await run_in_threadpool(lambda: play.api('GET', '/users/daily-attempt', token).json())
                if args.attempt_type == 'free':
                    available = att.get('free_attempt_available')
                    remaining = att.get('free_attempts_remaining', 0)
                else:
                    available = att.get('paid_attempt_available')
                    remaining = att.get('paid_attempts_remaining')
                if not available or remaining == 0:
                    if args.attempt_type == 'free':
                        token = await self._wait_for_free_attempt(token, creds)
                        continue
                    self._add_log("Платные попытки недоступны — стоп")
                    break
                
                sess = await run_in_threadpool(farm9_lib.start_attempt, token, args.attempt_type)
            except Exception as e:
                self._stats['errors'] += 1
                self._add_log(f"Ошибка старта: {e}, перелогин")
                try:
                    token = await run_in_threadpool(play.get_token, creds)
                    cfg = await run_in_threadpool(play.get_config, token)
                    balance = await run_in_threadpool(farm9_lib.get_balance, token)
                    self._stats['balance'] = balance
                except Exception as e2:
                    self._add_log(f"Перелогин не удался: {e2}")
                    await asyncio.sleep(5)
                continue
            
            sid = sess['session_id']
            decision = farm9_lib.decide(defaults['strategy'], sess, cfg, args.attempt_type,
                                        gift_min, booster_price, balance)
            self._stats['rounds'] += 1
            if decision['promo'] is not None:
                self._stats['promo_seen'] = self._stats.get('promo_seen', 0) + 1
            if decision.get('reachable'):
                self._stats['promo_reachable'] = self._stats.get('promo_reachable', 0) + 1
            if decision['skip']:
                self._stats['skipped'] += 1
                self._add_log(f"Раунд {self._stats['rounds']}: пропуск (промо={decision.get('promo')})")
            else:
                self._stats['played'] += 1
                self._add_log(f"Раунд {self._stats['rounds']}: цель={decision['target']}, промо={decision.get('promo')}")
            
            try:
                stop = await run_in_threadpool(farm9_lib.execute_round, token, sid, decision)
            except Exception as e:
                self._stats['errors'] += 1
                self._add_log(f"Ошибка раунда: {e}, перелогин")
                try:
                    token = await run_in_threadpool(play.get_token, creds)
                except Exception:
                    pass
                continue
            
            if stop.get('status') == 'win_finished':
                self._stats['wins'] = self._stats.get('wins', 0) + 1
                coins, known = await run_in_threadpool(farm9_lib.collect_new_rewards, token, known)
                self._stats['coins'] += coins
                balance += coins
                self._stats['balance'] = balance
                self._add_log(f"Победа! +{coins} монет, баланс={balance}")
            
            # Track coupon by checking if promo was collected
            if decision['coupon'] and stop.get('status') == 'win_finished':
                self._stats['coupons'] += 1
                # We don't know the exact discount from the session, but we can try to infer
                # For now, let's use a placeholder - in reality we'd need to parse the reward
                promo_click = decision.get('promo')
                if promo_click:
                    # Estimate discount based on promo position (this is approximate)
                    # In reality, we'd need to check the reward details
                    estimated_discount = 70 if promo_click <= 30 else 50  # rough estimate
                    self._update_coupon_stats(estimated_discount)
                    self._add_log(f"🎁 Купон получен! (примерно {estimated_discount}%)")
            
            if self._stats['rounds'] % 10 == 0:
                self._add_log(f"Статистика: раундов={self._stats['rounds']} played={self._stats['played']} "
                              f"skipped={self._stats['skipped']} купонов={self._stats['coupons']} "
                              f"монет={self._stats['coins']} баланс={balance}")
            
            if delay:
                await asyncio.sleep(delay)
        
        # Final balance
        balance = await run_in_threadpool(farm9_lib.get_balance, token)
        self._stats['balance'] = balance
        self._stats['duration_sec'] = round(time.monotonic() - started, 1)
        
        self._add_log(f"ИТОГО: раундов={self._stats['rounds']} played={self._stats['played']} "
                      f"skipped={self._stats['skipped']} купонов={self._stats['coupons']} "
                      f"монет={self._stats['coins']} баланс={start_balance}->{balance} "
                      f"за {self._stats['duration_sec']}с")
    
    async def _wait_for_free_attempt(self, token: str, creds: dict) -> str:
        """Wait for free attempt to become available."""
        from farm9_lib import seconds_until_next_utc_day
        wait = seconds_until_next_utc_day()
        self._add_log(f"Бесплатных попыток нет — ждём сброса ~{int(wait // 60)} мин (до нового дня UTC)")
        while not self._stop_event.is_set():
            chunk = min(600.0, wait)
            await asyncio.sleep(chunk)
            wait -= chunk
            try:
                att = await run_in_threadpool(lambda: play.api('GET', '/users/daily-attempt', token).json())
            except Exception:
                token = await run_in_threadpool(play.get_token, creds)
                continue
            if att.get('free_attempt_available') and (att.get('free_attempts_remaining') or 0) != 0:
                self._add_log(f"Бесплатные попытки вернулись: {att.get('free_attempts_remaining')}")
                return token
            if wait <= 0:
                wait = 60.0
        return token
    
    def get_logs(self, since: int = 0) -> list[str]:
        """Get logs since monotonic index."""
        start = max(0, since - self._log_dropped)
        return self._log_buffer[start:]

    @property
    def log_total(self) -> int:
        """Total number of log lines ever added (monotonic)."""
        return self._log_total

    def get_stats(self) -> dict:
        """Get current stats."""
        stats = dict(self._stats)
        stats['running'] = self.is_running
        stats['logs_count'] = self._log_total
        stats['log_total'] = self._log_total
        stats.setdefault('wins', 0)
        # Elapsed time of the current run (frozen after the run stops)
        elapsed = 0
        started = stats.get('started_at')
        if started and self.is_running:
            try:
                elapsed = max(0, int((datetime.now() - datetime.fromisoformat(started)).total_seconds()))
            except (TypeError, ValueError):
                elapsed = 0
        elif stats.get('duration_sec') is not None:
            try:
                elapsed = max(0, int(stats.get('duration_sec') or 0))
            except (TypeError, ValueError):
                elapsed = 0
        stats['elapsed_sec'] = elapsed
        # Add formatted 10-min stats
        stats['formatted_10min'] = self._format_10min_stats()
        # Add total coupon stats formatted
        if stats['coupon_stats']:
            parts = [f"{d}% - {c} шт" for d, c in sorted(stats['coupon_stats'].items(), reverse=True)]
            stats['formatted_total'] = ", ".join(parts)
        else:
            stats['formatted_total'] = "нет купонов"
        return stats


# Global controller instance
farm9_controller = Farm9Controller()