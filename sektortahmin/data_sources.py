"""Veri kaynakları: TÜİK SDMX, TCMB EVDS, IMF SDMX.

API anahtarları ortam değişkenlerinden okunur:
    TUIK_API_KEY  (veya TUIK_SDMX_API_KEY)
    EVDS_API_KEY
IMF API anahtar gerektirmez.
"""
from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pandas as pd
import requests

TUIK_TOKEN_URL = "https://giris.tuik.gov.tr/realms/web/protocol/openid-connect/token"
TUIK_BASE = "https://nsiws.tuik.gov.tr/rest/"
EVDS_BASE = "https://evds3.tcmb.gov.tr/igmevdsms-dis/"
IMF_BASE = "https://api.imf.org/external/sdmx/2.1/"

# Sanayi üretim endeksi dataflow'ları (2021=100)
IPI_FLOWS = {
    2: ("DF_SANAYI_URETIM_ENDEKS_ANA_C", "5"),  # FAAL_GRUP 5 = Bölüm (2'li)
    3: ("DF_SANAYI_URETIM_ENDEKS_ALT_C", "6"),  # FAAL_GRUP 6 = Grup (3'lü)
}
SEASONAL_SA_CA = "Y"   # Mevsim ve takvim etkisinden arındırılmış
DEGISIM_INDEX = "1"    # Endeks düzeyi

EVDS_GDP_SA = "TP.GSYIH30.HY.B1GQ"  # GSYH, mevsim ve takvim etkisinden arındırılmış zincirlenmiş hacim endeksi
EVDS_REER_CPI = "TP.RK.T1.Y"        # TÜFE bazlı reel efektif döviz kuru


def _get(url, *, headers=None, params=None, timeout=300, tries=4):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            return r
        except (requests.RequestException,) as e:  # ağ hatası: tekrar dene
            last = e
            time.sleep(2 ** i)
    raise RuntimeError(f"İstek başarısız: {url} ({last})")


