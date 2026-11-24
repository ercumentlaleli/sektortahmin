"""Birim kök testleri (ADF, PP), tahmin performansı istatistikleri ve zaman serisi çapraz doğrulaması."""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller

ALPHA = 0.05
CV_ORIGINS = 12   # çapraz doğrulama başlangıç noktası sayısı (son 3 yıl, çeyreklik)
CV_H = 4          # tahmin ufku (çeyrek)
CV_MIN_OBS = 30   # bir başlangıç noktasında asgari tahmin gözlemi


# ------------------------------------------------------------------ birim kök
def _pp(x: np.ndarray, trend: str):
    from arch.unitroot import PhillipsPerron
    t = PhillipsPerron(x, trend=trend)
    return float(t.stat), float(t.pvalue), int(t.lags)


def unit_root_table(series: dict[str, pd.Series]) -> pd.DataFrame:
    """Her seri için log düzey (sabit + trend) ve Δlog (sabit) üzerinde ADF ve PP testleri.

    series: {etiket: pozitif düzey serisi}. H0: birim kök var (durağan değil).
    """
    rows = []
    for name, s in series.items():
        s = s.dropna()
        s = s[s > 0]
        if len(s) < 20:
            continue
        for tr_name, x, reg in [("log düzey", np.log(s).values, "ct"), ("Δlog", np.log(s).diff().dropna().values, "c")]:
            det = "sabit + trend" if reg == "ct" else "sabit"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    st, p, lags, *_ = adfuller(x, regression=reg, autolag="AIC")
                    rows.append({"Değişken": name, "Dönüşüm": tr_name, "Test": "ADF", "Deterministik": det,
                                 "İstatistik": float(st), "p-değeri": float(p), "Gecikme": int(lags)})
                except Exception:
                    pass
                try:
                    st, p, lags = _pp(x, reg)
                    rows.append({"Değişken": name, "Dönüşüm": tr_name, "Test": "PP", "Deterministik": det,
                                 "İstatistik": st, "p-değeri": p, "Gecikme": lags})
                except Exception:
                    pass
    df = pd.DataFrame(rows)
    if len(df):
        df["Sonuç (%5)"] = np.where(df["p-değeri"] < ALPHA, "Durağan", "Birim kök (durağan değil)")
    return df


def unit_root_summary(t: pd.DataFrame) -> str:
    """Kısa yorum: Δlog durağan mı (ARDL farklarda kurulduğu için gerekli)."""
    if t is None or t.empty:
        return "Hesaplanamadı"
    d = t[t["Dönüşüm"] == "Δlog"]
    lv = t[t["Dönüşüm"] == "log düzey"]
    d_ok = (d["p-değeri"] < ALPHA).all()
    lv_ur = (lv["p-değeri"] >= ALPHA).any()
    if d_ok and lv_ur:
        return "I(1): düzey birim köklü, Δlog durağan (ADF ve PP) – farklarla model uygun"
    if d_ok:
        return "Δlog durağan (ADF ve PP); düzey de durağan görünebilir"
    return "Uyarı: Δlog için en az bir testte birim kök reddedilemedi"


# ------------------------------------------------------------------ performans istatistikleri
def error_stats(err: np.ndarray, err_naive: np.ndarray | None = None, actual_level=None, fc_level=None) -> dict:
    e = np.asarray(err, float)
    e = e[~np.isnan(e)]
    if not len(e):
        return {}
    out = {"N": int(len(e)), "ME (sapma)": float(e.mean()), "MAE": float(np.abs(e).mean()),
           "RMSE": float(np.sqrt((e ** 2).mean()))}
    if err_naive is not None:
        en = np.asarray(err_naive, float)
        en = en[~np.isnan(en)]
        rn = float(np.sqrt((en ** 2).mean())) if len(en) else np.nan
        out["Theil U (naif'e göre)"] = out["RMSE"] / rn if rn else np.nan
    if actual_level is not None and fc_level is not None:
        a, f = np.asarray(actual_level, float), np.asarray(fc_level, float)
        ok = ~np.isnan(a) & ~np.isnan(f) & (a != 0)
        out["MAPE (düzey, %)"] = float(np.mean(np.abs((f[ok] - a[ok]) / a[ok])) * 100) if ok.any() else np.nan
    return out


