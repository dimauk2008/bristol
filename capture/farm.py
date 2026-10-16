"""Фарм до заданного баланса, запускается отдельно от сервера.

Использование:
  python farm.py 200000            играть, пока баланс не станет >= 200000
  python farm.py 200000 --delay 5  пауза между раундами, сек
  python farm.py --status          только показать текущий баланс

Лог пишется в farm.log рядом со скриптом и в консоль.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import play

HERE = Path(__file__).parent
LOG = HERE / "farm.log"


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_balance(token: str) -> int:
    return play.api("GET", "/users/profile", token).json().get("balance") or 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", nargs="?", type=int, help="баланс, до которого играть")
    parser.add_argument("--status", action="store_true", help="показать баланс и выйти")
    parser.add_argument("--delay", type=float, default=2.0, help="пауза между раундами, сек")
    args = parser.parse_args()

    creds = json.loads((HERE / "credentials.json").read_text(encoding="utf-8"))
    token = play.get_token(creds)
    log(f"старт, баланс={get_balance(token)}, цель={args.target if args.target else '-'}")

    if args.status:
        return
    if not args.target:
        print("укажи баланс: python farm.py 200000")
        sys.exit(1)

    attempt_type = "free"
    while True:
        try:
            balance = get_balance(token)
        except Exception as e:
            log(f"ошибка профиля: {e}, перелогин")
            try:
                token = play.get_token(creds)
                balance = get_balance(token)
            except Exception as e2:
                log(f"перелогин не удался: {e2}")
                time.sleep(5)
                continue

        if balance >= args.target:
            log(f"ЦЕЛЬ ДОСТИГНУТА: баланс {balance} >= {args.target}")
            break

        try:
            res = play.play_one(token, attempt_type=attempt_type)
        except Exception as e:
            log(f"ошибка раунда: {e}, перелогин")
            try:
                token = play.get_token(creds)
                continue
            except Exception as e2:
                log(f"перелогин не удался: {e2}")
                time.sleep(5)
                continue

        reason = res.get("reason")
        if reason == "no_free_attempts":
            log("бесплатных попыток нет -> платные")
            attempt_type = "paid"
            continue
        if reason:
            log(f"стоп: {reason} (баланс {res.get('balance')})")
            break

        log(
            f"победа, монеты +{res.get('collected') or 0}, "
            f"баланс {res.get('balance_before')} -> {res.get('balance')} "
            f"(цель {args.target}), осталось попыток {res.get('remaining_today')}"
        )
        time.sleep(args.delay)


if __name__ == "__main__":
    main()
