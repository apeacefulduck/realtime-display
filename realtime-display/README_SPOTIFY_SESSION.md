# HomeFlow Spotify — Render Free + Supabase Free

5 Ekim 2026: Düzeltme Render Free ve Vercel'de canlıya alındı (kod commit fef40cf). Kullanıcının mevcut Supabase secret key'i yalnız Render sunucu ortamına eklendi. Gerçek Spotify oturumu şifreli olarak Supabase'e taşındı. Üç gerçek Render restart ve USB'den gerçek ESP32 restart sonrasında oturum otomatik geri yüklendi. 53 backend testi, frontend regresyonları ve syntax kontrolü geçti.

## İnceleme

Eski kod access/refresh tokenlarını Fernet ile şifreleyip RAM, backend .spotify-session dosyası ve tarayıcı localStorage session'ında tutuyordu. ESP32'de OAuth tokenı yoktu. Render Free dosya sistemi kalıcı olmadığından servis restart/deploy sırasında dosya kaybolabilir. Varsayılan dosya yolu çalışma dizinine bağlıydı; okuma hataları sessizce geçiliyordu.

Eski kodla yeniden üretildi: browser yokken startup refresh çağrısı 0, connected False; eski browser session'ı rotasyonla güncellenmiş refresh tokenı RAM/diskte geri ezebiliyor; API 401'inden sonra token endpointi 503'ü de authorization 401'e dönüşüyor. Kanıt: before-evidence.log. Canlı kapanış geçmişindeki tek tek token kaybı olayları loglardan doğrulanmadı.

30 saniye önceden expiry refresh ve 401 sonrası tek retry zaten vardı. Refresh_token alanı yoksa eski değer korunuyordu, fakat null/boş değer korunmuyordu. Yapay 30 günlük şifreli session TTL'i de geçerli Spotify yetkisini sonlandırabiliyordu. show_dialog=true, zorunlu tekrar onay veya otomatik OAuth redirect yoktu; OAuth state doğrulaması ve 600 saniyelik TTL korunuyor.

## Yeni davranış

İlk OAuth → backend tokenları alır → Fernet ciphertext Supabase'e kaydedilir.

Render açılır → Supabase kaydı okunur → refresh token ile yeni access token alınır → playback ve ESP32 state browser gerektirmeden geri yüklenir.

- SpotifyAuthManager auth/expiry/rotasyonun tek sahibidir. Async lock ile 12 paralel istekte tek refresh.
- Token süresinden 30 saniye önce veya API 401'inde refresh; en fazla bir retry. Refresh token yok/null/boş ise eski değer korunur. Yeni token playback çağrısından önce kalıcı kayda yazılır.
- Frontend yalnız şifreli session_id alır. Spotify tokenları ve Supabase secret key frontend/ESP32'ye gönderilmez. Eski browser session'ı hesabı doğrulayabilir, güncel tokenları ezemez.
- invalid_grant kalıcı invalid işareti oluşturur; eski browser session'ı revoked yetkiyi canlandıramaz. Ağ/5xx/invalid_client/429/bozuk response yetkiyi silmez.
- Startup store kesintisi session_store_unavailable olarak yayınlanır; bağlı client varken tekrar okunur, erişim geri geldiğinde OAuth gerektirmeden restore edilir. Bilinmeyen remote state eski browser tokenıyla ezilmez.
- Store I/O ayrı thread'de çalışır; WebSocket/event loop bloklanmaz. Görüntüleme açık/kapalı ayarı kalıcıdır.
- Dinamik tokenlar .env'e yazılmaz ve loglanmaz. Şifreleme mevcut SPOTIFY_CLIENT_SECRET'ten türetilir; bu secret değişirse eski kayıt çözülemez. Dosya store ve geçici dosyalar Git dışındadır.

## Supabase ve Render ayarları

Proje: https://slnmmiirixgebphazlle.supabase.co. Kuruluş homeflow, plan Free. Migration homeflow_spotify_session_store; SQL kaynağı homeflow_sessions.sql.

