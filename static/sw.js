// Family Dashboard Service Worker
// Handles caching for offline support and Web Push notifications.

const CACHE_NAME = "family-dashboard-v3";

// Only cache static assets and the offline page — NOT authenticated pages.
// Caching authenticated routes in the install phase fails for users who are
// not yet logged-in (the server returns a 302 redirect which cannot be cached).
const ASSETS_TO_CACHE = [
    "/offline",
    "/static/manifest.json",
];

// ---------------------------------------------------------------------------
// Install – pre-cache essential assets
// ---------------------------------------------------------------------------

self.addEventListener("install", (event) => {
    console.log("[SW] Installing service worker v3...");

    event.waitUntil(
        caches.open(CACHE_NAME)
            .then((cache) => {
                // Use {cache: 'no-cache'} so we always get the freshest offline page.
                return Promise.allSettled(
                    ASSETS_TO_CACHE.map((url) =>
                        cache.add(new Request(url, { cache: "no-cache" }))
                    )
                );
            })
            .then(() => {
                console.log("[SW] Pre-cache complete.");
                // Activate immediately without waiting for old tabs to close.
                return self.skipWaiting();
            })
    );
});

// ---------------------------------------------------------------------------
// Activate – clean up old caches
// ---------------------------------------------------------------------------

self.addEventListener("activate", (event) => {
    console.log("[SW] Activating service worker v3...");

    event.waitUntil(
        caches.keys()
            .then((cacheNames) =>
                Promise.all(
                    cacheNames
                        .filter((name) => name !== CACHE_NAME)
                        .map((name) => {
                            console.log("[SW] Deleting old cache:", name);
                            return caches.delete(name);
                        })
                )
            )
            .then(() => {
                console.log("[SW] Now controlling all clients.");
                return self.clients.claim();
            })
    );
});

// ---------------------------------------------------------------------------
// Push – show a notification
// ---------------------------------------------------------------------------

self.addEventListener("push", (event) => {
    console.log("[SW] Push received:", event);

    if (!event.data) {
        console.warn("[SW] Push event had no data – skipping.");
        return;
    }

    let payload;
    try {
        payload = event.data.json();
    } catch (err) {
        console.error("[SW] Failed to parse push payload as JSON:", err);
        payload = { title: "Family Dashboard", body: event.data.text(), url: "/board" };
    }

    const title = payload.title || "Family Dashboard";
    const options = {
        body:    payload.body  || "You have a new update.",
        icon:    "/static/icons/icon-192.png",
        badge:   "/static/icons/icon-192.png",
        data:    { url: payload.url || "/board" },
        // Use a tag so duplicate notifications for the same announcement are collapsed.
        tag:     `announcement-${payload.id || Date.now()}`,
        // Vibrate pattern for mobile devices [ms on, ms off, ms on].
        vibrate: [200, 100, 200],
        // Keep the notification visible until the user interacts.
        requireInteraction: false,
    };

    event.waitUntil(
        self.registration.showNotification(title, options)
    );
});

// ---------------------------------------------------------------------------
// Notification click – navigate to the linked page
// ---------------------------------------------------------------------------

self.addEventListener("notificationclick", (event) => {
    event.notification.close();
    const targetUrl = (event.notification.data && event.notification.data.url) || "/board";

    event.waitUntil(
        clients.matchAll({ type: "window", includeUncontrolled: true })
            .then((clientList) => {
                // If a window is already open at our origin, focus it and navigate.
                for (const client of clientList) {
                    if ("focus" in client) {
                        return client.focus().then(() => client.navigate(targetUrl));
                    }
                }
                // No existing window – open a new one.
                return clients.openWindow(targetUrl);
            })
    );
});

// ---------------------------------------------------------------------------
// Fetch – network-first with graceful offline fallback
// ---------------------------------------------------------------------------

self.addEventListener("fetch", (event) => {
    // Only intercept GET requests.
    if (event.request.method !== "GET") return;

    // Never intercept push API calls or browser extension requests.
    const url = new URL(event.request.url);
    if (url.pathname.startsWith("/api/")) return;

    event.respondWith(
        fetch(event.request)
            .then((response) => {
                // Cache successful responses for future offline use.
                if (response && response.status === 200) {
                    const responseClone = response.clone();
                    caches.open(CACHE_NAME).then((cache) => {
                        cache.put(event.request, responseClone);
                    });
                }
                return response;
            })
            .catch(() => {
                // Network failed – try cache, then fall back to /offline.
                return caches.match(event.request).then((cached) => {
                    if (cached) return cached;
                    return caches.match("/offline");
                });
            })
    );
});

// ---------------------------------------------------------------------------
// Message – allow pages to send commands to the SW
// ---------------------------------------------------------------------------

self.addEventListener("message", (event) => {
    if (event.data === "SKIP_WAITING") {
        self.skipWaiting();
    }
});