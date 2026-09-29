#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Twitch Drops — трекер прогресса + автоматический клейм наград.

Что делает:
  * опрашивает GQL Twitch и показывает прогресс по всем активным кампаниям;
  * сам нажимает "Claim" (dropsPage_claimDropRewards), как только дропс дозрел;
  * предупреждает, когда до конца кампании осталось мало времени;
  * логирует забранные награды в claims.csv.

Чего НЕ делает: не накручивает watch-time. Смотреть стрим нужно самому.
Программа только читает ваш прогресс и забирает готовые награды.

Запуск:
  pip install -r requirements.txt
  python twitch_drops.py --token ВАШ_AUTH_TOKEN
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import secrets
import string
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

# На Windows консоль по умолчанию cp866/cp1251 — принудительно UTF-8,
# иначе русский текст в выводе превращается в кракозябры.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:
    import requests
except ImportError:
    sys.exit("Не найден модуль requests. Установите его: pip install requests")


# --------------------------------------------------------------------------- #
#  Константы GQL
# --------------------------------------------------------------------------- #

GQL_URL = "https://gql.twitch.tv/gql"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"

# За токеном нужен доступ к аккаунту пользователя; user:read:email — чтобы
# отличать один аккаунт от другого при нескольких входах.
OAUTH_SCOPES = ["user:read:email"]

# persisted query: инвентарь дропсов текущего пользователя
PQ_INVENTORY = {
    "version": 1,
    "sha256Hash": "8337eb8541b314040b0edde0c09c5c7a2783ba1960aa9edfbf3bac16d0fec404",
}

# persisted query: мутация "забрать награду"
PQ_CLAIM = {
    "version": 1,
    "sha256Hash": "a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930",
}

# Client-ID мобильного приложения Twitch. В отличие от веб-клиента
# (kimne78kx3ncx6brgo4mv6wki5h1ko) он не требует Client-Integrity
# (браузерный JS-челлендж Kasada), поэтому мутации работают из Python.
CLIENT_ID_ANDROID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
CLIENT_ID_WEB = "kimne78kx3ncx6brgo4mv6wki5h1ko"

USER_AGENT_ANDROID = (
    "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 7 Build/UQ1A.240205.002) "
    "tv.twitch.android.app/25.3.0/2503006"
)
USER_AGENT_WEB = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

def app_dir() -> Path:
    """
    Папка, где лежат config.json, claims.csv и ui/.

    В собранном .exe файлы должны лежать РЯДО с ним, но __file__ внутри
    onefile-сборки указывает во временный каталог распаковки. Поэтому
    берём путь к самому исполняемому файлу.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Папка с ресурсами: временная при сборке, иначе рядом со скриптом."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", str(app_dir())))
    return app_dir()


CONFIG_PATH = app_dir() / "config.json"
CLAIMS_PATH = app_dir() / "claims.csv"

# Статусы ответа claimDropRewards, которые считаем успехом
CLAIM_OK = {"ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED"}


# --------------------------------------------------------------------------- #
#  Модели
# --------------------------------------------------------------------------- #


@dataclass
class Channel:
    """Канал, на котором капает дропс."""

    name: str
    url: str


@dataclass
class Drop:
    """Один предмет внутри кампании."""

    campaign_id: str
    campaign_name: str
    game_name: str
    drop_id: str
    drop_name: str
    benefit_name: str
    seconds_needed: int
    seconds_watched: int
    is_claimed: bool
    preconditions_met: bool
    drop_instance_id: str | None
    account_connected: bool = True
    end_at: str | None = None
    # --- поля, которые Twitch отдаёт, а программа раньше выбрасывала --- #
    campaign_status: str = ""
    start_at: str | None = None
    channels: list[Channel] = field(default_factory=list)
    prerequisite_names: list[str] = field(default_factory=list)
    detail_url: str = ""
    account_link_url: str = ""
    benefit_url: str = ""
    game_slug: str = ""
    is_event_based: bool = False

    @property
    def percent(self) -> float:
        if self.is_claimed:
            return 100.0
        if self.seconds_needed <= 0:
            return 0.0
        return min(100.0, self.seconds_watched / self.seconds_needed * 100.0)

    @property
    def seconds_left(self) -> int:
        return max(0, self.seconds_needed - self.seconds_watched)

    @property
    def can_claim(self) -> bool:
        return (
            not self.is_claimed
            and self.drop_instance_id is not None
            and self.seconds_watched >= self.seconds_needed
            and self.preconditions_met
        )

    @property
    def is_finished(self) -> bool:
        """Дропс закрыт: забран или требует времени, которого уже не набрать."""
        return self.is_claimed or self.seconds_watched >= self.seconds_needed


@dataclass
class Viewer:
    """Ответ /oauth2/validate — кто мы."""

    user_id: str = ""
    login: str = ""
    client_id: str = ""
    expires_in: int = 0

    @property
    def expires_at(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self.expires_in)


# --------------------------------------------------------------------------- #
#  Конфиг
# --------------------------------------------------------------------------- #


DEFAULT_CONFIG: dict[str, Any] = {
    "auth_token": "",
    "client_id": CLIENT_ID_ANDROID,
    "device_id": "",
    "poll_interval": 60,
    "auto_claim": True,
    "claim_retry": 3,
    "fetch_reward_campaigns": False,
    "warn_minutes_before_end": 30,
    "sound": True,
    "log_claims": True,
    "max_failures": 10,
    "show_channels": True,
    "max_channels": 8,
    # --- OAuth (нужен, чтобы использовать собственный Client-ID) ---
    "oauth_client_id": "",
    "oauth_client_secret": "",
    "oauth_redirect_uri": "http://localhost:8765",
    "refresh_token": "",
    "token_expires_at": 0,
}


def _random_device_id() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(32))


def load_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Читает config.json, дополняет дефолтами, применяет аргументы CLI."""
    config = dict(DEFAULT_CONFIG)

    if CONFIG_PATH.exists():
        try:
            # utf-8-sig, а не utf-8: config.json часто правят в Блокноте,
            # VS Code или PowerShell, и они добавляют BOM — обычный utf-8
            # тогда не спарсит файл.
            raw = CONFIG_PATH.read_text(encoding="utf-8-sig")
            config.update(json.loads(raw))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[!] config.json не читается ({exc}) — использую значения по умолчанию.",
                  file=sys.stderr)

    if overrides:
        config.update({k: v for k, v in overrides.items() if v is not None})

    config["auth_token"] = (
        os.environ.get("TWITCH_AUTH_TOKEN") or config.get("auth_token") or ""
    ).strip()
    if config["auth_token"].lower().startswith("oauth "):
        config["auth_token"] = config["auth_token"][6:].strip()

    if not config.get("device_id"):
        config["device_id"] = _random_device_id()
        _save(config)

    return config


