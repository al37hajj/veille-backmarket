#!/usr/bin/env python3
"""
Veille Back Market : iPhone 16 Pro 256 Go, « SIM physique + eSIM »,
Très bon état ou Parfait état, à 760 € ou moins (tous coloris).

Quand une offre correspond, le script vous prévient sur Telegram (et sur ntfy si
un canal est configuré).

Réglages (secrets GitHub ou variables d'environnement) :
  TELEGRAM_TOKEN    jeton donné par @BotFather
  TELEGRAM_CHAT_ID  facultatif : trouvé tout seul si vous avez écrit à votre bot
  NTFY_TOPIC        facultatif : canal ntfy, en plus de Telegram
  PRIX_MAX          prix maximum en euros (760 par défaut)
  RESUME_HEURE      heure du point quotidien, heure de Paris (9 par défaut, "non" = aucun)
  MODE_TEST         "true" pour recevoir un bilan même sans offre

Pourquoi un vrai navigateur : sur une fiche Back Market, le prix affiché à côté
d'un état appartient souvent à une autre configuration (ex. un 128 Go eSIM moins
cher). Le script clique donc sur l'état, relit ce qui est réellement proposé et,
si ce n'est pas la bonne config, remet « 256 Go » puis « SIM physique + eSIM »
pour trouver le vrai prix de la bonne version. Il n'alerte que si le récapitulatif
de la fiche (état, batterie, stockage, SIM, coloris, prix) concorde avec le titre et
le prix affichés en haut de la fiche.
"""

import html
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
COULEURS = ("Titane noir", "Titane naturel", "Titane sable", "Titane blanc")
SIMS = ("SIM physique + eSIM", "Double SIM physique", "eSIM")  # du plus précis au plus court
MOTIF_STOCKAGE = r"\b(128|256|512|1000) Go\b"

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

try:  # le workflow transmet tous les secrets d'un coup (SECRETS_JSON)
    SECRETS = json.loads(os.environ.get("SECRETS_JSON") or "{}") or {}
except ValueError:
    SECRETS = {}


def reglage(nom, defaut=""):
    valeur = os.environ.get(nom) or SECRETS.get(nom) or defaut
    return valeur.strip() if isinstance(valeur, str) else valeur


