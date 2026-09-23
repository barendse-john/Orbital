// Orbital service worker: offline shell, last-known data, and notifications.
const SHELL = "orbital-shell-v1";
const DATA = "orbital-data-v1";
const SHELL_FILES = ["/app/", "/app/manifest.webmanifest", "/app/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((k) => k !== SHELL && k !== DATA).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

// Network first for everything, so the app is never stale while online;
// the cache only answers when the Pi or the phone's connection is down.
self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  const cacheable = url.pathname.startsWith("/app") || url.pathname === "/api/launches"
    || url.pathname === "/api/me" || url.pathname === "/api/me/briefing";
  if (!cacheable) return;
  e.respondWith(fetch(req).then((res) => {
    if (res.ok) {
      const copy = res.clone();
      caches.open(url.pathname.startsWith("/api") ? DATA : SHELL).then((c) => c.put(req, copy));
    }
    return res;
  }).catch(() => caches.match(req, { ignoreVary: true }).then((hit) => hit ||
    (url.pathname.startsWith("/app") ? caches.match("/app/") : Response.error()))));
});

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { title: "Orbital", body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "Orbital", {
    body: d.body || "", tag: d.tag, renotify: !!d.tag, icon: "/app/icon-192.png",
    badge: "/app/icon-192.png", data: { url: d.url || "/app/", link: d.link || "" },
    actions: d.link ? [{ action: "watch", title: "▶ Watch" }] : [],
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const { url, link } = e.notification.data || {};
  const target = e.action === "watch" && link ? link : url || "/app/";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    if (!target.startsWith("http")) {
      for (const w of wins) {
        if (w.url.includes("/app")) { w.focus(); return w.navigate(target); }
      }
    }
    return self.clients.openWindow(target);
  }));
});
