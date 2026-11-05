# UTM32-Harmonisierungs-Workflow – lokale Ausführung

Eine einzelne Datei: **`utm32_workflow.py`** – reine Python-Standardbibliothek
+ deine GDAL-Installation (Binarys, z.B. aus QGIS). Kein pip, keine
GDAL-Python-Bindings.

## Voraussetzungen

- **Python 3** (Standardbibliothek genügt: `argparse`, `subprocess`, `json`,
  `math`, `re`, `os`, `shutil`, `threading`, `concurrent.futures`,
  `dataclasses`, `time`)
- **GDAL-Binarys** – genutzt werden: `gdalwarp`, `gdalinfo`, `gdaltransform`,
  `gdal_translate`, `gdalbuildvrt`, `gdal_rasterize`, `ogr2ogr`, `ogrinfo`
  (alle in QGIS enthalten)

## Konfiguration

Am Dateianfang unter `KONFIGURATION` anpassen:

```python
GDAL_BIN_DIR  = r"C:\Program Files\QGIS 3.40.8\bin"   # "" = GDAL im PATH
GDAL_DATA_DIR = r"C:\Program Files\QGIS 3.40.8\share\gdal"

ROOT_INPUT_DIR = r"C:\daten\dgm1_streifen"
OUTPUT_DIR     = r"C:\daten\dgm1_utm32_harmonisiert"
VG25_PATH      = r"C:\daten\vg25.geojson"   # OGR-lesbar (GPKG/GeoJSON/GML)
VG25_AGS_FIELD = "ags"                       # Spalte mit Bundesland-AGS
```

VG25 kann jetzt **jedes OGR-lesbare Format** sein (GPKG, GeoJSON, GML,
Shapefile) – der Workflow konvertiert sie einmalig per `ogr2ogr` nach
UTM32-GeoJSON. Die VG25 braucht ein Attribut mit dem Bundesland-AGS
(z.B. `"05"` für NRW); Name der Spalte in `VG25_AGS_FIELD`.

Bundesland-Zonen (32/33) werden automatisch aus den AdV-Kürzeln in den
Dateinamen abgeleitet (`dgm1_33_372_5805_1_be_2025.tif` → `be` → Zone 33).
Offizielle Kürzel: **BW, BY, BE, BB, HB, HH, HE, MV, NI, NW, RP, SL, SN,
ST, SH, TH**. `STATE_ZONES` ist nur Fallback für Kacheln ohne Kürzel.

## Ausführen

```bash
# Variante A: Konfiguration in der Datei
python utm32_workflow.py

# Variante B: per CLI (überschreibt die Datei-Werte)
python utm32_workflow.py --input C:\daten\in --output C:\daten\out ^
    --vg25 C:\daten\vg25.gpkg --tmp E:\scratch --progress log

# Zwischendaten behalten (Debug):
python utm32_workflow.py ... --keep-intermediate
```

## Wie die VG25-Verarbeitung ohne Geometrie-Bibliothek funktioniert

- **Grenzkachel-Erkennung**: Je Bundesland wird die VG25 einmalig per
  `gdal_rasterize -at` (ALL_TOUCHED) auf ein 1-km-Raster über der
  VG25-Ausdehnung gerastert; das Ergebnis wird als `gdal_translate -of XYZ`
  (Klartext `x y value`) eingelesen. Jedes belegte Pixel = eine vom Land
  berührte 1-km-Kachel. Kacheln mit ≥2 Ländern = Grenzkacheln.
- **Clipping**: `gdalwarp -cutline <vg25_utm32.geojson> -cwhere "ags='XX"`
  schneidet je Bundesland direkt gegen die VG25 – die Maske bildet GDAL
  intern, Python parst keine Geometrien.
- **Koordinatentransformation**: Eckpunkte der UTM33-Kacheln werden per
  `gdaltransform` (stdin/stdout, ein Aufruf pro Kachel) nach UTM32
  transformiert, um die Zielkacheln zu bestimmen.

## Eingabeformat

```
<ROOT_INPUT_DIR>\
    s32_283\dgm1_32_283_5651_1_be_2025.tif     (UTM32, bleibt unberührt)
    s33_372\dgm1_33_372_5805_1_be_2025.tif     (UTM33, wird gewarpt)
```

## Ausgabeformat

```
<OUTPUT_DIR>\
    s32_283\dgm1_32_283_5651_1.tif
    s32_714\...
```

## Performance-Tuning (Threadripper PRO 5955WX, 128 GB RAM)

```python
WORKERS = max(1, min(48, (os.cpu_count() or 8) // 2))  # -> 32 auf 5955WX
GDAL_WARP_THREADS = 2        # -wo NUM_THREADS je gdalwarp
GDAL_COMPRESS_THREADS = 2    # -co NUM_THREADS (DEFLATE)
GDAL_CACHEMAX_MB = 512       # Cache je Prozess (32*512MB = 16 GB)
```

- `WORKERS = 32` (Automatik) als Startpunkt; bei reiner CPU-Last bis 48–64
- Kompression: `DEFLATE` + `PREDICTOR=3` (Floating-Point-Prädiktor,
  korrekt für float32-Höhen) – 20–35 % kleiner, konfigurierbar über
  `TIF_CREATE_OPTIONS`
- Tipp: `--tmp` auf eine andere physische Platte als die Eingabedaten legen

## Was der Workflow intern beachtet

- **Zonengrenzen-Trapez**: UTM33-rw 292 ≠ UTM32-rw 293 – die Zielkachel
  wird per `gdaltransform` aus der echten Georeferenzierung bestimmt;
  eine Quellkachel wird auf jede überlappte Zielkachel gewarpt
- **Quell-Trennung**: Zwischendateien tragen die Quellkachel-Identität
  im Namen (kein Überschreiben an der Zonengrenze)
- **Fehlende Partnerkacheln** werden gezählt und gemeldet (kein Abbruch)
- **Parallelisierung**: Warp + Harmonisierung über ThreadPool mit
  `WORKERS` parallelen GDAL-Prozessen; thread-sichere Statusleiste
