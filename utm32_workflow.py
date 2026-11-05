# -*- coding: utf-8 -*-
"""
UTM32-Harmonisierungs-Workflow für DGM1-Kacheln (ADV-Schema, ETRS89).

Ziel: Vollständiger, harmonisierter Datensatz in UTM32N (EPSG:25832).

Ablauf:
  1. Inventur      – alle DGM1-Kacheln aus Streifenordnern lesen.
  2. Umprojektion – UTM33-Kacheln per gdalwarp exakt auf das UTM32-1-km-
                    Kachelraster warpen (-te/-tr); UTM32-Kacheln bleiben
                    unberührt. An der Zonengrenze wird jede Quellkachel auf
                    JEDE überlappte Zielkachel gewarpt (Trapezüberlappung,
                    viele-zu-viel). Zielposition via gdaltransform.
  3. Klassifikation– Zielkacheln mit >= 2 Bundesländern (VG25) werden
                    Grenzkacheln. Bestimmung ohne Geometrie-Bibliothek:
                    je Bundesland wird die VG25 mit gdal_rasterize
                    (ALL_TOUCHED, 1-km-Raster) gerastert und das Ergebnis
                    als XYZ-Text (gdal_translate) ausgewertet.
  4. Harmonisierung– je Grenzkachel: Landesbeiträge mit der VG25-Landes-
                    maske clippen (gdalwarp -cutline -cwhere) und alle
                    Teile verschmelzen (gdalbuildvrt + gdal_translate).
  5. Ausgabe      – Mosaikierung in Streifenordner s32_<rw> mit Kacheln
                    dgm1_32_<rw>_<hw>_1.tif.

Abhängigkeiten: NUR Python-Standardbibliothek + GDAL-Binarys (gdalwarp,
gdalinfo, gdaltransform, gdal_translate, gdalbuildvrt, gdal_rasterize,
ogr2ogr, ogrinfo). Keine pip-Pakete, keine GDAL-Python-Bindings.

Aufruf:
    python utm32_workflow.py            (Konfiguration unten anpassen)
    python utm32_workflow.py --input ... --output ... --vg25 ...
"""

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

# ===========================================================================
# KONFIGURATION (bei Bedarf anpassen; CLI-Argumente überschreiben dies)
# ===========================================================================

GDAL_BIN_DIR = r"C:\Program Files\QGIS 3.40.8\bin"          # "" = GDAL im PATH
GDAL_DATA_DIR = r"C:\Program Files\QGIS 3.40.8\share\gdal"  # "" = nicht setzen

ROOT_INPUT_DIR = r"C:\daten\dgm1_streifen"      # Wurzel der Streifenordner
OUTPUT_DIR = r"C:\daten\dgm1_utm32_harmonisiert"  # Ziel (Streifen s32_*)
VG25_PATH = r"C:\daten\vg25.gpkg"   # VG25 (OGR-lesbar: GPKG/GeoJSON/SHP/GML)
VG25_LAYER = ""     # leer = Länderebene automatisch erkennen (z.B. "vg250_lan")
VG25_AGS_FIELD = "ags"                 # Spalte mit dem Bundesland-AGS (z.B. "ars")
TMP_ROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "utm32_workflow")

PROGRESS_MODE = "bar"        # "bar" (TTY) | "log" (Datei-Redirect)
KEEP_INTERMEDIATE = False    # Zwischendaten behalten (Debug)

NODATA = -99999.0
RES_M = 1.0                  # Zielauflösung (DGM1 = 1 m)
# Resampling-Methode für den UTM33->UTM32-Warp:
#   "near"    = Original-Höhenwerte bleiben erhalten (keine Glättung);
#               empfohlen für DGM, wenn Höhenwerte originalgetreu bleiben
#               sollen (bis ~0.7 m Lageversatz durch die Gitterdrehung)
#   "bilinear" = glättet (4-Nachbarn-Mischung), für kontinuierliche
#               Visualisierung ggf. schöner, aber Höhen verändert
#   "cubic"   = schärfer als bilinear, ebenfalls interpolierend
RESAMPLE = "near"
TILE_SIZE_M = 1000.0

# Fill-Algorithmus fuer kleine Fehlstellen (z.B. 1 fehlender Pixel auf
# der VG-Linie beim Cutline-Clipping oder Sub-Pixel-Spalten am Trapezrand
# der Zonengrenze). Nur zusammenhaengende NoData-Bereiche bis zu dieser
# Pixelzahl werden mit dem Mittel der gueltigen 8-Nachbarn gefuellt;
# groessere Datenluecken (Lieferluecken, Aussengrenzen) bleiben unberuehrt.
FILL_HOLES = True
FILL_MAX_HOLE_PX = 4

