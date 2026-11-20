#!/usr/bin/env python3
"""
iNaturalist-Daten für die Website vorberechnen (läuft nächtlich als GitHub Action, geht aber auch lokal).

Aufruf (im Ordner der Website, nur Python-Standardbibliothek nötig):
    python inat_daten.py --site-url https://benutzer.github.io/repo/

Ergebnis:
    daten/inat/uebersicht.json        Artenliste, Familien, Kennzahlen, Jahressummen, «Was fliegt?» pro Halbmonat
    daten/inat/arten/<id>.json        alle Nachweise einer Art, Fotos (nur mit CC-Lizenz), neueste Beobachtungen
    art/<name>/index.html             eine statische Seite pro Art (für Google, Teilen, Lesezeichen)
    sitemap.xml, robots.txt           für Suchmaschinen (nur mit --site-url)

Fehlen diese Dateien, lädt die Website alles live über die iNaturalist-API (langsamer, aber funktionsfähig).
"""
import argparse, html, json, re, shutil, sys, time, unicodedata, urllib.parse, urllib.request, urllib.error
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

# iNaturalist-Projekte (ID oder Kurzname); wie CONFIG.projects in index.html
PROJEKTE = "209545,microlepidoptera-of-the-regio-basiliensis"
LEPIDOPTERA = 47157
SP_RANKS = {"species", "complex", "subspecies", "hybrid"}
LIZENZEN = {"cc0", "cc-by", "cc-by-sa", "cc-by-nc", "cc-by-nc-sa", "cc-by-nd", "cc-by-nc-nd"}  # wie CONFIG.photoLicenses
MONATE = {"de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"],
          "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]}
SEITE = {"de": "Lepidoptera der Region Basiliensis", "en": "Lepidoptera of the Region Basiliensis"}
# Qualitätsstufen, die zählen (wie CONFIG.qualityGrades in index.html); "casual" = Gelegentlich ist ausgeschlossen
QUALITAET = "research,needs_id"
# Familien der Grossschmetterlinge (wie CONFIG.familyOrder in index.html); alle übrigen zählen als Kleinschmetterlinge
GROSS = {"Hepialidae", "Cossidae", "Zygaenidae", "Limacodidae", "Psychidae", "Sesiidae", "Lasiocampidae", "Endromidae",
         "Saturniidae", "Sphingidae", "Drepanidae", "Hesperiidae", "Papilionidae", "Pieridae", "Riodinidae", "Lycaenidae", "Nymphalidae",
         "Geometridae", "Notodontidae", "Erebidae", "Nolidae", "Noctuidae"}

API = "https://api.inaturalist.org/v1"
PAUSE = 1.0  # Sekunden zwischen Anfragen (iNat empfiehlt höchstens ~60 pro Minute)
_letzte = 0.0


def api(pfad, params=None, locale="de"):
    """GET mit Pause und bis zu fünf Wiederholungen bei 429/5xx oder Netzfehlern."""
    global _letzte
    url = f"{API}{pfad}?{urllib.parse.urlencode({'locale': locale, **(params or {})})}"
    for versuch in range(6):
        warte = PAUSE - (time.time() - _letzte)
        if warte > 0:
            time.sleep(warte)
        _letzte = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Regio-Basiliensis-Website/1.0 (GitHub Action)"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or versuch == 5:
                raise
            pause = int(e.headers.get("Retry-After") or 0) or 10 * (versuch + 1)
        except (urllib.error.URLError, TimeoutError):
            if versuch == 5:
                raise
            pause = 10 * (versuch + 1)
        print(f"  … Anfrage fehlgeschlagen, neuer Versuch in {pause} s", file=sys.stderr)
        time.sleep(pause)


