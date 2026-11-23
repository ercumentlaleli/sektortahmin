"""Aylık sektörel tahmin çalıştırması (iki model seti).

Kullanım:
    python run_monthly.py --excel "Makro_Tahminler.xlsx" [--models sue,ufe] [--end-year 2032]

Model setleri:
  sue  Sanayi üretimi: 2'li/3'lü NACE MA SÜE ~ TR GSYH (MA), IMF küresel GSYH, REDK
  ufe  Yurt içi üretici fiyatları: 2'li/3'lü NACE Yİ-ÜFE ~ Yİ-ÜFE genel (Excel E), Dolar/TL (Excel H), Brent (EVDS)

Ortak yöntem: aylık -> çeyreklik ortalama (eksik aylar varsa açıklanan ayların ortalaması), Δlog,
mevsimsellik testi/arındırma, ARDL (maks. 4 gecikme, AIC), tanı testleri, dinamik tahmin,
uzun vade yakınsaması, yıllık ortalama değişim, NACE başına Excel + bar grafik.

Çıktılar: outputs/<YYYY-AA>/ (SÜE) ve outputs/<YYYY-AA>/ufe/ (Yİ-ÜFE)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import traceback
import zipfile
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from sektortahmin import data_sources as ds
from sektortahmin import model as model_mod
from sektortahmin import projections as pj
from sektortahmin.model import SUE_SPEC, UFE_SPEC, apply_factors, fit_and_forecast, seasonal_factors, seasonality_test
from sektortahmin.report import model_str, safe_name, write_sector_workbook, write_summary_workbook

MIN_QUARTERS = 24   # bir NACE serisinin modele girmesi için gereken asgari çeyrek sayısı
MIN_EST_OBS = 40    # ARDL tahmin örnekleminde (gecikmeler düşüldükten sonra) gereken asgari gözlem
MAX_STALE_MONTHS = 12  # son verisi en güncel aydan bu kadar eski olan seriler (yayımı durmuş) atlanır


# ------------------------------------------------------------------ ortak yardımcılar
def cached_frame(path: Path, fetch, freq="M"):
    if path.exists():
        w = pd.read_csv(path, index_col=0)
        w.index = pd.PeriodIndex(w.index, freq=freq)
        return w
    w = fetch()
    ds.save_cache(w, path)
    return w


def extend_growth_table(g: pd.Series, end_year: int) -> tuple[pd.Series, str]:
    """Yıllık büyüme serisini end_year'a kadar son yılın değeriyle uzatır."""
    g = g.copy()
    last = int(g.index.max())
    note = "-"
    if end_year > last:
        for yr in range(last + 1, end_year + 1):
            g.loc[yr] = g.loc[last]
        note = f"{last + 1}-{end_year}: son yıl ({last}, %{g.loc[last]:.2f}) sabit tutuldu"
    return g, note


def x_seasonality(X_dl: pd.DataFrame, x_last_actual, start: str):
    rows = []
    for c in X_dl.columns:
        s_act = X_dl[c].loc[:x_last_actual].dropna()
        s_act = s_act[s_act.index >= pd.Period(start, "Q")]
        t = seasonality_test(s_act)
        rows.append({"Değişken": c, **t})
        if t["mevsimsel"]:
            X_dl[c] = apply_factors(X_dl[c], seasonal_factors(s_act))
            print(f"  {c}: mevsimsellik bulundu, arındırıldı")
    return pd.DataFrame(rows).set_index("Değişken")


