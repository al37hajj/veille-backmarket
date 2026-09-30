#!/usr/bin/env python3
"""
Historique des prix de la veille Back Market (iPhone 16 Pro 256 Go SIM physique + eSIM).

  python historique.py ajouter       ajoute le relevé du passage (releve.json) à historique.csv
  python historique.py reconstituer  relit les logs des passages précédents via l'API GitHub
  python historique.py dessiner      redessine le graphique sans rien ajouter

Le graphique (historique.png) est redessiné au point du jour, lors d'un test et après une
reconstitution : il est alors envoyé sur Telegram et affiché en tête du dépôt (README.md).
"""

import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

FICHIER_CSV = Path(os.environ.get("HISTORIQUE_CSV") or "historique.csv")
FICHIER_PNG = Path(os.environ.get("HISTORIQUE_PNG") or "historique.png")
FICHIER_README = Path(os.environ.get("HISTORIQUE_README") or "README.md")
FICHIER_RELEVE = Path(os.environ.get("FICHIER_RELEVE") or "releve.json")
FICHIER_MEMOIRE = Path(os.environ.get("FICHIER_MEMOIRE") or "state.json")
COLONNES = ["horodatage", "run_id", "etat", "couleur", "prix", "type", "version"]
ETATS = {"Très bon état": ("Très bon", "#1f6feb"), "Parfait état": ("Parfait", "#1a7f37")}
PRIX = r"(\d{1,3}(?:[\s\u00a0\u202f]\d{3})*,\d{2})"

try:  # le workflow transmet tous les secrets d'un coup (SECRETS_JSON)
    SECRETS = json.loads(os.environ.get("SECRETS_JSON") or "{}") or {}
except ValueError:
    SECRETS = {}


def reglage(nom, defaut=""):
    valeur = os.environ.get(nom) or SECRETS.get(nom) or defaut
    return valeur.strip() if isinstance(valeur, str) else valeur


PRIX_MAX = float(reglage("PRIX_MAX", "760"))
TELEGRAM_API = reglage("TELEGRAM_API", "https://api.telegram.org").rstrip("/")
TELEGRAM_TOKEN = reglage("TELEGRAM_TOKEN")
GITHUB_API = reglage("GITHUB_API_URL", "https://api.github.com").rstrip("/")
GITHUB_TOKEN = reglage("GITHUB_TOKEN") or SECRETS.get("github_token", "")


def en_euros(texte):
    return float(re.sub(r"[\s\u00a0\u202f]", "", texte).replace(",", "."))


def euros(valeur):
    return f"{valeur:,.0f} €".replace(",", " ")


def paris(moment):
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo("Europe/Paris"))
    except Exception:
        return moment.astimezone(timezone(timedelta(hours=2)))


# --- Historique CSV ------------------------------------------------------------

def charger_csv():
    if not FICHIER_CSV.exists():
        return []
    with FICHIER_CSV.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def ecrire_csv(lignes):
    uniques = {}
    for l in lignes:  # une ligne par passage, état, coloris et type de prix
        uniques[(l["run_id"], l["etat"], l["couleur"], l["type"])] = l
    tri = sorted(uniques.values(), key=lambda l: (l["horodatage"], l["etat"], l["couleur"], l["type"]))
    with FICHIER_CSV.open("w", encoding="utf-8", newline="") as f:
        ecrivain = csv.DictWriter(f, fieldnames=COLONNES, lineterminator="\n")
        ecrivain.writeheader()
        ecrivain.writerows({k: l.get(k, "") for k in COLONNES} for l in tri)
    return len(tri)


# --- Relecture d'un ancien log ---------------------------------------------------

