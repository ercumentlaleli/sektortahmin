# Sektörel Sanayi Üretimi Tahmin Modeli (ARDL)

TÜİK sanayi üretim endeksinin (SÜE) 2'li ve 3'lü NACE kırılımları için her ay çalıştırılan ARDL tahmin sistemi.

## Aylık çalıştırma

```bash
pip install -r requirements.txt
export TUIK_API_KEY=...   # TÜİK SDMX API anahtarı
export EVDS_API_KEY=...   # TCMB EVDS API anahtarı
python run_monthly.py --excel "inputs/<yeni_makro_excel>.xlsx"
```

## Arayüz (Streamlit)

```bash
streamlit run app.py
```

* **Genel bakış**: tüm sektörlerin yıllık gerçekleşen ve tahmin büyümeleri, tanı testi özeti
* **Sektör detayı**: yıllık bar grafik, çeyreklik düzey grafiği, tanı testleri, katsayılar, sektör Excel'ini indirme
* **Varsayımlar**: Türkiye/küresel büyüme varsayımları, açıklayıcı değişken projeksiyonları
* **Yeni çalıştırma**: yeni makro Excel'i yükleyip modeli arayüzden çalıştırma (API anahtarları ortamda tanımlı olmalı)
* Yan menü: özet Excel ve tüm sektör dosyalarını (zip) indirme

Çıktılar `outputs/<YYYY-AA>/` altına yazılır:

* `Ozet_Tum_Sektorler_<YYYY-AA>.xlsx`: tüm sektörlerin model, yıllık büyüme ve tanı testi özeti
* `sektorler/2li/<NACE>.xlsx`, `sektorler/3lu/<NACE>.xlsx`: her NACE kodu için ayrı çalışma kitabı
  * **Özet**: model bilgisi, tanı testleri, gerçekleşen ve tahmin yıllık büyüme tablosu, **bar grafik**
  * **Katsayılar**: ARDL katsayıları, AIC'ye göre en iyi 10 model, uzun dönem esneklikleri
  * **Çeyreklik**: çeyreklik SÜE, açıklanan ay sayısı, Δlog, model içi uyum, tahmin, açıklayıcılar (gerçekleşen/projeksiyon etiketli)
  * **Aylık SÜE**: TÜİK'ten çekilen aylık mevsim ve takvim etkisinden arındırılmış endeks
  * **Varsayımlar**: Türkiye/küresel büyüme varsayımları, REDK projeksiyonu, mevsimsellik testleri
  * **Model Çıktısı**: tam regresyon çıktısı

Opsiyonlar: `--end-year 2032` (varsayılan) (tahmin ufku), `--only C24,C241` (yalnızca seçilen kodlar), `--levels 2,3`.

## Yöntem

| Adım | Uygulama |
|---|---|
| 1. Bağımlı değişken | TÜİK SDMX (`nsiws.tuik.gov.tr`): `DF_SANAYI_URETIM_ENDEKS_ANA_C` (bölüm, 2'li) ve `DF_SANAYI_URETIM_ENDEKS_ALT_C` (grup, 3'lü), `SEASONAL_ADJUST=Y` (mevsim ve takvim etkisinden arındırılmış), endeks 2021=100. Aylık veri çeyrek ortalamasına çevrilir. Çeyrekte 2 ay açıklanmışsa 2 ayın, 1 ay açıklanmışsa o ayın değeri kullanılır. |
| 2. Açıklayıcılar | Türkiye GSYH: EVDS `TP.GSYIH30.HY.B1GQ` (MA zincirlenmiş hacim endeksi). Küresel GSYH: IMF `QGDP_WCA` / `G001.B1GQ_S1_Q.IX.Q` (sabit fiyat, MA endeks, 2012Q1'den itibaren). REDK: EVDS `TP.RK.T1.Y` (TÜFE bazlı, aylık veriden tam çeyrek ortalaması). |
| Dönüşüm | Tüm seriler için Δlog |
| 3. Mevsimsellik | Δlog serilerde çeyrek kuklaları F testi ve Kruskal-Wallis testi. İki test de %5'te anlamlıysa çeyrek etkileri arındırılır. |
| ARDL | Δlog(SÜE) = c + Σφᵢ Δlog(SÜE)₍t−i₎ + Σ β Δlog(X)₍t−j₎. p = 1..4, her X için q = 0..4. 500 aday modelin hepsi aynı örneklemde AIC ile karşılaştırılır (EViews mantığı). Üç açıklayıcı da her modelde en az eşanlı olarak yer alır. |
| Örneklem | Bağımlı ve tüm açıklayıcıların gerçekleşen olduğu ortak dönem. Küresel GSYH serisi nedeniyle başlangıç 2012Q2, ilk 4 çeyrek gecikmeler için ayrıldığından tahmin 2013Q2'den başlar. |
| COVID kuklaları | Varsayılan olarak **kullanılmaz**. `--covid-dummy` ile 2020Q2 ve 2020Q3 impuls kuklaları her aday modele eklenir (tahmin döneminde 0). |
| 4. Tanı testleri | Breusch-Godfrey LM (1 ve 4 gecikme), Breusch-Pagan-Godfrey, White, ARCH LM(4), Jarque-Bera, Ramsey RESET |
| 5. Küresel projeksiyon | IMF WEO (son yayım) dünya reel büyümesi `G001.NGDP_RPCH`. WEO ufkunun (şu an 2031) ötesindeki yıllarda son WEO yılının büyümesi sabit tutulur. |
| 6. Türkiye projeksiyonu | Makro Excel → *Macro Projections* sayfası, "GSYH büyüme, %" satırı (26. satır) |
| Yıllık → çeyreklik | Yılın eksik çeyrekleri için sabit bir çeyreklik büyüme oranı bulunur. Bu oran, yıllık ortalama endeks büyümesini varsayılan yıllık büyümeye eşitler. |
| 7. REDK projeksiyonu | Excel → *Aylık Kur Tahminleri*: B (TÜFE endeksi), H (Dolar/TL). Çeyrek ortalamalarının % değişimi alınır. REDK = REDK(−1)·(1+TÜFE değ.)/(1+USD değ.) formülü son tam REDK çeyreğinden itibaren uygulanır. |
| Tahmin | Dinamik tahmin. Bağımlı değişken gecikmeleri için gerçekleşen değer varsa o, yoksa tahmin kullanılır. |
| 8. Yıllığa çevirme | Yıllık ortalama endeks büyümesi. Geçmiş yıllarda "Model / Tahmin" sütunu, tek adım ileri model içi uyumdan hesaplanır. |

## Notlar

* TÜİK'in bazı 3'lü kırılımları (ör. C191, C192, C211) gizlilik nedeniyle yayımlanmadığı için modellenemez. Bu kodlar özet dosyasının *Çalıştırma Bilgisi* sayfasında listelenir.
* Aynı gün yeniden çalıştırmada TÜİK verisi `data/cache/<tarih>/` önbelleğinden okunur.
