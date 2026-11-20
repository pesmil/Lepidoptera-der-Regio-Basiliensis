#!/usr/bin/env python3
"""
Historische Nachweise (Excel) für die Website aufbereiten.

Aufruf (im Ordner der Website):
    pip install pandas openpyxl
    python historische_daten.py Nachweise.xlsx --perimeter MLRB_Perimeter_Boundaries.kml

Ergebnis:
    daten/historisch/index.json        Übersicht: Arten, Anzahl, Jahressummen
    daten/historisch/arten/<id>.json   Nachweise pro Art (id = iNaturalist-Taxon-ID)
    nicht_zugeordnet.csv               Namen, die keinem iNaturalist-Taxon zugeordnet werden konnten

Die Artnamen werden über die iNaturalist-API einer Taxon-ID zugeordnet (braucht Internet,
Ergebnis wird in taxa_cache.json gespeichert). Nicht gefundene Namen in SYNONYME eintragen
und das Skript erneut laufen lassen.
"""
import argparse, csv, json, math, re, sys, time, urllib.parse, urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 1) SPALTEN – None = automatisch erkennen. Sonst exakten Spaltennamen eintragen.
# ---------------------------------------------------------------------------
SPALTEN = {
    "art":    None,   # wissenschaftlicher Name, z. B. "Art", "Taxon", "Species"
    "datum":  None,   # vollständiges Datum ODER die drei folgenden Spalten
    "jahr":   None,
    "monat":  None,
    "tag":    None,
    "x":      None,   # Koordinate Ost (LV95, LV03) oder Längengrad (WGS84)
    "y":      None,   # Koordinate Nord (LV95, LV03) oder Breitengrad (WGS84)
    "stadium": None,  # optional: Imago/Falter, Raupe, Ei, Puppe …
    "ort":    None,   # optional: Fundort
}
KANDIDATEN = {
    "art":    ["art", "taxon", "species", "wiss. name", "wissenschaftlicher name", "name", "artname", "scientific name"],
    "datum":  ["datum", "date", "funddatum", "beobachtungsdatum", "eventdate"],
    "jahr":   ["jahr", "year", "j"],
    "monat":  ["monat", "month", "m"],
    "tag":    ["tag", "day", "t"],
    "x":      ["x", "koord_x", "koordx", "e", "east", "ost", "lv95_e", "lv03_x", "lon", "lng", "longitude", "länge", "laenge"],
    "y":      ["y", "koord_y", "koordy", "n", "north", "nord", "lv95_n", "lv03_y", "lat", "latitude", "breite"],
    "stadium":["stadium", "stage", "life stage", "entwicklungsstadium"],
    "ort":    ["ort", "fundort", "lokalität", "lokalitaet", "locality", "gemeinde", "place"],
}

# ---------------------------------------------------------------------------
# 2) SYNONYME – alte Namen auf aktuelle Namen (oder direkt auf eine iNat-Taxon-ID) abbilden
# ---------------------------------------------------------------------------
SYNONYME = {
    # "Scotia segetum": "Agrotis segetum",
    # "Eupithecia goossensiata": 1234567,
}

STADIEN = {  # 1 = Falter, 2 = frühe Stadien, 0 = nicht erfasst
    "imago": 1, "falter": 1, "adult": 1, "ad": 1, "i": 1, "f": 1,
    "raupe": 2, "larve": 2, "larva": 2, "r": 2, "l": 2, "ei": 2, "eier": 2, "egg": 2,
    "puppe": 2, "pupa": 2, "p": 2, "kokon": 2,
    "mine": 2, "blattmine": 2, "gespinst": 2, "sack": 2, "web": 2, "case": 2,  # Entwicklungsspuren (v. a. Kleinschmetterlinge)
}
RUNDUNG_M = 100        # Koordinaten auf 100 m runden (Dateigrösse, Datenschutz)
API = "https://api.inaturalist.org/v1"


def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def finde_spalten(cols):
    m, low = {}, {norm(c): c for c in cols}
    for k, v in SPALTEN.items():
        if v:
            if v not in cols:
                sys.exit(f"Spalte «{v}» für {k} nicht gefunden. Vorhanden: {list(cols)}")
            m[k] = v
        else:
            m[k] = next((low[c] for c in KANDIDATEN[k] if c in low), None)
    return m


# --- Koordinaten -----------------------------------------------------------
def wgs_to_lv95(lat, lon):
    p, l = (lat * 3600 - 169028.66) / 1e4, (lon * 3600 - 26782.5) / 1e4
    E = 2600072.37 + 211455.93*l - 10938.51*l*p - 0.36*l*p*p - 44.54*l**3
    N = 1200147.07 + 308807.95*p + 3745.25*l*l + 76.63*p*p - 194.56*l*l*p + 119.79*p**3
    return E, N