def lire_log(texte):
    """Prix de la config cherchée dans le log d'un passage. Renvoie (version, [(état, coloris, prix, type)]).
    Deux formats connus : l'actuel (« Très bon en 256 Go SIM + eSIM : … ») et l'ancien
    (« Meilleur Très bon en … : 5 834,99 € », où le « 5 » vient de la note 4,5/5 collée au prix)."""
    version, lignes = None, []
    for brute in texte.splitlines():
        ligne = re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?", "", brute)
        m = re.match(r"\s*fiche : ([^,]+), 256 Go SIM \+ eSIM, Titane (\w+), " + PRIX + " €", ligne)
        if m:  # prix par défaut de chaque fiche (souvent « État correct »)
            lignes.append((m.group(1).strip(), m.group(2), en_euros(m.group(3)), "fiche"))
            continue
        m = re.match(r"\s*(Très bon|Parfait) en 256 Go SIM \+ eSIM : (.*)", ligne)
        if m:
            version = "actuelle"
            etat = "Très bon état" if m.group(1) == "Très bon" else "Parfait état"
            for morceau in m.group(2).split(" (mis en avant")[0].split(" ; "):
                genre = ("vérifié" if "vérifié" in morceau else
                         "affiché" if "affiché" in morceau else None)
                if genre:  # « tout est au-dessus (…) » ou « aucune trouvée » : pas un prix de la config
                    for couleur, prix in re.findall(r"(noir|naturel|sable|blanc) " + PRIX + " €", morceau):
                        lignes.append((etat, couleur, en_euros(prix), genre))
            continue
        m = re.match(r"\s*Meilleur (Très bon|Parfait) en 256 Go SIM physique \+ eSIM : "
                     r"(?:5 )?" + PRIX + r" € \(Titane (\w+)\)", ligne)
        if m:
            version = version or "ancienne"
            etat = "Très bon état" if m.group(1) == "Très bon" else "Parfait état"
            lignes.append((etat, m.group(3), en_euros(m.group(2)), "vérifié"))
    if lignes and version is None:
        version = "ancienne"
    return version, [l for l in lignes if 100 <= l[2] <= 3000]


# --- API GitHub (reconstitution) --------------------------------------------------