def slug(name):
    """Wie slug() in index.html: «Catocala fraxini» -> «catocala-fraxini»."""
    s = unicodedata.normalize("NFD", str(name or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def credit(a):
    return re.sub(r",?\s*uploaded by.*$", "", str(a or ""), flags=re.I).strip()


def lizenz_ok(code):
    return bool(code) and str(code).lower() in LIZENZEN


def lade_familien():
    """Familien der Lepidoptera: {id: [wiss. Name, deutscher Name, englischer Name]}"""
    fam = {}
    for i, locale in enumerate(("de", "en")):
        seite = 1
        while True:
            d = api("/taxa", {"taxon_id": LEPIDOPTERA, "rank": "family", "per_page": 200, "page": seite, "is_active": "true"}, locale)
            for t in d["results"]:
                fam.setdefault(t["id"], [t["name"], "", ""])[i + 1] = t.get("preferred_common_name") or ""
            if seite * 200 >= d["total_results"]:
                break
            seite += 1
    return fam


def lade_beobachtungen(projekt):
    """Alle Beobachtungen des Projekts, seitenweise nach ID (stabil auch bei vielen tausend)."""
    alle, ab = [], 0
    while True:
        q = {"quality_grade": QUALITAET} if QUALITAET else {}
        d = api("/observations", {"project_id": projekt, **q, "per_page": 200, "order_by": "id", "order": "asc", "id_above": ab})
        res = d["results"]
        if not res:
            return alle
        alle.extend(res)
        ab = res[-1]["id"]
        print(f"  {len(alle)} von {d['total_results']} Beobachtungen", file=sys.stderr)


def lade_taxa(ids, locale="de"):
    """Standardfoto, Name und Vorfahren für viele Taxa (30 pro Anfrage)."""
    out, ids = {}, sorted(ids)
    for i in range(0, len(ids), 30):
        for t in api("/taxa/" + ",".join(map(str, ids[i:i + 30])), locale=locale)["results"]:
            out[t["id"]] = t
    return out


def stadium(o):
    ann = next((a for a in o.get("annotations") or [] if a.get("controlled_attribute_id") == 1), None)
    v = ann and ann.get("controlled_value_id")
    return 1 if v == 2 else 2 if v in (4, 5, 6, 7, 8) else 0


def ymd(o):
    try:
        y, m, d = (int(x) for x in (o.get("observed_on") or "").split("-"))
        return y, m, d
    except ValueError:
        return 0, 0, 0


def foto(ph):
    """Foto-Objekt einer Beobachtung oder eines Taxons -> {u, c, l} oder None, wenn die Lizenz nicht passt."""
    if not ph or not lizenz_ok(ph.get("license_code")):
        return None
    u = ph.get("medium_url") or (ph.get("url") or "").replace("square", "medium")
    return {"u": u, "c": credit(ph.get("attribution")), "l": ph["license_code"]} if u else None


def schreibe_json(pfad, daten):
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(json.dumps(daten, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def berechne(obs, familien, hist):
    taxa_info, n_exakt = {}, defaultdict(int)
    jahre, beobachter = defaultdict(int), set()
    jahre_g = {"macro": defaultdict(int), "micro": defaultdict(int)}  # Nenner der relativen Häufigkeit pro Gruppe
    perioden_total, perioden_art = defaultdict(int), defaultdict(lambda: defaultdict(int))
    for o in obs:
        y, m, d = ymd(o)
        if y:
            jahre[str(y)] += 1
            fid = next((a for a in (o.get("taxon") or {}).get("ancestor_ids") or [] if a in familien), None)
            if fid:
                jahre_g["macro" if familien[fid][0] in GROSS else "micro"][str(y)] += 1
        if o.get("user"):
            beobachter.add(o["user"]["id"])
        if m and d:
            perioden_total[f"{m}-{1 if d <= 15 else 2}"] += 1
        t = o.get("taxon")
        if t and t.get("rank") in SP_RANKS:
            taxa_info[t["id"]] = t
            n_exakt[t["id"]] += 1
            if m and d:
                perioden_art[f"{m}-{1 if d <= 15 else 2}"][t["id"]] += 1

    # Wie bei der Live-Abfrage mit taxon_id zählen Beobachtungen von Unterarten auch bei der Art
    pro_art = defaultdict(list)
    for o in obs:
        t = o.get("taxon")
        if not t:
            continue
        for tid in {t["id"], *(a for a in t.get("ancestor_ids") or [] if a in taxa_info)}:
            if tid in taxa_info:
                pro_art[tid].append(o)

    hist_ids = {int(k) for k in (hist or {}).get("species", {})}
    details = lade_taxa(set(taxa_info) | hist_ids)
    englisch = {k: v.get("preferred_common_name") or "" for k, v in lade_taxa(set(taxa_info) | hist_ids, "en").items()}

    arten, dateien = [], {}
    for tid in sorted(set(taxa_info) | hist_ids):
        t = details.get(tid) or taxa_info.get(tid)
        if not t:
            continue
        fid = next((a for a in t.get("ancestor_ids") or [] if a in familien), None)
        eintrag = {"id": tid, "name": t["name"], "de": t.get("preferred_common_name") or "", "en": englisch.get(tid, ""), "fam": fid, "n": n_exakt.get(tid, 0)}
        ph = foto(t.get("default_photo"))
        if ph:
            eintrag["ph"] = ph
        arten.append(eintrag)
        if tid not in pro_art:
            continue
        eintrag["f"] = 1  # Datei arten/<id>.json vorhanden
        eintrag["q"] = sum(1 for o in pro_art[tid] if o.get("quality_grade") == "research")  # davon Forschungsqualität
        liste = sorted(pro_art[tid], key=lambda o: (o.get("observed_on") or "", o["id"]), reverse=True)
        recs, aj = [], defaultdict(int)
        for o in liste:
            y, m, d = ymd(o)
            c = (o.get("geojson") or {}).get("coordinates") or [None, None]
            lat = round(c[1], 5) if c[1] is not None else None
            lon = round(c[0], 5) if c[0] is not None else None
            recs.append([y, m, d, lat, lon, stadium(o), o.get("place_guess") or "", o["id"], 1 if o.get("quality_grade") == "research" else 0])
            if y:
                aj[str(y)] += 1
        fotos = []
        for o in liste:
            ph = foto((o.get("photos") or [None])[0])
            if ph:
                fotos.append({"u": ph["u"], "c": ph["c"], "l": ph["l"], "o": o["id"], "d": o.get("observed_on") or "",
                              "n": (o.get("user") or {}).get("login", "")})
            if len(fotos) >= 24:
                break
        tabelle = [{"d": o.get("observed_on") or "", "p": o.get("place_guess") or "", "n": (o.get("user") or {}).get("login", ""),
                    "q": o.get("quality_grade") or "", "o": o["id"]} for o in liste[:20]]
        dateien[tid] = {"r": recs, "y": dict(aj), "f": fotos, "t": tabelle}

    # Neueste Beobachtungen (Ersatz, falls die Live-Abfrage auf der Startseite scheitert)
    neu = []
    for o in sorted(obs, key=lambda o: o.get("created_at") or "", reverse=True):
        t = o.get("taxon")
        if not t or t.get("rank") not in SP_RANKS:
            continue
        ph = (o.get("photos") or [None])[0]
        neu.append({"taxon": {**{k: t.get(k) for k in ("id", "name", "rank", "preferred_common_name")}, "en": englisch.get(t["id"], "")},
                    "photos": [{"url": ph.get("url"), "license_code": ph.get("license_code")}] if ph else [],
                    "user": {"login": (o.get("user") or {}).get("login", "")}, "observed_on": o.get("observed_on")})
        if len(neu) >= 12:
            break

    uebersicht = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(obs), "observers": len(beobachter), "years": dict(sorted(jahre.items())),
        "years_g": {k: dict(sorted(v.items())) for k, v in jahre_g.items()},
        "families": {str(k): v for k, v in familien.items() if any(a.get("fam") == k for a in arten)},
        "species": arten,
        "periods": {k: {"t": perioden_total[k], "s": sorted(([tid, n] for tid, n in perioden_art[k].items()), key=lambda x: -x[1])}
                    for k in perioden_total},
        "recent": neu,
    }
    return uebersicht, dateien


# ---------------------------------------------------------------------------
# Statische Artseiten
# ---------------------------------------------------------------------------
def ersetze(vorlage, muster, neu):
    s, n = re.subn(muster, lambda m: neu, vorlage, count=1, flags=re.S)
    if not n:
        raise SystemExit(f"Muster nicht in index.html gefunden: {muster}")
    return s


def meta_kopf(s, titel, beschreibung, url, bild, lang="de", alternativen=None):
    """Titel, Beschreibung, Open Graph, kanonische Adresse und Sprachversionen (hreflang) setzen."""
    e = lambda x: html.escape(x, quote=True)
    s = ersetze(s, r'<html lang="[^"]*">', f'<html lang="{lang}">')
    s = ersetze(s, r"<title>.*?</title>", f"<title>{e(titel)}</title>")
    s = ersetze(s, r'<meta name="description" content="[^"]*">', f'<meta name="description" content="{e(beschreibung)}">')
    s = ersetze(s, r'<meta property="og:title" content="[^"]*">', f'<meta property="og:title" content="{e(titel)}">')
    s = ersetze(s, r'<meta property="og:description" content="[^"]*">', f'<meta property="og:description" content="{e(beschreibung)}">')
    s = ersetze(s, r'<meta property="og:image" content="[^"]*">', f'<meta property="og:image" content="{e(bild)}">')
    s = ersetze(s, r'<meta property="og:locale" content="[^"]*">', f'<meta property="og:locale" content="{"en_GB" if lang == "en" else "de_CH"}">')
    s = re.sub(r'<meta property="og:locale:alternate" content="[^"]*">', f'<meta property="og:locale:alternate" content="{"de_CH" if lang == "en" else "en_GB"}">', s, count=1)
    s = re.sub(r'\s*<meta property="og:url"[^>]*>|\s*<link rel="canonical"[^>]*>|\s*<link rel="alternate" hreflang[^>]*>', "", s)
    if url:
        extra = f'<link rel="canonical" href="{e(url)}">\n<meta property="og:url" content="{e(url)}">\n'
        for hl, u in (alternativen or {}).items():
            extra += f'<link rel="alternate" hreflang="{hl}" href="{e(u)}">\n'
        s = s.replace('<meta property="og:type"', extra + '<meta property="og:type"', 1)
    return s


def mit_basis(s, tiefe, route, hero=True):
    """Erzeugte Seite in einem Unterordner: Basisadresse, Startroute und (ohne Banner) leichterer Kopf."""
    s = s.replace('<meta charset="utf-8">', f'<meta charset="utf-8">\n<base href="{"../" * tiefe}">', 1)
    s = ersetze(s, r"<body[^>]*>", f'<body data-route="{route}">')
    if not hero:
        s = ersetze(s, r'<div class="hero" id="band">\s*<img id="band-img"[^>]*>', '<div class="hero" id="band" hidden>\n  <img id="band-img" alt="" hidden>')
    return s


def datum(s, lang="de"):
    """2026-10-03 -> 3.10.2026 bzw. 3/10/2026, 1990-09 -> September 1990, 1955 -> 1955"""
    t = [int(x) for x in str(s).split("-") if x]
    if len(t) == 3:
        return f"{t[2]}/{t[1]}/{t[0]}" if lang == "en" else f"{t[2]}.{t[1]}.{t[0]}"
    return f"{MONATE[lang][t[1] - 1]} {t[0]}" if len(t) == 2 else str(s)


def flugzeit_text(recs, lang="de"):
    monate = defaultdict(int)
    for r in recs:
        if r[1]:
            monate[r[1]] += 1
    if not monate:
        return ""
    M = MONATE[lang]
    gesamt, aktiv = sum(monate.values()), [m for m in range(1, 13) if monate[m]]
    haupt = max(monate, key=monate.get)
    if lang == "en":
        return f"Records from {M[aktiv[0] - 1]} to {M[aktiv[-1] - 1]}, most in {M[haupt - 1]} ({monate[haupt]} of {gesamt})."
    return f"Nachweise von {M[aktiv[0] - 1]} bis {M[aktiv[-1] - 1]}, die meisten im {M[haupt - 1]} ({monate[haupt]} von {gesamt})."


TEXT = {
    "de": dict(fam="Familie", nachw="Nachweise", total="Total", hist="davon historisch", erst="Erster Nachweis", letzt="Letzter Nachweis",
               flug="Flugzeit", laden="Karte und Diagramme werden geladen …",
               beschr="{name}: Verbreitungskarte, Flugzeit, Häufigkeit und Fotos aus der Region Basiliensis. {n} Nachweise."),
    "en": dict(fam="Family", nachw="Records", total="Total", hist="of which historical", erst="First record", letzt="Latest record",
               flug="Flight period", laden="Loading map and charts …",
               beschr="{name}: distribution map, flight period, frequency and photos from the Region Basiliensis. {n} records."),
}


def artseiten(wurzel, vorlage, uebersicht, dateien, hist, site_url):
    """Eine statische Seite pro Art und Sprache: art/<name>/ (deutsch) und en/art/<name>/ (englisch)."""
    familien = {int(k): v for k, v in uebersicht["families"].items()}
    for alt in (wurzel / "art", wurzel / "en" / "art"):
        if alt.exists():
            shutil.rmtree(alt)
    e = html.escape
    urls = []
    for a in uebersicht["species"]:
        h = (hist or {}).get("species", {}).get(str(a["id"])) or {}
        d = dateien.get(a["id"]) or {"r": []}
        n_inat, n_hist = len(d["r"]), h.get("n", 0)
        if not n_inat + n_hist:
            continue
        s = slug(a["name"])
        url = {"de": f"{site_url}art/{s}/", "en": f"{site_url}en/art/{s}/"} if site_url else {}
        alternativen = {"de": url["de"], "en": url["en"], "x-default": url["de"]} if site_url else None
        daten = sorted([f"{r[0]}-{r[1]:02d}-{r[2]:02d}" for r in d["r"] if r[0] and r[1] and r[2]] + [x for x in (h.get("first"), h.get("last")) if x])
        fam = familien.get(a["fam"])
        for lang in ("de", "en"):
            T = TEXT[lang]
            cn = (a["de"] or h.get("de") or "") if lang == "de" else a.get("en", "")
            name = f"{a['name']} ({cn})" if cn else a["name"]
            titel = f"{name} · {SEITE[lang]}"
            beschr = T["beschr"].format(name=name, n=n_inat + n_hist)
            bild = a.get("ph", {}).get("u", "").replace("medium", "large") or (f"{site_url}bilder/vorschau.jpg" if site_url else "bilder/vorschau.jpg")
            fam_cn = fam and (fam[2] if lang == "en" else fam[1])
            pfx = "#/en/" if lang == "en" else "#/"
            inhalt = f"""<div class="sp-title"><h1>{e(a['name'])}</h1>{f'<div class="cn">{e(cn)}</div>' if cn else ''}</div>
  <div class="sp-top"><div class="blocks">
    <div class="blk"><span class="label">{T['fam']}</span><div class="val">{f'<a href="{pfx}familie/{slug(fam[0])}">{e(fam[0])}</a>' + (f' · {e(fam_cn)}' if fam_cn else '') if fam else '–'}</div></div>
    <div class="blk"><span class="label">{T['nachw']}</span><dl class="facts"><dt>{T['total']}</dt><dd>{n_inat + n_hist}</dd>{f"<dt>{T['hist']}</dt><dd>{n_hist}</dd>" if n_hist else ''}<dt>{T['erst']}</dt><dd>{e(datum(daten[0], lang)) if daten else '–'}</dd><dt>{T['letzt']}</dt><dd>{e(datum(daten[-1], lang)) if daten else '–'}</dd></dl></div>
    <div class="blk"><span class="label">{T['flug']}</span><div class="val">{e(flugzeit_text(d['r'], lang) or '–')}</div></div>
  </div><div class="pic">{f'<img src="{e(a["ph"]["u"].replace("medium", "large"))}" alt="{e(a["name"])}"><div class="credit">{e(a["ph"]["c"])}</div>' if a.get('ph') else ''}</div></div>
  <div class="msg"><span class="loader"></span>{T['laden']}</div>"""
            seite = meta_kopf(vorlage, titel, beschr, url.get(lang, ""), bild, lang, alternativen)
            route = f"{'en/' if lang == 'en' else ''}art/{s}"
            seite = mit_basis(seite, 3 if lang == "en" else 2, route, hero=False)
            seite, n = re.subn(r'(<main class="wrap" id="app"[^>]*>).*?(</main>)', lambda m: m.group(1) + inhalt + m.group(2), seite, count=1, flags=re.S)
            if not n:
                raise SystemExit("<main id=\"app\"> nicht in index.html gefunden")
            p = wurzel / ("en" if lang == "en" else "") / "art" / s / "index.html"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(seite, encoding="utf-8")
        if site_url:
            urls.append((url["de"], url["en"]))
    return urls


def main():
    global API, PAUSE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site-url", default="", help="öffentliche Adresse der Website, z. B. https://benutzer.github.io/repo/")
    ap.add_argument("--projekt", default=PROJEKTE, help="iNat-Projekte, durch Komma getrennt (ID oder Kurzname)")
    ap.add_argument("--api", default=API, help="nur zum Testen ändern")
    ap.add_argument("--pause", type=float, default=PAUSE, help="Sekunden zwischen Anfragen")
    ap.add_argument("--ohne-artseiten", action="store_true", help="nur JSON-Daten erzeugen")
    a = ap.parse_args()
    API, PAUSE = a.api.rstrip("/"), a.pause
    site_url = a.site_url.rstrip("/") + "/" if a.site_url else ""
    wurzel = Path(__file__).resolve().parent

    hist_pfad = wurzel / "daten" / "historisch" / "index.json"
    hist = json.loads(hist_pfad.read_text(encoding="utf-8")) if hist_pfad.exists() else None

    print("Familien laden …", file=sys.stderr)
    familien = lade_familien()
    print("Beobachtungen laden …", file=sys.stderr)
    obs = lade_beobachtungen(a.projekt)
    print("Taxa laden und Daten berechnen …", file=sys.stderr)
    uebersicht, dateien = berechne(obs, familien, hist)

    # Erst in einen Nebenordner schreiben und dann austauschen, damit nie halbe Daten online sind
    ziel, neu = wurzel / "daten" / "inat", wurzel / "daten" / "inat.neu"
    if neu.exists():
        shutil.rmtree(neu)
    schreibe_json(neu / "uebersicht.json", uebersicht)
    for tid, d in dateien.items():
        schreibe_json(neu / "arten" / f"{tid}.json", d)
    if ziel.exists():
        shutil.rmtree(ziel)
    neu.rename(ziel)
    print(f"{len(obs)} Beobachtungen, {len(uebersicht['species'])} Taxa -> {ziel}", file=sys.stderr)

    if a.ohne_artseiten:
        return
    vorlage_pfad = wurzel / "index.html"
    vorlage = vorlage_pfad.read_text(encoding="utf-8")
    beschr = re.search(r'<meta name="description" content="([^"]*)">', vorlage).group(1)
    start = {"de": site_url, "en": f"{site_url}en/"} if site_url else {}
    alt_start = {"de": start["de"], "en": start["en"], "x-default": start["de"]} if site_url else None
    if site_url:  # Startseite: volle Adressen für Vorschaubild, kanonische Adresse und Sprachversionen
        neu_index = meta_kopf(vorlage, SEITE["de"], beschr, start["de"], site_url + "bilder/vorschau.jpg", "de", alt_start)
        if neu_index != vorlage:
            vorlage_pfad.write_text(neu_index, encoding="utf-8")
            vorlage = neu_index
    # Englische Startseite en/index.html (gleiche Seite, startet auf #/en/)
    en_start = meta_kopf(vorlage, SEITE["en"],
                         "All butterflies and moths of the Basel region: species accounts, distribution maps, flight periods and photos from iNaturalist and historical records.",
                         start.get("en", ""), (site_url or "") + "bilder/vorschau.jpg", "en", alt_start)
    (wurzel / "en").mkdir(exist_ok=True)
    (wurzel / "en" / "index.html").write_text(mit_basis(en_start, 1, "en/"), encoding="utf-8")
    urls = artseiten(wurzel, vorlage, uebersicht, dateien, hist, site_url)
    print(f"{len(urls) or len(list((wurzel / 'art').glob('*')))} Arten mit statischen Seiten (deutsch und englisch)", file=sys.stderr)
    if site_url:
        heute = date.today().isoformat()
        def eintrag(de, en):
            alt = f'<xhtml:link rel="alternate" hreflang="de" href="{html.escape(de)}"/><xhtml:link rel="alternate" hreflang="en" href="{html.escape(en)}"/>'
            return "".join(f"<url><loc>{html.escape(u)}</loc><lastmod>{heute}</lastmod>{alt}</url>\n" for u in (de, en))
        eintraege = eintrag(start["de"], start["en"]) + "".join(eintrag(de, en) for de, en in urls)
        (wurzel / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
            f"{eintraege}</urlset>\n", encoding="utf-8")
        (wurzel / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {site_url}sitemap.xml\n", encoding="utf-8")


if __name__ == "__main__":
    main()
