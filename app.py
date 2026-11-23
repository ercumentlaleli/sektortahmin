"""Sektörel SÜE tahmin sonuçları – Streamlit arayüzü.

Çalıştırma:  streamlit run app.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outputs"
INPUTS = ROOT / "inputs"

st.set_page_config(page_title="Sektörel SÜE Tahminleri", page_icon="🏭", layout="wide")

C_ACT, C_FC = "#1F3864", "#ED7D31"


# ------------------------------------------------------------------ veri
def list_runs() -> list[str]:
    if not OUT.exists():
        return []
    return sorted([p.name for p in OUT.iterdir() if (p / "app_data" / "models.csv").exists()], reverse=True)


@st.cache_data(show_spinner=False)
def load_run(run: str, _mtime: float) -> dict:
    d = OUT / run / "app_data"
    data = {n: pd.read_csv(d / f"{n}.csv") for n in
            ["models", "annual", "quarterly", "diagnostics", "coefficients", "inputs_quarterly"]}
    data["assumptions"] = pd.read_csv(d / "assumptions_annual.csv")
    data["run_info"] = pd.read_csv(d / "run_info.csv", index_col=0)
    return data


def run_mtime(run: str) -> float:
    return (OUT / run / "app_data" / "models.csv").stat().st_mtime


def download(path: Path, label: str, key: str):
    if path.exists():
        st.download_button(label, data=path.read_bytes(), file_name=path.name, key=key,
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                           if path.suffix == ".xlsx" else "application/zip", width="stretch")


def annual_chart(a: pd.DataFrame, title: str):
    m = a.melt(id_vars=["Yıl", "Tür"], value_vars=["Gerçekleşen büyüme %", "Model / Tahmin büyüme %"],
               var_name="Seri", value_name="Büyüme %").dropna(subset=["Büyüme %"])
    m["Seri"] = m["Seri"].map({"Gerçekleşen büyüme %": "Gerçekleşen", "Model / Tahmin büyüme %": "Model / Tahmin"})
    m["Yıl"] = m["Yıl"].astype(str)
    return (alt.Chart(m, title=title).mark_bar()
            .encode(x=alt.X("Yıl:N", title=None),
                    xOffset=alt.XOffset("Seri:N", sort=["Gerçekleşen", "Model / Tahmin"]),
                    y=alt.Y("Büyüme %:Q", title="Yıllık büyüme (%)"),
                    color=alt.Color("Seri:N", scale=alt.Scale(domain=["Gerçekleşen", "Model / Tahmin"], range=[C_ACT, C_FC]),
                                    legend=alt.Legend(orient="bottom", title=None)),
                    tooltip=["Yıl", "Seri", alt.Tooltip("Büyüme %:Q", format=".2f"), "Tür"])
            .properties(height=380))


def level_chart(q: pd.DataFrame, title: str):
    q = q.copy()
    q["Tarih"] = pd.PeriodIndex(q["Çeyrek"], freq="Q").to_timestamp()
    act = q[["Tarih", "SÜE (gerçekleşen)"]].rename(columns={"SÜE (gerçekleşen)": "Endeks"}).assign(Seri="Gerçekleşen")
    fit = q[["Tarih", "SÜE (model içi uyum)"]].rename(columns={"SÜE (model içi uyum)": "Endeks"}).assign(Seri="Model içi uyum")
    last_act = q["SÜE (gerçekleşen)"].last_valid_index()
    fc = q.loc[last_act:, ["Tarih", "SÜE (gerçekleşen + tahmin)"]].rename(
        columns={"SÜE (gerçekleşen + tahmin)": "Endeks"}).assign(Seri="Tahmin")
    m = pd.concat([act, fit, fc]).dropna()
    return (alt.Chart(m, title=title).mark_line(point=alt.OverlayMarkDef(size=12))
            .encode(x=alt.X("Tarih:T", title=None),
                    y=alt.Y("Endeks:Q", scale=alt.Scale(zero=False), title="SÜE (MA, 2021=100)"),
                    color=alt.Color("Seri:N", scale=alt.Scale(domain=["Gerçekleşen", "Model içi uyum", "Tahmin"],
                                                              range=[C_ACT, "#A5A5A5", C_FC]),
                                    legend=alt.Legend(orient="bottom", title=None)),
                    strokeDash=alt.condition(alt.datum.Seri == "Model içi uyum", alt.value([4, 3]), alt.value([1, 0])),
                    tooltip=[alt.Tooltip("Tarih:T", format="%Y-%m"), "Seri", alt.Tooltip("Endeks:Q", format=".2f")])
            .properties(height=380))


def growth_color(v, lim=10.0):
    """Kırmızı (negatif) – beyaz – yeşil (pozitif) renk skalası (matplotlib gerektirmez)."""
    try:
        if v != v:
            return ""
    except TypeError:
        return ""
    t = max(-1.0, min(1.0, float(v) / lim))
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
st.sidebar.title("🏭 Sektörel SÜE")
runs = list_runs()
page = st.sidebar.radio("Sayfa", ["Genel bakış", "Sektör detayı", "Varsayımlar", "Yeni çalıştırma"])

if not runs and page != "Yeni çalıştırma":
    st.info("Henüz çıktı yok. 'Yeni çalıştırma' sayfasından makro Excel dosyasını yükleyip modeli çalıştırın.")
    st.stop()

if runs:
    run = st.sidebar.selectbox("Çalıştırma (ay)", runs)
    D = load_run(run, run_mtime(run))
    run_dir = OUT / run
    models = D["models"]
    st.sidebar.divider()
    st.sidebar.caption("İndir")
    with st.sidebar:
        download(run_dir / f"Ozet_Tum_Sektorler_{run}.xlsx", "⬇ Özet Excel", "dl_sum_side")
        download(run_dir / f"Sektor_Tahminleri_{run}.zip", "⬇ Tüm sektör dosyaları (zip)", "dl_zip_side")

# ------------------------------------------------------------------ sayfalar
if page == "Genel bakış":
    st.title(f"Sektörel sanayi üretimi tahminleri – {run}")
    ri = D["run_info"]["Değer"]
    c = st.columns(4)
    c[0].metric("Modellenen seri", ri.get("Modellenen seri"))
    c[1].metric("SÜE son ay", ri.get("SÜE (2'li) son ay", ri.get("SÜE son ay (2'li)")))
    c[2].metric("Tahmin örneklemi sonu", ri.get("Tahmin örneklemi sonu"))
    c[3].metric("Tahmin ufku", ri.get("Tahmin ufku"))

    a = D["annual"]
    years = sorted(a["Yıl"].unique())
    fc_years = [y for y in years if y >= int(str(ri.get("Tahmin örneklemi sonu"))[:4])]
    last_full = [y for y in years if a.loc[a["Yıl"] == y, "Gerçekleşen büyüme %"].notna().any()]
    show_years = (last_full[-2:] if last_full else []) + fc_years

    f1, f2 = st.columns([1, 3])
    lvl = f1.multiselect("Düzey", sorted(models["Düzey"].unique()), default=sorted(models["Düzey"].unique()))
    search = f2.text_input("Ara (kod veya ad)", "")
    piv_act = a.pivot(index="NACE", columns="Yıl", values="Gerçekleşen büyüme %")
    piv_fc = a.pivot(index="NACE", columns="Yıl", values="Model / Tahmin büyüme %")
    tbl = models.set_index("NACE")[["Ad", "Düzey", "Model", "Düz. R2"]].copy()
    for y in show_years:
        if y in last_full:
            tbl[f"{y} Gerç. %"] = piv_act.get(y)
        tbl[f"{y} {'Tahmin' if y in fc_years else 'Model'} %"] = piv_fc.get(y)
    diag = D["diagnostics"].pivot(index="NACE", columns="Test", values="Sonuç (%5)")
    tbl["Geçen test"] = (diag == "Geçti").sum(axis=1).astype(str) + "/" + diag.notna().sum(axis=1).astype(str)
    tbl = tbl[tbl["Düzey"].isin(lvl)]
    if search:
        s = search.lower()
        tbl = tbl[tbl.index.str.lower().str.contains(s) | tbl["Ad"].str.lower().str.contains(s)]
    num_cols = [c for c in tbl.columns if c.endswith("%") or c == "Düz. R2"]
    st.dataframe(tbl.style.format({c: "{:.1f}" for c in num_cols if c != "Düz. R2"} | {"Düz. R2": "{:.2f}"})
                 .map(growth_color, subset=[c for c in num_cols if c != "Düz. R2"]),
                 width="stretch", height=560)

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
               "Δlog(SÜE) ~ Δlog(TR GSYH), Δlog(Küresel GSYH), Δlog(REDK)"
               + ("" if str(D["run_info"]["Değer"].get("COVID kuklaları", "Yok")) == "Yok"
                  else " + " + str(D["run_info"]["Değer"].get("COVID kuklaları"))))

    c = st.columns(5)
    c[0].metric("Model (p, q_GSYH, q_Dünya, q_REDK)", m["Model"].replace("ARDL", ""))
    c[1].metric("R²", f"{m['R2']:.3f}")
    c[2].metric("Düzeltilmiş R²", f"{m['Düz. R2']:.3f}")
    c[3].metric("AIC", f"{m['AIC']:.3f}")
    with c[4]:
        download(run_dir / m["Dosya"], f"⬇ {code}.xlsx", "dl_sector")

    a = D["annual"][D["annual"]["NACE"] == code]
    q = D["quarterly"][D["quarterly"]["NACE"] == code]
    g1, g2 = st.columns(2)
    g1.altair_chart(annual_chart(a, "Yıllık büyüme: gerçekleşen vs. model / tahmin"), width="stretch")
    g2.altair_chart(level_chart(q, "Çeyreklik SÜE düzeyi"), width="stretch")

    t1, t2, t3, t4 = st.tabs(["Yıllık tablo", "Tanı testleri", "Katsayılar", "Çeyreklik veri"])
    with t1:
        st.dataframe(a.drop(columns="NACE").set_index("Yıl").style.format(
            {"Gerçekleşen büyüme %": "{:.2f}", "Model / Tahmin büyüme %": "{:.2f}"}, na_rep="–"),
            width="stretch")
    with t2:
        dg = D["diagnostics"][D["diagnostics"]["NACE"] == code].drop(columns="NACE").set_index("Test")
        st.dataframe(dg.style.format({"İstatistik": "{:.3f}", "p-değeri": "{:.3f}"}).map(color_result, subset=["Sonuç (%5)"]),
                     width="stretch")
    with t3:
        cf = D["coefficients"][D["coefficients"]["NACE"] == code].drop(columns="NACE").set_index("Değişken")
        st.dataframe(cf.style.format("{:.4f}").map(lambda v: "font-weight:bold" if v < 0.05 else "", subset=["p"]),
                     width="stretch")
    with t4:
        st.dataframe(q.drop(columns="NACE").set_index("Çeyrek"), width="stretch", height=420)

elif page == "Varsayımlar":
    st.title("Varsayımlar ve açıklayıcı değişkenler")
    st.subheader("Yıllık büyüme varsayımları")
    st.dataframe(D["assumptions"].set_index("Yıl").style.format("{:.2f}"), width="stretch")
    x = D["inputs_quarterly"].copy()
    x["Tarih"] = pd.PeriodIndex(x["Çeyrek"], freq="Q").to_timestamp()
    cols = st.columns(3)
    for col, (v, k, t) in zip(cols, [("TR GSYH (MA endeks)", "TR GSYH kaynak", "Türkiye GSYH (MA)"),
                                     ("Küresel GSYH (IMF endeks)", "Küresel GSYH kaynak", "Küresel GSYH (IMF)"),
                                     ("REDK", "REDK kaynak", "REDK (TÜFE bazlı)")]):
        dd = x[["Tarih", v, k]].dropna().rename(columns={v: "Değer", k: "Kaynak"})
        dd = dd[dd["Tarih"] >= "2012-01-01"]
        ch = (alt.Chart(dd, title=t).mark_line(point=alt.OverlayMarkDef(size=10))
              .encode(x=alt.X("Tarih:T", title=None), y=alt.Y("Değer:Q", scale=alt.Scale(zero=False), title=None),
                      color=alt.Color("Kaynak:N", scale=alt.Scale(domain=["Gerçekleşen", "Projeksiyon"], range=[C_ACT, C_FC]),
                                      legend=alt.Legend(orient="bottom", title=None)),
                      tooltip=[alt.Tooltip("Tarih:T", format="%Y-Q%q"), alt.Tooltip("Değer:Q", format=".2f"), "Kaynak"])
              .properties(height=300))
        col.altair_chart(ch, width="stretch")
    with st.expander("Çeyreklik açıklayıcı veriler"):
        st.dataframe(D["inputs_quarterly"].set_index("Çeyrek"), width="stretch")

else:  # Yeni çalıştırma
    st.title("Yeni aylık çalıştırma")
    st.write("Güncel makro tahmin Excel dosyasını yükleyin. TÜİK, EVDS ve IMF verileri yeniden çekilir, "
             "tüm modeller kurulur ve sonuçlar bu arayüze eklenir (yaklaşık 5–10 dakika).")
    up = st.file_uploader("Makro tahminler Excel (Macro Projections + Aylık Kur Tahminleri sayfaları)", type=["xlsx"])
    end_year = st.number_input("Tahmin ufku (son yıl)", min_value=2027, max_value=2045, value=2032, step=1)
    import os
    missing = [k for k in ("TUIK_API_KEY", "EVDS_API_KEY") if not (os.environ.get(k) or (k == "TUIK_API_KEY" and os.environ.get("TUIK_SDMX_API_KEY")))]
    if missing:
        st.warning("Eksik ortam değişkeni: " + ", ".join(missing))
    if up is not None and st.button("Modeli çalıştır", type="primary", disabled=bool(missing)):
        INPUTS.mkdir(exist_ok=True)
        dest = INPUTS / up.name
        dest.write_bytes(up.getvalue())
        log_box = st.empty()
        lines = []
        with st.spinner("Çalışıyor..."):
            proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "run_monthly.py"), "--excel", str(dest),
                                     "--end-year", str(int(end_year))],
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
