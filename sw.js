/* Java 后端勇者闯关 · Service Worker
   目标：第一次打开后把整个静态站点缓存下来，之后断网/地铁里也能刷题。
   策略：
     - 导航请求：network-first，失败回退缓存的 index.html
     - 同源静态资源：cache-first（版本化缓存名，发新版自动清理旧缓存）
     - POST / 跨域：完全不拦截（编译服务、AI 接口走网络）
   安全：只缓存同源 GET，绝不缓存任何提交内容。
*/
const CACHE = 'java-rpg-v2';
const CORE = [
  './',
  './index.html',
  './manifest.webmanifest',
  './icon-192.png',
  './icon-512.png'
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(CORE).catch(() => c.add('./index.html')))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k.startsWith('java-rpg-') && k !== CACHE)
        .map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;                       // 编译 / AI 请求不碰
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;        // 跨域不碰

  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req)
        .then(res => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then(c => c.put('./index.html', copy));
          }
          return res;
        })
        .catch(() => caches.match('./index.html').then(r => r || caches.match('./')))
    );
    return;
  }

  e.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      if (res && res.ok && res.type === 'basic') {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(req, copy));
      }
      return res;
    }).catch(() => caches.match('./index.html')))
  );
});

/* 页面里「清除离线缓存」会发这条消息 */
self.addEventListener('message', e => {
  if (e.data === 'CLEAR_CACHE') {
    caches.keys().then(ks => Promise.all(ks.filter(k => k.startsWith('java-rpg-')).map(k => caches.delete(k))));
  }
});
