"""ARDL modelleme, tanı testleri ve tahmin."""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats
from statsmodels.stats.diagnostic import (acorr_breusch_godfrey, het_arch, het_breuschpagan,
                                          het_white, linear_reset)
from statsmodels.stats.stattools import jarque_bera
import itertools

X_COLS = ["dl_GSYH", "dl_DUNYA", "dl_REDK"]
X_LABELS = {"dl_GSYH": "Türkiye GSYH (MA)", "dl_DUNYA": "Küresel GSYH (IMF)", "dl_REDK": "REDK (TÜFE bazlı)"}
MAXLAG = 4


@dataclass(frozen=True)
class ModelSpec:
    """Bir model setinin (bağımlı değişken + açıklayıcılar) tanımı."""
    key: str                 # "sue" / "ufe"
    title: str               # arayüzde gösterilen ad
    dep_short: str           # "SÜE" / "Yİ-ÜFE"
    dep_source: str          # veri kaynağı açıklaması
    dep_unit: str            # aylık sayfa sütun adı
    x_cols: tuple
    x_labels: dict
    anchor_col: str          # uzun vade yakınsamasında hedefin bağlandığı açıklayıcı
    anchor_label: str
    word: str                # "büyüme" / "değişim"
    sample_start: str        # bağımlı değişken için en erken çeyrek
    sa_note: str             # mevsimsellik arındırması gerekmezse yazılacak not
    partial_as_estimate: bool = False  # True: eksik aylı çeyrek doldurulmuş tahmin sayılır (gerçekleşen değil)


SUE_SPEC = ModelSpec(
    key="sue", title="Sanayi üretimi (SÜE)", dep_short="SÜE",
    dep_source="TÜİK SDMX – SÜE (2021=100), mevsim ve takvim etkisinden arındırılmış",
    dep_unit="SÜE (MA, 2021=100)",
    x_cols=tuple(X_COLS), x_labels=X_LABELS, anchor_col="dl_GSYH", anchor_label="TR GSYH büyümesi",
    word="büyüme", sample_start="2011Q1", sa_note="Gerek yok (seri zaten MA)")

UFE_SPEC = ModelSpec(
    key="ufe", title="Yurt içi üretici fiyatları (Yİ-ÜFE)", dep_short="Yİ-ÜFE",
    dep_source="TÜİK SDMX – Yurt İçi Üretici Fiyat Endeksi (2003=100), NACE Rev.2",
    dep_unit="Yİ-ÜFE (2003=100)",
    x_cols=("dl_YIUFE", "dl_USD", "dl_BRENT"),
    x_labels={"dl_YIUFE": "Yİ-ÜFE genel (Excel E)", "dl_USD": "Dolar/TL (Excel H)", "dl_BRENT": "Brent petrol (USD, EVDS)"},
    anchor_col="dl_YIUFE", anchor_label="Yİ-ÜFE genel değişimi",
    word="değişim", sample_start="2005Q1", sa_note="Gerek yok (mevsimsellik bulunmadı)",
    partial_as_estimate=True)
# Opsiyonel COVID-19 impuls kuklaları (varsayılan: kapalı). run_monthly.py --covid-dummy ile
# ["2020Q2", "2020Q3"] olarak açılır; açıkken her aday modelde sabit yer alır, tahmin döneminde 0'dır.
COVID_PERIODS = ["2020Q2", "2020Q3"]
DUMMY_PERIODS: list[str] = []
# Sabit terim (True: modelde sabit var; False: sabitsiz)
USE_CONST = True

# Uzun vade yakınsaması (yargısal düzeltme, ARDL tahminine dokunmaz; ayrı seri olarak raporlanır):
#   büyüme_t = w_t * model_t + (1 - w_t) * hedef_t
#   hedef_t  = oran * Δlog(TR GSYH)_t,  oran = ort. Δlog(SÜE) / ort. Δlog(GSYH) (tarihsel), [0, 2] ile sınırlı
#   w_t = 1 (CONV_START öncesi), CONV_START'tan ufuk sonuna doğrusal olarak 0'a iner.
CONV_START = "2029Q1"
REL_START = "2013Q1"          # göreli büyüme oranı için tarihsel pencere başlangıcı
RATIO_BOUNDS = (0.0, 2.0)


def dummy_frame(index: pd.PeriodIndex) -> pd.DataFrame:
    return pd.DataFrame({f"D{p}": (index == pd.Period(p, "Q")).astype(float) for p in DUMMY_PERIODS}, index=index)

ALPHA = 0.05


