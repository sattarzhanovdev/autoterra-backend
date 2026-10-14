document.addEventListener('DOMContentLoaded', () => {
    const client = document.getElementById('id_client');
    const store = document.getElementById('id_store');
    const config = document.getElementById('order-stores-config');
    if (!client || !store || !config) return;
    let pending;
    client.addEventListener('change', async () => {
        if (pending) pending.abort();
        pending = new AbortController();
        store.replaceChildren(new Option('---------', ''));
        store.dispatchEvent(new Event('change', {bubbles: true}));
        try {
            const url = new URL(config.dataset.url, window.location.origin);
            url.searchParams.set('client', client.value);
            const response = await fetch(url, {signal: pending.signal, credentials: 'same-origin'});
            if (!response.ok) return;
            const data = await response.json();
            for (const item of data.stores) store.add(new Option(item.name, item.id));
        } catch (error) {
            if (error.name !== 'AbortError') console.error('Не удалось загрузить магазины клиента');
        }
    });
});