def koord(x, y):
    """Gibt (E, N) in LV95 zurück, erkennt LV95, LV03 und WGS84 automatisch."""
    try:
        x, y = float(str(x).replace("'", "").replace(",", ".")), float(str(y).replace("'", "").replace(",", "."))
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isnan(y):
        return None
    if 2_400_000 < x < 2_900_000 and 1_000_000 < y < 1_400_000:
        E, N = x, y
    elif 400_000 < x < 900_000 and 0 < y < 400_000:
        E, N = x + 2_000_000, y + 1_000_000
    elif 400_000 < y < 900_000 and 0 < x < 400_000:          # vertauscht
        E, N = y + 2_000_000, x + 1_000_000
    elif 5 < x < 11 and 45 < y < 49:
        E, N = wgs_to_lv95(y, x)
    elif 5 < y < 11 and 45 < x < 49:
        E, N = wgs_to_lv95(x, y)
    else:
        return None
    r = RUNDUNG_M
    return int(round(E / r) * r), int(round(N / r) * r)


def punkt_in_polygon(px, py, poly):
    inside, j = False, len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]; xj, yj = poly[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def lade_perimeter(pfad):
    txt = Path(pfad).read_text(encoding="utf-8")
    polys = []
    for c in re.findall(r"<coordinates>(.*?)</coordinates>", txt, re.S):
        pts = [tuple(map(float, p.split(",")[:2])) for p in c.split()]
        polys.append([wgs_to_lv95(lat, lon) for lon, lat in pts])
    return polys


# --- Datum -----------------------------------------------------------------
def datum(row, sp):
    y = m = d = 0
    v = row.get(sp["datum"]) if sp["datum"] else None
    if v is not None and str(v).strip() not in ("", "nan", "NaT"):
        if isinstance(v, (datetime, date)):
            return v.year, v.month, v.day
        s = str(v).strip()
        for pat in (r"^(\d{1,2})\.(\d{1,2})\.(\d{4})", r"^(\d{4})-(\d{1,2})-(\d{1,2})", r"^(\d{1,2})\.(\d{4})$", r"^(\d{4})$"):
            mm = re.match(pat, s)
            if mm:
                g = [int(x) for x in mm.groups()]
                if len(g) == 3:
                    return (g[2], g[1], g[0]) if pat.startswith(r"^(\d{1,2})\.") else (g[0], g[1], g[2])
                if len(g) == 2:
                    return g[1], g[0], 0
                return g[0], 0, 0
    def zahl(k):
        try:
            return int(float(row.get(sp[k]))) if sp[k] else 0
        except (TypeError, ValueError):
            return 0
    y, m, d = zahl("jahr"), zahl("monat"), zahl("tag")
    return y, (m if 1 <= m <= 12 else 0), (d if 1 <= d <= 31 else 0)


def stadium(v):
    s = norm(v)
    if not s or s == "nan":
        return 0
    for k, code in STADIEN.items():
        if s == k or s.startswith(k):
            return code
    return 0


# --- iNaturalist-Taxa --------------------------------------------------------
def api(path, params):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "regio-basiliensis-falter/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def taxon(name, cache, offline):
    key = name
    ziel = SYNONYME.get(name, name)
    if key in cache:
        return cache[key]
    if offline:
        return None
    try:
        if isinstance(ziel, int):
            res = api(f"/taxa/{ziel}", {"locale": "de"})["results"]
        else:
            res = api("/taxa", {"q": ziel, "per_page": 10, "locale": "de", "is_active": "true"})["results"]
        time.sleep(1.0)  # iNaturalist bittet um max. ~60 Anfragen pro Minute
    except Exception as e:
        print(f"  ! Fehler bei {name}: {e}")
        return None
    best = None
    for t in res:
        if isinstance(ziel, int) or norm(t["name"]) == norm(ziel) or norm(t.get("matched_term")) == norm(ziel):
            if t["rank"] in ("species", "subspecies", "complex", "hybrid"):
                best = t
                break
    if best and best["rank"] == "subspecies" and best.get("parent_id"):
        # Unterarten auf die Art zusammenführen
        try:
            best = api(f"/taxa/{best['parent_id']}", {"locale": "de"})["results"][0]
            time.sleep(1.0)
        except Exception:
            pass
    cache[key] = None if not best else {
        "id": best["id"], "name": best["name"], "de": best.get("preferred_common_name") or "",
        "anc": best.get("ancestor_ids") or [],
        "photo": (best.get("default_photo") or {}).get("medium_url"),
        "lic": (best.get("default_photo") or {}).get("license_code"),   # Foto wird auf der Website nur mit CC-Lizenz gezeigt
        "credit": re.sub(r",?\s*uploaded by.*$", "", (best.get("default_photo") or {}).get("attribution") or "", flags=re.I).strip(),
    }
    return cache[key]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("excel", help="Excel-Datei mit den historischen Nachweisen")
    ap.add_argument("--blatt", default=0, help="Tabellenblatt (Name oder Nummer, Standard: erstes)")
    ap.add_argument("--perimeter", help="KML mit dem Projektperimeter; Nachweise ausserhalb werden weggelassen")
    ap.add_argument("--ziel", default="daten/historisch", help="Ausgabeordner")
    ap.add_argument("--offline", action="store_true", help="nur taxa_cache.json verwenden, keine API-Anfragen")
    a = ap.parse_args()

    import pandas as pd
    blatt = int(a.blatt) if str(a.blatt).isdigit() else a.blatt
    df = pd.read_excel(a.excel, sheet_name=blatt)
    sp = finde_spalten(list(df.columns))
    print("Erkannte Spalten:", {k: v for k, v in sp.items() if v})
    if not sp["art"]:
        sys.exit("Keine Artspalte gefunden – bitte SPALTEN['art'] im Skript setzen.")
    if not (sp["datum"] or sp["jahr"]):
        sys.exit("Keine Datums- oder Jahresspalte gefunden – bitte SPALTEN im Skript setzen.")

    perim = lade_perimeter(a.perimeter) if a.perimeter else None
    cache_pfad = Path("taxa_cache.json")
    cache = json.loads(cache_pfad.read_text(encoding="utf-8")) if cache_pfad.exists() else {}

    namen = Counter(str(n).strip() for n in df[sp["art"]] if str(n).strip() not in ("", "nan"))
    print(f"{len(df)} Zeilen, {len(namen)} verschiedene Namen. Ordne Namen iNaturalist-Taxa zu …")
    for i, n in enumerate(sorted(namen), 1):
        if n not in cache and not a.offline:
            print(f"  [{i}/{len(namen)}] {n}")
        taxon(n, cache, a.offline)
        if i % 25 == 0:
            cache_pfad.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    cache_pfad.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    arten = defaultdict(list)
    jahre = Counter()
    stat = Counter()
    for row in df.to_dict("records"):
        n = str(row.get(sp["art"], "")).strip()
        t = cache.get(n)
        if not t:
            stat["ohne Taxon"] += 1
            continue
        y, m, d = datum(row, sp)
        E_N = koord(row.get(sp["x"]), row.get(sp["y"])) if sp["x"] and sp["y"] else None
        if perim and E_N and not any(punkt_in_polygon(E_N[0], E_N[1], p) for p in perim):
            stat["ausserhalb Perimeter"] += 1
            continue
        if not E_N:
            stat["ohne Koordinaten (nur in Diagrammen)"] += 1
        if not y:
            stat["ohne Jahr"] += 1
        ort = str(row.get(sp["ort"]) or "").strip() if sp["ort"] else ""
        rec = [y, m, d, E_N[0] if E_N else 0, E_N[1] if E_N else 0, stadium(row.get(sp["stadium"])) if sp["stadium"] else 0]
        if ort and ort != "nan":
            rec.append(ort)
        arten[t["id"]].append(rec)
        if y:
            jahre[y] += 1
        stat["übernommen"] += 1

    ziel = Path(a.ziel)
    (ziel / "arten").mkdir(parents=True, exist_ok=True)
    for f in (ziel / "arten").glob("*.json"):
        f.unlink()
    index = {"generated": date.today().isoformat(), "total": stat["übernommen"],
             "years": {str(k): v for k, v in sorted(jahre.items())}, "species": {}}
    tax_by_id = {v["id"]: v for v in cache.values() if v}
    for tid, recs in arten.items():
        recs.sort(key=lambda r: (r[0], r[1], r[2]))
        t = tax_by_id[tid]
        ds = [f"{r[0]:04d}" + (f"-{r[1]:02d}" if r[1] else "") + (f"-{r[2]:02d}" if r[1] and r[2] else "") for r in recs if r[0]]
        index["species"][str(tid)] = {"name": t["name"], "de": t["de"], "n": len(recs), "anc": t["anc"],
                                      "photo": t["photo"], "lic": t.get("lic"), "credit": t.get("credit", ""), "first": ds[0] if ds else None, "last": ds[-1] if ds else None}
        (ziel / "arten" / f"{tid}.json").write_text(json.dumps({"r": recs}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (ziel / "index.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    fehlend = [(n, c) for n, c in namen.items() if not cache.get(n)]
    with open("nicht_zugeordnet.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Name", "Anzahl Nachweise"])
        w.writerows(sorted(fehlend, key=lambda x: -x[1]))

    print("\nFertig.")
    for k, v in stat.items():
        print(f"  {k}: {v}")
    print(f"  Arten: {len(arten)}  →  {ziel}/")
    if fehlend:
        print(f"  {len(fehlend)} Namen ohne Zuordnung → nicht_zugeordnet.csv (in SYNONYME eintragen)")


if __name__ == "__main__":
    main()