class SansRedirection(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # on suit la redirection nous-mêmes, sans y joindre le jeton


def api_github(chemin):
    requete = urllib.request.Request(GITHUB_API + chemin, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(requete, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def telecharger_logs(depot, run_id):
    """Archive zip des logs d'un passage (None si expirés ou indisponibles)."""
    requete = urllib.request.Request(f"{GITHUB_API}/repos/{depot}/actions/runs/{run_id}/logs", headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.build_opener(SansRedirection).open(requete, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
            # Lien signé de stockage : surtout pas le jeton GitHub avec (il serait refusé)
            with urllib.request.urlopen(e.headers["Location"], timeout=120) as r:
                return r.read()
        if e.code in (404, 410):
            return None
        raise


def texte_du_zip(contenu):
    """Le log de l'étape de vérification (un seul fichier, pour ne pas compter deux fois)."""
    with zipfile.ZipFile(io.BytesIO(contenu)) as z:
        candidats = []
        for nom in z.namelist():
            if nom.endswith(".txt"):
                texte = z.read(nom).decode("utf-8", "replace")
                if "Veille : iPhone 16 Pro" in texte:
                    candidats.append((len(texte), texte))
        return min(candidats)[1] if candidats else ""


def reconstituer():
    depot = os.environ.get("GITHUB_REPOSITORY")
    if not (depot and GITHUB_TOKEN):
        print("Reconstitution impossible hors de GitHub Actions (dépôt ou jeton absent).")
        return 0
    fichier = (os.environ.get("GITHUB_WORKFLOW_REF") or "").split("@")[0].rsplit("/", 1)[-1] \
        or "veille-backmarket.yml"
    existantes = charger_csv()
    deja = {l["run_id"] for l in existantes}
    passages, page = [], 1
    while page <= 50:  # 100 passages par page, 90 jours de logs au maximum
        lot = api_github(f"/repos/{depot}/actions/workflows/{fichier}/runs?per_page=100&page={page}")
        lot = lot.get("workflow_runs") or []
        passages += lot
        if len(lot) < 100:
            break
        page += 1
    courant = os.environ.get("GITHUB_RUN_ID")
    a_lire = [p for p in passages if p.get("status") == "completed"
              and str(p["id"]) not in deja and str(p["id"]) != courant]
    print(f"Reconstitution : {len(passages)} passages trouvés, {len(a_lire)} à relire.")
    nouvelles, relus, versions = [], 0, {}
    for p in a_lire:
        try:
            contenu = telecharger_logs(depot, p["id"])
        except Exception as e:
            print(f"  passage {p['id']} : logs illisibles ({str(e)[:80]})")
            continue
        if not contenu:
            continue
        version, lignes = lire_log(texte_du_zip(contenu))
        if not lignes:
            continue
        debut = (p.get("run_started_at") or p.get("created_at") or "").replace("Z", "+00:00")
        quand = paris(datetime.fromisoformat(debut)).isoformat(timespec="minutes")
        for etat, couleur, prix, genre in lignes:
            nouvelles.append({"horodatage": quand, "run_id": str(p["id"]), "etat": etat,
                              "couleur": couleur, "prix": f"{prix:.2f}", "type": genre,
                              "version": version})
        relus += 1
        versions[version] = versions.get(version, 0) + 1
    total = ecrire_csv(existantes + nouvelles)
    print(f"Reconstitution : {relus} passage(s) relus {versions}, {len(nouvelles)} prix ajoutés, "
          f"{total} lignes au total.")
    return relus


# --- Graphique -------------------------------------------------------------------------

def series(lignes):
    """Pour chaque état : [(moment, prix le plus bas du passage, version)] trié par date."""
    par_passage = {}
    for l in lignes:
        if l["etat"] in ETATS and l["type"] in ("vérifié", "affiché"):
            cle = (l["horodatage"], l["run_id"], l["etat"])
            prix = float(l["prix"])
            if cle not in par_passage or prix < par_passage[cle][0]:
                par_passage[cle] = (prix, l["version"])
    resultat = {etat: [] for etat in ETATS}
    for (horodatage, _, etat), (prix, version) in par_passage.items():
        resultat[etat].append((datetime.fromisoformat(horodatage), prix, version))
    return {etat: sorted(points) for etat, points in resultat.items()}


def dessiner(lignes):
    points = series(lignes)
    if not any(points.values()):
        print("Graphique : pas encore de prix à tracer.")
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.size": 12})
    fig, ax = plt.subplots(figsize=(10, 6), dpi=150)
    fuseau = next(iter(p for s in points.values() for p in s))[0].tzinfo
    derniers = {}
    for etat, (nom, teinte) in ETATS.items():
        serie = points[etat]
        if not serie:
            continue
        dates, prix = [p[0] for p in serie], [p[1] for p in serie]
        ax.plot(dates, prix, "-", color=teinte, lw=2 if len(serie) < 400 else 1.4, label=f"{nom} état")
        recents = [(d, v) for d, v, ver in serie if ver == "actuelle"]
        anciens = [(d, v) for d, v, ver in serie if ver != "actuelle"]
        taille = 4 if len(serie) < 150 else 2 if len(serie) < 400 else 0  # lisible même après des semaines
        if recents and taille:
            ax.plot(*zip(*recents), "o", color=teinte, ms=taille)
        if anciens:
            ax.plot(*zip(*anciens), "o", color=teinte, ms=max(taille, 2) + 1, mfc="white")
        derniers[nom] = prix[-1]
        ax.annotate(euros(prix[-1]), (dates[-1], prix[-1]), xytext=(6, 0),
                    textcoords="offset points", va="center", color=teinte, fontweight="bold")
    ax.axhline(PRIX_MAX, ls="--", color="#cf222e", lw=1.5, label=f"Votre plafond : {euros(PRIX_MAX)}")
    if any(v != "actuelle" for s in points.values() for _, _, v in s):
        ax.plot([], [], "o", color="gray", mfc="white", label="Points creux : ancienne version, moins fiable")
    ax.set_title("iPhone 16 Pro 256 Go SIM physique + eSIM\nprix le plus bas relevé à chaque passage",
                 fontsize=14)
    ax.set_ylabel("Prix (€)")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f} €"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m\n%Hh", tz=fuseau))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8, tz=fuseau))
    tous = [p[1] for s in points.values() for p in s] + [PRIX_MAX]
    marge = max(15, (max(tous) - min(tous)) * 0.12)
    ax.set_ylim(min(tous) - marge, max(tous) + marge)
    ax.grid(alpha=0.3)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=2, fontsize=10, frameon=False)
    fig.tight_layout()
    fig.savefig(FICHIER_PNG)
    plt.close(fig)
    nb = len({(l["run_id"]) for l in lignes if l["type"] in ("vérifié", "affiché")})
    print(f"Graphique : {FICHIER_PNG} ({nb} passages)")
    return {"derniers": derniers, "passages": nb,
            "debut": min(p[0] for s in points.values() for p in s),
            "fin": max(p[0] for s in points.values() for p in s)}


def resume_texte(infos):
    prix = " · ".join(f"{nom} {euros(v)}" for nom, v in infos["derniers"].items())
    return (f"{infos['passages']} passages du {infos['debut']:%d/%m %Hh%M} au {infos['fin']:%d/%m %Hh%M}. "
            f"Derniers prix : {prix} (plafond {euros(PRIX_MAX)}).")