def write_app_data(d: Path, results, metas, run_info, inputs_q, assumptions, spec):
    """Streamlit arayüzü için düz CSV çıktıları."""
    d.mkdir(parents=True, exist_ok=True)
    models, ann, qs, diags, coefs = [], [], [], [], []
    for r in results:
        m = metas[r.code]
        models.append({"NACE": r.code, "Ad": m.get("name", ""), "Düzey": m["level"], "Son ay": m["last_month"],
                       "Model": model_str(r),
                       "Göreli büyüme oranı (ham)": r.convergence.get("Göreli büyüme oranı (ham)"),
                       "Göreli büyüme oranı": r.convergence.get("Göreli büyüme oranı (sınırlı)"),
                       "Örneklem": f"{r.sample[0]}–{r.sample[1]}", "N": r.nobs,
                       "R2": r.ols.rsquared, "Düz. R2": r.ols.rsquared_adj, "AIC": r.aic,
                       "Mevsimsellik arındırıldı": r.season["mevsimsel"],
                       "Dosya": f"sektorler/{'2li' if m['level'][0] == '2' else '3lu'}/{safe_name(r.code)}.xlsx"})
        ann.append(r.annual.assign(NACE=r.code))
        q = r.quarterly.copy()
        q.index = q.index.astype(str)
        qs.append(q.rename_axis("Çeyrek").reset_index().assign(NACE=r.code))
        diags.append(r.diagnostics.assign(NACE=r.code))
        coefs.append(pd.DataFrame({"Değişken": r.design_cols, "Katsayı": np.asarray(r.ols.params),
                                   "Std. hata": np.asarray(r.ols.bse), "t": np.asarray(r.ols.tvalues),
                                   "p": np.asarray(r.ols.pvalues)}).assign(NACE=r.code))
    pd.DataFrame(models).to_csv(d / "models.csv", index=False)
    pd.concat(ann).to_csv(d / "annual.csv", index=False)
    pd.concat(qs).to_csv(d / "quarterly.csv", index=False)
    pd.concat(diags).to_csv(d / "diagnostics.csv", index=False)
    pd.concat(coefs).to_csv(d / "coefficients.csv", index=False)
    x = inputs_q.copy()
    x.index = x.index.astype(str)
    x.rename_axis("Çeyrek").to_csv(d / "inputs_quarterly.csv")
    assumptions["Yıllık varsayımlar"].to_csv(d / "assumptions_annual.csv")
    pd.Series(run_info).to_csv(d / "run_info.csv", header=["Değer"])
    (d / "spec.json").write_text(json.dumps(asdict(spec), ensure_ascii=False, indent=1))


def make_zip(out_dir: Path, run_tag: str, prefix: str):
    zp = out_dir / f"{prefix}_{run_tag}.zip"
    files = sorted((out_dir / "sektorler").rglob("*.xlsx")) + sorted(out_dir.glob("Ozet_*.xlsx"))
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(out_dir))


def run_model_set(spec, deps: dict, names: dict, X_dl: pd.DataFrame, x_last_actual, horizon_end,
                  inputs_q, assumptions, run_info_base: dict, out_dir: Path, run_tag: str, only: set,
                  meta_extra: dict, zip_prefix: str, summary_prefix: str):
    results, metas, failed = [], {}, []
    out_dir.mkdir(parents=True, exist_ok=True)
    sector_dir = out_dir / "sektorler"
    if sector_dir.exists():
        shutil.rmtree(sector_dir)
    start = pd.Period(spec.sample_start, "Q")
    latest = max(m.index.max() for _, m in deps.values() if len(m))
    for code, (lvl, m) in sorted(deps.items()):
        if only and code not in only:
            continue
        if not len(m):
            failed.append((code, "TÜİK veri yayımlamıyor (gizli)"))
            continue
        if (latest - m.index.max()).n > MAX_STALE_MONTHS:
            failed.append((code, f"yayımı durmuş (son veri {m.index.max()})"))
            continue
        mean_q, cnt_q = pj.monthly_to_quarterly_partial(m)
        mean_q = mean_q[mean_q.index >= start]
        if len(mean_q) < MIN_QUARTERS:
            failed.append((code, f"yetersiz gözlem ({len(mean_q)} çeyrek)"))
            continue
        meta = {"name": names.get(code, ""), "level": f"{lvl}'li (NACE Rev.2)", "last_month": str(m.index.max()),
                **meta_extra}
        try:
            r = fit_and_forecast(code, mean_q, cnt_q, X_dl, x_last_actual, horizon_end, spec=spec)
        except Exception as e:
            failed.append((code, repr(e)))
            traceback.print_exc()
            continue
        if r.nobs < MIN_EST_OBS:
            failed.append((code, f"tahmin örneklemi kısa ({r.nobs} gözlem < {MIN_EST_OBS}; veri {m.index.min()}'dan başlıyor)"))
            continue
        metas[code] = meta
        results.append(r)
        fn = sector_dir / ("2li" if lvl == 2 else "3lu") / f"{safe_name(code)}.xlsx"
        write_sector_workbook(fn, r, meta, inputs_q, m, assumptions)
        tail = r.annual.tail(3)
        print(f"  [{spec.key}] {code}: {model_str(r)}  N={r.nobs}  " + " ".join(
            f"{int(y)}:{v:.1f}" for y, v in zip(tail["Yıl"], tail["Yakınsamalı tahmin büyüme %"])))

    run_info = {**run_info_base,
                "Uzun vade yakınsaması": f"{model_mod.CONV_START}–{horizon_end} doğrusal; hedef = tarihsel göreli {spec.word} oranı "
                                         f"({model_mod.REL_START}'ten, [{model_mod.RATIO_BOUNDS[0]:g}, {model_mod.RATIO_BOUNDS[1]:g}] sınırlı) x {spec.anchor_label}",
                "Tahmin örneklemi sonu": str(x_last_actual),
                "Tahmin ufku": str(horizon_end),
                "Modellenen seri": len(results),
                "Modellenemeyen": "; ".join(f"{c}: {w}" for c, w in failed) or "-"}
    if results:
        write_summary_workbook(out_dir / f"{summary_prefix}_{run_tag}.xlsx", results, metas, run_info)
        write_app_data(out_dir / "app_data", results, metas, run_info, inputs_q, assumptions, spec)
        make_zip(out_dir, run_tag, zip_prefix)
    print(f"--- {spec.title}")
    print("\n".join(f"{k}: {v}" for k, v in run_info.items()))
    print(f"Çıktılar: {out_dir}")
    return results


