/* 每日影视简报 · Service Worker —— 自毁版（2026-09-04 起）
 *
 * 为什么改成自毁：
 *   服务器（server.py）已强制给 HTML 写了 no-cache / no-store / must-revalidate，
 *   每次打开都会向服务器要最新一版，Service Worker 不再是必需品。
 *   而老版 SW 曾在本地存过整份 HTML（缓存名 film-brief-v1），
 *   成了「点开还是昨天」的头号嫌疑 —— 服务器再新，手机也可能拿到本地那份旧货。
 *
 * 所以本 SW 只做一次清扫：
 *   ① 清空本机所有历史缓存  ② 注销自己  ③ 不拦截任何请求
 * 清扫完成后，缓存只由服务器响应头说了算。
 */
self.addEventListener('install', function () {
  self.skipWaiting();
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) { return caches.delete(k); }));
    }).catch(function () {}).then(function () {
      return self.registration.unregister();
    }).catch(function () {}).then(function () {
      return self.clients.claim();
    }).catch(function () {})
  );
});

/* 故意不写 fetch 监听：SW 不拦截任何请求，全部直连服务器。 */
