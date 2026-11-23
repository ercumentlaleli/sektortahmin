"""Kullanıcı Excel'inden ve IMF WEO'dan ileriye dönük varsayımların üretilmesi."""
from __future__ import annotations

import datetime as dt

import numpy as np
import openpyxl
import pandas as pd
from scipy.optimize import brentq


def _find_sheet(wb, prefix: str):
    for ws in wb.worksheets:
        if ws.title.strip().lower().startswith(prefix.lower()):
            return ws
    raise KeyError(f"'{prefix}' ile başlayan sayfa bulunamadı: {wb.sheetnames}")


def read_turkey_growth(xlsx_path: str, row: int = 26) -> pd.Series:
    """Macro Projections sayfası: 1. satır yıllar, 'GSYH büyüme' satırı (varsayılan 26)."""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = _find_sheet(wb, "Macro Projections")
    # Etiketle bul; bulunamazsa verilen satırı kullan
    for r in range(1, min(ws.max_row, 200) + 1):
        v = ws.cell(r, 1).value
        if isinstance(v, str) and "gsyh büyüme" in v.lower():
            row = r
            break
    out = {}
    for c in range(2, ws.max_column + 1):
        y, g = ws.cell(1, c).value, ws.cell(row, c).value
        if y is None or g is None:
            continue
        year = y.year if isinstance(y, (dt.date, dt.datetime)) else int(y)
        out[year] = float(g)
    s = pd.Series(out, name="TR_GSYH_BUYUME").sort_index()
    s.attrs["row"] = row
    return s


def read_monthly_cpi_usd(xlsx_path: str) -> pd.DataFrame:
    """Aylık Kur Tahminleri: A=tarih, B=TÜFE endeksi, H=Dolar/TL."""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = _find_sheet(wb, "Aylık Kur")
    rows = []
    for r in range(3, ws.max_row + 1):
        d, cpi, usd = ws.cell(r, 1).value, ws.cell(r, 2).value, ws.cell(r, 8).value
        if isinstance(d, (dt.date, dt.datetime)) and cpi is not None and usd is not None:
            rows.append((pd.Period(d, "M"), float(cpi), float(usd)))
    df = pd.DataFrame(rows, columns=["Ay", "TUFE", "USDTRY"]).set_index("Ay")
    return df


def extend_with_annual_growth(q: pd.Series, growth: pd.Series, end_year: int) -> pd.DataFrame:
    """Çeyreklik endeksi, yıllık ortalama büyüme hedeflerini sağlayacak şekilde uzatır.

    Bir yılın eksik çeyrekleri için sabit bir çeyreklik büyüme oranı g bulunur:
        sum(yıl çeyrekleri) / sum(önceki yıl çeyrekleri) = 1 + büyüme/100
    Dönüş: DataFrame[değer, kaynak('Gerçekleşen'/'Projeksiyon')]
    """
    q = q.dropna().copy()
    src = pd.Series("Gerçekleşen", index=q.index)
    last = q.index[-1]
    year = last.year if last.quarter < 4 else last.year + 1
    while year <= end_year:
        if year not in growth.index:
            raise KeyError(f"{year} için büyüme varsayımı yok ({growth.name}).")
        prev = q[[p for p in q.index if p.year == year - 1]]
        if len(prev) != 4:
            raise ValueError(f"{year-1} yılı tamamlanmamış.")
        known = q[[p for p in q.index if p.year == year]]
        missing = [pd.Period(f"{year}Q{k}", "Q") for k in range(1, 5) if pd.Period(f"{year}Q{k}", "Q") not in q.index]
        target = prev.sum() * (1 + growth[year] / 100.0)
        L = q.iloc[-1]
        m = len(missing)

        def f(g):
            return known.sum() + sum(L * (1 + g) ** k for k in range(1, m + 1)) - target

        g = brentq(f, -0.5, 0.5)
        for k, p in enumerate(missing, start=1):
            q.loc[p] = L * (1 + g) ** k
            src.loc[p] = "Projeksiyon"
        q = q.sort_index()
        year += 1
    src = src.reindex(q.index)
    return pd.DataFrame({"deger": q, "kaynak": src})


def quarterly_mean_complete(m: pd.Series) -> pd.Series:
    """Aylık seriden yalnızca 3 ayı tamam olan çeyreklerin ortalaması."""
    g = m.groupby(m.index.asfreq("Q"))
    out = g.mean()[g.count() == 3]
    return out


