#!/usr/bin/env python3
"""
cs2_steam_prices.py

Pobiera ekwipunek CS2 z podanego konta Steam (SteamID64), pokazuje go
jako interaktywną stronę HTML (ikonki, kolory rzadkości, sortowanie)
i opcjonalnie dorabia ceny ze Steam Community Market: najniższą ofertę
sprzedaży i najwyższą ofertę kupna dla każdego przedmiotu.

Ekwipunek jest zapisywany do lokalnego pliku cache (domyślnie
inventory_cache.json), więc kolejne uruchomienia nie muszą za każdym
razem odpytywać Steam od nowa.

WYMAGANIA:
    pip install requests

UŻYCIE (sam ekwipunek, bez cen):
    python cs2_steam_prices.py --steamid 7656119XXXXXXXXXX --html --open

Wymuszenie świeżego pobrania (ignorując cache):
    python cs2_steam_prices.py --steamid 7656119XXXXXXXXXX --refresh

Z cenami Steam:
    python cs2_steam_prices.py --steamid 7656119XXXXXXXXXX --prices --html --open
    python cs2_steam_prices.py --steamid 7656119XXXXXXXXXX --prices --csv wynik.csv
    python cs2_steam_prices.py --steamid 7656119XXXXXXXXXX --prices --skip-steam-buy

UWAGI:
  - Ekwipunek Steam musi być PUBLICZNY (Ustawienia prywatności Steam ->
    Szczegóły gry / Ekwipunek -> Publiczny), inaczej Steam zwróci błąd 403.
  - Steam potrafi czasowo zablokować zbyt częste zapytania (błąd 429,
    404, albo zwrócić "null"). Skrypt sam czeka i próbuje ponownie, ale
    jeśli blokada się utrzymuje, jedyne wyjście to odczekać dłużej
    (czasem nawet kilkanaście godzin) zanim spróbujesz ponownie.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import quote, urlparse, parse_qs

import requests

try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    cffi_requests = None
    CURL_CFFI_AVAILABLE = False

STEAM_INVENTORY_URL = "https://steamcommunity.com/inventory/{steamid}/730/2"
STEAM_MARKET_URL = "https://steamcommunity.com/market/listings/730/{name}"
CSFLOAT_SEARCH_URL = "https://csfloat.com/search?market_hash_name={name}"
STEAM_PRICEOVERVIEW_URL = "https://steamcommunity.com/market/priceoverview/"
STEAM_HISTOGRAM_URL = "https://steamcommunity.com/market/itemordershistogram"
STEAM_ICON_CDN = "https://community.cloudflare.steamstatic.com/economy/image/{icon}/128x128"

NAMEID_REGEX = re.compile(r"Market_LoadOrderSpread\(\s*(\d+)\s*\)")
# Nowy interfejs Steam nie osadza już Market_LoadOrderSpread w kodzie strony,
# ale nadal wypisuje tekstem najwyższą ofertę kupna, np.
# "5.866 requests to buy at<!-- -->27,05 zł<!-- --> or lower"
# Dla niektórych przedmiotów (np. noży) otoczka HTML jest inna - używa
# <span ...>ceny</span> zamiast komentarzy - regex pomija dowolne tagi
# HTML między "at" a samą liczbą, żeby działać w obu przypadkach.
HIGHEST_BUY_TEXT_REGEX = re.compile(
    r"requests to buy at\s*(?:<[^>]*>\s*)*([\d.,\s\xa0]+?)\s*z[l\u0142]",
    re.IGNORECASE,
)
PRICE_NUMBER_REGEX = re.compile(r"[\d]+[.,]\d+|\d+")


def build_steam_market_url(name: str, exterior_tag: str = "", quality_type_tags=None) -> str:
    """Buduje link do strony przedmiotu na Steam Market, z filtrami
    dokładnego stanu (Exterior) i jakości (Normal/StatTrak/Souvenir),
    żeby trafiać od razu w konkretny wariant zamiast strony zbiorczej.

    quality_type_tags może być pojedynczym stringiem (dla wstecznej
    kompatybilności) albo listą - niektóre przedmioty (np. noże ★)
    mają WIĘCEJ NIŻ JEDEN tag jakości naraz i potrzebują obu jako
    osobnych parametrów category_Quality w linku.
    """
    if quality_type_tags is None:
        quality_type_tags = []
    elif isinstance(quality_type_tags, str):
        quality_type_tags = [quality_type_tags] if quality_type_tags else []

    url = STEAM_MARKET_URL.format(name=quote(name, safe=""))
    params = ["appid=730"]
    if exterior_tag:
        params.append(f"category_Exterior={exterior_tag}")
    for tag in quality_type_tags:
        if tag:
            params.append(f"category_Quality={tag}")
    return url + "?" + "&".join(params)

QUALITY_RANK = {
    "Consumer Grade": 0,
    "Base Grade": 0,
    "Industrial Grade": 1,
    "Mil-Spec Grade": 2,
    "Restricted": 3,
    "Classified": 4,
    "Covert": 5,
    "Contraband": 6,
    "Extraordinary": 7,
}

# Naklejki, patche, muzyka itp. mają zupełnie inne nazwy rzadkości niż
# bronie (np. "High Grade", "Remarkable", "Exotic"), ale Valve konsekwentnie
# koduje rzadkość TYM SAMYM kolorem w całej grze, niezależnie od typu
# przedmiotu. Rankingujemy więc po kolorze, nie po nazwie - to jedyny
# sposób, żeby "Jakość" faktycznie sortowała WSZYSTKO razem, a nie tylko
# bronie.
COLOR_RANK = {
    "b0c3d9": 0,  # Consumer / Base Grade (biało-szary)
    "5e98d9": 1,  # Industrial Grade (jasnoniebieski)
    "4b69ff": 2,  # Mil-Spec / High Grade (niebieski)
    "8847ff": 3,  # Restricted / Remarkable (fioletowy)
    "d32ce6": 4,  # Classified / Exotic (różowy)
    "eb4b4b": 5,  # Covert (czerwony)
    "e4ae39": 6,  # Contraband / Extraordinary (złoty)
}


def compute_quality_rank(name_color: str, quality: str) -> int:
    color_key = (name_color or "").lower()
    if color_key in COLOR_RANK:
        return COLOR_RANK[color_key]
    return QUALITY_RANK.get(quality, -1)

HEADERS_STEAM = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://steamcommunity.com/market/",
}

_steam_session: Optional[requests.Session] = None
_steam_login_cookie: Optional[str] = None


def get_steam_session():
    """Zwraca współdzieloną sesję HTTP z ciasteczkami Steam.

    Jeśli zainstalowana jest biblioteka curl_cffi, używa jej z
    impersonate="chrome" - to sprawia, że połączenie TLS/HTTP2
    wygląda dokładnie jak z prawdziwej przeglądarki Chrome, a nie
    jak standardowe zapytanie Pythona. To jedyny sposób, żeby ominąć
    zabezpieczenia rozpoznające klienty po "odcisku palca" połączenia,
    niezależnie od nagłówków czy ciasteczek.

    Bez curl_cffi używa zwykłego requests.Session (mniejsza szansa
    na ominięcie zabezpieczeń antybotowych, ale nadal próbuje).

    Pierwsze użycie odwiedza stronę główną Steam Community, żeby
    dostać podstawowe ciasteczka (sessionid, browserid) - dokładnie
    tak, jak robi to przeglądarka przy pierwszym wejściu.

    Jeśli podano --login-cookie (wartość ciasteczka steamLoginSecure
    z zalogowanej przeglądarki), zapytania wyglądają jak z Twojego
    zalogowanego konta.
    """
    global _steam_session
    if _steam_session is not None:
        return _steam_session

    if CURL_CFFI_AVAILABLE:
        session = cffi_requests.Session(impersonate="chrome")
        session.headers.update(HEADERS_STEAM)
    else:
        session = requests.Session()
        session.headers.update(HEADERS_STEAM)

    if _steam_login_cookie:
        session.cookies.set("steamLoginSecure", _steam_login_cookie, domain="steamcommunity.com")
    try:
        session.get("https://steamcommunity.com/market/", timeout=15)
    except Exception:
        pass  # nawet jeśli się nie uda, jedziemy dalej z pustą sesją
    _steam_session = session
    return _steam_session


def fetch_steam_inventory(
    steamid: str, max_retries: int = 12, include_non_marketable: bool = False
) -> List[Dict]:
    """Pobiera ekwipunek CS2 (appid 730, contextid 2) dla danego SteamID64.

    Bardzo cierpliwie ponawia próbę z rosnącym odczekiwaniem, jeśli Steam
    odpowie błędem 429 (za dużo zapytań) lub przejściowym błędem serwera (5xx).
    Nie ma pośpiechu - czas nie gra roli, ważne żeby w końcu się udało.

    Zwraca listę słowników: {"market_hash_name": str, "count": int}
    """
    params = {"l": "english", "count": 2000}
    resp = None
    wait = 15  # sekund, rośnie po każdej próbie

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(
                STEAM_INVENTORY_URL.format(steamid=steamid),
                params=params,
                headers=HEADERS_STEAM,
                timeout=20,
            )
        except requests.exceptions.RequestException as e:
            if attempt == max_retries:
                raise RuntimeError(f"Problem z połączeniem do Steam: {e}")
            print(f"Problem z połączeniem ({e}). Czekam {wait}s i próbuję ponownie...")
            time.sleep(wait)
            wait = min(wait * 2, 180)
            continue

        if resp.status_code == 200:
            break

        if resp.status_code == 403:
            raise RuntimeError(
                "Steam zwrócił 403 - ekwipunek jest prywatny albo SteamID jest "
                "błędny. Ustaw ekwipunek jako publiczny w ustawieniach prywatności Steam."
            )

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_retries:
                raise RuntimeError(
                    f"Steam ciągle zwraca błąd {resp.status_code} po {max_retries} "
                    "próbach rozłożonych na kilkanaście minut. Odczekaj dłużej "
                    "(np. 15-30 minut) i uruchom skrypt jeszcze raz."
                )
            print(
                f"Steam zwrócił {resp.status_code} (limit zapytań). "
                f"Czekam {wait}s i próbuję ponownie ({attempt}/{max_retries})..."
            )
            time.sleep(wait)
            wait = min(wait * 2, 180)  # nie czekaj dłużej niż 3 minuty na próbę
            continue

        # inny, nieoczekiwany błąd - nie ma sensu ponawiać
        resp.raise_for_status()

    resp.raise_for_status()

    data = resp.json()
    if not data or "assets" not in data or "descriptions" not in data:
        raise RuntimeError(
            "Nie udało się odczytać ekwipunku - konto może nie mieć CS2 "
            "lub ekwipunek jest pusty/prywatny."
        )

    # Zbuduj mapę classid_instanceid -> opis (zawiera market_hash_name)
    desc_map = {}
    for d in data["descriptions"]:
        key = (d.get("classid"), d.get("instanceid"))
        desc_map[key] = d

    counts: Dict[str, Dict] = {}
    for asset in data["assets"]:
        key = (asset.get("classid"), asset.get("instanceid"))
        desc = desc_map.get(key)
        if not desc:
            continue
        name = desc.get("market_hash_name", "Nieznany przedmiot")
        icon_url = desc.get("icon_url", "")
        name_color = desc.get("name_color", "") or ""
        marketable = bool(desc.get("marketable", 1))

        quality = ""
        item_type = ""
        exterior_tag = ""
        quality_type_tags = []
        for tag in desc.get("tags", []):
            category = tag.get("category")
            if category == "Rarity":
                quality = tag.get("localized_tag_name", "") or tag.get("name", "")
            elif category == "Type":
                item_type = tag.get("localized_tag_name", "") or tag.get("name", "")
            elif category == "Exterior":
                # Steam podaje tu wprost wewnętrzną nazwę typu "WearCategory2"
                exterior_tag = tag.get("internal_name", "")
            elif category == "Quality":
                # "normal" / "strange" (StatTrak) / "tournament" (Souvenir) / "unusual" (★).
                # StatTrak ★ przedmioty mają WIĘCEJ NIŻ JEDEN taki tag naraz
                # (np. "unusual" + "strange") - zbieramy wszystkie.
                internal = tag.get("internal_name", "")
                if internal and internal not in quality_type_tags:
                    quality_type_tags.append(internal)

        if "unusual" in quality_type_tags and not any(
            t in quality_type_tags for t in ("normal", "strange", "tournament")
        ):
            # Zwykłe (nie-StatTrak, nie-Souvenir) przedmioty ★ (noże/rękawice)
            # nie mają explicite tagu "normal" w danych Steam - trzeba go
            # dopowiedzieć samemu, inaczej link prowadzi do strony "wszystkie
            # warianty" zamiast konkretnie tego jednego.
            quality_type_tags.append("normal")

        if name not in counts:
            counts[name] = {
                "count": 0,
                "icon_url": icon_url,
                "name_color": name_color,
                "quality": quality,
                "item_type": item_type,
                "exterior_tag": exterior_tag,
                "quality_type_tags": quality_type_tags,
                "max_assetid": 0,
                "marketable": marketable,
            }
        counts[name]["count"] += 1
        try:
            assetid_num = int(asset.get("assetid", 0))
        except (TypeError, ValueError):
            assetid_num = 0
        if assetid_num > counts[name]["max_assetid"]:
            counts[name]["max_assetid"] = assetid_num

    all_items = [
        {
            "market_hash_name": n,
            "count": v["count"],
            "icon_url": v["icon_url"],
            "name_color": v["name_color"],
            "quality": v["quality"],
            "item_type": v["item_type"],
            "exterior_tag": v["exterior_tag"],
            "quality_type_tags": v["quality_type_tags"],
            "max_assetid": v["max_assetid"],
            "marketable": v["marketable"],
        }
        for n, v in sorted(counts.items())
    ]

    if include_non_marketable:
        return all_items
    return [item for item in all_items if item["marketable"]]


def load_cache(cache_file: str) -> Optional[List[Dict]]:
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_cache(cache_file: str, items: List[Dict]) -> None:
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def _steam_request_with_retry(
    url: str, params: Dict, max_retries: int = 2, base_wait: float = 6.0
) -> Optional[requests.Response]:
    """Generyczne zapytanie GET do Steam z cierpliwym ponawianiem.

    Używane dla endpointów cenowych (priceoverview, strona listingu,
    itemordershistogram). Zwraca None (zamiast wyjątku), jeśli po
    wszystkich próbach się nie uda - dla pojedynczego przedmiotu to
    nie jest błąd krytyczny, po prostu nie pokażemy dla niego ceny.
    """
    wait = base_wait
    for attempt in range(1, max_retries + 1):
        try:
            session = get_steam_session()
            resp = session.get(url, params=params, timeout=15)
        except Exception as e:
            if DEBUG_PRICES:
                print(f"    [debug] {url}: wyjątek sieciowy {e!r}")
            if attempt == max_retries:
                return None
            time.sleep(wait)
            wait = min(wait * 2, 20)
            continue

        if resp.status_code == 200:
            return resp

        if resp.status_code == 429 or resp.status_code >= 500:
            if DEBUG_PRICES:
                print(f"    [debug] {url}: status={resp.status_code} (próba {attempt}/{max_retries})")
            if attempt == max_retries:
                return None
            time.sleep(wait)
            wait = min(wait * 2, 20)
            continue

        # 403/404/etc - nie ma sensu ponawiać
        if DEBUG_PRICES:
            body = resp.text[:150].replace("\n", " ")
            print(f"    [debug] {url}: status={resp.status_code} body={body!r} (nie ponawiam)")
        return None

    return None


def parse_price_to_float(text: Optional[str]) -> Optional[float]:
    """Wyciąga liczbę z tekstu ceny Steam typu '$12.34', '12,34 zł'
    albo '2 363,05 zł' (spacja/NBSP jako separator tysięcy)."""
    if not text:
        return None
    # Usuń spacje i twarde spacje używane jako separator tysięcy,
    # np. "2 363,05 zł" -> "2363,05 zł"
    cleaned = text.replace("\xa0", "").replace(" ", "")
    match = PRICE_NUMBER_REGEX.search(cleaned)
    if not match:
        return None
    number = match.group(0).replace(",", ".")
    try:
        return float(number)
    except ValueError:
        return None


def load_json_dict(path: str) -> Dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_json_dict(path: str, data: Dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_steam_highest_buy_from_page(
    name: str, exterior_tag: str = "", quality_type_tags=None
) -> Optional[float]:
    """Najwyższa aktualna oferta kupna na Steam Community Market.

    Nowy interfejs Steam nie ma już starego, łatwo dostępnego API
    (item_nameid + itemordershistogram) osadzonego w kodzie strony -
    zamiast tego wyciągamy cenę bezpośrednio z tekstu strony przedmiotu,
    z linijki w stylu "5 866 requests to buy at 27,05 zł or lower".
    """
    url = build_steam_market_url(name, exterior_tag, quality_type_tags)
    resp = _steam_request_with_retry(url, params={})

    if DEBUG_PRICES:
        if resp is None:
            print("    [debug] Steam kupno: brak odpowiedzi ze strony listingu")
        else:
            match = HIGHEST_BUY_TEXT_REGEX.search(resp.text)
            if match:
                print(f"    [debug] Steam kupno: znaleziono tekst z ceną {match.group(1)}")
            else:
                print(f"    [debug] Steam kupno: status={resp.status_code}, nie znaleziono tekstu oferty kupna w HTML")

    if resp is None:
        return None
    match = HIGHEST_BUY_TEXT_REGEX.search(resp.text)
    if not match:
        return None
    return parse_price_to_float(match.group(1))


def fetch_steam_lowest_sell(name: str) -> Optional[float]:
    """Najniższa aktualna oferta sprzedaży na Steam Community Market."""
    resp = _steam_request_with_retry(
        STEAM_PRICEOVERVIEW_URL,
        params={"appid": 730, "currency": 6, "market_hash_name": name},
    )
    _debug("Steam priceoverview", resp)
    if resp is None:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not data.get("success"):
        return None
    return parse_price_to_float(data.get("lowest_price"))


def fetch_and_cache_price(
    name: str,
    prices_cache: Dict,
    nameid_cache: Dict,
    skip_buy: bool = False,
    delay: float = 1.0,
    exterior_tag: str = "",
    quality_type_tags=None,
) -> Dict:
    """Pobiera cenę jednego przedmiotu ze Steam, zachowując starą cenę
    jeśli świeże zapytanie się nie uda, i dopisując punkt do historii
    tylko gdy faktycznie coś nowego udało się pobrać.

    Aktualizuje prices_cache i nameid_cache w miejscu (in-place).
    Zwraca {"sell": ..., "buy": ..., "fresh_sell_ok": bool}.
    """
    old_entry = prices_cache.get(name)

    fresh_lowest = None
    fresh_highest_buy = None

    try:
        fresh_lowest = fetch_steam_lowest_sell(name)
    except Exception as e:
        if DEBUG_PRICES:
            print(f"    [debug] Steam sprzedaż: nieoczekiwany błąd {e!r}")
    time.sleep(delay)

    if not skip_buy:
        try:
            fresh_highest_buy = fetch_steam_highest_buy_from_page(name, exterior_tag, quality_type_tags)
            time.sleep(delay)
        except Exception as e:
            if DEBUG_PRICES:
                print(f"    [debug] Steam kupno: nieoczekiwany błąd {e!r}")

    old_sell = old_entry.get("sell") if old_entry else None
    old_buy = old_entry.get("buy") if old_entry else None
    sell = fresh_lowest if fresh_lowest is not None else old_sell
    buy = fresh_highest_buy if fresh_highest_buy is not None else old_buy

    # Przedmioty przy samym dnie cenowym Steam (ok. 0,12 zł to praktyczne
    # minimum) zwykle nie mają żadnych ofert kupna - nikt nie stawia
    # zlecenia na coś tak tanie. W takim wypadku przyjmujemy cenę kupna
    # równą cenie sprzedaży zamiast pokazywać brak danych.
    FLOOR_PRICE_THRESHOLD = 0.20
    if buy is None and sell is not None and sell <= FLOOR_PRICE_THRESHOLD:
        buy = sell

    history = list(old_entry.get("history", [])) if old_entry else []
    if fresh_lowest is not None or fresh_highest_buy is not None:
        history.append(
            {
                "date": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                "sell": sell,
                "buy": buy,
            }
        )
        history = history[-200:]

    prices_cache[name] = {"sell": sell, "buy": buy, "history": history}
    return {"sell": sell, "buy": buy, "fresh_sell_ok": fresh_lowest is not None}


DEBUG_PRICES = False


def _debug(label: str, resp: Optional[requests.Response]) -> None:
    if not DEBUG_PRICES:
        return
    if resp is None:
        print(f"    [debug] {label}: brak odpowiedzi (błąd sieci albo wyczerpane próby)")
    else:
        body = resp.text[:200].replace("\n", " ")
        print(f"    [debug] {label}: status={resp.status_code} body={body!r}")


def print_inventory(items: List[Dict]) -> None:
    print("\n" + "=" * 60)
    print(f"{'Przedmiot':<50}{'Ilość':>8}")
    print("-" * 60)
    for item in items:
        name = item["market_hash_name"]
        name_display = (name[:47] + "...") if len(name) > 50 else name
        print(f"{name_display:<50}{item['count']:>8}")
    print("-" * 60)
    total_items = sum(i["count"] for i in items)
    print(f"Unikalnych przedmiotów: {len(items)}   |   Wszystkich sztuk: {total_items}")
    print("=" * 60)


def generate_html(
    items: List[Dict],
    output_path: str,
    value_history: Optional[List[Dict]] = None,
    serve_mode: bool = False,
) -> None:
    """Generuje samodzielny plik HTML z kafelkami przedmiotów.

    Każdy kafelek ma ikonkę, nazwę, ilość i dwa przyciski-linki:
    do strony przedmiotu na Steam Community Market oraz do wyszukiwania
    tego przedmiotu na CSFloat. Jest też prosta wyszukiwarka (filtrowanie
    po nazwie) działająca w przeglądarce bez potrzeby serwera.
    """
    value_history = value_history or []
    value_history_json = json.dumps(value_history, ensure_ascii=False)
    serve_mode_js = "true" if serve_mode else "false"

    cards_html = []
    total_sell_value = 0.0
    total_buy_value = 0.0
    for item in items:
        name = item["market_hash_name"]
        count = item.get("count", 1)
        icon = item.get("icon_url", "")
        name_color = item.get("name_color") or "4b5563"
        quality = item.get("quality", "")
        quality_rank = compute_quality_rank(name_color, quality)
        steam_lowest = item.get("steam_lowest")
        steam_highest_buy = item.get("steam_highest_buy")
        if steam_lowest is not None:
            total_sell_value += steam_lowest * count
        if steam_highest_buy is not None:
            total_buy_value += steam_highest_buy * count
        sortable_price = steam_lowest
        price_attr = f"{sortable_price:.2f}" if sortable_price is not None else "-1"
        assetid = item.get("max_assetid", 0)

        icon_url = STEAM_ICON_CDN.format(icon=icon) if icon else ""
        exterior_tag = item.get("exterior_tag", "")
        quality_type_tags = item.get("quality_type_tags", [])
        steam_url = build_steam_market_url(name, exterior_tag, quality_type_tags)
        csfloat_url = CSFLOAT_SEARCH_URL.format(name=quote(name, safe=""))
        name_escaped = (
            name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        name_attr = name_escaped.replace('"', "&quot;")
        quality_escaped = quality.replace("&", "&amp;").replace("<", "&lt;")
        img_tag = (
            f'<img src="{icon_url}" alt="" loading="lazy">'
            if icon_url
            else '<div class="no-icon">?</div>'
        )
        title_attr = f' title="{quality_escaped}"' if quality else ""
        quality_html = (
            f'<a href="{csfloat_url}" target="_blank" rel="noopener" '
            f'class="quality-chip" style="background:#{name_color}"{title_attr}>CSFloat ↗</a>'
        )

        if steam_lowest is not None or steam_highest_buy is not None:
            sell_html = f'<span class="sell">↓ {steam_lowest:,.2f} zł</span>' if steam_lowest is not None else '<span class="sell na">↓ -</span>'
            buy_html = f'<span class="buy">↑ {steam_highest_buy:,.2f} zł</span>' if steam_highest_buy is not None else '<span class="buy na">↑ -</span>'
            btn_label = f'{sell_html}{buy_html}'
        else:
            btn_label = 'Steam Market'

        history_compact = [
            {"date": h.get("date"), "sell": h.get("sell"), "buy": h.get("buy")}
            for h in item.get("price_history", [])
        ]
        history_json = json.dumps(history_compact, ensure_ascii=False).replace("'", "&#39;")

        cards_html.append(f"""
        <div class="card" style="border-color:#{name_color}"
             data-name="{name_escaped.lower()}"
             data-raw-name="{name_attr}"
             data-count="{count}"
             data-quality="{quality_rank}"
             data-price="{price_attr}"
             data-assetid="{assetid}"
             data-history='{history_json}'>
            <div class="badge">x{count}</div>
            <button type="button" class="refresh-btn" title="Odśwież cenę tego przedmiotu">↻</button>
            {img_tag}
            <div class="name">{name_escaped}</div>
            {quality_html}
            <a href="{steam_url}" target="_blank" rel="noopener" class="btn steam-price-btn">{btn_label}</a>
        </div>""")

    total_items = sum(i.get("count", 1) for i in items)

    html = f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<title>Mój ekwipunek CS2</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  :root {{
    --bg: #0f1115;
    --card-bg: #1a1d24;
    --border: #2a2e37;
    --text: #e8e8e8;
    --muted: #9aa0aa;
    --steam: #1b2838;
    --steam-hover: #2a475e;
    --csfloat: #4a7fff;
    --csfloat-hover: #6d99ff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    margin: 0;
    padding: 24px;
  }}
  h1 {{
    margin: 0 0 4px 0;
    font-size: 24px;
  }}
  .subtitle {{
    color: var(--muted);
    margin-bottom: 16px;
    font-size: 14px;
  }}
  .totals {{
    display: flex;
    gap: 12px;
    margin-bottom: 24px;
    flex-wrap: wrap;
  }}
  .total-box {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 20px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 180px;
  }}
  .total-label {{
    font-size: 12px;
    color: var(--muted);
  }}
  .total-value {{
    font-size: 22px;
    font-weight: 700;
  }}
  .total-value.sell {{
    color: #4ade80;
  }}
  .total-value.buy {{
    color: #f87171;
  }}
  #search {{
    padding: 10px 14px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--card-bg);
    color: var(--text);
    font-size: 14px;
  }}
  #search {{
    flex: 1;
    min-width: 200px;
    max-width: 400px;
  }}
  #search:focus {{
    outline: none;
    border-color: var(--csfloat);
  }}
  #sortBy {{
    padding: 10px 14px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--card-bg);
    color: var(--text);
    font-size: 14px;
  }}
  .controls {{
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 20px;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 14px;
  }}
  .card {{
    background: var(--card-bg);
    border: 2px solid #4b5563;
    border-radius: 12px;
    padding: 14px;
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    position: relative;
    cursor: pointer;
    transition: transform 0.15s, box-shadow 0.15s;
  }}
  .card:hover {{
    transform: translateY(-3px);
  }}
  .card.selected {{
    box-shadow: 0 0 0 3px #ffd700, 0 0 14px rgba(255,215,0,0.35);
    background: rgba(255,215,0,0.06);
  }}
  .quality-chip {{
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: #ffffff;
    padding: 4px 10px;
    border-radius: 999px;
    margin-bottom: 12px;
    text-shadow: 0 1px 2px rgba(0,0,0,0.6);
    text-decoration: none;
    display: inline-block;
    cursor: pointer;
    transition: filter 0.15s;
  }}
  .quality-chip:hover {{
    filter: brightness(1.15);
  }}
  .badge {{
    position: absolute;
    top: 8px;
    left: 10px;
    color: var(--text);
    font-size: 13px;
    font-weight: 700;
    text-shadow: 0 1px 3px rgba(0,0,0,0.8);
  }}
  .refresh-btn {{
    position: absolute;
    top: 6px;
    right: 6px;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    border: 1px solid var(--border);
    background: var(--card-bg);
    color: var(--muted);
    font-size: 13px;
    line-height: 1;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: transform 0.2s, color 0.15s, border-color 0.15s;
  }}
  .refresh-btn:hover {{
    color: var(--text);
    border-color: var(--csfloat);
  }}
  .refresh-btn.spinning {{
    animation: spin 0.8s linear infinite;
    color: var(--csfloat);
  }}
  @keyframes spin {{
    from {{ transform: rotate(0deg); }}
    to {{ transform: rotate(360deg); }}
  }}
  .card img {{
    width: 96px;
    height: 96px;
    object-fit: contain;
    margin-bottom: 10px;
  }}
  .no-icon {{
    width: 96px;
    height: 96px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--muted);
    font-size: 32px;
    margin-bottom: 10px;
  }}
  .name {{
    font-size: 13px;
    line-height: 1.3;
    margin-bottom: 12px;
    min-height: 34px;
  }}
  .header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 20px;
    margin-bottom: 20px;
  }}
  .header-left h1 {{
    margin: 0 0 4px 0;
  }}
  .header-right {{
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 8px;
  }}
  .action-btn {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    color: var(--text);
    font-size: 13px;
    font-weight: 600;
    padding: 9px 16px;
    border-radius: 8px;
    cursor: pointer;
    font-family: inherit;
    white-space: nowrap;
    transition: border-color 0.15s;
  }}
  .action-btn:hover {{
    border-color: var(--csfloat);
  }}
  .action-btn:disabled {{
    opacity: 0.5;
    cursor: default;
  }}
  .action-btn.primary {{
    background: var(--csfloat);
    border-color: var(--csfloat);
    color: white;
  }}
  .action-btn.primary:hover {{
    background: var(--csfloat-hover);
  }}
  .import-export {{
    display: flex;
    gap: 8px;
  }}
  .action-btn.file-label {{
    display: flex;
    align-items: center;
  }}
  .fetch-progress {{
    font-size: 12px;
    color: var(--muted);
    min-height: 16px;
  }}
  .btn {{
    display: block;
    width: 100%;
    text-decoration: none;
    color: white;
    font-size: 11px;
    font-weight: 600;
    padding: 7px 4px;
    border-radius: 6px;
    border: none;
    cursor: pointer;
    font-family: inherit;
    transition: background 0.15s;
  }}
  .steam-price-btn {{
    background: var(--steam);
    display: flex;
    justify-content: center;
    gap: 10px;
    font-size: 12px;
    font-weight: 700;
  }}
  .steam-price-btn:hover {{
    background: var(--steam-hover);
  }}
  .steam-price-btn .sell {{
    color: #4ade80;
  }}
  .steam-price-btn .buy {{
    color: #f87171;
  }}
  .steam-price-btn .na {{
    color: var(--muted);
    font-weight: 400;
  }}
  .chart-box {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 20px;
    width: 100%;
    box-sizing: border-box;
  }}
  .chart-range-buttons {{
    display: flex;
    gap: 6px;
    margin-bottom: 12px;
    flex-wrap: wrap;
  }}
  .range-btn {{
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
    font-size: 12px;
    font-weight: 600;
    padding: 6px 14px;
    border-radius: 999px;
    cursor: pointer;
    font-family: inherit;
    transition: all 0.15s;
  }}
  .range-btn:hover {{
    border-color: var(--csfloat);
    color: var(--text);
  }}
  .range-btn.active {{
    background: var(--csfloat);
    border-color: var(--csfloat);
    color: white;
  }}
  .item-chart-popover {{
    display: none;
    position: fixed;
    background: #0a0b0e;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px;
    z-index: 50;
    pointer-events: none;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
  }}
  .alerts-box {{
    border-radius: 10px;
    padding: 12px 16px;
    margin-bottom: 20px;
    max-height: 220px;
    overflow-y: auto;
  }}
  .alerts-box.up {{
    background: rgba(74,222,128,0.08);
    border: 1px solid #4ade80;
  }}
  .alerts-box.down {{
    background: rgba(248,113,113,0.08);
    border: 1px solid #f87171;
  }}
  .alerts-title {{
    font-weight: 700;
    margin-bottom: 8px;
    font-size: 13px;
  }}
  .alerts-box.up .alerts-title {{
    color: #4ade80;
  }}
  .alerts-box.down .alerts-title {{
    color: #f87171;
  }}
  .alert-row {{
    display: block;
    font-size: 13px;
    padding: 4px 0;
    color: var(--text);
    text-decoration: none;
    cursor: pointer;
  }}
  .alerts-box.up .alert-row {{
    border-bottom: 1px solid rgba(74,222,128,0.15);
  }}
  .alerts-box.down .alert-row {{
    border-bottom: 1px solid rgba(248,113,113,0.15);
  }}
  .alerts-box.up .alert-row:hover {{
    color: #4ade80;
  }}
  .alerts-box.down .alert-row:hover {{
    color: #f87171;
  }}
  .alert-row:last-child {{
    border-bottom: none;
  }}
  .alert-pct.up {{
    color: #4ade80;
    font-weight: 700;
  }}
  .alert-pct.down {{
    color: #f87171;
    font-weight: 700;
  }}
  #empty-msg {{
    display: none;
    color: var(--muted);
    padding: 40px;
    text-align: center;
  }}
</style>
</head>
<body>
  <div class="header">
    <div class="header-left">
      <h1>Mój ekwipunek CS2</h1>
      <div class="subtitle">{len(items)} unikalnych przedmiotów &middot; {total_items} sztuk łącznie</div>
      <div class="totals">
        <div class="total-box">
          <span class="total-label">Suma wg sprzedaży</span>
          <span class="total-value sell" id="totalSell">{total_sell_value:,.2f} zł</span>
        </div>
        <div class="total-box">
          <span class="total-label">Suma wg ofert kupna</span>
          <span class="total-value buy" id="totalBuy">{total_buy_value:,.2f} zł</span>
        </div>
        <div class="total-box" id="selectedTotalBox" style="display:none; border-color:#ffd700;">
          <span class="total-label">Suma zaznaczonych (<span id="selectedCount">0</span>)</span>
          <span class="total-value sell" id="selectedSell">0,00 zł</span>
          <span class="total-value buy" id="selectedBuy">0,00 zł</span>
        </div>
      </div>
    </div>
    <div class="header-right">
      <button id="fetchPricesBtn" class="action-btn primary">Pobierz ceny ze Steam</button>
      <button id="fillMissingBtn" class="action-btn">Uzupełnij puste ceny</button>
      <button id="refreshSelectedBtn" class="action-btn primary" style="display:none;">Odśwież zaznaczone</button>
      <div id="fetchProgress" class="fetch-progress"></div>
      <div class="import-export">
        <button id="exportPricesBtn" class="action-btn">Zapisz ceny do pliku</button>
        <button id="exportHistoryCsvBtn" class="action-btn">Historia wartości (CSV)</button>
        <button id="addChartPointBtn" class="action-btn">+ Punkt na wykresie</button>
        <label class="action-btn file-label">
          Wczytaj ceny z pliku
          <input type="file" id="importPricesInput" accept="application/json" style="display:none">
        </label>
      </div>
    </div>
  </div>
  <div class="chart-box" id="valueChartBox" style="display:none">
    <div class="chart-range-buttons">
      <button type="button" class="range-btn" data-range="7">1 tydzień</button>
      <button type="button" class="range-btn" data-range="30">1 miesiąc</button>
      <button type="button" class="range-btn" data-range="180">6 miesięcy</button>
      <button type="button" class="range-btn" data-range="365">1 rok</button>
      <button type="button" class="range-btn active" data-range="all">Wszystko</button>
    </div>
    <div style="height:340px;">
      <canvas id="valueChart"></canvas>
    </div>
  </div>
  <div id="itemChartPopover" class="item-chart-popover">
    <canvas id="itemChartCanvas" width="260" height="140"></canvas>
  </div>
  <div id="alertsBox" class="alerts-box up" style="display:none">
    <div class="alerts-title">📈 Znaczący wzrost ceny względem historycznego minimum</div>
    <div id="alertsList"></div>
  </div>
  <div id="alertsBoxDown" class="alerts-box down" style="display:none">
    <div class="alerts-title">📉 Znaczący spadek ceny względem historycznego maksimum</div>
    <div id="alertsListDown"></div>
  </div>
  <div class="controls">
    <input type="text" id="search" placeholder="Szukaj przedmiotu...">
    <select id="sortBy">
      <option value="name">Sortuj: Nazwa</option>
      <option value="count">Sortuj: Ilość</option>
      <option value="quality">Sortuj: Jakość</option>
      <option value="price">Sortuj: Cena</option>
      <option value="newest">Sortuj: Data dodania do EQ</option>
    </select>
    <select id="sortDir">
      <option value="desc">Malejąco ▼</option>
      <option value="asc">Rosnąco ▲</option>
    </select>
  </div>
  <div class="grid" id="grid">
    {''.join(cards_html)}
  </div>
  <div id="empty-msg">Brak przedmiotów pasujących do wyszukiwania.</div>

  <script>
    const search = document.getElementById('search');
    const sortBy = document.getElementById('sortBy');
    const sortDir = document.getElementById('sortDir');
    const grid = document.getElementById('grid');
    const cards = Array.from(document.querySelectorAll('.card'));
    const emptyMsg = document.getElementById('empty-msg');

    const fieldMap = {{
      name: c => c.dataset.name,
      count: c => parseInt(c.dataset.count),
      quality: c => parseInt(c.dataset.quality),
      price: c => parseFloat(c.dataset.price),
      newest: c => parseInt(c.dataset.assetid),
    }};

    function applyFilter() {{
      const q = search.value.trim().toLowerCase();
      let visibleCount = 0;
      cards.forEach(card => {{
        const match = card.dataset.name.includes(q);
        card.style.display = match ? '' : 'none';
        if (match) visibleCount++;
      }});
      emptyMsg.style.display = visibleCount === 0 ? 'block' : 'none';
    }}

    function applySort() {{
      const getValue = fieldMap[sortBy.value];
      const dir = sortDir.value === 'asc' ? 1 : -1;
      const sorted = [...cards].sort((a, b) => {{
        const va = getValue(a);
        const vb = getValue(b);
        if (typeof va === 'string') {{
          return dir * va.localeCompare(vb);
        }}
        return dir * (va - vb);
      }});
      sorted.forEach(card => grid.appendChild(card));
    }}

    search.addEventListener('input', applyFilter);
    sortDir.addEventListener('change', applySort);
    sortBy.addEventListener('change', () => {{
      // Dla nazwy sensowniejszy jest domyślnie porządek A-Z niż Z-A.
      if (sortBy.value === 'name' && sortDir.value === 'desc') {{
        sortDir.value = 'asc';
      }}
      applySort();
    }});

    applySort();

    // ==========================================================
    // Pobieranie cen Steam bezpośrednio w przeglądarce (JS)
    // ==========================================================
    const PRICE_STORAGE_KEY = 'cs2_price_store_v1';
    const VALUE_HISTORY_KEY = 'cs2_value_history_v1';
    const PRICE_CURRENCY = 6; // PLN
    const SERVE_MODE = {serve_mode_js}; // true = ceny idą przez lokalny serwer (bez CORS)

    const fetchBtn = document.getElementById('fetchPricesBtn');
    const refreshSelectedBtn = document.getElementById('refreshSelectedBtn');
    const progressEl = document.getElementById('fetchProgress');
    const exportBtn = document.getElementById('exportPricesBtn');
    const importInput = document.getElementById('importPricesInput');
    const totalSellEl = document.getElementById('totalSell');
    const totalBuyEl = document.getElementById('totalBuy');
    const selectedTotalBox = document.getElementById('selectedTotalBox');
    const selectedCountEl = document.getElementById('selectedCount');
    const selectedSellEl = document.getElementById('selectedSell');
    const selectedBuyEl = document.getElementById('selectedBuy');
    const valueChartBox = document.getElementById('valueChartBox');
    const itemPopover = document.getElementById('itemChartPopover');
    const itemPopoverCanvas = document.getElementById('itemChartCanvas');

    function loadJSONLocal(key, fallback) {{
      try {{
        const raw = localStorage.getItem(key);
        return raw ? JSON.parse(raw) : fallback;
      }} catch (e) {{
        return fallback;
      }}
    }}

    function saveJSONLocal(key, val) {{
      try {{
        localStorage.setItem(key, JSON.stringify(val));
      }} catch (e) {{ /* localStorage niedostępny - trudno, po prostu nie zapiszemy */ }}
    }}

    // Łączy dwie listy historii (np. z Pythona i z przeglądarki), usuwa
    // duplikaty po dacie i sortuje chronologicznie.
    function mergeHistories(a, b) {{
      const map = new Map();
      [...(a || []), ...(b || [])].forEach(h => {{
        if (h && h.date) map.set(h.date, h);
      }});
      return Array.from(map.values()).sort((x, y) => x.date.localeCompare(y.date));
    }}

    let priceStore = loadJSONLocal(PRICE_STORAGE_KEY, {{}});
    let valueHistory = mergeHistories({value_history_json}, loadJSONLocal(VALUE_HISTORY_KEY, []));

    function savePriceStore() {{
      saveJSONLocal(PRICE_STORAGE_KEY, priceStore);
    }}

    function sleep(ms) {{
      return new Promise(resolve => setTimeout(resolve, ms));
    }}

    function parsePricePL(text) {{
      if (!text) return null;
      const m = String(text).match(/[\\d]+[.,]\\d+|\\d+/);
      if (!m) return null;
      return parseFloat(m[0].replace(',', '.'));
    }}

    function formatDate(iso) {{
      if (!iso) return null;
      const d = new Date(iso);
      if (isNaN(d.getTime())) return null;
      return d.toLocaleString('pl-PL', {{dateStyle: 'medium', timeStyle: 'short'}});
    }}

    function latestHistoryDate(history) {{
      if (!history || !history.length) return null;
      return history[history.length - 1].date || null;
    }}

    function updateCardPrice(card, sell, buy, dateStr) {{
      const btn = card.querySelector('.steam-price-btn');
      const sellHtml = (sell !== null && sell !== undefined)
        ? `<span class="sell">↓ ${{sell.toFixed(2)}} zł</span>`
        : '<span class="sell na">↓ -</span>';
      const buyHtml = (buy !== null && buy !== undefined)
        ? `<span class="buy">↑ ${{buy.toFixed(2)}} zł</span>`
        : '<span class="buy na">↑ -</span>';
      btn.innerHTML = sellHtml + buyHtml;
      card.dataset.price = (sell !== null && sell !== undefined) ? sell.toFixed(2) : '-1';
      const formatted = formatDate(dateStr);
      btn.title = formatted ? `Cena z: ${{formatted}}` : '';
    }}

    function applyStoredPrices() {{
      document.querySelectorAll('.card').forEach(card => {{
        const rawName = card.dataset.rawName;
        const entry = priceStore[rawName];
        let embeddedHistory = [];
        try {{ embeddedHistory = JSON.parse(card.dataset.history || '[]'); }} catch (e) {{ /* brak historii */ }}
        if (entry) {{
          const mergedHistory = mergeHistories(embeddedHistory, entry.history || []);
          updateCardPrice(card, entry.sell, entry.buy, latestHistoryDate(mergedHistory));
        }} else if (embeddedHistory.length) {{
          updateCardPrice(card, card.dataset.price !== '-1' ? parseFloat(card.dataset.price) : null, null, latestHistoryDate(embeddedHistory));
        }}
      }});
    }}

    function updateTotals() {{
      let totalSell = 0;
      let totalBuy = 0;
      document.querySelectorAll('.card').forEach(card => {{
        const rawName = card.dataset.rawName;
        const count = parseInt(card.dataset.count) || 1;
        const entry = priceStore[rawName];
        if (entry) {{
          if (entry.sell !== null && entry.sell !== undefined) totalSell += entry.sell * count;
          if (entry.buy !== null && entry.buy !== undefined) totalBuy += entry.buy * count;
        }}
      }});
      totalSellEl.textContent = totalSell.toLocaleString('pl-PL', {{minimumFractionDigits: 2, maximumFractionDigits: 2}}) + ' zł';
      totalBuyEl.textContent = totalBuy.toLocaleString('pl-PL', {{minimumFractionDigits: 2, maximumFractionDigits: 2}}) + ' zł';
      updateSelectionUI();
      checkPriceAlerts();
      return {{ totalSell, totalBuy }};
    }}

    // ---- Alerty: znaczący wzrost ceny względem historycznego minimum ----
    // Próg zależy od ceny (im tańszy przedmiot, tym wyższy próg %, żeby
    // drobne wahania groszowe nie generowały alertów bez sensu).
    function alertThresholdFor(price) {{
      if (price > 1000) return 0.10;
      if (price > 100) return 0.12;   // przedział pośredni - dociągnięty
      if (price > 1) return 0.15;
      return 0.30;
    }}

    function escapeHtml(str) {{
      const div = document.createElement('div');
      div.textContent = str;
      return div.innerHTML;
    }}

    function checkPriceAlerts() {{
      const alertsBox = document.getElementById('alertsBox');
      const alertsList = document.getElementById('alertsList');
      const alertsBoxDown = document.getElementById('alertsBoxDown');
      const alertsListDown = document.getElementById('alertsListDown');
      const upAlerts = [];
      const downAlerts = [];

      document.querySelectorAll('.card').forEach(card => {{
        const rawName = card.dataset.rawName;
        const entry = priceStore[rawName];
        if (!entry || entry.sell === null || entry.sell === undefined) return;

        let embeddedHistory = [];
        try {{ embeddedHistory = JSON.parse(card.dataset.history || '[]'); }} catch (e) {{ /* brak */ }}
        const fullHistory = mergeHistories(embeddedHistory, entry.history || []);

        const sells = fullHistory.map(h => h.sell).filter(v => v !== null && v !== undefined && v > 0);
        if (sells.length < 2) return; // potrzeba przynajmniej 2 punktów, żeby mówić o "historii"

        const current = entry.sell;
        const link = card.querySelector('.steam-price-btn');
        const url = link ? link.href : '#';

        const minHist = Math.min(...sells);
        if (minHist > 0) {{
          const upRatio = (current - minHist) / minHist;
          if (upRatio >= alertThresholdFor(minHist)) {{
            upAlerts.push({{ name: rawName, refPrice: minHist, current, pct: upRatio * 100, url }});
          }}
        }}

        const maxHist = Math.max(...sells);
        if (maxHist > 0) {{
          const downRatio = (maxHist - current) / maxHist;
          if (downRatio >= alertThresholdFor(maxHist)) {{
            downAlerts.push({{ name: rawName, refPrice: maxHist, current, pct: downRatio * 100, url }});
          }}
        }}
      }});

      function renderBox(box, list, alerts, refLabel, sign, pctClass) {{
        if (alerts.length === 0) {{
          box.style.display = 'none';
          list.innerHTML = '';
          return;
        }}
        alerts.sort((a, b) => b.pct - a.pct);
        list.innerHTML = alerts.map(a => `
          <a class="alert-row" href="${{a.url}}" target="_blank" rel="noopener">
            <b>${{escapeHtml(a.name)}}</b>: było ${{a.refPrice.toFixed(2)}} zł (${{refLabel}})
            → teraz ${{a.current.toFixed(2)}} zł
            (<span class="alert-pct ${{pctClass}}">${{sign}}${{a.pct.toFixed(1)}}%</span>)
          </a>
        `).join('');
        box.style.display = 'block';
      }}

      renderBox(alertsBox, alertsList, upAlerts, 'min. histor.', '+', 'up');
      renderBox(alertsBoxDown, alertsListDown, downAlerts, 'maks. histor.', '-', 'down');
    }}

    // ---- Zaznaczanie przedmiotów ----
    function getSelectedCards() {{
      return Array.from(document.querySelectorAll('.card.selected'));
    }}

    function updateSelectionUI() {{
      const selected = getSelectedCards();
      if (selected.length === 0) {{
        selectedTotalBox.style.display = 'none';
        refreshSelectedBtn.style.display = 'none';
        return;
      }}
      let sellSum = 0;
      let buySum = 0;
      selected.forEach(card => {{
        const rawName = card.dataset.rawName;
        const count = parseInt(card.dataset.count) || 1;
        const entry = priceStore[rawName];
        if (entry) {{
          if (entry.sell !== null && entry.sell !== undefined) sellSum += entry.sell * count;
          if (entry.buy !== null && entry.buy !== undefined) buySum += entry.buy * count;
        }}
      }});
      selectedCountEl.textContent = selected.length;
      selectedSellEl.textContent = '↓ ' + sellSum.toLocaleString('pl-PL', {{minimumFractionDigits: 2, maximumFractionDigits: 2}}) + ' zł';
      selectedBuyEl.textContent = '↑ ' + buySum.toLocaleString('pl-PL', {{minimumFractionDigits: 2, maximumFractionDigits: 2}}) + ' zł';
      selectedTotalBox.style.display = 'flex';
      refreshSelectedBtn.style.display = 'inline-block';
    }}

    document.querySelectorAll('.card').forEach(card => {{
      card.addEventListener('click', (e) => {{
        // Kliknięcia w przyciski/linki wewnątrz karty mają swoje własne
        // akcje - nie powinny przy okazji zaznaczać/odznaczać karty.
        if (e.target.closest('.refresh-btn') || e.target.closest('.steam-price-btn') || e.target.closest('.quality-chip')) {{
          return;
        }}
        card.classList.toggle('selected');
        updateSelectionUI();
      }});
    }});

    refreshSelectedBtn.addEventListener('click', () => {{
      startFetching(getSelectedCards());
    }});

    // ---- Wykres łącznej wartości ekwipunku w czasie ----
    let valueChartInstance = null;
    let currentChartRange = 'all';

    function formatZl(value) {{
      return value.toLocaleString('pl-PL', {{minimumFractionDigits: 2, maximumFractionDigits: 2}}) + ' zł';
    }}

    function renderValueChart() {{
      if (valueHistory.length < 2) return;
      if (typeof Chart === 'undefined') {{
        valueChartBox.style.display = 'block';
        valueChartBox.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:8px;">'
          + 'Nie udało się wczytać biblioteki wykresów (Chart.js) - sprawdź blokery reklam/rozszerzenia '
          + 'przeglądarki, które mogą blokować cdn.jsdelivr.net. Masz zapisane '
          + valueHistory.length + ' migawek - dane są bezpieczne, tylko wykres się nie renderuje.</div>';
        return;
      }}
      valueChartBox.style.display = 'block';

      // Filtruj wg wybranego zakresu czasu
      let filtered = valueHistory;
      if (currentChartRange !== 'all') {{
        const days = parseInt(currentChartRange);
        const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
        filtered = valueHistory.filter(h => new Date(h.date).getTime() >= cutoff);
        if (filtered.length < 2) filtered = valueHistory.slice(-2); // zawsze pokaż coś sensownego
      }}

      const sellPoints = filtered.map(h => ({{ x: new Date(h.date), y: h.total_sell }}));
      const buyPoints = filtered.map(h => ({{ x: new Date(h.date), y: h.total_buy }}));

      const ctx = document.getElementById('valueChart');
      if (valueChartInstance) valueChartInstance.destroy();
      valueChartInstance = new Chart(ctx, {{
        type: 'line',
        data: {{
          datasets: [
            {{ label: 'Suma sprzedaży', data: sellPoints, borderColor: '#4ade80', backgroundColor: 'rgba(74,222,128,0.08)', tension: 0.25, fill: true, pointRadius: 3, pointHoverRadius: 5 }},
            {{ label: 'Suma kupna', data: buyPoints, borderColor: '#f87171', backgroundColor: 'rgba(248,113,113,0.08)', tension: 0.25, fill: true, pointRadius: 3, pointHoverRadius: 5 }},
          ]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          interaction: {{ mode: 'nearest', axis: 'x', intersect: false }},
          plugins: {{
            legend: {{ labels: {{ color: '#e8e8e8', font: {{ size: 12 }} }} }},
            tooltip: {{
              callbacks: {{
                title: (items) => items.length ? new Date(items[0].parsed.x).toLocaleString('pl-PL', {{dateStyle: 'medium', timeStyle: 'short'}}) : '',
                label: (item) => `${{item.dataset.label}}: ${{formatZl(item.parsed.y)}}`,
              }}
            }}
          }},
          scales: {{
            x: {{
              type: 'time',
              time: {{ tooltipFormat: 'dd.MM.yyyy HH:mm' }},
              ticks: {{ color: '#9aa0aa', font: {{ size: 10 }}, maxRotation: 0 }},
              grid: {{ color: 'rgba(255,255,255,0.05)' }},
            }},
            y: {{
              ticks: {{
                color: '#9aa0aa',
                font: {{ size: 11 }},
                callback: (value) => value.toLocaleString('pl-PL') + ' zł',
              }},
              grid: {{ color: 'rgba(255,255,255,0.05)' }},
            }}
          }}
        }}
      }});
    }}

    document.querySelectorAll('.range-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentChartRange = btn.dataset.range;
        renderValueChart();
      }});
    }});

    function recordValueHistorySnapshot() {{
      const {{ totalSell, totalBuy }} = updateTotals();
      valueHistory = mergeHistories(valueHistory, [{{
        date: new Date().toISOString(),
        total_sell: Math.round(totalSell * 100) / 100,
        total_buy: Math.round(totalBuy * 100) / 100,
      }}]);
      saveJSONLocal(VALUE_HISTORY_KEY, valueHistory);
      renderValueChart();
    }}

    // ---- Wykres pojedynczego przedmiotu po najechaniu ----
    let itemChartInstance = null;
    document.querySelectorAll('.card').forEach(card => {{
      card.addEventListener('mouseenter', () => {{
        if (typeof Chart === 'undefined') return;
        let history = [];
        try {{ history = JSON.parse(card.dataset.history || '[]'); }} catch (e) {{ /* brak historii */ }}
        const rawName = card.dataset.rawName;
        const stored = priceStore[rawName];
        if (stored && stored.history) {{
          history = mergeHistories(history, stored.history);
        }}
        if (history.length < 2) return;

        const rect = card.getBoundingClientRect();
        let left = rect.left;
        if (left + 280 > window.innerWidth) left = window.innerWidth - 280;
        itemPopover.style.left = left + 'px';
        itemPopover.style.top = (rect.bottom + 6) + 'px';
        itemPopover.style.display = 'block';

        if (itemChartInstance) itemChartInstance.destroy();
        const sellPoints = history.map(h => ({{ x: new Date(h.date), y: h.sell }}));
        const buyPoints = history.map(h => ({{ x: new Date(h.date), y: h.buy }}));
        itemChartInstance = new Chart(itemPopoverCanvas, {{
          type: 'line',
          data: {{
            datasets: [
              {{ label: 'Sprzedaż', data: sellPoints, borderColor: '#4ade80', tension: 0.25, pointRadius: 2 }},
              {{ label: 'Kupno', data: buyPoints, borderColor: '#f87171', tension: 0.25, pointRadius: 2 }},
            ]
          }},
          options: {{
            responsive: false,
            interaction: {{ mode: 'nearest', axis: 'x', intersect: false }},
            plugins: {{
              legend: {{ labels: {{ color: '#e8e8e8', font: {{ size: 9 }} }} }},
              tooltip: {{
                callbacks: {{
                  title: (items) => items.length ? new Date(items[0].parsed.x).toLocaleString('pl-PL', {{dateStyle: 'medium', timeStyle: 'short'}}) : '',
                  label: (item) => `${{item.dataset.label}}: ${{formatZl(item.parsed.y)}}`,
                }}
              }}
            }},
            scales: {{
              x: {{ type: 'time', display: false }},
              y: {{ ticks: {{ color: '#9aa0aa', font: {{ size: 9 }}, callback: (v) => v.toLocaleString('pl-PL') + ' zł' }} }}
            }}
          }}
        }});
      }});
      card.addEventListener('mouseleave', () => {{
        itemPopover.style.display = 'none';
      }});
    }});

    async function fetchPriceOverview(rawName) {{
      const url = 'https://steamcommunity.com/market/priceoverview/?appid=730&currency='
        + PRICE_CURRENCY + '&market_hash_name=' + encodeURIComponent(rawName);
      const resp = await fetch(url);
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      if (!data.success) return null;
      return parsePricePL(data.lowest_price);
    }}

    async function fetchNameId(rawName) {{
      const url = 'https://steamcommunity.com/market/listings/730/' + encodeURIComponent(rawName);
      const resp = await fetch(url);
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const html = await resp.text();
      const m = html.match(/Market_LoadOrderSpread\\(\\s*(\\d+)\\s*\\)/);
      return m ? m[1] : null;
    }}

    async function fetchHighestBuy(nameid) {{
      const url = 'https://steamcommunity.com/market/itemordershistogram?country=PL&language=english&currency='
        + PRICE_CURRENCY + '&item_nameid=' + nameid + '&two_factor=0';
      const resp = await fetch(url);
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      if (!data.success) return null;
      return parsePricePL(data.highest_buy_order);
    }}

    // W trybie serwera lokalny Python robi zapytania do Steam za nas -
    // strona pyta tylko "swój własny" serwer, więc CORS nie ma tu w ogóle
    // zastosowania (to samo pochodzenie: ten sam host i port).
    async function fetchViaLocalServer(rawName) {{
      const resp = await fetch('/local-api/price?name=' + encodeURIComponent(rawName));
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      return await resp.json(); // {{sell, buy}}
    }}

    // Pobiera i zapisuje świeżą cenę dla JEDNEGO przedmiotu. Zwraca
    // {{ok, error}} - ok=false gdy zapytanie się nie udało (do liczników
    // porażek w masowym pobieraniu i do sygnalizacji błędu na przycisku).
    async function refreshOneItem(card) {{
      const rawName = card.dataset.rawName;
      const oldEntry = priceStore[rawName] || {{}};
      let freshSell = null;
      let freshBuy = null;
      let ok = true;
      let error = '';

      if (SERVE_MODE) {{
        try {{
          const data = await fetchViaLocalServer(rawName);
          freshSell = data.sell;
          freshBuy = data.buy;
          if (!data.ok) {{
            ok = false;
            error = 'Steam nie odpowiada (limit/blokada zapytań)';
          }}
        }} catch (e) {{
          ok = false;
          error = e.message || String(e);
          console.error('Błąd lokalnego serwera dla', rawName, e);
        }}
      }} else {{
        try {{
          freshSell = await fetchPriceOverview(rawName);
          if (freshSell === null) ok = false;
        }} catch (e) {{
          ok = false;
          error = e.message || String(e);
          console.error('Błąd pobierania ceny sprzedaży dla', rawName, e);
        }}

        await sleep(250);

        try {{
          const nameid = await fetchNameId(rawName);
          if (nameid) {{
            await sleep(250);
            freshBuy = await fetchHighestBuy(nameid);
          }}
        }} catch (e) {{
          console.error('Błąd pobierania oferty kupna dla', rawName, e);
        }}
      }}

      // Jeśli świeże zapytanie się nie udało, zostaw starą, znaną cenę
      // zamiast nadpisywać ją brakiem danych.
      const sell = (freshSell !== null && freshSell !== undefined) ? freshSell : (oldEntry.sell ?? null);
      let buy = (freshBuy !== null && freshBuy !== undefined) ? freshBuy : (oldEntry.buy ?? null);

      // Przedmioty przy dnie cenowym Steam (ok. 0,12 zł) zwykle nie mają
      // żadnych ofert kupna - przyjmujemy wtedy cenę kupna = cenie sprzedaży.
      if ((buy === null || buy === undefined) && sell !== null && sell <= 0.20) {{
        buy = sell;
      }}

      let history = oldEntry.history || [];
      if ((freshSell !== null && freshSell !== undefined) || (freshBuy !== null && freshBuy !== undefined)) {{
        history = [...history, {{ date: new Date().toISOString(), sell: sell, buy: buy }}].slice(-200);
      }}

      priceStore[rawName] = {{ sell: sell, buy: buy, history: history, updated: Date.now() }};
      updateCardPrice(card, sell, buy, latestHistoryDate(history));
      updateTotals();
      savePriceStore();

      return {{ ok, error }};
    }}

    async function startFetching(targetCards) {{
      const cards = targetCards || Array.from(document.querySelectorAll('.card'));
      fetchBtn.disabled = true;
      refreshSelectedBtn.disabled = true;
      let consecutiveFailures = 0;
      const FAILURE_THRESHOLD = 6;
      let lastError = '';

      for (let i = 0; i < cards.length; i++) {{
        const card = cards[i];
        const rawName = card.dataset.rawName;
        progressEl.textContent = `${{i + 1}}/${{cards.length}} - ${{rawName}}`;

        const {{ ok, error }} = await refreshOneItem(card);
        if (!ok) {{
          consecutiveFailures++;
          lastError = error;
        }} else {{
          consecutiveFailures = 0;
        }}
        await sleep(SERVE_MODE ? 50 : 200);

        // Jeśli kilka prób z rzędu zawiodło, przerwij od razu zamiast
        // męczyć się przez wszystkie przedmioty na próżno.
        if (consecutiveFailures >= FAILURE_THRESHOLD) {{
          const reason = SERVE_MODE
            ? 'Steam blokuje zapytania z Twojego adresu (limit/429)'
            : 'przeglądarka blokuje te zapytania (CORS) lub Steam blokuje limit';
          progressEl.innerHTML = `Zatrzymano po ${{i + 1}}/${{cards.length}}: ${{reason}}. `
            + `Błąd: "${{lastError}}". Otwórz konsolę (F12) po szczegóły.`;
          fetchBtn.disabled = false;
          refreshSelectedBtn.disabled = false;
          return;
        }}
      }}

      recordValueHistorySnapshot();

      progressEl.textContent = `Gotowe: ${{cards.length}}/${{cards.length}}`;
      fetchBtn.disabled = false;
      refreshSelectedBtn.disabled = false;
    }}

    fetchBtn.addEventListener('click', () => {{
      startFetching();
    }});

    document.getElementById('fillMissingBtn').addEventListener('click', () => {{
      const missing = Array.from(document.querySelectorAll('.card')).filter(card => {{
        const entry = priceStore[card.dataset.rawName];
        return !entry || entry.sell === null || entry.sell === undefined
                      || entry.buy === null || entry.buy === undefined;
      }});
      if (missing.length === 0) {{
        progressEl.textContent = 'Wszystkie przedmioty mają już komplet cen.';
        return;
      }}
      startFetching(missing);
    }});

    document.getElementById('addChartPointBtn').addEventListener('click', () => {{
      recordValueHistorySnapshot();
      progressEl.textContent = 'Dodano punkt na wykresie z aktualnych danych.';
    }});

    exportBtn.addEventListener('click', () => {{
      const blob = new Blob([JSON.stringify(priceStore, null, 2)], {{type: 'application/json'}});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'steam_prices.json';
      a.click();
      URL.revokeObjectURL(url);
    }});

    document.getElementById('exportHistoryCsvBtn').addEventListener('click', () => {{
      const rows = [['Data', 'Suma sprzedaz (zl)', 'Suma kupno (zl)']];
      valueHistory.forEach(h => {{
        rows.push([h.date, (h.total_sell ?? '').toString().replace('.', ','), (h.total_buy ?? '').toString().replace('.', ',')]);
      }});
      const csv = rows.map(r => r.map(v => `"${{String(v).replace(/"/g, '""')}}"`).join(';')).join('\\r\\n');
      const blob = new Blob(['\\ufeff' + csv], {{type: 'text/csv;charset=utf-8'}});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'historia_wartosci_ekwipunku.csv';
      a.click();
      URL.revokeObjectURL(url);
    }});

    importInput.addEventListener('change', (e) => {{
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {{
        try {{
          const imported = JSON.parse(reader.result);
          priceStore = Object.assign({{}}, priceStore, imported);
          savePriceStore(priceStore);
          applyStoredPrices();
          updateTotals();
          progressEl.textContent = 'Wczytano ceny z pliku.';
        }} catch (err) {{
          progressEl.textContent = 'Nie udało się wczytać pliku (zły format).';
        }}
      }};
      reader.readAsText(file);
    }});

    // Przycisk odświeżania ceny pojedynczego przedmiotu (prawy górny róg karty)
    document.querySelectorAll('.refresh-btn').forEach(btn => {{
      btn.addEventListener('click', async (e) => {{
        e.preventDefault();
        e.stopPropagation();
        const card = btn.closest('.card');
        btn.classList.add('spinning');
        btn.disabled = true;
        const {{ ok, error }} = await refreshOneItem(card);
        btn.classList.remove('spinning');
        btn.disabled = false;
        if (!ok) {{
          btn.title = `Błąd: ${{error}}`;
        }} else {{
          btn.title = 'Odśwież cenę tego przedmiotu';
        }}
      }});
    }});

    // Zastosuj ceny zapisane wcześniej w tej przeglądarce (jeśli są)
    applyStoredPrices();
    updateTotals();
    renderValueChart();
  </script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


class PriceServerHandler(BaseHTTPRequestHandler):
    """Serwuje wygenerowaną stronę i endpoint /local-api/price, który
    po cichu odpytuje Steam po stronie Pythona - strona pyta tylko
    "siebie samą" (ten sam host:port), więc CORS nie ma tu zastosowania.

    Współdzielone stany (prices_cache, nameid_cache, itd.) są ustawiane
    jako atrybuty klasy przez main() przed startem serwera.
    """

    html_path: str = ""
    prices_cache: Dict = {}
    nameid_cache: Dict = {}
    skip_buy: bool = False
    delay: float = 1.0
    prices_cache_file: str = "prices_cache.json"
    nameid_cache_file: str = "steam_nameid_cache.json"
    item_tags: Dict = {}  # market_hash_name -> (exterior_tag, quality_type_tags)

    def log_message(self, format, *args):
        pass  # main() sam informuje o statusie - wyciszamy domyślne logi

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/inventory.html", "/index.html"):
            self._serve_html()
        elif parsed.path == "/local-api/price":
            self._serve_price(parsed)
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_html(self):
        try:
            with open(self.html_path, "rb") as f:
                content = f.read()
        except OSError:
            self.send_response(500)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_price(self, parsed):
        qs = parse_qs(parsed.query)
        name = (qs.get("name") or [""])[0]
        if not name:
            self.send_response(400)
            self.end_headers()
            return

        exterior_tag, quality_type_tags = self.item_tags.get(name, ("", []))
        result = fetch_and_cache_price(
            name, self.prices_cache, self.nameid_cache,
            skip_buy=self.skip_buy, delay=self.delay,
            exterior_tag=exterior_tag, quality_type_tags=quality_type_tags,
        )
        # Zapisz na bieżąco, żeby nic nie zgubić nawet przy przerwaniu
        save_json_dict(self.prices_cache_file, self.prices_cache)
        save_json_dict(self.nameid_cache_file, self.nameid_cache)

        body = json.dumps({
            "sell": result["sell"],
            "buy": result["buy"],
            "ok": result["fresh_sell_ok"],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="Ekwipunek CS2 (+ opcjonalnie ceny CSFloat/Steam)")
    parser.add_argument("--steamid", required=True, help="Twój SteamID64 (17 cyfr)")
    parser.add_argument(
        "--cache-file",
        default="inventory_cache.json",
        help="Plik, w którym zapisywany/wczytywany jest ekwipunek (domyślnie inventory_cache.json)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Pomiń cache i pobierz ekwipunek od nowa ze Steam",
    )
    parser.add_argument(
        "--prices",
        action="store_true",
        help="Dodatkowo pobierz ceny: CSFloat (sprzedaż) oraz Steam (sprzedaż i kupno)",
    )
    parser.add_argument("--csv", default=None, help="Ścieżka do pliku CSV z wynikami (opcjonalnie)")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.4,
        help="Opóźnienie (s) między zapytaniami cenowymi, domyślnie 0.4s "
             "(zwiększ np. do 2-5, jeśli zauważysz blokady/429)",
    )
    parser.add_argument(
        "--nameid-cache-file",
        default="steam_nameid_cache.json",
        help="Plik z trwałym cache ID przedmiotów Steam potrzebnym do ofert kupna",
    )
    parser.add_argument(
        "--skip-steam-buy",
        action="store_true",
        help="Pomiń pobieranie najwyższej oferty kupna ze Steam (szybsze, mniej zapytań)",
    )
    parser.add_argument(
        "--html",
        nargs="?",
        const="inventory.html",
        default=None,
        metavar="PLIK",
        help="Wygeneruj interaktywną stronę HTML z ekwipunkiem (domyślnie inventory.html)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Otwórz wygenerowaną stronę HTML automatycznie w przeglądarce",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Pokaż szczegółowe informacje diagnostyczne o zapytaniach cenowych (statusy HTTP, treść odpowiedzi)",
    )
    parser.add_argument(
        "--show-all",
        action="store_true",
        help="Pokaż też przedmioty, których nie da się sprzedać (medale, coiny) - domyślnie są ukryte",
    )
    parser.add_argument(
        "--live-update-every",
        type=int,
        default=15,
        metavar="N",
        help="Przy --prices i --html, odświeżaj plik HTML co N przetworzonych przedmiotów (domyślnie 15, 0 = wyłącz)",
    )
    parser.add_argument(
        "--prices-cache-file",
        default="prices_cache.json",
        help="Plik z trwałym cache cen - przedmioty już w nim zapisane nie są pobierane ponownie",
    )
    parser.add_argument(
        "--refresh-prices",
        action="store_true",
        help="Ignoruj cache cen i pobierz wszystkie ceny od nowa",
    )
    parser.add_argument(
        "--value-history-file",
        default="value_history.json",
        help="Plik z historią łącznej wartości ekwipunku (do wykresu)",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Uruchom lokalny serwer: przycisk 'Pobierz ceny' w przeglądarce działa przez "
             "Pythona (bez ryzyka CORS). Otwiera stronę automatycznie, działa do Ctrl+C.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port lokalnego serwera przy --serve (domyślnie 8765)",
    )
    parser.add_argument(
        "--login-cookie",
        default=None,
        metavar="WARTOŚĆ",
        help="Wartość ciasteczka steamLoginSecure z Twojej zalogowanej przeglądarki - "
             "sprawia, że zapytania o ceny wyglądają jak z Twojego konta (opcjonalne)",
    )
    args = parser.parse_args()

    global DEBUG_PRICES
    DEBUG_PRICES = args.debug

    global _steam_login_cookie
    _steam_login_cookie = args.login_cookie

    items = None
    if not args.refresh:
        items = load_cache(args.cache_file)
        if items:
            print(f"Wczytano ekwipunek z cache ({args.cache_file}) - {len(items)} unikalnych przedmiotów.")
            print("Jeśli chcesz pobrać świeże dane ze Steam, uruchom z flagą --refresh.")

    if items is None:
        print(f"Pobieram ekwipunek CS2 dla SteamID {args.steamid} (może to chwilę potrwać)...")
        try:
            items = fetch_steam_inventory(args.steamid, include_non_marketable=args.show_all)
        except RuntimeError as e:
            print(f"BŁĄD: {e}", file=sys.stderr)
            sys.exit(1)
        save_cache(args.cache_file, items)
        print(f"Zapisano ekwipunek do cache: {args.cache_file}")

    if not items:
        print("Ekwipunek jest pusty albo nie zawiera przedmiotów CS2.")
        sys.exit(0)

    print_inventory(items)

    value_history = load_json_dict(args.value_history_file)
    if not isinstance(value_history, list):
        value_history = []

    # Wczytaj poprzednio zapisaną historię cen per przedmiot z cache,
    # nawet jeśli w tym uruchomieniu nie pobieramy nowych cen.
    existing_prices_cache = load_json_dict(args.prices_cache_file)
    if isinstance(existing_prices_cache, dict):
        for it in items:
            entry = existing_prices_cache.get(it["market_hash_name"])
            if entry:
                it.setdefault("steam_lowest", entry.get("sell"))
                it.setdefault("steam_highest_buy", entry.get("buy"))
                it["price_history"] = entry.get("history", [])

    if args.serve:
        if CURL_CFFI_AVAILABLE:
            print("curl_cffi wykryte - zapytania będą naśladować przeglądarkę Chrome (TLS).\n")
        else:
            print(
                "Uwaga: biblioteka curl_cffi nie jest zainstalowana - zapytania będą\n"
                "wyglądać jak standardowy Python, co Steam może łatwiej zablokować.\n"
                "Zalecane: zatrzymaj to (Ctrl+C), zainstaluj: pip install curl_cffi\n"
                "i uruchom ponownie.\n"
            )
        nameid_cache = load_json_dict(args.nameid_cache_file)
        stale_failures = [k for k, v in nameid_cache.items() if v is None]
        for k in stale_failures:
            del nameid_cache[k]

        prices_cache = existing_prices_cache if isinstance(existing_prices_cache, dict) else {}
        item_tags = {
            it["market_hash_name"]: (it.get("exterior_tag", ""), it.get("quality_type_tags", []))
            for it in items
        }

        html_path = args.html or "inventory.html"
        generate_html(items, html_path, value_history, serve_mode=True)
        html_abs = os.path.abspath(html_path)

        handler_cls = type(
            "BoundPriceServerHandler",
            (PriceServerHandler,),
            {
                "html_path": html_abs,
                "prices_cache": prices_cache,
                "nameid_cache": nameid_cache,
                "skip_buy": args.skip_steam_buy,
                "delay": args.delay,
                "prices_cache_file": args.prices_cache_file,
                "nameid_cache_file": args.nameid_cache_file,
                "item_tags": item_tags,
            },
        )
        server = ThreadingHTTPServer(("0.0.0.0", args.port), handler_cls)
        url = f"http://0.0.0.0:{args.port}/"
        print(f"\nSerwer działa pod adresem: {url}")
        print("Otwórz stronę (powinna się otworzyć sama) i kliknij 'Pobierz ceny ze Steam'.")
        print("Ten sposób NIE ma problemu z CORS - jedyne ryzyko to nadal ewentualna blokada Steam.")
        print("Zamknij to okno / wciśnij Ctrl+C tutaj, kiedy skończysz.\n")
        webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nZatrzymano serwer. Pobrane ceny zostały zapisane w cache.")
        return

    if not args.prices:
        print("\n(Ceny pominięte - dodaj flagę --prices, żeby je pobrać.)")
        if args.html:
            generate_html(items, args.html, value_history)
            html_path = os.path.abspath(args.html)
            print(f"\nWygenerowano interaktywną stronę: {html_path}")
            if args.open:
                webbrowser.open(f"file://{html_path}")
            else:
                print("Otwórz ten plik dwuklikiem albo dodaj flagę --open, żeby otworzył się sam.")
        return

    print(
        f"\nPobieram ceny Steam dla {len(items)} przedmiotów "
        f"(sprzedaż" + ("" if args.skip_steam_buy else " i kupno") + ")...\n"
    )

    nameid_cache = load_json_dict(args.nameid_cache_file)
    stale_failures = [k for k, v in nameid_cache.items() if v is None]
    if stale_failures:
        for k in stale_failures:
            del nameid_cache[k]
        print(
            f"Czyszczę {len(stale_failures)} zapamiętanych wcześniej porażek w cache ID "
            "przedmiotów - spróbujemy ich ponownie.\n"
        )
    nameid_cache_dirty = False

    prices_cache = load_json_dict(args.prices_cache_file)
    prices_cache_dirty = False
    already_cached = 0 if args.refresh_prices else sum(1 for it in items if it["market_hash_name"] in prices_cache)
    if already_cached:
        print(
            f"{already_cached}/{len(items)} przedmiotów ma już zapisane ceny w cache "
            f"({args.prices_cache_file}) - pomijam je. Użyj --refresh-prices, żeby odświeżyć wszystko.\n"
        )

    results = []
    steam_blocked = False
    consecutive_steam_failures = 0
    STEAM_FAILURE_THRESHOLD = 5

    for i, item in enumerate(items, 1):
        name = item["market_hash_name"]
        count = item["count"]

        cached = None if args.refresh_prices else prices_cache.get(name)

        if cached is not None:
            steam_lowest = cached.get("sell")
            steam_highest_buy = cached.get("buy")
        elif not steam_blocked:
            result = fetch_and_cache_price(
                name, prices_cache, nameid_cache,
                skip_buy=args.skip_steam_buy, delay=args.delay,
                exterior_tag=item.get("exterior_tag", ""),
                quality_type_tags=item.get("quality_type_tags", []),
            )
            steam_lowest = result["sell"]
            steam_highest_buy = result["buy"]
            nameid_cache_dirty = True
            prices_cache_dirty = True

            if not result["fresh_sell_ok"]:
                consecutive_steam_failures += 1
            else:
                consecutive_steam_failures = 0

            if consecutive_steam_failures >= STEAM_FAILURE_THRESHOLD:
                steam_blocked = True
                print(
                    f"\n!!! Steam nie odpowiada poprawnie od {STEAM_FAILURE_THRESHOLD} "
                    "przedmiotów z rzędu - prawdopodobnie chwilowa blokada/limit.\n"
                    "    Pomijam dalsze zapytania do końca tego przebiegu.\n"
                    "    Spróbuj ponownie później - to co już pobrane zostało zapisane w cache.\n"
                )
        else:
            steam_lowest = None
            steam_highest_buy = None

        item["steam_lowest"] = steam_lowest
        item["steam_highest_buy"] = steam_highest_buy
        item["price_history"] = prices_cache.get(name, {}).get("history", [])

        results.append(
            {
                "name": name,
                "count": count,
                "steam_lowest": steam_lowest,
                "steam_highest_buy": steam_highest_buy,
            }
        )

        def fmt(p):
            return f"{p:,.2f} zł" if p is not None else "-"

        cache_marker = " (z cache)" if cached is not None else ""
        print(
            f"[{i}/{len(items)}] {name} x{count} -> "
            f"Steam sprzedaż: {fmt(steam_lowest)} | "
            f"Steam kupno: {fmt(steam_highest_buy)}{cache_marker}"
        )

        # Zapisuj cache nameid i cen co jakiś czas, żeby nie stracić postępu
        if nameid_cache_dirty and i % 20 == 0:
            save_json_dict(args.nameid_cache_file, nameid_cache)
        if prices_cache_dirty and i % 20 == 0:
            save_json_dict(args.prices_cache_file, prices_cache)

        # Odśwież podgląd HTML co jakiś czas, żeby nie czekać do samego końca
        if args.html and args.live_update_every > 0 and i % args.live_update_every == 0:
            generate_html(items, args.html, value_history)

    if nameid_cache_dirty:
        save_json_dict(args.nameid_cache_file, nameid_cache)
    if prices_cache_dirty:
        save_json_dict(args.prices_cache_file, prices_cache)
        print(f"\nZapisano ceny do trwałego cache: {args.prices_cache_file}")

        # Zapisz też migawkę łącznej wartości ekwipunku do wykresu historii
        snapshot_sell = sum(
            (it["steam_lowest"] or 0) * it["count"] for it in items if it.get("steam_lowest") is not None
        )
        snapshot_buy = sum(
            (it["steam_highest_buy"] or 0) * it["count"] for it in items if it.get("steam_highest_buy") is not None
        )
        value_history = load_json_dict(args.value_history_file)
        if not isinstance(value_history, list):
            value_history = []
        value_history.append(
            {
                "date": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                "total_sell": round(snapshot_sell, 2),
                "total_buy": round(snapshot_buy, 2),
            }
        )
        value_history = value_history[-500:]
        with open(args.value_history_file, "w", encoding="utf-8") as f:
            json.dump(value_history, f, ensure_ascii=False, indent=2)
        print(f"Zapisano migawkę łącznej wartości do: {args.value_history_file}")

    results.sort(key=lambda r: (r["steam_lowest"] is None, -(r["steam_lowest"] or 0)))

    print("\n" + "=" * 70)
    print(f"{'Przedmiot':<45}{'Ilość':>6}{'Steam sprz.':>10}{'Steam kupno':>13}")
    print("-" * 70)
    for r in results:
        name_display = (r["name"][:42] + "...") if len(r["name"]) > 45 else r["name"]

        def fmt2(p):
            return f"{p:,.2f} zł" if p is not None else "-"

        print(f"{name_display:<45}{r['count']:>6}{fmt2(r['steam_lowest']):>10}{fmt2(r['steam_highest_buy']):>13}")
    print("=" * 70)

    if args.html:
        generate_html(items, args.html, value_history)
        html_path = os.path.abspath(args.html)
        print(f"\nWygenerowano interaktywną stronę: {html_path}")
        if args.open:
            webbrowser.open(f"file://{html_path}")
        else:
            print("Otwórz ten plik dwuklikiem albo dodaj flagę --open, żeby otworzył się sam.")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["market_hash_name", "ilosc", "steam_sprzedaz_usd", "steam_kupno_usd"])
            for r in results:
                def fmt3(p):
                    return f"{p:.2f}" if p is not None else ""

                writer.writerow([r["name"], r["count"], fmt3(r["steam_lowest"]), fmt3(r["steam_highest_buy"])])
        print(f"\nZapisano wyniki do: {args.csv}")


if __name__ == "__main__":
    main()
