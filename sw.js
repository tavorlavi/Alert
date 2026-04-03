const POLL_INTERVAL = 5000;
const CACHE_NAME = 'alert-shell-v1';
const SHELL_URLS = ['/', '/icon.svg', '/manifest.json'];
let polling = false;
let lastNotifiedKey = null;

self.addEventListener('install', (event) => {
    self.skipWaiting();
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL_URLS))
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        Promise.all([
            self.clients.claim(),
            caches.keys().then(keys =>
                Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
            )
        ])
    );
    startPolling();
});

self.addEventListener('message', (event) => {
    if (event.data === 'start-polling') startPolling();
    if (event.data === 'stop-polling') polling = false;
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clients => {
            for (const client of clients) {
                if ('focus' in client) return client.focus();
            }
            return self.clients.openWindow('/');
        })
    );
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);
    if (url.pathname.startsWith('/api/')) return;
    event.respondWith(
        fetch(event.request).catch(() => caches.match(event.request))
    );
});

async function startPolling() {
    if (polling) return;
    polling = true;
    while (polling) {
        try {
            const clients = await self.clients.matchAll({ type: 'window' });
            const anyVisible = clients.some(c => c.visibilityState === 'visible');
            if (!anyVisible) {
                const resp = await fetch('/api/latest');
                const data = await resp.json();
                if (data.has_data) {
                    const areas = (data.alerts || []).flatMap(a => a.areas || []).join(', ');
                    const dedupKey = (data.target_time || data.received_at || '') + '|' + areas;
                    if (dedupKey !== lastNotifiedKey) {
                        lastNotifiedKey = dedupKey;
                        self.registration.showNotification('זוהה שיגור!', {
                            body: areas || 'התכוננו להתמגן',
                            icon: '/icon.svg',
                            tag: 'air-command-alert',
                            renotify: true,
                            data: { url: '/' }
                        });
                    }
                } else {
                    lastNotifiedKey = null;
                }
            }
        } catch (e) {}
        await new Promise(r => setTimeout(r, POLL_INTERVAL));
    }
}

startPolling();
