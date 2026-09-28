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

JS_REPERER_ETAT = r"""
(etat) => {
  const norm = s => (s || '').normalize('NFC').replace(/\u2019/g, "'")
                             .replace(/[\s\u00a0\u202f]+/g, ' ').trim();
  const prix = /\d{1,3}(?:[\s\u00a0\u202f]?\d{3})*,\d{2}[\s\u00a0\u202f]*€/;
  const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  document.querySelectorAll('[data-veille-cible]').forEach(e => e.removeAttribute('data-veille-cible'));
  const marche = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let cible = null;
  while (!cible && marche.nextNode()) {
    if (!norm(marche.currentNode.nodeValue).startsWith(etat)) continue;
    // Remonter jusqu'au plus petit bloc qui contient l'état ET un prix (l'option du sélecteur)
    for (let n = marche.currentNode.parentElement, i = 0; n && i < 7; n = n.parentElement, i++) {
      if (!visible(n)) break;
      const t = norm(n.innerText);
      if (!t.startsWith(etat) || t.length > 90 || t.includes('SIM')) break;
      if (prix.test(t)) { cible = n; break; }
    }
  }
  if (!cible) return null;
  const decrire = n => {
    if (!n) return '';
    const attrs = [...n.attributes]
      .filter(a => /^(id|href|role|type|name|value|disabled|aria-[a-z]+|data-[a-z0-9-]+)$/.test(a.name))
      .map(a => `${a.name}="${a.value.slice(0, 40)}"`).join(' ');
    return `<${n.tagName.toLowerCase()}${attrs ? ' ' + attrs : ''}>`;
  };
  const chaine = [];
  for (let n = cible, i = 0; n && i < 4; n = n.parentElement, i++) chaine.push(decrire(n));
  const interactif = cible.closest(
    'a[href],button,[role="radio"],[role="option"],[role="button"],[role="tab"],label,[tabindex]');
  cible.setAttribute('data-veille-cible', 'bloc');
  if (interactif && interactif !== cible) interactif.setAttribute('data-veille-cible', 'interactif');
  return {texte: norm(cible.innerText), chaine: chaine.join(' < ')};
}
"""

JS_CLIC_SECOURS = """() => {
  const el = document.querySelector('[data-veille-cible="interactif"]')
          || document.querySelector('[data-veille-cible="bloc"]');
  if (el) el.click();
}"""


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


MARQUEURS_BLOCAGE = ("captcha-delivery.com", "datadome", "cf-chl", "challenge-platform",
                     "just a moment", "attention required", "access denied", "accès refusé",
                     "vous avez été bloqué", "enable js and disable any ad blocker")


def est_fiche(html):
    return "Sélectionnez" in html and "iPhone 16 Pro" in html


def contenu(page):
    try:
        return page.content()
    except Exception:  # page en cours de navigation
        return ""


def diagnostic(page, code):
    try:
        titre = page.title()
    except Exception:
        titre = "?"
    try:
        extrait = normaliser(page.inner_text("body"))[:200]
    except Exception:
        extrait = ""
    return f"HTTP {code} | {page.url} | titre « {titre} » | texte : {extrait}"


def ouvrir(page, url):
    """Charge une fiche. Renvoie "ok", "bloqué" ou "page inattendue"."""
    reponse = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    code = reponse.status if reponse else 0
    patienter(page)
    html = contenu(page)
    fin = time.time() + 20 * RYTHME  # une vérification anti-robot peut se résoudre seule
    while not est_fiche(html) and time.time() < fin:
        page.wait_for_timeout(2500)
        html = contenu(page)
    if est_fiche(html):
        fermer_cookies(page)
        return "ok"
    bas = html.lower()
    statut = ("bloqué" if code >= 400 or any(m in bas for m in MARQUEURS_BLOCAGE)
              else "page inattendue")
    print(f"    {statut} : {diagnostic(page, code)}")
    return statut


def echauffement(page):
    """Visite l'accueil d'abord, comme un visiteur normal."""
    accueil = os.environ.get("BM_ACCUEIL") or "https://www.backmarket.fr/fr-fr"
    try:
        page.goto(accueil, wait_until="domcontentloaded", timeout=45000)
        patienter(page, 3)
        fermer_cookies(page)
    except Exception as e:
        print(f"  (accueil injoignable : {str(e)[:100]})")


def blocs_prix(texte):
    """Chaque prix « … € avant reprise », avec seulement le texte qui le précède
    depuis le prix précédent (sinon on capterait les sélecteurs voisins)."""
    blocs = []
    for m in re.finditer(MOTIF_PRIX + " avant reprise", texte):
        debut = max(texte.rfind("€", 0, m.start()) + 1, m.start() - 200)
        blocs.append((en_euros(m.group(1)), texte[debut:m.start()].strip()))
    return blocs


