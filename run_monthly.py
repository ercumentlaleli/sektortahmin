"""Aylık sektörel SÜE tahmin çalıştırması.

Kullanım:
    python run_monthly.py --excel "Makro_Tahminler.xlsx" [--end-year 2028] [--out outputs]

Adımlar:
 1. TÜİK SDMX: 2'li (bölüm) ve 3'lü (grup) NACE düzeyinde mevsim ve takvim etkisinden
    arındırılmış SÜE -> çeyreklik ortalama (eksik aylar varsa açıklanan ayların ortalaması)
 2. EVDS: MA GSYH (zincirlenmiş hacim) ve TÜFE bazlı REDK; IMF: çeyreklik küresel GSYH endeksi
 3. Mevsimsellik testi (çeyrek kuklaları F + Kruskal-Wallis), gerekirse arındırma
 4. Her NACE için ARDL (Δlog, maks. 4 gecikme, AIC) + tanı testleri
 5-7. Projeksiyonlar: WEO küresel büyüme, Excel'deki Türkiye büyümesi, REDK formülü
 8. Yıllık büyüme, bar grafik ve NACE başına Excel dosyası
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

from sektortahmin import data_sources as ds
from sektortahmin import projections as pj
from sektortahmin.model import X_COLS, apply_factors, fit_and_forecast, seasonal_factors, seasonality_test
from sektortahmin.report import safe_name, write_sector_workbook, write_summary_workbook

MIN_QUARTERS = 24  # bir NACE serisinin modele girmesi için gereken asgari çeyrek sayısı


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--excel", required=True, help="Makro tahminler Excel dosyası")
    ap.add_argument("--end-year", type=int, default=None, help="Tahmin ufku son yılı (varsayılan: içinde bulunulan yıl + 2)")
    ap.add_argument("--out", default="outputs")
    ap.add_argument("--cache", default="data/cache")
    ap.add_argument("--levels", default="2,3")
    ap.add_argument("--only", default="", help="Virgülle NACE kodları (test için)")
    args = ap.parse_args()

    today = dt.date.today()
    end_year = args.end_year or today.year + 2
    horizon_end = pd.Period(f"{end_year}Q4", "Q")
    run_tag = today.strftime("%Y-%m")
    out_dir = Path(args.out) / run_tag
    cache = Path(args.cache) / today.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 1) SÜE
    print("TÜİK SDMX: SÜE verisi çekiliyor...")
    tok = ds.tuik_token()
    names = ds.tuik_nace_names(tok)
    ipi_m = {}
    for lvl in [int(x) for x in args.levels.split(",")]:
        cf = cache / f"sue_aylik_{lvl}li.csv"
        if cf.exists():  # aynı gün tekrar çalıştırmada önbellek
            w = pd.read_csv(cf, index_col=0)
            w.index = pd.PeriodIndex(w.index, freq="M")
        else:
            w = ds.tuik_ipi_monthly(lvl, start="2010-01", token=tok)
            ds.save_cache(w, cf)
        for c in w.columns:
            ipi_m[c] = (lvl, w[c].dropna())
        print(f"  {lvl}'li düzey: {w.shape[1]} seri, son ay {w.index.max()}")

    # ------------------------------------------------------------ 2) Açıklayıcılar
    print("EVDS / IMF verileri çekiliyor...")
    gdp = ds.evds_gdp_sa_quarterly()
    reer_m = ds.evds_reer_monthly()
    world = ds.imf_world_gdp_quarterly()
    weo, weo_pub = ds.imf_weo_world_growth()
    for o, n in [(gdp, "gsyh_ma"), (reer_m, "redk_aylik"), (world, "imf_dunya_gsyh"), (weo, "imf_weo_dunya_buyume")]:
        ds.save_cache(o, cache / f"{n}.csv")
    reer_q = pj.quarterly_mean_complete(reer_m)

    # ------------------------------------------------------------ 5-7) Projeksiyonlar
    tr_growth = pj.read_turkey_growth(args.excel)
    cpi_usd = pj.read_monthly_cpi_usd(args.excel)
    gdp_ext = pj.extend_with_annual_growth(gdp, tr_growth, end_year)
    world_ext = pj.extend_with_annual_growth(world, weo, end_year)
    reer_ext = pj.project_reer(reer_q, cpi_usd, end_year)

    x_last_actual = min(gdp.index[-1], world.index[-1], reer_q.index[-1])
    levels = pd.DataFrame({"GSYH": gdp_ext["deger"], "DUNYA": world_ext["deger"], "REDK": reer_ext["REDK"]})
    X_dl = np.log(levels).diff()
    X_dl.columns = X_COLS

    # ------------------------------------------------------------ 3) Mevsimsellik (açıklayıcılar)
    season_rows = []
    for c in X_COLS:
        s_act = X_dl[c].loc[:x_last_actual].dropna()
        s_act = s_act[s_act.index >= pd.Period("2012Q2", "Q")]
        t = seasonality_test(s_act)
        season_rows.append({"Değişken": c, **t})
        if t["mevsimsel"]:
            X_dl[c] = apply_factors(X_dl[c], seasonal_factors(s_act))
            print(f"  {c}: mevsimsellik bulundu, arındırıldı")
    season_x = pd.DataFrame(season_rows).set_index("Değişken")

    inputs_q = pd.DataFrame({
        "TR GSYH (MA endeks)": gdp_ext["deger"], "TR GSYH kaynak": gdp_ext["kaynak"],
        "Küresel GSYH (IMF endeks)": world_ext["deger"], "Küresel GSYH kaynak": world_ext["kaynak"],
        "REDK": reer_ext["REDK"], "REDK kaynak": reer_ext["kaynak"],
        "dlog TR GSYH": X_dl["dl_GSYH"], "dlog Küresel GSYH": X_dl["dl_DUNYA"], "dlog REDK": X_dl["dl_REDK"],
    })

    annual_tr = tr_growth[(tr_growth.index >= today.year - 3) & (tr_growth.index <= end_year)].to_frame("Türkiye GSYH büyümesi % (Excel)")
    annual_tr["Küresel GSYH büyümesi % (IMF WEO)"] = weo.reindex(annual_tr.index)
    annual_tr.index.name = "Yıl"
    redk_tab = reer_ext[reer_ext["kaynak"] == "Projeksiyon"].copy()
    redk_tab.index = redk_tab.index.astype(str)
    redk_tab.index.name = "Çeyrek"
    assumptions = {
        "Yıllık varsayımlar": annual_tr,
        "REDK projeksiyonu: REDK = REDK(-1)*(1+TÜFE değ.)/(1+USD değ.)": redk_tab,
        "Açıklayıcı değişkenlerde mevsimsellik testi (Δlog, çeyrek kuklaları)": season_x,
    }

    # ------------------------------------------------------------ 4, 8) Modeller
    only = set(filter(None, args.only.split(",")))
    results, metas, failed = [], {}, []
    sector_dir = out_dir / "sektorler"
    if sector_dir.exists():
        shutil.rmtree(sector_dir)
    for code, (lvl, m) in sorted(ipi_m.items()):
        if only and code not in only:
            continue
        mean_q, cnt_q = pj.monthly_to_quarterly_partial(m)
        mean_q = mean_q[mean_q.index >= pd.Period("2011Q1", "Q")]
        if len(mean_q) < MIN_QUARTERS:
            failed.append((code, f"yetersiz gözlem ({len(mean_q)} çeyrek)"))
            continue
        meta = {"name": names.get(code, ""), "level": f"{lvl}'li (NACE Rev.2)", "last_month": str(m.index.max()),
                "run_date": today.isoformat()}
        try:
            r = fit_and_forecast(code, mean_q, cnt_q, X_dl, x_last_actual, horizon_end)
        except Exception as e:
            failed.append((code, repr(e)))
            traceback.print_exc()
            continue
        metas[code] = meta
        results.append(r)
        fn = sector_dir / f"{lvl}li" / f"{safe_name(code)}.xlsx"
        write_sector_workbook(fn, r, meta, inputs_q, m, assumptions)
        print(f"  {code}: ARDL{tuple(r.order.values())}  N={r.nobs}  "
              f"{' '.join(f'{int(a.Yıl)}:{a._3:.1f}' for a in r.annual.tail(3).itertuples())}")

    run_info = {
        "Çalıştırma tarihi": today.isoformat(),
        "Makro Excel": Path(args.excel).name,
        "Türkiye büyüme satırı (Macro Projections)": tr_growth.attrs.get("row"),
        "SÜE son ay (2'li)": max((str(m.index.max()) for c, (l, m) in ipi_m.items() if l == 2), default=""),
        "TR GSYH son gerçekleşen": str(gdp.index[-1]),
        "IMF küresel GSYH son gerçekleşen": str(world.index[-1]),
        "IMF WEO yayım tarihi": weo_pub,
        "REDK son tam çeyrek": str(reer_q.index[-1]) + f" (son ay {reer_m.index[-1]})",
        "Tahmin örneklemi sonu": str(x_last_actual),
        "Tahmin ufku": str(horizon_end),
        "Modellenen seri": len(results),
        "Modellenemeyen": "; ".join(f"{c}: {w}" for c, w in failed) or "-",
    }
    write_summary_workbook(out_dir / f"Ozet_Tum_Sektorler_{run_tag}.xlsx", results, metas, run_info)
    print("\n".join(f"{k}: {v}" for k, v in run_info.items()))
    print(f"Çıktılar: {out_dir}")
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