# ---------------------------------------------------------------- TÜİK
def tuik_token() -> str:
    key = os.environ.get("TUIK_API_KEY") or os.environ.get("TUIK_SDMX_API_KEY")
    if not key:
        raise RuntimeError("TUIK_API_KEY ortam değişkeni tanımlı değil.")
    r = requests.post(
        TUIK_TOKEN_URL,
        data={"grant_type": "password", "client_id": "nsi-ws-consumer", "api_key": key},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def tuik_ipi_monthly(level: int, start: str = "2010-01", token: str | None = None) -> pd.DataFrame:
    """Mevsim ve takvim etkisinden arındırılmış SÜE (endeks) - aylık, geniş format.

    Sütunlar NACE kodu, indeks aylık PeriodIndex.
    """
    flow, faal = IPI_FLOWS[level]
    token = token or tuik_token()
    h = {"Authorization": "Bearer " + token, "Accept": "application/vnd.sdmx.data+csv;version=2.0"}
    r = _get(TUIK_BASE + f"data/{flow}/all", headers=h, params={"startPeriod": start}, timeout=900)
    r.raise_for_status()
    d = pd.read_csv(io.StringIO(r.text), dtype=str)
    d = d[(d.SEASONAL_ADJUST == SEASONAL_SA_CA) & (d.DEGISIM_TUR == DEGISIM_INDEX) & (d.FAAL_GRUP == faal)]
    if d.empty:
        raise RuntimeError(f"{flow}: filtre sonrası veri yok.")
    # Birden fazla yayım dönemi olursa en sonuncusunu al
    d = d.sort_values("YAYIM_DONEMI").drop_duplicates(["ACTIVITY_NACE_REV2", "TIME_PERIOD"], keep="last")
    d["OBS_VALUE"] = pd.to_numeric(d["OBS_VALUE"], errors="coerce")
    w = d.pivot(index="TIME_PERIOD", columns="ACTIVITY_NACE_REV2", values="OBS_VALUE")
    w.index = pd.PeriodIndex(w.index, freq="M")
    return w.sort_index()


def tuik_nace_names(token: str | None = None) -> dict[str, str]:
    token = token or tuik_token()
    h = {"Authorization": "Bearer " + token, "Accept": "application/vnd.sdmx.structure+json;version=1.0"}
    r = _get(TUIK_BASE + "codelist/TR/CL_NACE_REV2_364_HIYERARSIK/latest", headers=h, timeout=300)
    names = {}
    try:
        for cl in r.json()["data"]["codelists"]:
            for c in cl["codes"]:
                n = c.get("names", {})
                names[c["id"]] = n.get("tr") or n.get("en") or c.get("name", "")
    except Exception:
        pass
    return names


# ---------------------------------------------------------------- EVDS
def evds_series(code: str, start="01-01-1995", end="31-12-2100", frequency: str | None = None,
                aggregation: str | None = None) -> pd.Series:
    key = os.environ.get("EVDS_API_KEY")
    if not key:
        raise RuntimeError("EVDS_API_KEY ortam değişkeni tanımlı değil.")
    url = EVDS_BASE + f"series={code}&startDate={start}&endDate={end}&type=json"
    if frequency:
        url += f"&frequency={frequency}"
    if aggregation:
        url += f"&aggregationTypes={aggregation}"
    r = _get(url, headers={"key": key}, timeout=120)
    r.raise_for_status()
    items = r.json()["items"]
    col = code.replace(".", "_")
    rows = [(it["Tarih"], it.get(col)) for it in items]
    s = pd.Series({t: (float(v) if v not in (None, "", "null") else None) for t, v in rows}, dtype=float)
    return s.dropna()


def evds_gdp_sa_quarterly() -> pd.Series:
    s = evds_series(EVDS_GDP_SA)
    s.index = pd.PeriodIndex([t.replace("-", "") for t in s.index], freq="Q")
    s.name = "GSYH_SA"
    return s.sort_index()


def evds_reer_monthly() -> pd.Series:
    s = evds_series(EVDS_REER_CPI)
    s.index = pd.PeriodIndex(pd.to_datetime(s.index, format="%Y-%m"), freq="M")
    s.name = "REDK"
    return s.sort_index()


# ---------------------------------------------------------------- IMF
def _imf_csv(path: str) -> pd.DataFrame:
    r = _get(IMF_BASE + path, headers={"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"}, timeout=300)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text), dtype=str)


def imf_world_gdp_quarterly() -> pd.Series:
    """IMF QGDP_WCA: Dünya (G001) GSYH, sabit fiyatlar, mevsimsellikten arındırılmış endeks."""
    d = _imf_csv("data/IMF.STA,QGDP_WCA/G001.B1GQ_S1_Q.IX.Q")
    s = pd.Series(pd.to_numeric(d.OBS_VALUE).values,
                  index=pd.PeriodIndex([t.replace("-", "") for t in d.TIME_PERIOD], freq="Q"),
                  name="DUNYA_GSYH").sort_index()
    s.attrs["publication_date"] = d.get("PUBLICATION_DATE", pd.Series([""])).iloc[0]
    return s


def imf_weo_world_growth() -> tuple[pd.Series, str]:
    """En son WEO: Dünya reel GSYH büyümesi (%), yıllık. (seri, yayım tarihi)"""
    d = _imf_csv("data/IMF.RES,WEO/G001.NGDP_RPCH.A?startPeriod=2000")
    s = pd.Series(pd.to_numeric(d.OBS_VALUE).values, index=d.TIME_PERIOD.astype(int), name="WEO_DUNYA_BUYUME")
    pub = d.get("PUBLICATION_DATE", pd.Series([""])).iloc[0]
    return s.sort_index(), pub


def save_cache(obj, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(obj, (pd.Series, pd.DataFrame)):
        obj.to_csv(path)
    else:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