# ------------------------------------------------------------------ mevsimsellik
def seasonality_test(s: pd.Series) -> dict:
    """Çeyrek kuklaları ile F testi ve Kruskal-Wallis testi (log-fark serisi üzerinde)."""
    s = s.dropna()
    q = s.index.quarter
    D = pd.get_dummies(q, prefix="Q", drop_first=True).astype(float)
    D.index = s.index
    res = sm.OLS(s.values, sm.add_constant(D.values)).fit()
    f = res.f_test(np.eye(D.shape[1] + 1)[1:])
    groups = [s[q == k].values for k in range(1, 5) if (q == k).sum() > 1]
    kw = stats.kruskal(*groups)
    fp = float(np.squeeze(f.pvalue))
    return {"F": float(np.squeeze(f.fvalue)), "F_p": fp, "KW": float(kw.statistic), "KW_p": float(kw.pvalue),
            "mevsimsel": bool(fp < ALPHA and kw.pvalue < ALPHA)}


def seasonal_factors(s: pd.Series) -> pd.Series:
    """Çeyrek ortalamasının genel ortalamadan sapması (log-fark cinsinden)."""
    s = s.dropna()
    m = s.groupby(s.index.quarter).mean()
    return m - m.mean()


def apply_factors(s: pd.Series, fac: pd.Series | None, sign=-1) -> pd.Series:
    if fac is None:
        return s
    return s + sign * np.array([fac.get(p.quarter, 0.0) for p in s.index])


# ------------------------------------------------------------------ ARDL
@dataclass
class ModelResult:
    code: str
    order: dict
    ols: object
    design_cols: list
    sample: tuple
    nobs: int
    aic: float
    diagnostics: pd.DataFrame
    season: dict
    fitted: pd.Series
    forecast: pd.DataFrame
    quarterly: pd.DataFrame
    annual: pd.DataFrame
    select_table: pd.DataFrame = field(default_factory=pd.DataFrame)
    convergence: dict = field(default_factory=dict)
    spec: ModelSpec = SUE_SPEC
    notes: list = field(default_factory=list)


def _design(df: pd.DataFrame, p: int, q: dict) -> pd.DataFrame:
    cols = {"const": pd.Series(1.0, index=df.index)} if USE_CONST else {}
    for c, v in dummy_frame(df.index).items():
        cols[c] = v
    for i in range(1, p + 1):
        cols[f"dly(-{i})"] = df["dly"].shift(i)
    for x, qq in q.items():
        for j in range(0, qq + 1):
            cols[f"{x}" + (f"(-{j})" if j else "")] = df[x].shift(j)
    return pd.DataFrame(cols)


def select_order_aic(est: pd.DataFrame, x_cols=tuple(X_COLS)):
    x_cols = list(x_cols)
    rows = est.index[MAXLAG:]
    yv = est.loc[rows, "dly"].values
    lagged = {}
    for i in range(1, MAXLAG + 1):
        lagged[("dly", i)] = est["dly"].shift(i).loc[rows].values
    for x in x_cols:
        for j in range(0, MAXLAG + 1):
            lagged[(x, j)] = est[x].shift(j).loc[rows].values
    n = len(yv)
    dums = [d for d in dummy_frame(rows).T.values if d.any()]
    out = []
    for p in range(1, MAXLAG + 1):
        for qs in itertools.product(range(MAXLAG + 1), repeat=len(x_cols)):
            cols = ([np.ones(n)] if USE_CONST else []) + dums + [lagged[("dly", i)] for i in range(1, p + 1)]
            for x, qq in zip(x_cols, qs):
                cols += [lagged[(x, j)] for j in range(qq + 1)]
            Xm = np.column_stack(cols)
            beta, *_ = np.linalg.lstsq(Xm, yv, rcond=None)
            ssr = float(((yv - Xm @ beta) ** 2).sum())
            k = Xm.shape[1]
            llf = -n / 2 * (np.log(2 * np.pi) + np.log(ssr / n) + 1)
            out.append(((p, *qs), -2 * llf / n + 2 * k / n, k))
    out.sort(key=lambda t: t[1])
    best = out[0][0]
    tab = pd.DataFrame({"Model (p, " + ", ".join("q_" + x[3:] for x in x_cols) + ")": [f"ARDL{o}" for o, _, _ in out[:10]],
                        "AIC (EViews ölçeği)": [a for _, a, _ in out[:10]],
                        "Parametre sayısı": [k for _, _, k in out[:10]]})
    return best[0], dict(zip(x_cols, best[1:])), tab


