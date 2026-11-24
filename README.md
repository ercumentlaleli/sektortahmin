# Sektörel Sanayi Üretimi ve Üretici Fiyatları Tahmin Modeli (ARDL)

TÜİK verileriyle 2'li ve 3'lü NACE kırılımlarında her ay çalıştırılan iki ARDL tahmin seti:

| Model | Bağımlı değişken | Açıklayıcılar | Çıktı klasörü |
|---|---|---|---|
| **SÜE** | Mevsim ve takvim etkisinden arındırılmış sanayi üretim endeksi | TR GSYH (MA), IMF küresel GSYH, REDK | `outputs/<YYYY-AA>/` |
| **Yİ-ÜFE** | Yurt içi üretici fiyat endeksi (2003=100) | Yİ-ÜFE genel (Excel E), Dolar/TL (Excel H), Brent (EVDS) | `outputs/<YYYY-AA>/ufe/` |

İki model aynı yöntemi kullanır: çeyreklik ortalama, Δlog, mevsimsellik testi, AIC ile ARDL, tanı testleri, dinamik tahmin, uzun vade yakınsaması.

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

Opsiyonlar: `--models sue,ufe` (varsayılan ikisi), `--end-year 2032` (varsayılan) (tahmin ufku), `--only C24,C241` (yalnızca seçilen kodlar), `--levels 2,3`.

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
| Uzun vade yakınsaması | Yargısal düzeltme, ham ARDL tahmini ayrıca raporlanır. büyüme_t = w_t·model_t + (1−w_t)·hedef_t. Hedef = göreli büyüme oranı × TR GSYH büyümesi. Oran = sektörün 2013Q1'den son tam yıla ortalama Δlog büyümesi / GSYH'nin ortalama Δlog büyümesi, [0, 2] aralığında sınırlı. w = 1 (2028Q4'e kadar), 2029Q1'den ufuk sonuna doğrusal olarak 0'a iner. Ayarlar: `sektortahmin/model.py` → `CONV_START`, `REL_START`, `RATIO_BOUNDS`. |
| 8. Yıllığa çevirme | Yıllık ortalama endeks büyümesi. Geçmiş yıllarda "Model / Tahmin" sütunu, tek adım ileri model içi uyumdan hesaplanır. |

## Notlar

* TÜİK'in bazı 3'lü kırılımları (ör. C191, C192, C211) gizlilik nedeniyle yayımlanmadığı için modellenemez. Bu kodlar özet dosyasının *Çalıştırma Bilgisi* sayfasında listelenir.
* Aynı gün yeniden çalıştırmada TÜİK verisi `data/cache/<tarih>/` önbelleğinden okunur.

## Yİ-ÜFE modeli

| Adım | Uygulama |
|---|---|
| Bağımlı değişken | TÜİK SDMX `DF_YIUFE_EDO_V1`, endeks (2003=100). FAAL_GRUP 4 = 2'li, 5 = 3'lü. Aylık → çeyrek ortalaması. Örneklem 2005Q1'den. |
| Kısmi çeyrek | SÜE'den farklı olarak, eksik aylar açıklanan ayların ortalamasıyla değil doldurularak tamamlanır. Kalan aylar = sektörün son değeri × Excel E'nin (genel Yİ-ÜFE) aylık değişimi. Bu çeyrek gerçekleşen değil tahmin sayılır. Yıl bar grafikte "Tahmin (3 çeyrek gerçekleşen; Q4: n ay gerçekleşen + kalan aylar tahmini)" olarak görünür. |
| Yİ-ÜFE genel | Gerçekleşen: TÜİK genel endeks (B-E36). Gelecek: Excel *Aylık Kur Tahminleri* E sütununun aylık % değişimleri son gerçekleşen aydan zincirlenir. |
| Dolar/TL | Gerçekleşen: EVDS `TP.DK.USD.A.YTL` aylık ortalama (tamamlanmış aylar). Kısmi çeyrekteki ve sonraki aylar: Excel H sütununun aylık % değişimleri. |
| Brent | Gerçekleşen: EVDS `TP.BRENTPETROL.EUBP` aylık ortalama (tamamlanmış aylar). Kısmi çeyreğin kalan ayları son gerçekleşen ayın değeriyle sabit tutulur; aylık Brent projeksiyonu yok. Sonraki yıllar: IMF WEO ortalama petrol fiyatı `G001.POILAPSP` yıllık % değişimleri. WEO ufku (2031) sonrasında son yılın değişimi sabit tutulur. |
| Uzun vade yakınsaması | Hedef = sektörün tarihsel göreli fiyat değişimi oranı × genel Yİ-ÜFE değişimi. |
| Dışlanan seriler | Tahmin örneklemi 40 çeyrekten kısa olanlar ve yayımı 12 aydan uzun süredir durmuş olanlar. |

## Doğrulama: birim kök testleri ve tahmin performansı

| Bölüm | Uygulama |
|---|---|
| Birim kök | Her sektörün bağımlı değişkeni ve açıklayıcılar için ADF (gecikme AIC ile) ve Phillips-Perron (`arch` paketi). Log düzeyde sabit + trend, Δlog'da sabit. H0: birim kök var. |
| Örneklem içi | Tek adım ileri kalıntılardan ME, MAE, RMSE (çeyreklik log-değişim, yüzde puan). Theil U = model RMSE / naif RMSE. |
| Çapraz doğrulama | Zaman serisi çapraz doğrulaması (genişleyen pencere, kaydırmalı başlangıç). Son 12 çeyreğin her birinde yalnızca o güne kadarki veriyle gecikmeler AIC ile yeniden seçilir, model yeniden tahmin edilir, 1–4 çeyrek sonrası dinamik olarak tahmin edilir. Açıklayıcıların gerçekleşen değerleri kullanılır (koşullu tahmin). Ufuk bazında ve 4 çeyreklik kümülatif ME, MAE, RMSE, Theil U raporlanır. Naif ölçüt: o güne kadarki ortalama değişim (sürüklenmeli rassal yürüyüş). `--no-cv` ile kapatılabilir. |
| Çıktılar | Excel: Özet sayfasında "Tahmin performansı" ve "Birim kök testleri" tabloları, "Çapraz Doğrulama" sayfası; özet dosyada RMSE, Theil U, ADF/PP sütunları. Streamlit: sektör sayfasında "Tahmin performansı" ve "Birim kök testleri" sekmeleri, genel bakışta RMSE ve Theil U sütunları. |
