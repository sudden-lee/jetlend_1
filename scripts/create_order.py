"""Log in and create an order on the local development server using only stdlib."""

import argparse
import getpass
import json
import time
import uuid
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

BASE_URL = "http://127.0.0.1:8000"


def cookie_value(cookies: CookieJar, name: str) -> str:
    for cookie in cookies:
        if cookie.name == name and not cookie.is_expired():
            return cookie.value
    raise ValueError(f"Сервер не установил cookie {name}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Создать заказ на 127.0.0.1:8000.")
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--good-id", type=int, required=True)
    parser.add_argument("--quantity", type=int, default=2)
    parser.add_argument("--promo-code")
    parser.add_argument("--idempotency-key", default=None)
    args = parser.parse_args()
    cookies = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookies))
    login_url = f"{BASE_URL}/accounts/login/"
    try:
        with opener.open(login_url, timeout=10) as response:
            response.read()
        form = urlencode(
            {
                "username": input("Логин: "),
                "password": getpass.getpass("Пароль: "),
                "csrfmiddlewaretoken": cookie_value(cookies, "csrftoken"),
                "next": "/accounts/login/",
            }
        ).encode()
        login_request = Request(login_url, data=form, headers={"Referer": login_url})
        with opener.open(login_request, timeout=10) as response:
            response.read()
        cookie_value(cookies, "sessionid")
        payload = {
            "user_id": args.user_id,
            "goods": [{"good_id": args.good_id, "quantity": args.quantity}],
        }
        if args.promo_code is not None:
            payload["promo_code"] = args.promo_code
        idempotency_key = args.idempotency_key or str(uuid.uuid4())
        print(f"Idempotency-Key: {idempotency_key}")
        for attempt in range(3):
            request = Request(
                f"{BASE_URL}/api/orders/",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key,
                    "X-CSRFToken": cookie_value(cookies, "csrftoken"),
                    "Referer": login_url,
                },
            )
            try:
                with opener.open(request, timeout=10) as response:
                    print(f"HTTP {response.status}")
                    print(f"Idempotency-Replayed: {response.headers['Idempotency-Replayed']}")
                    print(json.dumps(json.load(response), ensure_ascii=False, indent=2))
                    return
            except HTTPError as exc:
                if exc.code < 500 or attempt == 2:
                    raise
                exc.close()
            except (URLError, TimeoutError):
                if attempt == 2:
                    raise
            time.sleep(0.2 * 2**attempt)
    except HTTPError as exc:
        parser.exit(1, f"HTTP {exc.code}: {exc.read().decode(errors='replace')}\n")
    except (URLError, ValueError, TimeoutError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
