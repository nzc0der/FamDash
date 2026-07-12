const CACHE_NAME = "family-dashboard-v1";

const PAGES_TO_CACHE = [
    "/",
    "/login",
    "/dashboard",
    "/notes",
    "/board",
    "/calendar",
    "/shopping",
    "/admin"
];


// Install service worker
self.addEventListener("install", (event) => {
    console.log("Service Worker installing...");

    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(PAGES_TO_CACHE);
        })
    );

    self.skipWaiting();
});


// Remove old cache versions
self.addEventListener("activate", (event) => {
    console.log("Service Worker activated...");

    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames
                    .filter((name) => name !== CACHE_NAME)
                    .map((name) => caches.delete(name))
            );
        })
    );

    self.clients.claim();
});


// Network first, cache fallback
self.addEventListener("fetch", (event) => {

    // Only handle GET requests
    if (event.request.method !== "GET") {
        return;
    }

    event.respondWith(
        fetch(event.request)
            .then((response) => {

                // Save newest version
                const responseClone = response.clone();

                caches.open(CACHE_NAME).then((cache) => {
                    cache.put(event.request, responseClone);
                });

                return response;
            })
            .catch(() => {

                // Offline fallback
                return caches.match(event.request);
            })
    );
});


// Allow instant updates
self.addEventListener("message", (event) => {
    if (event.data === "SKIP_WAITING") {
        self.skipWaiting();
    }
});