def _save(config: dict[str, Any]) -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass  # не критично — токен всё равно в памяти


# --------------------------------------------------------------------------- #
#  API-клиент
# --------------------------------------------------------------------------- #


class TwitchError(Exception):
    """Ошибка, из которой имеет смысл показать пользователю текст."""


class TokenInvalid(TwitchError):
    pass


class NeedClientIntegrity(TwitchError):
    pass


# --------------------------------------------------------------------------- #
#  OAuth (режим --login)
# --------------------------------------------------------------------------- #


class _CallbackHandler(BaseHTTPRequestHandler):
    """Одноразовый обработчик: ловит ?code=... и глушит лог."""

    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 — имя задано BaseHTTPRequestHandler
        params = parse_qs(urlparse(self.path).query)
        if "code" in params:
            _CallbackHandler.code = params["code"][0]
            body = (
                "<html><meta charset=utf-8><body style='font:16px sans-serif;"
                "padding:40px'><h2>Готово</h2><p>Можно закрыть вкладку и вернуться"
                " в консоль.</p></body></html>"
            )
            status = 200
        else:
            _CallbackHandler.error = params.get(
                "error_description", params.get("error", "unknown")
            )[0]
            body = (
                "<html><meta charset=utf-8><body style='font:16px sans-serif;"
                "padding:40px'><h2>Ошибка авторизации</h2><p>"
                f"{_CallbackHandler.error}</p></body></html>"
            )
            status = 400

        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: Any) -> None:
        pass


def oauth_login(config: dict[str, Any]) -> dict[str, Any]:
    """
    Полноценный OAuth authorization-code flow.

    Нужен, чтобы работал ВАШ собственный Client-ID: токен из cookie привязан
    к приложению, которое его выдало, и со сторонним Client-ID GQL его
    отвергает (401). Здесь Twitch выпускает токен уже под ваше приложение.
    """
    client_id = (config.get("oauth_client_id") or "").strip()
    client_secret = (config.get("oauth_client_secret") or "").strip()
    redirect_uri = (config.get("oauth_redirect_uri") or "http://localhost:8765").strip()

    if not client_id:
        raise TwitchError(
            "Не задан oauth_client_id в config.json.\n"
            "    Укажите идентификатор из https://dev.twitch.tv/console/apps"
        )
    if not client_secret:
        raise TwitchError(
            "Не задан oauth_client_secret в config.json.\n"
            "    Секрет виден в консоли Twitch сразу после создания приложения."
        )

    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1"):
        raise TwitchError(
            f"redirect_uri ({redirect_uri}) должен указывать на localhost.\n"
            f"    Впишите в OAuth Redirect URLs в консоли Twitch ровно: {redirect_uri}\n"
            "    Например: http://localhost:8765"
        )

    state = secrets.token_urlsafe(24)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(OAUTH_SCOPES),
        "state": state,
    }
    url = f"{AUTHORIZE_URL}?{urlencode(params)}"

    _CallbackHandler.code = None
    _CallbackHandler.error = None

    server = HTTPServer((parsed.hostname, parsed.port or 80), _CallbackHandler)
    host, port = server.server_address[0], server.server_address[1]
    shown = f"http://{host}:{port}"
    print(f"Открываю браузер. Если не открылся — вставьте адрес вручную:\n  {url}\n")
    print(f"Жду подтверждения на {shown} ...")

    try:
        webbrowser.open(url)
        server.timeout = 300
        deadline = time.time() + 300
        while _CallbackHandler.code is None and _CallbackHandler.error is None:
            if time.time() > deadline:
                raise TwitchError("Не дождался подтверждения за 5 минут.")
            server.handle_request()
    finally:
        server.server_close()

    if _CallbackHandler.error:
        raise TwitchError(f"Twitch отклонил авторизацию: {_CallbackHandler.error}")

    code = _CallbackHandler.code
    if not code:
        raise TwitchError("Twitch не вернул код авторизации.")

    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        raise TwitchError(f"Сеть недоступна при обмене кода на токен: {exc}") from exc

    if resp.status_code != 200:
        detail = resp.text[:300]
        raise TwitchError(f"Twitch отверг обмен кода (HTTP {resp.status_code}): {detail}")

    data = resp.json()
    if "access_token" not in data:
        raise TwitchError(f"В ответе нет access_token: {str(data)[:300]}")

    data["obtained_at"] = int(time.time())
    return data


def oauth_refresh(config: dict[str, Any], token_data: dict[str, Any]) -> dict[str, Any]:
    """Обновляет access_token по refresh_token."""
    refresh = token_data.get("refresh_token") or config.get("refresh_token") or ""
    if not refresh:
        raise TwitchError("Нет refresh_token — выполните --login заново.")

    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": (config.get("oauth_client_id") or "").strip(),
                "client_secret": (config.get("oauth_client_secret") or "").strip(),
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        raise TwitchError(f"Сеть недоступна при обновлении токена: {exc}") from exc

    if resp.status_code != 200:
        raise TwitchError(
            f"Не удалось обновить токен (HTTP {resp.status_code}): {resp.text[:300]}"
        )

    data = resp.json()
    # Twitch не всегда возвращает новый refresh_token — тогда берём прежний.
    data.setdefault("refresh_token", refresh)
    data["obtained_at"] = int(time.time())
    return data


def token_expiry(token_data: dict[str, Any]) -> int:
    """Unix-время истечения токена с учётом того, когда он получен."""
    try:
        obtained = int(token_data.get("obtained_at") or 0)
        expires = int(token_data.get("expires_in") or 0)
    except (TypeError, ValueError):
        return 0
    return obtained + expires if obtained and expires else 0


class DropsClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.token = config["auth_token"]
        self.device_id = config["device_id"]
        self.client_id = config["client_id"]
        self.session = requests.Session()
        self._history: list[dict[str, Any]] = []
        self._configure_session()

    # -- HTTP ------------------------------------------------------------- #

    def _configure_session(self) -> None:
        ua = (
            USER_AGENT_ANDROID
            if self.client_id != CLIENT_ID_WEB
            else USER_AGENT_WEB
        )
        self.session.headers.update(
            {
                "Client-ID": self.client_id,
                # ВАЖНО: Twitch принимает только префикс "OAuth".
                # С "Bearer" или "oauth" токен молча игнорируется.
                "Authorization": f"OAuth {self.token}",
                "Content-Type": "application/json",
                "User-Agent": ua,
                "X-Device-Id": self.device_id,
                "Accept": "application/json",
            }
        )

    def switch_to_web_client(self) -> None:
        """Откат на веб-клиент (нужен, если вдруг мобильный Client-ID отозвали)."""
        self.client_id = CLIENT_ID_WEB
        self.config["client_id"] = CLIENT_ID_WEB
        self.session.headers["Client-ID"] = CLIENT_ID_WEB
        self.session.headers["User-Agent"] = USER_AGENT_WEB

    def _gql(
        self,
        operation_name: str,
        variables: dict[str, Any],
        persisted_query: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "operationName": operation_name,
            "variables": variables,
            "extensions": {"persistedQuery": persisted_query},
        }
        try:
            resp = self.session.post(GQL_URL, json=payload, timeout=20)
        except requests.RequestException as exc:
            raise TwitchError(f"Сеть недоступна: {exc}") from exc

        if resp.status_code == 401:
            raise TokenInvalid("Токен недействителен или истёк (HTTP 401).")
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            raise TwitchError(f"Rate limit от Twitch, ждите {retry_after} с.")

        try:
            body = resp.json()
        except ValueError as exc:
            raise TwitchError(
                f"Twitch вернул не-JSON (HTTP {resp.status_code})."
            ) from exc

        errors = body.get("errors") or []
        for err in errors:
            code = (err.get("extensions") or {}).get("code", "")
            message = err.get("message", "")
            if code == "IntegrityCheckFailed":
                raise NeedClientIntegrity(
                    "Twitch требует Client-Integrity для этого клиента.\n"
                    "    Выбранный Client-ID — браузерный. Укажите в config.json\n"
                    f'    "client_id": "{CLIENT_ID_ANDROID}" (мобильное приложение),\n'
                    "    для него проверка целостности не выполняется."
                )
            if code in {"PersistedQueryNotFound", "PersistedQueryNotSupported"}:
                raise TwitchError(
                    f"Twitch больше не знает запрос {operation_name!r}.\n"
                    "    Операция сменилась — нужно обновить sha256Hash в PQ_INVENTORY\n"
                    "    / PQ_CLAIM (они лежат в начале этого файла).\n"
                    f"    Детали: {message}"
                )
            if code in {"Unauthenticated", "Unauthorized"} or "unauthenticated" in message.lower():
                raise TokenInvalid("Twitch не признаёт токен (unauthenticated).")

        if errors:
            raise TwitchError(
                "Ошибка GQL: " + "; ".join(e.get("message", "?") for e in errors)
            )

        return body.get("data") or {}

    # -- Публичные вызовы -------------------------------------------------- #

    def validate(self) -> Viewer:
        """Проверяет токен и узнаёт, под кем мы вошли."""
        try:
            resp = self.session.get(VALIDATE_URL, timeout=15)
        except requests.RequestException as exc:
            raise TwitchError(f"Сеть недоступна: {exc}") from exc

        if resp.status_code in (401, 403):
            raise TokenInvalid(
                "Токен отвергнут Twitch'ом. Скопируйте свежий auth-token из браузера."
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise TwitchError("Не удалось разобрать ответ /oauth2/validate.") from exc

        return Viewer(
            user_id=str(data.get("user_id", "")),
            login=data.get("login", ""),
            client_id=data.get("client_id", ""),
            expires_in=int(data.get("expires_in", 0)),
        )

    def fetch_drops(self) -> list[Drop]:
        """Возвращает список дропсов по всем кампаниям в процессе."""
        data = self._gql(
            "Inventory",
            {"fetchRewardCampaigns": bool(self.config["fetch_reward_campaigns"])},
            PQ_INVENTORY,
        )

        current_user = data.get("currentUser")
        if not current_user:
            raise TokenInvalid(
                "GQL вернул currentUser: null — токен не подхватился. "
                "Проверьте, что это auth-token из cookie twitch.tv."
            )

        inventory = current_user.get("inventory") or {}
        campaigns = inventory.get("dropCampaignsInProgress") or []
        self._history = inventory.get("gameEventDrops") or []
        return parse_campaigns(campaigns)

    def fetch_history(self) -> list[dict[str, Any]]:
        """Уже заработанные награды (gameEventDrops)."""
        if not self._history:
            try:
                self.fetch_drops()
            except TwitchError:
                pass
        return self._history

    def claim(self, drop_instance_id: str) -> tuple[bool, str]:
        """Забирает награду. Возвращает (успех, статус)."""
        data = self._gql(
            "DropsPage_ClaimDropRewards",
            {"input": {"dropInstanceID": drop_instance_id}},
            PQ_CLAIM,
        )

        result = data.get("claimDropRewards") or {}
        status = result.get("status", "UNKNOWN")

        if status in CLAIM_OK:
            return True, status
        if status == "NOT_ELIGIBLE":
            return False, "NOT_ELIGIBLE (ещё не дозрел)"
        return False, status


def parse_campaigns(campaigns: list[dict[str, Any]]) -> list[Drop]:
    """
    Разбирает список кампаний Twitch в объекты Drop.

    Используется дважды: для dropCampaignsInProgress из Inventory и для
    каталога, импортированного из файла (--import-catalog) — структура
    у них одинаковая.
    """
    drops: list[Drop] = []

    for campaign in campaigns:
        campaign_id = str(campaign.get("id", ""))
        campaign_name = campaign.get("name", "?")
        game_obj = campaign.get("game") or {}
        game = game_obj.get("displayName") or game_obj.get("name") or "?"
        account_connected = bool(
            ((campaign.get("self") or {}).get("isAccountConnected"))
        )
        end_at = campaign.get("endAt")

        # Список каналов, на которых капает дропс. У nopixel V их 621.
        channels = [
            Channel(name=ch.get("name", ""), url=ch.get("url", ""))
            for ch in ((campaign.get("allow") or {}).get("channels") or [])
            if ch.get("name")
        ]

        all_drops = list(campaign.get("timeBasedDrops") or []) + list(
            campaign.get("eventBasedDrops") or []
        )
        # Имена всех дропсов кампании — нужны, чтобы показать предпосылки.
        name_by_id = {str(d.get("id", "")): d.get("name", "?") for d in all_drops}

        for drop in all_drops:
            own = drop.get("self") or {}
            required = int(drop.get("requiredMinutesWatched") or 0)
            watched = int(own.get("currentMinutesWatched") or 0)

            benefits = drop.get("benefitEdges") or []
            benefit_name = "?"
            benefit_url = ""
            if benefits:
                benefit = benefits[0].get("benefit") or {}
                benefit_name = benefit.get("name", "?")
                benefit_url = benefit.get("imageAssetURL", "") or ""

            prerequisites = [
                name_by_id.get(str(pid), "")
                for pid in (drop.get("preconditionDrops") or [])
            ]
            prerequisites = [p for p in prerequisites if p]

            drops.append(
                Drop(
                    campaign_id=campaign_id,
                    campaign_name=campaign_name,
                    game_name=game,
                    drop_id=str(drop.get("id", "")),
                    drop_name=drop.get("name", "?"),
                    benefit_name=benefit_name,
                    seconds_needed=required * 60,
                    seconds_watched=watched * 60,
                    is_claimed=bool(own.get("isClaimed")),
                    preconditions_met=bool(own.get("hasPreconditionsMet", True)),
                    drop_instance_id=own.get("dropInstanceID"),
                    account_connected=account_connected,
                    end_at=end_at,
                    campaign_status=campaign.get("status", "") or "",
                    start_at=campaign.get("startAt"),
                    channels=channels,
                    prerequisite_names=prerequisites,
                    detail_url=campaign.get("detailsURL", "") or "",
                    account_link_url=campaign.get("accountLinkURL", "") or "",
                    benefit_url=benefit_url,
                    game_slug=game_obj.get("slug", "") or "",
                    is_event_based=drop.get("__typename") == "EventBasedDrop",
                )
            )

    return drops


# --------------------------------------------------------------------------- #
#  Терминал
# --------------------------------------------------------------------------- #


class Ansi:
    """Цвета и очистка экрана. Отключаются флагом --no-color или без TTY."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and sys.stdout.isatty()
        if self.enabled and os.name == "nt":
            self._enable_vt()

    @staticmethod
    def _enable_vt() -> None:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def green(self, t: str) -> str:
        return self._wrap("32", t)

    def yellow(self, t: str) -> str:
        return self._wrap("33", t)

    def red(self, t: str) -> str:
        return self._wrap("31", t)

    def cyan(self, t: str) -> str:
        return self._wrap("36", t)

    def magenta(self, t: str) -> str:
        return self._wrap("35", t)

    def clear(self) -> None:
        if self.enabled:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()


def beep() -> None:
    """Звук при успешном клейме."""
    if os.name == "nt":
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return
        except Exception:
            pass
    sys.stdout.write("\a")
    sys.stdout.flush()


def human_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} с"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes:02d} мин"
    days, hours = divmod(hours, 24)
    return f"{days} д {hours:02d} ч"


def bar(percent: float, width: int = 20) -> str:
    filled = int(round(percent / 100.0 * width))
    filled = max(0, min(width, filled))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def parse_end_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
#  Лог забертых наград
# --------------------------------------------------------------------------- #


class ClaimLog:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def write(self, drop: Drop, status: str) -> None:
        if not self.enabled:
            return
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            drop.game_name,
            drop.campaign_name,
            drop.drop_name,
            drop.benefit_name,
            f"{drop.seconds_watched // 60}/{drop.seconds_needed // 60} мин",
            status,
        ]
        is_new = not CLAIMS_PATH.exists()
        try:
            with CLAIMS_PATH.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if is_new:
                    writer.writerow(
                        ["time", "game", "campaign", "drop", "reward",
                         "watched", "status"]
                    )
                writer.writerow(row)
        except OSError as exc:
            print(f"[!] Не удалось записать {CLAIMS_PATH.name}: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
#  Рендер таблицы
# --------------------------------------------------------------------------- #


def render(
    drops: list[Drop],
    a: Ansi,
    viewer: Viewer,
    auto_claim: bool,
    seconds_to_end: int,
    rate: float | None = None,
    show_channels: bool = True,
    max_channels: int = 8,
) -> None:
    print(a.bold("=" * 78))
    mode = (
        a.green("автоклейм ВКЛ")
        if auto_claim
        else a.yellow("автоклейм выкл")
    )
    print(
        f"{a.bold('Twitch Drops')}  ·  {a.cyan(viewer.login or '?')}"
        f"  ·  {mode}  ·  {a.dim(datetime.now().strftime('%H:%M:%S'))}"
    )
    if rate is not None and rate > 0:
        print("Темп просмотра: " + a.cyan(f"{rate * 60:.1f} мин прогресса в минуту"))
    elif rate == 0.0:
        print(a.yellow("Прогресс не растёт — стрим не смотрится."))
    if seconds_to_end >= 0:
        print(
            "Ближайший дедлайн: "
            + (
                a.red(f"через {human_duration(seconds_to_end)}")
                if seconds_to_end < 3600
                else a.yellow(f"через {human_duration(seconds_to_end)}")
            )
        )
    print(a.dim("-" * 78))

    visible = [d for d in drops if not d.is_claimed]

    if not visible:
        print(a.dim("Активных дропсов нет. Смотрите стрим — кампания появится здесь."))
        print(a.dim("(Требуются кампании, в которых вы уже начали смотреть.)"))
        print(a.bold("=" * 78))
        return

    # Сначала то, за что стоит взяться сейчас, потом остальное.
    ordered = sorted(
        _group(visible), key=lambda item: -max(priority_score(d, rate) for d in item[1])
    )

    for _cid, group in ordered:
        head = group[0]
        label = f"{head.campaign_name}  ({head.game_name})"
        state, seconds = campaign_deadline(group)

        if not head.account_connected:
            print(
                a.yellow(f"⚠ {label}  — аккаунт игры НЕ привязан, дропс не выдадут")
            )
            if head.account_link_url:
                print("    привязать: " + a.cyan(head.account_link_url))
        elif state == "expired":
            print(f"{a.dim('▸')} {label}  " + a.red("кампания уже закончилась"))
        elif state == "soon":
            tail = (
                a.red(f"до конца {human_duration(seconds)}")
                if seconds < 3600
                else a.yellow(f"до конца {human_duration(seconds)}")
            )
            print(f"{a.magenta('▸')} {label}  " + tail)
        else:
            print(f"{a.magenta('▸')} {label}")

        for d in group:
            if d.is_claimed:
                continue
            if not d.preconditions_met:
                need = ", ".join(d.prerequisite_names) or "предыдущий предмет"
                line = f"    {bar(0)}  0%  {d.drop_name} — " + a.dim(f"сначала: {need}")
            elif d.can_claim:
                line = (
                    f"    {a.green(bar(d.percent))} {d.percent:5.1f}%  {d.drop_name}"
                    f"  {a.green('ГОТОВ К КЛЕЙМУ')}"
                )
            else:
                line = (
                    f"    {a.cyan(bar(d.percent))} {d.percent:5.1f}%  {d.drop_name}"
                    f"  {a.dim('осталось ' + human_duration(d.seconds_left))}"
                    + a.dim(eta_for(d, rate))
                )
            print(line)

        # Где смотреть — самое ценное, что Twitch отдаёт, а программа
        # раньше выбрасывала. У nopixel V в списке 621 канал.
        if show_channels and head.channels and state != "expired":
            names = [c.name for c in head.channels[:max_channels]]
            more = len(head.channels) - len(names)
            line = "    где смотреть: " + a.cyan(", ".join(names))
            if more > 0:
                line += a.dim(f" … и ещё {more}")
            print(line)

        print(a.dim("-" * 78))

    print(a.bold("=" * 78))


def _group(drops: list[Drop]) -> list[tuple[str, list[Drop]]]:
    groups: dict[str, list[Drop]] = {}
    order: list[str] = []
    for d in drops:
        if d.campaign_id not in groups:
            groups[d.campaign_id] = []
            order.append(d.campaign_id)
        groups[d.campaign_id].append(d)
    return [(cid, groups[cid]) for cid in order]


def load_catalog(path: Path) -> list[Drop]:
    """
    Загружает каталог кампаний из файла, выгруженного вручную.

    Нужен для обхода ограничения: полный список кампаний Twitch отдаёт
    только операция ViewerDropsDashboard, а она требует Client-Integrity —
    браузерный отпечаток, который программа на Python не подделает.
    Поэтому каталог выгружается вручную в браузере, а программа лишь
    читает и разбирает файл.
    """
    if not path.exists():
        raise TwitchError(f"Файл не найден: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise TwitchError(f"{path.name} — невалидный JSON: {exc}") from exc

    # Принимаем и «голый» список кампаний, и полный ответ GQL.
    campaigns: Any = raw
    if isinstance(raw, dict):
        for path_keys in (
            ("data", "currentUser", "dropCampaigns"),
            ("data", "currentUser", "inventory", "dropCampaignsInProgress"),
            ("dropCampaigns",),
        ):
            node: Any = raw
            for key in path_keys:
                if isinstance(node, dict) and key in node:
                    node = node[key]
                else:
                    node = None
                    break
            if node is not None:
                campaigns = node
                break

    if not isinstance(campaigns, list):
        raise TwitchError(
            f"Не нашёл список кампаний в {path.name}.\n"
            "    Ожидался либо массив кампаний, либо ответ GQL с\n"
            "    data.currentUser.dropCampaigns."
        )

    drops = parse_campaigns(campaigns)
    if not drops:
        raise TwitchError(f"Кампаний не найдено — файл {path.name} пуст или другой формат.")
    return drops


def measure_watch_rate(state: dict[str, Any]) -> float | None:
    """
    Секунды прогресса в секунду реального времени, по двум замерам.

    Возвращает None, пока данных мало — раньше двух опросов оценивать
    нечего, и любая цифра была бы выдумкой.
    """
    prev_time = state.get("sample_time")
    prev_total = state.get("sample_total")
    if prev_time is None or prev_total is None:
        return None
    elapsed = time.time() - prev_time
    if elapsed < 30:  # слишком короткий интервал — делить не на что
        return None
    gained = state.get("progress_total", 0) - prev_total
    if gained <= 0:
        return 0.0
    return gained / elapsed


def eta_for(d: Drop, rate: float | None) -> str:
    """Через сколько примерно дозреет дропс при текущем темпе просмотра."""
    if d.is_claimed or d.can_claim:
        return ""
    if not rate:
        return ""
    remaining = d.seconds_left
    if remaining <= 0:
        return ""
    return f" ~{human_duration(int(remaining / rate))}"


def priority_score(d: Drop, rate: float | None) -> float:
    """
    Насколько дропс стоит внимания прямо сейчас. Больше — важнее.

    Учитывает риск не успеть до дедлайна, близость к готовности и
    предусловия: дропс, который нельзя взять без предыдущего, не срочный.
    """
    if d.is_claimed or d.campaign_status == "EXPIRED":
        return -1.0
    if not d.account_connected:
        return -1.0

    score = d.percent / 10.0  # чем ближе к 100%, тем интереснее

    end = parse_end_at(d.end_at)
    if end is not None:
        left = (end - datetime.now(timezone.utc)).total_seconds()
        needed = d.seconds_left
        if needed > 0 and left < needed * 1.2:
            score += 40.0  # риск не успеть
        elif needed > 0 and left < needed * 3:
            score += 15.0

    if d.seconds_left == 0:
        score += 100.0
    if not d.preconditions_met:
        score -= 20.0
    if rate and d.seconds_left > 0:
        # Дропс на час просмотра ценнее дропса на десять часов.
        score += max(0.0, 20.0 - d.seconds_left / 3600.0)

    return score


def _drop_deadline(drops: list[Drop]) -> datetime | None:
    """Ближайший ещё не прошедший дедлайн среди незабранных дропсов."""
    best: datetime | None = None
    for d in drops:
        if d.is_claimed:
            continue
        end = parse_end_at(d.end_at)
        if end is None or end < datetime.now(timezone.utc):
            continue
        best = end if best is None or end < best else best
    return best


def campaign_deadline(group: list[Drop]) -> tuple[str, int]:
    """
    Состояние дедлайна кампании: ("none"|"soon"|"expired", секунды).

    "none"    — дропсы забраны либо Twitch не отдал дату окончания.
    "soon"    — есть незабранный дропс с дедлайном в будущем, вернётся остаток.
    "expired" — дедлайн известен и прошёл, а дропсы не забраны: награды больше нет.
    """
    pending = [d for d in group if not d.is_claimed]
    if not pending:
        return "none", -1

    end = _drop_deadline(pending)
    if end is not None:
        return "soon", max(0, int((end - datetime.now(timezone.utc)).total_seconds()))

    # Дедлайн в будущем есть? Нет, но Twitch его вообще не прислал — неизвестно.
    if not any(parse_end_at(d.end_at) for d in pending):
        return "none", -1

    # Дедлайн пришёл, но уже в прошлом, а дропсы не забраны.
    return "expired", -1


def nearest_deadline(drops: list[Drop], warn_window: int) -> int:
    """Секунд до ближайшего дедлайна; -1 если ничего срочного."""
    end = _drop_deadline(drops)
    if end is None:
        return -1
    seconds = int((end - datetime.now(timezone.utc)).total_seconds())
    return seconds if seconds <= warn_window * 60 else -1


# --------------------------------------------------------------------------- #
#  Веб-панель
# --------------------------------------------------------------------------- #

UI_DIR = resource_dir() / "ui"


def ui_file(name: str) -> Path:
    """Путь к файлу интерфейса; в EXE ресурсы лежат во временной папке."""
    return UI_DIR / name


def build_payload(
    client: DropsClient,
    config: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Переводит внутреннее состояние в JSON для панели."""
    drops: list[Drop] = state.get("drops") or []
    rate = state.get("rate")
    viewer: Viewer = state.get("viewer") or Viewer()

    campaigns: list[dict[str, Any]] = []
    for cid, group in _group(drops):
        head = group[0]
        deadline_state, seconds = campaign_deadline(group)
        expired = deadline_state == "expired"

        # «Под дедлайном» — когда на дропс осталось больше, чем есть до конца.
        risk = False
        if not expired:
            for d in group:
                if d.is_claimed or d.seconds_left <= 0:
                    continue
                end = parse_end_at(d.end_at)
                if end is not None:
                    left = (end - datetime.now(timezone.utc)).total_seconds()
                    if left < d.seconds_left * 1.2:
                        risk = True
                        break

        campaigns.append(
            {
                "id": cid,
                "name": head.campaign_name,
                "game": head.game_name,
                "status": head.campaign_status,
                "account_connected": head.account_connected,
                "account_link_url": head.account_link_url,
                "expired": expired or head.campaign_status == "EXPIRED",
                "deadline_seconds": seconds if deadline_state == "soon" else None,
                "risk": risk,
                "channels": [
                    {"name": c.name, "url": c.url} for c in head.channels[:60]
                ],
                "channels_total": len(head.channels),
                "drops": [
                    {
                        "name": d.drop_name,
                        "benefit": d.benefit_name,
                        "percent": round(d.percent, 1),
                        "watched_min": d.seconds_watched // 60,
                        "needed_min": d.seconds_needed // 60,
                        "left": human_duration(d.seconds_left),
                        "eta": eta_for(d, rate).replace("~", "").strip(),
                        "claimed": d.is_claimed,
                        "ready": d.can_claim,
                        "blocked": not d.preconditions_met,
                        "needs": d.prerequisite_names,
                    }
                    for d in group
                ],
            }
        )

    active = [d for d in drops if not d.campaign_status == "EXPIRED"]
    total_pct = (
        sum(d.percent for d in active) / len(active) if active else 0.0
    )

    return {
        "connected": not state.get("error"),
        "error": state.get("error"),
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "poll_interval": int(config["poll_interval"]),
        "auto_claim": bool(config["auto_claim"]),
        "viewer": {"login": viewer.login, "user_id": viewer.user_id},
        "rate": round(rate, 5) if rate else 0,
        "deadline_seconds": nearest_deadline(
            drops, config["warn_minutes_before_end"]
        )
        if nearest_deadline(drops, config["warn_minutes_before_end"]) >= 0
        else (
            int((_drop_deadline(drops) - datetime.now(timezone.utc)).total_seconds())
            if _drop_deadline(drops)
            else None
        ),
        "stats": {
            "campaigns": len({d.campaign_id for d in drops if d.campaign_status != "EXPIRED"}),
            "drops_total": len(drops),
            "drops_ready": sum(1 for d in drops if d.can_claim),
            "claimed": sum(1 for d in drops if d.is_claimed),
            "unlinked": len(
                {d.campaign_id for d in drops if not d.account_connected}
            ),
            "avg_percent": round(total_pct, 1),
        },
        "campaigns": campaigns,
    }


class Dashboard:
    """
    Фоновый сборщик состояния + локальный HTTP-сервер панели.

    Опрос Twitch идёт в отдельном потоке, чтобы запросы из браузера
    отдавались мгновенно и не ждали сеть.
    """

    def __init__(self, client: DropsClient, config: dict[str, Any], viewer: Viewer) -> None:
        self.client = client
        self.config = config
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.state: dict[str, Any] = {
            "drops": [],
            "viewer": viewer,
            "rate": None,
            "error": None,
            "claims": [],
        }
        self._busy = False
        self._thread: threading.Thread | None = None
        self._server: HTTPServer | None = None
        self._progress: dict[str, int] = {}
        self._sample_time: float | None = None
        self._sample_total: int = 0

    # -- сбор данных ------------------------------------------------------ #

    def start_worker(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass

    def request_poll(self) -> list[str]:
        """Форсирует опрос и ждёт его. Возвращает список забранных наград."""
        self.wake.set()
        time.sleep(0.05)
        deadline = time.time() + 25
        while time.time() < deadline:
            with self.lock:
                if not self._busy:
                    break
            time.sleep(0.1)
        with self.lock:
            claims = list(self.state.get("claims") or [])
            self.state["claims"] = []
        return claims

    def set_auto_claim(self, enabled: bool) -> None:
        with self.lock:
            self.config["auto_claim"] = bool(enabled)
        _save(self.config)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                self._busy = True
            try:
                drops = self.client.fetch_drops()

                total = sum(d.seconds_watched for d in drops)
                rate = None
                if self._sample_time is not None:
                    elapsed = time.time() - self._sample_time
                    if elapsed >= 30:
                        gained = total - self._sample_total
                        rate = max(0.0, gained / elapsed)
                self._sample_time = time.time()
                self._sample_total = total

                claims = self._claim_ready(drops)

                with self.lock:
                    self.state["drops"] = drops
                    self.state["rate"] = rate
                    self.state["error"] = None
                    if claims:
                        self.state["claims"] = claims

            except TokenInvalid as exc:
                with self.lock:
                    self.state["error"] = f"токен недействителен: {exc}"
            except TwitchError as exc:
                with self.lock:
                    self.state["error"] = str(exc)
            except Exception as exc:  # noqa: BLE001
                with self.lock:
                    self.state["error"] = repr(exc)
            finally:
                with self.lock:
                    self._busy = False

            self.wake.wait(timeout=max(15, int(self.config["poll_interval"])))
            self.wake.clear()

    def _claim_ready(self, drops: list[Drop]) -> list[str]:
        if not self.config["auto_claim"]:
            return []
        claimed: list[str] = []
        log = ClaimLog(self.config["log_claims"])
        for d in drops:
            if not d.can_claim or d.drop_instance_id is None:
                continue
            try:
                ok, _status = self.client.claim(d.drop_instance_id)
            except TwitchError:
                continue
            if ok:
                d.is_claimed = True
                claimed.append(f"{d.benefit_name} — {d.campaign_name}")
                log.write(d, "ELIGIBLE_FOR_ALL")
        return claimed

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return build_payload(self.client, self.config, self.state)


class _PanelHandler(BaseHTTPRequestHandler):
    """Отдаёт статику панели и JSON-API."""

    dashboard: Dashboard | None = None
    server_version = "TwitchDropsPanel"

    def log_message(self, *_args: Any) -> None:
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            code,
        )

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/state":
            if self.dashboard is None:
                self._json({"error": "панель не инициализирована"}, 503)
                return
            self._json(self.dashboard.snapshot())
            return

        name = {
            "/": "index.html",
            "/index.html": "index.html",
            "/style.css": "style.css",
            "/app.js": "app.js",
        }.get(route)
        if not name:
            self._json({"error": "not found"}, 404)
            return

        path = ui_file(name)
        if not path.exists():
            self._json(
                {"error": f"файл интерфейса не найден: {name}"}, 500
            )
            return

        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }[path.suffix]
        self._send(path.read_bytes(), ctype)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._json({"error": "плохой JSON"}, 400)
            return

        if self.dashboard is None:
            self._json({"error": "панель не инициализирована"}, 503)
            return

        if route == "/api/autoclaim":
            self.dashboard.set_auto_claim(bool(payload.get("enabled")))
            self._json({"auto_claim": self.dashboard.config["auto_claim"]})
        elif route == "/api/refresh":
            claims = self.dashboard.request_poll()
            self._json({"ok": True, "claims": claims})
        else:
            self._json({"error": "not found"}, 404)


