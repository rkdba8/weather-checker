import json
import os
import re
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, Error as PlaywrightError

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

DATE_CIBLE = datetime(2026, 9, 1)

URL_RECHERCHE = (
    "https://www.immoweb.be/en/search/house-and-apartment/for-rent"
    "?countries=BE&maxPrice=1500"
    "&postalCodes=BE-1080,BE-1081,BE-1082,BE-1083,BE-1090,BE-1700,BE-1702,BE-1731,BE-1780"
    "&page=1&orderBy=newest"
)

SEEN_FILE = Path(__file__).parent / "seen.json"

REGEX_URL = re.compile(
    r"https://www\.immoweb\.be/en/classified/[a-z\-]+/for-rent/[^/\"]+/\d+/(\d+)"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# MEMOIRE (remplace PropertiesService)
# ---------------------------------------------------------------------------
def charger_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def sauvegarder_seen(seen_ids):
    SEEN_FILE.write_text(json.dumps(sorted(seen_ids), indent=2))


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
def envoyer_telegram(texte):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Secrets Telegram manquants, message non envoyé.")
        print(texte)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": texte,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        rep = requests.post(url, json=payload, timeout=20)
        print("Telegram HTTP:", rep.status_code)
    except requests.RequestException as e:
        print("Erreur envoi Telegram:", e)


# ---------------------------------------------------------------------------
# NAVIGATEUR (remplace ScraperAPI)
# ---------------------------------------------------------------------------
def get_browser_context(playwright):
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    context = browser.new_context(
        locale="fr-BE",
        timezone_id="Europe/Brussels",
        user_agent=USER_AGENT,
        viewport={"width": 1365, "height": 900},
    )
    return browser, context


def charger_page(context, url, wait_ms=3000):
    """Ouvre une URL et retourne le HTML rendu, avec gestion des erreurs."""
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)
        html = page.content()
        return html
    except PlaywrightError as e:
        print(f"❌ Erreur Playwright sur {url} : {str(e)[:300]}")
        return None
    finally:
        page.close()


# ---------------------------------------------------------------------------
# EXTRACTION
# ---------------------------------------------------------------------------
def extraire_annonces(html):
    annonces = []
    ids_vus = set()
    for m in REGEX_URL.finditer(html):
        id_annonce = m.group(1)
        url = m.group(0).split("?")[0]
        if id_annonce not in ids_vus:
            ids_vus.add(id_annonce)
            annonces.append({"id": id_annonce, "url": url})
    return annonces


def extraire_date_dispo(html):
    # Méthode A : JSON embarqué
    m_json = re.search(r'"availabilityDate"\s*:\s*"([^"]+)"', html, re.IGNORECASE)
    if m_json:
        date_texte = m_json.group(1)
        try:
            return date_texte, datetime.fromisoformat(date_texte.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass

    # Méthode B : tableau HTML
    m_table = re.search(
        r"Available\s+date[\s\S]{0,300}?<td[^>]*>\s*([\w]+\s+\d{1,2}\s+\d{4})\s*</td>",
        html,
        re.IGNORECASE,
    )
    if m_table:
        date_texte = m_table.group(1).strip()
        try:
            return date_texte, datetime.strptime(date_texte, "%B %d %Y")
        except ValueError:
            return date_texte, None

    return None, None


def extraire_prix_titre(html):
    m_prix = re.search(r'"mainValue"\s*:\s*(\d+)', html, re.IGNORECASE)
    m_titre = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    prix = f"{m_prix.group(1)} €/mois" if m_prix else "?"
    titre = re.sub(r"\s*\|.*", "", m_titre.group(1)).strip() if m_titre else "Annonce Immoweb"
    return prix, titre


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    seen = charger_seen()
    print(f"Mémoire actuelle : {len(seen)} annonces déjà traitées.")

    with sync_playwright() as p:
        browser, context = get_browser_context(p)

        # ---- 1. Page de recherche ----
        print("📡 Chargement de la liste...")
        html = charger_page(context, URL_RECHERCHE)
        if not html:
            print("❌ Impossible de charger la page de recherche.")
            browser.close()
            raise SystemExit(1)

        annonces = extraire_annonces(html)
        print(f"{len(annonces)} annonces trouvées.")
        if not annonces:
            print("⚠️ Aucune annonce extraite. Extrait HTML :")
            print(html[:800])
            browser.close()
            return

        # ---- 2. Analyse de chaque annonce ----
        nouvelles_ids = set()
        for annonce in annonces:
            if annonce["id"] in seen:
                print(f"⏩ Déjà traitée : {annonce['id']}")
                continue

            print(f"🔍 Analyse : {annonce['id']}")
            html_a = charger_page(context, annonce["url"], wait_ms=1500)
            nouvelles_ids.add(annonce["id"])  # marquée traitée même si erreur, comme dans l'original

            if not html_a:
                print(f"❌ Impossible de charger {annonce['id']}")
                continue

            date_texte, date_dispo = extraire_date_dispo(html_a)

            if date_dispo:
                print(f"📅 Date : {date_texte}")
                if date_dispo >= DATE_CIBLE:
                    prix, titre = extraire_prix_titre(html_a)
                    msg = (
                        "🟢 <b>Appart dispo dès septembre !</b>\n\n"
                        f"🏠 <b>{titre}</b>\n"
                        f"📅 <b>Disponible :</b> {date_texte}\n"
                        f"💶 <b>Prix :</b> {prix}\n\n"
                        f'<a href="{annonce["url"]}">👉 Voir l\'annonce</a>'
                    )
                    envoyer_telegram(msg)
                    print(f"✅ Notification envoyée : {annonce['id']}")
                else:
                    print(f"⏩ Trop tôt ({date_texte}) : {annonce['id']}")
            else:
                print(f"⏩ Pas de date pour {annonce['id']} (dispo immédiate probable)")

        browser.close()

    # ---- 3. Sauvegarde mémoire ----
    seen.update(nouvelles_ids)
    sauvegarder_seen(seen)
    print("✅ Run terminé.")


if __name__ == "__main__":
    main()