def instantane(page):
    try:
        titre = normaliser(page.locator("h1").first.inner_text(timeout=2000))
    except Exception:
        titre = ""
    try:
        texte = normaliser(page.inner_text("body"))
    except Exception:
        texte = ""
    blocs = blocs_prix(texte)
    return {"url": page.url, "titre": titre, "blocs": blocs,
            "entete": blocs[0][0] if blocs else None}


def attendre_changement(page, avant, secondes):
    fin = time.time() + secondes * max(RYTHME, 0.3)
    while time.time() < fin:
        page.wait_for_timeout(500)
        apres = instantane(page)
        if apres["url"] != avant["url"]:
            return "page changée"
        if apres["titre"] and apres["titre"] != avant["titre"]:
            return "titre changé"
        if apres["entete"] is not None and apres["entete"] != avant["entete"]:
            return f"prix {euros(avant['entete'])} → {euros(apres['entete'])}"
        if apres["blocs"] and apres["blocs"] != avant["blocs"]:
            return "récapitulatif changé"
    return None


def cliquer_etat(page, etat):
    """Clique sur l'état voulu. Renvoie (infos, méthode, changement observé)."""
    try:
        infos = page.evaluate(JS_REPERER_ETAT, etat)
    except Exception:
        infos = None
    if not infos:
        return None, "", None
    avant = instantane(page)
    try:
        page.locator('[data-veille-cible="bloc"]').first.click(timeout=5000)
        methode = "clic"
    except Exception as e:
        methode = f"clic refusé ({type(e).__name__})"
    changement = attendre_changement(page, avant, 12)
    if not changement:  # second essai : clic direct sur l'élément interactif
        try:
            page.evaluate(JS_CLIC_SECOURS)
            methode += " + clic de secours"
        except Exception:
            pass
        changement = attendre_changement(page, avant, 8)
    if changement:
        patienter(page, 2)
    else:
        try:
            fenetre = page.locator('[role="dialog"]:visible, [aria-modal="true"]:visible').first
            if fenetre.count():
                changement_txt = normaliser(fenetre.inner_text(timeout=2000))[:160]
                print(f"      fenêtre ouverte après le clic : « {changement_txt} »")
        except Exception:
            pass
    return infos, methode, changement


def lire_offre(page, etat, prix_affiche):
    """Relit la fiche après le clic et vérifie état, stockage, SIM et prix."""
    vue = instantane(page)
    titre, blocs, prix_entete = vue["titre"], vue["blocs"], vue["entete"]
    # Bloc récapitulatif : « État · Batterie · 256 Go · SIM physique + eSIM · Coloris · prix »
    resume = next(((p, avant) for p, avant in blocs
                   if etat in avant and STOCKAGE in avant and SIM in avant), None)
    config_titre = STOCKAGE in titre and SIM in titre
    if resume:
        prix, certitude = resume[0], "confirmé"
    elif (config_titre and prix_entete is not None and prix_affiche is not None
          and abs(prix_entete - prix_affiche) < 0.005):
        prix, certitude = prix_entete, "probable"
    else:
        prix, certitude = None, "autre configuration"
    couleur = re.search(r"Titane (noir|naturel|sable|blanc)",
                        titre + " " + (resume[1] if resume else ""), re.I)
    return {
        "etat": etat, "affiche": prix_affiche, "prix": prix, "certitude": certitude,
        "couleur": couleur.group(0).capitalize() if couleur else None,
        "titre": titre, "url": page.url, "entete": prix_entete,
        "recap": blocs[-1][1][-110:] if blocs else "",
    }


