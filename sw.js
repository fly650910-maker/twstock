/* 台股分析 PWA Service Worker */
const CACHE = 'twstock-v1';
const OFFLINE_URL = '台股分析.html';

/* 安裝時快取主頁面 */
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll([OFFLINE_URL, 'manifest.json']))
      .then(() => self.skipWaiting())
  );
});

/* 啟動時清除舊快取 */
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

/* 攔截請求：HTML 頁面先走網路，失敗才用快取；API 請求永遠直接打網路 */
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  /* API 請求不快取 */
  if (url.hostname.includes('twse.com.tw') ||
      url.hostname.includes('finance.yahoo.com') ||
      url.hostname.includes('corsproxy.io')) {
    return;
  }

  /* HTML / CSS / JS 資源：stale-while-revalidate */
  e.respondWith(
    caches.open(CACHE).then(async cache => {
      const cached = await cache.match(e.request);
      const networkPromise = fetch(e.request).then(res => {
        if (res.ok) cache.put(e.request, res.clone());
        return res;
      }).catch(() => null);
      return cached || networkPromise || new Response('離線中，請稍後再試', {status: 503});
    })
  );
});