def project_reer(reer_q: pd.Series, cpi_usd_m: pd.DataFrame, end_year: int) -> pd.DataFrame:
    """REDK_t = REDK_{t-1} * (1 + TÜFE çeyreklik % değişim) / (1 + ortalama kur % değişim).

    TÜFE ve kur için çeyrek ortalamaları kullanılır.
    """
    cq = cpi_usd_m.groupby(cpi_usd_m.index.asfreq("Q")).mean()
    cq = cq[cpi_usd_m.groupby(cpi_usd_m.index.asfreq("Q")).size() == 3]
    pi = cq["TUFE"].pct_change()
    e = cq["USDTRY"].pct_change()
    r = reer_q.dropna().copy()
    rows = [{"Ceyrek": p, "REDK": v, "kaynak": "Gerçekleşen", "TUFE_ceyreklik_%": np.nan, "USD_ceyreklik_%": np.nan}
            for p, v in r.items()]
    p = r.index[-1] + 1
    val = r.iloc[-1]
    while p.year <= end_year:
        if p not in pi.index or pd.isna(pi.get(p)) or pd.isna(e.get(p)):
            raise KeyError(f"{p} için TÜFE/kur verisi Excel'de yok.")
        val = val * (1 + pi[p]) / (1 + e[p])
        rows.append({"Ceyrek": p, "REDK": val, "kaynak": "Projeksiyon",
                     "TUFE_ceyreklik_%": pi[p] * 100, "USD_ceyreklik_%": e[p] * 100})
        p += 1
    return pd.DataFrame(rows).set_index("Ceyrek")


def monthly_to_quarterly_partial(m: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aylık -> çeyreklik ortalama. Çeyrekte açıklanan ay sayısı kadar ortalama alınır
    (3 ay varsa 3'ünün, 2 ay varsa 2'sinin, 1 ay varsa tek ayın değeri)."""
    qi = m.index.asfreq("Q")
    mean = m.groupby(qi).mean()
    cnt = m.groupby(qi).count()
    return mean, cnt


def read_monthly_columns(xlsx_path: str, cols: dict[str, int]) -> pd.DataFrame:
    """Aylık Kur Tahminleri sayfasından A sütunu (tarih) ve istenen sütunları okur. cols: {ad: sütun no}."""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = _find_sheet(wb, "Aylık Kur")
    rows = []
    for r in range(3, ws.max_row + 1):
        d = ws.cell(r, 1).value
        vals = [ws.cell(r, c).value for c in cols.values()]
        if isinstance(d, (dt.date, dt.datetime)) and all(isinstance(v, (int, float)) for v in vals):
            rows.append((pd.Period(d, "M"), *map(float, vals)))
    return pd.DataFrame(rows, columns=["Ay", *cols]).set_index("Ay")


def splice_monthly(actual: pd.Series, excel: pd.Series) -> pd.DataFrame:
    """Gerçekleşen aylık seriyi, son gerçekleşen aydan sonra Excel'deki aylık % değişimlerle uzatır."""
    actual = actual.dropna().sort_index()
    excel = excel.dropna().sort_index()
    last = actual.index[-1]
    val = actual.copy().astype(float)
    src = pd.Series("Gerçekleşen", index=actual.index)
    fut = excel[excel.index > last]
    if len(fut):
        if last not in excel.index:
            raise KeyError(f"Excel'de {last} ayı yok; zincirleme yapılamıyor.")
        growth = excel.loc[last:].pct_change().iloc[1:]
        level = actual.iloc[-1]
        for p, g in growth.items():
            level *= 1 + g
            val.loc[p] = level
            src.loc[p] = "Projeksiyon (Excel)"
    return pd.DataFrame({"deger": val, "kaynak": src}).sort_index()


def quarterly_from_monthly_path(m: pd.DataFrame, last_actual_month: pd.Period) -> pd.DataFrame:
    """Gerçekleşen+projeksiyon aylık yolundan tam çeyrek ortalamaları; kaynak: çeyreğin tüm ayları
    gerçekleşense 'Gerçekleşen', değilse 'Projeksiyon'."""
    qi = m.index.asfreq("Q")
    g = m["deger"].groupby(qi)
    q = g.mean()[g.count() == 3]
    lastq = last_actual_month.asfreq("Q") if last_actual_month.month % 3 == 0 else last_actual_month.asfreq("Q") - 1
    src = pd.Series(["Gerçekleşen" if p <= lastq else "Projeksiyon" for p in q.index], index=q.index)
    return pd.DataFrame({"deger": q, "kaynak": src})
