#!/usr/bin/env python3
"""
Veille Back Market : iPhone 16 Pro 256 Go, « SIM physique + eSIM »,
Très bon état ou Parfait état, à 760 € ou moins (tous coloris).

Quand une offre correspond, le script envoie une notification push via ntfy
(app iPhone gratuite) et, si NTFY_EMAIL est renseigné, un e-mail en plus.

Réglages (variables d'environnement) :
  NTFY_TOPIC   obligatoire : nom secret de votre canal ntfy
  NTFY_EMAIL   facultatif  : adresse qui reçoit aussi les alertes par e-mail
  PRIX_MAX     facultatif  : prix maximum en euros (760 par défaut)
  MODE_TEST    facultatif  : "true" pour recevoir un récapitulatif même sans offre

Pourquoi un vrai navigateur : sur une fiche Back Market, le prix affiché à côté
d'un état peut appartenir à une autre configuration (autre SIM, autre stockage).
Le script clique donc sur l'état et relit la fiche pour vérifier ce qui est
réellement proposé avant d'alerter.
"""

import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

# --- Ce que l'on cherche -----------------------------------------------------

PRIX_MAX = float(os.environ.get("PRIX_MAX") or 760)
ETATS_VOULUS = ("Très bon état", "Parfait état")
TOUS_LES_ETATS = ("État correct", "Très bon état", "Parfait état", "Premium")
STOCKAGE = "256 Go"
SIM = "SIM physique + eSIM"

# Fiches des 4 coloris en 256 Go « SIM physique + eSIM » (modèle européen)
PRODUITS = {
    "Titane noir": "https://www.backmarket.fr/fr-fr/p/go/c223b8ba-39cf-40e4-b6d5-1e1182dfd3ef",
    "Titane naturel": "https://www.backmarket.fr/fr-fr/p/go/16169559-2171-4b78-afc3-68bccb852247",
    "Titane sable": "https://www.backmarket.fr/fr-fr/p/go/3f59ddca-1e3e-4c89-807f-87a1da634ef0",
    "Titane blanc": "https://www.backmarket.fr/fr-fr/p/go/ec58cfe8-8a6e-488e-9d5d-3c64333c795a",
}
if os.environ.get("BM_PRODUITS"):  # remplace la liste (tests)
    PRODUITS = json.loads(os.environ["BM_PRODUITS"])

# --- Notifications et mémoire ------------------------------------------------

