#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Publication automatique sur Instagram et Facebook a partir d'un planning Google Sheets.
Lance toutes les 15 minutes par GitHub Actions (voir .github/workflows/publier.yml).

Secrets GitHub necessaires :
  META_TOKEN     jeton de l'utilisateur systeme (scarwatch-bot)
  FB_PAGE_ID     identifiant de la Page Facebook
  IG_USER_ID     identifiant du compte Instagram
  SHEET_CSV_URL  lien CSV du Google Sheets ("Publier sur le web")
"""
import csv
import io
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

VERSION = os.environ.get("GRAPH_VERSION", "").strip()
BASE = "https://graph.facebook.com" + (f"/{VERSION}" if VERSION else "")
FICHIER_ETAT = "published.json"
RETARD_MAX_HEURES = float(os.environ.get("RETARD_MAX_HEURES", "24"))
DECALAGE_HEURES = float(os.environ.get("DECALAGE_HEURES", "0"))  # Cote d'Ivoire = heure UTC
TENTATIVES_MAX = 3
MODE = os.environ.get("MODE", "run").strip().lower()  # "test" ou "run"

FORMATS_DATE = [
    "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S",
]
TYPES_IMAGE = {"image", "photo", "img"}
TYPES_VIDEO = {"video", "reel", "reels", "chanson", "musique", "son"}


def log(message):
    print(message, flush=True)


def norm(texte):
    t = unicodedata.normalize("NFD", texte or "")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")


def val(ligne, *noms):
    for nom in noms:
        v = ligne.get(nom, "")
        if v:
            return v
    return ""


# ---------------------------------------------------------------- Meta (Graph API)
def api(methode, chemin, **params):
    url = f"{BASE}/{chemin}"
    if methode == "GET":
        r = requests.request("GET", url, params=params, timeout=120)
    else:
        r = requests.request("POST", url, data=params, timeout=300)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:300]}
    if r.status_code >= 400 or (isinstance(data, dict) and "error" in data):
        err = data.get("error", data) if isinstance(data, dict) else data
        message = err.get("message", err) if isinstance(err, dict) else err
        raise RuntimeError(f"Erreur Meta ({r.status_code}) : {message}")
    return data


def jeton_page(page_id, token):
    data = api("GET", page_id, fields="name,access_token", access_token=token)
    return data.get("access_token", token), data.get("name", "?")


def attendre_ig(container_id, token, delai_max=600):
    debut = time.time()
    while True:
        info = api("GET", container_id, fields="status_code,status", access_token=token)
        code = info.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram a refuse le fichier ({code}) : {info.get('status', '')}")
        if time.time() - debut > delai_max:
            raise RuntimeError("Instagram met trop de temps a traiter le fichier")
        time.sleep(10)


def publier_instagram(ig_id, token, type_, url, legende):
    params = {"caption": legende, "access_token": token}
    if type_ == "image":
        params["image_url"] = url
    else:
        params.update(media_type="REELS", video_url=url, share_to_feed="true")
    container = api("POST", f"{ig_id}/media", **params)["id"]
    attendre_ig(container, token)
    return api("POST", f"{ig_id}/media_publish", creation_id=container, access_token=token).get("id")


def publier_facebook(page_id, page_token, type_, url, legende):
    if type_ == "image":
        res = api("POST", f"{page_id}/photos", url=url, caption=legende,
                  published="true", access_token=page_token)
    else:
        res = api("POST", f"{page_id}/videos", file_url=url, description=legende,
                  access_token=page_token)
    return res.get("post_id") or res.get("id")


# ---------------------------------------------------------------- Planning
def lire_planning(url):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    r.encoding = "utf-8"
    lignes = []
    for brut in csv.DictReader(io.StringIO(r.text)):
        lignes.append({norm(k): (v or "").strip() for k, v in brut.items() if k})
    return lignes


def lire_date(texte):
    for f in FORMATS_DATE:
        try:
            d = datetime.strptime(texte.strip(), f)
            return (d - timedelta(hours=DECALAGE_HEURES)).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def plateformes(texte):
    t = norm(texte)
    if not t or "deux" in t or "tous" in t or "both" in t or "all" in t:
        return ["instagram", "facebook"]
    res = []
    if "insta" in t or t == "ig":
        res.append("instagram")
    if "face" in t or t == "fb":
        res.append("facebook")
    return res


def type_media(texte, media):
    t = norm(texte)
    if t in TYPES_IMAGE:
        return "image"
    if t in TYPES_VIDEO:
        return "video"
    if not t:
        return "video" if re.search(r"\.(mp4|mov|m4v)(\?|$)", media.lower()) else "image"
    return None


def lien_media(valeur):
    v = valeur.strip()
    if "drive.google.com" in v:
        m = re.search(r"/d/([\w-]+)", v) or re.search(r"[?&]id=([\w-]+)", v)
        if m:
            return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
        return v
    if v.lower().startswith("http"):
        return v
    depot = os.environ.get("GITHUB_REPOSITORY", "")
    branche = os.environ.get("GITHUB_REF_NAME", "main")
    nom = v.lstrip("/")
    if not nom.startswith("media/"):
        nom = "media/" + nom
    return f"https://raw.githubusercontent.com/{depot}/{branche}/{quote(nom)}"


# ---------------------------------------------------------------- Etat (ce qui est deja publie)
def charger_etat():
    try:
        with open(FICHIER_ETAT, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def sauver_etat(etat):
    with open(FICHIER_ETAT, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=2, sort_keys=True)


# ---------------------------------------------------------------- Programme principal
def main():
    manquants = [n for n in ("META_TOKEN", "FB_PAGE_ID", "IG_USER_ID", "SHEET_CSV_URL")
                 if not os.environ.get(n, "").strip()]
    if manquants:
        log("ERREUR : il manque ces secrets GitHub : " + ", ".join(manquants))
        return 1
    token = os.environ["META_TOKEN"].strip()
    page_id = os.environ["FB_PAGE_ID"].strip()
    ig_id = os.environ["IG_USER_ID"].strip()
    url_planning = os.environ["SHEET_CSV_URL"].strip()

    try:
        page_token, nom_page = jeton_page(page_id, token)
        nom_ig = api("GET", ig_id, fields="username", access_token=token).get("username", "?")
    except Exception as e:
        log(f"ERREUR : connexion a Meta impossible. {e}")
        return 1
    log(f"OK Page Facebook : {nom_page}")
    log(f"OK Compte Instagram : @{nom_ig}")

    try:
        lignes = lire_planning(url_planning)
    except Exception as e:
        log(f"ERREUR : lecture du Google Sheets impossible. {e}")
        return 1
    log(f"OK Planning lu : {len(lignes)} ligne(s)")

    etat = charger_etat()
    maintenant = datetime.now(timezone.utc)
    erreurs = 0
    publies = 0

    for ligne in lignes:
        id_ = val(ligne, "id")
        if not id_:
            continue
        quand = lire_date(val(ligne, "date_heure", "date", "date_et_heure", "heure"))
        cibles = plateformes(val(ligne, "plateforme", "plateformes", "reseau"))
        media = val(ligne, "media", "fichier", "lien", "url", "media_url")
        type_ = type_media(val(ligne, "type"), media)
        legende = val(ligne, "legende", "texte", "description", "caption")

        if quand is None:
            log(f"[{id_}] date illisible : ligne ignoree")
            continue
        if not cibles or type_ is None or not media:
            log(f"[{id_}] ligne incomplete (plateforme, type ou media) : ignoree")
            continue

        a_faire = [p for p in cibles
                   if etat.get(f"{id_}|{p}", {}).get("statut") != "publie"
                   and etat.get(f"{id_}|{p}", {}).get("tentatives", 0) < TENTATIVES_MAX]
        if not a_faire:
            continue
        if quand > maintenant:
            if MODE == "test":
                log(f"[{id_}] a venir : {quand:%Y-%m-%d %H:%M} (UTC)")
            continue
        if maintenant - quand > timedelta(hours=RETARD_MAX_HEURES):
            if MODE == "test":
                log(f"[{id_}] trop ancien, ignore : {quand:%Y-%m-%d %H:%M} (UTC)")
            continue

        url = lien_media(media)
        for plateforme in a_faire:
            cle = f"{id_}|{plateforme}"
            if MODE == "test":
                log(f"[{id_}] serait publie sur {plateforme} ({type_})")
                continue
            try:
                if plateforme == "instagram":
                    ident = publier_instagram(ig_id, token, type_, url, legende)
                else:
                    ident = publier_facebook(page_id, page_token, type_, url, legende)
                etat[cle] = {"statut": "publie", "numero": str(ident),
                             "date": maintenant.strftime("%Y-%m-%d %H:%M UTC")}
                publies += 1
                log(f"[{id_}] PUBLIE sur {plateforme}")
            except Exception as e:
                essais = etat.get(cle, {}).get("tentatives", 0) + 1
                etat[cle] = {"statut": "echec", "tentatives": essais, "erreur": str(e)[:300]}
                erreurs += 1
                log(f"[{id_}] ECHEC sur {plateforme} (essai {essais}/{TENTATIVES_MAX}) : {e}")
            sauver_etat(etat)

    log(f"Termine : {publies} publication(s), {erreurs} erreur(s).")
    return 1 if erreurs else 0


if __name__ == "__main__":
    sys.exit(main())