def diagnostics(res, X: np.ndarray, X_het: np.ndarray | None = None) -> pd.DataFrame:
    """X_het: değişen varyans testlerinde kullanılan regresörler (kuklalar hariç;
    impuls kuklalarında kalıntı tam sıfır olduğundan bu testleri bozar)."""
    X_het = X if X_het is None else X_het
    # Yardımcı regresyonlarda (BP, White) sabit terim bulunmalı
    if not np.allclose(X_het[:, 0], 1.0):
        X_het = np.column_stack([np.ones(len(X_het)), X_het])
    rows = []
    e = res.resid
    warnings.simplefilter("ignore", FutureWarning)

    def add(test, h0, stat, p):
        rows.append({"Test": test, "H0": h0, "İstatistik": stat, "p-değeri": p,
                     "Sonuç (%5)": ("Geçti" if p >= ALPHA else "Kaldı") if p == p else "Hesaplanamadı"})

    for lag in (1, 4):
        try:
            lm, lmp, fv, fp = acorr_breusch_godfrey(res, nlags=lag)
            add(f"Breusch-Godfrey LM (gecikme {lag})", "Otokorelasyon yok", lm, lmp)
        except Exception:
            add(f"Breusch-Godfrey LM (gecikme {lag})", "Otokorelasyon yok", np.nan, np.nan)
    try:
        lm, lmp, fv, fp = het_breuschpagan(e, X_het)
        add("Breusch-Pagan-Godfrey", "Sabit varyans", lm, lmp)
    except Exception:
        add("Breusch-Pagan-Godfrey", "Sabit varyans", np.nan, np.nan)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if X_het.shape[1] * (X_het.shape[1] + 1) / 2 < len(e) - 1:
                lm, lmp, fv, fp = het_white(e, X_het)
            else:  # çapraz terimler için gözlem yetersiz: kareler ile White (çapraz terimsiz)
                Z = np.column_stack([X_het, X_het[:, 1:] ** 2])
                lm, lmp, fv, fp = het_breuschpagan(e, Z)
        add("White", "Sabit varyans", lm, lmp)
    except Exception:
        add("White", "Sabit varyans", np.nan, np.nan)
    try:
        lm, lmp, fv, fp = het_arch(e, nlags=4)
        add("ARCH LM (gecikme 4)", "ARCH etkisi yok", lm, lmp)
    except Exception:
        add("ARCH LM (gecikme 4)", "ARCH etkisi yok", np.nan, np.nan)
    jb, jbp, skew, kurt = jarque_bera(e)
    add("Jarque-Bera", "Normal dağılım", float(jb), float(jbp))
    try:
        r = linear_reset(res, power=2, use_f=True)
        add("Ramsey RESET", "Doğru fonksiyonel biçim", float(r.fvalue), float(r.pvalue))
    except Exception:
        add("Ramsey RESET", "Doğru fonksiyonel biçim", np.nan, np.nan)
    return pd.DataFrame(rows)