TELEGRAM_API = reglage("TELEGRAM_API", "https://api.telegram.org").rstrip("/")
TELEGRAM_TOKEN = reglage("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = reglage("TELEGRAM_CHAT_ID")
NTFY_SERVEUR = reglage("NTFY_SERVEUR", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = reglage("NTFY_TOPIC")
RESUME_HEURE = reglage("RESUME_HEURE", "9")
MODE_TEST = (os.environ.get("MODE_TEST") or "").strip().lower() in ("1", "true", "oui", "yes")
FICHIER_MEMOIRE = Path(os.environ.get("FICHIER_MEMOIRE") or "state.json")
RYTHME = float(os.environ.get("BM_RYTHME") or 1)  # < 1 pour raccourcir les pauses (tests)

# (?<!…) : un prix ne commence pas juste après « 4,5/ » (la note), une virgule ou un chiffre
MOTIF_PRIX = r"(?<![\d,./])(\d{1,3}(?:[\s\u00a0\u202f]?\d{3})*,\d{2})[\s\u00a0\u202f]*€"
PRIX_VRAISEMBLABLES = (100, 3000)  # au-delà : erreur de lecture, pas un prix d'iPhone


# --- Outils texte ------------------------------------------------------------

def normaliser(texte):
    texte = unicodedata.normalize("NFC", texte or "").replace("\u2019", "'")
    return re.sub(r"[\s\u00a0\u202f]+", " ", texte).strip()


def en_euros(chaine):
    return float(re.sub(r"[\s\u00a0\u202f]", "", chaine).replace(",", "."))


def vraisemblable(prix):
    return PRIX_VRAISEMBLABLES[0] <= prix <= PRIX_VRAISEMBLABLES[1]


def euros(valeur):
    if valeur is None:
        return "—"
    return f"{valeur:,.2f} €".replace(",", " ").replace(".", ",")


def prix_de_la_zone(texte, titre_zone, libelles, repli=False):
    """Prix affichés à côté de chaque option d'un sélecteur : {option: prix}."""
    debut = texte.find(titre_zone)
    if debut < 0:
        if not repli:
            return {}
        zone = texte
    else:
        fins = [i for i in (texte.find("Sélectionnez", debut + len(titre_zone)),
                            texte.find("avant reprise", debut)) if i > 0]
        zone = texte[debut:min(fins + [debut + 1500])]
    reperes = [(m.start(), m.group(0))
               for m in re.finditer("|".join(map(re.escape, libelles)), zone)]
    prix = {}
    for i, (pos, libelle) in enumerate(reperes):
        fin = reperes[i + 1][0] if i + 1 < len(reperes) else len(zone)
        # Le prix suit immédiatement l'option (jamais au-delà de l'option suivante) ;
        # « Déjà vendu » = pas de prix, même si un autre montant suit (ex. « Jusqu'à 150 € … »)
        morceau = zone[pos + len(libelle):fin][:40]
        if re.match(r"\s*(Déjà vendu|Indisponible|Épuisé)", morceau, re.I):
            continue
        m = re.match(r"\s*.{0,15}?" + MOTIF_PRIX, morceau)
        if m and libelle not in prix and vraisemblable(en_euros(m.group(1))):
            prix[libelle] = en_euros(m.group(1))
    return prix


def prix_du_selecteur(texte):
    return prix_de_la_zone(texte, "Sélectionnez l'état", TOUS_LES_ETATS, repli=True)


def prix_des_coloris(texte):
    return prix_de_la_zone(texte, "Sélectionnez une couleur", COULEURS)


# --- Navigation --------------------------------------------------------------

JS_REPERER_OPTION = r"""
([etat, exclureSIM]) => {
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
      if (!t.startsWith(etat) || t.length > 90 || (exclureSIM && t.includes('SIM'))) break;
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
        if not vraisemblable(en_euros(m.group(1))):
            continue
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


def cliquer_option(page, libelle):
    """Clique sur une option d'un sélecteur (état, stockage, SIM ou coloris).
    Renvoie (infos, méthode, changement observé)."""
    try:
        infos = page.evaluate(JS_REPERER_OPTION, [libelle, libelle in TOUS_LES_ETATS])
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
                texte = normaliser(fenetre.inner_text(timeout=2000))[:160]
                print(f"      fenêtre ouverte après le clic : « {texte} »")
        except Exception:
            pass
    return infos, methode, changement


def attributs(texte):
    """(stockage, SIM, coloris) mentionnés dans un texte."""
    stockage = re.search(MOTIF_STOCKAGE, texte)
    couleur = re.search(r"Titane (noir|naturel|sable|blanc)", texte, re.I)
    return (f"{stockage.group(1)} Go" if stockage else None,
            next((x for x in SIMS if x in texte), None),
            couleur.group(0).capitalize() if couleur else None)


def lire_config_une_fois(page):
    vue = instantane(page)
    titre, blocs = vue["titre"], vue["blocs"]
    motif_etats = "|".join(map(re.escape, TOUS_LES_ETATS))
    candidats = [(p, a) for p, a in blocs
                 if re.search(motif_etats, a) and re.search(MOTIF_STOCKAGE, a)]
    # Le récapitulatif de la fiche cite la batterie (« Batterie standard ») ; les cartes
    # « Vous aimerez aussi » non. À défaut, le premier bloc (le récap précède ces cartes).
    recap = next((c for c in candidats if "Batterie" in c[1]), candidats[0] if candidats else None)
    t_stockage, t_sim, t_couleur = attributs(titre)  # le titre = identité du produit affiché
    cfg = {"etat": None, "stockage": t_stockage, "sim": t_sim, "couleur": t_couleur,
           "prix": vue["entete"], "certain": False, "coherent": True,
           "titre": titre, "url": page.url, "recap": ""}
    if not recap:
        return cfg
    prix, segment = recap
    # Ne lire que la liste du récapitulatif : à partir du dernier état cité
    etat = None
    for m in re.finditer(motif_etats, segment):
        etat, depart = m.group(0), m.start()
    liste = segment[depart:]
    r_stockage, r_sim, r_couleur = attributs(liste)
    coherent = all(t is None or r == t for r, t in
                   ((r_stockage, t_stockage), (r_sim, t_sim), (r_couleur, t_couleur)))
    if vue["entete"] is not None and abs(vue["entete"] - prix) > 0.005:
        coherent = False  # le prix en haut de fiche doit être celui du récapitulatif
    cfg.update(etat=etat, stockage=r_stockage or t_stockage, sim=r_sim or t_sim,
               couleur=t_couleur or r_couleur, prix=prix, certain=True, coherent=coherent,
               recap=liste[-110:])
    return cfg


def lire_config(page):
    """Ce que la fiche propose vraiment. Le récapitulatif (état, batterie, stockage, SIM,
    coloris, prix) doit concorder avec le titre et le prix en haut de fiche ; sinon on relit
    une fois (la page peut être en cours de mise à jour)."""
    cfg = lire_config_une_fois(page)
    if not cfg["coherent"]:
        page.wait_for_timeout(int(2500 * max(RYTHME, 0.3)))
        cfg = lire_config_une_fois(page)
        if not cfg["coherent"]:
            print(f"      ⚠ lecture incohérente : titre « {cfg['titre']} » / récap « {cfg['recap']} »")
    return cfg


def bonne_config(cfg):
    return cfg["stockage"] == STOCKAGE and cfg["sim"] == SIM and cfg.get("coherent", True)


def est_cible(cfg, etat):
    return bonne_config(cfg) and cfg["etat"] == etat


def decrire(cfg):
    sim = {"SIM physique + eSIM": "SIM + eSIM"}.get(cfg["sim"], cfg["sim"] or "?")
    return (f"{cfg['etat'] or '?'}, {cfg['stockage'] or '?'} {sim}, "
            f"{cfg['couleur'] or '?'}, {euros(cfg['prix'])}"
            + ("" if cfg.get("coherent", True) else " (incohérent)"))


def ajuster(page, etat, cfg):
    """Back Market a proposé une autre config (ex. 128 Go eSIM) : on remet 256 Go puis
    « SIM physique + eSIM » en gardant l'état, pour voir si la bonne version existe."""
    etapes = []
    for libelle, champ in ((STOCKAGE, "stockage"), (SIM, "sim")):
        if cfg[champ] == libelle:
            continue
        infos, _, changement = cliquer_option(page, libelle)
        if not infos or not changement:
            etapes.append(f"{libelle} : option introuvable ou sans effet")
            break
        cfg = lire_config(page)
        etapes.append(f"{libelle} → {decrire(cfg)}")
    if bonne_config(cfg) and cfg["etat"] != etat:  # l'état a pu sauter : on le redemande
        infos, _, changement = cliquer_option(page, etat)
        if infos and changement:
            cfg = lire_config(page)
            etapes.append(f"{etat} → {decrire(cfg)}")
    trouvees, coloris = [], {}
    if est_cible(cfg, etat):
        trouvees.append(cfg)
        # Les autres coloris dans cette config : on vérifie ceux affichés sous le plafond
        coloris = prix_des_coloris(normaliser(page.inner_text("body")))
        for couleur, prix in coloris.items():
            if couleur == cfg["couleur"] or prix > PRIX_MAX:
                continue
            infos, _, changement = cliquer_option(page, couleur)
            if infos and changement:
                autre = lire_config(page)
                etapes.append(f"{couleur} → {decrire(autre)}")
                if est_cible(autre, etat):
                    trouvees.append(autre)
    for e in etapes:
        print(f"      ajustement : {e}")
    return {"offres": trouvees, "etapes": etapes, "coloris": coloris}


def verdict_bonne_config(cfg, couleur_fiche):
    autre = f" ({cfg['couleur']})" if cfg["couleur"] and cfg["couleur"] != couleur_fiche else ""
    if cfg["prix"] is not None and cfg["prix"] <= PRIX_MAX:
        return f"{euros(cfg['prix'])} ✓{autre}"
    return f"{euros(cfg['prix'])} bonne config mais trop cher{autre}"


def verifier_coloris(page, couleur, url, ajustements, vus):
    r = {"couleur": couleur, "url": url, "statut": "", "selecteur": {}, "offres": [],
         "verdicts": {}, "defaut": None}
    r["statut"] = ouvrir(page, url)
    if r["statut"] != "ok":
        return r
    texte = normaliser(page.inner_text("body"))
    r["selecteur"] = sel = prix_du_selecteur(texte)
    if not sel:
        r["statut"] = "prix introuvables"
        i = texte.find("Sélectionnez")
        print("    Extrait de la page :", texte[max(0, i):i + 600] if i >= 0 else texte[:600])
        return r
    r["defaut"] = depart = lire_config(page)
    print(f"  fiche : {decrire(depart)} | affiché : Très bon {euros(sel.get('Très bon état'))}, "
          f"Parfait {euros(sel.get('Parfait état'))}")
    if depart["etat"] in ETATS_VOULUS and est_cible(depart, depart["etat"]):
        r["offres"].append(depart)
        r["verdicts"][depart["etat"]] = verdict_bonne_config(depart, couleur)

    fraiche = True
    for etat in ETATS_VOULUS:
        if etat in r["verdicts"]:
            continue
        affiche = sel.get(etat)
        if affiche is None:
            r["verdicts"][etat] = "déjà vendu"
            continue
        if affiche > PRIX_MAX:  # Back Market affiche le moins cher : rien en dessous
            r["verdicts"][etat] = f"{euros(affiche)} affiché, trop cher"
            continue
        deja_vu = vus.get((etat, affiche))
        if deja_vu and not est_cible(deja_vu, etat):  # même offre mise en avant qu'ailleurs
            r["verdicts"][etat] = f"{euros(affiche)} = {deja_vu['stockage'] or '?'} {deja_vu['sim'] or '?'}"
            print(f"    {etat} (affiché {euros(affiche)}) : déjà vu, {decrire(deja_vu)}")
            continue
        if not fraiche:  # repartir de la fiche d'origine
            pause(2, 4)
            if ouvrir(page, url) != "ok":
                break
        fraiche = False
        infos, methode, changement = cliquer_option(page, etat)
        if not infos:
            r["verdicts"][etat] = "option introuvable"
            continue
        cfg = lire_config(page)
        if (not cfg["certain"] and bonne_config(cfg) and cfg["prix"] is not None
                and abs(cfg["prix"] - affiche) < 0.005):
            cfg["etat"] = etat  # pas de récapitulatif lisible : titre + prix concordants
        print(f"    {etat} (affiché {euros(affiche)}) : {methode} → "
              f"{changement or 'aucun changement'} ⇒ {decrire(cfg)}")
        if changement:
            vus[(etat, affiche)] = cfg
        if est_cible(cfg, etat):
            r["offres"].append(cfg)
            r["verdicts"][etat] = verdict_bonne_config(cfg, couleur)
            continue
        if not changement:
            r["verdicts"][etat] = "clic sans effet"
            print(f"      élément cliqué : {infos['chaine']}")
            continue
        sim = {"SIM physique + eSIM": "SIM + eSIM"}.get(cfg["sim"], cfg["sim"] or "?")
        r["verdicts"][etat] = f"{euros(cfg['prix'])} = {cfg['stockage'] or '?'} {sim}"
        if etat not in ajustements:  # une seule recherche par état et par passage
            ajustements[etat] = ajuster(page, etat, cfg)
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
    resultats, ajustements, vus = {}, {}, {}
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
                    r = verifier_coloris(page, couleur, url, ajustements, vus)
                except Exception as e:
                    r = {"couleur": couleur, "url": url, "selecteur": {}, "offres": [],
                         "verdicts": {}, "defaut": None,
                         "statut": f"erreur ({type(e).__name__}: {str(e)[:120]})"}
                resultats[couleur] = r
                if r["statut"] != "ok":
                    print(f"  ⇒ {r['statut']}")
        navigateur.close()
    return [resultats[c] for c, _ in PRODUITS.items()], ajustements, vus


# --- Notifications -----------------------------------------------------------

def appel_http(url, donnees=None, timeout=20):
    """POST JSON (ou GET si donnees est None). Renvoie (code, corps décodé)."""
    corps = json.dumps(donnees).encode("utf-8") if donnees is not None else None
    requete = urllib.request.Request(url, data=corps, method="POST" if corps else "GET",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(requete, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            return e.code, {}
    except Exception as e:
        return 0, {"description": str(e)}


def trouver_chat_telegram(memoire):
    """Identifiant de la conversation : secret, sinon mémoire, sinon dernier message reçu par le bot."""
    if TELEGRAM_CHAT_ID:
        return TELEGRAM_CHAT_ID, "secret"
    if memoire.get("telegram_chat_id"):
        return str(memoire["telegram_chat_id"]), "mémoire"
    code, rep = appel_http(f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/getUpdates")
    if code == 401:
        print("❌ Telegram refuse le jeton : vérifiez le secret TELEGRAM_TOKEN.")
        return None, "jeton refusé"
    for maj in reversed(rep.get("result") or []):
        message = maj.get("message") or maj.get("my_chat_member") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            memoire["telegram_chat_id"] = chat["id"]
            return str(chat["id"]), "trouvé"
    print("❌ Telegram : aucun message reçu par le bot. Ouvrez votre bot, touchez « Démarrer » "
          "(ou envoyez-lui un message), puis relancez.")
    return None, "introuvable"


CANAUX = {"telegram": None}  # rempli par main() : identifiant de conversation Telegram


def envoyer_telegram(titre, message, lien=None, silencieux=False):
    chat = CANAUX.get("telegram")
    if not (TELEGRAM_TOKEN and chat):
        return False
    texte = f"<b>{html.escape(titre, quote=False)}</b>\n{html.escape(message, quote=False)}"
    donnees = {"chat_id": chat, "text": texte[:4000], "parse_mode": "HTML",
               "disable_web_page_preview": True, "disable_notification": silencieux}
    if lien:
        donnees["reply_markup"] = {"inline_keyboard": [[{"text": "Ouvrir Back Market", "url": lien}]]}
    code, rep = appel_http(f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/sendMessage", donnees)
    if code == 200 and rep.get("ok"):
        return True
    print(f"  Telegram a répondu {code} : {str(rep.get('description', rep))[:200]}")
    return False


def envoyer_ntfy(titre, message, priorite=3, lien=None, tags=None):
    if not NTFY_TOPIC:
        return False
    donnees = {"topic": NTFY_TOPIC, "title": titre, "message": message, "priority": priorite}
    if tags:
        donnees["tags"] = tags
    if lien:
        donnees["click"] = lien
        donnees["actions"] = [{"action": "view", "label": "Ouvrir Back Market", "url": lien}]
    code, rep = appel_http(NTFY_SERVEUR + "/", donnees)
    if 200 <= code < 300:
        return True
    print(f"  ntfy a répondu {code} : {str(rep)[:200]}")
    return False


def notifier(titre, message, priorite=3, lien=None, tags=None, silencieux=False):
    envois = [nom for nom, ok in (
        ("Telegram", envoyer_telegram(titre, message, lien, silencieux)),
        ("ntfy", envoyer_ntfy(titre, message, priorite, lien, tags))) if ok]
    print(f"  Notification {'envoyée (' + ', '.join(envois) + ')' if envois else 'NON envoyée'} : {titre}")
    return bool(envois)


# --- Mémoire (pour ne pas répéter la même alerte) ------------------------------

def charger_memoire():
    try:
        return json.loads(FICHIER_MEMOIRE.read_text(encoding="utf-8"))
    except Exception:
        return {}


ABREGE = {"Très bon état": "Très bon", "Parfait état": "Parfait"}


def court(couleur):
    return (couleur or "?").replace("Titane ", "")


def lignes_bilan(resultats, ajustements, vus):
    """Bilan lisible : prix des fiches, puis pour chaque état le vrai prix en 256 Go SIM + eSIM."""
    fiches = []
    for r in resultats:
        d = r["defaut"]
        if r["statut"] != "ok":
            fiches.append(f"{court(r['couleur'])} {r['statut']}")
        elif d and d["prix"] is not None:
            fiches.append(f"{court(r['couleur'])} {euros(d['prix'])}" + (f" ({d['etat']})" if d["etat"] else ""))
    lignes = ["Fiches 256 Go SIM + eSIM : " + ", ".join(fiches)]
    verifiees = [o for r in resultats for o in r["offres"]]
    verifiees += [o for a in ajustements.values() for o in a["offres"]]
    for etat in ETATS_VOULUS:
        bonnes = {}
        for o in verifiees:
            if est_cible(o, etat) and o["prix"] is not None:
                c = court(o["couleur"])
                bonnes[c] = min(o["prix"], bonnes.get(c, o["prix"]))
        morceaux = [", ".join(f"{c} {euros(p)} vérifié" for c, p in sorted(bonnes.items(), key=lambda t: t[1]))] if bonnes else []
        a = ajustements.get(etat) or {}
        affiches = [f"{court(c)} {euros(p)}" for c, p in (a.get("coloris") or {}).items()
                    if court(c) not in bonnes]
        if affiches:
            morceaux.append(", ".join(affiches) + " affiché(s)")
        if not morceaux:
            vus_ici = [r["selecteur"].get(etat) for r in resultats if r["statut"] == "ok"]
            vus_ici = [v for v in vus_ici if v is not None]
            if vus_ici and min(vus_ici) > PRIX_MAX:
                morceaux.append(f"tout est au-dessus ({euros(min(vus_ici))} minimum)")
            else:
                morceaux.append("aucune trouvée")
        ligne = f"{ABREGE[etat]} en 256 Go SIM + eSIM : " + " ; ".join(morceaux)
        promus = sorted({(v["prix"], v["stockage"], v["sim"]) for (e, _), v in vus.items()
                         if e == etat and not est_cible(v, e) and v["prix"] is not None})
        if promus:
            ligne += " (mis en avant : " + ", ".join(
                f"{st or '?'} {si or '?'} {euros(px)}" for px, st, si in promus) + ")"
        lignes.append(ligne)
    return lignes


def resume_github(resultats, ajustements, vus, trouvees):
    chemin = os.environ.get("GITHUB_STEP_SUMMARY")
    if not chemin:
        return
    lignes = ["### Veille Back Market", "",
              f"Cible : iPhone 16 Pro {STOCKAGE}, {SIM}, Très bon ou Parfait état, "
              f"{euros(PRIX_MAX)} maximum.", ""]
    lignes += [f"- {l}" for l in lignes_bilan(resultats, ajustements, vus)]
    lignes += ["", f"Offres valides : {len(trouvees)}"]
    with open(chemin, "a", encoding="utf-8") as f:
        f.write("\n".join(lignes) + "\n")


def maintenant_paris():
    from datetime import datetime, timedelta, timezone
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Paris"))
    except Exception:  # base de fuseaux absente : approximation heure d'été
        return datetime.now(timezone(timedelta(hours=2)))


def main():
    print(f"Veille : iPhone 16 Pro {STOCKAGE}, {SIM}, "
          f"{' ou '.join(ETATS_VOULUS)}, {euros(PRIX_MAX)} maximum")
    if not (TELEGRAM_TOKEN or NTFY_TOPIC):
        print("❌ Aucun canal : ajoutez le secret TELEGRAM_TOKEN (ou NTFY_TOPIC).")
        if os.environ.get("GITHUB_ACTIONS"):
            sys.exit(1)

    memoire = charger_memoire()
    avant = json.dumps(memoire, sort_keys=True)
    deja = set(memoire.get("actives", []))
    memoire.pop("email_refuse", None)  # ancienne option e-mail, abandonnée

    chat_trouve = None
    if TELEGRAM_TOKEN:
        CANAUX["telegram"], origine = trouver_chat_telegram(memoire)
        chat_trouve = CANAUX["telegram"] if origine == "trouvé" else None
        print(f"Telegram : conversation {CANAUX['telegram'] or '—'} ({origine})")

    resultats, ajustements, vus = verifier()

    # Toutes les offres dont la config a été vérifiée (fiches + ajustements)
    trouvees = {}
    offres = [o for r in resultats for o in r["offres"]]
    offres += [o for a in ajustements.values() for o in a["offres"]]
    for o in offres:
        if o["prix"] is not None and o["prix"] <= PRIX_MAX and est_cible(o, o["etat"]):
            couleur = o["couleur"] or "?"
            trouvees[f"{couleur}|{o['etat']}|{o['prix']:.2f}"] = dict(o, couleur=couleur)

    # Alerte uniquement pour les offres nouvelles (ou revenues après disparition)
    for cle, o in sorted(trouvees.items(), key=lambda kv: kv[1]["prix"]):
        if cle in deja:
            continue
        verif = ("Config vérifiée : récapitulatif, titre et prix concordent." if o["certain"]
                 else "Vérifiez l'état et la SIM sur la fiche avant de payer.")
        notifier(f"iPhone 16 Pro à {euros(o['prix'])} sur Back Market",
                 f"{o['etat']}, {o['couleur']}, {STOCKAGE}, {SIM}\n{verif}",
                 priorite=5, lien=o["url"], tags=["iphone", "moneybag"])

    toutes_lues = all(r["statut"] == "ok" for r in resultats)
    aucune_lue = not any(r["statut"] == "ok" for r in resultats)
    memoire["actives"] = sorted(set(trouvees) if toutes_lues else set(trouvees) | deja)

    # Panne : signalée après 2 passages ratés d'affilée (un blocage isolé est courant)
    statuts = ", ".join(f"{court(r['couleur'])} : {r['statut']}" for r in resultats)
    if aucune_lue:
        memoire["echecs"] = memoire.get("echecs", 0) + 1
        if memoire["echecs"] >= 2 and not memoire.get("panne_signalee") and not MODE_TEST:
            notifier("Veille Back Market en panne",
                     f"Aucune fiche lue depuis {memoire['echecs']} passages ({statuts}). "
                     "Cette alerte n'est envoyée qu'une fois.", priorite=3, tags=["warning"])
            memoire["panne_signalee"] = True
    else:
        memoire["echecs"] = 0
        if memoire.get("panne_signalee"):
            notifier("Veille Back Market : c'est reparti",
                     "Les fiches sont de nouveau lisibles.", priorite=2,
                     tags=["white_check_mark"], silencieux=True)
        memoire["panne_signalee"] = False

    bilan = lignes_bilan(resultats, ajustements, vus)
    print("Bilan :\n  " + "\n  ".join(bilan))
    conclusion = ("\nOffres valides : " + ", ".join(
        f"{o['couleur']} {o['etat']} {euros(o['prix'])}" for o in trouvees.values())
        if trouvees else f"\nAucune offre valide à {euros(PRIX_MAX)} ou moins.")
    lues = sum(r["statut"] == "ok" for r in resultats)

    if MODE_TEST:
        corps = "\n".join(bilan) + conclusion
        if chat_trouve:
            corps += (f"\n\nVotre identifiant Telegram est {chat_trouve}. Ajoutez-le en secret "
                      "TELEGRAM_CHAT_ID pour que les alertes ne dépendent plus de la mémoire.")
        notifier(f"Test : {lues}/{len(resultats)} fiches lues", corps,
                 priorite=3, tags=["test_tube"])
    elif RESUME_HEURE.isdigit():  # point quotidien discret, au premier passage après l'heure choisie
        heure = maintenant_paris()
        jour = heure.strftime("%Y-%m-%d")
        if heure.hour >= int(RESUME_HEURE) and memoire.get("resume_du") != jour:
            if notifier(f"Veille Back Market : point du jour ({lues}/{len(resultats)} fiches lues)",
                        "\n".join(bilan) + conclusion, priorite=2, tags=["calendar"],
                        silencieux=True):
                memoire["resume_du"] = jour

    resume_github(resultats, ajustements, vus, trouvees)
    FICHIER_MEMOIRE.write_text(json.dumps(memoire, ensure_ascii=False, indent=1), encoding="utf-8")
    change = json.dumps(memoire, sort_keys=True) != avant
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if change else 'false'}\n")
    print(f"Terminé : {len(trouvees)} offre(s) valide(s).")
    if TELEGRAM_TOKEN and not CANAUX.get("telegram") and MODE_TEST:
        sys.exit(1)  # test en rouge : Telegram n'est pas encore relié


if __name__ == "__main__":
    main()