NTFY_SERVEUR = (os.environ.get("NTFY_SERVEUR") or "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = (os.environ.get("NTFY_TOPIC") or "").strip()
NTFY_EMAIL = (os.environ.get("NTFY_EMAIL") or "").strip()
MODE_TEST = (os.environ.get("MODE_TEST") or "").strip().lower() in ("1", "true", "oui", "yes")
FICHIER_MEMOIRE = Path(os.environ.get("FICHIER_MEMOIRE") or "state.json")
RYTHME = float(os.environ.get("BM_RYTHME") or 1)  # < 1 pour raccourcir les pauses (tests)

MOTIF_PRIX = r"(\d{1,3}(?:[\s\u00a0\u202f]?\d{3})*,\d{2})[\s\u00a0\u202f]*€"


# --- Outils texte ------------------------------------------------------------

def normaliser(texte):
    texte = unicodedata.normalize("NFC", texte or "").replace("\u2019", "'")
    return re.sub(r"[\s\u00a0\u202f]+", " ", texte).strip()


def en_euros(chaine):
    return float(re.sub(r"[\s\u00a0\u202f]", "", chaine).replace(",", "."))


def euros(valeur):
    if valeur is None:
        return "—"
    return f"{valeur:,.2f} €".replace(",", " ").replace(".", ",")


def prix_du_selecteur(texte):
    """Prix affichés dans le sélecteur « Sélectionnez l'état » : {état: prix}."""
    debut = texte.find("Sélectionnez l'état")
    zone = texte[debut:debut + 1500] if debut >= 0 else texte
    reperes = [(m.start(), m.group(0))
               for m in re.finditer("|".join(map(re.escape, TOUS_LES_ETATS)), zone)]
    prix = {}
    for i, (pos, etat) in enumerate(reperes):
        fin = reperes[i + 1][0] if i + 1 < len(reperes) else len(zone)
        morceau = zone[pos + len(etat):fin][:60]  # jamais au-delà de l'état suivant
        m = re.search(MOTIF_PRIX, morceau)
        if m and etat not in prix:
            prix[etat] = en_euros(m.group(1))
    return prix


# --- Navigation --------------------------------------------------------------

JS_CLIC_ETAT = r"""
(etat) => {
  const norm = s => (s || '').normalize('NFC').replace(/\u2019/g, "'")
                             .replace(/[\s\u00a0\u202f]+/g, ' ').trim();
  const prix = /\d{1,3}(?:[\s\u00a0\u202f]?\d{3})*,\d{2}[\s\u00a0\u202f]*€/;
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const candidats = Array.from(document.querySelectorAll(
    'button, [role="radio"], [role="option"], [role="button"], a, label, li'
  )).filter(el => {
    const t = norm(el.innerText);
    return t.startsWith(etat) && t.length < 150 && prix.test(t);
  });
  if (!candidats.length) return null;
  candidats.sort((a, b) =>
    (visible(b) - visible(a)) ||
    ((b.tagName === 'BUTTON') - (a.tagName === 'BUTTON')) ||
    (norm(a.innerText).length - norm(b.innerText).length));
  const el = candidats[0];
  el.scrollIntoView({block: 'center'});
  el.click();
  return norm(el.innerText);
}
"""


def pause(mini, maxi):
    time.sleep(random.uniform(mini, maxi) * RYTHME)


def patienter(page, secondes=2.5):
    page.wait_for_timeout(int(1000 * RYTHME))
    try:
        page.wait_for_load_state("networkidle", timeout=int(6000 * max(RYTHME, 0.3)))
    except Exception:
        pass
    page.wait_for_timeout(int(secondes * 1000 * RYTHME))


def fermer_cookies(page):
    for nom in ("Tout accepter", "Accepter", "OK pour moi"):
        try:
            bouton = page.get_by_role("button", name=re.compile(nom, re.I)).first
            if bouton.is_visible():
                bouton.click(timeout=2000)
                page.wait_for_timeout(500)
                return
        except Exception:
            pass


def ouvrir(page, url):
    """Charge une fiche. Renvoie "ok", "bloqué" ou "page inattendue"."""
    reponse = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    code = reponse.status if reponse else 0
    patienter(page)
    for tentative in range(2):
        html = page.content()
        fiche = "Sélectionnez" in html
        bloque = not fiche and ("captcha-delivery.com" in html or code in (403, 429))
        if not bloque:
            break
        if tentative == 0:  # laisser finir une éventuelle vérification automatique
            page.wait_for_timeout(int(8000 * RYTHME))
    if bloque:
        return "bloqué"
    if "iPhone 16 Pro" not in html:
        return "page inattendue"
    fermer_cookies(page)
    return "ok"


def cliquer_etat(page, etat):
    try:
        return page.evaluate(JS_CLIC_ETAT, etat)
    except Exception as e:
        message = str(e).lower()
        if "context was destroyed" in message or "navigat" in message:
            return "(navigation)"
        return None


def lire_offre(page, etat, prix_affiche, clic):
    """Relit la fiche après le clic et vérifie état, stockage, SIM et prix."""
    try:
        titre = normaliser(page.locator("h1").first.inner_text(timeout=5000))
    except Exception:
        titre = ""
    texte = normaliser(page.inner_text("body"))
    # Chaque prix « … € avant reprise », avec seulement le texte qui le précède
    # depuis le prix précédent (sinon on capterait les sélecteurs voisins)
    blocs = []
    for m in re.finditer(MOTIF_PRIX + " avant reprise", texte):
        debut = max(texte.rfind("€", 0, m.start()) + 1, m.start() - 200)
        blocs.append((en_euros(m.group(1)), texte[debut:m.start()]))
    # Bloc récapitulatif : « État · Batterie · 256 Go · SIM physique + eSIM · Coloris · prix »
    resume = next(((p, avant) for p, avant in blocs
                   if etat in avant and STOCKAGE in avant and SIM in avant), None)
    prix_entete = blocs[0][0] if blocs else None
    config_titre = STOCKAGE in titre and SIM in titre

    if clic is None:
        prix, certitude = None, "clic impossible"
    elif resume:
        prix, certitude = resume[0], "confirmé"
    elif (config_titre and prix_entete is not None and prix_affiche is not None
          and abs(prix_entete - prix_affiche) < 0.005):
        prix, certitude = prix_entete, "probable"
    else:
        prix, certitude = None, "autre configuration"

    couleur = re.search(r"Titane (noir|naturel|sable|blanc)",
                        titre + " " + (resume[1] if resume else ""), re.I)
    return {
        "etat": etat,
        "affiche": prix_affiche,
        "prix": prix,
        "certitude": certitude,
        "couleur": couleur.group(0).capitalize() if couleur else None,
        "titre": titre,
        "url": page.url,
    }


def verifier_coloris(page, couleur, url):
    r = {"couleur": couleur, "url": url, "statut": "", "selecteur": {}, "offres": []}
    r["statut"] = ouvrir(page, url)
    if r["statut"] != "ok":
        return r
    texte = normaliser(page.inner_text("body"))
    r["selecteur"] = prix_du_selecteur(texte)
    if not r["selecteur"]:
        r["statut"] = "prix introuvables"
        i = texte.find("Sélectionnez")
        print("    Extrait de la page :", texte[max(0, i):i + 600] if i >= 0 else texte[:600])
        return r

    premiere = True
    for etat in ETATS_VOULUS:
        affiche = r["selecteur"].get(etat)
        if affiche is None or affiche > PRIX_MAX:
            continue
        if not premiere:  # repartir de la fiche d'origine
            pause(2, 4)
            if ouvrir(page, url) != "ok":
                break
        premiere = False
        clic = cliquer_etat(page, etat)
        patienter(page, 3)
        r["offres"].append(lire_offre(page, etat, affiche, clic))
    return r


def lancer_chrome(p):
    headless = (os.environ.get("HEADLESS", "").lower() in ("1", "true")
                or (sys.platform.startswith("linux") and not os.environ.get("DISPLAY")))
    options = dict(headless=headless,
                   args=["--disable-blink-features=AutomationControlled", "--lang=fr-FR"])
    try:
        navigateur = p.chromium.launch(channel="chrome", **options)  # Google Chrome installé
    except Exception:
        navigateur = p.chromium.launch(**options)  # Chromium de Playwright
    reglages = dict(locale="fr-FR", timezone_id="Europe/Paris",
                    viewport={"width": 1366, "height": 900})
    if headless:  # sans fenêtre, Chrome s'annonce « HeadlessChrome » : on corrige
        majeure = navigateur.version.split(".")[0]
        systeme = {"darwin": "Macintosh; Intel Mac OS X 10_15_7",
                   "win32": "Windows NT 10.0; Win64; x64"}.get(sys.platform, "X11; Linux x86_64")
        reglages["user_agent"] = (f"Mozilla/5.0 ({systeme}) AppleWebKit/537.36 "
                                  f"(KHTML, like Gecko) Chrome/{majeure}.0.0.0 Safari/537.36")
    return navigateur, navigateur.new_context(**reglages)


def verifier():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ Playwright manque : lancez « pip install playwright ».")
        sys.exit(1)
    resultats = []
    with sync_playwright() as p:
        navigateur, contexte = lancer_chrome(p)
        page = contexte.new_page()
        for i, (couleur, url) in enumerate(PRODUITS.items()):
            if i:
                pause(3, 7)
            try:
                r = verifier_coloris(page, couleur, url)
            except Exception as e:
                r = {"couleur": couleur, "url": url, "selecteur": {}, "offres": [],
                     "statut": f"erreur ({type(e).__name__}: {str(e)[:120]})"}
            resultats.append(r)
            sel = r["selecteur"]
            print(f"- {couleur} : {r['statut']}"
                  + (f" | affiché : Très bon {euros(sel.get('Très bon état'))}, "
                     f"Parfait {euros(sel.get('Parfait état'))}" if sel else ""))
            for o in r["offres"]:
                print(f"    {o['etat']} → {euros(o['prix'])} ({o['certitude']}) | {o['titre']}")
        navigateur.close()
    return resultats


# --- Notifications -----------------------------------------------------------

def notifier(titre, message, priorite=3, lien=None, tags=None):
    if not NTFY_TOPIC:
        print(f"  (pas de NTFY_TOPIC, notification non envoyée : {titre})")
        return False
    donnees = {"topic": NTFY_TOPIC, "title": titre, "message": message, "priority": priorite}
    if tags:
        donnees["tags"] = tags
    if lien:
        donnees["click"] = lien
        donnees["actions"] = [{"action": "view", "label": "Ouvrir Back Market", "url": lien}]
    essais = [dict(donnees, email=NTFY_EMAIL), donnees] if NTFY_EMAIL else [donnees]
    for n, corps in enumerate(essais):
        requete = urllib.request.Request(
            NTFY_SERVEUR + "/", data=json.dumps(corps).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(requete, timeout=20):
                pass
            if n:
                print("  E-mail refusé par ntfy : notification envoyée sans e-mail.")
            print(f"  Notification envoyée : {titre}")
            return True
        except urllib.error.HTTPError as e:
            print(f"  ntfy a répondu {e.code} : {e.read().decode('utf-8', 'replace')[:200]}")
        except Exception as e:
            print(f"  Échec de l'envoi ntfy : {e}")
            return False
    return False


# --- Mémoire (pour ne pas répéter la même alerte) ------------------------------

def charger_memoire():
    try:
        return json.loads(FICHIER_MEMOIRE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def resume_github(resultats, trouvees):
    chemin = os.environ.get("GITHUB_STEP_SUMMARY")
    if not chemin:
        return
    lignes = [
        "### Veille Back Market", "",
        f"Cible : iPhone 16 Pro {STOCKAGE}, {SIM}, Très bon ou Parfait état, "
        f"{euros(PRIX_MAX)} maximum.", "",
        "| Coloris | Lecture | Très bon (affiché) | Parfait (affiché) | Vérification |",
        "|---|---|---|---|---|",
    ]
    for r in resultats:
        sel = r["selecteur"]
        verif = "; ".join(f"{o['etat']} : {euros(o['prix'])} ({o['certitude']})"
                          for o in r["offres"]) or "—"
        lignes.append(f"| {r['couleur']} | {r['statut']} | {euros(sel.get('Très bon état'))} "
                      f"| {euros(sel.get('Parfait état'))} | {verif} |")
    lignes += ["", f"Offres valides : {len(trouvees)}"]
    with open(chemin, "a", encoding="utf-8") as f:
        f.write("\n".join(lignes) + "\n")


def main():
    print(f"Veille : iPhone 16 Pro {STOCKAGE}, {SIM}, "
          f"{' ou '.join(ETATS_VOULUS)}, {euros(PRIX_MAX)} maximum")
    if not NTFY_TOPIC:
        print("❌ Le secret NTFY_TOPIC n'est pas défini : aucune notification possible.")
        if os.environ.get("GITHUB_ACTIONS"):
            sys.exit(1)

    memoire = charger_memoire()
    avant = json.dumps(memoire, sort_keys=True)
    deja = set(memoire.get("actives", []))

    resultats = verifier()

    trouvees = {}
    for r in resultats:
        for o in r["offres"]:
            if o["prix"] is not None and o["prix"] <= PRIX_MAX:
                couleur = o["couleur"] or r["couleur"]
                cle = f"{couleur}|{o['etat']}|{o['prix']:.2f}"
                trouvees[cle] = dict(o, couleur=couleur)

    # Alerte uniquement pour les offres nouvelles (ou revenues après disparition)
    for cle, o in sorted(trouvees.items(), key=lambda kv: kv[1]["prix"]):
        if cle in deja:
            continue
        verif = ("Prix vérifié sur la fiche." if o["certitude"] == "confirmé"
                 else "Vérifiez l'état et la SIM sur la fiche avant de payer.")
        notifier(f"iPhone 16 Pro à {euros(o['prix'])} sur Back Market",
                 f"{o['etat']}, {o['couleur']}, {STOCKAGE}, {SIM}\n{verif}",
                 priorite=5, lien=o["url"], tags=["iphone", "moneybag"])

    toutes_lues = all(r["statut"] == "ok" for r in resultats)
    aucune_lue = not any(r["statut"] == "ok" for r in resultats)
    memoire["actives"] = sorted(set(trouvees) if toutes_lues else set(trouvees) | deja)

    # Panne (blocage, page changée…) : une seule alerte, puis une quand ça repart
    statuts = ", ".join(f"{r['couleur']} : {r['statut']}" for r in resultats)
    if aucune_lue:
        if not memoire.get("panne_signalee") and not MODE_TEST:
            notifier("Veille Back Market en panne",
                     f"Aucune fiche n'a pu être lue ({statuts}). "
                     "Cette alerte n'est envoyée qu'une fois.", priorite=3, tags=["warning"])
        memoire["panne_signalee"] = True
    else:
        if memoire.get("panne_signalee"):
            notifier("Veille Back Market : c'est reparti",
                     "Les fiches sont de nouveau lisibles.", priorite=2,
                     tags=["white_check_mark"])
        memoire["panne_signalee"] = False

    if MODE_TEST:
        lignes = ["Prix affichés sur chaque fiche (avant vérification de la config) :"]
        for r in resultats:
            sel = r["selecteur"]
            lignes.append(f"{r['couleur']} : " + (
                f"Très bon {euros(sel.get('Très bon état'))}, Parfait {euros(sel.get('Parfait état'))}"
                if r["statut"] == "ok" else r["statut"]))
        if trouvees:
            lignes.append(f"\nOffres vérifiées à {euros(PRIX_MAX)} ou moins : " + ", ".join(
                f"{o['couleur']} {o['etat']} {euros(o['prix'])}" for o in trouvees.values()))
        else:
            lignes.append(f"\nAucune offre vérifiée à {euros(PRIX_MAX)} ou moins pour l'instant.")
        notifier("Test réussi : la veille lit Back Market" if not aucune_lue
                 else "Test : Back Market bloque la vérification",
                 "\n".join(lignes), priorite=3, tags=["test_tube"])

    resume_github(resultats, trouvees)
    FICHIER_MEMOIRE.write_text(json.dumps(memoire, ensure_ascii=False, indent=1), encoding="utf-8")
    change = json.dumps(memoire, sort_keys=True) != avant
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if change else 'false'}\n")
    print(f"Terminé : {len(trouvees)} offre(s) valide(s).")


if __name__ == "__main__":
    main()