# ------------------------------------------------------------------ çapraz doğrulama
def _forecast_path(df: pd.DataFrame, p: int, q: dict, params: pd.Series, origin, H: int) -> np.ndarray:
    path = df["dly"].copy()
    path.loc[path.index > origin] = np.nan
    out = []
    for h in range(1, H + 1):
        t = origin + h
        v = params.get("const", 0.0)
        for i in range(1, p + 1):
            v += params[f"dly(-{i})"] * path[t - i]
        for x, qq in q.items():
            for j in range(qq + 1):
                v += params[f"{x}" + (f"(-{j})" if j else "")] * df.loc[t - j, x]
        path[t] = v
        out.append(v)
    return np.array(out)


def rolling_origin_cv(df: pd.DataFrame, est_idx: pd.PeriodIndex, x_cols, select_fn, design_fn, maxlag: int,
                      n_origins: int = CV_ORIGINS, H: int = CV_H) -> pd.DataFrame:
    """Genişleyen pencereli, kaydırmalı başlangıç noktalı çapraz doğrulama (time-series CV).

    Her başlangıç noktası T için: yalnızca T'ye kadarki veriyle gecikmeler AIC ile yeniden seçilir,
    model yeniden tahmin edilir, T+1..T+H dinamik olarak tahmin edilir. Açıklayıcıların gerçekleşen
    değerleri kullanılır (koşullu / ex-post tahmin). Naif ölçüt: T'ye kadarki ortalama Δlog (sürüklenmeli rassal yürüyüş).
    """
    import statsmodels.api as sm

    est_end = est_idx[-1]
    origins = [o for o in est_idx[-(n_origins + 1):-1]]
    rows = []
    for o in origins:
        est = est_idx[est_idx <= o]
        if len(est) - maxlag < CV_MIN_OBS:
            continue
        p, q, _ = select_fn(df.loc[est], x_cols)
        D = design_fn(df, p, q)
        rr = est[maxlag:]
        Xe, ye = D.loc[rr], df.loc[rr, "dly"]
        ok = Xe.notna().all(axis=1) & ye.notna()
        res = sm.OLS(ye[ok].values, Xe[ok].values).fit()
        params = pd.Series(res.params, index=Xe.columns)
        h_max = min(H, (est_end - o).n)
        f = _forecast_path(df, p, q, params, o, h_max)
        drift = df.loc[est, "dly"].mean()
        for h in range(1, h_max + 1):
            a = df.loc[o + h, "dly"]
            rows.append({"Başlangıç": str(o), "Hedef çeyrek": str(o + h), "h": h,
                         "Model": f"ARDL({p}," + ",".join(str(q[x]) for x in x_cols) + ")",
                         "Gerçekleşen Δlog": a, "Tahmin Δlog": f[h - 1], "Naif Δlog": drift,
                         "Hata": f[h - 1] - a, "Naif hata": drift - a,
                         "Kümülatif gerçekleşen": df.loc[o + 1:o + h, "dly"].sum(),
                         "Kümülatif tahmin": f[:h].sum(), "Kümülatif naif": drift * h})
    return pd.DataFrame(rows)


def cv_metrics(cv: pd.DataFrame, H: int = CV_H) -> pd.DataFrame:
    """Ufuk bazında çapraz doğrulama metrikleri (çeyreklik Δlog, yüzde puan) + 4 çeyreklik kümülatif."""
    if cv is None or cv.empty:
        return pd.DataFrame()
    rows = []
    for h in range(1, H + 1):
        c = cv[cv["h"] == h]
        if c.empty:
            continue
        st = error_stats(c["Hata"].values * 100, c["Naif hata"].values * 100)
        rows.append({"Ölçüt": f"Örneklem dışı, h={h} çeyreklik değişim (puan)", **st})
    c = cv[cv["h"] == H]
    if len(c):
        e = (c["Kümülatif tahmin"] - c["Kümülatif gerçekleşen"]).values * 100
        en = (c["Kümülatif naif"] - c["Kümülatif gerçekleşen"]).values * 100
        rows.append({"Ölçüt": f"Örneklem dışı, {H} çeyreklik kümülatif değişim (puan)", **error_stats(e, en)})
    return pd.DataFrame(rows)
