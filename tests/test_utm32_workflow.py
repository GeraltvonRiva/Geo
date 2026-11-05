"""End-to-End-Test für utm32_workflow.py (mit GDAL-CLI-Shims).

Szenario: NRW (AGS 5, UTM32-Datensatz) + Berlin (AGS 11, UTM33-Datensatz)
mit gemeinsamer VG-Grenze bei x=714500. Die NRW-Lieferung reicht mit einer
Kachel ins Berliner Territorium (715) – diese Fremddaten müssen
weggeclippt werden. Eine Eingangskachel enthält einen Fehlpixel, der vom
Fill-Algorithmus repariert und protokolliert werden muss.

Voraussetzung: rasterio im Test-Pfad (nur für die Shims; der Workflow
selbst braucht nur die Standardbibliothek).
"""

import json
import os
import subprocess
import sys
import tempfile

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin


def _write_tile(root, zone, rw, hw, val, abbr, hole=None):
    epsg = 25832 if zone == 32 else 25833
    d = os.path.join(root, f"s{zone}_{rw}")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"dgm1_{zone}_{rw}_{hw}_1_{abbr}_2025.tif")
    arr = np.full((1000, 1000), val, dtype=np.float32)
    if hole:
        arr[hole] = -99999.0
    prof = dict(driver="GTiff", width=1000, height=1000, count=1,
                dtype="float32", crs=rasterio.crs.CRS.from_epsg(epsg),
                transform=from_origin(rw * 1000, (hw + 1) * 1000, 1, 1),
                nodata=-99999.0)
    with rasterio.open(p, "w", **prof) as ds:
        ds.write(arr, 1)


def _poly(x0, y0, x1, y1):
    return {"type": "Polygon",
            "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


@pytest.fixture
def scenario(tmp_path):
    root = os.path.join(str(tmp_path), "input")
    os.makedirs(root, exist_ok=True)
    # NRW (UTM32) + Berlin (UTM33); NRW-Kachel 715 liegt jenseits der
    # VG-Grenze in Berliner Territorium; NRW-Kachel 713/5651 hat einen
    # Fehlpixel bei (500, 500)
    for hw in (5650, 5651, 5652):
        _write_tile(root, 32, 713, hw, 10.0, "nw",
                    hole=(500, 500) if hw == 5651 else None)
        _write_tile(root, 32, 714, hw, 10.0, "nw")
        _write_tile(root, 32, 715, hw, 10.0, "nw")
        _write_tile(root, 33, 292, hw, 20.0, "be")
        _write_tile(root, 33, 293, hw, 20.0, "be")

    # VG25 mit problematischen Werten: '--------' (Platzhalter),
    # 8-stelliger Gemeinde-AGS, und das Feld heißt 'ars'
    vg = os.path.join(str(tmp_path), "vg25.geojson")
    feats = [
        {"type": "Feature", "properties": {"ars": "05"},
         "geometry": _poly(710000, 5650000, 714500, 5660000)},
        {"type": "Feature", "properties": {"ars": "11"},
         "geometry": _poly(714500, 5650000, 720000, 5660000)},
        {"type": "Feature", "properties": {"ars": "--------"},
         "geometry": _poly(718000, 5658000, 719000, 5659000)},
        {"type": "Feature", "properties": {"ars": "05334002"},
         "geometry": _poly(711000, 5651000, 712000, 5652000)},
    ]
    with open(vg, "w") as fh:
        json.dump({"type": "FeatureCollection",
                   "crs": {"type": "name",
                            "properties": {"name": "urn:ogc:def:crs:EPSG::25832"}},
                   "features": feats}, fh)
    return root, vg


def test_end_to_end(scenario, tmp_path):
    root, vg = scenario
    out = os.path.join(str(tmp_path), "out")
    tmpdir = os.path.join(str(tmp_path), "tmpwork")
    shims = os.path.join(os.path.dirname(__file__), "shims")
    env = dict(os.environ, PATH=shims + os.pathsep + os.environ.get("PATH", ""))

    r = subprocess.run(
        [sys.executable, "-c",
         "import utm32_workflow as w; w.VG25_AGS_FIELD='ars';\n"
         "import sys; sys.exit(w.main(['--input', sys.argv[1], "
         "'--output', sys.argv[2], '--vg25', sys.argv[3], "
         "'--tmp', sys.argv[4], '--progress', 'log']))",
         root, out, vg, tmpdir],
        capture_output=True, text=True, env=env,
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )
    assert r.returncode == 0, r.stdout + r.stderr

    # Grenzkachel 714/5651: Westteil NRW (10), Ostteil Berlin (20)
    t = os.path.join(out, "s32_714", "dgm1_32_714_5651_1.tif")
    assert os.path.exists(t)
    with rasterio.open(t) as ds:
        a = ds.read(1)
        assert ds.crs.to_epsg() == 25832
    mid = a.shape[1] // 2
    assert np.allclose(a[:, : mid - 20], 10.0)
    assert (a[:, mid + 20:] == 20.0).mean() > 0.4

    # Kachel 715/5651 liegt laut VG25 komplett in Berlin; die NRW-
    # Fremddaten muessen komplett weggeclippt sein (kein Wert 10)
    t = os.path.join(out, "s32_715", "dgm1_32_715_5651_1.tif")
    assert os.path.exists(t)
    with rasterio.open(t) as ds:
        a = ds.read(1)
    assert not np.any(a == 10.0), "NRW-Daten ragen ins Berliner Territorium"

    # Fehlpixel in 713/5651 wurde repariert
    t = os.path.join(out, "s32_713", "dgm1_32_713_5651_1.tif")
    with rasterio.open(t) as ds:
        a = ds.read(1)
    assert a[500, 500] == 10.0, "Fehlpixel wurde nicht repariert"

    # Reparaturprotokoll (CSV + Shapefile) existiert mit korrektem Punkt
    csv_p = os.path.join(out, "pixelreparaturen.csv")
    shp_p = os.path.join(out, "pixelreparaturen.shp")
    assert os.path.exists(csv_p)
    assert os.path.exists(shp_p)
    with open(csv_p) as fh:
        lines = [l.strip() for l in fh if l.strip()]
    assert lines[0] == "x,y,kachel,schritt,wert"
    assert len(lines) >= 2