# ------------------------------------------------------------------ SÜE modeli
def prepare_sue(args, tok, cache, end_year, today):
    print("TÜİK SDMX: SÜE verisi çekiliyor...")
    deps = {}
    last2 = ""
    for lvl in [int(x) for x in args.levels.split(",")]:
        w = cached_frame(cache / f"sue_aylik_{lvl}li.csv", lambda: ds.tuik_ipi_monthly(lvl, start="2010-01", token=tok))
        for c in w.columns:
            deps[c] = (lvl, w[c].dropna())
        if lvl == 2:
            last2 = str(w.index.max())
        print(f"  {lvl}'li düzey: {w.shape[1]} seri, son ay {w.index.max()}")

    print("EVDS / IMF verileri çekiliyor (SÜE açıklayıcıları)...")
    gdp = ds.evds_gdp_sa_quarterly()
    reer_m = ds.evds_reer_monthly()
    world = ds.imf_world_gdp_quarterly()
    weo, weo_pub = ds.imf_weo_world_growth()
    for o, n in [(gdp, "gsyh_ma"), (reer_m, "redk_aylik"), (world, "imf_dunya_gsyh"), (weo, "imf_weo_dunya_buyume")]:
        ds.save_cache(o, cache / f"{n}.csv")
    weo, weo_note = extend_growth_table(weo, end_year)
    reer_q = pj.quarterly_mean_complete(reer_m)

    tr_growth = pj.read_turkey_growth(args.excel)
    if end_year > tr_growth.index.max():
        raise SystemExit(f"Excel'de {end_year} yılına kadar Türkiye büyüme varsayımı yok (son yıl {tr_growth.index.max()}).")
    cpi_usd = pj.read_monthly_cpi_usd(args.excel)
    gdp_ext = pj.extend_with_annual_growth(gdp, tr_growth, end_year)
    world_ext = pj.extend_with_annual_growth(world, weo, end_year)
    reer_ext = pj.project_reer(reer_q, cpi_usd, end_year)

    x_last_actual = min(gdp.index[-1], world.index[-1], reer_q.index[-1])
    levels = pd.DataFrame({"GSYH": gdp_ext["deger"], "DUNYA": world_ext["deger"], "REDK": reer_ext["REDK"]})
    X_dl = np.log(levels).diff()
    X_dl.columns = list(SUE_SPEC.x_cols)
    season_x = x_seasonality(X_dl, x_last_actual, "2012Q2")

    inputs_q = pd.DataFrame({
        "TR GSYH (MA endeks)": gdp_ext["deger"], "TR GSYH kaynak": gdp_ext["kaynak"],
        "Küresel GSYH (IMF endeks)": world_ext["deger"], "Küresel GSYH kaynak": world_ext["kaynak"],
        "REDK": reer_ext["REDK"], "REDK kaynak": reer_ext["kaynak"],
        "dlog TR GSYH": X_dl["dl_GSYH"], "dlog Küresel GSYH": X_dl["dl_DUNYA"], "dlog REDK": X_dl["dl_REDK"],
    })
    annual = tr_growth[(tr_growth.index >= today.year - 3) & (tr_growth.index <= end_year)].to_frame("Türkiye GSYH büyümesi % (Excel)")
    annual["Küresel GSYH büyümesi % (IMF WEO)"] = weo.reindex(annual.index)
    annual.index.name = "Yıl"
    redk_tab = reer_ext[reer_ext["kaynak"] == "Projeksiyon"].copy()
    redk_tab.index = redk_tab.index.astype(str)
    redk_tab.index.name = "Çeyrek"
    assumptions = {
        "Yıllık varsayımlar": annual,
        "REDK projeksiyonu: REDK = REDK(-1)*(1+TÜFE değ.)/(1+USD değ.)": redk_tab,
        "Açıklayıcı değişkenlerde mevsimsellik testi (Δlog, çeyrek kuklaları)": season_x,
    }
    info = {
        "Model": SUE_SPEC.title,
        "Türkiye büyüme satırı (Macro Projections)": tr_growth.attrs.get("row"),
        "SÜE son ay (2'li)": last2,
        "TR GSYH son gerçekleşen": str(gdp.index[-1]),
        "IMF küresel GSYH son gerçekleşen": str(world.index[-1]),
        "IMF WEO yayım tarihi": weo_pub,
        "WEO ufku dışı yıllar": weo_note,
        "REDK son tam çeyrek": str(reer_q.index[-1]) + f" (son ay {reer_m.index[-1]})",
    }
    return deps, X_dl, x_last_actual, inputs_q, assumptions, info


