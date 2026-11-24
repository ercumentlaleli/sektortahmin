"""Sektörel tahmin sonuçları (SÜE ve Yİ-ÜFE) – Streamlit arayüzü.

Çalıştırma:  streamlit run app.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outputs"
INPUTS = ROOT / "inputs"

st.set_page_config(page_title="Sektörel Tahminler", page_icon="🏭", layout="wide")

C_ACT, C_FC, C_CONV = "#1F3864", "#ED7D31", "#70AD47"
CONV_COL = "Yakınsamalı tahmin büyüme %"
MODEL_DIRS = {"sue": "", "ufe": "ufe"}          # çıktı alt klasörü
MODEL_TITLES = {"sue": "Sanayi üretimi (SÜE)", "ufe": "Yurt içi üretici fiyatları (Yİ-ÜFE)"}
SUE_DEFAULT_SPEC = {"key": "sue", "title": MODEL_TITLES["sue"], "dep_short": "SÜE", "dep_unit": "SÜE (MA, 2021=100)",
                    "x_cols": ["dl_GSYH", "dl_DUNYA", "dl_REDK"],
                    "x_labels": {"dl_GSYH": "Türkiye GSYH (MA)", "dl_DUNYA": "Küresel GSYH (IMF)", "dl_REDK": "REDK (TÜFE bazlı)"},
                    "anchor_label": "TR GSYH büyümesi", "word": "büyüme"}


# ------------------------------------------------------------------ veri
def model_dir(run: str, model: str) -> Path:
    return OUT / run / MODEL_DIRS[model]


def available_models(run: str) -> list[str]:
    return [k for k in MODEL_DIRS if (model_dir(run, k) / "app_data" / "models.csv").exists()]


def list_runs() -> list[str]:
    if not OUT.exists():
        return []
    return sorted([p.name for p in OUT.iterdir() if p.is_dir() and available_models(p.name)], reverse=True)


@st.cache_data(show_spinner=False)
def load_run(run: str, model: str, _mtime: float) -> dict:
    d = model_dir(run, model) / "app_data"
    data = {n: pd.read_csv(d / f"{n}.csv") for n in
            ["models", "annual", "quarterly", "diagnostics", "coefficients", "inputs_quarterly"]}
    data["assumptions"] = pd.read_csv(d / "assumptions_annual.csv")
    data["run_info"] = pd.read_csv(d / "run_info.csv", index_col=0)
    for n in ["performance", "unit_root", "cv", "unit_root_x"]:
        data[n] = pd.read_csv(d / f"{n}.csv") if (d / f"{n}.csv").exists() else pd.DataFrame()
    sp = d / "spec.json"
    data["spec"] = json.loads(sp.read_text()) if sp.exists() else SUE_DEFAULT_SPEC
    return data


def run_mtime(run: str, model: str) -> float:
    return (model_dir(run, model) / "app_data" / "models.csv").stat().st_mtime


def disp(c: str, spec: dict) -> str:
    """Dahili sütun adlarını model türüne göre görüntüleme adına çevirir."""
    c = c.replace("SÜE", spec["dep_short"])
    if spec["word"] != "büyüme":
        c = c.replace("büyüme", spec["word"]).replace("Büyüme", spec["word"].capitalize())
    return c


def download(path: Path, label: str, key: str):
    if path.exists():
        st.download_button(label, data=path.read_bytes(), file_name=path.name, key=key,
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                           if path.suffix == ".xlsx" else "application/zip", width="stretch")


def annual_chart(a: pd.DataFrame, title: str, ytitle: str = "Yıllık büyüme (%)"):
    names = {"Gerçekleşen büyüme %": "Gerçekleşen", "Model / Tahmin büyüme %": "Model / Tahmin (ham)",
             CONV_COL: "Yakınsamalı tahmin"}
    cols = [c for c in names if c in a.columns]
    m = a.melt(id_vars=["Yıl", "Tür"], value_vars=cols, var_name="Seri", value_name="Büyüme %").dropna(subset=["Büyüme %"])
    m["Seri"] = m["Seri"].map(names)
    m["Yıl"] = m["Yıl"].astype(str)
    dom = [names[c] for c in cols]
    return (alt.Chart(m, title=title).mark_bar()
            .encode(x=alt.X("Yıl:N", title=None),
                    xOffset=alt.XOffset("Seri:N", sort=dom),
                    y=alt.Y("Büyüme %:Q", title=ytitle),
                    color=alt.Color("Seri:N", scale=alt.Scale(domain=dom, range=[C_ACT, C_FC, C_CONV][:len(dom)]),
                                    legend=alt.Legend(orient="bottom", title=None)),
                    tooltip=["Yıl", "Seri", alt.Tooltip("Büyüme %:Q", format=".2f"), "Tür"])
            .properties(height=380))


def level_chart(q: pd.DataFrame, title: str, ytitle: str = "SÜE (MA, 2021=100)", log: bool = False):
    q = q.copy()
    q["Tarih"] = pd.PeriodIndex(q["Çeyrek"], freq="Q").to_timestamp()
    act = q[["Tarih", "SÜE (gerçekleşen)"]].rename(columns={"SÜE (gerçekleşen)": "Endeks"}).assign(Seri="Gerçekleşen")
    fit = q[["Tarih", "SÜE (model içi uyum)"]].rename(columns={"SÜE (model içi uyum)": "Endeks"}).assign(Seri="Model içi uyum")
    last_act = q["SÜE (gerçekleşen)"].last_valid_index()
    fc = q.loc[last_act:, ["Tarih", "SÜE (gerçekleşen + tahmin)"]].rename(
        columns={"SÜE (gerçekleşen + tahmin)": "Endeks"}).assign(Seri="Tahmin (ham)")
    parts = [act, fit, fc]
    if "SÜE (gerçekleşen + yakınsamalı tahmin)" in q.columns:
        parts.append(q.loc[last_act:, ["Tarih", "SÜE (gerçekleşen + yakınsamalı tahmin)"]].rename(
            columns={"SÜE (gerçekleşen + yakınsamalı tahmin)": "Endeks"}).assign(Seri="Yakınsamalı tahmin"))
    m = pd.concat(parts).dropna()
    return (alt.Chart(m, title=title).mark_line(point=alt.OverlayMarkDef(size=12))
            .encode(x=alt.X("Tarih:T", title=None),
                    y=alt.Y("Endeks:Q", scale=alt.Scale(type="log") if log else alt.Scale(zero=False),
                            title=ytitle + (" – log ölçek" if log else "")),
                    color=alt.Color("Seri:N", scale=alt.Scale(domain=["Gerçekleşen", "Model içi uyum", "Tahmin (ham)", "Yakınsamalı tahmin"],
                                                              range=[C_ACT, "#A5A5A5", C_FC, C_CONV]),
                                    legend=alt.Legend(orient="bottom", title=None)),
                    strokeDash=alt.condition(alt.datum.Seri == "Model içi uyum", alt.value([4, 3]), alt.value([1, 0])),
                    tooltip=[alt.Tooltip("Tarih:T", format="%Y-%m"), "Seri", alt.Tooltip("Endeks:Q", format=".2f")])
            .properties(height=380))


def growth_color(v, lim=10.0, center=0.0):
    """Kırmızı (merkezin altı) – beyaz – yeşil (merkezin üstü) renk skalası (matplotlib gerektirmez)."""
    try:
        if v != v or center != center:
            return ""
    except TypeError:
        return ""
    t = max(-1.0, min(1.0, (float(v) - center) / lim))
    if t >= 0:
        r, g, b = int(255 - 107 * t), int(255 - 50 * t), int(255 - 107 * t)
    else:
        r, g, b = int(255 + 7 * t), int(255 + 140 * t), int(255 + 140 * t)
    return f"background-color: rgb({r},{g},{b})"


def color_result(v):
    if v == "Geçti":
        return "background-color:#C6EFCE;color:#006100"
    if v == "Kaldı":
        return "background-color:#FFC7CE;color:#9C0006"
    return ""


# ------------------------------------------------------------------ yan menü
st.sidebar.title("🏭 Sektörel Tahminler")
runs = list_runs()
page = st.sidebar.radio("Sayfa", ["Genel bakış", "Sektör detayı", "Varsayımlar", "Yeni çalıştırma"])

if not runs and page != "Yeni çalıştırma":
    st.info("Henüz çıktı yok. 'Yeni çalıştırma' sayfasından makro Excel dosyasını yükleyip modeli çalıştırın.")
    st.stop()

if runs:
    run = st.sidebar.selectbox("Çalıştırma (ay)", runs)
    avail = available_models(run)
    model = st.sidebar.radio("Model", avail, format_func=lambda k: MODEL_TITLES[k])
    D = load_run(run, model, run_mtime(run, model))
    SP = D["spec"]
    W = SP["word"]                       # "büyüme" / "değişim"
    DEP = SP["dep_short"]
    run_dir = model_dir(run, model)
    models = D["models"]
    st.sidebar.divider()
    st.sidebar.caption("İndir")
    with st.sidebar:
        for f in sorted(run_dir.glob(f"Ozet_*_{run}.xlsx")):
            download(f, "⬇ Özet Excel", f"dl_sum_{model}")
        for f in sorted(run_dir.glob(f"*_{run}.zip")):
            download(f, "⬇ Tüm sektör dosyaları (zip)", f"dl_zip_{model}")

# ------------------------------------------------------------------ sayfalar
if page == "Genel bakış":
    st.title(f"{SP['title']} – sektörel tahminler – {run}")
    ri = D["run_info"]["Değer"]
    c = st.columns(4)
    c[0].metric("Modellenen seri", ri.get("Modellenen seri"))
    c[1].metric(f"{DEP} son ay", ri.get("SÜE son ay (2'li)", ri.get("Yİ-ÜFE son ay")))
    c[2].metric("Tahmin örneklemi sonu", ri.get("Tahmin örneklemi sonu"))
    c[3].metric("Tahmin ufku", ri.get("Tahmin ufku"))

    a = D["annual"]
    years = sorted(a["Yıl"].unique())
    fc_years = [y for y in years if y >= int(str(ri.get("Tahmin örneklemi sonu"))[:4])]
    last_full = [y for y in years if a.loc[a["Yıl"] == y, "Gerçekleşen büyüme %"].notna().any()]
    show_years = (last_full[-2:] if last_full else []) + fc_years

    f1, f2, f3 = st.columns([1, 1, 2])
    has_conv = CONV_COL in a.columns
    kind = f1.radio("Tahmin serisi", ["Yakınsamalı", "Model (ham)"] if has_conv else ["Model (ham)"], horizontal=True,
                    help=f"Yakınsamalı: 2029'dan itibaren tahmin, sektörün tarihsel göreli {W}{'si' if W.endswith('e') else 'i'} x {SP['anchor_label']}ne "
                         "doğrusal olarak yaklaştırılır. 2026–2028 iki seride aynıdır.")
    lvl = f2.multiselect("Düzey", sorted(models["Düzey"].unique()), default=sorted(models["Düzey"].unique()))
    search = f3.text_input("Ara (kod veya ad)", "")
    piv_act = a.pivot(index="NACE", columns="Yıl", values="Gerçekleşen büyüme %")
    piv_fit = a.pivot(index="NACE", columns="Yıl", values="Model / Tahmin büyüme %")
    piv_fc = a.pivot(index="NACE", columns="Yıl", values=CONV_COL if kind == "Yakınsamalı" else "Model / Tahmin büyüme %")
    base_cols = ["Ad", "Düzey", "Model", "Düz. R2"] + (["Göreli büyüme oranı"] if "Göreli büyüme oranı" in models.columns else [])
    tbl = models.set_index("NACE")[base_cols].copy().rename(columns={"Göreli büyüme oranı": f"Göreli {W} oranı"})
    for y in show_years:
        if y in last_full:
            tbl[f"{y} Gerç. %"] = piv_act.get(y)
            tbl[f"{y} Model %"] = piv_fit.get(y)
        else:
            tbl[f"{y} Tahmin %"] = piv_fc.get(y)
    diag = D["diagnostics"].pivot(index="NACE", columns="Test", values="Sonuç (%5)")
    tbl["Geçen test"] = (diag == "Geçti").sum(axis=1).astype(str) + "/" + diag.notna().sum(axis=1).astype(str)
    pf = D["performance"]
    if not pf.empty:
        k4 = pf[pf["Ölçüt"].str.contains("kümülatif")].set_index("NACE")
        h1 = pf[pf["Ölçüt"].str.contains("h=1")].set_index("NACE")
        tbl["RMSE h=1"] = h1["RMSE"]
        tbl["RMSE 4Ç"] = k4["RMSE"]
        tbl["Theil U 4Ç"] = k4["Theil U (naif'e göre)"]
    ur = D["unit_root"]
    if not ur.empty:
        d_ok = ur[ur["Dönüşüm"] == "Δlog"].groupby("NACE")["p-değeri"].max() < 0.05
        tbl["Δlog durağan (ADF+PP)"] = d_ok.map({True: "Evet", False: "Hayır"})
    tbl = tbl[tbl["Düzey"].isin(lvl)]
    if search:
        s = search.lower()
        tbl = tbl[tbl.index.str.lower().str.contains(s) | tbl["Ad"].str.lower().str.contains(s)]
    num_cols = [c for c in tbl.columns if c.endswith("%")]
    sty = tbl.style.format({c: "{:.1f}" for c in num_cols} | {"Düz. R2": "{:.2f}", f"Göreli {W} oranı": "{:.2f}",
                                                             "RMSE h=1": "{:.2f}", "RMSE 4Ç": "{:.2f}", "Theil U 4Ç": "{:.2f}"}, na_rep="–")
    if "Theil U 4Ç" in tbl.columns:
        sty = sty.map(lambda v: ("background-color:#C6EFCE;color:#006100" if v < 1 else "background-color:#FFC7CE;color:#9C0006")
                      if v == v else "", subset=["Theil U 4Ç"])
    if model == "ufe":
        # Renk: sektör fiyat değişiminin genel Yİ-ÜFE değişiminden sapması
        gen = D["assumptions"].set_index("Yıl").iloc[:, 0]
        for c in num_cols:
            sty = sty.map(lambda v, ctr=gen.get(int(c[:4]), float("nan")): growth_color(v, lim=10.0, center=ctr), subset=[c])
        st.caption(f"Gösterilen tahmin serisi: **{kind}** · Renkler: sektörün yıllık ortalama fiyat değişiminin genel "
                   "Yİ-ÜFE değişiminin üstünde (yeşil) / altında (kırmızı) olması")
    else:
        sty = sty.map(growth_color, subset=num_cols)
        st.caption(f"Gösterilen tahmin serisi: **{kind}**")
    st.dataframe(sty, width="stretch", height=560)

    st.subheader("Tanı testleri – geçen/kalan seri sayısı")
    dg = D["diagnostics"].pivot_table(index="Test", columns="Sonuç (%5)", values="NACE", aggfunc="count").fillna(0).astype(int)
    st.dataframe(dg, width="stretch")
    with st.expander("Çalıştırma bilgisi"):
        st.table(D["run_info"])

elif page == "Sektör detayı":
    opts = [f"{r.NACE} – {r.Ad}" for r in models.itertuples()]
    choice = st.sidebar.selectbox("NACE kodu", opts)
    code = choice.split(" – ")[0]
    m = models.set_index("NACE").loc[code]
    st.title(f"{code} – {m['Ad']}")
    st.caption(f"{m['Düzey']} · Son açıklanan ay: {m['Son ay']} · Örneklem: {m['Örneklem']} · "
               f"Δlog({DEP}) ~ " + ", ".join(f"Δlog({SP['x_labels'][x]})" for x in SP["x_cols"])
               + ("" if str(D["run_info"]["Değer"].get("COVID kuklaları", "Yok")) == "Yok"
                  else " + " + str(D["run_info"]["Değer"].get("COVID kuklaları"))))

    c = st.columns(6)
    c[0].metric("Model (p, " + ", ".join("q_" + x[3:] for x in SP["x_cols"]) + ")", m["Model"].replace("ARDL", ""))
    c[1].metric("R²", f"{m['R2']:.3f}")
    c[2].metric("Düzeltilmiş R²", f"{m['Düz. R2']:.3f}")
    c[3].metric("AIC", f"{m['AIC']:.3f}")
    if "Göreli büyüme oranı" in m.index:
        c[4].metric(f"Göreli {W} oranı", f"{m['Göreli büyüme oranı']:.2f}",
                    help=f"Sektörün tarihsel ortalama {W}{'si' if W.endswith('e') else 'i'} / {SP['anchor_label']} ortalaması (ham: {m['Göreli büyüme oranı (ham)']:.2f}, "
                         f"[0, 2] aralığında sınırlı). Uzun vade hedefi = bu oran x {SP['anchor_label']}.")
    with c[5]:
        download(run_dir / m["Dosya"], f"⬇ {code}.xlsx", "dl_sector")

    a = D["annual"][D["annual"]["NACE"] == code]
    q = D["quarterly"][D["quarterly"]["NACE"] == code]
    g1, g2 = st.columns(2)
    g1.altair_chart(annual_chart(a, f"Yıllık ortalama {W}: gerçekleşen, model (ham) ve yakınsamalı tahmin",
                                 f"Yıllık ortalama {W} (%)"), width="stretch")
    g2.altair_chart(level_chart(q, f"Çeyreklik {DEP} düzeyi", SP.get("dep_unit", DEP), log=(model == "ufe")), width="stretch")

    if CONV_COL in a.columns:
        st.caption("Yakınsamalı tahmin: 2028 sonuna kadar model tahminiyle aynıdır; 2029Q1'den ufuk sonuna doğru ağırlık "
                   f"doğrusal olarak modelden hedefe (göreli {W} oranı x {SP['anchor_label']}) kayar. Yargısal bir düzeltmedir.")
    t1, t2, t5, t6, t3, t4 = st.tabs(["Yıllık tablo", "Tanı testleri", "Tahmin performansı", "Birim kök testleri",
                                      "Katsayılar", "Çeyreklik veri"])
    with t1:
        at = a.drop(columns="NACE").set_index("Yıl")
        at.columns = [disp(c, SP) for c in at.columns]
        st.dataframe(at.style.format({c: "{:.2f}" for c in at.columns if c.endswith("%")}, na_rep="–"), width="stretch")
    with t2:
        dg = D["diagnostics"][D["diagnostics"]["NACE"] == code].drop(columns="NACE").set_index("Test")
        st.dataframe(dg.style.format({"İstatistik": "{:.3f}", "p-değeri": "{:.3f}"}).map(color_result, subset=["Sonuç (%5)"]),
                     width="stretch")
    with t5:
        pf = D["performance"]
        pf = pf[pf["NACE"] == code].drop(columns="NACE").set_index("Ölçüt") if not pf.empty else pf
        if pf.empty:
            st.info("Bu çalıştırmada tahmin performansı hesaplanmamış.")
        else:
            sty = pf.style.format("{:.3f}", na_rep="–", subset=[c for c in pf.columns if c != "N"])
            if "Theil U (naif'e göre)" in pf.columns:
                sty = sty.map(lambda v: ("background-color:#C6EFCE;color:#006100" if v < 1 else "background-color:#FFC7CE;color:#9C0006")
                              if v == v else "", subset=["Theil U (naif'e göre)"])
            st.dataframe(sty, width="stretch")
            st.caption("Birim: çeyreklik log-değişim, yüzde puan. Örneklem dışı satırlar zaman serisi çapraz doğrulamasından gelir: "
                       "son 12 çeyrekte her başlangıç noktasında yalnızca o güne kadarki veriyle gecikmeler AIC ile yeniden seçilir, "
                       "model yeniden tahmin edilir ve 1–4 çeyrek sonrası açıklayıcıların gerçekleşen değerleriyle tahmin edilir. "
                       "Theil U < 1: model, naif tahminden (geçmiş ortalama değişim) daha başarılı.")
            cv = D["cv"]
            cv = cv[cv["NACE"] == code] if not cv.empty else cv
            if not cv.empty:
                with st.expander("Çapraz doğrulama ayrıntısı"):
                    st.dataframe(cv.drop(columns="NACE"), width="stretch")
    with t6:
        ur = D["unit_root"]
        ur = ur[ur["NACE"] == code].drop(columns="NACE") if not ur.empty else ur
        if ur.empty:
            st.info("Bu çalıştırmada birim kök testleri hesaplanmamış.")
        else:
            st.dataframe(ur.set_index(["Dönüşüm", "Test"]).style.format({"İstatistik": "{:.3f}", "p-değeri": "{:.3f}"})
                         .map(lambda v: "background-color:#C6EFCE;color:#006100" if v == "Durağan"
                              else ("background-color:#FFC7CE;color:#9C0006" if isinstance(v, str) and v.startswith("Birim") else ""),
                              subset=["Sonuç (%5)"]), width="stretch")
            st.caption("H0: seride birim kök var (durağan değil). Log düzeyde sabit + trend, Δlog'da sabit terimli test. "
                       "ADF gecikmesi AIC ile, PP bant genişliği otomatik seçilir. ARDL farklarla kurulduğu için Δlog'un durağan olması gerekir.")
    with t3:
        cf = D["coefficients"][D["coefficients"]["NACE"] == code].drop(columns="NACE").set_index("Değişken")
        st.dataframe(cf.style.format("{:.4f}").map(lambda v: "font-weight:bold" if v < 0.05 else "", subset=["p"]),
                     width="stretch")
    with t4:
        qt = q.drop(columns="NACE").set_index("Çeyrek")
        qt.columns = [disp(c, SP) for c in qt.columns]
        st.dataframe(qt, width="stretch", height=420)

elif page == "Varsayımlar":
    st.title(f"Varsayımlar ve açıklayıcı değişkenler – {SP['title']}")
    st.subheader("Yıllık varsayımlar")
    st.dataframe(D["assumptions"].set_index("Yıl").style.format("{:.2f}"), width="stretch")
    x = D["inputs_quarterly"].copy()
    x["Tarih"] = pd.PeriodIndex(x["Çeyrek"], freq="Q").to_timestamp()
    xcols = list(x.columns)
    pairs = [(xcols[i - 1], c, xcols[i - 1]) for i, c in enumerate(xcols) if c.endswith(" kaynak")]
    cols = st.columns(len(pairs) or 1)
    for col, (v, k, t) in zip(cols, pairs):
        dd = x[["Tarih", v, k]].dropna().rename(columns={v: "Değer", k: "Kaynak"})
        dd["Kaynak"] = dd["Kaynak"].str.replace(r" \(Excel\)", "", regex=True)
        dd = dd[dd["Tarih"] >= "2012-01-01"]
        ch = (alt.Chart(dd, title=t).mark_line(point=alt.OverlayMarkDef(size=10))
              .encode(x=alt.X("Tarih:T", title=None), y=alt.Y("Değer:Q", scale=alt.Scale(zero=False), title=None),
                      color=alt.Color("Kaynak:N", scale=alt.Scale(domain=["Gerçekleşen", "Projeksiyon"], range=[C_ACT, C_FC]),
                                      legend=alt.Legend(orient="bottom", title=None)),
                      tooltip=[alt.Tooltip("Tarih:T", format="%Y-Q%q"), alt.Tooltip("Değer:Q", format=".2f"), "Kaynak"])
              .properties(height=300))
        col.altair_chart(ch, width="stretch")
    if not D["unit_root_x"].empty:
        st.subheader("Açıklayıcılarda birim kök testleri (ADF, PP)")
        st.dataframe(D["unit_root_x"].set_index(["Değişken", "Dönüşüm", "Test"]).style.format({"İstatistik": "{:.3f}", "p-değeri": "{:.3f}"}),
                     width="stretch")
    with st.expander("Çeyreklik açıklayıcı veriler"):
        st.dataframe(D["inputs_quarterly"].set_index("Çeyrek"), width="stretch")

else:  # Yeni çalıştırma
    st.title("Yeni aylık çalıştırma")
    st.write("Güncel makro tahmin Excel dosyasını yükleyin. TÜİK, EVDS ve IMF verileri yeniden çekilir, "
             "tüm modeller kurulur ve sonuçlar bu arayüze eklenir (yaklaşık 5–10 dakika).")
    up = st.file_uploader("Makro tahminler Excel (Macro Projections + Aylık Kur Tahminleri sayfaları)", type=["xlsx"])
    end_year = st.number_input("Tahmin ufku (son yıl)", min_value=2027, max_value=2045, value=2032, step=1)
    run_models = st.multiselect("Çalıştırılacak modeller", list(MODEL_TITLES), default=list(MODEL_TITLES),
                                format_func=lambda k: MODEL_TITLES[k])
    import os
    missing = [k for k in ("TUIK_API_KEY", "EVDS_API_KEY") if not (os.environ.get(k) or (k == "TUIK_API_KEY" and os.environ.get("TUIK_SDMX_API_KEY")))]
    if missing:
        st.warning("Eksik ortam değişkeni: " + ", ".join(missing))
    if up is not None and st.button("Modeli çalıştır", type="primary", disabled=bool(missing) or not run_models):
        INPUTS.mkdir(exist_ok=True)
        dest = INPUTS / up.name
        dest.write_bytes(up.getvalue())
        log_box = st.empty()
        lines = []
        with st.spinner("Çalışıyor..."):
            proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "run_monthly.py"), "--excel", str(dest),
                                     "--end-year", str(int(end_year)), "--models", ",".join(run_models)],
                                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                if "Warning" in line or "warnings.warn" in line:
                    continue
                lines.append(line.rstrip())
                log_box.code("\n".join(lines[-25:]))
            proc.wait()
        if proc.returncode == 0:
            st.success("Tamamlandı. Sol menüden çalıştırmayı seçerek sonuçları görebilirsiniz.")
            st.cache_data.clear()
        else:
            st.error(f"Çalıştırma hata ile bitti (kod {proc.returncode}). Günlüğü kontrol edin.")