public.homeflow_sessions tek spotify slot'unda yalnız ciphertext tutar. RLS açık, public/anon/authenticated erişimi kaldırıldı; service_role SELECT/INSERT/UPDATE yapabilir. Policy olmaması deny-by-default için bilinçlidir. Advisor'ın [RLS enabled no policy INFO](https://supabase.com/docs/guides/database/database-linter?lint=0008_rls_enabled_no_policy) kaydı bu durum içindir.

Canlı sorgu: RLS true, anon_can_read false, authenticated_can_read false, backend_can_use true. SQL insert/read rollback ile geçti. Public anahtarla gerçek REST read HTTP 401 ile reddedildi. Gerçek oturum kaydı tek satır Fernet ciphertext olarak mevcut; doğrulamada yalnız kayıt sayısı/şifreli zarf bilgisi okundu, tokenlar rapora alınmadı.

Render Environment:

```text
HOMEFLOW_SESSION_STORE=supabase
HOMEFLOW_SUPABASE_URL=https://slnmmiirixgebphazlle.supabase.co
HOMEFLOW_SUPABASE_SECRET_KEY=<bu projenin mevcut sb_secret_... anahtarı>
```

Modern secret key apikey header'ında kullanılır; legacy service_role JWT de desteklenir. Publishable/anon key yeterli değildir. Secret yalnız Render sunucu ortamında saklanır; sohbet veya frontend'e konulmaz. Ücretli Render planı/disk gerekmiyor.

Mevcut Spotify client/secret/redirect URI ve cihaz anahtarı korunmalı. Tek Uvicorn worker kullanılmalı; çoklu worker/instance bu tek ev mimarisinde desteklenmez. Backend dosyaları birlikte ve frontend/script.js Vercel'e dağıtılmalı. Store konfigürasyonu hazır olmadan yeni sürümü Free servise dağıtmayın.

Eski browser session'ı varsa ilk frontend açılışında Supabase'e aktarılabilir; zaten geçersiz/kayıpsa bir kez OAuth gerekir. Bundan sonraki normal açılışlar Supabase kaydını kullanır.

Health: version 2026-10-05-spotify-persistence; kayıt sonrası spotify_session_stored true, spotify_authorization_required false, spotify_session_store_unavailable false.

Yerel PC servisi için HOMEFLOW_SESSION_STORE=file varsayılandır. Windows yolu %LOCALAPPDATA%/HomeFlow/.spotify-session; Linux $XDG_DATA_HOME/HomeFlow/.spotify-session veya ~/.local/share/HomeFlow/.spotify-session. SPOTIFY_SESSION_FILE kaynak deposu dışında özel yol seçer.

Supabase Free, düşük aktiviteyle yaklaşık 7 günlük dönemde projeyi duraklatabilir; uzun süre kullanılmayan proje panelden tekrar açılmalıdır. Normal token refresh kayıtları veritabanı aktivitesi oluşturur. Render Free cold start gecikmesi devam eder.

## Testler ve sınırlar

53 backend testi geçti. İlk bağlantı/callback, 4 bağımsız player başlangıcı, başka cwd'den 3 ayrı süreç restart, expiry, 401 retry, refresh token yok/null/boş/rotasyon, revoke/bozuk kayıt, 12 paralel çağrı ve WebSocket reconnect test edildi. Supabase ciphertext upsert/yeni service restore, HTTP hata ayrımı, yanlış config'te ephemeral dosyaya düşmeme, store kesintisinden otomatik kurtarma ve event loop açık kalması da sınandı.

Play/pause/resume/next/previous, paused metadata, artwork, aktif cihaz, diğer shopping/weather stream'leri ve browser olmadan polling regresyonları geçti. Firmware değişmedi.

Canlıda üç ardışık Render restart'ta worker 65 → 63 → 64 oldu. Her kontrolde session_stored true, authorization_required false, session_store_unavailable false ve cihaz bağlantısı mevcut. Başlangıç loglarında Stored session found → Restoring session → Access token refreshed → Session restored successfully görüldü. COM8 ESP32 firmware yazmadan resetlendi; boot, WiFi ve Render WebSocket yeniden bağlantısı seri olaylarıyla doğrulandı. Son canlı Spotify state: connected true, enabled true, error boş. WebSocket ping/pong ve spotify/display/weather stream'leri, hava API'sinin 7 günlük yanıtı doğrulandı.

Kontrol sırasında aktif Spotify oynatma cihazı veya parça yoktu. Gerçek şarkı/album art ve play/pause etkisi bu canlı kontrolde doğrulanamadı; bu özelliklerin otomatik testleri geçti. Expiry, eksik refresh token ve revoked-token senaryoları kontrollü testlerde sınandı; kullanıcının canlı yetkisi kaldırılmadı. Startup restore sunucunun client mesajlarını kabul etmesinden önce çalışır; kullanıcının tüm tarayıcılarını fiziksel olarak kapatma testi yapılmadı.

HTTP testlerinde Spotify/Supabase taklit yanıtları kullanıldı; ayrı SQL/REST kontrolleri gerçek Supabase üzerinde yapıldı. Mevcut FastAPI/Starlette bağımlılıklarından 8 deprecation uyarısı var.

Kaynaklar: [Spotify refresh](https://developer.spotify.com/documentation/web-api/tutorials/refreshing-tokens), [Render depolama](https://render.com/docs/disks), [Supabase API güvenliği](https://supabase.com/docs/guides/api/securing-your-api), [Free pausing](https://supabase.com/docs/guides/platform/free-project-pausing).