def fit_and_forecast(code: str, y_level_q: pd.Series, n_months: pd.Series, X_dl: pd.DataFrame,
                     x_last_actual: pd.Period, horizon_end: pd.Period, spec: ModelSpec = SUE_SPEC) -> ModelResult:
    """y_level_q: çeyreklik SÜE düzeyi (gerçekleşen); X_dl: açıklayıcıların log-farkları
    (gerçekleşen + projeksiyon, horizon_end'e kadar)."""
    notes = []
    X_dl = X_dl[list(spec.x_cols)]
    y = y_level_q.dropna()
    dly_raw = np.log(y).diff()

    # Tahmin örneklemi: y ve tüm X'lerin gerçekleşen olduğu ortak dönem
    est_end = min(y.index[-1], x_last_actual)
    common_start = max(dly_raw.dropna().index[0], X_dl.dropna().index[0])
    est_idx = pd.period_range(common_start, est_end, freq="Q")

    # 3) mevsimsellik kontrolü (bağımlı değişken)
    season = seasonality_test(dly_raw.reindex(est_idx))
    fac = seasonal_factors(dly_raw.reindex(est_idx)) if season["mevsimsel"] else None
    if fac is not None:
        notes.append("Bağımlı değişkende kalıntı mevsimsellik bulundu; çeyrek etkileri arındırıldı.")
    dly = apply_factors(dly_raw, fac)

    full_idx = pd.period_range(common_start, horizon_end, freq="Q")
    df = pd.DataFrame({"dly": dly.reindex(full_idx)}).join(X_dl.reindex(full_idx))
    est = df.loc[est_idx]

    # 2) AIC ile gecikme seçimi (EViews tarzı): p = 1..4, her açıklayıcı için q = 0..4,
    #    tüm modeller aynı örneklemde (ilk MAXLAG gözlem tutulur) karşılaştırılır.
    p, q, sel_tab = select_order_aic(df.loc[est_idx], spec.x_cols)

    # Seçilen modeli aynı örneklemde (ilk MAXLAG gözlem tutularak) OLS ile tahmin et
    D = _design(df, p, q)
    est_rows = est_idx[MAXLAG:]
    Xe = D.loc[est_rows]
    ye = df.loc[est_rows, "dly"]
    ok = Xe.notna().all(axis=1) & ye.notna()
    Xe, ye = Xe[ok], ye[ok]
    Xe = Xe.loc[:, [c for c in Xe.columns if not c.startswith("D") or Xe[c].any()]]
    res = sm.OLS(ye.reset_index(drop=True).rename("dlog_SUE"), Xe.reset_index(drop=True)).fit()
    params = pd.Series(np.asarray(res.params), index=Xe.columns)
    het_cols = [c for c in Xe.columns if not (c.startswith("D") and c[1:] in DUMMY_PERIODS)]
    diag = diagnostics(res, Xe.values, Xe[het_cols].values)
    fitted = pd.Series(np.asarray(res.fittedvalues), index=Xe.index)

    # Tahmin: y'nin son gerçekleşeninden sonrası, dinamik
    path = df["dly"].copy()
    fc_idx = pd.period_range(y.index[-1] + 1, horizon_end, freq="Q")
    for t in fc_idx:
        row = {**({"const": 1.0} if USE_CONST else {}), **{c: 0.0 for c in params.index if c[1:] in DUMMY_PERIODS}}
        for i in range(1, p + 1):
            row[f"dly(-{i})"] = path[t - i]
        for x, qq in q.items():
            for j in range(0, qq + 1):
                row[f"{x}" + (f"(-{j})" if j else "")] = df.loc[t - j, x]
        path[t] = float(sum(params[k] * v for k, v in row.items()))
    # mevsim faktörlerini geri ekle (düzey için)
    fc_dly = apply_factors(path.loc[fc_idx], fac, sign=+1)
    fitted_raw = apply_factors(fitted, fac, sign=+1)

    # Uzun vade yakınsaması: hedef = sektörün tarihsel göreli büyümesi x TR GSYH büyümesi
    last_full_year = y.index[-1].year - (0 if y.index[-1].quarter == 4 else 1)
    win = pd.period_range(max(pd.Period(REL_START, "Q"), dly_raw.dropna().index[0]),
                          pd.Period(f"{last_full_year}Q4", "Q"), freq="Q")
    g_y, g_x = dly_raw.reindex(win).mean(), X_dl[spec.anchor_col].reindex(win).mean()
    ratio_raw = float(g_y / g_x) if g_x else np.nan
    ratio = float(np.clip(ratio_raw, *RATIO_BOUNDS)) if ratio_raw == ratio_raw else 1.0
    target = ratio * X_dl[spec.anchor_col].reindex(fc_idx)
    c0 = pd.Period(CONV_START, "Q")
    n_conv = max((horizon_end - c0).n + 1, 1)
    w = pd.Series([1.0 if t < c0 else max(0.0, 1 - ((t - c0).n + 1) / n_conv) for t in fc_idx], index=fc_idx)
    fc_conv = w * fc_dly + (1 - w) * target
    conv = {"Tarihsel pencere": f"{win[0]}–{win[-1]}", f"Ort. Δlog {spec.dep_short} (çeyreklik)": float(g_y),
            f"Ort. Δlog {spec.anchor_col[3:]} (çeyreklik)": float(g_x), "Göreli büyüme oranı (ham)": ratio_raw,
            "Göreli büyüme oranı (sınırlı)": ratio, "Yakınsama": f"{c0} – {horizon_end} (doğrusal)"}

    # Düzeyler
    lvl = y.copy().astype(float)
    lvl_c = y.copy().astype(float)
    for t in fc_idx:
        lvl.loc[t] = lvl[t - 1] * np.exp(fc_dly[t])
        lvl_c.loc[t] = lvl_c[t - 1] * np.exp(fc_conv[t])
    fitted_lvl = pd.Series({t: y[t - 1] * np.exp(fitted_raw[t]) for t in fitted_raw.index})

    qdf = pd.DataFrame(index=pd.period_range(y.index[0], horizon_end, freq="Q"))
    qdf["SÜE (gerçekleşen)"] = y
    qdf["Açıklanan ay sayısı"] = n_months
    qdf["dlog SÜE (gerçekleşen)"] = dly_raw
    qdf["dlog SÜE (model içi uyum)"] = fitted_raw
    qdf["dlog SÜE (tahmin)"] = fc_dly
    qdf["SÜE (gerçekleşen + tahmin)"] = lvl
    qdf["SÜE (model içi uyum)"] = fitted_lvl
    qdf["Yakınsama ağırlığı (model)"] = w
    qdf["dlog hedef (oran x GSYH)"] = target
    qdf["dlog SÜE (yakınsamalı tahmin)"] = fc_conv
    qdf["SÜE (gerçekleşen + yakınsamalı tahmin)"] = lvl_c
    qdf["Çeyreklik büyüme % (q/q)"] = (np.exp(qdf["dlog SÜE (gerçekleşen)"].fillna(qdf["dlog SÜE (tahmin)"])) - 1) * 100
    qdf["Yıllık büyüme % (y/y)"] = qdf["SÜE (gerçekleşen + tahmin)"].pct_change(4) * 100

    # 8) Yıllığa çevirme (yıllık ortalama endeks büyümesi)
    lv = qdf["SÜE (gerçekleşen + tahmin)"]
    annual_rows = []
    years = sorted(set(p_.year for p_ in lv.index))
    for yr in years[1:]:
        cur = [pd.Period(f"{yr}Q{k}", "Q") for k in range(1, 5)]
        prv = [pd.Period(f"{yr-1}Q{k}", "Q") for k in range(1, 5)]
        if not all(c in lv.index and pd.notna(lv[c]) for c in cur + prv):
            continue
        if spec.partial_as_estimate:  # eksik aylı (doldurulmuş) çeyrek gerçekleşen sayılmaz
            n_act = sum(c in y.index and n_months.get(c, 3) >= 3 for c in cur)
        else:
            n_act = sum(c in y.index for c in cur)
        act = (y.reindex(cur).sum() / y.reindex(prv).sum() - 1) * 100 if n_act == 4 and all(c in y.index for c in prv) else np.nan
        mix = (lv[cur].sum() / lv[prv].sum() - 1) * 100
        mix_c = (lvl_c.reindex(cur).sum() / lvl_c.reindex(prv).sum() - 1) * 100
        # model içi uyum: tüm çeyrekler için one-step uyum mevcutsa
        fit_g = np.nan
        if all(c in fitted_lvl.index for c in cur) and all(c in y.index for c in prv):
            fit_g = (fitted_lvl[cur].sum() / y[prv].sum() - 1) * 100
        partial = [c for c in cur if c in y.index and pd.notna(n_months.get(c)) and n_months.get(c) < 3]
        if n_act == 4:
            model_g, tur = fit_g, ("Model içi uyum" if fit_g == fit_g else "Gerçekleşen")
            if partial:
                tur += f" ({partial[-1]}: {int(n_months[partial[-1]])} ay açıklandı)"
        elif n_act == 0:
            model_g, tur = mix, "Tahmin"
        else:
            model_g, tur = mix, f"Tahmin ({n_act} çeyrek gerçekleşen)"
            if partial and spec.partial_as_estimate:
                tur = f"Tahmin ({n_act} çeyrek gerçekleşen; {partial[-1]}: {int(n_months[partial[-1]])} ay gerçekleşen + kalan aylar tahmini)"
        annual_rows.append({"Yıl": yr, "Gerçekleşen büyüme %": act, "Model / Tahmin büyüme %": model_g,
                            "Yakınsamalı tahmin büyüme %": (mix_c if n_act < 4 else np.nan), "Tür": tur})
    annual = pd.DataFrame(annual_rows)
    first_fit = annual.index[annual["Model / Tahmin büyüme %"].notna()]
    annual = annual.loc[first_fit[0]:].reset_index(drop=True) if len(first_fit) else annual

    fcdf = pd.DataFrame({"dlog tahmin": fc_dly, "SÜE tahmin": lvl.reindex(fc_idx)})
    return ModelResult(code=code, order={"p": p, **q}, ols=res, design_cols=list(Xe.columns),
                       sample=(Xe.index[0], Xe.index[-1]), nobs=int(res.nobs), aic=float(sel_tab.iloc[0, 1]),
                       diagnostics=diag, season=season, fitted=fitted_raw, forecast=fcdf,
                       quarterly=qdf, annual=annual, select_table=sel_tab, notes=notes, convergence=conv, spec=spec)
