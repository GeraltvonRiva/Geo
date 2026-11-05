# DGM1 UTM32-Harmonisierungs-Workflow

Harmonisiert DGM1-Kacheln (1×1 km, ADV-Schema, ETRS89) aus 16 Bundesländern
zu einem vollständigen, lückenlosen Datensatz in **UTM32N (EPSG:25832)** –
inklusive Nahtloser Zusammenführung an Landesgrenzen (VG25) und
Zonengrenzen (UTM33 → UTM32).

**Eine einzige Datei: `utm32_workflow.py`** – reine Python-Standardbibliothek
+ GDAL-Binarys (z.B. aus QGIS). Keine pip-Pakete, keine GDAL-Python-Bindings.

## Features

- **Inventur**: Liest alle DGM1-Kacheln aus Streifenordnern; Zone und
  Bundesland (AdV-Kürzel wie `be`, `nw` im Dateinamen-Suffix) werden
  automatisch erkannt
- **Umprojektion**: UTM33-Kacheln per `gdalwarp` exakt aufs UTM32-1-km-Raster
  (`-te`/`-tr`, Nearest-Neighbor für originalgetreue Höhen); UTM32-Kacheln
  bleiben unberührt. An der Zonengrenze wird jede Quellkachel auf jede
  überlappte Zielkachel gewarpt (Trapezüberlappung, viele-zu-viel)
- **Grenzkachel-Harmonisierung**: Jeder Landesbeitrag wird an seine
  VG25-Landesmaske geclippt (`gdalwarp -cutline -cwhere`) – Daten jenseits
  der VG-Grenze werden entfernt, die Kacheln werden an der Grenzlinie
  zusammengefügt. Erkennt auch Lieferungen, die über die Landesgrenze
  ins Nachbarland ragen
- **Pixel-Reparatur**: Kleine NoData-Fehlstellen (≤ 4 Pixel, z.B.
  VG-Linien-Pixel, die beiden Landesmasken entfielen) werden mit dem
  Mittel der 8-Nachbarn gefüllt; große Datenlücken bleiben unberührt
- **Reparaturprotokoll**: `pixelreparaturen.shp` (Punkt-Shapefile,
  EPSG:25832) + CSV mit Position, Kachel, Arbeitsschritt und gefülltem
  Wert jeder Reparatur
- **VG250-tauglich**: GPKG/Shapefile mit allen Verwaltungsebenen – die
  Länderebene (AGS 1–16) wird automatisch erkannt; Platzhalter-AGS wie
  `--------` werden gefiltert
- **Parallelisiert**: Thread-Pool mit konfigurierbarer Worker-Anzahl
  (abgestimmt auf Vielkern-CPUs wie Threadripper PRO 5955WX)
- **Statusleiste**: Prozent, Zähler, ETA, aktuelle Kachel – als
  ANSI-Balken oder Log-Zeilen
- **Wiederanlauffähig**: Vorhandene Zwischendateien werden übersprungen

## Voraussetzungen

- Python 3 (nur Standardbibliothek)
- GDAL-Binarys: `gdalwarp`, `gdalinfo`, `gdaltransform`, `gdal_translate`,
  `gdalbuildvrt`, `gdal_rasterize`, `ogr2ogr`, `ogrinfo`
  (alle in QGIS enthalten)

## Schnellstart

1. Konfiguration am Dateianfang anpassen (`GDAL_BIN_DIR`, `ROOT_INPUT_DIR`,
   `OUTPUT_DIR`, `VG25_PATH`, ggf. `VG25_LAYER`/`VG25_AGS_FIELD`)
2. Testlauf mit einem Bundesland:
   ```cmd
   python utm32_workflow.py --input D:\test_land --output D:\test_out --keep-intermediate --progress bar
   ```
3. Produktionslauf mit allen 16 Ländern (vorher Temp-Ordner leeren!)

## Eingabe

```
<ROOT_INPUT_DIR>\
    s32_283\dgm1_32_283_5651_1_be_2025.tif     (UTM32, bleibt unberührt)
    s33_372\dgm1_33_372_5805_1_be_2025.tif     (UTM33, wird gewarpt)
```

## Ausgabe

```
<OUTPUT_DIR>\
    s32_283\dgm1_32_283_5651_1.tif             (Streifenordner s32_<rw>)
    pixelreparaturen.shp / .csv               (Reparaturprotokoll)
```

## Projektstruktur

| Datei | Beschreibung |
|---|---|
| `utm32_workflow.py` | Der komplette Workflow (Einzeldatei) |
| `LOKALE_AUSFUEHRUNG.md` | Anleitung: Konfiguration, CLI, Performance-Tuning |
| `tests/` | End-to-End-Tests mit GDAL-CLI-Shims (rasterio-basiert, nur für Testumgebungen ohne echtes gdal-bin) |

## Lizenz / Datenquellen

- DGM1: Open Data der Landesvermessungsämter (Datenlizenz Deutschland)
- VG250: © GeoBasis-DE / BKG