def verifier_coloris(page, couleur, url):
    r = {"couleur": couleur, "url": url, "statut": "", "selecteur": {}, "offres": []}
    r["statut"] = ouvrir(page, url)
    if r["statut"] != "ok":
        return r
    texte = normaliser(page.inner_text("body"))
    r["selecteur"] = prix_du_selecteur(texte)
    sel = r["selecteur"]
    if sel:
        print(f"  affiché : Très bon {euros(sel.get('Très bon état'))}, "
              f"Parfait {euros(sel.get('Parfait état'))}")
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
        infos, methode, changement = cliquer_etat(page, etat)
        if infos is None:
            offre = {"etat": etat, "affiche": affiche, "prix": None, "couleur": None,
                     "certitude": "option introuvable", "titre": "", "url": page.url,
                     "entete": None, "recap": ""}
        else:
            offre = lire_offre(page, etat, affiche)
            if not changement and offre["certitude"] != "confirmé":
                offre["certitude"] = "clic sans effet"
        r["offres"].append(offre)
        print(f"    {etat} (affiché {euros(affiche)}) : {methode or '—'} → "
              f"{changement or 'aucun changement'} ⇒ {euros(offre['prix'])} ({offre['certitude']})")
        if offre["certitude"] not in ("confirmé", "probable"):
            if infos:
                print(f"      élément cliqué : {infos['chaine']}")
            print(f"      après : titre « {offre['titre']} » | en-tête {euros(offre['entete'])} "
                  f"| récap « {offre['recap']} » | {offre['url']}")
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
    resultats = {}
    with sync_playwright() as p:
        navigateur, contexte = lancer_chrome(p)
        page = contexte.new_page()
        echauffement(page)
        coloris = list(PRODUITS.items())
        for passage in (1, 2):
            a_faire = [(c, u) for c, u in coloris
                       if passage == 1 or resultats[c]["statut"] != "ok"]
            if passage == 2 and a_faire:
                print(f"Nouvel essai pour : {', '.join(c for c, _ in a_faire)}")
                pause(8, 15)
            for i, (couleur, url) in enumerate(a_faire):
                if i:
                    pause(3, 7)
                print(f"- {couleur}")
                try:
                    r = verifier_coloris(page, couleur, url)
                except Exception as e:
                    r = {"couleur": couleur, "url": url, "selecteur": {}, "offres": [],
                         "statut": f"erreur ({type(e).__name__}: {str(e)[:120]})"}
                resultats[couleur] = r
                if r["statut"] != "ok":
                    print(f"  ⇒ {r['statut']}")
        navigateur.close()
    return [resultats[c] for c, _ in PRODUITS.items()]


# --- Notifications -----------------------------------------------------------

EMAIL_REFUSE = False


def notifier(titre, message, priorite=3, lien=None, tags=None):
    global EMAIL_REFUSE
    if not NTFY_TOPIC:
        print(f"  (pas de NTFY_TOPIC, notification non envoyée : {titre})")
        return False
    donnees = {"topic": NTFY_TOPIC, "title": titre, "message": message, "priority": priorite}
    if tags:
        donnees["tags"] = tags
    if lien:
        donnees["click"] = lien
        donnees["actions"] = [{"action": "view", "label": "Ouvrir Back Market", "url": lien}]
    essais = ([dict(donnees, email=NTFY_EMAIL), donnees] if NTFY_EMAIL and not EMAIL_REFUSE
              else [donnees])
    for n, corps in enumerate(essais):
        requete = urllib.request.Request(
            NTFY_SERVEUR + "/", data=json.dumps(corps).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(requete, timeout=20):
                pass
            if n:
                EMAIL_REFUSE = True
                print("  E-mail refusé par ntfy (compte requis) : notification envoyée sans e-mail. "
                      "Vous pouvez supprimer le secret NTFY_EMAIL.")
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
        abrege = {"Très bon état": "Très bon", "Parfait état": "Parfait"}
        lignes = []
        for r in resultats:
            if r["statut"] != "ok":
                lignes.append(f"{r['couleur']} : {r['statut']}")
                continue
            sel = r["selecteur"]
            details = []
            for etat in ETATS_VOULUS:
                o = next((o for o in r["offres"] if o["etat"] == etat), None)
                affiche = euros(sel.get(etat))
                if o is None:
                    details.append(f"{abrege[etat]} {affiche}")
                elif o["certitude"] in ("confirmé", "probable"):
                    details.append(f"{abrege[etat]} {euros(o['prix'])} ✓")
                else:
                    details.append(f"{abrege[etat]} {affiche} ({o['certitude']})")
            lignes.append(f"{r['couleur']} : " + ", ".join(details))
        if trouvees:
            lignes.append("\nOffres vérifiées : " + ", ".join(
                f"{o['couleur']} {o['etat']} {euros(o['prix'])}" for o in trouvees.values()))
        else:
            lignes.append(f"\nAucune offre vérifiée à {euros(PRIX_MAX)} ou moins.")
        lues = sum(r["statut"] == "ok" for r in resultats)
        notifier(f"Test : {lues}/{len(resultats)} fiches lues", "\n".join(lignes),
                 priorite=3, tags=["test_tube"])

    resume_github(resultats, trouvees)
    FICHIER_MEMOIRE.write_text(json.dumps(memoire, ensure_ascii=False, indent=1), encoding="utf-8")
    change = json.dumps(memoire, sort_keys=True) != avant
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if change else 'false'}\n")
    print(f"Terminé : {len(trouvees)} offre(s) valide(s).")


if __name__ == "__main__":
    main()
