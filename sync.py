#!/usr/bin/env python3
"""Synchronise une liste statique MDBList avec la filmographie d'un acteur (TMDB)
et genere des bannieres d'acteur.

Usage : python sync.py [--dry-run] [--actor "Nom"] [--no-banner]
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
CACHE_PATH = ROOT / "cache" / "imdb_ids.json"
BANNER_DIR = ROOT / "banners"
OUTPUT_DIR = ROOT / "output"
ADDON_DIR = ROOT / "docs"
ADDON_PREFIX = "actorlists-"

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/original"
MDBLIST_BASE = "https://api.mdblist.com"
CHUNK = 100
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
    if backdrop is not None:
        bg = _cover(backdrop.convert("RGB"), W, H).filter(ImageFilter.GaussianBlur(18))
        bg = Image.blend(bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.55)
    else:
        bg = Image.new("RGB", (W, H))
        d = ImageDraw.Draw(bg)
        for y in range(H):
            v = int(15 + 35 * y / H)
            d.line([(0, y), (W, y)], fill=(v, v, v + 12))
    canvas = bg.convert("RGBA")

    ph_w, ph_h = 560, 840
    if photo is not None:
        p = _cover(photo.convert("RGB"), ph_w, ph_h)
    else:
        p = Image.new("RGB", (ph_w, ph_h), (60, 60, 70))
    x0, y0 = 120, (H - ph_h) // 2
    mask = Image.new("L", (ph_w, ph_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, ph_w - 1, ph_h - 1), radius=36, fill=255)
    sm = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sm).rounded_rectangle((x0, y0 + 14, x0 + ph_w, y0 + ph_h + 14),
                                         radius=36, fill=150)
    sm = sm.filter(ImageFilter.GaussianBlur(24))
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    shadow.putalpha(sm)
    canvas = Image.alpha_composite(canvas, shadow)
    canvas.paste(p.convert("RGBA"), (x0, y0), mask)

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


def make_portrait(name: str, photo):
    from PIL import Image, ImageDraw
    W, H = 1000, 1500
    if photo is not None:
        canvas = _cover(photo.convert("RGB"), W, H)
    else:
        canvas = Image.new("RGB", (W, H), (45, 45, 55))
    canvas = canvas.convert("RGBA")
    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    start = int(H * 0.55)
    for y in range(start, H):
        a = int(255 * min(1.0, (y - start) / (H - start) * 1.15) ** 1.2)
        gd.line([(0, y), (W, y)], fill=(8, 8, 12, a))
    canvas = Image.alpha_composite(canvas, grad)
    draw = ImageDraw.Draw(canvas)
    max_w = W - 120
    font, size = _fit_text(draw, name, max_w, 120, 56)
    lines = _wrap(draw, name, font, max_w)
    lh = int(size * 1.15)
    y = H - 90 - lh * len(lines)
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


def build_manifest(logo: str | None) -> dict:
    m = {
        "id": "community.actorlists.hichgss", "version": "1.0.0", "name": "Acteurs",
        "description": "Filmographies d'acteurs (mise a jour quotidienne)",
        "resources": ["catalog", {"name": "meta", "types": list(ADDON_TYPES),
                                  "idPrefixes": [ADDON_PREFIX]}],
        "types": list(ADDON_TYPES),
        "catalogs": [{"type": "movie", "id": "actorlists-movie", "name": "Acteurs"},
                     {"type": "series", "id": "actorlists-series", "name": "Acteurs (test serie)"}],
        "idPrefixes": [ADDON_PREFIX],
        "behaviorHints": {"configurable": False, "configurationRequired": False},
    }
    if logo:
        m["logo"] = logo
    return m


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

    by_slug = {r["slug"]: r for r in results}
    ordered_slugs: list[str] = []
    for a in config.get("actors") or []:
        if a.get("name"):
            sl = slugify(a["name"])
            if sl not in ordered_slugs:
                ordered_slugs.append(sl)
    for r in results:  # acteurs sans nom dans la config
        if r["slug"] not in ordered_slugs:
            ordered_slugs.append(r["slug"])

    catalogs: dict[str, list[dict]] = {k: [] for k in ADDON_TYPES}
    logo = None
    for slug in ordered_slugs:
        poster, background = banner_urls(repo, slug)
        res = by_slug.get(slug)
        for kind in ADDON_TYPES:
            mid = f"{ADDON_PREFIX}{kind}-{slug}"
            meta_rel = f"meta/{kind}/{mid}.json"
            if res is None:  # echec ce jour-la : on garde l'existant
                try:
                    old = json.loads((out_dir / meta_rel).read_text(encoding="utf-8"))["meta"]
                except (OSError, ValueError, KeyError):
                    continue
                catalogs[kind].append({k: v for k, v in old.items() if k != "videos"})
                continue
            videos = build_videos(res["desired"], kind)
            meta = {"id": mid, "type": kind, "name": res["name"], "poster": poster,
                    "posterShape": "poster", "background": background,
                    "description": f"Filmographie de {res['name']} ({len(videos)} films)"}
            catalogs[kind].append(dict(meta))
            put(meta_rel, _json_bytes({"meta": {**meta, "videos": videos}}))
        if logo is None:
            logo = poster

    put("manifest.json", _json_bytes(build_manifest(logo)))
    for kind in ADDON_TYPES:
        put(f"catalog/{kind}/{ADDON_PREFIX}{kind}.json", _json_bytes({"metas": catalogs[kind]}))
    if not (out_dir / ".nojekyll").exists():
        put(".nojekyll", b"")

    # Nettoyage : metas d'acteurs qui ne sont plus dans la config
    keep = set(ordered_slugs)
    removed = []
    for kind in ADDON_TYPES:
        prefix = f"{ADDON_PREFIX}{kind}-"
        for f in (out_dir / "meta" / kind).glob(f"{prefix}*.json"):
            if f.name[len(prefix):-len(".json")] not in keep:
                f.unlink()
                removed.append(f"meta/{kind}/{f.name}")
    print(f"  Addon : {len(written)} fichier(s) ecrit(s), {len(removed)} supprime(s).")
    for rel in written:
        print(f"    + docs/{rel}")
    for rel in removed:
        print(f"    - docs/{rel}")
    return written


def process_actor(actor: dict, args, cache: dict) -> dict:
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
    if not have_key and args.dry_run:
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
    args = ap.parse_args(argv)
    load_dotenv()

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
    failed = 0
    results: list[dict] = []
    for actor in actors:
        try:
            results.append(process_actor(actor, args, cache))
        except SyncError as exc:
            failed += 1
            print(f"  ERREUR ({actor.get('name')}) : {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERREUR inattendue ({actor.get('name')}) : {exc!r}", file=sys.stderr)
    if not args.dry_run:
        save_cache(cache)
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
