const CACHE_NAME = 'vidos-v1';
const assets = [
    '/',
    '/static/app.css',
    '/static/manifest.json',
    '/static/img/icon-192.png',
    '/static/img/icon-512.png'
];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            return cache.addAll(assets);
        })
    );
});

self.addEventListener('fetch', event => {
    event.respondWith(
        caches.match(event.request).then(response => {
            return response || fetch(event.request);
        })
    );

    // Push Notification Event
    self.addEventListener('push', function (event) {
        if (event.data) {
            const data = event.data.json();
            const options = {
                body: data.body,
                icon: '/static/img/icon-192.png',
                badge: '/static/img/icon-96.png',
                vibrate: [100, 50, 100],
                data: {
                    dateOfArrival: Date.now(),
                    url: data.url || '/'
                }
            };
            event.waitUntil(
                self.registration.showNotification(data.title || 'Vidos', options)
            );
        }
    });

    // Notification Click Event
    self.addEventListener('notificationclick', function (event) {
        event.notification.close();
        event.waitUntil(
            clients.matchAll({
                type: 'window'
            }).then(function (clientList) {
                const url = event.notification.data.url;

                // Check if window is already open
                for (let i = 0; i < clientList.length; i++) {
                    const client = clientList[i];
                    if (client.url === url && 'focus' in client) {
                        return client.focus();
                    }
                }
                // If not, open new window
                if (clients.openWindow) {
                    return clients.openWindow(url);
                }
            })
        );
    });
