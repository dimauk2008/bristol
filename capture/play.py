"""Сбор призов в игре collect-the-bag: гарантированный выигрыш.

Использование:
  python play.py --once      один бесплатный раунд (по умолчанию)
  python play.py --all       все оставшиеся бесплатные попытки за день
  python play.py --once --dry-run   только показать, что будет сделано
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import requests

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
CREDS = HERE / "credentials.json"
API = "https://collect-the-bag-api.prisma-games.ru/api/v1"
GAME = "https://collect-the-bag.prisma-games.ru"

BASE_HEADERS = {
    "origin": GAME,
    "referer": GAME + "/",
    "content-type": "application/json",
    "accept": "application/json, text/plain, */*",
}


def api(method: str, path: str, token: str, **kw) -> requests.Response:
    headers = dict(BASE_HEADERS)
    if token:
        headers["authorization"] = f"Bearer {token}"
    resp = requests.request(method, f"{API}{path}", headers=headers, timeout=30, **kw)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
    return resp


def get_token(creds: dict) -> str:
    resp = api("POST", "/auth/token", "", json={"user_id": creds["user_id"], "hash": creds["hash"]})
    return resp.json()["access_token"]


def get_config(token: str) -> dict:
    return api("GET", "/config", token).json()


def score_fn_for(config: dict, attempt_type: str):
    arr = config.get("attempt_types", {}).get(attempt_type, {}).get("score_by_tap") or []

    def score(click: int) -> int:
        if click <= 0 or not arr:
            return 0
        return arr[min(click, len(arr)) - 1]

    return score


def plan_target(losses: list[int], max_taps: int, booster_available: bool,
                gift: bool, score, promo: int | None = None,
                booster_price: int = 100, gift_min: int = 200
                ) -> tuple[int, bool, int | None]:
    """Цель = максимум безопасных тапов.

    Первый белок = проигрыш, бустер (1 за попытку, 100 монет) его переживает.
    Бустер берём только если: без него не дотянуть до подарка, либо он
    окупается (рост награды > цены бустера).
    """
    losses = sorted(losses)
    t0 = max(1, min(losses[0] - 1 if losses else max_taps, max_taps))
    rest = losses[1:]
    t1 = max(1, min((rest[0] - 1) if rest else max_taps, max_taps)) if (
        booster_available and losses) else -1

    def gift_ok(t: int) -> bool:
        if t <= 0:
            return False
        if score(t) >= gift_min:
            return True
        return promo is not None and t >= promo

    if t1 > 0:
        if gift and gift_ok(t1) and not gift_ok(t0):
            return t1, True, losses[0]
        if score(t1) - booster_price > score(t0):
            return t1, True, losses[0]
    return t0, False, None


def play_one(token: str, dry: bool = False, use_booster: bool = True,
             attempt_type: str = "free", gift: bool = True,
             booster_price: int = 100, gift_min: int = 200) -> dict:
    att = api("GET", "/users/daily-attempt", token).json()
    if attempt_type == "free":
        available = att.get("free_attempt_available")
        remaining = att.get("free_attempts_remaining", 0)
        reason = "no_free_attempts"
    else:
        available = att.get("paid_attempt_available")
        remaining = att.get("paid_attempts_remaining")
        reason = "no_paid_attempts"

    if not available or remaining == 0:
        return {"ok": False, "reason": reason, "state": att}

    if dry:
        return {"ok": True, "dry": True, "attempt_type": attempt_type,
                "remaining": remaining, "state": att}

    config = get_config(token)
    score_fn = score_fn_for(config, attempt_type)
    booster_price = int(config.get("booster_price", booster_price) or booster_price)
    balance_before = api("GET", "/users/profile", token).json().get("balance")
    attempt_price = int(config.get("paid_attempt_price", 100) or 100)
    if attempt_type == "paid" and (balance_before or 0) < attempt_price:
        return {"ok": False, "reason": "insufficient_balance",
                "balance": balance_before, "need": attempt_price}
    session = api("POST", "/game/attempts", token, json={"attempt_type": attempt_type}).json()
    losses = sorted(session.get("loss_click_numbers") or [])
    promo_list = sorted(session.get("promo_click_numbers") or [])
    sid = session["session_id"]
    booster_left = session.get("booster_remaining", 0)
    promo = promo_list[0] if promo_list else None

    target, use_booster, booster_at = plan_target(
        losses, session.get("max_taps", 120),
        booster_available=use_booster and booster_left > 0,
        gift=gift, score=score_fn, promo=promo,
        booster_price=booster_price, gift_min=gift_min,
    )

    boost_at = booster_at if use_booster and booster_at else None
    pre_limit = (boost_at - 1) if boost_at else target
    steps = [s for s in (1, max(1, target // 2), pre_limit) if 1 <= s <= pre_limit]
    for step in dict.fromkeys(steps):
        api("PATCH", f"/game/sessions/{sid}", token, json={"click_count": step})
    if boost_at:
        api("POST", f"/game/sessions/{sid}/booster", token, json={"click_count": boost_at})
    if target > pre_limit:
        api("PATCH", f"/game/sessions/{sid}", token, json={"click_count": target})

    stop = api("POST", f"/game/sessions/{sid}/stop", token, json={"click_count": target}).json()

    reward_id, coins = None, None
    if stop.get("status") == "win_finished":
        for rw in api("GET", "/game/rewards?status=pending", token).json():
            reward_id, coins = rw["reward_id"], rw["coins"]
            break
        if reward_id:
            collected = api("POST", f"/game/rewards/collect", token, params={"reward_id": reward_id}).json()
            coins = collected.get("coins", coins)

    profile = api("GET", "/users/profile", token).json()
    daily = api("GET", "/users/daily-attempt", token).json()

    return {
        "ok": stop.get("status") == "win_finished",
        "status": stop.get("status"),
        "attempt_type": attempt_type,
        "target": target,
        "promo": promo,
        "first_loss": losses[0] if losses else None,
        "score": score_fn(target),
        "gift_reached": score_fn(target) >= gift_min or (promo is not None and target >= promo),
        "booster": use_booster,
        "reward_coins": stop.get("reward_coins"),
        "collected": coins,
        "reward_id": reward_id,
        "balance": profile.get("balance"),
        "balance_before": balance_before,
        "delta": (profile.get("balance") or 0) - (balance_before or 0),
        "remaining_today": daily.get("free_attempts_remaining"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--paid", action="store_true")
    parser.add_argument("--count", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-booster", action="store_true")
    parser.add_argument("--no-gift", action="store_true")
    parser.add_argument("--gift-min", type=int, default=200)
    parser.add_argument("--booster-price", type=int, default=100)
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args()

    creds = json.loads(CREDS.read_text(encoding="utf-8"))
    token = get_token(creds)
    attempt_type = "paid" if args.paid else "free"
    max_rounds = args.count if args.count else (5 if args.all else 1)

    results = []
    rounds = 0
    while True:
        result = play_one(token, dry=args.dry_run, use_booster=not args.no_booster,
                          attempt_type=attempt_type, gift=not args.no_gift,
                          booster_price=args.booster_price, gift_min=args.gift_min)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False))
        rounds += 1
        if rounds >= max_rounds:
            break
        if not result.get("ok") and result.get("reason"):
            break
        time.sleep(args.delay)

    won = [r for r in results if r.get("ok")]
    total = sum(r.get("collected") or 0 for r in won)
    delta = sum(r.get("delta") or 0 for r in results)
    gifts = sum(1 for r in results if r.get("gift_reached"))
    print(f"итого раундов={len(results)}, побед={len(won)}, монет собрано={total}, "
          f"баланс {delta:+d}, подошли к подарку={gifts}")


if __name__ == "__main__":
    main()