def maj_readme(infos):
    bloc = ("<!-- veille:debut -->\n## Évolution du prix\n\n![Évolution du prix](historique.png)\n\n"
            f"{resume_texte(infos)} Données complètes : [historique.csv](historique.csv).\n"
            "<!-- veille:fin -->")
    texte = FICHIER_README.read_text(encoding="utf-8") if FICHIER_README.exists() else "# Veille Back Market\n"
    if "<!-- veille:debut -->" in texte and "<!-- veille:fin -->" in texte:
        texte = re.sub(r"<!-- veille:debut -->.*?<!-- veille:fin -->", lambda _: bloc, texte, flags=re.S)
    else:
        texte = texte.rstrip() + "\n\n" + bloc + "\n"
    FICHIER_README.write_text(texte, encoding="utf-8")


def envoyer_photo(chat, legende):
    frontiere = f"----veille{int(time.time() * 1000)}"
    corps = io.BytesIO()
    for nom, valeur in (("chat_id", str(chat)), ("caption", legende[:1024]), ("disable_notification", "true")):
        corps.write(f'--{frontiere}\r\nContent-Disposition: form-data; name="{nom}"\r\n\r\n{valeur}\r\n'.encode())
    corps.write(f'--{frontiere}\r\nContent-Disposition: form-data; name="photo"; filename="historique.png"\r\n'
                f"Content-Type: image/png\r\n\r\n".encode())
    corps.write(FICHIER_PNG.read_bytes())
    corps.write(f"\r\n--{frontiere}--\r\n".encode())
    requete = urllib.request.Request(f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/sendPhoto", data=corps.getvalue(),
                                     headers={"Content-Type": f"multipart/form-data; boundary={frontiere}"})
    try:
        with urllib.request.urlopen(requete, timeout=60) as r:
            ok = json.loads(r.read().decode("utf-8")).get("ok")
    except urllib.error.HTTPError as e:
        print(f"  Telegram (graphique) a répondu {e.code} : {e.read().decode('utf-8', 'replace')[:150]}")
        return False
    except Exception as e:
        print(f"  Telegram (graphique) injoignable : {e}")
        return False
    print("  Graphique envoyé sur Telegram." if ok else "  Graphique refusé par Telegram.")
    return bool(ok)


def publier(lignes, chat):
    infos = dessiner(lignes)
    if not infos:
        return
    maj_readme(infos)
    if TELEGRAM_TOKEN and chat:
        envoyer_photo(chat, "Évolution du prix — " + resume_texte(infos))


# --- Commandes ---------------------------------------------------------------------------

def chat_telegram(releve):
    if releve.get("telegram_chat_id"):
        return releve["telegram_chat_id"]
    if reglage("TELEGRAM_CHAT_ID"):
        return reglage("TELEGRAM_CHAT_ID")
    try:
        return json.loads(FICHIER_MEMOIRE.read_text(encoding="utf-8")).get("telegram_chat_id")
    except Exception:
        return None


def ajouter():
    try:
        releve = json.loads(FICHIER_RELEVE.read_text(encoding="utf-8"))
    except Exception:
        releve = {}
        print("Aucun relevé pour ce passage (vérification interrompue).")
    lignes = charger_csv()
    run_id = str(releve.get("run_id") or releve.get("horodatage") or "")
    nouvelles = [{"horodatage": releve["horodatage"], "run_id": run_id, "etat": l["etat"],
                  "couleur": l["couleur"], "prix": f"{float(l['prix']):.2f}", "type": l["type"],
                  "version": "actuelle"} for l in releve.get("lignes", [])]
    total = ecrire_csv(lignes + nouvelles)
    print(f"Historique : {len(nouvelles)} prix ajoutés, {total} lignes au total.")
    if releve.get("point_du_jour") or releve.get("test") or reglage("RECONSTITUER").lower() == "true":
        publier(charger_csv(), chat_telegram(releve))


def main():
    commande = sys.argv[1] if len(sys.argv) > 1 else "ajouter"
    if commande == "reconstituer":
        reconstituer()
    elif commande == "dessiner":
        publier(charger_csv(), chat_telegram({}))
    else:
        ajouter()


if __name__ == "__main__":
    main()
