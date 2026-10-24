#!/usr/bin/env python3
"""Synchronise une liste statique MDBList avec la filmographie d'un acteur (TMDB)
et genere des bannieres d'acteur.

Usage : python sync.py [--dry-run] [--actor "Nom"] [--no-banner]
        python sync.py --tmdb-login | --tmdb-login-finish
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
CACHE_PATH = ROOT / "cache" / "imdb_ids.json"
BANNER_DIR = ROOT / "banners"
OUTPUT_DIR = ROOT / "output"
ADDON_DIR = ROOT / "docs"
ADDON_PREFIX = "actorlists-"
ENV_PATH = ROOT / ".env"
REQUEST_TOKEN_PATH = ROOT / ".tmdb_request_token"
TMDB_LISTS_OUTPUT = "tmdb_lists.json"

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB4_BASE = "https://api.themoviedb.org/4"
TMDB_IMG = "https://image.tmdb.org/t/p/original"
MDBLIST_BASE = "https://api.mdblist.com"
CHUNK = 100
TMDB_LIST_CHUNK = 50
PAGE = 1000

DEFAULTS = {
    "max_cast_order": 15,
    "min_vote_count": 20,
    "exclude_genre_ids": [99],
    "include_upcoming": True,
    "exclude_tmdb_ids": [],
    "include_tmdb_ids": [],
}


class SyncError(Exception):
    pass


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Charge les cles depuis .env (lancement local) sans ecraser l'environnement existant."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value and not os.environ.get(key.strip()):
            os.environ[key.strip()] = value


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def http(method: str, url: str, retries: int = 3, **kwargs) -> requests.Response:
    """Requete avec 3 essais et backoff sur 429 / 5xx / erreurs reseau."""
    kwargs.setdefault("timeout", 30)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            last = exc
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                last = SyncError(f"HTTP {resp.status_code} sur {url.split('?')[0]}")
                if attempt < retries - 1:
                    try:
                        wait = float(resp.headers.get("Retry-After", ""))
                    except ValueError:
                        wait = 2 ** (attempt + 1)
                    time.sleep(min(wait, 30))
                    continue
            else:
                return resp
        if attempt < retries - 1:
            time.sleep(2 ** (attempt + 1))
    raise SyncError(f"Echec de la requete {method} {url.split('?')[0]} : {last}")


# --------------------------------------------------------------------------
# TMDB
# --------------------------------------------------------------------------
def tmdb_request(path: str, params: dict | None = None) -> dict:
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if not key:
        raise SyncError("La variable d'environnement TMDB_API_KEY est absente ou vide.")
    params = dict(params or {})
    headers = {"Accept": "application/json"}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    resp = http("GET", f"{TMDB_BASE}{path}", params=params, headers=headers)
    if resp.status_code == 401:
        raise SyncError("TMDB a refuse la cle API (401). Verifiez TMDB_API_KEY.")
    if resp.status_code != 200:
        raise SyncError(f"TMDB {path} : HTTP {resp.status_code}")
    return resp.json()


def search_person(name: str) -> dict:
    data = tmdb_request("/search/person", {"query": name})
    results = data.get("results") or []
    if not results:
        raise SyncError(f"Aucune personne trouvee sur TMDB pour '{name}'.")
    best = max(results, key=lambda r: r.get("popularity") or 0)
    print(f"  Recherche '{name}' -> {best.get('name')} (tmdb_id {best['id']}, "
          f"popularite {best.get('popularity')})")
    return best


def fetch_person(person_id: int) -> dict:
    return tmdb_request(f"/person/{person_id}", {"append_to_response": "movie_credits"})


def is_upcoming(release_date: str | None, today: dt.date | None = None) -> bool:
    if not release_date:
        return True
    today = today or dt.date.today()
    try:
        return dt.date.fromisoformat(release_date[:10]) > today
    except ValueError:
        return True


def filter_cast(cast: list[dict], opts: dict, today: dt.date | None = None) -> list[dict]:
    """Applique les filtres de config a la liste `movie_credits.cast`."""
    cfg = {**DEFAULTS, **{k: v for k, v in opts.items() if k in DEFAULTS}}
    excluded = set(cfg["exclude_tmdb_ids"] or [])
    included = set(cfg["include_tmdb_ids"] or [])
    excl_genres = set(cfg["exclude_genre_ids"] or [])
    max_order = cfg["max_cast_order"]
    min_votes = cfg["min_vote_count"] or 0
    kept: dict[int, dict] = {}
    for m in cast:
        mid = m.get("id")
        if mid is None or mid in excluded:
            continue
        if mid in included:
            kept[mid] = m
            continue
        if m.get("adult"):
            continue
        if re.search(r"\b(himself|herself|self)\b", m.get("character") or "", re.I):
            continue
        if excl_genres & set(m.get("genre_ids") or []):
            continue
        if max_order is not None and (m.get("order") is None or m["order"] > max_order):
            continue
        if (m.get("vote_count") or 0) < min_votes:
            continue
        if not cfg["include_upcoming"] and is_upcoming(m.get("release_date"), today):
            continue
        kept[mid] = m
    return list(kept.values())


def sort_movies(movies: list[dict]) -> list[dict]:
    """Date decroissante ; les dates vides (a venir) en premier."""
    dated = sorted((m for m in movies if m.get("release_date")),
                   key=lambda m: m["release_date"], reverse=True)
    undated = [m for m in movies if not m.get("release_date")]
    return undated + dated


def load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(cache, indent=1, sort_keys=True) + "\n"
    if not CACHE_PATH.exists() or CACHE_PATH.read_text(encoding="utf-8") != text:
        CACHE_PATH.write_text(text, encoding="utf-8")


def get_imdb_id(tmdb_id: int, cache: dict) -> str | None:
    """IMDb id via cache ; seuls les ids trouves sont mis en cache."""
    key = str(tmdb_id)
    if cache.get(key):
        return cache[key]
    try:
        data = tmdb_request(f"/movie/{tmdb_id}/external_ids")
    except SyncError as exc:
        print(f"  ! external_ids {tmdb_id} : {exc}")
        return None
    imdb = data.get("imdb_id") or None
    if imdb:
        cache[key] = imdb
    return imdb


def build_desired(movies: list[dict], cache: dict) -> list[dict]:
    out = []
    for m in sort_movies(movies):
        rd = m.get("release_date") or ""
        out.append({
            "title": m.get("title") or m.get("original_title") or "?",
            "year": rd[:4] if rd else "",
            "tmdb": m["id"],
            "imdb": get_imdb_id(m["id"], cache),
            "release_date": rd,
            "poster_path": m.get("poster_path") or None,
            "backdrop_path": m.get("backdrop_path") or None,
            "overview": m.get("overview") or "",
        })
    return out


def pick_backdrop(cast: list[dict]) -> str | None:
    with_bd = [m for m in cast if m.get("backdrop_path")]
    if not with_bd:
        return None
    return max(with_bd, key=lambda m: m.get("vote_count") or 0)["backdrop_path"]


# --------------------------------------------------------------------------
# MDBList
# --------------------------------------------------------------------------
def mdb_request(method: str, path: str, params: dict | None = None, body: dict | None = None):
    key = os.environ.get("MDBLIST_API_KEY", "").strip()
    if not key:
        raise SyncError("La variable d'environnement MDBLIST_API_KEY est absente ou vide.")
    params = {**(params or {}), "apikey": key}
    resp = http(method, f"{MDBLIST_BASE}{path}", params=params, json=body)
    if resp.status_code in (401, 403):
        raise SyncError(f"MDBList a refuse l'acces (HTTP {resp.status_code}). "
                        "Verifiez MDBLIST_API_KEY et l'id de liste.")
    if resp.status_code == 404:
        raise SyncError(f"MDBList : liste introuvable ({path}).")
    if resp.status_code >= 400:
        raise SyncError(f"MDBList {path} : HTTP {resp.status_code} {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError:
        return {}


def mdb_get_items(list_id: int) -> list[dict]:
    """Retourne les films actuellement dans la liste (pagination limit/offset)."""
    items: list[dict] = []
    offset = 0
    while True:
        data = mdb_request("GET", f"/lists/{list_id}/items", {"limit": PAGE, "offset": offset})
        if isinstance(data, list):
            movies, shows = data, []
        else:
            movies = data.get("movies") or []
            shows = data.get("shows") or []
        items.extend(movies)
        if len(movies) + len(shows) < PAGE:
            break
        offset += PAGE
    return items


def find_list_id(lists: list[dict], list_name: str) -> int:
    """Retrouve l'id d'une liste MDBList par son nom (casse et accents ignores) ou son slug."""
    wanted = slugify(list_name)
    matches = [l for l in lists
               if slugify(l.get("name") or "") == wanted or (l.get("slug") or "") == wanted]
    if not matches:
        names = ", ".join(f"'{l.get('name')}'" for l in lists) or "aucune"
        raise SyncError(f"Aucune liste MDBList nommee '{list_name}'. Vos listes : {names}.")
    if len(matches) > 1:
        ids = ", ".join(str(l.get("id")) for l in matches)
        raise SyncError(f"Plusieurs listes MDBList nommees '{list_name}' (ids {ids}) : "
                        "renommez-en une ou indiquez mdblist_list_id.")
    return int(matches[0]["id"])


def resolve_list_id(actor: dict, actor_name: str) -> int:
    """mdblist_list_id s'il est renseigne, sinon recherche par mdblist_list_name (ou nom de l'acteur)."""
    if actor.get("mdblist_list_id"):
        return int(actor["mdblist_list_id"])
    list_name = actor.get("mdblist_list_name") or actor_name
    lists = mdb_request("GET", "/lists/user")
    if isinstance(lists, dict):
        lists = lists.get("lists") or []
    list_id = find_list_id(lists, list_name)
    print(f"  Liste MDBList '{list_name}' trouvee : id {list_id}")
    return list_id


def compute_diff(current: list[dict], desired: list[dict]) -> tuple[list[dict], list[str]]:
    """Retourne (a_ajouter, imdb_a_retirer).

    - retirer : imdb dans la liste mais absent de la liste desiree
    - ajouter : film desire dont l'imdb n'est pas dans la liste (envoye par tmdb) ;
      un film sans imdb connu n'est ajoute que si son tmdb n'est pas deja dans la liste.
    """
    cur_imdb = {i.get("imdb_id") for i in current if i.get("imdb_id")}
    cur_tmdb = {i.get("id") for i in current if i.get("id") is not None}
    want_imdb = {d["imdb"] for d in desired if d.get("imdb")}
    want_tmdb = {d["tmdb"] for d in desired}
    to_add = []
    for d in desired:
        if d.get("imdb"):
            if d["imdb"] not in cur_imdb:
                to_add.append(d)
        elif d["tmdb"] not in cur_tmdb:
            to_add.append(d)
    to_remove = sorted(
        i["imdb_id"] for i in current
        if i.get("imdb_id") and i["imdb_id"] not in want_imdb
        and i.get("id") not in want_tmdb
    )
    return to_add, to_remove


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def mdb_apply(list_id: int, to_add: list[dict], to_remove: list[str]) -> None:
    for part in chunks(to_remove, CHUNK):
        res = mdb_request("POST", f"/lists/{list_id}/items/remove",
                          body={"movies": [{"imdb": i} for i in part]})
        print(f"  Retrait : {res}")
    for part in chunks(to_add, CHUNK):
        res = mdb_request("POST", f"/lists/{list_id}/items/add",
                          body={"movies": [{"tmdb": d["tmdb"]} for d in part]})
        res = res if isinstance(res, dict) else {}
        added = (res.get("added") or {}).get("movies", 0)
        existing = (res.get("existing") or {}).get("movies", 0)
        nf = (res.get("not_found") or {}).get("movies", 0)
        print(f"  Ajout : {added} ajoute(s), {existing} deja present(s), {nf} introuvable(s)")


# --------------------------------------------------------------------------
# Listes TMDB (v4) : une liste publique par acteur, roles principaux seulement
# --------------------------------------------------------------------------
TMDB_LISTS_DEFAULTS = {
    "enabled": True,
    "max_cast_order": 3,
    "name_template": "{name}",
    "description_template": "Films où {name} tient un rôle principal — mise à jour automatique",
}


def tmdb4_request(method: str, path: str, token: str, params: dict | None = None,
                  body: dict | None = None):
    """Appel TMDB v4 avec jeton Bearer ; leve SyncError sur HTTP >= 400."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "Content-Type": "application/json"}
    kwargs: dict = {"headers": headers}
    if params:
        kwargs["params"] = params
    if body is not None:
        kwargs["json"] = body
    resp = http(method, f"{TMDB4_BASE}{path}", **kwargs)
    if resp.status_code in (401, 403):
        raise SyncError(f"TMDB v4 a refuse l'acces (HTTP {resp.status_code}) sur {path}. "
                        "Verifiez les jetons TMDB.")
    if resp.status_code >= 400:
        raise SyncError(f"TMDB v4 {method} {path} : HTTP {resp.status_code} {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError:
        return {}


def update_env_file(path: Path, updates: dict) -> None:
    """Remplace (ou ajoute) les lignes KEY=valeur dans un .env en conservant le reste."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    pending = dict(updates)
    out = []
    for line in lines:
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in pending:
            out.append(f"{m.group(1)}={pending.pop(m.group(1))}")
        else:
            out.append(line)
    for k, v in pending.items():
        out.append(f"{k}={v}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def tmdb_login(token_path: Path = REQUEST_TOKEN_PATH) -> int:
    read_token = os.environ.get("TMDB_READ_TOKEN", "").strip()
    if not read_token:
        print("Erreur : TMDB_READ_TOKEN est absent (.env ou environnement). Recuperez le "
              "« API Read Access Token » sur https://www.themoviedb.org/settings/api.",
              file=sys.stderr)
        return 1
    try:
        data = tmdb4_request("POST", "/auth/request_token", read_token, body={})
    except SyncError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    rt = data.get("request_token")
    if not rt:
        print("Erreur : reponse TMDB sans request_token.", file=sys.stderr)
        return 1
    token_path.write_text(rt, encoding="utf-8")
    print("Jeton de requete enregistre dans .tmdb_request_token.")
    print("1. Ouvrez cette adresse dans votre navigateur, connectez-vous et cliquez sur "
          "« Approuver » :")
    print(f"   https://www.themoviedb.org/auth/access?request_token={rt}")
    print("2. Ensuite, lancez : python sync.py --tmdb-login-finish")
    return 0


def tmdb_login_finish(token_path: Path = REQUEST_TOKEN_PATH, env_path: Path = ENV_PATH) -> int:
    read_token = os.environ.get("TMDB_READ_TOKEN", "").strip()
    if not read_token:
        print("Erreur : TMDB_READ_TOKEN est absent (.env ou environnement).", file=sys.stderr)
        return 1
    if not token_path.exists():
        print("Erreur : .tmdb_request_token introuvable. Lancez d'abord --tmdb-login.",
              file=sys.stderr)
        return 1
    rt = token_path.read_text(encoding="utf-8").strip()
    try:
        data = tmdb4_request("POST", "/auth/access_token", read_token,
                             body={"request_token": rt})
    except SyncError as exc:
        print(f"Erreur : {exc} (avez-vous bien approuve la demande dans le navigateur ?)",
              file=sys.stderr)
        return 1
    access, account = data.get("access_token"), data.get("account_id")
    if not access or not account:
        print("Erreur : reponse TMDB sans access_token / account_id.", file=sys.stderr)
        return 1
    update_env_file(env_path, {"TMDB_USER_TOKEN": access, "TMDB_ACCOUNT_ID": account})
    token_path.unlink()
    print(f"Connexion reussie : TMDB_USER_TOKEN ({len(access)} caracteres) et TMDB_ACCOUNT_ID "
          "ecrits dans .env.")
    print("Ajoutez aussi TMDB_READ_TOKEN, TMDB_USER_TOKEN et TMDB_ACCOUNT_ID dans les secrets "
          "GitHub.")
    return 0


def tmdb_lists_settings(config: dict) -> dict:
    return {**TMDB_LISTS_DEFAULTS, **(config.get("tmdb_lists") or {})}


def tmdb_lists_context(config: dict) -> dict | None:
    """Contexte de run pour les listes TMDB, ou None (avec une seule note) si desactive."""
    settings = tmdb_lists_settings(config)
    if not settings.get("enabled"):
        print("Listes TMDB desactivees (tmdb_lists.enabled) : ignorees.")
        return None
    token = os.environ.get("TMDB_USER_TOKEN", "").strip()
    account = os.environ.get("TMDB_ACCOUNT_ID", "").strip()
    if not token or not account:
        print("Listes TMDB ignorees : TMDB_USER_TOKEN / TMDB_ACCOUNT_ID absents "
              "(voir README, --tmdb-login).")
        return None
    return {"settings": settings, "token": token, "account": account,
            "lists": None, "output": {}}


def tmdb_pages(token: str, path: str):
    """Itere sur les `results` de toutes les pages d'un endpoint v4 pagine."""
    page, total = 1, 1
    while page <= total:
        data = tmdb4_request("GET", path, token, params={"page": page})
        total = int(data.get("total_pages") or 1)
        yield from (data.get("results") or [])
        page += 1


def tmdb_account_lists(ctx: dict) -> list[dict]:
    if ctx["lists"] is None:
        ctx["lists"] = list(tmdb_pages(ctx["token"], f"/account/{ctx['account']}/lists"))
    return ctx["lists"]


def tmdb_list_items(ctx: dict, list_id: int) -> set[int]:
    return {int(i["id"]) for i in tmdb_pages(ctx["token"], f"/list/{list_id}")
            if i.get("media_type", "movie") == "movie" and i.get("id") is not None}


def tmdb_list_apply(ctx: dict, list_id: int, method: str, ids: list[int]) -> None:
    for part in chunks(sorted(ids), TMDB_LIST_CHUNK):
        tmdb4_request(method, f"/list/{list_id}/items", ctx["token"],
                      body={"items": [{"media_type": "movie", "media_id": i} for i in part]})


def sync_tmdb_list(name: str, actor: dict, cast: list[dict], ctx: dict, dry_run: bool) -> None:
    """Synchronise la liste TMDB publique de l'acteur (roles principaux uniquement)."""
    s = ctx["settings"]
    kept = filter_cast(cast, {**actor, "max_cast_order": s["max_cast_order"]})
    wanted = {int(m["id"]) for m in kept}
    list_name = s["name_template"].format(name=name)
    description = s["description_template"].format(name=name)
    print(f"  Liste TMDB '{list_name}' : {len(wanted)} films en role principal "
          f"(ordre <= {s['max_cast_order']}).")
    matches = [l for l in tmdb_account_lists(ctx) if l.get("name") == list_name]
    if matches:
        list_id = int(matches[0]["id"])
        current = tmdb_list_items(ctx, list_id)
    elif dry_run:
        print(f"  (dry-run : la liste TMDB '{list_name}' serait creee)")
        list_id, current = None, set()
    else:
        data = tmdb4_request("POST", "/list", ctx["token"], body={
            "name": list_name, "iso_639_1": "fr", "description": description, "public": True})
        if not data.get("id"):
            raise SyncError(f"Creation de la liste TMDB '{list_name}' : reponse sans id.")
        list_id, current = int(data["id"]), set()
        ctx["lists"].append({"id": list_id, "name": list_name})
        print(f"  Liste TMDB creee : id {list_id}")
        try:
            tmdb4_request("PUT", f"/list/{list_id}", ctx["token"],
                          body={"sort_by": "primary_release_date.desc"})
        except SyncError as exc:
            print(f"  ! tri de la liste TMDB non applique : {exc}")
    to_add, to_remove = sorted(wanted - current), sorted(current - wanted)
    print(f"  Liste TMDB : {len(current)} films ; a ajouter : {len(to_add)} ; "
          f"a retirer : {len(to_remove)}")
    if dry_run:
        print("  (dry-run : aucune ecriture TMDB)")
        return
    if to_add:
        tmdb_list_apply(ctx, list_id, "POST", to_add)
    if to_remove:
        tmdb_list_apply(ctx, list_id, "DELETE", to_remove)
    ctx["output"][name] = {"id": list_id, "url": f"https://www.themoviedb.org/list/{list_id}",
                           "count": len(wanted)}


def write_tmdb_lists_output(ctx: dict) -> None:
    if not ctx["output"]:
        return
    path = OUTPUT_DIR / TMDB_LISTS_OUTPUT
    try:
        merged = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        merged = {}
    merged.update(ctx["output"])
    write_if_changed(path, (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


# --------------------------------------------------------------------------
# Bannieres
# --------------------------------------------------------------------------
def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "acteur"


def load_font(size: int):
    from PIL import ImageFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "C:/Windows/Fonts/arialbd.ttf",
              "/Library/Fonts/Arial Bold.ttf",
              "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def write_if_changed(path: Path, data: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == data:
        return False
    path.write_bytes(data)
    return True


def _jpeg_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _cover(img, w: int, h: int):
    from PIL import Image
    r = max(w / img.width, h / img.height)
    img = img.resize((max(w, round(img.width * r)), max(h, round(img.height * r))), Image.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


def _fit_text(draw, text: str, max_w: int, start: int, minimum: int = 40):
    """Plus grande police dont le texte tient sur une ligne, sinon la taille minimale."""
    size = start
    while size > minimum:
        font = load_font(size)
        if draw.textlength(text, font=font) <= max_w:
            return font, size
        size -= 4
    return load_font(minimum), minimum


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        t = f"{cur} {word}".strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def make_landscape(name: str, photo, backdrop):
    from PIL import Image, ImageDraw, ImageFilter
    W, H = 1920, 1080
    ph_w, ph_h = 560, 840  # le backdrop n'est plus utilise : fond noir vignette
    x0, y0 = 120, (H - ph_h) // 2
    canvas = _photo_card(_vignette_bg(W, H), photo, x0, y0, ph_w, ph_h)

    draw = ImageDraw.Draw(canvas)
    tx = x0 + ph_w + 100
    max_w = W - tx - 100
    font, size = _fit_text(draw, name, max_w, 150, 70)
    lines = _wrap(draw, name, font, max_w)
    lh = int(size * 1.15)
    y = H // 2 - lh * len(lines) // 2 - 30
    for ln in lines:
        draw.text((tx + 3, y + 3), ln, font=font, fill=(0, 0, 0, 160))
        draw.text((tx, y), ln, font=font, fill=(255, 255, 255, 255))
        y += lh
    draw.text((tx + 4, y + 20), "Filmographie", font=load_font(52), fill=(210, 210, 215, 255))
    return canvas.convert("RGB")


def _vignette_bg(W: int, H: int):
    """Fond noir vignette : centre gris tres sombre, bords noirs (RGBA)."""
    from PIL import Image
    fade = Image.radial_gradient("L").resize((W, H), Image.BICUBIC)  # 0 au centre, 255 aux bords
    bg = Image.eval(fade, lambda v: max(0, 46 - v * 46 // 200))
    return Image.merge("RGB", (bg, bg, bg)).convert("RGBA")


def _photo_card(canvas, photo, x0: int, y0: int, w: int, h: int):
    """Colle la photo aux coins arrondis, avec ombre portee, sur canvas (RGBA)."""
    from PIL import Image, ImageDraw, ImageFilter
    W, H = canvas.size
    p = _cover(photo.convert("RGB"), w, h) if photo is not None else Image.new("RGB", (w, h), (60, 60, 70))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=36, fill=255)
    sm = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sm).rounded_rectangle((x0, y0 + 14, x0 + w, y0 + h + 14), radius=36, fill=150)
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    shadow.putalpha(sm.filter(ImageFilter.GaussianBlur(24)))
    canvas = Image.alpha_composite(canvas, shadow)
    canvas.paste(p.convert("RGBA"), (x0, y0), mask)
    return canvas


def make_portrait(name: str, photo):
    from PIL import ImageDraw
    W, H = 1000, 1500
    ph_w, ph_h = 760, 1080
    canvas = _photo_card(_vignette_bg(W, H), photo, (W - ph_w) // 2, 90, ph_w, ph_h)
    draw = ImageDraw.Draw(canvas)
    max_w = W - 120
    font, size = _fit_text(draw, name, max_w, 110, 56)
    lines = _wrap(draw, name, font, max_w)
    lh = int(size * 1.15)
    top = 90 + ph_h + 20  # zone sous la photo
    y = top + (H - top - lh * len(lines)) // 2 - 10
    for ln in lines:
        x = (W - draw.textlength(ln, font=font)) / 2
        draw.text((x + 3, y + 3), ln, font=font, fill=(0, 0, 0, 160))
        draw.text((x, y), ln, font=font, fill=(255, 255, 255, 255))
        y += lh
    return canvas.convert("RGB")


def download_image(path: str | None):
    from PIL import Image
    if not path:
        return None
    try:
        resp = http("GET", f"{TMDB_IMG}{path}")
        if resp.status_code != 200:
            return None
        img = Image.open(io.BytesIO(resp.content))
        img.load()
        return img
    except (SyncError, OSError) as exc:
        print(f"  ! image {path} : {exc}")
        return None


def generate_banners(name: str, slug: str, profile_path: str | None,
                     backdrop_path: str | None, out_dir: Path = BANNER_DIR) -> list[str]:
    """Genere les 3 fichiers ; retourne la liste des fichiers reellement reecrits."""
    photo = download_image(profile_path)
    backdrop = download_image(backdrop_path)
    jobs = []
    if photo is not None:
        jobs.append((f"{slug}_photo.jpg", _jpeg_bytes(photo)))
    jobs.append((f"{slug}.jpg", _jpeg_bytes(make_landscape(name, photo, backdrop))))
    jobs.append((f"{slug}_poster.jpg", _jpeg_bytes(make_portrait(name, photo))))
    return [fn for fn, data in jobs if write_if_changed(out_dir / fn, data)]


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def write_output(slug: str, desired: list[dict]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(desired, indent=2, ensure_ascii=False) + "\n"
    p = OUTPUT_DIR / f"{slug}.json"
    if not p.exists() or p.read_text(encoding="utf-8") != text:
        p.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# Addon Stremio statique (docs/)
# --------------------------------------------------------------------------
ADDON_TYPES = ("movie", "series")
TMDB_THUMB = "https://image.tmdb.org/t/p/w500"
FILMS_CATALOG = "actorlists-films"


def _json_bytes(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def banner_urls(repo: str, slug: str) -> tuple[str, str]:
    base = f"https://raw.githubusercontent.com/{repo}/main/banners"
    return f"{base}/{slug}_poster.jpg", f"{base}/{slug}.jpg"


def build_videos(desired: list[dict], kind: str) -> list[dict]:
    """Videos Stremio. movie : plus recent d'abord (sans date en premier) ;
    series : chronologique (sans date a la fin), numerotes saison 1 / episode i."""
    items = [d for d in desired if d.get("imdb")]
    dated = sorted((d for d in items if d.get("release_date")), key=lambda d: d["release_date"],
                   reverse=(kind == "movie"))
    undated = [d for d in items if not d.get("release_date")]
    ordered = undated + dated if kind == "movie" else dated + undated
    videos = []
    for i, d in enumerate(ordered, 1):
        title = d.get("title") or "?"
        v = {"id": d["imdb"], "title": f"{title} ({d['year']})" if d.get("year") else title}
        if d.get("release_date"):
            v["released"] = f"{d['release_date'][:10]}T00:00:00.000Z"
        img = d.get("poster_path") or d.get("backdrop_path")
        if img:
            v["thumbnail"] = f"{TMDB_THUMB}{img}"
        if d.get("overview"):
            v["overview"] = d["overview"]
        if kind == "series":
            v["season"] = 1
            v["episode"] = i
        videos.append(v)
    return videos


def addon_base_url(repo: str) -> str:
    owner, _, name = repo.partition("/")
    return f"https://{owner.lower()}.github.io/{name}/"


BAD_FILENAME_CHARS = set('\\/:*?"<>|')


def build_manifest(logo: str | None, actor_names: list[str] | None = None) -> dict:
    m = {
        "id": "community.actorlists.hichgss", "version": "1.1.0", "name": "Acteurs",
        "description": "Filmographies d'acteurs (mise a jour quotidienne)",
        "resources": ["catalog", {"name": "meta", "types": ["series"],
                                  "idPrefixes": [ADDON_PREFIX]}],
        "types": list(ADDON_TYPES),
        "catalogs": [
            {"type": "series", "id": "actorlists-series", "name": "Acteurs"},
            {"type": "movie", "id": "actorlists-films", "name": "Filmographies",
             "extra": [{"name": "genre", "isRequired": True, "options": list(actor_names or [])}],
             "extraSupported": ["genre"], "extraRequired": ["genre"]}],
        "idPrefixes": [ADDON_PREFIX],
        "behaviorHints": {"configurable": False, "configurationRequired": False},
    }
    if logo:
        m["logo"] = logo
    return m


def build_poster_metas(desired: list[dict]) -> list[dict]:
    """Metas film (grille d'affiches) : plus recent d'abord, sans date en premier, sans imdb ignores."""
    metas = []
    for d in sort_movies([d for d in desired if d.get("imdb")]):
        m = {"id": d["imdb"], "type": "movie", "name": d.get("title") or "?"}
        if d.get("poster_path"):
            m["poster"] = f"{TMDB_THUMB}{d['poster_path']}"
        if d.get("year"):
            m["releaseInfo"] = d["year"]
        if d.get("overview"):
            m["description"] = d["overview"]
        metas.append(m)
    return metas


def build_copy_page(repo: str, actors: list[tuple[str, str]]) -> bytes:
    """Page web (GitHub Pages) pour copier les URLs des bannieres depuis l'iPad."""
    rows = []
    for name, slug in actors:
        for label, url in zip(("Poster", "Paysage"), banner_urls(repo, slug)):
            u = html.escape(url, quote=True)
            rows.append(f'<div class="c"><img src="{u}" loading="lazy" alt="">'
                        f'<b>{html.escape(name)} – {label}</b>'
                        f'<button data-u="{u}">Copier</button></div>')
    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bannieres acteurs</title>
<style>body{{font-family:-apple-system,sans-serif;background:#111;color:#eee;margin:16px}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px}}
.c{{background:#222;border-radius:10px;padding:8px;display:flex;flex-direction:column;gap:6px}}
img{{width:100%;border-radius:6px}}button{{font-size:16px;padding:10px;border:0;border-radius:8px;background:#3a7afe;color:#fff}}
button.ok{{background:#2a9d4b}}</style></head><body><h1>Bannières acteurs</h1>
<p>Addon : <code>{html.escape(addon_base_url(repo))}manifest.json</code></p><div class="g">
{chr(10).join(rows)}
</div><script>document.querySelectorAll('button').forEach(b=>b.onclick=async()=>{{
try{{await navigator.clipboard.writeText(b.dataset.u);b.textContent='Copié ✓';b.className='ok'}}
catch(e){{prompt('Copie cette URL :',b.dataset.u)}}}});</script></body></html>
"""
    return page.encode("utf-8")


def generate_addon(config: dict, results: list[dict], out_dir: Path = ADDON_DIR) -> list[str]:
    """Genere l'addon dans out_dir ; retourne les chemins (relatifs) reellement ecrits."""
    repo = (config.get("github_repo") or "").strip()
    if not repo:
        print("  ! github_repo absent de config.json : addon non genere.")
        return []
    written: list[str] = []

    def put(rel: str, data: bytes) -> None:
        if write_if_changed(out_dir / rel, data):
            written.append(rel)

    base = addon_base_url(repo)
    manifest_url = quote(base + "manifest.json", safe="")
    by_slug = {r["slug"]: r for r in results}
    ordered_slugs: list[str] = []
    names: dict[str, str] = {}
    for a in config.get("actors") or []:
        if a.get("name"):
            sl = slugify(a["name"])
            if sl not in ordered_slugs:
                ordered_slugs.append(sl)
                names[sl] = a["name"]
    for r in results:  # acteurs sans nom dans la config
        if r["slug"] not in ordered_slugs:
            ordered_slugs.append(r["slug"])
            names[r["slug"]] = r["name"]

    kind = "series"
    catalog: list[dict] = []
    logo = None
    for slug in ordered_slugs:
        poster, background = banner_urls(repo, slug)
        res = by_slug.get(slug)
        mid = f"{ADDON_PREFIX}{kind}-{slug}"
        meta_rel = f"meta/{kind}/{mid}.json"
        if res is None:  # echec ce jour-la : on garde l'existant (meta et fichier genre)
            try:
                old = json.loads((out_dir / meta_rel).read_text(encoding="utf-8"))["meta"]
            except (OSError, ValueError, KeyError):
                old = None
            if old is not None:
                catalog.append({k: v for k, v in old.items() if k not in ("videos", "links")})
        else:
            videos = build_videos(res["desired"], kind)
            meta = {"id": mid, "type": kind, "name": res["name"], "poster": poster,
                    "posterShape": "poster", "background": background,
                    "description": f"Filmographie de {res['name']} ({len(videos)} films)"}
            catalog.append(dict(meta))
            link = (f"stremio:///discover/{manifest_url}/movie/{FILMS_CATALOG}"
                    f"?genre={quote(names[slug])}")
            meta["links"] = [{"name": "Voir en affiches", "category": "Filmographie", "url": link}]
            put(meta_rel, _json_bytes({"meta": {**meta, "videos": videos}}))
            gname = names[slug]
            if BAD_FILENAME_CHARS & set(gname):
                print(f"  ! nom d'acteur '{gname}' inutilisable comme nom de fichier : "
                      "catalogue Filmographies ignore pour cet acteur.")
            else:
                put(f"catalog/movie/{FILMS_CATALOG}/genre={gname}.json",
                    _json_bytes({"metas": build_poster_metas(res["desired"])}))
        if logo is None:
            logo = poster

    put("manifest.json", _json_bytes(build_manifest(logo, [names[s] for s in ordered_slugs])))
    put(f"catalog/series/{ADDON_PREFIX}series.json", _json_bytes({"metas": catalog}))
    put("index.html", build_copy_page(repo, [(names[s], s) for s in ordered_slugs]))
    if not (out_dir / ".nojekyll").exists():
        put(".nojekyll", b"")

    # Nettoyage : metas d'acteurs retires de la config, variante film abandonnee, genres obsoletes
    keep = set(ordered_slugs)
    removed = []
    prefix = f"{ADDON_PREFIX}series-"
    for f in (out_dir / "meta" / "series").glob(f"{prefix}*.json"):
        if f.name[len(prefix):-len(".json")] not in keep:
            f.unlink()
            removed.append(f"meta/series/{f.name}")
    for f in (out_dir / "meta" / "movie").glob(f"{ADDON_PREFIX}*.json"):
        f.unlink()
        removed.append(f"meta/movie/{f.name}")
    old_cat = out_dir / "catalog" / "movie" / f"{ADDON_PREFIX}movie.json"
    if old_cat.exists():
        old_cat.unlink()
        removed.append(f"catalog/movie/{old_cat.name}")
    wanted = {f"genre={n}.json" for n in names.values()}
    for f in (out_dir / "catalog" / "movie" / FILMS_CATALOG).glob("genre=*.json"):
        if f.name not in wanted:
            f.unlink()
            removed.append(f"catalog/movie/{FILMS_CATALOG}/{f.name}")
    print(f"  Addon : {len(written)} fichier(s) ecrit(s), {len(removed)} supprime(s).")
    for rel in written:
        print(f"    + docs/{rel}")
    for rel in removed:
        print(f"    - docs/{rel}")
    return written


def process_actor(actor: dict, args, cache: dict, tmdb_ctx: dict | None = None) -> dict:
    name = actor.get("name") or ""
    print(f"\n=== {name or '(sans nom)'} ===")
    tmdb_id = actor.get("tmdb_id")
    if not tmdb_id:
        if not name:
            raise SyncError("Acteur sans 'name' ni 'tmdb_id'.")
        tmdb_id = search_person(name)["id"]
    person = fetch_person(int(tmdb_id))
    name = name or person.get("name") or str(tmdb_id)
    slug = slugify(name)
    cast = (person.get("movie_credits") or {}).get("cast") or []
    kept = filter_cast(cast, actor)
    print(f"  {len(cast)} roles TMDB, {len(kept)} films retenus.")
    desired = build_desired(kept, cache)
    save_cache(cache)

    print("  Liste desiree (annee - titre - tmdb) :")
    for d in desired:
        print(f"    {d['year'] or '????'} - {d['title']} - {d['tmdb']}")
    if not args.dry_run:
        write_output(slug, desired)

    errors = []
    have_key = bool(os.environ.get("MDBLIST_API_KEY", "").strip())
    if actor.get("mdblist") is False:
        print("  (mdblist: false : pas de liste MDBList, addon uniquement)")
    elif not have_key and args.dry_run:
        print("  (MDBLIST_API_KEY absente : diff non calcule en mode dry-run)")
    else:
        try:
            list_id = resolve_list_id(actor, name)
            current = mdb_get_items(int(list_id))
            to_add, to_remove = compute_diff(current, desired)
            print(f"  Liste MDBList : {len(current)} films ; "
                  f"a ajouter : {len(to_add)} ; a retirer : {len(to_remove)}")
            for d in to_add:
                print(f"    + {d['year']} {d['title']} ({d['tmdb']})")
            title_by_imdb = {i.get("imdb_id"): i.get("title") for i in current}
            for i in to_remove:
                print(f"    - {i} {title_by_imdb.get(i) or ''}")
            if args.dry_run:
                print("  (dry-run : aucune ecriture)")
            else:
                mdb_apply(int(list_id), to_add, to_remove)
        except SyncError as exc:
            errors.append(str(exc))

    if tmdb_ctx is not None and actor.get("tmdb_list") is not False:
        try:
            sync_tmdb_list(name, actor, cast, tmdb_ctx, args.dry_run)
        except SyncError as exc:
            errors.append(f"liste TMDB : {exc}")

    if not args.no_banner and not args.dry_run:
        try:
            changed = generate_banners(name, slug, person.get("profile_path"), pick_backdrop(kept))
            print(f"  Bannieres : {', '.join(changed) if changed else 'inchangees'}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"bannieres : {exc}")
    if errors:
        raise SyncError("; ".join(errors))
    return {"name": name, "slug": slug, "desired": desired}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="n'ecrit rien (ni MDBList, ni bannieres, ni output)")
    ap.add_argument("--actor", help="ne traiter que cet acteur (nom exact du config.json)")
    ap.add_argument("--no-banner", action="store_true", help="ne pas generer les bannieres")
    ap.add_argument("--tmdb-login", action="store_true",
                    help="demarre la connexion TMDB v4 (affiche l'adresse d'approbation)")
    ap.add_argument("--tmdb-login-finish", action="store_true",
                    help="termine la connexion TMDB v4 et ecrit les jetons dans .env")
    args = ap.parse_args(argv)
    load_dotenv()
    if args.tmdb_login:
        return tmdb_login()
    if args.tmdb_login_finish:
        return tmdb_login_finish()

    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Erreur : config.json illisible : {exc}", file=sys.stderr)
        return 2
    actors = config.get("actors") or []
    if args.actor:
        actors = [a for a in actors if (a.get("name") or "").lower() == args.actor.lower()]
        if not actors:
            print(f"Erreur : acteur '{args.actor}' absent de config.json.", file=sys.stderr)
            return 2
    if not os.environ.get("TMDB_API_KEY", "").strip():
        print("Erreur : la variable d'environnement TMDB_API_KEY est absente. "
              "Obtenez une cle sur https://www.themoviedb.org/settings/api puis definissez-la "
              "(ex. export TMDB_API_KEY=...).", file=sys.stderr)
        return 1

    cache = load_cache()
    tmdb_ctx = tmdb_lists_context(config)
    failed = 0
    results: list[dict] = []
    for actor in actors:
        try:
            results.append(process_actor(actor, args, cache, tmdb_ctx))
        except SyncError as exc:
            failed += 1
            print(f"  ERREUR ({actor.get('name')}) : {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERREUR inattendue ({actor.get('name')}) : {exc!r}", file=sys.stderr)
    if not args.dry_run:
        save_cache(cache)
        if tmdb_ctx is not None:
            write_tmdb_lists_output(tmdb_ctx)
        if results:
            try:
                generate_addon(config, results)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  ERREUR addon : {exc!r}", file=sys.stderr)
    if failed:
        print(f"\n{failed} acteur(s) en echec.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
