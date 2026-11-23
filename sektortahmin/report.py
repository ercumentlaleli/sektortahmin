"""Excel çıktıları (xlsxwriter, yerel Excel bar grafikleri ile)."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from .model import X_COLS, X_LABELS, ModelResult


def _clean(v):
    if isinstance(v, pd.Period):
        return str(v)
    if isinstance(v, (float, np.floating)) and (np.isnan(v) or np.isinf(v)):
        return None
    if isinstance(v, np.generic):
        return v.item()
    return v


def _write_df(ws, df: pd.DataFrame, r0: int, c0: int, fmt_h, fmt_n=None, index=True, num_fmt_cols=None):
    cols = ([df.index.name or ""] if index else []) + list(df.columns)
    for j, c in enumerate(cols):
        ws.write(r0, c0 + j, str(c), fmt_h)
    for i, (idx, row) in enumerate(df.iterrows(), start=1):
        vals = ([idx] if index else []) + list(row.values)
        for j, v in enumerate(vals):
            v = _clean(v)
            if v is None:
                ws.write_blank(r0 + i, c0 + j, None)
            elif isinstance(v, int) and not isinstance(v, bool):
                ws.write_number(r0 + i, c0 + j, v)
            elif isinstance(v, float) and fmt_n is not None:
                ws.write_number(r0 + i, c0 + j, v, fmt_n)
            else:
                ws.write(r0 + i, c0 + j, v)
    return r0 + len(df) + 1


def model_str(r: ModelResult, sep: str = ",") -> str:
    return "ARDL(" + sep.join(str(r.order[k]) for k in ["p", *r.spec.x_cols]) + ")"


def disp_name(c: str, spec) -> str:
    """Dahili sütun adlarını model türüne göre görüntüleme adına çevirir."""
    c = c.replace("SÜE", spec.dep_short)
    if spec.word != "büyüme":
        c = c.replace("büyüme", spec.word).replace("Büyüme", spec.word.capitalize())
    return c


def write_sector_workbook(path: Path, r: ModelResult, meta: dict, inputs_q: pd.DataFrame,
                          monthly: pd.Series, assumptions: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    sp = r.spec
    with pd.ExcelWriter(path, engine="xlsxwriter") as xw:
        wb = xw.book
        H = wb.add_format({"bold": True, "bg_color": "#1F3864", "font_color": "white", "border": 1, "text_wrap": True})
        T = wb.add_format({"bold": True, "font_size": 14})
        B = wb.add_format({"bold": True})
        N2 = wb.add_format({"num_format": "0.00"})
        N4 = wb.add_format({"num_format": "0.0000"})
        OK = wb.add_format({"font_color": "#006100", "bg_color": "#C6EFCE"})
        BAD = wb.add_format({"font_color": "#9C0006", "bg_color": "#FFC7CE"})

        # ---------------- Özet
        ws = wb.add_worksheet("Özet")
        ws.set_column(0, 0, 38)
        ws.set_column(1, 6, 18)
        ws.write(0, 0, f"{r.code} – {meta.get('name', '')}", T)
        info = [
            ("NACE düzeyi", meta.get("level")),
            ("Veri kaynağı", sp.dep_source),
            ("Son açıklanan ay", meta.get("last_month")),
            ("Model", f"{model_str(r, ', ')}  –  AIC ile seçildi (maks. 4 gecikme); sıra: p, " + ", ".join(sp.x_labels[x] for x in sp.x_cols)),
            ("Bağımlı değişken", f"Δlog({sp.dep_short})"),
            ("Açıklayıcılar", ", ".join(f"Δlog({sp.x_labels[x]})" for x in sp.x_cols) + " ve gecikmeleri"),
            ("Kukla değişkenler", meta.get("dummies", "Yok")),
            ("Tahmin örneklemi", f"{r.sample[0]} – {r.sample[1]}"),
            ("Gözlem sayısı", r.nobs),
            ("R²", r.ols.rsquared),
            ("Düzeltilmiş R²", r.ols.rsquared_adj),
            ("AIC (EViews ölçeği, seçilen model)", r.aic),
            ("Mevsimsellik testi (F, p)", f"F={r.season['F']:.2f}, p={r.season['F_p']:.3f}; KW p={r.season['KW_p']:.3f}"),
            ("Mevsimsellik arındırması", "Uygulandı" if r.season["mevsimsel"] else sp.sa_note),
            ("Uzun vade yakınsaması", f"{r.convergence.get('Yakınsama', '')}: tahmin, sektörün tarihsel göreli {sp.word}{'si' if sp.word.endswith('e') else 'i'} x {sp.anchor_label}ne yaklaştırılır"),
            (f"Göreli {sp.word} oranı (ham → sınırlı [0, 2])",
             f"{r.convergence.get('Göreli büyüme oranı (ham)', float('nan')):.2f} → {r.convergence.get('Göreli büyüme oranı (sınırlı)', float('nan')):.2f}"
             f"  (pencere {r.convergence.get('Tarihsel pencere', '')})"),
            ("Çalıştırma tarihi", meta.get("run_date")),
        ]
        for i, (k, v) in enumerate(info, start=2):
            ws.write(i, 0, k, B)
            if isinstance(v, float):
                ws.write_number(i, 1, v, N4)
            else:
                ws.write(i, 1, _clean(v))
        row = 2 + len(info) + 1
        for n in r.notes:
            ws.write(row, 0, "Not: " + n)
            row += 1

        ws.write(row, 0, "Tanı testleri", T)
        row += 1
        d = r.diagnostics.copy()
        _write_df(ws, d, row, 0, H, N4, index=False)
        for i, res in enumerate(d["Sonuç (%5)"], start=1):
            ws.write(row + i, 4, res, OK if res == "Geçti" else BAD)
        row += len(d) + 2

        ws.write(row, 0, f"Yıllık ortalama {sp.word}: gerçekleşen vs. model (ham) vs. yakınsamalı tahmin (%)", T)
        row += 1
        a = r.annual.copy()
        a.columns = [disp_name(c, sp) for c in a.columns]
        a_start = row
        _write_df(ws, a, row, 0, H, N2, index=False)
        a_end = row + len(a)
        row = a_end + 2

        ch = wb.add_chart({"type": "column"})
        ch.add_series({"name": "Gerçekleşen", "categories": ["Özet", a_start + 1, 0, a_end, 0],
                       "values": ["Özet", a_start + 1, 1, a_end, 1], "fill": {"color": "#1F3864"}})
        ch.add_series({"name": "Model / Tahmin (ham)", "categories": ["Özet", a_start + 1, 0, a_end, 0],
                       "values": ["Özet", a_start + 1, 2, a_end, 2], "fill": {"color": "#ED7D31"}})
        ch.add_series({"name": "Yakınsamalı tahmin", "categories": ["Özet", a_start + 1, 0, a_end, 0],
                       "values": ["Özet", a_start + 1, 3, a_end, 3], "fill": {"color": "#70AD47"}})
        ch.set_title({"name": f"{r.code} – Yıllık ortalama {sp.dep_short} {sp.word}{'si' if sp.word.endswith('e') else 'i'} (%)"})
        ch.set_x_axis({"name": "Yıl", "num_font": {"rotation": -45}})
        ch.set_y_axis({"name": "%", "major_gridlines": {"visible": True, "line": {"color": "#D9D9D9"}}})
        ch.set_legend({"position": "bottom"})
        ch.set_size({"width": 900, "height": 400})
        ws.insert_chart(a_start, 5, ch)

        # ---------------- Katsayılar
        ws2 = wb.add_worksheet("Katsayılar")
        res = r.ols
        coef = pd.DataFrame({"Katsayı": np.asarray(res.params), "Std. hata": np.asarray(res.bse),
                             "t": np.asarray(res.tvalues), "p": np.asarray(res.pvalues)},
                            index=pd.Index(r.design_cols, name="Değişken"))
        ws2.set_column(0, 0, 22)
        ws2.set_column(1, 4, 14)
        ws2.write(0, 0, "ARDL katsayıları (OLS)", T)
        rr = _write_df(ws2, coef, 2, 0, H, N4)
        ws2.write(rr + 1, 0, "AIC'ye göre en iyi 10 model", T)
        if not r.select_table.empty:
            ws2.set_column(6, 7, 30)
            _write_df(ws2, r.select_table, rr + 2, 0, H, N4, index=False)
        # uzun dönem çarpanları
        lr = []
        p = r.order["p"]
        ar = sum(res.params.get(f"dly(-{i})", 0.0) for i in range(1, p + 1))
        for x in sp.x_cols:
            sx = sum(v for k, v in res.params.items() if k.startswith(x))
            lr.append({"Değişken": sp.x_labels[x], "Uzun dönem esnekliği": sx / (1 - ar) if abs(1 - ar) > 1e-8 else np.nan})
        ws2.write(rr + 15, 0, "Uzun dönem esneklikleri  Σβ / (1 − Σφ)", T)
        _write_df(ws2, pd.DataFrame(lr), rr + 16, 0, H, N4, index=False)

        # ---------------- Çeyreklik
        q = r.quarterly.copy()
        q = q.join(inputs_q, how="left")
        q.columns = [disp_name(c, sp) for c in q.columns]
        q.index.name = "Çeyrek"
        ws3 = wb.add_worksheet("Çeyreklik")
        ws3.set_column(0, 0, 10)
        ws3.set_column(1, len(q.columns), 16)
        ws3.freeze_panes(1, 1)
        _write_df(ws3, q, 0, 0, H, N4)

        # ---------------- Aylık veri
        ws4 = wb.add_worksheet(f"Aylık {sp.dep_short}")
        m = monthly.dropna().to_frame(sp.dep_unit)
        m.index.name = "Ay"
        _write_df(ws4, m, 0, 0, H, N4)
        ws4.set_column(0, 1, 18)

        # ---------------- Varsayımlar
        ws5 = wb.add_worksheet("Varsayımlar")
        ws5.set_column(0, 8, 20)
        rr = 0
        for title, df in assumptions.items():
            ws5.write(rr, 0, title, T)
            rr = _write_df(ws5, df, rr + 1, 0, H, N4) + 2

        # ---------------- Model çıktısı
        ws6 = wb.add_worksheet("Model Çıktısı")
        ws6.set_column(0, 0, 110)
        mono = wb.add_format({"font_name": "Consolas", "font_size": 9})
        for i, line in enumerate(str(res.summary()).splitlines()):
            ws6.write(i, 0, line, mono)


def write_summary_workbook(path: Path, results: list[ModelResult], metas: dict, run_info: dict):
    rows = []
    for r in results:
        a = r.annual.set_index("Yıl")
        d = r.diagnostics.set_index("Test")["Sonuç (%5)"]
        row = {"NACE": r.code, "Ad": metas[r.code].get("name", ""), "Düzey": metas[r.code]["level"],
               "Model": model_str(r),
               "Örneklem": f"{r.sample[0]}–{r.sample[1]}", "N": r.nobs, "Düz. R²": r.ols.rsquared_adj}
        row[f"Göreli {r.spec.word} oranı"] = r.convergence.get("Göreli büyüme oranı (sınırlı)")
        for yr in sorted(a.index):
            g = a.loc[yr]
            if pd.notna(g["Gerçekleşen büyüme %"]):
                if yr >= sorted(a.index)[-1] - 9:
                    row[f"{yr} Gerçekleşen %"] = g["Gerçekleşen büyüme %"]
                continue
            row[f"{yr} Model (ham) %"] = g["Model / Tahmin büyüme %"]
            row[f"{yr} Yakınsamalı %"] = g["Yakınsamalı tahmin büyüme %"]
        for t in ["Breusch-Godfrey LM (gecikme 4)", "Breusch-Pagan-Godfrey", "Jarque-Bera", "ARCH LM (gecikme 4)", "Ramsey RESET"]:
            row[t] = d.get(t)
        rows.append(row)
    df = pd.DataFrame(rows)
    with pd.ExcelWriter(path, engine="xlsxwriter") as xw:
        df.to_excel(xw, sheet_name="Tüm Sektörler", index=False)
        ws = xw.sheets["Tüm Sektörler"]
        ws.set_column(0, 0, 8)
        ws.set_column(1, 1, 50)
        ws.set_column(2, len(df.columns), 14)
        ws.freeze_panes(1, 2)
        fmt = xw.book.add_format({"num_format": "0.00"})
        for j, c in enumerate(df.columns):
            if df[c].dtype.kind == "f":
                ws.set_column(j, j, 14, fmt)
        ok = xw.book.add_format({"font_color": "#006100", "bg_color": "#C6EFCE"})
        bad = xw.book.add_format({"font_color": "#9C0006", "bg_color": "#FFC7CE"})
        for j, c in enumerate(df.columns):
            if c in ["Breusch-Godfrey LM (gecikme 4)", "Breusch-Pagan-Godfrey", "Jarque-Bera", "ARCH LM (gecikme 4)", "Ramsey RESET"]:
                ws.conditional_format(1, j, len(df), j, {"type": "cell", "criteria": "==", "value": '"Geçti"', "format": ok})
                ws.conditional_format(1, j, len(df), j, {"type": "cell", "criteria": "==", "value": '"Kaldı"', "format": bad})
        info = pd.DataFrame(list(run_info.items()), columns=["Bilgi", "Değer"])
        info.to_excel(xw, sheet_name="Çalıştırma Bilgisi", index=False)
        xw.sheets["Çalıştırma Bilgisi"].set_column(0, 1, 60)


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s)