def run_gui(
    client: DropsClient,
    config: dict[str, Any],
    viewer: Viewer,
    port: int,
    open_browser: bool,
) -> int:
    """Поднимает панель и крутит её, пока пользователь не закроет."""
    dash = Dashboard(client, config, viewer)

    handler = type("H", (_PanelHandler,), {"dashboard": dash})
    try:
        httpd = HTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        print(f"[✖] Не удалось занять порт {port}: {exc}", file=sys.stderr)
        return 1

    actual = httpd.server_address[1]
    dash._server = httpd
    dash.start_worker()

    url = f"http://127.0.0.1:{actual}/"
    print("=" * 62)
    print(f"  Панель Twitch Drops запущена:  {url}")
    print("=" * 62)
    print("  Закройте это окно, чтобы остановить программу.")
    print()

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        dash.stop()
        try:
            httpd.server_close()
        except Exception:
            pass

    print("\nПанель остановлена. Пока!")
    return 0


# --------------------------------------------------------------------------- #
#  Основной цикл
# --------------------------------------------------------------------------- #


def poll_once(
    client: DropsClient,
    config: dict[str, Any],
    a: Ansi,
    log: ClaimLog,
    state: dict[str, Any],
) -> list[Drop]:
    """Один цикл: забрать данные, нарисовать, поклеймить готовое."""
    drops = client.fetch_drops()

    # Отслеживаем рост прогресса, чтобы понять, идёт ли просмотр.
    total = 0
    for d in drops:
        key = f"{d.drop_id}:{d.campaign_id}"
        prev = state.get("progress", {}).get(key)
        if prev is not None and d.seconds_watched > prev:
            state.setdefault("growing", set()).add(key)
        state.setdefault("progress", {})[key] = d.seconds_watched
        total += d.seconds_watched

    # Темп считаем между двумя опросами, а не с начала сессии: так он
    # отражает то, что происходит сейчас, а не среднее за всё время.
    rate = measure_watch_rate(state)
    state["sample_time"] = time.time()
    state["sample_total"] = total
    state["progress_total"] = total

    render(
        drops,
        a,
        state["viewer"],
        config["auto_claim"],
        nearest_deadline(drops, config["warn_minutes_before_end"]),
        rate=rate,
        show_channels=config["show_channels"],
    )

    # Новая кампания, которой раньше не было, — повод посмотреть.
    seen = state.setdefault("campaigns_seen", set())
    for cid, group in _group(drops):
        if cid not in seen:
            if seen:
                print(a.cyan(f"★ Новая кампания: {group[0].campaign_name} ({group[0].game_name})"))
            seen.add(cid)

    if not config["auto_claim"]:
        return drops

    for d in drops:
        if not d.can_claim or d.drop_instance_id is None:
            continue

        ok, status = False, ""
        for attempt in range(1, config["claim_retry"] + 1):
            try:
                ok, status = client.claim(d.drop_instance_id)
            except TwitchError as exc:
                status = str(exc)
            if ok:
                break
            if attempt < config["claim_retry"]:
                time.sleep(2 * attempt)

        if ok:
            print(
                a.green(f"✔ Забрано: {d.benefit_name}  ({d.campaign_name} / {d.drop_name})")
            )
            log.write(d, status)
            if config["sound"]:
                beep()
        else:
            print(a.red(f"✖ Не удалось забрать {d.drop_name}: {status}"))

    return drops


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Twitch Drops: трекер прогресса и автоклейм наград.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Как получить auth-token: откройте twitch.tv под своим аккаунтом,\n"
            "   в DevTools -> Application -> Cookies -> https://www.twitch.tv -> auth-token\n"
            "   (или воспользуйтесь расширением Twitch Cookie Exporter)."
        ),
    )
    parser.add_argument("--token", help="auth-token (перекрывает config.json и переменную TWITCH_AUTH_TOKEN)")
    parser.add_argument("--interval", type=int, help="интервал опроса, секунд (по умолчанию 60)")
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    parser.add_argument("--no-claim", action="store_true", help="только показывать, не клеймить")
    parser.add_argument("--no-color", action="store_true", help="без цветов и очистки экрана")
    parser.add_argument("--web-client", action="store_true", help="использовать веб-Client-ID (нужен Client-Integrity)")
    parser.add_argument(
        "--login",
        action="store_true",
        help="войти через OAuth под СВОИМ приложением и сохранить токен (нужны oauth_client_id и oauth_client_secret)",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="обновить токен по refresh_token"
    )
    parser.add_argument(
        "--no-channels", action="store_true", help="не показывать списки каналов"
    )
    parser.add_argument(
        "--import-catalog",
        metavar="FILE",
        help="разобрать каталог кампаний из файла (JSON, выгруженный из браузера)",
    )
    parser.add_argument(
        "--history", action="store_true", help="показать уже заработанные награды и выйти"
    )
    parser.add_argument(
        "--gui", action="store_true", help="веб-панель в браузере вместо консоли"
    )
    parser.add_argument(
        "--port", type=int, default=0, help="порт панели (0 — выбрать свободный)"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="не открывать браузер автоматически"
    )
    args = parser.parse_args()

    overrides: dict[str, Any] = {}
    if args.token:
        overrides["auth_token"] = args.token
    if args.interval is not None:
        overrides["poll_interval"] = args.interval
    if args.no_claim:
        overrides["auto_claim"] = False
    if args.web_client:
        overrides["client_id"] = CLIENT_ID_WEB
    if args.no_channels:
        overrides["show_channels"] = False

    config = load_config(overrides)

    a = Ansi(enabled=not args.no_color)

    # --- Режим --login: OAuth вместо копирования cookie ------------------- #
    if args.login:
        try:
            data = oauth_login(config)
        except TwitchError as exc:
            print(f"[✖] {exc}", file=sys.stderr)
            return 2

        config["auth_token"] = data["access_token"]
        config["refresh_token"] = data.get("refresh_token", "")
        config["token_expires_at"] = token_expiry(data)
        _save(config)

        print(f"\n✔ Токен сохранён в {CONFIG_PATH.name}.")
        print(f"  Действителен до: {datetime.fromtimestamp(config['token_expires_at'])}")
        print("  Дальше запускайте обычным образом: start.bat")
        return 0

    # --- Режим --refresh: обновить истекающий токен ------------------------ #
    if args.refresh:
        try:
            data = oauth_refresh(config, {"refresh_token": config.get("refresh_token", "")})
        except TwitchError as exc:
            print(f"[✖] {exc}", file=sys.stderr)
            return 2

        config["auth_token"] = data["access_token"]
        config["refresh_token"] = data.get("refresh_token", config.get("refresh_token", ""))
        config["token_expires_at"] = token_expiry(data)
        _save(config)
        print(f"✔ Токен обновлён, действует до: {datetime.fromtimestamp(config['token_expires_at'])}")
        return 0

    # --- Автообновление протухшего OAuth-токена ---------------------------- #
    expiry = int(config.get("token_expires_at") or 0)
    if expiry and config.get("refresh_token") and expiry - int(time.time()) < 300:
        print("Токен скоро истекает, обновляю...")
        try:
            data = oauth_refresh(
                config, {"refresh_token": config["refresh_token"]}
            )
            config["auth_token"] = data["access_token"]
            config["refresh_token"] = data.get("refresh_token", config["refresh_token"])
            config["token_expires_at"] = token_expiry(data)
            _save(config)
        except TwitchError as exc:
            print(f"[!] Автообновление не удалось: {exc}", file=sys.stderr)
            print("    Попробуйте start.bat --login", file=sys.stderr)
            return 2

    if not config["auth_token"]:
        print(
            "Не найден auth-token.\n"
            "Передайте его так:\n"
            "    python twitch_drops.py --token ВАШ_ТОКЕН\n"
            "или положите его в config.json -> поле \"auth_token\"\n"
            "либо войдите под своим приложением:\n"
            "    python twitch_drops.py --login",
            file=sys.stderr,
        )
        return 2

    log = ClaimLog(config["log_claims"])
    client = DropsClient(config)

    # --- Импорт каталога: полный список кампаний, выгруженный руками --- #
    if args.import_catalog:
        try:
            cat = load_catalog(Path(args.import_catalog))
        except TwitchError as exc:
            print(f"[✖] {exc}", file=sys.stderr)
            return 2

        print(f"Загружено кампаний: {len({d.campaign_id for d in cat})}")
        print(f"Дропсов всего:      {len(cat)}")
        print(
            "Активных:           "
            f"{len({d.campaign_id for d in cat if d.campaign_status == 'ACTIVE'})}"
        )
        print(
            "Истёкших:           "
            f"{len({d.campaign_id for d in cat if d.campaign_status == 'EXPIRED'})}"
        )
        print()
        print(
            "ВНИМАНИЕ: в выгруженном каталоге нет вашего прогресса — Twitch\n"
            "не отдаёт его для кампаний, которые вы не начинали. Поэтому\n"
            "проценты здесь нулевые, а автоклейм не сработает. Файл годится,\n"
            "чтобы посмотреть, что вообще бывает, и найти нужные каналы."
        )
        print()
        render(
            cat,
            a,
            Viewer(),
            auto_claim=False,
            seconds_to_end=nearest_deadline(cat, config["warn_minutes_before_end"]),
            show_channels=config["show_channels"],
            max_channels=int(config["max_channels"]),
        )
        return 0

    try:
        viewer = client.validate()
    except TokenInvalid as exc:
        print(f"[✖] {exc}", file=sys.stderr)
        return 2
    except TwitchError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1

    expires_in = viewer.expires_in
    if expires_in and expires_in < 3600:
        print(f"[!] Токен истекает через {human_duration(expires_in)}.")

    state: dict[str, Any] = {
        "viewer": viewer,
        "progress": {},
        "growing": set(),
        "campaigns_seen": set(),
    }

    print(
        f"Авторизован как {a.bold(viewer.login or '?')} "
        f"(id {viewer.user_id}, Client-ID {viewer.client_id})"
    )
    print(
        "Смотрите стрим сами, программа только следит за прогрессом и забирает награды."
    )

    # --- История заработанного --------------------------------------------- #
    if args.history:
        items = client.fetch_history()
        print(f"\nЗаработано всего: {len(items)}\n")
        for item in sorted(
            items, key=lambda i: i.get("lastAwardedAt") or "", reverse=True
        ):
            when = (item.get("lastAwardedAt") or "")[:10]
            linked = "привязан" if item.get("isConnected") else "нет привязки"
            print(f"  {when}  {item.get('name', '?'):<34} {a.dim(linked)}")
        return 0

    if args.gui:
        return run_gui(client, config, viewer, args.port, not args.no_browser)

    if args.once:
        print()

    failures = 0
    try:
        while True:
            a.clear()
            try:
                poll_once(client, config, a, log, state)
                failures = 0
            except TokenInvalid as exc:
                print(a.red(f"[✖] {exc}"))
                return 2
            except NeedClientIntegrity as exc:
                print(a.red(f"[✖] {exc}"))
                return 3
            except TwitchError as exc:
                failures += 1
                print(a.yellow(f"[!] Сбой запроса ({failures}/{config['max_failures']}): {exc}"))
                if failures >= config["max_failures"]:
                    print("Слишком много ошибок подряд — останавливаюсь.")
                    return 1
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 — не падать из-за одной итерации
                failures += 1
                print(a.yellow(f"[!] Непредвиденная ошибка: {exc!r}"))

            if args.once:
                return 0

            try:
                time.sleep(max(15, int(config["poll_interval"])))
            except KeyboardInterrupt:
                break

    except KeyboardInterrupt:
        print("\nОстановлено. Пока!")
        return 0

    print("\nОстановлено. Пока!")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