# ------------------------------------------------------------------ Yİ-ÜFE modeli
def prepare_ufe(args, tok, cache, end_year, today):
    print("TÜİK SDMX: Yİ-ÜFE verisi çekiliyor...")
    files = {k: cache / f"yiufe_aylik_{k}.csv" for k in ("2", "3", "genel")}
    if all(f.exists() for f in files.values()):
        ppi = {k: cached_frame(f, None) for k, f in files.items()}
    else:
        ppi = ds.tuik_ppi_monthly(start="2004-01", token=tok)
        for k, w in ppi.items():
            ds.save_cache(w, files[k])
    levels_wanted = [int(x) for x in args.levels.split(",")]
    deps = {}
    for lvl in levels_wanted:
        w = ppi[str(lvl)]
        for c in w.columns:
            deps[c] = (lvl, w[c].dropna())
        print(f"  {lvl}'li düzey: {w.shape[1]} seri, son ay {w.index.max()}")
    total = ppi["genel"].iloc[:, 0].dropna()

    print("EVDS / IMF verileri çekiliyor (Yİ-ÜFE açıklayıcıları)...")
    usd_m = ds.evds_monthly_avg(ds.EVDS_USD, start="01-01-2003")
    brent_m = ds.evds_monthly_avg(ds.EVDS_BRENT, start="01-01-2003")
    oil, oil_pub = ds.imf_weo_oil()
    for o, n in [(usd_m, "usdtry_aylik"), (brent_m, "brent_aylik"), (oil, "imf_weo_petrol")]:
        ds.save_cache(o, cache / f"{n}.csv")

    xl = pj.read_monthly_columns(args.excel, {"YIUFE": 5, "USDTRY": 8})
    ppi_path = pj.splice_monthly(total, xl["YIUFE"])
    usd_path = pj.splice_monthly(usd_m, xl["USDTRY"])
    if ppi_path.index.max().year < end_year or usd_path.index.max().year < end_year:
        raise SystemExit(f"Excel'de {end_year} sonuna kadar Yİ-ÜFE/Dolar-TL tahmini yok.")
    ppi_q = pj.quarterly_from_monthly_path(ppi_path, total.index[-1])
    usd_q = pj.quarterly_from_monthly_path(usd_path, usd_m.index[-1])

    oil_g = (oil.pct_change() * 100).dropna()
    oil_g, oil_note = extend_growth_table(oil_g, end_year)
    brent_q = pj.quarterly_mean_complete(brent_m)
    brent_ext = pj.extend_with_annual_growth(brent_q, oil_g, end_year)

    horizon_end = pd.Period(f"{end_year}Q4", "Q")
    lv = pd.DataFrame({"YIUFE": ppi_q["deger"], "USD": usd_q["deger"], "BRENT": brent_ext["deger"]}).loc[:horizon_end]
    x_last_actual = min(ppi_q.index[ppi_q["kaynak"] == "Gerçekleşen"][-1],
                        usd_q.index[usd_q["kaynak"] == "Gerçekleşen"][-1], brent_q.index[-1])
    X_dl = np.log(lv).diff()
    X_dl.columns = list(UFE_SPEC.x_cols)
    season_x = x_seasonality(X_dl, x_last_actual, "2005Q2")

    inputs_q = pd.DataFrame({
        "Yİ-ÜFE genel (2003=100)": ppi_q["deger"], "Yİ-ÜFE genel kaynak": ppi_q["kaynak"],
        "Dolar/TL (çeyrek ort.)": usd_q["deger"], "Dolar/TL kaynak": usd_q["kaynak"],
        "Brent (USD/varil)": brent_ext["deger"], "Brent kaynak": brent_ext["kaynak"],
        "dlog Yİ-ÜFE genel": X_dl["dl_YIUFE"], "dlog Dolar/TL": X_dl["dl_USD"], "dlog Brent": X_dl["dl_BRENT"],
    }).loc[:horizon_end]

    yrs = range(today.year - 3, end_year + 1)

    def ann(qs):
        a = qs.groupby(qs.index.year).mean()
        return (a.pct_change() * 100).reindex(yrs)

    annual = pd.DataFrame({"Yİ-ÜFE genel yıllık ort. değişim % (Excel E)": ann(ppi_q["deger"]),
                           "Dolar/TL yıllık ort. değişim % (Excel H)": ann(usd_q["deger"]),
                           "Brent yıllık ort. değişim % (IMF WEO petrol)": ann(brent_ext["deger"]),
                           "Brent yıllık ortalama (USD/varil)": brent_ext["deger"].groupby(brent_ext.index.year).mean().reindex(yrs)})
    annual.index.name = "Yıl"
    assumptions = {
        "Yıllık varsayımlar": annual,
        "Açıklayıcı değişkenlerde mevsimsellik testi (Δlog, çeyrek kuklaları)": season_x,
    }
    info = {
        "Model": UFE_SPEC.title,
        "Yİ-ÜFE son ay": str(total.index[-1]),
        "Dolar/TL son tam ay (EVDS)": str(usd_m.index[-1]),
        "Brent son tam çeyrek (EVDS)": str(brent_q.index[-1]),
        "Yİ-ÜFE / Dolar-TL projeksiyonu": "Excel 'Aylık Kur Tahminleri' E ve H sütunlarının aylık % değişimleri, son gerçekleşen aydan zincirlenerek",
        "Brent projeksiyonu": f"IMF WEO ortalama petrol fiyatı (APSP) yıllık % değişimleri; WEO yayım {oil_pub}",
        "WEO petrol ufku dışı yıllar": oil_note,
    }
    return deps, X_dl, x_last_actual, inputs_q, assumptions, info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--excel", required=True, help="Makro tahminler Excel dosyası")
    ap.add_argument("--models", default="sue,ufe", help="Çalıştırılacak modeller: sue, ufe")
    ap.add_argument("--end-year", type=int, default=2032, help="Tahmin ufku son yılı (varsayılan: 2032)")
    ap.add_argument("--out", default="outputs")
    ap.add_argument("--cache", default="data/cache")
    ap.add_argument("--levels", default="2,3")
    ap.add_argument("--covid-dummy", action="store_true", help="2020Q2/2020Q3 COVID impuls kuklalarını ekle")
    ap.add_argument("--only", default="", help="Virgülle NACE kodları (test için)")
    args = ap.parse_args()

    today = dt.date.today()
    if args.covid_dummy:
        model_mod.DUMMY_PERIODS[:] = model_mod.COVID_PERIODS
    dummy_text = ("COVID impuls kuklaları: " + ", ".join(model_mod.DUMMY_PERIODS)) if model_mod.DUMMY_PERIODS else "Yok"
    end_year = args.end_year
    horizon_end = pd.Period(f"{end_year}Q4", "Q")
    run_tag = today.strftime("%Y-%m")
    out_root = Path(args.out) / run_tag
    cache = Path(args.cache) / today.isoformat()
    only = set(filter(None, args.only.split(",")))
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    tok = ds.tuik_token()
    names = ds.tuik_nace_names(tok)
    base = {"Çalıştırma tarihi": today.isoformat(), "Makro Excel": Path(args.excel).name, "COVID kuklaları": dummy_text}
    meta_extra = {"run_date": today.isoformat(), "dummies": dummy_text}
    ok = True
    if "sue" in models:
        deps, X_dl, xla, inputs_q, assumptions, info = prepare_sue(args, tok, cache, end_year, today)
        res = run_model_set(SUE_SPEC, deps, names, X_dl, xla, horizon_end, inputs_q, assumptions, {**base, **info},
                            out_root, run_tag, only, meta_extra, "Sektor_Tahminleri", "Ozet_Tum_Sektorler")
        ok &= bool(res)
    if "ufe" in models:
        deps, X_dl, xla, inputs_q, assumptions, info = prepare_ufe(args, tok, cache, end_year, today)
        res = run_model_set(UFE_SPEC, deps, names, X_dl, xla, horizon_end, inputs_q, assumptions, {**base, **info},
                            out_root / "ufe", run_tag, only, meta_extra, "YIUFE_Tahminleri", "Ozet_YIUFE_Tum_Sektorler")
        ok &= bool(res)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
