const POLL_INTERVAL = 5000;
let polling = false;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('message', (event) => {
    if (event.data === 'start-polling') startPolling();
    if (event.data === 'stop-polling') polling = false;
});

async function startPolling() {
    if (polling) return;
    polling = true;
    while (polling) {
        try {
            const clients = await self.clients.matchAll({ type: 'window' });
            const anyVisible = clients.some(c => c.visibilityState === 'visible');
            if (!anyVisible && clients.length > 0) {
                const resp = await fetch('/api/latest');
                const data = await resp.json();
                if (data.has_data) {
                    const areas = (data.alerts || []).flatMap(a => a.areas || []).join(', ');
                    self.registration.showNotification('זוהה שיגור!', {
                        body: areas || 'התכוננו להתמגן',
                        icon: '/icon.svg',
                        tag: 'air-command-alert',
                        renotify: true
                    });
                }
            }
        } catch (e) {}
        await new Promise(r => setTimeout(r, POLL_INTERVAL));
    }
}

startPolling();