# ---------------------------------------------------------------------------
# Performance-Konfiguration (abgestimmt auf Vielkern-CPUs, z.B. Threadripper
# PRO 5955WX: 64 Kerne/128 Threads, 128 GB RAM). Der Workflow parallelisiert
# über viele unabhängige gdalwarp-Prozesse (jede 1-km-Kachel ist klein);
# daher wenige Threads PRO Prozess und ein moderater Cache je Prozess.
# Faustregel: WORKERS * GDAL_CACHEMAX_MB sollte unter ~50% des RAM bleiben.
# ---------------------------------------------------------------------------
WORKERS = max(1, min(48, (os.cpu_count() or 8) // 2))  # parallele GDAL-Prozesse
# Threadripper PRO 5955WX (64C/128T) -> WORKERS=32; für maximale Last
# kann WORKERS=48..64 gesetzt werden.
GDAL_WARP_THREADS = 2       # -wo NUM_THREADS je gdalwarp (kleine Kacheln!)
GDAL_COMPRESS_THREADS = 2   # -co NUM_THREADS (DEFLATE, GDAL >= 3.6)
GDAL_CACHEMAX_MB = 512      # Cache je GDAL-Prozess (48*512MB = 24 GB)

# Kompressions-Optionen: DEFLATE mit Floating-Point-Prädiktor (3) ist
# für DGM1 (float32-Höhen) 20-35 % kleiner als reines DEFLATE bei nur
# geringfügig mehr CPU-Last (auf Vielkern-CPUs praktisch gratis).
TIF_CREATE_OPTIONS = [
    "TILED=YES",
    "COMPRESS=DEFLATE",
    "PREDICTOR=3",
    "BIGTIFF=IF_SAFER",
]


def _tif_co():
    """GDAL-Creation-Optionen als -co-Argumentliste (+ Thread-Option)."""
    args = []
    for opt in TIF_CREATE_OPTIONS:
        args.extend(["-co", opt])
    args.extend(["-co", f"NUM_THREADS={GDAL_COMPRESS_THREADS}"])
    return args


# AdV-Länderkürzel (offizielle zweistellige Kürzel im Datei-Suffix:
# dgm1_33_372_5805_1_be_2025.tif -> "be"). Mapping Kürzel -> AGS:
ABBR_TO_AGS = {
    "bw": 8, "by": 9, "be": 11, "bb": 12, "hb": 4, "hh": 2,
    "he": 6, "mv": 13, "ni": 3, "nw": 5, "rp": 7, "sl": 10,
    "sn": 14, "st": 15, "sh": 1, "th": 16,
}

# Fallback, falls ein Bundesland ohne Kürzel-Suffix geliefert wird
# (AGS -> 32|33). Bei Kürzel-Suffixen wird die Zone automatisch aus dem
# Dateinamen abgeleitet; diese Tabelle dient nur als Rückfall.
STATE_ZONES = {
    1: 32, 2: 32, 3: 32, 4: 32, 5: 32, 6: 32, 7: 32, 8: 32,
    9: 33, 10: 32, 11: 33, 12: 33, 13: 33, 14: 33, 15: 33, 16: 33,
}

# ===========================================================================
# GDAL/OGR-Subprocess-Wrapper (alle Geodaten-Operationen laufen hierüber)
# ===========================================================================

_STATE = {"bin_dir": None}


def configure_gdal_env(gdal_bin_dir=None, gdal_data_dir=None):
    if gdal_bin_dir:
        _STATE["bin_dir"] = gdal_bin_dir
        os.environ["PATH"] = gdal_bin_dir + os.pathsep + os.environ.get("PATH", "")
    if gdal_data_dir:
        os.environ["GDAL_DATA"] = gdal_data_dir
    # Pro-Prozess-Limits: Der Workflow parallelisiert über WORKERS Prozesse,
    # daher bewusst kleine interne Thread-Pools je Prozess.
    os.environ["GDAL_CACHEMAX"] = f"{GDAL_CACHEMAX_MB}"
    os.environ["GDAL_NUM_THREADS"] = str(GDAL_WARP_THREADS)
    os.environ.setdefault("OMP_NUM_THREADS", str(GDAL_WARP_THREADS))


def _resolve(exe):
    bin_dir = _STATE.get("bin_dir")
    if bin_dir:
        candidate = os.path.join(bin_dir, exe + (".exe" if os.name == "nt" else ""))
        if os.path.exists(candidate):
            return candidate
    path = shutil.which(exe)
    if path is None:
        raise FileNotFoundError(
            f"{exe} nicht gefunden. GDAL-Binarys installieren oder "
            f"GDAL_BIN_DIR korrekt setzen."
        )
    return path


class GdalError(RuntimeError):
    def __init__(self, cmd, returncode, stderr):
        self.cmd = cmd
        tail = "\n".join(str(stderr).splitlines()[-200:])
        super().__init__(
            f"Befehl fehlgeschlagen ({returncode}): {' '.join(cmd)}\n{tail}"
        )


def run(cmd, timeout=3600):
    exe = _resolve(cmd[0])
    proc = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise GdalError(cmd, proc.returncode, proc.stderr)
    return proc.stdout, proc.stderr


def gdal_warp(args):
    return run(["gdalwarp", *args])


def gdal_translate(args):
    return run(["gdal_translate", *args])


def gdalbuild_vrt(args):
    return run(["gdalbuildvrt", *args])


def gdal_rasterize(args):
    return run(["gdal_rasterize", *args])


def ogr2ogr(args):
    return run(["ogr2ogr", *args])


def ogrinfo(args):
    return run(["ogrinfo", *args])


def transform_points(points, s_srs, t_srs):
    """Projiziert (x, y)-Punkte per gdaltransform (stdin/stdout, ein Call).

    points: Liste von (x, y). Rückgabe: Liste von (x, y) im Ziel-CRS.
    gdaltransform liest Koordinaten zeilenweise von stdin und schreibt die
    transformierten Koordinaten zeilenweise nach stdout; die ersten beiden
    Werte je Zeile sind x/y (gegebenenfalls folgt z).
    """
    exe = _resolve("gdaltransform")
    stdin = "\n".join(f"{x} {y}" for x, y in points)
    proc = subprocess.run(
        [exe, "-s_srs", s_srs, "-t_srs", t_srs],
        input=stdin, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise GdalError(["gdaltransform", s_srs, t_srs], proc.returncode, proc.stderr)
    out = []
    for line in proc.stdout.strip().splitlines():
        parts = line.replace(",", " ").split()
        if len(parts) >= 2:
            out.append((float(parts[0]), float(parts[1])))
    if len(out) != len(points):
        raise RuntimeError(
            f"gdaltransform: {len(points)} Eingaben, aber {len(out)} Ausgaben"
        )
    return out


# ===========================================================================
# ADV-Kachelschema (Dateinamen)
# ===========================================================================

DGM_TILE_RE = re.compile(
    r"^dgm1_(?P<zone>3[23])_(?P<rw>\d+)_(?P<hw>\d+)_(?P<n>\d+)"
    r"(?:_(?P<suffix>[^.]*))?"
    r"(?P<ext>\.tiff?)$",
    re.IGNORECASE,
)

BUNDESLAENDER = {
    1: "Schleswig-Holstein", 2: "Hamburg", 3: "Niedersachsen", 4: "Bremen",
    5: "Nordrhein-Westfalen", 6: "Hessen", 7: "Rheinland-Pfalz",
    8: "Baden-Württemberg", 9: "Bayern", 10: "Saarland", 11: "Berlin",
    12: "Brandenburg", 13: "Mecklenburg-Vorpommern", 14: "Sachsen",
    15: "Sachsen-Anhalt", 16: "Thüringen",
}


def parse_tile_name(name):
    m = DGM_TILE_RE.match(name)
    if not m:
        raise ValueError(f"Kein DGM1-Kachelname nach ADV-Schema: {name!r}")
    return {
        "zone": int(m.group("zone")),
        "rw": int(m.group("rw")),
        "hw": int(m.group("hw")),
        "n": int(m.group("n")),
        "suffix": m.group("suffix"),
    }


# ===========================================================================
# Fortschrittsanzeige (Statusleiste)
# ===========================================================================

class ProgressBar:
    """Thread-sichere Fortschrittsanzeige (für parallele Worker)."""

    def __init__(self, total, desc="", mode="bar", stream=None):
        self.total = max(total, 1)
        self.desc = desc
        self.mode = mode
        self.stream = stream or sys.stderr
        self._n = 0
        self._t0 = time.time()
        self._lock = threading.Lock()

    def update(self, note=""):
        with self._lock:
            self._n = min(self._n + 1, self.total)
            frac = self._n / self.total
            elapsed = time.time() - self._t0
            eta = elapsed / self._n * (self.total - self._n) if self._n else 0.0
            if self.mode == "bar":
                filled = int(32 * frac)
                bar = "#" * filled + "-" * (32 - filled)
                line = (f"\r{self.desc} |{bar}| {100 * frac:5.1f}% "
                        f"({self._n}/{self.total}) ETA {eta:5.0f}s")
                if note:
                    line += f" {note}"
                self.stream.write(line)
                self.stream.flush()
                if self._n >= self.total:
                    self.stream.write("\n")
            else:
                print(f"[{self.desc}] {self._n}/{self.total} ({100 * frac:.1f}%) {note}",
                      file=self.stream)


# ===========================================================================
# Schritt 1: Inventur
# ===========================================================================

@dataclass
class Tile:
    path: str
    zone: int
    rw: int
    hw: int
    n: int
    suffix: str = None
    abbr: str = None      # AdV-Länderkürzel aus dem Suffix (z.B. "be")


def abbr_of_suffix(suffix):
    """Extrahiert das AdV-Länderkürzel aus dem Datei-Suffix.

    'be_2025' -> 'be'; 'bw' -> 'bw'; 'x_y_z' -> None (unbekannt).
    """
    if not suffix:
        return None
    first = suffix.split("_")[0].lower()
    return first if first in ABBR_TO_AGS else None


@dataclass
class Inventory:
    tiles: list = field(default_factory=list)
    utm33_tiles: list = field(default_factory=list)
    utm32_tiles: list = field(default_factory=list)
    unreadable: list = field(default_factory=list)


def scan_stripes(root):
    inv = Inventory()
    for dirpath, _dirnames, filenames in sorted(os.walk(root)):
        for fn in sorted(filenames):
            if not fn.lower().endswith((".tif", ".tiff")):
                continue
            full = os.path.join(dirpath, fn)
            try:
                meta = parse_tile_name(fn)
            except ValueError:
                inv.unreadable.append(full)
                continue
            meta["abbr"] = abbr_of_suffix(meta.get("suffix"))
            tile = Tile(path=full, **meta)
            inv.tiles.append(tile)
            (inv.utm33_tiles if tile.zone == 33 else inv.utm32_tiles).append(tile)
    return inv


def derive_state_zones(inv):
    """Leitet {AGS: Datensatzzone} automatisch aus den Dateinamen ab.

    Das AdV-Kürzel im Suffix (z.B. 'be' in dgm1_33_372_5805_1_be_2025.tif)
    identifiziert das Bundesland; die Zone (32/33) steht im Zonen-Segment
    des Dateinamens. Bei widersprüchlichen Zonen je Kürzel wird gewarnt
    und die erste beobachtete Zone verwendet.
    """
    zone_of_state = {}
    conflicts = {}
    for tile in inv.tiles:
        if not tile.abbr:
            continue
        ags = ABBR_TO_AGS[tile.abbr]
        if ags in zone_of_state and zone_of_state[ags] != tile.zone:
            conflicts.setdefault(tile.abbr, set()).add(zone_of_state[ags])
            conflicts[tile.abbr].add(tile.zone)
        else:
            zone_of_state[ags] = tile.zone
    for abbr, zones in conflicts.items():
        print(f"      WARNUNG: Kürzel '{abbr}' mit gemischten Zonen {sorted(zones)}; "
              f"verwende Zone {zone_of_state[ABBR_TO_AGS[abbr]]}")
    return zone_of_state


# ===========================================================================
# Schritt 2: Umprojektion (gdalinfo + gdaltransform + gdalwarp)
# ===========================================================================

def read_gdalinfo(path):
    out, _ = run(["gdalinfo", "-json", path])
    return json.loads(out)


def raster_bounds(path):
    info = read_gdalinfo(path)
    gt = info["geoTransform"]
    size = info["size"]
    x0, ps_x, _, y0, _, ps_y = gt
    xmin, xmax = sorted((x0, x0 + ps_x * size[0]))
    ymin, ymax = sorted((y0, y0 + ps_y * size[1]))
    return xmin, ymin, xmax, ymax


def tile_extent(rw, hw):
    return (rw * TILE_SIZE_M, hw * TILE_SIZE_M,
            (rw + 1) * TILE_SIZE_M, (hw + 1) * TILE_SIZE_M)


def target_tiles_of(src, zone):
    """Alle UTM32-Zielkacheln, die von der Quellkachel berührt werden.

    Wichtig: Eine UTM33-Kachel mit rw=292 liegt ~421 km weiter östlich als
    UTM32-rw=292; die Zielposition wird daher aus der Georeferenzierung
    der Datei bestimmt: Ecken per gdaltransform nach EPSG:25832 und dann
    auf das km-Raster gesnappt.
    """
    xmin, ymin, xmax, ymax = raster_bounds(src)
    if zone == 33:
        corners = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
        t_corners = transform_points(corners, "EPSG:25833", "EPSG:25832")
        xs = [p[0] for p in t_corners]
        ys = [p[1] for p in t_corners]
        xmin, ymin, xmax, ymax = min(xs), min(ys), max(xs), max(ys)
    return [
        (rw, hw)
        for rw in range(math.floor(xmin / TILE_SIZE_M), math.floor(xmax / TILE_SIZE_M) + 1)
        for hw in range(math.floor(ymin / TILE_SIZE_M), math.floor(ymax / TILE_SIZE_M) + 1)
    ]


def warp_to_utm32(src, dst, rw, hw):
    xmin, ymin, xmax, ymax = tile_extent(rw, hw)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    gdal_warp([
        "-overwrite",
        "-t_srs", "EPSG:25832",
        "-te", f"{xmin}", f"{ymin}", f"{xmax}", f"{ymax}",
        "-tr", str(RES_M), str(RES_M),
        "-r", RESAMPLE,
        "-dstnodata", str(NODATA),
        "-srcnodata", str(NODATA),
        *_tif_co(),
        "-multi", "-wo", f"NUM_THREADS={GDAL_WARP_THREADS}",
        src, dst,
    ])


# ===========================================================================
# VG25: Vorbereitung und Auswertung ausschließlich über OGR/GDAL-CLI
# ===========================================================================

def list_layers(gdal_path):
    """Auflistung aller Layer einer OGR-Quelle (z.B. GPKG) via ogrinfo."""
    out, _ = ogrinfo(["-ro", "-so", "-al", "-q", gdal_path])
    names = re.findall(r"^\d+:\s+(.+?)\s+\(.+\)\s*$", out, re.MULTILINE)
    if not names:
        names = re.findall(r"^Layer name:\s*(.+?)\s*$", out, re.MULTILINE)
    return names


def parse_ags(value):
    """Parst einen AGS-Wert robust; Rückgabe int oder None.

    VG250-Quellen enthalten Platzhalter wie '--------' (z.B. Bodensee)
    und fehlende Werte; alles was kein reiner Ziffern-String ist, wird
    verworfen.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or not s.isdigit():
        return None
    try:
        return int(s)
    except ValueError:
        return None


def detect_state_layer(gdal_path, ags_field):
    """Findet die Länderebene (Bundesländer) in einer VG250-Quelle.

    VG250 enthält alle Verwaltungsebenen (Länder, Kreise, Gemeinden).
    Die Länderebene erkennt man daran, dass ihre AGS-Werte genau die
    Bundesland-Schlüssel 1..16 sind (Gemeinde-/Kreis-AGS sind 5- bzw.
    8-stellig). Es wird je Layer geprüft, ob ALLE numerischen AGS-Werte
    im Bereich 1..16 liegen. Rückgabe: Layername oder None.
    """
    layers = list_layers(gdal_path)
    for layer in layers:
        try:
            out, _ = ogrinfo([
                "-ro", "-so", "-al", gdal_path, layer,
                "-sql", f"SELECT DISTINCT {ags_field} AS v FROM \"{layer}\"",
            ])
        except Exception:
            continue
        vals = []
        for line in out.splitlines():
            s = line.strip().strip('"').strip("'")
            ags = parse_ags(s)
            if ags is not None:
                vals.append(ags)
        if vals and all(1 <= v <= 16 for v in vals):
            return layer
    return None


def prepare_vg25(vg25_path, tmp_root):
    """Konvertiert die VG25-Länderebene einmalig nach UTM32 (GeoJSON).

    Ist VG25_LAYER gesetzt, wird dieser Layer genutzt; sonst wird die
    Länderebene automatisch erkannt (detect_state_layer). Die Zieldatei
    ist die gemeinsame Quelle für Rasterisierung (Klassifikation) und
    Cutline-Clips (Harmonisierung); als UTM32-Datei entfällt jede
    On-the-fly-Umprojektion.
    """
    dst = os.path.join(tmp_root, "vg25_utm32.geojson")
    if os.path.exists(dst):
        return dst

    layer = VG25_LAYER or detect_state_layer(vg25_path, VG25_AGS_FIELD)
    if layer:
        print(f"      VG25-Länderebene: '{layer}'")
        ogr2ogr(["-f", "GeoJSON", "-t_srs", "EPSG:25832", dst, vg25_path, layer])
    else:
        # Ein-Layer-Quelle (z.B. Shapefile/Laender-GeoJSON): alles nehmen
        print("      VG25: keine Länderebene erkannt, nutze gesamten Layer")
        ogr2ogr(["-f", "GeoJSON", "-t_srs", "EPSG:25832", dst, vg25_path])
    return dst


def vg25_layer_name(vg25_utm32):
    out, _ = ogrinfo(["-ro", "-so", "-al", vg25_utm32])
    m = re.search(r"^Layer name:\s*(.+?)\s*$", out, re.MULTILINE)
    if not m:
        raise RuntimeError(f"Layername nicht lesbar aus {vg25_utm32}")
    return m.group(1)


def vg25_extent(vg25_utm32):
    """Layer-Ausdehnung (UTM32) aus ogrinfo; wird aufs km-Raster gedehnt."""
    out, _ = ogrinfo(["-ro", "-so", "-al", vg25_utm32])
    m = re.search(
        r"Extent:\s*\(\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*\)\s*-\s*"
        r"\(\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*\)",
        out,
    )
    if not m:
        raise RuntimeError(f"Ausdehnung nicht lesbar aus {vg25_utm32}")
    xmin, ymin, xmax, ymax = (float(g) for g in m.groups())
    xmin = math.floor(xmin / TILE_SIZE_M) * TILE_SIZE_M
    ymin = math.floor(ymin / TILE_SIZE_M) * TILE_SIZE_M
    xmax = math.ceil(xmax / TILE_SIZE_M) * TILE_SIZE_M
    ymax = math.ceil(ymax / TILE_SIZE_M) * TILE_SIZE_M
    return xmin, ymin, xmax, ymax


def vg25_distinct_ags(vg25_utm32, layer, field):
    """DISTINCT-Werte des AGS-Feldes via ogr2ogr (CSV) – robust gefiltert.

    Nur gültige Bundesland-Schlüssel (Ziffern, Wert 1..16) werden
    zurückgegeben; Platzhalter wie '--------' oder fehlende Werte werden
    verworfen."""
    csv_path = os.path.splitext(vg25_utm32)[0] + "_ags.csv"
    ogr2ogr([
        "-f", "CSV", csv_path, vg25_utm32, layer,
        "-sql", f"SELECT DISTINCT {field} AS v FROM \"{layer}\"",
    ])
    seen = set()
    uniq = []
    with open(csv_path, "r", encoding="utf-8", errors="ignore") as fh:
        lines = [l.strip() for l in fh if l.strip()]
    for line in lines[1:]:
        v = line.strip('"').strip("'")
        ags = parse_ags(v)
        if ags is None or not (1 <= ags <= 16):
            continue
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    if not uniq:
        raise RuntimeError(
            f"Keine gültigen Bundesland-AGS aus {vg25_utm32} gelesen "
            f"(Feld '{field}'). Prüfen, ob VG25_AGS_FIELD/VG25_LAYER stimmen."
        )
    return uniq


def classify_tiles_by_vg25(vg25_utm32, layer, distinct, extent, tmp_root):
    """Bestimmt je 1-km-Kachel die berührenden Bundesländer (reine GDAL-CLI).

    Je Bundesland wird die VG25 mit gdal_rasterize -at (ALL_TOUCHED) auf
    ein 1-km-Raster über der VG25-Ausdehnung gerastert; das Raster wird
    per gdal_translate -of XYZ in Klartext (x y value) überführt und mit
    Standardbibliothek geparst. Pixelmitte = Kachelmitte (+500 m), also
    kodiert jedes belegte Pixel eine berührte Kachel.

    Rückgabe: {(rw, hw): set(AGS-int)} aller von >=1 Land berührten Kacheln.
    Hinweis: ALL_TOUCHED ist leicht überinklusiv (Kanten genügen); eine
    Nur-Rand-Berührung liefert nach dem Clip das unveränderte Kachelbild.
    """
    xmin, ymin, xmax, ymax = extent
    touched = {}
    workdir = os.path.join(tmp_root, "vg25_touch")
    os.makedirs(workdir, exist_ok=True)
    for val in distinct:
        grid = os.path.join(workdir, f"touch_{re.sub(r'[^0-9A-Za-z_]', '_', val)}.tif")
        gdal_rasterize([
            "-at", "-burn", "1", "-init", "0",
            "-where", f"{VG25_AGS_FIELD}='{val}'",
            "-te", f"{xmin}", f"{ymin}", f"{xmax}", f"{ymax}",
            "-tr", f"{TILE_SIZE_M:.0f}", f"{TILE_SIZE_M:.0f}",
            "-ot", "Byte",
            vg25_utm32, grid,
        ])
        xyz = os.path.splitext(grid)[0] + ".xyz"
        gdal_translate(["-of", "XYZ", grid, xyz])
        with open(xyz, "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3 or float(parts[2]) == 0.0:
                    continue
                x, y = float(parts[0]), float(parts[1])
                rw = int(x // TILE_SIZE_M)
                hw = int(y // TILE_SIZE_M)
                touched.setdefault((rw, hw), set()).add(parse_ags(val))
    return touched


# ===========================================================================
# Schritt 3/4: Klassifikation + Harmonisierung
# ===========================================================================

@dataclass
class TilePlan:
    rw: int
    hw: int
    state_tiles: dict = field(default_factory=dict)  # zone -> [Pfade]
    contrib_by_ags: dict = field(default_factory=dict)  # AGS -> [Pfade]
    states_in_tile: list = field(default_factory=list)


@dataclass
class Classification:
    interior: list = field(default_factory=list)
    border: list = field(default_factory=list)
    missing: list = field(default_factory=list)  # (ags, rw, hw)


def classify(target_index, contrib_by_ags, touched, zone_of_state):
    """Klassifikation je Zielkachel.

    target_index: {(rw,hw): {zone: [Pfade]}}
    contrib_by_ags: {(rw,hw): {AGS: [Pfade]}} – Beitragskacheln je
        Bundesland, abgeleitet aus dem AdV-Kürzel im Dateinamen.
    touched: {(rw,hw): {AGS}} – laut VG25 berührte Bundesländer.

    Eine Kachel wird HARMONISIERT (Grenzkachel), wenn:
      a) sie laut VG25 von >= 2 Bundesländern berührt wird, ODER
      b) Beitragskacheln eines Bundeslands vorliegen, das die Kachel laut
         VG25 gar nicht (vollständig) besitzt – d.h. eine Lieferung reicht
         über die VG-Grenze ins Nachbarland (typisch: Grenzstreifen der
         Landes-DGM-Lieferungen). Dann wird JE Beitrag an die
         VG-Landesmaske seines Bundeslands geclippt; Daten jenseits der
         VG-Grenze werden entfernt und an der Grenze zusammengefügt.
    """
    cls = Classification()
    for (rw, hw), by_zone in sorted(target_index.items()):
        states = sorted(touched.get((rw, hw), set()))
        contrib = contrib_by_ags.get((rw, hw), {})
        contrib_states = {a for a in contrib if a is not None}
        plan = TilePlan(rw=rw, hw=hw)
        plan.state_tiles = {z: list(p) for z, p in by_zone.items()}
        plan.contrib_by_ags = {a: list(p) for a, p in contrib.items()}
        plan.states_in_tile = states
        is_border = bool(states) and (
            len(states) >= 2
            or (contrib_states and not contrib_states <= set(states))
        )
        if not is_border:
            cls.interior.append(plan)
        else:
            for ags in states:
                if ags not in contrib_states:
                    zone = zone_of_state.get(ags)
                    if zone is not None and not by_zone.get(zone):
                        cls.missing.append((ags, rw, hw))
            cls.border.append(plan)
    return cls


def _write_list(paths, path):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(paths))
    return path


# Protokoll aller Pixel-Reparaturen (thread-sicher gesammelt):
# (x, y, kachel, schritt, wert) – wird am Ende als CSV + Shapefile
# ausgegeben, damit nachvollziehbar bleibt, wo repariert wurde.
_REPAIRS = []
_REPAIRS_LOCK = threading.Lock()


def fill_small_holes(path, tmp_root, schritt=""):
    """Repariert kleine NoData-Fehlstellen in einer Kachel (GDAL-CLI only).

    Ursachen solcher Fehlpixel (z.B. 999999 statt 1 Mio. Pixel):
      - Cutline-Clipping: Ein Pixel genau auf der VG-Linie kann bei beiden
        Landesmasken ausgeschlossen werden (Pixelmittelpunkt auf der
        gemeinsamen Grenze) -> Loch von 1-2 Pixeln.
      - Zonengrenzen-Warp: Sub-Pixel-Spalten am Trapezrand, in denen alle
        Beitraege NoData haben.

    Vorgehen (reine Standardbibliothek + gdal_translate):
      1. Kachel als XYZ-Klartext einlesen (gdal_translate -of XYZ)
      2. Zusammenhaengende NoData-Bereiche per BFS bestimmen
      3. Bereiche bis FILL_MAX_HOLE_PX Pixel mit dem Mittel der gueltigen
         8-Nachbarn fuellen; groessere Bereiche bleiben unberuehrt
      4. Nur bei Aenderungen: Kachel via XYZ -> GTiff zurueckschreiben

    Rueckgabe: Anzahl gefuellter Pixel.
    """
    if not FILL_HOLES:
        return 0
    import array as _array
    from collections import deque

    info = read_gdalinfo(path)
    gt = info["geoTransform"]
    width, height = info["size"]
    x0, ps_x, _, y0, _, ps_y = gt

    workdir = os.path.join(tmp_root, "fill")
    os.makedirs(workdir, exist_ok=True)
    xyz = os.path.join(workdir, f"{os.getpid()}_{os.path.basename(path)}.xyz")
    gdal_translate(["-of", "XYZ", path, xyz])

    n_cells = width * height
    grid = _array.array("f", [NODATA]) * n_cells
    with open(xyz, "r", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 3:
                continue
            x, y = float(parts[0]), float(parts[1])
            col = int(round((x - x0) / ps_x - 0.5))
            row = int(round((y - y0) / ps_y - 0.5))
            if 0 <= row < height and 0 <= col < width:
                grid[row * width + col] = float(parts[2])

    if NODATA not in grid:
        return 0

    visited = bytearray(n_cells)
    filled = 0
    for start in range(n_cells):
        if grid[start] != NODATA or visited[start]:
            continue
        # BFS: zusammenhaengender NoData-Bereich KOMPLETT durchlaufen
        # (kein frueher Abbruch – sonst bleiben Fragmente uebrig, die wie
        # kleine separate Loecher wirken und faelschlich gefuellt wuerden)
        comp = []
        dq = deque([start])
        visited[start] = 1
        while dq:
            i = dq.popleft()
            comp.append(i)
            r, c = divmod(i, width)
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < height and 0 <= cc < width:
                        j = rr * width + cc
                        if grid[j] == NODATA and not visited[j]:
                            visited[j] = 1
                            dq.append(j)
        if len(comp) > FILL_MAX_HOLE_PX:
            continue
        # Kleines Loch: je Pixel Mittel der gueltigen 8-Nachbarn
        for i in comp:
            r, c = divmod(i, width)
            vals = []
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < height and 0 <= cc < width:
                        v = grid[rr * width + cc]
                        if v != NODATA:
                            vals.append(v)
            if vals:
                new_v = sum(vals) / len(vals)
                grid[i] = new_v
                filled += 1
                with _REPAIRS_LOCK:
                    _REPAIRS.append((
                        x0 + (c + 0.5) * ps_x,
                        y0 + (r + 0.5) * ps_y,
                        os.path.basename(path),
                        schritt,
                        new_v,
                    ))

    if filled:
        out_xyz = xyz + ".filled"
        with open(out_xyz, "w", encoding="utf-8") as fh:
            for row in range(height):
                y = y0 + (row + 0.5) * ps_y
                base = row * width
                for col in range(width):
                    x = x0 + (col + 0.5) * ps_x
                    fh.write(f"{x} {y} {grid[base + col]}\n")
        tmp_tif = path + ".filled.tif"
        gdal_translate([
            "-of", "GTiff", "-ot", "Float32",
            "-a_srs", "EPSG:25832",
            "-a_nodata", str(NODATA),
            *_tif_co(),
            out_xyz, tmp_tif,
        ])
        os.replace(tmp_tif, path)
    return filled


def merge_tiles(sources, dst, tmpdir):
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    workdir = os.path.join(tmpdir, f"merge_{os.path.basename(dst)}")
    os.makedirs(workdir, exist_ok=True)
    vrt = os.path.join(workdir, "mosaic.vrt")
    gdalbuild_vrt([
        "-input_file_list",
        _write_list(sources, os.path.join(workdir, "parts.txt")),
        vrt,
    ])
    gdal_translate([*_tif_co(), vrt, dst])
    return dst


def harmonize_border_tile(tile_paths_by_state, state_str_by_ags,
                           vg25_utm32, rw, hw, dst, tmpdir):
    """Je Bundesland: alle (gewarpten) Quellbeiträge mit der VG25-Landes-
    maske clippen (gdalwarp -cutline -cwhere; GDAL schneidet die Maske
    intern auf die -te-Kachel), je Land mergen, dann alle Landes-Teile zur
    finalen Grenzkachel verschmelzen. NoData wird gefüllt."""
    os.makedirs(tmpdir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    xmin, ymin, xmax, ymax = tile_extent(rw, hw)
    workdir = os.path.join(tmpdir, f"harmonize_{rw:03d}_{hw:04d}")
    os.makedirs(workdir, exist_ok=True)

    state_parts = []
    for ags in sorted(tile_paths_by_state):
        val = state_str_by_ags.get(ags)
        if val is None:
            continue
        clips = []
        for j, src in enumerate(tile_paths_by_state[ags]):
            part = os.path.join(workdir, f"clip_{ags}_{j}.tif")
            gdal_warp([
                "-overwrite",
                "-t_srs", "EPSG:25832",
                "-te", f"{xmin}", f"{ymin}", f"{xmax}", f"{ymax}",
                "-tr", str(RES_M), str(RES_M),
                # Quellen liegen bereits rasterdeckungsgleich vor -> near
                # ist identitaet und verhindert zusaetzliche Glattung
                "-r", "near",
                "-cutline", vg25_utm32,
                "-cwhere", f"{VG25_AGS_FIELD}='{val}'",
                "-dstnodata", str(NODATA),
                "-srcnodata", str(NODATA),
                "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=3",
                "-co", f"NUM_THREADS={GDAL_COMPRESS_THREADS}",
                "-multi", "-wo", f"NUM_THREADS={GDAL_WARP_THREADS}",
                src, part,
            ])
            clips.append(part)
        if clips:
            merged = os.path.join(workdir, f"state_{ags}.tif")
            merge_tiles(clips, merged, workdir)
            state_parts.append(merged)
    if not state_parts:
        return None
    return merge_tiles(state_parts, dst, workdir)


# ===========================================================================
# Orchestrierung
# ===========================================================================

def strip_name(rw):
    return f"s32_{rw:03d}"


def run_workflow(input_root, output_root, vg25_path, tmp_root,
                 progress_mode="bar", keep_intermediate=False,
                 state_zones=None):
    with _REPAIRS_LOCK:
        _REPAIRS.clear()

    # -- 1. Inventur ------------------------------------------------------
    print(f"[1/5] Inventur: {input_root}")
    inv = scan_stripes(input_root)
    print(f"      {len(inv.tiles)} Kacheln "
          f"(UTM33: {len(inv.utm33_tiles)}, UTM32: {len(inv.utm32_tiles)})")
    if inv.unreadable:
        print(f"      WARNUNG: {len(inv.unreadable)} nicht parsbare Dateien")

    # Zone je Bundesland: automatisch aus den AdV-Kürzeln im Dateinamen
    # (z.B. dgm1_33_372_5805_1_be_2025.tif -> 'be' -> Berlin -> Zone 33);
    # STATE_ZONES-Fallback nur für Kacheln ohne Kürzel-Suffix.
    derived = derive_state_zones(inv)
    zone_of_state = dict(state_zones or STATE_ZONES)
    zone_of_state.update(derived)
    if derived:
        print(f"      Bundesland-Zonen aus Kürzeln abgeleitet: "
              f"{{{', '.join(f'{k}:{v}' for k, v in sorted(derived.items()))}}}")
    ProgressBar(1, "Inventur", progress_mode).update()

    os.makedirs(tmp_root, exist_ok=True)

    # -- 2. VG25 vorbereiten + Grenzkacheln bestimmen -----------------------
    print(f"[2/5] VG25 vorbereiten: {vg25_path}")
    vg25_utm32 = prepare_vg25(vg25_path, tmp_root)
    layer = vg25_layer_name(vg25_utm32)
    extent = vg25_extent(vg25_utm32)
    distinct = vg25_distinct_ags(vg25_utm32, layer, VG25_AGS_FIELD)
    state_str_by_ags = {parse_ags(v): v for v in distinct}
    print(f"      {len(distinct)} Bundesländer in VG25 "
          f"(Layer '{layer}', AGS-Feld '{VG25_AGS_FIELD}')")
    print(f"      VG25-Rasterisierung zur Grenzkachel-Bestimmung "
          f"({len(distinct)} Laender, ALL_TOUCHED 1-km-Raster)")
    touched = classify_tiles_by_vg25(vg25_utm32, layer, distinct, extent, tmp_root)
    n_border_possible = sum(1 for s in touched.values() if len(s) >= 2)
    print(f"      {len(touched)} Kacheln berühren >=1 Bundesland, "
          f"davon {n_border_possible} mit >=2 Bundesländern")

    # -- 3. Umprojektion UTM33 -> UTM32 --------------------------------------
    warped_dir = os.path.join(tmp_root, "warped")
    os.makedirs(warped_dir, exist_ok=True)
    print(f"[3/5] Umprojektion UTM33 -> UTM32 (gdalwarp, {WORKERS} Prozesse)")
    pb = ProgressBar(len(inv.utm33_tiles), "Warp UTM33", progress_mode)

    warp_jobs = []
    for tile in inv.utm33_tiles:
        ags_src = ABBR_TO_AGS.get(tile.abbr) if tile.abbr else None
        jobs_for_tile = []
        for (trw, thw) in target_tiles_of(tile.path, tile.zone):
            dst = os.path.join(
                warped_dir,
                f"dgm1_32_{trw:03d}_{thw:04d}_from_{tile.zone}_"
                f"{tile.rw:03d}_{tile.hw:04d}_{tile.n}.tif",
            )
            jobs_for_tile.append(((trw, thw), dst))
        warp_jobs.append((tile, ags_src, jobs_for_tile))

    def _warp_one(args):
        tile, ags_src, jobs_for_tile = args
        for (_pos, dst) in jobs_for_tile:
            if not os.path.exists(dst):
                warp_to_utm32(tile.path, dst, _pos[0], _pos[1])
        pb.update(note=os.path.basename(tile.path))
        return tile, ags_src, jobs_for_tile

    target_index = {}
    contrib_by_ags = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(_warp_one, job) for job in warp_jobs]
        errors = []
        for fut in as_completed(futures):
            try:
                tile, ags_src, jobs_for_tile = fut.result()
            except Exception as e:
                errors.append(str(e))
                pb.update()
                continue
            for (pos, dst) in jobs_for_tile:
                target_index.setdefault(pos, {}).setdefault(33, []).append(dst)
                if ags_src is not None:
                    contrib_by_ags.setdefault(pos, {}).setdefault(ags_src, []).append(dst)
        if errors:
            print(f"      WARNUNG: {len(errors)} Warp-Jobs fehlgeschlagen, "
                  f"z.B.: {errors[0][:200]}")
    for tile in inv.utm32_tiles:
        target_index.setdefault((tile.rw, tile.hw), {}).setdefault(32, []).append(tile.path)
        ags_src = ABBR_TO_AGS.get(tile.abbr) if tile.abbr else None
        if ags_src is not None:
            contrib_by_ags.setdefault((tile.rw, tile.hw), {}).setdefault(ags_src, []).append(tile.path)

    # -- 4. Klassifikation + Harmonisierung -----------------------------------
    print(f"[4/5] Klassifikation (VG25-Schnitt + Beitragslaender)")
    cls = classify(target_index, contrib_by_ags, touched, zone_of_state)
    print(f"      {len(cls.interior)} Innenkacheln, {len(cls.border)} Grenzkacheln, "
          f"{len(cls.missing)} fehlende Partnerkacheln")

    print(f"[4/5] Harmonisierung: {len(cls.border)} Grenzkacheln "
          f"({WORKERS} Prozesse)")
    harmonized_dir = os.path.join(tmp_root, "harmonized")
    os.makedirs(harmonized_dir, exist_ok=True)
    pb = ProgressBar(len(cls.border), "Harmonisierung", progress_mode)
    border_out = {}

    harm_jobs = []
    for plan in cls.border:
        srcs_by_state = {}
        # Je beteiligtem Bundesland (VG25-Länder der Kachel UND Beitrags-
        # länder): Quellkacheln dieses Landes; jeder Beitrag wird an die
        # VG-Landesmaske geclippt – Daten jenseits der VG-Grenze fallen weg.
        cand_states = set(plan.states_in_tile) | set(plan.contrib_by_ags)
        for ags in sorted(cand_states):
            if ags not in state_str_by_ags:
                continue
            cand = plan.contrib_by_ags.get(ags)
            if not cand:
                zone = zone_of_state.get(ags, 32)
                cand = plan.state_tiles.get(zone) or plan.state_tiles.get(65 - zone) or []
            if cand:
                srcs_by_state[ags] = cand
        dst = os.path.join(harmonized_dir,
                           f"harmonized_{plan.rw:03d}_{plan.hw:04d}.tif")
        harm_jobs.append((plan, srcs_by_state, dst))

    def _harm_one(args):
        plan, srcs_by_state, dst = args
        if srcs_by_state and not os.path.exists(dst):
            harmonize_border_tile(srcs_by_state, state_str_by_ags,
                                  vg25_utm32, plan.rw, plan.hw, dst, tmp_root)
            # Kleine Fehlstellen reparieren (z.B. Pixel auf der VG-Linie,
            # das beiden Landesmasken entfiel)
            if os.path.exists(dst):
                fill_small_holes(dst, tmp_root, "harmonisierung")
        pb.update(note=f"{plan.rw:03d}/{plan.hw:04d}")
        return (plan.rw, plan.hw), dst, os.path.exists(dst)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(_harm_one, job) for job in harm_jobs]
        errors = []
        for fut in as_completed(futures):
            try:
                pos, dst, ok = fut.result()
            except Exception as e:
                errors.append(str(e))
                pb.update()
                continue
            if ok:
                border_out[pos] = dst
        if errors:
            print(f"      WARNUNG: {len(errors)} Harmonisierungen fehlgeschlagen, "
                  f"z.B.: {errors[0][:200]}")

    # -- 5. Ausgabe in Streifenordner ----------------------------------------
    print(f"[5/5] Ausgabe in Streifenordner: {output_root} "
          f"({WORKERS} Prozesse)")
    pb = ProgressBar(len(cls.interior) + len(border_out), "Ausgabe", progress_mode)

    out_jobs = []
    for plan in cls.interior:
        # Bevorzugt die Beitraege des VG-Eigentuemers (Kachel vollstaendig
        # in EINEM Bundesland): Beitraege anderer Laender (Lieferungen, die
        # ueber die VG-Grenze reichen) werden ignoriert.
        contribs = []
        if len(plan.states_in_tile) == 1:
            owner = plan.states_in_tile[0]
            contribs = plan.contrib_by_ags.get(owner, [])
        if not contribs:
            for zone in sorted(plan.state_tiles):
                contribs.extend(plan.state_tiles[zone])
        if not contribs:
            pb.update()
            continue
        strip = os.path.join(output_root, strip_name(plan.rw))
        dst = os.path.join(strip, f"dgm1_32_{plan.rw:03d}_{plan.hw:04d}_1.tif")
        out_jobs.append((plan.rw, contribs, dst, len(contribs) > 1))
    for (rw, hw), src in sorted(border_out.items()):
        strip = os.path.join(output_root, strip_name(rw))
        dst = os.path.join(strip, f"dgm1_32_{rw:03d}_{hw:04d}_1.tif")
        # Grenzkacheln wurden in Schritt 4 bereits gefuellt
        out_jobs.append((rw, [src], dst, False))

    copied = 0

    def _out_one(args):
        rw, contribs, dst, needs_fill = args
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if len(contribs) == 1:
            if os.path.abspath(contribs[0]) != os.path.abspath(dst):
                shutil.copyfile(contribs[0], dst)
        else:
            merge_tiles(contribs, dst, tmp_root)
        if needs_fill:
            # Zusammengefuehrte Innenkacheln: Naht-Fehlstellen reparieren
            fill_small_holes(dst, tmp_root, "merge")
        pb.update(note=f"s32_{rw:03d}")
        return dst

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(_out_one, job) for job in out_jobs]
        errors = []
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                errors.append(str(e))
                pb.update()
                continue
            copied += 1
        if errors:
            print(f"      WARNUNG: {len(errors)} Ausgabe-Jobs fehlgeschlagen, "
                  f"z.B.: {errors[0][:200]}")

    if not keep_intermediate:
        shutil.rmtree(tmp_root, ignore_errors=True)

    # -- Reparaturprotokoll: CSV + Shapefile mit den Reparaturpunkten -----
    with _REPAIRS_LOCK:
        repairs = list(_REPAIRS)
    if repairs:
        csv_path = os.path.join(output_root, "pixelreparaturen.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["x", "y", "kachel", "schritt", "wert"])
            for rec in repairs:
                w.writerow(rec)
        shp_path = os.path.join(output_root, "pixelreparaturen.shp")
        ogr2ogr([
            "-f", "ESRI Shapefile", shp_path, csv_path,
            "-oo", "X_POSSIBLE_NAMES=x", "-oo", "Y_POSSIBLE_NAMES=y",
            "-a_srs", "EPSG:25832", "-nlt", "POINT",
        ])
        print(f"      {len(repairs)} Pixel repariert -> "
              f"{shp_path} (+ pixelreparaturen.csv)")

    print(f"Fertig: {copied} Kacheln in {output_root} (Streifen s32_*)")
    return {"interior": len(cls.interior), "border": len(cls.border),
            "missing": len(cls.missing), "copied": copied,
            "reparaturen": len(repairs)}


# ===========================================================================
# CLI
# ===========================================================================

def main(argv=None):
    p = argparse.ArgumentParser(description="UTM32-Harmonisierungs-Workflow")
    p.add_argument("--input", default=ROOT_INPUT_DIR)
    p.add_argument("--output", default=OUTPUT_DIR)
    p.add_argument("--vg25", default=VG25_PATH)
    p.add_argument("--tmp", default=TMP_ROOT)
    p.add_argument("--progress", default=PROGRESS_MODE, choices=["bar", "log"])
    p.add_argument("--keep-intermediate", action="store_true",
                   default=KEEP_INTERMEDIATE)
    args = p.parse_args(argv)

    configure_gdal_env(GDAL_BIN_DIR or None, GDAL_DATA_DIR or None)
    run_workflow(
        input_root=args.input,
        output_root=args.output,
        vg25_path=args.vg25,
        tmp_root=args.tmp,
        progress_mode=args.progress,
        keep_intermediate=args.keep_intermediate,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
